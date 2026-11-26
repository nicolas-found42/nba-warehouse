"""Register the league's original published schedule separately from completed games."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from load_nba_roster_releases import CACHE, TEAM_IDS
from pregame_sources import ET, publish as publish_pregame

@dataclass(frozen=True)
class ScheduleRelease:
    season: str
    filename: str
    source_url: str
    sha256: str
    published_by: dt.datetime
    printed_issue_date: str
    expected_games: int
    section: str = 'REGULAR SEASON'


RELEASES = (
    ScheduleRelease('2020-21', 'opening-followup/schedule-2020-second-half-official.pdf',
                    'https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2021/02/2020-21-SECOND-HALF-SCHEDULE.pdf',
                    '61c36db0d462437c57d6f89691b591d44164e696f059625bcb67894c05b808d0',
                    dt.datetime.fromisoformat('2021-02-24T20:05:35Z'), 'FEBRUARY 24, 2021', 546, 'SECOND HALF'),
    ScheduleRelease('2021-22', 'opening-followup/schedule-2021-by-date-official.pdf',
                    'https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2021/08/2021-22-NBA-Schedule-By-Date.pdf',
                    'bbfb82f61ccd23714b61c78ce0dcabdf2b64d67655590fcfaeffb176b9b2e027',
                    dt.datetime.fromisoformat('2021-08-20T19:06:03Z'), 'AUGUST 20, 2021', 1230),
    ScheduleRelease('2022-23', 'schedule-release-2022.pdf',
                    'https://pr.nba.com/wp-content/uploads/sites/46/2022/08/2022-23-NBA-Schedule-By-Date-8-17-22.pdf',
                    'f98d5b6273d3c7793a74b356f1fceba1f1a47656572b2825645bf5794ac9ef81',
                    dt.datetime.fromisoformat('2022-08-18T04:00:00Z'), 'AUGUST 17, 2022', 1230),
    ScheduleRelease('2023-24', 'schedule-release-2023.pdf',
                    'https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2023/08/2023-24-NBA-Schedule-By-Date-as-of-8-17-23.pdf',
                    '7e31cf6d8ca00f92fab8b28bd973b90d0f2c4f6beead11bcf4e487f9e63ed29a',
                    dt.datetime.fromisoformat('2023-08-17T19:05:38Z'), 'AUGUST 17, 2023', 1200),
    ScheduleRelease('2024-25', 'opening-followup/schedule-2024-by-date-official.pdf',
                    'https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2024/08/2024-25-NBA-Regular-Season-Schedule-By-Date.pdf',
                    'a6ad5d18e773d24d885c5b376df6d7fc44578590996e679f611049fc3b9687f0',
                    dt.datetime.fromisoformat('2024-08-15T19:09:10Z'), 'AUGUST 15, 2024', 1200),
    ScheduleRelease('2025-26', 'schedule-release-2025.pdf',
                    'https://pr.nba.com/wp-content/uploads/sites/46/2025/08/2025-26-NBA-Regular-Season-Schedule-By-Date-8-14-25.pdf',
                    '98a06493d0a107cc410853ad230e67d48858777a2baf76ac77d089cbeaf2435d',
                    dt.datetime.fromisoformat('2025-08-15T04:00:00Z'), 'AUGUST 14, 2025', 1200),
    ScheduleRelease('2026-27', 'opening-followup/schedule-2026-by-date-official.pdf',
                    'https://cdn.nba.com/manage/2026/08/2026-27-NBA-Regular-Season-Schedule-By-Date.pdf',
                    '5e82e37ef1b19e226dee57be69958b95b5694516280d3034aa7c1d64292b3570',
                    dt.datetime.fromisoformat('2026-08-13T19:13:47Z'), 'AUGUST 13, 2026', 1200),
)
TEAM_LABELS = '|'.join(map(re.escape, sorted((*TEAM_IDS, 'L.A. LAKERS', 'L.A. CLIPPERS'), key=len, reverse=True)))
CLOCK = r'(?:\d{1,2}:\d{2}\s+[AP]M|TBD)'
PRINTED_GAME = re.compile(rf'^\s*(?:\d+\s+)?([A-Z][a-z]{{2}})\.\s+(\d{{1,2}}/\d{{1,2}}/\d{{2}})\s+({TEAM_LABELS})'
                          rf'\s+(?:(?:at|vs\.?)\s+)?({TEAM_LABELS})\s+{CLOCK}\s+({CLOCK})(?:\s|$)', re.I)
# Both August 15 team schedules print 8 p.m. Eastern for Cleveland at Milwaukee.
TIP_CONFLICTS = {
    ('2024-25', dt.date(2025, 3, 9), 1610612739, 1610612749): (
        dt.datetime(2025, 3, 9, 20, tzinfo=ET),
        'https://pr.nba.com/wp-content/uploads/sites/46/2024/08/2024-25-NBA-Regular-Season-Schedule-By-Team.pdf',
        '7b0bf05c39e501c5791d42ffeb76053ba1ecce42df580f0742aae859da2eac3f',
    ),
}


def parse_schedule(layout: str, release: ScheduleRelease) -> list[tuple[dt.date, dt.datetime | None, int, int]]:
    if f'{release.season} NBA {release.section} SCHEDULE' not in layout or f'AS OF {release.printed_issue_date}' not in layout:
        raise ValueError(f'unexpected official NBA schedule issue: {release.filename}')
    games = []
    for row in layout.splitlines():
        match = PRINTED_GAME.match(row)
        if not match:
            continue
        weekday, printed_day, away, home, et = match.groups()
        day = dt.datetime.strptime(printed_day, '%m/%d/%y').date()
        if day.strftime('%a').lower() != weekday.lower():
            raise ValueError(f'weekday disagrees with printed NBA schedule date: {row.strip()}')
        tip = None if et.upper() == 'TBD' else dt.datetime.combine(day, dt.datetime.strptime(et, '%I:%M %p').time(), ET)
        games.append((day, tip, TEAM_IDS[away.upper().replace('.', '')], TEAM_IDS[home.upper().replace('.', '')]))
    if len(games) != release.expected_games or len({(day, away, home) for day, _, away, home in games}) != len(games):
        raise ValueError(f'expected {release.expected_games} distinct scheduled NBA games in {release.season}, got {len(games)}')
    return games


def load(c) -> None:
    rows, audit, conflicts = [], [], []
    has_schedule_capture = c.execute("""SELECT EXISTS (SELECT 1 FROM information_schema.views
        WHERE table_schema = 'core' AND table_name = 'pregame_fixture')""").fetchone()[0]
    for release in RELEASES:
        path = CACHE / release.filename
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != release.sha256:
            raise ValueError(f'{release.season} NBA schedule evidence has changed')
        layout = subprocess.run(['pdftotext', '-layout', '-', '-'], input=raw, check=True, capture_output=True).stdout.decode()
        published = parse_schedule(layout, release)
        fixtures = defaultdict(list)
        for gid, day, away, home in c.execute('''SELECT game_id,game_date,away_team_id,home_team_id FROM core.dim_game
            WHERE season_year=? AND season_type='Regular' AND participant_status='resolved' ''', [release.season]).fetchall():
            fixtures[day, away, home].append(gid)
        if has_schedule_capture:
            for gid, day, away, home in c.execute('''SELECT game_id,game_date,away_team_id,home_team_id
                FROM core.pregame_fixture WHERE fixture_source='nba_schedule_mirror' AND starts_with(game_id, ?)''',
                                                 [f'002{release.season[2:4]}']).fetchall():
                fixtures[day, away, home].append(gid)
        for game_date, tip, away, home in published:
            candidates = fixtures[game_date, away, home]
            gid = candidates[0] if len(candidates) == 1 else None
            stamp = tip.isoformat() if tip else game_date.isoformat()
            disposition = 'no_unique_exact_fixture' if gid is None else 'time_not_published' if tip is None else 'exact_date_and_sides'
            key = f'{stamp} {away} {home}'
            alternate = TIP_CONFLICTS.get((release.season, game_date, away, home))
            if alternate:
                conflicts.append((key, gid, tip, release.source_url, release.sha256, *alternate))
                disposition = 'conflicting_published_tip'
            audit.append((key, gid, game_date, tip, away, home, release.source_url, release.sha256,
                          disposition))
            if gid:
                rows.append((gid, game_date, None if alternate else tip, home, away, release.source_url,
                             release.published_by, release.sha256))
    if not audit:
        return
    with warehouse_transaction(c):
        publish_pregame(c)
        c.execute('''CREATE TABLE IF NOT EXISTS audit.nba_initial_schedule_resolution (
            source_key VARCHAR, game_id VARCHAR, game_date DATE, tipoff_at TIMESTAMPTZ, away_team_id BIGINT, home_team_id BIGINT,
            source_url VARCHAR, sha256 VARCHAR, resolution VARCHAR, PRIMARY KEY (source_key,sha256))''')
        c.executemany('''INSERT INTO audit.nba_initial_schedule_resolution VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (source_key,sha256) DO UPDATE SET game_id=excluded.game_id,resolution=excluded.resolution''', audit)
        if rows:
            c.executemany('''INSERT INTO raw.nba_initial_schedule VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT (game_id,source_url,sha256) DO UPDATE SET start_time=excluded.start_time''', rows)
        c.execute('''CREATE TABLE IF NOT EXISTS audit.nba_initial_schedule_tip_conflict (
            source_key VARCHAR,game_id VARCHAR,by_date_tipoff TIMESTAMPTZ,by_date_url VARCHAR,by_date_sha256 VARCHAR,
            by_team_tipoff TIMESTAMPTZ,by_team_url VARCHAR,by_team_sha256 VARCHAR,PRIMARY KEY (source_key,by_date_sha256))''')
        if conflicts:
            c.executemany('INSERT OR IGNORE INTO audit.nba_initial_schedule_tip_conflict VALUES (?,?,?,?,?,?,?,?)', conflicts)
    timed = sum(row[2] is not None for row in rows)
    print(f'Linked {len(rows)} of {len(audit)} games from the initial NBA schedule releases, {timed} with published tip times', flush=True)


if __name__ == '__main__':
    with warehouse_lock(), connect(DEFAULT_OUT) as warehouse:
        load(warehouse)
        warehouse.execute('CHECKPOINT')
