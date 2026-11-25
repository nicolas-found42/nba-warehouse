WITH official AS (
 SELECT r.game_id,b.team_id,i AS period_number,CASE b.side WHEN 'home' THEN r.home_q[i] ELSE r.away_q[i] END AS points
 FROM audit.official_period_receipt r JOIN nba.bridge_game_team b USING(game_id),range(1,5) n(i)
), published AS (
 SELECT p.* FROM nba.fact_game_period_score p SEMI JOIN audit.official_period_receipt r USING(game_id)
), checks AS (
 SELECT 'gamebook_period_cells_match' AS check_name,count(*) AS violations FROM (
   (SELECT * FROM official EXCEPT ALL SELECT * FROM published)
   UNION ALL (SELECT * FROM published EXCEPT ALL SELECT * FROM official)
 )
 UNION ALL SELECT 'gamebook_fixture_keys_and_finals_match',count(*)
 FROM audit.official_period_receipt r LEFT JOIN nba.fact_game g USING(game_id) LEFT JOIN audit.period_coverage p USING(game_id)
 WHERE (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score,g.season_type,g.result_status,p.status,p.selected_source)
 IS DISTINCT FROM (r.game_date,r.home_team_id,r.away_team_id,list_sum(r.home_q),list_sum(r.away_q),'Regular','final','published','official_period_receipt')
 UNION ALL SELECT 'gamebook_no_unreceipted_publication',count(*)
 FROM audit.period_coverage p ANTI JOIN audit.official_period_receipt r USING(game_id) WHERE p.selected_source='official_period_receipt'
 UNION ALL SELECT 'gamebook_comparison_one_per_game',count(*) FROM (
   (SELECT game_id,espn_game_id FROM audit.official_period_receipt EXCEPT ALL SELECT game_id,espn_game_id FROM audit.official_period_comparison)
   UNION ALL (SELECT game_id,espn_game_id FROM audit.official_period_comparison EXCEPT ALL SELECT game_id,espn_game_id FROM audit.official_period_receipt)
 )
 UNION ALL SELECT 'gamebook_provider_fixture_checks',count(*) FROM audit.official_period_comparison
 WHERE (key_rows,bridge_rows,schedule_rows,fixture_agrees,schedule_agrees) IS DISTINCT FROM (1,1,1,true,true)
)
SELECT check_name,violations FROM checks ORDER BY check_name;
