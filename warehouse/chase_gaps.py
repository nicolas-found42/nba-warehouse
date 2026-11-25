"""Background gap chasing (step 6): work the open-gap queue, one polite request at a time.

Queues, in order:
  periods        games with no accepted period scores -> Basketball-Reference box page line score.
                 Admitted only when the page is the same fixture (date, home/away abbreviations), the printed
                 total equals the warehouse final, quarters sum to it and scores are tied before any overtime
                 (fetch_bref_periods.parse_box). Admissions go to core.fact_game_period_score with a receipt in
                 audit.period_receipt_bref_chase; every attempt is logged to audit.gap_attempt.
  aba_boxscores  cache every ABA player game-log page (then load_bref.py + promote_new_sources.py aba_boxes).

Give-up rule: coverage.py marks a gap source_unavailable once two independent sources have failed
(the local snapshot mirror already counts as one for every period gap).

Resumable: games with an attempt from this source are skipped. Stops on any block (HTTP 403/429/challenge).

  uv run --no-project --with duckdb==2.0.0 python warehouse/chase_gaps.py periods [--limit N]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

from bref_fetch import BlockedError, NotFound, fetch
from build_warehouse import DEFAULT_OUT, connect, warehouse_lock
from fetch_bref_periods import Fixture, parse_box

SOURCE = 'basketball_reference_box'


def periods(limit: int | None, warehouse: Path) -> None:
    c = connect(warehouse)
    c.execute('''CREATE TABLE IF NOT EXISTS audit.gap_attempt (domain VARCHAR, game_id VARCHAR, source VARCHAR, attempted_at TIMESTAMPTZ,
        outcome VARCHAR, note VARCHAR)''')
    c.execute('''CREATE TABLE IF NOT EXISTS audit.period_receipt_bref_chase (game_id VARCHAR PRIMARY KEY, source_url VARCHAR, sha256 VARCHAR,
        home_periods INTEGER[], away_periods INTEGER[], admitted_at TIMESTAMPTZ)''')
    queue = c.execute(f'''
        SELECT g.game_id, g.game_date, g.home_team_id, g.away_team_id, r.home_score, r.away_score,
               (SELECT min(source_key) FROM core.team_source_id t WHERE t.team_id = g.home_team_id AND t.league <> 'ABA'
                 AND t.season_end_year = g.season_start + 1) AS h_abbr,
               (SELECT min(source_key) FROM core.team_source_id t WHERE t.team_id = g.away_team_id AND t.league <> 'ABA'
                 AND t.season_end_year = g.season_start + 1) AS a_abbr
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
        WHERE g.result_status = 'final'
          AND game_id NOT IN (SELECT game_id FROM core.fact_game_period_score)
          AND game_id NOT IN (SELECT game_id FROM audit.gap_attempt WHERE domain = 'period_scores' AND source = '{SOURCE}')
        ORDER BY g.game_date''').fetchall()
    c.close()  # never hold the warehouse lock while waiting on the network
    print(len(queue), 'period gaps queued', flush=True)
    for n, (gid, day, hid, aid, hs, as_, ha, aa) in enumerate(queue[:limit] if limit else queue):
        outcome, note, admit = 'not_found', None, None
        if not ha or not aa:
            outcome, note = 'no_data', 'no Basketball-Reference abbreviation for a side'
        else:
            path = f'/boxscores/{day:%Y%m%d}0{ha}.html'
            try:
                html = fetch(path)
            except NotFound:
                html = None
            except BlockedError as e:
                print('BLOCKED:', e, file=sys.stderr)
                sys.exit(3)
            if html is not None:
                f = Fixture(gid, day, hid, aid, hs, as_, path, ha, aa)
                res = parse_box(f, html.encode('utf-8'))
                note = res['status']
                if res['status'] == 'matched_fixture_exact_printed_periods':
                    outcome, admit = 'admitted', (path, hashlib.sha256(html.encode()).hexdigest(), res['home_periods'], res['away_periods'])
                elif res['status'] == 'line_score_unavailable':
                    outcome = 'no_data'
                else:
                    outcome = 'mismatch'
        with warehouse_lock():
            c = connect(warehouse)
            try:
                if admit:
                    path, digest, hp, ap = admit
                    c.execute('BEGIN')
                    for team, pts in ((hid, hp), (aid, ap)):
                        for i, p in enumerate(pts, 1):
                            c.execute('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)', [gid, team, i, p])
                    c.execute('INSERT INTO audit.period_receipt_bref_chase VALUES (?,?,?,?,?,now())',
                              [gid, 'https://www.basketball-reference.com' + path, digest, hp, ap])
                    c.execute('COMMIT')
                c.execute('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)', ['period_scores', gid, SOURCE, outcome, note])
            finally:
                c.close()
        print(n, gid, outcome, note, flush=True)


def finalize(warehouse: Path) -> None:
    """After a chasing pass: load newly cached box pages, promote ABA boxes, refresh the coverage ledger."""
    import duckdb
    import coverage
    import load_bref
    import promote_new_sources as pns
    with warehouse_lock():
        c = connect(warehouse)
        load_bref.boxscores(c)
        load_bref.gamelogs(c)
        load_bref.officials(c)
        for part in ('identities', 'aba_boxes', 'views'):
            getattr(pns, part)(c)
        import load_nba_gaps, promote_domains as pd
        load_nba_gaps.starters(c)
        load_nba_gaps.llimllib_starters(c)
        load_nba_gaps.pbp(c)
        load_nba_gaps.boxes(c)
        load_nba_gaps.officials(c)
        load_nba_gaps.periods(c)
        load_nba_gaps.pbp_starters(c)
        load_nba_gaps.team_from_players(c)
        import load_wayback_nba  # archived NBA.com pages (ranked above ESPN)
        if (load_wayback_nba.CACHE / 'pages').exists():
            load_wayback_nba.load(c, load_wayback_nba.build_index(c))
        # promotion reads these even before the first archive load
        c.execute('CREATE TABLE IF NOT EXISTS raw.wayback_game_official (game_id VARCHAR, official_name VARCHAR, official_order INTEGER, jersey_num VARCHAR, official_id BIGINT)')
        c.execute('CREATE TABLE IF NOT EXISTS raw.wayback_starter (game_id VARCHAR, team_id BIGINT, person_id BIGINT)')
        import load_espn  # ESPN public API (owner-approved 2026-11-25), lowest rank, fills only games nothing else covers
        if (load_espn.CACHE / 'matches.json').exists():
            load_espn.load(c, json.loads((load_espn.CACHE / 'matches.json').read_text()))
        for d in pd.DOMAINS:
            if d.name in ('fact_starting_lineup', 'dim_official', 'fact_game_official'):
                c.execute('DELETE FROM audit.domain_disposition WHERE domain = ?', [d.name])
                c.execute('DELETE FROM audit.domain_held WHERE domain = ?', [d.name])
                print(d.name, *pd.promote(c, d), sep='\t', flush=True)
        tracked = ', '.join(f"('{k}', {v})" for k, v in coverage.TRACKED_FROM.items())
        c.execute(coverage.SQL.replace('{tracked}', tracked))
        import label_gaps  # per-game bulk-source checks for the gaps still open, then recount
        label_gaps.label(c)
        c.execute(coverage.SQL.replace('{tracked}', tracked))
        c.execute('CHECKPOINT')
        c.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('queue', choices=['periods', 'aba_boxscores', 'finalize', 'all'])
    ap.add_argument('--limit', type=int)
    ap.add_argument('--warehouse', type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    if a.queue in ('periods', 'all'):
        periods(a.limit, a.warehouse)
    if a.queue in ('aba_boxscores', 'all'):
        from crawl_bref import aba_gamelogs
        try:
            aba_gamelogs()  # one page per player-season covers every ABA box line (cached pages are not refetched)
        except BlockedError as e:
            print('BLOCKED:', e, file=sys.stderr)
            sys.exit(3)
    if a.queue in ('finalize', 'all'):
        finalize(a.warehouse)


if __name__ == '__main__':
    main()
