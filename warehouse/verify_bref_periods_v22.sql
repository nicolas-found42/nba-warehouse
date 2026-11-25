WITH expected(game_id,disposition) AS (VALUES
 ('0025100086','admitted'),('0025200313','admitted'),
 ('0025400246','held_local_cells_conflict'),('0025500205','held_local_cells_conflict'),
 ('0025500208','held_local_cells_conflict'),('0025500269','held_local_cells_conflict'),
 ('0025900028','held_local_cells_conflict')
), checks(check_name,violations) AS (
 SELECT 'v22_exact_scope',count(*) FROM (
  (SELECT game_id,disposition FROM audit.bref_period_receipt_v22 EXCEPT ALL SELECT * FROM expected)
  UNION ALL (SELECT * FROM expected EXCEPT ALL SELECT game_id,disposition FROM audit.bref_period_receipt_v22))
 UNION ALL SELECT 'v22_source_identity_and_local_comparison',count(*)
 FROM audit.bref_period_receipt_v22 r LEFT JOIN nba.fact_game g USING(game_id)
 LEFT JOIN audit.mirror_v14_team h ON h.game_id=r.game_id AND h.is_home
 LEFT JOIN audit.mirror_v14_team a ON a.game_id=r.game_id AND NOT a.is_home
 WHERE (r.game_date,r.home_id,r.away_id,r.home_total,r.away_total) IS DISTINCT FROM
       (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
    OR NOT regexp_full_match(r.sha256,'[0-9a-f]{64}')
    OR (list_sum(r.home_periods),list_sum(r.away_periods)) IS DISTINCT FROM (r.home_total,r.away_total)
    OR (r.local_home_periods,r.local_away_periods) IS DISTINCT FROM (h.regulation,a.regulation)
    OR (r.disposition='admitted') IS DISTINCT FROM
       ((r.home_periods,r.away_periods) IS NOT DISTINCT FROM (r.local_away_periods,r.local_home_periods))
 UNION ALL SELECT 'v22_source_cells_exact',count(*) FROM (
   (SELECT * FROM nba.fact_game_period_score SEMI JOIN expected USING(game_id)
    EXCEPT ALL SELECT * FROM audit.bref_period_cells_v22)
   UNION ALL (SELECT * FROM audit.bref_period_cells_v22 EXCEPT ALL
      SELECT * FROM nba.fact_game_period_score SEMI JOIN expected USING(game_id)))
 UNION ALL SELECT 'v22_coverage_delta_exact',count(*) FROM audit.bref_period_receipt_v22 r
 LEFT JOIN audit.period_coverage c USING(game_id) LEFT JOIN audit.period_availability_v22 a USING(game_id)
 LEFT JOIN audit.period_availability_v21 p USING(game_id)
 WHERE (r.disposition='admitted' AND (c.status,c.selected_source,c.candidate_sources,c.valid_sources,a.recovery_disposition)
        IS DISTINCT FROM ('published','bref_v22_periods_match_reoriented_local_cells',r.old_candidates+1,1,
                           'bref_v22_periods_match_reoriented_local_cells'))
    OR (r.disposition<>'admitted' AND (c.status,c.selected_source,c.candidate_sources,c.valid_sources,a.recovery_disposition)
        IS DISTINCT FROM (r.old_status,NULL,r.old_candidates,0,p.recovery_disposition))
 UNION ALL SELECT 'v22_no_unledgered_publication',count(*) FROM audit.period_coverage c
 ANTI JOIN (SELECT game_id FROM expected WHERE disposition='admitted') r USING(game_id)
 WHERE c.selected_source='bref_v22_periods_match_reoriented_local_cells'
)
SELECT check_name,violations FROM checks ORDER BY check_name;
