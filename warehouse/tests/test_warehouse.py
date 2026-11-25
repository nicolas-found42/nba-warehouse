"""Tests for the guarantees the warehouse jobs rely on.

  uv run --no-project --with duckdb==2.0.0 --with pytest python -m pytest warehouse/tests -q
"""
from __future__ import annotations

import datetime as dt
import hashlib
import sys
from contextlib import nullcontext
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import coverage  # noqa: E402
import daily_refresh  # noqa: E402
from fetch_bref_periods import Fixture, parse_box  # noqa: E402

SEASON = daily_refresh.SEASON_YEAR
HOME, AWAY, OTHER = 1610612738, 1610612752, 1610612747

# ----------------------------------------------------------------------------------------- daily refresh
CORE_DDL = """
CREATE SCHEMA core; CREATE SCHEMA audit; CREATE SCHEMA raw;
CREATE TABLE core.dim_season (season_start SMALLINT PRIMARY KEY, season_year VARCHAR, league_era VARCHAR);
CREATE TABLE core.dim_team (team_id BIGINT PRIMARY KEY, display_name VARCHAR, abbreviation VARCHAR);
CREATE TABLE core.dim_person (person_id BIGINT PRIMARY KEY, display_name VARCHAR);
CREATE TABLE core.dim_game (game_id VARCHAR PRIMARY KEY, season_year VARCHAR, game_date DATE);
CREATE TABLE core.bridge_game_team (game_id VARCHAR, side VARCHAR, team_id BIGINT);
CREATE TABLE core.fact_game_result (game_id VARCHAR PRIMARY KEY, home_score INT, away_score INT, winner_team_id BIGINT);
CREATE TABLE core.fact_game_period_score (game_id VARCHAR, team_id BIGINT, period_number SMALLINT, points INT);
CREATE TABLE core.fact_team_game_stats (game_id VARCHAR, team_id BIGINT, pts INT);
CREATE TABLE core.fact_player_game_stats (game_id VARCHAR, person_id BIGINT, team_id BIGINT, points INT);
CREATE TABLE core.fact_play_event (game_id VARCHAR, action_number INT);
CREATE TABLE core.fact_shot (game_id VARCHAR, shot_key VARCHAR);
CREATE TABLE core.fact_regular_standings (season_year VARCHAR, team_id BIGINT, games INT, wins INT, losses INT,
    points_for INT, points_against INT, snapshot_kind VARCHAR, as_of_date DATE);
"""


def game_rows(prefix: str, gid: str, winner: int, home_score: int = 101, away_score: int = 99) -> list[str]:
    return [
        f"INSERT INTO {prefix}.dim_game VALUES ('{gid}', '{SEASON}', DATE '2026-11-20')",
        f"INSERT INTO {prefix}.bridge_game_team VALUES ('{gid}', 'home', {HOME}), ('{gid}', 'away', {AWAY})",
        f"INSERT INTO {prefix}.fact_game_result VALUES ('{gid}', {home_score}, {away_score}, {winner})",
        f"INSERT INTO {prefix}.fact_game_period_score VALUES ('{gid}', {HOME}, 1, {home_score}), ('{gid}', {AWAY}, 1, {away_score})",
        f"INSERT INTO {prefix}.fact_team_game_stats VALUES ('{gid}', {HOME}, {home_score}), ('{gid}', {AWAY}, {away_score})",
        f"INSERT INTO {prefix}.fact_player_game_stats VALUES ('{gid}', 1, {HOME}, 20)",
        f"INSERT INTO {prefix}.fact_play_event VALUES ('{gid}', 1)",
    ]


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    wh = tmp_path / 'wh.duckdb'
    c = duckdb.connect(str(wh))
    c.execute(CORE_DDL)
    c.execute(f"INSERT INTO core.dim_season VALUES (2026, '{SEASON}', 'NBA')")
    c.execute(f"INSERT INTO core.dim_team VALUES ({HOME}, 'Boston', 'BOS'), ({AWAY}, 'New York', 'NYK'), ({OTHER}, 'LA', 'LAL')")
    c.execute("INSERT INTO core.dim_person VALUES (1, 'Player One')")
    for s in game_rows('core', '0022600001', HOME):
        c.execute(s)
    c.execute(f"INSERT INTO core.fact_regular_standings VALUES ('{SEASON}', {HOME}, 1, 1, 0, 101, 99, 'as_of', DATE '2026-11-20')")
    c.execute(f'CREATE TABLE audit."{daily_refresh.PREFIX}scope" AS SELECT 2027 AS season_end, DATE \'2026-11-20\' AS cutoff')
    c.close()
    return wh


