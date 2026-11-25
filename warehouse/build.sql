INSERT INTO nba.dim_season
SELECT CAST(left(season_year, 4) AS SMALLINT), season_year,
       CASE WHEN left(season_year, 4) BETWEEN '1946' AND '1948' THEN 'BAA' ELSE 'NBA' END
FROM src.main.dim_season;

INSERT INTO nba.dim_team
SELECT team_id,
       coalesce(max(full_name) FILTER (WHERE is_current),
                CASE WHEN count(DISTINCT full_name)=1 THEN min(full_name) END),
       coalesce(max(abbreviation) FILTER (WHERE is_current),
                CASE WHEN count(DISTINCT abbreviation)=1 THEN min(abbreviation) END)
                FROM src.main.dim_team GROUP BY team_id;

-- Namespaced game-team identifiers for exhibition opponents and event squads.
-- Admit only exact same-key/date/orientation/score evidence. Do not merge this
-- provider's older club IDs based on their names or modern franchise IDs.
CREATE TABLE audit.team_identity_evidence AS
WITH corroborated AS (
    SELECT f.game_id, f.season_type, f.home_team_id, f.away_team_id,
           s.team_name_home, s.team_name_away, s.team_abbreviation_home, s.team_abbreviation_away
    FROM src.main.fact_game f JOIN src.main.game s USING (game_id)
    WHERE f.season_type IN ('Preseason','AllStar') AND f.game_date=s.game_date::DATE
      AND f.home_team_id=try_cast(s.team_id_home AS BIGINT) AND f.away_team_id=try_cast(s.team_id_away AS BIGINT)
      AND f.home_score=s.pts_home AND f.away_score=s.pts_away
), sides AS (
    SELECT game_id, season_type, home_team_id AS team_id, trim(team_name_home) AS display_name,
           trim(team_abbreviation_home) AS abbreviation FROM corroborated
    UNION ALL
    SELECT game_id, season_type, away_team_id, trim(team_name_away), trim(team_abbreviation_away) FROM corroborated
)
SELECT s.*, 'src.main.game' AS evidence_relation
FROM sides s ANTI JOIN nba.dim_team d USING (team_id)
WHERE s.team_id>0 AND s.display_name<>'';

INSERT INTO nba.dim_team
SELECT team_id,
       CASE WHEN count(DISTINCT display_name)=1 THEN min(display_name) END,
       CASE WHEN count(DISTINCT abbreviation)=1 THEN min(abbreviation) END
FROM audit.team_identity_evidence
GROUP BY team_id;

INSERT INTO nba.dim_team VALUES (15025, 'Ratiopharm Ulm', 'ULM');

CREATE TABLE audit.verified_score_repair (
    game_id VARCHAR PRIMARY KEY, game_date DATE NOT NULL,
    home_team_id BIGINT NOT NULL, away_team_id BIGINT NOT NULL,
    home_score INTEGER NOT NULL, away_score INTEGER NOT NULL,
    evidence_url VARCHAR NOT NULL
);
INSERT INTO audit.verified_score_repair VALUES
    ('0012400002', DATE '2024-10-04', 1610612762, 15020, 116, 87, 'https://www.nba.com/game/nzb-vs-uta-0012400002'),
    ('0012400011', DATE '2024-10-07', 1610612755, 15020, 139, 84, 'https://www.nba.com/game/nzb-vs-phi-0012400011'),
    ('0012400029', DATE '2024-10-10', 1610612760, 15020, 117, 89, 'https://www.nba.com/game/nzb-vs-okc-0012400029'),
    ('0012400060', DATE '2024-10-16', 1610612757, 15025, 111, 100, 'https://www.nba.com/game/ulm-vs-por-0012400060');

CREATE TABLE audit.fixture_collision_review (
    first_game_id VARCHAR PRIMARY KEY, second_game_id VARCHAR NOT NULL UNIQUE,
    disposition VARCHAR NOT NULL, evidence_url VARCHAR NOT NULL
);
INSERT INTO audit.fixture_collision_review VALUES
    ('0025300306', '0025300325', 'distinct_games_historical_doubleheader',
     'https://apnews.com/e04b7da2ef27449e8c761aec9d351afb');

