CREATE TABLE IF NOT EXISTS raw.sportsdataverse_nba_injury_archive (
    source_url VARCHAR, source_sha256 VARCHAR, source_row_number BIGINT, season BIGINT,
    report_datetime TIMESTAMP, game_date DATE, game_time VARCHAR, report_matchup VARCHAR,
    away_team_abbr VARCHAR, home_team_abbr VARCHAR, team VARCHAR, report_player_name VARCHAR,
    current_status VARCHAR, reason VARCHAR, category VARCHAR, previous_status VARCHAR,
    game_id VARCHAR, team_id BIGINT, person_id BIGINT, match_method VARCHAR
);

CREATE OR REPLACE MACRO core.injury_name_key(name) AS
    regexp_replace(lower(strip_accents(name)), '[^a-z0-9]', '', 'g');
CREATE OR REPLACE MACRO core.injury_printed_name_key(name) AS
    core.injury_name_key(trim(split_part(name, ',', 2)) || ' ' || trim(split_part(name, ',', 1)));
CREATE OR REPLACE MACRO core.injury_team_code(abbreviation) AS
    CASE abbreviation WHEN 'NJN' THEN 'BKN' WHEN 'NOH' THEN 'NOP' WHEN 'PHO' THEN 'PHX' WHEN 'UTH' THEN 'UTA'
        WHEN 'SAN' THEN 'SAS' ELSE abbreviation END;

CREATE OR REPLACE VIEW core.injury_report_team_name AS
SELECT team_id, lower(trim(display_name)) AS name FROM core.dim_team
UNION SELECT team_id, lower(trim(full_name)) FROM core.dim_team_version
UNION SELECT 1610612746, 'los angeles clippers'
UNION SELECT 1610612746, 'la clippers';

CREATE OR REPLACE MACRO core.injury_team_id(printed_name, home, away) AS (
    SELECT min(team_id) FROM core.injury_report_team_name t WHERE t.team_id IN (home, away)
        AND (t.name = lower(trim(printed_name)) OR (length(trim(printed_name)) >= 5 AND
             (starts_with(t.name, lower(trim(printed_name)) || ' ') OR ends_with(t.name, ' ' || lower(trim(printed_name))))))
    HAVING count(DISTINCT team_id) = 1
);

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

CREATE OR REPLACE VIEW audit.injury_report_archive_input AS
WITH reports AS (
    SELECT a.*, report_datetime AT TIME ZONE 'America/New_York' AS report_time,
           CASE
               WHEN report_player_name IS NULL AND upper(coalesce(current_status, reason)) IN
                   ('NOT YET SUBMITTED', 'ALL PLAYERS AVAILABLE') THEN upper(coalesce(current_status, reason))
               WHEN contains(report_player_name, ',') AND current_status IN
                   ('Out', 'Doubtful', 'Questionable', 'Probable', 'Available') THEN current_status
           END AS status
    FROM audit.injury_report_archive_copy a
    WHERE original_pdf_status = 'original_missing'
    QUALIFY count(DISTINCT source_sha256) OVER (PARTITION BY report_datetime) = 1
), complete_teams AS (
    SELECT report_datetime, game_date, report_matchup, team FROM reports GROUP BY ALL
    HAVING count(status) = count(*) AND count(DISTINCT game_id) <= 1 AND count(DISTINCT team_id) <= 1
       AND ((count(*) = 1 AND count(report_player_name) = 0)
            OR count(DISTINCT core.injury_printed_name_key(report_player_name)) = count(*))
)
SELECT r.* FROM reports r JOIN complete_teams USING (report_datetime, game_date, report_matchup, team)
WHERE timezone('America/New_York', report_time) = report_datetime
  AND timezone('America/New_York', report_time - INTERVAL 1 HOUR) <> report_datetime
  AND timezone('America/New_York', report_time + INTERVAL 1 HOUR) <> report_datetime;

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

