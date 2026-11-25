"""Extend v15 with reviewed historical periods and modern field-goal attempts."""
from __future__ import annotations

import argparse
from datetime import date, datetime
from hashlib import sha256 as hash_bytes
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, verify
from recover_archive_periods import sha256
from recover_local_periods import difference, snapshot
from recover_pbp_attempts import attach, open_config


SEASON_PINS = (
    (1961,25,1,'6400d9f05ec4acb73f694bbb81f196f0a23fa1d38bd6d0b6f152c08304486140'),
    (1965,26,2,'1394a1a8b0425f71b9efb853abad796cce697bdf3a8ba8cb69bb00999a223a44'),
    (1969,39,0,'4006527a41cbc8f6f54c893f3b74cfdd552eb6facc34071f08e0d970c27d111c'),
    (1994,77,7,'949c5b4dc3d8a7fe3cd1963e970c1bd03ec440886e14737810220cdca8e5ad5f'),
    (1996,68,5,'9dff5df485849acf1d879f924c5765d08ee36f0b4dc8dcfba42f13d165bec0ee'),
)
EARLY_OT = ('0026100105',date(1961,12,8),1610612744,1610612747,
            (34,28,21,26,12,12,14),(25,28,32,24,12,12,18),'https://www.nba.com/game/0026100105')
MODERN_SCOPE = '''SELECT g.game_id FROM prior.nba.dim_game g
ANTI JOIN (SELECT DISTINCT game_id FROM prior.nba.fact_shot) s USING(game_id)
WHERE (g.season_start,g.season_type) IN ((2024,'Regular'),(2025,'Playoffs'))'''
Schedule = tuple[int,int,date,str,str,int,int,int]
Pin = tuple[int,str,str,int,int]


def read_schedule(path: Path) -> tuple[list[Pin], list[Schedule]]:
    tables = json.loads(path.read_text())
    if set(tables)!={str(year) for year,_,_,_ in SEASON_PINS}:
        raise ValueError('Expected the five cached playoff schedule tables')
    schedule: list[Schedule] = []
    pins: list[Pin] = []
    for year,count,ot,digest in SEASON_PINS:
        rows = [r for r in tables[str(year)]['rows'] if re.fullmatch(r'[A-Za-z]{3}, [A-Za-z]{3} \d{1,2}, \d{4}', r[0])]
        canonical = json.dumps(rows,separators=(',',':'),ensure_ascii=False).encode()
        if hash_bytes(canonical).hexdigest()!=digest or len(rows)!=count:
            raise ValueError(f'Parsed BRef fixture-table digest or count differs: {year}')
        url = f'https://www.basketball-reference.com/playoffs/NBA_{year}_games.html'
        pins.append((year-1,url,digest,count,ot))
        for ordinal,r in enumerate(rows,1):
            if len(r)!=11 or not re.fullmatch(r'(?:[2-9]?OT)?',r[6]):
                raise ValueError(f'Invalid schedule receipt shape: {year}/{ordinal}')
            extra = int(r[6][:-2] or '1') if r[6] else 0
            day = datetime.strptime(r[0],'%a, %b %d, %Y').date()
            if day.year!=year:
                raise ValueError(f'Unexpected schedule date: {year}/{ordinal}')
            schedule.append((year-1,ordinal,day,r[3],r[1],int(r[4]),int(r[2]),extra))
    return pins,schedule


def verify_inputs(parent: Path, schedule_source: Path, release: Path) -> None:
    files = parent,schedule_source,release
    initial = tuple(map(snapshot,files))
    pins,schedule = read_schedule(schedule_source)
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='v16-verify-') as spill, \
         duckdb.connect(str(release),read_only=True,config=open_config(spill)) as c:
        attach(c,parent)
        if c.execute('SELECT sha256 FROM audit.pbp_attempt_input WHERE role=\'parent_v15\'').fetchall()!=[(sha256(parent),)]:
            raise RuntimeError('Pinned v15 digest differs')
        if c.execute('SELECT * FROM audit.playoff_ot_schedule_pin ORDER BY season_start').fetchall()!=pins:
            raise RuntimeError('Playoff season evidence manifest differs')
        if c.execute('SELECT * FROM audit.playoff_ot_schedule ORDER BY season_start,source_ordinal').fetchall()!=schedule:
            raise RuntimeError('Exact parsed schedule rows differ from the offline evidence')
        if c.execute('SELECT * FROM audit.early_ot3_receipt_v16').fetchall()!=[EARLY_OT]:
            raise RuntimeError('Exact 1961 NBA game-page receipt differs')
        if difference(c,'SELECT * FROM audit.pbp_attempt_modern_scope',MODERN_SCOPE):
            raise RuntimeError('Modern chart-gap keys differ from the parent')
        if difference(c,"SELECT * FROM audit.pbp_attempt_input WHERE role='parent_v14'",
                      'SELECT * FROM prior.audit.pbp_attempt_input'):
            raise RuntimeError('Inherited attempt input pin changed')
        view_filter = "WHERE schema_name='nba' AND view_name='field_goal_attempt'"
        if difference(c,f'SELECT sql FROM duckdb_views() {view_filter} AND database_name=current_database()',
                      f"SELECT sql FROM duckdb_views() {view_filter} AND database_name='prior'"):
            raise RuntimeError('Unified attempt projection changed')
        for rel in ('nba.dim_game','nba.dim_team','nba.dim_team_version','nba.fact_game_result','nba.bridge_game_team',
                    'nba.fact_regular_standings','nba.fact_shot','audit.shot_coverage','audit.mirror_v13_team','audit.mirror_v12_team',
                    'audit.mirror_v13_assessment','audit.mirror_v12_assessment'):
            if difference(c,f'SELECT * FROM {rel}',f'SELECT * FROM prior.{rel}'):
                raise RuntimeError(f'Parent identities, finals or facts changed: {rel}')
        for rel in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline','nba.game_overtime'):
            restrict = 'ANTI JOIN audit.period_adjudication_v16_keys USING(game_id)'
            if difference(c,f'SELECT * FROM {rel} {restrict}',f'SELECT * FROM prior.{rel} {restrict}'):
                raise RuntimeError(f'Period delta escaped its scope: {rel}')
        for rel in ('audit.pbp_attempt_scope','nba.fact_pbp_field_goal_attempt','audit.pbp_attempt_coverage'):
            restrict = 'ANTI JOIN audit.pbp_attempt_modern_scope USING(game_id)'
            if difference(c,f'SELECT * FROM {rel} {restrict}',f'SELECT * FROM prior.{rel}'):
                raise RuntimeError(f'Previous attempt scope changed: {rel}')
        for rel in ('nba.fact_play_event','nba.fact_player_game_stats','nba.fact_team_game_stats'):
            restrict = 'SEMI JOIN audit.pbp_attempt_scope USING(game_id)'
            if difference(c,f'SELECT * FROM {rel} {restrict}',f'SELECT * FROM prior.{rel} {restrict}'):
                raise RuntimeError(f'Attempt source differs from v15: {rel}')
        checks = verify(c)
    if tuple(map(snapshot,files))!=initial:
        raise RuntimeError('An input changed during verification')
    print(f'{checks} release checks passed; pinned parent, schedule, early OT receipt and exact deltas verified')


