"""Promote non-game domains from the raw layer into core (step 4).

Each domain is published by one rule: a row enters core only when every key it carries resolves
to a core dimension (person, team, game, season) and its natural key is unique. Every source row
gets exactly one disposition in audit.domain_disposition:
  published | duplicate (exact copy of a published row) | held (reason recorded)
and audit.domain_held keeps the held rows themselves as JSON, so nothing is dropped silently.
Conservation is asserted: source rows = published + duplicate + held, per domain.

People: core.dim_person is extended with every NBA person id the raw layer knows (raw.dim_player,
raw.common_player_info, raw.dim_all_players) and core.person_source_id records the crosswalk to
other providers (from raw.map_source_identity, matched/manually_resolved rows only).

  uv run --no-project --with duckdb==2.0.0 python warehouse/promote_domains.py
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

from build_warehouse import DEFAULT_OUT, connect, q


@dataclass
class Domain:
    name: str                 # core table name
    source: str               # raw relation(s) the rows come from (documentation)
    select: str               # SQL producing candidate rows (all columns that will be published)
    key: list[str]            # natural key, must be unique in core
    fks: dict[str, str] = field(default_factory=dict)  # column -> 'core.table(col)'
    not_null: list[str] = field(default_factory=list)


def person_dimension(c) -> None:
    """Extend core.dim_person with every NBA person id known in raw, and build the source crosswalk."""
    c.execute('''
    CREATE OR REPLACE TEMP TABLE person_candidate AS
    WITH dp AS (
        SELECT player_id AS person_id, arg_max(full_name, coalesce(valid_to, '9999')) AS display_name,
               arg_max(try_cast(birth_date AS DATE), coalesce(valid_to, '9999')) AS birth_date,
               min(from_year) AS from_year, max(to_year) AS to_year
        FROM raw.dim_player WHERE player_id > 0 GROUP BY 1),
    cpi AS (
        SELECT try_cast(person_id AS BIGINT) AS person_id, any_value(display_first_last) AS display_name,
               any_value(try_cast(birthdate AS DATE)) AS birth_date,
               min(try_cast(from_year AS SMALLINT)) AS from_year, max(try_cast(to_year AS SMALLINT)) AS to_year,
               any_value(playercode) AS player_code
        FROM raw.common_player_info WHERE try_cast(person_id AS BIGINT) > 0 GROUP BY 1)
    , other AS (  -- NBA-issued ids for people who never played: draftees, combine attendees, inactive-list names
        SELECT person_id, arg_max(name, src) AS display_name FROM (
            SELECT person_id, player_name AS name, 1 AS src FROM raw.fact_draft WHERE person_id > 0
            UNION ALL SELECT normalized_player_id, player_name, 2 FROM raw.fact_draft_combine WHERE normalized_player_id > 0
            UNION ALL SELECT person_id, trim(first_name || ' ' || last_name), 3 FROM raw.fact_inactive_player WHERE person_id > 0)
        WHERE nullif(trim(name), '') IS NOT NULL GROUP BY 1)
    SELECT person_id, display_name, birth_date, from_year, to_year, player_code FROM (
    SELECT coalesce(dp.person_id, cpi.person_id) AS person_id,
           coalesce(nullif(trim(dp.display_name), ''), nullif(trim(cpi.display_name), '')) AS display_name,
           coalesce(dp.birth_date, cpi.birth_date) AS birth_date,
           coalesce(dp.from_year, cpi.from_year)::SMALLINT AS from_year, coalesce(dp.to_year, cpi.to_year)::SMALLINT AS to_year,
           cpi.player_code
    FROM dp FULL JOIN cpi USING (person_id)
    UNION ALL
    SELECT person_id, display_name, NULL, NULL, NULL, NULL FROM other
    WHERE person_id NOT IN (SELECT person_id FROM dp WHERE person_id IS NOT NULL)
      AND person_id NOT IN (SELECT person_id FROM cpi WHERE person_id IS NOT NULL))''')
    c.execute('''INSERT INTO core.dim_person
        SELECT * FROM person_candidate p WHERE display_name IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM core.dim_person d WHERE d.person_id = p.person_id)''')
    c.execute('''CREATE TABLE IF NOT EXISTS core.person_source_id (
        person_id BIGINT NOT NULL REFERENCES core.dim_person(person_id), source_system VARCHAR NOT NULL,
        source_key VARCHAR NOT NULL, source_key_kind VARCHAR, match_method VARCHAR, match_confidence DOUBLE,
        resolution_status VARCHAR, season_from INTEGER, season_to INTEGER,
        PRIMARY KEY (source_system, source_key, person_id))''')
    c.execute('''INSERT INTO core.person_source_id
        SELECT DISTINCT ON (try_cast(canonical_id AS BIGINT), source_system, source_key)
               try_cast(canonical_id AS BIGINT), source_system, source_key, source_key_kind, match_method,
               match_confidence, resolution_status, season_from, season_to
        FROM raw.map_source_identity m
        WHERE entity_type = 'player' AND resolution_status IN ('matched', 'manually_resolved')
          AND try_cast(canonical_id AS BIGINT) IN (SELECT person_id FROM core.dim_person)
          AND NOT EXISTS (SELECT 1 FROM core.person_source_id p WHERE p.source_system = m.source_system
                          AND p.source_key = m.source_key AND p.person_id = try_cast(m.canonical_id AS BIGINT))
        ORDER BY 1, 2, 3, match_confidence DESC NULLS LAST''')


def bref_person(alias: str = 'x') -> str:
    """SQL expression resolving a Basketball-Reference row to an NBA person id: its own id, else the crosswalk."""
    return (f"coalesce({alias}.person_id, (SELECT min(s.person_id) FROM core.person_source_id s "
            f"WHERE s.source_system = 'basketball_reference' AND s.source_key = {alias}.bref_player_id))")


DOMAINS: list[Domain] = [
    Domain('fact_draft_pick', 'raw.fact_draft (NBA draft history)',
           '''SELECT try_cast(season AS SMALLINT) AS draft_year, round_number::SMALLINT AS round_number,
                     round_pick::SMALLINT AS round_pick, overall_pick::SMALLINT AS overall_pick,
                     team_id, person_id, player_name, organization, organization_type
              FROM raw.fact_draft''',
           key=['draft_year', 'overall_pick', 'person_id'],
           fks={'person_id': 'core.dim_person(person_id)', 'team_id': 'core.dim_team(team_id)'},
           not_null=['draft_year', 'person_id']),
    Domain('fact_draft_combine', 'raw.fact_draft_combine (NBA draft combine measurements and drills)',
           '''SELECT * EXCLUDE (normalized_player_id), normalized_player_id AS person_id FROM raw.fact_draft_combine''',
           key=['season', 'person_id', 'source_table'],
           fks={'person_id': 'core.dim_person(person_id)'}, not_null=['season', 'person_id']),
    Domain('fact_award_vote', 'raw.fact_player_award_vote (Basketball-Reference award voting, NBA/BAA/ABA)',
           f'''SELECT season_end_year, league, award, {bref_person()} AS person_id, bref_player_id, player_name,
                      team_id, age, first AS first_place_votes, pts_won, pts_max, share, winner
               FROM raw.fact_player_award_vote x''',
           key=['season_end_year', 'award', 'bref_player_id'],
           fks={'person_id': 'core.dim_person(person_id)'}, not_null=['season_end_year', 'award', 'bref_player_id']),
    Domain('fact_player_honor', 'raw.fact_player_honor (All-NBA / All-Defense / All-Rookie teams)',
           f'''SELECT season_end_year, lg AS league, type AS honor, number_tm AS honor_team, {bref_person()} AS person_id,
                      bref_player_id, player_name, position
               FROM raw.fact_player_honor x''',
           key=['season_end_year', 'honor', 'bref_player_id'],
           fks={'person_id': 'core.dim_person(person_id)'}, not_null=['season_end_year', 'honor', 'bref_player_id']),
    Domain('fact_all_star_selection', 'raw.fact_all_star_selection',
           f'''SELECT season_end_year, lg AS league, {bref_person()} AS person_id, bref_player_id, player_name,
                      team AS all_star_team, replaced
               FROM raw.fact_all_star_selection x''',
           key=['season_end_year', 'league', 'bref_player_id', 'all_star_team'],
           fks={'person_id': 'core.dim_person(person_id)'}, not_null=['season_end_year', 'bref_player_id']),
    Domain('fact_player_award', 'raw.fact_player_awards (NBA.com award listings incl. player/rookie of the month/week)',
           '''SELECT player_id AS person_id, description, award_type, season, month, week, conference,
                     all_nba_team_number, subtype1, subtype2, subtype3
              FROM raw.fact_player_awards''',
           key=['person_id', 'description', 'season', 'month', 'week', 'conference', 'all_nba_team_number', 'subtype1', 'subtype2', 'subtype3'],
           fks={'person_id': 'core.dim_person(person_id)'}, not_null=['person_id', 'description']),
    Domain('fact_coach_season', 'raw.fact_coach_season (Basketball-Reference head coaches by team-season)',
           '''SELECT season_year, season_end_year::SMALLINT AS season_end_year, team_id, coach_bbr_slug, coach_name,
                     first_name, last_name, wins, losses, win_pct, coaches_in_season, source_url
              FROM raw.fact_coach_season''',
           key=['season_year', 'team_id', 'coach_bbr_slug'],
           fks={'team_id': 'core.dim_team(team_id)', 'season_year': 'core.dim_season(season_year)'},
           not_null=['season_year', 'team_id', 'coach_bbr_slug']),
    Domain('bridge_player_team_season', 'raw.bridge_player_team_season (season-level roster membership)',
           '''SELECT player_id AS person_id, team_id, season_year, jersey_number, position FROM raw.bridge_player_team_season''',
           key=['person_id', 'team_id', 'season_year'],
           fks={'person_id': 'core.dim_person(person_id)', 'team_id': 'core.dim_team(team_id)',
                'season_year': 'core.dim_season(season_year)'},
           not_null=['person_id', 'team_id', 'season_year']),
    Domain('fact_starting_lineup', 'raw.fact_starting_lineup_player (1996-) + box-score starting positions where no lineup row exists + Kaggle eoinamoore starters for games neither has',
           '''SELECT game_id, team_id, person_id, starting_position, 'nba_starting_lineup' AS source FROM raw.fact_starting_lineup_player
              UNION ALL
              SELECT game_id, team_id, person_id, starting_position, 'box_score_starting_position' FROM core.fact_player_game_stats
              WHERE nullif(trim(starting_position), '') IS NOT NULL
                AND game_id NOT IN (SELECT game_id FROM raw.fact_starting_lineup_player)
              UNION ALL  -- NBA.com boxscoretraditionalv3 starters fetched for remaining gaps (load_nba_gaps.py)
              SELECT game_id, team_id, person_id, starting_position, 'nba_boxscore_v3' FROM raw.nba_gap_starter
              WHERE game_id NOT IN (SELECT game_id FROM raw.fact_starting_lineup_player)
                AND game_id NOT IN (SELECT game_id FROM core.fact_player_game_stats WHERE nullif(trim(starting_position), '') IS NOT NULL)
              UNION ALL  -- Kaggle eoinamoore box scores (NBA.com-sourced) only for games no source above covers
              SELECT lpad(gameId, 10, '0'), try_cast(playerteamId AS BIGINT), try_cast(personId AS BIGINT), trim(startingPosition), 'kaggle_eoinamoore'
              FROM raw_kaggle.eoinamoore_historical_nba_data_and_player_box_scores__playerstatistics
              WHERE nullif(trim(startingPosition), '') IS NOT NULL
                AND lpad(gameId, 10, '0') NOT IN (SELECT game_id FROM raw.fact_starting_lineup_player)
                AND lpad(gameId, 10, '0') NOT IN (SELECT game_id FROM core.fact_player_game_stats WHERE nullif(trim(starting_position), '') IS NOT NULL)
                AND lpad(gameId, 10, '0') NOT IN (SELECT game_id FROM raw.nba_gap_starter)''',
           key=['game_id', 'person_id'],
           fks={'game_id': 'core.dim_game(game_id)', 'person_id': 'core.dim_person(person_id)',
                '(game_id, team_id)': 'core.bridge_game_team(game_id, team_id)'},
           not_null=['game_id', 'team_id', 'person_id']),
    Domain('fact_inactive_player', 'raw.fact_inactive_player (official inactive lists, 1996-2023)',
           '''SELECT game_id, team_id, person_id, jersey_num FROM raw.fact_inactive_player''',
           key=['game_id', 'person_id'],
           fks={'game_id': 'core.dim_game(game_id)', 'person_id': 'core.dim_person(person_id)',
                '(game_id, team_id)': 'core.bridge_game_team(game_id, team_id)'},
           not_null=['game_id', 'team_id', 'person_id']),
    Domain('fact_rotation_stint', 'raw.fact_rotation (player on-court stints, 2019-20 onward)',
           '''SELECT game_id, team_id, player_id AS person_id, in_time_real, out_time_real, pts, pts_diff, usg_pct
              FROM raw.fact_rotation''',
           key=['game_id', 'person_id', 'in_time_real'],
           fks={'game_id': 'core.dim_game(game_id)', 'person_id': 'core.dim_person(person_id)',
                '(game_id, team_id)': 'core.bridge_game_team(game_id, team_id)'},
           not_null=['game_id', 'team_id', 'person_id', 'in_time_real']),
    Domain('dim_official', 'raw.dim_official + raw.officials (NBA referee ids) + ESPN-resolved officials',
           '''SELECT official_id, any_value(first_name) AS first_name, any_value(last_name) AS last_name, any_value(jersey_num) AS jersey_num
              FROM (SELECT try_cast(official_id AS BIGINT) AS official_id, first_name, last_name, jersey_num, 1 AS pref FROM raw.dim_official
                    UNION ALL SELECT try_cast(official_id AS BIGINT), first_name, last_name, jersey_num, 1 FROM raw.officials
                    UNION ALL SELECT official_id, split_part(espn_official_full_name, ' ', 1),
                                     trim(substr(espn_official_full_name, length(split_part(espn_official_full_name, ' ', 1)) + 2)), NULL, 2
                    FROM raw.bridge_official_espn WHERE NOT is_ambiguous AND NOT is_unresolved AND official_id IS NOT NULL)
              GROUP BY 1 HAVING bool_or(pref = 1) OR true''',
           key=['official_id'], not_null=['official_id']),
    Domain('fact_game_official', 'raw.officials (NBA box-score officials, 1996-2023); ESPN-resolved officials, then Kaggle eoinamoore names, only for games NBA data lacks',
           '''SELECT game_id, try_cast(official_id AS BIGINT) AS official_id, jersey_num, 'nba_box_summary' AS source FROM raw.officials
              UNION ALL
              SELECT game_id, official_id, NULL, 'espn' FROM raw.bridge_official_espn
              WHERE NOT is_ambiguous AND NOT is_unresolved AND official_id IS NOT NULL AND game_id IS NOT NULL
                AND game_id NOT IN (SELECT game_id FROM raw.officials)
              UNION ALL  -- Kaggle eoinamoore game officials (names, NBA.com-sourced), only for games neither source above has;
                         -- a name is used only when it matches exactly one referee
              SELECT g.game_id, o.official_id, o.jersey_num, 'kaggle_eoinamoore'
              FROM (SELECT DISTINCT lpad(gameId, 10, '0') AS game_id, trim(unnest(string_split(officials, ','))) AS name
                    FROM raw_kaggle.eoinamoore_historical_nba_data_and_player_box_scores__games WHERE nullif(trim(officials), '') IS NOT NULL) g
              JOIN (SELECT lower(first_name || ' ' || last_name) AS name, any_value(official_id) AS official_id, any_value(jersey_num) AS jersey_num
                    FROM core.dim_official GROUP BY 1 HAVING count(*) = 1) o ON o.name = lower(g.name)
              WHERE g.game_id NOT IN (SELECT game_id FROM raw.officials)
                AND g.game_id NOT IN (SELECT game_id FROM raw.bridge_official_espn WHERE NOT is_ambiguous AND NOT is_unresolved
                                      AND official_id IS NOT NULL AND game_id IS NOT NULL)''',
           key=['game_id', 'official_id'],
           fks={'game_id': 'core.dim_game(game_id)', 'official_id': 'core.dim_official(official_id)'},
           not_null=['game_id', 'official_id']),
    Domain('fact_player_season_estimated', 'raw.fact_player_estimated_metrics (NBA estimated ratings, 1996-97 onward)',
           '''SELECT season_year, player_id AS person_id, team_id, gp, w, l, min, e_off_rating, e_def_rating, e_net_rating,
                     e_pace, e_ast_ratio, e_oreb_pct, e_dreb_pct, e_reb_pct, e_tov_pct, e_usg_pct
              FROM raw.fact_player_estimated_metrics''',
           key=['season_year', 'person_id', 'team_id'],
           fks={'person_id': 'core.dim_person(person_id)', 'season_year': 'core.dim_season(season_year)'},
           not_null=['season_year', 'person_id']),
    Domain('fact_season_matchup', 'raw.fact_season_matchups (NBA player-vs-player matchups)',
           '''SELECT * EXCLUDE (off_player_name, def_player_name) FROM raw.fact_season_matchups''',
           key=['season_id', 'off_player_id', 'def_player_id', 'matchup_type'],
           fks={'off_player_id': 'core.dim_person(person_id)', 'def_player_id': 'core.dim_person(person_id)'},
           not_null=['season_id', 'off_player_id', 'def_player_id']),
    Domain('fact_franchise_history', 'raw.stg_franchise (franchise eras with records and titles)',
           '''SELECT team_id, league_id, team_city, team_name, start_year, end_year, years, games, wins, losses, win_pct,
                     po_appearances, div_titles, conf_titles, league_titles FROM raw.stg_franchise''',
           key=['team_id', 'team_city', 'team_name', 'start_year'],
           fks={'team_id': 'core.dim_team(team_id)'}, not_null=['team_id']),
    Domain('dim_arena', 'raw.dim_arena',
           '''SELECT arena_id, arena_name, city, state, country FROM raw.dim_arena''',
           key=['arena_id'], not_null=['arena_id', 'arena_name']),
]


def promote(c, d: Domain) -> tuple[int, int, int, int]:
    c.execute(f'CREATE OR REPLACE TEMP TABLE cand AS SELECT row_number() OVER () AS _row, * FROM ({d.select})')
    cols = [r[0] for r in c.execute("SELECT column_name FROM duckdb_columns() WHERE table_name='cand' AND column_name <> '_row' ORDER BY column_index").fetchall()]
    reasons = []
    for col in d.not_null:
        reasons.append((f'missing_{col}', f'{q(col)} IS NULL'))
    for col, ref in d.fks.items():
        table, rcols = ref[:-1].split('(')
        lhs = col if col.startswith('(') else f'({q(col)})'
        rc = f'({rcols})'
        nonnull = ' AND '.join(f'{x.strip()} IS NOT NULL' for x in lhs.strip('()').split(','))
        reasons.append((f'unresolved_{col.strip("()").replace(", ", "_")}',
                        f'{nonnull} AND {lhs} NOT IN (SELECT {rc} FROM {table})'))
    case = ' '.join(f"WHEN {cond} THEN '{name}'" for name, cond in reasons)
    keys = ', '.join(q(k) for k in d.key)
    allc = ', '.join(q(x) for x in cols)
    c.execute(f'''CREATE OR REPLACE TEMP TABLE judged AS
        SELECT *, CASE {case or "WHEN false THEN ''"} ELSE NULL END AS _hold FROM cand''')
    # among rows that pass, exact duplicates collapse; distinct rows sharing a key are a conflict -> held
    c.execute(f'''CREATE OR REPLACE TEMP TABLE judged2 AS
        SELECT *, row_number() OVER (PARTITION BY {allc} ORDER BY _row) AS _copy,
               count(DISTINCT md5(CAST(row({allc}) AS VARCHAR))) OVER (PARTITION BY {keys}) AS _variants
        FROM judged WHERE _hold IS NULL''')
    c.execute(f'''CREATE OR REPLACE TABLE core.{q(d.name)} AS
        SELECT {allc} FROM judged2 WHERE _variants = 1 AND _copy = 1 ORDER BY {keys}''')
    try:
        c.execute(f'ALTER TABLE core.{q(d.name)} ADD PRIMARY KEY ({keys})')
    except Exception:
        pass  # nullable key columns (e.g. month/week) cannot form a primary key; uniqueness is asserted below
    dup = c.execute(f'SELECT count(*) - count(DISTINCT ({keys})) FROM core.{q(d.name)}').fetchone()[0]
    if dup:
        raise SystemExit(f'{d.name}: {dup} duplicate keys after promotion')
    c.execute(f'''INSERT INTO audit.domain_held
        SELECT '{d.name}', _row, _hold, to_json(j) FROM (SELECT * EXCLUDE (_hold) , _hold FROM judged) j WHERE _hold IS NOT NULL
        UNION ALL SELECT '{d.name}', _row, 'conflicting_rows_for_key', to_json(j) FROM (SELECT * EXCLUDE (_copy, _variants, _hold) FROM judged2) j
            WHERE _row IN (SELECT _row FROM judged2 WHERE _variants > 1)''')
    n_src = c.execute('SELECT count(*) FROM cand').fetchone()[0]
    n_pub = c.execute(f'SELECT count(*) FROM core.{q(d.name)}').fetchone()[0]
    n_dup = c.execute('SELECT count(*) FROM judged2 WHERE _variants = 1 AND _copy > 1').fetchone()[0]
    n_held = c.execute(f"SELECT count(*) FROM audit.domain_held WHERE domain = '{d.name}'").fetchone()[0]
    if n_src != n_pub + n_dup + n_held:
        raise SystemExit(f'{d.name}: conservation failed {n_src} != {n_pub}+{n_dup}+{n_held}')
    c.execute('INSERT INTO audit.domain_disposition VALUES (?,?,?,?,?,?)', [d.name, d.source, n_src, n_pub, n_dup, n_held])
    return n_src, n_pub, n_dup, n_held


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--output', type=Path, default=DEFAULT_OUT)
    ap.add_argument('--only', nargs='*')
    a = ap.parse_args()
    c = connect(a.output)
    c.execute('''CREATE TABLE IF NOT EXISTS audit.domain_disposition (domain VARCHAR PRIMARY KEY, source VARCHAR,
        source_rows BIGINT, published BIGINT, duplicate BIGINT, held BIGINT)''')
    c.execute('CREATE TABLE IF NOT EXISTS audit.domain_held (domain VARCHAR, source_row BIGINT, reason VARCHAR, row_json JSON)')
    if not a.only:
        person_dimension(c)
    for d in DOMAINS:
        if a.only and d.name not in a.only:
            continue
        c.execute('DELETE FROM audit.domain_disposition WHERE domain = ?', [d.name])
        c.execute('DELETE FROM audit.domain_held WHERE domain = ?', [d.name])
        print(d.name, *promote(c, d), sep='\t', flush=True)
    c.execute('CHECKPOINT')


if __name__ == '__main__':
    main()
