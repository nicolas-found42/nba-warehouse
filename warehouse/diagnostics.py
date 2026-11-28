"""Read local operation history without opening DuckDB or the evidence caches."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from observability import DEFAULT_TARGET, GAME, ID, LABEL, MAX_EVENTS, RETENTION_DAYS, SCHEMA_VERSION, clean, store_path

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def timestamp(at_us: int) -> str:
    return (EPOCH + dt.timedelta(microseconds=at_us)).isoformat()


def parse_time(value: str) -> int:
    try:
        stamp = dt.datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ValueError
        return (stamp - EPOCH) // dt.timedelta(microseconds=1)
    except (ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError('use ISO 8601 with an explicit offset') from error


def inspect(args: argparse.Namespace) -> dict:
    result = {
        'schema_version': SCHEMA_VERSION,
        'source': {'kind': 'local_operation_history'},
        'range': {'since_inclusive': timestamp(args.since), 'until_exclusive': timestamp(args.until)},
        'filters': {key: getattr(args, key) for key in ('workflow', 'run_id', 'game_id', 'cutoff')},
        'retention': {'days': RETENTION_DAYS, 'max_events': MAX_EVENTS, 'sampling': 'none in this store'},
        'events': [],
    }
    try:
        path = store_path(args.warehouse)
    except (OSError, RuntimeError):
        return result | {'query_status': 'inaccessible', 'coverage': 'unknown', 'query_error': 'target_unresolvable'}
    result['source'].update(target_key=path.stem, store_relative_to_warehouse_parent='observability/' + path.name)
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.15)) as c:
            c.row_factory = sqlite3.Row
            c.execute('BEGIN')
            if c.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                raise sqlite3.DatabaseError('unsupported schema')
            bounds = c.execute('SELECT min(at_us),max(at_us),max(event_id) FROM events').fetchone()
            snapshot = args.through if args.through is not None else (bounds[2] or 0)
            counter = c.execute('SELECT time_evicted,volume_evicted,reported_dropped FROM retention').fetchone()
            result['retention'].update(dict(counter))
            result['retention'].update(oldest_record=timestamp(bounds[0]) if bounds[0] is not None else None,
                                       newest_record=timestamp(bounds[1]) if bounds[1] is not None else None)
            filters = [f'{key}=?' for key, value in result['filters'].items() if value is not None]
            clauses = ' AND '.join(['at_us>=?', 'at_us<?', 'event_id>?', 'event_id<=?', *filters])
            values = [value for value in result['filters'].values() if value is not None]
            rows = c.execute(f'SELECT * FROM events WHERE {clauses} ORDER BY event_id LIMIT ?',
                             [args.since, args.until, args.after, snapshot, *values, args.limit + 1]).fetchall()
            more = len(rows) > args.limit
            for row in rows[:args.limit]:
                entry = dict(row)
                entry['at'] = timestamp(entry.pop('at_us'))
                details = json.loads(entry['details'])
                if not isinstance(details, dict):
                    raise ValueError('invalid event details')
                entry['details'] = clean(details)
                result['events'].append(entry)
            result.update(query_status='ok', coverage='recorded_events_only',
                          no_results_meaning='no retained matches; emission and source coverage may be incomplete',
                          pagination={'through': snapshot, 'after': args.after, 'limit': args.limit,
                                      'truncated': more, 'next_after': rows[args.limit - 1]['event_id'] if more else None})
    except (OSError, sqlite3.Error, ValueError) as error:
        try:
            path.stat()
        except FileNotFoundError:
            status = 'missing'
        except OSError:
            status = 'inaccessible'
        else:
            status = 'inaccessible'
        result.update(query_status=status, coverage='unknown', events=[],
                      query_error=getattr(error, 'sqlite_errorname', 'unreadable_history'))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--warehouse', type=Path, default=DEFAULT_TARGET)
    parser.add_argument('--since', type=parse_time, required=True)
    parser.add_argument('--until', type=parse_time, required=True)
    parser.add_argument('--run-id')
    parser.add_argument('--workflow')
    parser.add_argument('--game-id')
    parser.add_argument('--cutoff', type=dt.date.fromisoformat)
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--after', type=int, default=0)
    parser.add_argument('--through', type=int)
    args = parser.parse_args()
    if args.since >= args.until:
        parser.error('--until must be later than --since')
    if not 1 <= args.limit <= 1000 or args.after < 0 or (args.after and args.through is None):
        parser.error('use a limit in [1,1000] and supply --through with any nonzero --after')
    if args.through is not None and not args.after <= args.through < 2**63:
        parser.error('--through must be at least --after and less than 2^63')
    for name, pattern in (('run_id', ID), ('game_id', GAME), ('workflow', LABEL)):
        value = getattr(args, name)
        if value is not None and not pattern.fullmatch(value):
            parser.error('invalid ' + name.replace('_', ' '))
    args.cutoff = args.cutoff.isoformat() if args.cutoff else None
    result = inspect(args)
    print(json.dumps(result, indent=2))
    return 0 if result['query_status'] == 'ok' else 2


if __name__ == '__main__':
    sys.exit(main())
