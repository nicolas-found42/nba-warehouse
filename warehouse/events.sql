-- Event grain follows the existing canonical action key. Never use order_number,
-- clock ordering, or `max(action_number)` as a proxy for number of plays.
CREATE TABLE nba.fact_play_event (
    game_id VARCHAR REFERENCES nba.dim_game(game_id),
    action_number INTEGER CHECK (action_number>=0),
    period SMALLINT NOT NULL CHECK (period>0),
    clock_iso VARCHAR NOT NULL,
    seconds_remaining DOUBLE NOT NULL CHECK (seconds_remaining>=0 AND isfinite(seconds_remaining)),
    action_kind VARCHAR NOT NULL,
    source_action VARCHAR NOT NULL,
    source_subtype VARCHAR,
    description VARCHAR,
    team_id BIGINT,
    person_id BIGINT,
    actor_kind VARCHAR NOT NULL CHECK (actor_kind IN ('person','team','unresolved','not_reported')),
    is_field_goal BOOLEAN,
    field_goal_value SMALLINT CHECK (field_goal_value IN (2,3)),
    shot_made BOOLEAN,
    reported_home_score INTEGER CHECK (reported_home_score>=0),
    reported_away_score INTEGER CHECK (reported_away_score>=0),
    CHECK ((actor_kind='person')=(person_id IS NOT NULL)),
    CHECK (actor_kind<>'team' OR team_id IS NOT NULL),
    CHECK ((field_goal_value IS NULL AND shot_made IS NULL) OR is_field_goal IS TRUE),
    PRIMARY KEY (game_id, action_number),
    FOREIGN KEY (game_id, team_id) REFERENCES nba.bridge_game_team(game_id, team_id),
    FOREIGN KEY (game_id, person_id) REFERENCES nba.fact_player_game_stats(game_id, person_id)
);
CREATE TABLE audit.event_hold (
    game_id VARCHAR, action_number INTEGER, reason VARCHAR NOT NULL,
    PRIMARY KEY (game_id, action_number)
);
CREATE TABLE audit.event_coverage (
    game_id VARCHAR PRIMARY KEY,
    input_events BIGINT NOT NULL CHECK (input_events>=0),
    published_events BIGINT NOT NULL CHECK (published_events>=0),
    held_events BIGINT NOT NULL CHECK (held_events>=0),
    unresolved_actors BIGINT NOT NULL CHECK (unresolved_actors BETWEEN 0 AND published_events),
    CHECK (input_events=published_events+held_events)
);

CREATE TEMP VIEW event_source AS
SELECT e.*,
       CASE WHEN game.game_id IS NULL THEN 'missing_game'
            WHEN e.action_number<0 THEN 'invalid_action_number'
            WHEN e.period IS NULL OR e.period<=0 THEN 'period_unavailable'
            WHEN e.clock IS NULL OR NOT regexp_full_match(e.clock,'PT[0-9]+M[0-5][0-9]([.][0-9]+)?S')
              THEN 'invalid_clock'
            WHEN nullif(trim(e.action_type),'') IS NULL THEN 'missing_action_kind' END AS hold_reason
FROM src.main.fact_pbp_events e LEFT JOIN nba.dim_game game USING (game_id);

INSERT INTO audit.event_hold
SELECT game_id, action_number, hold_reason FROM event_source WHERE hold_reason IS NOT NULL;

