WITH sides AS (
 SELECT t.game_id,b.team_id,CASE b.side WHEN 'home' THEN g.away_team_id ELSE g.home_team_id END AS opponent_team_id,
        b.side,g.game_date,g.season_year,
        CASE b.side WHEN 'home' THEN g.home_score ELSE g.away_score END AS final
 FROM audit.mirror_period_game t JOIN nba.fact_game g USING(game_id)
 JOIN nba.bridge_game_team b USING(game_id)
), observed AS (
 SELECT p.* FROM nba.fact_game_period_score p SEMI JOIN audit.mirror_period_game USING(game_id)
), stage_scope AS (
 SELECT game_id FROM nba.dim_game WHERE season_start IN (1975,1976) AND season_type='Regular'
), publication AS (
 SELECT g.game_id,CASE WHEN EXISTS (SELECT 1 FROM audit.mirror_period_tail_receipt t WHERE t.game_id=g.game_id)
        THEN 'local_team_box_mirror_plus_official_tail' ELSE 'local_team_box_mirror' END AS selected_source
 FROM audit.mirror_period_game g
), actual_publication AS (
 SELECT game_id,selected_source FROM audit.period_coverage
 WHERE selected_source IN ('local_team_box_mirror','local_team_box_mirror_plus_official_tail')
), checks AS (
 SELECT 'mirror_scope_exact_keys' AS check_name,count(*) AS violations FROM (
  (SELECT game_id FROM audit.mirror_period_game EXCEPT ALL SELECT game_id FROM stage_scope)
  UNION ALL (SELECT game_id FROM stage_scope EXCEPT ALL SELECT game_id FROM audit.mirror_period_game)
 )
 UNION ALL SELECT 'mirror_source_exact_team_keys',count(*) FROM (
  (SELECT game_id,team_id FROM sides EXCEPT ALL SELECT game_id,team_id FROM audit.mirror_period_team)
  UNION ALL (SELECT game_id,team_id FROM audit.mirror_period_team EXCEPT ALL SELECT game_id,team_id FROM sides)
 )
 UNION ALL SELECT 'mirror_source_identity_and_final',count(*)
 FROM audit.mirror_period_team m LEFT JOIN sides s USING(game_id,team_id)
 WHERE (m.opponent_team_id,m.is_home,m.game_date,m.season_year,m.pts,m.game_type)
    IS DISTINCT FROM (s.opponent_team_id,s.side='home',s.game_date,s.season_year,s.final,'Regular Season')
 UNION ALL SELECT 'mirror_source_period_slots_integral_and_complete',count(*)
 FROM audit.mirror_period_team m LEFT JOIN audit.mirror_period_tail_receipt t USING(game_id,team_id)
 WHERE len(list_filter(m.regulation,lambda x: x IS NOT NULL))<>4
    OR len(list_filter(list_concat(m.regulation::DOUBLE[],m.overtime_slots::DOUBLE[],[m.ot_all_points]),
                lambda x: x IS NOT NULL AND (NOT isfinite(x) OR x<0 OR x>2147483647 OR trunc(x)<>x)))<>0
    OR (m.overtime_slots[1] IS NULL AND m.overtime_slots[2] IS NOT NULL)
    OR m.ot_all_points IS DISTINCT FROM list_sum(m.overtime_slots)+coalesce(t.points,0)
    OR m.pts IS DISTINCT FROM list_sum(m.regulation)+coalesce(m.ot_all_points,0)
 UNION ALL SELECT 'mirror_extended_overtime_receipt_identity',count(*)
 FROM audit.mirror_period_tail_receipt t LEFT JOIN audit.mirror_period_team m USING(game_id,team_id)
 WHERE m.game_date IS DISTINCT FROM t.game_date OR m.overtime_slots[2] IS NULL
    OR m.ot_all_points IS DISTINCT FROM list_sum(m.overtime_slots)+t.points
 UNION ALL SELECT 'mirror_source_same_active_overtime_slots',count(*)
 FROM audit.mirror_period_team h JOIN audit.mirror_period_team a USING(game_id)
 WHERE h.is_home AND NOT a.is_home
   AND ((h.overtime_slots[1] IS NULL,h.overtime_slots[2] IS NULL)
       IS DISTINCT FROM (a.overtime_slots[1] IS NULL,a.overtime_slots[2] IS NULL))
 UNION ALL SELECT 'mirror_fact_cells_match_source',count(*) FROM (
  (SELECT * FROM audit.mirror_period_expected_cells EXCEPT ALL SELECT * FROM observed)
  UNION ALL (SELECT * FROM observed EXCEPT ALL SELECT * FROM audit.mirror_period_expected_cells)
 )
 UNION ALL SELECT 'mirror_publications_have_exact_scope',count(*) FROM (
  (SELECT * FROM publication EXCEPT ALL SELECT * FROM actual_publication)
  UNION ALL (SELECT * FROM actual_publication EXCEPT ALL SELECT * FROM publication)
 )
 UNION ALL SELECT 'mirror_publications_reconcile_coverage_counters',count(*)
 FROM audit.mirror_period_game g LEFT JOIN audit.period_coverage p USING(game_id)
 WHERE (p.status,p.candidate_sources,p.valid_sources) IS DISTINCT FROM ('published',g.prior_candidates+1,g.prior_valid+1)
 UNION ALL SELECT 'mirror_input_roles_complete',count(*) FROM (
  (SELECT role FROM audit.mirror_period_input EXCEPT ALL SELECT unnest(['parent','source','official_tail_csv']))
  UNION ALL (SELECT unnest(['parent','source','official_tail_csv']) EXCEPT ALL SELECT role FROM audit.mirror_period_input)
 )
)
SELECT check_name,violations FROM checks ORDER BY check_name;