CREATE TABLE audit.player_identity_review (
    game_id VARCHAR, team_id BIGINT NOT NULL, held_person_id BIGINT,
    retained_person_id BIGINT NOT NULL, evidence_url VARCHAR NOT NULL,
    PRIMARY KEY (game_id, held_person_id)
);
INSERT INTO audit.player_identity_review VALUES
    ('0010900001',1610612762,500032,202083,
     'https://www.nba.com/game/0010900001/box-score');

INSERT INTO nba.dim_team_version
SELECT team_id, valid_from, valid_to, full_name, abbreviation, city, state, arena,
       year_founded, conference, division, is_current, team_kind
FROM src.main.dim_team;

-- The audited snapshot's synthetic-ID band is not promoted as confirmed identities.
-- Do not resolve these rows on names alone. Historical small NBA IDs remain valid.
INSERT INTO nba.dim_person
SELECT a.person_id, trim(a.display_first_last), p.birth_date,
       try_cast(a.from_year AS SMALLINT), try_cast(a.to_year AS SMALLINT), a.playercode
FROM src.main.dim_all_players a LEFT JOIN src.main.dim_person p USING (person_id)
WHERE a.person_id NOT BETWEEN 1962000000 AND 1969999999
  AND a.person_id > 0 AND length(trim(a.display_first_last)) > 0;

-- Both games and audit dispositions use the same decision, once per input row.
CREATE TEMP TABLE game_choices AS
WITH inputs AS (
    SELECT g.* EXCLUDE (home_score,away_score,winner_team_id),
           g.home_score AS source_home_score, g.away_score AS source_away_score,
           coalesce(g.home_score,r.home_score) AS home_score,
           coalesce(g.away_score,r.away_score) AS away_score,
           coalesce(g.winner_team_id,CASE WHEN r.home_score>r.away_score THEN r.home_team_id
                                          WHEN r.away_score>r.home_score THEN r.away_team_id END) AS winner_team_id,
           (season_year = '2020-21' AND season_type = 'Preseason' AND
            g.game_date BETWEEN DATE '2020-07-22' AND DATE '2020-07-28') AS is_restart_scrimmage,
           (h.team_id IS NOT NULL AND a.team_id IS NOT NULL AND h.team_id <> a.team_id) AS has_pair,
           coalesce(g.game_datetime_est::DATE <> g.game_date, false) AS date_time_conflict
    FROM src.main.fact_game g
    LEFT JOIN nba.dim_team h ON g.home_team_id=h.team_id
    LEFT JOIN nba.dim_team a ON g.away_team_id=a.team_id
    LEFT JOIN audit.verified_score_repair r
      ON (g.game_id,g.game_date,g.home_team_id,g.away_team_id)=(r.game_id,r.game_date,r.home_team_id,r.away_team_id)
     AND g.home_score IS NULL AND g.away_score IS NULL AND g.season_type='Preseason'
)
SELECT *,
       CASE WHEN is_restart_scrimmage THEN '2019-20' ELSE season_year END AS effective_season,
       CASE WHEN is_restart_scrimmage THEN 'Exhibition' ELSE season_type END AS effective_phase,
       CASE WHEN has_pair THEN 'resolved' ELSE 'unresolved' END AS effective_participants,
       CASE WHEN home_score IS NULL OR away_score IS NULL THEN 'score_unavailable'
            WHEN NOT has_pair THEN 'participants_unresolved'
            WHEN home_score < 0 OR away_score < 0 OR home_score = away_score THEN 'score_conflict'
            WHEN winner_team_id IS DISTINCT FROM CASE WHEN home_score > away_score
                                                       THEN home_team_id ELSE away_team_id END THEN 'score_conflict'
            ELSE 'final' END AS effective_result
FROM inputs;

