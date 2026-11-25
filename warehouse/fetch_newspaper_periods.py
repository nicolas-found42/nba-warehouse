"""Quarter scores for old games from digitized newspapers (Library of Congress, Chronicling America; free, no key).

For each game still missing period scores (before 1970), search the loc.gov Chronicling America collection for pages
dated the game day through two days later. Search one nickname plus "periods" at a time, read page OCR, and look for the
"score by periods" (or "by quarters") lines printed under the box score. A line is admitted only when:
  - both teams' lines are found in the same box-score passage,
  - they have the same number of periods (at least 4),
  - each team's periods add up exactly to its official final, and
  - the teams are tied after regulation whenever overtime periods are printed.
OCR is noisy, so anything that does not reconcile exactly is ignored rather than corrected.

Cache: evidence/loc/{search,ocr}/ (raw responses, never refetched). Keep matching text in the candidate
ledger until the printed numbers can be checked against the scan. Source rank: archived newspapers.

  uv run --no-project --with duckdb==2.0.0 python warehouse/fetch_newspaper_periods.py [--limit N]
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from fetch_wayback_nba import cache_bytes

CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'loc'
SCANS = CACHE.parent / 'newspapers' / 'verified'
API = 'https://www.loc.gov/collections/chronicling-america/'
SOURCE = 'newspaper_loc'
PAUSE = 1.5
MARKERS = re.compile(r'(score\s*(at|by)\s*(the\s*)?(periods?|quarters?)|by\s+periods|by\s+quarters)', re.I)


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
                print(f'Chronicling America returned HTTP {e.code}: {url}', flush=True)
                raise
            if e.code == 404:
                time.sleep(PAUSE)
                return b''
            print('retry', e.code, url, flush=True)
        except (URLError, TimeoutError) as e:
            print('retry', type(e).__name__, url[:120], flush=True)
        time.sleep(PAUSE * (attempt + 1))
    raise OSError(f'Chronicling America request failed: {url}')


def ocr_text(raw: bytes) -> str:
    t = raw.decode('utf-8', errors='replace')
    try:
        j = json.loads(t)
        t = json.dumps(j) if not isinstance(j, str) else j
    except ValueError:
        pass
    return t.replace('\\n', '\n').replace('\\u2014', '-').replace('\\u2019', "'")


def numbers_after(text: str, key: str) -> list[list[int]]:
    """Find contiguous printed numbers after a team name; never skip a damaged cell."""
    out = []
    for m in re.finditer(r'(?<!\w)' + re.escape(key) + r'(?!\w)', text, re.I):
        tail = text[m.end():m.end() + 95]
        line = re.match(r'[ .:|_…\t-]*(?:\n[ \t]*)?(\d{1,3}(?:[ \t.·|:–—-]+\d{1,3}){3,9})(?!\d)', tail)
        if line:
            nums = [int(x) for x in re.findall(r'\d+', line[1])]
            if max(nums[:-1]) <= 99:
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
    matches = {}
    markers = list(MARKERS.finditer(text))
    for i, m in enumerate(markers):
        end = min(m.start() + 360, markers[i + 1].start()) if i + 1 < len(markers) else m.start() + 360
        passage = text[m.start():end]
        hc, ac = candidates(passage, *home), candidates(passage, *away)
        for h in hc:
            for a in ac:
                n = len(h)
                if n == len(a) and n >= 4 and all(sum(h[:i]) == sum(a[:i]) for i in range(4, n)):
                    matches[(tuple(h), tuple(a))] = h, a, passage
    return next(iter(matches.values())) if len(matches) == 1 else None


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
    aliases = {'Capitols': ('Caps', 'Wash.', 'Wash'), 'Knicks': ('Knickerbockers', 'N.Y.'),
               'Celtics': ('Bos.',), 'Warriors': ('Phila.', 'Phila'), 'Steamrollers': ('Providence', 'Prov.')}
    return sorted({k for k in (name, nick, city, *aliases.get(nick, ())) if k}, key=lambda k: (-len(k), k))


def search_game(gid, day, ht, at, hn, an, hs, aws):
    d1, d2 = day.isoformat(), (day + dt.timedelta(days=2)).isoformat()
    seen, hits = set(), {}
    queries = [(f'{hn.split()[-1]} periods', 'home'), (f'{an.split()[-1]} periods', 'away')]
    for q, side in queries:
        url = f'{API}?q={quote(q)}&dates={d1}/{d2}&fo=json&c=20'
        raw_search = get(url, CACHE / 'search' / f'{gid}-{side}-periods.json')
        results = json.loads(raw_search).get('results') or []
        for item in results:
            page = item.get('id') or ''
            m = re.search(r'/resource/(sn\w+)/(\d{4}-\d{2}-\d{2})/ed-(\d+)/\?sp=(\d+)', page)
            if not m or page in seen:
                continue
            seen.add(page)
            key = '_'.join(m.groups())
            meta = get(f'https://www.loc.gov/resource/{m[1]}/{m[2]}/ed-{m[3]}/?sp={m[4]}&fo=json', CACHE / 'page' / f'{key}.json')
            ocr_url = (json.loads(meta or b'{}').get('resource') or {}).get('fulltext_file')
            if not ocr_url:
                continue
            raw = get(ocr_url, CACHE / 'ocr' / f'{key}.txt')
            p = parse(ocr_text(raw), (keys(hn), hs), (keys(an), aws))
            if p:
                hits[(tuple(p[0]), tuple(p[1]))] = (gid, ht, at, p[0], p[1], page, ocr_url, hashlib.sha256(raw).hexdigest(), p[2])
    return next(iter(hits.values())) if len(hits) == 1 else None, len(seen), len(hits) > 1


def cumulative_periods(boundaries: list[tuple[int, int, int]], home_final: int, away_final: int) -> list[tuple[int, int]]:
    if [p for p, _, _ in boundaries] != list(range(1, len(boundaries) + 1)) or len(boundaries) < 4:
        raise ValueError('every period boundary must be explicitly printed')
    if boundaries[-1][1:] != (home_final, away_final):
        raise ValueError('printed final disagrees with the warehouse result')
    if any(h != a for p, h, a in boundaries[:-1] if p >= 4):
        raise ValueError('printed overtime boundary is not tied')
    result = [(h - hp, a - ap) for (_, h, a), (_, hp, ap) in zip(boundaries, [(0, 0, 0), *boundaries])]
    if any(min(h, a) < 0 for h, a in result):
        raise ValueError('printed cumulative scores decrease')
    return result


def publish_verified_boundaries(c, scans: Path = SCANS) -> None:
    fieldnames = Path(__file__).with_name('newspaper_period_boundaries.csv')
    groups: dict[str, list[dict]] = {}
    with fieldnames.open(newline='') as file:
        for row in csv.DictReader(file):
            groups.setdefault(row['game_id'], []).append(row)
    with warehouse_transaction(c):
        c.execute('''CREATE TABLE IF NOT EXISTS audit.period_receipt_newspaper (
            game_id VARCHAR PRIMARY KEY, page_url VARCHAR, ocr_url VARCHAR, ocr_sha256 VARCHAR,
            passage VARCHAR, recorded_at TIMESTAMPTZ)''')
        c.execute('''CREATE TABLE IF NOT EXISTS audit.period_receipt_newspaper_boundary (
            game_id VARCHAR, through_period INTEGER, home_cumulative INTEGER, away_cumulative INTEGER,
            page_url VARCHAR, edition VARCHAR, scan_sha256 VARCHAR, method VARCHAR,
            PRIMARY KEY (game_id, through_period))''')
        for gid, rows in groups.items():
            if c.execute('SELECT 1 FROM core.fact_game_period_score WHERE game_id = ? LIMIT 1', [gid]).fetchone():
                continue
            if any(not (scans / r['scan_name']).is_file() for r in rows):
                continue
            for row in rows:
                if hashlib.sha256((scans / row['scan_name']).read_bytes()).hexdigest() != row['scan_sha256']:
                    raise ValueError(f"scan digest disagrees: {row['scan_name']}")
            fixture = {(r['game_date'], int(r['home_team_id']), int(r['away_team_id'])) for r in rows}
            result = c.execute('''SELECT g.game_date::VARCHAR, g.home_team_id, g.away_team_id, r.home_score, r.away_score
                FROM core.dim_game g JOIN core.fact_game_result r USING (game_id) WHERE g.game_id = ?''', [gid]).fetchone()
            if result is None or fixture != {result[:3]}:
                raise ValueError(f'printed newspaper fixture disagrees: {gid}')
            points = cumulative_periods([(int(r['through_period']), int(r['home_cumulative']), int(r['away_cumulative'])) for r in rows], *result[3:])
            c.executemany('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)',
                          [(gid, team, i, pts) for i, (hp, ap) in enumerate(points, 1) for team, pts in ((result[1], hp), (result[2], ap))])
            c.executemany('''INSERT INTO audit.period_receipt_newspaper_boundary VALUES
                (?,?,?,?,?,?,?,'derived_from_printed_cumulative')''',
                [(gid, r['through_period'], r['home_cumulative'], r['away_cumulative'], r['page_url'], r['edition'], r['scan_sha256']) for r in rows])
            c.execute('INSERT INTO audit.period_receipt_newspaper VALUES (?,?,NULL,NULL,?,now())',
                      [gid, rows[0]['page_url'], 'Complete keyed cumulative boundaries verified against the two newspaper scans'])
            c.execute("INSERT INTO audit.gap_attempt VALUES ('period_scores', ?, 'newspaper_verified', now(), 'admitted', 'derived_from_printed_cumulative')", [gid])
            print(f'Verified newspaper period boundaries admitted: {gid}', flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--limit', type=int)
    ap.add_argument('--from-date', default='1946-01-01')
    ap.add_argument('--team', help='limit to games involving this team name or nickname')
    a = ap.parse_args()
    with connect(DEFAULT_OUT, read_only=True) as rc:
        todo = games(rc, None, a.from_date)
    if a.team:
        todo = [g for g in todo if any(a.team.lower() in name.lower() for name in g[4:6])]
    todo = todo[:a.limit]
    print(len(todo), 'games to search', flush=True)
    found, attempts = [], []
    failed = False
    for game in todo:
        gid, day, _, _, hn, an, _, _ = game
        try:
            hit, pages, conflict = search_game(*game)
        except (OSError, ValueError) as e:
            print(gid, day, e, flush=True)
            failed = True
            break
        if hit:
            found.append(hit)
        attempts.append((gid, 'pending_scan' if hit else 'mismatch' if conflict else 'no_data' if pages else 'not_found',
                         f'{pages} newspaper pages checked' + ('; conflicting period lines' if conflict else '')))
        print(gid, day, hn, 'v', an, 'FOUND' if hit else f'- ({pages} pages)', flush=True)
    with warehouse_lock(), connect(DEFAULT_OUT) as c:
        c.execute('BEGIN')
        try:
            c.execute('''CREATE TABLE IF NOT EXISTS audit.period_candidate_newspaper (
                game_id VARCHAR, home_team_id BIGINT, away_team_id BIGINT, home_periods INTEGER[], away_periods INTEGER[],
                page_url VARCHAR, ocr_url VARCHAR, ocr_sha256 VARCHAR, passage VARCHAR, captured_at TIMESTAMPTZ,
                PRIMARY KEY (game_id, page_url, ocr_sha256))''')
            if found:
                c.executemany('INSERT OR IGNORE INTO audit.period_candidate_newspaper VALUES (?,?,?,?,?,?,?,?,?,now())', found)
            if attempts:
                c.executemany("INSERT INTO audit.gap_attempt VALUES ('period_scores', ?, ?, now(), ?, ?)",
                              [(g, SOURCE, o, n) for g, o, n in attempts])
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK')
            raise
        c.execute('CHECKPOINT')
    print(len(found), 'OCR candidates for scan verification out of', len(todo), 'target games', flush=True)
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
