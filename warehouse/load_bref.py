"""Parse cached Basketball-Reference pages into the warehouse raw layer.

  raw.bref_transaction        one row per transaction statement (season page x date x paragraph)
  raw.bref_transaction_party  players and teams named in each statement, with from/to direction
  raw.bref_schedule_game      ABA (and any other cached) schedule rows: date, teams, scores, OT, box link
  raw.bref_team_season_table  every table on cached team-season pages (roster, salaries, ...), long form
  raw.bref_gamelog_row        one row per player game from cached player game-log pages

Pages come from bref_fetch's cache only; nothing is fetched here.

  uv run --no-project --with duckdb==2.0.0 python warehouse/load_bref.py [transactions] [schedules] [team_seasons]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html as htmllib
import json
import re
from pathlib import Path

import duckdb

from bref_fetch import CACHE, uncomment
from build_warehouse import DEFAULT_OUT

LI = re.compile(r'<li><span>([^<]+)</span>(.*?)</li>', re.S)
P = re.compile(r'<p>(.*?)</p>', re.S)
A = re.compile(r'<a([^>]*)href="([^"]+)"[^>]*>(.*?)</a>', re.S)
TAG = re.compile(r'<[^>]+>')

KINDS = [  # first match wins; order matters
    ('trade', r'\btraded\b|\bin a \d-team trade\b|\bas part of a \d-team trade\b'),
    ('sold', r'\bsold\b'),
    ('claimed_waivers', r'\bclaimed\b.*\bwaivers\b'),
    ('waived', r'\bwaived\b'),
    ('released', r'\breleased\b'),
    ('signed', r'\bsigned\b|\bre-signed\b'),
    ('drafted', r'\bdrafted\b|\bselected\b'),
    ('expansion_draft', r'\bexpansion draft\b'),
    ('dispersal_draft', r'\bdispersal draft\b'),
    ('coach_change', r'\bHead Coach\b|\bcoach\b'),
    ('retired', r'\bretire'),
    ('suspended', r'\bsuspend'),
    ('converted_contract', r'\bconverted\b'),
    ('assigned', r'\bassigned\b|\brecalled\b'),
    ('other', r'.'),
]


def text(s: str) -> str:
    return re.sub(r'\s+', ' ', htmllib.unescape(TAG.sub('', s))).strip()


def kind_of(t: str) -> str:
    for k, rx in KINDS:
        if re.search(rx, t, re.I):
            return k
    return 'other'


def transactions(c: duckdb.DuckDBPyConnection) -> None:
    events, parties = [], []
    for f in sorted((CACHE / 'leagues').glob('*_transactions.html')):
        league, year = f.name.split('_')[:2]
        page = f.read_text('utf-8')
        for li_i, (day, body) in enumerate(LI.findall(page)):
            try:
                d = dt.datetime.strptime(day.strip(), '%B %d, %Y').date()
            except ValueError:
                continue  # navigation lists share the markup; real entries always carry a date
            for p_i, para in enumerate(P.findall(body)):
                eid = f'{league}_{year}_{li_i:04d}_{p_i:02d}'
                t = text(para)
                k = 'season_marker' if re.fullmatch(r'\s*<a href="/leagues/[^"]+">[^<]+</a>\s*', para) else kind_of(t)
                events.append((eid, league, int(year), d, day.strip(), k, t, para, f'/leagues/{f.name}'))
                for pos, (attrs, href, label) in enumerate(A.findall(para)):
                    role = 'from_team' if 'data-attr-from' in attrs else 'to_team' if 'data-attr-to' in attrs else None
                    abbr = re.search(r'data-attr-(?:from|to)="([A-Z0-9]+)"', attrs)
                    if href.startswith('/players/'):
                        parties.append((eid, pos, 'player', re.sub(r'.*/([^/]+)\.html$', r'\1', href), text(label), None))
                    elif href.startswith('/teams/'):
                        parties.append((eid, pos, role or 'team', abbr.group(1) if abbr else href.split('/')[2], text(label), href))
                    elif href.startswith('/coaches/'):
                        parties.append((eid, pos, 'coach', re.sub(r'.*/([^/]+)\.html$', r'\1', href), text(label), None))
                    elif href.startswith('/executives/'):
                        parties.append((eid, pos, 'executive', re.sub(r'.*/([^/]+)\.html$', r'\1', href), text(label), None))
    c.execute('''CREATE OR REPLACE TABLE raw.bref_transaction (event_id VARCHAR PRIMARY KEY, league VARCHAR, season_end_year INTEGER,
        event_date DATE, date_text VARCHAR, kind VARCHAR, description VARCHAR, html VARCHAR, source_path VARCHAR)''')
    c.executemany('INSERT INTO raw.bref_transaction VALUES (?,?,?,?,?,?,?,?,?)', events)
    c.execute('''CREATE OR REPLACE TABLE raw.bref_transaction_party (event_id VARCHAR, position INTEGER, role VARCHAR,
        bref_id VARCHAR, label VARCHAR, href VARCHAR, PRIMARY KEY (event_id, position))''')
    c.executemany('INSERT INTO raw.bref_transaction_party VALUES (?,?,?,?,?,?)', parties)
    print('transactions', len(events), 'parties', len(parties))


def rows_of_table(page: str, table_id: str) -> list[dict[str, str]]:
    m = re.search(rf'<table[^>]*id="{table_id}"[^>]*>(.*?)</table>', page, re.S)
    if not m:
        return []
    out, section = [], ''
    for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', m.group(1), re.S):
        cells = re.findall(r'<(t[dh])[^>]*data-stat="([^"]+)"[^>]*>(.*?)</t[dh]>', tr, re.S)
        if not cells:
            label = text(tr)
            if label:
                section = label  # e.g. the "Playoffs" separator row in schedule tables
            continue
        row = {stat: text(v) for _, stat, v in cells}
        if section:
            row['_section'] = section
        for _, stat, v in cells:
            link = re.search(r'href="([^"]+)"', v)
            if link:
                row[stat + '__href'] = link.group(1)
        out.append(row)
    return out


def schedules(c: duckdb.DuckDBPyConnection) -> None:
    games = []
    for f in sorted((CACHE / 'leagues').glob('*_games*.html')):
        league, year = f.name.split('_')[:2]
        page = uncomment(f.read_text('utf-8'))
        for r in rows_of_table(page, 'schedule'):
            if not r.get('visitor_team_name'):
                continue
            games.append((league, int(year), f.name, r.get('date_game'), r.get('visitor_team_name'),
                          r.get('visitor_team_name__href'), r.get('visitor_pts'), r.get('home_team_name'),
                          r.get('home_team_name__href'), r.get('home_pts'), r.get('overtimes'),
                          r.get('box_score_text__href'), r.get('game_remarks'), json.dumps(r)))
    c.execute('''CREATE OR REPLACE TABLE raw.bref_schedule_game (league VARCHAR, season_end_year INTEGER, source_page VARCHAR,
        date_text VARCHAR, visitor_name VARCHAR, visitor_href VARCHAR, visitor_pts VARCHAR, home_name VARCHAR, home_href VARCHAR,
        home_pts VARCHAR, overtimes VARCHAR, box_href VARCHAR, remarks VARCHAR, row_json JSON)''')
    c.executemany('INSERT INTO raw.bref_schedule_game VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', games)
    # season index pages repeat the first month; keep one copy of each game
    c.execute('''CREATE OR REPLACE TABLE raw.bref_schedule_game AS SELECT DISTINCT ON (league, season_end_year, date_text, visitor_href, home_href,
        coalesce(box_href, '')) * FROM raw.bref_schedule_game ORDER BY ALL''')
    print('schedule rows', c.execute('SELECT league, count(*) FROM raw.bref_schedule_game GROUP BY 1').fetchall())


def team_seasons(c: duckdb.DuckDBPyConnection) -> None:
    out = []
    for f in sorted((CACHE / 'teams').glob('*/*.html')):
        team, year = f.parent.name, int(f.stem)
        page = uncomment(f.read_text('utf-8'))
        for tid in re.findall(r'<table[^>]*id="([a-z0-9_]+)"', page):
            for i, r in enumerate(rows_of_table(page, tid)):
                out.append((team, year, tid, i, json.dumps(r)))
    c.execute('''CREATE OR REPLACE TABLE raw.bref_team_season_table (team_abbr VARCHAR, season_end_year INTEGER, table_id VARCHAR,
        row_index INTEGER, row_json JSON)''')
    c.executemany('INSERT INTO raw.bref_team_season_table VALUES (?,?,?,?,?)', out)
    print('team-season rows', len(out))


def boxscores(c: duckdb.DuckDBPyConnection) -> None:
    """Basic box-score tables (players and team totals) from every cached box score page."""
    out = []
    for f in sorted((CACHE / 'boxscores').glob('*.html')):
        page = uncomment(f.read_text('utf-8'))
        for tid in re.findall(r'<table[^>]*id="(box-[A-Z0-9]+-game-basic)"', page):
            team = tid.split('-')[1]
            for i, r in enumerate(rows_of_table(page, tid)):
                key = re.sub(r'.*/([^/]+)\.html$', r'\1', r.get('player__href', '')) if r.get('player__href') else None
                out.append((f.stem, team, i, key, r.get('player'), json.dumps(r)))
    c.execute('''CREATE OR REPLACE TABLE raw.bref_box_row (bref_game_key VARCHAR, team_abbr VARCHAR, row_index INTEGER,
        bref_player_id VARCHAR, label VARCHAR, row_json JSON)''')
    c.executemany('INSERT INTO raw.bref_box_row VALUES (?,?,?,?,?,?)', out)
    print('box rows', len(out))


def gamelogs(c: duckdb.DuckDBPyConnection) -> None:
    """Player game-log pages (players/<x>/<id>/gamelog/<year>): one row per game the player has a box line for."""
    out = []
    for f in sorted((CACHE / 'players').glob('*/*/gamelog/*')):
        if f.suffix:  # .json receipts and .404 markers
            continue
        pid, year = f.parent.parent.name, f.name
        page = uncomment(f.read_text('utf-8'))
        for tr in re.findall(r'<tr[^>]*>(.*?)</tr>', page, re.S):
            box = re.search(r'href="/boxscores/([0-9]{9}[A-Z]{3})\.html"', tr)
            if not box:
                continue
            row = {stat: text(v) for stat, v in re.findall(r'<t[dh][^>]*data-stat="([^"]+)"[^>]*>(.*?)</t[dh]>', tr, re.S)}
            out.append((box.group(1), row.get('team_name_abbr') or row.get('team_id'), pid, int(year), json.dumps(row)))
    c.execute('''CREATE OR REPLACE TABLE raw.bref_gamelog_row (bref_game_key VARCHAR, team_abbr VARCHAR, bref_player_id VARCHAR,
        season_end_year INTEGER, row_json JSON)''')
    c.executemany('INSERT INTO raw.bref_gamelog_row VALUES (?,?,?,?,?)', out)
    print('game-log rows', len(out))


def officials(c: duckdb.DuckDBPyConnection) -> None:
    """Officials named on cached box pages ("Officials:" line), one row per game page and referee."""
    out = []
    for f in sorted((CACHE / 'boxscores').glob('*.html')):
        m = re.search(r'Officials:(.*?)</div>', f.read_text('utf-8', errors='replace'), re.S)
        if m:
            for i, (ref, name) in enumerate(re.findall(r"href=['\"]/referees/([a-z0-9]+)\.html['\"]>([^<]+)</a>", m.group(1)), 1):
                out.append((f.stem, i, ref, htmllib.unescape(name).strip()))
    c.execute('CREATE OR REPLACE TABLE raw.bref_box_official (bref_game_key VARCHAR, official_order INTEGER, bref_referee_id VARCHAR, official_name VARCHAR)')
    if out:
        c.executemany('INSERT INTO raw.bref_box_official VALUES (?,?,?,?)', out)
    print('box officials', len(out))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('parts', nargs='*', default=['transactions', 'schedules', 'boxscores'])
    ap.add_argument('--output', type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    con = duckdb.connect(str(a.output))
    for part in a.parts:
        globals()[part](con)
    con.execute('CHECKPOINT')
