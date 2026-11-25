WITH expected_cells AS (
 SELECT * FROM audit.mirror_v14_cells UNION ALL SELECT * FROM audit.mirror_v14_tail_cells
), actual_cells AS (
 SELECT p.* FROM nba.fact_game_period_score p SEMI JOIN audit.mirror_v14_changed_keys USING(game_id)
 ANTI JOIN verify_new_period_superseded USING(game_id)
), expected_coverage AS (
 SELECT game_id,status,candidate_sources,valid_sources,selected_source FROM audit.mirror_v14_coverage
 ANTI JOIN verify_new_period_superseded USING(game_id)
 UNION ALL SELECT * FROM audit.mirror_v14_tail_coverage
), actual_coverage AS (
 SELECT c.game_id,c.status,c.candidate_sources,c.valid_sources,c.selected_source
 FROM audit.period_coverage c SEMI JOIN audit.mirror_v14_changed_keys USING(game_id)
 ANTI JOIN verify_new_period_superseded USING(game_id)
), expected_team_keys AS (
 SELECT b.game_id,b.team_id FROM nba.bridge_game_team b JOIN audit.mirror_v14_assessment a USING(game_id)
 WHERE a.observed_rows>0
), checks AS (
 SELECT 'mirror_v14_prior_keys_match_cohort' AS check_name,count(*) AS violations FROM (
  (SELECT game_id FROM audit.mirror_v14_scope EXCEPT ALL SELECT game_id FROM audit.mirror_v14_prior)
  UNION ALL (SELECT game_id FROM audit.mirror_v14_prior EXCEPT ALL SELECT game_id FROM audit.mirror_v14_scope)
 )
 UNION ALL SELECT 'mirror_v14_historical_scope_membership',count(*)
 FROM audit.mirror_v14_scope s LEFT JOIN nba.dim_game g USING(game_id)
 LEFT JOIN audit.mirror_v12_scope prev USING(game_id)
 WHERE prev.game_id IS NOT NULL OR s.season_year IS DISTINCT FROM g.season_year
  OR NOT coalesce(CASE s.cohort WHEN 'legacy_regular' THEN g.season_type='Regular' AND g.season_start BETWEEN 1946 AND 1969
      WHEN 'legacy_playoffs' THEN g.season_type='Playoffs' AND g.season_start BETWEEN 1946 AND 1953 END,false)
 UNION ALL SELECT 'mirror_v14_team_keys_exact_for_present_games',count(*) FROM (
  (SELECT * FROM expected_team_keys EXCEPT ALL SELECT game_id,team_id FROM audit.mirror_v14_team)
  UNION ALL (SELECT game_id,team_id FROM audit.mirror_v14_team EXCEPT ALL SELECT * FROM expected_team_keys)
 )
 UNION ALL SELECT 'mirror_v14_cells_match_exact_sources',count(*) FROM (
  (SELECT * FROM expected_cells EXCEPT ALL SELECT * FROM actual_cells)
  UNION ALL (SELECT * FROM actual_cells EXCEPT ALL SELECT * FROM expected_cells)
 )
 UNION ALL SELECT 'mirror_v14_coverage_matches_exact_dispositions',count(*) FROM (
  (SELECT * FROM expected_coverage EXCEPT ALL SELECT * FROM actual_coverage)
  UNION ALL (SELECT * FROM actual_coverage EXCEPT ALL SELECT * FROM expected_coverage)
 )
 UNION ALL SELECT 'mirror_v14_no_unledgered_period_publication',count(*)
 FROM audit.period_coverage c ANTI JOIN audit.mirror_v14_changed_keys USING(game_id)
 WHERE selected_source IN ('mirror_v14_legacy_team_box','mirror_v14_keyed_official_ot3')
 UNION ALL SELECT 'mirror_v14_tail_keys_exact',count(*) FROM (
  (SELECT game_id FROM audit.mirror_v14_tail EXCEPT ALL SELECT * FROM (VALUES ('0027700297'),('0029200096')))
  UNION ALL (SELECT * FROM (VALUES ('0027700297'),('0029200096')) EXCEPT ALL SELECT game_id FROM audit.mirror_v14_tail)
 )
 UNION ALL SELECT 'mirror_v14_tail_keeps_prefix_and_proves_missing_period',count(*)
 FROM audit.mirror_v14_tail t LEFT JOIN nba.fact_game g USING(game_id)
 LEFT JOIN audit.mirror_v13_assessment a USING(game_id) LEFT JOIN audit.mirror_v13_coverage c USING(game_id)
 LEFT JOIN audit.mirror_v13_vectors h ON (h.game_id,h.team_id)=(t.game_id,t.home_id)
 LEFT JOIN audit.mirror_v13_vectors v ON (v.game_id,v.team_id)=(t.game_id,t.away_id)
 WHERE (t.game_date,t.home_id,t.away_id,list_sum(t.home_periods),list_sum(t.away_periods),a.disposition,c.status)
          IS DISTINCT FROM (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score,'overtime_slots_truncated','no_valid_candidate')
    OR (t.home_periods[:6],t.away_periods[:6],list_sum(t.home_periods[5:]),list_sum(t.away_periods[5:]))
          IS DISTINCT FROM (h.vector,v.vector,h.ot_all_points,v.ot_all_points)
    OR t.home_periods[7] IS NULL OR t.away_periods[7] IS NULL OR g.is_overtime IS DISTINCT FROM true
 UNION ALL SELECT 'mirror_v14_availability_matches_latest_evidence',count(*)
 FROM expected_coverage c LEFT JOIN audit.period_availability_v14 a USING(game_id)
 LEFT JOIN audit.mirror_v14_assessment x USING(game_id)
 WHERE (a.status,a.selected_source,a.recovery_disposition) IS DISTINCT FROM
       (c.status,c.selected_source,coalesce(c.selected_source,x.disposition))
 UNION ALL SELECT 'mirror_v14_no_unclaimed_external_evidence',count(*) FROM audit.mirror_v14_official
 UNION ALL SELECT 'mirror_v14_input_roles_complete',count(*) FROM (
  (SELECT role FROM audit.mirror_v14_input EXCEPT ALL SELECT unnest(['parent_v13','raw','ot3_receipts']))
  UNION ALL (SELECT unnest(['parent_v13','raw','ot3_receipts']) EXCEPT ALL SELECT role FROM audit.mirror_v14_input)
 )
)
SELECT check_name,violations FROM checks ORDER BY check_name;
