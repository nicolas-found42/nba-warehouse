"""Daily current-season refresh into the single warehouse.

Runs unattended (launchd, 06:00 ET with retries). Each run:
  1. captures the publisher-digest-pinned SportsDataverse season assets into local evidence,
  2. rebuilds the current-season snapshot with refresh_current_season.py (all of its checks,
     including that no previously admitted final changed and an exact offline replay),
  3. publishes NBA injury reports and roster captures independently of the completed-game cutoff,
  4. swaps the season's rows in core/audit on a copy-on-write clone of the warehouse file,
     re-runs the integrity checks, and atomically replaces the warehouse only if all pass.

A failed check leaves the completed-game release unchanged and writes a report to
data/refresh-reports/. When the publisher has not yet released data that closes yesterday's
games, the run exits 75 ("not ready") and the next scheduled attempt tries again.

  uv run --no-project --with duckdb==2.0.0 python warehouse/daily_refresh.py [--cutoff YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import duckdb
import pregame_sources

from build_warehouse import DEFAULT_OUT, ROOT, connect, lit, q, warehouse_lock
from injury_reports import capture_daily, publish as publish_injury_reports

ET = ZoneInfo('America/New_York')
EVIDENCE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence'
REPORTS = ROOT / 'data' / 'refresh-reports'
WAREHOUSE_DIR = ROOT / 'warehouse'
PY = [sys.executable]
NOT_READY = 75
SEASON_END = 2027
SEASON_YEAR = '2026-27'
PREFIX = 'season_2027__'
# core tables that hold current-season rows, and how a row is tied to the season
SEASON_TABLES = {
    'dim_season': f"season_year = '{SEASON_YEAR}'",
    'dim_game': f"season_year = '{SEASON_YEAR}'",
}
GAME_TABLES = ['bridge_game_team', 'fact_game_result', 'fact_game_period_score', 'fact_team_game_stats',
               'fact_player_game_stats', 'fact_play_event']
# The current release does not generate these tables. Restore the season's rows in dependency order.
PRESERVED_GAME_TABLES = ['bridge_event_fixture', 'fact_shot', 'fact_pbp_field_goal_attempt',
                         'bridge_shot_event_source_v20', 'bridge_shot_event']
UA = 'nba-warehouse-personal-refresh (local, low volume)'


class NotReady(Exception):
    pass


def run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, cwd=WAREHOUSE_DIR, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f'{" ".join(cmd[-6:])} failed:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}')
    return r.stdout


def export_inputs(wh: Path, work: Path) -> tuple[Path, Path, Path]:
    """Recreate the three small input contracts refresh_current_season.py expects, from the warehouse."""
    c = duckdb.connect(config={'threads': 4})
    c.execute(f'ATTACH {lit(str(wh))} AS wh (READ_ONLY)')
    hist, prior, prev = work / 'history.duckdb', work / 'prior.duckdb', work / 'previous.duckdb'
    c.execute(f'ATTACH {lit(str(hist))} AS h; CREATE SCHEMA h.nba')
    for (t,) in c.execute("SELECT table_name FROM duckdb_tables() WHERE database_name='wh' AND schema_name='core'").fetchall():
        cols = [r[0] for r in c.execute(f"SELECT column_name FROM duckdb_columns() WHERE database_name='wh' AND schema_name='core' AND table_name={lit(t)} AND column_name NOT IN ('snapshot_kind','as_of_date') ORDER BY column_index").fetchall()]
        sel = ', '.join(q(x) for x in cols)
        where = {'dim_game': f"WHERE season_year <> '{SEASON_YEAR}'", 'dim_team': '', 'dim_person': ''}.get(t, 'WHERE false')
        c.execute(f'CREATE TABLE h.nba.{q(t)} AS SELECT {sel} FROM wh.core.{q(t)} {where}')
    # fact_game is a view in the historical contract
    c.execute('''CREATE VIEW h.nba.fact_game AS SELECT g.*, r.home_score, r.away_score, r.winner_team_id, r.is_overtime
                 FROM h.nba.dim_game g LEFT JOIN h.nba.fact_game_result r USING (game_id)''')
    # the other core views only need their column shape (refresh_current_season.py compares shapes)
    for (v,) in c.execute("SELECT view_name FROM duckdb_views() WHERE database_name='wh' AND schema_name='core' AND view_name <> 'fact_game'").fetchall():
        c.execute(f'CREATE TABLE h.nba.{q(v)} AS SELECT * FROM wh.core.{q(v)} LIMIT 0')
    c.execute('DETACH h')
    c.execute(f'ATTACH {lit(str(prior))} AS p; CREATE SCHEMA p.audit')
    for t in ('result_for_local_review', 'verified_period_score', 'team_box_local_review'):
        c.execute(f'CREATE TABLE p.audit.{t} AS SELECT * FROM wh.audit."check_box_check_2026_11_19__{t}"')
    c.execute('DETACH p')
    c.execute(f'ATTACH {lit(str(prev))} AS v; CREATE SCHEMA v.nba; CREATE SCHEMA v.audit')
    c.execute(f'CREATE TABLE v.audit.scope AS SELECT * FROM wh.audit."{PREFIX}scope"')
    c.execute(f"CREATE TABLE v.nba.fact_game AS SELECT * FROM wh.core.fact_game WHERE season_year = '{SEASON_YEAR}'")
    c.execute('DETACH v')
    c.close()
    return hist, prior, prev


def capture(cutoff: dt.date, started_at: dt.datetime) -> Path:
    stamp = started_at.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H%M%S.%fZ')
    out = EVIDENCE / f'sdv-{SEASON_END}-{stamp}-for-{cutoff.isoformat()}'
    run(PY + ['capture_current_assets.py', '--season-end', str(SEASON_END), '--output', str(out)])
    return out


def build_snapshot(assets: Path, cutoff: dt.date, hist: Path, prior: Path, prev: Path, out: Path) -> None:
    args = ['refresh_current_season.py', 'build', '--cutoff', cutoff.isoformat(), '--season-end', str(SEASON_END),
            '--assets', str(assets), '--history', str(hist), '--prior', str(prior), '--previous-release', str(prev),
            '--output', str(out)]
    try:
        run(PY + args)
    except RuntimeError as e:
        if 'Cutoff is not closed' in str(e) or 'predates the end of cutoff' in str(e):
            raise NotReady('publisher assets do not yet cover the cutoff') from e
        raise


def referee_assignments(c: duckdb.DuckDBPyConnection, captured_at: dt.datetime) -> int:
    """Today's official referee crews (official.nba.com publishes only the current day)."""
    url = 'https://official.nba.com/referee-assignments/'
    with urlopen(Request(url, headers={'User-Agent': UA}), timeout=30) as r:
        page = r.read(5_000_000).decode('utf-8', 'replace')
    day = EVIDENCE / 'referee-assignments'
    day.mkdir(parents=True, exist_ok=True)
    (day / f'{captured_at.astimezone(ET).date().isoformat()}.html').write_text(page)
    c.execute('''CREATE TABLE IF NOT EXISTS raw.referee_assignment_snapshot (captured_at TIMESTAMPTZ, assignment_date DATE,
        table_index INTEGER, row_index INTEGER, cells VARCHAR[], source_url VARCHAR)''')
    n = 0
    for ti, table in enumerate(re.findall(r'<table.*?</table>', page, re.S | re.I)):
        for ri, row in enumerate(re.findall(r'<tr.*?</tr>', table, re.S | re.I)):
            cells = [html.unescape(re.sub(r'<[^>]+>', ' ', x)).split() for x in re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.S | re.I)]
            cells = [' '.join(x) for x in cells]
            if any(cells):
                c.execute('INSERT INTO raw.referee_assignment_snapshot VALUES (?,?,?,?,?,?)',
                          [captured_at, captured_at.astimezone(ET).date(), ti, ri, cells, url])
                n += 1
    return n


