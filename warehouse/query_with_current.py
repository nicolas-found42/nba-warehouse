"""Query the historical and current local checkpoints through one read-only NBA schema."""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

from nba_warehouse import ROOT, sql_string

SEASON_TABLES = (
    'dim_season','dim_game','bridge_game_team','fact_game','fact_game_result','fact_game_period_score',
    'fact_team_game_stats','fact_player_game_stats','fact_play_event','field_goal_attempt','game_overtime',
)


def connect(history: Path, current: Path) -> duckdb.DuckDBPyConnection:
    c = duckdb.connect(config={'threads':2})
    try:
        for alias,p in [('history',history),('current_season',current)]:
            c.execute(f'ATTACH {sql_string(str(p.resolve(strict=True)))} AS {alias} (READ_ONLY)')
        if c.execute('''SELECT h.game_id FROM history.nba.dim_game h JOIN current_season.nba.dim_game c USING(game_id)
                     UNION ALL SELECT h.season_year FROM history.nba.dim_season h
                       JOIN current_season.nba.dim_season c USING(season_year) LIMIT 1''').fetchone() is not None:
            raise ValueError('History and current-season inputs overlap; use disjoint season snapshots')
        scope = c.execute('''SELECT s.season_year,a.cutoff FROM current_season.nba.dim_season s
            FULL JOIN current_season.audit.scope a ON s.season_start+1=a.season_end''').fetchall()
        if len(scope)!=1 or any(field is None for field in scope[0]):
            raise ValueError('Current snapshot must have exactly one season and matching dated scope')
        c.execute('CREATE SCHEMA nba')
        for name in SEASON_TABLES:
            c.execute(f'''CREATE VIEW nba.{name} AS SELECT * FROM history.nba.{name}
                      UNION ALL BY NAME SELECT * FROM current_season.nba.{name}''')
        c.execute('''CREATE VIEW nba.dim_team AS SELECT * FROM history.nba.dim_team;
          CREATE VIEW nba.dim_person AS SELECT * FROM history.nba.dim_person
          UNION ALL SELECT c.* FROM current_season.nba.dim_person c ANTI JOIN history.nba.dim_person h USING(person_id);
          CREATE VIEW nba.current_shot_coordinates AS SELECT * FROM current_season.audit.shot_coordinates;
          CREATE VIEW nba.current_game_coverage AS SELECT * FROM current_season.audit.game_coverage;
          CREATE VIEW nba.current_snapshot_scope AS SELECT * FROM current_season.audit.scope;
          CREATE VIEW nba.fact_regular_standings AS
            SELECT *, 'historical_release' AS snapshot_kind, NULL::DATE AS as_of_date
            FROM history.nba.fact_regular_standings
            UNION ALL BY NAME
            SELECT s.*, 'current_snapshot' AS snapshot_kind, a.cutoff AS as_of_date
            FROM current_season.nba.fact_regular_standings s CROSS JOIN current_season.audit.scope a;''')
    except BaseException:
        c.close()
        raise
    return c


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--history',type=Path,default=ROOT/'data/nba-clean-v26.duckdb')
    p.add_argument('--current',type=Path,required=True)
    p.add_argument('query')
    a = p.parse_args()
    with connect(a.history,a.current) as c:
        c.sql(a.query).show(max_rows=40)


if __name__=='__main__':
    main()