INSERT INTO nba.fact_play_event
WITH classified AS (
    SELECT e.*, p.team_id AS box_team_id, bt.team_id AS reported_team_match, team_actor.team_id AS actor_team_id,
           lower(trim(e.action_type)) AS verb,
           trim(replace(lower(trim(e.shot_result)),':','')) AS goal_result
    FROM event_source e
    LEFT JOIN nba.bridge_game_team bt ON (e.game_id,e.team_id)=(bt.game_id,bt.team_id)
    LEFT JOIN nba.bridge_game_team team_actor ON (e.game_id,e.player_id)=(team_actor.game_id,team_actor.team_id)
    LEFT JOIN nba.fact_player_game_stats p ON (e.game_id,e.player_id)=(p.game_id,p.person_id)
    WHERE e.hold_reason IS NULL
), choices AS (
    SELECT *,
       CASE WHEN player_id IS NULL THEN 'not_reported'
            WHEN actor_team_id IS NOT NULL AND is_field_goal IS NOT TRUE AND box_team_id IS NULL
                 AND (reported_team_match IS NULL OR reported_team_match=actor_team_id)
                 AND verb IN ('rebound','timeout','turnover','violation','foul','stoppage','ejection') THEN 'team'
            WHEN actor_team_id IS NULL AND box_team_id IS NOT NULL
                 AND (team_id IS NULL OR team_id=box_team_id) THEN 'person'
            ELSE 'unresolved' END AS role,
       CASE WHEN is_field_goal IS TRUE THEN CASE WHEN shot_value IN (2,3) THEN shot_value
                   WHEN verb='2pt' THEN 2 WHEN verb='3pt' THEN 3 END END AS goal_value,
       CASE WHEN is_field_goal IS TRUE THEN CASE WHEN (goal_result='made' AND verb='missed shot')
                OR (goal_result='missed' AND verb='made shot') THEN NULL
                WHEN goal_result='made' OR verb='made shot' THEN true
                WHEN goal_result='missed' OR verb='missed shot' THEN false END END AS goal_made
    FROM classified
)
SELECT game_id, action_number, period, clock,
       60*try_cast(regexp_extract(clock,'^PT([0-9]+)M',1) AS DOUBLE)
          +try_cast(regexp_extract(clock,'M([0-9.]+)S$',1) AS DOUBLE),
       CASE WHEN is_field_goal IS TRUE THEN 'field_goal'
            WHEN verb IN ('free throw','freethrow') THEN 'free_throw'
            WHEN verb IN ('jump ball','jumpball') THEN 'jump_ball'
            WHEN verb IN ('instant replay','instantreplay') THEN 'instant_replay'
            WHEN verb IN ('2pt','3pt','made shot','missed shot','heave') THEN 'unclassified_shot_action'
            ELSE replace(verb,' ','_') END,
       action_type, sub_type, description,
       CASE role WHEN 'person' THEN box_team_id WHEN 'team' THEN actor_team_id ELSE reported_team_match END,
       CASE WHEN role='person' THEN player_id END, role,
       is_field_goal, goal_value, goal_made,
       CASE WHEN score_home>=0 THEN score_home END, CASE WHEN score_away>=0 THEN score_away END
FROM choices;

INSERT INTO audit.event_coverage
WITH source AS (SELECT game_id,count(*) n FROM src.main.fact_pbp_events GROUP BY 1),
     events AS (SELECT game_id,count(*) n,count(*) FILTER (WHERE actor_kind='unresolved') unknowns
                FROM nba.fact_play_event GROUP BY 1),
     holds AS (SELECT game_id,count(*) n FROM audit.event_hold GROUP BY 1)
SELECT game_id, coalesce(source.n,0), coalesce(events.n,0), coalesce(holds.n,0), coalesce(events.unknowns,0)
FROM (SELECT game_id FROM nba.dim_game UNION SELECT game_id FROM source) spine
LEFT JOIN source USING (game_id) LEFT JOIN events USING (game_id) LEFT JOIN holds USING (game_id);
DROP VIEW event_source;

