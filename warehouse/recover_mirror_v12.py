"""Recover historical periods with source-row accounting and six keyed NBA receipts."""
from __future__ import annotations

import argparse
from datetime import date, datetime
from hashlib import sha256 as digest_bytes
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import NamedTuple
from zoneinfo import ZoneInfo

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, sql_string, verify
from recover_archive_periods import sha256
from recover_local_periods import difference, snapshot
from validate_current_results import csv_rows, unique_index


DEFAULT_RECEIPTS = WAREHOUSE / 'mirror_ot_official_receipts.csv'
SOURCE_ROWS = '''
SELECT game_id,team_id,opponent_team_id,game_date,season_year,game_type,is_home,pts,
       [q1_points,q2_points,q3_points,q4_points],[ot1_points,ot2_points],ot_all_points
FROM src.main.stg_kaggle_team_box m
WHERE EXISTS (SELECT 1 FROM audit.mirror_v12_scope s WHERE s.game_id=m.game_id)
   OR (m.season_year IN ('1960-61','1961-62','1966-67','1970-71') AND m.game_type='Regular Season')
'''
PARENT_ROWS = '''
SELECT p.game_id,p.status,p.candidate_sources,p.valid_sources FROM prior.audit.period_coverage p
SEMI JOIN audit.mirror_v12_scope s USING(game_id)
'''
EXPECTED_COHORTS = [(1960,316,303),(1961,360,345),(1966,405,405),(1970,697,697)]


class Official(NamedTuple):
    game_id: str
    game_date: date
    home_id: int
    away_id: int
    home_periods: tuple[int,...]
    away_periods: tuple[int,...]
    source_url: str
    capture_sha256: str


def read_official_receipts(path: Path, captures: Path | None) -> list[Official]:
    rows = unique_index(csv_rows(path), 'game_id')
    result = []
    for gid, r in rows.items():
        h, a = [tuple(map(int, r[c].split('|'))) for c in ('home_periods','away_periods')]
        if (not re.fullmatch(r'00[24][0-9]{7}', gid) or len(h) not in (5,6) or len(h)!=len(a)
            or min(*h,*a)<0 or any(sum(h[:i])!=sum(a[:i]) for i in range(4,len(h))) or sum(h)==sum(a)
            or r['source_url']!=f'https://www.nba.com/game/{gid}' or not re.fullmatch('[a-f0-9]{64}',r['capture_sha256'])):
            raise ValueError(f'Invalid keyed overtime receipt: {gid}')
        receipt = Official(gid,date.fromisoformat(r['game_date']),int(r['home_id']),int(r['away_id']),h,a,
                           r['source_url'],r['capture_sha256'])
        if captures is not None:
            verify_capture(captures / f'nba_{gid}.html', receipt)
        result.append(receipt)
    if len(result)!=6:
        raise ValueError('Expected six keyed NBA overtime captures')
    return result


