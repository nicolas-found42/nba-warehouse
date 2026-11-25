"""Extend the local 2026–27 comparison by one league date, without changing its parent."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any, NamedTuple
from urllib.parse import urlparse

import duckdb

from validate_current_results import ET, GAME_ID, PROVIDER_NAMES, SEASON_START, csv_rows, unique_index

PARENT_DATE = date(2026, 11, 18)
CUTOFF = date(2026, 11, 19)


class Score(NamedTuple):
    game_date: date
    home_id: int
    away_id: int
    home_score: int
    away_score: int
    tipoff_utc: datetime
    neutral_site: bool
    cup_group_game: bool


class DailyFinal(NamedTuple):
    score: Score
    home_periods: tuple[int, ...]
    away_periods: tuple[int, ...]


class Receipt(NamedTuple):
    espn_id: str
    game_id: str
    game_date: date
    home_id: int
    away_id: int
    home_score: int
    away_score: int
    home_periods: tuple[int, ...]
    away_periods: tuple[int, ...]
    box_url: str
    gamebook_url: str


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        h = sha256()
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def checked_readonly(con: duckdb.DuckDBPyConnection, path: Path, alias: str) -> None:
    if not path.is_file() or path.with_suffix(path.suffix+'.wal').exists():
        raise ValueError(f'Expected an existing, checkpointed database: {path}')
    safe_path = str(path.resolve()).replace("'", "''")
    con.execute(f"ATTACH '{safe_path}' AS {alias} (READ_ONLY)")


def season_finals(paths: dict[date, Path], teams: dict[str, int]) -> dict[str, DailyFinal]:
    results: dict[str, DailyFinal] = {}
    for day, path in paths.items():
        doc = json.loads(path.read_bytes())
        if doc['leagues'][0]['calendarType'] != 'day':
            raise ValueError(f'Not a daily scoreboard: {path}')
        for event in doc['events']:
            if (event['season']['year'], event['season']['type']) != (2027, 2):
                continue
            status = event['status']['type']
            if (status['name'], status['state'], status['completed']) != ('STATUS_FINAL', 'post', True):
                raise ValueError(f'Unreviewed regular-season status: {event["id"]}')
            espn_id = event['id']
            utc = datetime.fromisoformat(event['date'].replace('Z', '+00:00'))
            if utc.utcoffset() != timedelta(0) or utc.astimezone(ET).date() != day or espn_id in results:
                raise ValueError(f'Duplicate ID or invalid Eastern date: {espn_id}')
            comp, = event['competitions']
            competitors: list[dict[str, Any]] = comp['competitors']
            sides = {side['homeAway']:side for side in competitors}
            if len(competitors)!=2 or sides.keys() != {'home', 'away'} or not isinstance(comp['neutralSite'], bool):
                raise ValueError(f'Missing designated sides or neutral flag: {espn_id}')
            home, away = sides['home'], sides['away']
            home_id, away_id = teams[home['team']['id']], teams[away['team']['id']]
            if home['team']['displayName'] not in PROVIDER_NAMES or PROVIDER_NAMES[home['team']['displayName']] != home_id or PROVIDER_NAMES.get(away['team']['displayName']) != away_id:
                raise ValueError(f'Team name/ID conflict: {espn_id}')
            hs, aws = int(home['score']), int(away['score'])
            notes = [n.get('headline') for n in comp.get('notes', [])]
            if comp['type']['id'] == '39' or 'NBA Cup Championship' in notes or hs == aws or min(hs, aws) < 1 or home_id == away_id:
                raise ValueError(f'Not an eligible counting final: {espn_id}')
            periods: list[tuple[int, ...]] = []
            for side in (home,away):
                seq: list[dict[str, Any]] = side.get('linescores',[])
                if any(p.get('period') != i or not isinstance(p.get('value'), (int,float)) or int(p['value']) != p['value'] or p['value'] < 0 for i,p in enumerate(seq, 1)):
                    raise ValueError(f'Invalid period score: {espn_id}')
                periods.append(tuple(int(p['value']) for p in seq))
            results[espn_id] = DailyFinal(Score(day,home_id,away_id,hs,aws,utc,comp['neutralSite'],'NBA Cup - Group Play' in notes),*periods)
    return results


def official_finals(path: Path) -> list[Receipt]:
    receipts = []
    for row in unique_index(csv_rows(path), 'espn_game_id').values():
        gid = row['game_id']
        box = urlparse(row['official_url'])
        book = urlparse(row['gamebook_url'])
        day = date.fromisoformat(row['game_date'])
        if (day != CUTOFF or not GAME_ID.fullmatch(gid) or (box.scheme,box.netloc)!=('https','www.nba.com') or
                not box.path.endswith(f'-{gid}/box-score') or (book.scheme,book.netloc)!=('https','statsdmz.nba.com') or
                not re.fullmatch(r'/pdfs/20261119/20261119_[A-Z]{6}\.pdf',book.path)):
            raise ValueError(f'Invalid official locator/date: {gid}')
        receipts.append(Receipt(row['espn_game_id'],gid,day,int(row['home_id']),int(row['away_id']),int(row['home_score']),int(row['away_score']),
                         tuple(map(int,row['home_periods'].split('|'))),tuple(map(int,row['away_periods'].split('|'))),row['official_url'],row['gamebook_url']))
    if len(receipts)!=4 or len({r.game_id for r in receipts})!=4:
        raise ValueError('Expected four unique official finals')
    return receipts


DDL = """
CREATE SCHEMA audit;
CREATE TABLE audit.scope(season_year VARCHAR PRIMARY KEY, cutoff DATE NOT NULL,
  redistribution_cleared BOOLEAN NOT NULL CHECK(NOT redistribution_cleared));
