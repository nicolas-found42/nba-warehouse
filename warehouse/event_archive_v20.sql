-- Two independently keyed snapshot gaps. Source-native action IDs, not locator matching.
CREATE TABLE audit.event_archive_v20 (
 game_id VARCHAR,domain VARCHAR CHECK(domain IN ('events','shotdetail')),archive_name VARCHAR NOT NULL,
 source_url VARCHAR NOT NULL,sha256 VARCHAR NOT NULL,source_bytes BIGINT NOT NULL,expected_rows INTEGER NOT NULL,
 redistribution_cleared BOOLEAN NOT NULL CHECK(NOT redistribution_cleared),PRIMARY KEY(game_id,domain)
);
CREATE TABLE audit.event_source_payload_v20 (
 domain VARCHAR,game_id VARCHAR,action_number INTEGER,payload VARCHAR NOT NULL CHECK(json_valid(payload)),
 PRIMARY KEY(domain,game_id,action_number)
);
CREATE TABLE audit.event_archive_parent_v20 (role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);
CREATE TABLE nba.bridge_shot_event_source_v20 (
 shot_key BLOB PRIMARY KEY REFERENCES nba.fact_shot(shot_key),game_id VARCHAR NOT NULL,action_number INTEGER NOT NULL,
 match_method VARCHAR NOT NULL CHECK(match_method='nba_stats_game_event_id'),UNIQUE(game_id,action_number),
 FOREIGN KEY(game_id,action_number) REFERENCES nba.fact_play_event(game_id,action_number)
);
CREATE VIEW nba.shot_event_link AS
SELECT shot_key,game_id,action_number,match_method FROM nba.bridge_shot_event
UNION ALL SELECT * FROM nba.bridge_shot_event_source_v20;
