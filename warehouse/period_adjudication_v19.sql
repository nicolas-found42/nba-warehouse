-- Accession exact printed quarter cells; keep reported totals as observations.
CREATE TABLE audit.period_adjudication_v19 (
 game_id VARCHAR PRIMARY KEY,game_date DATE NOT NULL,home_id BIGINT NOT NULL,away_id BIGINT NOT NULL,
 old_home INTEGER NOT NULL,old_away INTEGER NOT NULL,page_home_final INTEGER NOT NULL,page_away_final INTEGER NOT NULL,
 new_home INTEGER NOT NULL,new_away INTEGER NOT NULL,home_cells INTEGER[4] NOT NULL,away_cells INTEGER[4] NOT NULL,
 nba_page VARCHAR NOT NULL,comparison_locator VARCHAR NOT NULL,comparison_scope VARCHAR NOT NULL,
 old_period_status VARCHAR NOT NULL,old_candidates INTEGER NOT NULL,
 CHECK (new_home<>new_away AND (old_home>old_away)=(new_home>new_away)),
 CHECK ((page_home_final,page_away_final)<>(new_home,new_away)),
 CHECK (new_home=list_sum(home_cells) AND new_away=list_sum(away_cells)),
 CHECK (old_period_status IN ('no_valid_candidate','no_supported_source'))
);
CREATE TABLE audit.period_v19_input(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);
CREATE VIEW audit.period_cells_v19 AS
SELECT r.game_id,b.team_id,i::SMALLINT AS period_number,
       CASE b.side WHEN 'home' THEN r.home_cells[i] ELSE r.away_cells[i] END AS points
FROM audit.period_adjudication_v19 r JOIN nba.bridge_game_team b USING(game_id),range(1,5) p(i);
CREATE VIEW audit.period_availability_v19 AS
SELECT v.* REPLACE (coalesce(CASE WHEN r.game_id IS NOT NULL THEN 'official_v19_header_disagrees' END,
                       v.recovery_disposition) AS recovery_disposition)
FROM audit.period_availability_v16 v LEFT JOIN audit.period_adjudication_v19 r USING(game_id);
