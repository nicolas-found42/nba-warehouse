-- Offline staging of SportsDataverse releases (ESPN observations, not independent corroboration).
CREATE TEMP TABLE candidate AS
SELECT s.*,x.nba_game_id AS gid,h.nba_team_id::BIGINT AS hid,a.nba_team_id::BIGINT AS aid,
       x.game_date AS xdate,x.home_espn_team_id AS xhome,x.away_espn_team_id AS xaway,
       try_cast(x.nba_home_team_id AS BIGINT) AS xhid,try_cast(x.nba_away_team_id AS BIGINT) AS xaid,
       x.match_method AS game_match, x.season AS xseason,
       s.notes_headline='NBA Cup - Group Play' AS cup_group,
       cast(s.date AS TIMESTAMPTZ) AT TIME ZONE 'America/New_York' AS tip_et,
       CASE left(x.nba_game_id,3) WHEN '002' THEN 'Regular' WHEN '004' THEN 'Playoffs'
         WHEN '005' THEN 'PlayIn' WHEN '006' THEN 'Cup' END AS phase
FROM source_schedule s LEFT JOIN game_crosswalk x ON s.id::VARCHAR=x.espn_game_id
LEFT JOIN source_team_map h ON s.home_id=h.espn_team_id
LEFT JOIN source_team_map a ON s.away_id=a.espn_team_id
WHERE s.game_date <= (SELECT cutoff FROM config) AND s.status_type_completed;

CREATE TEMP TABLE sides AS
SELECT gid,id,game_date,season_type,hid AS team_id,aid AS opponent_id,home_id AS provider_team,
       away_id AS provider_opponent,home_score AS score,away_score AS opponent_score,'home' AS side, status_period::INT AS periods
FROM candidate UNION ALL
SELECT gid,id,game_date,season_type,aid,hid,away_id,home_id,away_score,home_score,'away',status_period::INT
FROM candidate;

CREATE TEMP TABLE map_person AS
SELECT DISTINCT espn_athlete_id::INT AS athlete,nba_player_id::BIGINT AS person,nba_player_name AS nba_name,
       espn_full_name AS espn_name, 'sdv_player_crosswalk' AS identity_source
FROM source_player_map WHERE nba_player_id IS NOT NULL AND match_method='exact_name' AND match_confidence=1
UNION ALL SELECT 5217746,1642905,'Yang Hansen','Yang Hansen','https://www.nba.com/stats/player/1642905'
WHERE NOT EXISTS (SELECT 1 FROM source_player_map WHERE espn_athlete_id='5217746' AND nba_player_id IS NOT NULL);

CREATE TEMP TABLE player_obs AS
SELECT b.*,s.gid,s.team_id AS nba_team,s.opponent_id AS nba_opponent,s.side,s.score,s.opponent_score,
       s.game_date AS canonical_date,s.season_type AS canonical_phase,p.person,p.nba_name,p.espn_name,p.identity_source
FROM source_player b JOIN candidate g ON b.game_id=g.id
LEFT JOIN sides s ON (b.game_id,b.team_id)=(s.id,s.provider_team)
LEFT JOIN map_person p ON b.athlete_id=p.athlete;

CREATE TEMP TABLE team_obs AS
SELECT b.*,s.gid,s.team_id AS nba_team,s.side,s.score,s.opponent_score,s.periods,
       s.game_date AS canonical_date,s.season_type AS canonical_phase,s.provider_opponent
FROM source_team b JOIN candidate g ON b.game_id=g.id
LEFT JOIN sides s ON (b.game_id,b.team_id)=(s.id,s.provider_team);

CREATE TEMP TABLE play_obs AS
SELECT p.*,g.gid,g.game_date AS canonical_date,g.season_type AS canonical_phase,g.home_id AS expected_home,
       g.away_id AS expected_away,s.team_id AS nba_team, b.person,
       p.shooting_play AND p.points_attempted IN (2,3) AS is_goal,
       p.clock_minutes*60+p.clock_seconds AS seconds_left,
       printf('PT%dM%02d%sS',p.clock_minutes,trunc(p.clock_seconds)::INT,
         CASE WHEN p.clock_seconds=trunc(p.clock_seconds) THEN ''
              ELSE regexp_extract(p.clock_seconds::VARCHAR,'[.][0-9]+$',0) END) AS iso_clock
