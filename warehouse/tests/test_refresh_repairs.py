"""Regression checks for the November 25 release blockers."""
from datetime import date, datetime, timezone

import duckdb
import pytest

from refresh_current_season import (
    CutoffNotReady,
    Input,
    PLAYER_SIDE_CHECK,
    check,
    freshness_date,
    label_periods,
    stage_game_crosswalk,
    verify_fixture_schedule,
)


def test_fixture_fallback_requires_one_exact_nba_mapping():
    with duckdb.connect() as c:
        c.execute("""
            CREATE TEMP TABLE config AS SELECT 2027 AS season_end, DATE '2026-11-25' AS cutoff;
            CREATE TEMP TABLE source_game_map AS
                SELECT 2027 AS season, '401909324' AS espn_game_id, NULL::VARCHAR AS nba_game_id,
                DATE '2026-11-25' AS game_date, 9 AS home_espn_team_id, 23 AS away_espn_team_id,
                NULL::VARCHAR AS nba_home_team_id, NULL::VARCHAR AS nba_away_team_id, 'espn_only' AS match_method;
            CREATE TEMP TABLE source_schedule AS
                SELECT 2027 AS season, 401909324 AS id, DATE '2026-11-25' AS game_date, 9 AS home_id, 23 AS away_id,
                '2026-11-26T03:00:00Z' AS date, FALSE AS neutral_site, TRUE AS status_type_completed,
                129 AS home_score, 110 AS away_score, '' AS home_linescores, '' AS away_linescores, 4 AS status_period;
            CREATE TEMP TABLE source_team_map AS SELECT * FROM (VALUES (9,'1610612744'),(23,'1610612758'))
                AS t(espn_team_id,nba_team_id);
            CREATE TEMP TABLE source_nba_fixture_schedule AS
                SELECT 2027 AS season, '0022600062' AS game_id, DATE '2026-11-25' AS game_date,
                TIMESTAMPTZ '2026-11-26 03:00:00+00' AS start_time, '1610612744' AS home_team_id,
                '1610612758' AS away_team_id, FALSE AS neutral_site, 'final' AS status;
        """)
        stage_game_crosswalk(c, False)
        with pytest.raises(CutoffNotReady, match='no unambiguous canonical game crosswalk'):
            label_periods(c)
        c.execute('DROP TABLE game_crosswalk')
        stage_game_crosswalk(c, True)
        assert c.execute('SELECT nba_game_id, match_method FROM game_crosswalk').fetchone() == (
            '0022600062', 'nba_schedule_exact')
        c.execute("""CREATE TEMP TABLE candidate AS SELECT nba_game_id AS gid, game_date,
                  nba_home_team_id::BIGINT AS hid, nba_away_team_id::BIGINT AS aid FROM game_crosswalk""")
        verify_fixture_schedule(c)
        c.execute("""DROP TABLE game_crosswalk; DROP TABLE official_fixtures;
            INSERT INTO source_nba_fixture_schedule
            SELECT * REPLACE ('0022600063' AS game_id) FROM source_nba_fixture_schedule""")
        stage_game_crosswalk(c, True)
        with pytest.raises(CutoffNotReady):
            label_periods(c)


def test_empty_day_freshness_requires_a_post_close_schedule_covering_the_date(tmp_path):
    path = tmp_path / 'schedule.parquet'
    with duckdb.connect() as c:
        c.execute("""COPY (SELECT * FROM (VALUES (2027,'0022600062', DATE '2026-11-25'),
                                              (2027,'0022600070', DATE '2026-11-27'))
                           AS t(season,game_id,game_date)) TO ? (FORMAT PARQUET)""", [str(path)])
    cutoff = date(2026, 11, 26)
    now = datetime(2026, 11, 27, 15, tzinfo=timezone.utc)
    recent = Input('nba_fixture_schedule', path, 'fixture', 'a' * 64, None, '2026-11-27T08:00:00Z')
    early = Input('nba_fixture_schedule', path, 'fixture', 'a' * 64, None, '2026-11-27T04:59:59Z')
    assert freshness_date([recent], cutoff, 2027, now) == date(2026, 11, 25)
    assert freshness_date([early], cutoff, 2027, now) == cutoff
    with pytest.raises(CutoffNotReady, match='not closed'):
        freshness_date([recent], date(2026, 11, 27), 2027, now)


def test_blank_player_line_is_held_only_without_participation_evidence():
    with duckdb.connect() as c:
        c.execute("""
            CREATE TEMP TABLE player_obs AS SELECT '0022600062' AS gid, 401909324 AS game_id,
                4702384 AS athlete_id, 1610612758 AS nba_team, FALSE AS did_not_play, DATE '2026-11-25' AS game_date,
                DATE '2026-11-25' AS canonical_date, 2 AS season_type, 2 AS canonical_phase,
                'away' AS home_away, 'away' AS side, 110 AS team_score, 110 AS score,
                129 AS opponent_team_score, 129 AS opponent_score, FALSE AS team_winner,
                123 AS person, 'Example' AS espn_name, 'Example' AS athlete_display_name,
                NULL::INT AS minutes, FALSE AS starter, NULL::VARCHAR AS reason,
                0 AS points, 0 AS field_goals_made, 0 AS field_goals_attempted,
                0 AS three_point_field_goals_made, 0 AS three_point_field_goals_attempted,
                0 AS free_throws_made, 0 AS free_throws_attempted, 0 AS offensive_rebounds,
                0 AS defensive_rebounds, 0 AS rebounds, 0 AS assists, 0 AS steals, 0 AS blocks,
                0 AS turnovers, 0 AS fouls, NULL::INTEGER AS plus_minus;
            CREATE TEMP TABLE play_obs(game_id BIGINT, athlete_id_1 INT, athlete_id_2 INT, athlete_id_3 INT);
        """)
        check(c, 'player side', PLAYER_SIDE_CHECK, game_key=True)
        c.execute('INSERT INTO play_obs VALUES (401909324, NULL, 4702384, NULL)')
        with pytest.raises(ValueError, match='missing_played_minutes'):
            check(c, 'player side', PLAYER_SIDE_CHECK, game_key=True)
        c.execute('DELETE FROM play_obs; UPDATE player_obs SET field_goals_attempted=1')
        with pytest.raises(ValueError, match='missing_played_minutes'):
            check(c, 'player side', PLAYER_SIDE_CHECK, game_key=True)
