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
