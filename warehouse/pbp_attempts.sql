-- Backfill official action-grain attempts, not historical shot-chart coordinates.
CREATE TABLE audit.pbp_attempt_scope(game_id VARCHAR PRIMARY KEY);
CREATE TABLE audit.pbp_attempt_input(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL CHECK(regexp_full_match(sha256,'[a-f0-9]{64}')));

CREATE VIEW audit.pbp_attempt_candidate AS
SELECT e.game_id,e.action_number,e.team_id,e.person_id,e.period,e.clock_iso,e.seconds_remaining,
       CASE e.source_action WHEN '2pt' THEN 2 ELSE 3 END::SMALLINT AS shot_value,
       CASE WHEN e.shot_made IS NOT NULL THEN e.shot_made WHEN e.description ILIKE '%missed%' THEN false END AS made,
       e.source_action,
       CASE WHEN e.is_field_goal THEN 'explicit_field_goal' ELSE 'missed_shot_action' END AS evidence_kind,
       coalesce(p.team_id=e.team_id AND e.actor_kind='person' AND e.clock_iso IS NOT NULL AND
          e.period BETWEEN 1 AND o.last_period AND isfinite(e.seconds_remaining)
          AND e.seconds_remaining BETWEEN 0 AND CASE WHEN e.period<=4 THEN 720 ELSE 300 END AND
          CASE WHEN e.action_kind='field_goal' THEN e.is_field_goal AND e.shot_made IS NOT NULL AND
               e.field_goal_value=CASE e.source_action WHEN '2pt' THEN 2 ELSE 3 END
          ELSE e.is_field_goal IS NOT TRUE AND e.shot_made IS NULL AND e.description ILIKE '%missed%' END,false) AS valid
FROM nba.fact_play_event e JOIN audit.pbp_attempt_scope s USING(game_id)
LEFT JOIN nba.game_overtime o USING(game_id)
LEFT JOIN nba.fact_player_game_stats p ON (p.game_id,p.person_id)=(e.game_id,e.person_id)
WHERE e.source_action IN ('2pt','3pt') AND e.action_kind IN ('field_goal','unclassified_shot_action');

CREATE VIEW audit.pbp_attempt_player_reconciliation AS
WITH attempts AS (
 SELECT game_id,team_id,person_id,count(*) AS fga,count(*) FILTER(WHERE made) AS fgm,
       count(*) FILTER(WHERE shot_value=3) AS fg3a,count(*) FILTER(WHERE shot_value=3 AND made) AS fg3m
 FROM audit.pbp_attempt_candidate WHERE valid GROUP BY 1,2,3
), box AS (
 SELECT p.game_id,p.team_id,p.person_id,p.fga,p.fgm,p.fg3a,p.fg3m
 FROM nba.fact_player_game_stats p SEMI JOIN audit.pbp_attempt_scope s USING(game_id)
)
SELECT game_id,team_id,person_id,b.fga AS box_fga,b.fgm AS box_fgm,b.fg3a AS box_fg3a,b.fg3m AS box_fg3m,
       coalesce(a.fga,0) AS event_fga,coalesce(a.fgm,0) AS event_fgm,coalesce(a.fg3a,0) AS event_fg3a,coalesce(a.fg3m,0) AS event_fg3m,
       (b.fga,b.fgm,b.fg3a,b.fg3m) IS NOT DISTINCT FROM
       (coalesce(a.fga,0),coalesce(a.fgm,0),coalesce(a.fg3a,0),coalesce(a.fg3m,0)) AS matches
FROM box b FULL JOIN attempts a USING(game_id,team_id,person_id);

CREATE VIEW audit.pbp_attempt_team_reconciliation AS
WITH attempts AS (
 SELECT game_id,team_id,count(*) AS fga,count(*) FILTER(WHERE made) AS fgm,
       count(*) FILTER(WHERE shot_value=3) AS fg3a,count(*) FILTER(WHERE shot_value=3 AND made) AS fg3m
 FROM audit.pbp_attempt_candidate WHERE valid GROUP BY 1,2
), box AS (
 SELECT p.game_id,p.team_id,p.fga,p.fgm,p.fg3a,p.fg3m
 FROM nba.fact_team_game_stats p SEMI JOIN audit.pbp_attempt_scope s USING(game_id)
)
SELECT game_id,team_id,b.fga AS box_fga,b.fgm AS box_fgm,b.fg3a AS box_fg3a,b.fg3m AS box_fg3m,
       a.fga AS event_fga,a.fgm AS event_fgm,a.fg3a AS event_fg3a,a.fg3m AS event_fg3m,
       coalesce(b.fga=a.fga AND b.fgm=a.fgm AND b.fg3a=a.fg3a AND b.fg3m=a.fg3m,false) AS matches
