from __future__ import annotations

import datetime as dt
import hashlib
import json
import select
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
import duckdb
import pytest

import diagnostics
import launch_job
import observability as obs
import refresh_current_season
import status_page

SCRIPTS = Path(__file__).resolve().parents[1]
RANGE = ['--since', '2025-01-01T00:00:00Z', '--until', '2099-01-01T00:00:00Z']


def retrieve(target: Path, *args, expected=0) -> dict:
    result = subprocess.run([sys.executable, str(SCRIPTS / 'diagnostics.py'), '--warehouse',
                             str(target), *RANGE, *args], capture_output=True, text=True, timeout=10)
    assert result.returncode == expected, result.stderr
    return json.loads(result.stdout)


def test_launch_correlates_child_failure_and_redacts(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONPATH', str(SCRIPTS))
    target = tmp_path / 'warehouse.duckdb'
    script = '''
from pathlib import Path
from urllib.error import HTTPError
from observability import operation, stage

with operation('fake_child', Path('/unused')) as run:
    try:
        with stage('dependency', dependency='nba', cutoff='2026-11-25'):
            raise HTTPError('https://example.invalid/?key=private-token', 429, 'private-name', {}, None)
    except HTTPError:
        run.outcome = 'failed'
        run.result['body'] = {'token': 'private-payload'}
        raise SystemExit(9)
'''
    assert launch_job.launch('daily_refresh', [sys.executable, '-c', script], target) == 9
    data = retrieve(target)
    events = data['events']
    assert [r['outcome'] for r in events] == ['started', 'started', 'started', 'failed', 'failed', 'failed']
    assert len({r['run_id'] for r in events}) == 1
    assert len({r['pid'] for r in events}) == 2
    assert events[1]['parent_span_id'] == events[0]['span_id']
    assert events[2]['parent_span_id'] == events[1]['span_id']
    assert events[2]['span_id'] == events[3]['span_id']
    assert events[3]['details']['http_status'] == 429
    assert events[0]['details']['trigger'] == 'manual'
    assert events[5]['details']['exit_code'] == 9
    assert events[5]['duration_ms'] >= 0
    assert 'private-' not in json.dumps(data)
    assert 'unused' not in json.dumps(data)


def test_exit_before_child_entry_is_distinguishable(tmp_path):
    target = tmp_path / 'warehouse.duckdb'
    assert launch_job.launch('daily_refresh', [sys.executable, '-c', 'raise SystemExit(11)'], target,
                             cutoff='2026-11-25', trigger='launchd') == 11
    events = retrieve(target, '--cutoff', '2026-11-25')['events']
    assert [(r['phase'], r['outcome']) for r in events] == [('launch', 'started'), ('launch', 'failed')]
    assert events[-1]['details']['trigger'] == 'launchd'


def test_killed_process_leaves_started_span_and_next_run_survives(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONPATH', str(SCRIPTS))
    target = tmp_path / 'warehouse.duckdb'
    script = '''
import sys
import time
from pathlib import Path
from observability import operation, stage

with operation('crash_case', Path(sys.argv[1])):
    with stage('warehouse_replace'):
        print('ready', flush=True)
        time.sleep(60)
'''
    with subprocess.Popen([sys.executable, '-c', script, str(target)], stdout=subprocess.PIPE) as child:
        try:
            assert select.select([child.stdout], [], [], 5)[0]
            assert child.stdout.readline() == b'ready\n'
            child.kill()
        finally:
            if child.poll() is None:
                child.kill()
    with obs.operation('crash_case', target, inherit=False):
        pass
    data = retrieve(target)
    first, second = sorted({r['run_id'] for r in data['events']},
                           key=lambda run_id: next(r['event_id'] for r in data['events'] if r['run_id'] == run_id))
    assert [(e['phase'], e['outcome']) for e in retrieve(target, '--run-id', first)['events']] == [
        ('run', 'started'), ('warehouse_replace', 'started')]
    assert [e['outcome'] for e in retrieve(target, '--run-id', second)['events']] == ['started', 'succeeded']


def test_inaccessible_store_is_best_effort_and_reports_loss(tmp_path, capsys):
    target = tmp_path / 'warehouse.duckdb'
    with obs.operation('test_workflow', target, inherit=False) as span:
        with sqlite3.connect(obs.store_path(target), timeout=0) as blocking:
            blocking.execute('BEGIN EXCLUSIVE')
            started = time.monotonic()
            obs.event('lock_held', 'failed')
            obs.event('backoff', 'skipped')
            assert time.monotonic() - started < 0.5
        assert span.trace.dropped == 2
        span.trace.retry_at = 0
        obs.event('available_again', 'succeeded')
    data = retrieve(target)
    assert data['retention']['reported_dropped'] == 2
    assert 'lock_held' not in {row['phase'] for row in data['events']}
    assert next(row for row in data['events'] if row['phase'] == 'available_again')['details']['dropped'] == 2
    assert capsys.readouterr().err.count('telemetry unavailable') == 1


def test_missing_and_corrupt_are_distinct_from_empty(tmp_path):
    target = tmp_path / 'warehouse.duckdb'
    assert retrieve(target, expected=2)['query_status'] == 'missing'
    with obs.operation('other', target, inherit=False):
        pass
    data = retrieve(target, '--workflow', 'missing')
    assert data['query_status'] == 'ok' and data['events'] == []
    path = obs.store_path(target)
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text('damaged file')
    assert retrieve(target, expected=2)['query_status'] == 'inaccessible'


def test_pagination_is_scoped_to_highwater(tmp_path):
    target = tmp_path / 'warehouse.duckdb'
    with obs.operation('other', target, inherit=False):
        obs.event('fixture', 'held', game_id='0022600062')
        obs.event('fixture', 'held', game_id='0022600062')
    page = retrieve(target, '--limit', '1', '--game-id', '0022600062')
    cursor = page['pagination']
    assert cursor['truncated']
    with obs.operation('other', target, inherit=False):
        obs.event('fixture', 'held', game_id='0022600062')
    remaining = retrieve(target, '--game-id', '0022600062', '--after', str(cursor['next_after']),
                         '--through', str(cursor['through']))
    assert len(remaining['events']) == 1
    assert remaining['events'][0]['event_id'] <= cursor['through']
    assert not remaining['pagination']['truncated']


def test_time_and_volume_pruning_and_size_cap(tmp_path, monkeypatch):
    target = tmp_path / 'warehouse.duckdb'
    now = dt.datetime(2026, 11, 27, tzinfo=dt.timezone.utc)
    seconds = int(now.timestamp())
    monkeypatch.setattr(obs.time, 'time_ns', lambda: (seconds - 32 * 86_400) * 1_000_000_000)
    with obs.operation('old', target, inherit=False):
        pass
    monkeypatch.setattr(obs.time, 'time_ns', lambda: seconds * 1_000_000_000)
    monkeypatch.setattr(obs, 'MAX_EVENTS', 3)
    with obs.operation('new', target, inherit=False) as span:
        assert span.trace.connection.execute('PRAGMA max_page_count').fetchone() == (obs.MAX_PAGES,)
        obs.event('one', 'succeeded')
        obs.event('two', 'succeeded')
        obs.event('three', 'succeeded')
    data = retrieve(target)
    assert len(data['events']) == 3
    assert all(row['workflow'] == 'new' for row in data['events'])
    assert data['retention']['time_evicted'] == 2
    assert data['retention']['volume_evicted'] == 2


def test_real_release_child_records_stale_asset_and_returns_deferred(tmp_path):
    target = tmp_path / 'warehouse.duckdb'
    assets = tmp_path / 'assets'
    assets.mkdir()
    for i, (role, (tag, stem)) in enumerate(refresh_current_season.ASSETS.items()):
        name = f'{stem}_2027.parquet'
        body = role.encode()
        (assets / name).write_bytes(body)
        (assets / (name + '.asset.json')).write_text(json.dumps(dict(
            name=name, url=f'{refresh_current_season.DOWNLOAD}/{tag}/{name}',
            sha256=hashlib.sha256(body).hexdigest(), asset_id=i,
            published_asset_updated_at='2026-11-24T00:00:00Z' if role == 'play' else '2026-11-26T12:00:00Z')))
    child = [sys.executable, str(SCRIPTS / 'refresh_current_season.py'), 'build',
             '--assets', str(assets), '--cutoff', '2026-11-25', '--output', str(target)]
    assert launch_job.launch('daily_refresh', child, target, cutoff='2026-11-25') == 75
    data = retrieve(target, '--cutoff', '2026-11-25')
    assert [r['phase'] for r in data['events']] == [
        'launch', 'release_validation_run', 'release_freshness', 'release_validation_run', 'launch']
    detail = data['events'][2]['details']
    assert (detail['role'], detail['reason'], detail['sha256']) == (
        'play', 'publisher_asset_stale', hashlib.sha256(b'play').hexdigest())
    assert all(r['outcome'] == 'deferred' for r in data['events'][2:])
    assert not target.exists()


def test_status_recovers_corrupt_history_and_records_stale_view(tmp_path, monkeypatch):
    target = tmp_path / 'warehouse.duckdb'
    out = tmp_path / 'status'
    out.mkdir()
    (out / 'history.json').write_text('{partial')
    monkeypatch.setattr(status_page, 'OUT', out)
    monkeypatch.setattr(status_page, 'DATA', tmp_path)
    monkeypatch.setattr(status_page, 'EVIDENCE', tmp_path)
    monkeypatch.setattr(status_page, 'CACHE', tmp_path)
    monkeypatch.setattr(status_page, 'db_numbers', lambda state: {'stale': True})
    monkeypatch.setattr(status_page, 'launchd', lambda label: dict(running=False, last_exit=None))
    monkeypatch.setattr(status_page, 'gamelog_running', lambda: False)
    with obs.operation('status_page', target, inherit=False):
        status_page.build()
    events = retrieve(target)['events']
    assert ('status_history', 'recovered') in {(e['phase'], e['outcome']) for e in events}
    assert any(e['phase'] == 'status_database_read' and e['details'].get('stale') for e in events)
    assert any(e['phase'] == 'status_page_write' and e['outcome'] == 'succeeded' for e in events)
    assert 'samples' in json.loads((out / 'history.json').read_text())
    assert (out / 'index.html').read_text().startswith('<!doctype html>')


def test_duckdb_writer_lock_is_distinct_from_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONPATH', str(SCRIPTS))
    target = tmp_path / 'lock' / 'warehouse.duckdb'
    target.parent.mkdir()
    missing = target.with_name('missing.duckdb')
    script = """
import sys
from pathlib import Path

import build_warehouse
from observability import operation

target = Path(sys.argv[1])
build_warehouse.DATA = target.parent
with operation('read_case', target):
    build_warehouse.connect(target, read_only=True, wait_seconds=0.1)
"""
    command = [sys.executable, '-c', script]
    with duckdb.connect(str(target)) as writer:
        writer.execute('CREATE TABLE fixture AS SELECT 1 AS sample')
        assert subprocess.run([*command, str(target)], capture_output=True, timeout=5).returncode == 1
    assert subprocess.run([*command, str(missing)], capture_output=True, timeout=5).returncode == 1
    attempts = [r for r in retrieve(target)['events'] if r['phase'] == 'duckdb_connect']
    assert [(r['outcome'], r['details']['reason']) for r in attempts] == [
        ('waiting', 'file_lock'), ('failed', 'lock_timeout')]
    assert attempts[-1]['details']['retries'] == 1
    missing_attempt = next(r for r in retrieve(missing)['events'] if r['phase'] == 'duckdb_connect')
    assert missing_attempt['details']['reason'] == 'warehouse_file_missing'
    assert missing_attempt['details']['retries'] == 0
