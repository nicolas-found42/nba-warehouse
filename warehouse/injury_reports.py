"""Cache official NBA injury reports, retain source rows, and publish pregame views."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import re
import subprocess
import time
import xml.etree.ElementTree as ETxml
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from dataclasses import astuple, dataclass, replace
from itertools import groupby
from pathlib import Path
from threading import Lock
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
MIRROR_PDF = 'https://basketball-data.billmill.org/nba/raw/stats/{season}/injury_report/'
FIRST_DATE = dt.date(2018, 12, 17)
LEGACY_MINUTE_CHANGE = dt.datetime(2025, 12, 19, 16, tzinfo=ET)
QUARTER_HOUR = dt.datetime(2025, 12, 22, 9, tzinfo=ET)
STATUSES = {'Out', 'Doubtful', 'Questionable', 'Probable', 'Available'}
TEAM_NOTES = {'NOT YET SUBMITTED', 'ALL PLAYERS AVAILABLE'}
REPORT_FILE = re.compile(r'Injury-Report_(\d{4}-\d{2}-\d{2})_\d{2}(?:_\d{2})?[AP]M\.pdf')
ARCHIVE_REPLAY_LOCK = Lock()


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


def report_filename(stamp: dt.datetime) -> str:
    suffix = stamp.strftime('%Y-%m-%d_%I_%M%p' if stamp >= QUARTER_HOUR else '%Y-%m-%d_%I%p')
    return f'Injury-Report_{suffix}.pdf'


def slots(day: dt.date):
    if day < FIRST_DATE:
        return
    hours = range(24) if (day >= dt.date(2021, 7, 1) or dt.date(2020, 7, 22) <= day <= dt.date(2020, 10, 15)) else (13, 17, 20)
    for hour in hours:
        start = dt.datetime.combine(day, dt.time(hour), ET)
        minutes = (0, 15, 30, 45) if start >= QUARTER_HOUR else (45,) if start >= LEGACY_MINUTE_CHANGE else (30,)
        for minute in minutes:
            stamp = start.replace(minute=minute)
            if stamp >= dt.datetime(2018, 12, 17, 17, 30, tzinfo=ET):
                yield stamp, report_filename(stamp)


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


def capture_days(days, now: dt.datetime, workers: int = 1) -> list[Path]:
    def capture(slot):
        stamp, name = slot
        target = CACHE / 'pdf' / name
        download(NBA + name, target, recent=stamp > now - dt.timedelta(hours=2))
        return target if target.is_file() else None

    saved = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for day in sorted(set(days)):
            batch = (slot for slot in slots(day) if slot[0] <= now)
            reports = [path for path in pool.map(capture, batch) if path is not None]
            saved.extend(reports)
            print(f'NBA injury report capture {day}: {len(reports)} PDFs', flush=True)
    return saved


def read_pdf(path: Path) -> tuple[str, dt.datetime, list[ReportRow]]:
    raw = path.read_bytes()
    layout = subprocess.run(['pdftotext', '-bbox-layout', '-', '-'], input=raw, check=True, capture_output=True).stdout
    try:
        stamp, rows = parse_report(layout)
    except ETxml.ParseError as e:
        raise ValueError(f'invalid PDF layout for {path.name}') from e
    return hashlib.sha256(raw).hexdigest(), stamp, rows


def retrieval_url(path: Path) -> str:
    if path.parent.parent == CACHE / 'pdf-mirror':
        return MIRROR_PDF.format(season=int(path.parent.name)) + path.name
    if path.parent.parent == CACHE / 'pdf-wayback':
        return f'https://web.archive.org/web/{path.parent.name}id_/{NBA}{path.name}'
    return NBA + path.name


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
        c.execute('''CREATE TABLE IF NOT EXISTS raw.nba_injury_report_retrieval (
            source_url VARCHAR, sha256 VARCHAR, retrieval_url VARCHAR,
            PRIMARY KEY (source_url, sha256, retrieval_url))''')
        if reports:
            documents, retrieved = {}, set()
            for path, report in reports.items():
                source, digest = NBA + path.name, report[0]
                key = (source, digest)
                if key in documents and documents[key] != report:
                    raise ValueError(f'one injury PDF digest has conflicting parsed rows: {path.name}')
                documents[key] = report
                retrieved.add((source, digest, retrieval_url(path)))
            c.execute('''CREATE OR REPLACE TEMP TABLE injury_pdf_replay AS
                SELECT unnest(?::VARCHAR[]) AS source_url, unnest(?::VARCHAR[]) AS sha256,
                       unnest(?::VARCHAR[]) AS retrieval_url''', list(zip(*sorted(retrieved))))
            c.execute('INSERT OR IGNORE INTO raw.nba_injury_report_retrieval SELECT * FROM injury_pdf_replay')
            c.execute('''DELETE FROM raw.nba_injury_report USING injury_pdf_replay d
                WHERE raw.nba_injury_report.source_url=d.source_url AND raw.nba_injury_report.sha256=d.sha256''')
            rows = [(source, digest, i, stamp, *astuple(row)) for (source, digest), (_, stamp, parsed) in documents.items()
                    for i, row in enumerate(parsed, 1)]
            if rows:
                c.execute('''INSERT INTO raw.nba_injury_report
                    SELECT unnest(?::VARCHAR[]), unnest(?::VARCHAR[]), unnest(?::INTEGER[]), unnest(?::TIMESTAMPTZ[]),
                           unnest(?::DATE[]), unnest(?::VARCHAR[]), unnest(?::VARCHAR[]), unnest(?::VARCHAR[]),
                           unnest(?::VARCHAR[]), unnest(?::VARCHAR[])''', list(zip(*rows)))
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


def indexed_pdfs(warehouse: Path, start: dt.date, end: dt.date, cached_only: bool) -> bool:
    with connect(warehouse, read_only=True) as c:
        stamps = c.execute('''SELECT DISTINCT season, epoch(report_time) AS report_epoch FROM raw.nba_injury_report_mirror
            WHERE timezone('America/New_York', report_time)::DATE BETWEEN ? AND ?
            ORDER BY report_epoch DESC, season''', [start, end]).fetchall()
    indexed = [(season, dt.datetime.fromtimestamp(stamp, ET)) for season, stamp in stamps]
    imported = held = missing = 0
    for (year, month), batch in groupby(indexed, key=lambda row: (row[1].year, row[1].month)):
        reports = {}
        for season, expected_at in batch:
            name = report_filename(expected_at)
            primary = CACHE / 'pdf' / name
            mirrored = CACHE / 'pdf-mirror' / str(season) / name
            path = primary if primary.is_file() else mirrored
            try:
                if not cached_only and path == mirrored:
                    download(retrieval_url(path), path)
                if not path.is_file():
                    missing += 1
                    continue
                document = read_pdf(path)
                if document[1] != expected_at:
                    raise ValueError(f'expected {expected_at.isoformat()}, PDF prints {document[1].isoformat()}')
                reports[path] = document
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                held += 1
                print(f'Held indexed injury report {season}/{name}: {exc}', flush=True)
        if reports:
            with warehouse_lock(), connect(warehouse) as c:
                publish(c, reports)
            imported += len(reports)
            print(f'Published {len(reports)} indexed injury PDFs for {year}-{month:02}', flush=True)
    print(f'Published {imported} of {len(indexed)} indexed PDF snapshots. '
          f'{held} errors or stamp mismatches. {missing} unavailable from the selected cache or route.', flush=True)
    return held == 0 and (cached_only or missing == 0)


def archive_entries(catalog: Path, start: dt.date, end: dt.date) -> dict:
    entries = {}
    with catalog.open(newline='') as source:
        for row in csv.DictReader(source):
            name = row['filename']
            m = REPORT_FILE.fullmatch(name)
            if not m:
                continue
            day = dt.date.fromisoformat(m[1])
            if start <= day <= end:
                capture = row['wayback_captured_at_utc']
                if capture and not re.fullmatch(r'\d{14}', capture):
                    raise ValueError(f'invalid archive capture time for {name}')
                entries[name] = (day, capture, int(row['ending_season']), bool(row.get('mirror_url')))
    return entries


def audit_archive(warehouse: Path, catalog: Path, start: dt.date, end: dt.date) -> bool:
    entries = archive_entries(catalog, start, end)
    with connect(warehouse, read_only=True) as c:
        found = {row[0] for row in c.execute(r'''SELECT DISTINCT regexp_extract(source_url, '[^/]+$')
            FROM raw.nba_injury_report WHERE starts_with(source_url, ?)
              AND timezone('America/New_York',report_time)::DATE =
                  try_cast(regexp_extract(source_url, 'Injury-Report_(\d{4}-\d{2}-\d{2})',1) AS DATE)''', [NBA]).fetchall()}
    missing = entries.keys() - found
    for name in sorted(missing):
        print(f'Missing published PDF from catalogue: {name}', flush=True)
    print(f'Verified {len(entries) - len(missing)} of {len(entries)} catalogued PDF filenames in the warehouse', flush=True)
    return not missing


def replay_cached_pdfs(warehouse: Path, start: dt.date, end: dt.date) -> bool:
    paths = [*CACHE.glob('pdf/*.pdf'), *CACHE.glob('pdf-mirror/*/*.pdf'), *CACHE.glob('pdf-wayback/*/*.pdf')]
    dates = []
    for path in paths:
        match = REPORT_FILE.fullmatch(path.name)
        if match and start <= (day := dt.date.fromisoformat(match[1])) <= end:
            dates.append((day, path))
    held = total = 0
    for (year, month), batch in groupby(sorted(dates), key=lambda item: (item[0].year, item[0].month)):
        reports = {}
        for day, path in batch:
            try:
                document = read_pdf(path)
                if document[1].date() != day:
                    raise ValueError(f'printed report date {document[1].date()} disagrees with the filename')
                reports[path] = document
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                held += 1
                print(f'Held cached injury report {retrieval_url(path)}: {exc}', flush=True)
        if reports:
            with warehouse_lock(), connect(warehouse) as c:
                publish(c, reports)
            total += len(reports)
            print(f'Replayed {len(reports)} cached PDF copies for {year}-{month:02}', flush=True)
    print(f'Replayed {total} cached PDF copies. {held} require another check.', flush=True)
    return held == 0


def archive_pdfs(warehouse: Path, catalog: Path, start: dt.date, end: dt.date) -> bool:
    entries = archive_entries(catalog, start, end)
    ordered = sorted(entries.items(), key=lambda item: (item[1][0], item[0]), reverse=True)
    imported = failures = 0
    with ThreadPoolExecutor(max_workers=3) as pool:
        for (year, week), batch in groupby(ordered, key=lambda entry: entry[1][0].isocalendar()[:2]):
            reports = {}
            for result in pool.map(load_archive_entry, batch):
                if result is None:
                    failures += 1
                else:
                    path, document = result
                    reports[path] = document
            if reports:
                with warehouse_lock(), connect(warehouse) as c:
                    publish(c, reports)
                imported += len(reports)
                print(f'Published {len(reports)} original injury PDFs for ISO week {year}-{week:02}', flush=True)
    print(f'Published {imported} of {len(entries)} catalogued injury PDFs; {failures} require another check', flush=True)
    return failures == 0


def load_archive_entry(entry):
    name, (_, capture, season, has_mirror) = entry
    primary = CACHE / 'pdf' / name
    mirrored = CACHE / 'pdf-mirror' / str(season) / name
    candidates = [primary]
    if mirrored.is_file():
        candidates.insert(int(primary.is_file()), mirrored)
    elif has_mirror:
        candidates.append(mirrored)
    if capture:
        candidates.append(CACHE / 'pdf-wayback' / capture / name)
    errors = []
    for path in candidates:
        try:
            url = retrieval_url(path)
            if path.parent.parent == CACHE / 'pdf-wayback':
                with ARCHIVE_REPLAY_LOCK:
                    download(url, path)
            else:
                download(url, path)
            if not path.is_file():
                continue
            document = read_pdf(path)
            if report_filename(document[1]) != name:
                raise ValueError(f'printed timestamp {document[1].isoformat()} disagrees with indexed filename')
            return path, document
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            errors.append(f'{retrieval_url(path)}: {exc}')
    print(f'Held archived injury report {name}: {". ".join(errors) or "no candidate URL yielded a PDF"}', flush=True)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['mirror', 'daily', 'backfill', 'cached', 'rosters', 'indexed-pdfs', 'archive-pdfs', 'audit-archive'])
    parser.add_argument('--warehouse', type=Path, default=DEFAULT_OUT)
    parser.add_argument('--from-date', type=dt.date.fromisoformat, default=FIRST_DATE)
    parser.add_argument('--through', type=dt.date.fromisoformat)
    parser.add_argument('--cached-only', action='store_true', help='indexed-pdfs: import downloaded files without fetching')
    parser.add_argument('--index', type=Path, help='archive-pdfs and audit-archive: local official and Wayback PDF URL index')
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
    elif args.mode == 'cached':
        raise SystemExit(0 if replay_cached_pdfs(args.warehouse, args.from_date, args.through or now.date()) else 1)
    elif args.mode == 'indexed-pdfs':
        raise SystemExit(0 if indexed_pdfs(args.warehouse, args.from_date, args.through or now.date(), args.cached_only) else 1)
    elif args.mode in ('archive-pdfs', 'audit-archive'):
        if args.index is None:
            parser.error(f'{args.mode} requires --index')
        operation = archive_pdfs if args.mode == 'archive-pdfs' else audit_archive
        raise SystemExit(0 if operation(args.warehouse, args.index, args.from_date, args.through or now.date()) else 1)
    elif args.mode == 'backfill':
        with connect(args.warehouse, read_only=True) as c:
            dates = c.execute('''SELECT DISTINCT game_date FROM core.dim_game WHERE season_type IN ('Preseason','Regular','Playoffs','PlayIn','Cup')
                AND game_date BETWEEN ? AND ?''', [args.from_date, args.through or now.date()]).fetchall()
        # Reports also include the following day's games.
        days = {day for (game_day,) in dates for day in (game_day, game_day - dt.timedelta(days=1)) if day >= args.from_date}
        total = 0
        for (year, month), batch in groupby(sorted(days), key=lambda day: (day.year, day.month)):
            reports = {path: read_pdf(path) for path in capture_days(batch, now, workers=3)}
            with warehouse_lock(), connect(args.warehouse) as c:
                publish(c, reports)
            total += len(reports)
            print(f'Published {len(reports)} injury reports for {year}-{month:02}', flush=True)
        print(f'Published {total} NBA injury reports in the requested range', flush=True)
        return
    with warehouse_lock(), connect(args.warehouse) as c:
        publish(c, reports, mirrors, pregame)
    print(f'Published source data from {len(reports)} NBA PDFs and {len(mirrors)} season files', flush=True)


if __name__ == '__main__':
    main()
