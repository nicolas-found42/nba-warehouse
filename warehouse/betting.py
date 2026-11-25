"""Link betting rows to core games and publish timestamped pregame features.

Sources (all from the raw layer, unchanged there):
  raw.fact_game_odds_snapshot   moneyline snapshots keyed by NBA game_id (2008-)
  raw.fact_game_betting_lines   one consensus line per game_id
  raw.fact_game_main_line       Pinnacle main lines keyed by game page URL + snapshot time
  raw.fact_game_market_odds     Pinnacle market odds keyed by "Team A vs Team B" + snapshot time

Matching for the time-keyed sources is redone here independently: the game must involve exactly
those two franchises and agree on preseason status. For multiple candidates, a unique away-home order
resolves the fixture under the ordering observed in the Pinnacle exports. Otherwise the row stays ambiguous.
A disagreement between same-side fixtures stays ambiguous when the only local evidence is the time window.
The source pipeline's crosswalk and full candidate list remain available for review.

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
UNION SELECT 'los angeles clippers', 1610612746, 0, 9999, 1
UNION SELECT 'guangzhou loong lions', 15018, 2025, 2025, 1
UNION SELECT 'usa stars', 1610616859, 2025, 2025, 1
UNION SELECT 'usa stripes', 1610616860, 2025, 2025, 1;

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
  CASE WHEN k.n1='world' AND k.n2 IN ('usa stars','usa stripes') AND k.snapshot_ts::DATE=DATE '2026-02-15' THEN 1610616861
       ELSE (SELECT arg_min(t.team_id, t.pref) FROM team_name t WHERE t.name = k.n1
             AND year(k.snapshot_ts - INTERVAL 6 MONTH) + 1 BETWEEN t.valid_from AND t.valid_to + 1) END AS team1_id,
  CASE WHEN k.n2='world' AND k.n1 IN ('usa stars','usa stripes') AND k.snapshot_ts::DATE=DATE '2026-02-15' THEN 1610616861
       ELSE (SELECT arg_min(t.team_id, t.pref) FROM team_name t WHERE t.name = k.n2
             AND year(k.snapshot_ts - INTERVAL 6 MONTH) + 1 BETWEEN t.valid_from AND t.valid_to + 1) END AS team2_id
FROM keyed k;

-- The dated league schedule and a unique same-key published detailed crosswalk corroborate these three early or postponed events.
CREATE OR REPLACE TEMP TABLE reviewed_pinnacle_fixture AS
SELECT * FROM (VALUES
    ('1615035859', DATE '2025-09-10', 'Houston Rockets vs Oklahoma City Thunder', '0022500001'),
    ('1615035860', DATE '2025-09-14', 'Golden State Warriors vs Los Angeles Lakers', '0022500002'),
    ('1621897123', DATE '2026-01-08', 'Miami Heat vs Chicago Bulls', '0022500529')
) AS verified(event_id, quote_day, matchup, game_id);

CREATE OR REPLACE TEMP TABLE reviewed_pinnacle_detail AS
WITH exact_event_day AS (
    SELECT v.game_id,v.matchup,b.snapshot_ts
    FROM reviewed_pinnacle_fixture v JOIN raw.bridge_game_market_odds b
      ON b.matchup=v.matchup AND b.snapshot_ts::DATE=v.quote_day
    GROUP BY ALL HAVING count(DISTINCT b.game_id)=1 AND bool_and(b.game_id=v.game_id)
), pinned_openers AS (
    SELECT v.game_id,v.matchup,b.snapshot_ts
    FROM reviewed_pinnacle_fixture v JOIN raw.bridge_game_market_odds b
      ON b.matchup=v.matchup AND b.snapshot_ts>=v.quote_day AND b.snapshot_ts<TIMESTAMP '2025-10-01'
    WHERE v.game_id IN ('0022500001','0022500002')
    GROUP BY ALL HAVING count(DISTINCT b.game_id)=1 AND bool_and(b.game_id=v.game_id
        AND b.match_method='pinned_exact_event_identity' AND b.match_confidence=1)
), early_payloads AS (
    SELECT matchup,snapshot_ts,list(struct_pack(market:=market,selection:=selection,odds:=odds)
           ORDER BY market,selection,odds) AS quotes
    FROM raw.fact_game_market_odds WHERE matchup='Golden State Warriors vs Los Angeles Lakers'
      AND is_preseason=false AND snapshot_ts>=TIMESTAMP '2025-09-14' AND snapshot_ts<TIMESTAMP '2025-10-01'
    GROUP BY 1,2 HAVING count(*)=50
), confirmed_repeats AS (
    SELECT DISTINCT p.game_id,x.matchup,x.snapshot_ts FROM early_payloads x
    JOIN early_payloads seed ON seed.quotes=x.quotes
    JOIN pinned_openers p ON p.matchup=seed.matchup AND p.snapshot_ts=seed.snapshot_ts
)
SELECT * FROM exact_event_day
UNION SELECT * FROM pinned_openers
UNION SELECT * FROM confirmed_repeats;

-- Each exact snapshot is corroborated by dated book lines for both possible fixtures and a distinct alternate event.
CREATE OR REPLACE TEMP TABLE reviewed_pinnacle_key AS
SELECT * FROM (VALUES
    ('1618981544',TIMESTAMP '2025-11-10 22:08:06','Cleveland Cavaliers vs Miami Heat','0022500201',1610612739,1610612748),
    ('1621316242',TIMESTAMP '2025-12-21 19:32:45','Chicago Bulls vs Atlanta Hawks','0022500396',1610612741,1610612737),
    ('1622988890',TIMESTAMP '2026-01-31 23:20:13','Minnesota Timberwolves vs Memphis Grizzlies','0022500700',1610612750,1610612763),
    ('1626305622',TIMESTAMP '2026-03-17 20:33:35','Detroit Pistons vs Washington Wizards','0022500993',1610612765,1610612764),
    ('1626840278',TIMESTAMP '2026-03-25 19:44:31','Miami Heat vs Cleveland Cavaliers','0022501055',1610612748,1610612739),
    ('1619136212',TIMESTAMP '2025-11-12 21:45:14','Golden State Warriors vs San Antonio Spurs','0022500220',1610612744,1610612759),
    ('1621763755',TIMESTAMP '2026-01-03 22:07:11','Atlanta Hawks vs Toronto Raptors','0022500488',1610612737,1610612761),
    ('1624093946',TIMESTAMP '2026-02-19 21:06:50','Indiana Pacers vs Washington Wizards','0022500795',1610612754,1610612764),
    ('1624393217',TIMESTAMP '2026-02-24 17:32:06','Washington Wizards vs Atlanta Hawks','0022500832',1610612764,1610612737),
    ('1625968540',TIMESTAMP '2026-03-18 19:06:20','Los Angeles Clippers vs New Orleans Pelicans','0022501005',1610612746,1610612740),
    ('1629014729',TIMESTAMP '2026-04-24 18:31:35','San Antonio Spurs vs Portland Trail Blazers','0042500153',1610612759,1610612757),
    ('1629019804',TIMESTAMP '2026-04-24 18:31:35','Los Angeles Lakers vs Houston Rockets','0042500173',1610612747,1610612745),
    -- Detailed-only keys repeat the complete payload of the reviewed November 12 and February 24 anchors above.
    (NULL,TIMESTAMP '2025-11-12 21:57:33','Golden State Warriors vs San Antonio Spurs','0022500220',1610612744,1610612759),
    (NULL,TIMESTAMP '2025-11-12 22:03:49','Golden State Warriors vs San Antonio Spurs','0022500220',1610612744,1610612759),
    (NULL,TIMESTAMP '2026-02-24 18:17:56','Washington Wizards vs Atlanta Hawks','0022500832',1610612764,1610612737)
) AS reviewed(event_id,snapshot_ts,matchup,game_id,away_id,home_id);

CREATE OR REPLACE TEMP TABLE cand AS
SELECT k.source, k.source_key, k.snapshot_ts, k.n1, k.n2, k.is_preseason, s.game_id, s.tip_est,
       k.team1_id = s.away_team_id AND k.team2_id = s.home_team_id AS away_home_order_agrees,
       s.game_id = coalesce(r.game_id, m.game_id, d.game_id) AS reviewed_fixture_agrees
FROM keyed_team k LEFT JOIN reviewed_pinnacle_fixture m
  ON k.source='main_line' AND regexp_extract(rtrim(k.source_key,'/'), '/([0-9]+)$', 1)=m.event_id
 AND k.snapshot_ts::DATE=m.quote_day
LEFT JOIN reviewed_pinnacle_detail d ON k.source='market_odds' AND d.matchup=k.source_key AND d.snapshot_ts=k.snapshot_ts
LEFT JOIN reviewed_pinnacle_key r ON k.snapshot_ts=r.snapshot_ts AND k.is_preseason=false
 AND k.team1_id=r.away_id AND k.team2_id=r.home_id
 AND ((k.source='main_line' AND regexp_extract(rtrim(k.source_key,'/'), '/([0-9]+)$', 1)=r.event_id)
      OR (k.source='market_odds' AND k.source_key=r.matchup))
JOIN game_slot s
  ON s.t_lo = least(k.team1_id, k.team2_id) AND s.t_hi = greatest(k.team1_id, k.team2_id)
 AND (k.is_preseason IS NULL OR k.is_preseason = (s.season_type = 'Preseason'))
 AND (s.game_id = coalesce(r.game_id, m.game_id, d.game_id)
      OR s.tip_est BETWEEN k.snapshot_ts - INTERVAL 6 HOUR AND k.snapshot_ts + INTERVAL 60 HOUR);

CREATE OR REPLACE TEMP TABLE our_match AS
SELECT k.source, k.source_key, k.snapshot_ts, k.n1, k.n2, k.is_preseason, k.team1_id, k.team2_id,
       CASE WHEN k.team1_id IS NULL OR k.team2_id IS NULL THEN 'team_name_unresolved'
            WHEN count(c.game_id) = 0 THEN 'unmatched'
            WHEN count_if(c.reviewed_fixture_agrees) = 1 OR count(c.game_id) = 1 OR count_if(c.away_home_order_agrees) = 1
                THEN 'matched' ELSE 'ambiguous' END AS match_status,
       CASE WHEN count_if(c.reviewed_fixture_agrees) = 1 THEN max(c.game_id) FILTER (WHERE c.reviewed_fixture_agrees)
            WHEN count(c.game_id) = 1 THEN any_value(c.game_id)
            WHEN count_if(c.away_home_order_agrees) = 1 THEN max(c.game_id) FILTER (WHERE c.away_home_order_agrees) END AS game_id,
       list(c.game_id ORDER BY c.tip_est, c.game_id) FILTER (WHERE c.game_id IS NOT NULL) AS candidate_game_ids,
       CASE WHEN count_if(c.reviewed_fixture_agrees) = 1 THEN 'reviewed_event_fixture'
            WHEN count(c.game_id) = 1 THEN 'sole_time_window_candidate'
            WHEN count_if(c.away_home_order_agrees) = 1 THEN 'sole_away_home_candidate' END AS fixture_link_method
FROM keyed_team k LEFT JOIN cand c ON c.source = k.source AND c.source_key IS NOT DISTINCT FROM k.source_key
    AND c.snapshot_ts IS NOT DISTINCT FROM k.snapshot_ts AND c.n1 IS NOT DISTINCT FROM k.n1
    AND c.n2 IS NOT DISTINCT FROM k.n2 AND c.is_preseason IS NOT DISTINCT FROM k.is_preseason
GROUP BY k.source, k.source_key, k.snapshot_ts, k.n1, k.n2, k.is_preseason, k.team1_id, k.team2_id;

-- A sole local time-window candidate cannot settle a same-side disagreement while the source clock is unknown.
CREATE OR REPLACE TEMP MACRO source_fixture_conflicts(method, selected_game, source_game, team1, team2, source_away, source_home) AS (
    method='sole_time_window_candidate' AND selected_game<>source_game AND team1=source_away AND team2=source_home
);

CREATE OR REPLACE TABLE betting.main_line AS
SELECT m.*, o.team1_id AS matched_team1_id, o.team2_id AS matched_team2_id,
       CASE WHEN conflict.hold THEN 'ambiguous' ELSE o.match_status END AS match_status,
       CASE WHEN conflict.hold THEN NULL ELSE o.game_id END AS game_id,
       CASE WHEN conflict.hold THEN list_append(o.candidate_game_ids,b.game_id) ELSE o.candidate_game_ids END AS candidate_game_ids,
       b.game_id AS source_game_id, b.match_method AS source_match_method,
       (b.game_id IS NOT NULL AND o.game_id IS NOT NULL AND b.game_id <> o.game_id) AS disagrees_with_source,
       CASE WHEN conflict.hold THEN 'unresolved_same_side_conflict' ELSE o.fixture_link_method END AS fixture_link_method
FROM raw.fact_game_main_line m JOIN our_match o ON o.source = 'main_line' AND o.source_key IS NOT DISTINCT FROM m.game_link
     AND o.snapshot_ts IS NOT DISTINCT FROM m.snapshot_ts AND o.n1 IS NOT DISTINCT FROM lower(trim(m.team1_name))
     AND o.n2 IS NOT DISTINCT FROM lower(trim(m.team2_name)) AND o.is_preseason IS NOT DISTINCT FROM m.is_preseason
LEFT JOIN (SELECT game_link, CASE WHEN count(DISTINCT game_id) = 1 THEN any_value(game_id) END AS game_id,
                  string_agg(DISTINCT match_method, ',') AS match_method
           FROM raw.bridge_game_main_line GROUP BY 1) b USING (game_link)
LEFT JOIN game_slot original ON original.game_id=b.game_id,
LATERAL (SELECT source_fixture_conflicts(o.fixture_link_method,o.game_id,b.game_id,o.team1_id,o.team2_id,
                                         original.away_team_id,original.home_team_id) AS hold) conflict;

CREATE OR REPLACE TABLE betting.market_odds AS
SELECT m.*, o.team1_id, o.team2_id,
       CASE WHEN conflict.hold THEN 'ambiguous' ELSE o.match_status END AS match_status,
       CASE WHEN conflict.hold THEN NULL ELSE o.game_id END AS game_id,
       CASE WHEN conflict.hold THEN list_append(o.candidate_game_ids,b.game_id) ELSE o.candidate_game_ids END AS candidate_game_ids,
       b.game_id AS source_game_id, b.match_method AS source_match_method,
       (b.game_id IS NOT NULL AND o.game_id IS NOT NULL AND b.game_id <> o.game_id) AS disagrees_with_source,
       CASE WHEN conflict.hold THEN 'unresolved_same_side_conflict' ELSE o.fixture_link_method END AS fixture_link_method
FROM raw.fact_game_market_odds m JOIN our_match o ON o.source = 'market_odds' AND o.source_key IS NOT DISTINCT FROM m.matchup
     AND o.snapshot_ts IS NOT DISTINCT FROM m.snapshot_ts AND o.is_preseason IS NOT DISTINCT FROM m.is_preseason
LEFT JOIN (SELECT matchup, snapshot_ts, CASE WHEN count(DISTINCT game_id) = 1 THEN any_value(game_id) END AS game_id,
                  string_agg(DISTINCT match_method, ',') AS match_method
           FROM raw.bridge_game_market_odds GROUP BY 1, 2) b
  ON b.matchup = m.matchup AND b.snapshot_ts = m.snapshot_ts
LEFT JOIN game_slot original ON original.game_id=b.game_id,
LATERAL (SELECT source_fixture_conflicts(o.fixture_link_method,o.game_id,b.game_id,o.team1_id,o.team2_id,
                                         original.away_team_id,original.home_team_id) AS hold) conflict;

CREATE OR REPLACE TABLE betting.match_summary AS
SELECT 'odds_snapshot' AS source, match_status, count(*) AS rows, count(DISTINCT game_id) AS games FROM betting.odds_snapshot GROUP BY ALL
UNION ALL SELECT 'game_line', match_status, count(*), count(DISTINCT game_id) FROM betting.game_line GROUP BY ALL
UNION ALL SELECT 'main_line', match_status, count(*), count(DISTINCT game_id) FROM betting.main_line GROUP BY ALL
UNION ALL SELECT 'market_odds', match_status, count(*), count(DISTINCT game_id) FROM betting.market_odds GROUP BY ALL
UNION ALL SELECT 'main_line disagrees_with_source', 'flag', count(*), count(DISTINCT game_id) FROM betting.main_line WHERE disagrees_with_source
UNION ALL SELECT 'market_odds disagrees_with_source', 'flag', count(*), count(DISTINCT game_id) FROM betting.market_odds WHERE disagrees_with_source;

CREATE OR REPLACE VIEW betting.pinnacle_match_review AS
WITH review AS (
SELECT DISTINCT 'main_line' AS source, game_link AS fixture_key, snapshot_ts, match_status,
       game_id, candidate_game_ids, source_game_id, source_match_method, disagrees_with_source, fixture_link_method,
       matched_team1_id AS team1_id, matched_team2_id AS team2_id
FROM betting.main_line WHERE match_status <> 'matched' OR disagrees_with_source OR fixture_link_method <> 'sole_time_window_candidate'
UNION ALL
SELECT DISTINCT 'market_odds', matchup, snapshot_ts, match_status, game_id, candidate_game_ids,
       source_game_id, source_match_method, disagrees_with_source, fixture_link_method, team1_id, team2_id
FROM betting.market_odds WHERE match_status <> 'matched' OR disagrees_with_source OR fixture_link_method <> 'sole_time_window_candidate')
SELECT review.* EXCLUDE (team1_id, team2_id),
       CASE WHEN g.game_id IS NULL OR team1_id IS NULL OR team2_id IS NULL THEN 'unavailable'
            WHEN team1_id = g.away_team_id AND team2_id = g.home_team_id THEN 'agrees'
            WHEN team2_id = g.away_team_id AND team1_id = g.home_team_id THEN 'reversed'
            ELSE 'different_teams' END AS source_side_status
FROM review LEFT JOIN core.dim_game g ON g.game_id = review.source_game_id;
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
