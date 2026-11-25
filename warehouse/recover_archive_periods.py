"""Admit four transcribed 1961 Finals vectors to a new historical warehouse release."""
from __future__ import annotations

import argparse
import csv
from datetime import date
from hashlib import file_digest
import os
from pathlib import Path
import shutil
import tempfile
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, sql_string, verify
from recover_local_periods import difference, snapshot


class Receipt(NamedTuple):
    game_id: str
    game_date: date
    series_game_number: int
    home_team_id: int
    away_team_id: int
    home_q: tuple[int, ...]
    away_q: tuple[int, ...]
    source_url: str


def load_receipts(path: Path) -> list[Receipt]:
    receipts = []
    with path.open(newline='') as stream:
        for row in csv.DictReader(stream):
            fixture = int(row['series_game_number'])
            game_id = row['game_id']
            game_date = date.fromisoformat(row['game_date'])
            url = urlparse(row['source_url'])
            home_q = tuple(map(int,row['home_q'].split('|')))
            away_q = tuple(map(int,row['away_q'].split('|')))
            game_pages = {1:('1961-04-03','15'),2:('1961-04-06','57'),3:('1961-04-09','41'),5:('1961-04-12','65')}
            article, page = game_pages[fixture]
            if (game_id!=f'004600041{fixture}' or (url.scheme,url.netloc)!=('https','www.loc.gov') or
                url.path!=f'/resource/sn83045462/{article}/ed-1/' or parse_qs(url.query)!={'sp':[page]} or
                len(home_q)!=4 or len(away_q)!=4 or min(*home_q,*away_q)<0 or
                any(h+a==0 for h,a in zip(home_q,away_q,strict=True))):
                raise ValueError(f'Invalid archival receipt: {game_id}')
            receipts.append(Receipt(game_id,game_date,fixture,int(row['home_team_id']),int(row['away_team_id']),home_q,away_q,row['source_url']))
    if len(receipts)!=4 or {r.series_game_number for r in receipts}!={1,2,3,5}:
        raise ValueError('Expected exactly four archive-verified 1961 Finals games; Game 4 is not in scope')
    return receipts


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return file_digest(stream,'sha256').hexdigest()


def verify_inputs(base: Path, release: Path, receipts_path: Path) -> None:
    """Check a release against external inputs; not just its mutable internal baseline."""
    for p in (base,release):
        snapshot(p)
    with duckdb.connect(str(release),read_only=True,config={'threads':2,'memory_limit':'2GB'}) as con:
        con.execute(f'ATTACH {sql_string(str(base))} AS base (READ_ONLY)')
        if con.execute('SELECT role,sha256 FROM audit.period_integrity_input ORDER BY role').fetchall()!=[
            ('archive_transcription',sha256(receipts_path)),('parent_v6',sha256(base))]:
            raise RuntimeError('Archive transcription or v6 parent differs from the pinned input')
        con.execute('CREATE TEMP TABLE external_receipts AS SELECT * FROM audit.archive_period_receipt LIMIT 0')
        con.executemany('INSERT INTO external_receipts VALUES (?,?,?,?,?,?,?,?)',load_receipts(receipts_path))
        if difference(con,'SELECT * FROM external_receipts','SELECT * FROM audit.archive_period_receipt'):
            raise RuntimeError('Stored archive receipt differs from the external transcription')
        for domain in ('nba.fact_game_period_score','audit.period_coverage'):
            cols = f'SELECT a.* FROM {domain} a ANTI JOIN external_receipts r USING(game_id)'
            orig = f'SELECT a.* FROM base.{domain} a ANTI JOIN external_receipts r USING(game_id)'
            if difference(con,cols,orig):
                raise RuntimeError(f'{domain}: previously approved period history changed')
        for rel in ('nba.dim_game','nba.fact_game_result','nba.bridge_game_team','nba.fact_regular_standings','audit.local_period_recovery'):
            if difference(con,f'SELECT * FROM {rel}',f'SELECT * FROM base.{rel}'):
                raise RuntimeError(f'{rel} changed outside archival recovery scope')
        checks = verify(con)
    print(f'{checks} warehouse checks passed; v6 parent and external archive transcriptions reconciled')


def build(base: Path, receipts_path: Path, output: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    base_id, receipts_id = snapshot(base), snapshot(receipts_path)
    verified = load_receipts(receipts_path)
    digests = [('archive_transcription',sha256(receipts_path)),('parent_v6',sha256(base))]
    SCRATCH.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.archive-periods-') as work:
        stage = Path(work)/'release.duckdb'
        shutil.copyfile(base,stage)
        with duckdb.connect(str(stage),config={'threads':2,'memory_limit':'2GB'}) as con:
            con.begin()
            con.execute('''CREATE TABLE audit.archive_period_receipt(
                game_id VARCHAR PRIMARY KEY,game_date DATE NOT NULL,series_game_number INTEGER NOT NULL,
                home_team_id BIGINT NOT NULL,away_team_id BIGINT NOT NULL,
                home_q INTEGER[4] NOT NULL,away_q INTEGER[4] NOT NULL,source_url VARCHAR NOT NULL UNIQUE)''')
            con.executemany('INSERT INTO audit.archive_period_receipt VALUES (?,?,?,?,?,?,?,?)',verified)
            if con.execute('''SELECT count(*) FROM audit.archive_period_receipt a JOIN nba.fact_game g USING(game_id)
                JOIN audit.period_coverage c USING(game_id) WHERE c.status='no_supported_source' AND g.game_label='NBA Finals'
                AND (g.game_date,g.series_game_number,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
                 = (a.game_date,a.series_game_number,a.home_team_id,a.away_team_id,list_sum(a.home_q),list_sum(a.away_q))''').fetchone()!=(len(verified),):
                raise RuntimeError('An archive receipt does not agree with the keyed fixture or would overwrite period history')
            con.execute('''INSERT INTO nba.fact_game_period_score
                SELECT r.game_id,b.team_id,i,CASE b.side WHEN 'home' THEN r.home_q[i] ELSE r.away_q[i] END
                FROM audit.archive_period_receipt r JOIN nba.bridge_game_team b USING(game_id),range(1,5) n(i);
                UPDATE audit.period_coverage c SET status='published',candidate_sources=candidate_sources+1,
                    valid_sources=valid_sources+1,selected_source='archival_line_score'
                FROM audit.archive_period_receipt a WHERE c.game_id=a.game_id;
                CREATE TABLE audit.period_integrity_input(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);''')
            con.executemany('INSERT INTO audit.period_integrity_input VALUES (?,?)',digests)
            con.execute((WAREHOUSE/'period_integrity.sql').read_text())
            con.commit()
            con.execute('CHECKPOINT')
        verify_inputs(base,stage,receipts_path)
        if snapshot(base)!=base_id or snapshot(receipts_path)!=receipts_id or os.path.lexists(f'{output}.wal'):
            raise RuntimeError('An input changed or output WAL exists; release was not installed')
        os.link(stage,output)
        print(f'Four archival game line scores installed in {output}')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--base',type=Path,default=ROOT/'data/nba-clean-v6.duckdb')
    parser.add_argument('--receipts',type=Path,default=WAREHOUSE/'archive_period_receipts.csv')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    base,args.receipts = args.base.resolve(strict=True),args.receipts.resolve(strict=True)
    if args.action=='build':
        build(base,args.receipts,args.output.absolute())
    else:
        verify_inputs(base,args.output.resolve(strict=True),args.receipts)