INSERT INTO audit.game_disposition
SELECT game_id, season_year, season_type, home_team_id, away_team_id, source_home_score, source_away_score,
       is_restart_scrimmage, date_time_conflict, effective_participants, effective_result
FROM game_choices;

INSERT INTO nba.dim_game
SELECT g.game_id, g.game_date, CASE WHEN NOT g.date_time_conflict THEN g.game_datetime_est END,
       s.season_start, g.effective_season, s.league_era, g.effective_phase,
       g.effective_phase = 'Regular',
       CASE WHEN g.effective_phase = 'Cup' OR g.game_type ILIKE '%cup%' OR g.game_label ILIKE '%cup%' THEN 'NBA Cup'
            WHEN g.effective_phase = 'PlayIn' THEN 'NBA Play-In'
            WHEN g.effective_phase = 'AllStar' THEN 'NBA All-Star'
            WHEN g.is_restart_scrimmage THEN 'NBA Restart Scrimmage' END,
       g.game_label, g.game_sub_label, g.series_game_number,
       CASE WHEN g.has_pair THEN g.home_team_id END, CASE WHEN g.has_pair THEN g.away_team_id END,
       g.effective_participants, g.effective_result, nullif(trim(g.arena_name),''),
       nullif(trim(g.arena_city),''), nullif(trim(g.arena_state),''), g.attendance
FROM game_choices g JOIN nba.dim_season s ON s.season_year = g.effective_season;

INSERT INTO nba.bridge_game_team
SELECT game_id, 'home', home_team_id FROM nba.dim_game WHERE participant_status = 'resolved'
UNION ALL
SELECT game_id, 'away', away_team_id FROM nba.dim_game WHERE participant_status = 'resolved';

INSERT INTO nba.fact_game_result
SELECT game_id, home_score, away_score, winner_team_id, is_overtime
FROM game_choices WHERE effective_result = 'final';
DROP TABLE game_choices;

CREATE VIEW nba.fact_game AS
SELECT g.*, r.home_score, r.away_score, r.winner_team_id, r.is_overtime
FROM nba.dim_game g LEFT JOIN nba.fact_game_result r USING (game_id);

CREATE VIEW nba.game_team_labels AS
SELECT b.game_id, b.side, b.team_id,
       CASE WHEN count(v.team_id) = 1 THEN max(v.full_name) END AS season_team_name,
       CASE WHEN count(v.team_id) = 1 THEN max(v.abbreviation) END AS season_abbreviation,
       CASE count(v.team_id) WHEN 0 THEN 'no_version' WHEN 1 THEN 'one_version' ELSE 'ambiguous' END AS label_status
FROM nba.bridge_game_team b JOIN nba.dim_game g USING (game_id)
LEFT JOIN nba.dim_team_version v ON b.team_id=v.team_id
    AND g.season_start >= v.valid_from AND (v.valid_to IS NULL OR g.season_start <= v.valid_to)
GROUP BY b.game_id, b.side, b.team_id;

CREATE TEMP TABLE team_choices AS
SELECT t.game_id, t.team_id, b.side, g.season_start,
       CASE b.side WHEN 'home' THEN r.home_score WHEN 'away' THEN r.away_score END AS final_pts,
       CASE b.side WHEN 'home' THEN r.away_score WHEN 'away' THEN r.home_score END AS opponent_pts,
       coalesce(t.fgm > t.fga, false) AS bad_fga,
       coalesce(t.fg3m > t.fg3a, false) AS bad_fg3a,
       coalesce(t.ftm > t.fta, false) AS bad_fta,
       CASE WHEN g.game_id IS NULL THEN 'missing_game'
            WHEN g.participant_status <> 'resolved' THEN 'game_participants_unresolved'
            WHEN b.team_id IS NULL THEN 'not_a_game_participant'
            WHEN t.pts < 0 OR t.min < 0 OR t.pts <> 2*t.fgm + CASE WHEN g.season_start < 1979 THEN 0 ELSE t.fg3m END + t.ftm THEN 'invalid_box_stats'
            WHEN t.pts IS NOT NULL AND final_pts IS NOT NULL AND t.pts <> final_pts THEN 'team_points_disagree_with_result'
       END AS hold_reason
