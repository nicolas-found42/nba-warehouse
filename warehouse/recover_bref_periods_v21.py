#!/usr/bin/env python3
"""Admit the two exact, locally corroborated Basketball-Reference period receipts."""
from __future__ import annotations

import argparse
from hashlib import sha256 as hash_bytes
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

import duckdb

from fetch_bref_periods import ORIGIN,fixtures,parse_box
from nba_warehouse import ROOT,SCRATCH,WAREHOUSE,sql_string,verify
from recover_archive_periods import sha256

from recover_legacy_attempts_v18 import verify_inherited
from recover_local_periods import difference,snapshot
from recover_pbp_attempts import open_config

PARENT_SHA = 'ca61d23dacd85a9c2666a23ad1d7213c8e3de968cb50c54825eddd23f6d31659'
SEED = WAREHOUSE/'bref_period_seed.csv'
PINS = {
 '0025100035': ('a3f9a185d2a7a51dbd8723e9281c23bc266c5cbaf7e38e39f7fc6bd4b7fedb8b','matched_fixture_exact_printed_periods','admitted'),
 '0025100052': ('ca6cc6d4c5ec1c08eaecf9f9049ae6f74d617ac4f06c9dc900c0f52d87954107','matched_fixture_exact_printed_periods','admitted'),
 '0025600127': ('861a43b2f0115699ca9936e50fb6e3ae52557bc65b3b638deddef37fc363b31a','designated_side_or_team_disagrees','held_designated_side'),
 '0024600169': ('44abf0c568a6b59afda6a1ab8bd4e5ed910ab587fa9b8a33c9eb0aab43e85256','matched_fixture_exact_printed_periods','held_local_cell_conflict'),
}
UNCHANGED = ('nba.dim_game','nba.fact_game_result','nba.bridge_game_team','nba.fact_regular_standings',
             'nba.fact_team_game_stats','nba.fact_player_game_stats','nba.fact_pbp_field_goal_attempt',
             'nba.fact_shot','nba.bridge_shot_event','nba.bridge_shot_event_source_v20','audit.local_period_recovery')
DDL = '''CREATE TABLE audit.bref_period_receipt_v21(
 game_id VARCHAR PRIMARY KEY,game_date DATE NOT NULL,home_id BIGINT NOT NULL,away_id BIGINT NOT NULL,
 home_total INT NOT NULL,away_total INT NOT NULL,source_url VARCHAR NOT NULL,sha256 VARCHAR NOT NULL,
 expected_home VARCHAR NOT NULL,expected_away VARCHAR NOT NULL,home_label VARCHAR,away_label VARCHAR,
 home_periods INT[4],away_periods INT[4],status VARCHAR NOT NULL,disposition VARCHAR NOT NULL,
 old_status VARCHAR NOT NULL,old_candidates INT NOT NULL,redistribution_cleared BOOLEAN CHECK(redistribution_cleared=false));
 CREATE TABLE audit.bref_period_input_v21(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);
 CREATE VIEW audit.bref_period_cells_v21 AS
 SELECT r.game_id,b.team_id,i::SMALLINT AS period_number,
       CASE b.side WHEN 'home' THEN r.home_periods[i] ELSE r.away_periods[i] END AS points
 FROM audit.bref_period_receipt_v21 r JOIN nba.bridge_game_team b USING(game_id),range(1,5) p(i)
 WHERE disposition='admitted';
 CREATE VIEW audit.period_availability_v21 AS
 SELECT v.* REPLACE(coalesce(CASE WHEN r.disposition='admitted' THEN 'bref_v21_printed_line_score' END,
            v.recovery_disposition) AS recovery_disposition)
 FROM audit.period_availability_v19 v LEFT JOIN audit.bref_period_receipt_v21 r USING(game_id);'''


def inputs(c: duckdb.DuckDBPyConnection,parent: Path,cache: Path) -> None:
    c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    fxs=fixtures(parent,SEED)
    if {f.game_id for f in fxs}!=PINS.keys():
        raise RuntimeError('Unexpected Basketball-Reference accession scope')
    rows: list[tuple[Any,...]] = []
    for f in fxs:
        digest,status,disposition=PINS[f.game_id]
        path=cache/Path(f.source_path).name
        snapshot(path)
        raw=path.read_bytes()

        if hash_bytes(raw).hexdigest()!=digest:
            raise RuntimeError(f'Source HTML differs from the live pin: {f.game_id}')
        obs=parse_box(f,raw)
        if obs['status']!=status:
            raise RuntimeError(f'Source replay differs from the verified disposition: {f.game_id}')
        prior=c.execute('SELECT status,candidate_sources,valid_sources FROM prior.audit.period_coverage WHERE game_id=?',[f.game_id]).fetchone()
        if prior is None or prior[2]!=0:
            raise RuntimeError(f'Prior hold missing: {f.game_id}')
        rows.append((f.game_id,f.day,f.home_id,f.away_id,f.home_final,f.away_final,ORIGIN+f.source_path,digest,
                     f.expected_home,f.expected_away,obs.get('source_home'),obs.get('source_away'),
                     obs.get('home_periods'),obs.get('away_periods'),status,disposition,prior[0],prior[1],False))
    c.execute('CREATE TEMP TABLE reviewed_bref_v21 AS SELECT * FROM audit.bref_period_receipt_v21 LIMIT 0')
    c.executemany('INSERT INTO reviewed_bref_v21 VALUES ('+','.join('?' for _ in range(19))+')',rows)


