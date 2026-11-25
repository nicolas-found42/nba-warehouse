-- Verified supplemental fixtures and box rows, separate from the original-spine accounting.
CREATE TABLE nba.dim_event_edition (
    edition_key VARCHAR PRIMARY KEY,
    season_year VARCHAR NOT NULL REFERENCES nba.dim_season(season_year),
    competition VARCHAR NOT NULL,
    competition_format VARCHAR NOT NULL,
    evidence_url VARCHAR NOT NULL
);
INSERT INTO nba.dim_event_edition VALUES
 ('AS-2025','2024-25','NBA All-Star','two_semifinals_and_final','https://www.nba.com/news/nightly-pulse-nba-news-highlights-from-feb-16'),
 ('AS-2026','2025-26','NBA All-Star','round_robin_and_final','https://www.nba.com/news/2026-nba-all-star-game-new-format');

-- Do not merge the existing descriptive 90000000xx placeholders with NBA game
-- source squad IDs. Only IDs backed by the specific fixture are used below.
INSERT INTO nba.dim_team VALUES
 (1610616851,'Chuck''s Global Stars','CHK'), (1610616852,'Shaq''s OGs','SHQ'),
 (1610616853,'Kenny''s Young Stars','KEN'), (1610616854,'Candace''s Rising Stars','CAN');

CREATE TABLE audit.fixture_addition (
    game_id VARCHAR PRIMARY KEY,
    game_date DATE NOT NULL,
    edition_key VARCHAR NOT NULL,
    stage VARCHAR NOT NULL CHECK(stage IN ('semifinal','final')),
    fixture_number INTEGER NOT NULL CHECK(fixture_number>0),
    home_team_id BIGINT NOT NULL,
    away_team_id BIGINT NOT NULL CHECK(away_team_id<>home_team_id),
    home_score INTEGER NOT NULL,
    away_score INTEGER NOT NULL CHECK(away_score<>home_score),
    team_player_seconds INTEGER NOT NULL CHECK(team_player_seconds>0),
    attendance INTEGER,
    evidence_url VARCHAR NOT NULL
);
INSERT INTO audit.fixture_addition VALUES
 ('0032400011',DATE '2025-02-16','AS-2025','semifinal',1,1610616851,1610616853,41,32,3035,NULL,
  'https://statsdmz.nba.com/pdfs/20250216/20250216_KENCHK.pdf'),
 ('0032400021',DATE '2025-02-16','AS-2025','semifinal',2,1610616852,1610616854,42,35,3870,17539,
  'https://statsdmz.nba.com/pdfs/20250216/20250216_CANSHQ.pdf'),
 ('0032400031',DATE '2025-02-16','AS-2025','final',3,1610616851,1610616852,25,41,3200,17539,
  'https://statsdmz.nba.com/pdfs/20250216/20250216_SHQCHK.pdf');

INSERT INTO nba.dim_game (
   game_id,game_date,season_start,season_year,league_era,season_type,
   counts_toward_regular_season,competition_tag,game_label,game_sub_label,
   home_team_id,away_team_id,participant_status,result_status,arena_name,arena_city,arena_state,attendance
)
SELECT f.game_id,f.game_date,s.season_start,s.season_year,s.league_era,'AllStar',false,'NBA All-Star',
       '2025 NBA All-Star Game',f.stage,f.home_team_id,f.away_team_id,'resolved','final',
       'Chase Center','San Francisco','CA',f.attendance
FROM audit.fixture_addition f JOIN nba.dim_event_edition e USING(edition_key) JOIN nba.dim_season s USING(season_year);

INSERT INTO nba.bridge_game_team
SELECT game_id,'home',home_team_id FROM audit.fixture_addition
UNION ALL SELECT game_id,'away',away_team_id FROM audit.fixture_addition;
INSERT INTO nba.fact_game_result
SELECT game_id,home_score,away_score,
       CASE WHEN home_score>away_score THEN home_team_id ELSE away_team_id END,false
FROM audit.fixture_addition;

CREATE TABLE nba.bridge_event_fixture (
    game_id VARCHAR PRIMARY KEY REFERENCES nba.dim_game(game_id),
    edition_key VARCHAR NOT NULL REFERENCES nba.dim_event_edition(edition_key),
    component VARCHAR NOT NULL CHECK(component IN ('all_star','rising_stars')),
    stage VARCHAR NOT NULL CHECK(stage IN ('semifinal','round_robin','final')),
    fixture_number INTEGER NOT NULL CHECK(fixture_number>0),
    neutral_site BOOLEAN NOT NULL,
    regulation_periods SMALLINT CHECK(regulation_periods>0),
    UNIQUE(edition_key,component,fixture_number)
);
INSERT INTO nba.bridge_event_fixture
SELECT game_id,edition_key,'all_star',stage,fixture_number,true,1 FROM audit.fixture_addition;
INSERT INTO nba.bridge_event_fixture
SELECT g.game_id,'AS-2026',v.component,v.stage,v.fixture_number,true,v.regulation_periods
FROM nba.dim_game g JOIN (VALUES
 ('0032500004','rising_stars','semifinal',1,NULL),('0032500005','rising_stars','semifinal',2,NULL),
 ('0032500006','rising_stars','final',3,NULL),('0032500011','all_star','round_robin',1,1),
 ('0032500021','all_star','round_robin',2,1),('0032500031','all_star','round_robin',3,1),
 ('0032500041','all_star','final',4,1)) v(game_id,component,stage,fixture_number,regulation_periods) USING(game_id)
