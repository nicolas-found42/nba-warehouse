CREATE OR REPLACE VIEW audit.injury_report_archive_copy AS
WITH originals AS (
    SELECT DISTINCT timezone('America/New_York', report_time) AS report_datetime
    FROM raw.nba_injury_report
)
SELECT a.*,
       CASE WHEN a.report_datetime >= TIMESTAMP '2025-12-22 09:00:00'
             AND minute(a.report_datetime) % 15 = 0 AND second(a.report_datetime) = 0
            THEN 'Injury-Report_' || strftime(a.report_datetime, '%Y-%m-%d_%I_%M%p') || '.pdf' END
       AS quarter_hour_filename_candidate,
       CASE WHEN o.report_datetime IS NULL THEN 'original_missing' ELSE 'original_present' END AS original_pdf_status
FROM raw.sportsdataverse_nba_injury_archive a LEFT JOIN originals o USING (report_datetime);

CREATE OR REPLACE VIEW audit.injury_report_archive_resolution AS
WITH fixtures AS (
    SELECT f.game_id, f.game_date, f.home_team_id, f.away_team_id,
           core.injury_team_code(a.abbreviation) || '@' || core.injury_team_code(h.abbreviation) AS matchup
    FROM core.pregame_fixture f
    JOIN core.dim_team a ON a.team_id = f.away_team_id JOIN core.dim_team h ON h.team_id = f.home_team_id
    QUALIFY count(*) OVER (PARTITION BY f.game_date, matchup) = 1
), games AS (
    SELECT s.*, g.game_id AS resolved_game_id,
           core.injury_team_id(s.team, g.home_team_id, g.away_team_id) AS matching_team_id
    FROM audit.injury_report_archive_copy s
    LEFT JOIN fixtures g ON g.game_date = s.game_date AND g.matchup =
        core.injury_team_code(split_part(s.report_matchup, '@', 1)) || '@' ||
        core.injury_team_code(split_part(s.report_matchup, '@', 2))
        AND (s.game_id IS NULL OR lpad(s.game_id, 10, '0') = g.game_id)
    WHERE s.original_pdf_status = 'original_missing'
), teams AS (
    SELECT * EXCLUDE (matching_team_id),
           CASE WHEN team_id IS NULL OR team_id = matching_team_id THEN matching_team_id END AS resolved_team_id
    FROM games
), official_people AS (
    SELECT game_id, team_id, person_id FROM core.fact_player_game_stats
    UNION
    SELECT game_id, team_id, person_id FROM core.fact_inactive_player
), named_people AS (
    SELECT p.game_id, p.team_id, core.injury_name_key(d.display_name) AS name_key, min(p.person_id) AS person_id
    FROM official_people p JOIN core.dim_person d USING (person_id)
    WHERE p.game_id IN (SELECT resolved_game_id FROM teams)
    GROUP BY ALL HAVING count(DISTINCT p.person_id) = 1
), people AS (
    SELECT t.*, CASE WHEN t.person_id IS NULL OR t.person_id = p.person_id THEN p.person_id END AS resolved_person_id
    FROM teams t
    LEFT JOIN named_people p ON p.game_id = t.resolved_game_id AND p.team_id = t.resolved_team_id
        AND p.name_key = core.injury_printed_name_key(t.report_player_name)
)
SELECT *, CASE
    WHEN resolved_game_id IS NULL THEN 'game_unresolved'
    WHEN resolved_team_id IS NULL THEN 'team_unresolved'
    WHEN report_player_name IS NULL AND coalesce(upper(current_status), upper(reason))
         IN ('NOT YET SUBMITTED', 'ALL PLAYERS AVAILABLE') THEN 'team_notice'
    WHEN resolved_person_id IS NULL THEN 'player_unresolved'
    ELSE 'linked' END AS resolution_status
FROM people;

CREATE OR REPLACE VIEW audit.injury_report_summer_fixture AS
WITH games AS (
    SELECT game_id, game_date, away_abbr || '@' || home_abbr AS matchup, source_url, source_sha256, game_url
    FROM raw.nba_summer_league_injury_schedule
    QUALIFY count(*) OVER (PARTITION BY game_date, matchup) = 1
), injury_rows AS (
    SELECT 'nba_official_pdf' AS report_source, source_url, sha256 AS source_sha256, row_number AS source_row_number,
           report_time, game_date, matchup, team_name, player_name, status
    FROM raw.nba_injury_report
    WHERE game_date BETWEEN (SELECT min(game_date) FROM games) AND (SELECT max(game_date) FROM games)
    UNION ALL
    SELECT 'sportsdataverse', source_url, source_sha256, source_row_number,
           report_datetime AT TIME ZONE 'America/New_York', game_date, report_matchup, team, report_player_name,
           coalesce(current_status, reason)
    FROM audit.injury_report_archive_copy
    WHERE original_pdf_status = 'original_missing'
      AND game_date BETWEEN (SELECT min(game_date) FROM games) AND (SELECT max(game_date) FROM games)
)
SELECT r.*, g.game_id AS official_game_id, g.game_url AS official_game_url, g.source_url AS schedule_url,
       g.source_sha256 AS schedule_capture_sha256
FROM injury_rows r JOIN games g ON g.game_date = r.game_date AND g.matchup =
    core.injury_team_code(split_part(r.matchup, '@', 1)) || '@' ||
    core.injury_team_code(split_part(r.matchup, '@', 2));
