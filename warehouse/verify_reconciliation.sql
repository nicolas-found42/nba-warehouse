-- Extra release checks for the snapshot reconciliation introduced in v3.
WITH same_day AS (
    SELECT a.game_id AS first_id,b.game_id AS second_id
    FROM nba.dim_game a JOIN nba.dim_game b ON a.game_id<b.game_id AND a.game_date=b.game_date
      AND least(a.home_team_id,a.away_team_id)=least(b.home_team_id,b.away_team_id)
      AND greatest(a.home_team_id,a.away_team_id)=greatest(b.home_team_id,b.away_team_id)
    WHERE a.season_type IN ('Regular','Playoffs') AND b.season_type IN ('Regular','Playoffs')
), checks(check_name, violations) AS (
    SELECT 'same_day_regular_matchups_reviewed', count(*) FROM same_day s
    ANTI JOIN audit.fixture_collision_review a ON (s.first_id,s.second_id)=(a.first_game_id,a.second_game_id)
    UNION ALL SELECT 'doubleheader_keeps_both_source_games', abs(2-count(*)) FROM nba.fact_game
    WHERE (game_id,home_score,away_score) IN (('0025300306',65,54),('0025300325',64,54))
      AND game_date=DATE '1954-03-08'
    UNION ALL SELECT 'verified_scores_applied_with_raw_evidence', count(*)
    FROM audit.verified_score_repair r LEFT JOIN nba.fact_game g USING (game_id)
    LEFT JOIN audit.game_disposition a USING (game_id)
    WHERE (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
        IS DISTINCT FROM (r.game_date,r.home_team_id,r.away_team_id,r.home_score,r.away_score)
       OR a.source_home_score IS NOT NULL OR a.source_away_score IS NOT NULL
    UNION ALL SELECT 'all_original_game_pairs_resolved', count(*) FROM nba.dim_game
    WHERE participant_status<>'resolved'
    UNION ALL SELECT 'all_original_game_results_resolved', count(*) FROM nba.dim_game
    WHERE result_status<>'final'
    UNION ALL SELECT 'historical_player_box_scoring_identity', count(*)
    FROM nba.fact_player_game_stats p JOIN nba.dim_game g USING (game_id)
    WHERE g.season_start<1979 AND p.points<>2*p.fgm+p.ftm
    UNION ALL SELECT 'historical_team_box_scoring_identity', count(*)
    FROM nba.fact_team_game_stats t JOIN nba.dim_game g USING (game_id)
    WHERE g.season_start<1979 AND t.pts<>2*t.fgm+t.ftm
    UNION ALL SELECT 'reviewed_player_duplicates_not_promoted', count(*)
    FROM audit.player_identity_review x
    LEFT JOIN nba.fact_player_game_stats b ON (x.game_id,x.team_id,x.held_person_id)=(b.game_id,b.team_id,b.person_id)
    LEFT JOIN nba.fact_player_game_stats k ON (x.game_id,x.team_id,x.retained_person_id)=(k.game_id,k.team_id,k.person_id)
    LEFT JOIN audit.player_box_disposition d ON (x.game_id,x.held_person_id)=(d.game_id,d.person_id)
    WHERE b.game_id IS NOT NULL OR k.game_id IS NULL OR d.reason IS DISTINCT FROM 'reviewed_duplicate_appearance'
)
SELECT check_name, violations FROM checks ORDER BY check_name;
