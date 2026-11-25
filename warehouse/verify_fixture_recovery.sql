-- Supplement acceptance: scorer-report fixtures, boxes, and result-based standings.
WITH recovered_box AS (
 SELECT p.game_id,p.team_id,count(*) AS player_rows,
        count(*) FILTER(WHERE p.participation_status='dnp') AS dnp,
        sum(round(p.num_minutes*60)) AS minutes_seconds,sum(p.points) AS points,
        count(*) FILTER(WHERE p.plus_minus IS NOT NULL OR p.participation_status='unknown') AS invalid
 FROM nba.fact_player_game_stats p JOIN audit.fixture_addition f USING(game_id)
 GROUP BY 1,2
), season_games AS (
 SELECT g.season_year,count(*) AS games
 FROM nba.dim_game g JOIN nba.fact_game_result r USING(game_id)
 WHERE g.counts_toward_regular_season GROUP BY 1
), standings_rollup AS (
 SELECT season_year,sum(wins) AS wins,sum(losses) AS losses,
        sum(points_for) AS pf,sum(points_against) AS pa
 FROM nba.fact_regular_standings GROUP BY 1
), recomputed_standings AS (
 SELECT g.season_year,b.team_id,count(*) AS games,
        sum((r.winner_team_id=b.team_id)::INTEGER) AS wins,
        sum((r.winner_team_id<>b.team_id)::INTEGER) AS losses,
        sum(CASE WHEN b.side='home' THEN r.home_score ELSE r.away_score END) AS points_for,
        sum(CASE WHEN b.side='home' THEN r.away_score ELSE r.home_score END) AS points_against
 FROM nba.dim_game g JOIN nba.fact_game_result r USING(game_id)
 JOIN nba.bridge_game_team b USING(game_id)
 WHERE g.counts_toward_regular_season GROUP BY 1,2
), checks(check_name,violations) AS (
 SELECT 'three_exact_verified_fixtures', abs(3-count(*)) FROM audit.fixture_addition
 WHERE (game_id,home_score,away_score,team_player_seconds) IN
       (('0032400011',41,32,3035),('0032400021',42,35,3870),('0032400031',25,41,3200))
 UNION ALL SELECT 'supplement_excludes_regular_and_matches_source',count(*)
 FROM audit.fixture_addition f LEFT JOIN nba.fact_game g USING(game_id)
 LEFT JOIN nba.bridge_event_fixture e USING(game_id)
 WHERE g.season_type IS DISTINCT FROM 'AllStar' OR g.counts_toward_regular_season IS DISTINCT FROM false
    OR (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
       IS DISTINCT FROM (f.game_date,f.home_team_id,f.away_team_id,f.home_score,f.away_score)
    OR (e.edition_key,e.stage,e.fixture_number,e.regulation_periods,e.neutral_site)
       IS DISTINCT FROM (f.edition_key,f.stage,f.fixture_number,1,true)
 UNION ALL SELECT 'final_stage_is_not_inferred_from_suffix', abs(2-count(*)) FROM nba.bridge_event_fixture
 WHERE (game_id,stage) IN (('0032400031','final'),('0032500031','round_robin'))
 UNION ALL SELECT 'supplement_preserves_box_counts_and_dnp',
      abs(52-coalesce(sum(player_rows),0))+abs(7-coalesce(sum(dnp),0)) FROM recovered_box
 UNION ALL SELECT 'supplement_player_totals_reconcile_by_side', count(*)
 FROM audit.fixture_added_team_box a JOIN audit.fixture_addition f USING(game_id)
 LEFT JOIN recovered_box r USING(game_id,team_id)
 LEFT JOIN nba.fact_team_game_stats t USING(game_id,team_id)
 WHERE r.points IS DISTINCT FROM CASE WHEN a.team_id=f.home_team_id THEN f.home_score ELSE f.away_score END
    OR r.minutes_seconds IS DISTINCT FROM a.team_player_seconds OR r.invalid IS DISTINCT FROM 0
    OR round(t.min*60) IS DISTINCT FROM a.team_player_seconds OR t.pts IS DISTINCT FROM r.points
 UNION ALL SELECT 'supplement_individual_player_minutes_match_audit',count(*)
 FROM audit.fixture_added_player_box a LEFT JOIN nba.fact_player_game_stats p USING(game_id,person_id)
 WHERE p.team_id IS DISTINCT FROM a.team_id
    OR p.num_minutes IS DISTINCT FROM a.minutes_seconds/60.0
    OR p.reported_num_minutes IS DISTINCT FROM a.raw_min_mm_ss
 UNION ALL SELECT 'supplement_periods_not_padded_to_four', abs(6-count(*))
 FROM nba.fact_game_period_score p JOIN audit.fixture_addition f USING(game_id)
 WHERE p.period_number=1
 UNION ALL SELECT 'standings_total_games_and_points_balance',count(*) FROM season_games g
 FULL JOIN standings_rollup s USING(season_year)
 WHERE g.games IS DISTINCT FROM s.wins OR g.games IS DISTINCT FROM s.losses OR s.pf IS DISTINCT FROM s.pa
 UNION ALL SELECT 'standings_historical_and_cup_controls', abs(6-count(*)) FROM nba.fact_regular_standings
 WHERE (season_year,team_id,wins,losses) IN (
   ('1953-54',1610612737,21,51),('1953-54',1610610024,16,56),
   ('2012-13',1610612738,41,40),('2012-13',1610612754,49,32),
   ('2023-24',1610612747,47,35),('2024-25',1610612749,48,34))
 UNION ALL SELECT 'standings_only_expected_team_seasons', abs(1723-count(*)) FROM nba.fact_regular_standings
 UNION ALL SELECT 'standings_match_verified_result_by_team',count(*) FROM (
   (SELECT * FROM nba.fact_regular_standings EXCEPT ALL SELECT * FROM recomputed_standings)
   UNION ALL (SELECT * FROM recomputed_standings EXCEPT ALL SELECT * FROM nba.fact_regular_standings)
 )
)
SELECT check_name,violations FROM checks ORDER BY check_name;
