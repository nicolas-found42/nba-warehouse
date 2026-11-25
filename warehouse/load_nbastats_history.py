"""Old-era NBA/BAA quarter scores and team box totals from nbastats.net (fills gaps only).

  raw.nbastats_game_result   GameResults.xlsx (http://nbastats.net/01NbaAbaNblAbl/04-GameResults/GameResults.xlsx),
                             one row per game: winner/loser, final score, quarter and overtime scores
  raw.nbastats_team_game     02-GameStatistics-Team.xlsx (.../06-Teams/02-GameStatistics-Team.xlsx; the server
                             truncates it, the readable part runs to 2018), one row per team per game
  core.fact_game_period_score  quarter scores for games with none, only when both teams' periods add up to the final
  core.fact_team_game_stats    team box totals for teams with no row, only when the points equal the final

Games are matched on date + the two final scores (one-to-one). Both files were converted to CSV once and are kept
under evidence/nbastats/ with their SHA-256. Attempts go to audit.gap_attempt (source 'nbastats_net').

  uv run --no-project --with duckdb==2.0.0 python warehouse/load_nbastats_history.py
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction

EVIDENCE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'nbastats'
SOURCE = 'nbastats_net'
OWN_BASKET_GAME = '0024900497'
TEAM_BOX_URL = 'http://nbastats.net/01NbaAbaNblAbl/06-Teams/02-GameStatistics-Team.xlsx'


def n(v: str) -> int | None:
    v = (v or '').strip()
    try:
        return int(float(v)) if v and v != '---' else None
    except ValueError:
        return None


def results() -> list[tuple]:
    out = []
    with open(EVIDENCE / 'GameResults.csv', newline='', encoding='utf-8', errors='replace') as f:
        for r in csv.reader(f):
            if len(r) < 36 or not r[0].strip().isdigit() or r[6] != 'NBA':
                continue
            periods = [(n(r[16 + 2 * i]), n(r[17 + 2 * i])) for i in range(10)]  # Q1-Q4, OT1-OT6 (winner, loser)
            out.append((f'{int(r[2]):04d}-{int(r[1]):02d}-{int(r[0]):02d}', r[4], r[8], r[11], n(r[12]), n(r[13]),
                        [p[0] for p in periods], [p[1] for p in periods]))
    return out


def team_games() -> list[tuple]:
    out = []
    with open(EVIDENCE / 'GameStatistics-Team.csv', newline='', encoding='utf-8', errors='replace') as f:
        for r in csv.reader(f):
            if len(r) < 33 or r[0] != 'NBA' or not r[2].strip().isdigit():
                continue
            out.append((f'{int(r[4]):04d}-{int(r[3]):02d}-{int(r[2]):02d}', r[1], r[6], r[7], n(r[8]), n(r[9]),
                        n(r[15]), n(r[16]), n(r[18]), n(r[19]), n(r[21]), n(r[22]), n(r[24]), n(r[25]), n(r[26]), n(r[27]),
                        n(r[28]), n(r[29]), n(r[30]), n(r[31]), n(r[32])))
    return out


def publish_own_basket(c) -> None:
    """The March 3, 1950 team table records two opponent own-basket points separately."""
    evidence = EVIDENCE / 'GameStatistics-Team.csv'
    if not evidence.is_file() or c.execute('SELECT 1 FROM core.fact_team_game_stats WHERE game_id = ? AND team_id = 1610612744',
                                          [OWN_BASKET_GAME]).fetchone():
        return
    fixture = c.execute('''SELECT g.home_team_id,g.away_team_id,r.home_score,r.away_score
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
        WHERE game_id = ? AND g.game_date = DATE '1950-03-03' ''', [OWN_BASKET_GAME]).fetchone()
    if fixture != (1610612744, 1610612747, 66, 61):
        raise ValueError('Philadelphia own-basket receipt does not match the warehouse fixture')
    raw = evidence.read_bytes()
    rows = [r for r in csv.reader(io.StringIO(raw.decode('utf-8', errors='replace'))) if len(r) > 33
            and r[:5] == ['NBA', 'Reg', '3', '3', '1950'] and r[6:10] == ['PHI', 'MIN', '66', '61']]
    if len(rows) != 1:
        raise ValueError('Philadelphia team box must identify one fixture')
    r = rows[0]
    note = next((cell for cell in r[33:] if re.search(r'\b\d+ points? scored by George Mikan\b', cell, re.I)), '')
    extra = re.search(r'\b(\d+) points? scored by George Mikan\b', note, re.I)
    fgm, fga, ftm, fta, ast, pf, pts = [n(r[i]) for i in (15, 16, 21, 22, 27, 31, 32)]
    if None in (fgm, fga, ftm, fta, pts) or not extra or 'Philadelphia basket' not in note or not (
            fgm <= fga and ftm <= fta and 2 * fgm + ftm == pts and pts + int(extra[1]) == fixture[2]):
        raise ValueError('own-basket score and the printed shooting totals disagree')
    with warehouse_transaction(c):
        c.execute('''CREATE TABLE IF NOT EXISTS audit.team_box_scoring_adjustment (
            game_id VARCHAR, team_id BIGINT, credited_points INTEGER, printed_player_points INTEGER,
            source_url VARCHAR, sha256 VARCHAR, note VARCHAR, PRIMARY KEY (game_id, team_id))''')
        c.execute('''INSERT INTO audit.team_box_scoring_adjustment VALUES (?,?,?,?,?,?,?)''',
                  [OWN_BASKET_GAME, fixture[0], int(extra[1]), pts, TEAM_BOX_URL, hashlib.sha256(raw).hexdigest(), note])
        c.execute('''INSERT INTO core.fact_team_game_stats (game_id,team_id,is_home,is_win,pts,fgm,fga,ftm,fta,ast,pf)
            VALUES (?,?,true,true,?,?,?,?,?,?,?)''', [OWN_BASKET_GAME, fixture[0], fixture[2], fgm, fga, ftm, fta, ast, pf])
        c.execute('''INSERT INTO audit.gap_attempt VALUES
            ('team_box', ?, ?, now(), 'admitted', 'Published 64 player points plus two documented opponent own-basket points')''',
            [OWN_BASKET_GAME, SOURCE])
    print(f'Philadelphia team box admitted with documented own-basket points: {OWN_BASKET_GAME}', flush=True)


def main() -> None:
    gr, gt = results(), team_games()
    with warehouse_lock():
        c = connect(DEFAULT_OUT)
        c.execute('''CREATE OR REPLACE TABLE raw.nbastats_game_result (game_date DATE, game_type VARCHAR, winner VARCHAR, loser VARCHAR,
            winner_score INTEGER, loser_score INTEGER, winner_periods INTEGER[], loser_periods INTEGER[])''')
        c.executemany('INSERT INTO raw.nbastats_game_result VALUES (?,?,?,?,?,?,?,?)', gr)
        c.execute('''CREATE OR REPLACE TABLE raw.nbastats_team_game (game_date DATE, game_type VARCHAR, team VARCHAR, opp VARCHAR, score INTEGER,
            opp_score INTEGER, fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER, oreb INTEGER, dreb INTEGER,
            reb INTEGER, ast INTEGER, stl INTEGER, blk INTEGER, tov INTEGER, pf INTEGER, pts INTEGER)''')
        c.executemany(f'INSERT INTO raw.nbastats_team_game VALUES ({",".join("?" * 21)})', gt)
        # quarter scores: match game on date + scores, keep only lines that add up to the official final for both teams
        c.execute('''CREATE OR REPLACE TEMP TABLE pm AS
            SELECT g.game_id, g.home_team_id, g.away_team_id, r.home_score, r.away_score, n.winner_periods, n.loser_periods
            FROM raw.nbastats_game_result n
            JOIN core.dim_game g ON g.game_date = n.game_date
            JOIN core.fact_game_result r ON r.game_id = g.game_id
             AND greatest(r.home_score, r.away_score) = n.winner_score AND least(r.home_score, r.away_score) = n.loser_score
            WHERE g.game_id NOT IN (SELECT game_id FROM core.fact_game_period_score)
            QUALIFY count(*) OVER (PARTITION BY g.game_id) = 1 AND count(*) OVER (PARTITION BY n.game_date, n.winner, n.loser, n.winner_score) = 1''')
        c.execute('''CREATE OR REPLACE TEMP TABLE pl AS
            SELECT game_id, team_id, final, list_filter(periods, lambda x: x IS NOT NULL) AS periods FROM (
                SELECT game_id, CASE WHEN home_score > away_score THEN home_team_id ELSE away_team_id END AS team_id,
                       greatest(home_score, away_score) AS final, winner_periods AS periods FROM pm
                UNION ALL
                SELECT game_id, CASE WHEN home_score > away_score THEN away_team_id ELSE home_team_id END,
                       least(home_score, away_score), loser_periods FROM pm)''')
        c.execute('''CREATE OR REPLACE TEMP TABLE pok AS
            SELECT game_id FROM pl GROUP BY 1
            HAVING bool_and(len(periods) >= 4 AND list_sum(periods) = final) AND count(*) = 2 AND count(DISTINCT len(periods)) = 1''')
        c.execute('''INSERT INTO core.fact_game_period_score
            SELECT pl.game_id, pl.team_id, i, pl.periods[i] FROM pl JOIN pok USING (game_id), range(1, len(pl.periods) + 1) t(i)''')
        c.execute('''INSERT INTO audit.gap_attempt
            SELECT 'period_scores', game_id, ?, now(), CASE WHEN game_id IN (SELECT game_id FROM pok) THEN 'admitted'
                   WHEN list_count(winner_periods) = 0 THEN 'no_data' ELSE 'mismatch' END, 'date + final score match'
            FROM pm''', [SOURCE])
        print('quarter scores: matched', c.execute('SELECT count(*) FROM pm').fetchone()[0], 'admitted', c.execute('SELECT count(*) FROM pok').fetchone()[0])
        # team box totals for team-games we lack, points must equal the official final
        c.execute('''CREATE OR REPLACE TEMP TABLE tm AS
            SELECT g.game_id, CASE WHEN t.score = r.home_score THEN g.home_team_id ELSE g.away_team_id END AS team_id,
                   t.score = r.home_score AS is_home, t.score > t.opp_score AS is_win, t.*
            FROM raw.nbastats_team_game t
            JOIN core.dim_game g ON g.game_date = t.game_date
            JOIN core.fact_game_result r ON r.game_id = g.game_id AND r.home_score <> r.away_score
             AND ((t.score = r.home_score AND t.opp_score = r.away_score) OR (t.score = r.away_score AND t.opp_score = r.home_score))
            QUALIFY count(*) OVER (PARTITION BY g.game_id, t.team) = 1 AND count(*) OVER (PARTITION BY t.game_date, t.team, t.score) = 1''')
        c.execute('''CREATE OR REPLACE TEMP TABLE tnew AS SELECT * FROM tm
            WHERE (game_id, team_id) NOT IN (SELECT game_id, team_id FROM core.fact_team_game_stats)
              AND coalesce(pts, score) = score AND coalesce(fgm, 0) <= coalesce(fga, 99999) AND coalesce(ftm, 0) <= coalesce(fta, 99999)
              AND (game_id, team_id) IN (SELECT game_id, team_id FROM core.bridge_game_team)''')
        c.execute('''INSERT INTO core.fact_team_game_stats BY NAME
            SELECT game_id, team_id, is_home, is_win, score AS pts, fgm, fga, fg3m, fg3a, ftm, fta, oreb, dreb, reb, ast, tov, stl, blk, pf FROM tnew''')
        c.execute('''INSERT INTO audit.gap_attempt SELECT DISTINCT 'team_box', game_id, ?, now(), 'admitted', 'date + final score match' FROM tnew''', [SOURCE])
        print('team box rows added', c.execute('SELECT count(*), count(DISTINCT game_id) FROM tnew').fetchone())
        c.execute('CHECKPOINT')
        c.close()


if __name__ == '__main__':
    main()
