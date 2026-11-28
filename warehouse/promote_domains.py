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
from observability import operation


# One referee per normalized name (lowercase letters only, so "Sha'Rae" = "ShaRae"). Several names carry more than one
# official_id (NBA box ids plus duplicates from the Kaggle and ESPN sources); the id NBA box scores use most wins, then the
# id the Kaggle kyleknowles1 assignments use most. Names whose ids are all unused stay ambiguous unless there is only one.
OFFICIAL_NAME_CANON = """(SELECT name, arg_max(official_id, score) AS official_id, arg_max(jersey_num, score) AS jersey_num FROM (
    SELECT n.name, n.official_id, d.jersey_num, coalesce(u.nba, 0) * 100000 + coalesce(k.kk, 0) AS score FROM (
        SELECT DISTINCT regexp_replace(lower(first_name || last_name), '[^a-z]', '', 'g') AS name, official_id FROM core.dim_official
        UNION SELECT DISTINCT regexp_replace(lower(espn_official_full_name), '[^a-z]', '', 'g'), official_id FROM raw.bridge_official_espn
              WHERE NOT is_ambiguous AND NOT is_unresolved AND official_id IS NOT NULL) n
    LEFT JOIN core.dim_official d USING (official_id)
    LEFT JOIN (SELECT try_cast(official_id AS BIGINT) AS official_id, count(*) AS nba FROM raw.officials GROUP BY 1) u USING (official_id)
    LEFT JOIN (SELECT try_cast(c AS BIGINT) AS official_id, count(*) AS kk FROM (SELECT unnest([official1_code, official2_code,
               official3_code, official4_code]) AS c FROM raw_kaggle.kyleknowles1_nba_referee_assignments) GROUP BY 1) k USING (official_id))
  GROUP BY 1 HAVING max(score) > 0 OR count(DISTINCT official_id) = 1)"""


def norm_name(expr: str) -> str:
    return f"regexp_replace(lower({expr}), '[^a-z]', '', 'g')"


