#!/usr/bin/env python3
"""Accession bounded historical box-score gaps from the supplied local snapshot."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, sql_string, verify, verify_source_accounting
from recover_archive_periods import sha256
from recover_local_periods import difference, snapshot
from recover_pbp_attempts import open_config

PARENT_SHA = 'ae87c35b188bf1c0f49a14dc751805feb5fcfeb7e5b176ca121bdb8f1002fdf1'
SOURCE_SHA = 'ee2ae0360fcbc9bc9c96e6fc9575cdaa661641f01bb4cbfa99d67dad8bf774b8'
PINS = [('parent_v16', PARENT_SHA), ('raw_snapshot', SOURCE_SHA)]
TEAM_GAMES = (
    '0022400147','0022400621','0022400633','0022401229','0022401230',
    '0022500147','0022500578','0022500602','0022501229','0022501230',
)
PLAYER_DISPOSITIONS = [
    ('0030300001','held_conflicting_minutes',24), ('0030300001','held_unknown_person_placeholder',2),
    ('0030400001','published_traditional',24), ('0035100001','published_traditional',20),
    ('0035200001','published_traditional',20), ('0039900001','held_unknown_person_placeholder',2),
    ('0039900001','published_traditional',24),
]
LEDGERS = (
    ('team_box_recovery_v17','team_observation_v17'),
    ('team_box_publication_v17','team_publication_v17'),
    ('player_box_recovery_v17','player_observation_v17'),
    ('player_box_publication_v17','player_publication_v17'),
    ('player_box_availability_v17','player_availability_v17'),
)
UNCHANGED = (
    'nba.dim_game','nba.dim_person','nba.bridge_game_team','nba.fact_game_result','nba.fact_regular_standings',
    'nba.fact_game_period_score','audit.period_coverage','nba.fact_pbp_field_goal_attempt',
    'audit.team_box_disposition','audit.player_box_disposition','audit.input_manifest',
)


def derive(c: duckdb.DuckDBPyConnection, parent: Path, source: Path) -> None:
    c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    c.execute(f'ATTACH {sql_string(str(source))} AS src (READ_ONLY)')
    c.execute((WAREHOUSE/'recover_boxes_v17.sql').read_text())
    if c.execute('SELECT game_id FROM team_scope_v17 ORDER BY game_id').fetchall() != [(g,) for g in TEAM_GAMES]:
        raise RuntimeError('Modern team-box gap scope differs from the admitted fixture list')
    expected = 'SELECT b.game_id,b.team_id FROM prior.nba.bridge_game_team b SEMI JOIN team_scope_v17 USING(game_id)'
    if (difference(c,'SELECT game_id,team_id FROM team_observation_v17 WHERE valid',expected)
        or difference(c,'SELECT game_id,team_id FROM team_observation_v17',expected)
        or difference(c,'SELECT game_id,team_id FROM team_publication_v17',expected)):
        raise RuntimeError('Team-box evidence fails exact participant, value or multiplicity checks')
    observed = c.execute('''SELECT game_id,disposition,count(*) FROM player_observation_v17
                            GROUP BY ALL ORDER BY 1,2''').fetchall()
    if observed != PLAYER_DISPOSITIONS:
        raise RuntimeError(f'Unexpected All-Star player box dispositions: {observed}')
    if difference(c,'SELECT game_id,person_id FROM player_publication_v17',
                  "SELECT game_id,person_id FROM player_observation_v17 WHERE disposition='published_traditional'"):
        raise RuntimeError('Player publication does not match admission keys')


def replay(c: duckdb.DuckDBPyConnection, parent: Path, source: Path) -> int:
    derive(c,parent,source)
    if c.execute('SELECT role,sha256 FROM audit.box_recovery_input_v17 ORDER BY role').fetchall() != PINS:
        raise RuntimeError('V17 recorded input pins differ')
    for ledger, expected in LEDGERS:
        if difference(c,f'SELECT * FROM audit.{ledger}',f'SELECT * FROM {expected}'):
            raise RuntimeError(f'Exact input replay differs: audit.{ledger}')
    for fact, delta in (('fact_team_game_stats','team_publication_v17'),('fact_player_game_stats','player_publication_v17')):
        if difference(c,f'SELECT * FROM nba.{fact}',
                      f'SELECT * FROM (SELECT * FROM prior.nba.{fact} UNION ALL SELECT * FROM {delta})'):
            raise RuntimeError(f'Published fact differs from parent plus exact input delta: nba.{fact}')
    for relation in UNCHANGED:
        if difference(c,f'SELECT * FROM {relation}',f'SELECT * FROM prior.{relation}'):
            raise RuntimeError(f'Inherited relation changed: {relation}')
    verify_source_accounting(c)
    return verify(c)


def pinned_inputs(parent: Path, source: Path) -> None:
    if sha256(parent)!=PARENT_SHA or sha256(source)!=SOURCE_SHA:
        raise RuntimeError('The supplied source or parent bytes differ from the pinned v17 inputs')


def run(action: str, parent: Path, source: Path, output: Path) -> None:
    if action=='build' and (os.path.lexists(output) or os.path.lexists(f'{output}.wal')):
        raise FileExistsError(f'Will not overwrite {output} or its WAL')
    inputs = (parent,source) if action=='build' else (parent,source,output)
    initial = tuple(map(snapshot,inputs))
    pinned_inputs(parent,source)
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-v17-spill-') as spill:
        if action=='verify-inputs':
            with duckdb.connect(str(output),read_only=True,config=open_config(spill)) as c:
                checks = replay(c,parent,source)
        else:
            with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v17-') as work:
                candidate = Path(work)/'release.duckdb'
                shutil.copyfile(parent,candidate)
                with duckdb.connect(str(candidate),config=open_config(spill)) as c:
                    derive(c,parent,source)
                    c.begin()
                    c.execute('CREATE TABLE audit.box_recovery_input_v17(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL)')
                    c.executemany('INSERT INTO audit.box_recovery_input_v17 VALUES (?,?)',PINS)
                    for ledger, derived in LEDGERS:
                        c.execute(f'CREATE TABLE audit.{ledger} AS SELECT * FROM {derived}')
                    c.execute('INSERT INTO nba.fact_team_game_stats SELECT * FROM team_publication_v17')
                    c.execute('INSERT INTO nba.fact_player_game_stats SELECT * FROM player_publication_v17')
                    c.commit()
                    c.execute('CHECKPOINT')
                snapshot(candidate)
                with duckdb.connect(str(candidate),read_only=True,config=open_config(spill)) as c:
                    checks = replay(c,parent,source)
                if tuple(map(snapshot,inputs))!=initial or os.path.lexists(f'{output}.wal'):
                    raise RuntimeError('An input changed or destination WAL exists; not installing')
                os.link(candidate,output)
    if tuple(map(snapshot,inputs))!=initial:
        raise RuntimeError('An input changed during verification')
    print(f'{checks} warehouse checks passed; {len(LEDGERS)+len(UNCHANGED)+2} exact relations replayed: {output}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v16.duckdb')
    parser.add_argument('--source',type=Path,default=ROOT/'data/nba.duckdb')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    run(args.action,args.parent.resolve(strict=True),args.source.resolve(strict=True),args.output.absolute())


if __name__=='__main__':
    main()
