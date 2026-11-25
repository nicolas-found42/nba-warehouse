#!/usr/bin/env python3
"""Write a local, offline Basketball-Reference comparison artifact. No HTTP requests."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import tempfile

import duckdb

from nba_warehouse import FileIdentity, ROOT, SCRATCH, WAREHOUSE, scalar_count, sql_string


def run(source: Path, curated: Path, output: Path) -> None:
    database_paths = {'src': source.resolve(strict=True), 'curated': curated.resolve(strict=True)}
    if os.path.lexists(output) or output.resolve() in database_paths.values():
        raise FileExistsError(f'Will not overwrite {output}')
    identities = {name: FileIdentity.read(path) for name, path in database_paths.items()}
    if len({(identity.device, identity.inode) for identity in identities.values()}) != 2:
        raise RuntimeError('Expected two distinct input files')
    output.parent.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.bref-audit-', dir=output.parent) as stage_dir, \
         tempfile.TemporaryDirectory(prefix='bref-audit-spill-', dir=SCRATCH) as spill:
        stage = Path(stage_dir) / 'comparison.duckdb'
        with duckdb.connect(str(stage), config={
            'threads': 2, 'memory_limit': '1GB', 'temp_directory': spill,
        }) as connection:
            connection.execute('SET enable_progress_bar = false')
            for name, path in database_paths.items():
                if Path(f'{path}.wal').exists():
                    raise RuntimeError(f'Input must be a closed, checkpointed database: {path}')
                connection.execute(f'ATTACH {sql_string(str(path))} AS {name} (READ_ONLY)')
            connection.begin()
            connection.execute((WAREHOUSE / 'validate_bref.sql').read_text())
            connection.execute('''CREATE TABLE input_identity (
                role VARCHAR PRIMARY KEY, path VARCHAR NOT NULL,
                bytes BIGINT NOT NULL, mtime_ns BIGINT NOT NULL
            )''')
            for name, path in database_paths.items():
                identity = identities[name]
                connection.execute('INSERT INTO input_identity VALUES (?, ?, ?, ?)',
                                   (name, str(path), identity.size, identity.mtime_ns))
            expected = {
                'team_season': "SELECT count(*) FROM src.main.fact_bref_team_season_totals WHERE league='NBA'",
                'player_season': """SELECT count(*) FROM (SELECT DISTINCT season_end_year,bref_player_id
                    FROM src.main.fact_bref_player_season_totals WHERE league='NBA' AND NOT is_playoffs)""",
            }
            for table, query in expected.items():
                if scalar_count(connection, query) != scalar_count(connection, f'SELECT count(*) FROM {table}'):
                    raise RuntimeError(f'{table} does not account for its reference population')
                grain = ('season_end_year, source_team_abbrev' if table == 'team_season'
                         else 'season_end_year, bref_player_id')
                if scalar_count(connection, f'SELECT count(*) FROM (SELECT {grain} FROM {table} GROUP BY ALL HAVING count(*)<>1)'):
                    raise RuntimeError(f'{table} joined ambiguously')
            connection.commit()
            connection.execute('CHECKPOINT')
        if Path(f'{stage}.wal').exists():
            raise RuntimeError('Audit output did not checkpoint')
        with duckdb.connect(str(stage), read_only=True) as connection:
            summaries = connection.execute('SELECT * FROM comparison_inventory ORDER BY comparison, rows DESC, status').fetchall()
            if not summaries:
                raise RuntimeError('No comparison results survived reopening')
        for name, path in database_paths.items():
            if FileIdentity.read(path) != identities[name] or Path(f'{path}.wal').exists():
                raise RuntimeError(f'Input changed during audit: {path}')
        os.link(stage, output)
    for relation, status, count in summaries:
        print(f'{relation}: {status}: {count:,}')
    print(f'Local Basketball-Reference comparison: {output}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT/'data/nba.duckdb')
    parser.add_argument('--curated', type=Path, default=ROOT/'data/nba-clean-v2.duckdb')
    parser.add_argument('--output', type=Path, default=ROOT/'data/nba-bref-validation-v2.duckdb')
    args = parser.parse_args()
    run(args.source, args.curated, args.output.absolute())


if __name__ == '__main__':
    main()
