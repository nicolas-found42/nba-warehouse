"""Bounded local pipeline history, independent of the warehouse's DuckDB connection."""
from __future__ import annotations

import datetime as dt
import errno
import hashlib
import json
import os
import re
import sqlite3
import socket
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError

SCHEMA_VERSION = 1
RETENTION_DAYS = 30
MAX_EVENTS = 50_000
MAX_PAGES = 12_288  # 48 MiB at the fixed 4096-byte page size
BUSY_SECONDS = 0.05
CONTEXT_ENV = 'NBA_WAREHOUSE_TRACE'
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGET = ROOT / 'data' / 'nba-warehouse.duckdb'
JOB_NAMES = ('daily_refresh', 'chase_gaps', 'status_page')
CURRENT: ContextVar[Span | None] = ContextVar('warehouse_observation', default=None)
ID = re.compile(r'[a-f0-9]{32}')
REVISION = re.compile(r'[a-f0-9]{40}')
LABEL = re.compile(r'[a-z0-9_][a-z0-9_.-]{0,79}')
DATE = re.compile(r'\d{4}-\d{2}-\d{2}', re.ASCII)
ASSET = re.compile(r'sdv-\d{4}-\d{4}-\d{2}-\d{2}T\d{6}\.\d{6}Z-for-\d{4}-\d{2}-\d{2}', re.ASCII)
GAME = re.compile(r'\d{10}', re.ASCII)
SOURCE_ID = re.compile(r'\d{1,12}', re.ASCII)
CHECK = re.compile(r'[a-zA-Z0-9_ ]{1,80}', re.ASCII)
FIELDS = {
    **dict.fromkeys(('count', 'errors', 'queued', 'rows', 'refs', 'reports', 'rosters', 'schedules',
                    'retries', 'wait_ms', 'exit_code', 'http_status', 'os_errno', 'dropped',
                    'sample_rows', 'assets', 'cache_hits', 'misses', 'requests'), int),
    **dict.fromkeys(('force', 'dirty', 'cached', 'stale', 'read_only', 'sample_limited'), bool),
    **dict.fromkeys(('cutoff', 'previous_cutoff', 'availability_date'), DATE),
    **dict.fromkeys(('reason', 'trigger', 'queue', 'dependency', 'role', 'source', 'decision'), LABEL),
    'asset_set': ASSET,
    'asset_updated_at': re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{6})?\+00:00', re.ASCII), 'game_id': GAME, 'source_id': SOURCE_ID, 'check': CHECK,
    'python_version': re.compile(r'\d{1,2}\.\d{1,2}\.\d{1,3}', re.ASCII),
    'duckdb_version': re.compile(r'\d{1,2}\.\d{1,2}\.\d{1,3}', re.ASCII),
    'revision': REVISION, 'sha256': re.compile(r'[a-f0-9]{64}'),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, at_us INTEGER NOT NULL,
    run_id TEXT NOT NULL, span_id TEXT NOT NULL, parent_span_id TEXT, pid INTEGER NOT NULL,
    workflow TEXT NOT NULL, phase TEXT NOT NULL, outcome TEXT NOT NULL, duration_ms INTEGER,
    cutoff TEXT, game_id TEXT, details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_at ON events(at_us);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id, event_id);
CREATE INDEX IF NOT EXISTS events_game ON events(game_id, event_id);
CREATE TABLE IF NOT EXISTS retention (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), time_evicted INTEGER NOT NULL DEFAULT 0,
    volume_evicted INTEGER NOT NULL DEFAULT 0, reported_dropped INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO retention(singleton) VALUES (1);
