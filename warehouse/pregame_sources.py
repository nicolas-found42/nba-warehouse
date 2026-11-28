"""Public NBA schedule and roster captures mirrored by llimllib/nba_data."""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import duckdb

from fetch_wayback_nba import cache_bytes

SOURCE = 'https://basketball-data.billmill.org/nba/'
CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'pregame'
ET = ZoneInfo('America/New_York')


from observability import error_details, event, stage

def fetch(url: str, dest: Path, signature: bytes) -> Path:
    if not dest.exists():
        with urlopen(Request(url, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=20) as response:
            body = response.read(12_000_000)
        if not body.startswith(signature):
            raise ValueError(f'unexpected file format: {url}')
        cache_bytes(dest, body)
    return dest


def roster_bytes(url: str, dest: Path) -> tuple[bytes, dt.datetime, dt.datetime | None]:
    receipt = dest.with_suffix('.http.json')
    if dest.is_file() and receipt.is_file():
        raw = dest.read_bytes()
        meta = json.loads(receipt.read_text())
        if meta['sha256'] == hashlib.sha256(raw).hexdigest() and meta['url'] == url:
            return (raw, dt.datetime.fromisoformat(meta['received_at']),
                    dt.datetime.fromisoformat(meta['last_modified']) if meta['last_modified'] else None)
    with urlopen(Request(url, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=20) as response:
        raw = response.read(12_000_000)
        last_modified = response.headers.get('Last-Modified')
    received = dt.datetime.now(dt.timezone.utc)
    modified = None
    if last_modified:
        try:
            modified = parsedate_to_datetime(last_modified)
        except ValueError:
            pass
    if not raw.startswith(b'\x1f\x8b'):
        raise ValueError(f'unexpected roster payload: {url}')
    digest = hashlib.sha256(raw).hexdigest()
    if dest.is_file():
        original = dest.read_bytes()
        if original != raw:
            cache_bytes(dest.with_name(f'{dest.stem}-{hashlib.sha256(original).hexdigest()}{dest.suffix}'), original)
    cache_bytes(dest, raw)
    cache_bytes(receipt, json.dumps({'url': url, 'sha256': digest, 'received_at': received.isoformat(),
                                    'last_modified': modified.isoformat() if modified else None}).encode())
    return raw, received, modified


def roster_availability(started: dt.datetime, received: dt.datetime, modified: dt.datetime | None) -> tuple[dt.datetime, str]:
    if started.tzinfo is None or received.tzinfo is None or started > received:
        raise ValueError('roster capture start is later than retrieval or has no timezone')
    if modified is not None and modified.tzinfo is not None and started <= modified <= received:
        return modified, 'publisher_object_last_modified'
    return received, 'local_retrieval'


def capture(now: dt.datetime, history: bool = False):
    season = now.year + (now.month >= 7)
    schedule_url = f'{SOURCE}stats/schedule/season={season}/data.parquet'
    schedule_target = CACHE / f'{now:%Y-%m-%d_%H}-schedule.parquet'
    with stage('pregame_schedule', cached=schedule_target.is_file()) as step:
        schedule = fetch(schedule_url, schedule_target, b'PAR1')
        step.result['schedules'] = 1
    if history:
        roster_index = fetch(f'{SOURCE}stats/rosters_daily/season={season}/data.parquet',
                             CACHE / f'{now:%Y-%m-%d_%H}-roster-index.parquet', b'PAR1')
        with duckdb.connect() as c:
            dates = [d for (d,) in c.execute('SELECT DISTINCT as_of_date FROM read_parquet(?) ORDER BY 1', [str(roster_index)]).fetchall()]
    else:
        dates = [now.date() - dt.timedelta(days=1), now.date()]
    rosters, publications = [], []
    for date in dates:
        url = f'{SOURCE}raw/stats/{season}/team_rosters/{date}.json.gz'
        try:
            raw, received, modified = roster_bytes(url, CACHE / f'{date}-roster.json.gz')
        except HTTPError as e:
            if e.code in (403, 404) and date == now.date():
                event('pregame_roster', 'deferred', availability_date=date.isoformat(), **error_details(e))
                continue  # the publisher may not have captured today's rosters yet
            raise
        event('pregame_roster', 'received', availability_date=date.isoformat(), sha256=hashlib.sha256(raw).hexdigest())
        envelope = json.loads(gzip.decompress(raw))
        captured_at = dt.datetime.fromisoformat(envelope['fetched'])
        if captured_at.tzinfo is None or captured_at.astimezone(ET).date() != date or captured_at > now:
            raise ValueError(f'unexpected roster capture timestamp: {url}')
        digest = hashlib.sha256(raw).hexdigest()
        available_at, basis = roster_availability(captured_at, received, modified)
        publications.append((url, digest, available_at, basis))
        for team_id, roster in envelope['rosters'].items():
            result = next(r for r in roster['resultSets'] if r['name'] == 'CommonTeamRoster')
            for values in result['rowSet']:
                row = dict(zip(result['headers'], values, strict=True))
                if row['TeamID'] != int(team_id) or row['LeagueID'] != '00' or int(row['SEASON']) != season - 1:
                    raise ValueError(f'wrong team or season in NBA roster: {url}')
                rosters.append((int(team_id), row['PLAYER_ID'], row['PLAYER'], row['NUM'], row['POSITION'], date,
                                captured_at, url, digest))
    return schedule, schedule_url, rosters, publications


def publish(c, capture_data=None) -> None:
    """Write to the caller's transaction and make fixture/roster views available on a fresh warehouse."""
    c.execute('''CREATE TABLE IF NOT EXISTS raw.nba_pregame_schedule (
        game_id VARCHAR, game_date DATE, start_time TIMESTAMPTZ, home_team_id BIGINT, away_team_id BIGINT,
        status VARCHAR, source_url VARCHAR, captured_at TIMESTAMPTZ, sha256 VARCHAR)''')
    c.execute('''CREATE TABLE IF NOT EXISTS raw.nba_initial_schedule (
        game_id VARCHAR, game_date DATE, start_time TIMESTAMPTZ, home_team_id BIGINT, away_team_id BIGINT,
        source_url VARCHAR, published_at TIMESTAMPTZ, sha256 VARCHAR,
        PRIMARY KEY (game_id,source_url,sha256))''')
    c.execute('''CREATE TABLE IF NOT EXISTS raw.nba_roster_capture (
        team_id BIGINT, person_id BIGINT, player_name VARCHAR, jersey_number VARCHAR, position VARCHAR,
        source_date DATE, observed_at TIMESTAMPTZ, source_url VARCHAR, sha256 VARCHAR,
        PRIMARY KEY (team_id, person_id, observed_at, sha256))''')
    c.execute('''CREATE TABLE IF NOT EXISTS raw.nba_roster_publication (
        source_url VARCHAR, sha256 VARCHAR, available_at TIMESTAMPTZ, availability_basis VARCHAR,
        PRIMARY KEY (source_url, sha256))''')
    if capture_data:
        schedule, url, rosters, publications = capture_data
        if schedule is not None:
            stamp = dt.datetime.fromtimestamp(schedule.stat().st_mtime, dt.timezone.utc)
            digest = hashlib.sha256(schedule.read_bytes()).hexdigest()
            c.execute('''INSERT INTO raw.nba_pregame_schedule
                SELECT game_id, game_date, start_time, try_cast(home_team_id AS BIGINT), try_cast(away_team_id AS BIGINT), status,
                       ?, ?, ? FROM read_parquet(?)
                WHERE NOT EXISTS (SELECT 1 FROM raw.nba_pregame_schedule WHERE captured_at = ? AND sha256 = ?)''',
                [url, stamp, digest, str(schedule), stamp, digest])
        if rosters:
            c.executemany('INSERT OR IGNORE INTO raw.nba_roster_capture VALUES (?,?,?,?,?,?,?,?,?)', rosters)
        if publications:
            c.executemany('''INSERT INTO raw.nba_roster_publication VALUES (?,?,?,?)
                ON CONFLICT (source_url, sha256) DO UPDATE SET available_at=excluded.available_at,
                availability_basis=excluded.availability_basis WHERE excluded.available_at < raw.nba_roster_publication.available_at''',
                publications)
    c.execute(Path(__file__).with_suffix('.sql').read_text())
