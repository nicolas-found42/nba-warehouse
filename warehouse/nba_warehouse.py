#!/usr/bin/env python3
"""Build or verify the local, snapshot-derived NBA game and box-score warehouse."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import tempfile

import duckdb

WAREHOUSE = Path(__file__).resolve().parent
ROOT = WAREHOUSE.parent
SCRATCH = Path.home() / '.hermes' / 'cache' / 'scratch'
SNAPSHOT_GAME_COUNT = 75_265


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def read(cls, path: Path) -> FileIdentity:
        st = path.stat()
        return cls(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def sql_string(text: str) -> str:
    # ATTACH accepts a literal, not a statement parameter.
    return "'" + text.replace("'", "''") + "'"


def scalar_count(connection: duckdb.DuckDBPyConnection, query: str) -> int:
    row = connection.execute(query).fetchone()
    if row is None or type(row[0]) is not int:
        raise RuntimeError('Expected a single integer count')
    return row[0]


def has_table(connection: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    row = connection.execute('''SELECT count(*) FROM information_schema.tables
        WHERE table_catalog=current_database() AND table_schema=? AND table_name=?''', (schema, table)).fetchone()
    return row is not None and row[0] == 1


def verify(connection: duckdb.DuckDBPyConnection) -> int:
    recovered = has_table(connection, 'audit', 'fixture_addition')
    box_v17_relations = ('box_recovery_input_v17','team_box_recovery_v17','team_box_publication_v17',
                         'player_box_recovery_v17','player_box_publication_v17','player_box_availability_v17')
    box_v17 = [has_table(connection,'audit',rel) for rel in box_v17_relations]
    if any(box_v17) and not all(box_v17):
        raise RuntimeError('V17 box recovery evidence or expected publication is missing')
    allstar_v24_parts = tuple(has_table(connection,'audit',rel) for rel in
        ('allstar_player_receipt_v24','allstar_player_publication_v24','allstar_player_input_v24','player_box_availability_v24'))
    allstar_v24 = all(allstar_v24_parts)
    if any(allstar_v24_parts) and (not allstar_v24 or not all(box_v17)):
        raise RuntimeError('V24 All-Star player recovery evidence or inherited publication is missing')
    for alias, base, supplement in (
        ('verify_game_keys', 'SELECT game_id FROM audit.game_disposition', 'SELECT game_id FROM audit.fixture_addition'),
        ('verify_team_box_keys', "SELECT game_id,team_id FROM audit.team_box_disposition WHERE disposition='published'",
         'SELECT game_id,team_id FROM audit.fixture_added_team_box'),
        ('verify_player_box_keys', "SELECT game_id,person_id FROM audit.player_box_disposition WHERE disposition='published'",
         'SELECT game_id,person_id FROM audit.fixture_added_player_box'),
    ):
        projection = f'{base} UNION ALL {supplement}' if recovered else base
        if all(box_v17) and alias == 'verify_team_box_keys':
            projection += ' UNION ALL SELECT game_id,team_id FROM audit.team_box_recovery_v17 WHERE valid'
        if all(box_v17) and alias == 'verify_player_box_keys':
            projection += " UNION ALL SELECT game_id,person_id FROM audit.player_box_recovery_v17 WHERE disposition='published_traditional'"
        if allstar_v24 and alias == 'verify_player_box_keys':
            projection += ' UNION ALL SELECT game_id,person_id FROM audit.allstar_player_receipt_v24'
        connection.execute(f'CREATE OR REPLACE TEMP VIEW {alias} AS {projection}')
    event_periods = has_table(connection, 'audit', 'event_period_receipt')
    if event_periods or has_table(connection, 'audit', 'event_period_input') or has_table(connection, 'nba', 'event_period_format'):
        if not all(has_table(connection, schema, table) for schema, table in (
                ('audit', 'period_coverage'), ('audit', 'event_period_receipt'), ('audit', 'event_period_input'),
                ('nba', 'event_period_format'), ('nba', 'event_period_score'))):
            raise RuntimeError('Event period evidence or published relations are missing')
    period_expectations = ('SELECT game_id,1 AS regulation_count,1 AS exact_count FROM audit.fixture_addition'
                           if recovered else 'SELECT NULL::VARCHAR AS game_id,4 AS regulation_count,4 AS exact_count WHERE false')
    if event_periods:
        period_expectations += ' UNION ALL SELECT game_id,1,len(home_periods)::INT FROM audit.event_period_receipt'
    header_relations = ('period_adjudication_v19','period_v19_input','period_cells_v19','period_availability_v19')
    header_tables = [has_table(connection,'audit',rel) for rel in header_relations]
    header_repair = all(header_tables)
    if any(header_tables) and not header_repair:
        raise RuntimeError('V19 line-score receipt evidence or publication views are missing')
    header_keys = ('SELECT game_id FROM audit.period_adjudication_v19' if header_repair else
                   'SELECT NULL::VARCHAR AS game_id WHERE false')
    if header_repair:
        period_expectations += ' UNION ALL SELECT game_id,4,4 FROM audit.period_adjudication_v19'
    connection.execute(f'CREATE OR REPLACE TEMP VIEW verify_period_v19_keys AS {header_keys}')
    bref_v21_parts = ('bref_period_receipt_v21','bref_period_cells_v21','period_availability_v21','bref_period_input_v21')
    bref_v21_tables = [has_table(connection,'audit',rel) for rel in bref_v21_parts]
    bref_v21 = all(bref_v21_tables)
    if any(bref_v21_tables) and not bref_v21:
        raise RuntimeError('V21 Basketball-Reference receipts or period publication views are missing')
    v21_keys = "SELECT game_id FROM audit.bref_period_receipt_v21 WHERE disposition='admitted'"
    superseding = 'SELECT game_id FROM verify_period_v19_keys'
    if bref_v21:
        superseding += f' UNION ALL {v21_keys}'
        period_expectations += f' UNION ALL SELECT game_id,4,4 FROM ({v21_keys})'
    v22_source = 'bref_v22_periods_match_reoriented_local_cells'
    v22_parts = tuple(has_table(connection,'audit',rel) for rel in
                      ('bref_period_receipt_v22','bref_period_cells_v22','period_availability_v22','bref_period_input_v22'))
    bref_v22 = all(v22_parts)
    if any(v22_parts) and (not bref_v22 or not bref_v21):
        raise RuntimeError('V22 Basketball-Reference receipts or parent publication views are missing')
    v22_keys = "SELECT game_id FROM audit.bref_period_receipt_v22 WHERE disposition='admitted'"
    if bref_v22:
        superseding += f' UNION ALL {v22_keys}'
        period_expectations += f' UNION ALL SELECT game_id,4,4 FROM ({v22_keys})'
    archive_v23_parts = tuple(has_table(connection,'audit',rel) for rel in
        ('archive_period_receipt_v23','archive_period_cells_v23','archive_period_input_v23',
         'archive_period_prior_v7_v23','period_availability_v23'))
    archive_v23 = all(archive_v23_parts)
    if any(archive_v23_parts) and (not archive_v23 or not bref_v22):
        raise RuntimeError('V23 archival receipts or parent publication views are missing')
    v23_keys = 'SELECT game_id FROM audit.archive_period_receipt_v23'
    if archive_v23:
        superseding += f' UNION ALL {v23_keys}'
        period_expectations += f' UNION ALL SELECT game_id,4,4 FROM ({v23_keys})'
    preseason_v25_parts = tuple(has_table(connection,'audit',rel) for rel in
        ('preseason_period_receipt_v25','preseason_period_cells_v25','preseason_period_input_v25',
         'preseason_prior_v7_v25','preseason_prior_v23_v25','period_availability_v25'))
    preseason_v25 = all(preseason_v25_parts)
    if any(preseason_v25_parts) and (not preseason_v25 or not archive_v23 or not allstar_v24):
        raise RuntimeError('V25 preseason receipts or inherited release relations are missing')
    v25_keys = "SELECT game_id FROM audit.preseason_period_receipt_v25 WHERE disposition='admitted'"
    if preseason_v25:
        superseding += f' UNION ALL {v25_keys}'
        period_expectations += f' UNION ALL SELECT game_id,4,4 FROM ({v25_keys})'
    preseason_v26_parts = tuple(has_table(connection,'audit',rel) for rel in
        ('preseason_period_receipt_v26','preseason_period_cells_v26','preseason_period_input_v26',
         'preseason_v26_prior_period_availability_v7','preseason_v26_prior_period_availability_v23','period_availability_v26'))
    preseason_v26 = all(preseason_v26_parts)
    if any(preseason_v26_parts) and (not preseason_v26 or not preseason_v25):
        raise RuntimeError('V26 preseason evidence or prior release is missing')
    v26_keys = 'SELECT game_id FROM audit.preseason_period_receipt_v26'
    if preseason_v26:
        superseding += f' UNION ALL {v26_keys}'
        period_expectations += ' UNION ALL SELECT game_id,4,len(home_periods)::INT FROM audit.preseason_period_receipt_v26'
    connection.execute(f'CREATE OR REPLACE TEMP VIEW verify_new_period_superseded AS {superseding}')
    connection.execute(f'CREATE OR REPLACE TEMP VIEW verify_period_expectations AS {period_expectations}')
    checks = connection.execute((WAREHOUSE / 'verify.sql').read_text()).fetchall()
    local_period_proof = has_table(connection, 'audit', 'local_period_recovery')
    if has_table(connection, 'audit', 'period_coverage'):
        checks.extend(connection.execute((WAREHOUSE / 'verify_periods.sql').read_text()).fetchall())
        if not local_period_proof and scalar_count(connection,
                "SELECT count(*) FROM audit.period_coverage WHERE selected_source='local_line_score_reoriented'"):
            raise RuntimeError('Local period recovery evidence is missing')
        archive_periods = has_table(connection, 'audit', 'archive_period_receipt')
        if archive_periods or scalar_count(connection,
                "SELECT count(*) FROM audit.period_coverage WHERE selected_source='archival_line_score'"):
            checks.extend(connection.execute((WAREHOUSE / 'verify_period_integrity.sql').read_text()).fetchall())
        if has_table(connection, 'audit', 'period_adjudication') or scalar_count(connection,
                "SELECT count(*) FROM audit.period_coverage WHERE selected_source='adjudicated_period_receipt'"):
            checks.extend(connection.execute((WAREHOUSE / 'verify_period_adjudication.sql').read_text()).fetchall())
        if has_table(connection, 'audit', 'official_period_receipt') or scalar_count(connection,
                "SELECT count(*) FROM audit.period_coverage WHERE selected_source='official_period_receipt'"):
            checks.extend(connection.execute((WAREHOUSE / 'verify_official_periods.sql').read_text()).fetchall())
        if event_periods or scalar_count(connection,
                "SELECT count(*) FROM audit.period_coverage WHERE selected_source='event_period_receipt'"):
            checks.extend(connection.execute((WAREHOUSE / 'verify_event_period_receipts.sql').read_text()).fetchall())
        if has_table(connection, 'audit', 'mirror_period_input') or scalar_count(connection,
                "SELECT count(*) FROM audit.period_coverage WHERE selected_source IN "
                "('local_team_box_mirror','local_team_box_mirror_plus_official_tail')"):
            checks.extend(connection.execute((WAREHOUSE / 'verify_mirror_periods.sql').read_text()).fetchall())
        period_v16 = has_table(connection, 'audit', 'playoff_ot_schedule_pin') or scalar_count(connection,
                "SELECT count(*) FROM audit.period_coverage WHERE selected_source IN "
                "('mirror_v16_playoff_ot_flag_adjudicated','mirror_v16_keyed_official_ot3')") > 0
        if has_table(connection, 'audit', 'mirror_v12_prior') or scalar_count(connection,
                "SELECT count(*) FROM audit.period_coverage WHERE selected_source IN "
                "('mirror_v12_legacy_team_box','mirror_v12_keyed_official_ot')"):
            early_superseded = ('SELECT game_id FROM audit.early_ot3_receipt_v16' if period_v16 else
                                'SELECT NULL::VARCHAR AS game_id WHERE false')
            connection.execute(f'CREATE OR REPLACE TEMP VIEW verify_mirror_v12_superseded AS {early_superseded}')
            checks.extend(connection.execute((WAREHOUSE / 'verify_mirror_v12.sql').read_text()).fetchall())
        mirror_v14 = has_table(connection, 'audit', 'mirror_v14_prior') or scalar_count(connection,
                "SELECT count(*) FROM audit.period_coverage WHERE selected_source IN "
                "('mirror_v14_legacy_team_box','mirror_v14_keyed_official_ot3')") > 0
        if has_table(connection, 'audit', 'mirror_v13_prior') or scalar_count(connection,
                "SELECT count(*) FROM audit.period_coverage WHERE selected_source IN "
                "('mirror_v13_legacy_team_box','mirror_v13_keyed_official_ot')"):
            superseded = ' UNION ALL '.join(sql for enabled, sql in (
                (mirror_v14, "SELECT t.game_id FROM audit.mirror_v14_tail t JOIN audit.mirror_v13_assessment a USING(game_id) "
                             "WHERE a.disposition='overtime_slots_truncated'"),
                (period_v16, "SELECT game_id FROM audit.period_adjudication_v16_keys "
                             "WHERE selected_source='mirror_v16_playoff_ot_flag_adjudicated'"),
                (header_repair, 'SELECT game_id FROM verify_period_v19_keys'),
            ) if enabled) or 'SELECT NULL::VARCHAR AS game_id WHERE false'
            connection.execute(f'CREATE OR REPLACE TEMP VIEW verify_mirror_v13_superseded AS {superseded}')
            checks.extend(connection.execute((WAREHOUSE / 'verify_mirror_v13.sql').read_text()).fetchall())
        if mirror_v14:
            checks.extend(connection.execute((WAREHOUSE / 'verify_mirror_v14.sql').read_text()).fetchall())
        if period_v16:
            checks.extend(connection.execute((WAREHOUSE / 'verify_period_adjudication_v16.sql').read_text()).fetchall())
        if header_repair:
            checks.extend(connection.execute((WAREHOUSE / 'verify_period_adjudication_v19.sql').read_text()).fetchall())
        if bref_v21:
            checks.extend(connection.execute((WAREHOUSE / 'verify_bref_periods_v21.sql').read_text()).fetchall())
        if bref_v22:
            checks.extend(connection.execute((WAREHOUSE / 'verify_bref_periods_v22.sql').read_text()).fetchall())
        elif scalar_count(connection,"SELECT count(*) FROM audit.period_coverage WHERE selected_source="+sql_string(v22_source)):
            raise RuntimeError('V22 publication is missing its evidence ledger')
        if archive_v23:
            checks.extend(connection.execute((WAREHOUSE / 'verify_archive_periods_v23.sql').read_text()).fetchall())
        elif scalar_count(connection,"SELECT count(*) FROM audit.period_coverage WHERE selected_source='archival_cumulative_v23'"):
            raise RuntimeError('V23 publication is missing its evidence ledger')
        if preseason_v25:
            checks.extend(connection.execute((WAREHOUSE / 'verify_preseason_periods_v25.sql').read_text()).fetchall())
        elif scalar_count(connection,"SELECT count(*) FROM audit.period_coverage WHERE selected_source='archived_nba_preseason_line_score_v25'"):
            raise RuntimeError('V25 publication is missing its evidence ledger')
        if preseason_v26:
            checks.extend(connection.execute((WAREHOUSE / 'verify_preseason_periods_v26.sql').read_text()).fetchall())
        elif scalar_count(connection,"SELECT count(*) FROM audit.period_coverage WHERE selected_source='archived_nba_preseason_line_score_v26'"):
            raise RuntimeError('V26 publication is missing its evidence ledger')
    if local_period_proof:
        later_periods = ' UNION ALL '.join(
            f"SELECT game_id,status,selected_source FROM audit.mirror_{version}_coverage WHERE status='no_valid_candidate'"
            for version in ('v13','v14') if has_table(connection, 'audit', f'mirror_{version}_prior')
        ) or 'SELECT NULL::VARCHAR AS game_id,NULL::VARCHAR AS status,NULL::VARCHAR AS selected_source WHERE false'
        if header_repair:
            later_periods += " UNION ALL SELECT game_id,'published','official_v19_header_disagrees' FROM verify_period_v19_keys"
        if bref_v21:
            later_periods += f" UNION ALL SELECT game_id,'published','bref_v21_printed_line_score' FROM ({v21_keys})"
        if bref_v22:
            later_periods += f" UNION ALL SELECT game_id,'published',{sql_string(v22_source)} FROM ({v22_keys})"
        if archive_v23:
            later_periods += f" UNION ALL SELECT game_id,'published','archival_cumulative_v23' FROM ({v23_keys})"
        if preseason_v25:
            later_periods += f" UNION ALL SELECT game_id,'published','archived_nba_preseason_line_score_v25' FROM ({v25_keys})"
        if preseason_v26:
            later_periods += f" UNION ALL SELECT game_id,'published','archived_nba_preseason_line_score_v26' FROM ({v26_keys})"
        connection.execute(f'CREATE OR REPLACE TEMP VIEW verify_local_period_later_evidence AS {later_periods}')
        checks.extend(connection.execute((WAREHOUSE / 'verify_local_period_recovery.sql').read_text()).fetchall())
    if has_table(connection, 'audit', 'verified_score_repair'):
        checks.extend(connection.execute((WAREHOUSE / 'verify_reconciliation.sql').read_text()).fetchall())
    if has_table(connection, 'audit', 'event_coverage'):
        archive_parts = [('audit','event_archive_v20'),('audit','event_source_payload_v20'),
                         ('audit','event_archive_parent_v20'),('nba','bridge_shot_event_source_v20'),('nba','shot_event_link')]
        archive = [has_table(connection,schema,rel) for schema,rel in archive_parts]
        if any(archive) and not all(archive):
            raise RuntimeError('V20 event archive evidence or publication relation is missing')
        links = 'nba.shot_event_link' if all(archive) else 'nba.bridge_shot_event'
        connection.execute(f'CREATE OR REPLACE TEMP VIEW verify_shot_event_links AS SELECT shot_key,game_id,action_number,match_method FROM {links}')
        if all(archive):
            connection.execute((WAREHOUSE / 'event_archive_projection_v20.sql').read_text())
            checks.extend(connection.execute((WAREHOUSE / 'verify_event_archive_v20.sql').read_text()).fetchall())
        checks.extend(connection.execute((WAREHOUSE / 'verify_events.sql').read_text()).fetchall())
    if has_table(connection, 'audit', 'pbp_attempt_scope') or has_table(connection, 'nba', 'fact_pbp_field_goal_attempt'):
        modern = has_table(connection, 'audit', 'pbp_attempt_modern_scope')
        legacy = has_table(connection, 'audit', 'legacy_attempt_scope_v18')
        if legacy != has_table(connection, 'audit', 'legacy_attempt_input_v18'):
            raise RuntimeError('Legacy attempt scope or input pins are missing')
        if legacy:
            connection.execute((WAREHOUSE / 'legacy_attempts_v18.sql').read_text())
            checks.extend(connection.execute((WAREHOUSE / 'verify_legacy_attempts_v18.sql').read_text()).fetchall())
        legacy_projection = ('SELECT * FROM legacy_attempt_expected' if legacy else
                             'SELECT * FROM nba.fact_pbp_field_goal_attempt WHERE false')
        connection.execute(f'CREATE OR REPLACE TEMP VIEW verify_legacy_attempt_expected AS {legacy_projection}')
        cohorts = "SELECT 2019 AS season_start,'Regular' AS season_type"
        if modern:
            cohorts += " UNION ALL VALUES (2024,'Regular'),(2025,'Playoffs')"
        connection.execute(f'CREATE OR REPLACE TEMP VIEW verify_pbp_attempt_cohorts AS {cohorts}')
        roles = "SELECT 'parent_v14' AS role" + (" UNION ALL SELECT 'parent_v15'" if modern else '')
        connection.execute(f'CREATE OR REPLACE TEMP VIEW verify_pbp_attempt_roles AS {roles}')
        checks.extend(connection.execute((WAREHOUSE / 'verify_pbp_attempts.sql').read_text()).fetchall())
        if modern:
            checks.extend(connection.execute((WAREHOUSE / 'verify_modern_attempts.sql').read_text()).fetchall())
    if recovered:
        checks.extend(connection.execute((WAREHOUSE / 'verify_fixture_recovery.sql').read_text()).fetchall())
    if all(box_v17):
        checks.extend(connection.execute((WAREHOUSE / 'verify_boxes_v17.sql').read_text()).fetchall())
    if allstar_v24:
        checks.extend(connection.execute((WAREHOUSE / 'verify_allstar_box_v24.sql').read_text()).fetchall())
    if not checks:
        raise RuntimeError('No warehouse checks were run')
    failures = [(name, count) for name, count in checks if count != 0]
    if failures:
        raise RuntimeError('Failed warehouse checks: ' + ', '.join(f'{n}={c}' for n, c in failures))
    return len(checks)


def verify_source_accounting(connection: duckdb.DuckDBPyConnection) -> None:
    # These statements compare key sets in both directions against the actual
    # source, not just counts against a saved manifest. All keys are SQL constants.
    for source, ledger, keys in (
        ('fact_game', 'game_disposition', 'game_id'),
        ('fact_team_game_stats', 'team_box_disposition', 'game_id, team_id'),
        ('fact_player_game_stats', 'player_box_disposition', 'game_id, person_id'),
    ):
        left = f'SELECT {keys} FROM src.main.{source}'
        right = f'SELECT {keys} FROM audit.{ledger}'
        mismatches = scalar_count(connection, f'SELECT count(*) FROM (({left} EXCEPT ALL {right}) '
                                f'UNION ALL ({right} EXCEPT ALL {left}))')
        if mismatches:
            raise RuntimeError(f'{source}: {mismatches} keys differ between source and audit ledger')


def verify_event_source_accounting(connection: duckdb.DuckDBPyConnection) -> None:
    # Audit the original raw inputs independently of separately pinned supplements.
    event_restrict = shot_restrict = ''
    if has_table(connection,'audit','event_archive_v20'):
        if scalar_count(connection, '''SELECT (SELECT count(*) FROM src.main.fact_pbp_events e
             SEMI JOIN audit.event_archive_v20 a ON e.game_id=a.game_id AND a.domain='events')
             + (SELECT count(*) FROM src.main.fact_shot_chart s
             SEMI JOIN audit.event_archive_v20 a ON s.game_id=a.game_id AND a.domain='shotdetail')'''):
            raise RuntimeError('Raw event/shot input unexpectedly overlaps the archive supplement')
        event_restrict = " ANTI JOIN (SELECT game_id FROM audit.event_archive_v20 WHERE domain='events') USING(game_id) "
        shot_restrict = " ANTI JOIN (SELECT game_id FROM audit.event_archive_v20 WHERE domain='shotdetail') USING(game_id) "
    event_source = 'SELECT game_id,action_number FROM src.main.fact_pbp_events'
    event_release = f'''SELECT game_id,action_number FROM nba.fact_play_event {event_restrict}
                       UNION ALL SELECT game_id,action_number FROM audit.event_hold'''
    shot_source = '''SELECT unhex(sha256(to_json(s))) AS shot_key, game_id, count(*) AS multiplicity
                     FROM (SELECT * EXCLUDE (season_year) FROM src.main.fact_shot_chart) s GROUP BY 1,2'''
    shot_release = f'''SELECT shot_key,game_id,1::BIGINT AS multiplicity FROM nba.fact_shot {shot_restrict}
                      UNION ALL SELECT shot_key,game_id,multiplicity FROM audit.shot_hold'''
    for domain, left, right in [('events', event_source, event_release), ('shots', shot_source, shot_release)]:
        difference = scalar_count(connection, f'''SELECT count(*) FROM (
              (SELECT * FROM ({left}) EXCEPT ALL SELECT * FROM ({right}))
              UNION ALL (SELECT * FROM ({right}) EXCEPT ALL SELECT * FROM ({left}))
        )''')
        if difference:
            raise RuntimeError(f'{domain}: {difference} source keys or multiplicities differ')


def record_manifest(connection: duckdb.DuckDBPyConnection, source: Path, identity: FileIdentity,
                    expected_games: int) -> None:
    game_count = scalar_count(connection, 'SELECT count(*) FROM src.main.fact_game')
    if game_count != expected_games:
        raise RuntimeError(f'Expected {expected_games:,} source games; found {game_count:,}')
    if scalar_count(connection, """SELECT count(*) FROM (
        SELECT team_id FROM src.main.dim_team GROUP BY 1 HAVING count(*) FILTER (WHERE is_current) > 1
    )"""):
        raise RuntimeError('Source has multiple current profiles for a team; cannot pick a display label')
    connection.execute('''INSERT INTO audit.input_manifest
        SELECT true, ?, ?, ?, (SELECT version FROM src.main.dim_warehouse_release WHERE is_current),
               ?, ?, (SELECT count(*) FROM src.main.fact_team_game_stats),
                     (SELECT count(*) FROM src.main.fact_player_game_stats)
    ''', (str(source), identity.size, identity.mtime_ns, duckdb.__version__, game_count))


def build(source: Path, output: Path, expected_games: int, spill: Path, with_periods: bool,
          with_events: bool, with_recovery: bool) -> int:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise RuntimeError(f'Source is not a regular file: {source}')
    if output.resolve() == source or os.path.lexists(output):
        raise FileExistsError(f'Will not overwrite {output}')
    if Path(f'{source}.wal').exists():
        raise RuntimeError(f'Source has a WAL sidecar; use a closed, checkpointed snapshot: {source}')
    identity = FileIdentity.read(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    spill.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.nba-build-', dir=output.parent) as workdir, \
         tempfile.TemporaryDirectory(prefix='nba-spill-', dir=spill) as temp_directory:
        stage = Path(workdir) / output.name
        with duckdb.connect(str(stage), config={
            'threads': 2, 'memory_limit': '7GB' if with_events else '2GB', 'temp_directory': temp_directory,
        }) as connection:
            connection.execute(f'ATTACH {sql_string(str(source))} AS src (READ_ONLY)')
            connection.begin()
            connection.execute((WAREHOUSE / 'schema.sql').read_text())
            record_manifest(connection, source, identity, expected_games)
            connection.execute((WAREHOUSE / 'build.sql').read_text())
            if with_recovery:
                connection.execute((WAREHOUSE / 'fixture_recovery.sql').read_text())
                difference = scalar_count(connection, '''WITH actual AS (
                    SELECT p.game_id,p.player_id AS person_id FROM src.main.fact_player_game_boxscore p
                    JOIN audit.fixture_addition f USING(game_id)
                ), recorded AS (SELECT game_id,person_id FROM audit.fixture_added_player_box)
                SELECT count(*) FROM ((SELECT * FROM actual EXCEPT ALL SELECT * FROM recorded)
                                       UNION ALL (SELECT * FROM recorded EXCEPT ALL SELECT * FROM actual))''')
                if difference:
                    raise RuntimeError(f'Recovered fixture inputs: {difference} keys are not exactly accounted for')
            if with_periods or with_events or with_recovery:
                supplemental = ('''SELECT game_id,home_team_id AS team_id,1 AS period_number,home_score AS points FROM audit.fixture_addition
                    UNION ALL SELECT game_id,away_team_id,1,away_score FROM audit.fixture_addition''' if with_recovery
                    else 'SELECT NULL::VARCHAR AS game_id,NULL::BIGINT AS team_id,1 AS period_number,0 AS points WHERE false')
                connection.execute(f'CREATE TEMP VIEW supplemental_period_scores AS {supplemental}')
                connection.execute((WAREHOUSE / 'periods.sql').read_text())
            if with_events:
                connection.execute(f'CREATE TEMP VIEW shot_clock_profile AS SELECT {420 if with_recovery else 0} AS overtime_offset')
                connection.execute((WAREHOUSE / 'events.sql').read_text())
                verify_event_source_accounting(connection)
            verify_source_accounting(connection)
            verify(connection)
            connection.commit()
            connection.execute('CHECKPOINT')
        if Path(f'{stage}.wal').exists():
            raise RuntimeError('Refusing to publish a file with an uncheckpointed WAL')
        with duckdb.connect(str(stage), read_only=True, config={
            'threads': 2, 'memory_limit': '2GB' if with_events else '1GB', 'temp_directory': temp_directory,
        }) as connection:
            checks = verify(connection)
        if FileIdentity.read(source) != identity or Path(f'{source}.wal').exists():
            raise RuntimeError('Input snapshot changed during the build; output was not installed')
        # The stage directory is on the destination volume. Install atomically
        # and fail if another process created the destination after our check.
        os.link(stage, output)
    return checks


def print_summary(output: Path, checks: int) -> None:
    with duckdb.connect(str(output), read_only=True, config={'threads': 2, 'memory_limit': '1GB'}) as connection:
        for label, query in (
            ('Game spine', 'SELECT count(*) FROM nba.dim_game'),
            ('Final results', 'SELECT count(*) FROM nba.fact_game_result'),
            ('Team box rows', 'SELECT count(*) FROM nba.fact_team_game_stats'),
            ('Player box rows', 'SELECT count(*) FROM nba.fact_player_game_stats'),
            ('Held team box rows', "SELECT count(*) FROM audit.team_box_disposition WHERE disposition='held'"),
            ('Held player box rows', "SELECT count(*) FROM audit.player_box_disposition WHERE disposition='held'"),
        ):
            print(f'{label}: {scalar_count(connection, query):,}')
        if has_table(connection, 'audit', 'player_box_recovery_v17'):
            held = scalar_count(connection, "SELECT count(*) FROM audit.player_box_recovery_v17 WHERE disposition<>'published_traditional'")
            print(f'Additional held player box rows (v17 inputs): {held:,}')
        if scalar_count(connection, "SELECT count(*) FROM information_schema.tables WHERE table_schema='audit' AND table_name='event_coverage'"):
            shot_links = 'nba.shot_event_link' if has_table(connection,'nba','shot_event_link') else 'nba.bridge_shot_event'
            for label, query in (
                ('Play events', 'SELECT count(*) FROM nba.fact_play_event'),
                ('Shots', 'SELECT count(*) FROM nba.fact_shot'),
                ('Shot-to-event links', f'SELECT count(*) FROM {shot_links}'),
            ):
                print(f'{label}: {scalar_count(connection, query):,}')
    print(f'{checks} quality checks passed: {output}')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    build_parser = sub.add_parser('build', help='create a separate verified warehouse; never overwrite')
    build_parser.add_argument('--source', type=Path, default=ROOT / 'data/nba.duckdb')
    build_parser.add_argument('--output', type=Path, default=ROOT / 'data/nba-clean-v1.duckdb')
    build_parser.add_argument('--expect-games', type=int, default=SNAPSHOT_GAME_COUNT)
    build_parser.add_argument('--spill', type=Path, default=SCRATCH)
    build_parser.add_argument('--with-periods', action='store_true', help='include locally reconciled period scores')
    build_parser.add_argument('--with-events', action='store_true', help='include periods, play events and shots')
    build_parser.add_argument('--with-recovery', action='store_true', help='include the verified 2025 All-Star backfill, standings, and corrected shot clocks')
    verify_parser = sub.add_parser('verify', help='recheck a built warehouse read-only')
    verify_parser.add_argument('output', type=Path)
    args = parser.parse_args()
    output: Path = args.output.absolute()
    try:
        if args.command == 'build':
            checks = build(args.source, output, args.expect_games, args.spill,
                           args.with_periods, args.with_events, args.with_recovery)
        else:
            SCRATCH.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='nba-verify-', dir=SCRATCH) as spill, \
                 duckdb.connect(str(output), read_only=True, config={
                     'threads': 2, 'memory_limit': '2GB', 'temp_directory': spill,
                 }) as connection:
                checks = verify(connection)
        print_summary(output, checks)
    except (duckdb.Error, OSError, RuntimeError) as exc:
        print(f'warehouse: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
