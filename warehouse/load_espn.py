"""Load cached ESPN summaries (fetch_espn.py) for games no other source covers. ESPN ranks below every other source.

  raw.espn_api_game_map     warehouse game_id -> ESPN event id (date + both final scores, one-to-one)
  core.fact_play_event      ESPN plays, only for games with no play events, and only when the log's running score reaches
                            the official final for both teams (otherwise logged 'mismatch')
  core.fact_shot            ESPN shooting plays with a resolved shooter, only for games with no shot rows; shot_key hashes
                            source 'espn_api'. ESPN puts the rim at (25, 0) in feet: x = (x - 25) * 10, y = y * 10 tenths of a
                            foot; (25, 0) is also ESPN's placeholder, so those shots are 'ambiguous_zero'
  raw.espn_api_game_official  referee names, promoted by promote_domains.py (OFFICIAL_NAME_CANON) for games with none
  raw.espn_api_starter      starters (5 resolved per team), promoted by promote_domains.py for games with none
  audit.espn_api_fill       games whose play-by-play / shots came from ESPN (kept across runs)
  audit.gap_attempt         source 'espn_api': admitted / no_data per game and domain, not_found for unmatched games

ESPN athletes are resolved per game: by name against the warehouse box score for that game and team, then by
raw.bridge_player_espn_base (unambiguous ids). Unresolved athletes leave the event's person empty ('unresolved').

  uv run --no-project --with duckdb==2.0.0 python warehouse/load_espn.py
"""
from __future__ import annotations

import hashlib
import json
import re

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock
from fetch_espn import CACHE, DOMAINS

SOURCE = 'espn_api'


def norm(name: str) -> str:
    n = re.sub(r'[^a-z ]', '', (name or '').lower())
    return re.sub(r'\s+(jr|sr|ii|iii|iv)$', '', n.strip()).replace(' ', '')


def clock(v: str) -> tuple[str | None, float | None]:
    m = re.fullmatch(r'(\d+):(\d+(?:\.\d+)?)', (v or '').strip()) or re.fullmatch(r'()(\d+(?:\.\d+)?)', (v or '').strip())
    if not m:
        return None, None
    mins, secs = int(m.group(1) or 0), float(m.group(2))
    return f'PT{mins:02d}M{secs:05.2f}S', mins * 60 + secs


def kind(t: str, shooting: bool) -> str:
    tl = t.lower()
    if 'free throw' in tl:
        return 'free_throw'
    if shooting:
        return 'field_goal'
    for key, k in (('rebound', 'rebound'), ('foul', 'foul'), ('substitution', 'substitution'), ('timeout', 'timeout'),
                   ('end period', 'period'), ('end game', 'period'), ('jump ball', 'jump_ball'), ('turnover', 'turnover'),
                   ('violation', 'violation')):
        if key in tl:
            return k
    return re.sub(r'[^a-z0-9]+', '_', tl).strip('_') or 'unknown'


def main() -> None:
    matches = json.loads((CACHE / 'matches.json').read_text())
    with warehouse_lock():
        c = connect(DEFAULT_OUT)
        load(c, matches)
        c.execute('CHECKPOINT')
        c.close()


