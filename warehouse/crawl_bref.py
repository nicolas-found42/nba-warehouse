"""Queue of Basketball-Reference pages to cache (fetching only; parsing is in load_bref.py).

Jobs:
  transactions   /leagues/{BAA,NBA,ABA}_{year}_transactions.html for every season
  aba_schedule   /leagues/ABA_{year}_games.html and each month page it links to
  team_seasons   every team-season page listed on the league season pages (rosters, salaries)
  aba_boxscores  every ABA box score linked from the cached ABA schedule pages (long: ~4,500 pages)

  python crawl_bref.py transactions aba_schedule [--from-year 1985]
"""
from __future__ import annotations

import argparse
import re
import sys

from bref_fetch import BlockedError, NotFound, fetch

NBA_YEARS = range(1950, 2028)
BAA_YEARS = range(1947, 1950)
ABA_YEARS = range(1968, 1977)


def leagues() -> list[tuple[str, int]]:
    return [('BAA', y) for y in BAA_YEARS] + [('NBA', y) for y in NBA_YEARS] + [('ABA', y) for y in ABA_YEARS]


def get(path: str) -> str | None:
    try:
        return fetch(path)
    except NotFound:
        print('404', path, flush=True)
        return None


def transactions() -> None:
    for lg, y in leagues():
        if get(f'/leagues/{lg}_{y}_transactions.html') is not None:
            print('ok', lg, y, flush=True)


def aba_schedule() -> list[str]:
    months: list[str] = []
    for y in ABA_YEARS:
        page = get(f'/leagues/ABA_{y}_games.html')
        if page is None:
            continue
        months.append(f'/leagues/ABA_{y}_games.html')
        for m in sorted(set(re.findall(rf'/leagues/ABA_{y}_games-[a-z]+\.html', page))):
            if get(m) is not None:
                months.append(m)
    return months


def team_seasons(from_year: int) -> None:
    for lg, y in leagues():
        if y < from_year:
            continue
        page = get(f'/leagues/{lg}_{y}.html')
        if page is None:
            continue
        for t in sorted(set(re.findall(rf'/teams/[A-Z]{{3}}/{y}\.html', page))):
            get(t)
        print('ok', lg, y, flush=True)


def aba_boxscores() -> None:
    paths: set[str] = set()
    for p in aba_schedule():
        paths.update(re.findall(r'/boxscores/[0-9]{9}[A-Z]{3}\.html', get(p) or ''))
    print(len(paths), 'ABA box scores', flush=True)
    for i, p in enumerate(sorted(paths)):
        get(p)
        if i % 100 == 0:
            print(i, p, flush=True)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('jobs', nargs='+', choices=['transactions', 'aba_schedule', 'team_seasons', 'aba_boxscores'])
    ap.add_argument('--from-year', type=int, default=1985)
    a = ap.parse_args()
    try:
        for job in a.jobs:
            if job == 'team_seasons':
                team_seasons(a.from_year)
            else:
                globals()[job]()
    except BlockedError as e:
        print('BLOCKED:', e, file=sys.stderr)
        sys.exit(3)
