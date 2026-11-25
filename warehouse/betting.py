"""Link betting rows to core games and publish timestamped pregame features.

Sources (all from the raw layer, unchanged there):
  raw.fact_game_odds_snapshot   moneyline snapshots keyed by NBA game_id (2008-)
  raw.fact_game_betting_lines   one consensus line per game_id
  raw.fact_game_main_line       Pinnacle main lines keyed by game page URL + snapshot time
  raw.fact_game_market_odds     Pinnacle market odds keyed by "Team A vs Team B" + snapshot time

Matching for the time-keyed sources is redone here independently: the game must involve exactly
those two franchises and agree on preseason status. A unique candidate is 'matched'; several are
'ambiguous'; none is 'unmatched'. The source pipeline's own crosswalk is
kept next to ours (source_game_id / source_match_method) and disagreements are flagged.

  uv run --no-project --with duckdb==2.0.0 python warehouse/betting.py [--output data/nba-warehouse.duckdb]
"""
from __future__ import annotations

import argparse
from pathlib import Path

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock
from betting_kaggle import build_kaggle

SQL = r"""
CREATE SCHEMA IF NOT EXISTS betting;

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
       g.game_datetime_est AS tip_est,
       least(g.home_team_id, g.away_team_id) AS t_lo, greatest(g.home_team_id, g.away_team_id) AS t_hi,
       g.home_team_id, g.away_team_id
FROM core.dim_game g WHERE g.participant_status = 'resolved';

-- 1. game_id-keyed sources ---------------------------------------------------------------
CREATE OR REPLACE TABLE betting.odds_snapshot AS
SELECT o.*, CASE WHEN g.game_id IS NULL THEN 'game_not_in_core' ELSE 'matched' END AS match_status,
       g.home_team_id, g.away_team_id
FROM raw.fact_game_odds_snapshot o LEFT JOIN core.dim_game g USING (game_id);

CREATE OR REPLACE TABLE betting.game_line AS
SELECT l.*, CASE WHEN g.game_id IS NULL THEN 'game_not_in_core' ELSE 'matched' END AS match_status,
       g.home_team_id, g.away_team_id
FROM raw.fact_game_betting_lines l LEFT JOIN core.dim_game g USING (game_id);

-- 2. time-keyed sources: our own independent match ----------------------------------------
CREATE OR REPLACE TEMP TABLE keyed AS
SELECT DISTINCT 'main_line' AS source, game_link AS source_key, snapshot_ts,
       lower(trim(team1_name)) AS n1, lower(trim(team2_name)) AS n2, is_preseason
FROM raw.fact_game_main_line
UNION ALL
SELECT DISTINCT 'market_odds', matchup, snapshot_ts,
       lower(trim(split_part(matchup, ' vs ', 1))), lower(trim(split_part(matchup, ' vs ', 2))), is_preseason
FROM raw.fact_game_market_odds;

CREATE OR REPLACE TEMP TABLE keyed_team AS
SELECT k.*,
  (SELECT arg_min(t.team_id, t.pref) FROM team_name t WHERE t.name = k.n1
     AND year(k.snapshot_ts - INTERVAL 6 MONTH) + 1 BETWEEN t.valid_from AND t.valid_to + 1) AS team1_id,
  (SELECT arg_min(t.team_id, t.pref) FROM team_name t WHERE t.name = k.n2
     AND year(k.snapshot_ts - INTERVAL 6 MONTH) + 1 BETWEEN t.valid_from AND t.valid_to + 1) AS team2_id
FROM keyed k;

CREATE OR REPLACE TEMP TABLE cand AS
SELECT k.source, k.source_key, k.snapshot_ts, k.n1, k.n2, k.is_preseason, s.game_id, s.tip_est
FROM keyed_team k JOIN game_slot s
  ON s.t_lo = least(k.team1_id, k.team2_id) AND s.t_hi = greatest(k.team1_id, k.team2_id)
 AND (k.is_preseason IS NULL OR k.is_preseason = (s.season_type = 'Preseason'))
 AND s.tip_est BETWEEN k.snapshot_ts - INTERVAL 6 HOUR AND k.snapshot_ts + INTERVAL 60 HOUR;

CREATE OR REPLACE TEMP TABLE our_match AS
SELECT k.source, k.source_key, k.snapshot_ts, k.n1, k.n2, k.is_preseason, k.team1_id, k.team2_id,
       CASE WHEN k.team1_id IS NULL OR k.team2_id IS NULL THEN 'team_name_unresolved'
            WHEN count(c.game_id) = 0 THEN 'unmatched'
            WHEN count(c.game_id) = 1 THEN 'matched' ELSE 'ambiguous' END AS match_status,
       CASE WHEN count(c.game_id) = 1 THEN any_value(c.game_id) END AS game_id,
       list(c.game_id ORDER BY c.tip_est) FILTER (WHERE c.game_id IS NOT NULL) AS candidate_game_ids
FROM keyed_team k LEFT JOIN cand c ON c.source = k.source AND c.source_key IS NOT DISTINCT FROM k.source_key
    AND c.snapshot_ts IS NOT DISTINCT FROM k.snapshot_ts AND c.n1 IS NOT DISTINCT FROM k.n1
    AND c.n2 IS NOT DISTINCT FROM k.n2 AND c.is_preseason IS NOT DISTINCT FROM k.is_preseason
GROUP BY k.source, k.source_key, k.snapshot_ts, k.n1, k.n2, k.is_preseason, k.team1_id, k.team2_id;

CREATE OR REPLACE TABLE betting.main_line AS
SELECT m.*, o.team1_id AS matched_team1_id, o.team2_id AS matched_team2_id, o.match_status, o.game_id, o.candidate_game_ids,
       b.game_id AS source_game_id, b.match_method AS source_match_method,
       (b.game_id IS NOT NULL AND o.game_id IS NOT NULL AND b.game_id <> o.game_id) AS disagrees_with_source
FROM raw.fact_game_main_line m JOIN our_match o ON o.source = 'main_line' AND o.source_key IS NOT DISTINCT FROM m.game_link
     AND o.snapshot_ts IS NOT DISTINCT FROM m.snapshot_ts AND o.n1 IS NOT DISTINCT FROM lower(trim(m.team1_name))
     AND o.n2 IS NOT DISTINCT FROM lower(trim(m.team2_name)) AND o.is_preseason IS NOT DISTINCT FROM m.is_preseason
LEFT JOIN (SELECT game_link, CASE WHEN count(DISTINCT game_id) = 1 THEN any_value(game_id) END AS game_id,
                  string_agg(DISTINCT match_method, ',') AS match_method
           FROM raw.bridge_game_main_line GROUP BY 1) b USING (game_link);

CREATE OR REPLACE TABLE betting.market_odds AS
SELECT m.*, o.team1_id, o.team2_id, o.match_status, o.game_id, o.candidate_game_ids,
       b.game_id AS source_game_id, b.match_method AS source_match_method,
       (b.game_id IS NOT NULL AND o.game_id IS NOT NULL AND b.game_id <> o.game_id) AS disagrees_with_source
FROM raw.fact_game_market_odds m JOIN our_match o ON o.source = 'market_odds' AND o.source_key IS NOT DISTINCT FROM m.matchup
     AND o.snapshot_ts IS NOT DISTINCT FROM m.snapshot_ts AND o.is_preseason IS NOT DISTINCT FROM m.is_preseason
LEFT JOIN (SELECT matchup, snapshot_ts, CASE WHEN count(DISTINCT game_id) = 1 THEN any_value(game_id) END AS game_id,
                  string_agg(DISTINCT match_method, ',') AS match_method
           FROM raw.bridge_game_market_odds GROUP BY 1, 2) b
  ON b.matchup = m.matchup AND b.snapshot_ts = m.snapshot_ts;

CREATE OR REPLACE TABLE betting.match_summary AS
SELECT 'odds_snapshot' AS source, match_status, count(*) AS rows, count(DISTINCT game_id) AS games FROM betting.odds_snapshot GROUP BY ALL
UNION ALL SELECT 'game_line', match_status, count(*), count(DISTINCT game_id) FROM betting.game_line GROUP BY ALL
UNION ALL SELECT 'main_line', match_status, count(*), count(DISTINCT game_id) FROM betting.main_line GROUP BY ALL
UNION ALL SELECT 'market_odds', match_status, count(*), count(DISTINCT game_id) FROM betting.market_odds GROUP BY ALL
UNION ALL SELECT 'main_line disagrees_with_source', 'flag', count(*), count(DISTINCT game_id) FROM betting.main_line WHERE disagrees_with_source
UNION ALL SELECT 'market_odds disagrees_with_source', 'flag', count(*), count(DISTINCT game_id) FROM betting.market_odds WHERE disagrees_with_source;

CREATE OR REPLACE VIEW betting.pinnacle_match_review AS
SELECT DISTINCT 'main_line' AS source, game_link AS fixture_key, snapshot_ts, match_status,
       game_id, candidate_game_ids, source_game_id, source_match_method, disagrees_with_source
FROM betting.main_line WHERE match_status <> 'matched' OR disagrees_with_source
UNION ALL
SELECT DISTINCT 'market_odds', matchup, snapshot_ts, match_status, game_id, candidate_game_ids,
       source_game_id, source_match_method, disagrees_with_source
FROM betting.market_odds WHERE match_status <> 'matched' OR disagrees_with_source;
"""


def build(c) -> None:
    c.execute('BEGIN')
    try:
        c.execute(SQL)
        build_kaggle(c)
        c.execute(Path(__file__).with_name('betting_features.sql').read_text())
        for raw, bet in [('fact_game_odds_snapshot', 'odds_snapshot'), ('fact_game_betting_lines', 'game_line'),
                         ('fact_game_main_line', 'main_line'), ('fact_game_market_odds', 'market_odds')]:
            r, b = (c.execute(f'SELECT count(*) FROM {s}').fetchone()[0] for s in (f'raw.{raw}', f'betting.{bet}'))
            if r != b:
                raise ValueError(f'row count changed: raw.{raw}={r} betting.{bet}={b}')
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--output', type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    with warehouse_lock(), connect(a.output) as c:
        build(c)
        c.execute('CHECKPOINT')
        for row in c.execute('SELECT * FROM betting.match_summary ORDER BY 1, 2').fetchall():
            print(*row, sep='\t')


if __name__ == '__main__':
    main()
