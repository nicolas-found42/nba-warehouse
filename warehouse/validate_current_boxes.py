"""Build a local four-game team-box validation sample from NBA source renderings."""
from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import NamedTuple

import duckdb

from recover_local_periods import difference, snapshot
from validate_current_results import csv_rows
from nba_warehouse import sql_string

# The labels differ in presentation order. Do not parse team totals by width.
METRICS = ('fgm','fga','fg3m','fg3a','ftm','fta','oreb','dreb','reb','ast','pf','stl','tov','blk','pts')
BOOK_ORDER = ('fgm','fga','fg3m','fg3a','ftm','fta','oreb','dreb','reb','ast','pf','stl','tov','blk')
HEADER = ('PLAYER','MIN','FGM','FGA','FG%','3PM','3PA','3P%','FTM','FTA','FT%','OREB','DREB','REB','AST','STL','BLK','TO','PF','PTS','+/-')
WEB_POS = (2,3,5,6,8,9,11,12,13,14,18,15,17,16,19)
SOURCE_LABELS = {'0022600270':('Golden State Warriors','Memphis Grizzlies'),
                 '0022600271':('Portland Trail Blazers','Milwaukee Bucks'),
                 '0022600272':('New York Knicks','Minnesota Timberwolves'),
                 '0022600273':('LA Clippers','San Antonio Spurs')}


class TeamBox(NamedTuple):
    game_id: str
    team_id: int
    side: str
    minutes_seconds: int
    website_counts: dict[str,int]
    gamebook_counts: dict[str,int]
    book_team_rebounds: int
    book_total_turnovers: int
    book_url: str
    website_url: str


def website_cells(line: str) -> list[str]:
    plain = re.sub(r'\[(\d+)\]\(https?://[^)]+\)',r'\1',line)
    return [cell.strip() for cell in plain.strip('|').split('|')]


def parse_boxes(source_dir: Path, receipts: Path) -> tuple[list[TeamBox],list[tuple[str,str,str]]]:
    boxes: list[TeamBox] = []
    inputs = []
    for game in csv_rows(receipts):
        gid = game['game_id']
        web_path,book_path = (source_dir/f'{gid}.{suffix}.txt' for suffix in ('box','gamebook'))
        for path in (web_path,book_path):
            inputs.append((gid,path.name,sha256(path.read_bytes()).hexdigest()))
        website,book = web_path.read_text(),book_path.read_text()
        if 'Thursday, November 19, 2026' not in book or 'FINAL BOX' not in book:
            raise ValueError(f'Not the targeted final scorer report: {gid}')
        header_count = sum(tuple(x.strip() for x in line.strip('|').split('|'))==HEADER
                           for line in website.splitlines() if line.startswith('| PLAYER'))
        if header_count!=2:
            raise ValueError(f'Unexpected website schema: {gid}')
        sections = [part.splitlines() for part in website.split('| PLAYER |')[1:]]
        if len(sections)!=2:
            raise ValueError(f'Expected two website totals: {gid}')
        for i,(side, label, book_label) in enumerate(zip(('away','home'),SOURCE_LABELS[gid],('VISITOR','HOME'),strict=True)):
            team = int(game[f'{side}_id'])
            margin = int(game[f'{side}_score'])-int(game[f'{"home" if side=="away" else "away"}_score'])
            if label not in website or not re.search(rf'(?mi)^{book_label}: {re.escape(label)} \(',book):
                raise ValueError(f'Team label mismatch: {gid} {side}')
            totals = [line for line in sections[i] if line.startswith('| TOTALS |')]
            if len(totals)!=1:
                raise ValueError(f'Expected one total for {gid} {side}')
            for url in re.findall(r'https?://[^) ]+',totals[0]):
                if f'GameID={gid}&' not in url or f'TeamID={team}&' not in url:
                    raise ValueError(f'Unexpected link key: {gid} {side}')
            parts = website_cells(totals[0])
            values = {k:int(parts[pos]) for k,pos in zip(METRICS,WEB_POS,strict=True)}
            block = re.split(r'(?m)^(?:VISITOR|HOME): ',book)[i+1]
            compact_rows = re.findall(r'(?m)^240:00([0-9-]+)\s*$',block)
            team_extras = re.findall(r'TM REB:\s*(\d+)\s*TOT TO:\s*(\d+)\b',block)
            if len(compact_rows)!=1 or len(team_extras)!=1:
                raise ValueError(f'Unexpected gamebook totals: {gid} {side}')
            # Retain a box only when counting stats agree. Book REB must equal OREB+DREB.
            # The public website's REB can differ; replace just this one cell in the
            # check so a known discrepancy is isolated rather than choosing digits.
            book_values = {**values, 'reb':values['oreb']+values['dreb']}
            expected = ''.join(str(book_values[k]) for k in BOOK_ORDER)+f'{margin}{book_values["pts"]}'
            if compact_rows[0]!=expected:
                raise ValueError(f'Website/gamebook cell comparison failed: {gid} {side}: {compact_rows[0]} <> {expected}')
            # Individual website IDs may be absent, but club sums are observable.
            played = [row for line in sections[i] if line.startswith('|')
                      and len(row:=website_cells(line))==len(HEADER) and re.fullmatch(r'\d+:[0-5]\d',row[1])]
            summed = {k:sum(int(row[pos]) for row in played) for k,pos in zip(METRICS,WEB_POS,strict=True)}
            seconds = sum(int(row[1].split(':')[0])*60+int(row[1].split(':')[1]) for row in played)
            if summed!=book_values or abs(seconds-14400)>1:
                raise ValueError(f'Player lines do not reconcile to gamebook totals: {gid} {side}')
            if (values['pts']!=int(game[f'{side}_score']) or values['pts']!=2*values['fgm']+values['fg3m']+values['ftm'] or
                not 0<=values['fg3m']<=values['fgm']<=values['fga'] or
                not values['fg3m']<=values['fg3a']<=values['fga'] or not 0<=values['ftm']<=values['fta'] or
                min(values.values())<0):
                raise ValueError(f'Invalid shooting or final totals: {gid} {side}')
            team_reb,total_to = map(int,team_extras[0])
            if total_to<book_values['tov']:
                raise ValueError(f'Team turnover convention conflict: {gid} {side}')
            boxes.append(TeamBox(gid,team,side,240*60,values,book_values,team_reb,total_to,game['gamebook_url'],game['official_url']))
    if len(boxes)!=8 or {b.game_id for b in boxes}!=set(SOURCE_LABELS):
        raise ValueError('This sample requires eight team totals across four games')
    return boxes,inputs


