WITH observed_cells AS (
 SELECT p.* FROM nba.fact_game_period_score p SEMI JOIN audit.mirror_v12_scope s USING(game_id)
 ANTI JOIN verify_mirror_v12_superseded USING(game_id)
), expected_team_keys AS (
 SELECT b.game_id,b.team_id FROM nba.bridge_game_team b SEMI JOIN audit.mirror_v12_scope s USING(game_id)
), actual_coverage AS (
 SELECT p.game_id,p.status,p.selected_source,p.candidate_sources,p.valid_sources
 FROM audit.period_coverage p SEMI JOIN audit.mirror_v12_scope s USING(game_id)
 ANTI JOIN verify_mirror_v12_superseded USING(game_id)
), expected_coverage AS (
 SELECT game_id,status,selected_source,candidate_sources,valid_sources FROM audit.mirror_v12_coverage
 ANTI JOIN verify_mirror_v12_superseded USING(game_id)
), checks AS (
 SELECT 'mirror_v12_scope_keys_preserved' AS check_name,count(*) AS violations FROM (
  (SELECT game_id FROM audit.mirror_v12_scope EXCEPT ALL SELECT game_id FROM audit.mirror_v12_prior)
  UNION ALL (SELECT game_id FROM audit.mirror_v12_prior EXCEPT ALL SELECT game_id FROM audit.mirror_v12_scope)
 )
 UNION ALL SELECT 'mirror_v12_source_team_keys_exact',count(*) FROM (
  (SELECT * FROM expected_team_keys EXCEPT ALL SELECT game_id,team_id FROM audit.mirror_v12_team)
  UNION ALL (SELECT game_id,team_id FROM audit.mirror_v12_team EXCEPT ALL SELECT * FROM expected_team_keys)
 )
 UNION ALL SELECT 'mirror_v12_receipt_keys_and_dispositions',count(*) FROM (
  (SELECT game_id FROM audit.mirror_v12_scope WHERE cohort='keyed_overtime'
   EXCEPT ALL SELECT game_id FROM audit.mirror_v12_assessment WHERE cohort='keyed_overtime' AND disposition='admitted')
  UNION ALL (SELECT game_id FROM audit.mirror_v12_official
   EXCEPT ALL SELECT game_id FROM audit.mirror_v12_scope WHERE cohort='keyed_overtime')
 )
 UNION ALL SELECT 'mirror_v12_prior_status_per_cohort',count(*)
 FROM audit.mirror_v12_prior p JOIN audit.mirror_v12_scope s USING(game_id)
 WHERE CASE WHEN s.cohort='legacy_regular' THEN p.status<>'no_supported_source' OR p.candidate_sources<>0
            ELSE p.status<>'no_valid_candidate' OR p.candidate_sources<1 END
 UNION ALL SELECT 'mirror_v12_published_exact_cells_and_holds',count(*) FROM (
  (SELECT * FROM observed_cells EXCEPT ALL SELECT * FROM audit.mirror_v12_cells)
  UNION ALL (SELECT * FROM audit.mirror_v12_cells EXCEPT ALL SELECT * FROM observed_cells)
 )
 UNION ALL SELECT 'mirror_v12_exact_coverage_delta',count(*) FROM (
  (SELECT * FROM actual_coverage EXCEPT ALL SELECT * FROM expected_coverage)
  UNION ALL (SELECT * FROM expected_coverage EXCEPT ALL SELECT * FROM actual_coverage)
 )
 UNION ALL SELECT 'mirror_v12_no_publication_outside_cohort',count(*)
 FROM audit.period_coverage p ANTI JOIN audit.mirror_v12_scope s USING(game_id)
 WHERE p.selected_source IN ('mirror_v12_keyed_official_ot','mirror_v12_legacy_team_box')
 UNION ALL SELECT 'mirror_v12_availability_has_scoped_lineage',count(*)
 FROM audit.mirror_v12_coverage c LEFT JOIN audit.period_availability_v12 a USING(game_id)
 ANTI JOIN verify_mirror_v12_superseded USING(game_id)
 WHERE (a.status,a.selected_source,a.recovery_disposition) IS DISTINCT FROM
       (c.status,c.selected_source,coalesce(c.selected_source,c.disposition))
 UNION ALL SELECT 'mirror_v12_input_roles_complete',count(*) FROM (
  (SELECT role FROM audit.mirror_v12_input EXCEPT ALL SELECT unnest(['parent_v11','raw','keyed_ot_receipts']))
  UNION ALL (SELECT unnest(['parent_v11','raw','keyed_ot_receipts']) EXCEPT ALL SELECT role FROM audit.mirror_v12_input)
 )
)
SELECT check_name,violations FROM checks ORDER BY check_name;
