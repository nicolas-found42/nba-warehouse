WITH hold_keys AS (
 SELECT game_id FROM audit.mirror_v13_assessment WHERE disposition='reported_overtime_disagrees'
), admitted_holds AS (
 SELECT game_id FROM audit.period_adjudication_v16_keys WHERE selected_source='mirror_v16_playoff_ot_flag_adjudicated'
), source_ot AS (
 SELECT game_id,ot_count FROM audit.playoff_ot_schedule_match WHERE ot_count>0
), derived_ot AS (
 SELECT f.game_id,o.last_period-4 AS ot_count FROM audit.playoff_ot_fixture f JOIN nba.game_overtime o USING(game_id)
 WHERE o.is_overtime IS TRUE
), unmatched AS (
 SELECT game_id FROM audit.playoff_ot_fixture ANTI JOIN audit.playoff_ot_schedule_match USING(game_id)
), expected_unmatched AS (
 SELECT x.game_id FROM (VALUES ('0046000222'),('0046800323'),('0049500046')) x(game_id)
 ANTI JOIN verify_period_v19_keys USING(game_id)
), compared_counts AS (
 SELECT p.season_start,p.game_count,p.ot_count,
   (SELECT count(*) FROM audit.playoff_ot_schedule r WHERE r.season_start=p.season_start) AS source_games,
   (SELECT count(*) FROM audit.playoff_ot_schedule r WHERE r.season_start=p.season_start AND r.ot_count>0) AS source_ot,
   (SELECT count(*) FROM audit.playoff_ot_fixture g WHERE g.season_start=p.season_start) AS warehouse_games
 FROM audit.playoff_ot_schedule_pin p
), expected_counts(season_start,game_count,ot_count) AS (
 VALUES (1960,25,1),(1964,26,2),(1968,39,0),(1993,77,7),(1995,68,5)
), cells AS (
 SELECT f.* FROM nba.fact_game_period_score f SEMI JOIN audit.period_adjudication_v16_keys USING(game_id)
), actual_coverage AS (
 SELECT p.game_id,p.status,p.candidate_sources,p.valid_sources,p.selected_source
 FROM audit.period_coverage p SEMI JOIN audit.period_adjudication_v16_keys USING(game_id)
), checks AS (
 SELECT 'playoff_ot_source_denominators' AS check_name,count(*) AS violations
 FROM compared_counts c FULL JOIN expected_counts e USING(season_start)
 WHERE (c.game_count,c.source_games,c.warehouse_games,c.ot_count,c.source_ot)
    IS DISTINCT FROM (e.game_count,e.game_count,e.game_count,e.ot_count,e.ot_count)
 UNION ALL SELECT 'playoff_ot_matches_one_to_one',count(*) FROM (
  SELECT game_id FROM audit.playoff_ot_schedule_match GROUP BY 1 HAVING count(*)<>1
  UNION ALL SELECT min(game_id) FROM audit.playoff_ot_schedule_match GROUP BY season_start,source_ordinal HAVING count(*)<>1
 )
 UNION ALL SELECT 'playoff_ot_exact_unmatched_fixture_controls',count(*) FROM (
  (SELECT * FROM unmatched EXCEPT ALL SELECT * FROM expected_unmatched)
  UNION ALL (SELECT * FROM expected_unmatched EXCEPT ALL SELECT * FROM unmatched)
 )
 UNION ALL SELECT 'playoff_ot_holds_have_exact_unmarked_receipts',count(*) FROM (
  (SELECT * FROM hold_keys EXCEPT ALL SELECT * FROM admitted_holds)
  UNION ALL (SELECT * FROM admitted_holds EXCEPT ALL SELECT * FROM hold_keys)
 )
 UNION ALL SELECT 'playoff_ot_true_controls_match_exact_keys_and_count',count(*) FROM (
  (SELECT * FROM source_ot EXCEPT ALL SELECT * FROM derived_ot)
  UNION ALL (SELECT * FROM derived_ot EXCEPT ALL SELECT * FROM source_ot)
 )
 UNION ALL SELECT 'playoff_ot_reported_flag_preserved',count(*)
 FROM admitted_holds k LEFT JOIN nba.game_overtime o USING(game_id)
 WHERE (o.last_period,o.is_overtime,o.reported_is_overtime) IS DISTINCT FROM (4,false,true)
 UNION ALL SELECT 'v16_period_cells_exact',count(*) FROM (
  (SELECT * FROM cells EXCEPT ALL SELECT * FROM audit.period_adjudication_v16_cells)
  UNION ALL (SELECT * FROM audit.period_adjudication_v16_cells EXCEPT ALL SELECT * FROM cells)
 )
 UNION ALL SELECT 'v16_period_coverage_exact',count(*) FROM (
  (SELECT * FROM actual_coverage EXCEPT ALL SELECT * FROM audit.period_adjudication_v16_coverage)
  UNION ALL (SELECT * FROM audit.period_adjudication_v16_coverage EXCEPT ALL SELECT * FROM actual_coverage)
 )
 UNION ALL SELECT 'v16_period_unledgered_publications_absent',count(*)
 FROM audit.period_coverage p ANTI JOIN audit.period_adjudication_v16_keys k USING(game_id)
 WHERE selected_source IN ('mirror_v16_playoff_ot_flag_adjudicated','mirror_v16_keyed_official_ot3')
 UNION ALL SELECT 'v16_period_availability_matches_adjudication',count(*)
 FROM audit.period_adjudication_v16_coverage c LEFT JOIN audit.period_availability_v16 a USING(game_id)
 WHERE (a.status,a.selected_source,a.recovery_disposition) IS DISTINCT FROM (c.status,c.selected_source,c.selected_source)
 UNION ALL SELECT 'v16_period_adjudication_ledger_complete',CASE WHEN count(*)=199 AND count(*) FILTER(WHERE selected_source='mirror_v16_keyed_official_ot3')=1 THEN 0 ELSE 1 END
 FROM audit.period_adjudication_v16_keys
 UNION ALL SELECT 'v16_early_ot3_source_identity',count(*)
 FROM audit.early_ot3_receipt_v16 t LEFT JOIN nba.fact_game g USING(game_id)
 LEFT JOIN audit.mirror_v12_assessment a USING(game_id)
 LEFT JOIN audit.mirror_v12_vectors h ON (h.game_id,h.team_id)=(t.game_id,t.home_id)
 LEFT JOIN audit.mirror_v12_vectors v ON (v.game_id,v.team_id)=(t.game_id,t.away_id)
 WHERE (t.game_date,t.home_id,t.away_id,list_sum(t.home_periods),list_sum(t.away_periods),a.disposition)
  IS DISTINCT FROM (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score,'overtime_slots_truncated')
 OR (t.home_periods[:6],t.away_periods[:6],list_sum(t.home_periods[5:]),list_sum(t.away_periods[5:]))
  IS DISTINCT FROM (h.vector,v.vector,h.ot_all_points,v.ot_all_points)
)
SELECT check_name,violations FROM checks ORDER BY check_name;
