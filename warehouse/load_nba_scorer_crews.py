"""Recover a published crew from an exact NBA final scorer report."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from fetch_wayback_nba import team_code
from load_cska_box import normalized
from load_preseason_press import verified_official
from promote_domains import OFFICIAL_NAME_CANON

SOURCE = 'nba_official_scorer_report'
CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'nba-official-crews-2026-27'
INDEX_DIGEST = 'd8014cbb49c01a2e141922708c822da58310056ef760374ffdb5a22a850242f6'
STAFF_SOURCE = 'https://cms.nba.com/official/wp-content/uploads/sites/4/2026/10/2026-27-NBA-Officiating-Staff.pdf'
STAFF = {'julianscott': ('Julian', 'Scott', '76'), 'kastineevans': ('Kastine', 'Evans', '78'),
         'catherinechang': ('Catherine', 'Chang', '83'), 'italoaraujo': ('Italo', 'Araujo', '86'),
         'jpprimm': ('JP', 'Primm', '82')}


def create_table(c) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS raw.nba_scorer_crew (
        game_id VARCHAR, official_id BIGINT, jersey_num VARCHAR, official_order INTEGER, printed_name VARCHAR,
        source_url VARCHAR, extraction_sha256 VARCHAR, PRIMARY KEY (game_id, official_id, extraction_sha256))''')


def checked_game(c, receipt):
    url = receipt['url']
    key = re.fullmatch(r'https://statsdmz\.nba\.com/pdfs/(\d{8})/\1_([A-Z]{3})([A-Z]{3})\.pdf', url)
    if not key:
        raise ValueError(f'unexpected NBA report URL for {receipt["game_id"]}')
    path = CACHE / f'{Path(url).stem}.tinyfish.json'
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != receipt['sha256']:
        raise ValueError(f'NBA report extraction digest changed: {path.name}')
    extraction = json.loads(raw)
    document = unicodedata.normalize('NFKC', extraction['text'])
    crew = re.search(r'^Officials:\s*(.*?)\s*$', document, re.M | re.I)
    if extraction['url'] != url or not crew or crew[1] != receipt['officials_printed']:
        raise ValueError(f'NBA report extraction does not contain the reviewed crew: {path.name}')
    names = re.findall(r'#(\d{1,3})\s+([^,]+)', crew[1])
    if len(names) != 3 or len({n for n, _ in names}) != 3:
        raise ValueError('NBA official report does not contain three distinct crew members')
    game = c.execute('''SELECT g.game_date, a.abbreviation,h.abbreviation,r.away_score,r.home_score
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
        JOIN core.dim_team a ON a.team_id=g.away_team_id JOIN core.dim_team h ON h.team_id=g.home_team_id
        WHERE g.game_id=?''', [receipt['game_id']]).fetchone()
    if not game:
        raise ValueError('scorer report has no exact warehouse fixture')
    day, away, home, aws, hs = game
    if (day.strftime('%Y%m%d'), team_code(away), team_code(home)) != (key[1], team_code(key[2]), team_code(key[3])) or (
            aws, hs) != (receipt['away']['f'], receipt['home']['f']):
        raise ValueError('NBA report date, sides or final disagree with the warehouse')
    return names


def load(c) -> None:
    create_table(c)
    manifest = CACHE / 'game-crew-extraction-index.json'
    if not manifest.is_file():
        return
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != INDEX_DIGEST:
        raise ValueError('reviewed NBA scorer crew manifest changed')
    documents = [(receipt, checked_game(c, receipt)) for receipt in json.loads(raw)]
    names = dict(c.execute(f'SELECT name, official_id FROM {OFFICIAL_NAME_CANON}').fetchall())
    rows = []
    with warehouse_transaction(c):
        for receipt, crew in documents:
            gid = receipt['game_id']
            for position, (number, printed) in enumerate(crew, 1):
                key = normalized(printed)
                if (gid, number, key) == ('0022600148', '86', 'italioaroujo'):
                    key = 'italoaraujo'  # the verified staff guide and another scorer report confirm the spelling
                if key not in names:
                    first, last, confirmed_number = STAFF[key]
                    if confirmed_number != number:
                        raise ValueError(f'NBA staff directory number disagrees for {printed}')
                    names[key] = verified_official(c, names, first, last, STAFF_SOURCE)
                rows.append((gid, names[key], number, position, printed, receipt['url'], receipt['sha256']))
            c.execute('''INSERT INTO audit.gap_attempt SELECT 'officials',?,?,now(),'admitted',?
                WHERE NOT EXISTS (SELECT 1 FROM audit.gap_attempt WHERE domain='officials' AND game_id=? AND source=?)''',
                [gid, SOURCE, receipt['url'], gid, SOURCE])
        c.executemany('INSERT OR IGNORE INTO raw.nba_scorer_crew VALUES (?,?,?,?,?,?,?)', rows)
    print(f'NBA scorer reports verified {len(rows)} official assignments across {len(documents)} games', flush=True)


if __name__ == '__main__':
    with warehouse_lock(), connect(DEFAULT_OUT) as warehouse:
        load(warehouse)
        warehouse.execute('CHECKPOINT')