def load(c, matches: dict) -> None:
    c.execute('CREATE OR REPLACE TABLE raw.espn_api_game_map (game_id VARCHAR PRIMARY KEY, espn_event_id VARCHAR)')
    if matches:
        c.executemany('INSERT INTO raw.espn_api_game_map VALUES (?,?)', list(matches.items()))
    games = {r[0]: r[1:] for r in c.execute('SELECT game_id, home_team_id, away_team_id FROM core.dim_game').fetchall()}
    finals = {g: (h, a) for g, h, a in c.execute('SELECT game_id, home_score, away_score FROM core.fact_game_result').fetchall()}
    have_ev = {r[0] for r in c.execute('SELECT DISTINCT game_id FROM core.fact_play_event').fetchall()}
    have_shot = {r[0] for r in c.execute('SELECT DISTINCT game_id FROM core.fact_shot').fetchall()}
    box: dict[tuple, dict] = {}
    for g, t, p, name in c.execute('''SELECT s.game_id, s.team_id, s.person_id, d.display_name FROM core.fact_player_game_stats s
            JOIN core.dim_person d USING (person_id) WHERE s.game_id IN (SELECT game_id FROM raw.espn_api_game_map)''').fetchall():
        box.setdefault((g, t), {}).setdefault(norm(name), set()).add(p)
    in_box = set(c.execute('''SELECT game_id, person_id FROM core.fact_player_game_stats
        WHERE game_id IN (SELECT game_id FROM raw.espn_api_game_map)''').fetchall())
    persons = {r[0] for r in c.execute('SELECT person_id FROM core.dim_person').fetchall()}
    bridge = dict(c.execute('''SELECT espn_athlete_id, any_value(player_id) FROM raw.bridge_player_espn_base
        WHERE NOT is_ambiguous AND NOT is_unresolved AND player_id IS NOT NULL GROUP BY 1 HAVING count(DISTINCT player_id) = 1''').fetchall())
    ev, sh, refs, starters, att = [], [], [], [], []
    for gid, eid in sorted(matches.items()):
        f = CACHE / 'summary' / f'{eid}.json'
        if gid not in games or not f.exists():
            continue
        d = json.loads(f.read_text())
        home, away = games[gid]
        side = {x['team']['id']: (home if x.get('homeAway') == 'home' else away)
                for x in ((d.get('header') or {}).get('competitions') or [{}])[0].get('competitors') or []}
        # athletes -> persons, per team
        who, start = {}, {}
        for t in (d.get('boxscore') or {}).get('players') or []:
            tid = side.get((t.get('team') or {}).get('id'))
            for s in t.get('statistics') or []:
                for a in s.get('athletes') or []:
                    aid, name = (a.get('athlete') or {}).get('id'), (a.get('athlete') or {}).get('displayName')
                    cand = box.get((gid, tid), {}).get(norm(name), set())
                    pid = next(iter(cand)) if len(cand) == 1 else bridge.get(aid)
                    if aid and pid in persons:
                        who[aid] = (pid, tid)
                        if a.get('starter'):
                            start.setdefault(tid, []).append(pid)
        n_st = 0
        for tid, ps in start.items():
            if len(set(ps)) == 5 and tid:
                starters += [(gid, tid, p) for p in set(ps)]
                n_st += 1
        att.append(('starting_lineup', gid, 'admitted' if n_st == 2 else 'no_data', f'{n_st} teams with 5 starters'))
        o = [(gid, (x.get('fullName') or x.get('displayName') or '').strip(), x.get('order')) for x in (d.get('gameInfo') or {}).get('officials') or []]
        refs += [r for r in o if r[1]]
        att.append(('officials', gid, 'admitted' if o else 'no_data', f'{len(o)} officials'))
        n_ev = n_sh = 0
        seen: set[int] = set()
        plays = d.get('plays') or []
        hs = [p.get('homeScore') for p in plays if isinstance(p.get('homeScore'), int)]
        aws = [p.get('awayScore') for p in plays if isinstance(p.get('awayScore'), int)]
        if plays and (max(hs, default=None), max(aws, default=None)) != finals.get(gid):
            # running score never reaches the official final: incomplete or inconsistent log, not admitted
            att += [(dm, gid, 'mismatch', 'ESPN running score does not reach the final') for dm in ('play_by_play', 'shot_chart')
                    if (dm == 'play_by_play' and gid not in have_ev) or (dm == 'shot_chart' and gid not in have_shot)]
            plays = []
        for p in plays:
            try:
                an = int(p.get('sequenceNumber'))
            except (TypeError, ValueError):
                continue
            per = (p.get('period') or {}).get('number')
            iso, secs = clock((p.get('clock') or {}).get('displayValue'))
            if an in seen or not per or secs is None:
                continue
            seen.add(an)
            text, shooting = (p.get('type') or {}).get('text') or '', bool(p.get('shootingPlay'))
            k = kind(text, shooting)
            tid = side.get((p.get('team') or {}).get('id'))
            aid = ((p.get('participants') or [{}])[0].get('athlete') or {}).get('id')
            pid = who.get(aid, (None, None))[0] if aid else None
            pid = pid if (gid, pid) in in_box else None  # play and shot rows need the player in this game's box score
            tid = tid or (who.get(aid, (None, None))[1] if aid else None)
            actor = 'person' if pid else 'team' if tid else 'unresolved' if aid else 'not_reported'
            fg = k == 'field_goal'
            val = p.get('pointsAttempted') if fg and p.get('pointsAttempted') in (2, 3) else None
            if not val and fg:
                val = 3 if 'three point' in (p.get('text') or '').lower() else 2
            made = bool(p.get('scoringPlay')) if fg else None
            if gid not in have_ev:
                ev.append((gid, an, per, iso, secs, k, text, None, p.get('text'), tid, pid, actor, fg, val if fg else None, made,
                           p.get('homeScore'), p.get('awayScore')))
                n_ev += 1
            co = p.get('coordinate') or {}
            x, y = co.get('x'), co.get('y')
            if fg and pid and tid and gid not in have_shot:
                ok = isinstance(x, (int, float)) and isinstance(y, (int, float)) and 0 <= x <= 50 and 0 <= y <= 94
                geo = 'unavailable' if not ok else 'ambiguous_zero' if (x, y) == (25, 0) else 'reported'
                m = re.search(r'(\d+)-foot', p.get('text') or '')
                dist = int(m.group(1)) if m else (round(((x - 25) ** 2 + y ** 2) ** 0.5) if geo == 'reported' else None)
                key = hashlib.sha256(json.dumps({'source': SOURCE, 'game_id': gid, 'action_number': an}).encode()).digest()
                sh.append((key, gid, tid, pid, per, int(secs), val, made, text, None, None, None, dist,
                           round((x - 25) * 10) if geo == 'reported' else None, round(y * 10) if geo == 'reported' else None, geo))
                n_sh += 1
        if gid not in have_ev and not any(a[:2] == ('play_by_play', gid) for a in att[-2:]):
            att.append(('play_by_play', gid, 'admitted' if n_ev else 'no_data', f'{n_ev} plays'))
        if gid not in have_shot and not any(a[:2] == ('shot_chart', gid) for a in att[-2:]):
            att.append(('shot_chart', gid, 'admitted' if n_sh else 'no_data', f'{n_sh} shots'))
    q = ','.join(f"'{x}'" for x in DOMAINS)
    for (gid,) in c.execute(f'''SELECT DISTINCT gc.game_id FROM audit.game_coverage gc JOIN core.dim_game g USING (game_id)
            WHERE gc.domain IN ({q}) AND gc.status IN ('missing_recoverable', 'partial', 'source_unavailable')
              AND g.game_date >= '2001-10-01' ''').fetchall():
        if gid not in matches:
            att += [(dom, gid, 'not_found', 'no ESPN event with this date and score') for dom in DOMAINS]
    if ev:
        c.executemany(f'INSERT INTO core.fact_play_event VALUES ({",".join("?" * 17)})', ev)
    if sh:
        c.executemany(f'INSERT INTO core.fact_shot VALUES ({",".join("?" * 16)})', sh)
    c.execute('CREATE OR REPLACE TABLE raw.espn_api_game_official (game_id VARCHAR, official_name VARCHAR, official_order INTEGER)')
    if refs:
        c.executemany('INSERT INTO raw.espn_api_game_official VALUES (?,?,?)', refs)
    c.execute('CREATE OR REPLACE TABLE raw.espn_api_starter (game_id VARCHAR, team_id BIGINT, person_id BIGINT)')
    if starters:
        c.executemany('INSERT INTO raw.espn_api_starter VALUES (?,?,?)', starters)
    # games ESPN filled stay recorded across runs (later runs skip them because they already have rows)
    c.execute('CREATE TABLE IF NOT EXISTS audit.espn_api_fill (domain VARCHAR, game_id VARCHAR, espn_event_id VARCHAR, filled_at TIMESTAMPTZ, PRIMARY KEY (domain, game_id))')
    filled = [(dm, g, matches[g]) for dm, g, o, _ in att if o == 'admitted' and dm in ('play_by_play', 'shot_chart')]
    if filled:
        c.executemany('INSERT OR IGNORE INTO audit.espn_api_fill VALUES (?,?,?,now())', filled)
    done = set(c.execute("SELECT domain, game_id FROM audit.espn_api_fill").fetchall())
    att = [a for a in att if (a[0], a[1]) not in done] + [(dm, g, 'admitted', 'ESPN rows loaded') for dm, g in sorted(done)]
    c.execute('DELETE FROM audit.gap_attempt WHERE source = ?', [SOURCE])
    if att:
        c.executemany('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)', [(dm, g, SOURCE, o, n) for dm, g, o, n in att])
    print('espn: play events', len(ev), '| shots', len(sh), '| officials', len(refs), '| starters', len(starters))


if __name__ == '__main__':
    main()