CREATE TABLE audit.inputs(role VARCHAR PRIMARY KEY, sha256 VARCHAR NOT NULL);
CREATE TABLE audit.result_for_local_review(
  game_id VARCHAR PRIMARY KEY CHECK(regexp_full_match(game_id,'00226[0-9]{5}')),
  game_date DATE NOT NULL, home_id BIGINT NOT NULL, away_id BIGINT NOT NULL,
  home_score INTEGER NOT NULL, away_score INTEGER NOT NULL, neutral_site BOOLEAN NOT NULL,
  cup_group_game BOOLEAN NOT NULL, tipoff_utc TIMESTAMPTZ NOT NULL,
  espn_game_id VARCHAR NOT NULL UNIQUE, evidence_route VARCHAR NOT NULL, official_url VARCHAR,
  CHECK(home_id<>away_id AND home_score<>away_score AND least(home_score,away_score)>0));
CREATE TABLE audit.gamebook_receipt(game_id VARCHAR PRIMARY KEY REFERENCES audit.result_for_local_review(game_id),
  gamebook_url VARCHAR NOT NULL UNIQUE);
CREATE TABLE audit.verified_period_score(game_id VARCHAR REFERENCES audit.gamebook_receipt(game_id),
  period INTEGER CHECK(period BETWEEN 1 AND 4), home_points INTEGER NOT NULL CHECK(home_points>=0),
  away_points INTEGER NOT NULL CHECK(away_points>=0), PRIMARY KEY(game_id,period));
CREATE VIEW audit.regular_standings_as_of AS
WITH side AS (
 SELECT home_id AS team_id,home_score AS pf,away_score AS pa FROM audit.result_for_local_review
 UNION ALL SELECT away_id,away_score,home_score FROM audit.result_for_local_review
)
SELECT season_year,cutoff,team_id,count(*) AS games,count(*) FILTER(WHERE pf>pa) AS wins,
       count(*) FILTER(WHERE pa>pf) AS losses,sum(pf) AS points_for,sum(pa) AS points_against