CREATE OR REPLACE VIEW audit.injury_report_document AS
WITH receipts AS (
    SELECT source_url, sha256, min(CASE WHEN source_url = retrieval_url THEN 0 ELSE 1 END) AS source_priority
    FROM raw.nba_injury_report_retrieval GROUP BY ALL
), documents AS (
    SELECT DISTINCT p.source_url, p.sha256, p.report_time, coalesce(r.source_priority, 0) AS source_priority
    FROM raw.nba_injury_report p LEFT JOIN receipts r USING (source_url, sha256)
), source_choice AS (
    SELECT *, min(source_priority) OVER (PARTITION BY report_time) AS best_priority FROM documents
), digest_choice AS (
    SELECT report_time, count(DISTINCT sha256) AS digests, min(source_url) AS preferred_url
    FROM source_choice WHERE source_priority = best_priority GROUP BY report_time
)
SELECT d.*, CASE
    WHEN d.source_priority <> d.best_priority THEN 'lower_source_priority'
    WHEN c.digests > 1 THEN 'conflicting_document'
    WHEN d.source_url <> c.preferred_url THEN 'duplicate_document'
    ELSE 'preferred' END AS disposition
FROM source_choice d JOIN digest_choice c USING (report_time);

CREATE OR REPLACE VIEW audit.injury_report_resolution AS
WITH games AS (
    SELECT g.game_id, g.game_date, g.home_team_id, g.away_team_id,
           core.injury_team_code(a.abbreviation) || '@' || core.injury_team_code(h.abbreviation) AS matchup,
           count(*) OVER (PARTITION BY g.game_date, core.injury_team_code(a.abbreviation), core.injury_team_code(h.abbreviation)) AS fixtures
    FROM core.pregame_fixture g JOIN core.dim_team a ON a.team_id = g.away_team_id
    JOIN core.dim_team h ON h.team_id = g.home_team_id
), pdf_guesses AS (
    SELECT p.*, g.game_id, core.injury_team_id(p.team_name, g.home_team_id, g.away_team_id) AS team_id,
           core.injury_printed_name_key(p.player_name) AS person_name_key
    FROM raw.nba_injury_report p
    LEFT JOIN games g ON g.game_date = p.game_date AND g.fixtures = 1 AND g.matchup =
        core.injury_team_code(split_part(p.matchup, '@', 1)) || '@' || core.injury_team_code(split_part(p.matchup, '@', 2))
), names AS (
    SELECT person_id, core.injury_name_key(display_name) AS name_key
    FROM core.dim_person WHERE display_name IS NOT NULL
), name_match AS (
    SELECT name_key, min(person_id) AS person_id FROM names GROUP BY name_key HAVING count(*) = 1
), official_game_people AS (
    SELECT game_id, team_id, person_id FROM core.fact_player_game_stats
    UNION SELECT game_id, team_id, person_id FROM core.fact_inactive_player
), game_name_match AS (
    SELECT b.game_id, b.team_id, n.name_key, min(n.person_id) AS person_id
    FROM official_game_people b JOIN names n USING (person_id)
    WHERE b.game_id IN (SELECT game_id FROM pdf_guesses WHERE game_id IS NOT NULL)
    GROUP BY ALL HAVING count(DISTINCT n.person_id) = 1
), pdf AS (
    SELECT 'nba_official_pdf' AS source, p.source_url, p.sha256, p.row_number, p.report_time, p.game_date, p.game_id, p.team_id,
           p.player_name, p.team_name, p.matchup, NULL::VARCHAR AS source_player_id,
           coalesce(b.person_id, n.person_id) AS person_id, p.status, p.reason,
           CASE WHEN p.game_id IS NULL THEN 'game_unresolved' WHEN p.team_id IS NULL THEN 'team_unresolved'
                WHEN p.player_name IS NULL THEN 'team_notice'
                WHEN coalesce(b.person_id, n.person_id) IS NULL THEN 'player_unresolved' ELSE 'linked' END AS resolution_status
    FROM pdf_guesses p LEFT JOIN name_match n ON n.name_key = p.person_name_key
    LEFT JOIN game_name_match b ON b.game_id = p.game_id AND b.team_id = p.team_id AND b.name_key = p.person_name_key
), mirror AS (
    SELECT 'llimllib_nba_injury' AS source, m.source_url, NULL::VARCHAR AS sha256, NULL::INTEGER AS row_number,
           m.report_time, m.game_date, g.game_id, try_cast(m.team_id AS BIGINT) AS team_id, NULL::VARCHAR AS player_name,
           NULL::VARCHAR AS team_name, NULL::VARCHAR AS matchup, m.player_id AS source_player_id, b.person_id,
           m.status, concat_ws(' ', nullif(m.reason, ''), nullif(m.detail, '')) AS reason,
           CASE WHEN g.game_id IS NULL THEN 'game_unresolved'
                WHEN try_cast(m.team_id AS BIGINT) IS NULL OR try_cast(m.team_id AS BIGINT) NOT IN (g.home_team_id, g.away_team_id)
                    THEN 'team_unresolved'
                WHEN m.status IN ('NOT YET SUBMITTED', 'ALL PLAYERS AVAILABLE') THEN 'team_notice'
                WHEN b.person_id IS NULL THEN 'player_unresolved' ELSE 'linked' END AS resolution_status
    FROM raw.nba_injury_report_mirror m
    LEFT JOIN core.pregame_fixture g ON g.game_id = lpad(m.game_id, 10, '0') AND g.game_date = m.game_date
    LEFT JOIN official_game_people b
      ON b.game_id = g.game_id AND b.team_id = try_cast(m.team_id AS BIGINT) AND b.person_id = try_cast(m.player_id AS BIGINT)
), archive AS (
    SELECT 'sportsdataverse' AS source, a.source_url, a.source_sha256 AS sha256,
           a.source_row_number::INTEGER AS row_number, a.report_time, a.game_date, r.resolved_game_id AS game_id,
           r.resolved_team_id AS team_id, a.report_player_name AS player_name, a.team AS team_name,
           a.report_matchup AS matchup, a.person_id::VARCHAR AS source_player_id, r.resolved_person_id AS person_id,
           a.status, concat_ws(' - ', nullif(a.category, ''), nullif(a.reason, a.status)) AS reason, r.resolution_status
    FROM audit.injury_report_archive_input a
    JOIN audit.injury_report_archive_resolution r USING (source_sha256, source_row_number)
    QUALIFY r.resolved_team_id IS NULL OR count(DISTINCT a.team) OVER
        (PARTITION BY r.resolved_game_id, r.resolved_team_id, a.report_time) = 1
)
SELECT * FROM pdf UNION ALL SELECT * FROM mirror UNION ALL SELECT * FROM archive;

