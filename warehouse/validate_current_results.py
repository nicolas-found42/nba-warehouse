"""Build a local-only NBA 2026–27 result comparison, without writing the warehouse."""
from __future__ import annotations

import argparse
from collections import Counter
from csv import DictReader
from datetime import date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Any, NamedTuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import duckdb

from fetch_team_revisions import TEAMS

OPPONENTS = {name: number for name, number in TEAMS.items()}
for name, number in TEAMS.items():
    city, _ = name.rsplit(' ', 1)
    if city != 'Los Angeles':
        OPPONENTS[city] = number
OPPONENTS.update({'L.A. Clippers': TEAMS['Los Angeles Clippers'],
                  'L.A. Lakers': TEAMS['Los Angeles Lakers'],
                  'Portland': TEAMS['Portland Trail Blazers']})
GAME_ID = re.compile(r'00226[0-9]{5}')
GAME_TEMPLATE = re.compile(r'\{\{NBA game\s*\|([^{}]*)\}\}', re.I)
SEASON_START = date(2026, 10, 20)
ET = ZoneInfo('America/New_York')
PROVIDER_NAMES = {**TEAMS, 'LA Clippers': TEAMS['Los Angeles Clippers']}


class Side(NamedTuple):
    game_id: str | None
    game_date: date
    team_id: int
    opponent_id: int
    away: bool
    points_for: int
    points_against: int
    cup_noted: bool
    revision_id: int
    row_no: int


def evidence_rows(path: Path, cutoff: date) -> tuple[list[Side], list[tuple[int, int, str]]]:
    pages = json.loads(path.read_bytes())['query']['pages']
    rows: list[Side] = []
    revisions = []
    seen_teams = set()
    for page in pages:
        name = page['title'].removeprefix('2026–27 ').removesuffix(' season')
        team_id = TEAMS[name]
        if team_id in seen_teams:
            raise ValueError(f'Duplicate page identity: {name}')
        seen_teams.add(team_id)
        rev, = page['revisions']
        rev_id = int(rev['revid'])
        revisions.append((team_id, rev_id, f'https://en.wikipedia.org/w/index.php?oldid={rev_id}'))
        content = re.sub(r'<!--.*?-->', '', rev['slots']['main']['content'], flags=re.S)
        heading = re.search(r'(?m)^===[ \t]*Regular season[ \t]*===[ \t]*$', content)
        if heading is None:
            raise ValueError(f'Missing regular season heading: {name}')
        next_section = re.split(r'(?m)^={2,3}[^=\n].*?={2,3}[ \t]*$', content[heading.end():], maxsplit=1)[0]
        for ordinal, row in enumerate(re.split(r'(?m)^\|-', next_section)):
            games = list(GAME_TEMPLATE.finditer(row))
            if not games:
                continue
            game, = games
            params = [s.strip() for s in game[1].split('|') if s.strip()]
            keys = [p.split('=', 1)[0].strip() for p in params]
            if len(keys) != len(set(keys)):
                raise ValueError(f'Duplicate template keys: {name} row {ordinal}')
            fields = dict((k.strip(), v.strip()) for k, v in (s.split('=', 1) for s in params))
            if not fields.get('pf') and not fields.get('pa'):
                continue  # schedule, not a result
            cell = [x.strip() for x in re.findall(r'(?m)^\|[ \t]*([^\n]*)', row[:game.start()]) if x.strip()]
            if len(cell) != 3 or not re.fullmatch(r'\d+', cell[0]):
                raise ValueError(f'Unexpected game row: {name} row {ordinal}')
            day_match = re.search(r'\b(October|November|December|January|February|March|April)\s+(\d{1,2})\b', cell[1])
            if day_match is None:
                raise ValueError(f'Unexpected game date: {name} {cell[1]}')
            month = datetime.strptime(day_match[1], '%B').month
            game_date = date(2026 if month >= 10 else 2027, month, int(day_match[2]))
            if not SEASON_START <= game_date <= cutoff:
                continue
            game_id = fields['id'].strip()
            pf, pa = (fields[k].strip() for k in ('pf', 'pa'))
            if (game_id and not GAME_ID.fullmatch(game_id)) or not re.fullmatch(r'[0-9]+',pf) or not re.fullmatch(r'[0-9]+',pa):
                raise ValueError(f'Invalid game id or score: {name} row {ordinal}')
            away = cell[2].startswith('@')
            opponent = cell[2].removeprefix('@').strip()
            link = re.fullmatch(r'\[\[2026–27 (.*?) season(?:\|.*?)?\]\]', opponent)
            opponent_id = OPPONENTS[link[1] if link else opponent]
            rows.append(Side(game_id or None, game_date, team_id, opponent_id, away, int(pf), int(pa),
                             '2026 NBA Cup' in cell[1], rev_id, ordinal))
    if seen_teams != set(TEAMS.values()):
        raise ValueError('Not all 30 current franchise ledgers were supplied')
    return rows, revisions


def csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline='') as stream:
        return list(DictReader(stream))


def unique_index(rows: list[dict[str, str]], field: str) -> dict[str, dict[str, str]]:
    keyed = {r[field]: r for r in rows}
    if '' in keyed or len(keyed) != len(rows):
        raise ValueError(f'Non-unique/empty input key: {field}')
    return keyed


def espn_finals(schedule: Path, game_map: Path, team_map: Path, cutoff: date) -> list[tuple[Any, ...]]:
    xwalk = unique_index([r for r in csv_rows(game_map) if r['espn_game_id']], 'espn_game_id')
    team_xwalk = unique_index(csv_rows(team_map), 'espn_team_id')
    if len(team_xwalk) != len(TEAMS) or any(PROVIDER_NAMES.get(r['nba_team_name']) != int(r['nba_team_id']) or
           r['espn_display_name'] != r['nba_team_name'] for r in team_xwalk.values()):
        raise ValueError('Team crosswalk disagrees with exact franchise names')
    if len({r['nba_team_id'] for r in team_xwalk.values()}) != len(TEAMS):
        raise ValueError('Team crosswalk is not bijective')
    result = []
    for espn_id, row in unique_index(csv_rows(schedule), 'id').items():
        if row['season'] != '2027' or row['season_type'] != '2' or row['status_type_completed'] != 'true':
            continue
        day = date.fromisoformat(row['game_date'])
        if not SEASON_START <= day <= cutoff:
            continue
        if row['status_type_name'] != 'STATUS_FINAL' or row['status_type_state'] != 'post':
            raise ValueError(f'Unexpected completed status {espn_id}: {row["status_type_name"]}')
        home = int(team_xwalk[row['home_id']]['nba_team_id'])
        away = int(team_xwalk[row['away_id']]['nba_team_id'])
        utc = datetime.fromisoformat(row['date'].replace('Z','+00:00'))
        if home == away or utc.astimezone(ET).date() != day:
            raise ValueError(f'Provider home/away or ET date contradiction: {espn_id}')
        if row['home_display_name'] != team_xwalk[row['home_id']]['nba_team_name'] or row['away_display_name'] != team_xwalk[row['away_id']]['nba_team_name']:
            raise ValueError(f'Provider team name contradicts game team IDs: {espn_id}')
        hs, aws = (int(row[col]) for col in ('home_score','away_score'))
        if min(hs, aws) <= 0 or hs == aws:
            raise ValueError(f'Invalid provider final: {espn_id}')
        mapping = xwalk.get(espn_id)
        game_id = mapping['nba_game_id'] if mapping else ''
        if game_id:
            code = mapping['nba_game_code'] if mapping else ''
            if not (mapping and GAME_ID.fullmatch(game_id) and mapping['season_type'] == 'Regular Season'
                    and mapping['season'] == '2027' and mapping['game_date'] == row['game_date']
                    and code.startswith(day.strftime('%Y%m%d') + '/')
                    and int(mapping['nba_home_team_id']) == home and int(mapping['nba_away_team_id']) == away
                    and mapping['home_espn_team_id'] == row['home_id'] and mapping['away_espn_team_id'] == row['away_id']
                    and mapping['match_method'] == 'both' and mapping['match_confidence'] == '1.0'):
                raise ValueError(f'Conflicting game crosswalk: {espn_id} {game_id}')
        if row['neutral_site'] not in ('true', 'false'):
            raise ValueError(f'Unspecified site designation: {espn_id}')
        result.append((espn_id, game_id or None, day, home, away, hs, aws, row['neutral_site']=='true',
                       row['notes_headline'] or None, utc))
    return result


