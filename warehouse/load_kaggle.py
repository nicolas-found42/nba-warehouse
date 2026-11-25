"""Load downloaded Kaggle datasets into the warehouse's raw layer (schema raw_kaggle).

Every tabular file becomes one table, loaded exactly as received: CSV/TSV values stay
text (no type guessing, so IDs like 0021900891 keep their leading zeros); Parquet,
JSON and SQLite keep their own types. raw_kaggle._file records every file seen,
including the ones that were not tabular or failed to parse, with the reason.

  uv run --no-project --with duckdb==2.0.0 python warehouse/load_kaggle.py [--only owner/slug ...]

Datasets are downloaded beforehand into data/kaggle/<owner>__<slug>/ (see fetch_kaggle.sh).
Re-running replaces the tables of each dataset it loads.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import re
from pathlib import Path

import duckdb

from build_warehouse import DEFAULT_OUT, ROOT, connect, lit, q

KAGGLE = ROOT / 'data' / 'kaggle'
EXCLUDE = re.compile(r'top-?shot', re.I)  # NFT trading data, not basketball
TABULAR = {'.csv', '.tsv', '.txt', '.parquet', '.parq', '.json', '.jsonl', '.ndjson', '.sqlite', '.db', '.sqlite3', '.xlsx'}


def slug(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def load_dataset(c: duckdb.DuckDBPyConnection, ref: str, d: Path, meta: dict) -> None:
    prefix = slug(ref.replace('/', '__'))[:60]
    for (t,) in c.execute(f"SELECT table_name FROM duckdb_tables() WHERE schema_name='raw_kaggle' AND table_name LIKE {lit(prefix + '__%')}").fetchall():
        c.execute(f'DROP TABLE raw_kaggle.{q(t)}')
    c.execute('DELETE FROM raw_kaggle._file WHERE dataset_ref=?', [ref])
    used: set[str] = set()
    for f in sorted(p for p in d.rglob('*') if p.is_file() and not p.name.startswith('.')):
        rel = str(f.relative_to(d))
        ext = f.suffix.lower()
        status, note, tables_made, rows = 'skipped', 'not tabular', [], 0
        if ext in TABULAR:
            try:
                tables_made, rows = load_file(c, f, ext, prefix, rel, used)
                status, note = ('loaded', None) if tables_made else ('skipped', 'no rows/tables')
            except Exception as e:  # keep going; record why
                status, note = 'failed', f'{type(e).__name__}: {str(e)[:300]}'
        c.execute('INSERT INTO raw_kaggle._file VALUES (?,?,?,?,?,?,?,?,?)',
                  [ref, rel, f.stat().st_size, sha256(f), status, note, tables_made, rows, meta.get('lastUpdated')])
    print(ref, flush=True)


def table_name(prefix: str, rel: str, used: set[str]) -> str:
    base = f'{prefix}__{slug(Path(rel).with_suffix("").as_posix())}'[:110]
    name, i = base, 2
    while name in used:
        name, i = f'{base}_{i}', i + 1
    used.add(name)
    return name


def load_file(c, f: Path, ext: str, prefix: str, rel: str, used: set[str]) -> tuple[list[str], int]:
    p = lit(str(f))
    if ext in {'.sqlite', '.db', '.sqlite3'}:
        c.execute(f'ATTACH {p} AS kag_sqlite (TYPE sqlite, READ_ONLY)')
        try:
            made, rows = [], 0
            for (t,) in c.execute("SELECT table_name FROM duckdb_tables() WHERE database_name='kag_sqlite'").fetchall():
                name = table_name(prefix, f'{rel}/{t}', used)
                c.execute("SET sqlite_all_varchar=true")
                c.execute(f'CREATE TABLE raw_kaggle.{q(name)} AS SELECT * FROM kag_sqlite.{q(t)}')
                made.append(name); rows += c.execute(f'SELECT count(*) FROM raw_kaggle.{q(name)}').fetchone()[0]
            return made, rows
        finally:
            c.execute('DETACH kag_sqlite')
    name = table_name(prefix, rel, used)
    if ext in {'.csv', '.tsv', '.txt'}:
        # strictest reading first; fall back only on parse errors, never by skipping rows
        variants = ["", ", parallel=false", ", parallel=false, encoding='latin-1'", ", parallel=false, delim=';'",
                    ", parallel=false, delim=';', encoding='latin-1'"]
        err = None
        for v in variants:
            src = f"read_csv({p}, all_varchar=true, header=true, sample_size=-1, ignore_errors=false, null_padding=true, strict_mode=false{v})"
            try:
                c.execute(f'CREATE OR REPLACE TABLE raw_kaggle.{q(name)} AS SELECT * FROM {src}')
                err = None
                break
            except Exception as e:
                err = e
        if err is not None:
            used.discard(name)
            raise err
        n = c.execute(f'SELECT count(*) FROM raw_kaggle.{q(name)}').fetchone()[0]
        if n == 0:
            c.execute(f'DROP TABLE raw_kaggle.{q(name)}')
            used.discard(name)
            return [], 0
        return [name], n
    elif ext in ('.parquet', '.parq'):
        src = f'read_parquet({p})'
    elif ext in {'.json', '.jsonl', '.ndjson'}:
        src = f'read_json({p}, maximum_object_size=1073741824)'
    elif ext == '.xlsx':
        src = f'read_xlsx({p}, all_varchar=true)'
    else:
        return [], 0
    c.execute(f'CREATE TABLE raw_kaggle.{q(name)} AS SELECT * FROM {src}')
    n = c.execute(f'SELECT count(*) FROM raw_kaggle.{q(name)}').fetchone()[0]
    if n == 0:
        c.execute(f'DROP TABLE raw_kaggle.{q(name)}')
        used.discard(name)
        return [], 0
    return [name], n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--output', type=Path, default=DEFAULT_OUT)
    ap.add_argument('--only', nargs='*')
    a = ap.parse_args()
    catalog = {r['ref']: r for r in csv.DictReader((KAGGLE / '_catalog.csv').open())}
    c = connect(a.output)
    for ext in ('sqlite', 'excel'):
        c.execute(f'INSTALL {ext}; LOAD {ext}')
    c.execute('CREATE SCHEMA IF NOT EXISTS raw_kaggle')
    c.execute('''CREATE TABLE IF NOT EXISTS raw_kaggle._dataset (dataset_ref VARCHAR PRIMARY KEY, title VARCHAR,
        kaggle_last_updated VARCHAR, votes INTEGER, downloaded_at VARCHAR, local_dir VARCHAR)''')
    c.execute('''CREATE TABLE IF NOT EXISTS raw_kaggle._file (dataset_ref VARCHAR, file_path VARCHAR, bytes BIGINT,
        sha256 VARCHAR, status VARCHAR, note VARCHAR, tables VARCHAR[], row_count BIGINT, kaggle_last_updated VARCHAR)''')
    for d in sorted(KAGGLE.iterdir()):
        if not d.is_dir() or not (d / '.done').exists():
            continue
        ref = d.name.replace('__', '/', 1)
        if a.only and ref not in a.only:
            continue
        if EXCLUDE.search(ref):
            continue
        meta = catalog.get(ref, {})
        c.execute('INSERT OR REPLACE INTO raw_kaggle._dataset VALUES (?,?,?,?,?,?)',
                  [ref, meta.get('title'), meta.get('lastUpdated'), int(meta.get('votes') or 0),
                   (d / '.done').read_text().strip(), str(d.relative_to(ROOT))])
        load_dataset(c, ref, d, meta)
    c.execute('CHECKPOINT')
    print(c.execute("SELECT status, count(*), sum(row_count) FROM raw_kaggle._file GROUP BY 1").fetchall())


if __name__ == '__main__':
    main()