CREATE OR REPLACE VIEW core.injury_report_snapshot AS
SELECT i.source, i.source_url, i.sha256, i.row_number, i.source_player_id, i.report_time, i.game_date, i.game_id,
       i.team_id, i.player_name, i.person_id, i.status, i.reason, i.resolution_status, g.tipoff_at
FROM audit.injury_report_resolution i JOIN core.pregame_fixture g USING (game_id)
LEFT JOIN audit.injury_report_document d USING (source_url, sha256, report_time)
WHERE i.resolution_status NOT IN ('game_unresolved', 'team_unresolved')
  AND ((i.source = 'nba_official_pdf' AND d.disposition = 'preferred') OR
      (i.source <> 'nba_official_pdf' AND NOT EXISTS (
          SELECT 1 FROM audit.injury_report_resolution p JOIN audit.injury_report_document pd USING (source_url,sha256,report_time)
          WHERE p.source = 'nba_official_pdf' AND p.game_id = i.game_id AND p.team_id = i.team_id
            AND p.report_time = i.report_time AND pd.disposition IN ('preferred', 'conflicting_document'))))
  AND (i.source <> 'sportsdataverse' OR NOT EXISTS (
        SELECT 1 FROM raw.nba_injury_report_mirror m
        WHERE lpad(m.game_id, 10, '0') = i.game_id AND m.game_date = i.game_date
          AND try_cast(m.team_id AS BIGINT) = i.team_id AND m.report_time = i.report_time))
