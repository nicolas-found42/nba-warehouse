-- Published-schema acceptance checks. A nonzero violations count blocks the release.
WITH checks(check_name, violations) AS (
    SELECT 'game_spine_matches_manifest',
           abs((SELECT game_rows FROM audit.input_manifest) - (SELECT count(*) FROM audit.game_disposition))
    UNION ALL SELECT 'game_view_preserves_spine',
           abs((SELECT count(*) FROM nba.dim_game) - count(*)) FROM nba.fact_game
    UNION ALL SELECT 'game_dispositions_exact', count(*) FROM (
        (SELECT game_id FROM nba.dim_game EXCEPT ALL SELECT game_id FROM verify_game_keys)
        UNION ALL
        (SELECT game_id FROM verify_game_keys EXCEPT ALL SELECT game_id FROM nba.dim_game)
    )
    UNION ALL SELECT 'game_participant_pairs', count(*) FROM (
        SELECT g.game_id FROM nba.dim_game g LEFT JOIN nba.bridge_game_team b USING (game_id)
        GROUP BY g.game_id, g.participant_status, g.home_team_id, g.away_team_id
        HAVING count(b.team_id) <> CASE WHEN g.participant_status = 'resolved' THEN 2 ELSE 0 END
            OR count(*) FILTER (WHERE (b.side = 'home' AND b.team_id IS DISTINCT FROM g.home_team_id)
                                  OR (b.side = 'away' AND b.team_id IS DISTINCT FROM g.away_team_id)) > 0
    )
    UNION ALL SELECT 'final_result_status_consistent', count(*)
        FROM nba.dim_game g LEFT JOIN nba.fact_game_result r USING (game_id)
        WHERE (g.result_status = 'final') IS DISTINCT FROM (r.game_id IS NOT NULL)
           OR (r.game_id IS NOT NULL AND (g.participant_status <> 'resolved'
               OR r.winner_team_id IS DISTINCT FROM CASE WHEN r.home_score > r.away_score
                                                         THEN g.home_team_id ELSE g.away_team_id END))
    UNION ALL SELECT 'team_box_keys_and_scores', count(*)
        FROM nba.fact_team_game_stats t JOIN nba.dim_game g USING (game_id)
        LEFT JOIN nba.bridge_game_team b ON (t.game_id, t.team_id) = (b.game_id, b.team_id)
        LEFT JOIN nba.fact_game_result r ON t.game_id = r.game_id
        WHERE b.team_id IS NULL OR t.is_home IS DISTINCT FROM (b.side = 'home')
           OR (r.game_id IS NOT NULL AND t.pts IS NOT NULL AND
              t.pts IS DISTINCT FROM CASE b.side WHEN 'home' THEN r.home_score ELSE r.away_score END)
    UNION ALL SELECT 'player_box_keys_and_sides', count(*)
        FROM nba.fact_player_game_stats p JOIN nba.dim_game g USING (game_id)
        LEFT JOIN nba.bridge_game_team b ON (p.game_id, p.team_id) = (b.game_id, b.team_id)
        LEFT JOIN nba.dim_person x ON p.person_id = x.person_id
        WHERE b.team_id IS NULL OR x.person_id IS NULL OR p.is_home IS DISTINCT FROM (b.side = 'home')
           OR p.opponent_team_id IS DISTINCT FROM CASE b.side WHEN 'home' THEN g.away_team_id ELSE g.home_team_id END
    UNION ALL SELECT 'player_field_goal_score_identity', count(*) FROM nba.fact_player_game_stats
        WHERE points <> 2*fgm + fg3m + ftm
    UNION ALL SELECT 'team_box_input_accounted',
        abs((SELECT team_box_rows FROM audit.input_manifest) - count(*)) FROM audit.team_box_disposition
    UNION ALL SELECT 'player_box_input_accounted',
        abs((SELECT player_box_rows FROM audit.input_manifest) - count(*)) FROM audit.player_box_disposition
    UNION ALL SELECT 'published_team_box_disposition_keys', count(*) FROM (
        (SELECT game_id, team_id FROM nba.fact_team_game_stats EXCEPT ALL
         SELECT game_id, team_id FROM verify_team_box_keys)
        UNION ALL
        (SELECT game_id, team_id FROM verify_team_box_keys EXCEPT ALL
         SELECT game_id, team_id FROM nba.fact_team_game_stats)
    )
    UNION ALL SELECT 'published_player_box_disposition_keys', count(*) FROM (
        (SELECT game_id, person_id FROM nba.fact_player_game_stats EXCEPT ALL
         SELECT game_id, person_id FROM verify_player_box_keys)
        UNION ALL
        (SELECT game_id, person_id FROM verify_player_box_keys EXCEPT ALL
         SELECT game_id, person_id FROM nba.fact_player_game_stats)
    )
    UNION ALL SELECT 'baa_scope', count(*) FROM nba.dim_game
        WHERE (league_era = 'BAA') IS DISTINCT FROM (season_start BETWEEN 1946 AND 1948)
    UNION ALL SELECT 'historical_all_star_regression', 1 - count(*) FROM nba.fact_game
        WHERE game_id='0035100001' AND season_year='1951-52' AND game_date=DATE '1952-02-11'
          AND home_score=108 AND away_score=91
    UNION ALL SELECT 'bubble_scrimmage_regression', abs(33-count(*)) FROM nba.dim_game
        WHERE season_year='2019-20' AND season_type='Exhibition'
          AND game_date BETWEEN DATE '2020-07-22' AND DATE '2020-07-28'
    UNION ALL SELECT 'played_finals_regression', abs(5-count(*)) FROM nba.fact_game_result
        WHERE game_id BETWEEN '0042500401' AND '0042500405'
    UNION ALL SELECT 'unplayed_finals_excluded', count(*) FROM nba.dim_game
        WHERE game_id IN ('0042500406','0042500407')
    UNION ALL SELECT 'known_fixture_exceptions_not_silently_dropped',
        abs(5-(SELECT count(*) FROM audit.known_fixture)) + count(*)
        FROM audit.known_fixture k JOIN nba.dim_game g USING (game_id)
        WHERE k.disposition='scheduled_not_played'
)
SELECT check_name, violations FROM checks ORDER BY check_name;
