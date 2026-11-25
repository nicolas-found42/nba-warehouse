"""Add the seven format-specific 2026 All-Star weekend line scores to v9."""
from __future__ import annotations

import argparse
from datetime import date
import os
from pathlib import Path
import shutil
import tempfile
from typing import NamedTuple

import duckdb

from nba_warehouse import ROOT, WAREHOUSE, sql_string, verify
from recover_archive_periods import sha256
from recover_local_periods import difference, snapshot
from validate_current_results import csv_rows, unique_index


class Receipt(NamedTuple):
    game_id: str
    game_date: date
    home_id: int
    away_id: int
    component: str
    stage: str
    fixture_number: int
    regulation_seconds: int | None
    regulation_target: int | None
    overtime_target: int | None
    home_periods: tuple[int, ...]
    away_periods: tuple[int, ...]
    evidence_kind: str
    source_url: str
    rules_url: str


REPORTS = dict(zip(('0032500004','0032500005','0032500006','0032500011','0032500021','0032500031','0032500041'),
                   ('AUSMEL','VINTMC','VINMEL','WLDSTR',None,'WLDSTP','STRSTP'),strict=True))
RISING_RULES = 'https://www.nba.com/news/2026-nba-rising-stars'
ALLSTAR_RULES = 'https://cdn.nba.com/manage/2026/02/2026-NBA-All-Star-Game-Rules.pdf'


def read_receipts(path: Path) -> list[Receipt]:
    rows = unique_index(csv_rows(path),'game_id')
    if rows.keys()!=REPORTS.keys():
        raise ValueError('Expected exactly the seven 2026 All-Star weekend fixtures')
    result = []
    for gid,r in rows.items():
        day = date.fromisoformat(r['game_date'])
        component = r['component']
        rising = component=='rising_stars'
        if component not in ('rising_stars','all_star') or day!=date(2026,2,13 if rising else 15):
            raise ValueError(f'Invalid event class or date: {gid}')
        code = REPORTS[gid]
        expected_url = (f'https://statsdmz.nba.com/pdfs/{day:%Y%m%d}/{day:%Y%m%d}_{code}.pdf' if code else
                        f'https://www.nba.com/game/{gid}/box-score')
        expected_kind = 'scorer_report' if code else 'keyed_official_box_and_pbp'
        h,a = [tuple(map(int,r[k].split('|'))) for k in ('home_periods','away_periods')]
        if (len(h)!=len(a) or len(h) not in (1,2) or min(*h,*a)<0 or sum(h)==sum(a) or
            any(hp+ap==0 for hp,ap in zip(h,a,strict=True)) or r['source_url']!=expected_url or
            r['rules_url']!=(RISING_RULES if rising else ALLSTAR_RULES) or r['evidence_kind']!=expected_kind):
            raise ValueError(f'Unexpected line score or source locator: {gid}')
        seconds,target,ot = [int(r[k]) if r[k] else None for k in ('regulation_seconds','regulation_target','overtime_target')]
        result.append(Receipt(gid,day,int(r['home_id']),int(r['away_id']),component,r['stage'],int(r['fixture_number']),
                              seconds,target,ot,h,a,expected_kind,expected_url,r['rules_url']))
    return result


def verify_inputs(parent: Path, receipts: Path, release: Path) -> None:
    inputs = parent,receipts,release
    initial = tuple(map(snapshot,inputs))
    parsed = read_receipts(receipts)
    with duckdb.connect(str(release),read_only=True,config={'threads':2,'memory_limit':'2GB'}) as c:
        c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
        if c.execute('SELECT role,sha256 FROM audit.event_period_input ORDER BY role').fetchall()!=[
             ('parent_v9',sha256(parent)),('receipt_csv',sha256(receipts))]:
            raise ValueError('Parent or receipt digest differs')
        c.execute('CREATE TEMP TABLE expected_receipts AS SELECT * FROM audit.event_period_receipt LIMIT 0')
        c.executemany('INSERT INTO expected_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',parsed)
        if difference(c,'SELECT * FROM expected_receipts','SELECT * FROM audit.event_period_receipt'):
            raise ValueError('Stored event receipts differ from the external transcription')
        for rel in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline','nba.game_overtime'):
            if difference(c,f'SELECT * FROM {rel} ANTI JOIN expected_receipts USING(game_id)',
                          f'SELECT * FROM prior.{rel} ANTI JOIN expected_receipts USING(game_id)'):
                raise ValueError(f'Non-event scope changed: {rel}')
        for rel in ('nba.dim_game','nba.fact_game_result','nba.bridge_game_team','nba.bridge_event_fixture','nba.fact_regular_standings'):
            if difference(c,f'SELECT * FROM {rel}',f'SELECT * FROM prior.{rel}'):
                raise ValueError(f'Existing game or event fixture data changed: {rel}')
        if difference(c,'''SELECT * FROM audit.period_coverage SEMI JOIN expected_receipts USING(game_id)''',
                      '''SELECT p.* REPLACE ('published' AS status,'event_period_receipt' AS selected_source,
                         p.candidate_sources+1 AS candidate_sources,p.valid_sources+1 AS valid_sources)
                         FROM prior.audit.period_coverage p SEMI JOIN expected_receipts USING(game_id)'''):
            raise ValueError('Recovery dispositions differ from expected v9 delta')
        checks = verify(c)
    if tuple(map(snapshot,inputs))!=initial:
        raise ValueError('Input changed during verification')
    print(f'{checks} checks passed; source receipts, period format, play boundaries and unchanged history verified')


