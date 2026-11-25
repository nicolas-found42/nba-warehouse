"""Load archived NBA.com game pages (fetch_wayback_nba.py) for games with gaps. Source rank: archived nba.com.

  boxscore.html  referees ("Officials: #129 Tre Maddox Maddox, ...") -> raw.wayback_game_official
                 starters (players with a position)                 -> raw.wayback_starter
  gameinfo.html  quarter line (visitor row, then home row)          -> core.fact_game_period_score, only when both rows
                 add up to the official finals
  playbyplay.html rows: visitor play | clock [TEAM a-b] | home play -> core.fact_play_event and core.fact_shot (no
                 coordinates: geometry 'unavailable'), only when the running score ends at the official final

Players are named by surname (and initial in the box); they are resolved within the game's warehouse box score for
that team, and left unresolved when ambiguous. Attempts go to audit.gap_attempt (source 'nba_com_archive').

  uv run --no-project --with duckdb==2.0.0 python warehouse/load_wayback_nba.py
"""
from __future__ import annotations

import hashlib
import html as htmllib
import json
import re
from collections import Counter

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock
from fetch_wayback_nba import CACHE, fixture_code, team_code

SOURCE = 'nba_com_archive'


def norm(s: str) -> str:
    return re.sub(r'[^a-z]', '', (s or '').lower())


def text(h: str) -> str:
    return re.sub(r'\s+', ' ', htmllib.unescape(re.sub(r'<[^>]+>', ' ', h))).strip()


def box_players(page: str) -> list[dict]:
    out = []
    for m in re.finditer(r'<td[^>]*id="name_pl_(vtm|htm)-(\d+)-(\d+)"[^>]*>(.*?)</td>(.*?)</tr>', page, re.S):
        side, _, _, name_html, rest = m.groups()
        cells = dict(re.findall(r'id="(\w+?)_pl_[vh]tm-\d+-\d+"[^>]*>(.*?)</td>', rest, re.S))
        slug = re.search(r'/playerfile/([a-z0-9_]+)/', name_html)
        out.append({'side': 'away' if side == 'vtm' else 'home', 'name': text(name_html), 'slug': slug.group(1) if slug else '',
                    'pos': text(cells.get('pos', ''))})
    if out:
        return out
    return [dict(side=side, name=name, slug=slug, pos=cells[1])
            for side, rows in legacy_box(page).items() for name, slug, cells in rows]


def legacy_box(page: str) -> dict[str, list[tuple[str, str, list[str]]]]:
    """The SportsTicker layout has two player tables in visitor, home order."""
    teams = []
    for table in re.findall(r'<table\b[^>]*class="gSGTablenull"[^>]*>(.*?)</table>', page, re.S | re.I):
        players = []
        for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>', table, re.S | re.I):
            slug = re.search(r'/playerfile/([a-z0-9_]+)/', tr, re.I)
            cells = [text(cell) for cell in re.findall(r'<td\b[^>]*>(.*?)</td>', tr, re.S | re.I)]
            if slug and len(cells) >= 2 and cells[0]:
                players.append((cells[0], slug[1], cells))
        if players:
            teams.append(players)
    return dict(zip(('away', 'home'), teams)) if len(teams) == 2 else {}


def officials(page: str) -> list[tuple[str, str]]:
    m = re.search(r'Officials:(.*?)(Attendance|Duration|</div>|</span>)', page, re.S | re.I)
    if not m:
        for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>', page, re.S | re.I):
            cells = [text(cell) for cell in re.findall(r'<td\b[^>]*>(.*?)</td>', row, re.S | re.I)]
            if len(cells) == 2 and cells[0].lower().rstrip(':') == 'officials':
                names = [n.strip() for n in re.split(r'[,;]|\s+and\s+', cells[1])]
                return [('', n) for n in names if len(n.split()) >= 2]
        return []
    out = []
    for num, name in re.findall(r'#\s*(\d+)\s*([^,#]+)', text(m.group(1))):
        words = name.split()
        if len(words) >= 3 and words[-1] == words[-2]:  # "Tre Maddox Maddox": surname repeated
            words = words[:-1]
        if len(words) >= 2:
            out.append((num, ' '.join(words)))
    return out


def line_score(page: str) -> list[list[int]]:
    rows = []
    for tr in re.findall(r'<tr>(.*?)</tr>', page.split('nbaGIQtrScrs', 1)[-1][:6000], re.S):
        nums = re.findall(r'class="nbaGIQtrPts(?:Last)?">\s*(\d+)\s*<', tr)
        if nums:
            rows.append([int(n) for n in nums])
    return rows[:2]


