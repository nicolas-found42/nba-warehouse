WITH expected_periods AS (
 SELECT s.game_id,p.period FROM legacy_attempt_scope_expected s CROSS JOIN range(1,5) p(period)
), boundary_stats AS (
 SELECT m.game_id,m.period,m.starts,m.ends,m.start_action,m.end_action,m.prior_end_action,
        m.start_clock,m.end_clock,m.start_home,m.start_away,m.end_home,m.end_away,m.prior_home,m.prior_away,
        h.points AS home_points,a.points AS away_points,g.home_score,g.away_score
 FROM legacy_period_boundaries m JOIN nba.fact_game g USING(game_id)
 LEFT JOIN nba.fact_game_period_score h ON (m.game_id,g.home_team_id,m.period)=(h.game_id,h.team_id,h.period_number)
 LEFT JOIN nba.fact_game_period_score a ON (m.game_id,g.away_team_id,m.period)=(a.game_id,a.team_id,a.period_number)
), checks(check_name,violations) AS (
 SELECT 'legacy_attempt_scope_exact',count(*) FROM (
   (SELECT * FROM legacy_attempt_scope_expected EXCEPT ALL SELECT * FROM audit.legacy_attempt_scope_v18)
   UNION ALL
   (SELECT * FROM audit.legacy_attempt_scope_v18 EXCEPT ALL SELECT * FROM legacy_attempt_scope_expected))
 UNION ALL SELECT 'legacy_attempt_exact_season_denominator',abs(1189-count(*)) FROM nba.dim_game
 WHERE season_start=2000 AND season_type='Regular' AND result_status='final'
 UNION ALL SELECT 'legacy_attempt_scope_11_disjoint_games',abs(11-count(*))+count(*) FILTER(WHERE p.game_id IS NOT NULL)
 FROM audit.legacy_attempt_scope_v18 l LEFT JOIN audit.pbp_attempt_scope p USING(game_id)
 UNION ALL SELECT 'legacy_attempt_44_periods',count(*) FROM (
   (SELECT game_id,period FROM legacy_period_boundaries EXCEPT ALL SELECT * FROM expected_periods)
   UNION ALL
   (SELECT * FROM expected_periods EXCEPT ALL SELECT game_id,period FROM legacy_period_boundaries))
 UNION ALL SELECT 'legacy_attempt_boundaries_and_finals',count(*) FROM boundary_stats
 WHERE NOT coalesce(starts=1 AND ends=1 AND start_clock=720 AND end_clock=0 AND start_action<end_action
   AND (period=1 OR prior_end_action<start_action) AND start_home=prior_home AND start_away=prior_away
   AND end_home-start_home=home_points AND end_away-start_away=away_points
   AND (period<>4 OR (end_home=home_score AND end_away=away_score)),false)
 UNION ALL SELECT 'legacy_attempt_actor_clock_and_explicit_values',count(*) FROM legacy_attempt_candidates WHERE NOT valid
 UNION ALL SELECT 'legacy_attempt_exact_shooting_totals',
   abs(1745-count(*))+abs(764-count(*) FILTER(WHERE made))
   +abs(277-count(*) FILTER(WHERE shot_value=3))+abs(102-count(*) FILTER(WHERE shot_value=3 AND made))
 FROM legacy_attempt_expected
 UNION ALL SELECT 'legacy_attempt_exact_player_boxes',abs(262-count(*))+count(*) FILTER (WHERE NOT (has_box AND matches))
 FROM legacy_attempt_player_checks
 UNION ALL SELECT 'legacy_attempt_exact_team_boxes',abs(22-count(*))+count(*) FILTER (WHERE NOT (has_box AND matches))
 FROM legacy_attempt_team_checks
 UNION ALL SELECT 'legacy_attempt_input_pins',abs(2-count(*))+count(*) FILTER (
    WHERE role NOT IN ('parent_v17','raw_snapshot') OR NOT regexp_full_match(sha256,'[a-f0-9]{64}'))
 FROM audit.legacy_attempt_input_v18
)
SELECT check_name,violations FROM checks ORDER BY check_name;
