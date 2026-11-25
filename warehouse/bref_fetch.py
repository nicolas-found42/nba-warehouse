"""Polite, cache-first Basketball-Reference page fetcher (personal research use).

* one request at a time across every process (file lock), at least MIN_INTERVAL seconds apart
  (site robots.txt asks for 3 s and Sports Reference allows 20/min; going over gets the IP blocked for hours)
* robots.txt is checked for every path; disallowed paths are refused
* every response body is cached under the evidence directory with its SHA-256; a cached page is never
  refetched unless --refresh is given
* HTTP 429/403 or a challenge page stops the whole run immediately (BlockedError)

Library use:  from bref_fetch import fetch;  html = fetch('/leagues/NBA_1977_transactions.html')
CLI:          python bref_fetch.py /path1 /path2 ...
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

ORIGIN = 'https://www.basketball-reference.com'
AGENT = 'NBAWarehouse-Research/1.0 (personal historical data validation)'
MIN_INTERVAL = 3.1
MAX_BYTES = 12 * 1024 * 1024
CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'bref-cache'
LOCK = CACHE / '.lock'
STAMP = CACHE / '.last_request'


class BlockedError(RuntimeError):
    pass


class NotFound(RuntimeError):
    pass


_robots: RobotFileParser | None = None


def robots() -> RobotFileParser:
    global _robots
    if _robots is None:
        rp = RobotFileParser()
        with urlopen(Request(ORIGIN + '/robots.txt', headers={'User-Agent': AGENT}), timeout=30) as r:
            rp.parse(r.read().decode('utf-8', 'replace').splitlines())
        _robots = rp
    return _robots


def cache_path(path: str) -> Path:
    clean = path.lstrip('/').replace('?', '_q_')
    return CACHE / clean


def fetch(path: str, refresh: bool = False, ignore_robots: bool = False) -> str:
    """Return page HTML for an absolute site path, from cache when present.

    ignore_robots skips the robots.txt path check (the owner's decision for player game-log pages); the rate
    limit and the stop-on-block rules still apply."""
    p = cache_path(path)
    if p.exists() and not refresh:
        return p.read_text('utf-8')
    if (p.with_suffix(p.suffix + '.404')).exists() and not refresh:
        raise NotFound(path)
    if not ignore_robots and not robots().can_fetch(AGENT, ORIGIN + path):
        raise PermissionError(f'robots.txt disallows {path}')
    CACHE.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a+') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        last = float(STAMP.read_text()) if STAMP.exists() else 0.0
        wait = MIN_INTERVAL - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
        try:
            with urlopen(Request(ORIGIN + path, headers={'User-Agent': AGENT}), timeout=60) as r:
                body = r.read(MAX_BYTES + 1)
                status = r.status
        except HTTPError as e:
            STAMP.write_text(str(time.time()))
            if e.code in (403, 429):
                raise BlockedError(f'HTTP {e.code} on {path}; stopping all fetching') from e
            if e.code == 404:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.with_suffix(p.suffix + '.404').write_text(time.strftime('%FT%TZ', time.gmtime()))
                raise NotFound(path) from e
            raise
        except URLError:
            STAMP.write_text(str(time.time()))
            raise
        STAMP.write_text(str(time.time()))
    if len(body) > MAX_BYTES:
        raise ValueError(f'response too large: {path}')
    text = body.decode('utf-8', 'replace')
    if status != 200 or 'challenge-platform' in text[:5000] or 'Just a moment' in text[:2000]:
        raise BlockedError(f'challenge or status {status} on {path}')
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, 'utf-8')
    meta = {'url': ORIGIN + path, 'sha256': hashlib.sha256(body).hexdigest(),
            'fetched_at': time.strftime('%FT%TZ', time.gmtime()), 'bytes': len(body)}
    p.with_suffix(p.suffix + '.json').write_text(json.dumps(meta))
    return text


def uncomment(html: str) -> str:
    """Basketball-Reference hides most secondary tables inside HTML comments."""
    return html.replace('<!--', '').replace('-->', '')


if __name__ == '__main__':
    for arg in sys.argv[1:]:
        try:
            print(arg, len(fetch(arg)))
        except NotFound:
            print(arg, 'not found')
