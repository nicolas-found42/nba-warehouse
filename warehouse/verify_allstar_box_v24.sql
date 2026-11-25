WITH expected_players(person_id,team_id) AS (VALUES
 (76243,1610616833),(600003,1610616833),(78076,1610616833),(76773,1610616833),(76704,1610616833),
 (77169,1610616833),(77429,1610616833),(77537,1610616833),(78117,1610616833),(78126,1610616833),
 (76514,1610616834),(76744,1610616834),(77089,1610616834),(77867,1610616834),(600012,1610616834),
 (77967,1610616834),(77480,1610616834),(77853,1610616834),(78278,1610616834),(78453,1610616834)),
actual_scope AS (SELECT person_id,team_id FROM audit.allstar_player_receipt_v24 WHERE game_id='0035300004'),
sides AS (
 SELECT p.team_id,count(*) AS players,sum(p.reported_num_minutes) AS player_minutes,sum(p.points) AS points,
        sum(p.fgm) AS fgm,sum(p.fga) AS fga,sum(p.ftm) AS ftm,sum(p.fta) AS fta,sum(p.reb) AS reb,sum(p.assists) AS ast
 FROM nba.fact_player_game_stats p WHERE p.game_id='0035300004' GROUP BY 1
), checks(check_name,violations) AS (
 SELECT 'v24_exact_person_side_scope',count(*) FROM (
  (SELECT * FROM expected_players EXCEPT ALL SELECT * FROM actual_scope)
  UNION ALL (SELECT * FROM actual_scope EXCEPT ALL SELECT * FROM expected_players)
  UNION ALL (SELECT person_id,team_id FROM audit.allstar_player_receipt_v24 WHERE game_id<>'0035300004'))
 UNION ALL SELECT 'v24_exact_player_projection',count(*) FROM (
  (SELECT * FROM nba.fact_player_game_stats WHERE game_id='0035300004' EXCEPT ALL SELECT * FROM audit.allstar_player_publication_v24)
  UNION ALL (SELECT * FROM audit.allstar_player_publication_v24 EXCEPT ALL
     SELECT * FROM nba.fact_player_game_stats WHERE game_id='0035300004'))
 UNION ALL SELECT 'v24_player_observed_fields_and_precision',count(*)
 FROM audit.allstar_player_receipt_v24 a LEFT JOIN nba.fact_player_game_stats p USING(game_id,person_id)
 LEFT JOIN nba.dim_person x ON x.person_id=a.person_id LEFT JOIN nba.bridge_game_team b ON (b.game_id,b.team_id)=(a.game_id,a.team_id)
 WHERE (p.team_id,p.opponent_team_id,p.is_home,p.is_win,p.participation_status,p.reported_num_minutes,
        p.points,p.fgm,p.fga,p.ftm,p.fta,p.reb,p.assists,x.display_name)
  IS DISTINCT FROM (a.team_id,CASE b.side WHEN 'home' THEN 1610616834 ELSE 1610616833 END,b.side='home',b.side='home','played',
        a.whole_minutes,a.pts,a.fgm,a.fga,a.ftm,a.fta,a.reb,a.ast,a.canonical_name)
  OR p.num_minutes IS NOT NULL OR p.starting_position IS NOT NULL OR p.blocks IS NOT NULL OR p.steals IS NOT NULL
  OR p.turnovers IS NOT NULL OR p.oreb IS NOT NULL OR p.dreb IS NOT NULL OR p.fg3a IS NOT NULL OR p.fg3m IS NOT NULL
 UNION ALL SELECT 'v24_player_foul_disposition',count(*)
 FROM audit.allstar_player_receipt_v24 a LEFT JOIN nba.fact_player_game_stats p USING(game_id,person_id)
 WHERE (a.paper_pf<>a.compiler_pf) IS DISTINCT FROM (a.person_id=76704)
   OR (a.person_id=76704 AND (a.paper_pf,a.compiler_pf)<>(6,4))
   OR (a.foul_status,p.fouls_personal) IS DISTINCT FROM
     (CASE WHEN a.person_id=76704 THEN 'held_disputed' ELSE 'sources_agree' END,
      CASE WHEN a.person_id<>76704 THEN a.paper_pf END)
 UNION ALL SELECT 'v24_players_reconcile_to_retained_team_boxes',count(*)
 FROM sides s FULL JOIN (SELECT * FROM nba.fact_team_game_stats WHERE game_id='0035300004') t USING(team_id)
 WHERE (s.players,s.player_minutes,s.points,s.fgm,s.fga,s.ftm,s.fta,s.reb,s.ast)
  IS DISTINCT FROM (10,t.min,t.pts,t.fgm,t.fga,t.ftm,t.fta,t.reb,t.ast)
 UNION ALL SELECT 'v24_player_box_availability',count(*)
 FROM (VALUES ('0035300004')) AS s(game_id) LEFT JOIN audit.player_box_availability_v24 a USING(game_id)
 LEFT JOIN audit.player_box_availability_v17 p USING(game_id)
 WHERE (a.candidate_rows,a.published_rows,a.status,a.held_field_cells,p.published_rows,p.status)
  IS DISTINCT FROM (20,20,'played_box_recovered_with_field_hold',1,0,'source_unavailable')
)
SELECT * FROM checks ORDER BY check_name;