def build(parent: Path, receipts: Path, output: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    initial = snapshot(parent),snapshot(receipts)
    parsed = read_receipts(receipts)
    digests = [('parent_v9',sha256(parent)),('receipt_csv',sha256(receipts))]
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.event-periods-') as temp:
        stage = Path(temp)/'candidate.duckdb'
        shutil.copyfile(parent,stage)
        with duckdb.connect(str(stage),config={'threads':2,'memory_limit':'2GB'}) as c:
            c.begin()
            c.execute('''CREATE TABLE audit.event_period_receipt(
                game_id VARCHAR PRIMARY KEY,game_date DATE NOT NULL,home_id BIGINT NOT NULL,away_id BIGINT NOT NULL,
                component VARCHAR NOT NULL,stage VARCHAR NOT NULL,fixture_number INTEGER NOT NULL,
                regulation_seconds INTEGER,regulation_target INTEGER,overtime_target INTEGER,
                home_periods INTEGER[] NOT NULL,away_periods INTEGER[] NOT NULL,evidence_kind VARCHAR NOT NULL,
                source_url VARCHAR NOT NULL UNIQUE,rules_url VARCHAR NOT NULL,
                CHECK(len(home_periods)=len(away_periods) AND len(home_periods) BETWEEN 1 AND 2));
                CREATE TABLE audit.event_period_input(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);''')
            c.executemany('INSERT INTO audit.event_period_receipt VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',parsed)
            c.executemany('INSERT INTO audit.event_period_input VALUES (?,?)',digests)
            if c.execute('''SELECT count(*) FROM audit.event_period_receipt r JOIN audit.period_coverage p USING(game_id)
              WHERE p.status='no_valid_candidate' AND NOT EXISTS (SELECT 1 FROM nba.fact_game_period_score f WHERE f.game_id=r.game_id)''').fetchone()!=(len(parsed),):
                raise ValueError('Unexpected parent status or would overwrite a period')
            c.execute('''INSERT INTO nba.fact_game_period_score
                SELECT r.game_id,b.team_id,i,CASE b.side WHEN 'home' THEN r.home_periods[i] ELSE r.away_periods[i] END
                FROM audit.event_period_receipt r JOIN nba.bridge_game_team b USING(game_id), LATERAL range(1,len(r.home_periods)+1) n(i);
                UPDATE audit.period_coverage p SET status='published',selected_source='event_period_receipt',
                  candidate_sources=p.candidate_sources+1,valid_sources=p.valid_sources+1 FROM audit.event_period_receipt r WHERE p.game_id=r.game_id;
                INSERT INTO audit.period_vector_baseline SELECT o.* FROM audit.period_vector_observed o SEMI JOIN audit.event_period_receipt USING(game_id);
                CREATE VIEW nba.event_period_format AS SELECT game_id,component,stage,fixture_number,
                  1::SMALLINT AS regulation_periods,regulation_seconds,regulation_target,overtime_target,
                  CASE WHEN regulation_target IS NOT NULL THEN 'target_score' ELSE 'timed' END AS regulation_mode,
                  CASE WHEN overtime_target IS NOT NULL THEN 'target_score' END AS overtime_mode
                FROM audit.event_period_receipt;
                CREATE OR REPLACE VIEW nba.game_overtime AS
                WITH periods AS (SELECT game_id,max(period_number) AS last_period FROM nba.fact_game_period_score GROUP BY game_id)
                SELECT r.game_id,p.last_period,p.last_period>coalesce(f.regulation_periods,4) AS is_overtime,r.is_overtime AS reported_is_overtime
                FROM nba.fact_game_result r LEFT JOIN periods p USING(game_id) LEFT JOIN nba.event_period_format f USING(game_id);
                CREATE VIEW nba.event_period_score AS
                SELECT s.*,CASE WHEN s.period_number>f.regulation_periods THEN 'overtime' ELSE 'regulation' END AS period_phase,
                  CASE WHEN s.period_number>f.regulation_periods THEN f.overtime_mode ELSE f.regulation_mode END AS period_mode
                FROM nba.fact_game_period_score s JOIN nba.event_period_format f USING(game_id);
                CREATE VIEW audit.period_availability_v10 AS SELECT v.* REPLACE
                   (coalesce(r.evidence_kind,v.recovery_disposition) AS recovery_disposition)
                FROM audit.period_availability_v9 v LEFT JOIN audit.event_period_receipt r USING(game_id);''')
            c.commit()
            c.execute('CHECKPOINT')
        verify_inputs(parent,receipts,stage)
        if (snapshot(parent),snapshot(receipts))!=initial or os.path.lexists(f'{output}.wal'):
            raise ValueError('Input changed or output WAL exists')
        os.link(stage,output)
        print(output)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('build','verify-inputs'))
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v9.duckdb')
    parser.add_argument('--receipts',type=Path,default=WAREHOUSE/'event_period_receipts_2026.csv')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    parent,receipts=(getattr(args,k).resolve(strict=True) for k in ('parent','receipts'))
    if args.action=='build':
        build(parent,receipts,args.output.absolute())
    else:
        verify_inputs(parent,receipts,args.output.resolve(strict=True))
