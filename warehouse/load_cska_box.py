"""Recover the Philadelphia–CSKA Europe Live box from CSKA's match archive."""
from __future__ import annotations

import argparse
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock
from fetch_wayback_nba import cache_bytes
from load_wayback_nba import text

GAME = '0010600023'
URL = 'https://old.cskabasket.ru/gm/?a=boxscore&id=360&lang=en'
SOURCE = 'cska_club_box'
CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'club-boxes' / 'cska-360.html'
IDENTITY_BOX = 'https://old.cskabasket.ru/gm/?a=boxscore&id=332&lang=en'
IDENTITIES = {
    'davidandersen': (525, 'Andersen Andersen'), 'jonrobertholden': (526, 'Holden Holden'),
    'nikitakurbanov': (527, 'Kurbanov Kurbanov'), 'trajanlangdon': (528, 'Trajan Langdon'),
    'zakharpashutin': (529, 'Pachoutine Pachoutine'), 'theodorospapaloukas': (530, 'Theo Papaloukas'),
    'antonponkrashov': (531, 'Anton Ponkrashov'), 'matjazsmodis': (533, 'Smodis Smodis'),
    'tomasvandenspiegel': (534, 'Van Den Spiegel Van Den Spiegel'), 'davidvanterpool': (535, 'Vanterpool Vanterpool'),
    'andreyvorontsevich': (536, 'Voroncevich Voroncevich'),
}


@dataclass(frozen=True)
class Player:
    jersey: str
    display_name: str
    starter: bool
    seconds: int | None
    stats: tuple[str, ...]

    @property
    def points(self) -> int | None:
        return number(self.stats[0])


def number(value: str) -> int | None:
    return int(value) if value.isascii() and value.isdecimal() else None


def attempts(value: str) -> tuple[int | None, int | None]:
    pair = re.fullmatch(r'(\d+)\s*/\s*(\d+)', value)
    if pair:
        made, tried = int(pair[1]), int(pair[2])
        if made > tried:
            raise ValueError('made exceeds attempted on CSKA box')
        return made, tried
    if value not in ('-', ''):
        raise ValueError('unrecognized shot counts on CSKA box')
    return None, None


def parse(page: str) -> dict[str, list[Player]]:
    if not re.search(r'<div class="date">\s*11\.10\.2006\s*</div>', page):
        raise ValueError('wrong game date on CSKA box')
    result = {}
    for table in re.findall(r'<table class="layout margin_t">(.*?)</table>', page, re.S):
        team = re.findall(r'<td[^>]*colspan="3"[^>]*>\s*<b>(Philadelphia 76ers|CSKA)</b>', table)
        if len(team) != 1 or team[0] in result:
            continue
        players = []
        for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.S):
            tags = re.findall(r'<td\b([^>]*)>(.*?)</td>', tr, re.S)
            if len(tags) != 19 or number(text(tags[0][1])) is None:
                continue
            name = re.search(r'title="([^"]+)"', tags[1][0])
            if not name:
                raise ValueError('missing full player name in CSKA box')
            full, _, marker = text(name[1]).partition(' – ')
            cells = [text(raw) for _, raw in tags]
            mmss = re.fullmatch(r'(\d+):([0-5]\d)', cells[2])
            if not mmss and cells[2] != '-':
                raise ValueError('invalid playing time in CSKA box')
            played_seconds = int(mmss[1]) * 60 + int(mmss[2]) if mmss else None
            players.append(Player(cells[0], full, marker == 'Starting Lineup', played_seconds, tuple(cells[4:])))
        result[team[0]] = players
    if set(result) != {'Philadelphia 76ers', 'CSKA'}:
        raise ValueError('could not identify both club box tables')
    return result