FROM src.main.fact_team_game_stats t
LEFT JOIN nba.dim_game g USING (game_id)
LEFT JOIN nba.bridge_game_team b ON (t.game_id,t.team_id)=(b.game_id,b.team_id)
LEFT JOIN nba.fact_game_result r ON t.game_id=r.game_id;

INSERT INTO audit.team_box_disposition
SELECT game_id, team_id, CASE WHEN hold_reason IS NULL THEN 'published' ELSE 'held' END,
       hold_reason, bad_fga, bad_fg3a, bad_fta
FROM team_choices;

INSERT INTO nba.fact_team_game_stats
SELECT t.game_id, t.team_id, c.side='home', c.final_pts > c.opponent_pts, t.min, t.pts,
       t.fgm, CASE WHEN NOT c.bad_fga THEN t.fga END, CASE WHEN NOT c.bad_fga THEN t.fg_pct END,
       CASE WHEN c.season_start >= 1979 THEN t.fg3m END,
       CASE WHEN c.season_start >= 1979 AND NOT c.bad_fg3a THEN t.fg3a END,
       CASE WHEN c.season_start >= 1979 AND NOT c.bad_fg3a THEN t.fg3_pct END,
       t.ftm, CASE WHEN NOT c.bad_fta THEN t.fta END, CASE WHEN NOT c.bad_fta THEN t.ft_pct END,
       CASE WHEN c.season_start >= 1973 THEN t.oreb END, CASE WHEN c.season_start >= 1973 THEN t.dreb END,
       t.reb, t.ast, CASE WHEN c.season_start >= 1977 THEN t.tov END,
       CASE WHEN c.season_start >= 1973 THEN t.stl END, CASE WHEN c.season_start >= 1973 THEN t.blk END,
       CASE WHEN c.season_start >= 1973 THEN t.blka END, t.pf, t.pfd, c.final_pts - c.opponent_pts,
       t.off_rating, t.def_rating, t.net_rating, t.ast_pct, t.ast_to, t.ast_ratio, t.oreb_pct, t.dreb_pct,
       t.reb_pct, t.tm_tov_pct, t.efg_pct, t.ts_pct, t.pace, t.pace_per40, t.poss, t.pie, t.fta_rate,
       t.opp_efg_pct, t.opp_fta_rate, t.opp_tov_pct, t.opp_oreb_pct,
       t.pts_off_tov, t.pts_2nd_chance, t.pts_fb, t.pts_paint, t.opp_pts_off_tov,
       t.opp_pts_2nd_chance, t.opp_pts_fb, t.opp_pts_paint,
       t.pct_fga_2pt, t.pct_fga_3pt, t.pct_pts_2pt, t.pct_pts_2pt_mr,
       t.pct_pts_3pt, t.pct_pts_fb, t.pct_pts_ft, t.pct_pts_off_tov, t.pct_pts_paint,
       t.pct_ast_2pm, t.pct_uast_2pm, t.pct_ast_3pm, t.pct_uast_3pm, t.pct_ast_fgm, t.pct_uast_fgm,
       t.bench_points, t.biggest_lead, t.biggest_scoring_run, t.lead_changes, t.times_tied, t.timeouts_remaining
FROM src.main.fact_team_game_stats t JOIN team_choices c USING (game_id, team_id)
WHERE c.hold_reason IS NULL;
DROP TABLE team_choices;

