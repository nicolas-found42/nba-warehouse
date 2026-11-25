#!/usr/bin/env python3
"""Restore two scoped NBA Stats events/charts from pinned local archives (internal use)."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from io import TextIOWrapper
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, scalar_count, sql_string, verify
from recover_archive_periods import sha256
from recover_legacy_attempts_v18 import verify_inherited
from recover_local_periods import difference, snapshot
from recover_pbp_attempts import open_config

PARENT_SHA = '985cbe9cb8caa53e36fbc949375d7beb990a67092f4b61340814a72a48508c99'
DATASETS = 'https://raw.githubusercontent.com/shufinskiy/nba_data/main/datasets/'


@dataclass(frozen=True)
class Archive:
    game_id: str
    raw_game_id: str
    domain: str
    stem: str
    sha256: str
    size: int
    rows: int

    @property
    def filename(self) -> str:
        return self.stem+'.tar.xz'

    @property
    def key_field(self) -> str:
        return 'EVENTNUM' if self.domain=='events' else 'GAME_EVENT_ID'

    def manifest(self) -> tuple[str,str,str,str,str,int,int,bool]:
        return (self.game_id,self.domain,self.filename,DATASETS+self.filename,self.sha256,self.size,self.rows,False)


ARCHIVES = (
    Archive('0020600975','20600975','events','nbastats_2006','0d325c0df0ace9087f9cf2413f7c8970c05befc844fcf25897e48d41ad3c071c',7787008,429),
    Archive('0020700753','20700753','events','nbastats_2007','875a5928d6c3267485627705805dcec1571d33c3c7ac948dd9a8d50bf2993e41',7728748,413),
    Archive('0020600975','20600975','shotdetail','shotdetail_2006','7c238aee908a59bb707b34da321fafcb63d677fc10349997ba7c1fb7ded96ecf',2346208,151),
    Archive('0020700753','20700753','shotdetail','shotdetail_2007','067489fce1075f51cbce32f7b59a2f5e2c406a95d8f46f830a0a492677d0ceea',2406988,150),
)
Payload = tuple[str,str,int,str]
UNCHANGED = ('nba.dim_game','nba.bridge_game_team','nba.fact_game_result','nba.fact_regular_standings',
             'nba.fact_game_period_score','nba.fact_team_game_stats','nba.fact_player_game_stats',
             'nba.bridge_shot_event','nba.fact_pbp_field_goal_attempt','audit.event_hold','audit.shot_hold')


def read_archive(a: Archive,path: Path) -> list[Payload]:
    if path.stat().st_size!=a.size or sha256(path)!=a.sha256:
        raise RuntimeError(f'Original archive differs from reviewed evidence: {a.filename}')
    rows: list[Payload] = []
    with tarfile.open(path,mode='r:xz') as tar:
        member = tar.next()
        if member is None or not member.isfile() or member.name!=a.stem+'.csv':
            raise RuntimeError(f'Archive member differs from expected original name: {a.filename}')
        fh = tar.extractfile(member)
        if fh is None:
            raise RuntimeError(f'No original CSV data: {a.filename}')
        with TextIOWrapper(fh,encoding='utf-8-sig',newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            if (reader.fieldnames is None or len(reader.fieldnames)!=len(set(reader.fieldnames))
                or not {'GAME_ID',a.key_field}.issubset(reader.fieldnames)):
                raise RuntimeError(f'Unexpected source columns: {a.filename}')
            for row in reader:
                if row['GAME_ID']!=a.raw_game_id:
                    continue
                if None in row or any(v is None for v in row.values()) or not row[a.key_field].isascii() or not row[a.key_field].isdigit():
                    raise RuntimeError(f'Malformed scoped archived action row: {a.game_id}/{a.domain}')
                rows.append((a.domain,a.game_id,int(row[a.key_field]),json.dumps(row,sort_keys=True,separators=(',',':'),ensure_ascii=False)))
        if tar.next() is not None:
            raise RuntimeError(f'Unexpected additional member: {a.filename}')
    if len(rows)!=a.rows or len({r[2] for r in rows})!=a.rows:
        raise RuntimeError(f'Original scoped row count or uniqueness differs: {a.filename}')
    return rows


def verify_partitioned(c: duckdb.DuckDBPyConnection,relation: str,left_suffix: str='') -> None:
    # Exact comparison at bounded memory: use the union of partition keys, never
    # silently ignore a newly added season or unexpected partition.
    left,right = f'SELECT * FROM {relation} {left_suffix}',f'SELECT * FROM prior.{relation}'
    part = "coalesce(substr(game_id,4,2),'')"
    groups = c.execute(f'SELECT DISTINCT {part} FROM ({left} UNION ALL {right}) ORDER BY 1').fetchall()
    for (group,) in groups:
        where = f' WHERE {part}={sql_string(group)}'
        if difference(c,left+where,right+where):
            raise RuntimeError(f'Inherited relation changed: {relation}, season {group}')


def replay(c: duckdb.DuckDBPyConnection,parent: Path,rows: list[Payload]) -> int:
    c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    if c.execute('SELECT * FROM audit.event_archive_parent_v20').fetchall()!=[('parent_v19',PARENT_SHA)]:
        raise RuntimeError('Pinned parent receipt differs')
    if c.execute('SELECT * FROM audit.event_archive_v20 ORDER BY game_id,domain').fetchall()!=sorted(a.manifest() for a in ARCHIVES):
        raise RuntimeError('Archive metadata or license disposition differs')
    if c.execute('SELECT * FROM audit.event_source_payload_v20 ORDER BY domain,game_id,action_number').fetchall()!=sorted(rows):
        raise RuntimeError('Exact staged source rows differ from the pinned archive bytes')
    checks = verify(c)
    for rel in UNCHANGED:
        verify_inherited(c,rel)
    games = "('0020600975','0020700753')"
    omit = f'WHERE game_id NOT IN {games}'
    for rel in ('nba.fact_play_event','nba.fact_shot'):
        if scalar_count(c,f'SELECT count(*) FROM prior.{rel} WHERE game_id IN {games}'):
            raise RuntimeError(f'Archive scope overlaps parent observations: {rel}')
        verify_partitioned(c,rel,f'ANTI JOIN (SELECT DISTINCT game_id FROM audit.event_archive_v20) a USING(game_id)')
    for rel in ('audit.event_coverage','audit.shot_coverage'):
        if difference(c,f'SELECT * FROM {rel} {omit}',f'SELECT * FROM prior.{rel} {omit}'):
            raise RuntimeError(f'Changed coverage escaped the two exact fixtures: {rel}')
    return checks


def run(action: str,parent: Path,archive_dir: Path,output: Path) -> None:
    if action=='build' and (os.path.lexists(output) or os.path.lexists(f'{output}.wal')):
        raise FileExistsError(f'Will not overwrite {output} or its WAL')
    files = tuple(archive_dir/a.filename for a in ARCHIVES)
    paths = (parent,*files) if action=='build' else (parent,output,*files)
    initial = tuple(map(snapshot,paths))
    if sha256(parent)!=PARENT_SHA:
        raise RuntimeError('Supplied parent does not match pinned v19 bytes')
    rows = [row for a,path in zip(ARCHIVES,files,strict=True) for row in read_archive(a,path)]
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-v20-spill-') as spill:
        if action=='verify-inputs':
            with duckdb.connect(str(output),read_only=True,config=open_config(spill)) as c:
                checks = replay(c,parent,rows)
        else:
            with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v20-') as work:
                candidate = Path(work)/'release.duckdb'
                shutil.copyfile(parent,candidate)
                with duckdb.connect(str(candidate),config=open_config(spill)) as c:
                    c.begin()
                    c.execute((WAREHOUSE/'event_archive_v20.sql').read_text())
                    c.executemany('INSERT INTO audit.event_archive_v20 VALUES (?,?,?,?,?,?,?,?)',[a.manifest() for a in ARCHIVES])
                    c.executemany('INSERT INTO audit.event_source_payload_v20 VALUES (?,?,?,?)',rows)
                    c.execute("INSERT INTO audit.event_archive_parent_v20 VALUES ('parent_v19',?)",[PARENT_SHA])
                    c.execute((WAREHOUSE/'event_archive_projection_v20.sql').read_text())
                    c.execute('''INSERT INTO nba.fact_play_event SELECT * FROM archive_play_expected_v20;
                                 INSERT INTO nba.fact_shot SELECT * FROM archive_shot_expected_v20;
                                 INSERT INTO nba.bridge_shot_event_source_v20 SELECT * FROM archive_link_expected_v20;
                                 UPDATE audit.event_coverage c SET input_events=a.expected_rows,published_events=a.expected_rows
                                 FROM audit.event_archive_v20 a WHERE a.domain='events' AND c.game_id=a.game_id;
                                 UPDATE audit.shot_coverage c SET input_rows=a.expected_rows,published_shots=a.expected_rows,matched_shots=a.expected_rows
                                 FROM audit.event_archive_v20 a WHERE a.domain='shotdetail' AND c.game_id=a.game_id;''')
                    c.commit()
                    c.execute('CHECKPOINT')
                snapshot(candidate)
                with duckdb.connect(str(candidate),read_only=True,config=open_config(spill)) as c:
                    checks = replay(c,parent,rows)
                if tuple(map(snapshot,paths))!=initial or os.path.lexists(f'{output}.wal'):
                    raise RuntimeError('An input changed or the destination WAL exists; not installing')
                os.link(candidate,output)
    if tuple(map(snapshot,paths))!=initial:
        raise RuntimeError('Input identity changed during verification')
    print(f'{checks} quality checks passed, four pinned archives and {len(UNCHANGED)+4} inherited relations replayed: {output}')


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v19.duckdb')
    parser.add_argument('--archive-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    run(args.action,args.parent.resolve(strict=True),args.archive_dir.resolve(strict=True),args.output.absolute())


if __name__=='__main__':
    main()
