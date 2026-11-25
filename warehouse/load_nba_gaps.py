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


LLIMLLIB = CACHE.parent / 'llimllib' / 'player_game_status'


def llimllib_starters(c) -> None:
    """raw.llimllib_starter: starters (NBA.com boxscoretraditionalv3 starter flag) from llimllib/nba_data season files
    (https://basketball-data.billmill.org/nba/stats/player_game_status/season=<YYYY>/data.parquet). No positions."""
    files = sorted(LLIMLLIB.glob('*.parquet'))
    if not files:
        return
    c.execute(f'''CREATE OR REPLACE TABLE raw.llimllib_starter AS
        SELECT lpad(game_id, 10, '0') AS game_id, try_cast(team_id AS BIGINT) AS team_id, try_cast(player_id AS BIGINT) AS person_id
        FROM read_parquet({[str(f) for f in files]}) WHERE starter
        QUALIFY count(*) OVER (PARTITION BY game_id, team_id) = 5''')
    print('llimllib starters', c.execute('SELECT count(*), count(DISTINCT game_id) FROM raw.llimllib_starter').fetchone())


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
        seen: set[int] = set()
        for a in acts:
            s, per, an = secs(a.get('clock')), num(a.get('period')), num(a.get('actionNumber'))
            if s is None or not per or per <= 0 or an is None or an < 0 or an in seen:
                continue  # repeated action numbers: the first one is kept
            seen.add(an)
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


def officials(c) -> None:
    """raw.nba_gap_official: referees (NBA ids) from cached boxscoresummaryv3 responses; promoted into core.fact_game_official
    by promote_domains.py for games the NBA box-score summary data lacks."""
    rows, attempts = [], []
    for f in sorted((CACHE / 'boxscoresummaryv3').glob('*.json')):
        o = (json.loads(f.read_text()).get('boxScoreSummary') or {}).get('officials') or []
        got = [(f.stem, num(x.get('personId')), (x.get('firstName') or '').strip(), (x.get('familyName') or '').strip(),
                (x.get('jerseyNum') or '').strip() or None) for x in o if num(x.get('personId'))]
        rows += got
        attempts.append((f.stem, 'admitted' if got else 'no_data', f'{len(got)} officials'))
    c.execute('CREATE OR REPLACE TABLE raw.nba_gap_official (game_id VARCHAR, official_id BIGINT, first_name VARCHAR, last_name VARCHAR, jersey_num VARCHAR)')
    if rows:
        c.executemany('INSERT INTO raw.nba_gap_official VALUES (?,?,?,?,?)', rows)
    log(c, 'officials', attempts)
    print('officials from NBA.com v3:', len(rows), 'rows,', sum(a[1] == 'admitted' for a in attempts), 'of', len(attempts), 'games')


def mins(v) -> float | None:
    m = re.fullmatch(r'(\d+):(\d+)(?:\.\d+)?', (v or '').strip())
    return int(m.group(1)) + int(m.group(2)) / 60 if m else None


