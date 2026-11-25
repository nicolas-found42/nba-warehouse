#!/usr/bin/env python3
"""Replay the two reviewed 1949 Evening Star score-boundary receipts offline."""
from __future__ import annotations

import argparse
import csv
from datetime import date
import os
from pathlib import Path
import shutil
import tempfile
from typing import NamedTuple

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, sql_string, verify
from recover_archive_periods import sha256
from recover_legacy_attempts_v18 import verify_inherited
from recover_local_periods import difference, snapshot
from recover_pbp_attempts import open_config

PARENT_SHA = 'fb03c8d635489b135e43cf3be17af0130109f38f0b6b13130aa41adaa5d19404'
MANIFEST_SHA = 'f020b1a0fee91fae754ebbc0e7bb480d63390ef161878c0c6056571ace3cbc62'
CACHE = Path.home()/'Library/Application Support/NBA Warehouse/evidence/loc-1949-v23'
SOURCE = 'archival_cumulative_v23'
BOUNDARIES = ('Q1','HALF','Q3','FINAL')
RECEIPT = 'audit.archive_period_receipt_v23'
FROZEN = 'audit.archive_period_prior_v7_v23'
PINNED = 'audit.archive_period_input_v23'
CELLS = 'audit.archive_period_cells_v23'
UNCHANGED = (
    'nba.fact_game','nba.dim_game','nba.fact_game_result','nba.bridge_game_team',
    'nba.fact_regular_standings','nba.fact_team_game_stats','nba.fact_player_game_stats',
    'nba.fact_play_event','nba.fact_shot','nba.bridge_shot_event','nba.bridge_shot_event_source_v20',
    'nba.shot_event_link','nba.fact_pbp_field_goal_attempt','audit.event_coverage','audit.shot_coverage',
    'audit.period_availability_v7','audit.period_availability_v21','audit.period_availability_v22',
    'audit.bref_period_receipt_v22','audit.mirror_v13_assessment','audit.mirror_v14_assessment',
)


class Fixture(NamedTuple):
    game_date: date
    away_id: int
    away_label: str
    edition: date
    page: int

    @property
    def page_file(self) -> str:
        return f'page-{self.edition}-p{self.page}.jpg'

    @property
    def source_url(self) -> str:
        return f'https://www.loc.gov/resource/sn83045462/{self.edition}/ed-1/?sp={self.page}'


HOME = 1610610036
FIXTURES = {
    '0024800149': Fixture(date(1949,1,1),1610610025,'CHS',date(1949,1,2),34),
    '0024900027': Fixture(date(1949,11,8),1610612747,'MIN',date(1949,11,9),42),
}
PINS = [('parent_v22',PARENT_SHA),('receipt_manifest',MANIFEST_SHA)]
DDL = f'''
CREATE TABLE {RECEIPT} (
    game_id VARCHAR PRIMARY KEY, game_date DATE NOT NULL, home_id BIGINT NOT NULL, away_id BIGINT NOT NULL,
    edition DATE NOT NULL, page INTEGER NOT NULL, venue_observed VARCHAR NOT NULL, source_url VARCHAR UNIQUE NOT NULL,
    image_url VARCHAR UNIQUE NOT NULL, image_sha256 VARCHAR NOT NULL,
    home_cumulative INTEGER[4] NOT NULL, away_cumulative INTEGER[4] NOT NULL,
    home_boundary_notes VARCHAR[4] NOT NULL, away_boundary_notes VARCHAR[4] NOT NULL,
    evidence_kind VARCHAR NOT NULL CHECK(evidence_kind='derived_from_printed_cumulative'),
    redistribution_cleared BOOLEAN NOT NULL CHECK(NOT redistribution_cleared));
CREATE TABLE {PINNED}(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);
CREATE VIEW {CELLS} AS
SELECT r.game_id,b.team_id,i::SMALLINT AS period_number,
    CASE b.side WHEN 'home' THEN r.home_cumulative[i] - CASE i WHEN 1 THEN 0 ELSE r.home_cumulative[i-1] END
        ELSE r.away_cumulative[i] - CASE i WHEN 1 THEN 0 ELSE r.away_cumulative[i-1] END END AS points
FROM {RECEIPT} r JOIN nba.bridge_game_team b USING(game_id),range(1,5) p(i);
CREATE VIEW audit.period_availability_v23 AS
SELECT v.* REPLACE(c.status AS status,c.selected_source AS selected_source,
    coalesce(r.evidence_kind,v.recovery_disposition) AS recovery_disposition,
    o.last_period AS last_period,o.is_overtime AS derived_overtime,o.reported_is_overtime AS reported_overtime,
    CASE WHEN o.is_overtime IS NULL THEN 'periods_unavailable'
         WHEN o.reported_is_overtime IS NULL THEN 'reported_flag_missing'
         WHEN o.is_overtime=o.reported_is_overtime THEN 'agrees' ELSE 'reported_flag_disagrees' END AS overtime_status)
FROM audit.period_availability_v22 v JOIN audit.period_coverage c USING(game_id)
JOIN nba.game_overtime o USING(game_id) LEFT JOIN {RECEIPT} r USING(game_id);
'''


