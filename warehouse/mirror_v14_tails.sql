CREATE TABLE audit.mirror_v14_tail (
    game_id VARCHAR PRIMARY KEY,game_date DATE NOT NULL,home_id BIGINT NOT NULL,away_id BIGINT NOT NULL,
    home_periods INTEGER[7] NOT NULL,away_periods INTEGER[7] NOT NULL,source_url VARCHAR NOT NULL UNIQUE,
    CHECK(source_url='https://www.nba.com/game/'||game_id)
);
CREATE VIEW audit.mirror_v14_tail_cells AS
SELECT t.game_id,b.team_id,i::SMALLINT AS period_number,
       CASE b.side WHEN 'home' THEN t.home_periods[i] ELSE t.away_periods[i] END AS points
FROM audit.mirror_v14_tail t JOIN nba.bridge_game_team b USING(game_id), range(1,8) n(i);

CREATE VIEW audit.mirror_v14_tail_coverage AS
SELECT t.game_id,'published' AS status,v.candidate_sources+1 AS candidate_sources,v.valid_sources+1 AS valid_sources,
       'mirror_v14_keyed_official_ot3' AS selected_source
FROM audit.mirror_v14_tail t JOIN audit.mirror_v13_coverage v USING(game_id);

CREATE VIEW audit.mirror_v14_changed_keys AS
SELECT game_id FROM audit.mirror_v14_scope UNION ALL SELECT game_id FROM audit.mirror_v14_tail;
