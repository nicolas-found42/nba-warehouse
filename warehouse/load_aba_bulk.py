"""Bulk ABA player box lines from two free files (no page crawling).

  raw.boxtop_aba_stat      Basketball BOXTOP Project ABA release v3.1 (Michael Hamel; CC BY-NC-SA 3.0, "obtained free
                           of charge from and is copyrighted by the Basketball BOXTOP Project"): one row per player
                           box line, with Basketball-Reference player ids
  raw.nbastats_aba_gamelog nbastats.net ABA game logs (gamelogsABA.xlsx): one row per player game, names and
                           nbastats team codes only (matched to games and players in promote_new_sources.aba_boxes)

Files are downloaded once into the evidence directory with their SHA-256 and never refetched unless missing.

  uv run --no-project --with duckdb==2.0.0 --with openpyxl python warehouse/load_aba_bulk.py
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock

EVIDENCE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'aba-bulk'
BOXTOP_URL = 'https://raw.githubusercontent.com/mhamel12/boxtopaba/master/releases/ABA_Boxtop_v3_1_csv.zip'
NBASTATS_URL = 'http://nbastats.net/01NbaAbaNblAbl/05-GameLogs/gamelogsABA.xlsx'
AGENT = 'NBAWarehouse-Research/1.0 (personal historical data validation)'
BOXTOP_STATS = ['min', 'fgm', 'fga', 'ftm', 'fta', 'fg3m', 'fg3a', 'pts', 'oreb', 'reb', 'ast', 'pf', 'blk', 'tov', 'stl', 'tech']


def download(url: str) -> Path:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    p = EVIDENCE / url.rsplit('/', 1)[1]
    if not p.exists():
        with urlopen(Request(url, headers={'User-Agent': AGENT}), timeout=120) as r:
            body = r.read()
        p.write_bytes(body)
        (p.parent / (p.name + '.json')).write_text(json.dumps({
            'url': url, 'sha256': hashlib.sha256(body).hexdigest(), 'bytes': len(body),
            'fetched_at': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}))
    return p


def boxtop_rows(zpath: Path) -> list[tuple]:
    out = []
    with zipfile.ZipFile(zpath) as z:
        for name in sorted(n for n in z.namelist() if n.lower().endswith('.csv') and '/aba' in '/' + n.lower()):
            season_end = int(Path(name).stem[-4:])
            game, date, teams, phase = 0, None, {}, 'regular'
            for rec in csv.reader(io.TextIOWrapper(z.open(name), encoding='latin-1')):
                if not rec:
                    continue
                kind = rec[0]
                if kind == 'gamebxt':
                    game += 1; date = None; teams = {}
                elif kind in ('playoffsstarthere', 'abatiebreaker'):
                    phase = 'postseason'
                elif kind == 'info' and len(rec) > 2:
                    if rec[1] == 'date':
                        date = dt.datetime.strptime(rec[2], '%m/%d/%Y').date()
                    elif rec[1] in ('rteam', 'hteam'):
                        teams[rec[1]] = rec[2]
                elif kind == 'stat' and len(rec) > 5:
                    vals = (rec[6:6 + len(BOXTOP_STATS)] + [''] * len(BOXTOP_STATS))[:len(BOXTOP_STATS)]
                    out.append((season_end, game, phase, date, rec[1], teams.get(rec[1]), teams.get('hteam' if rec[1] == 'rteam' else 'rteam'),
                                rec[3], f'{rec[4]} {rec[5]}'.strip(), *[v.strip() or None for v in vals]))
    return out


def nbastats_rows(xpath: Path) -> list[tuple]:
    import openpyxl
    ws = openpyxl.load_workbook(xpath, read_only=True, data_only=True).worksheets[0]
    head = None
    out = []
    for r in ws.iter_rows(values_only=True):
        if head is None:
            if r and r[0] == 'Name':
                head = r
            continue
        if not r or r[4] is None or r[10] is None:
            continue
        v = dict(zip(['first', 'last', 'pos', 'rookie', 'd', 'm', 'y', 'season', 'type', 'lg', 'team', 'opp_lg', 'opp', 'score', 'opp_score',
                      'wl', 'ot', 'g', 'gs', 'min', 'fgm', 'fga', 'fg_pct', 'fg3m', 'fg3a', 'fg3_pct', 'ftm', 'fta', 'ft_pct', 'oreb', 'dreb',
                      'reb', 'ast', 'stl', 'blk', 'tov', 'pf', 'pts', 'notes'], r))
        mins = v['min'].total_seconds() / 60 if isinstance(v['min'], dt.timedelta) else v['min'] if isinstance(v['min'], (int, float)) else None
        num = lambda k: None if v[k] in (None, '') or isinstance(v[k], str) else int(v[k])
        out.append((dt.date(int(v['y']), int(v['m']), int(v['d'])), v['season'], v['type'], v['team'], v['opp'], v['first'], v['last'],
                    num('score'), num('opp_score'), None if mins is None else round(float(mins), 2),
                    *[num(k) for k in ('fgm', 'fga', 'fg3m', 'fg3a', 'ftm', 'fta', 'oreb', 'dreb', 'reb', 'ast', 'stl', 'blk', 'tov', 'pf', 'pts')],
                    v['notes']))
    return out


def main() -> None:
    bt = boxtop_rows(download(BOXTOP_URL))
    ns = nbastats_rows(download(NBASTATS_URL))
    with warehouse_lock():
        c = connect(DEFAULT_OUT)
        c.execute('CREATE OR REPLACE TABLE raw.boxtop_aba_stat (season_end_year INTEGER, game_seq INTEGER, phase VARCHAR, game_date DATE, side VARCHAR, '
                  'team_name VARCHAR, opp_name VARCHAR, bref_player_id VARCHAR, player_name VARCHAR, '
                  + ', '.join(f'{s} VARCHAR' for s in BOXTOP_STATS) + ')')
        c.executemany(f'INSERT INTO raw.boxtop_aba_stat VALUES ({",".join("?" * (9 + len(BOXTOP_STATS)))})', bt)
        c.execute('''CREATE OR REPLACE TABLE raw.nbastats_aba_gamelog (game_date DATE, season VARCHAR, game_type VARCHAR, team VARCHAR, opp VARCHAR,
            first_name VARCHAR, last_name VARCHAR, team_score INTEGER, opp_score INTEGER, minutes DOUBLE, fgm INTEGER, fga INTEGER, fg3m INTEGER,
            fg3a INTEGER, ftm INTEGER, fta INTEGER, oreb INTEGER, dreb INTEGER, reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER,
            pf INTEGER, pts INTEGER, notes VARCHAR)''')
        c.executemany(f'INSERT INTO raw.nbastats_aba_gamelog VALUES ({",".join("?" * 26)})', ns)
        c.execute('CHECKPOINT')
        c.close()
    print('boxtop rows', len(bt), 'nbastats rows', len(ns))


if __name__ == '__main__':
    main()
