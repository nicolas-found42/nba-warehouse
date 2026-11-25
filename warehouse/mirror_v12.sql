-- Four older regular seasons and six individually receipted overtime fixtures.
CREATE VIEW audit.mirror_v12_scope AS
SELECT game_id,season_year,
       CASE WHEN season_start IN (1960,1961,1966,1970) THEN 'legacy_regular' ELSE 'keyed_overtime' END AS cohort
FROM nba.dim_game
WHERE (season_start IN (1960,1961,1966,1970) AND season_type='Regular')
   OR game_id IN ('0020200400','0020500356','0029600158','0029800101','0040100317','0040200141');

CREATE TABLE audit.mirror_v12_prior (
    game_id VARCHAR PRIMARY KEY,status VARCHAR NOT NULL,candidate_sources BIGINT NOT NULL,valid_sources BIGINT NOT NULL,
    CHECK(status IN ('no_supported_source','no_valid_candidate') AND valid_sources=0)
);
CREATE TABLE audit.mirror_v12_team (
    game_id VARCHAR NOT NULL,team_id BIGINT NOT NULL,opponent_team_id BIGINT,
    game_date DATE,season_year VARCHAR,game_type VARCHAR,is_home BOOLEAN,pts DOUBLE,
    regulation DOUBLE[4] NOT NULL,overtime_slots DOUBLE[2] NOT NULL,ot_all_points DOUBLE,
    PRIMARY KEY (game_id,team_id)
);
CREATE TABLE audit.mirror_v12_official (
    game_id VARCHAR PRIMARY KEY,game_date DATE NOT NULL,home_id BIGINT NOT NULL,away_id BIGINT NOT NULL,
    home_periods INTEGER[] NOT NULL,away_periods INTEGER[] NOT NULL,source_url VARCHAR NOT NULL UNIQUE,
    capture_sha256 VARCHAR NOT NULL CHECK(regexp_full_match(capture_sha256,'[a-f0-9]{64}')),
    CHECK(home_id<>away_id AND len(home_periods)=len(away_periods) AND len(home_periods) BETWEEN 5 AND 6),
    CHECK(source_url='https://www.nba.com/game/'||game_id)
);
CREATE TABLE audit.mirror_v12_input (
    role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL CHECK(regexp_full_match(sha256,'[a-f0-9]{64}')),
    provenance VARCHAR NOT NULL,redistribution_cleared BOOLEAN NOT NULL CHECK(NOT redistribution_cleared)
);

CREATE VIEW audit.mirror_v12_vectors AS
SELECT *,list_concat(regulation::DOUBLE[],list_filter(overtime_slots,lambda x: x IS NOT NULL)) AS vector,
       len(list_filter(regulation,lambda x: x IS NOT NULL)) AS regulation_cells,
       list_sum(regulation) AS regulation_sum,list_sum(overtime_slots) AS slot_sum,
       len(list_filter(list_concat(regulation::DOUBLE[],overtime_slots::DOUBLE[],[ot_all_points]),
            lambda x: x IS NOT NULL AND (NOT isfinite(x) OR x<0 OR x>2147483647 OR trunc(x)<>x))) AS invalid_cells
FROM audit.mirror_v12_team;