CREATE TABLE nba.fact_shot (
    shot_key BLOB PRIMARY KEY CHECK (octet_length(shot_key)=32),
    game_id VARCHAR NOT NULL,
    team_id BIGINT NOT NULL,
    person_id BIGINT NOT NULL,
    period SMALLINT NOT NULL CHECK (period>0),
    seconds_remaining INTEGER NOT NULL CHECK (seconds_remaining>=0),
    shot_value SMALLINT NOT NULL CHECK (shot_value IN (2,3)),
    made BOOLEAN NOT NULL,
    source_action VARCHAR,
    source_zone_basic VARCHAR,
    source_zone_area VARCHAR,
    source_zone_range VARCHAR,
    distance_feet SMALLINT CHECK (distance_feet BETWEEN 0 AND 110),
    x_tenths_of_foot SMALLINT,
    y_tenths_of_foot SMALLINT,
    geometry_status VARCHAR NOT NULL CHECK (geometry_status IN ('reported','ambiguous_zero','unavailable','outlier')),
    CHECK ((geometry_status='reported')=(x_tenths_of_foot IS NOT NULL AND y_tenths_of_foot IS NOT NULL)),
    FOREIGN KEY (game_id,team_id) REFERENCES nba.bridge_game_team(game_id,team_id),
    FOREIGN KEY (game_id,person_id) REFERENCES nba.fact_player_game_stats(game_id,person_id)
);
CREATE TABLE audit.shot_hold (
    shot_key BLOB PRIMARY KEY,
    game_id VARCHAR,
    multiplicity BIGINT NOT NULL CHECK (multiplicity>0),
    reason VARCHAR NOT NULL
);
CREATE TABLE audit.shot_match_ambiguous (
    shot_key BLOB PRIMARY KEY,
    event_candidates BIGINT NOT NULL CHECK (event_candidates>0),
    shot_claimants BIGINT NOT NULL CHECK (shot_claimants>0),
    CHECK (event_candidates>1 OR shot_claimants>1)
);
CREATE TABLE nba.bridge_shot_event (
    shot_key BLOB PRIMARY KEY REFERENCES nba.fact_shot(shot_key),
    game_id VARCHAR NOT NULL,
    action_number INTEGER NOT NULL,
    match_method VARCHAR NOT NULL CHECK (match_method='clock_person_value_outcome_exact_coords'),
    x_legacy SMALLINT NOT NULL,
    y_legacy SMALLINT NOT NULL,
    UNIQUE (game_id, action_number),
    FOREIGN KEY (game_id, action_number) REFERENCES nba.fact_play_event(game_id, action_number)
);
CREATE TABLE audit.shot_coverage (
    game_id VARCHAR PRIMARY KEY,
    input_rows BIGINT NOT NULL CHECK (input_rows>=0),
    published_shots BIGINT NOT NULL CHECK (published_shots>=0),
    held_rows BIGINT NOT NULL CHECK (held_rows>=0),
    matched_shots BIGINT NOT NULL CHECK (matched_shots BETWEEN 0 AND published_shots),
    CHECK (input_rows=published_shots+held_rows)
);

-- A full SHA-256 of the local locator payload, not an invented provider event ID.
-- The source season label is not part of shot identity; the game owns its season.
CREATE TEMP TABLE shot_choices AS
WITH rows AS (
    SELECT unhex(sha256(to_json(s))) AS shot_key, s.*
    FROM (SELECT * EXCLUDE (season_year) FROM src.main.fact_shot_chart) s
), key_counts AS (
    SELECT shot_key, count(*) AS multiplicity FROM rows GROUP BY shot_key
)
SELECT r.*, k.multiplicity,
    r.minutes_remaining*60+r.seconds_remaining-greatest(r.period-4,0)*profile.overtime_offset AS clock_seconds_normalized,
    CASE WHEN k.multiplicity>1 THEN CASE WHEN profile.overtime_offset>0 THEN 'ambiguous_repeated_locator' ELSE 'duplicate_locator_payload' END
         WHEN b.team_id IS NULL THEN 'unknown_game_or_team'
         WHEN p.person_id IS NULL THEN 'player_box_membership_unresolved'
         WHEN r.period IS NULL OR r.period<=0 OR r.minutes_remaining IS NULL OR r.minutes_remaining<0
           OR r.seconds_remaining IS NULL OR r.seconds_remaining NOT BETWEEN 0 AND 59
           OR (profile.overtime_offset>0 AND r.period>4 AND
                r.minutes_remaining*60+r.seconds_remaining-(r.period-4)*profile.overtime_offset NOT BETWEEN 0 AND 300)
           OR r.shot_made_flag IS NULL OR r.shot_made_flag NOT IN (0,1)
           OR r.shot_type IS NULL OR r.shot_type NOT IN ('2PT Field Goal','3PT Field Goal') THEN 'invalid_shot_fields'
    END AS reason,
    CASE WHEN r.loc_x IS NULL OR r.loc_y IS NULL THEN 'unavailable'
         WHEN abs(r.loc_x)>500 OR r.loc_y NOT BETWEEN -100 AND 950 OR r.shot_distance NOT BETWEEN 0 AND 110 THEN 'outlier'
         WHEN r.loc_x=0 AND r.loc_y=0 AND r.shot_distance=0 THEN 'ambiguous_zero'
         ELSE 'reported' END AS geometry_status
FROM rows r JOIN key_counts k USING (shot_key) CROSS JOIN shot_clock_profile profile
LEFT JOIN nba.bridge_game_team b ON (r.game_id,r.team_id)=(b.game_id,b.team_id)
LEFT JOIN nba.fact_player_game_stats p ON (r.game_id,r.player_id,r.team_id)=(p.game_id,p.person_id,p.team_id);

