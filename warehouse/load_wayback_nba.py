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

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock
from fetch_wayback_nba import CACHE

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
    return out


def officials(page: str) -> list[tuple[str, str]]:
    m = re.search(r'Officials:(.*?)(Attendance|Duration|</div>)', page, re.S)
    if not m:
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


def secs(clock: str) -> float | None:
    m = re.fullmatch(r'(\d+):(\d+(?:\.\d+)?)', clock.strip())
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


KINDS = (('free throw', 'free_throw'), ('rebound', 'rebound'), ('foul', 'foul'), ('turnover', 'turnover'), ('substitution', 'substitution'),
         ('timeout', 'timeout'), ('jump ball', 'jump_ball'), ('violation', 'violation'))


def kind(desc: str) -> tuple[str, bool]:
    d = desc.lower()
    if 'free throw' in d:
        return 'free_throw', False
    if re.search(r':\s*(made|missed)', d) and ('shot' in d or 'layup' in d or 'dunk' in d or 'hook' in d or 'tip' in d or 'fadeaway' in d):
        return 'field_goal', True
    for k, v in KINDS:
        if k in d:
            return v, False
    return re.sub(r'[^a-z0-9]+', '_', d.split(':')[0]).strip('_')[:40] or 'unknown', False


def pbp_rows(page: str) -> list[tuple]:
    """(period, clock, scoring team code, a, b, side, description) per play, in page order."""
    out, period = [], 0
    for tr in re.findall(r'<tr class="pbpRow[OE]"[^>]*>(.*?)</tr>', page, re.S):
        t = text(tr)
        pm = re.search(r'Start of (\d)(?:st|nd|rd|th) Quarter|Start of (\d)(?:st|nd|rd|th)? ?(?:Overtime|OT)', t)
        if pm:
            period = int(pm.group(1)) if pm.group(1) else 4 + int(pm.group(2))
            continue
        tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)
        if len(tds) != 3 or not period:
            continue
        mid = text(tds[1])
        cm = re.match(r'(\d+:\d+(?:\.\d+)?)\s*(?:\[(\w{3})\s+(\d+)-(\d+)\])?', mid)
        if not cm:
            continue
        for side, cell in (('away', tds[0]), ('home', tds[2])):
            d = text(cell)
            if d:
                out.append((period, cm.group(1), cm.group(2), cm.group(3), cm.group(4), side, d))
    return out


ALIAS = {'NJN': 'BKN', 'NOH': 'NOP', 'NOK': 'NOP', 'SEA': 'OKC', 'VAN': 'MEM', 'SAN': 'SAS', 'CHH': 'CHA', 'GS': 'GSW', 'NY': 'NYK',
         'NO': 'NOP', 'SA': 'SAS', 'PHO': 'PHX', 'UTH': 'UTA'}


def build_index(c) -> dict:
    """folder -> game_id: the dominant NBA game id printed on the archived box page; otherwise the one warehouse game that
    day whose visitor and home codes match the folder name (old codes mapped to current ones)."""
    abbr = {}
    for gid, d, h, a in c.execute('''SELECT g.game_id, strftime(g.game_date, '%Y%m%d'), th.abbreviation, ta.abbreviation FROM core.dim_game g
            JOIN core.dim_team th ON th.team_id = g.home_team_id JOIN core.dim_team ta ON ta.team_id = g.away_team_id
            WHERE g.game_date BETWEEN '2001-09-01' AND '2010-07-01' ''').fetchall():
        abbr.setdefault((d, ALIAS.get(a, a), ALIAS.get(h, h)), []).append(gid)
    out = {}
    for f in sorted((CACHE / 'pages').glob('*/*')):
        d, folder = f.parent.name, f.name
        ids = []
        if (f / 'boxscore.html').exists():
            ids = re.findall(r'\b(00[1-4]\d{7})\b', (f / 'boxscore.html').read_text(errors='replace'))
        top = max(set(ids), key=ids.count) if ids else None
        if top and ids.count(top) >= 10 and ids.count(top) > 3 * max([ids.count(x) for x in set(ids) if x != top] or [0]):
            out[f'{d}/{folder}'] = top
            continue
        a, h = folder[:3], folder[3:]
        cand = abbr.get((d, ALIAS.get(a, a), ALIAS.get(h, h)), [])
        out[f'{d}/{folder}'] = cand[0] if len(cand) == 1 else None
    return out