CREATE VIEW audit.mirror_v12_assessment AS
WITH source_count AS (SELECT game_id,count(*) AS observed_rows FROM audit.mirror_v12_team GROUP BY 1)
SELECT s.game_id,s.season_year,s.cohort,coalesce(n.observed_rows,0) AS observed_rows,
  CASE WHEN n.observed_rows IS DISTINCT FROM 2 OR h.team_id IS NULL OR a.team_id IS NULL THEN 'unmatched_team_keys'
       WHEN (h.is_home,a.is_home,h.opponent_team_id,a.opponent_team_id,h.game_date,a.game_date,h.season_year,a.season_year,
             h.game_type,a.game_type,h.pts,a.pts,g.result_status)
            IS DISTINCT FROM (true,false,g.away_team_id,g.home_team_id,g.game_date,g.game_date,g.season_year,g.season_year,
             CASE WHEN g.season_type='Regular' THEN 'Regular Season' ELSE g.season_type END,
             CASE WHEN g.season_type='Regular' THEN 'Regular Season' ELSE g.season_type END,g.home_score,g.away_score,'final')
            THEN 'fixture_or_final_disagrees'
       WHEN h.regulation_cells=0 OR a.regulation_cells=0 THEN 'no_quarter_cells'
       WHEN h.regulation_cells<>4 OR a.regulation_cells<>4 THEN 'incomplete_quarter_cells'
       WHEN h.invalid_cells<>0 OR a.invalid_cells<>0 THEN 'invalid_numeric_cells'
       WHEN (h.regulation_sum=0 AND h.pts>0) OR (a.regulation_sum=0 AND a.pts>0) THEN 'zero_filled_quarters'
       WHEN (h.overtime_slots[1] IS NULL,h.overtime_slots[2] IS NULL)
             IS DISTINCT FROM (a.overtime_slots[1] IS NULL,a.overtime_slots[2] IS NULL)
         OR (h.overtime_slots[1] IS NULL AND h.overtime_slots[2] IS NOT NULL) THEN 'invalid_overtime_slot_shape'
       WHEN h.slot_sum IS NULL AND a.slot_sum IS NULL AND h.ot_all_points=h.pts AND a.ot_all_points=a.pts
         AND h.regulation_sum=h.pts AND a.regulation_sum=a.pts THEN 'ot_aggregate_duplicates_final'
       WHEN (h.slot_sum IS NOT NULL AND h.ot_all_points>h.slot_sum)
         OR (a.slot_sum IS NOT NULL AND a.ot_all_points>a.slot_sum) THEN 'overtime_slots_truncated'
       WHEN h.ot_all_points IS DISTINCT FROM h.slot_sum OR a.ot_all_points IS DISTINCT FROM a.slot_sum THEN 'overtime_aggregate_disagrees'
       WHEN h.regulation_sum+coalesce(h.slot_sum,0) IS DISTINCT FROM h.pts
         OR a.regulation_sum+coalesce(a.slot_sum,0) IS DISTINCT FROM a.pts THEN 'cells_do_not_reconcile'
       WHEN (h.slot_sum IS NOT NULL AND h.regulation_sum<>a.regulation_sum)
         OR (h.overtime_slots[2] IS NOT NULL AND h.overtime_slots[1]<>a.overtime_slots[1]) THEN 'untied_before_overtime'
       WHEN len(list_filter(list_zip(h.vector,a.vector),lambda x: x[1]+x[2]=0))>0 THEN 'paired_zero_period'
       WHEN g.is_overtime IS NOT NULL AND g.is_overtime<>(h.slot_sum IS NOT NULL) THEN 'reported_overtime_disagrees'
       WHEN s.cohort='keyed_overtime' AND ((o.game_date,o.home_id,o.away_id,o.home_periods,o.away_periods)
          IS DISTINCT FROM (g.game_date,g.home_team_id,g.away_team_id,h.vector,a.vector)) THEN 'keyed_official_vector_disagrees'
       ELSE 'admitted' END AS disposition
FROM audit.mirror_v12_scope s JOIN nba.fact_game g USING(game_id)
LEFT JOIN source_count n USING(game_id)
LEFT JOIN audit.mirror_v12_vectors h ON s.game_id=h.game_id AND g.home_team_id=h.team_id
LEFT JOIN audit.mirror_v12_vectors a ON s.game_id=a.game_id AND g.away_team_id=a.team_id
LEFT JOIN audit.mirror_v12_official o ON s.game_id=o.game_id;

CREATE VIEW audit.mirror_v12_cells AS
SELECT m.game_id,m.team_id,i::SMALLINT AS period_number,cast(m.vector[i] AS INTEGER) AS points
FROM audit.mirror_v12_vectors m JOIN audit.mirror_v12_assessment a USING(game_id),
     LATERAL range(1,len(m.vector)+1) n(i)
WHERE a.disposition='admitted';

CREATE VIEW audit.mirror_v12_coverage AS
SELECT a.game_id, a.disposition,
       CASE WHEN disposition='admitted' THEN 'published' WHEN observed_rows>0 THEN 'no_valid_candidate' ELSE p.status END AS status,
       CASE WHEN disposition='admitted' AND cohort='keyed_overtime' THEN 'mirror_v12_keyed_official_ot'
            WHEN disposition='admitted' THEN 'mirror_v12_legacy_team_box' END AS selected_source,
       p.candidate_sources+CASE WHEN observed_rows>0 THEN 1 ELSE 0 END AS candidate_sources,
       p.valid_sources+CASE WHEN disposition='admitted' THEN 1 ELSE 0 END AS valid_sources
FROM audit.mirror_v12_assessment a JOIN audit.mirror_v12_prior p USING(game_id);
