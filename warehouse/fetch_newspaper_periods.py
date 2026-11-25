"""Quarter scores for old games from digitized newspapers (Library of Congress, Chronicling America; free, no key).

For each game still missing period scores (before 1970), search the loc.gov Chronicling America collection for pages
dated the game day and the next day that mention both team nicknames, read each page's OCR text, and look for the
"score by periods" (or "by quarters") lines printed under the box score. A line is admitted only when:
  - both teams' lines are found in the same box-score passage,
  - they have the same number of periods (at least 4),
  - each team's periods add up exactly to its official final, and
  - the teams are tied after regulation whenever overtime periods are printed.
OCR is noisy, so anything that does not reconcile exactly is ignored rather than corrected.

Cache: evidence/loc/{search,ocr}/ (raw responses, never refetched). Receipts: audit.period_receipt_newspaper
(game_id, page url, OCR url, sha256 of the OCR text, the matched passage). Source rank: archived newspapers.

  uv run --no-project --with duckdb==2.0.0 python warehouse/fetch_newspaper_periods.py [--limit N]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock

CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'loc'
API = 'https://www.loc.gov/collections/chronicling-america/'
SOURCE = 'newspaper_loc'
PAUSE = 1.5
MARKERS = re.compile(r'(score\s*(at|by)\s*(the\s*)?(periods?|quarters?)|by\s+periods|by\s+quarters)', re.I)


def get(url: str, path: Path) -> bytes:
    if path.exists():
        return path.read_bytes()
    for attempt in range(4):
        try:
            with urlopen(Request(url, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=90) as r:
                body = r.read()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            time.sleep(PAUSE)
            return body
        except Exception as e:
            print('retry', type(e).__name__, url[:120], flush=True)
            time.sleep(20 * (attempt + 1))
    return b''


def ocr_text(raw: bytes) -> str:
    t = raw.decode('utf-8', errors='replace')
    try:
        j = json.loads(t)
        t = json.dumps(j) if not isinstance(j, str) else j
    except ValueError:
        pass
    return t.replace('\\n', '\n').replace('\\u2014', '-').replace('\\u2019', "'")


def numbers_after(text: str, key: str) -> list[list[int]]:
    """Every run of 4 to 8 small numbers printed after `key` on the same line (OCR separators tolerated)."""
    out = []
    for m in re.finditer(re.escape(key), text, re.I):
        line = text[m.end():m.end() + 70].split('\n')[0]
        nums = [int(x) for x in re.findall(r'\b\d{1,3}\b', line)]
        if 4 <= len(nums) <= 9:
            out.append(nums)
    return out


def candidates(text: str, keys: list[str], final: int) -> list[list[int]]:
    found = []
    for k in keys:
        for nums in numbers_after(text, k):
            if nums[-1] == final and sum(nums[:-1]) == final and len(nums) - 1 >= 4:
                found.append(nums[:-1])
            elif sum(nums) == final and len(nums) >= 4:
                found.append(nums)
    return found


def parse(text: str, home: tuple, away: tuple) -> tuple[list[int], list[int], str] | None:
    """home/away = (keys, final). Look only inside passages that start at a 'by periods' marker."""
    for m in MARKERS.finditer(text):
        passage = text[m.start():m.start() + 260]
        hc, ac = candidates(passage, *home), candidates(passage, *away)
        for h in hc:
            for a in ac:
                n = len(h)
                if n == len(a) and n >= 4 and all(sum(h[:i]) == sum(a[:i]) for i in range(4, n)):
                    return h, a, passage
    return None


def games(c, limit: int | None, since: str = '1946-01-01') -> list[tuple]:
    # the name each franchise used that season (raw.stg_franchise eras), falling back to the current name
    era = '''(SELECT f.team_id, CAST(f.start_year AS INTEGER) AS y0, CAST(f.end_year AS INTEGER) AS y1, f.team_city || ' ' || f.team_name AS name
              FROM raw.stg_franchise f WHERE f.start_year <> f.end_year OR true)'''
    q = f'''SELECT g.game_id, g.game_date, g.home_team_id, g.away_team_id,
               coalesce((SELECT arg_min(e.name, e.y1 - e.y0) FROM {era} e WHERE e.team_id = g.home_team_id AND CAST(substr(g.season_year, 1, 4) AS INTEGER) BETWEEN e.y0 AND e.y1), h.display_name),
               coalesce((SELECT arg_min(e.name, e.y1 - e.y0) FROM {era} e WHERE e.team_id = g.away_team_id AND CAST(substr(g.season_year, 1, 4) AS INTEGER) BETWEEN e.y0 AND e.y1), a.display_name),
               r.home_score, r.away_score
        FROM audit.game_coverage gc JOIN core.dim_game g USING (game_id) JOIN core.fact_game_result r USING (game_id)
        JOIN core.dim_team h ON h.team_id = g.home_team_id JOIN core.dim_team a ON a.team_id = g.away_team_id
        WHERE gc.domain = 'period_scores' AND gc.status IN ('missing_recoverable', 'source_unavailable') AND g.game_date < '1970-01-01'
          AND g.game_date >= ?
        ORDER BY g.game_date'''
    rows = c.execute(q, [since]).fetchall()
    return rows[:limit] if limit else rows


def keys(name: str) -> list[str]:
    parts = name.split()
    nick = parts[-1]
    city = ' '.join(parts[:-1])
    return [k for k in {nick, city, city.split()[-1] if city else nick} if len(k) >= 4]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--limit', type=int)
    ap.add_argument('--from-date', default='1946-01-01')
    a = ap.parse_args()
    import duckdb
    rc = duckdb.connect(str(DEFAULT_OUT), read_only=True)
    todo = games(rc, a.limit, a.from_date)
    rc.close()
    print(len(todo), 'games to search', flush=True)
    found, attempts = [], []
    for gid, day, ht, at, hn, an, hs, aws in todo:
        hit = None
        pages = 0
        d1 = day.isoformat()
        d2 = (day + dt.timedelta(days=1)).isoformat()
        q = f'{hn.split()[-1]} {an.split()[-1]}'
        url = f'{API}?q={quote(q)}&dates={d1}/{d2}&fo=json&c=25'
        res = get(url, CACHE / 'search' / f'{gid}.json')
        try:
            results = json.loads(res or b'{}').get('results') or []
        except ValueError:
            results = []
        for r in results:
            page = r.get('id') or ''
            m = re.search(r'/resource/(sn\w+)/(\d{4}-\d{2}-\d{2})/ed-(\d+)/\?sp=(\d+)', page)
            if not m:
                continue
            pages += 1
            meta = get(f'https://www.loc.gov/resource/{m[1]}/{m[2]}/ed-{m[3]}/?sp={m[4]}&fo=json',
                       CACHE / 'page' / f'{m[1]}_{m[2]}_{m[3]}_{m[4]}.json')
            try:
                ocr_url = (json.loads(meta or b'{}').get('resource') or {}).get('fulltext_file')
            except ValueError:
                ocr_url = None
            if not ocr_url:
                continue
            raw = get(ocr_url, CACHE / 'ocr' / f'{m[1]}_{m[2]}_{m[3]}_{m[4]}.txt')
            text = ocr_text(raw)
            p = parse(text, (keys(hn), hs), (keys(an), aws))
            if p:
                hit = (gid, ht, at, p[0], p[1], page, ocr_url, hashlib.sha256(raw).hexdigest(), p[2])
                break
        if hit:
            found.append(hit)
        attempts.append((gid, 'admitted' if hit else 'not_found' if not pages else 'no_data',
                         f'{pages} newspaper pages searched'))
        print(gid, day, hn, 'v', an, 'FOUND' if hit else f'- ({pages} pages)', flush=True)
    with warehouse_lock():
        c = connect(DEFAULT_OUT)
        c.execute('''CREATE TABLE IF NOT EXISTS audit.period_receipt_newspaper (game_id VARCHAR PRIMARY KEY, page_url VARCHAR,
            ocr_url VARCHAR, ocr_sha256 VARCHAR, passage VARCHAR, recorded_at TIMESTAMPTZ)''')
        have = {r[0] for r in c.execute('SELECT DISTINCT game_id FROM core.fact_game_period_score').fetchall()}
        for gid, ht, at, h, aw, page, ocr, sha, passage in found:
            if gid in have:
                continue
            c.executemany('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)',
                          [(gid, ht, i + 1, v) for i, v in enumerate(h)] + [(gid, at, i + 1, v) for i, v in enumerate(aw)])
            c.execute('INSERT OR REPLACE INTO audit.period_receipt_newspaper VALUES (?,?,?,?,?,now())', [gid, page, ocr, sha, passage])
        c.execute('CREATE OR REPLACE TEMP TABLE relog (game_id VARCHAR)')
        if attempts:
            c.executemany('INSERT INTO relog VALUES (?)', [(x[0],) for x in attempts])
            c.execute("DELETE FROM audit.gap_attempt WHERE domain = 'period_scores' AND source = ? AND game_id IN (SELECT game_id FROM relog)", [SOURCE])
            c.executemany("INSERT INTO audit.gap_attempt VALUES ('period_scores', ?, ?, now(), ?, ?)", [(g, SOURCE, o, n) for g, o, n in attempts])
        c.execute('CHECKPOINT')
        c.close()
    print('admitted', len(found), 'of', len(todo), flush=True)


if __name__ == '__main__':
    main()
