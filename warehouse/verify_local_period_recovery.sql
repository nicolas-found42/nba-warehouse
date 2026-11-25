-- Re-derive exact recovered rows from the source-vector ledger, not only totals.
WITH expected AS (
    SELECT a.game_id,b.team_id,i AS period_number,CASE b.side WHEN 'home' THEN a.home_periods[i] ELSE a.away_periods[i] END AS points
    FROM audit.local_period_recovery a JOIN nba.bridge_game_team b USING(game_id), LATERAL range(1,len(a.home_periods)+1) p(i)
    WHERE a.status='admitted'
), actual AS (
    SELECT p.* FROM nba.fact_game_period_score p JOIN audit.local_period_recovery a USING(game_id)
    JOIN audit.period_coverage c USING(game_id)
    LEFT JOIN verify_new_period_superseded v USING(game_id)
    WHERE a.status='admitted' OR (v.game_id IS NULL AND c.selected_source IS DISTINCT FROM 'adjudicated_period_receipt')
), checks AS (
    SELECT 'local_period_rows_match_source_vector' AS check_name,count(*) AS violations FROM (
        (SELECT * FROM expected EXCEPT ALL SELECT * FROM actual)
        UNION ALL (SELECT * FROM actual EXCEPT ALL SELECT * FROM expected)
    )
    UNION ALL SELECT 'local_period_recovery_dispositions_match',count(*)
    FROM audit.local_period_recovery a JOIN nba.fact_game g USING(game_id) JOIN audit.period_coverage c USING(game_id)
    LEFT JOIN verify_local_period_later_evidence l ON l.game_id=c.game_id
        AND (l.status,l.selected_source) IS NOT DISTINCT FROM (c.status,c.selected_source)
    WHERE g.home_team_id IS DISTINCT FROM a.original_away_team_id OR g.away_team_id IS DISTINCT FROM a.original_home_team_id
       OR (a.status='admitted' AND (c.status<>'published' OR c.selected_source IS DISTINCT FROM 'local_line_score_reoriented'
          OR a.distinct_line_payloads<>1 OR a.original_line_date IS DISTINCT FROM g.game_date
          OR a.original_home_final IS DISTINCT FROM g.away_score OR a.original_away_final IS DISTINCT FROM g.home_score
          OR len(a.home_periods)<>len(a.away_periods) OR a.prior_status NOT IN ('no_supported_source','no_valid_candidate')))
       OR (a.status<>'admitted' AND c.status<>a.prior_status AND c.selected_source IS DISTINCT FROM 'adjudicated_period_receipt'
          AND l.game_id IS NULL)
    UNION ALL SELECT 'local_period_recovery_no_unledgered_publication',count(*)
    FROM audit.period_coverage c ANTI JOIN (SELECT game_id FROM audit.local_period_recovery WHERE status='admitted') a USING(game_id)
    WHERE c.selected_source='local_line_score_reoriented'
    UNION ALL SELECT 'local_period_published_receipts',count(*) FROM (
        SELECT game_id,home_periods,away_periods FROM audit.local_period_recovery WHERE status='admitted' AND game_id IN ('0012200062','0024600026')
    ) RIGHT JOIN (VALUES ('0012200062',[29,31,32,22],[39,42,22,19]),('0024600026',[14,15,19,22,13],[15,19,14,22,4]))
       known(id,h,a) ON game_id=id AND home_periods=h AND away_periods=a WHERE game_id IS NULL
    UNION ALL SELECT 'local_period_balanced_fill_not_published',CASE WHEN count(*)=1 THEN 0 ELSE 1 END
    FROM audit.local_period_recovery WHERE game_id='0010600016' AND status='possible_balanced_fill'
)
SELECT check_name,violations FROM checks ORDER BY check_name;
