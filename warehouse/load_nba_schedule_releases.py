"""Register the league's original published schedule separately from completed games."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import subprocess
from collections import defaultdict

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from load_nba_roster_releases import CACHE, TEAM_IDS
from pregame_sources import ET, publish as publish_pregame

RELEASE_NAME = 'schedule-release-2025.pdf'
RELEASE_URL = 'https://pr.nba.com/wp-content/uploads/sites/46/2025/08/2025-26-NBA-Regular-Season-Schedule-By-Date-8-14-25.pdf'
RELEASE_DIGEST = '98a06493d0a107cc410853ad230e67d48858777a2baf76ac77d089cbeaf2435d'
RELEASE_PUBLISHED_BY = dt.datetime(2025, 8, 15, 4, tzinfo=dt.timezone.utc)
PRINTED_GAME = re.compile(r'^\s*[A-Z][a-z]{2}\.\s+(\d{1,2}/\d{1,2}/\d{2})\s+(.+?)\s+(?:at|vs\.?)\s+(.+?)\s+'
                          r'\d{1,2}:\d{2}\s+[AP]M\s+(\d{1,2}:\d{2}\s+[AP]M)(?:\s|$)')


def parse_schedule(layout: str) -> list[tuple[dt.datetime, int, int]]:
    if '2025-26 NBA REGULAR SEASON SCHEDULE' not in layout or 'AS OF AUGUST 14, 2025' not in layout:
        raise ValueError('unexpected official NBA schedule issue')
    games = []
    for row in layout.splitlines():
        match = PRINTED_GAME.match(row)
        if not match:
            continue
        day, away, home, et = match.groups()
        tip = dt.datetime.strptime(f'{day} {et}', '%m/%d/%y %I:%M %p').replace(tzinfo=ET)
        games.append((tip, TEAM_IDS[away.upper().replace('.', '')], TEAM_IDS[home.upper().replace('.', '')]))
    if len(games) != 1200 or len(set(games)) != len(games):
        raise ValueError(f'expected 1,200 distinct scheduled NBA games, got {len(games)}')
    return games


def load(c) -> None:
    path = CACHE / RELEASE_NAME
    if not path.is_file():
        return
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != RELEASE_DIGEST:
        raise ValueError('2025–26 NBA schedule evidence has changed')
    layout = subprocess.run(['pdftotext', '-layout', '-', '-'], input=raw, check=True, capture_output=True).stdout.decode()
    fixtures = defaultdict(list)
    for gid, day, away, home in c.execute('''SELECT game_id,game_date,away_team_id,home_team_id FROM core.dim_game
        WHERE season_year='2025-26' AND season_type='Regular' AND participant_status='resolved' ''').fetchall():
        fixtures[day, away, home].append(gid)
    rows, audit = [], []
    for tip, away, home in parse_schedule(layout):
        game_date = tip.date()
        candidates = fixtures[game_date, away, home]
        gid = candidates[0] if len(candidates) == 1 else None
        audit.append((source_key(tip, away, home), gid, game_date, tip, away, home, RELEASE_URL, RELEASE_DIGEST,
                      'exact_date_and_sides' if gid else 'no_unique_exact_fixture'))
        if gid:
            rows.append((gid, game_date, tip, home, away, RELEASE_URL, RELEASE_PUBLISHED_BY, RELEASE_DIGEST))
    with warehouse_transaction(c):
        publish_pregame(c)
        c.execute('''CREATE TABLE IF NOT EXISTS audit.nba_initial_schedule_resolution (
            source_key VARCHAR, game_id VARCHAR, game_date DATE, tipoff_at TIMESTAMPTZ, away_team_id BIGINT, home_team_id BIGINT,
            source_url VARCHAR, sha256 VARCHAR, resolution VARCHAR, PRIMARY KEY (source_key,sha256))''')
        c.executemany('INSERT OR IGNORE INTO audit.nba_initial_schedule_resolution VALUES (?,?,?,?,?,?,?,?,?)', audit)
        c.executemany('INSERT OR IGNORE INTO raw.nba_initial_schedule VALUES (?,?,?,?,?,?,?,?)', rows)
    print(f'Published {len(rows)} of {len(audit)} games from the NBA initial 2025–26 schedule', flush=True)


def source_key(tip: dt.datetime, away: int, home: int) -> str:
    return f'{tip.isoformat()} {away} {home}'


if __name__ == '__main__':
    with warehouse_lock(), connect(DEFAULT_OUT) as warehouse:
        load(warehouse)
        warehouse.execute('CHECKPOINT')
