"""Build a new, local release by recovering reversed line scores from the supplied snapshot."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile

import duckdb

from nba_warehouse import FileIdentity, ROOT, SCRATCH, WAREHOUSE, scalar_count, sql_string, verify


def difference(con: duckdb.DuckDBPyConnection, left: str, right: str) -> int:
    return scalar_count(con,f'''SELECT count(*) FROM (({left} EXCEPT ALL {right})
        UNION ALL ({right} EXCEPT ALL {left}))''')


def verify_delta(con: duckdb.DuckDBPyConnection) -> None:
    for relation in ('nba.dim_game','nba.fact_game_result','nba.bridge_game_team', 'nba.fact_regular_standings'):
        if difference(con, f'SELECT * FROM {relation}',f'SELECT * FROM base.{relation}'):
            raise RuntimeError(f'{relation} changed outside the period recovery scope')
    for relation in ('nba.fact_game_period_score','audit.period_coverage'):
        left = f'''SELECT p.* FROM {relation} p ANTI JOIN
            (SELECT game_id FROM audit.local_period_recovery WHERE status='admitted') a USING(game_id)'''
        if difference(con,left,f'SELECT * FROM base.{relation}'+('' if relation.startswith('nba.') else
                " WHERE game_id NOT IN (SELECT game_id FROM audit.local_period_recovery WHERE status='admitted')")):
            raise RuntimeError(f'{relation}: unapproved records changed or disappeared')
    if difference(con,'''SELECT game_id,prior_status FROM audit.local_period_recovery''',
        '''SELECT c.game_id,c.status FROM base.audit.period_coverage c JOIN audit.local_period_recovery a USING(game_id)'''):
        raise RuntimeError('Prior period dispositions differ from the base release')


def snapshot(path: Path) -> FileIdentity:
    if not path.is_file() or Path(f'{path}.wal').exists():
        raise RuntimeError(f'Not a closed, checkpointed input: {path}')
    return FileIdentity.read(path)


def build(base: Path, source: Path, output: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    base_id, source_id = snapshot(base), snapshot(source)
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.recover-local-periods-') as temp, \
         tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-period-spill-') as spill:
        stage = Path(temp)/'release.duckdb'
        shutil.copyfile(base,stage)
        attach = f'ATTACH {sql_string(str(source))} AS src (READ_ONLY)'
        with duckdb.connect(str(stage),config={'threads':2,'memory_limit':'2GB','temp_directory':spill}) as con:
            if con.execute('SELECT source_size,source_mtime_ns FROM audit.input_manifest').fetchall()!=[(source_id.size,source_id.mtime_ns)]:
                raise RuntimeError('The supplied raw input does not match the base manifest')
            con.execute(attach)
            con.begin()
            con.execute((WAREHOUSE/'recover_local_periods.sql').read_text())
            con.execute('''CREATE TABLE audit.local_period_recovery_input
                (source_role VARCHAR PRIMARY KEY,path VARCHAR NOT NULL,size_bytes BIGINT NOT NULL,mtime_ns BIGINT NOT NULL)''')
            con.executemany('INSERT INTO audit.local_period_recovery_input VALUES (?,?,?,?)',
                [('base',str(base),base_id.size,base_id.mtime_ns),('source',str(source),source_id.size,source_id.mtime_ns)])
            con.commit()
            con.execute('CHECKPOINT')
        if Path(f'{stage}.wal').exists():
            raise RuntimeError('Refusing to publish a file with a WAL')
        with duckdb.connect(str(stage),read_only=True,config={'threads':2,'memory_limit':'2GB','temp_directory':spill}) as con:
            con.execute(f'ATTACH {sql_string(str(base))} AS base (READ_ONLY)')
            checks = verify(con)
            verify_delta(con)
            result = con.execute('SELECT prior_status,status,count(*) FROM audit.local_period_recovery GROUP BY ALL ORDER BY ALL').fetchall()
            counts = con.execute('''SELECT count(*),count(DISTINCT game_id) FROM nba.fact_game_period_score''').fetchone()
        if snapshot(base)!=base_id or snapshot(source)!=source_id or os.path.lexists(f'{output}.wal'):
            raise RuntimeError('An input changed or output WAL exists; output was not installed')
        os.link(stage,output)
        print(f'{checks} warehouse checks passed; period rows/games: {counts}\nRecovery dispositions: {result}')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,default=ROOT/'data/nba-clean-v5.duckdb')
    parser.add_argument('--source',type=Path,default=ROOT/'data/nba.duckdb')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    build(args.base.resolve(strict=True),args.source.resolve(strict=True),args.output.absolute())