def main() -> None:
    with warehouse_lock():
        c = connect(DEFAULT_OUT)
        load(c, index)
        c.execute('CHECKPOINT')
        c.close()


def load(c, index: dict) -> None:
    by_game = {g: k for k, g in index.items() if g}
    gaps = {(d, g) for d, g in c.execute("""SELECT domain, game_id FROM audit.game_coverage
        WHERE status NOT IN ('present', 'not_tracked', 'not_applicable')""").fetchall()}
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

    refs, starters, per_rows, ev, sh, att = [], [], [], [], [], []
    for g, key in sorted(by_game.items()):
        if g not in gm:
            continue
        home, away, hs, aws = gm[g]
        side_team = {'home': home, 'away': away}
        d = CACHE / 'pages' / key
        bpage = (d / 'boxscore.html').read_text(errors='replace') if (d / 'boxscore.html').exists() else ''
        if ('officials', g) in gaps:
            o = officials(bpage)
            refs += [(g, n, j + 1, num, official_id(n, num)) for j, (num, n) in enumerate(o)]
            att.append(('officials', g, 'admitted' if o else 'no_data', f'{len(o)} referees on the archived box page'))
        if ('starting_lineup', g) in gaps:
            st = [(side_team[p['side']], resolve(g, side_team[p['side']], p['name'])) for p in box_players(bpage) if p['pos']]
            teams_ok = [t for t in (home, away) if len([x for x in st if x[0] == t and x[1]]) == 5 and len([x for x in st if x[0] == t]) == 5]
            starters += [(g, t, p) for t, p in st if t in teams_ok and p]
            att.append(('starting_lineup', g, 'admitted' if len(teams_ok) == 2 else 'no_data', f'{len(teams_ok)} teams with 5 resolved starters'))
        gpage = (d / 'gameinfo.html').read_text(errors='replace') if (d / 'gameinfo.html').exists() else ''
        if ('period_scores', g) in gaps and g not in have_per:
            ls = line_score(gpage)
            ok = False
            if len(ls) == 2 and len(ls[0]) == len(ls[1]) >= 5:
                (av, at), (hv, ht) = (ls[0][:-1], ls[0][-1]), (ls[1][:-1], ls[1][-1])
                ok = (at, ht) == (aws, hs) and sum(av) == aws and sum(hv) == hs and all(sum(hv[:i]) == sum(av[:i]) for i in range(4, len(hv)))
                if ok:
                    per_rows += [(g, home, i + 1, v) for i, v in enumerate(hv)] + [(g, away, i + 1, v) for i, v in enumerate(av)]
            att.append(('period_scores', g, 'admitted' if ok else 'no_data' if not ls else 'mismatch', 'archived line score'))
        ppage = (d / 'playbyplay.html').read_text(errors='replace') if (d / 'playbyplay.html').exists() else ''
        need_ev, need_sh = ('play_by_play', g) in gaps and g not in have_ev, ('shot_chart', g) in gaps and g not in have_shot
        if need_ev or need_sh:
            rows = pbp_rows(ppage)
            codes = (re.search(r'class="nbaGIATeam">\s*(\w{3})', gpage), re.search(r'class="nbaGIHTeam">\s*(\w{3})', gpage))
            acode, hcode = (codes[0].group(1) if codes[0] else None), (codes[1].group(1) if codes[1] else None)
            h_sc = a_sc = 0
            g_ev, g_sh = [], []
            for n, (per, clock, code, x, y, side, desc) in enumerate(rows):
                if code and x is not None:
                    x, y = int(x), int(y)
                    if code == hcode:
                        h_sc, a_sc = x, y
                    elif code == acode:
                        a_sc, h_sc = x, y
                k, fg = kind(desc)
                tid = side_team[side]
                who = re.split(r'\s+(?:Jump|Layup|Dunk|Hook|Tip|Fadeaway|Driving|Running|Reverse|Turnaround|Alley|Finger|Slam|Pullup|Step|3pt|'
                               r'Free|Rebound|Foul|Turnover|Substitution|Violation|Timeout|Bank|Floating|Putback|Jumper|Shot|Personal|'
                               r'Offensive|Defensive|Technical|Lost|Bad|Traveling|Kicked|Goaltending|Jump\s+Ball)\b', desc, maxsplit=1)[0]
                pid = resolve(g, tid, who) if who and who != desc else None
                actor = 'person' if pid else 'team'
                val = (3 if '3pt' in desc.lower() else 2) if fg else None
                made = (': made' in desc.lower()) if fg else None
                s = secs(clock)
                if s is None:
                    continue
                g_ev.append((g, n, per, f'PT{int(s // 60):02d}M{s % 60:05.2f}S', s, k, desc.split(':')[0][:60], None, desc, tid, pid, actor,
                             fg, val, made, h_sc, a_sc))
                if fg and pid:
                    key_ = hashlib.sha256(json.dumps({'source': SOURCE, 'game_id': g, 'action_number': n}).encode()).digest()
                    g_sh.append((key_, g, tid, pid, per, int(s), val, made, desc.split(':')[0][:60], None, None, None, None, None, None, 'unavailable'))
            complete = bool(rows) and (h_sc, a_sc) == (hs, aws)
            if need_ev:
                if complete:
                    ev += g_ev
                att.append(('play_by_play', g, 'admitted' if complete else 'no_data' if not rows else 'mismatch',
                            f'{len(g_ev)} archived plays' + ('' if complete or not rows else f', running score ends {h_sc}-{a_sc}')))
            if need_sh:
                if complete and g_sh:
                    sh += g_sh
                att.append(('shot_chart', g, 'admitted' if complete and g_sh else 'no_data' if not rows else 'mismatch', f'{len(g_sh)} shots'))
    for name, ddl, rows_ in (('wayback_game_official', 'game_id VARCHAR, official_name VARCHAR, official_order INTEGER, jersey_num VARCHAR, official_id BIGINT', refs),
                             ('wayback_starter', 'game_id VARCHAR, team_id BIGINT, person_id BIGINT', starters)):
        c.execute(f'CREATE TABLE IF NOT EXISTS raw.{name} ({ddl})')
        if rows_:
            c.execute(f'DELETE FROM raw.{name} WHERE game_id IN (SELECT unnest(?))', [sorted({r[0] for r in rows_})])
            c.executemany(f'INSERT INTO raw.{name} VALUES ({",".join("?" * len(rows_[0]))})', rows_)
    if per_rows:
        c.executemany('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)', per_rows)
    if ev:
        c.executemany(f'INSERT INTO core.fact_play_event VALUES ({",".join("?" * 17)})', ev)
    if sh:
        c.executemany(f'INSERT INTO core.fact_shot VALUES ({",".join("?" * 16)})', sh)
    if att:
        c.execute('CREATE OR REPLACE TEMP TABLE relog (domain VARCHAR, game_id VARCHAR)')
        c.executemany('INSERT INTO relog VALUES (?,?)', [(a[0], a[1]) for a in att])
        c.execute('DELETE FROM audit.gap_attempt WHERE source = ? AND (domain, game_id) IN (SELECT domain, game_id FROM relog)', [SOURCE])
        c.executemany('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)', [(a[0], a[1], SOURCE, a[2], a[3]) for a in att])
    print('archived nba.com: referees', len(refs), '| starters', len(starters), '| quarter rows', len(per_rows), '| plays', len(ev), '| shots', len(sh),
          '| attempts', sorted({(a[0], a[2]) for a in att}))


if __name__ == '__main__':
    main()
