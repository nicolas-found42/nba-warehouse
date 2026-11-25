-- Read-only comparison of the existing local Basketball-Reference extracts.
-- v2 is attached as 'curated'; the original snapshot is attached as 'src'.
-- Exact season-total agreements do not verify any individual game or event.
CREATE TABLE team_season AS
WITH reference_rows AS (
    SELECT t.season_end_year, t.team_id, t.source_team_abbrev,
           t.g, t.pts, s.w, s.l, o.opp_pts,
           count(*) OVER (PARTITION BY t.season_end_year, t.team_id) AS candidates
    FROM src.main.fact_bref_team_season_totals t
    LEFT JOIN src.main.fact_bref_team_season_summary s USING (season_end_year, league, source_team_abbrev)
    LEFT JOIN src.main.fact_bref_team_season_opponent_totals o USING (season_end_year, league, source_team_abbrev)
    WHERE t.league = 'NBA'
), local_results AS (
    SELECT d.season_start + 1 AS season_end_year, b.team_id,
           count(*) AS n,
           count(*) FILTER (WHERE r.game_id IS NOT NULL) AS finals,
           count(*) FILTER (WHERE r.winner_team_id=b.team_id) AS wins,
           count(*) FILTER (WHERE r.winner_team_id<>b.team_id) AS losses,
           sum(CASE b.side WHEN 'home' THEN r.home_score ELSE r.away_score END) AS points,
           sum(CASE b.side WHEN 'home' THEN r.away_score ELSE r.home_score END) AS allowed
    FROM curated.nba.dim_game d JOIN curated.nba.bridge_game_team b USING (game_id)
    LEFT JOIN curated.nba.fact_game_result r USING (game_id)
    WHERE d.counts_toward_regular_season
    GROUP BY 1,2
)
SELECT b.season_end_year, b.team_id, b.source_team_abbrev,
       a.n-b.g AS games_delta, a.wins-b.w AS wins_delta, a.losses-b.l AS losses_delta,
       a.points-b.pts AS points_delta, a.allowed-b.opp_pts AS allowed_delta,
       CASE WHEN b.team_id IS NULL THEN 'unmapped_reference_team'
            WHEN b.candidates<>1 THEN 'ambiguous_reference_team'
            WHEN a.team_id IS NULL THEN 'missing_local_team_season'
            WHEN b.g IS NULL OR b.w IS NULL OR b.l IS NULL OR b.pts IS NULL OR b.opp_pts IS NULL
               OR b.g<>b.w+b.l THEN 'reference_fields_incomplete'
            WHEN a.n<>b.g OR a.n<>a.finals THEN 'games_coverage_differs'
            WHEN a.wins=b.w AND a.losses=b.l AND a.points=b.pts AND a.allowed=b.opp_pts THEN 'matches'
            ELSE 'totals_disagree' END AS status
FROM reference_rows b LEFT JOIN local_results a USING (season_end_year, team_id);

CREATE TABLE player_season AS
WITH ranked_reference AS (
    SELECT season_end_year, bref_player_id, g, pts,
           source_team_abbrev='TOT' OR regexp_full_match(source_team_abbrev,'[0-9]+TM') AS all_teams_row
    FROM src.main.fact_bref_player_season_totals
    WHERE league='NBA' AND NOT is_playoffs
), reference_totals AS (
    SELECT season_end_year, bref_player_id, count(*) AS rows_in_group,
           count(*) FILTER (WHERE all_teams_row) AS total_rows,
           CASE WHEN count(*)=1 THEN max(g) ELSE max(g) FILTER(WHERE all_teams_row) END AS games,
           CASE WHEN count(*)=1 THEN max(pts) ELSE max(pts) FILTER(WHERE all_teams_row) END AS points
    FROM ranked_reference GROUP BY 1,2
), identity AS (
    SELECT source_key AS bref_player_id, count(*) AS n,
           max(try_cast(canonical_id AS BIGINT)) AS resolved_id,
           count(*) FILTER (WHERE resolution_status='intentionally_excluded') AS exclusions
    FROM src.main.map_source_identity
    WHERE source_system='basketball_reference' AND entity_type='player'
    GROUP BY 1
), hold_counts AS (
    SELECT d.season_start + 1 AS season_end_year, a.person_id, count(*) AS n
    FROM curated.audit.player_box_disposition a JOIN curated.nba.dim_game d USING (game_id)
    WHERE a.disposition='held' AND d.counts_toward_regular_season
    GROUP BY 1,2
), local_totals AS (
    SELECT d.season_start + 1 AS season_end_year, p.person_id,
           count(*) FILTER (WHERE p.participation_status='played') AS played,
           count(*) FILTER (WHERE p.participation_status='unknown') AS unknown_rows,
           count(*) FILTER (WHERE p.participation_status='played' AND p.points IS NULL) AS played_missing_points,
           sum(p.points) FILTER (WHERE p.participation_status='played') AS points
    FROM curated.nba.fact_player_game_stats p JOIN curated.nba.dim_game d USING (game_id)
    WHERE d.counts_toward_regular_season
    GROUP BY 1,2
), comparison AS (
    SELECT b.season_end_year, b.bref_player_id, i.resolved_id AS person_id,
           i.n AS mapping_count, i.exclusions AS exclusions,
           b.rows_in_group, b.total_rows, b.games, b.points,
           a.played, a.unknown_rows, a.played_missing_points, a.points AS local_points,
           coalesce(h.n,0) AS held_rows,
           (a.season_end_year IS NOT NULL) AS found_local_season
    FROM reference_totals b
    LEFT JOIN identity i ON b.bref_player_id=i.bref_player_id
    LEFT JOIN local_totals a ON (b.season_end_year,i.resolved_id)=(a.season_end_year,a.person_id)
    LEFT JOIN hold_counts h ON (b.season_end_year,i.resolved_id)=(h.season_end_year,h.person_id)
)
SELECT season_end_year, bref_player_id, person_id,
       played-games AS games_delta, local_points-points AS points_delta, unknown_rows, held_rows,
       CASE WHEN rows_in_group<>1 AND total_rows<>1 THEN 'reference_total_ambiguous'
            WHEN exclusions>0 THEN 'explicit_identity_exclusion'
            WHEN mapping_count>1 THEN 'ambiguous_reference_person'
            WHEN person_id IS NULL THEN 'unmapped_reference_person'
            WHEN NOT found_local_season THEN 'missing_local_player_season'
            WHEN games IS NULL OR points IS NULL THEN 'reference_fields_incomplete'
            WHEN held_rows>0 OR unknown_rows>0 OR played_missing_points>0 THEN 'local_box_coverage_uncertain'
            WHEN played<>games THEN 'games_coverage_differs'
            WHEN local_points=points THEN 'matches'
            ELSE 'totals_disagree' END AS status
FROM comparison;

CREATE TABLE comparison_inventory AS
SELECT 'team_season' AS comparison, status, count(*) AS rows FROM team_season GROUP BY status
UNION ALL
SELECT 'player_season', status, count(*) FROM player_season GROUP BY status;
