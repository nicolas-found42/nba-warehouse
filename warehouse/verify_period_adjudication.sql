WITH expected AS (
 SELECT a.game_id,b.team_id,i AS period_number,CASE b.side WHEN 'home' THEN a.home_q[i] ELSE a.away_q[i] END AS points
 FROM audit.period_adjudication a JOIN nba.bridge_game_team b USING(game_id),range(1,5) n(i)
), actual AS (
 SELECT p.* FROM nba.fact_game_period_score p SEMI JOIN audit.period_adjudication a USING(game_id)
), checks AS (
 SELECT 'adjudication_matches_receipt_cells' AS check_name,count(*) AS violations FROM (
   (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
   UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
 )
 UNION ALL SELECT 'adjudication_keys_and_finals_match',count(*)
 FROM audit.period_adjudication a LEFT JOIN nba.fact_game g USING(game_id) LEFT JOIN audit.period_coverage c USING(game_id)
 LEFT JOIN nba.game_overtime o USING(game_id)
 WHERE (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score,c.status,c.selected_source,o.last_period,o.is_overtime)
   IS DISTINCT FROM (a.game_date,a.home_team_id,a.away_team_id,list_sum(a.home_q),list_sum(a.away_q),'published','adjudicated_period_receipt',4,false)
 UNION ALL SELECT 'all_five_adjudications_accounted',count(*) FROM (
   (SELECT game_id FROM audit.period_adjudication EXCEPT ALL SELECT * FROM (VALUES ('0021700793'),('0022400072'),('0029600106'),('0029600674'),('0046000414')))
   UNION ALL (SELECT * FROM (VALUES ('0021700793'),('0022400072'),('0029600106'),('0029600674'),('0046000414')) EXCEPT ALL SELECT game_id FROM audit.period_adjudication)
 )
 UNION ALL SELECT 'adjudication_no_unreceipted_periods',count(*)
 FROM audit.period_coverage c ANTI JOIN audit.period_adjudication a USING(game_id) WHERE c.selected_source='adjudicated_period_receipt'
)
SELECT check_name,violations FROM checks ORDER BY check_name;
