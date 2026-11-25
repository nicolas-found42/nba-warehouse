-- Period integrity must bind every cell, its owner and its ordinal, not just totals.
CREATE VIEW audit.period_vector_observed AS
SELECT game_id,count(*) AS row_count,
       sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points) ORDER BY team_id,period_number))) AS vector_sha256
FROM nba.fact_game_period_score GROUP BY game_id;

CREATE TABLE audit.period_vector_baseline (
    game_id VARCHAR PRIMARY KEY,
    row_count BIGINT NOT NULL CHECK(row_count>0),
    vector_sha256 VARCHAR NOT NULL CHECK(regexp_full_match(vector_sha256,'[0-9a-f]{64}'))
);
INSERT INTO audit.period_vector_baseline SELECT * FROM audit.period_vector_observed;

CREATE VIEW audit.period_availability_v7 AS
SELECT p.game_id,p.status,p.selected_source,
       CASE WHEN a.game_id IS NOT NULL THEN 'archival_line_score'
            WHEN l.game_id IS NOT NULL THEN l.status
            WHEN p.status='published' THEN 'retained_parent'
            ELSE 'not_recovered_by_reversed_line_or_archival_receipts' END AS recovery_disposition,
       o.last_period,o.is_overtime AS derived_overtime,o.reported_is_overtime AS reported_overtime,
       CASE WHEN o.is_overtime IS NULL THEN 'periods_unavailable'
            WHEN o.reported_is_overtime IS NULL THEN 'reported_flag_missing'
            WHEN o.is_overtime=o.reported_is_overtime THEN 'agrees' ELSE 'reported_flag_disagrees' END AS overtime_status
FROM audit.period_coverage p JOIN nba.game_overtime o USING(game_id)
LEFT JOIN audit.local_period_recovery l USING(game_id)
LEFT JOIN audit.archive_period_receipt a USING(game_id);
