"""Link every betting row to a core game (and its teams). Nothing is computed from the odds.

Sources (all from the raw layer, unchanged there):
  raw.fact_game_odds_snapshot   moneyline snapshots keyed by NBA game_id (2008-)
  raw.fact_game_betting_lines   one consensus line per game_id
  raw.fact_game_main_line       Pinnacle main lines keyed by game page URL + snapshot time
  raw.fact_game_market_odds     Pinnacle market odds keyed by "Team A vs Team B" + snapshot time

Matching for the time-keyed sources is redone here independently: the game must involve exactly
those two franchises and tip off 0-60 hours after the snapshot (odds are pregame). A unique candidate
is 'matched'; several are 'ambiguous'; none is 'unmatched'. The source pipeline's own crosswalk is
kept next to ours (source_game_id / source_match_method) and disagreements are flagged.

  uv run --no-project --with duckdb==2.0.0 python warehouse/betting.py [--output data/nba-warehouse.duckdb]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from build_warehouse import DEFAULT_OUT, connect

SQL = r"""
DROP SCHEMA IF EXISTS betting CASCADE; CREATE SCHEMA betting;

-- franchise names as they appear in odds feeds -> core team_id (current and historical names)
-- a name is matched only to the team that carried it in the snapshot's season (e.g. the 1949-50 Denver Nuggets
-- are a different franchise from today's); dim_team display names are a fallback with no date range
CREATE OR REPLACE TEMP TABLE team_name AS
SELECT DISTINCT lower(full_name) AS name, team_id, valid_from, coalesce(valid_to, 9999) AS valid_to, 1 AS pref
FROM core.dim_team_version WHERE full_name IS NOT NULL
UNION SELECT DISTINCT lower(display_name), team_id, 0, 9999, 2 FROM core.dim_team WHERE display_name IS NOT NULL
UNION SELECT 'los angeles clippers', 1610612746, 0, 9999, 1;   -- feed spelling of LA Clippers

CREATE OR REPLACE TEMP TABLE game_slot AS
SELECT g.game_id, g.season_year, g.season_type,
       coalesce(g.game_datetime_est, g.game_date::TIMESTAMP + INTERVAL 19 HOUR) AS tip_est,
       least(g.home_team_id, g.away_team_id) AS t_lo, greatest(g.home_team_id, g.away_team_id) AS t_hi,
       g.home_team_id, g.away_team_id
FROM core.dim_game g WHERE g.participant_status = 'resolved';

-- 1. game_id-keyed sources ---------------------------------------------------------------
CREATE TABLE betting.odds_snapshot AS
SELECT o.*, CASE WHEN g.game_id IS NULL THEN 'game_not_in_core' ELSE 'matched' END AS match_status,
       g.home_team_id, g.away_team_id
FROM raw.fact_game_odds_snapshot o LEFT JOIN core.dim_game g USING (game_id);

CREATE TABLE betting.game_line AS
SELECT l.*, CASE WHEN g.game_id IS NULL THEN 'game_not_in_core' ELSE 'matched' END AS match_status,
       g.home_team_id, g.away_team_id
FROM raw.fact_game_betting_lines l LEFT JOIN core.dim_game g USING (game_id);

-- 2. time-keyed sources: our own independent match ----------------------------------------
CREATE OR REPLACE TEMP TABLE keyed AS
SELECT 'main_line' AS source, row_number() OVER () AS source_row, game_link AS source_key, snapshot_ts,
       lower(team1_name) AS n1, lower(team2_name) AS n2
FROM raw.fact_game_main_line
UNION ALL
SELECT 'market_odds', row_number() OVER (), matchup, snapshot_ts,
       lower(split_part(matchup, ' vs ', 1)), lower(split_part(matchup, ' vs ', 2))
FROM raw.fact_game_market_odds;

CREATE OR REPLACE TEMP TABLE keyed_team AS
SELECT k.*,
  (SELECT arg_min(t.team_id, t.pref) FROM team_name t WHERE t.name = k.n1
     AND year(k.snapshot_ts - INTERVAL 6 MONTH) + 1 BETWEEN t.valid_from AND t.valid_to + 1) AS team1_id,
  (SELECT arg_min(t.team_id, t.pref) FROM team_name t WHERE t.name = k.n2
     AND year(k.snapshot_ts - INTERVAL 6 MONTH) + 1 BETWEEN t.valid_from AND t.valid_to + 1) AS team2_id
FROM keyed k;

CREATE OR REPLACE TEMP TABLE cand AS
SELECT k.source, k.source_row, s.game_id, s.tip_est
FROM keyed_team k JOIN game_slot s
  ON s.t_lo = least(k.team1_id, k.team2_id) AND s.t_hi = greatest(k.team1_id, k.team2_id)
 AND s.tip_est BETWEEN k.snapshot_ts - INTERVAL 6 HOUR AND k.snapshot_ts + INTERVAL 60 HOUR;

