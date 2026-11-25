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


def periods(c) -> None:
    """Quarter and overtime scores from cached boxscoresummaryv3 responses, for games with no period scores. Admitted only
    when both teams report the same number of periods and each team's periods add up to its official final (games stopped
    early, such as a 2-period preseason game, are admitted when they add up)."""
    have = {r[0] for r in c.execute('SELECT DISTINCT game_id FROM core.fact_game_period_score').fetchall()}
    final = {g: (h, a, ht, at) for g, h, a, ht, at in c.execute('''SELECT g.game_id, r.home_score, r.away_score, g.home_team_id, g.away_team_id
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)''').fetchall()}
    rows, attempts = [], []
    for f in sorted((CACHE / 'boxscoresummaryv3').glob('*.json')):
        g = f.stem
        if g not in final:
            continue
        b = json.loads(f.read_text()).get('boxScoreSummary') or {}
        if g in have:  # already filled: record 'admitted' when the stored periods are exactly these
            stored = sorted(c.execute('SELECT team_id, period_number, points FROM core.fact_game_period_score WHERE game_id = ?', [g]).fetchall())
            mine = sorted((num((b.get(s_) or {}).get('teamId')), num(p.get('period')), num(p.get('score')))
                          for s_ in ('homeTeam', 'awayTeam') for p in (b.get(s_) or {}).get('periods') or [])
            if stored and stored == mine:
                attempts.append((g, 'admitted', 'periods add up to the final'))
            continue
        hs, aws, ht, at = final[g]
        lines = []
        for side, tid, pts in (('homeTeam', ht, hs), ('awayTeam', at, aws)):
            t = b.get(side) or {}
            ps = sorted(((num(p.get('period')), num(p.get('score'))) for p in t.get('periods') or []), key=lambda x: x[0] or 0)
            ps = [(n_, s) for n_, s in ps if n_ and s is not None]
            lines.append((num(t.get('teamId')), tid, pts, ps))
        ok = (all(l[0] == l[1] for l in lines) and len(lines[0][3]) == len(lines[1][3]) >= 1
              and all(sum(s for _, s in l[3]) == l[2] for l in lines))
        if ok:
            rows += [(g, l[1], n_, s) for l in lines for n_, s in l[3]]
        attempts.append((g, 'admitted' if ok else 'no_data' if not lines[0][3] else 'mismatch',
                         'periods add up to the final' if ok else 'no periods' if not lines[0][3] else 'periods do not add up'))
    if rows:
        c.executemany('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)', rows)
    log(c, 'period_scores', attempts)
    print('quarter scores from NBA.com v3:', sum(a[1] == 'admitted' for a in attempts), 'of', len(attempts), 'games')


