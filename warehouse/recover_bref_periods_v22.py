"""Reconcile the seven locally reviewed Basketball-Reference period candidates."""
from __future__ import annotations

import argparse
from hashlib import sha256 as hash_bytes
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

import duckdb

from fetch_bref_periods import ORIGIN, fixtures, parse_box
from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, sql_string, verify
from recover_archive_periods import sha256
from recover_bref_periods_v21 import UNCHANGED
from recover_legacy_attempts_v18 import verify_inherited
from recover_local_periods import difference, snapshot
from recover_pbp_attempts import open_config

PARENT_SHA = '057f3ab021c87bc2c048c86e701c40dc4f01592815183b982c07727ac788625f'
SEED = WAREHOUSE/'bref_period_seed02.csv'
CACHE = Path.home()/'Library/Application Support/NBA Warehouse/evidence/bref-v22'
SOURCE = 'bref_v22_periods_match_reoriented_local_cells'
PINS = {
    '0025100086': ('4b43b8b8293b62a8238f9b31826f9a18617b5f2e6857a787022f80a101b18d6e', 'admitted'),
    '0025200313': ('00278237859adadc668f5f16d9ebd24ae71722c90285405d6ab1005b00377615', 'admitted'),
    '0025400246': ('5686ce65e84ff42b0d48ef4093f895f5f5afc3f50297b666fbc62aa2973e05ae', 'held_local_cells_conflict'),
    '0025500205': ('c6315a8ab1e05cde22e6db13e7d3b623374a16b91b21391c8a93285077e39f97', 'held_local_cells_conflict'),
    '0025500208': ('ba1b6ed4914b5a4f1770a8e819a0ba41ca79f84d807b3d37487aee3646320e4b', 'held_local_cells_conflict'),
    '0025500269': ('929a22b33157783cc9a08db70c3925ee3cd24e12d65c3292897be3c337b2b2d5', 'held_local_cells_conflict'),
    '0025900028': ('0b0c6cb05415faba1c5f0fb1df8bdeacbc2aa6e27bed663df96271c18bea3cef', 'held_local_cells_conflict'),
}
DDL = f'''CREATE TABLE audit.bref_period_receipt_v22(
 game_id VARCHAR PRIMARY KEY, game_date DATE NOT NULL, home_id BIGINT NOT NULL, away_id BIGINT NOT NULL,
 home_total INT NOT NULL, away_total INT NOT NULL, source_url VARCHAR NOT NULL UNIQUE,
 sha256 VARCHAR NOT NULL, home_label VARCHAR NOT NULL, away_label VARCHAR NOT NULL,
 home_periods INT[4] NOT NULL, away_periods INT[4] NOT NULL,
 local_home_periods INT[4] NOT NULL, local_away_periods INT[4] NOT NULL,
 old_status VARCHAR NOT NULL, old_candidates INT NOT NULL, disposition VARCHAR NOT NULL,
 redistribution_cleared BOOLEAN NOT NULL CHECK(NOT redistribution_cleared),
 CHECK(disposition IN ('admitted','held_local_cells_conflict')));
 CREATE TABLE audit.bref_period_input_v22(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);
 CREATE VIEW audit.bref_period_cells_v22 AS
 SELECT r.game_id,b.team_id,i::SMALLINT AS period_number,
        CASE b.side WHEN 'home' THEN r.home_periods[i] ELSE r.away_periods[i] END AS points
 FROM audit.bref_period_receipt_v22 r JOIN nba.bridge_game_team b USING(game_id),range(1,5) p(i)
 WHERE r.disposition='admitted';
 CREATE VIEW audit.period_availability_v22 AS
 SELECT v.* REPLACE(coalesce(CASE WHEN r.disposition='admitted' THEN '{SOURCE}' END,
        v.recovery_disposition) AS recovery_disposition)
 FROM audit.period_availability_v21 v LEFT JOIN audit.bref_period_receipt_v22 r USING(game_id);'''


def inputs(c: duckdb.DuckDBPyConnection, parent: Path, cache: Path) -> None:
    c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    scope = fixtures(parent, SEED)
    if {f.game_id for f in scope} != PINS.keys():
        raise RuntimeError('Unexpected reviewed fixture scope')
    rows: list[tuple[Any, ...]] = []
    for f in scope:
        digest, expected_disposition = PINS[f.game_id]
        raw = (cache/Path(f.source_path).name).read_bytes()
        if hash_bytes(raw).hexdigest() != digest:
            raise RuntimeError(f'Original receipt bytes differ: {f.game_id}')
        obs = parse_box(f, raw)
        if obs['status'] != 'matched_fixture_exact_printed_periods':
            raise RuntimeError(f'Fixture date, designated sides or totals differ: {f.game_id}')
        observations = c.execute('''SELECT is_home,team_id,opponent_team_id,game_date,pts,
                     list_transform(regulation,lambda n: CASE WHEN isfinite(n) AND n=trunc(n) AND n>=0
                       THEN try_cast(n AS INTEGER) END) AS vector
          FROM prior.audit.mirror_v14_team WHERE game_id=? ORDER BY is_home DESC''', [f.game_id]).fetchall()
        ids = [row[:5] for row in observations]
        if ids != [(True,f.home_id,f.away_id,f.day,f.home_final),(False,f.away_id,f.home_id,f.day,f.away_final)]:
            raise RuntimeError(f'Local observations disagree on fixture keys: {f.game_id}')
        home, away = [list(row[-1]) for row in observations]
        if any(len(vec)!=4 or any(n is None for n in vec) for vec in (home,away)):
            raise RuntimeError(f'Local regulation cells unavailable: {f.game_id}')
        # The team-keyed final headers are correct; the two quarter payloads are crossed.
        disposition = ('admitted' if (obs['home_periods'],obs['away_periods'])==(away,home)
                       else 'held_local_cells_conflict')
        if disposition != expected_disposition:
            raise RuntimeError(f'Unexpected local cell comparison: {f.game_id}')
        old = c.execute('''SELECT status,candidate_sources,valid_sources,selected_source,
                  EXISTS(SELECT 1 FROM prior.nba.fact_game_period_score p WHERE p.game_id=c.game_id)
            FROM prior.audit.period_coverage c WHERE game_id=?''', [f.game_id]).fetchone()
        if old is None or old[2:] != (0,None,False):
            raise RuntimeError(f'Expected an unpublished prior fixture: {f.game_id}')
        rows.append((f.game_id,f.day,f.home_id,f.away_id,f.home_final,f.away_final,ORIGIN+f.source_path,
                     digest,obs['source_home'],obs['source_away'],obs['home_periods'],obs['away_periods'],
                     home,away,old[0],old[1],disposition,False))
    c.execute('CREATE TEMP TABLE expected_v22 AS SELECT * FROM audit.bref_period_receipt_v22 LIMIT 0')
    c.executemany('INSERT INTO expected_v22 VALUES ('+','.join('?' for _ in range(18))+')', rows)


