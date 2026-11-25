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
DOMAINS = ('play_by_play', 'shot_chart', 'officials', 'starting_lineup')
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
          AND g.game_date >= '2001-10-01' ORDER BY 2, 1''').fetchall()
    c.close()
    return rows


def main() -> None:
    games = gap_games()
    print(len(games), 'gap games on', len({g[1] for g in games}), 'dates', flush=True)
    events: dict[str, list[tuple]] = {}
    for d in sorted({g[1] for g in games}):
        sb = get(f'{BASE}/scoreboard?dates={d}&limit=200', CACHE / 'scoreboard' / f'{d}.json')
        out = []
        for e in sb.get('events') or []:
            comp = (e.get('competitions') or [{}])[0]
            sc = {x.get('homeAway'): int(x['score']) for x in comp.get('competitors') or [] if str(x.get('score', '')).isdigit()}
            if 'home' in sc and 'away' in sc:
                out.append((e['id'], sc['home'], sc['away']))
        events[d] = out
    matches = {}
    for gid, d, hs, aws in games:
        hit = [e for e, h, a in events.get(d, []) if (h, a) == (hs, aws)]
        same = [g for g in games if g[1] == d and (g[2], g[3]) == (hs, aws)]
        if len(hit) == 1 and len(same) == 1:
            matches[gid] = hit[0]
    print(len(matches), 'of', len(games), 'games matched to an ESPN event', flush=True)
    (CACHE / 'matches.json').write_text(json.dumps(matches, indent=0, sort_keys=True))
    for i, (gid, eid) in enumerate(sorted(matches.items())):
        get(f'{BASE}/summary?event={eid}', CACHE / 'summary' / f'{eid}.json')
        if i % 100 == 0:
            print(i, gid, eid, flush=True)
    print('done', flush=True)


if __name__ == '__main__':
    sys.exit(main())
