WITH receipts AS (SELECT * FROM audit.period_adjudication_v19),
expected_fixtures(game_id,kind) AS (VALUES
 ('0024600016','Regular'),('0025700109','Regular'),('0046000222','Playoffs'),
 ('0046800323','Playoffs'),('0049500046','Playoffs')),
observed_cells AS (SELECT f.* FROM nba.fact_game_period_score f SEMI JOIN receipts r USING(game_id)),
player AS (
 SELECT p.game_id,p.team_id,sum(p.points) AS points,sum(p.fgm) AS fgm,
        sum(p.ftm) AS ftm,sum(p.fg3m) AS fg3m
 FROM nba.fact_player_game_stats p SEMI JOIN receipts USING(game_id) GROUP BY 1,2
), checks(check_name,violations) AS (
 SELECT 'v19_exact_receipt_fixtures',count(*) FROM receipts r FULL JOIN expected_fixtures e USING(game_id)
 LEFT JOIN nba.fact_game g USING(game_id) LEFT JOIN audit.game_disposition a USING(game_id)
 WHERE r.game_id IS NULL OR e.game_id IS NULL OR g.season_type IS DISTINCT FROM e.kind
   OR (r.game_date,r.home_id,r.away_id,r.new_home,r.new_away)
     IS DISTINCT FROM (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
   OR (r.old_home,r.old_away) IS DISTINCT FROM (a.source_home_score,a.source_away_score)
   OR (e.kind='Regular' AND (r.old_home,r.old_away)<>(r.new_home,r.new_away))
   OR (e.kind='Playoffs' AND ((r.page_home_final,r.page_away_final)<>(r.old_home,r.old_away)
                            OR (r.old_home,r.old_away)=(r.new_home,r.new_away)))
 UNION ALL SELECT 'v19_exact_team_period_cells',count(*) FROM (
   (SELECT * FROM observed_cells EXCEPT ALL SELECT * FROM audit.period_cells_v19)
   UNION ALL (SELECT * FROM audit.period_cells_v19 EXCEPT ALL SELECT * FROM observed_cells))
 UNION ALL SELECT 'v19_coverage_and_flags',count(*) FROM receipts r
 LEFT JOIN audit.period_availability_v19 c USING(game_id)
 LEFT JOIN audit.period_coverage p USING(game_id) LEFT JOIN nba.game_overtime o USING(game_id)
 WHERE (c.status,c.selected_source,c.recovery_disposition,p.candidate_sources,p.valid_sources,o.last_period,o.is_overtime)
    IS DISTINCT FROM ('published','official_v19_header_disagrees','official_v19_header_disagrees',r.old_candidates+1,1,4,false)
 UNION ALL SELECT 'v19_no_unledgered_publication',count(*) FROM audit.period_coverage c ANTI JOIN receipts r USING(game_id)
 WHERE c.selected_source='official_v19_header_disagrees'
 UNION ALL SELECT 'v19_players_and_shooting_support_final',abs(10-count(*))+count(*) FILTER (
    WHERE p.points IS DISTINCT FROM (CASE WHEN p.team_id=r.home_id THEN r.new_home WHEN p.team_id=r.away_id THEN r.new_away END)
       OR p.points IS DISTINCT FROM 2*p.fgm+p.ftm+CASE WHEN g.season_start<1979 THEN 0 ELSE p.fg3m END)
 FROM player p JOIN receipts r USING(game_id) JOIN nba.dim_game g USING(game_id)
 UNION ALL SELECT 'v19_local_exact_early_reversed_rows',count(*) FROM receipts r
 LEFT JOIN audit.local_period_recovery l USING(game_id) JOIN expected_fixtures e USING(game_id)
 WHERE e.kind='Regular' AND (l.original_line_date,l.original_home_team_id,l.original_away_team_id,
   l.original_home_final,l.original_away_final,l.home_periods,l.away_periods,l.status)
   IS DISTINCT FROM (r.game_date,r.away_id,r.home_id,r.page_away_final,r.page_home_final,r.home_cells,r.away_cells,'line_final_disagrees')
 UNION ALL SELECT 'v19_playoff_sources_apply_only_where_total_is_bad',count(*) FROM receipts r
 LEFT JOIN audit.mirror_v13_team h ON (r.game_id,r.home_id)=(h.game_id,h.team_id)
 LEFT JOIN audit.mirror_v13_team a ON (r.game_id,r.away_id)=(a.game_id,a.team_id)
 LEFT JOIN audit.mirror_v13_assessment d ON r.game_id=d.game_id
 LEFT JOIN audit.playoff_ot_schedule_match s ON r.game_id=s.game_id JOIN expected_fixtures e ON r.game_id=e.game_id
 WHERE e.kind='Playoffs' AND (
   (h.regulation,a.regulation,h.pts,a.pts,d.disposition,s.game_date,s.home_score,s.away_score,s.ot_count)
    IS DISTINCT FROM (r.home_cells,r.away_cells,r.page_home_final,r.page_away_final,'cells_do_not_reconcile',r.game_date,r.new_home,r.new_away,0)
   OR (r.game_id='0046000222' AND (list_sum(r.home_cells[:2]),list_sum(r.away_cells[:2]))<>(54,55))
   OR (r.game_id='0049500046' AND (list_sum(r.home_cells[:2]),list_sum(r.away_cells[:2]),list_sum(r.home_cells[:3]),list_sum(r.away_cells[:3]))<>(51,54,82,82)))
 UNION ALL SELECT 'v19_required_input_roles',count(*) FROM (
   (SELECT role FROM audit.period_v19_input EXCEPT ALL SELECT unnest(['parent_v18','printed_receipts']))
   UNION ALL (SELECT unnest(['parent_v18','printed_receipts']) EXCEPT ALL SELECT role FROM audit.period_v19_input))
)
SELECT check_name,violations FROM checks ORDER BY check_name;
