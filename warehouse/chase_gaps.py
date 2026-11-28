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

from bref_fetch import BlockedError, NotFound, cache_path, fetch
from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from fetch_bref_periods import Fixture, parse_box
from observability import event, operation, stage

SOURCE = 'basketball_reference_box'


def periods(limit: int | None, warehouse: Path) -> None:
    with warehouse_lock(), connect(warehouse) as c:
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
    print(len(queue), 'period gaps queued', flush=True)
    event('period_queue', 'selected', queued=len(queue), count=len(queue[:limit] if limit else queue))
    for n, fixture in enumerate(queue[:limit] if limit else queue):
        gid, day, hid, aid, hs, away_score, ha, aa = fixture
        with stage('period_attempt', dependency='basketball_reference', game_id=gid) as step:
            outcome, note, admit = 'not_found', None, None
            if not ha or not aa:
                outcome, note = 'no_data', 'no Basketball-Reference abbreviation for a side'
            else:
                path = f'/boxscores/{day:%Y%m%d}0{ha}.html'
                try:
                    page = fetch(path)
                except NotFound:
                    page = None
                except BlockedError as error:
                    step.outcome = 'blocked'
                    step.result.update(reason='provider_block', http_status=error.status)
                    raise
                if page is not None:
                    f = Fixture(gid, day, hid, aid, hs, away_score, path, ha, aa)
                    result = parse_box(f, page.encode('utf-8'))
                    note = result['status']
                    if note == 'matched_fixture_exact_printed_periods':
                        digest = hashlib.sha256(page.encode()).hexdigest()
                        admit = (path, digest, result['home_periods'], result['away_periods'])
                        outcome = 'admitted'
                    elif note == 'line_score_unavailable':
                        outcome = 'no_data'
                    else:
                        outcome = 'mismatch'
            with warehouse_lock(), connect(warehouse) as c, warehouse_transaction(c):
                existing = c.execute("""SELECT EXISTS (SELECT 1 FROM core.fact_game_period_score WHERE game_id=?)
                    OR EXISTS (SELECT 1 FROM audit.gap_attempt WHERE game_id=? AND domain='period_scores' AND source=?)""",
                    [gid, gid, SOURCE]).fetchone()[0]
                if existing:
                    step.outcome = 'skipped'
                    step.result['reason'] = 'resolved_after_selection'
                    continue
                current_fixture = c.execute("""SELECT g.game_date, g.home_team_id, g.away_team_id, r.home_score, r.away_score
                    FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
                    WHERE g.game_id=? AND g.result_status='final'""", [gid]).fetchone()
                if current_fixture != (day, hid, aid, hs, away_score):
                    step.outcome = 'skipped'
                    step.result['reason'] = 'fixture_changed_after_selection'
                    continue
                if admit:
                    path, digest, hp, ap = admit
                    c.executemany('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)',
                                  [(gid, team, i, pts) for team, vector in ((hid, hp), (aid, ap))
                                   for i, pts in enumerate(vector, 1)])
                    c.execute('INSERT INTO audit.period_receipt_bref_chase VALUES (?,?,?,?,?,now())',
                              [gid, 'https://www.basketball-reference.com' + path, digest, hp, ap])
                c.execute('INSERT INTO audit.gap_attempt VALUES (?,?,?,now(),?,?)',
                          ['period_scores', gid, SOURCE, outcome, note])
            step.outcome = outcome
            if admit:
                step.result['sha256'] = digest
            if note is not None:
                step.result['decision'] = note
            print(n, gid, outcome, note, flush=True)