def verify_capture(path: Path, receipt: Official) -> None:
    raw = path.read_bytes()
    if digest_bytes(raw).hexdigest()!=receipt.capture_sha256:
        raise ValueError(f'NBA HTML capture digest differs: {receipt.game_id}')
    script = re.search(rb'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
    if script is None:
        raise ValueError(f'NBA HTML capture lacks keyed game data: {receipt.game_id}')
    g = json.loads(script[1])['props']['pageProps']['game']
    local_day = datetime.fromisoformat(g['gameTimeUTC']).astimezone(ZoneInfo('America/New_York')).date()
    if (g['gameId'],local_day,g['gameStatus'],g['period'])!=(receipt.game_id,receipt.game_date,3,len(receipt.home_periods)):
        raise ValueError(f'NBA HTML capture identity or period count differs: {receipt.game_id}')
    for side, team_id, vector in (('homeTeam',receipt.home_id,receipt.home_periods),
                                  ('awayTeam',receipt.away_id,receipt.away_periods)):
        team = g[side]
        observed = [(p['period'],p['periodType'],p['score']) for p in team['periods']]
        expected = [(i,'REGULAR' if i<=4 else 'OVERTIME',score) for i,score in enumerate(vector,1)]
        if (team['teamId'],team['score'],observed)!=(team_id,sum(vector),expected):
            raise ValueError(f'NBA HTML capture team or period cells differ: {receipt.game_id} {side}')


def manifest(parent: Path, source: Path, receipts: Path) -> list[tuple[str,str,str,bool]]:
    return [('keyed_ot_receipts',sha256(receipts),'Six transcribed NBA line-score captures',False),
            ('parent_v11',sha256(parent),'nba-clean-v11',False),
            ('raw',sha256(source),'main.stg_kaggle_team_box (kaggle_nba, derivative)',False)]


def attach(con: duckdb.DuckDBPyConnection, parent: Path, source: Path) -> None:
    con.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    con.execute(f'ATTACH {sql_string(str(source))} AS src (READ_ONLY)')


def verify_inputs(parent: Path, source: Path, receipts: Path, release: Path, captures: Path | None = None) -> None:
    immutable = parent,source,receipts,release
    initial = tuple(map(snapshot,immutable))
    with duckdb.connect(str(release),read_only=True,config={'threads':2,'memory_limit':'2GB'}) as con:
        attach(con,parent,source)
        if con.execute('SELECT * FROM audit.mirror_v12_input ORDER BY role').fetchall()!=manifest(parent,source,receipts):
            raise RuntimeError('Read-only raw, parent or official receipt digest differs')
        con.execute('CREATE TEMP TABLE keyed_expected AS SELECT * FROM audit.mirror_v12_official LIMIT 0')
        con.executemany('INSERT INTO keyed_expected VALUES (?,?,?,?,?,?,?,?)',read_official_receipts(receipts,captures))
        for observed, expected in (('audit.mirror_v12_prior',PARENT_ROWS),('audit.mirror_v12_team',SOURCE_ROWS),
                                   ('audit.mirror_v12_official','SELECT * FROM keyed_expected')):
            if difference(con,f'SELECT * FROM {observed}',expected):
                raise RuntimeError(f'Exact read-only input differs from ledger: {observed}')
        for name in ('nba.dim_game','nba.dim_team','nba.fact_game_result','nba.bridge_game_team',
                     'nba.bridge_event_fixture','nba.fact_regular_standings'):
            if difference(con,f'SELECT * FROM {name}',f'SELECT * FROM prior.{name}'):
                raise RuntimeError(f'Prior spine, dimensions or results changed: {name}')
        anti_scope = 'ANTI JOIN audit.mirror_v12_scope USING(game_id)'
        for name in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline','nba.game_overtime'):
            if difference(con,f'SELECT * FROM {name} {anti_scope}',f'SELECT * FROM prior.{name} {anti_scope}'):
                raise RuntimeError(f'Non-scoped rows changed: {name}')
        counts = con.execute('''SELECT season_start,count(*),count(*) FILTER(WHERE disposition='admitted')
          FROM audit.mirror_v12_assessment a JOIN nba.dim_game USING(game_id)
          WHERE cohort='legacy_regular' GROUP BY 1 ORDER BY 1''').fetchall()
        if counts!=EXPECTED_COHORTS:
            raise RuntimeError(f'Unexpected legacy cohort or disposition counts: {counts}')
        checks = verify(con)
        assessment = con.execute('''SELECT season_year,disposition,count(*) FROM audit.mirror_v12_assessment
                                 GROUP BY ALL ORDER BY ALL''').fetchall()
    if tuple(map(snapshot,immutable))!=initial:
        raise RuntimeError('An input changed during verification')
    print(f'{checks} checks passed; exact attached inputs and parent scope verified; assessment: {assessment}')
    if captures is not None:
        print('Six source HTML captures matched pinned digests, keyed finals and exact period vectors')


def build(parent: Path, source: Path, receipts: Path, output: Path, captures: Path | None = None) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    immutable = parent,source,receipts
    initial = tuple(map(snapshot,immutable))
    pins = manifest(parent,source,receipts)
    official = read_official_receipts(receipts,captures)
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.mirror-v12-') as stage_dir, \
         tempfile.TemporaryDirectory(dir=SCRATCH,prefix='mirror-v12-spill-') as spill:
        stage = Path(stage_dir)/'release.duckdb'
        shutil.copyfile(parent,stage)
        with duckdb.connect(str(stage),config={'threads':2,'memory_limit':'2GB','temp_directory':spill}) as con:
            attach(con,parent,source)
            if con.execute("SELECT sha256 FROM prior.audit.mirror_period_input WHERE role='source'").fetchall()!=[(pins[2][1],)]:
                raise RuntimeError('Raw input differs from v11 SHA-256')
            con.begin()
            con.execute((WAREHOUSE/'mirror_v12.sql').read_text())
            con.execute('INSERT INTO audit.mirror_v12_prior '+PARENT_ROWS)
            con.execute('INSERT INTO audit.mirror_v12_team '+SOURCE_ROWS)
            con.executemany('INSERT INTO audit.mirror_v12_official VALUES (?,?,?,?,?,?,?,?)',official)
            con.executemany('INSERT INTO audit.mirror_v12_input VALUES (?,?,?,?)',pins)
            con.execute('''
                INSERT INTO nba.fact_game_period_score SELECT * FROM audit.mirror_v12_cells;
                INSERT INTO audit.period_vector_baseline
                SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
                       ORDER BY team_id,period_number))) FROM audit.mirror_v12_cells GROUP BY game_id;
                UPDATE audit.period_coverage p SET status=c.status,selected_source=c.selected_source,
                      candidate_sources=c.candidate_sources,valid_sources=c.valid_sources
                FROM audit.mirror_v12_coverage c WHERE p.game_id=c.game_id;
                CREATE VIEW audit.period_availability_v12 AS
                SELECT v.* REPLACE (coalesce(c.selected_source,c.disposition,v.recovery_disposition) AS recovery_disposition)
                FROM audit.period_availability_v11 v LEFT JOIN audit.mirror_v12_coverage c USING(game_id);
            ''')
            con.commit()
            con.execute('CHECKPOINT')
        verify_inputs(parent,source,receipts,stage,captures)
        if tuple(map(snapshot,immutable))!=initial or os.path.lexists(f'{output}.wal'):
            raise RuntimeError('An input changed or a destination WAL exists; not installing')
        os.link(stage,output)
        print(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v11.duckdb')
    parser.add_argument('--source',type=Path,default=ROOT/'data/nba.duckdb')
    parser.add_argument('--receipts',type=Path,default=DEFAULT_RECEIPTS)
    parser.add_argument('--capture-dir',type=Path,help='Optional verification against archived NBA html files')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    parent,source,receipts = (getattr(args,k).resolve(strict=True) for k in ('parent','source','receipts'))
    if args.action=='build':
        build(parent,source,receipts,args.output.absolute(),args.capture_dir)
    else:
        verify_inputs(parent,source,receipts,args.output.resolve(strict=True),args.capture_dir)


if __name__=='__main__':
    main()