def create_press_official_identities(c) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS raw.press_official_identity (
        official_id BIGINT PRIMARY KEY, first_name VARCHAR, last_name VARCHAR,
        source_url VARCHAR, UNIQUE (first_name, last_name))''')


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
    Domain('fact_starting_lineup', 'NBA starter facts and verified box scores, with other named or inferred starter facts filling gaps',
           '''WITH high_source AS (SELECT game_id, team_id, person_id, starting_position, 'nba_starting_lineup' AS source FROM raw.fact_starting_lineup_player
              UNION ALL
              SELECT game_id, team_id, person_id, starting_position, 'box_score_starting_position' FROM core.fact_player_game_stats
              WHERE nullif(trim(starting_position), '') IS NOT NULL
                AND game_id NOT IN (SELECT game_id FROM raw.fact_starting_lineup_player)
              UNION ALL  -- NBA.com boxscoretraditionalv3 starters fetched for remaining gaps (load_nba_gaps.py)
              SELECT game_id, team_id, person_id, starting_position, 'nba_boxscore_v3' FROM raw.nba_gap_starter
              WHERE game_id NOT IN (SELECT game_id FROM raw.fact_starting_lineup_player)
                AND game_id NOT IN (SELECT game_id FROM core.fact_player_game_stats WHERE nullif(trim(starting_position), '') IS NOT NULL)
              UNION ALL  -- llimllib/nba_data season files (NBA.com starter flag, no position) for games no source above covers
              SELECT game_id, team_id, person_id, NULL, 'nba_llimllib' FROM raw.llimllib_starter
              WHERE game_id NOT IN (SELECT game_id FROM raw.fact_starting_lineup_player)
                AND game_id NOT IN (SELECT game_id FROM core.fact_player_game_stats WHERE nullif(trim(starting_position), '') IS NOT NULL)
                AND game_id NOT IN (SELECT game_id FROM raw.nba_gap_starter GROUP BY 1 HAVING count(*) = 10)
              UNION ALL  -- Kaggle eoinamoore box scores (NBA.com-sourced) only for games no source above covers
              SELECT lpad(gameId, 10, '0'), try_cast(playerteamId AS BIGINT), try_cast(personId AS BIGINT), trim(startingPosition), 'kaggle_eoinamoore'
              FROM raw_kaggle.eoinamoore_historical_nba_data_and_player_box_scores__playerstatistics
              WHERE nullif(trim(startingPosition), '') IS NOT NULL
                AND lpad(gameId, 10, '0') NOT IN (SELECT game_id FROM raw.fact_starting_lineup_player)
                AND lpad(gameId, 10, '0') NOT IN (SELECT game_id FROM core.fact_player_game_stats WHERE nullif(trim(starting_position), '') IS NOT NULL)
                AND lpad(gameId, 10, '0') NOT IN (SELECT game_id FROM raw.nba_gap_starter)
                AND lpad(gameId, 10, '0') NOT IN (SELECT game_id FROM raw.llimllib_starter)),
              press_starters AS (
                SELECT DISTINCT game_id,team_id,person_id FROM raw.usatoday_starter
                QUALIFY count(DISTINCT person_id) OVER (PARTITION BY game_id,team_id) = 5),
              invalid_high_source AS (
                SELECT game_id, team_id FROM high_source GROUP BY 1,2
                HAVING count(DISTINCT person_id) <> 5 OR count(DISTINCT source) > 1),
              verified_nba_flags AS (
                SELECT DISTINCT s.game_id,s.team_id,s.person_id
                FROM raw.llimllib_starter s
                JOIN invalid_high_source b USING (game_id,team_id)
                JOIN core.fact_player_game_stats p USING (game_id,team_id,person_id)
                WHERE p.participation_status='played'
                QUALIFY count(DISTINCT s.person_id) OVER (PARTITION BY s.game_id,s.team_id) = 5),
              reviewed AS (
                SELECT game_id,team_id,person_id,starting_position,source FROM raw.verified_starting_lineup
                QUALIFY count(*) OVER (PARTITION BY game_id,team_id) = 5
                  AND count(DISTINCT person_id) OVER (PARTITION BY game_id,team_id) = 5
                  AND count(DISTINCT (source_url,sha256)) OVER (PARTITION BY game_id,team_id) = 1),
              reviewed_replacements AS (
                SELECT * FROM reviewed r WHERE source='nba_official_scorer_report' OR (
                    (r.game_id,r.team_id) NOT IN (SELECT game_id,team_id FROM high_source
                       WHERE (game_id,team_id) NOT IN (SELECT game_id,team_id FROM invalid_high_source))
                    AND (r.game_id,r.team_id) NOT IN (SELECT game_id,team_id FROM verified_nba_flags)
                    AND (r.game_id,r.team_id) NOT IN (SELECT game_id,team_id FROM raw.wayback_starter
                       GROUP BY 1,2 HAVING count(DISTINCT person_id)=5)
                    AND (source='nba_com_archive_verified' OR (r.game_id,r.team_id) NOT IN (
                        SELECT game_id,team_id FROM raw.captured_club_starter UNION ALL SELECT game_id,team_id FROM press_starters)))),
              hi AS (
                SELECT * FROM high_source WHERE (game_id,team_id) NOT IN (
                  SELECT p.game_id,p.team_id FROM press_starters p JOIN invalid_high_source b USING (game_id,team_id)
                  UNION ALL SELECT game_id,team_id FROM verified_nba_flags
                  UNION ALL SELECT game_id,team_id FROM reviewed_replacements)
                UNION ALL SELECT f.game_id,f.team_id,f.person_id,p.starting_position,'nba_llimllib'
                FROM verified_nba_flags f LEFT JOIN (
                  SELECT game_id,team_id,person_id,min(starting_position) AS starting_position
                  FROM high_source GROUP BY 1,2,3 HAVING count(DISTINCT starting_position) = 1
                ) p USING (game_id,team_id,person_id)
                WHERE (f.game_id,f.team_id) NOT IN (SELECT game_id,team_id FROM reviewed_replacements)
                UNION ALL SELECT * FROM reviewed_replacements)
              SELECT * FROM hi
              UNION ALL  -- archived NBA.com box pages (load_wayback_nba.py): only teams no source above names starters for
              SELECT game_id, team_id, person_id, NULL, 'nba_com_archive' FROM raw.wayback_starter
              WHERE (game_id, team_id) NOT IN (SELECT game_id, team_id FROM hi)
              UNION ALL
              SELECT game_id, team_id, person_id, NULL, source FROM raw.captured_club_starter
              WHERE (game_id, team_id) NOT IN (SELECT game_id, team_id FROM hi)
                AND (game_id, team_id) NOT IN (SELECT game_id, team_id FROM raw.wayback_starter)
              UNION ALL
              SELECT game_id, team_id, person_id, NULL, 'usatoday_preseason' FROM press_starters
              WHERE (game_id, team_id) NOT IN (SELECT game_id,team_id FROM hi UNION ALL
                  SELECT game_id,team_id FROM raw.wayback_starter UNION ALL SELECT game_id,team_id FROM raw.captured_club_starter)
              UNION ALL  -- ESPN public API starters (load_espn.py, owner-approved), lowest rank: only teams no source above names starters for
              SELECT game_id, team_id, person_id, NULL, 'espn_api' FROM raw.espn_api_starter
              WHERE (game_id, team_id) NOT IN (SELECT game_id, team_id FROM hi)
                AND (game_id, team_id) NOT IN (SELECT game_id, team_id FROM raw.wayback_starter)
                AND (game_id, team_id) NOT IN (SELECT game_id, team_id FROM raw.captured_club_starter)
                AND (game_id, team_id) NOT IN (SELECT game_id, team_id FROM press_starters)
              UNION ALL  -- starters inferred from NBA play-by-play (load_nba_gaps.pbp_starters), only teams no source names starters for
              SELECT game_id, team_id, person_id, NULL, 'pbp_inferred' FROM raw.pbp_inferred_starter
              WHERE (game_id, team_id) NOT IN (SELECT game_id, team_id FROM hi)
                AND (game_id, team_id) NOT IN (SELECT game_id, team_id FROM raw.espn_api_starter)
                AND (game_id, team_id) NOT IN (SELECT game_id, team_id FROM raw.wayback_starter)
                AND (game_id, team_id) NOT IN (SELECT game_id, team_id FROM raw.captured_club_starter)
                AND (game_id, team_id) NOT IN (SELECT game_id, team_id FROM press_starters)''',
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
           '''SELECT official_id, arg_min(first_name, pref) AS first_name, arg_min(last_name, pref) AS last_name, arg_min(jersey_num, pref) AS jersey_num
              FROM (SELECT try_cast(official_id AS BIGINT) AS official_id, first_name, last_name, jersey_num, 1 AS pref FROM raw.dim_official
                    UNION ALL SELECT try_cast(official_id AS BIGINT), first_name, last_name, jersey_num, 1 FROM raw.officials
                    UNION ALL SELECT official_id, first_name, last_name, jersey_num, 1 FROM raw.nba_gap_official
                    UNION ALL SELECT official_id, split_part(espn_official_full_name, ' ', 1),
                                     trim(substr(espn_official_full_name, length(split_part(espn_official_full_name, ' ', 1)) + 2)), NULL, 2
                    FROM raw.bridge_official_espn WHERE NOT is_ambiguous AND NOT is_unresolved AND official_id IS NOT NULL
                    UNION ALL  -- referees named with NBA ids in the Kaggle kyleknowles1 assignments (2016-2026)
                    SELECT try_cast(code AS BIGINT), split_part(name, ' ', 1), trim(substr(name, length(split_part(name, ' ', 1)) + 2)), jnum, 3
                    FROM (SELECT unnest([official1, official2, official3, official4]) AS name, unnest([official1_code, official2_code, official3_code, official4_code]) AS code,
                                 unnest([official1_JNum, official2_JNum, official3_JNum, official4_JNum]) AS jnum FROM raw_kaggle.kyleknowles1_nba_referee_assignments)
                    WHERE nullif(trim(code), '') IS NOT NULL
                    UNION ALL  -- referees on Basketball-Reference box pages with no NBA referee id (synthetic ids, load_bref.official_map)
                    SELECT official_id, split_part(official_name, ' ', 1), trim(substr(official_name, length(split_part(official_name, ' ', 1)) + 2)), NULL, 4
                    FROM raw.bref_official_map WHERE how = 'new'
                    UNION ALL SELECT official_id, first_name, last_name, NULL, 4 FROM raw.press_official_identity)
              GROUP BY 1 HAVING bool_or(pref = 1) OR true''',
           key=['official_id'], not_null=['official_id']),
    Domain('fact_game_official', 'raw.officials (NBA box-score officials, 1996-2023); ESPN-resolved officials, then Kaggle eoinamoore names, only for games NBA data lacks',
           '''WITH base AS (
SELECT game_id, try_cast(official_id AS BIGINT) AS official_id, jersey_num, 'nba_box_summary' AS source FROM raw.officials
              UNION ALL  -- NBA.com boxscoresummaryv3 referees (load_nba_gaps.officials), for games the summary data above lacks
              SELECT DISTINCT game_id, official_id, jersey_num, 'nba_summary_v3' FROM raw.nba_gap_official
              WHERE game_id NOT IN (SELECT game_id FROM raw.officials)
              UNION ALL
              SELECT game_id, official_id, NULL, 'espn' FROM raw.bridge_official_espn
              WHERE NOT is_ambiguous AND NOT is_unresolved AND official_id IS NOT NULL AND game_id IS NOT NULL
                AND game_id NOT IN (SELECT game_id FROM raw.officials) AND game_id NOT IN (SELECT game_id FROM raw.nba_gap_official)
              UNION ALL  -- Kaggle eoinamoore game officials (names, NBA.com-sourced), only for games neither source above has;
                         -- a name is used only when it matches exactly one referee
              SELECT g.game_id, o.official_id, o.jersey_num, 'kaggle_eoinamoore'
              FROM (SELECT DISTINCT lpad(gameId, 10, '0') AS game_id, trim(unnest(string_split(officials, ','))) AS name
                    FROM raw_kaggle.eoinamoore_historical_nba_data_and_player_box_scores__games WHERE nullif(trim(officials), '') IS NOT NULL) g
              JOIN ''' + OFFICIAL_NAME_CANON + ''' o ON o.name = ''' + norm_name('g.name') + '''
              WHERE g.game_id NOT IN (SELECT game_id FROM raw.officials) AND g.game_id NOT IN (SELECT game_id FROM raw.nba_gap_official)
                AND g.game_id NOT IN (SELECT game_id FROM raw.bridge_official_espn WHERE NOT is_ambiguous AND NOT is_unresolved
                                      AND official_id IS NOT NULL AND game_id IS NOT NULL)
              UNION ALL  -- Kaggle kyleknowles1 referee assignments 2016-2026 (NBA game and referee ids), only for games no source above has
              SELECT DISTINCT lpad(k.game_id, 10, '0'), coalesce(o.official_id, try_cast(k.code AS BIGINT)), k.jnum, 'kaggle_kyleknowles1'
              FROM (SELECT game_id, unnest([official1, official2, official3, official4]) AS name,
                           unnest([official1_code, official2_code, official3_code, official4_code]) AS code,
                           unnest([official1_JNum, official2_JNum, official3_JNum, official4_JNum]) AS jnum
                    FROM raw_kaggle.kyleknowles1_nba_referee_assignments) k
              LEFT JOIN ''' + OFFICIAL_NAME_CANON + ''' o ON o.name = ''' + norm_name('k.name') + '''
              WHERE nullif(trim(k.code), '') IS NOT NULL
                AND lpad(k.game_id, 10, '0') NOT IN (SELECT game_id FROM raw.officials)
                AND lpad(k.game_id, 10, '0') NOT IN (SELECT game_id FROM raw.nba_gap_official)
                AND lpad(k.game_id, 10, '0') NOT IN (SELECT game_id FROM raw.bridge_official_espn WHERE NOT is_ambiguous AND NOT is_unresolved
                                                    AND official_id IS NOT NULL AND game_id IS NOT NULL)
                AND lpad(k.game_id, 10, '0') NOT IN (SELECT lpad(gameId, 10, '0') FROM raw_kaggle.eoinamoore_historical_nba_data_and_player_box_scores__games
                                                    WHERE nullif(trim(officials), '') IS NOT NULL)
              UNION ALL  -- ESPN 1996-2002 crews (sportsdataverse release, load_espn_officials.py), only for games no source above has
              SELECT DISTINCT e.game_id, o.official_id, o.jersey_num, 'espn_sdv'
              FROM raw.espn_sdv_game_official e
              JOIN ''' + OFFICIAL_NAME_CANON + ''' o ON o.name = ''' + norm_name('e.official_full_name') + '''
              WHERE e.game_id IS NOT NULL
                AND e.game_id NOT IN (SELECT game_id FROM raw.officials) AND e.game_id NOT IN (SELECT game_id FROM raw.nba_gap_official)
                AND e.game_id NOT IN (SELECT game_id FROM raw.bridge_official_espn WHERE NOT is_ambiguous AND NOT is_unresolved
                                      AND official_id IS NOT NULL AND game_id IS NOT NULL)
                AND e.game_id NOT IN (SELECT lpad(gameId, 10, '0') FROM raw_kaggle.eoinamoore_historical_nba_data_and_player_box_scores__games
                                      WHERE nullif(trim(officials), '') IS NOT NULL)
),
bb AS (  -- Basketball-Reference box-page crews (referee ids resolved by load_bref.official_map)
              SELECT DISTINCT g.game_id, m.official_id, d.jersey_num, 'bref_box' AS source
              FROM raw.bref_box_official b
              JOIN raw.bref_official_map m USING (bref_referee_id)
              LEFT JOIN core.dim_official d ON d.official_id = m.official_id
              JOIN core.team_source_id t ON t.source_system = 'basketball_reference' AND t.league = 'NBA' AND t.source_key = substr(b.bref_game_key, 10)
              JOIN core.dim_game g ON g.home_team_id = t.team_id AND strftime(g.game_date, '%Y%m%d') = substr(b.bref_game_key, 1, 8)
                   AND t.season_end_year = CAST(substr(g.season_year, 1, 4) AS INTEGER) + 1
              WHERE g.season_type IN ('Regular', 'Playoffs')),
