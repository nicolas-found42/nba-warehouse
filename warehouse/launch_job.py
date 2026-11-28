"""Launch a scheduled workflow and record failures before its Python entry point."""
from __future__ import annotations

import argparse
import datetime as dt
import signal
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from observability import DEFAULT_TARGET, JOB_NAMES, operation

COMMANDS = {
    'daily_refresh': ['daily_refresh.py'],
    'chase_gaps': ['chase_gaps.py', 'all'],
    'status_page': ['status_page.py'],
}


def launch(job: str, command: list[str], target=DEFAULT_TARGET, **fields) -> int:
    with operation(job, target, 'launch', inherit=False, trigger='launchd', **fields) as span:
        with subprocess.Popen(command, env=span.child_env()) as child:
            def forward(signum, _frame):
                try:
                    child.send_signal(signum)
                except ProcessLookupError:
                    pass

            previous = signal.signal(signal.SIGTERM, forward)
            try:
                code = child.wait()
            finally:
                signal.signal(signal.SIGTERM, previous)
        span.result['exit_code'] = code
        if code == 75 and job == 'daily_refresh':
            span.outcome = 'deferred'
        elif code < 0:
            span.outcome = 'cancelled'
        elif code:
            span.outcome = 'failed'
        return 128 - code if code < 0 else code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job', choices=JOB_NAMES)
    args, rest = parser.parse_known_args()
    command = ['/opt/homebrew/bin/uv', 'run', '--offline', '-q', '--no-project', '--with', 'duckdb==2.0.0',
               'python', 'warehouse/' + COMMANDS[args.job][0], *COMMANDS[args.job][1:], *rest]
    if args.job == 'daily_refresh':
        refresh_parser = argparse.ArgumentParser(add_help=False)
        refresh_parser.add_argument('--warehouse', type=Path, default=DEFAULT_TARGET)
        yesterday = dt.datetime.now(ZoneInfo('America/New_York')).date() - dt.timedelta(days=1)
        refresh_parser.add_argument('--cutoff', type=dt.date.fromisoformat, default=yesterday)
        refresh, _ = refresh_parser.parse_known_args(rest)
        return launch(args.job, command, refresh.warehouse, cutoff=refresh.cutoff.isoformat())
    return launch(args.job, command)


if __name__ == '__main__':
    sys.exit(main())