def integrity(c: duckdb.DuckDBPyConnection) -> list[str]:
    fails = []
    for t in GAME_TABLES + ['fact_shot']:
        n = c.execute(f'SELECT count(*) FROM core.{t} ANTI JOIN core.dim_game USING (game_id)').fetchone()[0]
        if n:
            fails.append(f'core.{t}: {n} orphan rows')
    for t, k in (('dim_game', 'game_id'), ('dim_person', 'person_id'), ('dim_team', 'team_id'), ('fact_game_result', 'game_id')):
        n = c.execute(f'SELECT count(*) - count(DISTINCT {k}) FROM core.{t}').fetchone()[0]
        if n:
            fails.append(f'core.{t}: {n} duplicate {k}')
    n = c.execute(f"""SELECT count(*) FROM core.fact_regular_standings
        WHERE (season_year = '{SEASON_YEAR}') <> (snapshot_kind = 'as_of' AND as_of_date IS NOT NULL)""").fetchone()[0]
    if n:
        fails.append(f'{n} standings rows mislabelled')
    # every final's winner is one of its two teams, and scores never tie
    n = c.execute('''SELECT count(*) FROM core.fact_game_result r
        WHERE home_score = away_score OR winner_team_id NOT IN (SELECT team_id FROM core.bridge_game_team b WHERE b.game_id = r.game_id)''').fetchone()[0]
    if n:
        fails.append(f'{n} finals with a tie or a winner outside the fixture')
    return fails