GROUP BY ALL;

CREATE OR REPLACE MACRO core.injury_report_at(fixture, known_at) AS TABLE
WITH choice AS (
    SELECT f.* REPLACE (least(f.tipoff_at, completed.game_datetime_est AT TIME ZONE 'America/New_York') AS tipoff_at),
           core.injury_team_code(away.abbreviation) || '@' || core.injury_team_code(home.abbreviation) AS matchup
    FROM core.pregame_fixture_at(fixture, known_at) f
    JOIN core.dim_team away ON away.team_id = f.away_team_id JOIN core.dim_team home ON home.team_id = f.home_team_id
    LEFT JOIN core.dim_game completed ON completed.game_id = f.game_id AND completed.result_status = 'final'
    WHERE f.tipoff_at IS NOT NULL
), roster AS (
    SELECT team_id, core.injury_name_key(player_name) AS name_key, min(person_id) AS person_id
    FROM core.roster_at(fixture, known_at) GROUP BY ALL HAVING count(DISTINCT person_id) = 1
), pdf_rows AS (
    SELECT 'nba_official_pdf' AS source, p.source_url, p.sha256, p.row_number, NULL::VARCHAR AS source_player_id,
           p.report_time, p.game_date, g.game_id, core.injury_team_id(p.team_name, g.home_team_id, g.away_team_id) AS team_id,
           p.player_name, p.status, p.reason, g.tipoff_at, d.disposition
    FROM choice g JOIN raw.nba_injury_report p ON p.game_date = g.game_date
        AND g.matchup = core.injury_team_code(split_part(p.matchup, '@', 1)) || '@' || core.injury_team_code(split_part(p.matchup, '@', 2))
    JOIN audit.injury_report_document d USING (source_url, sha256, report_time)
    WHERE p.report_time <= known_at AND p.report_time < g.tipoff_at
), official AS (
    SELECT p.* EXCLUDE (tipoff_at, disposition), r.person_id, p.tipoff_at,
           CASE WHEN p.player_name IS NULL THEN 'team_notice' WHEN r.person_id IS NULL THEN 'player_unresolved' ELSE 'linked' END AS resolution_status
    FROM pdf_rows p LEFT JOIN roster r ON r.team_id = p.team_id AND r.name_key = core.injury_printed_name_key(p.player_name)
    WHERE p.team_id IS NOT NULL AND p.disposition = 'preferred'
), mirror AS (
    SELECT 'llimllib_nba_injury', m.source_url, NULL::VARCHAR, NULL::INTEGER, m.player_id, m.report_time, m.game_date,
           g.game_id, try_cast(m.team_id AS BIGINT), NULL::VARCHAR, m.status, concat_ws(' ', nullif(m.reason, ''), nullif(m.detail, '')),
           r.person_id, g.tipoff_at,
           CASE WHEN m.status IN ('NOT YET SUBMITTED', 'ALL PLAYERS AVAILABLE') THEN 'team_notice'
                WHEN r.person_id IS NULL THEN 'player_unresolved' ELSE 'linked' END
    FROM choice g JOIN raw.nba_injury_report_mirror m ON lpad(m.game_id, 10, '0') = g.game_id AND m.game_date = g.game_date
    LEFT JOIN (SELECT DISTINCT team_id, person_id FROM roster) r
        ON r.team_id = try_cast(m.team_id AS BIGINT) AND r.person_id = try_cast(m.player_id AS BIGINT)
    WHERE try_cast(m.team_id AS BIGINT) IN (g.home_team_id, g.away_team_id)
      AND m.report_time <= known_at AND m.report_time < g.tipoff_at
      AND NOT EXISTS (SELECT 1 FROM pdf_rows p WHERE p.team_id = try_cast(m.team_id AS BIGINT)
                      AND p.report_time = m.report_time AND p.disposition IN ('preferred', 'conflicting_document'))
), archive_fixture_candidates AS (
    SELECT game_id, game_date, away_team_id, home_team_id FROM core.pregame_fixture
    UNION ALL
    SELECT i.game_id, i.game_date, i.away_team_id, i.home_team_id FROM raw.nba_initial_schedule i
    WHERE i.published_at <= known_at AND NOT EXISTS (
        SELECT 1 FROM raw.nba_pregame_schedule s WHERE s.game_id = i.game_id AND s.captured_at <= known_at)
    UNION ALL
    SELECT game_id, game_date, away_team_id, home_team_id FROM core.nba_schedule_fixture
    WHERE fixture_observed_at = (SELECT max(captured_at) FROM raw.nba_pregame_schedule WHERE captured_at <= known_at)
), archive_rows AS (
    SELECT a.*, g.game_id AS fixture_id, g.tipoff_at,
           core.injury_team_id(a.team, g.home_team_id, g.away_team_id) AS fixture_team_id
    FROM choice g JOIN audit.injury_report_archive_input a ON a.game_date = g.game_date
        AND g.matchup = core.injury_team_code(split_part(a.report_matchup, '@', 1)) || '@' ||
            core.injury_team_code(split_part(a.report_matchup, '@', 2))
        AND (a.game_id IS NULL OR lpad(a.game_id, 10, '0') = g.game_id)
        AND NOT EXISTS (SELECT 1 FROM archive_fixture_candidates other
            WHERE other.game_date = g.game_date AND other.away_team_id = g.away_team_id
              AND other.home_team_id = g.home_team_id AND other.game_id <> g.game_id)
    WHERE a.report_time <= known_at AND a.report_time < g.tipoff_at
    QUALIFY count(DISTINCT a.team) OVER (PARTITION BY g.game_id, fixture_team_id, a.report_time) = 1
), archive AS (
    SELECT 'sportsdataverse', a.source_url, a.source_sha256, a.source_row_number::INTEGER, a.person_id::VARCHAR,
           a.report_time, a.game_date, a.fixture_id, a.fixture_team_id, a.report_player_name, a.status,
           concat_ws(' - ', nullif(a.category, ''), nullif(a.reason, a.status)), r.person_id, a.tipoff_at,
           CASE WHEN a.report_player_name IS NULL THEN 'team_notice'
                WHEN r.person_id IS NULL THEN 'player_unresolved' ELSE 'linked' END
    FROM archive_rows a LEFT JOIN roster r ON r.team_id = a.fixture_team_id
          AND r.name_key = core.injury_printed_name_key(a.report_player_name)
          AND (a.person_id IS NULL OR a.person_id = r.person_id)
    WHERE a.fixture_team_id IS NOT NULL AND (a.team_id IS NULL OR a.team_id = a.fixture_team_id)
      AND NOT EXISTS (SELECT 1 FROM raw.nba_injury_report_mirror m
        WHERE lpad(m.game_id, 10, '0') = a.fixture_id AND m.game_date = a.game_date
          AND try_cast(m.team_id AS BIGINT) = a.fixture_team_id AND m.report_time = a.report_time)
), reports AS (
    SELECT * FROM official UNION ALL SELECT * FROM mirror UNION ALL SELECT * FROM archive
)
SELECT DISTINCT * FROM reports
QUALIFY report_time = max(report_time) OVER (PARTITION BY game_id, team_id);

CREATE OR REPLACE VIEW core.injury_report_final_pregame AS
WITH latest AS (
    SELECT * FROM core.injury_report_snapshot WHERE report_time < tipoff_at
    QUALIFY report_time = max(report_time) OVER (PARTITION BY game_id, team_id)
)
SELECT i.*, p.participation_status, CASE WHEN o.person_id IS NOT NULL THEN true END AS official_inactive
FROM latest i LEFT JOIN core.fact_player_game_stats p USING (game_id, team_id, person_id)
LEFT JOIN core.fact_inactive_player o USING (game_id, team_id, person_id);