def evidence(cache: Path) -> tuple[dict[str, dict[str,str]], tuple[Path, ...]]:
    manifest = cache/'receipts.csv'
    if manifest.is_symlink() or sha256(manifest)!=MANIFEST_SHA:
        raise RuntimeError('Pinned archival manifest bytes differ')
    with manifest.open(newline='',encoding='utf-8') as f:
        records = list(csv.DictReader(f))
    ledger: dict[str, dict[str,str]] = {}
    for row in records:
        name = row['file']
        if not name or Path(name).name!=name or name in ledger:
            raise RuntimeError(f'Unsafe or duplicate archival path: {name}')
        source = cache/name
        if source.is_symlink() or (source.stat().st_size,sha256(source))!=(int(row['bytes']),row['sha256']):
            raise RuntimeError(f'Original archival evidence bytes differ: {name}')
        ledger[name] = row
    if len(ledger)!=24 or 'cumulative_boundaries.csv' not in ledger:
        raise RuntimeError('Expected 24 pinned archival evidence files')
    return ledger,(manifest,*(cache/name for name in ledger))


def inputs(c: duckdb.DuckDBPyConnection, parent: Path, cache: Path, ledger: dict[str,dict[str,str]]) -> None:
    c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    observed: dict[tuple[str,str,str], tuple[int,str]] = {}
    with (cache/'cumulative_boundaries.csv').open(newline='',encoding='utf-8') as f:
        for row in csv.DictReader(f):
            gid,side,ordinal = row['game_id'],row['side'],row['boundary']
            fixture = FIXTURES[gid]
            identity = {'home':(HOME,'WAS'),'away':(fixture.away_id,fixture.away_label)}[side]
            if ((int(row['team_id']),row['team'])!=identity or row['game_date']!=fixture.game_date.isoformat()
                or row['page']!=f'{fixture.edition} p{fixture.page}' or ordinal not in BOUNDARIES
                or (gid,side,ordinal) in observed or not row['printed']):
                raise RuntimeError(f'Invalid or duplicate printed boundary: {gid}/{side}/{ordinal}')
            observed[gid,side,ordinal] = int(row['cum_points']),row['printed']
    if len(observed)!=16:
        raise RuntimeError('Expected exactly 16 printed team boundaries')
    c.execute(f'CREATE TEMP TABLE expected_archive_v23 AS SELECT * FROM {RECEIPT} LIMIT 0')
    for gid,fixture in FIXTURES.items():
        home,away = ([observed[gid,side,n][0] for n in BOUNDARIES] for side in ('home','away'))
        if any(vec[0]<=0 or any(a>b for a,b in zip(vec,vec[1:])) for vec in (home,away)):
            raise RuntimeError(f'Invalid cumulative score vector: {gid}')
        old = c.execute('''SELECT g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score,
            g.arena_name,g.is_overtime,p.status,p.candidate_sources,p.valid_sources,p.selected_source,
            EXISTS(SELECT 1 FROM prior.nba.fact_game_period_score q WHERE q.game_id=g.game_id),
            EXISTS(SELECT 1 FROM prior.audit.period_vector_baseline q WHERE q.game_id=g.game_id)
            FROM prior.nba.fact_game g JOIN prior.audit.period_coverage p USING(game_id) WHERE g.game_id=?''',[gid]).fetchone()
        if (old is None or old[:5]!=(fixture.game_date,HOME,fixture.away_id,home[-1],away[-1])
            or old[5] not in (None,'Uline Arena') or old[6] is True or old[7:]!=('no_supported_source',0,0,None,False,False)):
            raise RuntimeError(f'Pinned parent fixture/venue/final/gap does not match: {gid}: {old}')
        image = ledger[fixture.page_file]
        printed = [[observed[gid,side,n][1] for n in BOUNDARIES] for side in ('home','away')]
        c.execute('INSERT INTO expected_archive_v23 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [gid,fixture.game_date,HOME,fixture.away_id,fixture.edition,fixture.page,'Uline Arena',fixture.source_url,
             image['source_url'],image['sha256'],home,away,*printed,'derived_from_printed_cumulative',False])


