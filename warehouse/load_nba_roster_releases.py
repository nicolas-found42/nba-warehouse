"""Load date-stamped NBA opening and restart roster publications."""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from load_cska_box import normalized
from pregame_sources import publish as publish_pregame

CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'pregame-history-research'
TEAM_IDS = {
    'ATLANTA': 1610612737, 'BOSTON': 1610612738, 'BROOKLYN': 1610612751, 'CHARLOTTE': 1610612766,
    'CHICAGO': 1610612741, 'CLEVELAND': 1610612739, 'DALLAS': 1610612742, 'DENVER': 1610612743,
    'DETROIT': 1610612765, 'GOLDEN STATE': 1610612744, 'HOUSTON': 1610612745, 'INDIANA': 1610612754,
    'LA CLIPPERS': 1610612746, 'LA LAKERS': 1610612747, 'LOS ANGELES LAKERS': 1610612747, 'MEMPHIS': 1610612763,
    'MIAMI': 1610612748, 'MILWAUKEE': 1610612749, 'MINNESOTA': 1610612750, 'NEW ORLEANS': 1610612740,
    'NEW YORK': 1610612752, 'OKLAHOMA CITY': 1610612760, 'ORLANDO': 1610612753, 'PHILADELPHIA': 1610612755,
    'PHOENIX': 1610612756, 'PORTLAND': 1610612757, 'SACRAMENTO': 1610612758, 'SAN ANTONIO': 1610612759,
    'TORONTO': 1610612761, 'UTAH': 1610612762, 'WASHINGTON': 1610612764,
}


@dataclass(frozen=True)
class RosterRelease:
    filename: str
    sha256: str
    source_url: str
    publication_url: str
    available_at: dt.datetime
    effective_on: dt.date
    teams: int
    title: str


RELEASES = (
    RosterRelease('opening-2018.pdf', '934a265c1d96eeceb4b3c9940e3f8aebfb7add981c867ddb45f7580200cf7be6',
                  'https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2018/10/2018-19-Team-Rosters.pdf',
                  'https://pr.nba.com/rosters-set-for-2018-19-nba-regular-season/',
                  dt.datetime.fromisoformat('2018-10-16T01:06:45+00:00'), dt.date(2018, 10, 16), 30,
                  '2018-19 NBA REGULAR SEASON OPENING ROSTERS'),
    RosterRelease('restart-2020.pdf', 'e2f1e8cd6e2da970164aa9cc2c3bcdb5ed0cbf7de0237311bf4c8e245cb9adb5',
                  'https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2020/07/Restart-Rosters-7-19-20.pdf',
                  'https://www.nba.com/news/nba-announces-teamrosters-season-restart-orlando',
                  dt.datetime.fromisoformat('2020-07-21T04:00:00+00:00'), dt.date(2020, 7, 30), 22, 'RESTART ROSTERS'),
)


def parse_rosters(text: str, release: RosterRelease) -> dict[int, list[tuple[str, str]]]:
    if release.title not in text:
        raise ValueError(f'unexpected title: {release.filename}')
    rosters = defaultdict(list)
    columns = []
    status = 'listed'
    for row in text.splitlines():
        cells = [(m[0], (m.start() + m.end()) / 2) for m in re.finditer(r'\S(?:.*?\S)?(?=\s{2,}|$)', row.rstrip())]
        if not cells or release.title == row.strip():
            continue
        labels = [name.replace('.', '') for name, _ in cells]
        if all(label in TEAM_IDS for label in labels):
            columns = [(TEAM_IDS[label], center) for label, (_, center) in zip(labels, cells)]
            status = 'listed'
            continue
        if row.strip() in ('INACTIVE LIST/TWO-WAY LISTS', 'SUSPENSION LIST'):
            status = 'suspended' if row.strip().startswith('SUSPENSION') else 'inactive_or_two_way'
            continue
        if not columns:
            continue
        for name, center in cells:
            if not re.fullmatch(r"[^\W\d_](?:[^\W\d_]|[ .,’'‐–-]|\b(?:II|III|IV)\b)+\*?", name):
                continue
            member_status = 'two_way' if name.endswith('*') else status
            name = name.removesuffix('*').replace('‐', '-')
            if ',' in name:
                last, first = name.split(',', maxsplit=1)
                name = f'{first.strip()} {last.strip()}'
            elif len(name.split()) < 2 and normalized(name) != 'nene':
                continue
            team, distance = min(((team, abs(center - anchor)) for team, anchor in columns), key=lambda item: item[1])
            if distance > 17:
                raise ValueError(f'roster name is outside the team columns: {name}')
            rosters[team].append((name, member_status))
    if len(rosters) != release.teams or any(not 10 <= len(players) <= 22 or len({name for name, _ in players}) != len(players)
                                           for players in rosters.values()):
        raise ValueError(f'unexpected team or roster shape in {release.filename}')
    return rosters


def load(c) -> None:
    people = defaultdict(set)
    for person_id, name in c.execute('SELECT person_id,display_name FROM core.dim_person WHERE display_name IS NOT NULL').fetchall():
        people[normalized(name)].add(person_id)
    roster_rows, publications, evidence = [], [], []
    for release in RELEASES:
        path = CACHE / release.filename
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != release.sha256:
            raise ValueError(f'reviewed roster release has changed: {release.filename}')
        layout = subprocess.run(['pdftotext', '-layout', '-', '-'], input=raw, check=True, capture_output=True).stdout.decode()
        teams = parse_rosters(layout, release)
        publications.append((release.source_url, release.sha256, release.available_at, 'dated_nba_roster_release'))
        for team, players in teams.items():
            for name, status in players:
                candidates = people[normalized(name)]
                pid = next(iter(candidates)) if len(candidates) == 1 else None
                evidence.append((team, name, status, pid, release.available_at, release.effective_on,
                                 release.source_url, release.publication_url, release.sha256))
                if pid is not None:
                    roster_rows.append((team, pid, name, None, None, release.effective_on, release.available_at,
                                        release.source_url, release.sha256))
    if not publications:
        return
    with warehouse_transaction(c):
        c.execute('''CREATE TABLE IF NOT EXISTS audit.nba_roster_release_name (
            team_id BIGINT, printed_name VARCHAR, printed_status VARCHAR, person_id BIGINT, available_at TIMESTAMPTZ,
            effective_on DATE, source_url VARCHAR, publication_url VARCHAR, sha256 VARCHAR,
            PRIMARY KEY (team_id,printed_name,sha256))''')
        c.executemany('''INSERT INTO audit.nba_roster_release_name VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT (team_id,printed_name,sha256) DO UPDATE SET printed_status=excluded.printed_status,
            person_id=excluded.person_id''', evidence)
        publish_pregame(c, (None, None, roster_rows, publications))
    print(f'Published {len(roster_rows)} linked historical roster names of {len(evidence)} printed across {len(publications)} releases', flush=True)


if __name__ == '__main__':
    with warehouse_lock(), connect(DEFAULT_OUT) as warehouse:
        load(warehouse)
        warehouse.execute('CHECKPOINT')