def legacy_line_score(page: str) -> list[list[int]]:
    section = re.search(r'SCORE BY PERIOD\s*</div>(.*?)</table>', page, re.S | re.I)
    if not section:
        return []
    rows = []
    for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>', section[1], re.S | re.I):
        if 'gSGTeamLink' not in tr:
            continue
        nums = [text(cell) for cell in re.findall(r'<td\b[^>]*>(.*?)</td>', tr, re.S | re.I)][1:]
        if len(nums) != 9 or not all(n.isascii() and n.isdecimal() for n in nums):
            return []
        rows.append([int(n) for n in nums])
    if len(rows) == 2:
        while len(rows[0]) > 5 and rows[0][-2] == rows[1][-2] == 0:
            for row in rows:
                del row[-2]  # unused columns in the fixed four-overtime layout
    return rows


def secs(clock: str) -> float | None:
    m = re.fullmatch(r'(\d+):(\d+(?:\.\d+)?)', clock.strip())
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


KINDS = (('free throw', 'free_throw'), ('rebound', 'rebound'), ('foul', 'foul'), ('turnover', 'turnover'), ('substitution', 'substitution'),
         ('timeout', 'timeout'), ('jump ball', 'jump_ball'), ('violation', 'violation'))


def kind(desc: str) -> tuple[str, bool]:
    d = desc.lower()
    if 'free throw' in d:
        return 'free_throw', False
    if re.search(r':\s*(made|missed)\b', d) and re.search(r'\b(?:shot|jumper|jump|layup|dunk|hook|tip|fadeaway|fade away|3pt)\b', d):
        return 'field_goal', True
    for k, v in KINDS:
        if k in d:
            return v, False
    return re.sub(r'[^a-z0-9]+', '_', d.split(':')[0]).strip('_')[:40] or 'unknown', False


def pbp_rows(page: str) -> list[tuple]:
    """(period, clock, scoring team code, a, b, side, description) per play, in page order."""
    out, period = [], 0
    for tr in re.findall(r'<tr\b[^>]*class=[\'\"]?pbpRow[OE]\b[^>]*>(.*?)</tr>', page, re.S | re.I):
        t = text(tr)
        pm = re.search(r'Start of (\d+)(?:st|nd|rd|th) Quarter|Start of (?:(\d+)(?:st|nd|rd|th)?\s*)?(?:Overtime|OT)', t, re.I)
        if pm:
            period = int(pm[1]) if pm[1] else 4 + int(pm[2] or 1)
            continue
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) != 3 or not period:
            continue
        mid = text(tds[1])
        cm = re.fullmatch(r'(\d+:\d+(?:\.\d+)?)\s*(?:\[([A-Z0-9]{2,4})\s+(\d+)\s*[-–]\s*(\d+)\])?', mid, re.I)
        if not cm:
            continue
        for side, cell in (('away', tds[0]), ('home', tds[2])):
            d = text(cell)
            if d:
                out.append((period, cm.group(1), cm.group(2), cm.group(3), cm.group(4), side, d))
    return out


def score_for_tag(code: str | None, x: str | None, y: str | None, away: str, home: str) -> tuple[int, int] | None:
    if code is None or x is None or y is None:
        return None
    if team_code(code) == team_code(home):
        return int(x), int(y)
    if team_code(code) == team_code(away):
        return int(y), int(x)
    return None


def unverified_page_outcome(folder_key: str, pages: tuple[str, ...]) -> tuple[str, str]:
    day, folder = folder_key.split('/')
    cdx = CACHE / 'cdx' / f'{day}.json'
    if cdx.is_file():
        try:
            header, *captures = json.loads(cdx.read_text())
        except (ValueError, TypeError):
            return 'unverified_capture', 'archive index invalid'
        if header[:3] == ['original', 'timestamp', 'statuscode'] and len(captures) < 5000:
            for url, *_ in captures:
                m = re.search(r'/games/(\d{8})/([a-z]{6})/([a-z_]+\.html)(?:\?|$)', url, re.I)
                if m and m[1] == day and m[3].lower() in pages and (team_code(m[2][:3]), team_code(m[2][3:])) == (
                        team_code(folder[:3]), team_code(folder[3:])):
                    return 'unverified_capture', 'archive index lists a capture without validated local bytes'
            return 'not_found', 'archive index lists no capture for the requested page and fixture'
    return 'unverified_capture', 'archive index or relevant capture unavailable'