bb_full AS (SELECT game_id FROM bb GROUP BY 1 HAVING count(DISTINCT official_id) >= 3),
espn_only AS (  -- games whose crew comes only from ESPN, which Basketball-Reference outranks
              SELECT game_id FROM base GROUP BY 1 HAVING bool_and(source IN ('espn', 'espn_sdv'))),
arc AS (
              SELECT DISTINCT game_id, official_id, nullif(jersey_num, '') AS jersey_num, 'nba_com_archive' AS source
              FROM raw.wayback_game_official WHERE official_id IS NOT NULL
              UNION ALL
              SELECT DISTINCT game_id, official_id, NULL,
                  CASE WHEN contains(source_url, 'hurriyet.com.tr/') THEN 'newspaper_hurriyet' ELSE 'verified_press_crew' END
              FROM raw.press_game_official
              WHERE game_id NOT IN (SELECT game_id FROM raw.wayback_game_official WHERE official_id IS NOT NULL)
              UNION ALL
              SELECT DISTINCT game_id, official_id, NULL, 'usatoday_preseason' FROM raw.usatoday_official
              WHERE game_id NOT IN (SELECT game_id FROM raw.wayback_game_official WHERE official_id IS NOT NULL UNION ALL
                                   SELECT game_id FROM raw.press_game_official)
              QUALIFY count(DISTINCT official_id) OVER (PARTITION BY game_id) = 3),