def replay(c: duckdb.DuckDBPyConnection, parent: Path, cache: Path, ledger: dict[str,dict[str,str]]) -> int:
    inputs(c,parent,cache,ledger)
    for rel, expected in (
        (RECEIPT,'SELECT * FROM expected_archive_v23'),
        (FROZEN,'SELECT * FROM prior.audit.period_availability_v7 SEMI JOIN expected_archive_v23 USING(game_id)'),
    ):
        if difference(c,f'SELECT * FROM {rel}',expected):
            raise RuntimeError(f'Exact archival replay differs: {rel}')
    if c.execute(f'SELECT * FROM {PINNED} ORDER BY role').fetchall()!=PINS:
        raise RuntimeError('Archival input pins differ')
    for rel in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline','nba.game_overtime'):
        query = f'SELECT * FROM {rel} ANTI JOIN expected_archive_v23 USING(game_id)'
        if difference(c,query,query.replace(f'FROM {rel}',f'FROM prior.{rel}',1)):
            raise RuntimeError(f'Out-of-scope delta: {rel}')
    skip = 'ANTI JOIN expected_archive_v23 USING(game_id)'
    if difference(c,f'SELECT * FROM audit.period_availability_v23 {skip}',
                  f'SELECT * FROM prior.audit.period_availability_v22 {skip}'):
        raise RuntimeError('V23 availability changed outside the two archival fixtures')
    checks = verify(c)
    for rel in UNCHANGED:
        verify_inherited(c,rel)
    return checks


def stage(c: duckdb.DuckDBPyConnection, parent: Path, cache: Path, ledger: dict[str,dict[str,str]]) -> None:
    c.begin()
    c.execute(DDL)
    inputs(c,parent,cache,ledger)
    old = c.execute("SELECT sql FROM duckdb_views() WHERE database_name=current_database() AND schema_name='audit' AND view_name='period_availability_v7'").fetchone()
    if old is None:
        raise RuntimeError('Missing inherited archival availability view')
    prefix = 'CREATE VIEW audit.period_availability_v7 AS '
    if not str(old[0]).startswith(prefix):
        raise RuntimeError('Unexpected inherited archival availability definition')
    body = str(old[0]).removeprefix(prefix).removesuffix(';')
    c.execute(f'''CREATE TABLE {FROZEN} AS SELECT * FROM audit.period_availability_v7
        SEMI JOIN expected_archive_v23 USING(game_id);
        INSERT INTO {RECEIPT} SELECT * FROM expected_archive_v23;
        CREATE OR REPLACE VIEW audit.period_availability_v7 AS
        SELECT * FROM ({body}) ANTI JOIN {FROZEN} USING(game_id) UNION ALL SELECT * FROM {FROZEN};
        INSERT INTO nba.fact_game_period_score SELECT * FROM {CELLS};
        INSERT INTO audit.period_vector_baseline
        SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
            ORDER BY team_id,period_number))) FROM {CELLS} GROUP BY game_id;
        UPDATE audit.period_coverage SET status='published',candidate_sources=1,valid_sources=1,
            selected_source=? WHERE game_id IN (SELECT game_id FROM {RECEIPT});''',[SOURCE])
    c.executemany(f'INSERT INTO {PINNED} VALUES (?,?)',PINS)
    c.commit()
    c.execute('CHECKPOINT')


def run(action: str, parent: Path, cache: Path, output: Path) -> None:
    wal = Path(f'{output}.wal')
    if action=='build' and (os.path.lexists(output) or os.path.lexists(wal)):
        raise FileExistsError(f'Will not overwrite {output} or its WAL')
    initial_parent = snapshot(parent)
    if sha256(parent)!=PARENT_SHA:
        raise RuntimeError('Expected exact checkpointed v22 parent')
    ledger, evidence_paths = evidence(cache)
    paths = (*evidence_paths,output) if action=='verify-inputs' else evidence_paths
    initial_inputs = tuple(map(snapshot,paths))
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-v23-spill-') as spill:
        if action=='verify-inputs':
            with duckdb.connect(str(output),read_only=True,config=open_config(spill)) as c:
                checks = replay(c,parent,cache,ledger)
        else:
            with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v23-') as work:
                candidate = Path(work)/'release.duckdb'
                shutil.copyfile(parent,candidate)
                with duckdb.connect(str(candidate),config=open_config(spill)) as c:
                    stage(c,parent,cache,ledger)
                snapshot(candidate)
                with duckdb.connect(str(candidate),read_only=True,config=open_config(spill)) as c:
                    checks = replay(c,parent,cache,ledger)
                evidence(cache)
                if snapshot(parent)!=initial_parent or tuple(map(snapshot,paths))!=initial_inputs or os.path.lexists(wal):
                    raise RuntimeError('Input changed or destination WAL exists; release not installed')
                os.link(candidate,output)
    if action=='verify-inputs':
        evidence(cache)
    if snapshot(parent)!=initial_parent or tuple(map(snapshot,paths))!=initial_inputs:
        raise RuntimeError('An input changed during archival replay')
    print(f'{checks} quality checks passed; 24 pinned files, two archival vectors and {len(UNCHANGED)} inherited relations replayed: {output}')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('build','verify-inputs'))
    p.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v22.duckdb')
    p.add_argument('--cache',type=Path,default=CACHE)
    p.add_argument('--output',type=Path,required=True)
    a = p.parse_args()
    run(a.action,a.parent.resolve(strict=True),a.cache.resolve(strict=True),a.output.absolute())


if __name__=='__main__':
    main()
