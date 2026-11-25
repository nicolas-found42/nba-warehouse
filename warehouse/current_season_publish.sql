-- Project only fields with a meaning compatible with the historical contracts.
CREATE TABLE nba.dim_season AS SELECT (season_end-1)::SMALLINT AS season_start,
 format('{}-{:02d}',season_end-1,season_end%100) AS season_year,'NBA'::VARCHAR AS league_era FROM config;
CREATE TABLE nba.dim_team AS SELECT h.* FROM history.nba.dim_team h
 SEMI JOIN sides s ON h.team_id=s.team_id;
CREATE TABLE nba.dim_person AS
WITH people AS (SELECT person AS person_id,min(nba_name) AS display_name FROM player_obs WHERE person IS NOT NULL GROUP BY 1)
SELECT h.* FROM history.nba.dim_person h SEMI JOIN people p USING(person_id)
UNION ALL
SELECT person_id,display_name,NULL::DATE,NULL::SMALLINT,NULL::SMALLINT,NULL::VARCHAR
FROM people ANTI JOIN history.nba.dim_person USING(person_id);
CREATE TABLE nba.dim_game AS
SELECT gid AS game_id,game_date,tip_et AS game_datetime_est,d.season_start,d.season_year,d.league_era,
 phase AS season_type,phase='Regular' AS counts_toward_regular_season,
 CASE WHEN cup_group THEN 'NBA Cup - Group Play' WHEN phase='Cup' THEN 'NBA Cup' END AS competition_tag,
 NULL::VARCHAR AS game_label,NULL::VARCHAR AS game_sub_label,NULL::INT AS series_game_number,
 hid AS home_team_id,aid AS away_team_id,'resolved'::VARCHAR AS participant_status,'final'::VARCHAR AS result_status,
 venue_full_name AS arena_name,venue_address_city AS arena_city,venue_address_state AS arena_state,
 CASE WHEN attendance>0 AND attendance=trunc(attendance) THEN try_cast(attendance AS INT) END AS attendance
FROM candidate CROSS JOIN nba.dim_season d;
CREATE TABLE nba.bridge_game_team AS SELECT gid AS game_id,side,team_id FROM sides;
CREATE TABLE nba.fact_game_result AS SELECT gid AS game_id,home_score,away_score,
 CASE WHEN home_score>away_score THEN hid ELSE aid END AS winner_team_id,
 status_period>4 AS is_overtime FROM candidate;
CREATE VIEW nba.fact_game AS SELECT * FROM nba.dim_game JOIN nba.fact_game_result USING(game_id);
CREATE TABLE nba.fact_game_period_score AS SELECT * FROM period_cells;
CREATE TABLE nba.fact_regular_standings AS SELECT g.season_year,b.team_id,count(*)::INT AS games,
 count(*) FILTER(WHERE b.score>b.opponent_score)::INT AS wins,
 count(*) FILTER(WHERE b.score<b.opponent_score)::INT AS losses,
 sum(b.score)::INT AS points_for,sum(b.opponent_score)::INT AS points_against
 FROM sides b JOIN nba.dim_game g ON b.gid=g.game_id WHERE g.counts_toward_regular_season GROUP BY 1,2;
CREATE VIEW nba.game_overtime AS SELECT o.game_id,p.last_period,o.is_overtime,
 NULL::BOOLEAN AS reported_is_overtime FROM nba.fact_game_result o
 JOIN (SELECT game_id,max(period_number) AS last_period FROM nba.fact_game_period_score GROUP BY game_id) p USING(game_id);

CREATE TABLE audit.game_source AS
 SELECT gid AS game_id,id AS espn_game_id,neutral_site,cup_group,status_type_name,game_match,notes_headline,
        home_score,away_score,home_id AS espn_home_id,away_id AS espn_away_id
 FROM candidate;
CREATE TABLE audit.player_source AS
 SELECT gid AS game_id,athlete_id AS espn_athlete_id,team_id AS espn_team_id,person AS person_id,
        identity_source,athlete_display_name,minutes AS reported_rounded_minutes,did_not_play,reason
 FROM player_obs;
