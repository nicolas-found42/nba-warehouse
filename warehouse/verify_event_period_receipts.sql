-- Verify the receipt, the exact period rows, and the event structure independently of the final alone.
WITH expected AS (
 SELECT r.game_id,b.team_id,i AS period_number,CASE b.side WHEN 'home' THEN r.home_periods[i] ELSE r.away_periods[i] END AS points
 FROM audit.event_period_receipt r JOIN nba.bridge_game_team b USING(game_id),LATERAL range(1,len(r.home_periods)+1) n(i)
), actual AS (
 SELECT p.* FROM nba.fact_game_period_score p SEMI JOIN audit.event_period_receipt USING(game_id)
), expected_format AS (
 SELECT game_id,component,stage,fixture_number,1 AS regulation_periods,regulation_seconds,regulation_target,overtime_target,
   CASE component WHEN 'rising_stars' THEN 'target_score' ELSE 'timed' END AS regulation_mode,
   CASE component WHEN 'all_star' THEN 'target_score' END AS overtime_mode
 FROM audit.event_period_receipt
), expected_labeled AS (
 SELECT p.*,CASE WHEN p.period_number=1 THEN 'regulation' ELSE 'overtime' END AS period_phase,
   CASE WHEN p.period_number=1 THEN f.regulation_mode ELSE f.overtime_mode END AS period_mode
 FROM expected p JOIN expected_format f USING(game_id)
), boundaries AS (
 SELECT game_id,period,
   count(*) FILTER(WHERE source_subtype='start') AS n_start,
   count(*) FILTER(WHERE source_subtype='end') AS n_end,
   max(reported_home_score) FILTER(WHERE source_subtype='start') AS hs,
   max(reported_away_score) FILTER(WHERE source_subtype='start') AS aws,
   max(reported_home_score) FILTER(WHERE source_subtype='end') AS he,
   max(reported_away_score) FILTER(WHERE source_subtype='end') AS ae,
   max(seconds_remaining) FILTER(WHERE source_subtype='start') AS start_clock,
   max(seconds_remaining) FILTER(WHERE source_subtype='end') AS end_clock,
   max(action_number) FILTER(WHERE source_subtype='start') AS start_action,
   max(action_number) FILTER(WHERE source_subtype='end') AS end_action
 FROM nba.fact_play_event SEMI JOIN audit.event_period_receipt USING(game_id)
 WHERE source_action='period' AND source_subtype IN ('start','end') GROUP BY 1,2
), expected_boundaries AS (
 SELECT r.game_id,i AS period,1 AS n_start,1 AS n_end,
   coalesce(list_sum(home_periods[:i-1]),0) AS hs,coalesce(list_sum(away_periods[:i-1]),0) AS aws,
   list_sum(home_periods[:i]) AS he,list_sum(away_periods[:i]) AS ae,
   CASE WHEN i=1 THEN coalesce(regulation_seconds,0) ELSE 0 END AS start_clock,0 AS end_clock
 FROM audit.event_period_receipt r,LATERAL range(1,len(r.home_periods)+1) n(i)
), compared AS (
 SELECT * EXCLUDE(start_action,end_action) FROM boundaries WHERE start_action<end_action
), checks AS (
 SELECT 'event_period_receipt_matches_cells' AS check_name,count(*) AS violations FROM (
  (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
  UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
 )
 UNION ALL SELECT 'event_period_boundary_crosscheck',count(*) FROM (
  (SELECT * FROM expected_boundaries EXCEPT ALL SELECT * FROM compared)
  UNION ALL (SELECT * FROM compared EXCEPT ALL SELECT * FROM expected_boundaries)
 )
 UNION ALL SELECT 'event_period_public_format_matches_rules',count(*) FROM (
  (SELECT * FROM expected_format EXCEPT ALL SELECT * FROM nba.event_period_format)
  UNION ALL (SELECT * FROM nba.event_period_format EXCEPT ALL SELECT * FROM expected_format)
 )
 UNION ALL SELECT 'event_period_public_labels_match_rules',count(*) FROM (
  (SELECT * FROM expected_labeled EXCEPT ALL SELECT * FROM nba.event_period_score)
  UNION ALL (SELECT * FROM nba.event_period_score EXCEPT ALL SELECT * FROM expected_labeled)
 )
 UNION ALL SELECT 'event_period_full_edition_keys',count(*) FROM (
  (SELECT game_id FROM audit.event_period_receipt EXCEPT ALL SELECT game_id FROM nba.bridge_event_fixture WHERE edition_key='AS-2026')
  UNION ALL (SELECT game_id FROM nba.bridge_event_fixture WHERE edition_key='AS-2026' EXCEPT ALL SELECT game_id FROM audit.event_period_receipt)
 )
 UNION ALL SELECT 'event_period_fixture_and_result_agree',count(*)
 FROM audit.event_period_receipt r LEFT JOIN nba.fact_game g USING(game_id)
 LEFT JOIN nba.bridge_event_fixture f USING(game_id) LEFT JOIN audit.period_coverage p USING(game_id) LEFT JOIN nba.game_overtime o USING(game_id)
 WHERE (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score,g.result_status,g.counts_toward_regular_season,
        f.edition_key,f.component,f.stage,f.fixture_number,f.neutral_site,p.status,p.selected_source,o.last_period,o.is_overtime)
 IS DISTINCT FROM (r.game_date,r.home_id,r.away_id,list_sum(r.home_periods),list_sum(r.away_periods),'final',false,
        'AS-2026',r.component,r.stage,r.fixture_number,true,'published','event_period_receipt',len(r.home_periods),len(r.home_periods)>1)
 UNION ALL SELECT 'event_period_no_unreceipted_publication',count(*)
 FROM audit.period_coverage p ANTI JOIN audit.event_period_receipt r USING(game_id) WHERE p.selected_source='event_period_receipt'
 UNION ALL SELECT 'event_period_squad_labels_match_ids',count(*)
 FROM (VALUES (1610616859,'Stars'),(1610616860,'Stripes'),(1610616861,'World'),
              (1610616862,'Team Melo'),(1610616863,'Team Vince'),(1610616864,'Team T-Mac'),(1610616865,'Team Austin'))
      e(team_id,display_name) LEFT JOIN nba.dim_team t USING(team_id)
 WHERE t.display_name IS DISTINCT FROM e.display_name
 UNION ALL SELECT 'event_period_rule_and_target_agree',count(*) FROM audit.event_period_receipt
 WHERE NOT coalesce(CASE component
  WHEN 'rising_stars' THEN regulation_seconds IS NULL AND overtime_target IS NULL AND len(home_periods)=1
   AND regulation_target=CASE stage WHEN 'semifinal' THEN 40 WHEN 'final' THEN 25 END
   AND greatest(home_periods[1],away_periods[1])>=regulation_target AND least(home_periods[1],away_periods[1])<regulation_target
  WHEN 'all_star' THEN regulation_seconds=720 AND regulation_target IS NULL AND overtime_target=5
   AND (len(home_periods)=1 OR (len(home_periods)=2 AND home_periods[1]=away_periods[1]
         AND greatest(home_periods[2],away_periods[2])>=5 AND least(home_periods[2],away_periods[2])<5))
  END,false)
 UNION ALL SELECT 'event_period_never_merges_same_pair',CASE WHEN count(*)=2 THEN 0 ELSE 1 END
 FROM audit.event_period_receipt WHERE game_id IN ('0032500021','0032500041') AND home_id=1610616860 AND away_id=1610616859
   AND ((stage='round_robin' AND fixture_number=2 AND evidence_kind='keyed_official_box_and_pbp'
      AND source_url='https://www.nba.com/game/0032500021/box-score')
    OR (stage='final' AND fixture_number=4 AND evidence_kind='scorer_report' AND source_url LIKE '%_STRSTP.pdf'))
)
SELECT check_name,violations FROM checks ORDER BY check_name;
