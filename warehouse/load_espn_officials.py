"""ESPN referee assignments for 1996-97 through 2001-02 (sportsdataverse espn_nba_officials release, MIT).

  raw.espn_sdv_game_official  one row per game and official, with the matched NBA game_id

ESPN game ids are matched to warehouse games on Eastern-time date + home score + away score (from the same release's
schedule files); an ESPN game that matches more than one warehouse game is left unmatched. Promoted into
core.fact_game_official by promote_domains.py only for games no NBA source covers (source 'espn_sdv').

  uv run --no-project --with duckdb==2.0.0 python warehouse/load_espn_officials.py
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock

EVIDENCE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'espn-sdv'
BASE = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download'
SEASONS = range(1997, 2003)  # season end years


def download(url: str) -> Path:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    p = EVIDENCE / url.rsplit('/', 1)[1]
    if not p.exists():
        with urlopen(Request(url, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=120) as r:
            body = r.read()
        p.write_bytes(body)
        (p.parent / (p.name + '.json')).write_text(json.dumps({
            'url': url, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
            'fetched_at': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}))
    return p


def main() -> None:
    off = [download(f'{BASE}/espn_nba_officials/officials_{y}.csv') for y in SEASONS]
    sch = [download(f'{BASE}/espn_nba_schedules/nba_schedule_{y}.csv') for y in SEASONS]
    with warehouse_lock():
        c = connect(DEFAULT_OUT)
        c.execute(f'''CREATE OR REPLACE TEMP TABLE sm AS
            SELECT DISTINCT id AS espn_game_id, (try_cast(date AS TIMESTAMPTZ) AT TIME ZONE 'America/New_York')::DATE AS d,
                   try_cast(home_score AS INTEGER) AS hs, try_cast(away_score AS INTEGER) AS aws
            FROM read_csv({[str(p) for p in sch]}, all_varchar = true, union_by_name = true)''')
        c.execute('''CREATE OR REPLACE TEMP TABLE gm AS
            SELECT sm.espn_game_id, any_value(g.game_id) AS game_id FROM sm
            JOIN core.dim_game g ON g.game_date = sm.d
            JOIN core.fact_game_result r ON r.game_id = g.game_id AND r.home_score = sm.hs AND r.away_score = sm.aws
            GROUP BY 1 HAVING count(*) = 1''')
        c.execute(f'''CREATE OR REPLACE TABLE raw.espn_sdv_game_official AS
            SELECT DISTINCT o.game_id AS espn_game_id, gm.game_id, o.official_full_name, try_cast(o.official_order AS INTEGER) AS official_order
            FROM read_csv({[str(p) for p in off]}, all_varchar = true, union_by_name = true) o
            LEFT JOIN gm ON gm.espn_game_id = o.game_id''')
        print(c.execute('SELECT count(*), count(DISTINCT espn_game_id), count(DISTINCT game_id) FROM raw.espn_sdv_game_official').fetchone())
        c.execute('CHECKPOINT')
        c.close()


if __name__ == '__main__':
    main()
