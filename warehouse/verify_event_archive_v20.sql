WITH fixture(game_id,day,home,away,home_score,away_score,events,shots,zeroes) AS (VALUES
 ('0020600975','20070316','DAL','BOS',106,101,429,151,41),
 ('0020700753','20080211','CHA','LAL',97,106,413,150,47)
), actual_event AS (
 SELECT e.* FROM nba.fact_play_event e SEMI JOIN fixture USING(game_id)
), actual_shot AS (
 SELECT s.* FROM nba.fact_shot s SEMI JOIN fixture USING(game_id)
), counts AS (
 SELECT f.*,e.n AS observed_events,s.n AS observed_shots,s.zeroes AS observed_zeroes,a.n AS observed_links
 FROM fixture f
 LEFT JOIN (SELECT game_id,count(*) n FROM actual_event GROUP BY 1) e USING(game_id)
 LEFT JOIN (SELECT game_id,count(*) n,count(*) FILTER(WHERE geometry_status='ambiguous_zero') zeroes FROM actual_shot GROUP BY 1) s USING(game_id)
 LEFT JOIN (SELECT game_id,count(*) n FROM nba.bridge_shot_event_source_v20 GROUP BY 1) a USING(game_id)
), box_players AS (
 SELECT p.game_id,p.team_id,p.person_id,p.fga,p.fgm,p.fg3a,p.fg3m FROM nba.fact_player_game_stats p SEMI JOIN fixture USING(game_id)
), shot_totals AS (
 SELECT game_id,team_id,person_id,count(*) AS fga,count(*) FILTER(WHERE made) AS fgm,
        count(*) FILTER(WHERE shot_value=3) AS fg3a,count(*) FILTER(WHERE made AND shot_value=3) AS fg3m
 FROM actual_shot GROUP BY GROUPING SETS ((game_id,team_id,person_id),(game_id,team_id))
), ft_totals AS (
 SELECT game_id,raw_team AS team_id,count(*) AS fta,count(*) FILTER(WHERE NOT starts_with(description,'MISS ')) AS ftm,
        count(*) FILTER(WHERE NOT coalesce(length(description)>0 AND contains(description,'Free Throw'),false)) AS malformed
 FROM archive_play_input_v20 WHERE msg=3 GROUP BY 1,2
), box_teams AS (
 SELECT t.game_id,t.team_id,t.fga,t.fgm,t.fg3a,t.fg3m,t.fta,t.ftm,t.pts FROM nba.fact_team_game_stats t SEMI JOIN fixture USING(game_id)
), boundary AS (
 SELECT game_id,period,count(*) FILTER(WHERE source_subtype='start' AND seconds_remaining=720) AS starts,
        count(*) FILTER(WHERE source_subtype='end' AND seconds_remaining=0) AS ends,
        min(reported_home_score) FILTER(WHERE source_subtype='start') AS start_home,
        min(reported_away_score) FILTER(WHERE source_subtype='start') AS start_away,
        max(reported_home_score) FILTER(WHERE source_subtype='end') AS end_home,
        max(reported_away_score) FILTER(WHERE source_subtype='end') AS end_away
 FROM actual_event WHERE source_action='period' GROUP BY 1,2
), periods AS (
 SELECT b.*, lag(end_home,1,0) OVER w AS before_home,lag(end_away,1,0) OVER w AS before_away
 FROM boundary b WINDOW w AS (PARTITION BY game_id ORDER BY period)
), checks(check_name,violations) AS (
 SELECT 'v20_exact_event_counts_and_archive_geometry',count(*) FROM counts
 WHERE (observed_events,observed_shots,observed_zeroes,observed_links) IS DISTINCT FROM (events,shots,zeroes,shots)
 UNION ALL SELECT 'v20_event_cells_equal_raw_projection',count(*) FROM (
   (SELECT * FROM actual_event EXCEPT ALL SELECT * FROM archive_play_expected_v20)
   UNION ALL (SELECT * FROM archive_play_expected_v20 EXCEPT ALL SELECT * FROM actual_event))
 UNION ALL SELECT 'v20_shot_cells_equal_raw_projection',count(*) FROM (
   (SELECT * FROM actual_shot EXCEPT ALL SELECT * FROM archive_shot_expected_v20)
   UNION ALL (SELECT * FROM archive_shot_expected_v20 EXCEPT ALL SELECT * FROM actual_shot))
 UNION ALL SELECT 'v20_link_native_ids_exact',count(*) FROM (
   (SELECT * FROM nba.bridge_shot_event_source_v20 EXCEPT ALL SELECT * FROM archive_link_expected_v20)
   UNION ALL (SELECT * FROM archive_link_expected_v20 EXCEPT ALL SELECT * FROM nba.bridge_shot_event_source_v20))
 UNION ALL SELECT 'v20_public_links_include_native_ids',count(*) FROM (
   (SELECT l.* FROM nba.shot_event_link l SEMI JOIN fixture USING(game_id) EXCEPT ALL SELECT * FROM archive_link_expected_v20)
   UNION ALL (SELECT * FROM archive_link_expected_v20 EXCEPT ALL SELECT l.* FROM nba.shot_event_link l SEMI JOIN fixture USING(game_id)))
 UNION ALL SELECT 'v20_typed_legacy_input_shape',count(*) FROM archive_play_actor_v20 e
 WHERE NOT coalesce(key_valid AND action_number=source_number AND action_number>=0 AND period BETWEEN 1 AND 4
   AND regexp_full_match(source_clock,'(1[0-2]|[0-9]):[0-5][0-9]') AND (source_clock NOT LIKE '12:%' OR source_clock='12:00')
   AND (source_score IS NULL OR regexp_full_match(source_score,'[0-9]+ - [0-9]+')) AND actor_kind<>'unresolved'
   AND (raw_team IS NULL OR game_team IS NOT NULL) AND msg IN (1,2,3,4,5,6,7,8,9,10,12,13),false)
 UNION ALL SELECT 'v20_exact_chart_fixture_and_bounds',count(*) FROM archive_shot_input_v20 s
 LEFT JOIN fixture f USING(game_id) LEFT JOIN nba.fact_game g USING(game_id)
 WHERE NOT coalesce(s.key_valid AND s.action_number=s.source_number AND s.day=f.day AND s.home_label=f.home AND s.away_label=f.away
   AND strftime(g.game_date,'%Y%m%d')=s.day AND g.season_type='Regular' AND (g.home_score,g.away_score)=(f.home_score,f.away_score)
   AND s.attempted_flag=1 AND s.shot_type IN ('2PT Field Goal','3PT Field Goal') AND s.made_flag IN (0,1)
   AND s.outcome=CASE s.made_flag WHEN 1 THEN 'Made Shot' ELSE 'Missed Shot' END
   AND s.period BETWEEN 1 AND 4 AND s.clock_seconds BETWEEN 0 AND 59 AND s.clock_minutes>=0
   AND 60*s.clock_minutes+s.clock_seconds BETWEEN 0 AND 720
   AND s.distance_feet BETWEEN 0 AND 110 AND s.x BETWEEN -500 AND 500 AND s.y BETWEEN -100 AND 950,false)
 UNION ALL SELECT 'v20_shots_match_exact_play_by_play_ids',count(*) FROM actual_shot s
 FULL JOIN nba.bridge_shot_event_source_v20 l USING(shot_key)
 FULL JOIN (SELECT * FROM actual_event WHERE is_field_goal) e ON (l.game_id,l.action_number)=(e.game_id,e.action_number)
 WHERE s.shot_key IS NULL OR l.shot_key IS NULL OR e.action_number IS NULL OR
       (s.game_id,s.team_id,s.person_id,s.period,s.seconds_remaining,s.shot_value,s.made)
       IS DISTINCT FROM (e.game_id,e.team_id,e.person_id,e.period,e.seconds_remaining,e.field_goal_value,e.shot_made)
 UNION ALL SELECT 'v20_player_shooting_reconciles',count(*) FROM box_players p
 FULL JOIN (SELECT * FROM shot_totals WHERE person_id IS NOT NULL) s USING(game_id,team_id,person_id)
 WHERE p.person_id IS NULL OR
  (p.fga,p.fgm,p.fg3a,p.fg3m) IS DISTINCT FROM (coalesce(s.fga,0),coalesce(s.fgm,0),coalesce(s.fg3a,0),coalesce(s.fg3m,0))
 UNION ALL SELECT 'v20_team_shooting_reconciles',abs(4-count(*))+count(*) FILTER(
  WHERE (b.fga,b.fgm,b.fg3a,b.fg3m,b.fta,b.ftm,b.pts) IS DISTINCT FROM
        (s.fga,s.fgm,s.fg3a,s.fg3m,t.fta,t.ftm,2*s.fgm+s.fg3m+t.ftm) OR t.malformed IS DISTINCT FROM 0)
 FROM box_teams b FULL JOIN (SELECT * FROM shot_totals WHERE person_id IS NULL) s USING(game_id,team_id)
 FULL JOIN ft_totals t USING(game_id,team_id)
 UNION ALL SELECT 'v20_native_period_end_scores_match_curated',abs(8-count(*))+count(*) FILTER(
  WHERE NOT coalesce(p.period BETWEEN 1 AND 4 AND p.starts=1 AND p.ends=1
   AND (CASE WHEN p.period=1 THEN p.start_home IS NULL AND p.start_away IS NULL
             ELSE (p.start_home,p.start_away)=(p.before_home,p.before_away) END)
   AND (p.end_home-p.before_home,p.end_away-p.before_away)=(h.points,a.points)
   AND (p.period<>4 OR (p.end_home,p.end_away)=(g.home_score,g.away_score)),false))
 FROM periods p JOIN nba.fact_game g USING(game_id)
 LEFT JOIN nba.fact_game_period_score h ON (p.game_id,g.home_team_id,p.period)=(h.game_id,h.team_id,h.period_number)
 LEFT JOIN nba.fact_game_period_score a ON (p.game_id,g.away_team_id,p.period)=(a.game_id,a.team_id,a.period_number)
 UNION ALL SELECT 'v20_no_comparison_only_scope_or_conflicting_origins',count(*) FROM audit.event_source_payload_v20 p
 ANTI JOIN audit.event_archive_v20 a USING(game_id,domain)
 UNION ALL SELECT 'v20_archive_manifest_complete_and_internal',abs(4-count(*))+count(*) FILTER(
 WHERE a.redistribution_cleared OR f.game_id IS NULL OR NOT regexp_full_match(a.sha256,'[a-f0-9]{64}')
  OR a.expected_rows IS DISTINCT FROM CASE a.domain WHEN 'events' THEN f.events WHEN 'shotdetail' THEN f.shots END)
 FROM audit.event_archive_v20 a LEFT JOIN fixture f USING(game_id)
)
SELECT check_name,violations FROM checks ORDER BY check_name;