def pbp_starters(c) -> None:
    """raw.pbp_inferred_starter: starters derived from NBA play-by-play for lineup gaps ('SUB: In FOR Out' events carry the
    outgoing player's id; the incoming surname is resolved within the team's box score). A player is a starter when his
    first first-quarter appearance comes before he is ever substituted in. Admitted only when that gives exactly 5 players per team."""
    c.execute('CREATE TABLE IF NOT EXISTS raw.pbp_inferred_starter (game_id VARCHAR, team_id BIGINT, person_id BIGINT)')
    prior: dict[str, list[tuple]] = {}
    for row in c.execute('SELECT game_id, team_id, person_id FROM raw.pbp_inferred_starter').fetchall():
        prior.setdefault(row[0], []).append(row)
    gaps = [r[0] for r in c.execute("""SELECT game_id FROM audit.game_coverage WHERE domain = 'starting_lineup'
        AND status IN ('missing_recoverable', 'source_unavailable') AND game_id IN (SELECT game_id FROM core.fact_play_event)
        UNION SELECT game_id FROM raw.pbp_inferred_starter""").fetchall()]  # earlier inferences are re-derived, not dropped
    rows, attempts = [], []
    for g in gaps:
        roster: dict[int, dict[str, list[int]]] = {}
        for t, p, name in c.execute("""SELECT s.team_id, s.person_id, d.display_name FROM core.fact_player_game_stats s
                JOIN core.dim_person d USING (person_id) WHERE s.game_id = ?""", [g]).fetchall():
            roster.setdefault(t, {}).setdefault(re.sub(r'[^a-z]', '', (name or '').split()[-1].lower()), []).append(p)
        first: dict[tuple, str] = {}
        unresolved_sub = False
        # first quarter only: lineup changes between periods are not logged as substitutions
        for an, kind_, desc, t, p in c.execute("""SELECT action_number, action_kind, description, team_id, person_id
                FROM core.fact_play_event WHERE game_id = ? AND period = 1 ORDER BY seconds_remaining DESC, action_number""", [g]).fetchall():
            if kind_ == 'substitution':
                m = re.match(r'SUB:\s*(.+?)\s+FOR\s+', desc or '', re.I) or re.search(
                    r'\bSubstitution replaced by\s+(.+?)\s*$', desc or '', re.I)
                label = re.sub(r'[^a-z]', '', m.group(1).split()[-1].lower()) if m else ''
                ids = roster.get(t, {}).get(label, [])
                if len(ids) != 1 or p is None:
                    unresolved_sub = True
                    break
                first.setdefault((t, ids[0]), 'in')
            if p and t in roster:
                first.setdefault((t, p), 'start')
        per = {}
        for (t, p), how in first.items():
            if how == 'start':
                per.setdefault(t, []).append(p)
        ok = not unresolved_sub and len(per) == 2 and all(len(v) == 5 for v in per.values())
        if not ok and g in prior:
            rows.extend(prior[g])
            continue
        if ok:
            rows += [(g, t, p) for t, ps in per.items() for p in ps]
        attempts.append((g, 'admitted' if ok else 'no_data', 'inferred from play-by-play' if ok else
                         'play-by-play does not give 5 starters per team'))
    c.execute('BEGIN')
    try:
        c.execute('DELETE FROM raw.pbp_inferred_starter')
        if rows:
            c.executemany('INSERT INTO raw.pbp_inferred_starter VALUES (?,?,?)', rows)
        for g, outcome, note in attempts:
            c.execute("DELETE FROM audit.gap_attempt WHERE domain = 'starting_lineup' AND source = 'pbp_inferred' AND game_id = ?", [g])
            c.execute("INSERT INTO audit.gap_attempt VALUES ('starting_lineup', ?, 'pbp_inferred', now(), ?, ?)", [g, outcome, note])
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise
    print('starters inferred from play-by-play:', sum(a[1] == 'admitted' for a in attempts), 'of', len(attempts), 'games')


