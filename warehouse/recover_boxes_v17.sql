-- Reconstruct from the read-only v16 parent (prior) and original snapshot (src).
CREATE TEMP TABLE team_scope_v17 AS
SELECT g.game_id FROM prior.nba.dim_game g
ANTI JOIN prior.nba.fact_team_game_stats t USING(game_id)
WHERE g.result_status='final' AND g.season_start IN (2024,2025) AND g.season_type='Regular';

CREATE TEMP TABLE team_input_v17 AS
SELECT b.* EXCLUDE(team_name,team_abbreviation,team_city,team_slug), count(*) AS source_multiplicity
FROM src.main.fact_box_score_team b SEMI JOIN team_scope_v17 USING(game_id)
GROUP BY ALL;

CREATE TEMP TABLE team_observation_v17 AS
WITH players AS (
 SELECT p.game_id,p.team_id,sum(p.turnovers) AS player_tov,
        sum(p.points) AS pts,sum(p.fgm) AS fgm,sum(p.fga) AS fga,
        sum(p.fg3m) AS fg3m,sum(p.fg3a) AS fg3a,sum(p.ftm) AS ftm,sum(p.fta) AS fta,
        sum(p.oreb) AS oreb,sum(p.dreb) AS dreb,sum(p.reb) AS reb,sum(p.assists) AS ast,
        sum(p.steals) AS stl,sum(p.blocks) AS blk,sum(p.fouls_personal) AS pf
 FROM prior.nba.fact_player_game_stats p SEMI JOIN team_scope_v17 USING(game_id) GROUP BY 1,2
), periods AS (
 SELECT p.game_id,p.team_id,list(p.points ORDER BY p.period_number) AS scores,
        list(p.period_number ORDER BY p.period_number) AS numbers
 FROM prior.nba.fact_game_period_score p SEMI JOIN team_scope_v17 USING(game_id) GROUP BY 1,2
)
SELECT b.game_id,b.team_id,b.source_multiplicity,b.min AS reported_elapsed_minutes,
       s.tov AS mirror_player_line_turnovers,p.player_tov,b.tov AS team_total_turnovers,
       'src.main.fact_box_score_team' AS traditional_source,
       'src.main.stg_kaggle_team_box' AS situational_source,
       'partial_traditional_and_situational' AS disposition,
       coalesce(k.team_id IS NOT NULL AND s.team_id IS NOT NULL AND p.team_id IS NOT NULL
         AND b.pts=CASE k.side WHEN 'home' THEN g.home_score ELSE g.away_score END
         AND b.plus_minus=b.pts-CASE k.side WHEN 'home' THEN g.away_score ELSE g.home_score END
         AND s.game_date=g.game_date AND s.season_year=g.season_year AND s.game_type='Regular Season'
         AND s.opponent_team_id=CASE k.side WHEN 'home' THEN g.away_team_id ELSE g.home_team_id END
         AND s.is_home=(k.side='home') AND s.is_win=(g.winner_team_id=b.team_id)
         AND b.min=48 AND s.min=5*b.min AND q.numbers=[1,2,3,4]
         AND q.scores=[s.q1_points,s.q2_points,s.q3_points,s.q4_points]
         AND b.pts=2*b.fgm+b.fg3m+b.ftm AND b.fg3m<=b.fgm AND b.fg3a<=b.fga
         AND b.fgm<=b.fga AND b.fg3m<=b.fg3a AND b.ftm<=b.fta
         AND b.reb=b.oreb+b.dreb AND b.tov>=s.tov AND s.tov=p.player_tov
         AND list_count([b.pts,b.fgm,b.fga,b.fg3m,b.fg3a,b.ftm,b.fta,b.oreb,b.dreb,b.reb,b.ast,b.stl,b.blk,b.tov,b.pf,
                         s.bench_points,s.biggest_lead,s.biggest_scoring_run,s.lead_changes,s.times_tied,s.timeouts_remaining])=21
         AND list_bool_and(list_transform([b.pts,b.fgm,b.fga,b.fg3m,b.fg3a,b.ftm,b.fta,b.oreb,b.dreb,b.reb,b.ast,b.stl,b.blk,b.tov,b.pf,
                         s.bench_points,s.biggest_lead,s.biggest_scoring_run,s.lead_changes,s.times_tied,s.timeouts_remaining],
                         lambda x: isfinite(x) AND x BETWEEN 0 AND 2147483647 AND x=trunc(x)))
         AND b.plus_minus=trunc(b.plus_minus) AND b.fg_pct BETWEEN 0 AND 1 AND b.fg3_pct BETWEEN 0 AND 1 AND b.ft_pct BETWEEN 0 AND 1
         AND (b.pts,b.fgm,b.fga,b.fg3m,b.fg3a,b.ftm,b.fta,b.oreb,b.dreb,b.reb,b.ast,b.stl,b.blk,b.pf)
             = (s.pts,s.fgm,s.fga,s.fg3m,s.fg3a,s.ftm,s.fta,s.oreb,s.dreb,s.reb,s.ast,s.stl,s.blk,s.pf)
         AND (b.pts,b.fgm,b.fga,b.fg3m,b.fg3a,b.ftm,b.fta,b.oreb,b.dreb,b.reb,b.ast,b.stl,b.blk,b.pf)
             = (p.pts,p.fgm,p.fga,p.fg3m,p.fg3a,p.ftm,p.fta,p.oreb,p.dreb,p.reb,p.ast,p.stl,p.blk,p.pf),false) AS valid
