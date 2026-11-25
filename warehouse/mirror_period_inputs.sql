-- Run with the unchanged parent attached as prior and the supplied snapshot as src.
CREATE TEMP TABLE mirror_period_scope AS
SELECT g.game_id,p.status AS prior_status,p.candidate_sources AS prior_candidates,p.valid_sources AS prior_valid
FROM prior.nba.fact_game g LEFT JOIN prior.audit.period_coverage p USING(game_id)
WHERE g.season_start IN (1975,1976) AND g.season_type='Regular';

CREATE TEMP TABLE mirror_period_source AS
SELECT m.game_id,m.team_id,m.opponent_team_id,m.game_date,m.season_year,m.game_type,m.is_home,m.pts,
       [m.q1_points,m.q2_points,m.q3_points,m.q4_points] AS regulation,
       [m.ot1_points,m.ot2_points] AS overtime_slots,m.ot_all_points
FROM src.main.stg_kaggle_team_box m
WHERE EXISTS (SELECT 1 FROM mirror_period_scope t WHERE t.game_id=m.game_id)
   OR (m.season_year IN ('1975-76','1976-77') AND m.game_type='Regular Season');
