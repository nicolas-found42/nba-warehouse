WITH expected AS (
 SELECT c.* EXCLUDE(valid) FROM audit.pbp_attempt_candidate c JOIN audit.pbp_attempt_coverage d USING(game_id)
 WHERE c.valid AND d.disposition='published_box_reconciled_pbp'
 UNION ALL SELECT * FROM verify_legacy_attempt_expected
), gap_keys AS (
 SELECT g.game_id FROM nba.dim_game g JOIN verify_pbp_attempt_cohorts c USING(season_start,season_type)
 ANTI JOIN (SELECT DISTINCT game_id FROM nba.fact_shot) s USING(game_id)
), public_attempts AS (
 SELECT * FROM nba.field_goal_attempt WHERE attempt_source='nba_play_by_play'
), expected_public AS (
 SELECT unhex(sha256('nba_play_by_play:'||game_id||':'||action_number::VARCHAR)) AS attempt_key,
        game_id,team_id,person_id,period,seconds_remaining,clock_iso,shot_value,made,'nba_play_by_play' AS attempt_source,
        action_number,source_action,'unavailable' AS geometry_status,NULL::SMALLINT AS distance_feet,
        NULL::SMALLINT AS x_tenths_of_foot,NULL::SMALLINT AS y_tenths_of_foot
 FROM expected
), coverage_counts AS (
 SELECT game_id,count(*) AS n FROM nba.fact_pbp_field_goal_attempt GROUP BY 1
), checks AS (
 SELECT 'pbp_attempt_exact_gap_keys' AS check_name,count(*) AS violations FROM (
  (SELECT * FROM gap_keys EXCEPT ALL SELECT * FROM audit.pbp_attempt_scope)
  UNION ALL (SELECT * FROM audit.pbp_attempt_scope EXCEPT ALL SELECT * FROM gap_keys)
 )
 UNION ALL SELECT 'pbp_attempts_exact_event_cells',count(*) FROM (
  (SELECT * FROM expected EXCEPT ALL SELECT * FROM nba.fact_pbp_field_goal_attempt)
  UNION ALL (SELECT * FROM nba.fact_pbp_field_goal_attempt EXCEPT ALL SELECT * FROM expected)
 )
 UNION ALL SELECT 'pbp_attempts_source_and_geometry_disclosed',count(*) FROM (
  (SELECT * FROM expected_public EXCEPT ALL SELECT * FROM public_attempts)
  UNION ALL (SELECT * FROM public_attempts EXCEPT ALL SELECT * FROM expected_public)
 )
 UNION ALL SELECT 'pbp_attempts_never_double_count_shot_charts',count(*)
 FROM coverage_counts c SEMI JOIN nba.fact_shot s USING(game_id)
 UNION ALL SELECT 'pbp_attempt_scope_holds_and_publication_reconcile',count(*)
 FROM audit.pbp_attempt_coverage a LEFT JOIN coverage_counts c USING(game_id)
 WHERE (a.disposition='published_box_reconciled_pbp' AND c.n IS DISTINCT FROM a.candidate_attempts)
    OR (a.disposition<>'published_box_reconciled_pbp' AND c.n IS NOT NULL)
 UNION ALL SELECT 'pbp_attempt_mismatch_kept_held',CASE WHEN count(*)=1 THEN 0 ELSE 1 END
 FROM audit.pbp_attempt_coverage WHERE game_id='0021900891' AND disposition='box_attempts_disagree'
   AND team_box_mismatches=1 AND player_box_mismatches=1
 UNION ALL SELECT 'pbp_attempt_parent_pin_present',count(*) FROM (
  (SELECT role FROM audit.pbp_attempt_input EXCEPT ALL SELECT role FROM verify_pbp_attempt_roles)
  UNION ALL (SELECT role FROM verify_pbp_attempt_roles EXCEPT ALL SELECT role FROM audit.pbp_attempt_input)
 )
)
SELECT check_name,violations FROM checks ORDER BY check_name;