arc_eligible AS (
              SELECT game_id FROM arc GROUP BY game_id
              HAVING count(DISTINCT official_id) >= 3 OR game_id NOT IN (SELECT game_id FROM base UNION SELECT game_id FROM bb)),
arc_selected AS (
              SELECT * FROM arc WHERE game_id IN (SELECT game_id FROM arc_eligible)
                AND game_id NOT IN (SELECT game_id FROM base WHERE source IN ('nba_box_summary', 'nba_summary_v3'))),
ranked AS (
              SELECT * FROM base WHERE game_id NOT IN (SELECT game_id FROM espn_only WHERE game_id IN (SELECT game_id FROM bb_full))
              UNION ALL  -- a full Basketball-Reference crew replaces an ESPN-only crew
              SELECT * FROM bb WHERE game_id IN (SELECT game_id FROM espn_only) AND game_id IN (SELECT game_id FROM bb_full)
              UNION ALL  -- ESPN public API crews (load_espn.py, owner-approved), lowest rank: only games no source above names anyone for
              SELECT DISTINCT e.game_id, o.official_id, o.jersey_num, 'espn_api'
              FROM raw.espn_api_game_official e
              JOIN ''' + OFFICIAL_NAME_CANON + ''' o ON o.name = ''' + norm_name('e.official_name') + '''
              WHERE e.game_id NOT IN (SELECT game_id FROM base) AND e.game_id NOT IN (SELECT game_id FROM bb)
                AND e.game_id NOT IN (SELECT game_id FROM arc_selected)
              UNION ALL  -- Wikipedia All-Star Game infobox crews (load_wiki_officials.py), lowest rank, only games no source above names anyone for
              SELECT DISTINCT w.game_id, o.official_id, o.jersey_num, 'wikipedia'
              FROM raw.wiki_game_official w
              JOIN ''' + OFFICIAL_NAME_CANON + ''' o ON o.name = ''' + norm_name('w.official_name') + '''
              WHERE w.game_id NOT IN (SELECT game_id FROM base) AND w.game_id NOT IN (SELECT game_id FROM bb)
                AND w.game_id NOT IN (SELECT game_id FROM raw.espn_api_game_official)
                AND w.game_id NOT IN (SELECT game_id FROM arc_selected)
              UNION ALL  -- otherwise Basketball-Reference completes crews of fewer than 3, or gives a full crew where no source has one
              SELECT * FROM bb
              WHERE bb.game_id NOT IN (SELECT game_id FROM espn_only)
                AND (bb.game_id IN (SELECT game_id FROM base GROUP BY 1 HAVING count(DISTINCT official_id) < 3)
                     OR (bb.game_id NOT IN (SELECT game_id FROM base) AND bb.game_id IN (SELECT game_id FROM bb_full)))
                AND (bb.game_id, bb.official_id) NOT IN (SELECT game_id, official_id FROM base)),