FROM team_input_v17 b JOIN prior.nba.fact_game g USING(game_id)
LEFT JOIN prior.nba.bridge_game_team k USING(game_id,team_id)
LEFT JOIN src.main.stg_kaggle_team_box s USING(game_id,team_id)
LEFT JOIN players p USING(game_id,team_id) LEFT JOIN periods q USING(game_id,team_id);

-- Empty clone preserves the fact column types; BY NAME leaves unsupported fields NULL.
CREATE TEMP TABLE team_publication_v17 AS SELECT * FROM prior.nba.fact_team_game_stats LIMIT 0;
INSERT INTO team_publication_v17 BY NAME
SELECT b.game_id,b.team_id,k.side='home' AS is_home,b.team_id=g.winner_team_id AS is_win,5*b.min AS min,
       b.pts,b.fgm,b.fga,b.fg_pct,b.fg3m,b.fg3a,b.fg3_pct,b.ftm,b.fta,b.ft_pct,
       b.oreb,b.dreb,b.reb,b.ast,b.tov,b.stl,b.blk,b.pf,b.plus_minus,
       s.bench_points,s.biggest_lead,s.biggest_scoring_run,s.lead_changes,s.times_tied,s.timeouts_remaining
FROM team_input_v17 b JOIN team_observation_v17 a USING(game_id,team_id)
JOIN prior.nba.fact_game g USING(game_id) JOIN prior.nba.bridge_game_team k USING(game_id,team_id)
JOIN src.main.stg_kaggle_team_box s USING(game_id,team_id) WHERE a.valid;

CREATE TEMP TABLE player_scope_v17 AS
SELECT g.game_id FROM prior.nba.dim_game g
ANTI JOIN prior.nba.fact_player_game_stats p USING(game_id) WHERE g.result_status='final';

CREATE TEMP TABLE player_input_v17 AS
SELECT p.*,g.season_start,k.side,x.display_name, g.game_date,g.winner_team_id,
       t.season_year AS traditional_reported_season,
       CASE k.side WHEN 'home' THEN g.home_score ELSE g.away_score END AS side_final,
       CASE k.side WHEN 'home' THEN g.away_team_id ELSE g.home_team_id END AS resolved_opponent,
       CASE WHEN p.min BETWEEN 0 AND 48
         THEN (floor(p.min)*60+round((p.min-floor(p.min))*100))::INTEGER END AS minute_seconds,
       coalesce(p.min>0 AND round((p.min-floor(p.min))*100) BETWEEN 0 AND 59,false) AS valid_mm_ss,
       coalesce(s.game_date=g.game_date AND s.game_type='All-Star Game' AND k.team_id IS NOT NULL
         AND p.is_home=(k.side='home') AND p.opponent_team_id=resolved_opponent
         AND s.team_id=p.team_id AND s.is_home=p.is_home AND s.opponent_team_id=p.opponent_team_id
         AND t.team_id=p.team_id
         AND (p.min,p.points,p.fgm,p.fga,p.fg3m,p.fg3a,p.ftm,p.fta,p.oreb,p.dreb,p.reb,p.assists,p.steals,p.blocks,p.turnovers,p.fouls_personal)
             IS NOT DISTINCT FROM (s.num_minutes,s.points,s.fgm,s.fga,s.fg3m,s.fg3a,s.ftm,s.fta,s.oreb,s.dreb,s.reb,s.assists,s.steals,s.blocks,s.turnovers,s.fouls_personal)
         AND (p.min,p.points,p.fgm,p.fga,p.fg3m,p.fg3a,p.ftm,p.fta,p.oreb,p.dreb,p.reb,p.assists,p.steals,p.blocks,p.turnovers,p.fouls_personal)
             IS NOT DISTINCT FROM (t.min,t.pts,t.fgm,t.fga,t.fg3m,t.fg3a,t.ftm,t.fta,t.oreb,t.dreb,t.reb,t.ast,t.stl,t.blk,t.tov,t.pf)
         AND s.fg_pct IS NOT DISTINCT FROM p.fg_pct AND t.fg_pct IS NOT DISTINCT FROM p.fg_pct
         AND s.fg3_pct IS NOT DISTINCT FROM p.fg3_pct AND t.fg3_pct IS NOT DISTINCT FROM p.fg3_pct
         AND s.ft_pct IS NOT DISTINCT FROM p.ft_pct AND t.ft_pct IS NOT DISTINCT FROM p.ft_pct,false) AS exact_siblings,
       coalesce(p.player_id>0 AND p.player_id NOT BETWEEN 1962000000 AND 1969999999
         AND upper(trim(x.display_name)) NOT IN ('','UNKNOWN')
         AND EXISTS (SELECT 1 FROM src.main.dim_player d WHERE d.player_id=p.player_id)
         AND x.from_year<=year(g.game_date) AND (x.to_year IS NULL OR x.to_year+1>=year(g.game_date)),false) AS named_identity
