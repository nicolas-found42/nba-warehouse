-- Read-only reconciliation of the official vectors to the existing provider bridge.
-- Only the league gamebook can authorize the vector. ESPN is a diagnostic control.
CREATE TEMP VIEW raw_gamebook_comparison AS
WITH matched AS (
 SELECT r.game_id,r.espn_game_id,r.home_q,r.away_q,
  (SELECT count(*) FROM raw.main.stg_espn_nba_schedule_crosswalk x
   WHERE x.nba_game_id=r.game_id OR x.espn_game_id=r.espn_game_id::VARCHAR) AS key_rows,
  (SELECT count(*) FROM raw.main.bridge_game_espn_base b
   WHERE b.game_id=r.game_id OR b.espn_game_id=r.espn_game_id) AS bridge_rows,
  x.home_espn_team_id,x.away_espn_team_id,
  coalesce(x.match_method='both' AND x.match_confidence=1 AND x.season_year=2026 AND x.game_date=r.game_date
     AND x.nba_home_team_id=r.home_team_id AND x.nba_away_team_id=r.away_team_id
     AND r.source_url='https://statsdmz.nba.com/pdfs/'||strftime(r.game_date,'%Y%m%d')||'/'||replace(x.nba_game_code,'/','_')||'.pdf'
     AND b.match_method='crosswalk_exact' AND NOT b.is_unresolved AND NOT b.is_ambiguous
     AND b.game_date=r.game_date AND b.home_team_id=r.home_team_id AND b.away_team_id=r.away_team_id
     AND b.home_score=list_sum(r.home_q) AND b.away_score=list_sum(r.away_q),false) AS fixture_agrees,
  (SELECT count(*) FROM raw.main.stg_espn_nba_schedule s WHERE s.espn_game_id=r.espn_game_id) AS schedule_rows,
  EXISTS(SELECT 1 FROM raw.main.stg_espn_nba_schedule s WHERE s.espn_game_id=r.espn_game_id
      AND (s.season_year,s.game_date,s.home_team_id,s.away_team_id,s.home_score,s.away_score)
      = (2026,r.game_date,x.home_espn_team_id,x.away_espn_team_id,list_sum(r.home_q),list_sum(r.away_q))
      AND s.status_type_completed AND s.status_type_name='STATUS_FINAL' AND s.status_period=4 AND s.season_type_id=2) AS schedule_agrees
 FROM audit.official_period_receipt r
 LEFT JOIN raw.main.stg_espn_nba_schedule_crosswalk x ON x.nba_game_id=r.game_id AND x.espn_game_id=r.espn_game_id::VARCHAR
 LEFT JOIN raw.main.bridge_game_espn_base b ON b.game_id=r.game_id AND b.espn_game_id=r.espn_game_id
), plays AS (
 SELECT r.game_id,p.period,p.type_text,p.clock_minutes,p.clock_seconds,p.home_score,p.away_score,p.game_play_number,
  (p.game_date,p.season_year,p.home_team_id,p.away_team_id) IS NOT DISTINCT FROM
  (a.game_date,2026,r.home_espn_team_id,r.away_espn_team_id) AS context_agrees
 FROM matched r JOIN raw.main.stg_espn_nba_pbp p USING(espn_game_id)
 JOIN audit.official_period_receipt a ON a.game_id=r.game_id
), logged AS (
 SELECT game_id,count(*) AS n,bool_and(context_agrees) AS context_agrees,
   count(*) FILTER(WHERE period IS NULL OR period NOT BETWEEN 1 AND 4) AS bad_periods
 FROM plays GROUP BY game_id
), ends AS (
 SELECT game_id,period,count(*) AS n,any_value(home_score) AS h,any_value(away_score) AS a,
        max(game_play_number) AS ordinal
 FROM plays WHERE type_text='End Period' AND clock_minutes=0 AND clock_seconds=0
 GROUP BY game_id,period
), scores AS (
 SELECT *,h-lag(h,1,0) OVER w AS hp,a-lag(a,1,0) OVER w AS ap,lag(ordinal,1,0) OVER w AS prev_ordinal
 FROM ends WINDOW w AS (PARTITION BY game_id ORDER BY period)
), vectors AS (
 SELECT game_id,coalesce(count(*)=4 AND min(period)=1 AND max(period)=4 AND count(hp)=4 AND count(ap)=4
        AND bool_and(n=1 AND hp>=0 AND ap>=0 AND hp+ap>0 AND ordinal>prev_ordinal),false) AS markers_eligible,
        list(hp ORDER BY period) AS home_vector,list(ap ORDER BY period) AS away_vector
 FROM scores GROUP BY game_id
)
SELECT m.game_id,m.espn_game_id,m.key_rows,m.bridge_rows,m.schedule_rows,m.fixture_agrees,m.schedule_agrees,
 coalesce(l.n,0) AS play_rows,v.home_vector,v.away_vector,
 CASE WHEN l.n IS NULL THEN 'no_local_plays'
      WHEN NOT l.context_agrees OR l.bad_periods<>0 OR NOT coalesce(v.markers_eligible,false) THEN 'marker_shape_unusable'
      WHEN (list_sum(v.home_vector),list_sum(v.away_vector)) IS DISTINCT FROM (list_sum(m.home_q),list_sum(m.away_q)) THEN 'pbp_final_disagrees'
      WHEN (v.home_vector,v.away_vector) IS DISTINCT FROM (m.home_q,m.away_q) THEN 'pbp_same_final_split_disagrees'
      ELSE 'pbp_markers_agree' END AS comparison_status
FROM matched m LEFT JOIN logged l USING(game_id) LEFT JOIN vectors v USING(game_id);
