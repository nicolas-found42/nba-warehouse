"""Resolve the four v7 period conflicts and the 1961 Finals Game 4 gap."""
from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path
import shutil
import tempfile
from typing import NamedTuple
from urllib.parse import urlparse

import duckdb

from nba_warehouse import ROOT, WAREHOUSE, sql_string, verify
from recover_archive_periods import sha256
from recover_local_periods import difference, snapshot
from validate_current_results import csv_rows, unique_index


class Receipt(NamedTuple):
    game_id: str
    game_date: date
    home_team_id: int
    away_team_id: int
    prior_status: str
    evidence_kind: str
    home_q: tuple[int, ...]
    away_q: tuple[int, ...]
    source_url: str
    source_note: str


EVIDENCE = {
    '0021700793': ('official_gamebook','statsdmz.nba.com'),
    '0022400072': ('official_gamebook','statsdmz.nba.com'),
    '0029600106': ('contemporaneous_cumulative_scores','www.mcall.com'),
    '0029600674': ('contemporaneous_box_with_one_cell_reconciled','www.spokesman.com'),
    '0046000414': ('archival_scan','www.loc.gov'),
}


def load_receipts(path: Path) -> list[Receipt]:
    rows = unique_index(csv_rows(path),'game_id')
    if rows.keys()!=EVIDENCE.keys():
        raise ValueError('Expected exactly the five identified adjudications')
    result = []
    for gid,row in rows.items():
        kind,host = EVIDENCE[gid]
        source = urlparse(row['source_url'])
        prior = 'no_supported_source' if kind=='archival_scan' else 'valid_sources_disagree'
        home_q,away_q = (tuple(map(int,row[k].split('|'))) for k in ('home_q','away_q'))
        if (source.scheme,source.netloc)!=('https',host) or not source.path or row['evidence_kind']!=kind or row['prior_status']!=prior:
            raise ValueError(f'Unexpected provenance: {gid}')
        if len(home_q)!=4 or len(away_q)!=4 or min(*home_q,*away_q)<0 or any(h+a==0 for h,a in zip(home_q,away_q,strict=True)):
            raise ValueError(f'Invalid regulation period vector: {gid}')
        result.append(Receipt(gid,date.fromisoformat(row['game_date']),int(row['home_team_id']),int(row['away_team_id']),
                              prior,kind,home_q,away_q,row['source_url'],row['source_note']))
    return result


def check_shooting(con: duckdb.DuckDBPyConnection) -> None:
    # Only the four disputed cells below are asserted. The same raw relation's
    # pts disagrees; derive points from shot counts, not from the erroneous cell.
    rows = con.execute('''SELECT a.game_id,q.period,q.pts,2*q.fgm+q.fg3m+q.ftm AS computed,a.away_q[q.period] AS admitted
      FROM audit.period_adjudication a JOIN raw.main.fact_game_quarter_scores q ON a.game_id=q.game_id AND a.away_team_id=q.team_id
      WHERE (a.game_id,q.period) IN (('0029600106',3),('0029600106',4),('0029600674',2),('0029600674',4))
      ORDER BY a.game_id,q.period''').fetchall()
    if rows!=[('0029600106',3,21,31,31),('0029600106',4,36,26,26),('0029600674',2,16,15,15),('0029600674',4,30,31,31)]:
        raise ValueError(f'Disputed 1996–97 points do not reconcile to shooting inputs: {rows}')


def verify_inputs(parent: Path, release: Path, receipts: Path, raw: Path) -> None:
    for p in (parent,release,raw):
        snapshot(p)
    parsed = load_receipts(receipts)
    with duckdb.connect(str(release),read_only=True,config={'threads':2,'memory_limit':'2GB'}) as con:
        con.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
        con.execute(f'ATTACH {sql_string(str(raw))} AS raw (READ_ONLY)')
        if con.execute('SELECT role,sha256 FROM audit.period_adjudication_input ORDER BY role').fetchall()!=[
                ('adjudication_receipts',sha256(receipts)),('parent_v7',sha256(parent))]:
            raise ValueError('v7 parent or receipt digest changed')
        raw_id = snapshot(raw)
        if con.execute('SELECT source_size,source_mtime_ns FROM prior.audit.input_manifest').fetchall()!=[(raw_id.size,raw_id.mtime_ns)]:
            raise ValueError('Raw snapshot does not match the v7 manifest')
        con.execute('CREATE TEMP TABLE expected_receipts AS SELECT * FROM audit.period_adjudication LIMIT 0')
        con.executemany('INSERT INTO expected_receipts VALUES (?,?,?,?,?,?,?,?,?,?)',parsed)
        if difference(con,'SELECT * FROM expected_receipts','SELECT * FROM audit.period_adjudication'):
            raise ValueError('Stored evidence disagrees with external adjudication receipts')
        if difference(con,'SELECT game_id,prior_status FROM expected_receipts',
             'SELECT p.game_id,p.status FROM prior.audit.period_coverage p SEMI JOIN expected_receipts USING(game_id)'):
            raise ValueError('Previous disposition changed or expected fixture is absent')
        for table in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline'):
            if difference(con,f'SELECT * FROM {table} ANTI JOIN expected_receipts USING(game_id)',
                          f'SELECT * FROM prior.{table} ANTI JOIN expected_receipts USING(game_id)'):
                raise ValueError(f'Changes outside the adjudication scope: {table}')
        for table in ('nba.dim_game','nba.fact_game_result','nba.bridge_game_team','nba.fact_regular_standings','audit.archive_period_receipt','audit.local_period_recovery'):
            if difference(con,f'SELECT * FROM {table}',f'SELECT * FROM prior.{table}'):
                raise ValueError(f'Changes outside periods: {table}')
        check_shooting(con)
        print(f'{verify(con)} quality checks passed; v7 rows, external receipts and four disputed shooting equations reconciled')


