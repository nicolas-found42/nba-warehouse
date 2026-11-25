"""Recover explicit USA TODAY preseason starting lineups and officiating crews."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from fetch_wayback_nba import cache_bytes, team_code
from load_cska_box import normalized
from promote_domains import OFFICIAL_NAME_CANON

SOURCE = 'usatoday_preseason'
ROOT = 'https://sportsdata.usatoday.com/basketball/nba/summary/'
CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'usatoday-preseason'


def manifest() -> dict[str, str]:
    with Path(__file__).with_name('usatoday_preseason_games.csv').open(newline='') as file:
        rows = [(r['game_id'], r['event_id']) for r in csv.DictReader(file)]
    if len({gid for gid, _ in rows}) != len(rows) or len({eid for _, eid in rows}) != len(rows):
        raise ValueError('USA TODAY fixture mapping is not one-to-one')
    return dict(rows)


def event_info(raw: bytes, event_id: str) -> dict:
    source = re.search(rb'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
    if not source:
        raise ValueError(f'no published event data for USA TODAY {event_id}')
    event = json.loads(source[1])['props']['pageProps']['fallback']['eventInfo']['query']
    if event.get('id') != event_id or event['league']['abbreviation'] != 'NBA' or event['eventStatus']['name'] != 'Final':
        raise ValueError(f'USA TODAY event identity or status disagrees: {event_id}')
    return event


def fixture_sides(event: dict, fixture: tuple) -> dict[str, int] | None:
    day, season, home, away, hc, ac, hs, aws, arena = fixture
    local_dates = {d['simplifiedFull'] for d in event['startDate'] if d['type'] in ('Local', 'Eastern')}
    if season != event['season']['year'] or day not in local_dates:
        return None
    scores = {t['id']: t['score'] for t in event['teams']}
    src_home, src_away = event['homeTeam'], event['awayTeam']
    want = {team_code(hc): (home, hs), team_code(ac): (away, aws)}
    actual = [(team_code(t['abbreviation']), scores.get(t['id'])) for t in (src_home, src_away)]
    if len(set(dict(actual))) != 2 or {k: v for k, (_, v) in want.items()} != dict(actual):
        return None
    if actual[0][0] != team_code(hc) and (not arena or normalized(arena) != normalized(event['venue']['name'])):
        return None  # accept different home designations only with the same venue and keyed scores
    return {team['id']: want[team_code(team['abbreviation'])][0] for team in (src_home, src_away)}


def opening_players(event: dict) -> dict[str, list[dict]]:
    out = {}
    for play in event['playByPlay']:
        clock = play['time']
        if play['playText'] != 'Starting Lineup' or play['period'] != 1 or clock['minutes'] != 12 or str(clock['seconds']).strip('0.'):
            continue
        for player in play['players']:
            if player.get('id') and player.get('teamID') == play.get('teamID'):
                out.setdefault(play['teamID'], []).append(player)
    return out


def create_tables(c) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS raw.usatoday_starter (
        game_id VARCHAR, team_id BIGINT, person_id BIGINT, event_id VARCHAR, player_source_id VARCHAR, sha256 VARCHAR,
        PRIMARY KEY (game_id, team_id, person_id, sha256))''')
    c.execute('''CREATE TABLE IF NOT EXISTS raw.usatoday_official (
        game_id VARCHAR, official_id BIGINT, official_name VARCHAR, event_id VARCHAR, sha256 VARCHAR,
        PRIMARY KEY (game_id, official_id, sha256))''')


def load(c) -> None:
    create_tables(c)
    official_names = dict(c.execute(f'SELECT name, official_id FROM {OFFICIAL_NAME_CANON}').fetchall())
    rows, officials, outcomes = [], [], []
    for gid, eid in manifest().items():
        path = CACHE / f'{eid}.html'
        if not path.is_file():
            continue
        raw = path.read_bytes()
        fixture = c.execute('''SELECT g.game_date::VARCHAR,g.season_start,g.home_team_id,g.away_team_id,h.abbreviation,a.abbreviation,
                   r.home_score,r.away_score,g.arena_name FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
            JOIN core.dim_team h ON h.team_id = g.home_team_id JOIN core.dim_team a ON a.team_id = g.away_team_id
            WHERE game_id = ? AND season_type = 'Preseason' ''', [gid]).fetchone()
        event = event_info(raw, eid)
        sides = fixture_sides(event, fixture) if fixture else None
        if not sides:
            outcomes.extend((domain, gid, 'mismatch', f'{ROOT}{eid}: date, designated teams, venue or final disagrees')
                            for domain in ('starting_lineup', 'officials'))
            continue
        box = {}
        for team, pid, name, participation in c.execute('''SELECT p.team_id,p.person_id,d.display_name,p.participation_status
            FROM core.fact_player_game_stats p JOIN core.dim_person d USING (person_id) WHERE game_id = ?''', [gid]).fetchall():
            box.setdefault((team, normalized(name)), []).append((pid, participation))
        digest = hashlib.sha256(raw).hexdigest()
        added = []
        for source_team, lineup in opening_players(event).items():
            team = sides.get(source_team)
            if team is None or len({p['id'] for p in lineup}) != 5 or len(lineup) != 5:
                continue
            matches = []
            for player in lineup:
                name = normalized(player['firstName'] + ' ' + player['lastName'])
                key = {'ronartest': 'mettaworldpeace', 'nenehilario': 'nene', 'earlsmith': 'jrsmith'}.get(name, name)
                candidates = box.get((team, key), []) or box.get((team, name), [])
                if len(candidates) == 1 and candidates[0][1] == 'played':
                    matches.append((gid, team, candidates[0][0], eid, player['id'], digest))
            if len(matches) == len({r[2] for r in matches}) == 5:
                added.extend(matches)
        rows.extend(added)
        outcomes.append(('starting_lineup', gid, 'admitted' if len(added) == 10 else 'partial' if added else 'no_data',
                         f'{ROOT}{eid}: {len(added)} identity-checked opening players'))
        ref_names = [r['firstName'] + ' ' + r['lastName'] for r in event['gameDetail']['referees']]
        chosen = {official_names.get(normalized(n)): n for n in ref_names}
        if len(chosen) == 3 and None not in chosen and len(ref_names) == 3:
            officials.extend((gid, oid, n, eid, digest) for oid, n in chosen.items())
            outcomes.append(('officials', gid, 'admitted', f'{ROOT}{eid}: three identified officials'))
        else:
            outcomes.append(('officials', gid, 'no_data', f'{ROOT}{eid}: {len(ref_names)} names, fewer than three independently identified'))
    with warehouse_transaction(c):
        if rows:
            c.executemany('INSERT OR IGNORE INTO raw.usatoday_starter VALUES (?,?,?,?,?,?)', rows)
        if officials:
            c.executemany('INSERT OR IGNORE INTO raw.usatoday_official VALUES (?,?,?,?,?)', officials)
        if outcomes:
            c.execute('CREATE OR REPLACE TEMP TABLE usatoday_reload (domain VARCHAR, game_id VARCHAR)')
            c.executemany('INSERT INTO usatoday_reload VALUES (?,?)', [r[:2] for r in outcomes])
            c.execute('DELETE FROM audit.gap_attempt WHERE source = ? AND (domain,game_id) IN (SELECT domain,game_id FROM usatoday_reload)', [SOURCE])
            c.executemany('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)',
                          [(domain, gid, SOURCE, state, note) for domain, gid, state, note in outcomes])
    print(f'USA TODAY verified {len(rows)} named starters and {len(officials)} crew members', flush=True)


def main() -> None:
    for eid in manifest().values():
        path = CACHE / f'{eid}.html'
        if path.is_file():
            continue
        with urlopen(Request(ROOT + eid, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=20) as response:
            raw = response.read(4_000_000)
        event_info(raw, eid)
        cache_bytes(path, raw)
        time.sleep(1)
    with warehouse_lock(), connect(DEFAULT_OUT) as c:
        load(c)
        c.execute('CHECKPOINT')


if __name__ == '__main__':
    main()
