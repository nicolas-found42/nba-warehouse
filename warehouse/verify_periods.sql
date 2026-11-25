-- Supplemental SQL acceptance checks for the independent period-score domain.
WITH sides AS (
    SELECT p.game_id, b.side, min(p.period_number) lo, max(p.period_number) hi,
           count(*) n, sum(p.points) pts
    FROM nba.fact_game_period_score p JOIN nba.bridge_game_team b USING (game_id, team_id)
    GROUP BY 1,2
), published AS (
    SELECT c.game_id, r.home_score, r.away_score, coalesce(e.regulation_count,4) AS regulation_count,e.exact_count,
           h.lo hl, a.lo al, h.hi hh, a.hi ah, h.n hn, a.n an, h.pts hp, a.pts ap
    FROM audit.period_coverage c LEFT JOIN nba.fact_game_result r USING (game_id)
    LEFT JOIN verify_period_expectations e USING (game_id)
    LEFT JOIN sides h ON h.game_id=c.game_id AND h.side='home'
    LEFT JOIN sides a ON a.game_id=c.game_id AND a.side='away'
    WHERE c.status='published'
), checks(check_name, violations) AS (
    SELECT 'period_spine_accounted', count(*) FROM (
       (SELECT game_id FROM nba.dim_game EXCEPT ALL SELECT game_id FROM audit.period_coverage)
       UNION ALL
       (SELECT game_id FROM audit.period_coverage EXCEPT ALL SELECT game_id FROM nba.dim_game)
    )
    UNION ALL SELECT 'period_published_games_reconcile', count(*) FROM published
    WHERE hl IS DISTINCT FROM 1 OR al IS DISTINCT FROM 1 OR hh IS DISTINCT FROM ah
       OR hn IS DISTINCT FROM hh OR an IS DISTINCT FROM ah OR hh<regulation_count
       OR (exact_count IS NOT NULL AND hh<>exact_count) OR hp IS DISTINCT FROM home_score
       OR ap IS DISTINCT FROM away_score
    UNION ALL SELECT 'period_tie_before_overtime', count(*) FROM (
       SELECT p.game_id,p.period_number,coalesce(e.regulation_count,4) AS regulation_count,
              sum(sum(CASE b.side WHEN 'home' THEN p.points ELSE -p.points END))
                OVER (PARTITION BY p.game_id ORDER BY p.period_number) AS lead,
              max(p.period_number) OVER (PARTITION BY p.game_id) AS last_period
       FROM nba.fact_game_period_score p JOIN nba.bridge_game_team b USING (game_id,team_id)
       LEFT JOIN verify_period_expectations e USING (game_id)
       GROUP BY p.game_id,p.period_number,e.regulation_count
    ) WHERE period_number>=regulation_count AND period_number<last_period AND lead<>0
    UNION ALL SELECT 'period_no_extra_rows', count(*)
    FROM nba.fact_game_period_score p ANTI JOIN published c USING (game_id)
    UNION ALL SELECT 'period_no_paired_zero', count(*) FROM (
       SELECT game_id,period_number FROM nba.fact_game_period_score GROUP BY 1,2 HAVING sum(points)=0
    )
    UNION ALL SELECT 'period_known_zero_fills_not_published', count(*) FROM nba.fact_game_period_score
    WHERE game_id='0022500002' AND period_number IN (1,4) AND points=0
    UNION ALL SELECT 'period_multiple_overtimes_representable', CASE WHEN count(*)>0 THEN 0 ELSE 1 END
    FROM nba.fact_game_period_score WHERE period_number>=7
    UNION ALL SELECT 'period_published_is_nonempty', CASE WHEN count(*)>0 THEN 0 ELSE 1 END
    FROM published
)
SELECT check_name, violations FROM checks ORDER BY check_name;
