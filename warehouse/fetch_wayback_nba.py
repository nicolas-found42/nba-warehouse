"""Archived NBA.com game pages (2001-2010 www.nba.com/games/<date>/<AWYHOM>/...) from the Internet Archive Wayback Machine.

Source rank: archived nba.com. Used only for games with gaps. For each gap date, the Wayback CDX index lists the archived
game folders; for each folder the largest 200-status snapshot of boxscore.html, playbyplay.html and gameinfo.html is
fetched raw (id_). The NBA game id is read from the box page ("game_component/static/<game_id>/"), so no team-code
guessing is needed. Everything is cached under evidence/wayback-nba/ and never refetched.

  uv run --no-project --with duckdb==2.0.0 python warehouse/fetch_wayback_nba.py
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT

CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'wayback-nba'
DOMAINS = ('play_by_play', 'shot_chart', 'starting_lineup', 'officials', 'period_scores', 'player_box', 'team_box')
PAGES = ('boxscore.html', 'playbyplay.html', 'gameinfo.html')
PAUSE = 1.0


def get(url: str, path: Path) -> bytes:
    if path.exists():
        return path.read_bytes()
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=90) as r:
                body = r.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            time.sleep(PAUSE)
            return body
        except Exception as e:
            print('retry', type(e).__name__, url[:110], flush=True)
            time.sleep(15 * (attempt + 1))
    return b''


MARK = {'boxscore.html': rb'(playerStats|Officials:|name_pl_)', 'playbyplay.html': rb'pbpRow', 'gameinfo.html': rb'nbaGIQtr'}


def has_content(page: str, body: bytes) -> bool:
    return bool(body) and re.search(MARK[page], body) is not None


def gap_dates() -> list[str]:
    import duckdb
    c = duckdb.connect(str(DEFAULT_OUT), read_only=True)
    q = ','.join(f"'{d}'" for d in DOMAINS)
    out = [r[0] for r in c.execute(f'''SELECT DISTINCT strftime(g.game_date, '%Y%m%d') FROM audit.game_coverage gc
        JOIN core.dim_game g USING (game_id) WHERE gc.domain IN ({q}) AND gc.status NOT IN ('present', 'not_tracked', 'not_applicable')
          AND g.game_date BETWEEN '2001-10-01' AND '2010-07-01' ORDER BY 1''').fetchall()]
    c.close()
    return out


def main() -> None:
    dates = gap_dates()
    print(len(dates), 'gap dates', flush=True)
    index = {}
    for d in dates:
        raw = get(f'https://web.archive.org/cdx/search/cdx?url=nba.com/games/{d}/*&output=json&fl=original,timestamp,statuscode,length'
                  f'&filter=statuscode:200&limit=5000', CACHE / 'cdx' / f'{d}.json')
        try:
            rows = json.loads(raw or b'[]')[1:]
        except ValueError:
            rows = []
        snaps: dict[tuple, list] = {}
        for orig, ts, _, length in rows:
            m = re.search(r'/games/(\d{8})/([A-Z]{6})/(\w+\.html)$', orig)
            if m and m[3] in PAGES and ts[:8] >= d:
                snaps.setdefault((m[2], m[3]), []).append((ts, orig))
        best = snaps
        for (folder, page), cands in sorted(snaps.items()):
            dest = CACHE / 'pages' / d / folder / page
            if dest.exists() and has_content(page, dest.read_bytes()):
                body = dest.read_bytes()
            else:
                body = b''
                for ts, orig in sorted(cands)[:3]:  # earliest snapshots after the game: later ones are often emptied shells
                    b = get(f'https://web.archive.org/web/{ts}id_/{orig}', CACHE / 'snap' / d / folder / f'{ts}_{page}')
                    if has_content(page, b):
                        body = b
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(b)
                        break
            if page == 'boxscore.html':
                gid = re.search(rb'game_component/static/(\d{10})/', body)
                index[f'{d}/{folder}'] = gid.group(1).decode() if gid else None
        print(d, len({k[0] for k in best}), 'archived games', flush=True)
    (CACHE / 'index.json').write_text(json.dumps(index, indent=0, sort_keys=True))
    print('done', sum(1 for v in index.values() if v), 'pages with a game id', flush=True)


if __name__ == '__main__':
    main()
