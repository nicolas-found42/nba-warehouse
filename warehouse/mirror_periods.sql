-- Retain the source's floating values and NULL slots until checked. Expected team-period cells
-- come from that retained input, not from an aggregate or a re-hash of the fact rows.
CREATE TABLE audit.mirror_period_game (
    game_id VARCHAR PRIMARY KEY,prior_status VARCHAR NOT NULL,
    prior_candidates BIGINT NOT NULL,prior_valid BIGINT NOT NULL,
    CHECK(prior_status='no_supported_source' AND prior_candidates=0 AND prior_valid=0)
);
INSERT INTO audit.mirror_period_game SELECT * FROM mirror_period_scope;

CREATE TABLE audit.mirror_period_team (
    game_id VARCHAR NOT NULL,team_id BIGINT NOT NULL,opponent_team_id BIGINT NOT NULL,
    game_date DATE NOT NULL,season_year VARCHAR NOT NULL,game_type VARCHAR NOT NULL,is_home BOOLEAN NOT NULL,
    pts DOUBLE NOT NULL,regulation DOUBLE[4] NOT NULL,overtime_slots DOUBLE[2] NOT NULL,ot_all_points DOUBLE,
    PRIMARY KEY (game_id,team_id)
);
INSERT INTO audit.mirror_period_team SELECT * FROM mirror_period_source;

CREATE TABLE audit.mirror_period_tail_receipt (
    game_id VARCHAR NOT NULL,team_id BIGINT NOT NULL,game_date DATE NOT NULL,
    period_number SMALLINT NOT NULL CHECK(period_number=7),points INTEGER NOT NULL CHECK(points>=0),
    source_url VARCHAR NOT NULL CHECK(source_url='https://www.nba.com/game/'||game_id),
    PRIMARY KEY(game_id,team_id)
);

CREATE VIEW audit.mirror_period_expected_cells AS
SELECT m.game_id,m.team_id,i::SMALLINT AS period_number,try_cast(m.vector[i] AS INTEGER) AS points
FROM (SELECT *,list_concat(regulation::DOUBLE[],list_filter(overtime_slots,lambda x: x IS NOT NULL)) AS vector
      FROM audit.mirror_period_team) m,
LATERAL range(1,len(m.vector)+1) n(i)
UNION ALL SELECT game_id,team_id,period_number,points FROM audit.mirror_period_tail_receipt;

CREATE TABLE audit.mirror_period_input (
    role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL CHECK(regexp_full_match(sha256,'[0-9a-f]{64}')),
    provenance VARCHAR NOT NULL,redistribution_cleared BOOLEAN NOT NULL CHECK(NOT redistribution_cleared)
);
