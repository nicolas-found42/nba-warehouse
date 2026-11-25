-- Secondary schedule evidence adjudicates the OT flag, not the quarter cells.
CREATE TABLE audit.playoff_ot_schedule_pin (
 season_start INTEGER PRIMARY KEY,source_url VARCHAR NOT NULL UNIQUE,parsed_sha256 VARCHAR NOT NULL,
 game_count INTEGER NOT NULL,ot_count INTEGER NOT NULL
);
CREATE TABLE audit.playoff_ot_schedule (
 season_start INTEGER,source_ordinal INTEGER,game_date DATE NOT NULL,home_label VARCHAR NOT NULL,away_label VARCHAR NOT NULL,
 home_score INTEGER NOT NULL,away_score INTEGER NOT NULL,ot_count INTEGER NOT NULL CHECK(ot_count>=0),
 PRIMARY KEY(season_start,source_ordinal)
);
CREATE TABLE audit.early_ot3_receipt_v16 (
 game_id VARCHAR PRIMARY KEY,game_date DATE NOT NULL,home_id BIGINT NOT NULL,away_id BIGINT NOT NULL,
 home_periods INTEGER[7] NOT NULL,away_periods INTEGER[7] NOT NULL,source_url VARCHAR NOT NULL,
 CHECK(game_id='0026100105' AND source_url='https://www.nba.com/game/'||game_id)
);
CREATE MACRO audit.playoff_ot_label(n) AS (
 CASE lower(replace(n,'.','')) WHEN 'baltimore bullets' THEN 'washington bullets'
      WHEN 'syracuse nationals' THEN 'philadelphia 76ers' ELSE lower(replace(n,'.','')) END
);
CREATE VIEW audit.playoff_ot_fixture AS
SELECT g.game_id,g.season_start,g.game_date,g.home_score,g.away_score,g.is_overtime,g.home_team_id,g.away_team_id,
       coalesce(h.season_team_name,ht.display_name) AS home_label,
       coalesce(a.season_team_name,away_dim.display_name) AS away_label
FROM nba.fact_game g JOIN audit.playoff_ot_schedule_pin p USING(season_start)
JOIN nba.game_team_labels h ON (g.game_id,g.home_team_id,'home')=(h.game_id,h.team_id,h.side)
JOIN nba.game_team_labels a ON (g.game_id,g.away_team_id,'away')=(a.game_id,a.team_id,a.side)
JOIN nba.dim_team ht ON ht.team_id=g.home_team_id JOIN nba.dim_team away_dim ON away_dim.team_id=g.away_team_id
WHERE g.season_type='Playoffs';
CREATE VIEW audit.playoff_ot_schedule_match AS
SELECT f.game_id,r.* FROM audit.playoff_ot_fixture f JOIN audit.playoff_ot_schedule r
 ON (f.season_start,f.game_date,f.home_score,f.away_score,audit.playoff_ot_label(f.home_label),audit.playoff_ot_label(f.away_label))
  =(r.season_start,r.game_date,r.home_score,r.away_score,audit.playoff_ot_label(r.home_label),audit.playoff_ot_label(r.away_label));
CREATE VIEW audit.period_adjudication_v16_keys AS
SELECT a.game_id,'mirror_v16_playoff_ot_flag_adjudicated' AS selected_source
FROM audit.mirror_v13_assessment a JOIN audit.playoff_ot_schedule_match m USING(game_id)
JOIN audit.playoff_ot_fixture f USING(game_id)
WHERE a.disposition='reported_overtime_disagrees' AND m.ot_count=0 AND f.is_overtime IS TRUE
UNION ALL SELECT game_id,'mirror_v16_keyed_official_ot3' FROM audit.early_ot3_receipt_v16;
CREATE VIEW audit.period_adjudication_v16_cells AS
SELECT m.game_id,m.team_id,i::SMALLINT AS period_number,m.regulation[i]::INTEGER AS points
FROM audit.mirror_v13_team m JOIN audit.period_adjudication_v16_keys k USING(game_id),range(1,5) r(i)
WHERE k.selected_source='mirror_v16_playoff_ot_flag_adjudicated'
UNION ALL
SELECT t.game_id,b.team_id,i::SMALLINT,CASE b.side WHEN 'home' THEN t.home_periods[i] ELSE t.away_periods[i] END
FROM audit.early_ot3_receipt_v16 t JOIN nba.bridge_game_team b USING(game_id),range(1,8) r(i);
CREATE VIEW audit.period_adjudication_v16_coverage AS
SELECT k.game_id,'published' AS status,v.candidate_sources+1 AS candidate_sources,1 AS valid_sources,k.selected_source
FROM audit.period_adjudication_v16_keys k JOIN (
 SELECT game_id,candidate_sources FROM audit.mirror_v13_coverage
 UNION ALL SELECT game_id,candidate_sources FROM audit.mirror_v12_coverage
) v USING(game_id);
