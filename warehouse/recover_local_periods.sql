-- Supplement v5 only where the original line score reverses the canonical sides.
-- The same upstream can populate several relations: exact agreement checks projection
-- integrity, not independent archival provenance. Keep every reviewed game visible.
CREATE TABLE audit.local_period_recovery (
    game_id VARCHAR PRIMARY KEY,
    prior_status VARCHAR NOT NULL,
    original_line_rows INTEGER NOT NULL CHECK(original_line_rows > 0),
    distinct_line_payloads INTEGER NOT NULL CHECK(distinct_line_payloads BETWEEN 1 AND original_line_rows),
    original_line_date DATE,
    original_home_team_id BIGINT NOT NULL,
    original_away_team_id BIGINT NOT NULL,
    original_home_final INTEGER,
    original_away_final INTEGER,
    home_periods INTEGER[],
    away_periods INTEGER[],
    status VARCHAR NOT NULL CHECK(status IN ('admitted','prior_source_conflict','nonunique_line','line_key_or_date_disagree',
       'line_final_disagrees','invalid_period_vector','possible_balanced_fill','scoreboard_vector_disagrees','final_period_not_confirmed')),
    CHECK(status<>'admitted' OR (home_periods IS NOT NULL AND away_periods IS NOT NULL))
);

INSERT INTO audit.local_period_recovery
WITH games AS (
    SELECT g.game_id,g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score,c.status AS prior_status
    FROM nba.fact_game g JOIN audit.period_coverage c USING(game_id)
    WHERE c.status IN ('no_supported_source','no_valid_candidate','valid_sources_disagree')
), line_raw AS (
    SELECT g.game_id, l.game_id AS literal_game_id, l.source_table,try_cast(l.game_date_est AS DATE) AS line_date,
        l.pts_away AS canonical_home,l.pts_home AS canonical_away,
        [try_cast(l.pts_qtr1_away AS INT),try_cast(l.pts_qtr2_away AS INT),try_cast(l.pts_qtr3_away AS INT),try_cast(l.pts_qtr4_away AS INT),
         l.pts_ot1_away,l.pts_ot2_away,l.pts_ot3_away,l.pts_ot4_away,l.pts_ot5_away,l.pts_ot6_away,l.pts_ot7_away,l.pts_ot8_away,l.pts_ot9_away,l.pts_ot10_away] AS hp,
        [try_cast(l.pts_qtr1_home AS INT),try_cast(l.pts_qtr2_home AS INT),try_cast(l.pts_qtr3_home AS INT),try_cast(l.pts_qtr4_home AS INT),
         l.pts_ot1_home,l.pts_ot2_home,l.pts_ot3_home,l.pts_ot4_home,l.pts_ot5_home,l.pts_ot6_home,l.pts_ot7_home,l.pts_ot8_home,l.pts_ot9_home,l.pts_ot10_home] AS ap
    FROM games g JOIN src.main.fact_game_line_score l ON g.game_id=l.normalized_game_id
    WHERE try_cast(l.team_id_home AS BIGINT)=g.away_team_id AND try_cast(l.team_id_away AS BIGINT)=g.home_team_id
), line_counts AS (
    SELECT game_id,count(*)::INT AS n,count(DISTINCT r)::INT AS nd FROM line_raw r GROUP BY game_id
), singles AS (
    SELECT DISTINCT r.* FROM line_raw r JOIN line_counts c USING(game_id) WHERE c.nd=1
), lines AS (
    SELECT g.*,c.n,c.nd,r.literal_game_id,r.source_table,r.line_date,r.canonical_home,r.canonical_away,r.hp,r.ap,
           greatest(4,coalesce((SELECT max(i) FROM range(1,15) p(i) WHERE coalesce(r.hp[i],0)<>0 OR coalesce(r.ap[i],0)<>0),0)) AS played
    FROM line_counts c JOIN games g USING(game_id) LEFT JOIN singles r USING(game_id)
), scoreboard AS (
    SELECT DISTINCT l.game_id,s.team_id,try_cast(s.game_date_est AS DATE) AS score_date,s.pts AS score,
        [try_cast(s.pts_qtr1 AS INT),try_cast(s.pts_qtr2 AS INT),try_cast(s.pts_qtr3 AS INT),try_cast(s.pts_qtr4 AS INT),
         s.pts_ot1,s.pts_ot2,s.pts_ot3,s.pts_ot4,s.pts_ot5,s.pts_ot6,s.pts_ot7,s.pts_ot8,s.pts_ot9,s.pts_ot10] AS pts
    FROM lines l JOIN src.main.fact_scoreboard_line_score s USING(game_id)
), reasoned AS (
    SELECT l.*,
      CASE WHEN l.prior_status='valid_sources_disagree' THEN 'prior_source_conflict'
       WHEN nd<>1 OR (SELECT count(*) FROM src.main.fact_game_line_score all_lines WHERE all_lines.normalized_game_id=l.game_id)<>n THEN 'nonunique_line'
       WHEN literal_game_id IS DISTINCT FROM l.game_id OR source_table IS DISTINCT FROM 'nba__line_score' OR line_date IS DISTINCT FROM l.game_date THEN 'line_key_or_date_disagree'
       WHEN canonical_home IS DISTINCT FROM l.home_score OR canonical_away IS DISTINCT FROM l.away_score THEN 'line_final_disagrees'
       WHEN list_sum(hp) IS DISTINCT FROM l.home_score OR list_sum(ap) IS DISTINCT FROM l.away_score OR EXISTS (
           SELECT 1 FROM range(1,played+1) p(i) WHERE hp[i] IS NULL OR ap[i] IS NULL OR least(hp[i],ap[i])<0
              OR hp[i]+ap[i]=0 OR (i>=4 AND i<played AND list_sum(hp[:i])<>list_sum(ap[:i]))) THEN 'invalid_period_vector'
       WHEN hp[1]=hp[2] AND hp[2]=hp[3] AND ap[1]=ap[2] AND ap[2]=ap[3] THEN 'possible_balanced_fill'
       WHEN (SELECT count(*) FROM scoreboard s WHERE s.game_id=l.game_id)<>2
           OR NOT EXISTS (SELECT 1 FROM scoreboard s WHERE s.game_id=l.game_id AND s.score_date=l.game_date
                          AND (s.team_id,s.score,s.pts) IS NOT DISTINCT FROM (l.home_team_id,l.home_score,l.hp))
           OR NOT EXISTS (SELECT 1 FROM scoreboard s WHERE s.game_id=l.game_id AND s.score_date=l.game_date
                          AND (s.team_id,s.score,s.pts) IS NOT DISTINCT FROM (l.away_team_id,l.away_score,l.ap)) THEN 'scoreboard_vector_disagrees'
       WHEN NOT coalesce((SELECT bool_and(coalesce(try_cast(s.game_date_est AS DATE)=l.game_date AND s.game_status_id=3 AND s.live_period=l.played
              AND (try_cast(s.home_team_id AS BIGINT),try_cast(s.visitor_team_id AS BIGINT)) IN ((l.home_team_id,l.away_team_id),(l.away_team_id,l.home_team_id)),false))
              FROM src.main.game_summary s WHERE s.game_id=l.game_id),false)
           OR NOT coalesce((SELECT bool_and(coalesce(try_cast(s.game_et AS DATE)=l.game_date AND s.game_status=3 AND s.period=l.played
              AND (try_cast(s.home_team_id AS BIGINT),try_cast(s.away_team_id AS BIGINT)) IN ((l.home_team_id,l.away_team_id),(l.away_team_id,l.home_team_id)),false))
              FROM src.main.fact_box_score_summary_v3_game_summary s WHERE s.game_id=l.game_id),false) THEN 'final_period_not_confirmed'
       ELSE 'admitted' END AS status
    FROM lines l
)
SELECT game_id,prior_status,n,nd,line_date,away_team_id,home_team_id,try_cast(canonical_away AS INT),try_cast(canonical_home AS INT),
       hp[:played],ap[:played],status FROM reasoned;

INSERT INTO nba.fact_game_period_score
SELECT a.game_id,b.team_id,i,CASE b.side WHEN 'home' THEN a.home_periods[i] ELSE a.away_periods[i] END
FROM audit.local_period_recovery a JOIN nba.bridge_game_team b USING(game_id), LATERAL range(1,len(a.home_periods)+1) p(i)
WHERE a.status='admitted';

UPDATE audit.period_coverage c
SET status='published',candidate_sources=c.candidate_sources+1,valid_sources=c.valid_sources+1,
    selected_source='local_line_score_reoriented'
FROM audit.local_period_recovery a WHERE c.game_id=a.game_id AND a.status='admitted';
