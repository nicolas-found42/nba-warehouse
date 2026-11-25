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
from collections import Counter

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from fetch_espn import CACHE, DOMAINS, validate_summary

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


def espn_athletes(gid: str, home: bool) -> list[tuple[str, str, dict, str | None]]:
    """ESPN ID, displayed name, stats and DNP reason for one matched fixture side."""
    matches = json.loads((CACHE / 'matches.json').read_text()) if (CACHE / 'matches.json').exists() else {}
    f = CACHE / 'summary' / f'{matches.get(gid)}.json'
    if gid not in matches or not f.exists():
        return []
    d = json.loads(f.read_text())
    want = 'home' if home else 'away'
    ids = {x['team']['id'] for x in ((d.get('header') or {}).get('competitions') or [{}])[0].get('competitors') or [] if x.get('homeAway') == want}
    out = []
    for t in (d.get('boxscore') or {}).get('players') or []:
        if (t.get('team') or {}).get('id') not in ids:
            continue
        for st in t.get('statistics') or []:
            keys = st.get('keys') or []
            for a in st.get('athletes') or []:
                v = dict(zip(keys, a.get('stats') or []))
                athlete = a.get('athlete') or {}
                if not athlete.get('id') or not athlete.get('displayName'):
                    continue
                out.append((athlete['id'], athlete['displayName'], v, a.get('reason') if a.get('didNotPlay') else None))
    return out


STAT_FIELDS = {
    'points': 'points', 'rebounds': 'reboundsTotal', 'assists': 'assists', 'turnovers': 'turnovers',
    'steals': 'steals', 'blocks': 'blocks', 'offensiveRebounds': 'reboundsOffensive',
    'defensiveRebounds': 'reboundsDefensive', 'fouls': 'foulsPersonal', 'plusMinus': 'plusMinusPoints',
}
SHOOTING_FIELDS = {
    'fieldGoalsMade-fieldGoalsAttempted': ('fieldGoalsMade', 'fieldGoalsAttempted'),
    'threePointFieldGoalsMade-threePointFieldGoalsAttempted': ('threePointersMade', 'threePointersAttempted'),
    'freeThrowsMade-freeThrowsAttempted': ('freeThrowsMade', 'freeThrowsAttempted'),
}


def numeric(value) -> int | None:
    s = str(value).strip()
    return int(s.split('.', 1)[0]) if re.fullmatch(r'[+-]?\d+(?:\.0+)?', s) else None


def played_line_matches(nba: dict, espn: dict) -> bool:
    stamp = re.fullmatch(r'(?:PT(?:(\d+)M)?(\d+(?:\.\d+)?)S|(\d+):(\d+(?:\.\d+)?))', nba.get('minutes') or '')
    espn_min = numeric(espn.get('minutes'))
    if not stamp or espn_min is None:
        return False
    seconds = int(stamp[1] or stamp[3] or 0) * 60 + float(stamp[2] or stamp[4])
    if not seconds or abs(seconds - espn_min * 60) >= 60:
        return False
    return all(numeric(espn.get(e)) is not None and numeric(espn[e]) == numeric(nba.get(n)) for e, n in STAT_FIELDS.items()) and all(
        numeric(nba.get(made)) is not None and numeric(nba.get(attempted)) is not None
        and re.sub(r'\s+', '', espn.get(e, '')) == f'{numeric(nba[made])}-{numeric(nba[attempted])}'
        for e, (made, attempted) in SHOOTING_FIELDS.items())


