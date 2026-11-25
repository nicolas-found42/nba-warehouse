"""Cache official NBA injury reports, retain source rows, and publish pregame views."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import re
import subprocess
import time
import xml.etree.ElementTree as ETxml
from bisect import bisect_right
from dataclasses import astuple, dataclass, replace
from itertools import groupby
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock
from fetch_wayback_nba import cache_bytes
import pregame_sources

ET = ZoneInfo('America/New_York')
CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'injury-reports'
NBA = 'https://ak-static.cms.nba.com/referee/injury/'
MIRROR = 'https://basketball-data.billmill.org/nba/stats/injury_report/'
FIRST_DATE = dt.date(2018, 12, 17)
QUARTER_HOUR = dt.date(2025, 12, 22)
STATUSES = {'Out', 'Doubtful', 'Questionable', 'Probable', 'Available'}
TEAM_NOTES = {'NOT YET SUBMITTED', 'ALL PLAYERS AVAILABLE'}


@dataclass(frozen=True)
class ReportRow:
    game_date: dt.date
    matchup: str
    team_name: str
    player_name: str | None
    status: str
    reason: str


def physical_lines(document: bytes):
    """Group words by their PDF baseline even when Poppler puts columns in separate blocks."""
    for page_number, page in enumerate(ETxml.fromstring(document).findall('.//{*}page')):
        words = sorted((float(w.attrib['yMin']), float(w.attrib['xMin']), w.text or '')
                       for w in page.findall('.//{*}word'))
        y, line = None, []
        for row_y, x, word in words:
            if y is None or row_y - y > 3:
                if line:
                    yield page_number, y, sorted(line)
                y, line = row_y, []
            line.append((x, word))
        if line:
            yield page_number, y, sorted(line)


def parse_report(document: bytes) -> tuple[dt.datetime, list[ReportRow]]:
    published = None
    columns = []
    day = None
    matchup = team = ''
    rows = []
    players_by_page: dict[int, list[tuple[float, int]]] = {}
    reasons_by_page: dict[int, list[tuple[float, str]]] = {}
    for page, y, words in physical_lines(document):
        line = ' '.join(word for _, word in words)
        if 'Game Date' in line and 'Player Name' in line and 'Current Status' in line:
            labels = ['Game Date', 'Game Time', 'Matchup', 'Team', 'Player Name', 'Current Status', 'Reason', 'Category', 'Previous Status']
            tokens = [word for _, word in words]
            columns = sorted((x - 1, label) for i, (x, _) in enumerate(words) for label in labels
                             if tokens[i:i + len(label.split())] == label.split())
            continue
        stamp = re.search(r'Injury Report:\s*(\d{1,2}/\d{1,2}/\d{2}\s+\d{1,2}:\d{2}\s+[AP]M)', line)
        if not line or stamp or re.fullmatch(r'Page \d+ of \d+', line):
            if stamp:
                time_value = dt.datetime.strptime(stamp[1], '%m/%d/%y %I:%M %p').replace(tzinfo=ET)
                if published is not None and published != time_value:
                    raise ValueError('pages have different injury report timestamps')
                published = time_value
            continue
        if not columns:
            continue
        boundaries, labels = zip(*columns)
        text_by_column = {label: [] for label in labels}
        for x, word in words:
            index = bisect_right(boundaries, x) - 1
            if index >= 0:
                text_by_column[labels[index]].append(word)
        cells = {label: ' '.join(tokens) for label, tokens in text_by_column.items()}
        if re.fullmatch(r'\d{1,2}/\d{1,2}/\d{4}', cells['Game Date']):
            day = dt.datetime.strptime(cells['Game Date'], '%m/%d/%Y').date()
        if re.fullmatch(r'[A-Z]{2,3}@[A-Z]{2,3}', cells['Matchup']):
            matchup, team = cells['Matchup'], ''
        if cells['Team']:
            team = cells['Team']
        player, status = cells['Player Name'], cells['Current Status']
        reason = ' '.join(cell for cell in (cells.get('Category'), cells['Reason']) if cell and cell != '-')
        if not day or not matchup or not team:
            continue
        if status in STATUSES and ',' in player:
            players_by_page.setdefault(page, []).append((y, len(rows)))
            rows.append(ReportRow(day, matchup, team, player, status, ''))
            if reason:
                reasons_by_page.setdefault(page, []).append((y, reason))
        elif (note := next((n for n in TEAM_NOTES if n in line), None)):
            rows.append(ReportRow(day, matchup, team, None, note, ''))
        elif reason and not player and not status:
            reasons_by_page.setdefault(page, []).append((y, reason))
    if not published or not rows:
        raise ValueError('no recognized report timestamp or rows')
    for page, fragments in reasons_by_page.items():
        anchors = players_by_page.get(page, [])
        for y, fragment in fragments:
            distances = sorted((abs(y - pos), index) for pos, index in anchors)
            if distances and distances[0][0] <= 20 and (len(distances) == 1 or distances[0][0] < distances[1][0]):
                _, index = distances[0]
                row = rows[index]
                rows[index] = replace(row, reason=f'{row.reason} {fragment}'.strip())
    return published, rows


def slots(day: dt.date):
    if day < FIRST_DATE:
        return
    if day >= QUARTER_HOUR:
        hours, minutes = range(24), (0, 15, 30, 45)
    elif day >= dt.date(2021, 7, 1) or dt.date(2020, 7, 22) <= day <= dt.date(2020, 10, 15):
        hours, minutes = range(24), (30,)
    else:
        hours, minutes = (13, 17, 20), (30,)
    for hour in hours:
        for minute in minutes:
            stamp = dt.datetime.combine(day, dt.time(hour, minute), ET)
            if stamp >= dt.datetime(2018, 12, 17, 17, 30, tzinfo=ET):
                suffix = stamp.strftime('%Y-%m-%d_%I_%M%p' if day >= QUARTER_HOUR else '%Y-%m-%d_%I%p')
                yield stamp, f'Injury-Report_{suffix}.pdf'


def download(url: str, dest: Path, recent: bool = False) -> None:
    missing = dest.with_suffix('.missing')
    if dest.is_file() or (not recent and missing.is_file()):
        return
    try:
        with urlopen(Request(url, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=30) as response:
            body = response.read(12_000_000)
        signature = b'%PDF-' if dest.suffix == '.pdf' else b'PAR1'
        if not body.startswith(signature):
            raise ValueError(f'unexpected document format: {url}')
        cache_bytes(dest, body)
    except HTTPError as e:
        # The static NBA CDN returns AccessDenied on nonexistent report objects.
        if dest.suffix != '.pdf' or e.code not in (403, 404):
            raise
        if not recent:
            cache_bytes(missing, f'{e.code} {url}\n'.encode())
    finally:
        time.sleep(1)


def capture_days(days, now: dt.datetime) -> list[Path]:
    saved = []
    for day in sorted(set(days)):
        prior = len(saved)
        for stamp, name in slots(day):
            if stamp > now:
                break
            target = CACHE / 'pdf' / name
            download(NBA + name, target, recent=stamp > now - dt.timedelta(hours=2))
            if target.is_file():
                saved.append(target)
        print(f'NBA injury report capture {day}: {len(saved) - prior} PDFs', flush=True)
    return saved


def read_pdf(path: Path) -> tuple[str, dt.datetime, list[ReportRow]]:
    raw = path.read_bytes()
    layout = subprocess.run(['pdftotext', '-bbox-layout', '-', '-'], input=raw, check=True, capture_output=True).stdout
    try:
        stamp, rows = parse_report(layout)
    except ETxml.ParseError as e:
        raise ValueError(f'invalid PDF layout for {path.name}') from e
    return hashlib.sha256(raw).hexdigest(), stamp, rows


def publish(c, reports: dict, mirror: list[Path] | None = None, pregame=None) -> None:
    c.execute('BEGIN')
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS raw.nba_injury_report (
            source_url VARCHAR, sha256 VARCHAR, row_number INTEGER, report_time TIMESTAMPTZ, game_date DATE,
            matchup VARCHAR, team_name VARCHAR, player_name VARCHAR, status VARCHAR, reason VARCHAR,
            PRIMARY KEY (source_url, sha256, row_number))''')
        c.execute('''CREATE TABLE IF NOT EXISTS raw.nba_injury_report_mirror (
            season INTEGER, report_time TIMESTAMPTZ, game_id VARCHAR, game_date DATE, team_id VARCHAR,
            player_id VARCHAR, status VARCHAR, reason VARCHAR, detail VARCHAR, source_url VARCHAR)''')
        for path, (digest, stamp, rows) in reports.items():
            c.execute('DELETE FROM raw.nba_injury_report WHERE source_url = ? AND sha256 = ?', [NBA + path.name, digest])
            c.executemany('INSERT INTO raw.nba_injury_report VALUES (?,?,?,?,?,?,?,?,?,?)',
                          [(NBA + path.name, digest, i, stamp, *astuple(row)) for i, row in enumerate(rows, 1)])
        for path in mirror or []:
            season = int(path.stem)
            c.execute('DELETE FROM raw.nba_injury_report_mirror WHERE season = ?', [season])
            c.execute('''INSERT INTO raw.nba_injury_report_mirror BY NAME
                SELECT season, report_time, game_id, game_date, team_id, player_id, status, reason, detail,
                       ? AS source_url FROM read_parquet(?) WHERE season = ?''',
                      [f'{MIRROR}season={season}/data.parquet', str(path), season])
        pregame_sources.publish(c, pregame)
        c.execute(Path(__file__).with_suffix('.sql').read_text())
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise


def capture_daily(now: dt.datetime) -> dict:
    paths = capture_days([now.date() - dt.timedelta(days=1), now.date()], now)
    return {path: read_pdf(path) for path in paths}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['mirror', 'daily', 'backfill', 'cached', 'rosters'])
    parser.add_argument('--warehouse', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--from-date', type=dt.date.fromisoformat, default=FIRST_DATE)
    parser.add_argument('--through', type=dt.date.fromisoformat)
    args = parser.parse_args()
    now = dt.datetime.now(ET)
    mirrors = []
    pregame = None
    if args.mode == 'mirror':
        last_season = now.year + (now.month >= 7)
        for season in range(2019, last_season + 1):
            directory = CACHE / 'mirror'
            if season == last_season:
                directory /= f'{now:%Y-%m-%d_%H}'
            target = directory / f'{season}.parquet'
            download(f'{MIRROR}season={season}/data.parquet', target)
            mirrors.append(target)
        reports = {}
    elif args.mode == 'daily':
        reports = capture_daily(now)
        pregame = pregame_sources.capture(now)
    elif args.mode == 'rosters':
        reports = {}
        pregame = pregame_sources.capture(now, history=True)
    elif args.mode == 'backfill':
        with connect(args.warehouse, read_only=True) as c:
            dates = c.execute('''SELECT DISTINCT game_date FROM core.dim_game WHERE season_type IN ('Regular','Playoffs')
                AND game_date BETWEEN ? AND ?''', [args.from_date, args.through or now.date()]).fetchall()
        # Reports also include the following day's games.
        days = {day for (game_day,) in dates for day in (game_day, game_day - dt.timedelta(days=1)) if day >= args.from_date}
        total = 0
        for (year, month), batch in groupby(sorted(days), key=lambda day: (day.year, day.month)):
            reports = {path: read_pdf(path) for path in capture_days(batch, now)}
            with warehouse_lock(), connect(args.warehouse) as c:
                publish(c, reports)
            total += len(reports)
            print(f'Published {len(reports)} injury reports for {year}-{month:02}', flush=True)
        print(f'Published {total} NBA injury reports in the requested range', flush=True)
        return
    else:
        reports = {path: read_pdf(path) for path in sorted((CACHE / 'pdf').glob('*.pdf'))}
    with warehouse_lock(), connect(args.warehouse) as c:
        publish(c, reports, mirrors, pregame)
    print(f'Published source data from {len(reports)} NBA PDFs and {len(mirrors)} season files', flush=True)


if __name__ == '__main__':
    main()
