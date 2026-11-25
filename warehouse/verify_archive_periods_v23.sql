WITH expected(game_id) AS (VALUES ('0024800149'),('0024900027')),
checks(check_name,violations) AS (
 SELECT 'v23_exact_archive_key_scope',count(*) FROM (
   (SELECT game_id FROM expected EXCEPT ALL SELECT game_id FROM audit.archive_period_receipt_v23)
   UNION ALL (SELECT game_id FROM audit.archive_period_receipt_v23 EXCEPT ALL SELECT game_id FROM expected))
 UNION ALL SELECT 'v23_receipt_fixture_and_provenance',count(*)
 FROM audit.archive_period_receipt_v23 r LEFT JOIN nba.fact_game g USING(game_id)
 WHERE (r.game_date,r.home_id,r.away_id,r.home_cumulative[4],r.away_cumulative[4])
         IS DISTINCT FROM (g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score)
   OR r.venue_observed<>'Uline Arena' OR (g.arena_name IS NOT NULL AND g.arena_name<>r.venue_observed)
   OR g.is_overtime IS TRUE
   OR r.edition<>r.game_date+1 OR r.page<=0
   OR r.source_url<>('https://www.loc.gov/resource/sn83045462/'||r.edition::VARCHAR||'/ed-1/?sp='||r.page::VARCHAR)
   OR NOT starts_with(r.image_url,'https://tile.loc.gov/image-services/iiif/')
   OR NOT regexp_full_match(r.image_sha256,'[0-9a-f]{64}')
   OR r.evidence_kind<>'derived_from_printed_cumulative' OR r.redistribution_cleared
   OR list_count(r.home_cumulative)<>4 OR list_count(r.away_cumulative)<>4
   OR list_count(r.home_boundary_notes)<>4 OR list_count(r.away_boundary_notes)<>4
 UNION ALL SELECT 'v23_archive_exact_derived_cells',count(*) FROM (
   (SELECT * FROM nba.fact_game_period_score SEMI JOIN expected USING(game_id)
    EXCEPT ALL SELECT * FROM audit.archive_period_cells_v23)
   UNION ALL (SELECT * FROM audit.archive_period_cells_v23 EXCEPT ALL
      SELECT * FROM nba.fact_game_period_score SEMI JOIN expected USING(game_id)))
 UNION ALL SELECT 'v23_archive_coverage_and_history',count(*)
 FROM expected e LEFT JOIN audit.period_coverage c USING(game_id)
 LEFT JOIN audit.period_availability_v23 n USING(game_id) LEFT JOIN audit.period_availability_v22 p USING(game_id)
 WHERE (c.status,c.candidate_sources,c.valid_sources,c.selected_source,n.status,n.selected_source,
        n.recovery_disposition,n.last_period,n.derived_overtime,p.status,p.last_period)
  IS DISTINCT FROM ('published',1,1,'archival_cumulative_v23','published','archival_cumulative_v23',
                    'derived_from_printed_cumulative',4,false,'no_supported_source',NULL)
 UNION ALL SELECT 'v23_no_unledgered_archive_publication',count(*)
 FROM audit.period_coverage c ANTI JOIN expected USING(game_id)
 WHERE c.selected_source='archival_cumulative_v23'
)
SELECT * FROM checks ORDER BY check_name;