def merge(wh: Path, snap: Path, cutoff: dt.date, refs_captured: dt.datetime, work: Path) -> dict:
    """Apply the snapshot to a clone; replace the warehouse only if every check passes."""
    clone = wh.with_name(wh.stem + '_refresh.duckdb')
    clone.unlink(missing_ok=True)
    subprocess.run(['cp', '-c', str(wh), str(clone)], check=True)  # APFS clone: instant, no extra space
    try:
        c = connect(clone)
        c.execute(f'ATTACH {lit(str(snap))} AS snap (READ_ONLY)')
        season_games = f"(SELECT game_id FROM core.dim_game WHERE season_year = '{SEASON_YEAR}')"
        before = {t: c.execute(f'SELECT count(*) FROM core.{t} WHERE game_id IN {season_games}').fetchone()[0] for t in GAME_TABLES}
        old_finals = c.execute(f'SELECT * FROM core.fact_game_result WHERE game_id IN {season_games}').fetchall()
        tables = {t for (t,) in c.execute('''SELECT table_name FROM duckdb_tables()
                    WHERE database_name = current_database() AND schema_name = 'core' ''').fetchall()}
        preserved = [t for t in PRESERVED_GAME_TABLES if t in tables]
        for t in reversed(preserved):
            c.execute(f'CREATE TEMP TABLE {q(t)} AS SELECT * FROM core.{q(t)} WHERE game_id IN {season_games}')
            c.execute(f'DELETE FROM core.{q(t)} WHERE game_id IN {season_games}')
        for t in reversed(GAME_TABLES):
            c.execute(f'DELETE FROM core.{t} WHERE game_id IN {season_games}')
        c.execute(f'DELETE FROM core.fact_regular_standings WHERE season_year = {lit(SEASON_YEAR)}')
        c.execute(f'DELETE FROM core.dim_game WHERE season_year = {lit(SEASON_YEAR)}')
        c.execute('INSERT INTO core.dim_person SELECT s.* FROM snap.nba.dim_person s ANTI JOIN core.dim_person USING (person_id)')
        c.execute('INSERT INTO core.dim_team SELECT s.* FROM snap.nba.dim_team s ANTI JOIN core.dim_team USING (team_id)')
        c.execute('INSERT INTO core.dim_season SELECT s.* FROM snap.nba.dim_season s ANTI JOIN core.dim_season USING (season_start)')
        c.execute('INSERT INTO core.dim_game SELECT * FROM snap.nba.dim_game')
        for t in GAME_TABLES:
            c.execute(f'INSERT INTO core.{t} SELECT * FROM snap.nba.{t}')
        for t in preserved:
            c.execute(f'INSERT INTO core.{q(t)} SELECT * FROM temp.main.{q(t)}')
        c.execute(f"INSERT INTO core.fact_regular_standings SELECT *, 'as_of', DATE {lit(cutoff.isoformat())} FROM snap.nba.fact_regular_standings")
        for (t,) in c.execute("SELECT table_name FROM duckdb_tables() WHERE database_name='snap' AND schema_name='audit'").fetchall():
            c.execute(f'CREATE OR REPLACE TABLE audit.{q(PREFIX + t)} AS SELECT * FROM snap.audit.{q(t)}')
        refs = referee_assignments(c, refs_captured)
        fails = integrity(c)
        new_finals = set(c.execute(f'SELECT * FROM core.fact_game_result WHERE game_id IN {season_games}').fetchall())
        changed = [r for r in old_finals if r not in new_finals]
        if changed:
            fails.append(f'{len(changed)} previously admitted finals changed or disappeared, e.g. {changed[:3]}')
        after = {t: c.execute(f'SELECT count(*) FROM core.{t} WHERE game_id IN {season_games}').fetchone()[0] for t in GAME_TABLES}
        shrunk = [t for t in GAME_TABLES if after[t] < before[t]]
        if shrunk:
            fails.append(f'season row counts went down: {shrunk}')
        if fails:
            raise RuntimeError('integrity checks failed: ' + '; '.join(fails))
        c.execute('''CREATE TABLE IF NOT EXISTS audit.refresh_log (ran_at TIMESTAMPTZ, cutoff DATE, outcome VARCHAR,
            rows_before JSON, rows_after JSON, referee_rows INTEGER, note VARCHAR)''')
        c.execute("INSERT INTO audit.refresh_log VALUES (now(), ?, 'committed', ?, ?, ?, NULL)",
                  [cutoff, json.dumps(before), json.dumps(after), refs])
        c.execute('DETACH snap')
        c.execute('CHECKPOINT')
        c.close()
        os.replace(clone, wh)
        return {'before': before, 'after': after, 'referee_rows': refs}
    finally:
        clone.unlink(missing_ok=True)
        Path(str(clone) + '.wal').unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--warehouse', type=Path, default=DEFAULT_OUT)
    ap.add_argument('--cutoff', type=dt.date.fromisoformat, help='last completed Eastern date (default: yesterday ET)')
    ap.add_argument('--force', action='store_true', help='rebuild even if the warehouse already covers the cutoff (re-verifies it)')
    a = ap.parse_args()
    now = dt.datetime.now(ET)
    cutoff = a.cutoff or (now.date() - dt.timedelta(days=1))
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = REPORTS / f'{now:%Y-%m-%dT%H%M}-cutoff-{cutoff}.txt'
    c = connect(a.warehouse, read_only=True)
    done = c.execute(f'SELECT cutoff FROM audit."{PREFIX}scope"').fetchone()[0]
    c.close()
    injury_reports = {}
    pregame = None
    capture_errors = []
    try:
        injury_reports, injury_errors = capture_daily(now)
        capture_errors.extend(injury_errors)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        capture_errors.append(f'injury report capture failed: {e}')
    try:
        pregame = pregame_sources.capture(now)
    except (OSError, ValueError) as e:
        capture_errors.append(f'pregame schedule or roster capture failed: {e}')
    for error in capture_errors:
        print(error, file=sys.stderr)
    try:
        if injury_reports or pregame:
            with warehouse_lock(), connect(a.warehouse) as c:
                publish_injury_reports(c, injury_reports, pregame=pregame)
        print(f'pregame refresh: {len(injury_reports)} injury report PDFs available', flush=True)
        if done >= cutoff and not a.force:
            print(f'game data already current through {done}')
            return int(bool(capture_errors))
        # hold the writers' lock for the whole run: export, rebuild, clone, swap (gap chasing waits)
        with warehouse_lock(), tempfile.TemporaryDirectory(dir=ROOT / 'data', prefix='.refresh-') as w:
            work = Path(w)
            assets = capture(cutoff, now)
            hist, prior, prev = export_inputs(a.warehouse, work)
            snap = work / 'snapshot.duckdb'
            build_snapshot(assets, cutoff, hist, prior, prev, snap)
            result = merge(a.warehouse, snap, cutoff, dt.datetime.now(dt.timezone.utc), work)
        result['injury_reports'] = len(injury_reports)
        if capture_errors:
            result['capture_errors'] = capture_errors
        report.write_text(f'committed cutoff {cutoff}\n{json.dumps(result, indent=2)}\n')
        print(report.read_text())
        return int(bool(capture_errors))
    except NotReady as e:
        report.write_text(f'not ready: {e}\n')
        print(f'not ready: {e}')
        return NOT_READY
    except Exception:
        report.write_text(f'FAILED, completed-game release unchanged (cutoff {cutoff})\n\n{traceback.format_exc()}')
        print(report.read_text(), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
