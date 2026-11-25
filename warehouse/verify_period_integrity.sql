WITH published AS (
    SELECT game_id FROM audit.period_coverage WHERE status='published'
), archive_expected AS (
    SELECT r.game_id,b.team_id,i AS period_number,CASE b.side WHEN 'home' THEN r.home_q[i] ELSE r.away_q[i] END AS points
    FROM audit.archive_period_receipt r JOIN nba.bridge_game_team b USING(game_id), range(1,5) n(i)
), archive_observed AS (
    SELECT p.* FROM nba.fact_game_period_score p SEMI JOIN audit.archive_period_receipt a USING(game_id)
), checks AS (
    SELECT 'period_exact_vectors_match_baseline' AS check_name,count(*) AS violations FROM (
        (SELECT * FROM audit.period_vector_baseline EXCEPT ALL SELECT * FROM audit.period_vector_observed)
        UNION ALL (SELECT * FROM audit.period_vector_observed EXCEPT ALL SELECT * FROM audit.period_vector_baseline)
    )
    UNION ALL SELECT 'period_vector_key_coverage_complete',count(*) FROM (
        (SELECT game_id FROM published EXCEPT ALL SELECT game_id FROM audit.period_vector_baseline)
        UNION ALL (SELECT game_id FROM audit.period_vector_baseline EXCEPT ALL SELECT game_id FROM published)
    )
    UNION ALL SELECT 'archive_period_receipts_match_vectors',count(*) FROM (
        (SELECT * FROM archive_expected EXCEPT ALL SELECT * FROM archive_observed)
        UNION ALL (SELECT * FROM archive_observed EXCEPT ALL SELECT * FROM archive_expected)
    )
    UNION ALL SELECT 'archive_period_keys_and_overtime_match',count(*)
    FROM audit.archive_period_receipt a LEFT JOIN nba.fact_game g USING(game_id)
    LEFT JOIN audit.period_availability_v7 c USING(game_id)
    WHERE (g.game_date,g.series_game_number,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
       IS DISTINCT FROM (a.game_date,a.series_game_number,a.home_team_id,a.away_team_id,list_sum(a.home_q),list_sum(a.away_q))
       OR c.derived_overtime IS DISTINCT FROM false OR c.selected_source IS DISTINCT FROM 'archival_line_score'
       OR g.game_label IS DISTINCT FROM 'NBA Finals' OR g.result_status IS DISTINCT FROM 'final'
    UNION ALL SELECT 'archive_period_no_unattributed_publication',count(*)
    FROM audit.period_coverage c ANTI JOIN audit.archive_period_receipt a USING(game_id) WHERE c.selected_source='archival_line_score'
    UNION ALL SELECT 'period_availability_keys_complete',count(*) FROM (
        (SELECT game_id FROM nba.dim_game EXCEPT ALL SELECT game_id FROM audit.period_availability_v7)
        UNION ALL (SELECT game_id FROM audit.period_availability_v7 EXCEPT ALL SELECT game_id FROM nba.dim_game)
    )
)
SELECT check_name,violations FROM checks ORDER BY check_name;