CREATE TEMP TABLE player_choices AS
WITH inputs AS (
    SELECT p.game_id, p.person_id, p.team_id, p.opponent_team_id, p.num_minutes, p.is_home,
           greatest(p.points, p.fga, p.fta, p.assists, p.reb, p.blocks, p.steals, p.turnovers) > 0 AS counting_activity,
           coalesce(m.participation_status IN ('dnp','roster_only','not_with_team'), false) AS mapped_nonplay,
           upper(trim(split_part(coalesce(p.comment,''), '-', 1))) AS comment_code,
           m.participation_status AS mapped_status, b.side, g.season_start, r.winner_team_id,
           CASE b.side WHEN 'home' THEN g.away_team_id WHEN 'away' THEN g.home_team_id END AS resolved_opponent,
           CASE WHEN g.game_id IS NULL THEN 'missing_game'
                WHEN p.person_id BETWEEN 1962000000 AND 1969999999 THEN 'synthetic_id_band_needs_review'
                WHEN review.held_person_id IS NOT NULL THEN 'reviewed_duplicate_appearance'
                WHEN x.person_id IS NULL THEN 'missing_person'
                WHEN g.participant_status <> 'resolved' THEN 'game_participants_unresolved'
                WHEN b.team_id IS NULL THEN 'not_a_game_participant'
                WHEN p.points < 0 OR p.num_minutes < 0 OR p.fgm > p.fga OR p.fg3m > p.fg3a OR p.ftm > p.fta
                     OR p.points <> 2*p.fgm + CASE WHEN g.season_start < 1979 THEN 0 ELSE p.fg3m END + p.ftm THEN 'invalid_box_stats'
           END AS hold_reason
    FROM src.main.fact_player_game_stats p
    LEFT JOIN audit.player_identity_review review ON (p.game_id,p.team_id,p.person_id)=(review.game_id,review.team_id,review.held_person_id)
    LEFT JOIN nba.dim_game g ON p.game_id=g.game_id
    LEFT JOIN nba.dim_person x ON p.person_id=x.person_id
    LEFT JOIN nba.bridge_game_team b ON (p.game_id,p.team_id)=(b.game_id,b.team_id)
    LEFT JOIN nba.fact_game_result r ON p.game_id=r.game_id
    LEFT JOIN src.main.map_player_game_participation m ON (p.game_id,p.person_id)=(m.game_id,m.person_id)
)
SELECT *, coalesce(num_minutes=0 AND counting_activity, false) AS conflicting_zero_minutes,
       CASE WHEN (num_minutes > 0 OR counting_activity OR mapped_status = 'played')
                 AND (mapped_nonplay OR comment_code IN ('DNP','DND','NWT')) THEN 'unknown'
            WHEN num_minutes > 0 OR counting_activity OR mapped_status = 'played' THEN 'played'
            WHEN mapped_nonplay THEN mapped_status
            WHEN comment_code='NWT' THEN 'not_with_team'
            WHEN comment_code IN ('DNP','DND') THEN 'dnp'
            ELSE 'unknown' END AS participation
FROM inputs;

INSERT INTO audit.player_box_disposition
SELECT game_id, person_id, team_id, CASE WHEN hold_reason IS NULL THEN 'published' ELSE 'held' END,
       hold_reason, hold_reason IS NULL AND is_home IS DISTINCT FROM (side='home'),
       hold_reason IS NULL AND opponent_team_id IS DISTINCT FROM resolved_opponent,
       hold_reason IS NULL AND conflicting_zero_minutes
FROM player_choices;

INSERT INTO nba.fact_player_game_stats
SELECT p.game_id, p.person_id, p.team_id, c.resolved_opponent, c.side='home', p.team_id=c.winner_team_id,
       c.participation, p.starting_position, p.comment,
       CASE WHEN NOT c.conflicting_zero_minutes THEN p.num_minutes END, p.num_minutes,
       p.points, p.assists, CASE WHEN c.season_start >= 1973 THEN p.blocks END,
       CASE WHEN c.season_start >= 1973 THEN p.steals END, CASE WHEN c.season_start >= 1977 THEN p.turnovers END,
       p.fga, p.fgm, p.fg_pct, CASE WHEN c.season_start >= 1979 THEN p.fg3a END,
       CASE WHEN c.season_start >= 1979 THEN p.fg3m END, CASE WHEN c.season_start >= 1979 THEN p.fg3_pct END,
       p.fta, p.ftm, p.ft_pct, CASE WHEN c.season_start >= 1973 THEN p.oreb END,
       CASE WHEN c.season_start >= 1973 THEN p.dreb END, p.reb, p.fouls_personal,
       CASE WHEN c.season_start >= 1996 THEN p.plus_minus END,
       p.off_rating, p.def_rating, p.net_rating, p.ast_pct, p.ast_to_turnover_ratio, p.ast_ratio,
       p.oreb_pct, p.dreb_pct, p.reb_pct, p.tov_pct, p.efg_pct, p.ts_pct, p.usg_pct, p.pace, p.pie
