"""Recover the ten missing 2025–26 regular-season period vectors with exact crosswalks."""
from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import NamedTuple

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, sql_string, verify
from recover_archive_periods import sha256
from recover_local_periods import difference, snapshot
from validate_current_results import csv_rows, unique_index


class Receipt(NamedTuple):
    game_id: str
    game_date: date
    home_team_id: int
    away_team_id: int
    espn_game_id: int
    home_q: tuple[int, ...]
    away_q: tuple[int, ...]
    source_url: str


def receipts_from(path: Path) -> list[Receipt]:
    rows = unique_index(csv_rows(path),'game_id')
    result = []
    for gid,row in rows.items():
        day = date.fromisoformat(row['game_date'])
        h,a = (tuple(map(int,row[k].split('|'))) for k in ('home_q','away_q'))
        if (not re.fullmatch(r'00225[0-9]{5}',gid) or not date(2025,10,21)<=day<=date(2026,4,12)
          or len(h)!=4 or len(a)!=4 or min(*h,*a)<0 or any(x+y==0 for x,y in zip(h,a,strict=True)) or sum(h)==sum(a)):
            raise ValueError(f'Invalid regular-season line score: {gid}')
        result.append(Receipt(gid,day,int(row['home_team_id']),int(row['away_team_id']),int(row['espn_game_id']),h,a,row['source_url']))
    if len(result)!=10 or len({r.espn_game_id for r in result})!=10:
        raise ValueError('Expected ten unique keyed receipts')
    return result


def compare_raw(con: duckdb.DuckDBPyConnection, raw: Path) -> None:
    con.execute(f'ATTACH {sql_string(str(raw))} AS raw (READ_ONLY)')
    raw_id = snapshot(raw)
    if con.execute('SELECT source_size,source_mtime_ns FROM audit.input_manifest').fetchall()!=[(raw_id.size,raw_id.mtime_ns)]:
        raise ValueError('Raw database does not match the parent manifest')
    con.execute((WAREHOUSE/'compare_official_periods.sql').read_text())


def verify_inputs(parent: Path, raw: Path, receipt_path: Path, release: Path) -> None:
    for p in (parent,raw,release):
        snapshot(p)
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='period-comparison-spill-') as spill, \
         duckdb.connect(str(release),read_only=True,config={'threads':1,'memory_limit':'1GB','temp_directory':spill}) as con:
        con.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
        if con.execute('SELECT role,sha256 FROM audit.official_period_input ORDER BY role').fetchall()!=[
            ('parent_v8',sha256(parent)),('receipt_csv',sha256(receipt_path))]:
            raise ValueError('Pinned parent or receipt digest differs')
        con.execute('CREATE TEMP TABLE receipt AS SELECT * FROM audit.official_period_receipt LIMIT 0')
        con.executemany('INSERT INTO receipt VALUES (?,?,?,?,?,?,?,?)',receipts_from(receipt_path))
        if difference(con,'SELECT * FROM receipt','SELECT * FROM audit.official_period_receipt'):
            raise ValueError('Stored receipt values differ from receipt file')
        compare_raw(con,raw)
        if difference(con,'SELECT * FROM raw_gamebook_comparison','SELECT * FROM audit.official_period_comparison'):
            raise ValueError('Stored provider comparison is not reproducible')
        prior_keys = '''SELECT DISTINCT b.game_id,b.espn_game_id FROM raw.main.bridge_game_espn_base b
            JOIN prior.audit.period_coverage p USING(game_id) JOIN prior.nba.dim_game g USING(game_id)
            WHERE p.status<>'published' AND b.match_method='crosswalk_exact' AND b.espn_game_id IS NOT NULL
              AND g.season_year='2025-26' AND g.season_type='Regular' '''
        if difference(con,'SELECT game_id,espn_game_id FROM receipt',prior_keys):
            raise ValueError('Receipt set differs from the defined keyed regular-season backlog')
        for rel in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline'):
            a=f'SELECT * FROM {rel} ANTI JOIN receipt USING(game_id)'
            b=f'SELECT * FROM prior.{rel} ANTI JOIN receipt USING(game_id)'
            if difference(con,a,b):
                raise ValueError(f'Unrelated prior period data changed: {rel}')
        for rel in ('nba.dim_game','nba.fact_game_result','nba.bridge_game_team','nba.fact_regular_standings','audit.period_adjudication'):
            if difference(con,f'SELECT * FROM {rel}',f'SELECT * FROM prior.{rel}'):
                raise ValueError(f'Unrelated historical data changed: {rel}')
        print(con.execute('SELECT comparison_status,count(*) FROM audit.official_period_comparison GROUP BY 1 ORDER BY 1').fetchall())
        print(f'{verify(con)} quality checks passed; pinned parent, gamebook receipts and local provider comparisons verified')


