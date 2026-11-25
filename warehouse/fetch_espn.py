"""Fetch ESPN public API game summaries for games whose gaps no other source covers (owner-approved 2026-11-25).

ESPN ranks below every other source: load_espn.py fills only games that still have no rows for a domain.

  1. scoreboard per game date  site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=YYYYMMDD
  2. match each warehouse game to one ESPN event on date + home score + away score (one-to-one only)
  3. summary per matched event site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=<id>

Responses are cached under evidence/espn-api/{scoreboard,summary}/ and never refetched. The match list is written to
evidence/espn-api/matches.json for load_espn.py.

  uv run --no-project --with duckdb==2.0.0 python warehouse/fetch_espn.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT

CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'espn-api'
BASE = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba'
DOMAINS = ('play_by_play', 'shot_chart', 'officials', 'starting_lineup', 'player_box', 'team_box', 'period_scores')
PAUSE = 0.3


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


def gap_games() -> list[tuple]:
    import duckdb
    c = duckdb.connect(str(DEFAULT_OUT), read_only=True)
    q = ','.join(f"'{d}'" for d in DOMAINS)
    rows = c.execute(f'''SELECT DISTINCT g.game_id, strftime(g.game_date, '%Y%m%d'), r.home_score, r.away_score
        FROM audit.game_coverage gc JOIN core.dim_game g USING (game_id) JOIN core.fact_game_result r USING (game_id)
        WHERE gc.domain IN ({q}) AND gc.status IN ('missing_recoverable', 'partial', 'source_unavailable')
          AND g.game_date >= '1996-10-01' ORDER BY 2, 1''').fetchall()
    c.close()
    return rows


def main() -> None:
    games = gap_games()
    print(len(games), 'gap games on', len({g[1] for g in games}), 'dates', flush=True)
    import datetime as dt
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
            sc = {x.get('homeAway'): int(x['score']) for x in comp.get('competitors') or [] if str(x.get('score', '')).isdigit()}
            if 'home' in sc and 'away' in sc:
                out.append((e['id'], sc['home'], sc['away']))
        events[d] = out
    import duckdb
    c = duckdb.connect(str(DEFAULT_OUT), read_only=True)
    all_scores = set(c.execute('''SELECT strftime(g.game_date, '%Y%m%d'), r.home_score, r.away_score FROM core.dim_game g
        JOIN core.fact_game_result r USING (game_id) WHERE g.game_date >= '2001-09-01' ''').fetchall())
    c.close()
    old = CACHE / 'matches.json'
    matches = json.loads(old.read_text()) if old.exists() else {}  # earlier matches are kept (their games may be filled now)
    for gid, d, hs, aws in games:
        hit = [e for e, h, a in events.get(d, []) if (h, a) == (hs, aws)]
        if not hit:  # ESPN may list the game a day early or late: allowed only when no warehouse game that day has these scores
            near = [(dt.datetime.strptime(d, '%Y%m%d') + dt.timedelta(days=k)).strftime('%Y%m%d') for k in (-1, 1)]
            hit = [e for n in near for e, h, a in events.get(n, []) if (h, a) == (hs, aws) and (n, h, a) not in all_scores]
        same = [g for g in games if g[1] == d and (g[2], g[3]) == (hs, aws)]
        if len(hit) == 1 and len(same) == 1:
            matches[gid] = hit[0]
    print(sum(g[0] in matches for g in games), 'of', len(games), 'gap games matched to an ESPN event;', len(matches), 'matches in all', flush=True)
    (CACHE / 'matches.json').write_text(json.dumps(matches, indent=0, sort_keys=True))
    for i, (gid, eid) in enumerate(sorted(matches.items())):
        get(f'{BASE}/summary?event={eid}', CACHE / 'summary' / f'{eid}.json')
        if i % 100 == 0:
            print(i, gid, eid, flush=True)
    print('done', flush=True)


if __name__ == '__main__':
    sys.exit(main())