def replay_cached_periods(c) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS audit.period_receipt_bref_chase (game_id VARCHAR PRIMARY KEY, source_url VARCHAR,
        sha256 VARCHAR, home_periods INTEGER[], away_periods INTEGER[], admitted_at TIMESTAMPTZ)''')
    queue = c.execute('''SELECT g.game_id,g.game_date,g.home_team_id,g.away_team_id,r.home_score,r.away_score,
        (SELECT min(source_key) FROM core.team_source_id t WHERE t.team_id=g.home_team_id
          AND t.source_system='basketball_reference' AND t.league <> 'ABA' AND t.season_end_year=g.season_start+1),
        (SELECT min(source_key) FROM core.team_source_id t WHERE t.team_id=g.away_team_id
          AND t.source_system='basketball_reference' AND t.league <> 'ABA' AND t.season_end_year=g.season_start+1)
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
        WHERE g.result_status='final' AND g.game_id NOT IN (SELECT game_id FROM core.fact_game_period_score)
          AND g.game_id IN (SELECT game_id FROM audit.game_coverage WHERE domain='period_scores' AND status <> 'present')''').fetchall()
    for gid, day, home, away, hs, aws, hc, ac in queue:
        if not hc or not ac:
            continue
        relative = f'/boxscores/{day:%Y%m%d}0{hc}.html'
        path = cache_path(relative)
        if not path.is_file():
            continue
        original = path.read_bytes()
        try:
            result = parse_box(Fixture(gid, day, home, away, hs, aws, relative, hc, ac), original)
        except ValueError as exc:
            print(f'Cached line score held for {gid}: {exc}', flush=True)
            continue
        if result['status'] != 'matched_fixture_exact_printed_periods':
            continue
        url, digest = 'https://www.basketball-reference.com' + relative, hashlib.sha256(original).hexdigest()
        hp, ap = result['home_periods'], result['away_periods']
        with warehouse_transaction(c):
            c.executemany('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)',
                          [(gid, team, i, points) for team, line in ((home, hp), (away, ap))
                           for i, points in enumerate(line, 1)])
            if len(hp) > 4:
                c.execute('UPDATE core.fact_game_result SET is_overtime=true WHERE game_id=? AND is_overtime IS NULL', [gid])
            c.execute('INSERT INTO audit.period_receipt_bref_chase VALUES (?,?,?,?,?,now()) ON CONFLICT DO NOTHING',
                      [gid, url, digest, hp, ap])
            c.execute("INSERT INTO audit.gap_attempt VALUES ('period_scores',?,?,now(),'admitted',?)", [gid, SOURCE, url])
        print(f'Published cached Basketball-Reference periods for {gid}', flush=True)


def finalize(warehouse: Path) -> None:
    """After a chasing pass: load newly cached box pages, promote ABA boxes, refresh the coverage ledger."""
    import coverage
    import load_bref
    import promote_new_sources as pns
    def load(function, *args):
        with stage(f'{function.__module__}.{function.__name__}'):
            return function(*args)

    with warehouse_lock(), connect(warehouse) as c:
        with stage('bref_load'):
            load(load_bref.boxscores, c)
            load(load_bref.gamelogs, c)
            load(load_bref.officials, c)
            for part in ('identities', 'aba_boxes', 'views'):
                load(getattr(pns, part), c)
        with stage('nba_gap_load'):
            import load_nba_gaps, promote_domains as pd
            load(load_nba_gaps.starters, c)
            load(load_nba_gaps.llimllib_starters, c)
            with warehouse_transaction(c):
                load(load_nba_gaps.pbp, c)
                load(load_nba_gaps.boxes, c)
            load(load_nba_gaps.repair_foreign_names, c)
            load(load_nba_gaps.officials, c)
            load(load_nba_gaps.periods, c)
            load(load_nba_gaps.team_from_players, c)
        with stage('historical_sources'):
            import load_wayback_nba  # archived NBA.com pages (ranked above ESPN)
            if (load_wayback_nba.CACHE / 'pages').exists():
                load(load_wayback_nba.load, c, load(load_wayback_nba.build_index, c))
            from fetch_newspaper_periods import publish_verified_boundaries
            load(publish_verified_boundaries, c)
            import load_cska_box
            load_cska_box.create_starter_table(c)
            if load_cska_box.CACHE.is_file():
                load(load_cska_box.load, c)
            from load_wayback_manual_box import load as load_manual_box
            load(load_manual_box, c)
            from load_preseason_press import load as load_press
            load(load_press, c)
            from load_nbastats_history import publish_own_basket
            load(publish_own_basket, c)
            from reconcile_historical_games import load as reconcile_historical
            load(reconcile_historical, c)
            load(replay_cached_periods, c)
        with stage('source_promotions'):
            from load_usatoday_preseason import load as load_usatoday
            load(load_usatoday, c)
            load(load_nba_gaps.pbp_starters, c)
            # promotion reads these even before the first archive load
            c.execute('CREATE TABLE IF NOT EXISTS raw.wayback_game_official (game_id VARCHAR, official_name VARCHAR, official_order INTEGER, jersey_num VARCHAR, official_id BIGINT)')
            c.execute('CREATE TABLE IF NOT EXISTS raw.wayback_starter (game_id VARCHAR, team_id BIGINT, person_id BIGINT)')
            import load_espn  # ESPN public API (owner-approved 2026-11-25), lowest rank, fills only games nothing else covers
            if (load_espn.CACHE / 'matches.json').exists():
                load(load_espn.load, c, json.loads((load_espn.CACHE / 'matches.json').read_text()))
            from load_nba_roster_releases import load as load_roster_releases
            load(load_roster_releases, c)
            from load_nba_schedule_releases import load as load_schedule_releases
            load(load_schedule_releases, c)
            from load_nba_scorer_crews import load as load_scorer_crews
            load(load_scorer_crews, c)
            from load_verified_lineups import load as load_verified_lineups
            load(load_verified_lineups, c)
        with stage('officiating_lineup_promotion'):
            for d in pd.DOMAINS:
                if d.name in ('fact_starting_lineup', 'dim_official', 'fact_game_official'):
                    c.execute('DELETE FROM audit.domain_disposition WHERE domain = ?', [d.name])
                    c.execute('DELETE FROM audit.domain_held WHERE domain = ?', [d.name])
                    print(d.name, *pd.promote(c, d), sep='\t', flush=True)
        with stage('coverage_rebuild'):
            tracked = ', '.join(f"('{k}', {v})" for k, v in coverage.TRACKED_FROM.items())
            c.execute(coverage.SQL.replace('{tracked}', tracked))
            import label_gaps  # per-game bulk-source checks for the gaps still open, then recount
            load(label_gaps.label, c)
            c.execute(coverage.SQL.replace('{tracked}', tracked))
            c.execute('CHECKPOINT')


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('queue', choices=['periods', 'aba_boxscores', 'finalize', 'all'])
    ap.add_argument('--limit', type=int)
    ap.add_argument('--warehouse', type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    with operation('chase_gaps', a.warehouse, queue=a.queue) as span:
        try:
            if a.queue in ('periods', 'all'):
                with stage('periods'):
                    periods(a.limit, a.warehouse)
            if a.queue in ('aba_boxscores', 'all'):
                from crawl_bref import aba_gamelogs
                with stage('aba_boxscores') as step:
                    try:
                        aba_gamelogs(a.warehouse)
                    except BlockedError as error:
                        step.outcome = 'blocked'
                        step.result.update(reason='provider_block', http_status=error.status)
                        raise
            if a.queue in ('finalize', 'all'):
                with stage('finalize'):
                    finalize(a.warehouse)
        except BlockedError as error:
            span.outcome = 'blocked'
            span.result.update(reason='provider_block', http_status=error.status, exit_code=3)
            print('BLOCKED:', error, file=sys.stderr)
            sys.exit(3)


if __name__ == '__main__':
    main()