def snapshot(tmp_path: Path, winner_second_game: int, keep_first: bool = True) -> Path:
    snap = tmp_path / 'snap.duckdb'
    c = duckdb.connect(str(snap))
    c.execute(CORE_DDL.replace('core.', 'nba.').replace('SCHEMA core', 'SCHEMA nba').replace('CREATE SCHEMA raw;', ''))
    c.execute(f"INSERT INTO nba.dim_season VALUES (2026, '{SEASON}', 'NBA')")
    c.execute(f"INSERT INTO nba.dim_team VALUES ({HOME}, 'Boston', 'BOS'), ({AWAY}, 'New York', 'NYK')")
    c.execute("INSERT INTO nba.dim_person VALUES (1, 'Player One')")
    if keep_first:
        for s in game_rows('nba', '0022600001', HOME):
            c.execute(s)
    for s in game_rows('nba', '0022600002', winner_second_game):
        c.execute(s)
    c.execute('ALTER TABLE nba.fact_regular_standings DROP COLUMN snapshot_kind; ALTER TABLE nba.fact_regular_standings DROP COLUMN as_of_date')
    c.execute(f"INSERT INTO nba.fact_regular_standings VALUES ('{SEASON}', {HOME}, 2, 2, 0, 202, 198)")
    c.execute('CREATE TABLE audit.scope AS SELECT 2027 AS season_end, DATE \'2026-11-21\' AS cutoff')
    c.close()
    return snap


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(daily_refresh, 'referee_assignments', lambda c, t: 0)
    monkeypatch.setattr(daily_refresh, 'connect', lambda p, read_only=False: duckdb.connect(str(p), read_only=read_only))


def test_refresh_commits_a_valid_snapshot(warehouse: Path, tmp_path: Path) -> None:
    out = daily_refresh.merge(warehouse, snapshot(tmp_path, HOME), dt.date(2026, 11, 21), dt.datetime.now(dt.timezone.utc), tmp_path)
    assert out['after']['fact_game_result'] == 2
    c = duckdb.connect(str(warehouse), read_only=True)
    assert c.execute('SELECT count(*) FROM core.fact_game_result').fetchone()[0] == 2
    assert c.execute("SELECT DISTINCT as_of_date FROM core.fact_regular_standings").fetchall() == [(dt.date(2026, 11, 21),)]
    assert not (tmp_path / 'wh_refresh.duckdb').exists()


def test_refresh_rejects_bad_winner_and_leaves_file_unchanged(warehouse: Path, tmp_path: Path) -> None:
    before = digest(warehouse)
    with pytest.raises(RuntimeError, match='winner outside the fixture'):
        daily_refresh.merge(warehouse, snapshot(tmp_path, OTHER), dt.date(2026, 11, 21), dt.datetime.now(dt.timezone.utc), tmp_path)
    assert digest(warehouse) == before
    assert not (tmp_path / 'wh_refresh.duckdb').exists()


def test_refresh_rejects_a_disappearing_final(warehouse: Path, tmp_path: Path) -> None:
    before = digest(warehouse)
    with pytest.raises(RuntimeError, match='previously admitted finals changed'):
        daily_refresh.merge(warehouse, snapshot(tmp_path, HOME, keep_first=False), dt.date(2026, 11, 21),
                            dt.datetime.now(dt.timezone.utc), tmp_path)
    assert digest(warehouse) == before


def test_daily_pregame_capture_is_published_when_game_assets_are_late(warehouse, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['daily_refresh.py', '--warehouse', str(warehouse), '--cutoff', '2026-11-21'])
    monkeypatch.setattr(daily_refresh, 'ROOT', tmp_path)
    monkeypatch.setattr(daily_refresh, 'REPORTS', tmp_path / 'data' / 'reports')
    monkeypatch.setattr(daily_refresh, 'warehouse_lock', nullcontext)
    monkeypatch.setattr(daily_refresh, 'capture_daily', lambda now: {'report': object()})
    monkeypatch.setattr(daily_refresh.pregame_sources, 'capture', lambda now: None)

    def publish(c, reports, pregame=None):
        assert list(reports) == ['report']
        c.execute('CREATE TABLE raw.pregame_capture AS SELECT 1 AS captured')

    def late(cutoff):
        raise daily_refresh.NotReady('publisher assets do not yet cover the cutoff')

    monkeypatch.setattr(daily_refresh, 'publish_injury_reports', publish)
    monkeypatch.setattr(daily_refresh, 'capture', late)

    assert daily_refresh.main() == daily_refresh.NOT_READY
    with duckdb.connect(str(warehouse), read_only=True) as c:
        assert c.execute('SELECT captured FROM raw.pregame_capture').fetchone() == (1,)
        assert c.execute('SELECT count(*) FROM core.fact_game_result').fetchone() == (1,)


# ----------------------------------------------------------------------------------------- gap chasing
def box_html(path: str, day: dt.date, home: tuple[str, list[int]], away: tuple[str, list[int]]) -> bytes:
    def row(team: str, q: list[int]) -> str:
        cells = ''.join(f'<td data-stat="{i}">{v}</td>' for i, v in enumerate(q, 1))
        return f'<tr><th data-stat="team">{team}</th>{cells}<td data-stat="T">{sum(q)}</td></tr>'
    title = f'{day:%B} {day.day}, {day.year}'
    return (f'<html><head><link rel="canonical" href="https://www.basketball-reference.com{path}"></head>'
            f'<body><h1>Away at Home Box Score, {title}</h1>'
            f'<table id="line_score"><tbody>{row(*away)}{row(*home)}</tbody></table></body></html>').encode()


