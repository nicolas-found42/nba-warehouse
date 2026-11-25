"""Coverage ledger: the warehouse's definition of done.

audit.game_coverage has one row per game x domain with a status:
  present             the record is in core
  missing_recoverable not in core yet; fewer than two independent sources have failed
  source_unavailable  two or more independent sources tried and failed (gap given up)
  disputed            sources disagree and no higher-ranked source settles it (held in audit)
  not_tracked         the league did not record this then (e.g. play-by-play before 1996-97)
  not_applicable      does not apply to this game (e.g. no box score for an unplayed fixture)

audit.gap_attempt records each attempt to fill a gap (domain, game, source, outcome); the two-source
rule reads from it. audit.domain_coverage summarises game_coverage by domain x season x season type.

  uv run --no-project --with duckdb==2.0.0 python warehouse/coverage.py
"""
from __future__ import annotations

import argparse
from pathlib import Path

from build_warehouse import DEFAULT_OUT, connect

# First season (season_start) each domain was recorded by the league, for NBA/BAA games.
TRACKED_FROM = {
    'final_score': 1946, 'period_scores': 1946, 'team_box': 1946, 'player_box': 1946,
    'play_by_play': 1996, 'shot_chart': 1996, 'starting_lineup': 1996, 'officials': 1996,
}

SQL = r"""
CREATE TABLE IF NOT EXISTS audit.gap_attempt (domain VARCHAR, game_id VARCHAR, source VARCHAR, attempted_at TIMESTAMPTZ,
    outcome VARCHAR, note VARCHAR);
-- seed attempts from the historical period-recovery ledger: every 'no_supported_source' / 'no_valid_candidate'
-- game was already checked against the local snapshot mirror; v21-v26 receipts record the paced external checks.
CREATE OR REPLACE TEMP TABLE prior_attempt AS
SELECT 'period_scores' AS domain, game_id, 'local_snapshot_mirror' AS source FROM audit.period_availability_v26 WHERE status <> 'published';

CREATE OR REPLACE TEMP TABLE attempts AS
SELECT domain, game_id, count(DISTINCT source) AS sources_failed FROM (
    SELECT domain, game_id, source FROM prior_attempt
    UNION SELECT domain, game_id, source FROM audit.gap_attempt WHERE outcome IN ('not_found', 'no_data', 'mismatch')
) GROUP BY ALL;

CREATE OR REPLACE TEMP TABLE g AS
SELECT game_id, season_start, season_year, season_type, result_status, league_era FROM core.dim_game;

CREATE OR REPLACE TEMP TABLE have AS
SELECT 'final_score' AS domain, game_id FROM core.fact_game_result
UNION ALL SELECT 'period_scores', game_id FROM core.fact_game_period_score GROUP BY game_id
UNION ALL SELECT 'team_box', game_id FROM core.fact_team_game_stats GROUP BY game_id HAVING count(*) = 2
UNION ALL SELECT 'player_box', game_id FROM core.fact_player_game_stats GROUP BY game_id HAVING count(DISTINCT team_id) = 2
UNION ALL SELECT 'play_by_play', game_id FROM core.fact_play_event GROUP BY game_id
UNION ALL SELECT 'shot_chart', game_id FROM core.fact_shot GROUP BY game_id
UNION ALL SELECT 'starting_lineup', game_id FROM core.fact_starting_lineup GROUP BY game_id
UNION ALL SELECT 'officials', game_id FROM core.fact_game_official GROUP BY game_id;

CREATE OR REPLACE TEMP TABLE partial AS
SELECT 'team_box' AS domain, game_id FROM core.fact_team_game_stats GROUP BY game_id HAVING count(*) = 1
UNION ALL SELECT 'player_box', game_id FROM core.fact_player_game_stats GROUP BY game_id HAVING count(DISTINCT team_id) = 1;

CREATE OR REPLACE TEMP TABLE disputed AS
SELECT 'period_scores' AS domain, game_id FROM audit.period_availability_v26 WHERE status = 'no_valid_candidate';

CREATE OR REPLACE TABLE audit.game_coverage AS
SELECT g.game_id, g.season_year, g.season_type, d.domain,
  CASE
    WHEN h.game_id IS NOT NULL THEN 'present'
    WHEN g.result_status <> 'final' AND d.domain <> 'final_score' THEN 'not_applicable'
    WHEN g.season_start < t.first_season THEN 'not_tracked'
    WHEN p.game_id IS NOT NULL THEN 'partial'
    WHEN x.game_id IS NOT NULL AND coalesce(a.sources_failed, 0) < 2 THEN 'disputed'
    WHEN coalesce(a.sources_failed, 0) >= 2 THEN 'source_unavailable'
    ELSE 'missing_recoverable' END AS status,
  coalesce(a.sources_failed, 0) AS sources_failed
FROM g CROSS JOIN (SELECT * FROM (VALUES {tracked}) v(domain, first_season)) t
JOIN (SELECT DISTINCT domain FROM (VALUES {tracked}) v(domain, first_season)) d ON d.domain = t.domain
LEFT JOIN have h ON h.game_id = g.game_id AND h.domain = d.domain
LEFT JOIN partial p ON p.game_id = g.game_id AND p.domain = d.domain
LEFT JOIN disputed x ON x.game_id = g.game_id AND x.domain = d.domain
LEFT JOIN attempts a ON a.game_id = g.game_id AND a.domain = d.domain;

CREATE OR REPLACE TABLE audit.domain_coverage AS
SELECT domain, season_year, season_type, status, count(*) AS games
FROM audit.game_coverage GROUP BY ALL ORDER BY ALL;

CREATE OR REPLACE VIEW audit.open_gaps AS
SELECT * FROM audit.game_coverage WHERE status IN ('missing_recoverable', 'partial', 'disputed');
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--output', type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    c = connect(a.output)
    tracked = ', '.join(f"('{k}', {v})" for k, v in TRACKED_FROM.items())
    c.execute(SQL.replace('{tracked}', tracked))
    c.execute('CHECKPOINT')
    for row in c.execute('''SELECT domain, status, sum(games) FROM audit.domain_coverage GROUP BY ALL ORDER BY 1, 2''').fetchall():
        print(*row, sep='\t')


if __name__ == '__main__':
    main()
