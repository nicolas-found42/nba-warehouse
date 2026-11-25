-- One row per game/team/period, including overtime. Admit complete games only.
CREATE TABLE nba.fact_game_period_score (
    game_id VARCHAR,
    team_id BIGINT,
    period_number SMALLINT CHECK (period_number > 0),
    points INTEGER NOT NULL CHECK (points >= 0),
    PRIMARY KEY (game_id, team_id, period_number),
    FOREIGN KEY (game_id, team_id) REFERENCES nba.bridge_game_team(game_id, team_id)
);
CREATE TABLE audit.period_coverage (
    game_id VARCHAR PRIMARY KEY,
    status VARCHAR NOT NULL CHECK (status IN ('published', 'final_result_unavailable',
        'no_supported_source', 'no_valid_candidate', 'valid_sources_disagree')),
    candidate_sources INTEGER NOT NULL CHECK (candidate_sources >= 0),
    valid_sources INTEGER NOT NULL CHECK (valid_sources BETWEEN 0 AND candidate_sources),
    selected_source VARCHAR,
    CHECK ((status='published') = (selected_source IS NOT NULL))
);

CREATE TEMP TABLE period_rows AS
WITH line_records AS (
    -- Group identical payloads before unpivoting. Conflicting alternatives remain
    -- separate; duplicate team/period keys then invalidate this candidate.
    SELECT DISTINCT g.game_id, g.home_team_id AS h_id, g.away_team_id AS a_id,
        [try_cast(s.pts_qtr1_home AS INT), try_cast(s.pts_qtr2_home AS INT),
         try_cast(s.pts_qtr3_home AS INT), try_cast(s.pts_qtr4_home AS INT),
         s.pts_ot1_home, s.pts_ot2_home, s.pts_ot3_home, s.pts_ot4_home, s.pts_ot5_home,
         s.pts_ot6_home, s.pts_ot7_home, s.pts_ot8_home, s.pts_ot9_home, s.pts_ot10_home] AS hp,
        [try_cast(s.pts_qtr1_away AS INT), try_cast(s.pts_qtr2_away AS INT),
         try_cast(s.pts_qtr3_away AS INT), try_cast(s.pts_qtr4_away AS INT),
         s.pts_ot1_away, s.pts_ot2_away, s.pts_ot3_away, s.pts_ot4_away, s.pts_ot5_away,
         s.pts_ot6_away, s.pts_ot7_away, s.pts_ot8_away, s.pts_ot9_away, s.pts_ot10_away] AS ap
    FROM src.main.fact_game_line_score s JOIN nba.fact_game g ON s.normalized_game_id=g.game_id
    WHERE try_cast(s.team_id_home AS BIGINT)=g.home_team_id AND try_cast(s.team_id_away AS BIGINT)=g.away_team_id
          AND s.pts_home=g.home_score AND s.pts_away=g.away_score
), line_pairs AS (
    SELECT game_id, h_id, a_id, i AS period_number, hp[i] AS home_pts, ap[i] AS away_pts,
           max(i) FILTER (WHERE i<=4 OR coalesce(hp[i],0)<>0 OR coalesce(ap[i],0)<>0)
                OVER (PARTITION BY game_id, h_id, a_id, hp, ap) AS last_used
    FROM line_records, range(1,15) AS p(i)
), markers AS (
    SELECT game_id, period AS period_number,
           count(*) FILTER (WHERE kind='start') AS starts,
           count(*) FILTER (WHERE kind='end') AS ends,
           min(action_number) FILTER (WHERE kind='start') AS first_action,
           max(action_number) FILTER (WHERE kind='end') AS last_action,
           max(score_home) FILTER (WHERE kind='start') AS h_start,
           max(score_away) FILTER (WHERE kind='start') AS a_start,
           max(score_home) FILTER (WHERE kind='end') AS h_end,
           max(score_away) FILTER (WHERE kind='end') AS a_end,
           bool_and(coalesce(regexp_full_match(clock,'PT0+M0+([.]0+)?S'),false)) FILTER (WHERE kind='end') AS end_at_zero
    FROM (
        SELECT *, lower(trim(sub_type)) AS kind FROM src.main.fact_pbp_events
        WHERE lower(trim(action_type))='period' AND lower(trim(sub_type)) IN ('start','end')
    ) GROUP BY 1,2
), bounds AS (
    SELECT *, coalesce(starts=1 AND ends=1 AND first_action<last_action AND end_at_zero
               AND first_action > lag(last_action,1,-1) OVER w
               AND h_start=lag(h_end,1,0) OVER w AND a_start=lag(a_end,1,0) OVER w,false) AS valid_boundary
    FROM markers WINDOW w AS (PARTITION BY game_id ORDER BY period_number)
), pbp_sides AS (
    SELECT p.game_id, b.team_id, p.period_number,
          CASE WHEN p.valid_boundary THEN CASE b.side WHEN 'home' THEN p.h_end-p.h_start ELSE p.a_end-p.a_start END END AS points
    FROM bounds p JOIN nba.bridge_game_team b USING (game_id)
)
SELECT 'quarter_scores' AS source_name, game_id, team_id, period AS period_number, pts AS points
FROM src.main.fact_game_quarter_scores
UNION ALL
SELECT 'legacy_quarter_backup', game_id, team_id, period, pts
FROM src.main._bak_fact_game_quarter_scores
UNION ALL
SELECT 'line_score', game_id, h_id, period_number, home_pts FROM line_pairs WHERE period_number<=last_used
UNION ALL
SELECT 'line_score', game_id, a_id, period_number, away_pts FROM line_pairs WHERE period_number<=last_used
UNION ALL
SELECT 'pbp_boundaries', game_id, team_id, period_number, points FROM pbp_sides
UNION ALL
SELECT 'scorer_single_period',game_id,team_id,period_number,points FROM supplemental_period_scores;