FROM src.main.fact_player_game_stats p JOIN player_choices c USING (game_id, person_id)
WHERE c.hold_reason IS NULL;
DROP TABLE player_choices;

INSERT INTO audit.known_fixture VALUES
('0032400011', '2024-25', 'played_missing_from_snapshot', 'https://www.nba.com/news/nightly-pulse-nba-news-highlights-from-feb-16'),
('0032400021', '2024-25', 'played_missing_from_snapshot', 'https://www.nba.com/news/nightly-pulse-nba-news-highlights-from-feb-16'),
('0032400031', '2024-25', 'played_missing_from_snapshot', 'https://www.nba.com/news/nightly-pulse-nba-news-highlights-from-feb-16'),
('0042500406', '2025-26', 'scheduled_not_played', 'https://www.nba.com/news/2026-nba-playoffs-schedule'),
('0042500407', '2025-26', 'scheduled_not_played', 'https://www.nba.com/news/2026-nba-playoffs-schedule');

CREATE VIEW audit.season_coverage AS
WITH tb AS (SELECT game_id, count(*) AS n FROM nba.fact_team_game_stats GROUP BY 1),
     pb AS (SELECT game_id, count(*) AS n FROM nba.fact_player_game_stats GROUP BY 1)
SELECT g.season_year, g.season_type, count(*) AS games,
       count(*) FILTER (WHERE g.participant_status='resolved') AS games_with_team_pair,
       count(*) FILTER (WHERE g.result_status='final') AS games_with_final_result,
       count(*) FILTER (WHERE tb.n=2) AS games_with_two_team_box_rows,
       count(*) FILTER (WHERE pb.n>0) AS games_with_any_player_box,
       coalesce(sum(pb.n),0) AS player_box_rows
FROM nba.dim_game g LEFT JOIN tb USING (game_id) LEFT JOIN pb USING (game_id)
GROUP BY g.season_year, g.season_type;

CREATE VIEW audit.box_points_comparison AS
WITH players AS (
    SELECT game_id, team_id, count(*) FILTER (WHERE participation_status='played') AS played_rows,
           count(points) FILTER (WHERE participation_status='played') AS played_points_rows,
           count(*) FILTER (WHERE participation_status='unknown') AS unknown_participation_rows,
           sum(points) FILTER (WHERE participation_status='played') AS played_points
    FROM nba.fact_player_game_stats GROUP BY 1, 2
), held AS (
    SELECT game_id, source_team_id AS team_id, count(*) AS held_rows
    FROM audit.player_box_disposition WHERE disposition='held' GROUP BY 1, 2
)
SELECT t.game_id, t.team_id, t.pts AS team_points, p.played_points, p.played_rows, p.played_points_rows,
       coalesce(h.held_rows,0) AS held_rows, coalesce(p.unknown_participation_rows,0) AS unknown_participation_rows,
       CASE WHEN t.pts IS NULL OR coalesce(p.played_rows,0)=0 OR p.played_rows<>p.played_points_rows
                  OR h.held_rows>0 OR p.unknown_participation_rows>0 THEN 'insufficient_evidence'
            WHEN t.pts=p.played_points THEN 'matches' ELSE 'disagrees' END AS comparison
FROM nba.fact_team_game_stats t LEFT JOIN players p USING (game_id, team_id) LEFT JOIN held h USING (game_id, team_id);