FROM box b FULL JOIN attempts a USING(game_id,team_id);

CREATE VIEW audit.pbp_attempt_coverage AS
WITH ends AS (
 SELECT e.game_id,e.period,count(*) AS marker_count,max(reported_home_score) AS h,max(reported_away_score) AS a
 FROM nba.fact_play_event e SEMI JOIN audit.pbp_attempt_scope s USING(game_id)
 WHERE source_action='period' AND source_subtype='end' AND seconds_remaining=0 GROUP BY 1,2
), final_evidence AS (
 SELECT e.game_id,count(*) AS period_count,min(period) AS first_period,max(period) AS last_period,
        max(marker_count) AS max_markers,list(h ORDER BY period DESC)[1] AS h,list(a ORDER BY period DESC)[1] AS a
 FROM ends e GROUP BY 1
), candidates AS (
 SELECT game_id,count(*) AS attempts,count(*) FILTER(WHERE NOT valid) AS invalid FROM audit.pbp_attempt_candidate GROUP BY 1
), team AS (
 SELECT game_id,count(*) AS teams,count(*) FILTER(WHERE NOT matches) AS mismatches
 FROM audit.pbp_attempt_team_reconciliation GROUP BY 1
), player AS (
 SELECT game_id,count(*) FILTER(WHERE NOT matches) AS mismatches FROM audit.pbp_attempt_player_reconciliation GROUP BY 1
)
SELECT s.game_id, coalesce(c.attempts,0) AS candidate_attempts,coalesce(c.invalid,0) AS invalid_attempts,
       coalesce(t.mismatches,0) AS team_box_mismatches,coalesce(p.mismatches,0) AS player_box_mismatches,
       CASE WHEN c.attempts IS NULL THEN 'no_event_attempts'
            WHEN c.invalid>0 THEN 'invalid_event_attempts'
            WHEN NOT coalesce(f.period_count=o.last_period AND f.first_period=1 AND f.last_period=o.last_period AND
                 f.max_markers=1 AND f.h=g.home_score AND f.a=g.away_score AND g.result_status='final',false) THEN 'boundary_or_final_disagrees'
            WHEN t.teams IS DISTINCT FROM 2 OR t.mismatches>0 OR p.mismatches IS DISTINCT FROM 0 THEN 'box_attempts_disagree'
            ELSE 'published_box_reconciled_pbp' END AS disposition
FROM audit.pbp_attempt_scope s JOIN nba.fact_game g USING(game_id) LEFT JOIN nba.game_overtime o USING(game_id)
LEFT JOIN final_evidence f USING(game_id) LEFT JOIN candidates c USING(game_id)
LEFT JOIN team t USING(game_id) LEFT JOIN player p USING(game_id);

CREATE TABLE nba.fact_pbp_field_goal_attempt (
 game_id VARCHAR,action_number INTEGER,team_id BIGINT NOT NULL,person_id BIGINT NOT NULL,
 period SMALLINT NOT NULL CHECK(period>0),clock_iso VARCHAR NOT NULL,seconds_remaining DOUBLE NOT NULL CHECK(seconds_remaining>=0),
 shot_value SMALLINT NOT NULL CHECK(shot_value IN (2,3)),made BOOLEAN NOT NULL,source_action VARCHAR NOT NULL,
 evidence_kind VARCHAR NOT NULL CHECK(evidence_kind IN ('explicit_field_goal','missed_shot_action')),
 PRIMARY KEY(game_id,action_number),FOREIGN KEY(game_id,action_number) REFERENCES nba.fact_play_event(game_id,action_number),
 FOREIGN KEY(game_id,team_id) REFERENCES nba.bridge_game_team(game_id,team_id),
 FOREIGN KEY(game_id,person_id) REFERENCES nba.fact_player_game_stats(game_id,person_id)
);

CREATE VIEW nba.field_goal_attempt AS
SELECT shot_key AS attempt_key,game_id,team_id,person_id,period,seconds_remaining::DOUBLE AS seconds_remaining,
       NULL::VARCHAR AS clock_iso,shot_value,made,'nba_shot_chart' AS attempt_source,
       NULL::INTEGER AS action_number,source_action,geometry_status,distance_feet,x_tenths_of_foot,y_tenths_of_foot
FROM nba.fact_shot
UNION ALL
SELECT unhex(sha256('nba_play_by_play:'||game_id||':'||action_number::VARCHAR)),game_id,team_id,person_id,period,
       seconds_remaining,clock_iso,shot_value,made,'nba_play_by_play',action_number,source_action,'unavailable',NULL,NULL,NULL
FROM nba.fact_pbp_field_goal_attempt;