FROM source_play p JOIN candidate g ON p.game_id=g.id
LEFT JOIN sides s ON (p.game_id,p.team_id)=(s.id,s.provider_team)
LEFT JOIN player_obs b ON (p.game_id,p.team_id,p.athlete_id_1)=(b.game_id,b.team_id,b.athlete_id)
 AND b.did_not_play IS FALSE AND b.minutes IS NOT NULL;

-- Scores are reconstructed on nearly all plays by the provider. Check each period boundary
-- against the separate linescore, and keep failed streams in the audit layer only.
CREATE TEMP TABLE play_boundary_conflict AS
WITH observed AS (
  SELECT gid,period_number,
         max(home_score) FILTER(WHERE type_text='End Period') AS home,
         max(away_score) FILTER(WHERE type_text='End Period') AS away,
         count(*) FILTER(WHERE type_text='End Period' AND seconds_left=0) AS period_markers,
         count(*) FILTER(WHERE type_text='End Period') AS all_period_markers,
         count(*) FILTER(WHERE type_text='End Game' AND seconds_left=0) AS game_markers,
         count(*) FILTER(WHERE type_text='End Game') AS all_game_markers,
         max(game_play_number) FILTER(WHERE type_text='End Period') AS marker_order,
         max(game_play_number) AS last_order,
         arg_max(home_score,game_play_number) AS last_home,arg_max(away_score,game_play_number) AS last_away
  FROM play_obs GROUP BY 1,2
), expected AS (
  SELECT h.game_id AS gid,h.period_number,g.status_period=h.period_number AS game_end,
      sum(h.points) OVER(PARTITION BY h.game_id ORDER BY h.period_number) AS home,
      sum(a.points) OVER(PARTITION BY h.game_id ORDER BY h.period_number) AS away
  FROM period_cells h JOIN candidate g ON h.game_id=g.gid AND h.team_id=g.hid
  JOIN period_cells a ON (h.game_id,h.period_number,g.aid)=(a.game_id,a.period_number,a.team_id)
)
SELECT coalesce(e.gid,o.gid) AS game_id,coalesce(e.period_number,o.period_number) AS period_number,
       e.home AS expected_home,e.away AS expected_away,o.home AS observed_home,o.away AS observed_away
FROM expected e FULL JOIN observed o USING(gid,period_number)
WHERE (e.home,e.away) IS DISTINCT FROM (o.home,o.away)
   OR (o.period_markers,o.all_period_markers,o.game_markers,o.all_game_markers)
      IS DISTINCT FROM (1,1,e.game_end::INT,e.game_end::INT)
   OR o.marker_order+e.game_end::INT IS DISTINCT FROM o.last_order
   OR (o.home,o.away) IS DISTINCT FROM (o.last_home,o.last_away);

CREATE TEMP TABLE shot_box_conflict AS
WITH shots AS (
 SELECT game_id,team_id,athlete_id_1 AS athlete_id,count(*) AS fga,
        count(*) FILTER(WHERE scoring_play) AS fgm,count(*) FILTER(WHERE points_attempted=3) AS fg3a,
        count(*) FILTER(WHERE points_attempted=3 AND scoring_play) AS fg3m
 FROM play_obs WHERE is_goal GROUP BY 1,2,3
), boxes AS (SELECT * FROM player_obs WHERE NOT did_not_play)
SELECT g.gid AS game_id,coalesce(s.team_id,b.team_id) AS espn_team_id,
       coalesce(s.athlete_id,b.athlete_id) AS espn_athlete_id,
       coalesce(fga,0) AS play_fga,coalesce(fgm,0) AS play_fgm,coalesce(fg3a,0) AS play_fg3a,coalesce(fg3m,0) AS play_fg3m,
       b.field_goals_attempted AS box_fga,b.field_goals_made AS box_fgm,
       b.three_point_field_goals_attempted AS box_fg3a,b.three_point_field_goals_made AS box_fg3m
FROM shots s FULL JOIN boxes b USING(game_id,team_id,athlete_id)
JOIN candidate g ON g.id=coalesce(s.game_id,b.game_id)
WHERE (coalesce(fga,0),coalesce(fgm,0),coalesce(fg3a,0),coalesce(fg3m,0)) IS DISTINCT FROM
      (b.field_goals_attempted,b.field_goals_made,b.three_point_field_goals_attempted,b.three_point_field_goals_made);