CREATE OR REPLACE TEMP TABLE our_match AS
SELECT k.source, k.source_row, k.team1_id, k.team2_id,
       CASE WHEN k.team1_id IS NULL OR k.team2_id IS NULL THEN 'team_name_unresolved'
            WHEN count(c.game_id) = 0 THEN 'unmatched'
            WHEN count(c.game_id) = 1 THEN 'matched' ELSE 'ambiguous' END AS match_status,
       CASE WHEN count(c.game_id) = 1 THEN any_value(c.game_id) END AS game_id,
       list(c.game_id ORDER BY c.tip_est) FILTER (WHERE c.game_id IS NOT NULL) AS candidate_game_ids
FROM keyed_team k LEFT JOIN cand c USING (source, source_row)
GROUP BY k.source, k.source_row, k.team1_id, k.team2_id;

CREATE TABLE betting.main_line AS
WITH m AS (SELECT *, row_number() OVER () AS source_row FROM raw.fact_game_main_line)
SELECT m.* EXCLUDE (source_row), o.match_status, o.game_id, o.candidate_game_ids,
       b.game_id AS source_game_id, b.match_method AS source_match_method,
       (b.game_id IS NOT NULL AND o.game_id IS NOT NULL AND b.game_id <> o.game_id) AS disagrees_with_source
FROM m JOIN our_match o ON o.source = 'main_line' AND o.source_row = m.source_row
LEFT JOIN (SELECT game_link, CASE WHEN count(DISTINCT game_id) = 1 THEN any_value(game_id) END AS game_id,
                  string_agg(DISTINCT match_method, ',') AS match_method
           FROM raw.bridge_game_main_line GROUP BY 1) b USING (game_link);

CREATE TABLE betting.market_odds AS
WITH m AS (SELECT *, row_number() OVER () AS source_row FROM raw.fact_game_market_odds)
SELECT m.* EXCLUDE (source_row), o.team1_id, o.team2_id, o.match_status, o.game_id, o.candidate_game_ids,
       b.game_id AS source_game_id, b.match_method AS source_match_method,
       (b.game_id IS NOT NULL AND o.game_id IS NOT NULL AND b.game_id <> o.game_id) AS disagrees_with_source
FROM m JOIN our_match o ON o.source = 'market_odds' AND o.source_row = m.source_row
LEFT JOIN (SELECT matchup, snapshot_ts, CASE WHEN count(DISTINCT game_id) = 1 THEN any_value(game_id) END AS game_id,
                  string_agg(DISTINCT match_method, ',') AS match_method
           FROM raw.bridge_game_market_odds GROUP BY 1, 2) b
  USING (matchup, snapshot_ts);

CREATE TABLE betting.match_summary AS
SELECT 'odds_snapshot' AS source, match_status, count(*) AS rows, count(DISTINCT game_id) AS games FROM betting.odds_snapshot GROUP BY ALL
UNION ALL SELECT 'game_line', match_status, count(*), count(DISTINCT game_id) FROM betting.game_line GROUP BY ALL
UNION ALL SELECT 'main_line', match_status, count(*), count(DISTINCT game_id) FROM betting.main_line GROUP BY ALL
UNION ALL SELECT 'market_odds', match_status, count(*), count(DISTINCT game_id) FROM betting.market_odds GROUP BY ALL
UNION ALL SELECT 'main_line disagrees_with_source', 'flag', count(*), count(DISTINCT game_id) FROM betting.main_line WHERE disagrees_with_source
UNION ALL SELECT 'market_odds disagrees_with_source', 'flag', count(*), count(DISTINCT game_id) FROM betting.market_odds WHERE disagrees_with_source;
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--output', type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    c = connect(a.output)
    c.execute(SQL)
    # row-count conservation: every raw betting row appears exactly once
    for raw, bet in [('fact_game_odds_snapshot', 'odds_snapshot'), ('fact_game_betting_lines', 'game_line'),
                     ('fact_game_main_line', 'main_line'), ('fact_game_market_odds', 'market_odds')]:
        r, b = (c.execute(f'SELECT count(*) FROM {s}').fetchone()[0] for s in (f'raw.{raw}', f'betting.{bet}'))
        if r != b:
            raise SystemExit(f'row count changed: raw.{raw}={r} betting.{bet}={b}')
    c.execute('CHECKPOINT')
    for row in c.execute('SELECT * FROM betting.match_summary ORDER BY 1, 2').fetchall():
        print(*row, sep='\t')


if __name__ == '__main__':
    main()