def boxes(c) -> None:
    """Team and player box rows from cached boxscoretraditionalv3 responses, for team-games core has no rows for.
    A team's totals are admitted only when its points equal the official final; its player rows only when the players'
    points add up to it and every player is in core.dim_person."""
    gaps = {r for r in c.execute('''SELECT domain, game_id FROM audit.game_coverage
        WHERE domain IN ('team_box', 'player_box') AND status IN ('missing_recoverable', 'partial', 'source_unavailable')''').fetchall()}
    score = {(g, t): s for g, t, s in c.execute('''SELECT g.game_id, g.home_team_id, r.home_score FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
        UNION ALL SELECT g.game_id, g.away_team_id, r.away_score FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)''').fetchall()}
    teams = set(c.execute('SELECT game_id, team_id FROM core.bridge_game_team').fetchall())
    have_t = set(c.execute('SELECT game_id, team_id FROM core.fact_team_game_stats').fetchall())
    have_p = set(c.execute('SELECT DISTINCT game_id, team_id FROM core.fact_player_game_stats').fetchall())
    persons = {r[0] for r in c.execute('SELECT person_id FROM core.dim_person').fetchall()}
    trows, prows, att = [], [], {'team_box': {}, 'player_box': {}}
    for dom, g in sorted(gaps):
        f = CACHE / 'boxscoretraditionalv3' / f'{g}.json'
        if not f.exists():
            continue
        b = json.loads(f.read_text()).get('boxScoreTraditional') or {}
        got = 0
        for side, opp in (('homeTeam', 'awayTeam'), ('awayTeam', 'homeTeam')):
            t, o = b.get(side) or {}, b.get(opp) or {}
            tid, oid, st = num(t.get('teamId')), num(o.get('teamId')), t.get('statistics') or {}
            if (g, tid) not in teams or score.get((g, tid)) is None or st.get('points') != score[(g, tid)]:
                continue
            if any((st.get(m) or 0) > (st.get(a) or 0) for m, a in (('fieldGoalsMade', 'fieldGoalsAttempted'),
                   ('threePointersMade', 'threePointersAttempted'), ('freeThrowsMade', 'freeThrowsAttempted'))):
                continue  # attempts not recorded (old-era placeholders): not admitted
            win, home = score[(g, tid)] > (score.get((g, oid)) or 0), side == 'homeTeam'
            if dom == 'team_box' and (g, tid) not in have_t:
                trows.append((g, tid, home, win, mins(st.get('minutes')), st['points'], st.get('fieldGoalsMade'), st.get('fieldGoalsAttempted'),
                              st.get('threePointersMade'), st.get('threePointersAttempted'), st.get('freeThrowsMade'), st.get('freeThrowsAttempted'),
                              st.get('reboundsOffensive'), st.get('reboundsDefensive'), st.get('reboundsTotal'), st.get('assists'), st.get('turnovers'),
                              st.get('steals'), st.get('blocks'), st.get('foulsPersonal')))
                have_t.add((g, tid))
                got += 1
            players = t.get('players') or []
            if (dom == 'player_box' and (g, tid) not in have_p and players and all(num(p.get('personId')) in persons for p in players)
                    and sum((p.get('statistics') or {}).get('points') or 0 for p in players) == st['points']):
                for p in players:
                    s = p.get('statistics') or {}
                    m = mins(s.get('minutes'))
                    prows.append((g, num(p['personId']), tid, oid, home, win, 'played' if m else 'dnp', (p.get('position') or '').strip() or None,
                                  (p.get('comment') or '').strip() or None, m, m, s.get('points'), s.get('assists'), s.get('blocks'), s.get('steals'),
                                  s.get('turnovers'), s.get('fieldGoalsAttempted'), s.get('fieldGoalsMade'), s.get('threePointersAttempted'),
                                  s.get('threePointersMade'), s.get('freeThrowsAttempted'), s.get('freeThrowsMade'), s.get('reboundsOffensive'),
                                  s.get('reboundsDefensive'), s.get('reboundsTotal'), s.get('foulsPersonal'), num(s.get('plusMinusPoints'))))
                have_p.add((g, tid))
                got += 1
        att[dom][g] = (g, 'admitted' if got else 'no_data', f'{got} team-games')
    if trows:
        c.executemany('''INSERT INTO core.fact_team_game_stats (game_id, team_id, is_home, is_win, min, pts, fgm, fga, fg3m, fg3a, ftm, fta,
            oreb, dreb, reb, ast, tov, stl, blk, pf) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', trows)
    if prows:
        c.executemany('''INSERT INTO core.fact_player_game_stats (game_id, person_id, team_id, opponent_team_id, is_home, is_win, participation_status,
            starting_position, comment, num_minutes, reported_num_minutes, points, assists, blocks, steals, turnovers, fga, fgm, fg3a, fg3m,
            fta, ftm, oreb, dreb, reb, fouls_personal, plus_minus) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', prows)
    for dom, a in att.items():
        log(c, dom, list(a.values()))
    print('box rows from NBA.com: team', len(trows), '| player', len(prows), 'rows')


def log(c, domain: str, attempts: list[tuple]) -> None:
    c.execute('DELETE FROM audit.gap_attempt WHERE domain = ? AND source = ?', [domain, SOURCE])
    if attempts:
        c.executemany('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)', [(domain, g, SOURCE, o, n) for g, o, n in attempts])


if __name__ == '__main__':
    with warehouse_lock():
        con = connect(DEFAULT_OUT)
        starters(con)
        llimllib_starters(con)
        pbp(con)
        boxes(con)
        officials(con)
        con.execute('CHECKPOINT')
        con.close()
