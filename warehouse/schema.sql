-- Local contracts adapted from nbadb dim_game, bridge_game_team, and player/team box schemas.
CREATE SCHEMA nba;
CREATE SCHEMA audit;

CREATE TABLE nba.dim_season (
    season_start SMALLINT PRIMARY KEY,
    season_year VARCHAR NOT NULL UNIQUE,
    league_era VARCHAR NOT NULL CHECK (league_era IN ('BAA', 'NBA'))
);
CREATE TABLE nba.dim_team (
    team_id BIGINT PRIMARY KEY CHECK (team_id > 0),
    display_name VARCHAR,
    abbreviation VARCHAR
);
CREATE TABLE nba.dim_team_version (
    team_id BIGINT REFERENCES nba.dim_team(team_id),
    valid_from INT,
    valid_to INT CHECK (valid_to IS NULL OR valid_to >= valid_from),
    full_name VARCHAR,
    abbreviation VARCHAR,
    city VARCHAR,
    state VARCHAR,
    arena VARCHAR,
    year_founded BIGINT,
    conference VARCHAR,
    division VARCHAR,
    is_current BOOLEAN,
    team_kind VARCHAR,
    PRIMARY KEY (team_id, valid_from)
);
CREATE TABLE nba.dim_person (
    person_id BIGINT PRIMARY KEY CHECK (person_id > 0),
    display_name VARCHAR NOT NULL CHECK (length(trim(display_name)) > 0),
    birth_date DATE,
    from_year SMALLINT,
    to_year SMALLINT,
    player_code VARCHAR
);
CREATE TABLE nba.dim_game (
    game_id VARCHAR PRIMARY KEY CHECK (regexp_full_match(game_id, '[0-9]{10}')),
    game_date DATE NOT NULL,
    game_datetime_est TIMESTAMP CHECK (game_datetime_est IS NULL OR game_datetime_est::DATE = game_date),
    season_start SMALLINT NOT NULL REFERENCES nba.dim_season(season_start),
    season_year VARCHAR NOT NULL,
    league_era VARCHAR NOT NULL CHECK (league_era IN ('BAA', 'NBA')),
    season_type VARCHAR NOT NULL CHECK (season_type IN ('Regular', 'Playoffs', 'PlayIn', 'Preseason', 'AllStar', 'Cup', 'Exhibition')),
    counts_toward_regular_season BOOLEAN NOT NULL CHECK (counts_toward_regular_season = (season_type = 'Regular')),
    competition_tag VARCHAR,
    game_label VARCHAR,
    game_sub_label VARCHAR,
    series_game_number INT,
    home_team_id BIGINT REFERENCES nba.dim_team(team_id),
    away_team_id BIGINT REFERENCES nba.dim_team(team_id),
    participant_status VARCHAR NOT NULL CHECK (participant_status IN ('resolved', 'unresolved')),
    result_status VARCHAR NOT NULL CHECK (result_status IN ('final', 'score_unavailable', 'participants_unresolved', 'score_conflict')),
    arena_name VARCHAR,
    arena_city VARCHAR,
    arena_state VARCHAR,
    attendance INT,
    CHECK ((participant_status = 'resolved' AND home_team_id IS NOT NULL AND away_team_id IS NOT NULL AND home_team_id <> away_team_id)
        OR (participant_status = 'unresolved' AND home_team_id IS NULL AND away_team_id IS NULL))
);
CREATE TABLE nba.bridge_game_team (
    game_id VARCHAR REFERENCES nba.dim_game(game_id),
    side VARCHAR CHECK (side IN ('home', 'away')),
    team_id BIGINT NOT NULL REFERENCES nba.dim_team(team_id),
    PRIMARY KEY (game_id, side),
    UNIQUE (game_id, team_id)
);
CREATE TABLE nba.fact_game_result (
    game_id VARCHAR PRIMARY KEY REFERENCES nba.dim_game(game_id),
    home_score INT NOT NULL CHECK (home_score >= 0),
    away_score INT NOT NULL CHECK (away_score >= 0 AND away_score <> home_score),
    winner_team_id BIGINT NOT NULL,
    is_overtime BOOLEAN,
    FOREIGN KEY (game_id, winner_team_id) REFERENCES nba.bridge_game_team(game_id, team_id)
);
CREATE TABLE nba.fact_team_game_stats (
    game_id VARCHAR,
    team_id BIGINT,
    is_home BOOLEAN NOT NULL,
    is_win BOOLEAN,
    min DOUBLE,
    pts INT CHECK (pts >= 0),
    fgm INT, fga INT, fg_pct DOUBLE,
    fg3m INT, fg3a INT, fg3_pct DOUBLE,
    ftm INT, fta INT, ft_pct DOUBLE,
    oreb INT, dreb INT, reb INT, ast INT, tov INT, stl INT, blk INT, blka INT, pf INT, pfd INT,
    plus_minus INT,
    off_rating DOUBLE, def_rating DOUBLE, net_rating DOUBLE,
    ast_pct DOUBLE, ast_to DOUBLE, ast_ratio DOUBLE, oreb_pct DOUBLE, dreb_pct DOUBLE,
    reb_pct DOUBLE, tm_tov_pct DOUBLE, efg_pct DOUBLE, ts_pct DOUBLE, pace DOUBLE, pace_per40 DOUBLE,
    poss DOUBLE, pie DOUBLE, fta_rate DOUBLE, opp_efg_pct DOUBLE, opp_fta_rate DOUBLE,
    opp_tov_pct DOUBLE, opp_oreb_pct DOUBLE,
    pts_off_tov INT, pts_2nd_chance INT, pts_fb INT, pts_paint INT,
    opp_pts_off_tov INT, opp_pts_2nd_chance INT, opp_pts_fb INT, opp_pts_paint INT,
    pct_fga_2pt DOUBLE, pct_fga_3pt DOUBLE, pct_pts_2pt DOUBLE, pct_pts_2pt_mr DOUBLE,
    pct_pts_3pt DOUBLE, pct_pts_fb DOUBLE, pct_pts_ft DOUBLE, pct_pts_off_tov DOUBLE, pct_pts_paint DOUBLE,
    pct_ast_2pm DOUBLE, pct_uast_2pm DOUBLE, pct_ast_3pm DOUBLE, pct_uast_3pm DOUBLE,
    pct_ast_fgm DOUBLE, pct_uast_fgm DOUBLE,
    bench_points INT, biggest_lead INT, biggest_scoring_run INT, lead_changes INT,
    times_tied INT, timeouts_remaining INT,
    CHECK (fgm <= fga AND fg3m <= fg3a AND ftm <= fta),
    PRIMARY KEY (game_id, team_id),
    FOREIGN KEY (game_id, team_id) REFERENCES nba.bridge_game_team(game_id, team_id)
);
CREATE TABLE nba.fact_player_game_stats (
    game_id VARCHAR,
    person_id BIGINT REFERENCES nba.dim_person(person_id),
    team_id BIGINT NOT NULL,
    opponent_team_id BIGINT NOT NULL CHECK (opponent_team_id <> team_id),
    is_home BOOLEAN NOT NULL,
    is_win BOOLEAN,
    participation_status VARCHAR NOT NULL CHECK (participation_status IN ('played', 'dnp', 'not_with_team', 'roster_only', 'unknown')),
    starting_position VARCHAR,
    comment VARCHAR,
    num_minutes DOUBLE CHECK (num_minutes >= 0),
    reported_num_minutes DOUBLE CHECK (reported_num_minutes >= 0),
    points INT CHECK (points >= 0),
    assists INT, blocks INT, steals INT, turnovers INT,
    fga INT, fgm INT, fg_pct DOUBLE,
    fg3a INT, fg3m INT, fg3_pct DOUBLE,
    fta INT, ftm INT, ft_pct DOUBLE,
    oreb INT, dreb INT, reb INT, fouls_personal INT, plus_minus INT,
    off_rating DOUBLE, def_rating DOUBLE, net_rating DOUBLE, ast_pct DOUBLE,
    ast_to_turnover_ratio DOUBLE, ast_ratio DOUBLE, oreb_pct DOUBLE, dreb_pct DOUBLE,
    reb_pct DOUBLE, tov_pct DOUBLE, efg_pct DOUBLE, ts_pct DOUBLE, usg_pct DOUBLE, pace DOUBLE, pie DOUBLE,
    CHECK (fgm <= fga AND fg3m <= fg3a AND ftm <= fta),
    CHECK (points = 2*fgm + fg3m + ftm),
    PRIMARY KEY (game_id, person_id),
    FOREIGN KEY (game_id, team_id) REFERENCES nba.bridge_game_team(game_id, team_id),
    FOREIGN KEY (game_id, opponent_team_id) REFERENCES nba.bridge_game_team(game_id, team_id)
);