FROM src.main.fact_player_game_boxscore p JOIN player_scope_v17 USING(game_id)
JOIN prior.nba.fact_game g USING(game_id)
LEFT JOIN prior.nba.bridge_game_team k USING(game_id,team_id)
LEFT JOIN prior.nba.dim_person x ON x.person_id=p.player_id
LEFT JOIN src.main.stg_kaggle_player_box s ON (s.game_id,s.person_id)=(p.game_id,p.player_id)
LEFT JOIN src.main.fact_player_game_traditional t ON (t.game_id,t.player_id,t.team_id)=(p.game_id,p.player_id,p.team_id);

CREATE TEMP TABLE player_observation_v17 AS
WITH sides AS (
 SELECT game_id,team_id,sum(minute_seconds) AS side_seconds,sum(points) AS side_points,
        bool_and(valid_mm_ss) AS valid_side_minutes
 FROM player_input_v17 WHERE named_identity GROUP BY 1,2
), fixtures AS (
 SELECT p.game_id,count(*)=2 AND bool_and(p.side_seconds=14400 AND p.valid_side_minutes) AS minutes_reconcile
 FROM sides p GROUP BY p.game_id
)
SELECT p.game_id,p.player_id AS person_id,p.team_id,p.display_name,p.min AS raw_min_mm_ss,p.minute_seconds,
       p.is_win AS reported_is_win,p.team_id=p.winner_team_id AS verified_is_win,p.plus_minus AS reported_plus_minus,
       s.side_seconds,p.exact_siblings,p.traditional_reported_season,
       'src.main.fact_player_game_boxscore' AS source_relation,
       CASE WHEN NOT p.named_identity THEN 'held_unknown_person_placeholder'
            WHEN NOT p.exact_siblings OR p.side_final IS DISTINCT FROM s.side_points
                OR NOT coalesce(p.points>=0 AND p.points=2*p.fgm+p.ftm+CASE WHEN p.season_start<1979 THEN 0 ELSE p.fg3m END
                 AND p.fg3m<=p.fgm AND p.fg3a<=p.fga AND p.fgm BETWEEN 0 AND p.fga AND p.ftm BETWEEN 0 AND p.fta
                 AND (p.season_start<1979 OR p.fg3m BETWEEN 0 AND p.fg3a),false) THEN 'held_box_conflict'
            WHEN NOT coalesce(f.minutes_reconcile,false) THEN 'held_conflicting_minutes'
            ELSE 'published_traditional' END AS disposition
FROM player_input_v17 p LEFT JOIN sides s USING(game_id,team_id) LEFT JOIN fixtures f USING(game_id);

CREATE TEMP TABLE player_publication_v17 AS SELECT * FROM prior.nba.fact_player_game_stats LIMIT 0;
INSERT INTO player_publication_v17 BY NAME
SELECT p.game_id,p.player_id AS person_id,p.team_id,p.resolved_opponent AS opponent_team_id,p.side='home' AS is_home,
       p.team_id=p.winner_team_id AS is_win,'played' AS participation_status,p.starting_position,p.comment,
       p.minute_seconds/60.0 AS num_minutes,p.min AS reported_num_minutes,p.points,p.assists,
       CASE WHEN p.season_start>=1973 THEN p.blocks END AS blocks,
       CASE WHEN p.season_start>=1973 THEN p.steals END AS steals,
       CASE WHEN p.season_start>=1977 THEN p.turnovers END AS turnovers,p.fga,p.fgm,p.fg_pct,
       CASE WHEN p.season_start>=1979 THEN p.fg3a END AS fg3a,
       CASE WHEN p.season_start>=1979 THEN p.fg3m END AS fg3m,
       CASE WHEN p.season_start>=1979 THEN p.fg3_pct END AS fg3_pct,p.fta,p.ftm,p.ft_pct,
       CASE WHEN p.season_start>=1973 THEN p.oreb END AS oreb,
       CASE WHEN p.season_start>=1973 THEN p.dreb END AS dreb,p.reb,p.fouls_personal
FROM player_input_v17 p JOIN player_observation_v17 a ON (p.game_id,p.player_id)=(a.game_id,a.person_id)
WHERE a.disposition='published_traditional';

CREATE TEMP TABLE player_availability_v17 AS
SELECT s.game_id,count(o.person_id) AS candidate_rows,
       count(*) FILTER (WHERE o.disposition='published_traditional') AS published_rows,
       CASE WHEN count(o.person_id)=0 THEN 'source_unavailable'
            WHEN count(*) FILTER(WHERE o.disposition='published_traditional')=0 THEN 'held'
            ELSE 'traditional_box_recovered' END AS status
FROM player_scope_v17 s LEFT JOIN player_observation_v17 o USING(game_id) GROUP BY 1;
