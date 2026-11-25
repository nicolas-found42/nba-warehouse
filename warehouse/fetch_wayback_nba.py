"""Archived NBA.com game pages (2001-2010 www.nba.com/games/<date>/<AWYHOM>/...) from the Internet Archive Wayback Machine.

Source rank: archived nba.com. For each gap date, the Wayback CDX index lists archived game folders.
Try the earliest distinct captures after the game, including older static box scores. Successful
responses are cached under evidence/wayback-nba/; temporary network failures are retried on later runs.

  uv run --no-project --with duckdb==2.0.0 python warehouse/fetch_wayback_nba.py
"""
from __future__ import annotations

import json
import argparse
import re
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT

CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'wayback-nba'
DOMAINS = ('play_by_play', 'shot_chart', 'starting_lineup', 'officials', 'period_scores', 'player_box', 'team_box')
PAGES = ('boxscore.html', 'manual_boxscore.html', 'playbyplay.html', 'gameinfo.html')
PAUSE = 1.0
ALIAS = {'NJN': 'BKN', 'NOH': 'NOP', 'NOK': 'NOP', 'SEA': 'OKC', 'VAN': 'MEM', 'SAN': 'SAS',
         'CHH': 'CHA', 'GS': 'GSW', 'NY': 'NYK', 'NO': 'NOP', 'SA': 'SAS', 'PHO': 'PHX', 'UTH': 'UTA',
         'TEL': 'MTA', 'MCB': 'MTA', 'TUR': 'EPT', 'EFP': 'EPT', 'BCK': 'KHI', 'LMR': 'ROM'}


def team_code(code: str) -> str:
    return ALIAS.get(code.upper(), code.upper())


def fixture_code(abbreviation: str | None, team_id: int) -> str | None:
    if abbreviation:
        return team_code(abbreviation)
    return 'BAR' if team_id == 12304 else None  # Barcelona: the archive labels the fixture PHIBAR.


def cache_bytes(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        f.write(body)
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def get(url: str, path: Path) -> bytes:
    if path.exists():
        return path.read_bytes()
    for attempt in range(2):
        try:
            with urlopen(Request(url, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=15) as r:
                body = r.read()
            cache_bytes(path, body)
            time.sleep(PAUSE)
            return body
        except HTTPError as e:
            if e.code in (403, 429):
                raise SystemExit(f'Wayback returned HTTP {e.code}: {url}') from e
            if e.code in (404, 410):
                time.sleep(PAUSE)
                return b''
            print('HTTP', e.code, url, flush=True)
        except (URLError, TimeoutError) as e:
            print(type(e).__name__, str(e), url, flush=True)
        time.sleep(PAUSE * (attempt + 1))
    return b''


MARK = {'boxscore.html': rb'(gSGPlayerLink|Officials:|name_pl_)',
        'manual_boxscore.html': rb'(gSGPlayerLink|Officials:|name_pl_|Score By Period|<t[dh][^>]*>\s*FGA)',
        'playbyplay.html': rb'<tr\b[^>]*class=[\'\"]?pbpRow[OE]\b',
        'gameinfo.html': rb'nbaGIQtrPts(?:Last)?[\'\"]?\s*>\s*\d+'}


def has_content(page: str, body: bytes) -> bool:
    return bool(body) and re.search(MARK[page], body, re.I) is not None


def gap_dates() -> dict[str, set[tuple[str, str]]]:
    import duckdb
    q = ','.join(f"'{d}'" for d in DOMAINS)
    out: dict[str, set[tuple[str, str]]] = {}
    with duckdb.connect(str(DEFAULT_OUT), read_only=True) as c:
        rows = c.execute(f'''SELECT DISTINCT strftime(g.game_date, '%Y%m%d'), a.abbreviation, h.abbreviation, a.team_id, h.team_id
            FROM core.dim_game g JOIN core.dim_team a ON a.team_id = g.away_team_id
            JOIN core.dim_team h ON h.team_id = g.home_team_id
            WHERE g.game_date BETWEEN '1996-10-01' AND '2010-07-01' AND (
                g.game_id IN (SELECT game_id FROM audit.game_coverage WHERE domain IN ({q})
                    AND status NOT IN ('present', 'not_tracked', 'not_applicable'))
                OR g.game_id IN (SELECT game_id FROM core.fact_game_official GROUP BY 1 HAVING count(*) < 3))
            ORDER BY 1''').fetchall()
    for d, aa, ha, at, ht in rows:
        a, h = fixture_code(aa, at), fixture_code(ha, ht)
        if a and h:
            out.setdefault(d, set()).add((a, h))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--date', action='append', help='only try this game date (YYYYMMDD); repeatable')
    args = ap.parse_args()
    dates = gap_dates()
    if args.date:
        dates = {d: teams for d, teams in dates.items() if d in args.date}
    print(len(dates), 'gap dates', flush=True)
    for d, teams in dates.items():
        raw = get(f'https://web.archive.org/cdx/search/cdx?url=nba.com/games/{d}/*&output=json&fl=original,timestamp,statuscode,length'
                  f'&filter=statuscode:200&limit=5000', CACHE / 'cdx' / f'{d}.json')
        if not raw:
            print(d, 'archive index unavailable; no negative coverage judgment', flush=True)
            continue
        try:
            rows = json.loads(raw)[1:]
        except ValueError:
            print(d, 'invalid archive index response; no negative coverage judgment', flush=True)
            continue
        snaps: dict[tuple[str, str], dict[str, str]] = {}
        for orig, ts, _, _ in sorted(rows):
            m = re.fullmatch(r'/games/(\d{8})/([A-Z]{6})/(\w+\.html)', urlsplit(orig).path, re.I)
            if not m or m[1] != d or m[3].lower() not in PAGES or ts[:8] < d:
                continue
            folder = m[2].upper()
            if (team_code(folder[:3]), team_code(folder[3:])) in teams:
                snaps.setdefault((folder, m[3].lower()), {}).setdefault(ts, orig)
        for (folder, page), cands in sorted(snaps.items()):
            dest = CACHE / 'pages' / d / folder / page
            if dest.exists() and has_content(page, dest.read_bytes()):
                continue
            for ts, orig in sorted(cands.items())[:3]:
                body = get(f'https://web.archive.org/web/{ts}id_/{orig}', CACHE / 'snap' / d / folder / f'{ts}_{page}')
                if has_content(page, body):
                    cache_bytes(dest, body)
                    break
        print(d, len({k[0] for k in snaps}), 'matching archived game folders', flush=True)


if __name__ == '__main__':
    main()