-- Audit keys intentionally do not reference NBA dimensions: they must preserve invalid keys.
CREATE TABLE audit.input_manifest (
    id BOOLEAN PRIMARY KEY CHECK (id),
    source_path VARCHAR NOT NULL,
    source_size BIGINT NOT NULL,
    source_mtime_ns BIGINT NOT NULL,
    source_version VARCHAR,
    duckdb_version VARCHAR NOT NULL,
    game_rows BIGINT NOT NULL CHECK (game_rows > 0),
    team_box_rows BIGINT NOT NULL CHECK (team_box_rows >= 0),
    player_box_rows BIGINT NOT NULL CHECK (player_box_rows >= 0)
);
CREATE TABLE audit.game_disposition (
    game_id VARCHAR PRIMARY KEY,
    source_season_year VARCHAR NOT NULL,
    source_season_type VARCHAR NOT NULL,
    source_home_team_id BIGINT,
    source_away_team_id BIGINT,
    source_home_score INT,
    source_away_score INT,
    season_reclassified BOOLEAN NOT NULL,
    timestamp_withheld BOOLEAN NOT NULL,
    participant_status VARCHAR NOT NULL CHECK (participant_status IN ('resolved', 'unresolved')),
    result_status VARCHAR NOT NULL CHECK (result_status IN ('final', 'score_unavailable', 'participants_unresolved', 'score_conflict'))
);
CREATE TABLE audit.team_box_disposition (
    game_id VARCHAR,
    team_id BIGINT,
    disposition VARCHAR NOT NULL CHECK (disposition IN ('published', 'held')),
    reason VARCHAR,
    withheld_fga BOOLEAN NOT NULL,
    withheld_fg3a BOOLEAN NOT NULL,
    withheld_fta BOOLEAN NOT NULL,
    CHECK ((disposition = 'held') = (reason IS NOT NULL)),
    PRIMARY KEY (game_id, team_id)
);
CREATE TABLE audit.player_box_disposition (
    game_id VARCHAR,
    person_id BIGINT,
    source_team_id BIGINT,
    disposition VARCHAR NOT NULL CHECK (disposition IN ('published', 'held')),
    reason VARCHAR,
    side_corrected BOOLEAN NOT NULL,
    opponent_corrected BOOLEAN NOT NULL,
    minutes_withheld BOOLEAN NOT NULL,
    CHECK ((disposition = 'held') = (reason IS NOT NULL)),
    PRIMARY KEY (game_id, person_id)
);
CREATE TABLE audit.known_fixture (
    game_id VARCHAR PRIMARY KEY,
    season_year VARCHAR NOT NULL,
    disposition VARCHAR NOT NULL CHECK (disposition IN ('scheduled_not_played', 'played_missing_from_snapshot')),
    evidence_url VARCHAR NOT NULL
);
