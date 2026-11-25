WITH expected(game_id,n) AS (VALUES ('0010600017',5),('0010600030',4),('0010600032',4),('0010600034',4),
 ('0010600058',4),('0010600070',4),('0010600076',4),('0010600089',4),('0010600092',4),('0010600093',4),('0010600094',4)),
keys AS (SELECT game_id,len(home_periods) AS n FROM audit.preseason_period_receipt_v26),
checks(check_name,violations) AS (
 SELECT 'v26_exact_preseason_scope_and_period_counts',count(*) FROM (
   (SELECT * FROM expected EXCEPT ALL SELECT * FROM keys)
   UNION ALL (SELECT * FROM keys EXCEPT ALL SELECT * FROM expected))
 UNION ALL SELECT 'v26_archive_fixture_and_regulation_tie',count(*) FROM audit.preseason_period_receipt_v26 r
 LEFT JOIN nba.fact_game g USING(game_id)
 WHERE (r.game_date,r.home_id,r.away_id,r.home_final,r.away_final,'Preseason',len(r.home_periods)>4)
       IS DISTINCT FROM (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score,g.season_type,g.is_overtime)
    OR list_count(r.home_periods)<>len(r.home_periods) OR list_count(r.away_periods)<>len(r.home_periods)
    OR (list_sum(r.home_periods),list_sum(r.away_periods)) IS DISTINCT FROM (r.home_final,r.away_final)
    OR (len(r.home_periods)=5 AND list_sum(r.home_periods[:4])<>list_sum(r.away_periods[:4]))
    OR NOT regexp_full_match(r.sha256,'[0-9a-f]{64}') OR r.redistribution_cleared
 UNION ALL SELECT 'v26_exact_archived_period_cells',count(*) FROM (
   (SELECT * FROM nba.fact_game_period_score SEMI JOIN expected USING(game_id) EXCEPT ALL SELECT * FROM audit.preseason_period_cells_v26)
   UNION ALL (SELECT * FROM audit.preseason_period_cells_v26 EXCEPT ALL SELECT * FROM nba.fact_game_period_score SEMI JOIN expected USING(game_id)))
 UNION ALL SELECT 'v26_prior_and_new_availability',count(*) FROM expected e
 LEFT JOIN audit.period_coverage c USING(game_id) LEFT JOIN audit.period_availability_v25 p USING(game_id)
 LEFT JOIN audit.period_availability_v26 a USING(game_id)
 WHERE (c.status,c.candidate_sources,c.valid_sources,c.selected_source,p.status,p.last_period,a.last_period,a.derived_overtime)
       IS DISTINCT FROM ('published',1,1,'archived_nba_preseason_line_score_v26','no_supported_source',NULL,e.n,e.n>4)
 UNION ALL SELECT 'v26_unresolved_efes_and_no_unledgered_admission',count(*) FROM audit.period_availability_v26 c
 WHERE (c.game_id='0010600026' AND c.last_period IS NOT NULL) OR (c.selected_source='archived_nba_preseason_line_score_v26'
       AND NOT EXISTS(SELECT 1 FROM expected e WHERE e.game_id=c.game_id))
)
SELECT * FROM checks ORDER BY check_name;