def build(parent: Path, schedule_source: Path, output: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    files = parent,schedule_source
    initial = tuple(map(snapshot,files))
    pins,schedule = read_schedule(schedule_source)
    parent_sha = sha256(parent)
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v16-') as temp, \
         tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-v16-spill-') as spill:
        stage = Path(temp)/'release.duckdb'
        shutil.copyfile(parent,stage)
        with duckdb.connect(str(stage),config=open_config(spill)) as c:
            attach(c,parent)
            c.begin()
            c.execute((WAREHOUSE/'period_adjudication_v16.sql').read_text())
            c.executemany('INSERT INTO audit.playoff_ot_schedule_pin VALUES (?,?,?,?,?)',pins)
            c.executemany('INSERT INTO audit.playoff_ot_schedule VALUES (?,?,?,?,?,?,?,?)',schedule)
            c.execute('INSERT INTO audit.early_ot3_receipt_v16 VALUES (?,?,?,?,?,?,?)',list(EARLY_OT))
            c.execute('''
               CREATE TEMP TABLE accepted_period_cells AS SELECT * FROM audit.period_adjudication_v16_cells;
               INSERT INTO nba.fact_game_period_score SELECT * FROM accepted_period_cells;
               INSERT INTO audit.period_vector_baseline
               SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
                   ORDER BY team_id,period_number))) FROM accepted_period_cells GROUP BY game_id;
               UPDATE audit.period_coverage p SET status=c.status,selected_source=c.selected_source,
                   candidate_sources=c.candidate_sources,valid_sources=c.valid_sources
               FROM audit.period_adjudication_v16_coverage c WHERE p.game_id=c.game_id;
               CREATE VIEW audit.period_availability_v16 AS
               SELECT p.* REPLACE (coalesce(k.selected_source,p.recovery_disposition) AS recovery_disposition)
               FROM audit.period_availability_v14 p LEFT JOIN audit.period_adjudication_v16_keys k USING(game_id);
               CREATE TABLE audit.pbp_attempt_modern_scope(game_id VARCHAR PRIMARY KEY);
            ''')
            c.execute('INSERT INTO audit.pbp_attempt_modern_scope '+MODERN_SCOPE)
            c.execute("INSERT INTO audit.pbp_attempt_input VALUES ('parent_v15',?)",[parent_sha])
            c.execute('''INSERT INTO audit.pbp_attempt_scope SELECT * FROM audit.pbp_attempt_modern_scope;
               INSERT INTO nba.fact_pbp_field_goal_attempt
               SELECT n.* EXCLUDE(valid) FROM audit.pbp_attempt_candidate n JOIN audit.pbp_attempt_coverage a USING(game_id)
               SEMI JOIN audit.pbp_attempt_modern_scope USING(game_id)
               WHERE n.valid AND a.disposition='published_box_reconciled_pbp';''')
            c.commit()
            c.execute('CHECKPOINT')
        verify_inputs(parent,schedule_source,stage)
        if tuple(map(snapshot,files))!=initial or os.path.lexists(f'{output}.wal'):
            raise RuntimeError('Input changed or destination WAL exists; not installing')
        os.link(stage,output)
        print(output)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('build','verify-inputs'))
    p.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v15.duckdb')
    p.add_argument('--schedule',type=Path,required=True,help='Cached parsed BRef playoff schedules; no live retrieval')
    p.add_argument('--output',type=Path,required=True)
    args = p.parse_args()
    parent,schedule = args.parent.resolve(strict=True),args.schedule.resolve(strict=True)
    if args.action=='build':
        build(parent,schedule,args.output.absolute())
    else:
        verify_inputs(parent,schedule,args.output.resolve(strict=True))


if __name__=='__main__':
    main()