DDL = """
CREATE TABLE audit.team_box_input(name VARCHAR PRIMARY KEY,game_id VARCHAR NOT NULL,sha256 VARCHAR NOT NULL);
CREATE TABLE audit.team_box_local_review(
  game_id VARCHAR REFERENCES audit.result_for_local_review(game_id),team_id BIGINT NOT NULL,
  side VARCHAR NOT NULL CHECK(side IN ('home','away')), team_seconds INTEGER NOT NULL CHECK(team_seconds=14400),
  website_counts MAP(VARCHAR,INTEGER) NOT NULL, gamebook_counts MAP(VARCHAR,INTEGER) NOT NULL,
  gamebook_team_rebounds INTEGER NOT NULL,gamebook_total_turnovers INTEGER NOT NULL,
  gamebook_url VARCHAR NOT NULL,website_url VARCHAR NOT NULL,PRIMARY KEY(game_id,team_id),UNIQUE(game_id,side));
CREATE VIEW audit.team_box_compared AS
SELECT game_id,team_id,side,team_seconds,
    gamebook_counts['fgm'] AS fgm,gamebook_counts['fga'] AS fga,gamebook_counts['fg3m'] AS fg3m,gamebook_counts['fg3a'] AS fg3a,
    gamebook_counts['ftm'] AS ftm,gamebook_counts['fta'] AS fta,
    gamebook_counts['oreb'] AS oreb,gamebook_counts['dreb'] AS dreb,
    CASE WHEN gamebook_counts['reb']=website_counts['reb'] THEN gamebook_counts['reb'] END AS rebounds_agreed,
    website_counts['reb'] AS rebounds_website,gamebook_counts['reb'] AS rebounds_gamebook,gamebook_team_rebounds,
    gamebook_counts['ast'] AS ast,gamebook_counts['stl'] AS stl,gamebook_counts['blk'] AS blk,gamebook_counts['pf'] AS pf,
    gamebook_counts['tov'] AS player_line_turnovers,gamebook_total_turnovers,
    gamebook_counts['pts'] AS pts,gamebook_url,website_url
FROM audit.team_box_local_review;
CREATE VIEW audit.team_box_game_coverage AS
SELECT r.game_id,r.game_date,count(b.team_id) AS admitted_teams,
       CASE count(b.team_id) WHEN 2 THEN 'sample_compared' WHEN 0 THEN 'not_sampled' ELSE 'incomplete' END AS status
FROM audit.result_for_local_review r LEFT JOIN audit.team_box_local_review b USING(game_id) GROUP BY r.game_id,r.game_date;
"""


