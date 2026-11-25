"""Recover the scoped 1946–69 mirror vectors and two keyed third overtimes."""
from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path
import shutil
import tempfile
from typing import NamedTuple

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, verify
from recover_archive_periods import sha256
from recover_local_periods import difference, snapshot
from recover_mirror_v12 import attach
from recover_mirror_v13 import assessment_ddl
from validate_current_results import csv_rows, unique_index


RECEIPTS = WAREHOUSE/'mirror_v14_ot3_receipts.csv'
SCOPE = '''SELECT g.game_id,g.season_year,
       CASE WHEN g.season_type='Regular' THEN 'legacy_regular' ELSE 'legacy_playoffs' END AS cohort
FROM prior.nba.dim_game g JOIN prior.audit.period_coverage p USING(game_id)
ANTI JOIN prior.audit.mirror_v12_scope s USING(game_id)
WHERE p.status<>'published' AND ((g.season_type='Regular' AND g.season_start BETWEEN 1946 AND 1969)
    OR (g.season_type='Playoffs' AND g.season_start BETWEEN 1946 AND 1953))'''
PRIOR = '''SELECT p.game_id,p.status,p.candidate_sources,p.valid_sources FROM prior.audit.period_coverage p
SEMI JOIN audit.mirror_v14_scope s USING(game_id)'''
SOURCE = '''SELECT m.game_id,m.team_id,m.opponent_team_id,m.game_date,m.season_year,m.game_type,m.is_home,m.pts,
       [m.q1_points,m.q2_points,m.q3_points,m.q4_points],[m.ot1_points,m.ot2_points],m.ot_all_points
FROM src.main.stg_kaggle_team_box m SEMI JOIN audit.mirror_v14_scope s USING(game_id)'''
IDS = {'0027700297','0029200096'}


class Tail(NamedTuple):
    game_id: str
    game_date: date
    home_id: int
    away_id: int
    home_periods: tuple[int,...]
    away_periods: tuple[int,...]
    source_url: str


def read_tails(path: Path) -> list[Tail]:
    rows = unique_index(csv_rows(path),'game_id')
    if rows.keys()!=IDS:
        raise ValueError('Need exactly the two keyed OT3 fixture receipts')
    tails = []
    for gid,r in rows.items():
        h,a = [tuple(map(int,r[col].split('|'))) for col in ('home_periods','away_periods')]
        if (len(h)!=7 or len(a)!=7 or min(*h,*a)<0 or sum(h)==sum(a)
            or any(sum(h[:i])!=sum(a[:i]) for i in range(4,7)) or r['source_url']!=f'https://www.nba.com/game/{gid}'):
            raise ValueError(f'Invalid official OT3 vectors or locator: {gid}')
        tails.append(Tail(gid,date.fromisoformat(r['game_date']),int(r['home_id']),int(r['away_id']),h,a,r['source_url']))
    return tails


def manifest(parent: Path, source: Path, receipts: Path) -> list[tuple[str,str,str,bool]]:
    return [('ot3_receipts',sha256(receipts),'Two transcribed original NBA game-page line scores',False),
            ('parent_v13',sha256(parent),'nba-clean-v13',False),
            ('raw',sha256(source),'main.stg_kaggle_team_box (kaggle_nba, derivative)',False)]


def verify_inputs(parent: Path, source: Path, receipts: Path, release: Path) -> None:
    files = parent,source,receipts,release
    initial = tuple(map(snapshot,files))
    with duckdb.connect(str(release),read_only=True,config={'threads':2,'memory_limit':'2GB'}) as con:
        attach(con,parent,source)
        if con.execute('SELECT * FROM audit.mirror_v14_input ORDER BY role').fetchall()!=manifest(parent,source,receipts):
            raise RuntimeError('Release input digests differ')
        con.execute('CREATE TEMP TABLE expected_tails AS SELECT * FROM audit.mirror_v14_tail LIMIT 0')
        con.executemany('INSERT INTO expected_tails VALUES (?,?,?,?,?,?,?)',read_tails(receipts))
        for name,expected in (('scope',SCOPE),('prior',PRIOR),('team',SOURCE),('tail','SELECT * FROM expected_tails')):
            if difference(con,f'SELECT * FROM audit.mirror_v14_{name}',expected):
                raise RuntimeError(f'Exact pinned input differs from early period ledger: {name}')
        for name in ('nba.dim_game','nba.dim_team','nba.fact_game_result','nba.bridge_game_team',
                     'nba.bridge_event_fixture','nba.fact_regular_standings','audit.mirror_v13_assessment'):
            if difference(con,f'SELECT * FROM {name}',f'SELECT * FROM prior.{name}'):
                raise RuntimeError(f'Existing dimensions, finals, standings or assessments changed: {name}')
        anti = 'ANTI JOIN audit.mirror_v14_changed_keys USING(game_id)'
        for name in ('nba.fact_game_period_score','nba.game_overtime','audit.period_coverage','audit.period_vector_baseline'):
            if difference(con,f'SELECT * FROM {name} {anti}',f'SELECT * FROM prior.{name} {anti}'):
                raise RuntimeError(f'Outside-cohort rows changed: {name}')
        counts = con.execute('''SELECT count(*),count(*) FILTER(WHERE disposition='admitted'),
               count(*) FILTER(WHERE observed_rows=0),sum(observed_rows) FROM audit.mirror_v14_assessment''').fetchone()
        if counts!=(2306,835,1110,2392):
            raise RuntimeError(f'Early source or admission counts differ: {counts}')
        row_counts = con.execute('''SELECT (SELECT count(*) FROM audit.mirror_v14_cells),
                                           (SELECT count(*) FROM audit.mirror_v14_tail_cells)''').fetchone()
        if row_counts!=(6776,28):
            raise RuntimeError(f'Unexpected early or overtime cell counts: {row_counts}')
        checks = verify(con)
        assessed = con.execute('SELECT disposition,count(*) FROM audit.mirror_v14_assessment GROUP BY 1 ORDER BY 1').fetchall()
    if tuple(map(snapshot,files))!=initial:
        raise RuntimeError('An input changed during verification')
    print(f'{checks} checks passed; exact inputs, two OT3 receipts and prior scope verified: {assessed}')