def normalized(name: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFKD', name).lower() if c.isascii() and c.isalnum())


def player_row(game, team, opponent, home, won, player_id, player):
    pts, twos, threes, free, oreb, dreb, reb, ast, stl, tov, blk, _, pf, *_ = player.stats
    twom, twoa = attempts(twos)
    threem, threea = attempts(threes)
    ftm, fta = attempts(free)
    if number(pts) is not None and 2 * (twom or 0) + 3 * (threem or 0) + (ftm or 0) != number(pts):
        raise ValueError(f'points and made shots disagree for {player.display_name}')
    fg_complete = twom is not None and threem is not None
    minutes = player.seconds / 60 if player.seconds is not None else None
    return (game, player_id, team, opponent, home, won, 'played', minutes, minutes,
            number(pts), number(ast), number(blk), number(stl), number(tov),
            twoa + threea if fg_complete else None, twom + threem if fg_complete else None,
            threea, threem, fta, ftm, number(oreb), number(dreb), number(reb), number(pf))


def create_starter_table(c) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS raw.captured_club_starter (
        game_id VARCHAR, team_id BIGINT, person_id BIGINT, source VARCHAR, source_url VARCHAR,
        PRIMARY KEY (game_id, team_id, person_id, source))''')


def load(c, path: Path = CACHE) -> None:
    raw = path.read_bytes()
    box = parse(raw.decode('utf-8'))
    row = c.execute('''SELECT g.home_team_id, g.away_team_id, r.home_score, r.away_score
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
        WHERE g.game_id = ? AND g.game_date = DATE '2006-10-11' ''', [GAME]).fetchone()
    if row != (12308, 1610612755, 71, 85):
        raise ValueError('warehouse fixture does not match the CSKA archive')
    home, away, hs, aws = row
    if [sum(p.points or 0 for p in box[k]) for k in ('CSKA', 'Philadelphia 76ers')] != [hs, aws]:
        raise ValueError('club players do not add up to both official final scores')
    digest = hashlib.sha256(raw).hexdigest()
    philly = {normalized(n): (pid, pts) for pid, n, pts in c.execute('''SELECT s.person_id, d.display_name, s.points
        FROM core.fact_player_game_stats s JOIN core.dim_person d USING (person_id) WHERE s.game_id = ? AND s.team_id = ?''', [GAME, away]).fetchall()}
    philly_names = {'samueldelambert': 'samueldalembert', 'louiswilliams': 'louwilliams'}
    steven = next((p for p in box['Philadelphia 76ers'] if p.display_name == 'Steven Smith' and p.jersey == '30'), None)
    if not steven or not all((identity := philly.get(philly_names.get(normalized(p.display_name), normalized(p.display_name))))
                             and identity[1] == (p.points or 0) for p in box['Philadelphia 76ers'] if p != steven):
        raise ValueError('the existing Philadelphia players do not reconcile to the CSKA archive')
    steven_id = c.execute('''SELECT person_id FROM core.dim_person WHERE display_name = 'Steven Smith'
        AND from_year = 2006 AND to_year = 2006''').fetchall()
    if steven_id != [(200848,)]:
        raise ValueError('could not verify Steven Smith’s NBA identity')
    known = {pid for (pid, _) in philly.values()}
    added = [] if 200848 in known else [player_row(GAME, away, home, False, True, 200848, steven)]
    # These are the established NBA source IDs in the same-week Clippers–CSKA box.
    # Pashutin's archived team-season profile supplies the alias for his DNP in that witness game.
    cska = [(p, IDENTITIES.get(normalized(p.display_name))) for p in box['CSKA']]
    expected = {pid: label for pid, label in IDENTITIES.values()}
    existing_names = dict(c.execute('SELECT person_id, display_name FROM core.dim_person WHERE person_id IN (SELECT unnest(?))',
                                    [list(expected)]).fetchall())
    played = [(p, identity[0]) for p, identity in cska if p.seconds and identity]
    if existing_names != expected or len(played) != sum(bool(p.seconds) for p, _ in cska):
        raise ValueError('could not verify every played CSKA identity against the existing NBA IDs')
    if round(sum(p.seconds for p, _ in played) / 60) != 240 or sum(p.points or 0 for p, _ in played) != hs:
        raise ValueError('the CSKA player rows fail the fixture’s minutes or scoring check')
    existing_cska = {pid for pid, in c.execute('SELECT person_id FROM core.fact_player_game_stats WHERE game_id = ? AND team_id = ?', [GAME, home]).fetchall()}
    if existing_cska and existing_cska != {pid for _, pid in played}:
        raise ValueError('existing CSKA player rows disagree with the full archive roster')
    if not existing_cska:
        added.extend(player_row(GAME, home, away, True, False, pid, p) for p, pid in played)
    cska_starters = [(GAME, home, pid) for p, pid in played if p.starter]
    if len(cska_starters) != 5:
        raise ValueError('CSKA page must name exactly five resolved starters')
    c.execute('BEGIN')
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS audit.club_box_receipt (
            game_id VARCHAR, team_id BIGINT, jersey VARCHAR, source_name VARCHAR, person_id BIGINT,
            source_url VARCHAR, sha256 VARCHAR, disposition VARCHAR,
            PRIMARY KEY (game_id, team_id, jersey, sha256))''')
        receipts = [(GAME, away, steven.jersey, steven.display_name, 200848, URL, digest, 'admitted')]
        receipts.extend((GAME, home, p.jersey, p.display_name, k[0] if k else None, URL, digest,
                         'admitted' if k else 'identity_unresolved_dnp') for p, k in cska)
        c.executemany('''INSERT INTO audit.club_box_receipt VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT (game_id, team_id, jersey, sha256) DO UPDATE SET
                person_id = excluded.person_id, disposition = excluded.disposition''', receipts)
        if added:
            c.executemany('''INSERT INTO core.fact_player_game_stats (game_id, person_id, team_id, opponent_team_id,
                is_home, is_win, participation_status, num_minutes, reported_num_minutes, points, assists, blocks, steals,
                turnovers, fga, fgm, fg3a, fg3m, fta, ftm, oreb, dreb, reb, fouls_personal)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', added)
        create_starter_table(c)
        c.executemany('INSERT OR IGNORE INTO raw.captured_club_starter VALUES (?,?,?,?,?)',
                      [(*starter, SOURCE, URL) for starter in cska_starters])
        if added:
            c.execute('''INSERT INTO audit.gap_attempt VALUES
                ('player_box', ?, ?, now(), 'admitted', ?)''',
                [GAME, SOURCE, f'Both sides reconcile to the final. CSKA identity comparison: {IDENTITY_BOX}'])
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise
    print(f'CSKA club box: added {len(added)} player rows, five CSKA starters verified; one unresolved DNP preserved in audit', flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--warehouse', type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if not CACHE.is_file():
        with urlopen(Request(URL, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=20) as response:
            raw = response.read(1_000_000)
        parse(raw.decode('utf-8'))
        cache_bytes(CACHE, raw)
    with warehouse_lock(), connect(args.warehouse) as c:
        load(c)
        c.execute('CHECKPOINT')


if __name__ == '__main__':
    main()
