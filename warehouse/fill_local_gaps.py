"""Fill coverage gaps from data already in the warehouse before fetching anything.

  shots   core.fact_shot rows from Kaggle eoinamoore play-by-play (NBA.com live-data actions with xLegacy/yLegacy
          coordinates), only for games with no shot rows at all. Keys are SHA-256 of the source row identity.

Referees and starting lineups from the same Kaggle set are promoted in promote_domains.py (fact_game_official,
fact_starting_lineup) so they keep the conservation accounting.

  uv run --no-project --with duckdb==2.0.0 python warehouse/fill_local_gaps.py
"""
from __future__ import annotations

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock

PBP = 'raw_kaggle.eoinamoore_historical_nba_data_and_player_box_scores__playbyplay'


def shots(c) -> None:
    c.execute(f'''CREATE OR REPLACE TEMP TABLE new_shot AS
        WITH p AS (
            SELECT lpad(gameId, 10, '0') AS game_id, try_cast(teamId AS BIGINT) AS team_id, try_cast(personId AS BIGINT) AS person_id,
                   try_cast(period AS SMALLINT) AS period,
                   try_cast(regexp_extract(clock, 'PT(\\d+)M', 1) AS INTEGER) * 60
                     + floor(try_cast(regexp_extract(clock, 'M([0-9.]+)S', 1) AS DOUBLE))::INTEGER AS seconds_remaining,
                   coalesce(try_cast(shotValue AS SMALLINT), CASE actionType WHEN '3pt' THEN 3 WHEN '2pt' THEN 2 END)::SMALLINT AS shot_value, shotResult = 'Made' AS made,
                   nullif(trim(coalesce(subType, '') || ' ' || coalesce(actionType, '')), '') AS source_action,
                   area AS source_zone_basic, areaDetail AS source_zone_area, NULL AS source_zone_range,
                   try_cast(try_cast(shotDistance AS DOUBLE) AS SMALLINT) AS distance_feet,
                   try_cast(try_cast(xLegacy AS DOUBLE) AS SMALLINT) AS x, try_cast(try_cast(yLegacy AS DOUBLE) AS SMALLINT) AS y,
                   actionNumber
            FROM {PBP} WHERE isFieldGoal = '1' AND shotResult IN ('Made', 'Missed'))
        , p2 AS (SELECT p.* REPLACE (coalesce(p.team_id, (SELECT min(b.team_id) FROM core.fact_player_game_stats b   -- team from the box score
                                                         WHERE b.game_id = p.game_id AND b.person_id = p.person_id)) AS team_id) FROM p)
        SELECT unhex(sha256(to_json({{'source': 'kaggle_eoinamoore_pbp', 'game_id': p.game_id, 'action_number': p.actionNumber}}))) AS shot_key,
               p.game_id, p.team_id, p.person_id, p.period, p.seconds_remaining, p.shot_value, p.made, p.source_action,
               p.source_zone_basic, p.source_zone_area, p.source_zone_range, p.distance_feet,
               CASE WHEN p.x IS NOT NULL AND p.y IS NOT NULL AND NOT (p.x = 0 AND p.y = 0) THEN p.x END,
               CASE WHEN p.x IS NOT NULL AND p.y IS NOT NULL AND NOT (p.x = 0 AND p.y = 0) THEN p.y END,
               CASE WHEN p.x IS NULL OR p.y IS NULL THEN 'unavailable' WHEN p.x = 0 AND p.y = 0 THEN 'ambiguous_zero' ELSE 'reported' END
        FROM p2 AS p
        WHERE p.game_id NOT IN (SELECT DISTINCT game_id FROM core.fact_shot)
          AND p.game_id IN (SELECT game_id FROM core.dim_game)
          AND p.period > 0 AND p.seconds_remaining >= 0 AND p.shot_value IN (2, 3) AND p.distance_feet BETWEEN 0 AND 110
          AND (p.game_id, p.team_id) IN (SELECT game_id, team_id FROM core.bridge_game_team)
          AND (p.game_id, p.person_id) IN (SELECT game_id, person_id FROM core.fact_player_game_stats)''')
    held = c.execute(f'''SELECT count(*) FROM {PBP} WHERE isFieldGoal = '1' AND lpad(gameId, 10, '0') NOT IN (SELECT DISTINCT game_id FROM core.fact_shot)
        AND lpad(gameId, 10, '0') IN (SELECT game_id FROM core.dim_game)''').fetchone()[0] - c.execute('SELECT count(*) FROM new_shot').fetchone()[0]
    c.execute('INSERT INTO core.fact_shot SELECT * FROM new_shot')
    print('shots added', c.execute('SELECT count(*), count(DISTINCT game_id) FROM new_shot').fetchone(), 'held (failed a key/check)', held)


if __name__ == '__main__':
    with warehouse_lock():
        con = connect(DEFAULT_OUT)
        shots(con)
        con.execute('CHECKPOINT')
        con.close()
