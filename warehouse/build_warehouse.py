"""Build and verify the single NBA warehouse file.

One DuckDB file, four schemas:
  raw      every imported table exactly as received (original snapshot + Kaggle), never edited
  core     verified, matched records (historical release v26 + current-season snapshot)
  audit    receipts, holds, disputes, legacy release fingerprints, build ledger
  betting  odds linked to core games (see betting.py); nothing computed from them

Usage:
  uv run --no-project --with duckdb==2.0.0 python warehouse/build_warehouse.py build
  uv run --no-project --with duckdb==2.0.0 python warehouse/build_warehouse.py verify
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
DEFAULT_OUT = DATA / 'nba-warehouse.duckdb'
RAW_SRC = DATA / 'nba.duckdb'
HIST_SRC = DATA / 'nba-clean-v26.duckdb'
CURRENT_SRC = DATA / 'nba-2026-27-local-2026-11-23.duckdb'
CURRENT_SEASON = '2026-27'  # season_year of the current-season snapshot
# Small one-off check databases whose tables are kept in audit under a prefix.
CHECK_SRCS = {
    'bref_validation_v2': DATA / 'nba-bref-validation-v2.duckdb',
    'bref_validation_v3': DATA / 'nba-bref-validation-v3.duckdb',
    'results_check_2026_11_18': DATA / 'nba-2026-27-results-check-2026-11-18.duckdb',
    'results_check_2026_11_19': DATA / 'nba-2026-27-results-check-2026-11-19.duckdb',
    'box_check_2026_11_19': DATA / 'nba-2026-27-box-check-2026-11-19.duckdb',
}
CURRENT_PREFIX = 'season_2027__'


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def connect(path: Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    tmp = DATA / '.duckdb_tmp'
    tmp.mkdir(exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only, config={
        'threads': max(2, (os.cpu_count() or 4) - 4), 'memory_limit': '24GB',
        'temp_directory': str(tmp), 'preserve_insertion_order': True})


from contextlib import contextmanager


@contextmanager
def warehouse_lock():
    """Exclusive lock for writers of the live warehouse (daily refresh swaps the file; gap chasing appends)."""
    import fcntl
    with (DATA / '.warehouse.lock').open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def attach_sources(c: duckdb.DuckDBPyConnection) -> None:
    c.execute(f'ATTACH {lit(str(RAW_SRC))} AS src_raw (READ_ONLY)')
    c.execute(f'ATTACH {lit(str(HIST_SRC))} AS src_hist (READ_ONLY)')
    c.execute(f'ATTACH {lit(str(CURRENT_SRC))} AS src_cur (READ_ONLY)')
    for alias, p in CHECK_SRCS.items():
        c.execute(f'ATTACH {lit(str(p))} AS {q("chk_" + alias)} (READ_ONLY)')


def tables(c, db: str, schema: str | None = None) -> list[tuple[str, str]]:
    cond = f" AND schema_name={lit(schema)}" if schema else ''
    return c.execute(f"SELECT schema_name, table_name FROM duckdb_tables() WHERE database_name={lit(db)}{cond} ORDER BY 1,2").fetchall()


def views(c, db: str, schema: str | None = None) -> list[tuple[str, str, str]]:
    cond = f" AND schema_name={lit(schema)}" if schema else ''
    return c.execute(f"SELECT schema_name, view_name, sql FROM duckdb_views() WHERE database_name={lit(db)} AND NOT internal{cond} ORDER BY 1,2").fetchall()


def create_views(c, items: list[tuple[str, str]], tolerate: bool = False) -> list[tuple[str, str, str]]:
    """Create views in dependency order by retrying until no progress.
    With tolerate=True, views that cannot bind are returned (name, sql, error) instead of raising."""
    pending = list(items)
    while pending:
        failed, errs = [], []
        for name, sql in pending:
            try:
                c.execute(sql)
            except duckdb.CatalogException as e:  # depends on a view not yet created
                failed.append((name, sql)); errs.append(str(e).splitlines()[0])
        if len(failed) == len(pending):
            if tolerate:
                return [(n, s, e) for (n, s), e in zip(failed, errs)]
            raise RuntimeError('Cannot create views:\n' + '\n'.join(errs[:10]))
        pending = failed
    return []


def rewrite_sql(sql: str, objects: set[str], old: str, new: str) -> str:
    """Point view/table SQL at a new schema name. Only references to known object names are rewritten."""
    def sub(m: re.Match) -> str:
        return (new + '.' if m.group(2).lower() in objects else m.group(1)) + m.group(2)
    return re.sub(r'\b(' + old + r'\.)([A-Za-z_][A-Za-z0-9_]*)', sub, sql)


def rewrite_hist_sql(sql: str, nba_objects: set[str]) -> str:
    return rewrite_sql(sql, nba_objects, 'nba', 'core')


def ordered_tables(c, db: str, schema: str) -> list[tuple[str, str]]:
    """(name, CREATE TABLE sql) in foreign-key dependency order (parents first)."""
    rows = c.execute(f"SELECT table_name, sql FROM duckdb_tables() WHERE database_name={lit(db)} AND schema_name={lit(schema)} ORDER BY 1").fetchall()
    deps = {t: set() for t, _ in rows}
    for t, ref in c.execute(f"""SELECT table_name, referenced_table FROM duckdb_constraints()
            WHERE database_name={lit(db)} AND schema_name={lit(schema)} AND constraint_type='FOREIGN KEY'""").fetchall():
        if ref != t:
            deps[t].add(ref)
    out, done = [], set()
    while len(done) < len(rows):
        ready = [(t, sql) for t, sql in rows if t not in done and deps[t] <= done]
        if not ready:
            raise RuntimeError(f'foreign-key cycle in {db}.{schema}')
        for t, sql in ready:
            out.append((t, sql)); done.add(t)
    return out


def create_macros(c, db: str, schema: str, target: str) -> None:
    rows = c.execute(f"""SELECT function_name, parameters, macro_definition FROM duckdb_functions()
        WHERE database_name={lit(db)} AND schema_name={lit(schema)} AND NOT internal AND function_type='macro'""").fetchall()
    create_views(c, [(n, f'CREATE MACRO {target}.{q(n)}({", ".join(p)}) AS {d}') for n, p, d in rows])


def build(out: Path) -> None:
    tmp = out.with_name(out.stem + '_building.duckdb')
    for f in (tmp, Path(str(tmp) + '.wal')):
        if f.exists():
            f.unlink()
    c = connect(tmp)
    db = c.execute('SELECT current_database()').fetchone()[0]
    attach_sources(c)
    for s in ('raw', 'core', 'audit', 'betting'):
        c.execute(f'CREATE SCHEMA {s}')
    started = dt.datetime.now(dt.timezone.utc)

    # --- raw: the original snapshot, untouched (same DDL, constraints, macros, views) ---
    c.execute(f'USE {q(db)}.raw')
    for (sql,) in c.execute("SELECT sql FROM duckdb_sequences() WHERE database_name='src_raw'").fetchall():
        c.execute(sql)  # START already equals the source's next value
    create_macros(c, 'src_raw', 'main', 'raw')
    for t, sql in ordered_tables(c, 'src_raw', 'main'):
        c.execute(sql)
        c.execute(f'INSERT INTO raw.{q(t)} SELECT * FROM src_raw.main.{q(t)}')
        print('raw', t, flush=True)
    for (sql,) in c.execute("SELECT sql FROM duckdb_indexes() WHERE database_name='src_raw' AND sql IS NOT NULL").fetchall():
        c.execute(sql)
    raw_objects = {t.lower() for _, t in tables(c, 'src_raw', 'main')} | {n.lower() for _, n, _ in views(c, 'src_raw', 'main')}
    unbound = create_views(c, [(n, rewrite_sql(sql, raw_objects, 'main', 'raw')) for _, n, sql in views(c, 'src_raw', 'main')], tolerate=True)
    # Views that do not bind in the source either (e.g. reference a schema that never existed) are kept as text.
    c.execute('CREATE TABLE raw._unbindable_view (view_name VARCHAR PRIMARY KEY, sql VARCHAR, error VARCHAR)')
    c.executemany('INSERT INTO raw._unbindable_view VALUES (?,?,?)', unbound)
    c.execute(f'USE {q(db)}.main')

    # --- core: v26 historical release + current-season snapshot -----------------------
    nba_objects = {t.lower() for _, t in tables(c, 'src_hist', 'nba')} | {n.lower() for _, n, _ in views(c, 'src_hist', 'nba')}
    cur_tables = {t for _, t in tables(c, 'src_cur', 'nba')}
    create_macros(c, 'src_hist', 'nba', 'core')
    for t, sql in ordered_tables(c, 'src_hist', 'nba'):
        c.execute(rewrite_hist_sql(sql, nba_objects))
        c.execute(f'INSERT INTO core.{q(t)} SELECT * FROM src_hist.nba.{q(t)}')
        if t in cur_tables:
            key = {'dim_person': 'person_id', 'dim_team': 'team_id'}.get(t)
            anti = f' ANTI JOIN src_hist.nba.{q(t)} h USING ({key})' if key else ''
            c.execute(f'INSERT INTO core.{q(t)} SELECT c.* FROM src_cur.nba.{q(t)} c{anti}')
        print('core', t, flush=True)
    # Current standings are a dated snapshot, never a completed season.
    c.execute("ALTER TABLE core.fact_regular_standings ADD COLUMN snapshot_kind VARCHAR DEFAULT 'completed_season'")
    c.execute('ALTER TABLE core.fact_regular_standings ADD COLUMN as_of_date DATE')
    cutoff = c.execute('SELECT cutoff FROM src_cur.audit.scope').fetchone()[0]
    c.execute(f"UPDATE core.fact_regular_standings SET snapshot_kind='as_of', as_of_date=DATE {lit(str(cutoff))} WHERE season_year={lit(CURRENT_SEASON)}")

    # --- audit: v26 ledgers, current-season ledgers, one-off check databases -----------
    create_macros(c, 'src_hist', 'audit', 'audit')
    for t, sql in ordered_tables(c, 'src_hist', 'audit'):
        c.execute(rewrite_hist_sql(sql, nba_objects))
        c.execute(f'INSERT INTO audit.{q(t)} SELECT * FROM src_hist.audit.{q(t)}')
    # core and audit views reference each other, so create them together once all tables exist
    create_views(c, [(n, rewrite_hist_sql(sql, nba_objects)) for s, n, sql in views(c, 'src_hist', None) if s in ('nba', 'audit')])
    for _, t in tables(c, 'src_cur', 'audit'):
        c.execute(f'CREATE TABLE audit.{q(CURRENT_PREFIX + t)} AS SELECT * FROM src_cur.audit.{q(t)}')
    for alias in CHECK_SRCS:
        for s, t in tables(c, 'chk_' + alias):
            c.execute(f'CREATE TABLE audit.{q("check_" + alias + "__" + t)} AS SELECT * FROM {q("chk_" + alias)}.{q(s)}.{q(t)}')
    print('audit done', flush=True)

    # --- build ledger -----------------------------------------------------------------
    c.execute('''CREATE TABLE audit.warehouse_build (
        built_at TIMESTAMPTZ, step VARCHAR, source_path VARCHAR, source_bytes BIGINT, note VARCHAR)''')
    srcs = [('raw', RAW_SRC), ('core+audit', HIST_SRC), ('core+audit current season', CURRENT_SRC)] + \
           [('audit check', p) for p in CHECK_SRCS.values()]
    for step, p in srcs:
        c.execute('INSERT INTO audit.warehouse_build VALUES (?,?,?,?,?)',
                  [started, step, str(p.relative_to(ROOT)), p.stat().st_size, None])
    c.execute('CHECKPOINT')
    c.close()
    os.replace(tmp, out)
    print('built', out)


# ------------------------------------------------------------------------------- verify
def fingerprint(c, rel: str) -> tuple[int, int]:
    """Row count and order-independent content hash of a relation."""
    n, h = c.execute(f'SELECT count(*), coalesce(sum(hash(x)::HUGEINT), 0) FROM {rel} AS x').fetchone()
    return int(n), int(h)


def verify(out: Path) -> bool:
    c = connect(out, read_only=True)
    attach_sources(c)
    db = q(c.execute('SELECT current_database()').fetchone()[0])
    fails: list[str] = []
    checks = 0

    def same(label: str, a: str, b: str) -> None:
        nonlocal checks
        checks += 1
        fa, fb = fingerprint(c, a), fingerprint(c, b)
        if fa != fb:
            fails.append(f'{label}: {a}={fa} vs {b}={fb}')

    # raw is a byte-for-byte relation copy of the original snapshot
    for _, t in tables(c, 'src_raw', 'main'):
        same(f'raw.{t}', f'{db}.raw.{q(t)}', f'src_raw.main.{q(t)}')
    unbound = {r[0] for r in c.execute(f'SELECT view_name FROM {db}.raw._unbindable_view').fetchall()}
    for _, n, _ in views(c, 'src_raw', 'main'):
        if n in unbound:
            checks += 1
            try:  # must be unbindable in the source too, otherwise we lost a working view
                c.execute(f'SELECT * FROM src_raw.main.{q(n)} LIMIT 0')
                fails.append(f'raw view {n} works in source but was not recreated')
            except duckdb.Error:
                pass
            continue
        same(f'raw view {n}', f'{db}.raw.{q(n)}', f'src_raw.main.{q(n)}')

    # sequences, indexes, macros and constraints survived the copy
    for kind, src_db, schema in [('duckdb_sequences()', 'src_raw', 'main'), ('duckdb_indexes()', 'src_raw', 'main'),
                                 ('duckdb_constraints()', 'src_raw', 'main'), ('duckdb_constraints()', 'src_hist', 'nba'),
                                 ('duckdb_constraints()', 'src_hist', 'audit')]:
        target = {'main': 'raw', 'nba': 'core', 'audit': 'audit'}[schema]
        checks += 1
        a = c.execute(f"SELECT count(*) FROM {kind} WHERE database_name={lit(src_db)} AND schema_name={lit(schema)}").fetchone()[0]
        b = c.execute(f"SELECT count(*) FROM {kind} WHERE database_name={lit(db.strip(chr(34)))} AND schema_name={lit(target)} {" AND table_name <> '_unbindable_view'" if 'constraints' in kind else ''}").fetchone()[0]
        if kind == 'duckdb_constraints()' and schema == 'audit':
            b = c.execute(f"SELECT count(*) FROM {kind} WHERE database_name={lit(db.strip(chr(34)))} AND schema_name='audit' AND table_name IN (SELECT table_name FROM duckdb_tables() WHERE database_name='src_hist' AND schema_name='audit')").fetchone()[0]
        if a != b:
            fails.append(f'{kind} {src_db}.{schema}: {a} in source vs {b} in {target}')
    for src_db, schema, target in [('src_raw', 'main', 'raw'), ('src_hist', 'nba', 'core'), ('src_hist', 'audit', 'audit')]:
        checks += 1
        a = c.execute(f"SELECT count(*) FROM duckdb_functions() WHERE database_name={lit(src_db)} AND schema_name={lit(schema)} AND NOT internal").fetchone()[0]
        b = c.execute(f"SELECT count(*) FROM duckdb_functions() WHERE database_name={lit(db.strip(chr(34)))} AND schema_name={lit(target)} AND NOT internal").fetchone()[0]
        if a != b:
            fails.append(f'macros {src_db}.{schema}: {a} vs {b}')

    # core = v26 + current season, per table (multiset)
    cur_tables = {t for _, t in tables(c, 'src_cur', 'nba')}
    for _, t in tables(c, 'src_hist', 'nba'):
        cols = [r[0] for r in c.execute(f"SELECT column_name FROM duckdb_columns() WHERE database_name='src_hist' AND schema_name='nba' AND table_name={lit(t)} ORDER BY column_index").fetchall()]
        sel = ', '.join(q(x) for x in cols)
        expect = f'(SELECT {sel} FROM src_hist.nba.{q(t)}'
        if t in cur_tables:
            key = {'dim_person': 'person_id', 'dim_team': 'team_id'}.get(t)
            if key:
                expect += f' UNION ALL SELECT {sel} FROM src_cur.nba.{q(t)} WHERE {key} NOT IN (SELECT {key} FROM src_hist.nba.{q(t)})'
            else:
                expect += f' UNION ALL SELECT {sel} FROM src_cur.nba.{q(t)}'
        expect += ')'
        same(f'core.{t}', f'(SELECT {sel} FROM {db}.core.{q(t)})', expect)
    # every v26 view returns exactly its v26 rows when restricted to historical games
    for _, n, _ in views(c, 'src_hist', 'nba'):
        has_game = c.execute(f"SELECT count(*) FROM duckdb_columns() WHERE database_name='src_hist' AND schema_name='nba' AND table_name={lit(n)} AND column_name='game_id'").fetchone()[0]
        flt = ' WHERE game_id IN (SELECT game_id FROM src_hist.nba.dim_game)' if has_game else ''
        same(f'core view {n}', f'(SELECT * FROM {db}.core.{q(n)}{flt})', f'src_hist.nba.{q(n)}')
    # current-season standings carry their cutoff
    checks += 1
    bad = c.execute(f"SELECT count(*) FROM {db}.core.fact_regular_standings WHERE (season_year={lit(CURRENT_SEASON)}) <> (as_of_date IS NOT NULL)").fetchone()[0]
    if bad:
        fails.append(f'{bad} standings rows with wrong snapshot labelling')

    # audit copies
    for _, t in tables(c, 'src_hist', 'audit'):
        same(f'audit.{t}', f'{db}.audit.{q(t)}', f'src_hist.audit.{q(t)}')
    # audit views summarise core, which now also holds the current season: compare historical rows
    for _, n, _ in views(c, 'src_hist', 'audit'):
        colset = {r[0] for r in c.execute(f"SELECT column_name FROM duckdb_columns() WHERE database_name='src_hist' AND schema_name='audit' AND table_name={lit(n)}").fetchall()}
        flt = (' WHERE game_id IN (SELECT game_id FROM src_hist.nba.dim_game)' if 'game_id' in colset else
               f' WHERE season_year <> {lit(CURRENT_SEASON)}' if 'season_year' in colset else '')
        same(f'audit view {n}', f'(SELECT * FROM {db}.audit.{q(n)}{flt})', f'src_hist.audit.{q(n)}')
    for _, t in tables(c, 'src_cur', 'audit'):
        same(f'audit current {t}', f'{db}.audit.{q(CURRENT_PREFIX + t)}', f'src_cur.audit.{q(t)}')
    for alias in CHECK_SRCS:
        for s, t in tables(c, 'chk_' + alias):
            same(f'audit check {alias}.{t}', f'{db}.audit.{q("check_" + alias + "__" + t)}', f'{q("chk_" + alias)}.{q(s)}.{q(t)}')

    # referential integrity inside core
    for t in ('fact_game_result', 'fact_game_period_score', 'fact_team_game_stats', 'fact_player_game_stats', 'fact_play_event', 'fact_shot', 'bridge_game_team'):
        checks += 1
        n = c.execute(f'SELECT count(*) FROM {db}.core.{t} f ANTI JOIN {db}.core.dim_game g USING (game_id)').fetchone()[0]
        if n:
            fails.append(f'core.{t}: {n} rows with game_id not in core.dim_game')
    for t in ('dim_game', 'dim_person', 'dim_team'):
        checks += 1
        key = {'dim_game': 'game_id', 'dim_person': 'person_id', 'dim_team': 'team_id'}[t]
        n = c.execute(f'SELECT count(*) - count(DISTINCT {key}) FROM {db}.core.{t}').fetchone()[0]
        if n:
            fails.append(f'core.{t}: {n} duplicate {key}')

    print(f'{checks} checks, {len(fails)} failures')
    for f in fails:
        print('FAIL', f)
    c.close()
    return not fails


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['build', 'verify'])
    p.add_argument('--output', type=Path, default=DEFAULT_OUT)
    a = p.parse_args()
    if a.command == 'build':
        if a.output.exists():
            sys.exit(f'{a.output} exists; refusing to overwrite')
        build(a.output)
    else:
        sys.exit(0 if verify(a.output) else 1)


if __name__ == '__main__':
    main()