def team_from_players(c) -> None:
    """Team box totals summed from the warehouse's own player rows, for team-games with no team row. Admitted only when the
    summed points equal the team's official final and every summed stat is reported for every player (else left null)."""
    c.execute("""CREATE OR REPLACE TEMP TABLE tp AS
        SELECT s.game_id, s.team_id, s.is_home, s.is_win, sum(s.points) AS pts, count(*) AS n,
               CASE WHEN count(s.fgm) = count(*) THEN sum(s.fgm) END AS fgm, CASE WHEN count(s.fga) = count(*) THEN sum(s.fga) END AS fga,
               CASE WHEN count(s.fg3m) = count(*) THEN sum(s.fg3m) END AS fg3m, CASE WHEN count(s.fg3a) = count(*) THEN sum(s.fg3a) END AS fg3a,
               CASE WHEN count(s.ftm) = count(*) THEN sum(s.ftm) END AS ftm, CASE WHEN count(s.fta) = count(*) THEN sum(s.fta) END AS fta,
               CASE WHEN count(s.oreb) = count(*) THEN sum(s.oreb) END AS oreb, CASE WHEN count(s.dreb) = count(*) THEN sum(s.dreb) END AS dreb,
               CASE WHEN count(s.reb) = count(*) THEN sum(s.reb) END AS reb, CASE WHEN count(s.assists) = count(*) THEN sum(s.assists) END AS ast,
               CASE WHEN count(s.turnovers) = count(*) THEN sum(s.turnovers) END AS tov, CASE WHEN count(s.steals) = count(*) THEN sum(s.steals) END AS stl,
               CASE WHEN count(s.blocks) = count(*) THEN sum(s.blocks) END AS blk, CASE WHEN count(s.fouls_personal) = count(*) THEN sum(s.fouls_personal) END AS pf
        FROM core.fact_player_game_stats s
        WHERE s.game_id IN (SELECT game_id FROM audit.game_coverage WHERE domain = 'team_box' AND status <> 'present')
          AND (s.game_id, s.team_id) NOT IN (SELECT game_id, team_id FROM core.fact_team_game_stats)
        GROUP BY ALL""")
    c.execute("""CREATE OR REPLACE TEMP TABLE tp_ok AS SELECT tp.* FROM tp JOIN core.dim_game g USING (game_id) JOIN core.fact_game_result r USING (game_id)
        WHERE tp.pts = CASE WHEN tp.team_id = g.home_team_id THEN r.home_score ELSE r.away_score END
          AND coalesce(tp.fgm, 0) <= coalesce(tp.fga, 99999) AND coalesce(tp.fg3m, 0) <= coalesce(tp.fg3a, 99999) AND coalesce(tp.ftm, 0) <= coalesce(tp.fta, 99999)""")
    c.execute("""INSERT INTO core.fact_team_game_stats BY NAME SELECT game_id, team_id, is_home, is_win, pts, fgm, fga, fg3m, fg3a, ftm, fta,
        oreb, dreb, reb, ast, tov, stl, blk, pf FROM tp_ok""")
    c.execute("DELETE FROM audit.gap_attempt WHERE domain = 'team_box' AND source = 'player_box_sum' AND game_id IN (SELECT game_id FROM tp)")
    c.execute("""INSERT INTO audit.gap_attempt SELECT DISTINCT 'team_box', game_id, 'player_box_sum', now(),
        CASE WHEN (game_id, team_id) IN (SELECT game_id, team_id FROM tp_ok) THEN 'admitted' ELSE 'mismatch' END,
        'team totals summed from player rows' FROM tp""")
    print('team boxes summed from player rows:', c.execute('SELECT count(*) FROM tp_ok').fetchone()[0], 'of', c.execute('SELECT count(*) FROM tp').fetchone()[0])


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
    # players of visiting foreign clubs carry NBA.com person ids but are not in core.dim_person: add them (with a
    # provenance row in raw.nba_gap_person) when their team's player points add up to its official final
    c.execute('CREATE TABLE IF NOT EXISTS raw.nba_gap_person (person_id BIGINT PRIMARY KEY, display_name VARCHAR, game_id VARCHAR, team_id BIGINT)')
    new_people, espn_names = [], None
    for dom, g in sorted(gaps):
        f = CACHE / 'boxscoretraditionalv3' / f'{g}.json'
        if dom != 'player_box' or not f.exists():
            continue
        b = json.loads(f.read_text()).get('boxScoreTraditional') or {}
        for side in ('homeTeam', 'awayTeam'):
            t = b.get(side) or {}
            tid, players = num(t.get('teamId')), t.get('players') or []
            if (g, tid) in teams and players and score.get((g, tid)) == sum((p.get('statistics') or {}).get('points') or 0 for p in players):
                for p in players:
                    pid = num(p.get('personId'))
                    if pid and pid not in persons:
                        if espn_names is None or espn_names[0] != (g, side):
                            from load_espn import verified_names_by_stat_line  # NBA.com publishes no names for these players
                            espn_names = ((g, side), verified_names_by_stat_line(c, g, side == 'homeTeam', players))
                        name = (f"{(p.get('firstName') or '').strip()} {(p.get('familyName') or '').strip()}".strip()
                                or espn_names[1].get(pid) or f'Unnamed player (NBA.com id {pid}, {t.get("teamTricode") or "visiting club"})')
                        new_people.append((pid, name, g, tid))
                        persons.add(pid)
    if new_people:
        c.executemany('INSERT INTO core.dim_person (person_id, display_name) VALUES (?, ?)', [(p, n) for p, n, _, _ in new_people])
        c.executemany('INSERT OR IGNORE INTO raw.nba_gap_person VALUES (?,?,?,?)', new_people)
    print('persons added from NBA.com boxes:', len(new_people))
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