def build(parent: Path, raw: Path, receipt_path: Path, output: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    paths = (parent,raw,receipt_path)
    initial = tuple(snapshot(p) for p in paths)
    receipts = receipts_from(receipt_path)
    pins = [('parent_v8',sha256(parent)),('receipt_csv',sha256(receipt_path))]
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.official-periods-') as work, \
         tempfile.TemporaryDirectory(dir=SCRATCH,prefix='official-period-spill-') as spill:
        stage = Path(work)/'release.duckdb'
        shutil.copyfile(parent,stage)
        with duckdb.connect(str(stage),config={'threads':1,'memory_limit':'1GB','temp_directory':spill}) as con:
            con.begin()
            con.execute('''CREATE TABLE audit.official_period_receipt(
                game_id VARCHAR PRIMARY KEY,game_date DATE NOT NULL,home_team_id BIGINT NOT NULL,away_team_id BIGINT NOT NULL,
                espn_game_id BIGINT NOT NULL UNIQUE,home_q INTEGER[4] NOT NULL,away_q INTEGER[4] NOT NULL,source_url VARCHAR NOT NULL UNIQUE);
                CREATE TABLE audit.official_period_input(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);''')
            con.executemany('INSERT INTO audit.official_period_receipt VALUES (?,?,?,?,?,?,?,?)',receipts)
            con.executemany('INSERT INTO audit.official_period_input VALUES (?,?)',pins)
            compare_raw(con,raw)
            con.execute('CREATE TABLE audit.official_period_comparison AS SELECT * FROM raw_gamebook_comparison')
            if con.execute('''SELECT count(*) FROM audit.official_period_receipt r JOIN nba.fact_game g USING(game_id)
              JOIN audit.period_coverage p USING(game_id) WHERE p.status='no_valid_candidate' AND g.season_type='Regular'
              AND (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
                 = (r.game_date,r.home_team_id,r.away_team_id,list_sum(r.home_q),list_sum(r.away_q))
              AND NOT EXISTS(SELECT 1 FROM nba.fact_game_period_score f WHERE f.game_id=r.game_id)''').fetchone()!=(len(receipts),):
                raise ValueError('Mismatched fixture or existing period coverage')
            con.execute('''INSERT INTO nba.fact_game_period_score
              SELECT r.game_id,b.team_id,i,CASE b.side WHEN 'home' THEN r.home_q[i] ELSE r.away_q[i] END
              FROM audit.official_period_receipt r JOIN nba.bridge_game_team b USING(game_id),range(1,5) n(i);
              UPDATE audit.period_coverage p SET status='published',candidate_sources=p.candidate_sources+1,
                 valid_sources=p.valid_sources+1,selected_source='official_period_receipt'
              FROM audit.official_period_receipt r WHERE p.game_id=r.game_id;
              INSERT INTO audit.period_vector_baseline SELECT o.* FROM audit.period_vector_observed o SEMI JOIN audit.official_period_receipt USING (game_id);
              CREATE VIEW audit.period_availability_v9 AS
              SELECT v.* REPLACE (CASE WHEN r.game_id IS NOT NULL THEN 'official_period_receipt' ELSE v.recovery_disposition END AS recovery_disposition)
              FROM audit.period_availability_v8 v LEFT JOIN audit.official_period_receipt r USING(game_id);''')
            con.commit()
            con.execute('CHECKPOINT')
        verify_inputs(parent,raw,receipt_path,stage)
        if tuple(snapshot(p) for p in paths)!=initial or os.path.lexists(f'{output}.wal'):
            raise ValueError('Input changed or output WAL exists')
        os.link(stage,output)
        print(output)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v8.duckdb')
    parser.add_argument('--raw',type=Path,default=ROOT/'data/nba.duckdb')
    parser.add_argument('--receipts',type=Path,default=WAREHOUSE/'official_period_receipts_2025_26.csv')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    parent,raw,receipt_path=(getattr(args,k).resolve(strict=True) for k in ('parent','raw','receipts'))
    if args.action=='build':
        build(parent,raw,receipt_path,args.output.absolute())
    else:
        verify_inputs(parent,raw,receipt_path,args.output.resolve(strict=True))