def build_index(c) -> dict:
    """folder -> game_id: the dominant NBA game id printed on the archived box page; otherwise the one warehouse game that
    day whose visitor and home codes match the folder name (old codes mapped to current ones)."""
    abbr, dates = {}, {}
    for gid, d, ha, aa, ht, at in c.execute('''SELECT g.game_id, strftime(g.game_date, '%Y%m%d'), th.abbreviation, ta.abbreviation,
            g.home_team_id, g.away_team_id FROM core.dim_game g
            JOIN core.dim_team th ON th.team_id = g.home_team_id JOIN core.dim_team ta ON ta.team_id = g.away_team_id
            WHERE g.game_date BETWEEN '1996-10-01' AND '2010-07-01' ''').fetchall():
        dates[gid] = d
        h, a = fixture_code(ha, ht), fixture_code(aa, at)
        if a and h:
            abbr.setdefault((d, a, h), []).append(gid)
    out = {}
    for f in sorted((CACHE / 'pages').glob('*/*')):
        d, folder = f.parent.name, f.name
        ids = []
        if (f / 'boxscore.html').exists():
            ids = re.findall(r'\b(00[1-4]\d{7})\b', (f / 'boxscore.html').read_text(errors='replace'))
        a, h = folder[:3], folder[3:]
        cand = abbr.get((d, team_code(a), team_code(h)), [])
        ranked = Counter(ids).most_common(2)
        if ranked and ranked[0][1] >= 10 and (len(ranked) == 1 or ranked[0][1] > 3 * ranked[1][1]):
            gid = ranked[0][0]
            if dates.get(gid) == d and (not cand or gid in cand):
                out[f'{d}/{folder}'] = gid
                continue
        out[f'{d}/{folder}'] = cand[0] if len(cand) == 1 else None
    return out


def main() -> None:
    with warehouse_lock():
        with connect(DEFAULT_OUT) as c:
            load(c, build_index(c))
            c.execute('CHECKPOINT')