INSERT INTO audit.shot_hold
SELECT shot_key, min(game_id), max(multiplicity), min(reason)
FROM shot_choices WHERE reason IS NOT NULL GROUP BY shot_key;
INSERT INTO nba.fact_shot
SELECT shot_key,game_id,team_id,player_id,period,clock_seconds_normalized,
       CASE shot_type WHEN '2PT Field Goal' THEN 2 ELSE 3 END, shot_made_flag=1, action_type,
       shot_zone_basic,shot_zone_area,shot_zone_range,
       CASE WHEN geometry_status<>'ambiguous_zero' AND shot_distance BETWEEN 0 AND 110 THEN shot_distance END,
       CASE WHEN geometry_status='reported' THEN loc_x END, CASE WHEN geometry_status='reported' THEN loc_y END,
       geometry_status
FROM shot_choices WHERE reason IS NULL;
DROP TABLE shot_choices;

-- Only use companion coordinates when v3 has the same event/person/team/period
-- and clock. A forward single match is insufficient without a unique reverse link.
CREATE TEMP TABLE locators AS
WITH plays AS (
    SELECT e.game_id,e.team_id,e.person_id,e.period,floor(e.seconds_remaining)::INTEGER AS seconds_remaining,
           e.field_goal_value AS shot_value,e.shot_made AS made,v.x_legacy AS x,v.y_legacy AS y,
           min(e.action_number) AS action_number,count(*) AS event_candidates
    FROM nba.fact_play_event e JOIN src.main.fact_play_by_play_v3 v USING (game_id,action_number)
    WHERE e.is_field_goal AND e.person_id=v.person_id AND e.team_id=v.team_id
          AND e.period=v.period AND e.clock_iso=v.clock AND v.is_field_goal
          AND e.field_goal_value IS NOT NULL AND e.shot_made IS NOT NULL
          AND v.x_legacy IS NOT NULL AND v.y_legacy IS NOT NULL
    GROUP BY 1,2,3,4,5,6,7,8,9
), shots AS (
    SELECT shot_key,game_id,team_id,person_id,period,seconds_remaining,shot_value,made,
           x_tenths_of_foot AS x,y_tenths_of_foot AS y,
           count(*) OVER (PARTITION BY game_id,team_id,person_id,period,seconds_remaining,shot_value,made,
                                      x_tenths_of_foot,y_tenths_of_foot) AS shot_claimants
    FROM nba.fact_shot WHERE geometry_status='reported'
)
SELECT s.shot_key,s.game_id,p.action_number,s.x,s.y,p.event_candidates,s.shot_claimants
FROM shots s JOIN plays p USING (game_id,team_id,person_id,period,seconds_remaining,shot_value,made,x,y);

INSERT INTO nba.bridge_shot_event
SELECT shot_key,game_id,action_number,'clock_person_value_outcome_exact_coords',x,y
FROM locators WHERE event_candidates=1 AND shot_claimants=1;
INSERT INTO audit.shot_match_ambiguous
SELECT shot_key,event_candidates,shot_claimants FROM locators WHERE event_candidates>1 OR shot_claimants>1;
DROP TABLE locators;

INSERT INTO audit.shot_coverage
WITH raw AS (SELECT game_id,count(*) n FROM src.main.fact_shot_chart GROUP BY 1),
     shots AS (SELECT game_id,count(*) n FROM nba.fact_shot GROUP BY 1),
     held AS (SELECT game_id,sum(multiplicity) n FROM audit.shot_hold GROUP BY 1),
     links AS (SELECT game_id,count(*) n FROM nba.bridge_shot_event GROUP BY 1)
SELECT game_id,coalesce(raw.n,0),coalesce(shots.n,0),coalesce(held.n,0),coalesce(links.n,0)
FROM (SELECT game_id FROM nba.dim_game UNION SELECT game_id FROM raw WHERE game_id IS NOT NULL) spine
LEFT JOIN raw USING (game_id) LEFT JOIN shots USING (game_id) LEFT JOIN held USING (game_id) LEFT JOIN links USING (game_id);

CREATE VIEW nba.shot_event_status AS
SELECT s.shot_key,s.game_id,b.action_number,
       CASE WHEN b.shot_key IS NOT NULL THEN 'matched'
            WHEN a.shot_key IS NOT NULL THEN 'ambiguous'
            WHEN s.geometry_status<>'reported' THEN 'geometry_unavailable'
            ELSE 'no_accepted_event_match' END AS match_status
FROM nba.fact_shot s LEFT JOIN nba.bridge_shot_event b USING (shot_key)
LEFT JOIN audit.shot_match_ambiguous a USING (shot_key);
