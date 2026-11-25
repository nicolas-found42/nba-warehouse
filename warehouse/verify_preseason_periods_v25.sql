WITH expected(game_id,disposition) AS (VALUES
 ('0010600001','control'),('0010600007','control'),('0010600016','admitted'),('0010600024','admitted'),('0010600025','admitted')),
checks(check_name,violations) AS (
 SELECT 'v25_exact_preseason_recovery_scope',count(*) FROM (
   (SELECT * FROM expected EXCEPT ALL SELECT game_id,disposition FROM audit.preseason_period_receipt_v25)
   UNION ALL (SELECT game_id,disposition FROM audit.preseason_period_receipt_v25 EXCEPT ALL SELECT * FROM expected))
 UNION ALL SELECT 'v25_archive_fixture_sides_and_totals',count(*)
 FROM audit.preseason_period_receipt_v25 r LEFT JOIN nba.fact_game g USING(game_id)
 WHERE (r.game_date,r.home_id,r.away_id,r.home_final,r.away_final,'Preseason',false)
  IS DISTINCT FROM (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score,g.season_type,g.is_overtime)
  OR list_count(r.home_periods)<>4 OR list_count(r.away_periods)<>4
  OR (list_sum(r.home_periods),list_sum(r.away_periods)) IS DISTINCT FROM (r.home_final,r.away_final)
  OR NOT regexp_full_match(r.sha256,'[0-9a-f]{64}') OR r.redistribution_cleared
  OR NOT starts_with(r.source_url,'https://web.archive.org/web/')
 UNION ALL SELECT 'v25_archive_exact_cells',count(*) FROM (
   (SELECT * FROM nba.fact_game_period_score SEMI JOIN (SELECT game_id FROM expected WHERE disposition='admitted') USING(game_id)
    EXCEPT ALL SELECT * FROM audit.preseason_period_cells_v25)
   UNION ALL (SELECT * FROM audit.preseason_period_cells_v25 EXCEPT ALL SELECT * FROM nba.fact_game_period_score
       SEMI JOIN (SELECT game_id FROM expected WHERE disposition='admitted') USING(game_id)))
 UNION ALL SELECT 'v25_every_printed_control_and_admission_cell',count(*)
 FROM audit.preseason_period_receipt_v25 r,range(1,5) p(n)
 LEFT JOIN nba.fact_game_period_score h ON h.game_id=r.game_id AND h.team_id=r.home_id AND h.period_number=n
 LEFT JOIN nba.fact_game_period_score a ON a.game_id=r.game_id AND a.team_id=r.away_id AND a.period_number=n
 WHERE (h.points,a.points) IS DISTINCT FROM (r.home_periods[n],r.away_periods[n])
 UNION ALL SELECT 'v25_period_availability_and_old_disposition',count(*)
 FROM audit.preseason_period_receipt_v25 r LEFT JOIN audit.period_coverage c USING(game_id)
 LEFT JOIN audit.period_availability_v23 p USING(game_id) LEFT JOIN audit.period_availability_v25 a USING(game_id)
 WHERE (r.prior_status,r.prior_source) IS DISTINCT FROM (p.status,p.selected_source)
 OR CASE r.disposition WHEN 'admitted' THEN
     (c.status,c.candidate_sources,c.valid_sources,c.selected_source,p.last_period,a.last_period,a.derived_overtime)
      IS DISTINCT FROM ('published',1,1,'archived_nba_preseason_line_score_v25',NULL,4,false)
    ELSE (c.status,c.candidate_sources,c.selected_source) IS DISTINCT FROM (r.prior_status,r.prior_candidates,r.prior_source) END
 UNION ALL SELECT 'v25_no_unledgered_archive_publication',count(*)
 FROM audit.period_coverage c ANTI JOIN (SELECT game_id FROM expected WHERE disposition='admitted') USING(game_id)
 WHERE c.selected_source='archived_nba_preseason_line_score_v25'
)
SELECT * FROM checks ORDER BY check_name;
