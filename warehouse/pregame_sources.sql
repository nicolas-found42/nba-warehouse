CREATE OR REPLACE VIEW core.nba_schedule_fixture AS
SELECT s.game_id, s.game_date, s.home_team_id, s.away_team_id, s.start_time AS tipoff_at,
       'nba_schedule_mirror' AS fixture_source, s.captured_at AS fixture_observed_at
FROM raw.nba_pregame_schedule s
JOIN core.dim_team h ON h.team_id = s.home_team_id JOIN core.dim_team a ON a.team_id = s.away_team_id
WHERE regexp_full_match(s.game_id, '[0-9]{10}') AND s.home_team_id <> s.away_team_id
  AND (s.start_time AT TIME ZONE 'America/New_York')::DATE = s.game_date
  AND s.status IN ('scheduled', 'in_progress', 'final')
QUALIFY count(*) OVER (PARTITION BY s.captured_at, s.game_id) = 1;

CREATE OR REPLACE VIEW core.pregame_fixture AS
SELECT game_id, game_date, home_team_id, away_team_id,
       game_datetime_est AT TIME ZONE 'America/New_York' AS tipoff_at,
       'core_game' AS fixture_source, NULL::TIMESTAMPTZ AS fixture_observed_at
FROM core.dim_game WHERE participant_status = 'resolved'
UNION ALL
SELECT s.* FROM core.nba_schedule_fixture s ANTI JOIN core.dim_game g USING (game_id)
WHERE s.fixture_observed_at = (SELECT max(captured_at) FROM raw.nba_pregame_schedule);

CREATE OR REPLACE MACRO core.pregame_fixture_at(fixture, known_at) AS TABLE
WITH current_capture AS (
    SELECT game_id, game_date, home_team_id, away_team_id, tipoff_at, fixture_source, fixture_observed_at
    FROM core.nba_schedule_fixture WHERE game_id = fixture AND fixture_observed_at = (
        SELECT max(captured_at) FROM raw.nba_pregame_schedule WHERE captured_at <= known_at)
), initial_release AS (
    SELECT s.game_id,s.game_date,s.home_team_id,s.away_team_id,s.start_time AS tipoff_at,
           'nba_initial_schedule_release' AS fixture_source,s.published_at AS fixture_observed_at
    FROM raw.nba_initial_schedule s WHERE s.game_id=fixture AND s.published_at<=known_at
      AND NOT EXISTS (SELECT 1 FROM raw.nba_pregame_schedule r WHERE r.game_id=fixture AND r.captured_at<=known_at)
    QUALIFY s.published_at = max(s.published_at) OVER () AND count(*) OVER (PARTITION BY s.published_at) = 1
)
SELECT * FROM current_capture
UNION ALL SELECT * FROM initial_release
UNION ALL
SELECT game_id, game_date, home_team_id, away_team_id,
       game_datetime_est AT TIME ZONE 'America/New_York', 'core_game', NULL::TIMESTAMPTZ
FROM core.dim_game WHERE game_id = fixture AND participant_status = 'resolved'
  AND game_id NOT IN (SELECT game_id FROM raw.nba_pregame_schedule WHERE game_id IS NOT NULL)
  AND game_id NOT IN (SELECT game_id FROM raw.nba_initial_schedule);

CREATE OR REPLACE MACRO core.roster_at(fixture, known_at) AS TABLE
WITH latest AS (
    SELECT r.* EXCLUDE (observed_at), p.available_at AS observed_at, r.observed_at AS capture_started_at, p.availability_basis
    FROM raw.nba_roster_capture r JOIN raw.nba_roster_publication p USING (source_url, sha256)
    JOIN core.pregame_fixture_at(fixture, known_at) g ON r.team_id IN (g.home_team_id, g.away_team_id)
    LEFT JOIN core.dim_game completed ON completed.game_id = g.game_id AND completed.result_status = 'final'
    WHERE g.game_id = fixture AND p.available_at <= known_at
      AND p.available_at < least(g.tipoff_at, completed.game_datetime_est AT TIME ZONE 'America/New_York')
      AND r.source_date BETWEEN g.game_date - INTERVAL 7 DAY AND g.game_date
    QUALIFY p.available_at = max(p.available_at) OVER (PARTITION BY r.team_id)
), conflicts AS (
    SELECT team_id, person_id FROM latest GROUP BY ALL HAVING count(*) > 1
)
SELECT r.*, p.person_id IS NOT NULL AS in_person_dimension
FROM latest r ANTI JOIN conflicts USING (team_id, person_id)
LEFT JOIN core.dim_person p USING (person_id);
