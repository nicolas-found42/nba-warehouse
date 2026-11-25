"""Load cached NBA.com responses from fetch_nba_gaps.py into the warehouse.

  raw.nba_gap_starter     starters (position set) from boxscoretraditionalv3; promoted into core.fact_starting_lineup
                          by promote_domains.py (NBA.com outranks the Kaggle fallback)
  core.fact_play_event    playbyplayv3 actions for games with no play events yet
  core.fact_shot          field-goal actions with coordinates, for games with no shot rows yet
  audit.gap_attempt       one row per game and domain: admitted, or no_data when NBA.com returned nothing

  uv run --no-project --with duckdb==2.0.0 python warehouse/load_nba_gaps.py
"""
from __future__ import annotations

import hashlib
import json
import re

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock
from fetch_nba_gaps import CACHE

SOURCE = 'nba_stats_v3'


def secs(clock: str) -> float | None:
    m = re.fullmatch(r'PT(\d+)M([\d.]+)S', clock or '')
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def kind(a: dict) -> str:
    if a.get('isFieldGoal') == 1:
        return 'field_goal'
    t = (a.get('actionType') or '').strip().lower()
    return re.sub(r'[^a-z0-9]+', '_', t).strip('_') or 'unknown'


def num(v) -> int | None:
    try:
        return int(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def starters(c) -> None:
    rows, attempts = [], []
    for f in sorted((CACHE / 'boxscoretraditionalv3').glob('*.json')):
        b = json.loads(f.read_text()).get('boxScoreTraditional') or {}
        got = []
        for side in ('homeTeam', 'awayTeam'):
            t = b.get(side) or {}
            for p in t.get('players') or []:
                if (p.get('position') or '').strip():
                    got.append((f.stem, t.get('teamId'), p.get('personId'), p['position'].strip()))
        rows += got
        attempts.append((f.stem, 'admitted' if len(got) == 10 else 'no_data', f'{len(got)} starters'))
    c.execute('CREATE OR REPLACE TABLE raw.nba_gap_starter (game_id VARCHAR, team_id BIGINT, person_id BIGINT, starting_position VARCHAR)')
    if rows:
        c.executemany('INSERT INTO raw.nba_gap_starter VALUES (?,?,?,?)', rows)
    log(c, 'starting_lineup', attempts)
    print('starters', len(rows), 'games with 10:', sum(a[1] == 'admitted' for a in attempts), 'of', len(attempts))


def pbp(c) -> None:
    have_ev = {r[0] for r in c.execute('SELECT DISTINCT game_id FROM core.fact_play_event').fetchall()}
    have_shot = {r[0] for r in c.execute('SELECT DISTINCT game_id FROM core.fact_shot').fetchall()}
    box = set(c.execute('SELECT game_id, person_id FROM core.fact_player_game_stats').fetchall())
    teams = set(c.execute('SELECT game_id, team_id FROM core.bridge_game_team').fetchall())
    team_of = dict(((g, p), t) for g, p, t in c.execute('SELECT game_id, person_id, min(team_id) FROM core.fact_player_game_stats GROUP BY 1, 2').fetchall())
    games = {r[0] for r in c.execute('SELECT game_id FROM core.dim_game').fetchall()}
    ev, sh, att_pbp, att_shot = [], [], [], []
    for f in sorted((CACHE / 'playbyplayv3').glob('*.json')):
        g = f.stem
        acts = (json.loads(f.read_text()).get('game') or {}).get('actions') or []
        if g not in games:
            continue
        n_ev = n_sh = 0
        for a in acts:
            s, per, an = secs(a.get('clock')), num(a.get('period')), num(a.get('actionNumber'))
            if s is None or not per or per <= 0 or an is None or an < 0:
                continue
            pid, tid = num(a.get('personId')) or None, num(a.get('teamId')) or None
            if pid and not tid:
                tid = team_of.get((g, pid))
            tid = tid if (g, tid) in teams else None
            person = pid if pid and (g, pid) in box else None
            actor = 'person' if person else 'team' if tid else 'unresolved' if pid else 'not_reported'
            fg = a.get('isFieldGoal') == 1
            val = num(a.get('shotValue')) if fg else None
            val = val if val in (2, 3) else None
            made = (a.get('shotResult') == 'Made') if fg and val else None
            if g not in have_ev:
                ev.append((g, an, per, a.get('clock'), s, kind(a), a.get('actionType'), a.get('subType'), a.get('description'),
                           tid, person, actor, fg, val, made, num(a.get('scoreHome')), num(a.get('scoreAway'))))
                n_ev += 1
            dist = num(a.get('shotDistance'))
            if fg and val and person and tid and g not in have_shot and dist is not None and 0 <= dist <= 110:
                x, y = num(a.get('xLegacy')), num(a.get('yLegacy'))
                geo = 'unavailable' if x is None or y is None else 'ambiguous_zero' if x == 0 and y == 0 else 'reported'
                key = hashlib.sha256(json.dumps({'source': 'nba_stats_pbp_v3', 'game_id': g, 'action_number': an}).encode()).digest()
                sh.append((key, g, tid, person, per, int(s), val, made, a.get('subType'), a.get('area'), a.get('areaDetail'), None, dist,
                           x if geo == 'reported' else None, y if geo == 'reported' else None, geo))
                n_sh += 1
        if g not in have_ev:
            att_pbp.append((g, 'admitted' if n_ev else 'no_data', f'{n_ev} actions'))
        if g not in have_shot:
            att_shot.append((g, 'admitted' if n_sh else 'no_data', f'{n_sh} shots'))
    if ev:
        c.executemany(f'INSERT INTO core.fact_play_event VALUES ({",".join("?" * 17)})', ev)
    if sh:
        c.executemany(f'INSERT INTO core.fact_shot VALUES ({",".join("?" * 16)})', sh)
    log(c, 'play_by_play', att_pbp)
    log(c, 'shot_chart', att_shot)
    print('play events', len(ev), 'games', sum(a[1] == 'admitted' for a in att_pbp), 'of', len(att_pbp),
          '| shots', len(sh), 'games', sum(a[1] == 'admitted' for a in att_shot), 'of', len(att_shot))


def log(c, domain: str, attempts: list[tuple]) -> None:
    c.execute('DELETE FROM audit.gap_attempt WHERE domain = ? AND source = ?', [domain, SOURCE])
    if attempts:
        c.executemany('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)', [(domain, g, SOURCE, o, n) for g, o, n in attempts])


if __name__ == '__main__':
    with warehouse_lock():
        con = connect(DEFAULT_OUT)
        starters(con)
        pbp(con)
        con.execute('CHECKPOINT')
        con.close()