def official_receipts(path: Path) -> list[tuple[Any, ...]]:
    result = []
    for row in csv_rows(path):
        locator = urlparse(row['source_url'])
        if (locator.scheme, locator.netloc) != ('https', 'www.nba.com') or not GAME_ID.fullmatch(row['game_id']) or not row['exception_reason']:
            raise ValueError(f'Invalid official receipt: {row}')
        result.append((row['espn_game_id'], row['game_id'], date.fromisoformat(row['game_date']),
                       *(int(row[k]) for k in ('home_id','away_id','home_score','away_score')),
                       row['source_url'],row['exception_reason']))
    return result


DDL = """
CREATE SCHEMA audit;
CREATE TABLE audit.scope(season_year VARCHAR PRIMARY KEY, cutoff DATE NOT NULL, redistribution_cleared BOOLEAN NOT NULL CHECK(NOT redistribution_cleared));
CREATE TABLE audit.inputs(role VARCHAR PRIMARY KEY, sha256 VARCHAR NOT NULL);
CREATE TABLE audit.revisions(team_id BIGINT PRIMARY KEY, revision_id BIGINT UNIQUE NOT NULL, revision_url VARCHAR NOT NULL);
CREATE TABLE audit.team_ledger(game_id VARCHAR, game_date DATE NOT NULL, team_id BIGINT NOT NULL REFERENCES audit.revisions(team_id), opponent_id BIGINT NOT NULL REFERENCES audit.revisions(team_id), away BOOLEAN NOT NULL, points_for INTEGER NOT NULL, points_against INTEGER NOT NULL, cup_noted BOOLEAN NOT NULL, revision_id BIGINT REFERENCES audit.revisions(revision_id), row_no INTEGER NOT NULL, PRIMARY KEY(revision_id,row_no));
CREATE TABLE audit.espn_final(espn_game_id VARCHAR PRIMARY KEY, game_id VARCHAR UNIQUE, game_date DATE NOT NULL, home_id BIGINT REFERENCES audit.revisions(team_id), away_id BIGINT REFERENCES audit.revisions(team_id), home_score INTEGER NOT NULL, away_score INTEGER NOT NULL, neutral_site BOOLEAN NOT NULL, notes_headline VARCHAR, tipoff_utc TIMESTAMPTZ NOT NULL);
CREATE VIEW audit.reciprocal_team_final AS
SELECT h.game_id,h.game_date,h.team_id AS home_id,a.team_id AS away_id,h.points_for AS home_score,h.points_against AS away_score,h.cup_noted,a.cup_noted AS away_cup_noted,h.revision_id AS home_revision_id,a.revision_id AS away_revision_id
FROM audit.team_ledger h JOIN audit.team_ledger a ON h.game_id=a.game_id
JOIN (SELECT game_id FROM audit.team_ledger GROUP BY game_id HAVING count(*)=2) two ON h.game_id=two.game_id
WHERE NOT h.away AND a.away AND h.team_id=a.opponent_id AND a.team_id=h.opponent_id AND h.team_id<>a.team_id
AND h.game_date=a.game_date AND h.points_for=a.points_against AND h.points_against=a.points_for
AND h.points_for<>h.points_against AND least(h.points_for,h.points_against)>0;
CREATE VIEW audit.wiki_key_disposition AS
SELECT k.game_id,k.editorial_rows,e.espn_game_id,
       CASE WHEN k.game_id IS NULL THEN 'wiki_nba_id_missing'
            WHEN w.game_id IS NULL THEN 'wiki_nonreciprocal'
            WHEN e.game_id IS NULL THEN 'crosswalk_or_espn_missing'
            WHEN (w.game_date,w.home_id,w.away_id,w.home_score,w.away_score) IS DISTINCT FROM
                 (e.game_date,e.home_id,e.away_id,e.home_score,e.away_score) THEN 'sources_disagree'
            ELSE 'corroborated_by_exact_key' END AS status
FROM (SELECT game_id,count(*) AS editorial_rows FROM audit.team_ledger GROUP BY game_id) k
LEFT JOIN audit.reciprocal_team_final w USING(game_id)
LEFT JOIN audit.espn_final e USING(game_id);
CREATE VIEW audit.espn_final_disposition AS
SELECT e.espn_game_id,e.game_id,
       coalesce(d.status, CASE WHEN e.game_id IS NULL THEN 'crosswalk_or_espn_missing' ELSE 'wiki_key_missing' END) AS status
FROM audit.espn_final e LEFT JOIN audit.wiki_key_disposition d USING(game_id);
CREATE VIEW audit.corroborated_result AS
SELECT w.*,e.espn_game_id,e.neutral_site,e.tipoff_utc
FROM audit.reciprocal_team_final w JOIN audit.wiki_key_disposition d USING(game_id) JOIN audit.espn_final e USING(game_id)
WHERE d.status='corroborated_by_exact_key';
CREATE TABLE audit.official_receipt(espn_game_id VARCHAR PRIMARY KEY REFERENCES audit.espn_final(espn_game_id), game_id VARCHAR NOT NULL UNIQUE, game_date DATE NOT NULL, home_id BIGINT NOT NULL, away_id BIGINT NOT NULL, home_score INTEGER NOT NULL, away_score INTEGER NOT NULL, source_url VARCHAR NOT NULL, exception_reason VARCHAR NOT NULL);
CREATE VIEW audit.result_for_local_review AS
SELECT d.game_id,e.game_date,e.home_id,e.away_id,e.home_score,e.away_score,e.neutral_site,
       d.cup_noted AS cup_group_game,e.tipoff_utc,e.espn_game_id,
       'reciprocal_team_logs_and_espn' AS evidence_route,NULL::VARCHAR AS official_url
FROM audit.corroborated_result d JOIN audit.espn_final e USING(espn_game_id)
WHERE d.cup_noted=d.away_cup_noted AND d.cup_noted=(e.notes_headline IS NOT DISTINCT FROM 'NBA Cup - Group Play')
UNION ALL
SELECT r.game_id,e.game_date,e.home_id,e.away_id,e.home_score,e.away_score,e.neutral_site,
       e.notes_headline IS NOT DISTINCT FROM 'NBA Cup - Group Play',e.tipoff_utc,e.espn_game_id,
       'nba_receipt_and_espn',r.source_url
FROM audit.official_receipt r JOIN audit.espn_final e USING(espn_game_id)
JOIN audit.espn_final_disposition d USING(espn_game_id)
WHERE d.status<>'corroborated_by_exact_key' AND (e.game_id IS NULL OR r.game_id=e.game_id)
  AND (r.game_date,r.home_id,r.away_id,r.home_score,r.away_score)=(e.game_date,e.home_id,e.away_id,e.home_score,e.away_score);
CREATE VIEW audit.regular_standings_as_of AS
WITH side AS (
 SELECT home_id AS team_id,home_score AS pf,away_score AS pa FROM audit.result_for_local_review
 UNION ALL SELECT away_id,away_score,home_score FROM audit.result_for_local_review
)
SELECT season_year,cutoff,team_id,count(*) AS games,count(*) FILTER(WHERE pf>pa) AS wins,
       count(*) FILTER(WHERE pa>pf) AS losses,sum(pf) AS points_for,sum(pa) AS points_against
FROM side CROSS JOIN audit.scope GROUP BY ALL;
"""


