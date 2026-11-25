-- Raw values are preserved in the audit payload. Normalize explicitly for each fact.
CREATE OR REPLACE TEMP VIEW archive_play_input_v20 AS
SELECT r.game_id,r.action_number,payload->>'GAME_ID' AS raw_game_id,
       (payload->>'EVENTNUM')::INTEGER AS source_number,
       (payload->>'EVENTMSGTYPE')::INTEGER AS msg,(payload->>'EVENTMSGACTIONTYPE')::INTEGER AS detail,
       (payload->>'PERIOD')::SMALLINT AS period,(payload->>'PCTIMESTRING') AS source_clock,
       (payload->>'PERSON1TYPE')::INTEGER AS actor_code,
       nullif((payload->>'PLAYER1_ID')::BIGINT,0) AS raw_person,
       nullif(payload->>'PLAYER1_TEAM_ID','')::BIGINT AS raw_team,
       concat_ws(' | ',nullif(payload->>'HOMEDESCRIPTION',''),nullif(payload->>'NEUTRALDESCRIPTION',''),
                       nullif(payload->>'VISITORDESCRIPTION','')) AS description,
       nullif(payload->>'SCORE','') AS source_score,
       (payload->>'GAME_ID') IN ('20600975','20700753') AND lpad(payload->>'GAME_ID',10,'0')=r.game_id AS key_valid
FROM audit.event_source_payload_v20 r WHERE domain='events';

CREATE OR REPLACE TEMP VIEW archive_play_actor_v20 AS
SELECT e.*,p.team_id AS roster_team,bt.team_id AS game_team,t.team_id AS team_actor,
       CASE WHEN e.raw_person IS NULL THEN 'not_reported'
            WHEN p.person_id IS NOT NULL AND t.team_id IS NULL AND (e.raw_team IS NULL OR e.raw_team=p.team_id) THEN 'person'
            WHEN t.team_id IS NOT NULL AND p.person_id IS NULL AND e.msg IN (4,5,6,7,9)
                 AND (e.raw_team IS NULL OR e.raw_team=t.team_id) THEN 'team'
            ELSE 'unresolved' END AS actor_kind
FROM archive_play_input_v20 e
LEFT JOIN nba.fact_player_game_stats p ON (e.game_id,e.raw_person)=(p.game_id,p.person_id)
LEFT JOIN nba.bridge_game_team bt ON (e.game_id,e.raw_team)=(bt.game_id,bt.team_id)
LEFT JOIN nba.bridge_game_team t ON (e.game_id,e.raw_person)=(t.game_id,t.team_id);

CREATE OR REPLACE TEMP VIEW archive_play_expected_v20 AS
SELECT e.game_id,e.action_number,e.period,
       'PT'||lpad(split_part(e.source_clock,':',1),2,'0')||'M'||split_part(e.source_clock,':',2)||'S' AS clock_iso,
       (60*split_part(e.source_clock,':',1)::INTEGER+split_part(e.source_clock,':',2)::INTEGER)::DOUBLE AS seconds_remaining,
       CASE msg WHEN 1 THEN 'field_goal' WHEN 2 THEN 'field_goal' WHEN 3 THEN 'free_throw'
                WHEN 4 THEN 'rebound' WHEN 5 THEN 'turnover' WHEN 6 THEN 'foul' WHEN 7 THEN 'violation'
                WHEN 8 THEN 'substitution' WHEN 9 THEN 'timeout' WHEN 10 THEN 'jump_ball'
                WHEN 12 THEN 'period' WHEN 13 THEN 'period' END AS action_kind,
       CASE msg WHEN 1 THEN 'Made Shot' WHEN 2 THEN 'Missed Shot' WHEN 3 THEN 'Free Throw'
                WHEN 4 THEN 'Rebound' WHEN 5 THEN 'Turnover' WHEN 6 THEN 'Foul' WHEN 7 THEN 'Violation'
                WHEN 8 THEN 'Substitution' WHEN 9 THEN 'Timeout' WHEN 10 THEN 'Jump Ball'
                WHEN 12 THEN 'period' WHEN 13 THEN 'period' END AS source_action,
       CASE msg WHEN 12 THEN 'start' WHEN 13 THEN 'end' ELSE e.detail::VARCHAR END AS source_subtype,
       nullif(e.description,'') AS description,
       CASE e.actor_kind WHEN 'person' THEN roster_team WHEN 'team' THEN team_actor ELSE game_team END AS team_id,
       CASE WHEN e.actor_kind='person' THEN e.raw_person END AS person_id,e.actor_kind,msg IN (1,2) AS is_field_goal,
       CASE WHEN msg IN (1,2) THEN CASE WHEN contains(e.description,'3PT') THEN 3 ELSE 2 END END::SMALLINT AS field_goal_value,
       CASE WHEN msg IN (1,2) THEN msg=1 END AS shot_made,
       CASE WHEN source_score IS NOT NULL THEN trim(split_part(source_score,'-',2))::INTEGER END AS reported_home_score,
       CASE WHEN source_score IS NOT NULL THEN trim(split_part(source_score,'-',1))::INTEGER END AS reported_away_score