def archived_player_names(c, unresolved: set[int]) -> dict[int, set[tuple[str, int, str]]]:
    from html.parser import HTMLParser

    folder = CACHE.parent / 'nba-placeholders-legacy'
    directories = (
        ('petteri-koponen-20161009035904-original.html', '2f5b4a0919f50cdf295e7a2e54bf25764c5cf0fec1cdf78d5e77e159d9260171'),
        ('nba-players-20161012211755-original.html', '18ecf2ca1e1852e8e210fb782d6484484aab45aaa155db00cca2f8288f4bbda4'),
    )
    names: dict[int, set[str]] = {}

    class PlayerLinks(HTMLParser):
        def handle_starttag(self, tag, attributes):
            attrs = dict(attributes)
            if tag != 'a' or 'playerList' not in (attrs.get('class') or '').split():
                return
            link = re.fullmatch(r'/players/[^?#]+/(\d+)', attrs.get('href') or '')
            title = (attrs.get('title') or '').strip()
            if link and title and int(link[1]) in unresolved:
                names.setdefault(int(link[1]), set()).add(title.title() if title.isupper() else title)

    for filename, digest in directories:
        path = folder / filename
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f'reviewed official NBA player directory bytes have changed: {filename}')
        PlayerLinks().feed(raw.decode('utf-8'))
    candidates = {pid: next(iter(labels)) for pid, labels in names.items() if len(labels) == 1}
    if not candidates:
        return {}
    fixtures = c.execute('''SELECT person_id,game_id,team_id FROM raw.nba_gap_person
        WHERE person_id IN (SELECT unnest(?))''', [list(candidates)]).fetchall()
    matches: dict[int, set[tuple[str, int, str]]] = {}
    for pid, game, team in fixtures:
        matches.setdefault(pid, set()).add((game, team, candidates[pid]))
    return matches


