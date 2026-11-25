-- Historical candidates can be compared by side, but never admitted by score-only lookup.
WITH checks(check_name,violations) AS (
 SELECT 'v21_bref_four_exact_dispositions',count(*) FROM (
   (SELECT game_id,disposition FROM audit.bref_period_receipt_v21 EXCEPT ALL SELECT * FROM (VALUES
     ('0025100035','admitted'),('0025100052','admitted'),('0025600127','held_designated_side'),('0024600169','held_local_cell_conflict')))
   UNION ALL
   (SELECT * FROM (VALUES ('0025100035','admitted'),('0025100052','admitted'),
      ('0025600127','held_designated_side'),('0024600169','held_local_cell_conflict'))
      EXCEPT ALL SELECT game_id,disposition FROM audit.bref_period_receipt_v21))
 UNION ALL SELECT 'v21_bref_receipt_identity_and_orientation',count(*) FROM audit.bref_period_receipt_v21 r
 LEFT JOIN nba.fact_game g USING(game_id) LEFT JOIN audit.local_period_recovery l USING(game_id)
 WHERE (r.game_date,r.home_id,r.away_id,r.home_total,r.away_total) IS DISTINCT FROM
       (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
    OR NOT regexp_full_match(r.sha256,'[a-f0-9]{64}')
    OR (r.disposition='admitted' AND (r.home_label,r.away_label,r.home_periods,r.away_periods,r.home_total,r.away_total)
           IS DISTINCT FROM (r.expected_home,r.expected_away,l.away_periods,l.home_periods,list_sum(r.home_periods),list_sum(r.away_periods)))
    OR (r.disposition='admitted' AND (l.original_home_team_id,l.original_away_team_id,l.original_home_final,l.original_away_final)
           IS DISTINCT FROM (r.away_id,r.home_id,r.away_total,r.home_total))
 UNION ALL SELECT 'v21_bref_exact_period_cells',count(*) FROM (
   (SELECT * FROM nba.fact_game_period_score SEMI JOIN audit.bref_period_receipt_v21 USING(game_id)
    EXCEPT ALL SELECT * FROM audit.bref_period_cells_v21)
   UNION ALL
   (SELECT * FROM audit.bref_period_cells_v21 EXCEPT ALL SELECT * FROM nba.fact_game_period_score
    SEMI JOIN audit.bref_period_receipt_v21 USING(game_id)))
 UNION ALL SELECT 'v21_bref_coverage_update_exact',count(*) FROM audit.bref_period_receipt_v21 r
 LEFT JOIN audit.period_coverage c USING(game_id) LEFT JOIN audit.period_availability_v21 a USING(game_id)
 WHERE (r.disposition='admitted' AND (c.status,c.selected_source,c.candidate_sources,c.valid_sources,a.recovery_disposition)
        IS DISTINCT FROM ('published','bref_v21_printed_line_score',r.old_candidates+1,1,'bref_v21_printed_line_score'))
    OR (r.disposition<>'admitted' AND (c.status,c.candidate_sources,c.valid_sources,c.selected_source)
        IS DISTINCT FROM (r.old_status,r.old_candidates,0,NULL))
 UNION ALL SELECT 'v21_bref_no_unledgered_publication',count(*) FROM audit.period_coverage c
 ANTI JOIN (SELECT game_id FROM audit.bref_period_receipt_v21 WHERE disposition='admitted') r USING(game_id)
 WHERE c.selected_source='bref_v21_printed_line_score'
)
SELECT check_name,violations FROM checks ORDER BY check_name;
