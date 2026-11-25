"""Recover two seasons of regulation and overtime scores from the supplied team-box mirror."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile

import duckdb

from nba_warehouse import ROOT, SCRATCH, WAREHOUSE, scalar_count, sql_string, verify
from recover_archive_periods import sha256
from recover_local_periods import difference, snapshot


RAW_PROVENANCE = 'main.stg_kaggle_team_box (kaggle_nba, derivative of NBA.com)'
TAIL_RECEIPTS = WAREHOUSE / 'mirror_period_tail_receipts.csv'


def input_manifest(parent: Path, source: Path, receipts: Path) -> list[tuple[str, str, str, bool]]:
    return [('official_tail_csv', sha256(receipts), 'Transcribed NBA game-page OT3 cells', False),
            ('parent', sha256(parent), 'nba-clean-v10', False),
            ('source', sha256(source), RAW_PROVENANCE, False)]


def attach_inputs(con: duckdb.DuckDBPyConnection, parent: Path, source: Path) -> None:
    for role, path in (('prior', parent), ('src', source)):
        con.execute(f'ATTACH {sql_string(str(path))} AS {role} (READ_ONLY)')
    con.execute((WAREHOUSE / 'mirror_period_inputs.sql').read_text())


def verify_inputs(parent: Path, source: Path, receipts: Path, release: Path) -> None:
    inputs = parent, source, receipts, release
    initial = tuple(map(snapshot, inputs))
    with duckdb.connect(str(release), read_only=True, config={'threads': 2, 'memory_limit': '2GB'}) as con:
        attach_inputs(con, parent, source)
        if con.execute('SELECT * FROM audit.mirror_period_input ORDER BY role').fetchall() != input_manifest(parent, source, receipts):
            raise RuntimeError('Mirror source, parent or official receipt digest does not match the release manifest')
        con.execute('CREATE TEMP TABLE mirror_period_tail_source AS SELECT * FROM audit.mirror_period_tail_receipt LIMIT 0')
        con.execute(f'COPY mirror_period_tail_source FROM {sql_string(str(receipts))} (FORMAT CSV, HEADER TRUE)')
        for expected, observed in (('mirror_period_scope', 'audit.mirror_period_game'),
                                   ('mirror_period_source', 'audit.mirror_period_team'),
                                   ('mirror_period_tail_source', 'audit.mirror_period_tail_receipt')):
            if difference(con, f'SELECT * FROM {expected}', f'SELECT * FROM {observed}'):
                raise RuntimeError(f'Exact attached input differs from release ledger: {observed}')
        for relation in ('nba.dim_game', 'nba.dim_team', 'nba.bridge_game_team', 'nba.bridge_event_fixture',
                         'nba.fact_game_result', 'nba.fact_regular_standings'):
            if difference(con, f'SELECT * FROM {relation}', f'SELECT * FROM prior.{relation}'):
                raise RuntimeError(f'Parent identity, results or standings changed: {relation}')
        for relation in ('nba.fact_game_period_score', 'nba.game_overtime', 'audit.period_coverage',
                         'audit.period_vector_baseline'):
            scope = 'ANTI JOIN mirror_period_scope USING(game_id)'
            if difference(con, f'SELECT * FROM {relation} {scope}', f'SELECT * FROM prior.{relation} {scope}'):
                raise RuntimeError(f'Out-of-scope records changed: {relation}')
        if scalar_count(con, 'SELECT count(*) FROM prior.nba.fact_game_period_score SEMI JOIN mirror_period_scope USING(game_id)'):
            raise RuntimeError('Mirror publication would replace a prior period vector')
        checks = verify(con)
    if tuple(map(snapshot, inputs)) != initial:
        raise RuntimeError('An input changed during verification')
    print(f'{checks} quality checks passed with matching source keys/cells, SHA-256 and parent delta')


def build(parent: Path, source: Path, receipts: Path, output: Path) -> None:
    if os.path.lexists(output) or os.path.lexists(f'{output}.wal'):
        raise FileExistsError(output)
    inputs = parent, source, receipts
    initial = tuple(map(snapshot, inputs))
    if parent.samefile(source):
        raise RuntimeError('Parent and raw input must be different databases')
    SCRATCH.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='.mirror-periods-') as temp, \
         tempfile.TemporaryDirectory(dir=SCRATCH, prefix='mirror-spill-') as spill:
        stage = Path(temp) / 'release.duckdb'
        shutil.copyfile(parent, stage)
        with duckdb.connect(str(stage), config={'threads': 2, 'memory_limit': '2GB', 'temp_directory': spill}) as con:
            if con.execute('SELECT source_size,source_mtime_ns FROM audit.input_manifest').fetchall() != [(initial[1].size, initial[1].mtime_ns)]:
                raise RuntimeError('Raw input differs from the parent manifest')
            attach_inputs(con, parent, source)
            if scalar_count(con, 'SELECT count(*) FROM mirror_period_scope') != 1640:
                raise RuntimeError('Expected the full 1,640-game parent cohort')
            con.begin()
            con.execute((WAREHOUSE / 'mirror_periods.sql').read_text())
            con.execute(f'COPY audit.mirror_period_tail_receipt FROM {sql_string(str(receipts))} (FORMAT CSV, HEADER TRUE)')
            if scalar_count(con, 'SELECT count(*) FROM audit.mirror_period_tail_receipt') != 2:
                raise RuntimeError('Expected both official OT3 cells')
            con.executemany('INSERT INTO audit.mirror_period_input VALUES (?,?,?,?)', input_manifest(parent, source, receipts))
            con.execute('''
                INSERT INTO nba.fact_game_period_score SELECT * FROM audit.mirror_period_expected_cells;
                INSERT INTO audit.period_vector_baseline
                SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
                       ORDER BY team_id,period_number))) FROM audit.mirror_period_expected_cells GROUP BY game_id;
                UPDATE audit.period_coverage c SET status='published',
                   selected_source=CASE WHEN EXISTS (SELECT 1 FROM audit.mirror_period_tail_receipt t WHERE t.game_id=g.game_id)
                       THEN 'local_team_box_mirror_plus_official_tail' ELSE 'local_team_box_mirror' END,
                   candidate_sources=g.prior_candidates+1,valid_sources=g.prior_valid+1
                FROM audit.mirror_period_game g WHERE g.game_id=c.game_id;
                CREATE VIEW audit.period_availability_v11 AS
                SELECT v.* REPLACE (CASE WHEN m.game_id IS NOT NULL THEN v.selected_source ELSE v.recovery_disposition END
                                      AS recovery_disposition)
                FROM audit.period_availability_v10 v LEFT JOIN audit.mirror_period_game m USING(game_id);
            ''')
            con.commit()
            con.execute('CHECKPOINT')
        verify_inputs(parent, source, receipts, stage)
        if tuple(map(snapshot, inputs)) != initial or os.path.lexists(f'{output}.wal'):
            raise RuntimeError('An input changed or output WAL exists; output was not installed')
        os.link(stage, output)
        print(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'verify-inputs'))
    parser.add_argument('--parent', type=Path, default=ROOT / 'data/nba-clean-v10.duckdb')
    parser.add_argument('--source', type=Path, default=ROOT / 'data/nba.duckdb')
    parser.add_argument('--receipts', type=Path, default=TAIL_RECEIPTS)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    parent, source, receipts = (getattr(args, k).resolve(strict=True) for k in ('parent', 'source', 'receipts'))
    if args.action == 'build':
        build(parent, source, receipts, args.output.absolute())
    else:
        verify_inputs(parent, source, receipts, args.output.resolve(strict=True))


if __name__ == '__main__':
    main()