def build(parent: Path, source: Path, receipts: Path, output: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    files = parent,source,receipts
    initial = tuple(map(snapshot,files))
    pins = manifest(parent,source,receipts)
    tail_rows = read_tails(receipts)
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.mirror-v14-') as temp, \
         tempfile.TemporaryDirectory(dir=SCRATCH,prefix='mirror-v14-spill-') as spill:
        stage = Path(temp)/'release.duckdb'
        shutil.copyfile(parent,stage)
        with duckdb.connect(str(stage),config={'threads':2,'memory_limit':'2GB','temp_directory':spill}) as con:
            attach(con,parent,source)
            if con.execute("SELECT sha256 FROM prior.audit.mirror_v13_input WHERE role='raw'").fetchall()!=[(pins[2][1],)]:
                raise RuntimeError('Raw input digest differs from v13')
            con.begin()
            con.execute('''CREATE TABLE audit.mirror_v14_scope(game_id VARCHAR PRIMARY KEY,season_year VARCHAR NOT NULL,
                           cohort VARCHAR NOT NULL CHECK(cohort IN ('legacy_regular','legacy_playoffs')));''')
            con.execute('INSERT INTO audit.mirror_v14_scope '+SCOPE)
            con.execute(assessment_ddl('mirror_v14_'))
            con.execute((WAREHOUSE/'mirror_v14_tails.sql').read_text())
            con.execute('INSERT INTO audit.mirror_v14_prior '+PRIOR)
            con.execute('INSERT INTO audit.mirror_v14_team '+SOURCE)
            con.executemany('INSERT INTO audit.mirror_v14_input VALUES (?,?,?,?)',pins)
            con.executemany('INSERT INTO audit.mirror_v14_tail VALUES (?,?,?,?,?,?,?)',tail_rows)
            con.execute('''
                CREATE TEMP TABLE v14_new_cells AS
                SELECT * FROM audit.mirror_v14_cells UNION ALL SELECT * FROM audit.mirror_v14_tail_cells;
                INSERT INTO nba.fact_game_period_score SELECT * FROM v14_new_cells;
                INSERT INTO audit.period_vector_baseline
                SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
                  ORDER BY team_id,period_number))) FROM v14_new_cells GROUP BY game_id;
                UPDATE audit.period_coverage p SET status=c.status, selected_source=c.selected_source,
                  candidate_sources=c.candidate_sources,valid_sources=c.valid_sources
                FROM (SELECT game_id,status,candidate_sources,valid_sources,selected_source FROM audit.mirror_v14_coverage
                      UNION ALL SELECT * FROM audit.mirror_v14_tail_coverage) c WHERE p.game_id=c.game_id;
                CREATE VIEW audit.period_availability_v14 AS
                SELECT v.* REPLACE (coalesce(t.selected_source,c.selected_source,c.disposition,v.recovery_disposition) AS recovery_disposition)
                FROM audit.period_availability_v13 v LEFT JOIN audit.mirror_v14_coverage c USING(game_id)
                LEFT JOIN audit.mirror_v14_tail_coverage t USING(game_id);''')
            con.commit()
            con.execute('CHECKPOINT')
        verify_inputs(parent,source,receipts,stage)
        if tuple(map(snapshot,files))!=initial or os.path.lexists(f'{output}.wal'):
            raise RuntimeError('Inputs changed or a destination WAL exists; not installing')
        os.link(stage,output)
        print(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v13.duckdb')
    parser.add_argument('--source',type=Path,default=ROOT/'data/nba.duckdb')
    parser.add_argument('--receipts',type=Path,default=RECEIPTS)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    parent,source,receipts = (getattr(args,k).resolve(strict=True) for k in ('parent','source','receipts'))
    if args.action=='build':
        build(parent,source,receipts,args.output.absolute())
    else:
        verify_inputs(parent,source,receipts,args.output.resolve(strict=True))


if __name__ == '__main__':
    main()