def build(args: argparse.Namespace) -> None:
    outputs_scope_max = date(2026, 11, 18)
    if not SEASON_START <= args.cutoff <= outputs_scope_max:
        raise ValueError(f'This slice supports {SEASON_START} through {outputs_scope_max} only')
    inputs = {k: getattr(args,k) for k in ('wiki','schedule','game_map','team_map','receipts')}
    snapshots = {role: sha256(path.read_bytes()).hexdigest() for role,path in inputs.items()}
    rows,revisions = evidence_rows(args.wiki,args.cutoff)
    finals = espn_finals(args.schedule,args.game_map,args.team_map,args.cutoff)
    receipts = [r for r in official_receipts(args.receipts) if r[2]<=args.cutoff]
    if not rows or not finals:
        raise ValueError('No complete source data for cutoff')
    output = args.output.absolute()
    if output.exists() or output.is_symlink() or output.with_suffix(output.suffix + '.wal').exists():
        raise FileExistsError(output)
    if any(sha256(path.read_bytes()).hexdigest()!=snapshots[role] for role,path in inputs.items()):
        raise ValueError('Input changed on disk while reading')
    with TemporaryDirectory(dir=output.parent,prefix='.compare-') as temp:
        candidate = Path(temp)/'review.duckdb'
        with duckdb.connect(str(candidate)) as con:
            con.execute(DDL)
            con.execute('INSERT INTO audit.scope VALUES (?,?,false)',['2026-27',args.cutoff])
            con.executemany('INSERT INTO audit.inputs VALUES (?,?)',list(snapshots.items()))
            con.executemany('INSERT INTO audit.revisions VALUES (?,?,?)',revisions)
            con.executemany('INSERT INTO audit.team_ledger VALUES (?,?,?,?,?,?,?,?,?,?)',rows)
            con.executemany('INSERT INTO audit.espn_final VALUES (?,?,?,?,?,?,?,?,?,?)',finals)
            if receipts:
                con.executemany('INSERT INTO audit.official_receipt VALUES (?,?,?,?,?,?,?,?,?)',receipts)
            con.execute('CHECKPOINT')
        with duckdb.connect(str(candidate),read_only=True) as con:
            wiki = dict(con.execute('SELECT status,sum(editorial_rows) FROM audit.wiki_key_disposition GROUP BY status').fetchall())
            espn = dict(con.execute('SELECT status,count(*) FROM audit.espn_final_disposition GROUP BY status').fetchall())
            total = espn.get('corroborated_by_exact_key',0)
            if (sum(wiki.values())!=len(rows) or sum(espn.values())!=len(finals) or not total or
                wiki.get('corroborated_by_exact_key',0)!=2*total or
                con.execute('SELECT count(*) FROM audit.corroborated_result').fetchone()!=(total,)):
                raise RuntimeError('Candidate accounting failure')
            if con.execute('''SELECT count(*),count(DISTINCT game_id),count(DISTINCT espn_game_id),
                count(*) FILTER(WHERE official_url IS NOT NULL) FROM audit.result_for_local_review''').fetchone() != (
                    len(finals),len(finals),len(finals),len(receipts)):
                raise RuntimeError('A provider final is missing, duplicated or has an inconsistent receipt or Cup tag')
            if con.execute('''SELECT sum(games),sum(wins),sum(losses),sum(points_for-points_against)
                FROM audit.regular_standings_as_of''').fetchone() != (2*len(finals),len(finals),len(finals),0):
                raise RuntimeError('Standings are not balanced')
            print(f'Editorial sides: {wiki}\nProvider finals: {espn}')
            print(f'For local review: {len(finals)} finals, including {len(receipts)} verified exception receipts')
        os.link(candidate,output)  # atomic and cannot replace any existing path


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cutoff', type=date.fromisoformat, required=True)
    for kind in ('wiki','schedule','game-map','team-map','receipts','output'):
        parser.add_argument(f'--{kind}',type=Path,required=True)
    build(parser.parse_args())