FROM archive_play_actor_v20 e;

CREATE OR REPLACE TEMP VIEW archive_shot_input_v20 AS
SELECT r.game_id,r.action_number,payload->>'GAME_ID' AS raw_game_id,
       (payload->>'GAME_EVENT_ID')::INTEGER AS source_number,(payload->>'TEAM_ID')::BIGINT AS team_id,
       (payload->>'PLAYER_ID')::BIGINT AS person_id,(payload->>'PERIOD')::SMALLINT AS period,
       (payload->>'MINUTES_REMAINING')::INTEGER AS clock_minutes,(payload->>'SECONDS_REMAINING')::INTEGER AS clock_seconds,
       (payload->>'SHOT_TYPE') AS shot_type,(payload->>'EVENT_TYPE') AS outcome,
       (payload->>'SHOT_MADE_FLAG')::INTEGER AS made_flag,(payload->>'SHOT_ATTEMPTED_FLAG')::INTEGER AS attempted_flag,
       (payload->>'ACTION_TYPE') AS source_action,(payload->>'SHOT_ZONE_BASIC') AS source_zone_basic,
       (payload->>'SHOT_ZONE_AREA') AS source_zone_area,(payload->>'SHOT_ZONE_RANGE') AS source_zone_range,
       (payload->>'SHOT_DISTANCE')::SMALLINT AS distance_feet,(payload->>'LOC_X')::SMALLINT AS x,
       (payload->>'LOC_Y')::SMALLINT AS y,(payload->>'GAME_DATE') AS day,(payload->>'HTM') AS home_label,(payload->>'VTM') AS away_label,
       (payload->>'GAME_ID') IN ('20600975','20700753') AND lpad(payload->>'GAME_ID',10,'0')=r.game_id AS key_valid
FROM audit.event_source_payload_v20 r WHERE domain='shotdetail';

CREATE OR REPLACE TEMP VIEW archive_shot_expected_v20 AS
SELECT unhex(sha256('nba_stats_shotdetail:'||game_id||':'||action_number::VARCHAR)) AS shot_key,
       game_id,team_id,person_id,period,(clock_minutes*60+clock_seconds)::INTEGER AS seconds_remaining,
       CASE shot_type WHEN '2PT Field Goal' THEN 2 WHEN '3PT Field Goal' THEN 3 END::SMALLINT AS shot_value,
       made_flag=1 AS made,source_action,source_zone_basic,source_zone_area,source_zone_range,
       CASE WHEN NOT (x=0 AND y=0 AND distance_feet=0) THEN distance_feet END AS distance_feet,
       CASE WHEN NOT (x=0 AND y=0 AND distance_feet=0) THEN x END AS x_tenths_of_foot,
       CASE WHEN NOT (x=0 AND y=0 AND distance_feet=0) THEN y END AS y_tenths_of_foot,
       CASE WHEN x=0 AND y=0 AND distance_feet=0 THEN 'ambiguous_zero' ELSE 'reported' END AS geometry_status
FROM archive_shot_input_v20;

CREATE OR REPLACE TEMP VIEW archive_link_expected_v20 AS
SELECT unhex(sha256('nba_stats_shotdetail:'||game_id||':'||action_number::VARCHAR)) AS shot_key,
       game_id,action_number,'nba_stats_game_event_id' AS match_method
FROM archive_shot_input_v20;