def build(parent: Path, receipts: Path, output: Path, raw: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    input_paths = (parent,receipts,raw)
    before = tuple(snapshot(p) for p in input_paths)
    values = load_receipts(receipts)
    pins = [('adjudication_receipts',sha256(receipts)),('parent_v7',sha256(parent))]
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.adjudicate-periods-') as tmp:
        stage = Path(tmp)/'candidate.duckdb'
        shutil.copyfile(parent,stage)
        with duckdb.connect(str(stage),config={'threads':2,'memory_limit':'2GB'}) as con:
            con.begin()
            con.execute('''CREATE TABLE audit.period_adjudication(
                game_id VARCHAR PRIMARY KEY,game_date DATE NOT NULL,home_team_id BIGINT NOT NULL,away_team_id BIGINT NOT NULL,
                prior_status VARCHAR NOT NULL,evidence_kind VARCHAR NOT NULL,home_q INTEGER[4] NOT NULL,away_q INTEGER[4] NOT NULL,
                source_url VARCHAR NOT NULL UNIQUE,source_note VARCHAR NOT NULL);
                CREATE TABLE audit.period_adjudication_input(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);''')
            con.executemany('INSERT INTO audit.period_adjudication VALUES (?,?,?,?,?,?,?,?,?,?)',values)
            con.executemany('INSERT INTO audit.period_adjudication_input VALUES (?,?)',pins)
            if con.execute('''SELECT count(*) FROM audit.period_adjudication a JOIN nba.fact_game g USING(game_id)
                JOIN audit.period_coverage p USING(game_id) WHERE p.status=a.prior_status AND g.result_status='final'
                AND (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
                  = (a.game_date,a.home_team_id,a.away_team_id,list_sum(a.home_q),list_sum(a.away_q))
                AND NOT EXISTS(SELECT 1 FROM nba.fact_game_period_score s WHERE s.game_id=a.game_id)''').fetchone()!=(len(values),):
                raise ValueError('Fixture, source status or final differs, or candidate would overwrite existing period cells')
            con.execute('''INSERT INTO nba.fact_game_period_score
                SELECT a.game_id,b.team_id,i,CASE b.side WHEN 'home' THEN a.home_q[i] ELSE a.away_q[i] END
                FROM audit.period_adjudication a JOIN nba.bridge_game_team b USING(game_id),range(1,5) n(i);
                UPDATE audit.period_coverage p SET status='published',candidate_sources=p.candidate_sources+1,
                    valid_sources=p.valid_sources+1,selected_source='adjudicated_period_receipt'
                FROM audit.period_adjudication a WHERE p.game_id=a.game_id;
                INSERT INTO audit.period_vector_baseline
                SELECT o.* FROM audit.period_vector_observed o SEMI JOIN audit.period_adjudication a USING(game_id);
                CREATE VIEW audit.period_availability_v8 AS
                SELECT v.* REPLACE (coalesce(a.evidence_kind,v.recovery_disposition) AS recovery_disposition)
                FROM audit.period_availability_v7 v LEFT JOIN audit.period_adjudication a USING(game_id);''')
            con.commit()
            con.execute('CHECKPOINT')
        verify_inputs(parent,stage,receipts,raw)
        if tuple(snapshot(p) for p in input_paths)!=before or os.path.lexists(f'{output}.wal'):
            raise RuntimeError('An input changed or the target has a WAL')
        os.link(stage,output)
        print(f'Installed {output}')


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v7.duckdb')
    parser.add_argument('--raw',type=Path,default=ROOT/'data/nba.duckdb')
    parser.add_argument('--receipts',type=Path,default=WAREHOUSE/'period_adjudication_receipts.csv')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    parent,receipts,raw = (getattr(args,k).resolve(strict=True) for k in ('parent','receipts','raw'))
    if args.action=='build':
        build(parent,receipts,args.output.absolute(),raw)
    else:
        verify_inputs(parent,args.output.resolve(strict=True),receipts,raw)
