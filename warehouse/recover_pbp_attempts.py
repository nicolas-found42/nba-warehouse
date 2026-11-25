"""Add box-reconciled NBA play-by-play attempts without rewriting shot charts."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, sql_string, verify
from recover_archive_periods import sha256
from recover_local_periods import difference, snapshot


SCOPE = '''SELECT g.game_id FROM prior.nba.dim_game g
ANTI JOIN (SELECT DISTINCT game_id FROM prior.nba.fact_shot) s USING(game_id)
WHERE g.season_start=2019 AND g.season_type='Regular' '''


def open_config(spill: str) -> dict[str, str | bool | int | float | list[str]]:
    return {'threads':2,'memory_limit':'2GB','temp_directory':spill}


def attach(con: duckdb.DuckDBPyConnection, parent: Path) -> None:
    con.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    con.execute('SET enable_progress_bar=false')


def verify_inputs(parent: Path, release: Path) -> None:
    initial = snapshot(parent),snapshot(release)
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='pbp-attempt-verify-') as spill, \
         duckdb.connect(str(release),read_only=True,config=open_config(spill)) as con:
        attach(con,parent)
        if con.execute('SELECT * FROM audit.pbp_attempt_input').fetchall()!=[('parent_v14',sha256(parent))]:
            raise RuntimeError('The v14 parent digest differs')
        if difference(con,'SELECT * FROM audit.pbp_attempt_scope',SCOPE):
            raise RuntimeError('Shot-gap fixture scope differs from v14')
        for name in ('nba.fact_play_event','nba.fact_player_game_stats','nba.fact_team_game_stats'):
            restrict = 'SEMI JOIN audit.pbp_attempt_scope USING(game_id)'
            if difference(con,f'SELECT * FROM {name} {restrict}',f'SELECT * FROM prior.{name} {restrict}'):
                raise RuntimeError(f'Scoped inputs differ from the pinned v14: {name}')
        for name in ('nba.dim_game','nba.fact_game_result','nba.fact_game_period_score','nba.fact_shot','audit.shot_coverage'):
            if difference(con,f'SELECT * FROM {name}',f'SELECT * FROM prior.{name}'):
                raise RuntimeError(f'Existing games, period scores or shot chart rows changed: {name}')
        counts = con.execute('''SELECT disposition,count(*),sum(candidate_attempts) FROM audit.pbp_attempt_coverage
                                GROUP BY 1 ORDER BY 1''').fetchall()
        if counts!=[('box_attempts_disagree',1,183),('published_box_reconciled_pbp',67,11819)]:
            raise RuntimeError(f'Unexpected play-by-play attempt dispositions: {counts}')
        checks = verify(con)
    if (snapshot(parent),snapshot(release))!=initial:
        raise RuntimeError('An input changed during verification')
    print(f'{checks} checks passed; v14 inputs and shot charts match exactly; dispositions: {counts}')


def build(parent: Path, output: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    initial = snapshot(parent)
    baseline = sha256(parent)
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.pbp-attempts-') as temp, \
         tempfile.TemporaryDirectory(dir=SCRATCH,prefix='pbp-attempt-build-') as spill:
        stage = Path(temp)/'release.duckdb'
        shutil.copyfile(parent,stage)
        with duckdb.connect(str(stage),config=open_config(spill)) as con:
            attach(con,parent)
            con.begin()
            con.execute((WAREHOUSE/'pbp_attempts.sql').read_text())
            con.execute('INSERT INTO audit.pbp_attempt_scope '+SCOPE)
            con.execute("INSERT INTO audit.pbp_attempt_input VALUES ('parent_v14',?)",[baseline])
            con.execute('''INSERT INTO nba.fact_pbp_field_goal_attempt
              SELECT n.* EXCLUDE(valid) FROM audit.pbp_attempt_candidate n JOIN audit.pbp_attempt_coverage a USING(game_id)
              WHERE n.valid AND a.disposition='published_box_reconciled_pbp' ''')
            con.commit()
            con.execute('CHECKPOINT')
        verify_inputs(parent,stage)
        if snapshot(parent)!=initial or os.path.lexists(f'{output}.wal'):
            raise RuntimeError('Input changed or destination WAL exists; not installing')
        os.link(stage,output)
        print(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v14.duckdb')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    parent = args.parent.resolve(strict=True)
    if args.action=='build':
        build(parent,args.output.absolute())
    else:
        verify_inputs(parent,args.output.resolve(strict=True))


if __name__=='__main__':
    main()