def load(c, index: dict) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS audit.nba_archive_game_receipt (
        game_id VARCHAR, page VARCHAR, sha256 VARCHAR, play_rows INTEGER, shot_rows INTEGER, captured_at TIMESTAMPTZ,
        PRIMARY KEY (game_id, page, sha256))''')
    replay = {(g, page, digest): (plays, shots) for g, page, digest, plays, shots in c.execute('''
        SELECT game_id,page,sha256,play_rows,shot_rows FROM audit.nba_archive_game_receipt
        WHERE play_rows > 0 AND game_id IN (SELECT game_id FROM audit.gap_attempt WHERE domain='play_by_play' AND outcome='admitted'
            GROUP BY game_id HAVING count(DISTINCT source)=1 AND min(source)='nba_com_archive')''').fetchall()}
    by_game = {g: k for k, g in index.items() if g}
    gaps = {(d, g) for d, g in c.execute("""SELECT domain, game_id FROM audit.game_coverage
        WHERE status NOT IN ('present', 'not_tracked', 'not_applicable')
        UNION SELECT 'officials', game_id FROM core.fact_game_official GROUP BY game_id HAVING count(*) < 3""").fetchall()}
    gm = {g: (h, a, hs, aws) for g, h, a, hs, aws in c.execute('''SELECT g.game_id, g.home_team_id, g.away_team_id, r.home_score, r.away_score
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)''').fetchall()}
    box: dict[tuple, list] = {}
    for g, t, p, name in c.execute('''SELECT s.game_id, s.team_id, s.person_id, d.display_name FROM core.fact_player_game_stats s
            JOIN core.dim_person d USING (person_id) WHERE s.game_id IN (SELECT unnest(?))''', [list(by_game)]).fetchall():
        box.setdefault((g, t), []).append((p, name or ''))
    have_ev = {r[0] for r in c.execute('SELECT DISTINCT game_id FROM core.fact_play_event').fetchall()}
    have_shot = {r[0] for r in c.execute('SELECT DISTINCT game_id FROM core.fact_shot').fetchall()}
    have_per = {r[0] for r in c.execute('SELECT DISTINCT game_id FROM core.fact_game_period_score').fetchall()}

    def resolve(g: str, team: int, label: str) -> int | None:
        """'C. Anthony' or 'Anthony' or 'Nene' -> the one box-score player on that team it can be."""
        words = [w for w in re.split(r'[\s.]+', label) if w]
        if not words:
            return None
        last, first = norm(words[-1]), norm(words[0]) if len(words) > 1 else ''
        if last == 'artest' and any(pid == 1897 for pid, _ in box.get((g, team), [])):
            return 1897  # Ron Artest's NBA ID is unchanged after the name change.
        cands = [p for p, n in box.get((g, team), []) if norm(n.split()[-1] if n.split() else '') == last or norm(n) == norm(label)]
        if len(cands) > 1 and first:
            cands = [p for p, n in box.get((g, team), []) if p in cands and norm(n)[:len(first)] == first]
        return cands[0] if len(cands) == 1 else None

    from promote_domains import OFFICIAL_NAME_CANON
    canon = dict(c.execute(f'SELECT name, official_id FROM {OFFICIAL_NAME_CANON}').fetchall())
    by_jersey: dict[tuple, set] = {}
    for oid, last, jersey in c.execute('SELECT official_id, last_name, trim(jersey_num) FROM core.dim_official').fetchall():
        by_jersey.setdefault((jersey, norm(last)), set()).add(oid)

    def official_id(name: str, jersey: str) -> int | None:
        """Name first; else the one referee with this jersey number and surname (catches misspellings such as 'Delrde')."""
        if norm(name) in canon:
            return canon[norm(name)]
        ids = by_jersey.get((jersey, norm(name.split()[-1])), set())
        return next(iter(ids)) if len(ids) == 1 else None

    refs, starters, per_rows, ev, sh, att, receipts = [], [], [], [], [], [], []
    repairs, replaced_shots, anomalies = [], [], []
    for g, key in sorted(by_game.items()):
        if g not in gm:
            continue
        home, away, hs, aws = gm[g]
        side_team = {'home': home, 'away': away}
        d = CACHE / 'pages' / key
        bpage = (d / 'boxscore.html').read_text(errors='replace') if (d / 'boxscore.html').exists() else ''
        manual = (d / 'manual_boxscore.html').read_text(errors='replace') if (d / 'manual_boxscore.html').exists() else ''
        candidates = [bpage, manual]
        if ('officials', g) in gaps or ('starting_lineup', g) in gaps:
            candidates.extend(f.read_text(errors='replace') for f in sorted((CACHE / 'snap' / key).glob('*boxscore.html')))
        if ('officials', g) in gaps:
            o = next((names for page in candidates if (names := officials(page))), [])
            crew = [(g, n, j + 1, num, oid) for j, (num, n) in enumerate(o) if (oid := official_id(n, num))]
            refs += crew
            outcome, note = (('admitted' if len({r[4] for r in crew}) >= 3 else 'no_data'), f'{len(crew)} of {len(o)} archived referees resolved') if any(candidates) else unverified_page_outcome(key, ('boxscore.html', 'manual_boxscore.html', 'gameinfo.html'))
            att.append(('officials', g, outcome, note))
        if ('starting_lineup', g) in gaps:
            st = [(side_team[p['side']], resolve(g, side_team[p['side']], p['name']))
                  for p in (box_players(bpage) or box_players(manual)) if p['pos']]
            teams_ok = [t for t in (home, away) if len({p for team, p in st if team == t and p}) == 5
                        and sum(team == t for team, _ in st) == 5]
            starters += [(g, t, p) for t, p in st if t in teams_ok and p]
            outcome, note = ('admitted' if len(teams_ok) == 2 else 'no_data', f'{len(teams_ok)} teams with 5 resolved starters') if any(candidates) else unverified_page_outcome(key, ('boxscore.html', 'manual_boxscore.html'))
            att.append(('starting_lineup', g, outcome, note))
        gpage = (d / 'gameinfo.html').read_text(errors='replace') if (d / 'gameinfo.html').exists() else ''
        if ('period_scores', g) in gaps and g not in have_per:
            ls = line_score(gpage) or legacy_line_score(bpage) or legacy_line_score(manual)
            ok = False
            if len(ls) == 2 and len(ls[0]) == len(ls[1]) >= 5:
                (av, at), (hv, ht) = (ls[0][:-1], ls[0][-1]), (ls[1][:-1], ls[1][-1])
                ok = (at, ht) == (aws, hs) and sum(av) == aws and sum(hv) == hs and all(sum(hv[:i]) == sum(av[:i]) for i in range(4, len(hv)))
                if ok:
                    per_rows += [(g, home, i + 1, v) for i, v in enumerate(hv)] + [(g, away, i + 1, v) for i, v in enumerate(av)]
            outcome, note = ('admitted' if ok else 'no_data' if not ls else 'mismatch', 'archived line score') if gpage or bpage or manual else unverified_page_outcome(key, ('gameinfo.html', 'boxscore.html', 'manual_boxscore.html'))
            att.append(('period_scores', g, outcome, note))
        p_raw = (d / 'playbyplay.html').read_bytes() if (d / 'playbyplay.html').exists() else b''
        digest = hashlib.sha256(p_raw).hexdigest()
        archive_page = key + '/playbyplay.html'
        expected = replay.get((g, archive_page, digest))
        ppage = p_raw.decode(errors='replace')
        need_ev, need_sh = ('play_by_play', g) in gaps and g not in have_ev, ('shot_chart', g) in gaps and g not in have_shot
        if need_ev or need_sh or expected:
            rows = pbp_rows(ppage)
            # The already-matched game folder names the two sides even if gameinfo was never archived.
            folder = d.name
            acode, hcode = folder[:3], folder[3:]
            h_sc = a_sc = 0
            g_ev, g_sh, g_anomalies = [], [], []
            for n, (per, clock, code, x, y, side, desc) in enumerate(rows):
                scores = score_for_tag(code, x, y, acode, hcode)
                if scores is not None:
                    dh, da = scores[0] - h_sc, scores[1] - a_sc
                    if dh < 0 or da < 0 or dh + da > 3 or (dh and da):
                        g_anomalies.append((g, n, h_sc, a_sc, scores[0], scores[1], digest))
                    h_sc, a_sc = scores
                k, fg = kind(desc)
                tid = side_team[side]
                who = re.split(r'\s+(?:Jump|Layup|Dunk|Hook|Tip|Fadeaway|Driving|Running|Reverse|Turnaround|Alley|Finger|Slam|Pullup|Step|3pt|'
                               r'Free|Rebound|Foul|Turnover|Substitution|Violation|Timeout|Bank|Floating|Putback|Jumper|Shot|Personal|'
                               r'Offensive|Defensive|Technical|Lost|Bad|Traveling|Kicked|Goaltending|Fade\s+Away|Jump\s+Ball)\b', desc, maxsplit=1)[0]
                pid = resolve(g, tid, who) if who and who != desc else None
                actor = 'person' if pid else 'team' if re.match(r'^team\b', desc, re.I) else 'unresolved'
                val = (3 if '3pt' in desc.lower() else 2) if fg else None
                made = bool(re.search(r':\s*made\b', desc, re.I)) if fg else None
                s = secs(clock)
                if s is None:
                    continue
                g_ev.append((g, n, per, f'PT{int(s // 60):02d}M{s % 60:05.2f}S', s, k, desc.split(':')[0][:60], None, desc, tid, pid, actor,
                             fg, val, made, *(scores or (None, None))))
                if fg and pid:
                    key_ = hashlib.sha256(json.dumps({'source': SOURCE, 'game_id': g, 'action_number': n}).encode()).digest()
                    g_sh.append((key_, g, tid, pid, per, int(s), val, made, desc.split(':')[0][:60], None, None, None, None, None, None, 'unavailable'))
            complete = bool(g_ev) and (h_sc, a_sc) == (hs, aws)
            if not p_raw:
                outcome, note = unverified_page_outcome(key, ('playbyplay.html',))
                att.extend((domain, g, outcome, note) for domain, need in (('play_by_play', need_ev), ('shot_chart', need_sh)) if need)
                continue
            if need_ev:
                if complete:
                    ev += g_ev
                att.append(('play_by_play', g, 'admitted' if complete else 'no_data' if not rows else 'mismatch',
                            f'{len(g_ev)} archived plays' + ('' if complete or not rows else f', running score ends {h_sc}-{a_sc}')))
            if need_sh:
                if complete and g_sh:
                    sh += g_sh
                att.append(('shot_chart', g, 'admitted' if complete and g_sh else 'no_data' if complete or not rows else 'mismatch', f'{len(g_sh)} shots'))
            if complete and (need_ev or need_sh or expected):
                anomalies.extend(g_anomalies)
                receipts.append((g, archive_page, digest, len(g_ev) if need_ev else 0, len(g_sh) if need_sh else 0))
            if complete and expected and len(g_ev) == expected[0]:
                old = c.execute('SELECT action_number,description FROM core.fact_play_event WHERE game_id = ? ORDER BY action_number', [g]).fetchall()
                if old == [(r[1], r[8]) for r in g_ev]:
                    repairs.append(g)
                    ev.extend(g_ev)
                    if expected[1] and not need_sh:
                        keys = [hashlib.sha256(json.dumps({'source': SOURCE, 'game_id': g, 'action_number': r[1]}).encode()).digest() for r in g_ev]
                        replaced_shots.extend(keys)
                        sh.extend(g_sh)
                        receipts[-1] = (g, archive_page, digest, len(g_ev), len(g_sh))
    c.execute('BEGIN')
    try:
        for name, ddl, rows_ in (('wayback_game_official', 'game_id VARCHAR, official_name VARCHAR, official_order INTEGER, jersey_num VARCHAR, official_id BIGINT', refs),
                                 ('wayback_starter', 'game_id VARCHAR, team_id BIGINT, person_id BIGINT', starters)):
            c.execute(f'CREATE TABLE IF NOT EXISTS raw.{name} ({ddl})')
            if rows_:
                if name == 'wayback_starter':
                    c.execute('CREATE OR REPLACE TEMP TABLE archive_starter_teams (game_id VARCHAR, team_id BIGINT)')
                    c.executemany('INSERT INTO archive_starter_teams VALUES (?,?)', sorted({r[:2] for r in rows_}))
                    c.execute('DELETE FROM raw.wayback_starter WHERE (game_id,team_id) IN (SELECT game_id,team_id FROM archive_starter_teams)')
                else:
                    c.execute(f'DELETE FROM raw.{name} WHERE game_id IN (SELECT unnest(?))', [sorted({r[0] for r in rows_})])
                c.executemany(f'INSERT INTO raw.{name} VALUES ({",".join("?" * len(rows_[0]))})', rows_)
        if per_rows:
            c.executemany('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)', per_rows)
        if ev:
            if repairs:
                c.execute('DELETE FROM core.fact_play_event WHERE game_id IN (SELECT unnest(?))', [repairs])
            c.executemany(f'INSERT INTO core.fact_play_event VALUES ({",".join("?" * 17)})', ev)
        if sh:
            if replaced_shots:
                c.execute('DELETE FROM core.fact_shot WHERE shot_key IN (SELECT unnest(?))', [replaced_shots])
            c.executemany(f'INSERT INTO core.fact_shot VALUES ({",".join("?" * 16)})', sh)
        c.execute('''CREATE TABLE IF NOT EXISTS audit.nba_archive_game_receipt (
            game_id VARCHAR, page VARCHAR, sha256 VARCHAR, play_rows INTEGER, shot_rows INTEGER, captured_at TIMESTAMPTZ,
            PRIMARY KEY (game_id, page, sha256))''')
        if receipts:
            c.executemany('''INSERT INTO audit.nba_archive_game_receipt VALUES (?,?,?,?,?,now())
                ON CONFLICT (game_id, page, sha256) DO UPDATE SET
                    play_rows = greatest(play_rows, excluded.play_rows), shot_rows = greatest(shot_rows, excluded.shot_rows)''', receipts)
        c.execute('''CREATE TABLE IF NOT EXISTS audit.nba_archive_score_anomaly (
            game_id VARCHAR, action_number INTEGER, previous_home INTEGER, previous_away INTEGER,
            printed_home INTEGER, printed_away INTEGER, sha256 VARCHAR, PRIMARY KEY (game_id,action_number,sha256))''')
        if anomalies:
            c.executemany('INSERT OR IGNORE INTO audit.nba_archive_score_anomaly VALUES (?,?,?,?,?,?,?)', anomalies)
        if att:
            c.execute('CREATE OR REPLACE TEMP TABLE archive_attempt (domain VARCHAR, game_id VARCHAR)')
            c.executemany('INSERT INTO archive_attempt VALUES (?,?)', [(a[0], a[1]) for a in att])
            c.execute('DELETE FROM audit.gap_attempt WHERE source = ? AND (domain, game_id) IN (SELECT domain, game_id FROM archive_attempt)', [SOURCE])
            c.executemany('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)', [(a[0], a[1], SOURCE, a[2], a[3]) for a in att])
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise
    print('archived nba.com: referees', len(refs), '| starters', len(starters), '| quarter rows', len(per_rows), '| plays', len(ev), '| shots', len(sh),
          '| attempts', sorted({(a[0], a[2]) for a in att}))


if __name__ == '__main__':
    main()