"""


def clean(fields: dict) -> dict:
    result = {}
    for key, value in fields.items():
        rule = FIELDS.get(key)
        if rule is bool:
            if type(value) is bool:
                result[key] = value
        elif rule is int:
            if type(value) is int and -(2**31) < value < 2**31:
                result[key] = value
        elif rule and isinstance(value, str) and rule.fullmatch(value):
            result[key] = value
    return result


def error_details(error: BaseException) -> dict:
    """Capture typed, bounded metadata. Exception messages, argv and payloads are excluded."""
    if isinstance(error, HTTPError):
        return {'reason': 'http_error', 'http_status': error.code}
    if isinstance(error, URLError):
        if isinstance(error.reason, (TimeoutError, OSError)):
            return error_details(error.reason)
        return {'reason': 'transport_error'}
    if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
        return {'reason': 'timeout'}
    if isinstance(error, subprocess.CalledProcessError):
        return {'reason': 'command_exit', 'exit_code': error.returncode}
    if isinstance(error, socket.gaierror):
        return {'reason': 'dns_resolution', 'os_errno': error.errno}
    if isinstance(error, OSError):
        names = {errno.ENOENT: 'not_found', errno.EACCES: 'permission_denied', errno.EPERM: 'permission_denied',
                 errno.ENOSPC: 'storage_full', errno.EAGAIN: 'busy', errno.ECONNRESET: 'connection_reset'}
        return {'reason': names.get(error.errno, 'os_error'), 'os_errno': error.errno}
    if isinstance(error, SystemExit):
        return {'reason': 'command_exit', 'exit_code': error.code if type(error.code) is int else int(error.code is not None)}
    if isinstance(error, KeyboardInterrupt):
        return {'reason': 'interrupted'}
    if isinstance(error, ValueError):
        return {'reason': 'validation_error'}
    if type(error).__module__ in ('_duckdb', 'duckdb'):
        names = {'IOException': 'duckdb_io', 'ConstraintException': 'duckdb_constraint',
                 'CatalogException': 'duckdb_catalog', 'BinderException': 'duckdb_binder'}
        return {'reason': names.get(type(error).__name__, 'duckdb_error')}
    return {'reason': 'unexpected_error'}


def store_path(target: Path) -> Path:
    target = target.resolve()
    key = hashlib.sha256(os.fsencode(target)).hexdigest()[:24]
    return target.parent / 'observability' / f'{key}.sqlite3'


def code_context() -> dict:
    try:
        deployed = ROOT / '.warehouse-revision'
        if deployed.exists():
            revision, dirty = deployed.read_text().split()
            return clean({'revision': revision, 'dirty': dirty == 'dirty'})
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True,
                                  text=True, timeout=0.3, check=True).stdout.strip()
        dirty = subprocess.run(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=ROOT,
                               capture_output=True, timeout=0.3, check=True).stdout
        return clean({'revision': revision, 'dirty': bool(dirty)})
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


@dataclass
class Trace:
    target: Path | None
    workflow: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    context: dict = field(default_factory=dict)
    connection: sqlite3.Connection | None = None
    dropped: int = 0
    warned: bool = False
    retry_at: float = 0
    purge_at: float = 0

    def open(self) -> sqlite3.Connection:
        path = store_path(self.target)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        c = sqlite3.connect(path, timeout=BUSY_SECONDS)
        try:
            version = c.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                c.execute('PRAGMA page_size=4096')
                c.executescript(SCHEMA)
                c.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            elif version != SCHEMA_VERSION:
                raise sqlite3.DatabaseError('unsupported history schema')
            c.execute(f'PRAGMA max_page_count={MAX_PAGES}')
            c.execute('PRAGMA synchronous=FULL')
        except sqlite3.Error:
            c.close()
            raise
        return c

    def emit(self, span: Span, outcome: str, details: dict, elapsed: int | None) -> None:
        if self.target is None:
            return
        if time.monotonic() < self.retry_at:
            self.dropped += 1
            return
        try:
            if self.connection is None:
                self.connection = self.open()
            at = time.time_ns() // 1000
            merged = self.context | span.fields | clean(details)
            if self.dropped:
                merged['dropped'] = self.dropped
            c = self.connection
            purge = time.monotonic() >= self.purge_at
            with c:
                row = c.execute('''INSERT INTO events
                    (at_us, run_id, span_id, parent_span_id, pid, workflow, phase, outcome,
                     duration_ms, cutoff, game_id, details) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (at, self.run_id, span.span_id, span.parent, os.getpid(), self.workflow, span.phase,
                     outcome, elapsed, merged.get('cutoff'), merged.get('game_id'), json.dumps(merged))).lastrowid
                evicted = c.execute('DELETE FROM events WHERE event_id <= ?', (row - MAX_EVENTS,)).rowcount
                expired = 0
                if purge:
                    expired = c.execute('DELETE FROM events WHERE at_us < ?',
                                        (at - RETENTION_DAYS * 86_400_000_000,)).rowcount
                if evicted or expired or self.dropped:
                    c.execute('''UPDATE retention SET time_evicted=time_evicted+?,
                        volume_evicted=volume_evicted+?, reported_dropped=reported_dropped+?''',
                        (expired, evicted, self.dropped))
            if purge:
                self.purge_at = time.monotonic() + 60
            self.dropped = 0
        except (OSError, RuntimeError, sqlite3.Error):
            self.dropped += 1
            self.retry_at = time.monotonic() + 1
            if not self.warned:
                self.warned = True
                try:
                    print(f'warehouse telemetry unavailable (run {self.run_id}); events may be lost',
                          file=sys.stderr, flush=True)
                except OSError:
                    pass


@dataclass
class Span:
    trace: Trace
    phase: str
    parent: str | None = None
    fields: dict = field(default_factory=dict)
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    outcome: str = 'succeeded'
    result: dict = field(default_factory=dict)

    def child_env(self) -> dict:
        context = {'target': str(self.trace.target), 'workflow': self.trace.workflow,
                   'run_id': self.trace.run_id, 'parent': self.span_id, 'fields': self.trace.context | self.fields}
        return os.environ | {CONTEXT_ENV: json.dumps(context)}


@contextmanager
def observe(span: Span):
    start = time.monotonic_ns()
    token = CURRENT.set(span)
    try:
        span.trace.emit(span, 'started', {}, None)
        try:
            yield span
        except BaseException as error:
            if span.outcome == 'succeeded':
                if isinstance(error, KeyboardInterrupt):
                    span.outcome = 'cancelled'
                elif not isinstance(error, SystemExit) or error.code not in (0, None):
                    span.outcome = 'failed'
            span.result = error_details(error) | span.result
            raise
        finally:
            span.trace.emit(span, span.outcome, span.result, (time.monotonic_ns() - start) // 1_000_000)
    finally:
        CURRENT.reset(token)


@contextmanager
def operation(workflow: str, target: Path, phase: str = 'run', *, inherit: bool = True, **fields):
    # Inheritance is only used by cooperating launcher/child entry points. No command line is retained.
    raw = os.environ.get(CONTEXT_ENV) if inherit else None
    parent = None
    trace = None
    if raw and len(raw) < 4096:
        try:
            ctx = json.loads(raw)
            if (ID.fullmatch(ctx['run_id']) and ID.fullmatch(ctx['parent'])
                    and LABEL.fullmatch(ctx['workflow']) and Path(ctx['target']).is_absolute()):
                trace = Trace(Path(ctx['target']), ctx['workflow'], ctx['run_id'], clean(ctx['fields']))
                parent = ctx['parent']
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
    if trace is None:
        # Absolute lexical path here; resolving and touching the sidecar are both best effort in emit().
        trace = Trace(target.absolute(), workflow, context=code_context() | {'trigger': 'manual'})
    trace.context.update(clean(fields | {
        'python_version': '.'.join(map(str, sys.version_info[:3])),
        'duckdb_version': getattr(sys.modules.get('duckdb'), '__version__', None),
    }))
    try:
        with observe(Span(trace, phase, parent)) as span:
            yield span
    finally:
        if trace.connection is not None:
            try:
                trace.connection.close()
            except sqlite3.Error:
                pass


@contextmanager
def stage(phase: str, **fields):
    active = CURRENT.get()
    if active is None:
        yield Span(Trace(None, ''), phase)
    else:
        with observe(Span(active.trace, phase, active.span_id, active.fields | clean(fields))) as span:
            yield span


def event(phase: str, outcome: str, **fields) -> None:
    active = CURRENT.get()
    if active is not None:
        active.trace.emit(Span(active.trace, phase, active.span_id, active.fields), outcome, fields, None)
