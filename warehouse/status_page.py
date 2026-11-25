"""Write a plain-language progress page for the warehouse's background jobs.

Reads only logs, cached pages and (briefly, read-only) the warehouse, then writes
data/status/index.html, which reloads itself every two minutes. Runs every 5 minutes from launchd.
Progress samples are kept in data/status/history.json so finish times come from the measured pace.

  uv run --no-project --with duckdb==2.0.0 python warehouse/status_page.py
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
OUT = DATA / 'status'
EVIDENCE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence'
CACHE = EVIDENCE / 'bref-cache'
ET = ZoneInfo('America/New_York')
SECONDS_PER_PAGE = 3.4  # 3.1 s gap (Basketball-Reference allows 20 pages/min) + fetch time; replaced by the measured pace once there is one

# Things that already happened (the one-time build), oldest first.
MILESTONES = [
    ('2026-11-24', 'Agreed the scope: NBA, BAA and ABA; one database; every gap verified or explained.'),
    ('2026-11-24', 'Built the single warehouse from the original snapshot, the last historical release (v26) and this season. 778 of 778 checks passed.'),
    ('2026-11-24', 'Downloaded and loaded 328 Kaggle datasets (27,265 files, about 300 million rows).'),
    ('2026-11-24', 'Linked 514,000 betting odds rows to their games (nothing calculated from them yet).'),
    ('2026-11-24', 'Moved draft, awards, coaches, rosters, lineups, inactive lists and referees into the main tables.'),
    ('2026-11-24', 'Added transactions (33,309), salaries (18,771), player tracking (139,098 rows), all 4,149 ABA games and dated roster stints.'),
    ('2026-11-24', 'Matched 537 more players across sources using Jev judgments; 609 players got new permanent ids.'),
    ('2026-11-24', 'Deleted the 35 old database copies and the Kaggle downloads after your approval (270 GB freed).'),
    ('2026-11-24', 'Published the code to GitHub (database stays on this Mac) and started the daily and nightly jobs.'),
    ('2026-11-25', 'Filled in quarter scores for 1,220 old games (156 set aside because they did not add up to the final).'),
    ('2026-11-25', 'Loaded box scores for all 4,149 ABA games at once from a Kaggle copy instead of waiting 7 hours for downloads; 8,288 of 8,298 team totals match the official final.'),
]


def now() -> dt.datetime:
    return dt.datetime.now(ET)


def launchd(label: str) -> dict:
    out = subprocess.run(['launchctl', 'list'], capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) == 3 and parts[2] == label:
            return {'running': parts[0] != '-', 'last_exit': parts[1]}
    return {'running': False, 'last_exit': None, 'missing': True}


def db_numbers(state: dict) -> dict:
    """Small read-only queries; if a job holds the database, reuse the last numbers."""
    import time
    import duckdb
    c = None
    for _ in range(6):  # a job may hold the file for a moment; try for ~30 s
        try:
            c = duckdb.connect(str(DATA / 'nba-warehouse.duckdb'), read_only=True)
            break
        except Exception:
            time.sleep(5)
    if c is None:
        return state.get('db', {}) | {'stale': True}
    q = lambda sql: c.execute(sql).fetchall()
    out: dict = {'stale': False}
    try:
        out['coverage'] = q("""SELECT domain, status, sum(games) FROM audit.domain_coverage GROUP BY ALL""")
        out['periods_present'] = q('SELECT count(DISTINCT game_id) FROM core.fact_game_period_score')[0][0]
        out['games'] = q('SELECT count(*) FROM core.dim_game')[0][0]
        out['attempts'] = q("SELECT outcome, count(*) FROM audit.gap_attempt GROUP BY 1")
        out['coverage_as_of'] = q("SELECT max(attempted_at)::VARCHAR FROM audit.gap_attempt")[0][0]
        out['filled_since'] = q('''SELECT count(*) FROM audit.period_receipt_bref_chase
            WHERE game_id IN (SELECT game_id FROM audit.game_coverage WHERE domain = 'period_scores' AND status <> 'present')''')[0][0]
        out['refresh'] = [(str(r[0]), str(r[1]), r[2]) for r in q("SELECT ran_at::VARCHAR, cutoff, outcome FROM audit.refresh_log ORDER BY ran_at DESC LIMIT 5")]
        out['scope_cutoff'] = str(q('SELECT cutoff FROM audit."season_2027__scope"')[0][0])
        out['current_finals'] = q("SELECT count(*) FROM core.fact_game_result r JOIN core.dim_game g USING (game_id) WHERE g.season_year = '2026-27'")[0][0]
        has_aba_box = q("SELECT count(*) FROM duckdb_tables() WHERE schema_name='core' AND table_name='aba_player_game_stats'")[0][0]
        out['aba_box_games'] = q('SELECT count(DISTINCT game_id) FROM core.aba_player_game_stats')[0][0] if has_aba_box else 0
        out['aba_games'] = q('SELECT count(*) FROM core.aba_game')[0][0]
    except Exception as e:
        out['error'] = str(e)[:200]
    finally:
        c.close()
    return out


def chase_log() -> dict:
    p = DATA / 'crawl-logs' / 'chase.log'
    lines = p.read_text(errors='replace').splitlines() if p.exists() else []
    starts = [i for i, l in enumerate(lines) if l.endswith('period gaps queued')]
    if not starts:
        return {'queued': None, 'done': 0, 'admitted': 0, 'recent': [], 'problem': None}
    run = lines[starts[-1]:]
    queued = int(run[0].split()[0])
    events = [l for l in run if re.match(r'^\d+ \d{10} ', l)]
    problem = next((l for l in reversed(run) if 'BLOCKED' in l or l.startswith('Traceback') or 'Error' in l), None)
    aba_line = next((l for l in reversed(lines) if l.endswith('ABA box scores')), None)
    return {'queued': queued, 'done': len(events), 'admitted': sum(' admitted ' in l for l in events),
            'no_data': sum(' no_data ' in l or ' not_found ' in l for l in events),
            'mismatch': sum(' mismatch ' in l for l in events), 'recent': events[-6:], 'problem': problem,
            'aba_total_logged': int(aba_line.split()[0]) if aba_line else None}


def aba_pages() -> tuple[int, int]:
    """ABA player game-log pages cached, out of the player-season count the crawler logged."""
    have = sum(1 for f in (CACHE / 'players').glob('*/*/gamelog/*') if not f.suffix or f.suffix == '.404')
    total = 1259
    for name in ('aba-gamelogs.log', 'chase.log'):
        p = DATA / 'crawl-logs' / name
        if p.exists():
            m = re.findall(r'^(\d+) ABA player game logs$', p.read_text(errors='replace'), re.M)
            if m:
                total = int(m[-1])
    return min(have, total), total


def gamelog_running() -> bool:
    return subprocess.run(['pgrep', '-f', 'crawl_bref.py aba_gamelogs'], capture_output=True).returncode == 0


def pace(history: list, key: str) -> float | None:
    """Items per hour over the last ~30 minutes of samples (short, so a change of pace shows quickly)."""
    pts = [(h['t'], h[key]) for h in history if h.get(key) is not None]
    if len(pts) < 2:
        return None
    t1, v1 = pts[-1]
    older = [p for p in pts if t1 - p[0] <= 1800]
    t0, v0 = older[0]
    if t1 - t0 < 600 or v1 <= v0:
        return None
    return (v1 - v0) / ((t1 - t0) / 3600)


def fmt_duration(hours: float) -> str:
    if hours < 1 / 60:
        return 'under a minute'
    if hours < 1:
        return f'about {round(hours * 60)} minutes'
    if hours < 36:
        h = round(hours * 2) / 2
        return 'about 1 hour' if h == 1 else f'about {h:g} hours'
    return f'about {round(hours / 24, 1):g} days'


def next_run(hours: list[int]) -> dt.datetime:
    t = now()
    for d in range(2):
        for h in hours:
            cand = (t + dt.timedelta(days=d)).replace(hour=h, minute=0, second=0, microsecond=0)
            if cand > t:
                return cand
    return t


def when(t: dt.datetime) -> str:
    day = 'today' if t.date() == now().date() else 'tomorrow' if t.date() == now().date() + dt.timedelta(days=1) else t.strftime('%a %b %-d')
    return f"{day} at {t.strftime('%-I:%M %p').lower()}"


def latest_refresh_report() -> tuple[str, str] | None:
    files = sorted((DATA / 'refresh-reports').glob('*-cutoff-*.txt'))
    if not files:
        return None
    f = files[-1]
    first = f.read_text(errors='replace').splitlines()[0] if f.stat().st_size else ''
    stamp = dt.datetime.strptime(f.name[:15], '%Y-%m-%dT%H%M').replace(tzinfo=ET)
    return when(stamp) if stamp.date() >= now().date() - dt.timedelta(days=1) else stamp.strftime('%b %-d, %-I:%M %p'), first


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    state_file = OUT / 'history.json'
    state = json.loads(state_file.read_text()) if state_file.exists() else {'samples': []}
    db = db_numbers(state)
    if not db.get('stale'):
        state['db'] = db
    chase = chase_log()
    aba_have, aba_total = aba_pages()
    chase_job, refresh_job = launchd('com.nba-warehouse.chase-gaps'), launchd('com.nba-warehouse.daily-refresh')
    t = now().timestamp()
    state['samples'] = [s for s in state['samples'] if t - s['t'] < 86400] + [
        {'t': t, 'period_done': chase['done'], 'aba_pages': aba_have}]
    state_file.write_text(json.dumps(state))

    tasks = []
    # 1. quarter scores
    remaining = (chase['queued'] or 0) - chase['done']
    rate = pace(state['samples'], 'period_done') or 3600 / SECONDS_PER_PAGE
    period_running = chase_job['running'] and remaining > 0
    tasks.append({
        'name': 'Filling in missing quarter-by-quarter scores',
        'plain': 'About 1,650 games, mostly from the 1940s and 1950s, have a final score but no quarter scores. '
                 'The job looks each one up on Basketball-Reference, one page every 3 seconds, the fastest the site allows. '
                 'A quarter-score line is only kept if it adds up exactly to the official final.',
        'status': 'problem' if chase['problem'] and not chase_job['running'] else 'running' if period_running else 'done' if remaining <= 0 and chase['queued'] is not None else 'waiting',
        'done': chase['done'], 'total': chase['queued'] or 0, 'unit': 'games checked',
        'detail': 'Nothing left to look up in this run; the earlier run filled in 1,220 games.' if chase['queued'] == 0 else f"{chase['admitted']:,} filled in · {chase['no_data']:,} had no quarter scores on the page · {chase['mismatch']:,} didn't add up and were set aside",
        'eta': fmt_duration(remaining / rate) if remaining > 0 else None,
        'next': 'Then it starts collecting ABA box scores.' if remaining > 0 else 'Finished for now; the nightly run at 1 am picks up anything new.',
        'recent': [plain_event(l) for l in chase['recent']],
    })
    # 2. ABA box pages
    aba_left = aba_total - aba_have
    aba_rate = pace(state['samples'], 'aba_pages') or 3600 / SECONDS_PER_PAGE
    aba_wait_hours = (remaining / rate if remaining > 0 else 0)
    tasks.append({
        'name': 'Filling in full detail for ABA box scores (1967–1976)',
        'plain': 'Every ABA game already has its box score, loaded from a Kaggle copy. That copy is missing some numbers '
                 '(assists, rebounds, fouls, shot attempts in many rows). This step downloads each player\'s game log for each '
                 'ABA season from Basketball-Reference (about 1,260 pages instead of 4,149 box score pages), one every 3 seconds, '
                 'the fastest the site allows.',
        'status': 'done' if aba_left == 0 and aba_total else 'running' if (chase_job['running'] or gamelog_running()) else 'waiting',
        'done': aba_have, 'total': aba_total, 'unit': 'pages downloaded',
        'detail': f'{aba_left:,} still to download',
        'eta': fmt_duration(aba_left / aba_rate) if aba_left else None,
        'next': 'Then the downloaded pages replace the Kaggle rows for those games.',
        'recent': [],
    })
    # NBA.com downloads for remaining modern gaps
    for kind, folder, label, plain in (
        ('lineups', 'boxscoretraditionalv3', 'Downloading missing starting lineups from NBA.com',
         'About 3,950 games (mostly 2000–01, 2021–22 and preseason) had no starting lineup. NBA.com has them in its box scores; '
         'this downloads one game per second and loads the starters.'),
        ('pbp', 'playbyplayv3', 'Downloading missing play-by-play from NBA.com',
         'About 975 games (mostly preseason) had no play-by-play or shot chart. NBA.com has some of them; '
         'games it returns empty for are recorded as one failed source.')):
        logf = DATA / 'crawl-logs' / f'nba-gaps-{kind}.log'
        if not logf.exists():
            continue
        m = re.findall(r'^(\d+) \w+ games to fetch$', logf.read_text(errors='replace'), re.M)
        total = int(m[0]) if m else 0
        done = sum(1 for _ in (EVIDENCE / 'nba-gaps' / folder).glob('*.json'))
        run = subprocess.run(['pgrep', '-f', f'fetch_nba_gaps.py {kind}'], capture_output=True).returncode == 0
        key = f'nba_{kind}'
        state['samples'][-1][key] = done
        r = pace(state['samples'], key) or 3000
        left = max(total - done, 0) if run else 0
        tasks.append({'name': label, 'plain': plain,
                      'status': 'running' if run else 'done' if done else 'waiting',
                      'done': min(done, total) if total else done, 'total': total, 'unit': 'games downloaded',
                      'detail': f'{left:,} still to download' if run else 'Download finished; loaded into the database.',
                      'eta': fmt_duration(left / r) if left else None,
                      'next': 'Then the results are loaded and the coverage report is recalculated.', 'recent': []})
    # 3. load ABA boxes
    loaded = db.get('aba_box_games', 0) or 0
    tasks.append({
        'name': 'Loading and checking the ABA box scores',
        'plain': 'Reads the downloaded box scores, matches every player to the right person, and checks that the '
                 'team totals equal the official final score before anything goes into the main tables.',
        'status': 'done' if loaded and aba_left == 0 else 'running' if not loaded else 'done',
        'done': loaded, 'total': db.get('aba_games') or aba_total, 'unit': 'games loaded',
        'detail': 'All games are in, with team totals checked against the official final score. '
                  'It reloads automatically (about 30 seconds) once the full-detail pages finish downloading.',
        'eta': None,
        'next': 'The coverage report below is recalculated each time it reloads.',
        'recent': [],
    })
    # 4. daily update
    rep = latest_refresh_report()
    nxt = next_run([6, 9, 12])
    last_ok = (rep[1] if rep else '')
    tasks.append({
        'name': 'Daily update of the 2026–27 season',
        'plain': 'Every morning the database adds the previous day\'s games: scores, quarter scores, box scores and play-by-play. '
                 'It works on a copy and only swaps it in if every check passes, so a bad day can\'t damage the data. '
                 'It also saves that day\'s referee assignments.',
        'status': 'problem' if last_ok.startswith('FAILED') else 'running' if refresh_job['running'] else 'scheduled',
        'done': db.get('current_finals') or 0, 'total': None, 'unit': 'games this season so far',
        'detail': (f"Up to date through {pretty_date(db.get('scope_cutoff'))}. Last run {rep[0]}: {plain_refresh(last_ok)}." if rep else 'Not run yet.'),
        'eta': None,
        'next': f'Next run {when(nxt)} (retries at 9 am and noon if the data isn\'t published yet).',
        'recent': [],
    })

    # what's still missing overall
    cov = {}
    for domain, status, n in db.get('coverage', []):
        cov.setdefault(domain, {})[status] = n
    names = {'final_score': 'Final scores', 'period_scores': 'Quarter scores', 'team_box': 'Team box scores',
             'player_box': 'Player box scores', 'play_by_play': 'Play-by-play', 'shot_chart': 'Shot charts',
             'starting_lineup': 'Starting lineups', 'officials': 'Referees'}
    coverage_rows = []
    for k, label in names.items():
        d = cov.get(k, {})
        total = sum(d.values()) or 1
        coverage_rows.append((label, d.get('present', 0), d.get('partial', 0), d.get('missing_recoverable', 0),
                              d.get('disputed', 0), d.get('source_unavailable', 0), d.get('not_tracked', 0) + d.get('not_applicable', 0), total))

    happened = list(MILESTONES)
    if db.get('refresh'):
        seen = set()
        for ran, cutoff, outcome in reversed(db['refresh'][:5]):
            text = f"Daily update {'added games through' if outcome == 'committed' else outcome + ' for'} {pretty_date(cutoff)}."
            if text not in seen:
                seen.add(text)
                happened.append((ran[:10], text))
    if chase['admitted']:
        happened.append((now().strftime('%Y-%m-%d'), f"Quarter-score job has filled in {chase['admitted']:,} old games so far."))

    (OUT / 'index.html').write_text(render(tasks, coverage_rows, happened, db, chase, chase_job), 'utf-8')


def pretty_date(s: str | None) -> str:
    try:
        d = dt.date.fromisoformat(str(s)[:10])
        return d.strftime('%B %-d, %Y')
    except Exception:
        return str(s)


def plain_refresh(first_line: str) -> str:
    if first_line.startswith('committed'):
        return 'all checks passed'
    if first_line.startswith('not ready'):
        return "the day's data wasn't published yet, will retry"
    if first_line.startswith('already current'):
        return 'already up to date'
    if first_line.startswith('FAILED'):
        return 'a check failed, so nothing was changed (see data/refresh-reports)'
    return first_line or 'no details'


def plain_event(line: str) -> str:
    parts = line.split()
    gid, outcome = parts[1], parts[2]
    season = 1900 + int(gid[3:5]) if int(gid[3:5]) > 40 else 2000 + int(gid[3:5])
    what = {'admitted': 'quarter scores filled in', 'no_data': 'page had no quarter scores',
            'not_found': 'no page found', 'mismatch': "numbers didn't add up, set aside"}.get(outcome, outcome)
    return f'Game {gid} ({season}–{str(season + 1)[2:]} season): {what}'


STATUS = {'running': ('Running now', 'run'), 'waiting': ('Waiting its turn', 'wait'), 'done': ('Done', 'done'),
          'scheduled': ('Scheduled', 'sched'), 'problem': ('Needs attention', 'bad')}


def render(tasks, coverage_rows, happened, db, chase, chase_job) -> str:
    e = html.escape
    stamp = now()
    running = [t for t in tasks if t['status'] == 'running']
    open_eta = [t['eta'] for t in tasks[:3] if t['eta']]
    headline = (f"{running[0]['name']}." if running else 'Nothing is running right now.')
    remaining_line = (f"All background work should be finished in {open_eta[-1]}." if open_eta else 'All one-off background work is finished; only the daily and nightly jobs remain.')
    cards = []
    for t in tasks:
        label, cls = STATUS[t['status']]
        pct = (100 * t['done'] / t['total']) if t['total'] else None
        bar = (f'<div class="bar" role="progressbar" aria-valuenow="{pct:.0f}" aria-valuemin="0" aria-valuemax="100"><span style="width:{pct:.1f}%"></span></div>'
               f'<p class="count"><b>{t["done"]:,}</b> of {t["total"]:,} {e(t["unit"])} <span class="pct">{pct:.0f}%</span></p>') if pct is not None else \
              f'<p class="count"><b>{t["done"]:,}</b> {e(t["unit"])}</p>'
        recent = ''.join(f'<li>{e(r)}</li>' for r in t['recent'])
        cards.append(f'''<article class="task {cls}">
  <header><h3>{e(t["name"])}</h3><span class="pill {cls}">{label}</span></header>
  <p class="plain">{e(t["plain"])}</p>
  {bar}
  <p class="detail">{e(t["detail"])}</p>
  <dl>{f'<div><dt>Time left</dt><dd>{e(t["eta"])}</dd></div>' if t["eta"] else ''}<div><dt>Next</dt><dd>{e(t["next"])}</dd></div></dl>
  {f'<details><summary>Latest activity</summary><ul class="recent">{recent}</ul></details>' if recent else ''}
</article>''')
    rows = []
    for label, present, partial, missing, disputed, unavailable, not_tracked, total in coverage_rows:
        segs = [('present', present), ('partial', partial), ('missing', missing), ('disputed', disputed), ('gone', unavailable), ('na', not_tracked)]
        bar = ''.join(f'<span class="seg {c}" style="flex:{n}" title="{n:,}"></span>' for c, n in segs if n)
        rows.append(f'<tr><th scope="row">{e(label)}</th><td><div class="stack">{bar}</div></td>'
                    f'<td class="num">{present:,}</td><td class="num">{missing + partial:,}</td><td class="num">{disputed:,}</td>'
                    f'<td class="num">{unavailable:,}</td><td class="num">{not_tracked:,}</td></tr>')
    caption_extra = (f"; since the last recalculation {db['filled_since']:,} more games got their quarter scores" if db.get('filled_since') else '')
    hist = ''.join(f'<li><time>{e(pretty_date(d))}</time><span>{e(txt)}</span></li>' for d, txt in reversed(happened))
    stale = '<p class="note">The database was busy when this page was made, so the numbers are from the previous update.</p>' if db.get('stale') else ''
    problem = f'<p class="alert">Problem reported by the quarter-score job: {e(chase["problem"])}</p>' if chase['problem'] and not chase_job['running'] else ''
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Warehouse Progress</title>
<meta name="claude:viewport-fit-sides" content="cover">
<meta http-equiv="refresh" content="120">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Source+Sans+3:wght@400;600&family=JetBrains+Mono:wght@500&display=swap">
<style>
/* Layout: a scoreboard header with the one-line answer, then task cards, the gaps table, and the history. */
:root {{
  --bg: #f3f5f7; --surface: #ffffff; --ink: #18212b; --muted: #5b6875; --line: #d9dfe5;
  --court: #c2531f; --run: #1f6fc2; --done: #2f8a4c; --wait: #8a94a0; --bad: #b3261e; --sched: #6d4bb3;
  --seg-missing: #e2a33b; --seg-disputed: #c2531f; --seg-gone: #8a94a0; --seg-na: #d9dfe5;
  --display: 'Barlow Condensed', 'Arial Narrow', sans-serif; --body: 'Source Sans 3', system-ui, sans-serif;
  --mono: 'JetBrains Mono', ui-monospace, monospace;
}}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{
  --bg: #11161c; --surface: #19212a; --ink: #e8edf2; --muted: #9aa6b2; --line: #2b3641;
  --court: #e07a45; --run: #5aa2ec; --done: #5cbf7a; --wait: #7c8895; --bad: #ef6b62; --sched: #a38be0;
  --seg-missing: #d9a14a; --seg-disputed: #e07a45; --seg-gone: #6c7884; --seg-na: #2b3641; color-scheme: dark; }} }}
:root[data-theme="dark"] {{
  --bg: #11161c; --surface: #19212a; --ink: #e8edf2; --muted: #9aa6b2; --line: #2b3641;
  --court: #e07a45; --run: #5aa2ec; --done: #5cbf7a; --wait: #7c8895; --bad: #ef6b62; --sched: #a38be0;
  --seg-missing: #d9a14a; --seg-disputed: #e07a45; --seg-gone: #6c7884; --seg-na: #2b3641; color-scheme: dark; }}
* {{ box-sizing: border-box }}
body {{ background: var(--bg); color: var(--ink); font: 16px/1.5 var(--body); margin: 0;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); padding-block: 24px 48px }}
main {{ max-width: 980px; margin: 0 auto; display: grid; gap: 32px }}
h1, h2, h3 {{ font-family: var(--display); letter-spacing: .01em; text-wrap: balance; margin: 0 }}
.board {{ border-top: 6px solid var(--court); background: var(--surface); padding: 20px 22px; display: grid; gap: 6px; border-radius: 4px }}
.board .eyebrow {{ font-family: var(--mono); font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .08em }}
.board h1 {{ font-size: clamp(28px, 5vw, 40px); line-height: 1.05 }}
.board p {{ margin: 0; color: var(--muted); max-width: 65ch }}
.board .big {{ color: var(--ink); font-size: 18px }}
h2 {{ font-size: 26px; margin-bottom: 12px }}
.tasks {{ display: grid; gap: 14px }}
.task {{ background: var(--surface); border: 1px solid var(--line); border-radius: 6px; padding: 18px 20px; display: grid; gap: 10px; min-width: 0 }}
.task header {{ display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap }}
.task h3 {{ font-size: 22px }}
.plain {{ margin: 0; color: var(--muted); max-width: 70ch }}
.pill {{ font: 600 12px var(--mono); text-transform: uppercase; letter-spacing: .06em; padding: 4px 10px; border-radius: 999px; border: 1.5px solid currentColor }}
.pill.run {{ color: var(--run) }} .pill.done {{ color: var(--done) }} .pill.wait {{ color: var(--wait) }} .pill.bad {{ color: var(--bad) }} .pill.sched {{ color: var(--sched) }}
.task.run {{ border-left: 4px solid var(--run) }} .task.bad {{ border-left: 4px solid var(--bad) }}
.bar {{ height: 10px; background: var(--line); border-radius: 999px; overflow: hidden }}
.bar span {{ display: block; height: 100%; background: var(--run) }}
.task.done .bar span {{ background: var(--done) }} .task.wait .bar span {{ background: var(--wait) }}
.count {{ margin: 0; font-variant-numeric: tabular-nums }}
.count b {{ font-family: var(--display); font-size: 24px }}
.pct {{ font-family: var(--mono); color: var(--muted); margin-left: 6px }}
.detail {{ margin: 0 }}
dl {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px 20px; margin: 0 }}
dt {{ font: 600 12px var(--mono); text-transform: uppercase; letter-spacing: .06em; color: var(--muted) }}
dd {{ margin: 2px 0 0 }}
details summary {{ cursor: pointer; color: var(--run); font-weight: 600 }}
details summary:focus-visible {{ outline: 2px solid var(--run); outline-offset: 2px }}
.recent {{ margin: 8px 0 0; padding-left: 18px; font-size: 14px; color: var(--muted) }}
.tablewrap {{ overflow-x: auto; background: var(--surface); border: 1px solid var(--line); border-radius: 6px }}
table {{ border-collapse: collapse; width: 100%; min-width: 640px; font-variant-numeric: tabular-nums }}
th, td {{ padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: middle }}
thead th {{ font: 600 12px var(--mono); text-transform: uppercase; letter-spacing: .05em; color: var(--muted) }}
td.num {{ text-align: right; font-family: var(--mono); font-size: 14px }}
.stack {{ display: flex; height: 12px; border-radius: 3px; overflow: hidden; min-width: 160px }}
.seg.present {{ background: var(--done) }} .seg.partial, .seg.missing {{ background: var(--seg-missing) }}
.seg.disputed {{ background: var(--seg-disputed) }} .seg.gone {{ background: var(--seg-gone) }} .seg.na {{ background: var(--seg-na) }}
.caption {{ margin: -4px 0 12px; color: var(--muted); max-width: 70ch }}
.legend {{ display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 14px; color: var(--muted); margin: 10px 0 0 }}
.legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; vertical-align: -1px }}
.history {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 10px }}
.history li {{ display: grid; grid-template-columns: 9.5em 1fr; gap: 12px }}
.history time {{ font: 500 13px var(--mono); color: var(--muted); padding-top: 2px }}
.note, .alert {{ margin: 0; padding: 10px 14px; border-radius: 6px; background: var(--surface); border: 1px solid var(--line) }}
.alert {{ border-color: var(--bad); color: var(--bad) }}
footer {{ color: var(--muted); font-size: 14px }}
@media (max-width: 560px) {{ .history li {{ grid-template-columns: 1fr; gap: 2px }} }}
@media (prefers-reduced-motion: no-preference) {{ .task.run .bar span {{ transition: width .6s ease }} }}
</style></head><body>
<main>
  <section class="board">
    <span class="eyebrow">NBA warehouse · updated {stamp.strftime('%a %b %-d, %-I:%M %p').replace('AM', 'am').replace('PM', 'pm')} Eastern</span>
    <h1>{e(headline)}</h1>
    <p class="big">{e(remaining_line)}</p>
    <p>This page updates itself every 5 minutes. Times are estimates from the pace measured so far.</p>
  </section>
  {problem}{stale}
  <section><h2>Background jobs</h2><div class="tasks">{''.join(cards)}</div></section>
  <section>
    <h2>What's still missing in the database</h2>
    <p class="caption">One row per kind of data, counted in games. This table is recalculated each time the nightly job finishes{caption_extra}.</p>
    <div class="tablewrap"><table>
      <thead><tr><th>Kind of data</th><th>Share of all games</th><th>Have it</th><th>Still to find</th><th>Sources disagree</th><th>Gave up</th><th>Never recorded</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table></div>
    <p class="legend"><span><i style="background:var(--done)"></i>Have it</span><span><i style="background:var(--seg-missing)"></i>Still to find</span>
      <span><i style="background:var(--seg-disputed)"></i>Sources disagree</span><span><i style="background:var(--seg-gone)"></i>Gave up after two sources</span>
      <span><i style="background:var(--seg-na)"></i>Never recorded (e.g. play-by-play before 1996)</span></p>
  </section>
  <section><h2>What already happened</h2><ul class="history">{hist}</ul></section>
  <footer>Generated by warehouse/status_page.py from the job logs and the database. Problems also go to data/refresh-reports and data/crawl-logs.</footer>
</main></body></html>
'''


if __name__ == '__main__':
    build()