def names_by_stat_line(gid: str, home: bool, players: list[dict], known: dict[int, str] | None = None) -> dict[int, str]:
    """Resolve only reciprocal same-side stat matches and a unique corroborated roster remainder."""
    esp = espn_athletes(gid, home)
    nba = {int(p['personId']): p for p in players if p.get('personId')}
    by_espn = {eid: (name, stats, reason) for eid, name, stats, reason in esp}
    if len(nba) != len(players) or len(by_espn) != len(esp):
        return {}
    known = known or {}
    candidates = {pid: [eid for eid, (name, stats, _) in by_espn.items()
                        if (pid in known and norm(known[pid]) == norm(name))
                        or played_line_matches(p.get('statistics') or {}, stats)] for pid, p in nba.items()}
    matched = {pid: ids[0] for pid, ids in candidates.items() if len(ids) == 1
               and sum(ids[0] in c for c in candidates.values()) == 1}
    remaining_nba, remaining_espn = set(nba) - set(matched), set(by_espn) - set(matched.values())
    if len(matched) >= 5 and len(remaining_nba) == len(remaining_espn) == 1:
        pid, eid = next(iter(remaining_nba)), next(iter(remaining_espn))
        nba_comment = (nba[pid].get('comment') or '').upper()
        _, stats, reason = by_espn[eid]
        if not stats and reason and ('DNP' in nba_comment or 'DND' in nba_comment or 'NWT' in nba_comment):
            if ('NWT' in nba_comment) == ('NOT WITH TEAM' in reason.upper()):
                matched[pid] = eid
    return {pid: by_espn[eid][0] for pid, eid in matched.items()}


def verified_names_by_stat_line(c, gid: str, home: bool, players: list[dict], known: dict[int, str] | None = None) -> dict[int, str]:
    match_file = CACHE / 'matches.json'
    if not match_file.is_file():
        return {}
    matches = json.loads(match_file.read_text())
    eid = matches.get(gid)
    if eid is None or list(matches.values()).count(eid) != 1:
        return {}
    try:
        validate_summary(c, gid, json.loads((CACHE / 'summary' / f'{eid}.json').read_text()), exact_venue=False)
    except (OSError, ValueError, KeyError):
        return {}
    return names_by_stat_line(gid, home, players, known)


def main() -> None:
    matches = json.loads((CACHE / 'matches.json').read_text())
    with warehouse_lock():
        c = connect(DEFAULT_OUT)
        load(c, matches)
        c.execute('CHECKPOINT')
        c.close()


def load(c, matches: dict) -> None:
    with warehouse_transaction(c):
        load_matched(c, matches)


