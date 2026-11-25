#!/usr/bin/env python3
"""Build and replay 2000-01 legacy NBA field-goal attempts without chart geometry."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, scalar_count, sql_string, verify
from recover_archive_periods import sha256
from recover_boxes_v17 import SOURCE_SHA
from recover_local_periods import difference, snapshot
from recover_pbp_attempts import open_config

PARENT_SHA = '0ac7927f3b47442be307572cd6d404efb059c1127851bf4cf93e38c383760cbb'
PINS = [('parent_v17',PARENT_SHA),('raw_snapshot',SOURCE_SHA)]
SCOPE = ('0020000374','0020000375','0020000376','0020000377','0020000378','0020000379',
         '0020000380','0020000381','0020000382','0020000383','0020000389')
UNCHANGED = (
    'nba.dim_game','nba.fact_game_result','nba.bridge_game_team','nba.fact_regular_standings',
    'nba.fact_game_period_score','audit.period_coverage','nba.fact_team_game_stats','nba.fact_player_game_stats',
    'nba.fact_shot','nba.bridge_shot_event','audit.shot_coverage','audit.pbp_attempt_scope',
    'audit.pbp_attempt_input','audit.pbp_attempt_coverage',
)
LARGE = frozenset(('nba.fact_shot','nba.bridge_shot_event','nba.fact_player_game_stats','nba.fact_play_event'))


def verify_inherited(c: duckdb.DuckDBPyConnection, relation: str) -> None:
    # The full chart multiset exceeds the verification memory budget. Partition
    # both sides by the same deterministic key expression, including nulls.
    base, parent = f'SELECT * FROM {relation}',f'SELECT * FROM prior.{relation}'
    if relation not in LARGE:
        if difference(c,base,parent):
            raise RuntimeError(f'Inherited relation changed: {relation}')
        return
    season_suffix = "coalesce(substr(game_id,4,2),'')"
    groups = c.execute(f'SELECT DISTINCT {season_suffix} FROM ({base} UNION ALL {parent}) ORDER BY 1').fetchall()
    for (group,) in groups:
        predicate = f' WHERE {season_suffix}={sql_string(group)}'
        if difference(c,base+predicate,parent+predicate):
            raise RuntimeError(f'Inherited relation changed: {relation}, era suffix {group!r}')


def verify_inputs(c: duckdb.DuckDBPyConnection, parent: Path, source: Path) -> int:
    c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    c.execute(f'ATTACH {sql_string(str(source))} AS src (READ_ONLY)')
    checks = verify(c)
    scope = 'SEMI JOIN audit.legacy_attempt_scope_v18 USING(game_id)'
    if c.execute('SELECT game_id FROM audit.legacy_attempt_scope_v18 ORDER BY 1').fetchall()!=[(g,) for g in SCOPE]:
        raise RuntimeError('Legacy chart gap keys differ from the reviewed 2000-01 regular games')
    if c.execute('SELECT role,sha256 FROM audit.legacy_attempt_input_v18 ORDER BY 1').fetchall()!=PINS:
        raise RuntimeError('Legacy attempt input pins differ from the reviewed snapshots')
    if scalar_count(c,f'SELECT count(*) FROM src.main.fact_shot_chart {scope}')!=0:
        raise RuntimeError('Raw shot-chart observations already exist in the proposed cohort')
    raw = f'SELECT * FROM src.main.fact_pbp_events {scope}'
    if scalar_count(c,f'SELECT count(*) FROM ({raw})')!=4908 or difference(c,
            f'SELECT game_id,action_number FROM ({raw})',
            f'SELECT game_id,action_number FROM nba.fact_play_event {scope}'):
        raise RuntimeError('Curated event grain differs from the exact original action grain')
    if difference(c,
        f'''SELECT game_id,action_number,team_id,player_id,period,clock,shot_value,
                   CASE shot_result WHEN 'Made' THEN true WHEN 'Missed' THEN false END,action_type
            FROM ({raw}) WHERE is_field_goal IS TRUE''',
        '''SELECT game_id,action_number,team_id,person_id,period,clock_iso,shot_value,made,source_action
           FROM legacy_attempt_expected'''):
        raise RuntimeError('Exact legacy field-goal observations differ from original encoded action rows')
    if difference(c,f'''SELECT game_id,action_number,period,clock,action_type,sub_type,score_home,score_away FROM ({raw})
                         WHERE action_type='period' AND sub_type IN ('start','end')''',
                    f'''SELECT game_id,action_number,period,clock_iso,source_action,source_subtype,
                               reported_home_score,reported_away_score FROM nba.fact_play_event {scope}
                         WHERE source_action='period' AND source_subtype IN ('start','end')'''):
        raise RuntimeError('Exact legacy period markers differ from original action rows')
    if difference(c,f'SELECT * FROM nba.fact_play_event {scope}',f'SELECT * FROM prior.nba.fact_play_event {scope}'):
        raise RuntimeError('Inherited curated legacy event rows changed')
    for relation in UNCHANGED:
        verify_inherited(c,relation)
    if difference(c,'SELECT * FROM nba.fact_pbp_field_goal_attempt',
                  'SELECT * FROM (SELECT * FROM prior.nba.fact_pbp_field_goal_attempt UNION ALL SELECT * FROM legacy_attempt_expected)'):
        raise RuntimeError('Published attempts differ from exact parent plus legacy delta')
    return checks


def run(action: str,parent: Path,source: Path,output: Path) -> None:
    if action=='build' and (os.path.lexists(output) or os.path.lexists(f'{output}.wal')):
        raise FileExistsError(f'Will not overwrite {output} or its WAL')
    paths = (parent,source) if action=='build' else (parent,source,output)
    initial = tuple(map(snapshot,paths))
    if sha256(parent)!=PARENT_SHA or sha256(source)!=SOURCE_SHA:
        raise RuntimeError('Legacy attempt inputs do not match the pinned v17 and raw database bytes')
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-v18-spill-') as spill:
        if action=='verify-inputs':
            with duckdb.connect(str(output),read_only=True,config=open_config(spill)) as c:
                checks = verify_inputs(c,parent,source)
        else:
            with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v18-') as work:
                candidate = Path(work)/'release.duckdb'
                shutil.copyfile(parent,candidate)
                with duckdb.connect(str(candidate),config=open_config(spill)) as c:
                    c.begin()
                    c.execute((WAREHOUSE/'legacy_attempts_v18.sql').read_text())
                    c.execute('''CREATE TABLE audit.legacy_attempt_scope_v18(game_id VARCHAR PRIMARY KEY);
                                 INSERT INTO audit.legacy_attempt_scope_v18 SELECT * FROM legacy_attempt_scope_expected;
                                 CREATE TABLE audit.legacy_attempt_input_v18(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);
                                 INSERT INTO nba.fact_pbp_field_goal_attempt SELECT * FROM legacy_attempt_expected;''')
                    c.executemany('INSERT INTO audit.legacy_attempt_input_v18 VALUES (?,?)',PINS)
                    c.commit()
                    c.execute('CHECKPOINT')
                snapshot(candidate)
                with duckdb.connect(str(candidate),read_only=True,config=open_config(spill)) as c:
                    checks = verify_inputs(c,parent,source)
                if tuple(map(snapshot,paths))!=initial or os.path.lexists(f'{output}.wal'):
                    raise RuntimeError('Input changed or destination WAL exists; not installing')
                os.link(candidate,output)
    if tuple(map(snapshot,paths))!=initial:
        raise RuntimeError('An input changed during verification')
    print(f'{checks} warehouse checks passed; raw attempt and period cells, exact delta, and {len(UNCHANGED)} inherited relations verified: {output}')


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v17.duckdb')
    parser.add_argument('--source',type=Path,default=ROOT/'data/nba.duckdb')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    run(args.action,args.parent.resolve(strict=True),args.source.resolve(strict=True),args.output.absolute())


if __name__=='__main__':
    main()