def replay(c: duckdb.DuckDBPyConnection, parent: Path, cache: Path) -> int:
    inputs(c, parent, cache)
    if difference(c,'SELECT * FROM expected_v22','SELECT * FROM audit.bref_period_receipt_v22'):
        raise RuntimeError('V22 receipt differs from independent replay')
    if c.execute('SELECT * FROM audit.bref_period_input_v22').fetchall() != [('parent_v21',PARENT_SHA)]:
        raise RuntimeError('Parent manifest differs')
    nchecks = verify(c)
    for rel in (*UNCHANGED,'nba.fact_play_event','audit.event_coverage','audit.shot_coverage',
                'audit.bref_period_receipt_v21'):
        verify_inherited(c,rel)
    skip = "ANTI JOIN (SELECT game_id FROM expected_v22 WHERE disposition='admitted') USING(game_id)"
    for rel in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline','nba.game_overtime',
                'audit.period_availability_v21'):
        if difference(c,f'SELECT * FROM {rel} {skip}',f'SELECT * FROM prior.{rel} {skip}'):
            raise RuntimeError(f'Out-of-scope delta in {rel}')
    if difference(c,f'SELECT * FROM audit.period_availability_v22 {skip}',
                  f'SELECT * FROM prior.audit.period_availability_v21 {skip}'):
        raise RuntimeError('V22 availability changed outside admitted fixtures')
    return nchecks


def run(action: str, parent: Path, cache: Path, output: Path) -> None:
    if action=='build' and (os.path.lexists(output) or os.path.lexists(f'{output}.wal')):
        raise FileExistsError(output)
    paths = (parent,SEED,*(cache/Path(f.source_path).name for f in fixtures(parent,SEED)))
    if action!='build':
        paths = (*paths,output)
    initial = tuple(map(snapshot,paths))
    if sha256(parent) != PARENT_SHA:
        raise RuntimeError('Expected exact checkpointed v21 parent')
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-v22-spill-') as spill:
        if action=='verify-inputs':
            with duckdb.connect(str(output),read_only=True,config=open_config(spill)) as c:
                nchecks = replay(c,parent,cache)
        else:
            with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v22-') as work:
                candidate = Path(work)/'release.duckdb'
                shutil.copyfile(parent,candidate)
                with duckdb.connect(str(candidate),config=open_config(spill)) as c:
                    c.execute(DDL)
                    inputs(c,parent,cache)
                    c.begin()
                    c.execute('''INSERT INTO audit.bref_period_receipt_v22 SELECT * FROM expected_v22;
                      INSERT INTO nba.fact_game_period_score SELECT * FROM audit.bref_period_cells_v22;
                      INSERT INTO audit.period_vector_baseline
                      SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
                        ORDER BY team_id,period_number))) FROM audit.bref_period_cells_v22 GROUP BY game_id;
                      UPDATE audit.period_coverage c SET status='published',candidate_sources=r.old_candidates+1,
                          valid_sources=1,selected_source=? FROM expected_v22 r
                      WHERE c.game_id=r.game_id AND r.disposition='admitted';''', [SOURCE])
                    c.execute("INSERT INTO audit.bref_period_input_v22 VALUES ('parent_v21',?)", [PARENT_SHA])
                    c.commit()
                    c.execute('CHECKPOINT')
                snapshot(candidate)
                with duckdb.connect(str(candidate),read_only=True,config=open_config(spill)) as c:
                    nchecks = replay(c,parent,cache)
                if tuple(map(snapshot,paths))!=initial or os.path.lexists(f'{output}.wal'):
                    raise RuntimeError('An input changed or destination WAL exists')
                os.link(candidate,output)
    if tuple(map(snapshot,paths)) != initial:
        raise RuntimeError('An input changed during replay')
    print(f'{nchecks} checks passed; seven exact receipt replays and parent deltas verified: {output}')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('build','verify-inputs'))
    p.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v21.duckdb')
    p.add_argument('--cache',type=Path,default=CACHE)
    p.add_argument('--output',type=Path,required=True)
    a = p.parse_args()
    run(a.action,a.parent.resolve(strict=True),a.cache.resolve(strict=True),a.output.absolute())


if __name__=='__main__':
    main()
