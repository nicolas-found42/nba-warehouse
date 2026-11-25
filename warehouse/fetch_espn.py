"""Fetch ESPN public API game summaries for games whose gaps no other source covers (owner-approved 2026-11-25).

ESPN ranks below every other source: load_espn.py fills only games that still have no rows for a domain.

  1. scoreboard per game date  site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
  2. match each warehouse game to one ESPN event on date, designated teams and both scores (one-to-one only)
  3. summary per matched event site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=<id>

Responses are cached under evidence/espn-api/{scoreboard,summary}/ and never refetched. The match list is written to
evidence/espn-api/matches.json for load_espn.py.

  uv run --no-project --with duckdb==2.0.0 python warehouse/fetch_espn.py
"""
from __future__ import annotations

import json
import argparse
import datetime as dt
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from build_warehouse import DEFAULT_OUT, connect
from fetch_wayback_nba import cache_bytes, fixture_code, team_code

CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'espn-api'
BASE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
DOMAINS = ('play_by_play', 'shot_chart', 'officials', 'starting_lineup', 'player_box', 'team_box', 'period_scores')
PAUSE = 0.3
FOREIGN_CLUB_IDS = {
    93: '142',       # Maccabi Haifa
    94: '109596',    # Olimpia Milano
    12315: '132',    # Real Madrid
    12321: '105236', # Fenerbahçe
    12325: '108597', # Flamengo
    12328: '109640', # Bauru
    12330: '109850', # San Lorenzo
    12332: '112638', # Franca
}


def code(abbreviation: str) -> str:
    aliases = {'WSH': 'WAS', 'BRK': 'BKN', 'NJ': 'BKN', 'UTAH': 'UTA', 'EAST': 'EST', 'WEST': 'WST',
               'MCC': 'MTA', 'SHA': 'SDS', 'EFS': 'EPT', 'LRO': 'ROM', 'UCJ': 'MAL', 'MDR': 'RMD',
               'PNT': 'PAN', 'ZGS': 'ZAK', 'CSK': 'MOS', 'FCB': 'BAR', 'LTV': 'LRY', 'PTZ': 'PAR', 'OLY': 'OLP'}
    return team_code(aliases.get(abbreviation, abbreviation))


def fixture_key(competition: dict) -> tuple | None:
    out = {}
    for side in competition.get('competitors') or []:
        team = (side.get('team') or {}).get('abbreviation')
        score = str(side.get('score', ''))
        if team and score.isdigit():
            out[side.get('homeAway')] = (code(team), int(score))
    if 'home' in out and 'away' in out:
        h, a = out['home'], out['away']
        return h[0], a[0], h[1], a[1]
    return None


def fixture_matches(competition: dict, home: tuple, away: tuple) -> bool:
    competitors = competition.get('competitors') or []
    sides = {entry.get('homeAway'): entry for entry in competitors}
    if len(competitors) != 2 or set(sides) != {'home', 'away'}:
        return False
    for side, (team_id, abbreviation, score) in (('home', home), ('away', away)):
        entry = sides[side]
        team = entry.get('team') or {}
        if str(entry.get('score')) != str(score):
            return False
        if team_id in FOREIGN_CLUB_IDS:
            if str(team.get('id')) != FOREIGN_CLUB_IDS[team_id]:
                return False
        elif not abbreviation or code(team.get('abbreviation') or '') != code(abbreviation):
            return False
    return True


