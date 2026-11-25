WITH expected_team AS (
 SELECT b.game_id,b.team_id FROM nba.bridge_game_team b SEMI JOIN audit.mirror_v13_scope s USING(game_id)
), expected_coverage AS (
 SELECT game_id,status,candidate_sources,valid_sources,selected_source FROM audit.mirror_v13_coverage
 ANTI JOIN verify_mirror_v13_superseded USING(game_id)
), observed_coverage AS (
 SELECT c.game_id,c.status,c.candidate_sources,c.valid_sources,c.selected_source
 FROM audit.period_coverage c SEMI JOIN audit.mirror_v13_scope s USING(game_id)
 ANTI JOIN verify_mirror_v13_superseded USING(game_id)
), observed_cells AS (
 SELECT p.* FROM nba.fact_game_period_score p SEMI JOIN audit.mirror_v13_scope s USING(game_id)
 ANTI JOIN verify_mirror_v13_superseded USING(game_id)
), checks AS (
 SELECT 'mirror_v13_prior_keyset_complete' AS check_name,count(*) AS violations FROM (
  (SELECT game_id FROM audit.mirror_v13_scope EXCEPT ALL SELECT game_id FROM audit.mirror_v13_prior)
  UNION ALL (SELECT game_id FROM audit.mirror_v13_prior EXCEPT ALL SELECT game_id FROM audit.mirror_v13_scope)
 )
 UNION ALL SELECT 'mirror_v13_scope_matches_game_attributes',count(*)
 FROM audit.mirror_v13_scope s LEFT JOIN nba.dim_game g USING(game_id)
 WHERE s.season_year IS DISTINCT FROM g.season_year OR NOT coalesce(CASE s.cohort
       WHEN 'legacy_regular' THEN g.season_type='Regular' AND g.season_start BETWEEN 1970 AND 1999
       WHEN 'legacy_playoffs' THEN g.season_type='Playoffs' AND g.season_start>=1954 END,false)
 UNION ALL SELECT 'mirror_v13_team_source_keyset_complete',count(*) FROM (
  (SELECT * FROM expected_team EXCEPT ALL SELECT game_id,team_id FROM audit.mirror_v13_team)
  UNION ALL (SELECT game_id,team_id FROM audit.mirror_v13_team EXCEPT ALL SELECT * FROM expected_team)
 )
 UNION ALL SELECT 'mirror_v13_coverage_matches_disposition',count(*) FROM (
  (SELECT * FROM expected_coverage EXCEPT ALL SELECT * FROM observed_coverage)
  UNION ALL (SELECT * FROM observed_coverage EXCEPT ALL SELECT * FROM expected_coverage)
 )
 UNION ALL SELECT 'mirror_v13_cells_match_admitted_source',count(*) FROM (
  (SELECT * FROM audit.mirror_v13_cells EXCEPT ALL SELECT * FROM observed_cells)
  UNION ALL (SELECT * FROM observed_cells EXCEPT ALL SELECT * FROM audit.mirror_v13_cells)
 )
 UNION ALL SELECT 'mirror_v13_no_unledgered_period_publication',count(*)
 FROM audit.period_coverage p ANTI JOIN audit.mirror_v13_scope s USING(game_id)
 WHERE p.selected_source IN ('mirror_v13_legacy_team_box','mirror_v13_keyed_official_ot')
 UNION ALL SELECT 'mirror_v13_availability_matches_disposition',count(*)
 FROM audit.mirror_v13_coverage c LEFT JOIN audit.period_availability_v13 a USING(game_id)
 ANTI JOIN verify_mirror_v13_superseded USING(game_id)
 WHERE (a.status,a.selected_source,a.recovery_disposition) IS DISTINCT FROM
       (c.status,c.selected_source,coalesce(c.selected_source,c.disposition))
 UNION ALL SELECT 'mirror_v13_no_unclaimed_external_evidence',count(*) FROM audit.mirror_v13_official
 UNION ALL SELECT 'mirror_v13_input_roles_complete',count(*) FROM (
  (SELECT role FROM audit.mirror_v13_input EXCEPT ALL SELECT unnest(['parent_v12','raw']))
  UNION ALL (SELECT unnest(['parent_v12','raw']) EXCEPT ALL SELECT role FROM audit.mirror_v13_input)
 )
)
SELECT check_name,violations FROM checks ORDER BY check_name;