WHERE g.season_year='2025-26' AND g.season_type='AllStar';

CREATE TABLE audit.fixture_added_player_box (
    game_id VARCHAR,
    person_id BIGINT,
    team_id BIGINT NOT NULL,
    raw_min_mm_ss DOUBLE,
    minutes_seconds INTEGER,
    source_relation VARCHAR NOT NULL,
    plus_minus_status VARCHAR NOT NULL,
    PRIMARY KEY(game_id,person_id)
);
INSERT INTO audit.fixture_added_player_box
SELECT p.game_id,p.player_id,p.team_id,p.min,
       (floor(p.min)*60+round((p.min-floor(p.min))*100))::INTEGER,
       'src.main.fact_player_game_boxscore','withheld_source_conflict'
FROM src.main.fact_player_game_boxscore p JOIN audit.fixture_addition f USING(game_id)
JOIN nba.bridge_game_team bt ON (p.game_id,p.team_id)=(bt.game_id,bt.team_id)
JOIN nba.dim_person x ON p.player_id=x.person_id
WHERE p.is_home IS NOT DISTINCT FROM (bt.side='home')
      AND p.opponent_team_id=CASE bt.side WHEN 'home' THEN f.away_team_id ELSE f.home_team_id END
      AND (p.min IS NULL OR (p.min>=0 AND round((p.min-floor(p.min))*100) BETWEEN 0 AND 59));

-- Keep verified basic fields. Ratings and conflicting plus-minus are not promoted.
INSERT INTO nba.fact_player_game_stats (
  game_id,person_id,team_id,opponent_team_id,is_home,is_win,participation_status,starting_position,comment,
  num_minutes,reported_num_minutes,points,assists,blocks,steals,turnovers,fga,fgm,fg_pct,fg3a,fg3m,fg3_pct,
  fta,ftm,ft_pct,oreb,dreb,reb,fouls_personal
)
SELECT p.game_id,p.player_id,p.team_id,p.opponent_team_id,p.is_home,(p.team_id=r.winner_team_id),
       CASE WHEN p.comment LIKE 'DNP%' THEN 'dnp' WHEN a.minutes_seconds>0 THEN 'played' ELSE 'unknown' END,
       p.starting_position,p.comment,a.minutes_seconds/60.0,p.min,p.points,p.assists,p.blocks,p.steals,p.turnovers,
       p.fga,p.fgm,p.fg_pct,p.fg3a,p.fg3m,p.fg3_pct,p.fta,p.ftm,p.ft_pct,p.oreb,p.dreb,p.reb,p.fouls_personal
FROM src.main.fact_player_game_boxscore p JOIN audit.fixture_added_player_box a
ON (p.game_id,p.player_id)=(a.game_id,a.person_id)
JOIN nba.fact_game_result r ON r.game_id=p.game_id;

CREATE TABLE audit.fixture_added_team_box (
    game_id VARCHAR,team_id BIGINT,
    team_player_seconds INTEGER NOT NULL,
    scorer_report_url VARCHAR NOT NULL,
    PRIMARY KEY(game_id,team_id)
);
INSERT INTO audit.fixture_added_team_box
SELECT b.game_id,b.team_id,f.team_player_seconds,f.evidence_url
FROM nba.bridge_game_team b JOIN audit.fixture_addition f USING(game_id);
INSERT INTO nba.fact_team_game_stats (
   game_id,team_id,is_home,is_win,min,pts,fgm,fga,fg3m,fg3a,ftm,fta,oreb,dreb,reb,ast,tov,stl,blk,pf
)
SELECT b.game_id,b.team_id,(b.side='home'),(b.team_id=r.winner_team_id),
       sum(a.minutes_seconds)/60.0,sum(p.points),sum(p.fgm),sum(p.fga),sum(p.fg3m),sum(p.fg3a),sum(p.ftm),sum(p.fta),
       sum(p.oreb),sum(p.dreb),sum(p.reb),sum(p.assists),sum(p.turnovers),sum(p.steals),sum(p.blocks),sum(p.fouls_personal)
FROM audit.fixture_added_player_box a JOIN nba.fact_player_game_stats p USING(game_id,person_id,team_id)
JOIN nba.bridge_game_team b USING(game_id,team_id) JOIN nba.fact_game_result r USING(game_id)
GROUP BY 1,2,3,4;

CREATE TABLE nba.fact_regular_standings (
    season_year VARCHAR REFERENCES nba.dim_season(season_year),
    team_id BIGINT REFERENCES nba.dim_team(team_id),
    games INTEGER NOT NULL CHECK(games>0), wins INTEGER NOT NULL CHECK(wins>=0),
    losses INTEGER NOT NULL CHECK(losses>=0), points_for INTEGER NOT NULL, points_against INTEGER NOT NULL,
    PRIMARY KEY(season_year,team_id), CHECK(wins+losses=games)
);
INSERT INTO nba.fact_regular_standings
SELECT g.season_year,b.team_id,count(*),count(*) FILTER(WHERE r.winner_team_id=b.team_id),
       count(*) FILTER(WHERE r.winner_team_id<>b.team_id),
       sum(CASE b.side WHEN 'home' THEN r.home_score ELSE r.away_score END),
       sum(CASE b.side WHEN 'home' THEN r.away_score ELSE r.home_score END)
FROM nba.dim_game g JOIN nba.fact_game_result r USING(game_id) JOIN nba.bridge_game_team b USING(game_id)
WHERE g.counts_toward_regular_season GROUP BY 1,2;