FROM side CROSS JOIN audit.scope GROUP BY ALL;
"""


def verify(path: Path, parent: Path, base: Path) -> None:
    with duckdb.connect(str(path),read_only=True) as c:
        checked_readonly(c,parent,'prior')
        checked_readonly(c,base,'v5')
        checks = {
            'unchanged_parent': ('''SELECT count(*) FROM (
                (SELECT * FROM audit.result_for_local_review WHERE game_date<=DATE '2026-11-18' EXCEPT ALL SELECT * FROM prior.audit.result_for_local_review)
                UNION ALL (SELECT * FROM prior.audit.result_for_local_review EXCEPT ALL SELECT * FROM audit.result_for_local_review))''', (0,)),
            'current_count': ('''SELECT count(*),count(*) FILTER(WHERE game_date=DATE '2026-11-19'),min(game_date),max(game_date),
                count(*) FILTER(WHERE cup_group_game),count(*) FILTER(WHERE neutral_site) FROM audit.result_for_local_review''', (226,4,SEASON_START,CUTOFF,28,1)),
            'no_base_collisions': ('''SELECT count(*) FROM audit.result_for_local_review JOIN v5.nba.dim_game USING(game_id)''',(0,)),
            'team_keys': ('''SELECT count(*) FROM (SELECT home_id AS id FROM audit.result_for_local_review UNION SELECT away_id FROM audit.result_for_local_review) t
                WHERE NOT EXISTS (SELECT 1 FROM v5.nba.dim_team WHERE team_id=t.id)''', (0,)),
            'no_double_booking': ('''SELECT count(*) FROM (SELECT game_date,home_id AS id FROM audit.result_for_local_review
                UNION ALL SELECT game_date,away_id FROM audit.result_for_local_review) GROUP BY game_date,id HAVING count(*)>1''', None),
            'four_gamebook_finals': ('''SELECT count(*),count(DISTINCT game_id),count(DISTINCT period) FROM audit.verified_period_score''', (16,4,4)),
            'period_finals_and_sides': ('''SELECT count(*) FROM (SELECT p.game_id FROM audit.verified_period_score p JOIN audit.result_for_local_review r USING(game_id)
                GROUP BY p.game_id,r.home_score,r.away_score,r.game_date
                HAVING count(*)<>4 OR sum(p.home_points)<>r.home_score OR sum(p.away_points)<>r.away_score OR r.game_date<>DATE '2026-11-19')''',(0,)),
            'standings': ('''SELECT count(*),sum(games),sum(wins),sum(losses),sum(points_for-points_against),count(*) FILTER (WHERE games<>wins+losses OR games>82)
                FROM audit.regular_standings_as_of''',(30,452,226,226,0,0)),
        }
        for name,(sql,expected) in checks.items():
            seen = c.execute(sql).fetchone()
            if seen != expected:
                raise ValueError(f'{name}: expected {expected}, got {seen}')
        print(f'{len(checks)} checks passed on the checkpointed database')


def build(args: argparse.Namespace) -> None:
    output = args.output.absolute()
    if output.exists() or output.is_symlink() or output.with_suffix(output.suffix+'.wal').exists():
        raise FileExistsError(output)
    paths = {SEASON_START+timedelta(days=i): args.scoreboards/f'raw_{(SEASON_START+timedelta(days=i)):%Y%m%d}.json' for i in range((CUTOFF-SEASON_START).days+1)}
    input_files = {'parent':args.parent,'base':args.base,'receipts':args.receipts,'team_map':args.team_map,
                   **{f'scoreboard_{d:%Y%m%d}':p for d,p in paths.items()}}
    digests = {role:digest(p) for role,p in input_files.items()}
    teams = {e:int(r['nba_team_id']) for e,r in unique_index(csv_rows(args.team_map),'espn_team_id').items()}
    scores = season_finals(paths,teams)
    receipts = official_finals(args.receipts)
    if len(scores)!=226 or {k for k,v in scores.items() if v.score.game_date==CUTOFF}!={r.espn_id for r in receipts}:
        raise ValueError('New date does not exactly match the expected four verified finals')
    with TemporaryDirectory(dir=output.parent,prefix='.extend-') as tmp:
        candidate = Path(tmp)/'review.duckdb'
        with duckdb.connect(str(candidate),config={'TimeZone':'UTC'}) as c:
            checked_readonly(c,args.parent,'prior')
            if c.execute('SELECT * FROM prior.audit.scope').fetchall()!=[('2026-27',PARENT_DATE,False)]:
                raise ValueError('Incorrect parent scope')
            if c.execute("SELECT sha256 FROM prior.audit.inputs WHERE role='team_map'").fetchone()!=(digests['team_map'],):
                raise ValueError('Team map differs from parent evidence')
            previous = c.execute('SELECT espn_game_id,game_date,home_id,away_id,home_score,away_score,tipoff_utc::VARCHAR,neutral_site,cup_group_game FROM prior.audit.result_for_local_review').fetchall()
            prior_finals = {r[0]:Score(r[1],r[2],r[3],r[4],r[5],datetime.fromisoformat(r[6]),r[7],r[8]) for r in previous}
            if prior_finals!={k:v.score for k,v in scores.items() if v.score.game_date<=PARENT_DATE}:
                differing = [k for k in prior_finals.keys() | scores.keys() if prior_finals.get(k)!=(scores[k].score if k in scores and scores[k].score.game_date<=PARENT_DATE else None)]
                raise ValueError(f'Previously reviewed finals differ from fresh scoreboard: {differing[:10]}')
            c.execute(DDL)
            c.execute("INSERT INTO audit.scope VALUES ('2026-27',?,false)",[CUTOFF])
            c.executemany('INSERT INTO audit.inputs VALUES (?,?)', list(digests.items()))
            c.execute('INSERT INTO audit.result_for_local_review SELECT * FROM prior.audit.result_for_local_review')
            for r in receipts:
                source = scores[r.espn_id]
                s = source.score
                if r[2:7]!=s[:5] or len(r.home_periods)!=4 or len(r.away_periods)!=4 or r.home_periods!=source.home_periods or r.away_periods!=source.away_periods or sum(r.home_periods)!=s.home_score or sum(r.away_periods)!=s.away_score:
                    raise ValueError(f'Official scorer report and scoreboard disagree: {r.game_id}')
                c.execute('INSERT INTO audit.result_for_local_review VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                          (r.game_id,*s[:5],s.neutral_site,s.cup_group_game,s.tipoff_utc,r.espn_id,'nba_gamebook_and_espn',r.box_url))
                c.execute('INSERT INTO audit.gamebook_receipt VALUES (?,?)',(r.game_id,r.gamebook_url))
                c.executemany('INSERT INTO audit.verified_period_score VALUES (?,?,?,?)',[(r.game_id,i,h,a) for i,(h,a) in enumerate(zip(r.home_periods,r.away_periods,strict=True),1)])
            c.execute('CHECKPOINT')
        verify(candidate,args.parent,args.base)
        if candidate.with_suffix('.duckdb.wal').exists() or any(digests[k]!=digest(p) for k,p in input_files.items()):
            raise ValueError('Uncheckpointed candidate or changed input; nothing installed')
        os.link(candidate,output)
        print('226 finals, including four new games with verified quarter scores; parent unchanged')


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for label in ('parent','base','scoreboards','team-map','receipts','output'):
        parser.add_argument(f'--{label}',type=Path,required=True)
    build(parser.parse_args())