selected AS (
              SELECT * FROM ranked WHERE game_id NOT IN (SELECT game_id FROM arc_selected)
              UNION ALL SELECT * FROM arc_selected),
confirmation_crew AS (
              SELECT game_id, official_id, 'usatoday_preseason' AS source, 2 AS priority FROM raw.usatoday_official
              UNION ALL SELECT game_id,official_id,
                CASE WHEN contains(source_url, 'hurriyet.com.tr/') THEN 'newspaper_hurriyet' ELSE 'verified_press_crew' END,1
              FROM raw.press_game_official),
complete_crew_confirmation AS (
              SELECT game_id,source,priority,list(DISTINCT official_id ORDER BY official_id) AS officials
              FROM confirmation_crew GROUP BY 1,2,3 HAVING count(DISTINCT official_id) = 3
              QUALIFY count(DISTINCT officials) OVER (PARTITION BY game_id,priority) = 1),
confirmed_partial AS (
              SELECT s.game_id,u.source,u.priority FROM selected s JOIN complete_crew_confirmation u USING (game_id)
              GROUP BY s.game_id,u.source,u.priority HAVING count(DISTINCT s.official_id) < 3
                AND bool_and(list_contains(u.officials, s.official_id))
              QUALIFY row_number() OVER (PARTITION BY s.game_id ORDER BY u.priority,u.source) = 1),
