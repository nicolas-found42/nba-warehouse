"""All-Star Game referees from English Wikipedia infoboxes, for All-Star games no other source names referees for.

raw.wiki_game_official (game_id, official_name, official_order, page, revision_id). Promoted into core.fact_game_official by
promote_domains.py (names resolved with OFFICIAL_NAME_CANON) at the lowest rank, only for games with no referees at all.
Responses are cached under evidence/wikipedia/ with the revision id as the receipt.

  uv run --no-project --with duckdb==2.0.0 python warehouse/load_wiki_officials.py
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock

CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'wikipedia'
API = 'https://en.wikipedia.org/w/api.php?action=parse&prop=wikitext|revid&format=json&redirects=1&page='


def referees(wikitext: str) -> list[str]:
    m = re.search(r'\|\s*referees?\s*=\s*(.*?)(?=\n\s*\|\s*\w+\s*=)', wikitext, re.I | re.S)
    if not m:
        return []
    v = re.sub(r'\[\[(?:[^|\]]*\|)?([^\]]+)\]\]', r'\1', m.group(1))       # [[Target|Name]] -> Name
    v = re.sub(r'\{\{\s*(unbulleted list|ubl|plainlist)\s*', '', v, flags=re.I)
    v = re.sub(r'<br\s*/?>|\n\s*\||\n\s*\*|,', '\n', v)
    names = [re.sub(r'^and\s+', '', re.sub(r'[{}|\[\].]|<[^>]+>', '', x).strip(), flags=re.I) for x in v.split('\n')]
    return [n for n in names if re.fullmatch(r"[A-Za-z' -]{4,40}", n)]


def main() -> None:
    with warehouse_lock():
        c = connect(DEFAULT_OUT)
        c.execute('''CREATE TABLE IF NOT EXISTS raw.wiki_game_official (game_id VARCHAR, official_name VARCHAR, official_order INTEGER,
            page VARCHAR, revision_id BIGINT)''')
        games = c.execute('''SELECT g.game_id, year(g.game_date) FROM core.dim_game g
            WHERE g.season_type = 'AllStar' AND g.game_id LIKE '003%' AND (g.game_id IN (SELECT game_id FROM raw.wiki_game_official)
               OR g.game_id IN (SELECT game_id FROM audit.game_coverage WHERE domain = 'officials'
                                AND status IN ('missing_recoverable', 'source_unavailable')))''').fetchall()
        rows, attempts = [], []
        for gid, year in games:
            page = f'{year}_NBA_All-Star_Game'
            f = CACHE / f'{page}.json'
            for attempt in range(5):
                if f.exists():
                    break
                try:
                    with urlopen(Request(API + quote(page, safe=''), headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=60) as r:
                        CACHE.mkdir(parents=True, exist_ok=True)
                        f.write_bytes(r.read())
                    time.sleep(3)
                except Exception as e:  # 429: back off
                    print('retry', page, type(e).__name__, flush=True)
                    time.sleep(30 * (attempt + 1))
            if not f.exists():
                attempts.append((gid, 'no_data', 'page not retrieved'))
                continue
            p = json.loads(f.read_text()).get('parse') or {}
            names = referees((p.get('wikitext') or {}).get('*', ''))
            if len(names) == 3:  # an All-Star crew is three referees
                rows += [(gid, n, i + 1, page, p.get('revid')) for i, n in enumerate(names)]
            attempts.append((gid, 'admitted' if len(names) == 3 else 'no_data', f'{len(names)} referees in the infobox'))
        if rows:
            c.execute('DELETE FROM raw.wiki_game_official WHERE game_id IN (SELECT unnest(?))', [[r[0] for r in rows]])
            c.executemany('INSERT INTO raw.wiki_game_official VALUES (?,?,?,?,?)', rows)
        if attempts:
            c.execute("DELETE FROM audit.gap_attempt WHERE domain = 'officials' AND source = 'wikipedia' AND game_id IN (SELECT unnest(?))",
                      [[a[0] for a in attempts]])
            c.executemany("INSERT INTO audit.gap_attempt VALUES ('officials', ?, 'wikipedia', now(), ?, ?)", attempts)
        print(rows)
        c.execute('CHECKPOINT')
        c.close()


if __name__ == '__main__':
    main()
