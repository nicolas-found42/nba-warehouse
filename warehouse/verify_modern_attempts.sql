WITH expected_scope AS (
 SELECT g.game_id FROM nba.dim_game g ANTI JOIN (SELECT DISTINCT game_id FROM nba.fact_shot) s USING(game_id)
 WHERE (g.season_start,g.season_type) IN ((2024,'Regular'),(2025,'Playoffs'))
), expected(season_start,season_type,games,attempts) AS (VALUES (2024,'Regular',5,901),(2025,'Playoffs',33,5663)),
 counts AS (
 SELECT g.season_start,g.season_type,count(*) AS games,sum(a.candidate_attempts) AS attempts
 FROM audit.pbp_attempt_modern_scope s JOIN nba.dim_game g USING(game_id)
 JOIN audit.pbp_attempt_coverage a USING(game_id) WHERE a.disposition='published_box_reconciled_pbp'
 GROUP BY 1,2
), checks AS (
 SELECT 'modern_attempt_exact_chart_gap_scope' AS check_name,count(*) AS violations FROM (
  (SELECT * FROM expected_scope EXCEPT ALL SELECT * FROM audit.pbp_attempt_modern_scope)
  UNION ALL (SELECT * FROM audit.pbp_attempt_modern_scope EXCEPT ALL SELECT * FROM expected_scope)
 )
 UNION ALL SELECT 'modern_attempt_expected_cohorts_complete',count(*)
 FROM counts c FULL JOIN expected e USING(season_start,season_type)
 WHERE (c.games,c.attempts) IS DISTINCT FROM (e.games,e.attempts)
 UNION ALL SELECT 'modern_attempt_value_and_make_explicit',count(*)
 FROM nba.fact_pbp_field_goal_attempt p SEMI JOIN audit.pbp_attempt_modern_scope USING(game_id)
 WHERE p.evidence_kind<>'explicit_field_goal'
)
SELECT check_name,violations FROM checks ORDER BY check_name;
