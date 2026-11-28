"""Record the start and exit of a manual warehouse script, including legacy tools."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from observability import DEFAULT_TARGET, LABEL, ROOT, operation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--warehouse', type=Path, default=DEFAULT_TARGET,
                        help="history target (also pass the script's own warehouse argument if needed)")
    parser.add_argument('script', help='script filename under warehouse/')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    script = ROOT / 'warehouse' / args.script
    if (not LABEL.fullmatch(args.script) or script.suffix not in ('.py', '.sh')
            or not script.is_file() or script.is_symlink()):
        parser.error('expected a Python or shell script filename under warehouse/')
    program = sys.executable if script.suffix == '.py' else '/bin/zsh'
    with operation(script.stem, args.warehouse, 'command', inherit=False) as span:
        code = subprocess.run([program, str(script), *args.arguments], env=span.child_env()).returncode
        span.result['exit_code'] = code
        if code < 0:
            span.outcome = 'cancelled'
        elif code:
            span.outcome = 'failed'
    return 128 - code if code < 0 else code


if __name__ == '__main__':
    sys.exit(main())