def replay(c: duckdb.DuckDBPyConnection,parent: Path,cache: Path) -> int:
    inputs(c,parent,cache)
    if c.execute('SELECT * FROM audit.bref_period_input_v21').fetchall()!=[('parent_v20',PARENT_SHA)]:
        raise RuntimeError('Pinned v20 role differs')
    if difference(c,'SELECT * FROM reviewed_bref_v21','SELECT * FROM audit.bref_period_receipt_v21'):
        raise RuntimeError('Re-parsed live Basketball-Reference rows differ from the release ledger')
    checks=verify(c)
    for rel in UNCHANGED:
        verify_inherited(c,rel)
    for rel in ('audit.event_coverage','audit.shot_coverage'):
        verify_inherited(c,rel)
    for rel in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline','nba.game_overtime'):
        omit="ANTI JOIN (SELECT game_id FROM reviewed_bref_v21 WHERE disposition='admitted') USING(game_id)"
        if difference(c,f'SELECT * FROM {rel} {omit}',f'SELECT * FROM prior.{rel} {omit}'):
            raise RuntimeError(f'Unscoped change in {rel}')
    return checks


def run(action: str,parent: Path,cache: Path,output: Path) -> None:
    if action=='build' and (os.path.lexists(output) or os.path.lexists(f'{output}.wal')):
        raise FileExistsError(output)
    paths=(parent,SEED,*(cache/Path(f.source_path).name for f in fixtures(parent,SEED)))
    if action!='build':
        paths=(*paths,output)
    initial=tuple(map(snapshot,paths))
    if sha256(parent)!=PARENT_SHA:
        raise RuntimeError('Expected the exact v20 parent')
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-bref-v21-spill-') as spill:
        if action=='verify-inputs':
            with duckdb.connect(str(output),read_only=True,config=open_config(spill)) as c:
                checks=replay(c,parent,cache)
        else:
            with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v21-') as work:
                candidate=Path(work)/'release.duckdb'
                shutil.copyfile(parent,candidate)
                with duckdb.connect(str(candidate),config=open_config(spill)) as c:
                    c.execute(DDL)
                    inputs(c,parent,cache)
                    c.begin()
                    c.execute('''INSERT INTO audit.bref_period_receipt_v21 SELECT * FROM reviewed_bref_v21;
                         INSERT INTO nba.fact_game_period_score SELECT * FROM audit.bref_period_cells_v21;
                         INSERT INTO audit.period_vector_baseline
                         SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
                              ORDER BY team_id,period_number))) FROM audit.bref_period_cells_v21 GROUP BY game_id;
                         UPDATE audit.period_coverage c SET status='published',candidate_sources=r.old_candidates+1,valid_sources=1,
                                selected_source='bref_v21_printed_line_score' FROM reviewed_bref_v21 r
                         WHERE c.game_id=r.game_id AND r.disposition='admitted';''')
                    c.execute("INSERT INTO audit.bref_period_input_v21 VALUES ('parent_v20',?)",[PARENT_SHA])
                    c.commit()
                    c.execute('CHECKPOINT')
                snapshot(candidate)
                with duckdb.connect(str(candidate),read_only=True,config=open_config(spill)) as c:
                    checks=replay(c,parent,cache)
                if tuple(map(snapshot,paths))!=initial or os.path.lexists(f'{output}.wal'):
                    raise RuntimeError('An input changed or destination WAL exists')
                os.link(candidate,output)
    if tuple(map(snapshot,paths))!=initial:
        raise RuntimeError('An input changed during verification')
    print(f'{checks} checks passed; four original Basketball-Reference pages and exact parent deltas verified: {output}')


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('build','verify-inputs'))
    p.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v20.duckdb')
    p.add_argument('--cache',type=Path,default=SCRATCH/'nba-bref-personal')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    run(a.action,a.parent.resolve(strict=True),a.cache.resolve(strict=True),a.output.absolute())


if __name__=='__main__':
    main()