def fixture(home_final: int, away_final: int) -> Fixture:
    day = dt.date(1947, 1, 5)
    return Fixture('0024600099', day, HOME, AWAY, home_final, away_final, '/boxscores/194701050BOS.html', 'BOS', 'NYK')


def test_chase_admits_consistent_line_score() -> None:
    f = fixture(80, 70)
    res = parse_box(f, box_html(f.source_path, f.day, ('BOS', [20, 20, 20, 20]), ('NYK', [15, 20, 15, 20])))
    assert res['status'] == 'matched_fixture_exact_printed_periods'
    assert res['home_periods'] == [20, 20, 20, 20]


def test_chase_rejects_total_that_disagrees_with_final() -> None:
    f = fixture(81, 70)
    res = parse_box(f, box_html(f.source_path, f.day, ('BOS', [20, 20, 20, 20]), ('NYK', [15, 20, 15, 20])))
    assert res['status'] == 'final_disagrees'


def test_chase_rejects_overtime_without_a_tie() -> None:
    f = fixture(90, 85)
    res = parse_box(f, box_html(f.source_path, f.day, ('BOS', [20, 20, 20, 20, 10]), ('NYK', [20, 20, 20, 15, 10])))
    assert res['status'] == 'untied_before_overtime'


def test_chase_reads_named_overtime_cells_in_contiguous_order() -> None:
    f = fixture(94, 91)
    raw = box_html(f.source_path, f.day, ('BOS', [20, 20, 20, 20, 7, 7]), ('NYK', [20, 20, 20, 20, 7, 4]))
    raw = raw.replace(b'data-stat="5"', b'data-stat="1OT"').replace(b'data-stat="6"', b'data-stat="2OT"')
    res = parse_box(f, raw)
    assert res['status'] == 'matched_fixture_exact_printed_periods'
    assert res['home_periods'] == [20, 20, 20, 20, 7, 7]
    assert parse_box(f, raw.replace(b'data-stat="2OT"', b'data-stat="3OT"'))['status'] == 'invalid_line_score_shape'


def test_chase_rejects_other_fixture_on_same_page() -> None:
    f = fixture(80, 70)
    res = parse_box(f, box_html(f.source_path, f.day, ('PHW', [20, 20, 20, 20]), ('NYK', [15, 20, 15, 20])))
    assert res['status'] == 'designated_side_or_team_disagrees'


# ----------------------------------------------------------------------------------------- coverage rule
def test_coverage_gives_up_after_two_independent_sources(tmp_path: Path) -> None:
    c = duckdb.connect()
    c.execute(CORE_DDL)
    c.execute("ALTER TABLE core.dim_game ADD COLUMN season_start SMALLINT; ALTER TABLE core.dim_game ADD COLUMN season_type VARCHAR;"
              "ALTER TABLE core.dim_game ADD COLUMN result_status VARCHAR; ALTER TABLE core.dim_game ADD COLUMN league_era VARCHAR")
    c.execute('CREATE TABLE core.fact_starting_lineup (game_id VARCHAR, team_id BIGINT)')
    c.execute('CREATE TABLE core.fact_game_official (game_id VARCHAR)')
    for gid in ('A', 'B', 'C'):
        c.execute(f"INSERT INTO core.dim_game VALUES ('{gid}', '1950-51', DATE '1951-01-01', 1950, 'Regular', 'final', 'NBA')")
    c.execute("CREATE TABLE audit.period_availability_v26 (game_id VARCHAR, status VARCHAR)")
    c.execute("INSERT INTO audit.period_availability_v26 VALUES ('A', 'no_supported_source'), ('B', 'no_supported_source'), ('C', 'no_valid_candidate')")
    c.execute('CREATE TABLE audit.gap_attempt (domain VARCHAR, game_id VARCHAR, source VARCHAR, attempted_at TIMESTAMPTZ, outcome VARCHAR, note VARCHAR)')
    c.execute("INSERT INTO audit.gap_attempt VALUES ('period_scores', 'A', 'basketball_reference_box', now(), 'no_data', NULL)")
    tracked = ', '.join(f"('{k}', {v})" for k, v in coverage.TRACKED_FROM.items())
    c.execute(coverage.SQL.replace('{tracked}', tracked))
    status = dict(c.execute("SELECT game_id, status FROM audit.game_coverage WHERE domain = 'period_scores'").fetchall())
    assert status == {'A': 'source_unavailable', 'B': 'missing_recoverable', 'C': 'disputed'}
    pbp = dict(c.execute("SELECT game_id, status FROM audit.game_coverage WHERE domain = 'play_by_play'").fetchall())
    assert set(pbp.values()) == {'not_tracked'}