CREATE TEMP TABLE period_choices AS
WITH sided AS (
    SELECT p.source_name, p.game_id, p.period_number,
           max(p.points) FILTER (WHERE t.side='home') AS hp,
           max(p.points) FILTER (WHERE t.side='away') AS ap,
           count(*) FILTER (WHERE t.side='home') AS homes, count(*) FILTER (WHERE t.side='away') AS aways,
           count(*) AS rows_in_period
    FROM period_rows p LEFT JOIN nba.bridge_game_team t ON (p.game_id,p.team_id)=(t.game_id,t.team_id)
    GROUP BY 1,2,3
), period_totals AS (
    SELECT *, sum(hp-ap) OVER (PARTITION BY source_name,game_id ORDER BY period_number) AS running_lead,
              max(period_number) OVER (PARTITION BY source_name,game_id) AS last_period
    FROM sided
), evaluated AS (
    SELECT s.game_id, s.source_name,
           list(struct_pack(h := s.hp, a := s.ap) ORDER BY s.period_number) AS score_vector,
           coalesce(min(s.period_number)=1
              AND CASE WHEN s.source_name='scorer_single_period' THEN max(s.period_number)=1 ELSE max(s.period_number)>=4 END
              AND count(*)=max(s.period_number)
              AND bool_and(s.homes=1 AND s.aways=1 AND s.rows_in_period=2
                           AND s.hp IS NOT NULL AND s.ap IS NOT NULL
                           AND s.hp>=0 AND s.ap>=0 AND s.hp+s.ap>0
                           AND (s.period_number<4 OR s.period_number=s.last_period OR s.running_lead=0))
              AND sum(s.hp)=max(r.home_score) AND sum(s.ap)=max(r.away_score), false) AS valid
    FROM period_totals s LEFT JOIN nba.fact_game_result r USING (game_id)
    GROUP BY 1,2
), rollup AS (
    SELECT game_id, count(*) AS candidates, count(*) FILTER (WHERE valid) AS valid_count,
           count(DISTINCT score_vector) FILTER (WHERE valid) AS distinct_valid,
           coalesce(max(source_name) FILTER (WHERE valid AND source_name='quarter_scores'),
                    max(source_name) FILTER (WHERE valid AND source_name='line_score'),
                    max(source_name) FILTER (WHERE valid AND source_name='pbp_boundaries'),
                    max(source_name) FILTER (WHERE valid)) AS preferred
    FROM evaluated GROUP BY game_id
)
SELECT g.game_id,
       CASE WHEN g.result_status<>'final' THEN 'final_result_unavailable'
            WHEN x.game_id IS NULL THEN 'no_supported_source'
            WHEN x.valid_count=0 THEN 'no_valid_candidate'
            WHEN x.distinct_valid>1 THEN 'valid_sources_disagree'
            ELSE 'published' END AS status,
       coalesce(x.candidates,0) AS candidates, coalesce(x.valid_count,0) AS valid_sources,
       CASE WHEN status='published' THEN x.preferred END AS selected_source
FROM nba.dim_game g LEFT JOIN rollup x USING (game_id);

INSERT INTO audit.period_coverage SELECT * FROM period_choices;
INSERT INTO nba.fact_game_period_score
SELECT p.game_id, p.team_id, p.period_number, p.points
FROM period_rows p JOIN period_choices c ON c.game_id=p.game_id AND c.selected_source=p.source_name;
DROP TABLE period_choices;
DROP TABLE period_rows;

CREATE VIEW nba.game_overtime AS
WITH periods AS (
    SELECT game_id, max(period_number) AS last_period FROM nba.fact_game_period_score GROUP BY game_id
)
SELECT r.game_id, p.last_period, p.last_period>4 AS is_overtime, r.is_overtime AS reported_is_overtime
FROM nba.fact_game_result r LEFT JOIN periods p USING (game_id);
