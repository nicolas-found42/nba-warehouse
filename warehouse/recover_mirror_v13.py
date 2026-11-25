"""Apply the unchanged v12 mirror assessment to two frozen v12 gap cohorts."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile
from typing import Literal

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, verify
from recover_archive_periods import sha256
from recover_local_periods import difference, snapshot
from recover_mirror_v12 import attach


SCOPE = '''
SELECT g.game_id,g.season_year,
       CASE WHEN g.season_type='Regular' THEN 'legacy_regular' ELSE 'legacy_playoffs' END AS cohort
FROM prior.nba.dim_game g JOIN prior.audit.period_coverage p USING(game_id)
WHERE p.status<>'published' AND ((g.season_type='Regular' AND g.season_start BETWEEN 1970 AND 1999)
                              OR (g.season_type='Playoffs' AND g.season_start>=1954))
'''
PRIOR = '''
SELECT p.game_id,p.status,p.candidate_sources,p.valid_sources FROM prior.audit.period_coverage p
SEMI JOIN audit.mirror_v13_scope s USING(game_id)
'''
SOURCE = '''
SELECT m.game_id,m.team_id,m.opponent_team_id,m.game_date,m.season_year,m.game_type,m.is_home,m.pts,
       [m.q1_points,m.q2_points,m.q3_points,m.q4_points],[m.ot1_points,m.ot2_points],m.ot_all_points
FROM src.main.stg_kaggle_team_box m SEMI JOIN audit.mirror_v13_scope s USING(game_id)
'''
EXPECTED = [('legacy_playoffs',482,271,2232),('legacy_regular',2879,2869,23282)]


def assessment_ddl(prefix: Literal['mirror_v13_', 'mirror_v14_'] = 'mirror_v13_') -> str:
    # Share the exact same classifier and typed ledgers. Only the frozen cohort
    # changes; the old view definitions already installed in v12 are untouched.
    start = 'CREATE TABLE audit.mirror_v12_prior ('
    _, marker, definitions = (WAREHOUSE / 'mirror_v12.sql').read_text().partition(start)
    if not marker:
        raise RuntimeError('Cannot locate v12 shared mirror assessment definitions')
    return (marker + definitions).replace('mirror_v12_', prefix)


def pins(parent: Path, source: Path) -> list[tuple[str,str,str,bool]]:
    return [('parent_v12',sha256(parent),'nba-clean-v12',False),
            ('raw',sha256(source),'main.stg_kaggle_team_box (kaggle_nba, derivative)',False)]


def verify_inputs(parent: Path, source: Path, release: Path) -> None:
    files = parent,source,release
    initial = tuple(map(snapshot,files))
    with duckdb.connect(str(release),read_only=True,config={'threads':2,'memory_limit':'2GB'}) as con:
        attach(con,parent,source)
        if con.execute('SELECT * FROM audit.mirror_v13_input ORDER BY role').fetchall()!=pins(parent,source):
            raise RuntimeError('The read-only v12 or raw input digest differs')
        for name, expected in (('scope',SCOPE),('prior',PRIOR),('team',SOURCE)):
            if difference(con,f'SELECT * FROM audit.mirror_v13_{name}',expected):
                raise RuntimeError(f'Exact attached input differs from release ledger: mirror_v13_{name}')
        for name in ('nba.dim_game','nba.dim_team','nba.fact_game_result','nba.bridge_game_team',
                     'nba.bridge_event_fixture','nba.fact_regular_standings'):
            if difference(con,f'SELECT * FROM {name}',f'SELECT * FROM prior.{name}'):
                raise RuntimeError(f'Existing dimensions, finals or standings changed: {name}')
        anti_scope = 'ANTI JOIN audit.mirror_v13_scope USING(game_id)'
        for name in ('nba.fact_game_period_score','nba.game_overtime','audit.period_coverage','audit.period_vector_baseline'):
            if difference(con,f'SELECT * FROM {name} {anti_scope}',f'SELECT * FROM prior.{name} {anti_scope}'):
                raise RuntimeError(f'Outside-cohort rows changed: {name}')
        counts = con.execute('''WITH n AS (
             SELECT game_id,count(*) AS cell_count FROM audit.mirror_v13_cells GROUP BY 1
           ) SELECT a.cohort,count(*),count(*) FILTER(WHERE a.disposition='admitted'),sum(n.cell_count)
           FROM audit.mirror_v13_assessment a LEFT JOIN n USING(game_id) GROUP BY 1 ORDER BY 1''').fetchall()
        if counts!=EXPECTED:
            raise RuntimeError(f'Unexpected admitted count or cell count by cohort: {counts}')
        checks = verify(con)
        assessments = con.execute('''SELECT cohort,disposition,count(*) FROM audit.mirror_v13_assessment
                                    GROUP BY ALL ORDER BY ALL''').fetchall()
    if tuple(map(snapshot,files))!=initial:
        raise RuntimeError('An input changed during verification')
    print(f'{checks} checks passed; exact inputs and parent delta verified; assessment: {assessments}')


def build(parent: Path, source: Path, output: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    initial = snapshot(parent),snapshot(source)
    digests = pins(parent,source)
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.mirror-v13-') as temp, \
         tempfile.TemporaryDirectory(dir=SCRATCH,prefix='mirror-v13-spill-') as spill:
        stage = Path(temp)/'release.duckdb'
        shutil.copyfile(parent,stage)
        with duckdb.connect(str(stage),config={'threads':2,'memory_limit':'2GB','temp_directory':spill}) as con:
            attach(con,parent,source)
            if con.execute("SELECT sha256 FROM prior.audit.mirror_v12_input WHERE role='raw'").fetchall()!=[(digests[1][1],)]:
                raise RuntimeError('Raw input hash differs from v12')
            con.begin()
            con.execute('''CREATE TABLE audit.mirror_v13_scope (
                game_id VARCHAR PRIMARY KEY,season_year VARCHAR NOT NULL,
                cohort VARCHAR NOT NULL CHECK(cohort IN ('legacy_regular','legacy_playoffs')));''')
            con.execute('INSERT INTO audit.mirror_v13_scope '+SCOPE)
            con.execute(assessment_ddl())
            con.execute('INSERT INTO audit.mirror_v13_prior '+PRIOR)
            con.execute('INSERT INTO audit.mirror_v13_team '+SOURCE)
            con.executemany('INSERT INTO audit.mirror_v13_input VALUES (?,?,?,?)',digests)
            con.execute('''
                INSERT INTO nba.fact_game_period_score SELECT * FROM audit.mirror_v13_cells;
                INSERT INTO audit.period_vector_baseline
                SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
                       ORDER BY team_id,period_number))) FROM audit.mirror_v13_cells GROUP BY game_id;
                UPDATE audit.period_coverage p SET status=c.status,selected_source=c.selected_source,
                      candidate_sources=c.candidate_sources,valid_sources=c.valid_sources
                FROM audit.mirror_v13_coverage c WHERE p.game_id=c.game_id;
                CREATE VIEW audit.period_availability_v13 AS
                SELECT v.* REPLACE (coalesce(c.selected_source,c.disposition,v.recovery_disposition) AS recovery_disposition)
                FROM audit.period_availability_v12 v LEFT JOIN audit.mirror_v13_coverage c USING(game_id);
            ''')
            con.commit()
            con.execute('CHECKPOINT')
        verify_inputs(parent,source,stage)
        if (snapshot(parent),snapshot(source))!=initial or os.path.lexists(f'{output}.wal'):
            raise RuntimeError('An input changed or a destination WAL exists; not installing')
        os.link(stage,output)
        print(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v12.duckdb')
    parser.add_argument('--source',type=Path,default=ROOT/'data/nba.duckdb')
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    parent,source = (getattr(args,k).resolve(strict=True) for k in ('parent','source'))
    if args.action=='build':
        build(parent,source,args.output.absolute())
    else:
        verify_inputs(parent,source,args.output.resolve(strict=True))


if __name__=='__main__':
    main()