def verify(path: Path, parent: Path, source_dir: Path, receipts: Path) -> None:
    snapshot(path)
    snapshot(parent)
    boxes,inputs = parse_boxes(source_dir,receipts)
    with duckdb.connect(str(path),read_only=True) as c:
        c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
        for table in ('result_for_local_review','verified_period_score','regular_standings_as_of','scope','gamebook_receipt'):
            if difference(c,f'SELECT * FROM audit.{table}',f'SELECT * FROM prior.audit.{table}'):
                raise ValueError(f'Parent {table} changed')
        c.execute('CREATE TEMP TABLE expected AS SELECT * FROM audit.team_box_local_review LIMIT 0')
        c.executemany('INSERT INTO expected VALUES (?,?,?,?,?,?,?,?,?,?)',boxes)
        if difference(c,'SELECT * FROM expected','SELECT * FROM audit.team_box_local_review'):
            raise ValueError('Published team stats differ from source receipts')
        if sorted(c.execute('SELECT game_id,name,sha256 FROM audit.team_box_input').fetchall())!=sorted(inputs):
            raise ValueError('Source receipt digest changed')
        if c.execute('''SELECT count(*) FROM audit.team_box_compared b JOIN audit.result_for_local_review r USING(game_id)
               JOIN audit.gamebook_receipt x USING(game_id)
               WHERE r.game_date=DATE '2026-11-19' AND b.gamebook_url=x.gamebook_url AND b.website_url=r.official_url
                 AND ((b.side,b.team_id,b.pts)=('home',r.home_id,r.home_score) OR (b.side,b.team_id,b.pts)=('away',r.away_id,r.away_score))''').fetchone()!=(8,):
            raise ValueError('Team/side totals disagree with result or provenance')
        print(c.execute('SELECT status,count(*) FROM audit.team_box_game_coverage GROUP BY 1 ORDER BY 1').fetchall())
        print(c.execute('SELECT game_id,team_id,rebounds_website,rebounds_gamebook FROM audit.team_box_compared WHERE rebounds_agreed IS NULL ORDER BY 1,2').fetchall())
        print('Read-only checks passed: exact receipt cells, source digests, parent preservation, team identity, and final scores')


def build(parent: Path, source_dir: Path, receipts: Path, output: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    before = snapshot(parent),snapshot(receipts)
    boxes,inputs = parse_boxes(source_dir,receipts)
    with tempfile.TemporaryDirectory(prefix='.current-boxes-',dir=output.parent) as temp:
        candidate=Path(temp)/'candidate.duckdb'
        shutil.copyfile(parent,candidate)
        with duckdb.connect(str(candidate)) as c:
            c.execute(DDL)
            c.executemany('INSERT INTO audit.team_box_local_review VALUES (?,?,?,?,?,?,?,?,?,?)',boxes)
            c.executemany('INSERT INTO audit.team_box_input VALUES (?,?,?)',[(name,gid,digest) for gid,name,digest in inputs])
            c.execute('CHECKPOINT')
        verify(candidate,parent,source_dir,receipts)
        if (snapshot(parent),snapshot(receipts))!=before or os.path.lexists(f'{output}.wal') or Path(f'{candidate}.wal').exists():
            raise RuntimeError('An input changed or output WAL exists')
        os.link(candidate,output)
        print(output)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['build','verify'])
    for label in ('parent','sources','receipts','output'):
        parser.add_argument(f'--{label}',type=Path,required=True)
    args=parser.parse_args()
    parent,sources,receipts = (getattr(args,k).resolve(strict=True) for k in ('parent','sources','receipts'))
    if args.action=='build':
        build(parent,sources,receipts,args.output.absolute())
    else:
        verify(args.output.resolve(strict=True),parent,sources,receipts)
