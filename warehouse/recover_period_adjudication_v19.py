#!/usr/bin/env python3
"""Apply scoped final-score adjudications and keyed line-score cells to v18."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, scalar_count, sql_string, verify
from recover_archive_periods import sha256
from recover_legacy_attempts_v18 import verify_inherited
from recover_local_periods import difference, snapshot
from recover_pbp_attempts import open_config

PARENT_SHA = '852ed297ed378b886b98f54f2f76bb499a621b2b12d56fef393cf358132b2802'
RECEIPT_SHA = '0f7905589ead7035bab9a8294dbd522f48ad1f5c0446f4a6cb1c46c959044786'
PINS = [('parent_v18',PARENT_SHA),('printed_receipts',RECEIPT_SHA)]
UNCHANGED = (
    'nba.dim_game','nba.bridge_game_team','nba.fact_regular_standings','nba.fact_team_game_stats',
    'nba.fact_player_game_stats','nba.fact_shot','nba.bridge_shot_event','nba.fact_pbp_field_goal_attempt',
    'audit.game_disposition','audit.local_period_recovery','audit.mirror_v13_assessment','audit.mirror_v14_assessment',
    'audit.playoff_ot_schedule','audit.playoff_ot_schedule_pin',
)


def read_receipts(c: duckdb.DuckDBPyConnection,parent: Path,receipts: Path) -> None:
    c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    c.execute('''CREATE TEMP TABLE reviewed_v19 AS
      SELECT a.game_id,a.game_date::DATE AS game_date,a.home_id::BIGINT AS home_id,a.away_id::BIGINT AS away_id,
        a.old_home::INT AS old_home,a.old_away::INT AS old_away,
        a.page_home_final::INT AS page_home_final,a.page_away_final::INT AS page_away_final,
        a.new_home::INT AS new_home,a.new_away::INT AS new_away,
        string_split(a.home_cells,'|')::INTEGER[4] AS home_cells,string_split(a.away_cells,'|')::INTEGER[4] AS away_cells,
        a.nba_page,a.comparison_locator,a.comparison_scope,p.status AS old_period_status,p.candidate_sources AS old_candidates
      FROM read_csv(?,header=true,all_varchar=true) a LEFT JOIN prior.audit.period_coverage p USING(game_id)''',[str(receipts)])
    if scalar_count(c,'''SELECT count(*) FROM reviewed_v19 a JOIN prior.nba.fact_game g USING(game_id)
      WHERE (a.game_date,a.home_id,a.away_id,a.old_home,a.old_away)=(g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
      AND g.result_status='final' AND (a.old_period_status IN ('no_supported_source','no_valid_candidate'))
      AND (g.season_type='Playoffs' OR (a.old_home,a.old_away)=(a.new_home,a.new_away))''')!=5:
        raise RuntimeError('A printed receipt does not match the parent fixture, old final and hold')
    if scalar_count(c,'SELECT count(*) FROM prior.nba.fact_game_period_score SEMI JOIN reviewed_v19 USING(game_id)'):
        raise RuntimeError('Existing periods cannot be superseded by a header-conflict receipt')


def replay(c: duckdb.DuckDBPyConnection,parent: Path,receipts: Path) -> int:
    read_receipts(c,parent,receipts)
    if c.execute('SELECT * FROM audit.period_v19_input ORDER BY role').fetchall()!=PINS:
        raise RuntimeError('Recorded v19 input pins differ')
    if difference(c,'SELECT * FROM audit.period_adjudication_v19','SELECT * FROM reviewed_v19'):
        raise RuntimeError('Exact line-score header receipt differs from pinned transcription')
    for relation in UNCHANGED:
        verify_inherited(c,relation)
    for relation in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline','nba.game_overtime'):
        scope = 'ANTI JOIN reviewed_v19 USING(game_id)'
        if difference(c,f'SELECT * FROM {relation} {scope}',f'SELECT * FROM prior.{relation} {scope}'):
            raise RuntimeError(f'Period delta escaped its exact scope: {relation}')
    if difference(c,'SELECT * FROM nba.fact_game_result','''SELECT p.* REPLACE (
            coalesce(r.new_home,p.home_score) AS home_score,coalesce(r.new_away,p.away_score) AS away_score)
            FROM prior.nba.fact_game_result p LEFT JOIN reviewed_v19 r USING(game_id)'''):
        raise RuntimeError('Final result differs from exact parent plus adjudicated score delta')
    return verify(c)


def preserve_v13_assessment_context(c: duckdb.DuckDBPyConnection) -> None:
    # The legacy assessment remains a live derivation, but from the same finals
    # that were available when that evidence was evaluated.
    row = c.execute("""SELECT sql FROM duckdb_views() WHERE database_name=current_database()
                       AND schema_name='audit' AND view_name='mirror_v13_assessment'""").fetchone()
    if row is None or not isinstance(row[0],str) or row[0].count('nba.fact_game AS g')!=1:
        raise RuntimeError('Unrecognized v13 assessment definition; refusing to rebase it')
    c.execute('''CREATE VIEW audit.mirror_v13_game_context_v19 AS
      SELECT g.* REPLACE (coalesce(r.old_home,g.home_score) AS home_score,coalesce(r.old_away,g.away_score) AS away_score)
      FROM nba.fact_game g LEFT JOIN audit.period_adjudication_v19 r USING(game_id)''')
    c.execute(row[0].replace('CREATE VIEW audit.mirror_v13_assessment',
                              'CREATE OR REPLACE VIEW audit.mirror_v13_assessment',1)
              .replace('nba.fact_game AS g','audit.mirror_v13_game_context_v19 AS g',1))


def run(action: str,parent: Path,receipts: Path,output: Path) -> None:
    if action=='build' and (os.path.lexists(output) or os.path.lexists(f'{output}.wal')):
        raise FileExistsError(f'Will not overwrite {output} or its WAL')
    paths = (parent,receipts) if action=='build' else (parent,receipts,output)
    initial = tuple(map(snapshot,paths))
    if sha256(parent)!=PARENT_SHA or sha256(receipts)!=RECEIPT_SHA:
        raise RuntimeError('Parent or exact line-score transcription does not match its v19 input pin')
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-v19-spill-') as spill:
        if action=='verify-inputs':
            with duckdb.connect(str(output),read_only=True,config=open_config(spill)) as c:
                checks = replay(c,parent,receipts)
        else:
            with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v19-') as work:
                candidate = Path(work)/'release.duckdb'
                shutil.copyfile(parent,candidate)
                with duckdb.connect(str(candidate),config=open_config(spill)) as c:
                    read_receipts(c,parent,receipts)
                    c.begin()
                    c.execute((WAREHOUSE/'period_adjudication_v19.sql').read_text())
                    preserve_v13_assessment_context(c)
                    c.execute('''
                        INSERT INTO audit.period_adjudication_v19 SELECT * FROM reviewed_v19;
                        UPDATE nba.fact_game_result g SET home_score=r.new_home,away_score=r.new_away
                        FROM reviewed_v19 r WHERE g.game_id=r.game_id AND (r.old_home,r.old_away)<>(r.new_home,r.new_away);
                        INSERT INTO nba.fact_game_period_score SELECT * FROM audit.period_cells_v19;
                        INSERT INTO audit.period_vector_baseline
                        SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
                            ORDER BY team_id,period_number))) FROM audit.period_cells_v19 GROUP BY game_id;
                        UPDATE audit.period_coverage g SET status='published',candidate_sources=r.old_candidates+1,valid_sources=1,
                            selected_source='official_v19_header_disagrees' FROM reviewed_v19 r WHERE g.game_id=r.game_id;
                    ''')
                    c.executemany('INSERT INTO audit.period_v19_input VALUES (?,?)',PINS)
                    c.commit()
                    c.execute('CHECKPOINT')
                snapshot(candidate)
                with duckdb.connect(str(candidate),read_only=True,config=open_config(spill)) as c:
                    checks = replay(c,parent,receipts)
                if tuple(map(snapshot,paths))!=initial or os.path.lexists(f'{output}.wal'):
                    raise RuntimeError('Input changed or destination WAL exists; not installing')
                os.link(candidate,output)
    if tuple(map(snapshot,paths))!=initial:
        raise RuntimeError('An input changed during verification')
    print(f'{checks} warehouse checks passed; exact adjudications and {len(UNCHANGED)+5} parent relation comparisons verified: {output}')


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v18.duckdb')
    parser.add_argument('--receipts',type=Path,default=ROOT/'data/evidence/nba-linescore-header-v19.csv')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    run(args.action,args.parent.resolve(strict=True),args.receipts.resolve(strict=True),args.output.absolute())


if __name__=='__main__':
    main()
