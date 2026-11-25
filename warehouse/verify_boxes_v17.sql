-- Fast, self-contained checks. Offline input replay independently re-derives these ledgers.
WITH player_totals AS (
 SELECT p.game_id,p.team_id,count(*) AS n,sum(round(p.num_minutes*60)) AS seconds,sum(p.points) AS points
 FROM nba.fact_player_game_stats p SEMI JOIN audit.player_box_recovery_v17 a USING(game_id)
 GROUP BY 1,2
), checks(check_name,violations) AS (
 SELECT 'v17_box_input_pins', abs(2-count(*))+count(*) FILTER (
   WHERE role NOT IN ('parent_v16','raw_snapshot') OR NOT regexp_full_match(sha256,'[a-f0-9]{64}'))
 FROM audit.box_recovery_input_v17
 UNION ALL SELECT 'v17_team_box_admissions',abs(20-count(*))+abs(10-count(DISTINCT game_id))
 FROM audit.team_box_recovery_v17 WHERE valid AND disposition='partial_traditional_and_situational'
 UNION ALL SELECT 'v17_team_box_exact_values',count(*) FROM (
  (SELECT p.* FROM nba.fact_team_game_stats p SEMI JOIN audit.team_box_recovery_v17 a USING(game_id,team_id)
   EXCEPT ALL SELECT * FROM audit.team_box_publication_v17)
  UNION ALL
  (SELECT * FROM audit.team_box_publication_v17 EXCEPT ALL SELECT p.* FROM nba.fact_team_game_stats p
   SEMI JOIN audit.team_box_recovery_v17 a USING(game_id,team_id)))
 UNION ALL SELECT 'v17_team_box_turnover_namespace',count(*)
 FROM audit.team_box_recovery_v17 a LEFT JOIN nba.fact_team_game_stats p USING(game_id,team_id)
 WHERE p.tov IS DISTINCT FROM a.team_total_turnovers OR a.player_tov IS DISTINCT FROM a.mirror_player_line_turnovers
   OR NOT coalesce(a.team_total_turnovers>=a.player_tov,false) OR p.min IS DISTINCT FROM 5*a.reported_elapsed_minutes
 UNION ALL SELECT 'v17_player_box_dispositions',abs(116-count(*))
   +abs(88-count(*) FILTER(WHERE disposition='published_traditional'))
   +abs(24-count(*) FILTER(WHERE disposition='held_conflicting_minutes'))
   +abs(4-count(*) FILTER(WHERE disposition='held_unknown_person_placeholder'))
 FROM audit.player_box_recovery_v17
 UNION ALL SELECT 'v17_player_box_exact_values',count(*) FROM (
  (SELECT p.* FROM nba.fact_player_game_stats p SEMI JOIN audit.player_box_recovery_v17 a USING(game_id,person_id)
   EXCEPT ALL SELECT * FROM audit.player_box_publication_v17)
  UNION ALL
  (SELECT * FROM audit.player_box_publication_v17 EXCEPT ALL SELECT p.* FROM nba.fact_player_game_stats p
   SEMI JOIN audit.player_box_recovery_v17 a USING(game_id,person_id)))
 UNION ALL SELECT 'v17_player_box_person_status_and_minutes',count(*)
 FROM audit.player_box_recovery_v17 a LEFT JOIN nba.fact_player_game_stats p USING(game_id,person_id)
 LEFT JOIN nba.dim_person x ON x.person_id=p.person_id
 WHERE CASE WHEN a.disposition='published_traditional' THEN
     p.team_id IS DISTINCT FROM a.team_id OR p.num_minutes IS DISTINCT FROM a.minute_seconds/60.0
     OR p.reported_num_minutes IS DISTINCT FROM a.raw_min_mm_ss OR p.is_win IS DISTINCT FROM a.verified_is_win
     OR p.plus_minus IS NOT NULL OR p.participation_status IS DISTINCT FROM 'played'
     OR x.display_name IS DISTINCT FROM a.display_name OR upper(trim(x.display_name)) IN ('','UNKNOWN')
    ELSE p.person_id IS NOT NULL END
 UNION ALL SELECT 'v17_player_box_side_totals',abs(8-count(*))+count(*) FILTER (
   WHERE t.seconds IS DISTINCT FROM 14400 OR t.points IS DISTINCT FROM
      CASE WHEN t.team_id=g.home_team_id THEN g.home_score WHEN t.team_id=g.away_team_id THEN g.away_score END)
 FROM player_totals t JOIN nba.fact_game g USING(game_id)
 UNION ALL SELECT 'v17_player_box_availability',abs(15-count(*))+abs(11-count(*) FILTER(WHERE published_rows=0))
 FROM audit.player_box_availability_v17
)
SELECT check_name,violations FROM checks ORDER BY check_name;