def repair_foreign_names(c) -> None:
    """Resolve previously unnamed NBA IDs using exact same-side matches across the cached fixtures."""
    from load_espn import norm, verified_names_by_stat_line

    people = dict(c.execute('SELECT person_id, display_name FROM core.dim_person').fetchall())
    unresolved = {pid for pid, name in people.items() if (name or '').startswith('Unnamed player (')}
    confirmed_spellings = {
        1962937476: ('Maxin Esteban', 'Màxim Esteban', 'maxim-esteban-spelling-receipt.json',
                     '02e0ab96e8ee495ce2c8d88ea5ab41d48ee03e2149a0ac4656b704cdecc17aa1', 'fc_barcelona_annual_report'),
        1962937541: ('Ofek Azital', 'Ofek Avital', 'ofek-avital-israel-basketball-2016-17.html',
                     'c84b2b78ed1430350cd33e9c332898ab8d4295a2d72fd4ba83ba97d79d8bfc2b', 'israeli_basketball_league_roster'),
    }
    unresolved.update(pid for pid, (reported, *_) in confirmed_spellings.items() if people.get(pid) == reported)
    if not unresolved:
        return
    exact_profiles = archived_player_names(c, unresolved)
    evidence = []
    for file in sorted((CACHE / 'boxscoretraditionalv3').glob('*.json')):
        box = json.loads(file.read_text()).get('boxScoreTraditional') or {}
        for side in ('homeTeam', 'awayTeam'):
            team = box.get(side) or {}
            players = team.get('players') or []
            pids = {num(p.get('personId')) for p in players}
            if not pids & unresolved:
                continue
            evidence.append((file.stem, side, team.get('teamId'), players, pids))
    resolutions: dict[int, dict[str, set[tuple[str, int, str]]]] = {}
    known = {pid: name for pid, name in people.items() if name and pid not in unresolved}
    for pid, witnesses in exact_profiles.items():
        known[pid] = next(iter(witnesses))[2]
        resolutions[pid] = {norm(known[pid]): witnesses}
        unresolved.remove(pid)
    spelling_receipts = []
    for pid, (reported, corrected, filename, digest, source) in confirmed_spellings.items():
        spelling = CACHE.parent / 'nba-placeholders-legacy' / filename
        if pid in exact_profiles and known[pid] == reported and spelling.is_file():
            if hashlib.sha256(spelling.read_bytes()).hexdigest() != digest:
                raise ValueError(f'reviewed spelling evidence has changed: {filename}')
            known[pid] = corrected
            spelling_receipts.extend((pid, game, team, corrected, source) for game, team, _ in exact_profiles[pid])
    while unresolved:
        proposals: dict[int, dict[str, set[tuple[str, int, str]]]] = {}
        for game, side, tid, players, pids in evidence:
            if not pids & unresolved:
                continue
            for pid, name in verified_names_by_stat_line(c, game, side == 'homeTeam', players, known).items():
                if pid in unresolved and norm(name):
                    proposals.setdefault(pid, {}).setdefault(norm(name), set()).add((game, tid, name))
        accepted = {pid: candidates for pid, candidates in proposals.items() if len(candidates) == 1}
        if not accepted:
            break
        for pid, candidate in accepted.items():
            known[pid] = min(next(iter(candidate.values())))[2]
            unresolved.remove(pid)
        resolutions.update(accepted)
    if not resolutions:
        return
    c.execute('BEGIN')
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS audit.nba_gap_person_name_receipt (
            person_id BIGINT, game_id VARCHAR, team_id BIGINT, display_name VARCHAR, source VARCHAR, admitted_at TIMESTAMPTZ,
            PRIMARY KEY (person_id, game_id, team_id, source))''')
        for pid, candidate in resolutions.items():
            c.execute('UPDATE core.dim_person SET display_name = ? WHERE person_id = ?', [known[pid], pid])
            c.execute('UPDATE raw.nba_gap_person SET display_name = ? WHERE person_id = ?', [known[pid], pid])
            c.executemany('INSERT OR IGNORE INTO audit.nba_gap_person_name_receipt VALUES (?,?,?,?,?,now())',
                          [(pid, game, tid, name, 'archived_nba_player_id' if pid in exact_profiles else 'espn_same_fixture')
                           for game, tid, name in sorted(next(iter(candidate.values())))])
        if spelling_receipts:
            c.executemany('INSERT OR IGNORE INTO audit.nba_gap_person_name_receipt VALUES (?,?,?,?,?,now())', spelling_receipts)
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise
    print('NBA.com blank player names resolved from verified fixtures and profiles:', len(resolutions), flush=True)


def log(c, domain: str, attempts: list[tuple]) -> None:
    # replace only the games logged now, so earlier 'admitted' records survive runs that skip already-filled games
    if attempts:
        c.execute('CREATE OR REPLACE TEMP TABLE relog (game_id VARCHAR)')
        c.executemany('INSERT INTO relog VALUES (?)', [(a[0],) for a in attempts])
        c.execute('DELETE FROM audit.gap_attempt WHERE domain = ? AND source = ? AND game_id IN (SELECT game_id FROM relog)', [domain, SOURCE])
        c.executemany('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)', [(domain, g, SOURCE, o, n) for g, o, n in attempts])


if __name__ == '__main__':
    with warehouse_lock():
        con = connect(DEFAULT_OUT)
        starters(con)
        llimllib_starters(con)
        pbp(con)
        boxes(con)
        officials(con)
        periods(con)
        pbp_starters(con)
        con.execute('CHECKPOINT')
        con.close()
