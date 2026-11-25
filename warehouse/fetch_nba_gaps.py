"""Fetch NBA.com stats responses for games the coverage ledger marks missing_recoverable.

  lineups  boxscoretraditionalv3 for starting_lineup gaps (starters carry a position)
  pbp      playbyplayv3 for play_by_play and shot_chart gaps (actions include shot coordinates)

Responses are cached as JSON under evidence/nba-gaps/<endpoint>/<game_id>.json and never refetched; an empty
response is cached too (that game then counts as a failed NBA.com attempt in audit.gap_attempt when loaded).

  uv run --no-project --with nba_api --with duckdb==2.0.0 python warehouse/fetch_nba_gaps.py lineups|pbp
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from build_warehouse import DEFAULT_OUT

CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'nba-gaps'
PAUSE = 0.7  # seconds between requests per process


def games(domains: list[str]) -> list[str]:
    import duckdb
    c = duckdb.connect(str(DEFAULT_OUT), read_only=True)
    q = ','.join(f"'{d}'" for d in domains)
    out = [r[0] for r in c.execute(f"""SELECT DISTINCT game_id FROM audit.game_coverage
        WHERE domain IN ({q}) AND status IN ('missing_recoverable', 'partial') ORDER BY 1""").fetchall()]
    c.close()
    return out


def run(kind: str) -> None:
    from nba_api.stats.endpoints import boxscoretraditionalv3, playbyplayv3
    endpoint, domains, call = {
        'lineups': ('boxscoretraditionalv3', ['starting_lineup'], lambda g: boxscoretraditionalv3.BoxScoreTraditionalV3(game_id=g, timeout=60)),
        'pbp': ('playbyplayv3', ['play_by_play', 'shot_chart'], lambda g: playbyplayv3.PlayByPlayV3(game_id=g, timeout=60)),
    }[kind]
    d = CACHE / endpoint
    d.mkdir(parents=True, exist_ok=True)
    todo = [g for g in games(domains) if not (d / f'{g}.json').exists()]
    print(len(todo), kind, 'games to fetch', flush=True)
    fails = 0
    for i, g in enumerate(todo):
        try:
            body = call(g).get_dict()
            fails = 0
        except Exception as e:  # timeouts and throttling: back off, give up after repeated failures
            fails += 1
            print('error', g, type(e).__name__, flush=True)
            if fails >= 5:
                print('STOPPED after 5 consecutive errors', file=sys.stderr)
                sys.exit(3)
            time.sleep(30 * fails)
            continue
        (d / f'{g}.json').write_text(json.dumps(body))
        if i % 100 == 0:
            print(i, g, flush=True)
        time.sleep(PAUSE)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('kind', choices=['lineups', 'pbp'])
    run(ap.parse_args().kind)
