-- Exact legacy scope on the current release. No date-only selection.
CREATE OR REPLACE TEMP VIEW legacy_attempt_scope_expected AS
SELECT g.game_id FROM nba.dim_game g ANTI JOIN (SELECT DISTINCT game_id FROM nba.fact_shot) s USING(game_id)
WHERE (g.season_start,g.season_year,g.season_type,g.result_status)=(2000,'2000-01','Regular','final');

CREATE OR REPLACE TEMP VIEW legacy_period_boundaries AS
WITH markers AS (
 SELECT e.game_id,e.period,
        count(*) FILTER(WHERE source_subtype='start') AS starts,
        count(*) FILTER(WHERE source_subtype='end') AS ends,
        min(action_number) FILTER(WHERE source_subtype='start') AS start_action,
        min(action_number) FILTER(WHERE source_subtype='end') AS end_action,
        min(seconds_remaining) FILTER(WHERE source_subtype='start') AS start_clock,
        min(seconds_remaining) FILTER(WHERE source_subtype='end') AS end_clock,
        min(reported_home_score) FILTER(WHERE source_subtype='start') AS start_home,
        min(reported_away_score) FILTER(WHERE source_subtype='start') AS start_away,
        min(reported_home_score) FILTER(WHERE source_subtype='end') AS end_home,
        min(reported_away_score) FILTER(WHERE source_subtype='end') AS end_away
 FROM nba.fact_play_event e SEMI JOIN legacy_attempt_scope_expected s USING(game_id)
 WHERE e.source_action='period' AND e.source_subtype IN ('start','end') GROUP BY 1,2
)
SELECT *,lag(end_home,1,0) OVER (PARTITION BY game_id ORDER BY period) AS prior_home,
         lag(end_away,1,0) OVER (PARTITION BY game_id ORDER BY period) AS prior_away,
         lag(end_action) OVER (PARTITION BY game_id ORDER BY period) AS prior_end_action
FROM markers;

CREATE OR REPLACE TEMP VIEW legacy_attempt_candidates AS
SELECT e.game_id,e.action_number,e.team_id,e.person_id,e.period,e.clock_iso,e.seconds_remaining,
       e.field_goal_value AS shot_value,e.shot_made AS made,e.source_action,'explicit_field_goal' AS evidence_kind,
       coalesce(e.action_kind='field_goal' AND e.actor_kind='person' AND e.is_field_goal AND e.field_goal_value IN (2,3)
          AND p.team_id=e.team_id AND b.team_id IS NOT NULL
          AND e.shot_made=(trim(e.source_action)='Made Shot')
          AND length(e.source_action)=40 AND trim(e.source_action) IN ('Made Shot','Missed Shot')
          AND e.period BETWEEN 1 AND 4 AND e.action_number BETWEEN m.start_action+1 AND m.end_action-1
          AND isfinite(e.seconds_remaining) AND e.seconds_remaining BETWEEN 0 AND 720
          AND regexp_full_match(e.clock_iso,'PT[0-9]+M[0-5][0-9](\.[0-9]+)?S')
          AND abs(e.seconds_remaining-(60*try_cast(regexp_extract(e.clock_iso,'^PT([0-9]+)M',1) AS DOUBLE)
              +try_cast(regexp_extract(e.clock_iso,'M([0-9]+\.?[0-9]*)S$',1) AS DOUBLE)))<1e-7,false) AS valid
FROM nba.fact_play_event e JOIN legacy_attempt_scope_expected s USING(game_id)
LEFT JOIN nba.fact_player_game_stats p USING(game_id,person_id)
LEFT JOIN nba.bridge_game_team b ON (e.game_id,e.team_id)=(b.game_id,b.team_id)
LEFT JOIN legacy_period_boundaries m ON (e.game_id,e.period)=(m.game_id,m.period)
WHERE e.is_field_goal IS TRUE OR e.field_goal_value IN (2,3) OR trim(e.source_action) IN ('Made Shot','Missed Shot');

CREATE OR REPLACE TEMP VIEW legacy_attempt_expected AS
SELECT * EXCLUDE(valid) FROM legacy_attempt_candidates WHERE valid;

CREATE OR REPLACE TEMP VIEW legacy_attempt_player_checks AS
WITH attempts AS (
 SELECT game_id,team_id,person_id,count(*) AS fga,count(*) FILTER(WHERE made) AS fgm,
        count(*) FILTER(WHERE shot_value=3) AS fg3a,count(*) FILTER(WHERE shot_value=3 AND made) AS fg3m
 FROM legacy_attempt_expected GROUP BY 1,2,3
), box AS (
 SELECT game_id,team_id,person_id,fga,fgm,fg3a,fg3m FROM nba.fact_player_game_stats
 SEMI JOIN legacy_attempt_scope_expected USING(game_id)
)
SELECT game_id,team_id,person_id, b.person_id IS NOT NULL AS has_box,
       (b.fga,b.fgm,b.fg3a,b.fg3m) IS NOT DISTINCT FROM
       (coalesce(a.fga,0),coalesce(a.fgm,0),coalesce(a.fg3a,0),coalesce(a.fg3m,0)) AS matches
FROM box b FULL JOIN attempts a USING(game_id,team_id,person_id);

CREATE OR REPLACE TEMP VIEW legacy_attempt_team_checks AS
WITH attempts AS (
 SELECT game_id,team_id,count(*) AS fga,count(*) FILTER(WHERE made) AS fgm,
        count(*) FILTER(WHERE shot_value=3) AS fg3a,count(*) FILTER(WHERE shot_value=3 AND made) AS fg3m
 FROM legacy_attempt_expected GROUP BY 1,2
), box AS (
 SELECT game_id,team_id,fga,fgm,fg3a,fg3m FROM nba.fact_team_game_stats
 SEMI JOIN legacy_attempt_scope_expected USING(game_id)
)
SELECT game_id,team_id, b.team_id IS NOT NULL AS has_box,
       coalesce(b.fga=a.fga AND b.fgm=a.fgm AND b.fg3a=a.fg3a AND b.fg3m=a.fg3m,false) AS matches
FROM box b FULL JOIN attempts a USING(game_id,team_id);