supplemented AS (
              SELECT * FROM selected UNION ALL
              SELECT p.game_id, u.official_id, NULL, p.source
              FROM confirmed_partial p JOIN (SELECT DISTINCT game_id,source,official_id FROM confirmation_crew) u USING (game_id,source)
              WHERE (p.game_id, u.official_id) NOT IN (SELECT game_id, official_id FROM selected)),
official_report AS (
              SELECT DISTINCT game_id,official_id,jersey_num,'nba_official_scorer_report' AS source FROM raw.nba_scorer_crew
              QUALIFY count(DISTINCT official_id) OVER (PARTITION BY game_id) = 3)
              SELECT * FROM official_report UNION ALL
              SELECT * FROM supplemented WHERE game_id NOT IN (SELECT game_id FROM official_report)''',
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
    if d.name == 'fact_starting_lineup':
        from load_verified_lineups import create_table
        create_table(c)
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
    with operation('promote_domains', a.output):
        c = connect(a.output)
        create_press_official_identities(c)
        c.execute('''CREATE TABLE IF NOT EXISTS raw.captured_club_starter (
            game_id VARCHAR, team_id BIGINT, person_id BIGINT, source VARCHAR, source_url VARCHAR,
            PRIMARY KEY (game_id, team_id, person_id, source))''')
        c.execute('''CREATE TABLE IF NOT EXISTS raw.press_game_official (
            game_id VARCHAR, official_id BIGINT, official_name VARCHAR, official_order INTEGER, source_url VARCHAR, sha256 VARCHAR,
            PRIMARY KEY (game_id, official_id, sha256))''')
        from load_usatoday_preseason import create_tables as create_usatoday_tables
        create_usatoday_tables(c)
        from load_nba_scorer_crews import create_table as create_scorer_crew_table
        create_scorer_crew_table(c)
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