def load_matched(c, matches: dict) -> None:
    verified, rejected = {}, []
    event_counts = Counter(matches.values())
    for gid, eid in matches.items():
        path = CACHE / 'summary' / f'{eid}.json'
        try:
            if event_counts[eid] != 1:
                raise ValueError('source event has multiple NBA matches')
            validate_summary(c, gid, json.loads(path.read_text()), exact_venue=False)
        except (OSError, ValueError, KeyError) as e:
            rejected.append((gid, eid, str(e)))
        else:
            verified[gid] = eid
    matches = verified
    c.execute('CREATE OR REPLACE TABLE audit.espn_event_match_review (game_id VARCHAR, espn_event_id VARCHAR, reason VARCHAR)')
    if rejected:
        c.executemany('INSERT INTO audit.espn_event_match_review VALUES (?,?,?)', rejected)
    # This particular old map demonstrably credited each club with the other club's score.
    if any(gid == '0010600015' and eid == '261010020' for gid, eid, _ in rejected):
        for table in ('raw.espn_api_game_official', 'raw.espn_api_starter'):
            if c.execute('SELECT 1 FROM duckdb_tables() WHERE schema_name = ? AND table_name = ?', table.split('.')).fetchone():
                c.execute(f"DELETE FROM {table} WHERE game_id = '0010600015'")
    c.execute('CREATE OR REPLACE TABLE raw.espn_api_game_map (game_id VARCHAR PRIMARY KEY, espn_event_id VARCHAR UNIQUE)')
    if matches:
        c.executemany('INSERT INTO raw.espn_api_game_map VALUES (?,?)', list(matches.items()))
    print(f'ESPN exact fixture validation: {len(matches)} accepted, {len(rejected)} held', flush=True)
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
              AND g.game_date >= '1996-10-01' ''').fetchall():
        if gid not in matches:
            att += [(dom, gid, 'not_found', 'no ESPN event with this date and score') for dom in DOMAINS]
    # quarter scores from ESPN line scores, for games with none; each side must add up to its official final
    have_per = {r[0] for r in c.execute('SELECT DISTINCT game_id FROM core.fact_game_period_score').fetchall()}
    per_rows = []
    for (gid,) in c.execute("SELECT game_id FROM audit.game_coverage WHERE domain = 'period_scores' AND status <> 'present'").fetchall():
        f = CACHE / 'summary' / f'{matches.get(gid)}.json'
        if gid in have_per or gid not in matches or not f.exists() or gid not in games:
            continue
        comp = ((json.loads(f.read_text()).get('header') or {}).get('competitions') or [{}])[0]
        lines = {}
        for x in comp.get('competitors') or []:
            vals = [str(l.get('displayValue', '')).strip() for l in x.get('linescores') or []]
            lines[x.get('homeAway')] = [int(v) for v in vals] if vals and all(v.isdigit() for v in vals) else []
        h, a = lines.get('home') or [], lines.get('away') or []
        hs, aws = finals.get(gid, (None, None))
        ok = len(h) == len(a) >= 4 and sum(h) == hs and sum(a) == aws and all(sum(h[:i]) == sum(a[:i]) for i in range(4, len(h)))
        if ok:
            per_rows += [(gid, games[gid][0], i + 1, v) for i, v in enumerate(h)] + [(gid, games[gid][1], i + 1, v) for i, v in enumerate(a)]
        att.append(('period_scores', gid, 'admitted' if ok else 'no_data' if not h else 'mismatch',
                    'line score adds up to the final' if ok else 'no line score' if not h else 'line score does not add up'))
    if per_rows:
        c.executemany('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)', per_rows)
    if ev:
        c.executemany(f'INSERT INTO core.fact_play_event VALUES ({",".join("?" * 17)})', ev)
    if sh:
        c.executemany(f'INSERT INTO core.fact_shot VALUES ({",".join("?" * 16)})', sh)
    c.execute('CREATE TABLE IF NOT EXISTS raw.espn_api_game_official (game_id VARCHAR, official_name VARCHAR, official_order INTEGER)')
    if refs:
        c.execute('DELETE FROM raw.espn_api_game_official WHERE game_id IN (SELECT unnest(?))', [sorted({r[0] for r in refs})])
        c.executemany('INSERT INTO raw.espn_api_game_official VALUES (?,?,?)', refs)
    c.execute('CREATE TABLE IF NOT EXISTS raw.espn_api_starter (game_id VARCHAR, team_id BIGINT, person_id BIGINT)')
    if starters:
        c.execute('CREATE OR REPLACE TEMP TABLE espn_lineup_reload (game_id VARCHAR, team_id BIGINT)')
        c.executemany('INSERT INTO espn_lineup_reload VALUES (?,?)', sorted({r[:2] for r in starters}))
        c.execute('DELETE FROM raw.espn_api_starter WHERE (game_id, team_id) IN (SELECT game_id, team_id FROM espn_lineup_reload)')
        c.executemany('INSERT INTO raw.espn_api_starter VALUES (?,?,?)', starters)
    # games ESPN filled stay recorded across runs (later runs skip them because they already have rows)
    c.execute('CREATE TABLE IF NOT EXISTS audit.espn_api_fill (domain VARCHAR, game_id VARCHAR, espn_event_id VARCHAR, filled_at TIMESTAMPTZ, PRIMARY KEY (domain, game_id))')
    filled = [(dm, g, matches[g]) for dm, g, o, _ in att if o == 'admitted' and dm in ('play_by_play', 'shot_chart', 'period_scores')]
    if filled:
        c.executemany('INSERT OR IGNORE INTO audit.espn_api_fill VALUES (?,?,?,now())', filled)
    done = set(c.execute("SELECT domain, game_id FROM audit.espn_api_fill").fetchall())
    att = [a for a in att if (a[0], a[1]) not in done or a[2] == 'admitted']
    if att:
        c.execute('CREATE OR REPLACE TEMP TABLE espn_gap_reload (domain VARCHAR, game_id VARCHAR)')
        c.executemany('INSERT INTO espn_gap_reload VALUES (?,?)', sorted({a[:2] for a in att}))
        c.execute('DELETE FROM audit.gap_attempt WHERE source = ? AND (domain, game_id) IN (SELECT domain, game_id FROM espn_gap_reload)', [SOURCE])
        c.executemany('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)', [(dm, g, SOURCE, o, n) for dm, g, o, n in att])
    print('espn: quarter-score games', len(per_rows) // 8 if per_rows else 0, '| play events', len(ev), '| shots', len(sh), '| officials', len(refs), '| starters', len(starters))


if __name__ == '__main__':
    main()