def get(url: str, path: Path) -> dict:
    if not path.exists():
        for attempt in range(4):
            try:
                with urlopen(Request(url, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=60) as r:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(r.read())
                break
            except Exception as e:  # back off on throttling or timeouts
                print('retry', url, type(e).__name__, flush=True)
                time.sleep(10 * (attempt + 1))
        else:
            raise SystemExit(f'giving up on {url}')
        time.sleep(PAUSE)
    return json.loads(path.read_text())


def games_for_matching() -> tuple[list[tuple], Counter]:
    q = ','.join(f"'{d}'" for d in DOMAINS)
    with connect(DEFAULT_OUT, read_only=True) as c:
        rows = c.execute(f'''SELECT g.game_id, strftime(g.game_date, '%Y%m%d'), h.abbreviation, a.abbreviation,
                   g.home_team_id, g.away_team_id, r.home_score, r.away_score,
                   g.game_id IN (SELECT game_id FROM audit.game_coverage WHERE domain IN ({q})
                                AND status IN ('missing_recoverable', 'partial', 'source_unavailable')) AS gap
            FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
            JOIN core.dim_team h ON h.team_id = g.home_team_id JOIN core.dim_team a ON a.team_id = g.away_team_id
            WHERE g.game_date >= '1996-10-01' ORDER BY 2, 1''').fetchall()
    gaps, all_keys = [], Counter()
    for gid, day, ha, aa, ht, at, hs, aws, gap in rows:
        h, a = fixture_code(ha, ht), fixture_code(aa, at)
        if h and a:
            key = (code(h), code(a), hs, aws)
            all_keys[(day, key)] += 1
            if gap:
                gaps.append((gid, day, key))
    return gaps, all_keys


def validate_summary(c, gid: str, event: dict, *, exact_venue: bool = True) -> None:
    row = c.execute('''SELECT g.game_date, h.abbreviation, a.abbreviation, h.team_id, a.team_id,
               r.home_score,r.away_score,g.arena_name
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
        JOIN core.dim_team h ON h.team_id = g.home_team_id JOIN core.dim_team a ON a.team_id = g.away_team_id WHERE game_id = ?''', [gid]).fetchone()
    if row is None:
        raise ValueError(f'warehouse fixture is missing: {gid}')
    day, ha, aa, ht, at, hs, aws, arena = row
    home, away = fixture_code(ha, ht), fixture_code(aa, at)
    comp = ((event.get('header') or {}).get('competitions') or [{}])[0]
    venue = ((event.get('gameInfo') or {}).get('venue') or {}).get('fullName')
    zone = 'Pacific/Honolulu' if arena == 'Stan Sheriff Center' else 'America/New_York'
    tip = dt.datetime.fromisoformat(comp['date']).astimezone(ZoneInfo(zone))
    teams_ok = fixture_matches(comp, (ht, home, hs), (at, away, aws))
    if exact_venue:
        place_ok = tip.date() == day and (not arena or (venue or '').casefold() == arena.casefold())
    else:
        place_ok = abs((tip.date() - day).days) <= 1
    if not teams_ok or not place_ok:
        raise ValueError(f'ESPN summary fixture, date, scores or venue disagree: {gid}')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--game', action='append', help='restrict discovery to these NBA game IDs')
    ap.add_argument('--verified-event', action='append', default=[], metavar='NBA_ID:ESPN_ID',
                    help='admit an independently identified event after date, sides, final and venue checks')
    args = ap.parse_args()
    games, all_keys = games_for_matching()
    explicit = dict(pair.split(':', 1) for pair in args.verified_event)
    if args.game or explicit:
        games = [g for g in games if g[0] in (args.game or []) and g[0] not in explicit]
    print(len(games), 'gap games on', len({g[1] for g in games}), 'dates', flush=True)
    days = {g[1] for g in games}
    for d in list(days):  # ESPN can list a late game under the next or previous day
        base = dt.datetime.strptime(d, '%Y%m%d')
        days |= {(base + dt.timedelta(days=k)).strftime('%Y%m%d') for k in (-1, 1)}
    events: dict[str, list[tuple]] = {}
    for d in sorted(days):
        sb = get(f'{BASE}/scoreboard?dates={d}&limit=200', CACHE / 'scoreboard' / f'{d}.json')
        out = []
        for e in sb.get('events') or []:
            comp = (e.get('competitions') or [{}])[0]
            key = fixture_key(comp)
            if key is not None:
                out.append((e['id'], key))
        events[d] = out
    old = CACHE / 'matches.json'
    matches = json.loads(old.read_text()) if old.exists() else {}  # earlier matches are kept (their games may be filled now)
    proposals = {}
    for gid, d, key in games:
        if gid in matches:
            continue
        hit = {eid for eid, k in events.get(d, []) if k == key}
        if not hit:  # ESPN sometimes lists a game on an adjacent date.
            near = [(dt.datetime.strptime(d, '%Y%m%d') + dt.timedelta(days=k)).strftime('%Y%m%d') for k in (-1, 1)]
            hit = {eid for n in near for eid, k in events.get(n, []) if k == key and (n, k) not in all_keys}
        if len(hit) == 1 and all_keys[(d, key)] == 1:
            proposals[gid] = next(iter(hit))
    used = set(matches.values())
    for gid, eid in proposals.items():
        if eid not in used and sum(eid == value for value in proposals.values()) == 1:
            matches[gid] = eid
    for gid, eid in explicit.items():
        if not (len(gid) == 10 and gid.isdigit() and eid.isdigit()):
            raise ValueError('verified event requires numeric NBA and ESPN IDs')
        if any(g != gid and e == eid for g, e in matches.items()):
            raise ValueError(f'ESPN event {eid} already belongs to another NBA fixture')
        summary = get(f'{BASE}/summary?event={eid}', CACHE / 'summary' / f'{eid}.json')
        with connect(DEFAULT_OUT, read_only=True) as c:
            validate_summary(c, gid, summary)
        matches[gid] = eid
        print(gid, eid, 'verified event', flush=True)
    print(sum(g[0] in matches for g in games), 'of', len(games), 'gap games matched to an ESPN event;', len(matches), 'matches in all', flush=True)
    for i, (gid, eid) in enumerate(sorted(matches.items())):
        if (args.game or explicit) and gid not in (args.game or []) and gid not in explicit:
            continue
        get(f'{BASE}/summary?event={eid}', CACHE / 'summary' / f'{eid}.json')
        if i % 100 == 0:
            print(i, gid, eid, flush=True)
    cache_bytes(CACHE / 'matches.json', json.dumps(matches, indent=0, sort_keys=True).encode())
    print('done', flush=True)


if __name__ == '__main__':
    sys.exit(main())
