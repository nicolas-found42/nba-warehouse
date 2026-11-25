"""Promote newly collected sources into core (step 5).

  identities     core.team_source_id (Basketball-Reference abbreviations by season -> team_id; permanent
                 synthetic ids for ABA-only teams) and permanent synthetic person ids for players who
                 never appeared in NBA data (recorded in core.person_source_id)
  transactions   core.fact_transaction + core.bridge_transaction_party from Basketball-Reference season
                 pages and NBA.com player movement (NBA.com outranks Basketball-Reference from 2015-07)
  aba            core.aba_game / aba_game_result / aba_regular_standings (+ box scores when cached)
  salaries       core.fact_player_salary from Basketball-Reference team pages and Kaggle salary tables
  tracking       core.fact_tracking_season from NBA.com tracking and hustle tables

Synthetic ids are stored, never recomputed: re-running keeps every id already issued.
Held rows (unresolved person/team/game) go to audit.domain_held with a reason, and
audit.domain_disposition records source = published + duplicate + held for each domain.

  uv run --no-project --with duckdb==2.0.0 python warehouse/promote_new_sources.py [part ...]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_warehouse import DEFAULT_OUT, connect

SYNTH_TEAM_BASE = 1690100000   # ABA-only teams: 1690100001, 1690100002, ...
SYNTH_PERSON_BASE = 1680000000  # players absent from all NBA data: 1680000001, ... (900000001-2 are used upstream)
JUDGMENTS = Path(__file__).resolve().parents[1] / 'data' / 'judgments'
MOVEMENT = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'nba-player-movement'


def disposition(c, domain: str, source: str, n_src: int) -> None:
    n_pub = c.execute(f'SELECT count(*) FROM core.{domain}').fetchone()[0]
    n_held = c.execute('SELECT count(*) FROM audit.domain_held WHERE domain = ?', [domain]).fetchone()[0]
    c.execute('DELETE FROM audit.domain_disposition WHERE domain = ?', [domain])
    c.execute('INSERT INTO audit.domain_disposition VALUES (?,?,?,?,?,?)', [domain, source, n_src, n_pub, n_src - n_pub - n_held, n_held])
    if n_src - n_pub - n_held < 0:
        raise SystemExit(f'{domain}: published+held exceeds source rows')
    print(domain, n_src, n_pub, n_held, sep='\t', flush=True)


def identities(c) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS core.team_source_id (team_id BIGINT NOT NULL, source_system VARCHAR NOT NULL,
        source_key VARCHAR NOT NULL, league VARCHAR NOT NULL, season_end_year INTEGER NOT NULL, team_name VARCHAR,
        PRIMARY KEY (source_system, source_key, league, season_end_year))''')
    # Basketball-Reference abbreviation per league-season; NBA/BAA rows carry the NBA team id
    c.execute('''CREATE OR REPLACE TEMP TABLE abbr0 AS
        SELECT DISTINCT season AS season_end_year, lg AS league, abbreviation AS abbr, team AS team_name,
               coalesce(nba_team_id, CASE WHEN lg = 'ABA' THEN CASE abbreviation
                   -- ABA franchises that joined the NBA in 1976 keep their NBA team id (Basketball-Reference franchise lineage)
                   WHEN 'DLC' THEN 1610612759 WHEN 'TEX' THEN 1610612759 WHEN 'SAA' THEN 1610612759     -- Spurs
                   WHEN 'NJA' THEN 1610612751 WHEN 'NYA' THEN 1610612751                                -- Nets
                   WHEN 'DNR' THEN 1610612743 WHEN 'DNA' THEN 1610612743                                -- Nuggets
                   WHEN 'INA' THEN 1610612754 END END) AS nba_team_id                                 -- Pacers
        FROM raw.stg_bref_team_abbrev WHERE abbreviation IS NOT NULL''')
    # NBA/BAA seasons where the source left the id blank: same abbreviation's id in other seasons, else the
    # core team whose dated name matches the season's team name
    c.execute('''CREATE OR REPLACE TEMP TABLE abbr AS
        SELECT a.season_end_year, a.league, a.abbr, a.team_name,
               coalesce(a.nba_team_id,
                        CASE WHEN a.league <> 'ABA' THEN max(a.nba_team_id) OVER (PARTITION BY a.league <> 'ABA', a.abbr) END,
                        CASE WHEN a.league <> 'ABA' THEN (SELECT min(v.team_id) FROM core.dim_team_version v
                              WHERE lower(v.full_name) = lower(a.team_name)
                                AND a.season_end_year - 1 BETWEEN v.valid_from AND coalesce(v.valid_to, 9999)) END) AS nba_team_id
        FROM abbr0 a''')
    # permanent synthetic ids for ABA teams without an NBA id (one per abbreviation), kept across runs
    c.execute('''CREATE TABLE IF NOT EXISTS core.synthetic_team (source_key VARCHAR PRIMARY KEY, team_id BIGINT UNIQUE,
        display_name VARCHAR, issued_at TIMESTAMPTZ)''')
    c.execute(f'''INSERT INTO core.synthetic_team
        SELECT abbr, {SYNTH_TEAM_BASE} + coalesce((SELECT max(team_id) - {SYNTH_TEAM_BASE} FROM core.synthetic_team), 0)
                   + row_number() OVER (ORDER BY min(season_end_year), abbr),
               arg_max(team_name, season_end_year), now()
        FROM abbr WHERE nba_team_id IS NULL AND league = 'ABA' AND abbr NOT IN (SELECT source_key FROM core.synthetic_team)
        GROUP BY abbr''')
    c.execute('''INSERT INTO core.dim_team SELECT team_id, display_name, source_key FROM core.synthetic_team
        WHERE team_id NOT IN (SELECT team_id FROM core.dim_team)''')
    c.execute('''DELETE FROM core.team_source_id WHERE source_system = 'basketball_reference';
        INSERT INTO core.team_source_id
        SELECT DISTINCT ON (a.abbr, a.league, a.season_end_year) coalesce(a.nba_team_id, s.team_id), 'basketball_reference', a.abbr,
               a.league, a.season_end_year, a.team_name
        FROM abbr a LEFT JOIN core.synthetic_team s ON s.source_key = a.abbr
        WHERE coalesce(a.nba_team_id, s.team_id) IN (SELECT team_id FROM core.dim_team)
        ORDER BY a.abbr, a.league, a.season_end_year, a.nba_team_id NULLS LAST''')
    # players: every Basketball-Reference id seen anywhere in raw that does not resolve to an NBA person
    c.execute('''CREATE TABLE IF NOT EXISTS core.synthetic_person (source_key VARCHAR PRIMARY KEY, person_id BIGINT UNIQUE,
        display_name VARCHAR, first_league VARCHAR, issued_at TIMESTAMPTZ)''')
    c.execute('''CREATE OR REPLACE TEMP TABLE bref_career AS
        SELECT player_id AS key, player AS name, try_cast(birth_date AS DATE) AS birth_date, try_cast("from" AS INT) AS first_end,
               try_cast("to" AS INT) AS last_end
        FROM raw_kaggle.sumitrodatta_nba_aba_baa_stats__player_career_info WHERE player_id IS NOT NULL''')
    c.execute('''CREATE OR REPLACE TEMP TABLE bref_people AS
        SELECT bref_player_id AS key, any_value(player_name) AS name, min(league) AS league, max(person_id) AS person_id
        FROM (SELECT bref_player_id, player_name, league, person_id FROM raw.fact_player_award_vote
              UNION ALL SELECT bref_player_id, player_name, league, person_id FROM raw.fact_all_star_selection
              UNION ALL SELECT bref_player_id, player_name, league, person_id FROM raw.fact_player_honor
              UNION ALL SELECT key, name, 'career_table', NULL FROM bref_career
              UNION ALL SELECT bref_id, label, 'transaction', NULL FROM raw.bref_transaction_party WHERE role = 'player')
        WHERE regexp_full_match(bref_player_id, '[a-z]+[0-9]{2}')  -- real player-page ids only
        GROUP BY 1''')
    # Resolve Basketball-Reference ids the raw crosswalk missed: same name and birth date, or same name and first
    # season within one year. Accept only one-to-one matches; anything ambiguous gets its own id.
    c.execute('''CREATE OR REPLACE TEMP TABLE bref_match AS
        WITH open_keys AS (
            SELECT b.key, b.name, k.birth_date,
                   -- players with no career row (e.g. draft picks named only on transaction pages): first transaction season
                   coalesce(k.first_end, (SELECT min(t.season_end_year) FROM raw.bref_transaction_party p
                                          JOIN raw.bref_transaction t USING (event_id) WHERE p.bref_id = b.key)) AS first_end
            FROM bref_people b LEFT JOIN bref_career k USING (key)
            WHERE b.key NOT IN (SELECT source_key FROM core.person_source_id WHERE source_system = 'basketball_reference')
              AND (b.person_id IS NULL OR b.person_id NOT IN (SELECT person_id FROM core.dim_person))),
        cand AS (
            SELECT o.key, d.person_id,
                   CASE WHEN o.birth_date = d.birth_date THEN 'name_birth_date'
                        WHEN d.from_year IS NOT NULL THEN 'name_first_season' ELSE 'name_draft_year' END AS method
            FROM open_keys o JOIN core.dim_person d
              ON lower(strip_accents(regexp_replace(d.display_name, '[^A-Za-z ]', '', 'g')))
               = lower(strip_accents(regexp_replace(o.name, '[^A-Za-z ]', '', 'g')))
             AND d.person_id < 1680000000
             AND (o.birth_date = d.birth_date
                  OR ((o.birth_date IS NULL OR d.birth_date IS NULL) AND abs(d.from_year - (o.first_end - 1)) <= 1)
                  -- draft-only NBA identities (no dates): drafted within six years before the debut season
                  OR (d.birth_date IS NULL AND d.from_year IS NULL AND EXISTS (
                        SELECT 1 FROM core.fact_draft_pick dp WHERE dp.person_id = d.person_id
                        AND dp.draft_year BETWEEN o.first_end - 6 AND o.first_end)))
            WHERE d.person_id NOT IN (SELECT person_id FROM core.person_source_id WHERE source_system = 'basketball_reference'))
        SELECT * FROM cand
        QUALIFY count(*) OVER (PARTITION BY key) = 1 AND count(*) OVER (PARTITION BY person_id) = 1''')
    c.execute('''INSERT INTO core.person_source_id
        SELECT person_id, 'basketball_reference', key, 'bref_player_id', method, 0.95, 'matched', NULL, NULL FROM bref_match''')
    # Jev judgments on same-name pairs the rules could not settle (judge_identities.py). Only 'auto' same_person
    # verdicts that are one-to-one in both directions are linked; everything else keeps its own id.
    jfile = sorted(JUDGMENTS.glob('jev_identity_*.json'))
    if jfile:
        c.execute(f'''CREATE OR REPLACE TABLE audit.jev_identity_judgment AS
            SELECT split_part(id, '|', 1) AS bref_key, try_cast(split_part(id, '|', 2) AS BIGINT) AS person_id,
                   "class" AS verdict, decision, top AS top_probability, probabilities::JSON AS probabilities,
                   '{jfile[-1].name}' AS judgment_file
            FROM read_json('{jfile[-1]}')''')
        c.execute('''INSERT INTO core.person_source_id
            SELECT person_id, 'basketball_reference', bref_key, 'bref_player_id', 'jev_same_person', top_probability, 'matched', NULL, NULL
            FROM audit.jev_identity_judgment j
            WHERE verdict = 'same_person' AND decision = 'auto'
              AND person_id IN (SELECT person_id FROM core.dim_person)
              AND NOT EXISTS (SELECT 1 FROM core.person_source_id p WHERE p.source_system = 'basketball_reference'
                              AND (p.source_key = j.bref_key OR p.person_id = j.person_id))
            QUALIFY count(*) OVER (PARTITION BY bref_key) = 1 AND count(*) OVER (PARTITION BY person_id) = 1''')
    c.execute(f'''INSERT INTO core.synthetic_person
        SELECT key, {SYNTH_PERSON_BASE} + coalesce((SELECT max(person_id) - {SYNTH_PERSON_BASE} FROM core.synthetic_person), 0)
                    + row_number() OVER (ORDER BY key), name, league, now()
        FROM bref_people b
        WHERE (b.person_id IS NULL OR b.person_id NOT IN (SELECT person_id FROM core.dim_person))
          AND key NOT IN (SELECT source_key FROM core.person_source_id WHERE source_system = 'basketball_reference')
          AND key NOT IN (SELECT source_key FROM core.synthetic_person)
          AND name IS NOT NULL''')
    c.execute('''INSERT INTO core.dim_person (person_id, display_name)
        SELECT person_id, display_name FROM core.synthetic_person WHERE person_id NOT IN (SELECT person_id FROM core.dim_person)''')
    c.execute('''INSERT INTO core.person_source_id
        SELECT person_id, 'basketball_reference', source_key, 'bref_player_id', 'synthetic_id_issued', 1.0, 'synthetic', NULL, NULL
        FROM core.synthetic_person s
        WHERE NOT EXISTS (SELECT 1 FROM core.person_source_id p WHERE p.source_system = 'basketball_reference' AND p.source_key = s.source_key)''')
    # Basketball-Reference ids that raw already tied to an NBA person
    c.execute('''INSERT INTO core.person_source_id
        SELECT DISTINCT person_id, 'basketball_reference', key, 'bref_player_id', 'raw_bref_row', 1.0, 'matched', NULL, NULL
        FROM bref_people b WHERE person_id IN (SELECT person_id FROM core.dim_person)
          AND NOT EXISTS (SELECT 1 FROM core.person_source_id p WHERE p.source_system = 'basketball_reference' AND p.source_key = b.key)''')
    print('teams', c.execute('SELECT count(*) FROM core.synthetic_team').fetchone(), 'people', c.execute('SELECT count(*) FROM core.synthetic_person').fetchone())


BREF_PERSON = '''(SELECT min(s.person_id) FROM core.person_source_id s WHERE s.source_system = 'basketball_reference' AND s.source_key = {k})'''
BREF_TEAM = '''(SELECT min(t.team_id) FROM core.team_source_id t WHERE t.source_system = 'basketball_reference'
                AND t.source_key = {k} AND t.season_end_year = {y})'''


def transactions(c) -> None:
    # NBA.com player movement (2015-07 onward) into raw
    files = sorted(MOVEMENT.glob('NBA_Player_Movement_*.json'))
    rows = json.loads(files[-1].read_text())['NBA_Player_Movement']['rows']
    c.execute('''CREATE OR REPLACE TABLE raw.nba_player_movement (transaction_type VARCHAR, transaction_date DATE, description VARCHAR,
        team_id BIGINT, team_slug VARCHAR, player_id BIGINT, player_slug VARCHAR, additional_sort DOUBLE, group_sort VARCHAR, source_file VARCHAR)''')
    c.executemany('INSERT INTO raw.nba_player_movement VALUES (?,?,?,?,?,?,?,?,?,?)',
                  [(r['Transaction_Type'], r['TRANSACTION_DATE'][:10], r['TRANSACTION_DESCRIPTION'], r['TEAM_ID'] and int(r['TEAM_ID']),
                    r['TEAM_SLUG'], r['PLAYER_ID'] and int(r['PLAYER_ID']), r['PLAYER_SLUG'], r['Additional_Sort'], r['GroupSort'], files[-1].name)
                   for r in rows])
    kind = {'Trade': 'trade', 'Signing': 'signed', 'Waive': 'waived', 'AwardOnWaivers': 'claimed_waivers', 'ContractConverted': 'converted_contract'}
    c.execute('CREATE OR REPLACE TEMP TABLE kind_map (k VARCHAR, v VARCHAR)')
    c.executemany('INSERT INTO kind_map VALUES (?,?)', list(kind.items()))
    c.execute('''CREATE OR REPLACE TABLE core.fact_transaction (transaction_id VARCHAR PRIMARY KEY, source VARCHAR NOT NULL,
        league VARCHAR NOT NULL, season_end_year INTEGER, transaction_date DATE NOT NULL, kind VARCHAR NOT NULL, description VARCHAR NOT NULL)''')
    c.execute('''CREATE OR REPLACE TABLE core.bridge_transaction_party (transaction_id VARCHAR NOT NULL REFERENCES core.fact_transaction(transaction_id),
        position INTEGER NOT NULL, role VARCHAR NOT NULL, person_id BIGINT, team_id BIGINT, coach_key VARCHAR, label VARCHAR,
        PRIMARY KEY (transaction_id, position))''')
    c.execute("DELETE FROM audit.domain_held WHERE domain IN ('fact_transaction', 'bridge_transaction_party')")
    # NBA.com rows: one transaction per (group_sort), parties = the player and team on the row
    c.execute('''INSERT INTO core.fact_transaction
        SELECT 'NBA_' || group_sort, 'nba_player_movement', 'NBA',
               CASE WHEN month(transaction_date) >= 7 THEN year(transaction_date) + 1 ELSE year(transaction_date) END,
               transaction_date, coalesce(k.v, lower(transaction_type)), any_value(description)
        FROM raw.nba_player_movement m LEFT JOIN kind_map k ON k.k = m.transaction_type
        GROUP BY ALL''')
    c.execute('''INSERT INTO core.bridge_transaction_party
        SELECT 'NBA_' || group_sort, row_number() OVER (PARTITION BY group_sort ORDER BY player_id, team_id) - 1,
               'player', CASE WHEN player_id IN (SELECT person_id FROM core.dim_person) THEN player_id END,
               CASE WHEN team_id IN (SELECT team_id FROM core.dim_team) THEN team_id END, NULL, player_slug
        FROM raw.nba_player_movement''')
    # Basketball-Reference rows; from 2015-07-01 only those NBA.com does not already list (same date, player, kind)
    c.execute(f'''CREATE OR REPLACE TEMP TABLE bt AS
        SELECT t.*, 'BREF_' || t.event_id AS transaction_id FROM raw.bref_transaction t WHERE kind <> 'season_marker' ''')
    c.execute(f'''CREATE OR REPLACE TEMP TABLE bref_party AS
        SELECT 'BREF_' || p.event_id AS transaction_id, p.position, p.role,
               CASE WHEN p.role = 'player' THEN {BREF_PERSON.format(k='p.bref_id')} END AS person_id,
               CASE WHEN p.role IN ('from_team', 'to_team', 'team') THEN {BREF_TEAM.format(k='p.bref_id', y='bt.season_end_year')} END AS team_id,
               CASE WHEN p.role IN ('coach', 'executive') THEN p.bref_id END AS coach_key, p.label, p.bref_id
        FROM raw.bref_transaction_party p JOIN bt USING (event_id)''')
    c.execute('''CREATE OR REPLACE TEMP TABLE superseded AS
        SELECT DISTINCT b.transaction_id FROM bt b JOIN bref_party bp USING (transaction_id)
        JOIN core.bridge_transaction_party np ON np.person_id = bp.person_id
        JOIN core.fact_transaction n ON n.transaction_id = np.transaction_id AND n.source = 'nba_player_movement'
         AND abs(date_diff('day', n.transaction_date, b.event_date)) <= 1
         AND (n.kind = b.kind OR (n.kind = 'signed' AND b.kind IN ('signed', 'converted_contract')))
        WHERE b.event_date >= DATE '2015-07-01' ''')
    c.execute('''INSERT INTO audit.domain_held SELECT 'fact_transaction', NULL, 'superseded_by_nba_player_movement', to_json(b)
        FROM bt b WHERE transaction_id IN (SELECT transaction_id FROM superseded)''')
    c.execute('''INSERT INTO core.fact_transaction SELECT transaction_id, 'basketball_reference', league, season_end_year, event_date, kind, description
        FROM bt WHERE transaction_id NOT IN (SELECT transaction_id FROM superseded)''')
    c.execute('''INSERT INTO core.bridge_transaction_party SELECT transaction_id, position, role, person_id, team_id, coach_key, label
        FROM bref_party WHERE transaction_id IN (SELECT transaction_id FROM core.fact_transaction)''')
    # a party that names a player/team we could not resolve is kept with NULL id and listed as held evidence
    c.execute('''INSERT INTO audit.domain_held SELECT 'bridge_transaction_party', position, 'unresolved_' || role, to_json(p)
        FROM bref_party p WHERE transaction_id IN (SELECT transaction_id FROM core.fact_transaction)
          AND ((role = 'player' AND person_id IS NULL) OR (role IN ('from_team', 'to_team', 'team') AND team_id IS NULL))''')
    n_src = c.execute("SELECT count(*) FROM raw.bref_transaction WHERE kind <> 'season_marker'").fetchone()[0] + \
        c.execute('SELECT count(DISTINCT group_sort) FROM raw.nba_player_movement').fetchone()[0]
    disposition(c, 'fact_transaction', 'raw.bref_transaction + raw.nba_player_movement', n_src)


def aba(c) -> None:
    """ABA games (1967-68 to 1975-76) from Basketball-Reference schedules: fixtures, finals, standings."""
    c.execute(f'''CREATE OR REPLACE TEMP TABLE sched AS
        SELECT *, strptime(date_text, '%a, %b %d, %Y')::DATE AS game_date,
               regexp_extract(visitor_href, '/teams/([A-Z0-9]+)/', 1) AS v_abbr,
               regexp_extract(home_href, '/teams/([A-Z0-9]+)/', 1) AS h_abbr,
               regexp_extract(box_href, '/boxscores/([0-9A-Z]+)\\.html', 1) AS bref_game_key
        FROM raw.bref_schedule_game
        WHERE league = 'ABA' AND visitor_pts <> 'PTS' AND visitor_href IS NOT NULL''')
    # playoffs start at the first game Basketball-Reference places after its "Playoffs" separator
    c.execute('''CREATE OR REPLACE TEMP TABLE sched2 AS
        SELECT s.*, CASE WHEN s.remarks ILIKE 'tiebreaker%' THEN 'Tiebreaker'   -- one-game division tiebreakers: not regular season
                         WHEN s.game_date >= p.first_playoff THEN 'Playoffs' ELSE 'Regular' END AS season_type
        FROM sched s LEFT JOIN (SELECT season_end_year, min(strptime(date_text, '%a, %b %d, %Y')::DATE) AS first_playoff
                                FROM raw.bref_schedule_game WHERE league = 'ABA' AND row_json->>'_section' = 'Playoffs'
                                GROUP BY 1) p USING (season_end_year)''')
    c.execute('''CREATE TABLE IF NOT EXISTS core.aba_game_key (bref_game_key VARCHAR PRIMARY KEY, game_id VARCHAR UNIQUE NOT NULL,
        issued_at TIMESTAMPTZ)''')
    # permanent ids: 08 + type digit (2 regular, 4 postseason incl. tiebreakers) + 2-digit season start + 5-digit sequence
    c.execute('''INSERT INTO core.aba_game_key
        SELECT bref_game_key, '08' || CASE season_type WHEN 'Regular' THEN '2' ELSE '4' END
               || lpad(((season_end_year - 1) % 100)::VARCHAR, 2, '0')
               || lpad((coalesce((SELECT max(try_cast(right(k.game_id, 5) AS INT)) FROM core.aba_game_key k
                        WHERE left(k.game_id, 5) = '08' || CASE season_type WHEN 'Regular' THEN '2' ELSE '4' END
                              || lpad(((season_end_year - 1) % 100)::VARCHAR, 2, '0')), 0)
                        + row_number() OVER (PARTITION BY season_end_year, season_type = 'Regular' ORDER BY game_date, bref_game_key))::VARCHAR, 5, '0'),
               now()
        FROM sched2 WHERE bref_game_key <> '' AND bref_game_key NOT IN (SELECT bref_game_key FROM core.aba_game_key)''')
    team = BREF_TEAM.format(k='{a}', y='s.season_end_year')
    c.execute(f'''CREATE OR REPLACE TEMP TABLE aba_cand AS
        SELECT k.game_id, s.bref_game_key, s.game_date, (s.season_end_year - 1)::SMALLINT AS season_start,
               (s.season_end_year - 1)::VARCHAR || '-' || lpad((s.season_end_year % 100)::VARCHAR, 2, '0') AS season_year,
               'ABA' AS league, s.season_type,
               {team.format(a='s.h_abbr')} AS home_team_id, {team.format(a='s.v_abbr')} AS away_team_id,
               try_cast(s.home_pts AS INTEGER) AS home_score, try_cast(s.visitor_pts AS INTEGER) AS away_score,
               nullif(s.overtimes, '') AS overtimes, s.row_json->>'arena_name' AS arena_name,
               try_cast(replace(s.row_json->>'attendance', ',', '') AS INTEGER) AS attendance,
               nullif(s.remarks, '') AS remarks, s.source_page
        FROM sched2 s LEFT JOIN core.aba_game_key k USING (bref_game_key)''')
    c.execute("DELETE FROM audit.domain_held WHERE domain = 'aba_game'")
    c.execute('''INSERT INTO audit.domain_held SELECT 'aba_game', NULL,
            CASE WHEN game_id IS NULL THEN 'no_box_score_key' WHEN home_team_id IS NULL OR away_team_id IS NULL THEN 'unresolved_team'
                 WHEN home_score IS NULL OR away_score IS NULL THEN 'missing_score' WHEN home_score = away_score THEN 'tied_score' END,
            to_json(a)
        FROM aba_cand a WHERE game_id IS NULL OR home_team_id IS NULL OR away_team_id IS NULL OR home_score IS NULL
           OR away_score IS NULL OR home_score = away_score''')
    c.execute('''CREATE OR REPLACE TABLE core.aba_game AS
        SELECT game_id, bref_game_key, game_date, season_start, season_year, league, season_type, home_team_id, away_team_id,
               arena_name, attendance, remarks
        FROM aba_cand WHERE game_id IS NOT NULL AND home_team_id IS NOT NULL AND away_team_id IS NOT NULL
          AND home_score IS NOT NULL AND away_score IS NOT NULL AND home_score <> away_score ORDER BY game_id''')
    c.execute('ALTER TABLE core.aba_game ADD PRIMARY KEY (game_id)')
    c.execute('''CREATE OR REPLACE TABLE core.aba_game_result AS
        SELECT a.game_id, a.home_score, a.away_score, CASE WHEN a.home_score > a.away_score THEN a.home_team_id ELSE a.away_team_id END AS winner_team_id,
               a.overtimes IS NOT NULL AS is_overtime, a.overtimes
        FROM aba_cand a WHERE a.game_id IN (SELECT game_id FROM core.aba_game) ORDER BY 1''')
    c.execute('ALTER TABLE core.aba_game_result ADD PRIMARY KEY (game_id)')
    c.execute('''CREATE OR REPLACE TABLE core.aba_regular_standings AS
        WITH side AS (SELECT g.season_year, g.home_team_id AS team_id, r.home_score AS pf, r.away_score AS pa FROM core.aba_game g JOIN core.aba_game_result r USING (game_id) WHERE g.season_type = 'Regular'
                      UNION ALL SELECT g.season_year, g.away_team_id, r.away_score, r.home_score FROM core.aba_game g JOIN core.aba_game_result r USING (game_id) WHERE g.season_type = 'Regular')
        SELECT season_year, team_id, count(*)::INT AS games, count(*) FILTER (WHERE pf > pa)::INT AS wins, count(*) FILTER (WHERE pf < pa)::INT AS losses,
               sum(pf)::INT AS points_for, sum(pa)::INT AS points_against
        FROM side GROUP BY ALL ORDER BY ALL''')
    # cross-check standings against Basketball-Reference's season summaries (Kaggle copy, which includes the ABA)
    c.execute(f'''CREATE OR REPLACE TABLE audit.aba_standings_check AS
        WITH b AS (SELECT try_cast(x.season AS INT) AS season_end_year, x.abbreviation,
                          {BREF_TEAM.format(k='x.abbreviation', y='try_cast(x.season AS INT)')} AS team_id,
                          try_cast(x.w AS INT) AS w, try_cast(x.l AS INT) AS l
                   FROM raw_kaggle.sumitrodatta_nba_aba_baa_stats__team_summaries x WHERE x.lg = 'ABA' AND x.abbreviation <> 'NA')
        SELECT coalesce(s.season_year, (b.season_end_year - 1)::VARCHAR || '-' || lpad((b.season_end_year % 100)::VARCHAR, 2, '0')) AS season_year,
               coalesce(s.team_id, b.team_id) AS team_id, b.abbreviation, s.games, s.wins, s.losses, b.w AS bref_wins, b.l AS bref_losses,
               coalesce(s.wins = b.w AND s.losses = b.l, false) AS agrees
        FROM core.aba_regular_standings s
        FULL JOIN b ON b.team_id = s.team_id AND (b.season_end_year - 1)::VARCHAR || '-' || lpad((b.season_end_year % 100)::VARCHAR, 2, '0') = s.season_year''')
    print('aba standings agree', c.execute('SELECT count(*) FILTER (WHERE agrees), count(*) FROM audit.aba_standings_check').fetchone())
    disposition(c, 'aba_game', 'raw.bref_schedule_game (ABA)', c.execute('SELECT count(*) FROM aba_cand').fetchone()[0])


def salaries(c) -> None:
    """Player salaries 1984-85 to 2025-26 from Kaggle mirrors of Basketball-Reference (to 2018) and HoopsHype (2016-).

    Basketball-Reference rows carry its player id (crosswalked); HoopsHype rows are resolved by exact name plus
    having played for that team that season in core box scores. Where both cover a player-season and differ by more
    than 1%, the row is held as disputed (same-rank sources) and both values stay in audit.
    """
    norm = "lower(strip_accents(regexp_replace({x}, '[^A-Za-z ]', '', 'g')))"
    c.execute(f'''CREATE OR REPLACE TEMP TABLE team_by_name AS
        SELECT DISTINCT lower(full_name) AS name, team_id, valid_from, coalesce(valid_to, 9999) AS valid_to FROM core.dim_team_version''')
    c.execute(f'''CREATE OR REPLACE TEMP TABLE sal_bref AS
        SELECT try_cast(s.season_end AS INT) AS season_end_year, s.player_id AS bref_key, s.team AS team_name,
               try_cast(s.salary AS BIGINT) AS salary_usd,
               {BREF_PERSON.format(k='s.player_id')} AS person_id,
               (SELECT min(t.team_id) FROM team_by_name t WHERE t.name = lower(s.team)
                  AND try_cast(s.season_start AS INT) BETWEEN t.valid_from AND t.valid_to) AS team_id
        FROM raw_kaggle.ulrikthygepedersen_nba_player_salaries__salaries_1985to2018 s WHERE s.league = 'NBA' ''')
    c.execute(f'''CREATE OR REPLACE TEMP TABLE season_roster AS
        SELECT DISTINCT g.season_start + 1 AS season_end_year, b.team_id, b.person_id, {norm.format(x='p.display_name')} AS nname
        FROM core.fact_player_game_stats b JOIN core.dim_game g USING (game_id) JOIN core.dim_person p USING (person_id)''')
    c.execute(f'''CREATE OR REPLACE TEMP TABLE sal_hh AS
        SELECT try_cast(s.season_year AS INT) + 1 AS season_end_year, s.player AS player_name, s.team AS team_name,
               try_cast(s.salary AS BIGINT) AS salary_usd, t.team_id,
               (SELECT CASE WHEN count(*) = 1 THEN min(r.person_id) END FROM season_roster r
                WHERE r.season_end_year = try_cast(s.season_year AS INT) + 1 AND r.team_id = t.team_id
                  AND r.nname = {norm.format(x='s.player')}) AS person_id
        FROM raw_kaggle.mrbridge_nba_salary_cap_contracts_2016_2026__player_salaries s
        LEFT JOIN team_by_name t ON t.name = lower(s.team) AND try_cast(s.season_year AS INT) BETWEEN t.valid_from AND t.valid_to''')
    c.execute('''CREATE OR REPLACE TEMP TABLE sal_all AS
        SELECT 'basketball_reference_via_kaggle' AS source, season_end_year, person_id, team_id, salary_usd, bref_key AS source_key FROM sal_bref
        UNION ALL SELECT 'hoopshype_via_kaggle', season_end_year, person_id, team_id, salary_usd, player_name FROM sal_hh''')
    c.execute("DELETE FROM audit.domain_held WHERE domain = 'fact_player_salary'")
    c.execute('''INSERT INTO audit.domain_held SELECT 'fact_player_salary', NULL,
            CASE WHEN person_id IS NULL THEN 'unresolved_person' WHEN team_id IS NULL THEN 'unresolved_team' ELSE 'missing_salary' END, to_json(a)
        FROM sal_all a WHERE person_id IS NULL OR team_id IS NULL OR salary_usd IS NULL''')
    # same player-team-season in both sources: agree within 1% -> keep one (Basketball-Reference first); else disputed
    c.execute('''CREATE OR REPLACE TEMP TABLE sal_ok AS
        SELECT *, count(DISTINCT source) OVER w AS n_sources,
               (max(salary_usd) OVER w - min(salary_usd) OVER w) > 0.01 * max(salary_usd) OVER w AS disputed
        FROM sal_all WHERE person_id IS NOT NULL AND team_id IS NOT NULL AND salary_usd IS NOT NULL
        WINDOW w AS (PARTITION BY season_end_year, person_id, team_id)''')
    c.execute('''INSERT INTO audit.domain_held SELECT 'fact_player_salary', NULL, 'disputed_between_sources', to_json(a) FROM sal_ok a WHERE disputed''')
    c.execute('''CREATE OR REPLACE TABLE core.fact_player_salary AS
        SELECT season_end_year, person_id, team_id, salary_usd, source, n_sources AS sources_agreeing
        FROM sal_ok WHERE NOT disputed
        QUALIFY row_number() OVER (PARTITION BY season_end_year, person_id, team_id ORDER BY source) = 1
        ORDER BY ALL''')
    c.execute('ALTER TABLE core.fact_player_salary ADD PRIMARY KEY (season_end_year, person_id, team_id)')
    disposition(c, 'fact_player_salary', 'Kaggle: ulrikthygepedersen (BRef 1985-2018) + mrbridge (HoopsHype 2016-2026)',
                c.execute('SELECT count(*) FROM sal_all').fetchone()[0])


BOX_STATS = ('fg', 'fga', 'fg3', 'fg3a', 'ft', 'fta', 'orb', 'drb', 'trb', 'ast', 'stl', 'blk', 'tov', 'pf', 'pts')
KAGGLE_COL = {'fg': 'FG', 'fga': 'FGA', 'fg3': '3P', 'fg3a': '3PA', 'ft': 'FT', 'fta': 'FTA', 'orb': 'ORB', 'drb': 'DRB',
              'trb': 'TRB', 'ast': 'AST', 'stl': 'STL', 'blk': 'BLK', 'tov': 'TOV', 'pf': 'PF', 'pts': 'PTS'}


def load_kaggle_aba_box(c) -> bool:
    """raw_kaggle.gonzalo_aba_box: the nine ABA_*_basic tables of Kaggle gonzalogigena/nba-all-time-stats (loaded by
    load_kaggle.py), stacked. Player rows scraped from Basketball-Reference box pages, keyed by the same game and
    player ids; no team-total rows."""
    tabs = [r[0] for r in c.execute("""SELECT table_name FROM duckdb_tables() WHERE schema_name = 'raw_kaggle'
        AND table_name LIKE 'gonzalogigena_nba_all_time_stats__boxscores_by_year_aba_%_basic' ORDER BY 1""").fetchall()]
    if not tabs:
        return False
    union = ' UNION ALL BY NAME '.join(f"SELECT *, '{t}' AS source_table FROM raw_kaggle.{t}" for t in tabs)
    c.execute(f'''CREATE OR REPLACE TABLE raw_kaggle.gonzalo_aba_box AS
        SELECT *, row_number() OVER (PARTITION BY "Game Reference", "Team") AS row_index FROM ({union})''')
    return True


BULK_COL = {  # warehouse field -> (raw.boxtop_aba_stat column, raw.nbastats_aba_gamelog column)
    'fg': ('fgm', 'fgm'), 'fga': ('fga', 'fga'), 'fg3': ('fg3m', 'fg3m'), 'fg3a': ('fg3a', 'fg3a'), 'ft': ('ftm', 'ftm'),
    'fta': ('fta', 'fta'), 'orb': ('oreb', 'oreb'), 'drb': (None, 'dreb'), 'trb': ('reb', 'reb'), 'ast': ('ast', 'ast'),
    'stl': ('stl', 'stl'), 'blk': ('blk', 'blk'), 'tov': ('tov', 'tov'), 'pf': ('pf', 'pf'), 'pts': ('pts', 'pts')}


def aba_bulk_fill(c) -> None:
    """Fill blank fields of ABA player box lines (abox_src) from the two bulk files, below Basketball-Reference in rank.

    Boxtop rows match on date + Basketball-Reference player id; nbastats rows on date + last name + points (first
    initial breaks ties), one-to-one only. A blank field takes the Boxtop/nbastats value; when both have a value and
    they differ (same rank), the field stays blank and the pair goes to audit.aba_box_field_conflict. Values that
    disagree with a Basketball-Reference value are logged there too and not used. Team totals of games without a
    box page are re-summed from the filled player rows.
    """
    have = {r[0] for r in c.execute("""SELECT table_name FROM duckdb_tables() WHERE schema_name = 'raw'
        AND table_name IN ('boxtop_aba_stat', 'nbastats_aba_gamelog')""").fetchall()}
    if len(have) < 2:
        return
    norm = "lower(regexp_replace(strip_accents({x}), '[^A-Za-z ]', '', 'g'))"
    c.execute('''CREATE OR REPLACE TEMP TABLE ab_rows AS
        SELECT r.*, strptime(substr(r.bref_game_key, 1, 8), '%Y%m%d')::DATE AS game_date, coalesce(k."Player Name", r.label) AS player_name
        FROM abox_src r LEFT JOIN (SELECT DISTINCT "Game Reference", "Player Reference", "Player Name" FROM raw_kaggle.gonzalo_aba_box) k
          ON k."Game Reference" = r.bref_game_key AND k."Player Reference" = r.bref_player_id
        WHERE r.label <> 'Team Totals' AND r.bref_player_id IS NOT NULL''')
    c.execute('''CREATE OR REPLACE TEMP TABLE bt_match AS
        SELECT a.bref_game_key, a.bref_player_id, any_value(b) AS b FROM ab_rows a
        JOIN raw.boxtop_aba_stat b ON b.game_date = a.game_date AND b.bref_player_id = a.bref_player_id
        GROUP BY 1, 2 HAVING count(*) = 1''')
    c.execute(f'''CREATE OR REPLACE TEMP TABLE ns_cand AS
        WITH n AS (SELECT *, row_number() OVER () AS rid FROM raw.nbastats_aba_gamelog)
        SELECT n.rid, a.bref_game_key, a.bref_player_id, n AS ns,
               {norm.format(x='a.player_name')} LIKE {norm.format(x='n.first_name')}[1] || '%' AS initial_ok
        FROM n JOIN ab_rows a ON a.game_date = n.game_date AND a.pts IS NOT DISTINCT FROM n.pts
         AND {norm.format(x='a.player_name')} LIKE '%' || {norm.format(x='n.last_name')}''')
    c.execute('''CREATE OR REPLACE TEMP TABLE ns_match AS
        WITH one AS (SELECT * FROM ns_cand QUALIFY count(*) OVER (PARTITION BY rid) = 1
                     UNION ALL
                     SELECT * FROM ns_cand WHERE initial_ok QUALIFY count(*) OVER (PARTITION BY rid) > 1
                       AND count(*) FILTER (WHERE initial_ok) OVER (PARTITION BY rid) = 1)
        SELECT bref_game_key, bref_player_id, any_value(ns) AS ns FROM one GROUP BY 1, 2 HAVING count(*) = 1''')
    fill, conflict = [], []
    for f, (bc, nc) in BULK_COL.items():
        b = f"try_cast(m1.b.{bc} AS INTEGER)" if bc else 'NULL::INTEGER'
        n = f"m2.ns.{nc}"
        fill.append(f'''coalesce(r.{f}, CASE WHEN {b} IS NOT NULL AND {n} IS NOT NULL AND {b} <> {n} THEN NULL
                                             ELSE coalesce({b}, {n}) END) AS {f}''')
        conflict.append(f'''SELECT r.bref_game_key, r.bref_player_id, '{f}' AS field, r.box_source, r.{f} AS bref_value,
                                   {b} AS boxtop_value, {n} AS nbastats_value
                            FROM abox_src r LEFT JOIN bt_match m1 USING (bref_game_key, bref_player_id)
                            LEFT JOIN ns_match m2 USING (bref_game_key, bref_player_id)
                            WHERE r.label <> 'Team Totals' AND (
                                  ({b} IS NOT NULL AND {n} IS NOT NULL AND {b} <> {n})
                               OR (r.{f} IS NOT NULL AND {b} IS NOT NULL AND {b} <> r.{f})
                               OR (r.{f} IS NOT NULL AND {n} IS NOT NULL AND {n} <> r.{f}))''')
    c.execute('CREATE OR REPLACE TABLE audit.aba_box_field_conflict AS ' + ' UNION ALL '.join(conflict))
    keep = 'box_source, bref_game_key, team_abbr, row_index, bref_player_id, label, minutes_text, dnp_reason'
    before = ', '.join(f'count(r.{f}) AS {f}' for f in BOX_STATS)
    c.execute(f'''CREATE OR REPLACE TEMP TABLE abox_filled AS
        SELECT r.box_source, r.bref_game_key, r.team_abbr, r.row_index, r.bref_player_id, r.label, r.minutes_text, r.dnp_reason,
               {', '.join(fill)}
        FROM abox_src r LEFT JOIN bt_match m1 USING (bref_game_key, bref_player_id) LEFT JOIN ns_match m2 USING (bref_game_key, bref_player_id)
        WHERE r.label <> 'Team Totals' ''')
    tot = ', '.join(f'sum({f})::INTEGER' for f in BOX_STATS)
    c.execute(f'''CREATE OR REPLACE TEMP TABLE abox_src2 AS
        SELECT {keep}, {', '.join(BOX_STATS)} FROM abox_filled
        UNION ALL SELECT {keep}, {', '.join(BOX_STATS)} FROM abox_src WHERE label = 'Team Totals' AND box_source <> 'player_sum'
        UNION ALL SELECT 'player_sum', bref_game_key, team_abbr, 9999, NULL, 'Team Totals', NULL, NULL, {tot}
            FROM abox_filled WHERE bref_game_key NOT IN (SELECT bref_game_key FROM abox_src WHERE label = 'Team Totals' AND box_source <> 'player_sum')
            GROUP BY bref_game_key, team_abbr''')
    b0 = c.execute(f"SELECT {before} FROM abox_src r WHERE label <> 'Team Totals'").fetchall()[0]
    b1 = c.execute(f"SELECT {before} FROM abox_filled r").fetchall()[0]
    print('bulk matches: boxtop', c.execute('SELECT count(*) FROM bt_match').fetchone()[0], 'nbastats', c.execute('SELECT count(*) FROM ns_match').fetchone()[0])
    print('filled fields:', {f: (x, y) for f, x, y in zip(BOX_STATS, b0, b1) if y != x})
    print('field conflicts:', c.execute('SELECT field, count(*) FROM audit.aba_box_field_conflict GROUP BY 1 ORDER BY 2 DESC').fetchall())
    c.execute('CREATE OR REPLACE TEMP TABLE abox_src AS SELECT * FROM abox_src2')


def aba_boxes(c) -> None:
    """ABA team and player box scores.

    Source per game, best first: the Basketball-Reference box page (raw.bref_box_row); else per player the
    Basketball-Reference game-log line (raw.bref_gamelog_row), else the Kaggle scrape of the box pages
    (raw_kaggle.gonzalo_aba_box), with team totals the sum of the player rows (box_source 'player_sum'). Games present in both are compared field by field in audit.aba_box_source_compare.
    """
    has_page = c.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name='raw' AND table_name='bref_box_row'").fetchone()[0] > 0
    has_kaggle = load_kaggle_aba_box(c)
    if not (has_page or has_kaggle):
        print('aba_boxes: no box pages loaded yet'); return
    n = "try_cast(r.row_json->>'{f}' AS INTEGER)"
    page_stats = ', '.join(f"{n.format(f=f)} AS {f}" for f in BOX_STATS)
    kg_stats = ', '.join(f'try_cast(try_cast(r."{KAGGLE_COL[f]}" AS DOUBLE) AS INTEGER) AS {f}' for f in BOX_STATS)
    parts = []
    if has_page:
        parts.append(f'''SELECT 'bref_page' AS box_source, r.bref_game_key, r.team_abbr, r.row_index, r.bref_player_id, r.label,
                   r.row_json->>'mp' AS minutes_text, r.row_json->>'reason' AS dnp_reason, {page_stats}
            FROM raw.bref_box_row r WHERE r.bref_game_key IN (SELECT bref_game_key FROM core.aba_game_key)''')
    if has_kaggle:
        page_games = "SELECT DISTINCT bref_game_key FROM raw.bref_box_row" if has_page else "SELECT NULL"
        parts.append(f'''SELECT 'kaggle_gonzalo' AS box_source, r."Game Reference" AS bref_game_key, r."Team" AS team_abbr,
                   r.row_index::INTEGER AS row_index, r."Player Reference" AS bref_player_id, r."Player Name" AS label,
                   r."MP" AS minutes_text, r."Reason" AS dnp_reason, {kg_stats}
            FROM raw_kaggle.gonzalo_aba_box r''')
        c.execute(f'CREATE OR REPLACE TEMP TABLE kbox AS {parts[-1]}')
        has_log = c.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name='raw' AND table_name='bref_gamelog_row'").fetchone()[0] > 0
        # games without a box page: the player's game-log line where we have one (complete fields), else the Kaggle row
        log = (f'''SELECT 'bref_gamelog' AS box_source, r.bref_game_key, r.team_abbr, 0 AS row_index, r.bref_player_id,
                   r.bref_player_id AS label, r.row_json->>'mp' AS minutes_text, r.row_json->>'reason' AS dnp_reason, {page_stats}
            FROM raw.bref_gamelog_row r WHERE r.bref_game_key IN (SELECT bref_game_key FROM kbox)''' if has_log else
               "SELECT * FROM kbox WHERE false")
        c.execute(f'''CREATE OR REPLACE TEMP TABLE nonpage AS
            WITH g AS ({log})
            SELECT * FROM g WHERE bref_game_key NOT IN ({page_games})
            UNION ALL SELECT * FROM kbox k WHERE bref_game_key NOT IN ({page_games})
              AND NOT EXISTS (SELECT 1 FROM g WHERE g.bref_game_key = k.bref_game_key AND g.bref_player_id = k.bref_player_id)''')
        # team totals for these games: sum of the player rows
        tot = ', '.join(f'sum({f})::INTEGER' for f in BOX_STATS)
        parts[-1] = f'''SELECT * FROM nonpage
            UNION ALL SELECT 'player_sum', bref_game_key, team_abbr, 9999, NULL, 'Team Totals', NULL, NULL, {tot}
            FROM nonpage GROUP BY bref_game_key, team_abbr'''
    c.execute(f'CREATE OR REPLACE TEMP TABLE abox_src AS {" UNION ALL ".join(parts)}')
    aba_bulk_fill(c)
    c.execute(f'''CREATE OR REPLACE TEMP TABLE abox AS
        SELECT k.game_id, g.season_start + 1 AS season_end_year, r.box_source, r.team_abbr, r.bref_player_id, r.label,
               {BREF_TEAM.format(k='r.team_abbr', y='g.season_start + 1')} AS team_id,
               {BREF_PERSON.format(k='r.bref_player_id')} AS person_id,
               r.minutes_text, r.dnp_reason, {', '.join(BOX_STATS)}
        FROM abox_src r JOIN core.aba_game_key k ON k.bref_game_key = r.bref_game_key
        JOIN core.aba_game g ON g.game_id = k.game_id''')
    if has_page and has_kaggle:
        diff = ' OR '.join(f'p.{f} IS DISTINCT FROM k.{f}' for f in BOX_STATS)
        c.execute(f'''CREATE OR REPLACE TABLE audit.aba_box_source_compare AS
            WITH p AS (SELECT bref_game_key, team_abbr, bref_player_id, {page_stats.replace('r.row_json', 'row_json')}
                       FROM raw.bref_box_row r WHERE bref_player_id IS NOT NULL
                         AND bref_game_key IN (SELECT bref_game_key FROM core.aba_game_key)
                         AND bref_game_key IN (SELECT bref_game_key FROM kbox)),
                 k AS (SELECT * FROM kbox WHERE bref_game_key IN (SELECT bref_game_key FROM p))
            SELECT coalesce(p.bref_game_key, k.bref_game_key) AS bref_game_key, coalesce(p.team_abbr, k.team_abbr) AS team_abbr,
                   coalesce(p.bref_player_id, k.bref_player_id) AS bref_player_id,
                   CASE WHEN p.bref_player_id IS NULL THEN 'kaggle_only' WHEN k.bref_player_id IS NULL THEN 'page_only'
                        WHEN {diff} THEN 'values_differ' ELSE 'identical' END AS outcome,
                   to_json(p) AS page_row, to_json(k) AS kaggle_row
            FROM p FULL JOIN k ON p.bref_game_key = k.bref_game_key AND p.team_abbr = k.team_abbr AND p.bref_player_id = k.bref_player_id''')
        print('aba box source compare:', c.execute('SELECT outcome, count(*) FROM audit.aba_box_source_compare GROUP BY 1 ORDER BY 1').fetchall())
    c.execute('''CREATE OR REPLACE TABLE core.aba_team_game_stats AS
        SELECT game_id, team_id, fg AS fgm, fga, fg3 AS fg3m, fg3a, ft AS ftm, fta, orb AS oreb, drb AS dreb, trb AS reb, ast, stl, blk,
               tov, pf, pts, box_source FROM abox WHERE label = 'Team Totals' AND team_id IS NOT NULL''')
    c.execute('''CREATE OR REPLACE TABLE core.aba_player_game_stats AS
        SELECT game_id, person_id, team_id, CASE WHEN dnp_reason IS NOT NULL THEN 'did_not_play' ELSE 'played' END AS participation_status,
               minutes_text, fg AS fgm, fga, fg3 AS fg3m, fg3a, ft AS ftm, fta, orb AS oreb, drb AS dreb, trb AS reb, ast, stl, blk, tov, pf, pts,
               box_source
        FROM abox WHERE label <> 'Team Totals' AND label <> 'Reserves' AND label <> 'Starters' AND person_id IS NOT NULL AND team_id IS NOT NULL''')
    c.execute("DELETE FROM audit.domain_held WHERE domain = 'aba_player_game_stats'")
    c.execute('''INSERT INTO audit.domain_held SELECT 'aba_player_game_stats', NULL,
            CASE WHEN team_id IS NULL THEN 'unresolved_team' ELSE 'unresolved_person' END, to_json(a)
        FROM abox a WHERE label NOT IN ('Team Totals', 'Reserves', 'Starters') AND (person_id IS NULL OR team_id IS NULL)''')
    # team totals must equal the official final, and player points must sum to team points
    c.execute('''CREATE OR REPLACE TABLE audit.aba_box_check AS
        SELECT t.game_id, t.team_id, t.pts AS team_pts,
               CASE WHEN t.team_id = g.home_team_id THEN r.home_score ELSE r.away_score END AS final_pts,
               (SELECT sum(p.pts) FROM core.aba_player_game_stats p WHERE p.game_id = t.game_id AND p.team_id = t.team_id) AS player_pts
        FROM core.aba_team_game_stats t JOIN core.aba_game g USING (game_id) JOIN core.aba_game_result r USING (game_id)''')
    print('aba boxes: teams', c.execute('SELECT count(*), count(*) FILTER (WHERE team_pts = final_pts), count(*) FILTER (WHERE player_pts = team_pts) FROM audit.aba_box_check').fetchone())
    disposition(c, 'aba_player_game_stats', 'raw.bref_box_row + raw_kaggle.gonzalo_aba_box (ABA)',
                c.execute("SELECT count(*) FROM abox WHERE label NOT IN ('Team Totals', 'Reserves', 'Starters')").fetchone()[0])


def rosters(c) -> None:
    """Dated roster spells: who was on which team between which dates.

    A spell is a run of consecutive games for one team (from box scores, NBA/BAA and ABA). Its start moves back to
    the latest transaction bringing the player to that team after his previous game, and its end moves forward to
    the earliest transaction taking him away (traded from / waived / released) before his next spell; otherwise
    the first and last game dates stand and date_basis says so.
    """
    has_aba = c.execute("SELECT count(*) FROM duckdb_tables() WHERE schema_name='core' AND table_name='aba_player_game_stats'").fetchone()[0]
    c.execute('''CREATE OR REPLACE TEMP TABLE appear AS
        SELECT p.person_id, p.team_id, g.game_date FROM core.fact_player_game_stats p JOIN core.dim_game g USING (game_id)
        WHERE p.participation_status = 'played' AND p.person_id IS NOT NULL
          AND g.season_type IN ('Regular', 'Playoffs', 'PlayIn', 'Preseason', 'Cup')   -- club games only (no All-Star squads)
          AND p.team_id IN (SELECT team_id FROM core.dim_team_version WHERE team_kind = 'nba_franchise')''' + ('''
        UNION ALL
        SELECT p.person_id, p.team_id, g.game_date FROM core.aba_player_game_stats p JOIN core.aba_game g USING (game_id)
        WHERE p.participation_status = 'played' AND g.season_type <> 'AllStar' ''' if has_aba else ''))
    c.execute('''CREATE OR REPLACE TEMP TABLE spell AS
        WITH o AS (SELECT DISTINCT person_id, team_id, game_date FROM appear),
             s AS (SELECT *, CASE WHEN team_id IS DISTINCT FROM lag(team_id) OVER w THEN 1 ELSE 0 END AS brk
                   FROM o WINDOW w AS (PARTITION BY person_id ORDER BY game_date, team_id)),
             n AS (SELECT *, sum(brk) OVER (PARTITION BY person_id ORDER BY game_date, team_id) AS spell_no FROM s)
        SELECT person_id, spell_no, any_value(team_id) AS team_id, min(game_date) AS first_game, max(game_date) AS last_game,
               count(*) AS games FROM n GROUP BY person_id, spell_no''')
    c.execute('''CREATE OR REPLACE TEMP TABLE moves AS
        SELECT t.transaction_date, t.kind, t.transaction_id, p.person_id, tm.team_id, tm.role
        FROM core.fact_transaction t JOIN core.bridge_transaction_party p USING (transaction_id)
        JOIN core.bridge_transaction_party tm ON tm.transaction_id = t.transaction_id AND tm.team_id IS NOT NULL
        WHERE p.role = 'player' AND p.person_id IS NOT NULL
        UNION ALL  -- NBA.com movement rows name the team on the player's own row (direction not stated)
        SELECT t.transaction_date, t.kind, t.transaction_id, p.person_id, p.team_id, 'team'
        FROM core.fact_transaction t JOIN core.bridge_transaction_party p USING (transaction_id)
        WHERE p.role = 'player' AND p.person_id IS NOT NULL AND p.team_id IS NOT NULL''')
    c.execute('''CREATE OR REPLACE TABLE core.fact_roster_spell AS
        WITH b AS (SELECT s.*, lag(last_game) OVER (PARTITION BY person_id ORDER BY spell_no) AS prev_last,
                          lead(first_game) OVER (PARTITION BY person_id ORDER BY spell_no) AS next_first FROM spell s)
        SELECT b.person_id, b.team_id, b.spell_no,
               coalesce((SELECT max(m.transaction_date) FROM moves m WHERE m.person_id = b.person_id AND m.team_id = b.team_id
                          AND m.role IN ('to_team', 'team') AND m.kind IN ('trade', 'signed', 'claimed_waivers', 'sold', 'drafted', 'converted_contract')
                          AND m.transaction_date <= b.first_game AND m.transaction_date > coalesce(b.prev_last, DATE '1900-01-01')),
                        b.first_game) AS start_date,
               coalesce((SELECT min(m.transaction_date) FROM moves m WHERE m.person_id = b.person_id AND m.team_id = b.team_id
                          AND ((m.role = 'from_team' AND m.kind IN ('trade', 'sold')) OR m.kind IN ('waived', 'released'))
                          AND m.transaction_date >= b.last_game AND m.transaction_date < coalesce(b.next_first, DATE '2999-01-01')),
                        b.last_game) AS end_date,
               b.first_game, b.last_game, b.games
        FROM b ORDER BY person_id, spell_no''')
    c.execute('''ALTER TABLE core.fact_roster_spell ADD COLUMN date_basis VARCHAR;
        UPDATE core.fact_roster_spell SET date_basis = CASE
            WHEN start_date < first_game AND end_date > last_game THEN 'transactions_both_ends'
            WHEN start_date < first_game THEN 'transaction_start_game_end'
            WHEN end_date > last_game THEN 'game_start_transaction_end' ELSE 'game_dates_only' END''')
    print('roster spells', c.execute('SELECT date_basis, count(*) FROM core.fact_roster_spell GROUP BY 1 ORDER BY 2 DESC').fetchall())


def views(c) -> None:
    """Core views spanning sources: current-season shot attempts, and opt-in all-pro-leagues views (NBA/BAA + ABA)."""
    old = c.execute("SELECT sql FROM duckdb_views() WHERE schema_name='core' AND view_name='field_goal_attempt'").fetchone()[0]
    if 'espn_pbp' not in old:
        body = old.split(' AS ', 1)[1].rstrip(';')
        # current season: attempts from the ESPN-sourced play stream (same keys as the season snapshot used)
        c.execute(f'''CREATE OR REPLACE VIEW core.field_goal_attempt AS {body} UNION ALL (
            SELECT CAST(sha256(CAST(('espn:' || s.espn_play_id) AS VARCHAR)) AS BLOB), p.game_id, p.team_id, p.person_id, p.period,
                   p.seconds_remaining, p.clock_iso, p.field_goal_value, p.shot_made, 'espn_pbp', p.action_number, p.source_action,
                   'not_normalized', CAST(NULL AS SMALLINT), CAST(NULL AS SMALLINT), CAST(NULL AS SMALLINT)
            FROM core.fact_play_event p JOIN audit."season_2027__play_source" s USING (game_id, action_number)
            WHERE p.is_field_goal AND p.team_id IS NOT NULL AND p.person_id IS NOT NULL)''')
    c.execute('''CREATE OR REPLACE VIEW core.game_all_leagues AS
        SELECT game_id, game_date, season_start, season_year, league_era AS league, season_type, home_team_id, away_team_id,
               arena_name, attendance FROM core.dim_game
        UNION ALL SELECT game_id, game_date, season_start, season_year, league, season_type, home_team_id, away_team_id,
               arena_name, attendance FROM core.aba_game''')
    c.execute('''CREATE OR REPLACE VIEW core.game_result_all_leagues AS
        SELECT game_id, home_score, away_score, winner_team_id, is_overtime FROM core.fact_game_result
        UNION ALL SELECT game_id, home_score, away_score, winner_team_id, is_overtime FROM core.aba_game_result''')
    c.execute('''CREATE OR REPLACE VIEW core.regular_standings_all_leagues AS
        SELECT g.league_era AS league, s.season_year, s.team_id, s.games, s.wins, s.losses, s.points_for, s.points_against
        FROM core.fact_regular_standings s JOIN (SELECT DISTINCT season_year, league_era FROM core.dim_season) g USING (season_year)
        UNION ALL SELECT 'ABA', season_year, team_id, games, wins, losses, points_for, points_against FROM core.aba_regular_standings''')
    print('views ok', c.execute("SELECT count(*) FROM core.field_goal_attempt WHERE attempt_source = 'espn_pbp'").fetchone())


def tracking(c) -> None:
    c.execute('''CREATE OR REPLACE TABLE core.fact_tracking_season AS
        SELECT kind, season_year, CASE season_type WHEN 'RegularSeason' THEN 'Regular' ELSE season_type END AS season_type,
               lower(entity) AS entity, measure,
               CASE WHEN entity = 'Player' THEN player_id END AS person_id, team_id, stats
        FROM raw.nba_tracking
        WHERE (entity = 'Player' AND player_id IN (SELECT person_id FROM core.dim_person))
           OR (entity = 'Team' AND team_id IN (SELECT team_id FROM core.dim_team))''')
    c.execute("DELETE FROM audit.domain_held WHERE domain = 'fact_tracking_season'")
    c.execute('''INSERT INTO audit.domain_held SELECT 'fact_tracking_season', NULL, 'unresolved_' || lower(entity), to_json(t)
        FROM raw.nba_tracking t
        WHERE NOT ((entity = 'Player' AND player_id IN (SELECT person_id FROM core.dim_person))
                OR (entity = 'Team' AND team_id IN (SELECT team_id FROM core.dim_team)))''')
    disposition(c, 'fact_tracking_season', 'raw.nba_tracking (NBA.com tracking + hustle)', c.execute('SELECT count(*) FROM raw.nba_tracking').fetchone()[0])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('parts', nargs='*', default=['identities', 'transactions', 'aba', 'salaries', 'tracking', 'rosters', 'views'])
    ap.add_argument('--output', type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    c = connect(a.output)
    c.execute('''CREATE TABLE IF NOT EXISTS audit.domain_disposition (domain VARCHAR PRIMARY KEY, source VARCHAR,
        source_rows BIGINT, published BIGINT, duplicate BIGINT, held BIGINT)''')
    c.execute('CREATE TABLE IF NOT EXISTS audit.domain_held (domain VARCHAR, source_row BIGINT, reason VARCHAR, row_json JSON)')
    for part in a.parts:
        globals()[part](c)
    c.execute('CHECKPOINT')


if __name__ == '__main__':
    main()