CREATE TABLE audit.team_source AS
 SELECT gid AS game_id,nba_team AS team_id,team_id AS espn_team_id,turnovers AS player_line_turnovers,
        team_turnovers,total_turnovers,total_rebounds FROM team_obs;
CREATE TABLE audit.play_boundary_conflict AS SELECT * FROM play_boundary_conflict;
CREATE TABLE audit.shot_box_conflict AS SELECT * FROM shot_box_conflict;
CREATE TABLE audit.play_source AS
 SELECT gid AS game_id,sequence_number AS action_number,id AS espn_play_id,game_play_number AS play_order,
   period_number AS period,clock_display_value,team_id AS espn_team_id,athlete_id_1,athlete_id_2,athlete_id_3,
   score_source,home_score,away_score,type_id,type_text,text AS source_description
 FROM play_obs;
-- Original coordinate values and provider projection remain in their own coordinate system.
-- The chart release lacks a play ID; exact multiset reconciliation is performed before write.
CREATE TABLE audit.shot_coordinates AS
 SELECT gid AS game_id,sequence_number AS action_number,id AS espn_play_id,nba_team AS team_id,person AS person_id,
    points_attempted AS shot_value,scoring_play AS made,coordinate_x_raw,coordinate_y_raw,coordinate_x,coordinate_y,
    CASE WHEN coordinate_x_raw=0 AND coordinate_y_raw=0 THEN 'ambiguous_origin'
         WHEN coordinate_x_raw BETWEEN 0 AND 50 AND coordinate_y_raw BETWEEN -5 AND 94 THEN 'provider_coordinates'
         ELSE 'out_of_domain_or_missing' END AS coordinate_status
 FROM play_obs WHERE is_goal;

CREATE TABLE nba.fact_play_event AS
 SELECT gid AS game_id,sequence_number AS action_number,period_number::SMALLINT AS period,
   iso_clock AS clock_iso,seconds_left AS seconds_remaining,
   CASE WHEN is_goal THEN 'field_goal' WHEN shooting_play AND points_attempted=1 THEN 'free_throw'
        ELSE lower(regexp_replace(trim(type_text),'[^a-zA-Z0-9]+','_','g')) END AS action_kind,
   type_text AS source_action,NULL::VARCHAR AS source_subtype,text AS description,
   nba_team AS team_id,person AS person_id,
   CASE WHEN person IS NOT NULL THEN 'person' WHEN athlete_id_1 IS NOT NULL THEN 'unresolved'
        WHEN nba_team IS NOT NULL THEN 'team' ELSE 'not_reported' END AS actor_kind,
   is_goal AS is_field_goal,CASE WHEN is_goal THEN points_attempted::SMALLINT END AS field_goal_value,
   CASE WHEN is_goal THEN scoring_play END AS shot_made,
   CASE WHEN score_source='espn' THEN home_score END AS reported_home_score,
   CASE WHEN score_source='espn' THEN away_score END AS reported_away_score
 FROM play_obs p ANTI JOIN play_boundary_conflict c ON p.gid=c.game_id;
CREATE VIEW nba.field_goal_attempt AS
 SELECT sha256(('espn:'||s.espn_play_id)::VARCHAR)::BLOB AS attempt_key,
   p.game_id,p.team_id,p.person_id,p.period,p.seconds_remaining,p.clock_iso,p.field_goal_value AS shot_value,
   p.shot_made AS made,'espn_pbp'::VARCHAR AS attempt_source,p.action_number,p.source_action,
   'not_normalized'::VARCHAR AS geometry_status,NULL::SMALLINT AS distance_feet,
   NULL::SMALLINT AS x_tenths_of_foot,NULL::SMALLINT AS y_tenths_of_foot
 FROM nba.fact_play_event p JOIN audit.play_source s USING(game_id,action_number)
 WHERE p.is_field_goal AND p.team_id IS NOT NULL AND p.person_id IS NOT NULL;
