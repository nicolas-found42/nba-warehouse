"""Record, game by game, which bulk sources were checked for each open gap and do not have the game.

Each check is a membership test against a source already on disk; a game is logged 'not_found' for a source only when
that source's coverage period includes the game and the game is absent from it. coverage.py then marks a gap
source_unavailable once two independent sources have failed. Rows go to audit.gap_attempt with note 'bulk check'
(replaced on every run). The ESPN public API is not checked here: it is used only with the owner's approval.

  sources checked
    nba_box_summary      raw.officials (NBA box-score summary officials, 1996-97 to 2022-23)
    kaggle_eoinamoore    Kaggle eoinamoore games.officials, playbyplay, playerstatistics, teamstatistics (1946 onward)
    bref_box             Basketball-Reference box page cached by crawl_bref.py partial_crews, with no officials on it
    llimllib             llimllib/nba_data player_game_status season files (NBA.com box scores, 1996-97 onward)
    nba_starters         raw.fact_starting_lineup_player (NBA.com starters)
    nbastats_net         raw.nbastats_team_game (nbastats.net team game statistics, through 2017-18)

  uv run --no-project --with duckdb==2.0.0 python warehouse/label_gaps.py
"""
from __future__ import annotations

from pathlib import Path

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock

EVIDENCE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence'
EOIN = 'raw_kaggle.eoinamoore_historical_nba_data_and_player_box_scores__'
NOTE = 'bulk check'


def eoin_ids(table: str, where: str = 'true') -> str:
    return f"SELECT DISTINCT lpad(CAST(gameId AS VARCHAR), 10, '0') FROM {EOIN}{table} WHERE {where}"


# (domains, source, SQL returning the game_ids the source HAS, SQL condition on g (core.dim_game) for the source's span)
CHECKS = [
    (['officials'], 'nba_box_summary', 'SELECT DISTINCT game_id FROM raw.officials', "g.game_date BETWEEN '1996-10-01' AND '2023-06-30'"),
    (['officials'], 'kaggle_eoinamoore', eoin_ids('games', "nullif(trim(officials), '') IS NOT NULL"), 'true'),
    (['play_by_play', 'shot_chart'], 'kaggle_eoinamoore', eoin_ids('playbyplay'), 'true'),
    (['player_box'], 'kaggle_eoinamoore', eoin_ids('playerstatistics'), 'true'),
    (['team_box'], 'kaggle_eoinamoore', eoin_ids('teamstatistics'), 'true'),
    (['starting_lineup'], 'nba_starters', 'SELECT DISTINCT game_id FROM raw.fact_starting_lineup_player', "g.game_date >= '1996-10-01'"),
    (['starting_lineup', 'player_box'], 'llimllib',
     f"SELECT DISTINCT lpad(CAST(game_id AS VARCHAR), 10, '0') FROM read_parquet('{EVIDENCE}/llimllib/player_game_status/*.parquet')",
     "g.game_date >= '1996-10-01'"),
    (['team_box'], 'nbastats_net', '''SELECT DISTINCT g.game_id FROM raw.nbastats_team_game t JOIN core.dim_game g ON g.game_date = t.game_date
        JOIN core.fact_game_result r ON r.game_id = g.game_id AND t.score IN (r.home_score, r.away_score) AND t.opp_score IN (r.home_score, r.away_score)''',
     "g.game_date <= '2018-06-30'"),
]


def label(c) -> None:
    c.execute('DELETE FROM audit.gap_attempt WHERE note = ?', [NOTE])
    c.execute('''CREATE OR REPLACE TEMP TABLE open_gap AS SELECT gc.domain, gc.game_id FROM audit.game_coverage gc
        WHERE gc.status IN ('missing_recoverable', 'partial', 'source_unavailable')''')
    for domains, source, has, span in CHECKS:
        q = ','.join(f"'{d}'" for d in domains)
        n = c.execute(f'''INSERT INTO audit.gap_attempt
            SELECT o.domain, o.game_id, ?, now(), 'not_found', ? FROM open_gap o JOIN core.dim_game g USING (game_id)
            WHERE o.domain IN ({q}) AND ({span}) AND o.game_id NOT IN ({has})''', [source, NOTE]).fetchone()[0]
        print(f'{source:18} {"/".join(domains):28} not found for {n} open gaps', flush=True)
    # Basketball-Reference box pages fetched for officials gaps that list no officials
    keys = c.execute('''SELECT o.game_id, strftime(g.game_date, '%Y%m%d') || '0' || t.source_key FROM open_gap o
        JOIN core.dim_game g USING (game_id)
        JOIN core.team_source_id t ON t.team_id = g.home_team_id AND t.source_system = 'basketball_reference' AND t.league = 'NBA'
             AND t.season_end_year = CAST(substr(g.season_year, 1, 4) AS INTEGER) + 1
        WHERE o.domain = 'officials' ''').fetchall()
    rows = [('officials', gid, 'bref_box', 'not_found', NOTE) for gid, k in keys
            if (EVIDENCE / 'bref-cache' / 'boxscores' / f'{k}.html').exists()]
    if rows:
        c.executemany('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)', rows)
    print(f'{"bref_box":18} {"officials":28} page has no officials for {len(rows)} open gaps')


def main() -> None:
    import coverage
    with warehouse_lock():
        c = connect(DEFAULT_OUT)
        label(c)
        tracked = ', '.join(f"('{k}', {v})" for k, v in coverage.TRACKED_FROM.items())
        c.execute(coverage.SQL.replace('{tracked}', tracked))
        c.execute('CHECKPOINT')
        c.close()


if __name__ == '__main__':
    main()
