"""Collect NBA.com player-tracking and hustle season tables (no key), one request at a time.

Tracking (SportVU/Second Spectrum) exists from 2013-14; hustle from 2015-16. Earlier seasons were
never tracked. Each response is saved as JSON evidence; load_nba_tracking() writes them to
raw.nba_tracking (long format: one row per entity x season x measure, columns as JSON).

  uv run --no-project --with nba_api --with duckdb==2.0.0 python warehouse/fetch_nba_tracking.py fetch
  uv run --no-project --with nba_api --with duckdb==2.0.0 python warehouse/fetch_nba_tracking.py load
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

EVIDENCE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'nba-tracking'
MEASURES = ['SpeedDistance', 'Rebounding', 'Possessions', 'CatchShoot', 'PullUpShot', 'Defense', 'Drives',
            'Passing', 'ElbowTouch', 'PostTouch', 'PaintTouch', 'Efficiency']
INTERVAL = 1.6


def seasons(first: int, last: int = 2026) -> list[str]:
    return [f'{y}-{str(y + 1)[2:]}' for y in range(first, last + 1)]


def fetch() -> None:
    from nba_api.stats.endpoints import leaguedashptstats, leaguehustlestatsplayer, leaguehustlestatsteam
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    jobs = []
    for s in seasons(2013):
        for st in ('Regular Season', 'Playoffs'):
            for who in ('Player', 'Team'):
                for m in MEASURES:
                    jobs.append(('pt', s, st, who, m))
    for s in seasons(2015):
        for st in ('Regular Season', 'Playoffs'):
            for who in ('Player', 'Team'):
                jobs.append(('hustle', s, st, who, 'Hustle'))
    for kind, s, st, who, m in jobs:
        out = EVIDENCE / f'{kind}_{s}_{st.replace(" ", "")}_{who}_{m}.json'
        if out.exists():
            continue
        for attempt in range(3):
            try:
                if kind == 'pt':
                    r = leaguedashptstats.LeagueDashPtStats(season=s, season_type_all_star=st, player_or_team=who,
                                                            pt_measure_type=m, per_mode_simple='Totals', timeout=60)
                else:
                    ep = leaguehustlestatsplayer.LeagueHustleStatsPlayer if who == 'Player' else leaguehustlestatsteam.LeagueHustleStatsTeam
                    r = ep(season=s, season_type_all_star=st, per_mode_time='Totals', timeout=60)
                out.write_text(r.get_json())
                print('ok', out.name, flush=True)
                break
            except Exception as e:  # transient 5xx / timeouts are common on this API
                print('retry', out.name, type(e).__name__, flush=True)
                time.sleep(10 * (attempt + 1))
        else:
            print('FAILED', out.name, flush=True)
        time.sleep(INTERVAL)


def load(warehouse: Path) -> None:
    import duckdb
    rows = []
    for f in sorted(EVIDENCE.glob('*.json')):
        kind, season, stype, who, measure = f.stem.split('_')
        d = json.loads(f.read_text())
        for rs in d.get('resultSets', []):
            hdr = rs['headers']
            for row in rs['rowSet']:
                rec = dict(zip(hdr, row))
                rows.append((kind, season, stype, who, measure, rs.get('name'),
                             rec.get('PLAYER_ID'), rec.get('TEAM_ID'), json.dumps(rec), f.name))
    c = duckdb.connect(str(warehouse))
    c.execute('''CREATE OR REPLACE TABLE raw.nba_tracking (kind VARCHAR, season_year VARCHAR, season_type VARCHAR,
        entity VARCHAR, measure VARCHAR, result_set VARCHAR, player_id BIGINT, team_id BIGINT, stats JSON, source_file VARCHAR)''')
    c.executemany('INSERT INTO raw.nba_tracking VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
    print(c.execute('SELECT kind, entity, count(*), min(season_year), max(season_year) FROM raw.nba_tracking GROUP BY ALL ORDER BY ALL').fetchall())
    c.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('action', choices=['fetch', 'load'])
    ap.add_argument('--warehouse', type=Path, default=Path(__file__).resolve().parents[1] / 'data' / 'nba-warehouse.duckdb')
    a = ap.parse_args()
    fetch() if a.action == 'fetch' else load(a.warehouse)
