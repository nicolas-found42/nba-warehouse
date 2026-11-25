"""Fingerprint every legacy database file before it is retired.

For each file: size and SHA-256 of the bytes; for each table/view inside it: row count and an
order-independent content hash. Results go to audit.legacy_release_fingerprint in the warehouse,
which is the permanent record of what each release contained (and lets anyone prove that a
rebuilt file matches a retired one).

  uv run --no-project --with duckdb==2.0.0 python warehouse/fingerprint_legacy.py --stage FILE.duckdb
  uv run --no-project --with duckdb==2.0.0 python warehouse/fingerprint_legacy.py --load FILE.duckdb
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import re
from pathlib import Path

import duckdb

from build_warehouse import DATA, DEFAULT_OUT, connect, lit, q


def files() -> list[Path]:
    key = lambda s: [int(x) if x.isdigit() else x for x in re.split(r'(\d+)', s)]
    return [Path(p) for p in sorted(glob.glob(str(DATA / '*.duckdb')), key=key)
            if not Path(p).name.startswith(('nba-warehouse', 'legacy-fingerprints'))]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 24), b''):
            h.update(chunk)
    return h.hexdigest()


def stage(out: Path) -> None:
    s = duckdb.connect(str(out))
    s.execute('''CREATE TABLE IF NOT EXISTS legacy_file (file_name VARCHAR PRIMARY KEY, bytes BIGINT, sha256 VARCHAR)''')
    s.execute('''CREATE TABLE IF NOT EXISTS legacy_relation (file_name VARCHAR, schema_name VARCHAR, relation_name VARCHAR,
        kind VARCHAR, row_count BIGINT, content_hash HUGEINT, column_list VARCHAR, PRIMARY KEY (file_name, schema_name, relation_name))''')
    done = {r[0] for r in s.execute('SELECT file_name FROM legacy_file').fetchall()}
    for p in files():
        if p.name in done:
            continue
        c = duckdb.connect(str(p), read_only=True, config={'threads': 8, 'memory_limit': '16GB'})
        rels = c.execute("""SELECT schema_name, table_name, 'table' FROM duckdb_tables() WHERE database_name = current_database()
            UNION ALL SELECT schema_name, view_name, 'view' FROM duckdb_views() WHERE database_name = current_database() AND NOT internal
            ORDER BY 1, 2""").fetchall()
        rows = []
        for sch, rel, kind in rels:
            cols = ','.join(r[0] for r in c.execute(f"SELECT column_name FROM duckdb_columns() WHERE database_name=current_database() AND schema_name={lit(sch)} AND table_name={lit(rel)} ORDER BY column_index").fetchall())
            try:
                n, h = c.execute(f'SELECT count(*), coalesce(sum(hash(x)::HUGEINT), 0) FROM {q(sch)}.{q(rel)} x').fetchone()
            except duckdb.Error:
                n, h = None, None  # a view that does not bind in this file
            rows.append((p.name, sch, rel, kind, n, h, cols))
        c.close()
        s.executemany('INSERT INTO legacy_relation VALUES (?,?,?,?,?,?,?)', rows)
        s.execute('INSERT INTO legacy_file VALUES (?,?,?)', [p.name, p.stat().st_size, sha256(p)])
        print(p.name, len(rows), flush=True)
    s.close()


def load(staged: Path, warehouse: Path) -> None:
    c = connect(warehouse)
    c.execute(f'ATTACH {lit(str(staged))} AS fp (READ_ONLY)')
    c.execute('CREATE OR REPLACE TABLE audit.legacy_release_file AS SELECT * FROM fp.legacy_file ORDER BY file_name')
    c.execute('CREATE OR REPLACE TABLE audit.legacy_release_fingerprint AS SELECT * FROM fp.legacy_relation ORDER BY ALL')
    c.execute('DETACH fp')
    c.execute('CHECKPOINT')
    print(c.execute('SELECT count(*), sum(bytes) FROM audit.legacy_release_file').fetchone())


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stage', type=Path)
    ap.add_argument('--load', type=Path)
    ap.add_argument('--warehouse', type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    if a.stage:
        stage(a.stage)
    if a.load:
        load(a.load, a.warehouse)
