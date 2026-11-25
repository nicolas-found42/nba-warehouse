"""Load exact starter sets from reviewed NBA and Basketball-Reference box scores."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from build_warehouse import warehouse_transaction
from load_cska_box import normalized

EVIDENCE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence'
NBA_RECEIPT = (EVIDENCE / 'starting-lineup-repairs' / 'six-verified-nba-starting-fives.json',
               'd0c613bdc6945bdf6d15c846dc040c369681797875ef9d746571ccefb1b1a3c9')
BREF_RECEIPT = (EVIDENCE / 'public-starter-gaps' / 'first-seven' / 'starter-receipts.json',
                '2dd331be8d1b2bd5be32cca1f7aec8aaefeb65176f93c05b525999569f39e882')


def create_table(c) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS raw.verified_starting_lineup (
        game_id VARCHAR, team_id BIGINT, person_id BIGINT, starting_position VARCHAR, source VARCHAR,
        printed_name VARCHAR, source_order INTEGER, printed_minutes VARCHAR, source_url VARCHAR, sha256 VARCHAR,
        PRIMARY KEY (game_id,team_id,person_id,source_url,sha256))''')


def verified_digest(path: Path, expected: str) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(f'lineup evidence has changed: {path.name}')
    return raw


def starter_name_key(name: str) -> str:
    key = normalized(name)
    return {'stanislavmedvedenko': 'slavamedvedenko', 'charleshayes': 'chuckhayes'}.get(key, key)


def checked_starters(c, receipt: dict, directory: Path, source: str) -> list[tuple]:
    game_id = receipt['game_id']
    verified_digest(directory / Path(receipt['source_artifact']).name, receipt['sha256'])
    match = c.execute('''SELECT g.game_date::VARCHAR,g.season_start,g.away_team_id,g.home_team_id,r.away_score,r.home_score
        FROM core.dim_game g JOIN core.fact_game_result r USING(game_id)
        WHERE g.game_id=? AND g.participant_status='resolved' AND g.result_status='final' ''', [game_id]).fetchone()
    if not match:
        raise ValueError(f'lineup receipt has no final fixture: {game_id}')
    game_date, season, away_id, home_id, away_score, home_score = match
    if (game_date, away_score, home_score) != (receipt['game_date'], receipt['away_score'], receipt['home_score']):
        raise ValueError(f'lineup date or final disagrees with the warehouse: {game_id}')
    source_names = [(away_id, receipt['away_team']), (home_id, receipt['home_team'])]
    for team_id, printed in source_names:
        row = c.execute('''SELECT full_name FROM core.dim_team_version WHERE team_id=? AND valid_from<=?
            AND (valid_to IS NULL OR valid_to>=?)''', [team_id, season, season]).fetchone()
        if not row or normalized(row[0]) != normalized(printed):
            raise ValueError(f'lineup team disagrees with the warehouse: {game_id} {printed}')
    team_id = {'away': away_id, 'home': home_id}[receipt['side']]
    roster = defaultdict(set)
    for person_id, name, status, minutes in c.execute('''SELECT p.person_id,d.display_name,p.participation_status,p.num_minutes
        FROM core.fact_player_game_stats p
        JOIN core.dim_person d USING(person_id)
        WHERE p.game_id=? AND p.team_id=?''', [game_id, team_id]).fetchall():
        roster[starter_name_key(name)].add((person_id, status, minutes))
    starters = []
    for order, player in enumerate(receipt['official_rows'], 1):
        name = player['name']
        people = roster[starter_name_key(name)]
        if len(people) != 1:
            raise ValueError(f'lineup name is unresolved: {game_id} {name}')
        person_id, status, recorded_minutes = next(iter(people))
        minute, second = map(int, player['minutes'].split(':'))
        # Earlier NBA feeds round short appearances down to zero and leave the participation flag unknown.
        short_appearance = status == 'unknown' and recorded_minutes == 0 and minute == 0 and 0 < second < 60
        if status != 'played' and not short_appearance:
            raise ValueError(f'lineup contradicts participation record: {game_id} {name}')
        if player.get('person_id', person_id) != person_id:
            raise ValueError(f'lineup identity disagrees with the reviewed match: {game_id} {name}')
        starters.append((game_id, team_id, person_id, player.get('position'), source, name, order,
                         player['minutes'], receipt['source_url'], receipt['sha256']))
    if len(starters) != 5 or len({row[2] for row in starters}) != 5:
        raise ValueError(f'lineup receipt needs five different players: {game_id}')
    return starters


def load(c) -> None:
    create_table(c)
    source_rows = []
    for (path, digest), default_source in ((NBA_RECEIPT, 'nba_official_scorer_report'), (BREF_RECEIPT, 'bref_verified_box')):
        if not path.is_file():
            continue
        for receipt in json.loads(verified_digest(path, digest)):
            source = 'nba_com_archive_verified' if receipt['source_url'].startswith('https://web.archive.org/') else default_source
            starters = checked_starters(c, receipt, path.parent, source)
            if source == 'bref_verified_box':
                verified_digest(path.parent / f'{receipt["game_id"]}-espn.json', receipt['espn_sha256'])
                if {row['person_id'] for row in receipt['espn_source_order']} != {row[2] for row in starters}:
                    raise ValueError(f'independent starter sets disagree: {receipt["game_id"]}')
            source_rows.append((receipt, starters))
    if not source_rows:
        return
    with warehouse_transaction(c):
        for receipt, starters in source_rows:
            c.executemany('INSERT OR IGNORE INTO raw.verified_starting_lineup VALUES (?,?,?,?,?,?,?,?,?,?)', starters)
            gid, _, _, _, source, *_ = starters[0]
            c.execute('''INSERT INTO audit.gap_attempt SELECT 'starting_lineup',?,?,now(),'admitted',?
                WHERE NOT EXISTS (SELECT 1 FROM audit.gap_attempt WHERE domain='starting_lineup'
                    AND game_id=? AND source=? AND outcome='admitted')''',
                [gid, source, f'{receipt["side"]}: five marked starters in {receipt["source_url"]}', gid, source])
    print(f'Verified starting fives in {len(source_rows)} published boxes', flush=True)
