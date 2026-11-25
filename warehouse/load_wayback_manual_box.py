"""Admit verified player boxes from the archived NBA Europe Live scorebooks."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from fetch_wayback_nba import CACHE
from load_cska_box import create_starter_table, normalized
from load_wayback_nba import text

SOURCE = 'nba_com_archive_manual_box'
DAY = '20061005'
BARCELONA_REPORT = 'https://www.sport.es/es/noticias/baloncesto/barca-hizo-historia-costa-sixers-5529093'
CONFIRMED_POINTS_WITH_CONFLICT = {'aiguodala': 13, 'mkasun': 4}


@dataclass(frozen=True)
class Scorebook:
    game_id: str
    folder: str
    capture: str
    digest: str
    sides: tuple[tuple[str, int, int], tuple[str, int, int]]

    @property
    def url(self) -> str:
        return f'https://web.archive.org/web/{self.capture}id_/http://www.nba.com/games/{DAY}/{self.folder}/manual_boxscore.html'


SCOREBOOKS = (
    Scorebook('0010600001', 'PHIBAR', '20070630213129', 'e1c32c080e2ef63ff8c2378a3393e70d47b8b378d8ca24ec1a828d5ee0dfdbd5',
              (('Philadelphia 76ers', 1610612755, 99), ('Winterthur FC Barcelona', 12304, 104))),
    Scorebook('0010600002', 'SASLYV', '20061103004013', 'f427cfe7f80a76dc32beb63689fc1e817d382ed54870ccf0678d0a0ea3f639ab',
              (('San Antonio Spurs', 1610612759, 115), ('Addeco ASVEL', 12305, 90))),
)
BARCELONA_IDENTITIES = {
    'jlakovic': (564, 'Lakovic Lakovic'),
    'jcnavarro': (2434, 'Juan Carlos Navarro'),
    'rdelafuente': (560, 'de la Fuente de la Fuente'),
    'fvazquez': (569, 'Vazquez Vazquez'),
    'dmarconato': (565, 'Marconato Marconato'),
    'gbasile': (559, 'Basile Basile'),
    'mkakiouzis': (562, 'Kakiouzis Kakiouzis'),
    'mkasun': (2435, 'Mario Kasun'),
    'rukic': (101146, 'Roko Ukic'),
    'rgrimau': (561, 'Grimau Grimau'),
    'jtrias': (567, 'Trias Trias'),
}


@dataclass(frozen=True)
class PlayerLine:
    printed_name: str
    starter_mark: bool
    seconds: int
    fg: tuple[int, int]
    three: tuple[int, int]
    ft: tuple[int, int]
    oreb: int
    dreb: int
    reb: int
    ast: int
    pf: int
    stl: int
    tov: int
    blk: int
    pts: int

    def shooting_reconciles(self) -> bool:
        fgm, fga = self.fg
        tpm, tpa = self.three
        ftm, fta = self.ft
        return tpm <= fgm <= fga and tpm <= tpa <= fga and ftm <= fta and self.pts == 2 * fgm + tpm + ftm


def parse_table(table: str) -> list[PlayerLine]:
    players = []
    for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.I | re.S):
        tags = re.findall(r'<td\b[^>]*>(.*?)</td>', tr, re.I | re.S)
        cells = [text(cell) for cell in tags]
        if len(cells) not in (12, 14) or cells[0].lower() in ('name', 'team', 'total', 'totals'):
            continue
        m = re.fullmatch(r'(\d+):([0-5]\d)', cells[1])
        if not m or not cells[0]:
            raise ValueError('invalid player name or playing time in archived scorebook')
        seconds = int(m[1]) * 60 + int(m[2])
        fg, three, ft = [tuple(map(int, v.split('-'))) for v in cells[2:5]]
        if len(cells) == 12:
            # The Lyon scorebook labels rebounds D-O-T in one cell.
            dreb, oreb, reb = map(int, cells[5].split('-'))
            stats = (oreb, dreb, reb, *map(int, cells[6:]))
        else:
            stats = tuple(map(int, cells[5:]))
        players.append(PlayerLine(cells[0], re.fullmatch(r'\s*<b>.*?</b>\s*', tags[0], re.I | re.S) is not None,
                                  seconds, fg, three, ft, *stats))
    return players


def source_tables(raw: bytes) -> dict[str, tuple[int | None, list[PlayerLine]]]:
    page = raw.decode('windows-1252', errors='replace')
    result = {}
    for label, table in re.findall(r'<div[^>]*>\s*<b>([^<]+)</b>\s*</div>\s*<table[^>]*>(.*?)</table>', page, re.I | re.S):
        if 'FGM-A' not in table or 'PTS' not in table:
            continue
        label, _, score = text(label).partition(' - ')
        key = normalized(label)
        if key in result:
            raise ValueError('scorebook repeats a team table')
        result[key] = (int(score) if score.isdecimal() else None, parse_table(table))
    if len(result) != 2:
        raise ValueError('scorebook must have two named team tables')
    return result


def nba_roster(c, team: int) -> dict[str, set[int]]:
    aliases = {}
    for pid, name in c.execute('''SELECT DISTINCT p.person_id, d.display_name FROM core.fact_player_game_stats p
        JOIN core.dim_game g USING (game_id) JOIN core.dim_person d USING (person_id)
        WHERE p.team_id = ? AND g.season_start = 2006''', [team]).fetchall():
        first, _, last = name.partition(' ')
        for name_key in {normalized(first[:1] + last), normalized(last)}:
            aliases.setdefault(name_key, set()).add(pid)
    return aliases


def export_player(fixture, player, pid, team, opponent, home, won):
    fgm, fga = player.fg
    tpm, tpa = player.three
    ftm, fta = player.ft
    if not player.shooting_reconciles():
        fgm = fga = tpm = tpa = ftm = fta = None
    minutes = player.seconds / 60
    return (fixture.game_id, pid, team, opponent, home, won, 'played' if minutes else 'dnp', minutes, minutes,
            player.pts, player.ast, player.blk, player.stl, player.tov, fga, fgm, tpa, tpm, fta, ftm,
            player.oreb, player.dreb, player.reb, player.pf)


def load_scorebook(c, fixture: Scorebook, raw: bytes) -> None:
    if hashlib.sha256(raw).hexdigest() != fixture.digest:
        raise ValueError(f'archive digest changed for {fixture.game_id}')
    expected = tuple(key for _, team, score in reversed(fixture.sides) for key in (team, score))
    actual = c.execute('''SELECT g.home_team_id, r.home_score, g.away_team_id, r.away_score FROM core.dim_game g
        JOIN core.fact_game_result r USING (game_id) WHERE g.game_id = ? AND g.game_date = DATE '2006-10-05' ''',
        [fixture.game_id]).fetchone()
    if actual != expected:
        raise ValueError(f'archived manual box fixture disagrees: {fixture.game_id}')
    box = source_tables(raw)
    if set(box) != {normalized(label) for label, _, _ in fixture.sides}:
        raise ValueError('scorebook team names disagree with the fixture')
    rows, starters, receipts, dispositions = [], [], [], []
    for i, (label, team, score) in enumerate(fixture.sides):
        _, opponent, other_score = fixture.sides[1 - i]
        printed_score, players = box[normalized(label)]
        confirmed_points = CONFIRMED_POINTS_WITH_CONFLICT if fixture.folder == 'PHIBAR' else {}
        if printed_score not in (None, score) or sum(p.pts for p in players) != score or not all(
                (p.shooting_reconciles() or confirmed_points.get(normalized(p.printed_name)) == p.pts)
                and p.reb == p.oreb + p.dreb and (p.seconds or not p.pts) for p in players):
            dispositions.append((team, 'mismatch', 'player scoring or zero-minute lines disagree with the named team'))
            receipts.extend((team, p.printed_name, None, 'held_box_inconsistent') for p in players)
            continue
        roster = nba_roster(c, team) if team != 12304 else {}
        if team == 12304:
            actual_names = dict(c.execute('SELECT person_id,display_name FROM core.dim_person WHERE person_id IN (SELECT unnest(?))',
                                          [[data[0] for data in BARCELONA_IDENTITIES.values()]]).fetchall())
            roster = {k: {pid} for k, (pid, label) in BARCELONA_IDENTITIES.items() if actual_names.get(pid) == label}
        mapped = [(p, next(iter(roster[normalized(p.printed_name)]))) for p in players
                  if len(roster.get(normalized(p.printed_name), ())) == 1]
        mapped_names = {p.printed_name: pid for p, pid in mapped}
        if sum(p.pts for p, _ in mapped) != score or any(p.seconds and p.printed_name not in mapped_names for p in players):
            raise ValueError(f'played identity unresolved on verified manual box for {team}: {fixture.game_id}')
        if len(mapped) != len({pid for _, pid in mapped}):
            raise ValueError('two scorebook rows map to the same person')
        existing = dict(c.execute('''SELECT person_id,points FROM core.fact_player_game_stats WHERE game_id = ? AND team_id = ?''',
                                  [fixture.game_id, team]).fetchall())
        mapped_points = {pid: p.pts for p, pid in mapped}
        if any(pid not in mapped_points or pts not in (None, mapped_points[pid]) for pid, pts in existing.items()):
            raise ValueError('verified manual box conflicts with existing player facts')
        rows.extend(export_player(fixture, p, pid, team, opponent, i == 1, score > other_score)
                    for p, pid in mapped if pid not in existing)
        played_starters = [pid for p, pid in mapped if p.starter_mark and p.seconds]
        if fixture.folder == 'PHIBAR' and len(played_starters) == 5:
            starters.extend((fixture.game_id, team, pid, SOURCE, fixture.url) for pid in played_starters)
        receipts.extend((team, p.printed_name, mapped_names.get(p.printed_name), 'unresolved_dnp' if p.printed_name not in mapped_names
                         else 'accepted' if p.shooting_reconciles() else 'shooting_conflict_held') for p in players)
        note = f'{len(mapped)} identities, {score} points, printed player minutes total {sum(p.seconds for p in players) / 60:g}'
        if fixture.folder == 'PHIBAR':
            note += f'. Contemporary report corroborates the disputed points and both starting fives: {BARCELONA_REPORT}'
        dispositions.append((team, 'admitted', note))
    with warehouse_transaction(c):
        c.execute('''CREATE TABLE IF NOT EXISTS audit.manual_box_player_receipt (
            game_id VARCHAR, team_id BIGINT, printed_name VARCHAR, person_id BIGINT, disposition VARCHAR,
            source_url VARCHAR, sha256 VARCHAR, PRIMARY KEY (game_id, team_id, printed_name, sha256))''')
        c.executemany('INSERT OR IGNORE INTO audit.manual_box_player_receipt VALUES (?,?,?,?,?,?,?)',
                      [(fixture.game_id, *entry, fixture.url, fixture.digest) for entry in receipts])
        if rows:
            c.executemany('''INSERT INTO core.fact_player_game_stats (game_id, person_id, team_id, opponent_team_id,
                is_home, is_win, participation_status, num_minutes, reported_num_minutes, points, assists, blocks, steals,
                turnovers, fga, fgm, fg3a, fg3m, fta, ftm, oreb, dreb, reb, fouls_personal)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
        create_starter_table(c)
        if starters:
            c.executemany('INSERT OR IGNORE INTO raw.captured_club_starter VALUES (?,?,?,?,?)', starters)
        outcomes = [('player_box', 'admitted' if all(x[1] == 'admitted' for x in dispositions) else 'partial',
                     '. '.join(f'{team}: {note}' for team, _, note in dispositions))]
        if starters:
            outcomes.append(('starting_lineup', 'admitted', f'{len(starters)} marked opening players'))
        for domain, outcome, note in outcomes:
            c.execute('''INSERT INTO audit.gap_attempt SELECT ?,?,?,now(),?,? WHERE NOT EXISTS (
                SELECT 1 FROM audit.gap_attempt WHERE domain = ? AND game_id = ? AND source = ? AND outcome = ?)''',
                [domain, fixture.game_id, SOURCE, outcome, f'{fixture.url}: {note}', domain, fixture.game_id, SOURCE, outcome])
    print(f'Archived manual box {fixture.game_id}: {len(rows)} new player rows, {len(starters)} starters, {dispositions}', flush=True)


def load(c) -> None:
    for fixture in SCOREBOOKS:
        path = CACHE / 'snap' / DAY / fixture.folder / f'{fixture.capture}_manual_boxscore.html'
        if path.is_file():
            load_scorebook(c, fixture, path.read_bytes())


if __name__ == '__main__':
    with warehouse_lock(), connect(DEFAULT_OUT) as warehouse:
        load(warehouse)
        warehouse.execute('CHECKPOINT')
