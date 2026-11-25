#!/usr/bin/env python3
"""Reconcile every field-goal observation for the held Knicks–Timberwolves stream.

An exact gamebook page/line receipt is audit-only; this never edits a DuckDB.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass
from hashlib import file_digest
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import NamedTuple

import duckdb

from nba_warehouse import ROOT
from recover_local_periods import snapshot
from verify_current_scorer_discrepancy import GAME, LINE, PDF, PINS, SCORE, SOURCE, clock_ms, nba_scoring

BOX = PDF.with_name('20261119_NYKMIN.pdf')
BOX_URL = 'https://statsdmz.nba.com/pdfs/20261119/20261119_NYKMIN.pdf'
INPUT_PINS = (*PINS, ('final_box', '55e85592c46cb96edc0171b1119c086e90128598ca722532d57a2d9c54b6e250'))
SCOPE = 'Thursday, November 19, 2026 Target Center, Minneapolis, MN'
HOME, AWAY = 1610612750, 1610612752
COLUMNS = ('fga', 'fgm', 'fg3a', 'fg3m')
ALIASES = {
    'A.Dosunmu': 'Ayo Dosunmu', 'B.Hyland': 'Bones Hyland', 'J.McDaniels': 'Jaden McDaniels',
    'J.Beringer': 'Joan Beringer', 'J.Kuminga': 'Jonathan Kuminga', 'L.Ball': 'LaMelo Ball',
    'R.Gobert': 'Rudy Gobert', 'T.ShannonJr': 'Terrence Shannon Jr.', 'T.Lyles': 'Trey Lyles',
    'A.Drummond': 'Andre Drummond', 'J.Brunson': 'Jalen Brunson', 'J.Alvarado': 'Jose Alvarado',
    'J.Hart': 'Josh Hart', 'K.Towns': 'Karl-Anthony Towns', 'L.Shamet': 'Landry Shamet',
    'M.Bridges': 'Mikal Bridges', 'M.McBride': 'Miles McBride', 'O.Anunoby': 'OG Anunoby', 'T.Kolek': 'Tyler Kolek',
}
FGA_NAME = re.compile(r'^([A-Z]\.\S+) +(.+)$')


class Counts(NamedTuple):
    fga: int
    fgm: int
    fg3a: int
    fg3m: int


@dataclass(frozen=True)
class Box:
    players: dict[tuple[int, int], Counts]
    teams: dict[int, Counts]
    team_fga: dict[int, int]


class Spot(NamedTuple):
    period: int
    clock_ms: int
    team_id: int


@dataclass(frozen=True)
class Attempt:
    period: int
    clock_ms: int
    team_id: int
    person_id: int | None
    shot_value: int | None
    made: bool
    page: int
    line: int
    printed: str

    def spot(self) -> Spot:
        return Spot(self.period, self.clock_ms, self.team_id)

    def credited_key(self) -> tuple[Spot, int, int, bool]:
        if self.person_id is None or self.shot_value is None:
            raise RuntimeError('Team observation has no credited-player shot key')
        return self.spot(), self.person_id, self.shot_value, self.made


@dataclass(frozen=True)
class ProviderShot:
    action_number: int
    espn_play_id: int
    play_order: int
    period: int
    clock_display_value: str
    team_id: int
    person_id: int
    shot_value: int
    made: bool
    description: str

    def spot(self) -> Spot:
        return Spot(self.period, clock_ms(self.clock_display_value), self.team_id)

    def credited_key(self) -> tuple[Spot, int, int, bool]:
        return self.spot(), self.person_id, self.shot_value, self.made


def layout(path: Path) -> list[str]:
    return subprocess.check_output(['pdftotext', '-layout', str(path), '-'], text=True, encoding='utf-8').rstrip('\f\n').split('\f')


def sum_counts(items: list[Counts]) -> Counts:
    return Counts(*(sum(col) for col in zip(*items, strict=True))) if items else Counts(0, 0, 0, 0)


def box_counts(page: str, title: str, roster: dict[str, tuple[int, int]]) -> Box:
    if SCOPE not in page or not any(line.strip() == title for line in page.splitlines()):
        raise RuntimeError(f'Scorer box fixture or page heading differs: {title}')
    team: int | None = None
    players: dict[tuple[int, int], Counts] = {}
    teams: dict[int, Counts] = {}
    team_fga: dict[int, int] = {}
    for line in page.splitlines():
        if line.startswith('VISITOR:') or line.startswith('HOME:'):
            tag = line.split(' (', 1)[0]
            team = {'VISITOR: New York Knicks': AWAY, 'HOME: MINNESOTA TIMBERWOLVES': HOME}[tag]
        elif match := re.search(r'\bTM FGA: (\d+)\s*$', line):
            if team is None or team not in teams or team in team_fga:
                raise RuntimeError(f'Unpaired team-attempt total: {title}')
            team_fga[team] = int(match[1])
        elif match := re.match(r'^\s*(.*?)\b(\d{2,3}:\d{2})\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)(?:\s|$)', line):
            prefix, _, fgm, fga, fg3m, fg3a = match.groups()
            vector = Counts(*map(int, (fga, fgm, fg3a, fg3m)))
            if team is None:
                raise RuntimeError('No designated side for scorer box row')
            if prefix.strip() == '':
                if team in teams:
                    raise RuntimeError('Duplicate team total')
                teams[team] = vector
                continue
            named = re.fullmatch(r'\s*\d{1,2}\s+(.+?)(?:\s+[FCG])?\s*', prefix)
            if named is None:
                raise RuntimeError(f'Unexpected scorer box player label: {prefix}')
            name = 'Terrence Shannon Jr.' if named[1] == 'Terrence Shannon Jr' else named[1]
            key = roster[name]
            if key[0] != team or key in players:
                raise RuntimeError(f'Wrong-side or duplicate player box row: {named[1]}')
            players[key] = vector
    if set(teams) != {HOME, AWAY} or set(team_fga) != set(teams):
        raise RuntimeError('Two designated team totals required')
    for tid, counts in teams.items():
        if sum_counts([v for (t, _), v in players.items() if t == tid]) != counts:
            raise RuntimeError(f'Individual shooting totals do not sum to the box: {title}, team {tid}')
    return Box(players, teams, team_fga)


def gamebook_attempts(pages: list[str], path: Path, roster: dict[str, tuple[int, int]]) -> list[Attempt]:
    scores = {(x.page, x.printed): x for x in nba_scoring(path)}
    if len(scores) != 99:
        raise RuntimeError('Unexpected nonunique scoring line')
    attempts: list[Attempt] = []
    for pno, page in enumerate(pages, 1):
        if 'OFFICIAL PLAY-BY-PLAY' not in page:
            continue
        q = re.search(r'(?m)^\s*([1-4])(?:st|nd|rd|th) QUARTER\s*$', page)
        if q is None or SCOPE not in page:
            raise RuntimeError(f'Unexpected play page: {pno}')
        for lno, line in enumerate(page.splitlines(), 1):
            tick = LINE.match(line)
            miss = re.search(r'\bMISS (.+?)(?: {3,}|$)', line)
            score = scores.pop((pno, line), None)
            if not (miss or score):
                continue
            if tick is None or (miss is not None and score is not None):
                raise RuntimeError(f'Ambiguous shot line: {pno}:{lno}')
            period, clock = int(q[1]), clock_ms(tick[1])
            if miss:
                if 'Free Throw' in miss[1]:
                    continue
                team = HOME if miss.start() == 8 else AWAY
                if (team == AWAY and miss.start() < 65) or (line[:8].strip() != tick[1]):
                    raise RuntimeError(f'Miss has unexpected side column: {pno}:{lno}')
                if re.fullmatch(r"(?:Knicks|TIMBERWOLVES) \d+' HEAVE", miss[1]):
                    if miss[1].startswith('Knicks ') != (team == AWAY):
                        raise RuntimeError(f'Wrong-side team heave: {pno}:{lno}')
                    attempts.append(Attempt(period, clock, team, None, None, False, pno, lno, line))
                    continue
                desc = miss[1]
                value = 3 if '3PT' in desc else 2
            else:
                if score is None:
                    raise RuntimeError('Scoring observation was lost')
                match = SCORE.search(tick[2])
                if match is None:
                    raise RuntimeError('Missing cumulative score columns')
                team = HOME if score.home_delta else AWAY
                desc = (tick[2][:match.start()].strip() if team == HOME else
                        re.sub(r'^(?:[+\-]\d+\*?|TIE)\s+', '', tick[2][match.end():].strip()))
                value = max(score.home_delta, score.away_delta)
                if 'Free Throw' in desc:
                    if value != 1:
                        raise RuntimeError('Unexpected free throw value')
                    continue
                if value not in (2, 3) or (value == 3) != ('3PT' in desc):
                    raise RuntimeError(f'Field goal value differs from printed score change: {line}')
            who = FGA_NAME.match(desc)
            if who is None:
                raise RuntimeError(f'Unresolved shooter label: {desc}')
            tid, person = roster[ALIASES[who[1]]]
            if tid != team:
                raise RuntimeError(f'Shooter is on the other side: {line}')
            attempts.append(Attempt(period, clock, tid, person, value, score is not None, pno, lno, line))
    if scores or any((a.period, -a.clock_ms) > (b.period, -b.clock_ms) for a, b in zip(attempts, attempts[1:])):
        raise RuntimeError('Unconsumed score lines or unordered gamebook attempts')
    return attempts


def reconcile_box(observations: list[Attempt], box: Box, label: str) -> int:
    grouped: dict[tuple[int, int], list[Counts]] = {}
    team_only: Counter[int] = Counter()
    for row in observations:
        if row.person_id is None:
            team_only[row.team_id] += 1
        else:
            group = grouped.setdefault((row.team_id, row.person_id), [])
            group.append(Counts(1, int(row.made), int(row.shot_value == 3), int(row.made and row.shot_value == 3)))
    observed = {k: sum_counts(v) for k, v in grouped.items()}
    zero = Counts(0, 0, 0, 0)
    if set(observed) - set(box.players) or any(observed.get(k, zero) != v for k, v in box.players.items()):
        raise RuntimeError(f'{label} player shooting differs: {[(k, observed.get(k, zero), v) for k,v in box.players.items() if observed.get(k, zero) != v]}')
    if any(team_only[k] != v for k, v in box.team_fga.items()):
        raise RuntimeError(f'{label} team-only FGA differs: {team_only}')
    return len(box.players)


def reconcile_feed(official: list[Attempt], feed: list[ProviderShot]) -> tuple[list[dict[str, object]], Counter[str]]:
    by_key = {row.credited_key(): row for row in feed}
    if len(by_key) != len(feed) or len({a.credited_key() for a in official if a.person_id is not None}) != sum(a.person_id is not None for a in official):
        raise RuntimeError('Repeated equivalent shot locators require independent adjudication, not tie-breaking')
    unmatched = {row.espn_play_id: row for row in feed}
    ledger: list[dict[str, object]] = []
    orders: list[int] = []
    totals: Counter[str] = Counter()
    for a in official:
        match: ProviderShot | None
        if a.person_id is None:
            candidates = [row for row in unmatched.values() if row.spot() == a.spot() and not row.made
                          and row.shot_value == 3 and 'heave' in row.description.lower()]
            if len(candidates) != 1:
                raise RuntimeError(f'Cannot align the two sources’ same-clock heave observation: {a}')
            match = candidates[0]
            disposition = 'team_heave_not_player_fga'
        else:
            match = by_key.get(a.credited_key())
            disposition = 'matching_credited_attempt' if match else 'nba_credited_attempt_missing_in_espn'
        if match:
            del unmatched[match.espn_play_id]
            orders.append(match.play_order)
        totals[disposition] += 1
        ledger.append({'official': asdict(a), 'espn': asdict(match) if match else None, 'disposition': disposition})

    if unmatched or orders != sorted(set(orders)):
        raise RuntimeError('Provider attempts remain unmatched or sequence order differs')
    return ledger, totals


def audit(parent: Path, pdf: Path, final_box: Path) -> dict[str, object]:
    paths = (parent, pdf, final_box)
    before = [snapshot(p) for p in paths]
    for path, (role, digest) in zip(paths, INPUT_PINS, strict=True):
        with path.open('rb') as f:
            if file_digest(f, 'sha256').hexdigest() != digest:
                raise RuntimeError(f'Exact {role} pin required')
    with duckdb.connect(str(parent), read_only=True, config={'threads': 2}) as c:
        fixture = c.execute('SELECT game_date::VARCHAR,home_team_id,away_team_id,home_score,away_score FROM nba.fact_game WHERE game_id=?', [GAME]).fetchone()
        if fixture != ('2026-11-19', HOME, AWAY, 99, 102):
            raise RuntimeError('Wrong fixture or designated sides')
        named = c.execute('''SELECT p.athlete_display_name,b.team_id,b.person_id
            FROM audit.player_source p JOIN nba.fact_player_game_stats b USING(game_id,person_id)
            WHERE game_id=? AND b.participation_status='played' AND NOT p.did_not_play''', [GAME]).fetchall()
        roster = {name: (tid, pid) for name, tid, pid in named}
        if set(roster) != set(ALIASES.values()) or len(roster) != len(named) or len(set(roster.values())) != len(roster):
            raise RuntimeError('Ambiguous or missing fixture roster names')
        players = {(t,p): Counts(*v) for t,p,*v in c.execute('''SELECT team_id,person_id,fga,fgm,fg3a,fg3m
            FROM nba.fact_player_game_stats WHERE game_id=? AND participation_status='played' ''', [GAME]).fetchall()}
        teams = {t:Counts(*v) for t,*v in c.execute('SELECT team_id,fga,fgm,fg3a,fg3m FROM nba.fact_team_game_stats WHERE game_id=?', [GAME]).fetchall()}
        source = c.execute('''SELECT s.action_number,s.espn_play_id,p.play_order,p.period,p.clock_display_value,
            s.team_id,s.person_id,s.shot_value,s.made,p.source_description
            FROM audit.shot_coordinates s JOIN audit.play_source p USING(game_id,action_number,espn_play_id)
            JOIN audit.player_source a ON a.game_id=p.game_id AND a.espn_team_id=p.espn_team_id
                AND a.espn_athlete_id=p.athlete_id_1 AND a.person_id=s.person_id
            WHERE s.game_id=? ORDER BY p.play_order''', [GAME]).fetchall()
        feed = [ProviderShot(*r) for r in source]
        hold = c.execute('''SELECT (SELECT count(*) FROM audit.play_source WHERE game_id=?),
            (SELECT count(*) FROM audit.shot_coordinates WHERE game_id=?),
            (SELECT count(*) FROM nba.field_goal_attempt WHERE game_id=?),
            (SELECT count(*) FROM nba.fact_play_event WHERE game_id=?)''', [GAME]*4).fetchone()
        if hold != (447,165,0,0) or len(feed) != 165 or len({r.espn_play_id for r in feed}) != len(feed):
            raise RuntimeError('Audited shot source does not cover the exact held stream')
    pages = layout(pdf)
    standalone = layout(final_box)
    if len(pages) != 18 or len(standalone) != 1:
        raise RuntimeError('Unexpected official PDF page inventory')
    final = box_counts(standalone[0], 'FINAL BOX', roster)
    if final != box_counts(pages[0], 'FINAL BOX', roster) or final.players != players or final.teams != teams:
        raise RuntimeError('Official final box shooting does not match retained warehouse box')
    attempts = gamebook_attempts(pages, pdf, roster)
    counts = {q: reconcile_box([r for r in attempts if r.period == q], box_counts(pages[p-1], f'{q}{ordinal} QUARTER ONLY', roster), f'Q{q}')
              for q, p, ordinal in ((1,2,'st'), (2,3,'nd'), (3,5,'rd'), (4,7,'th'))}
    final_checked = reconcile_box(attempts, final, 'Final')
    ledger, dispositions = reconcile_feed(attempts, feed)
    expected = {'matching_credited_attempt': 163, 'team_heave_not_player_fga': 2, 'nba_credited_attempt_missing_in_espn': 1}
    if dispositions != expected or [(a.period,a.clock_ms,a.team_id,a.person_id,a.shot_value,a.made) for a,row in zip(attempts,ledger,strict=True) if row['espn'] is None] != [(1,619000,HOME,1630163,2,True)]:
        raise RuntimeError(f'Unexpected shot-reconciliation disposition: {dispositions}')
    if [snapshot(p) for p in paths] != before:
        raise RuntimeError('An input changed during reconciliation')
    return {'game_id':GAME, 'redistribution_cleared':False, 'sha256':dict(INPUT_PINS), 'gamebook_url':SOURCE,
            'final_box_url':BOX_URL, 'disposition':'audit_only_no_canonical_pbp_or_attempts',
            'espn_attempt_count':len(feed), 'official_observation_count':len(attempts),
            'official_final_team_totals':final.teams, 'official_final_team_heaves':final.team_fga,
            'final_player_lines_checked': final_checked, 'player_quarter_lines_checked':counts,
            'shooting_fields':COLUMNS, 'attempt_dispositions':dict(dispositions), 'attempts':ledger}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent',type=Path,default=ROOT/'data/nba-2026-27-local-2026-11-23.duckdb')
    parser.add_argument('--gamebook',type=Path,default=PDF)
    parser.add_argument('--final-box',type=Path,default=BOX)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument('--output',type=Path,help='Write an immutable local audit receipt (no overwrite)')
    output.add_argument('--verify-receipt',type=Path,help='Recompute and compare every receipt field')
    a = parser.parse_args()
    if a.output is not None and os.path.lexists(a.output):
        raise FileExistsError(f'Will not overwrite {a.output}')
    receipt = audit(a.parent.resolve(strict=True),a.gamebook.resolve(strict=True),a.final_box.resolve(strict=True))
    encoded = json.dumps(receipt,indent=2,sort_keys=True,ensure_ascii=False)+'\n'
    if a.output is not None:
        with tempfile.TemporaryDirectory(prefix='.shot-receipt-',dir=a.output.parent) as scratch:
            staged = Path(scratch)/'receipt.json'
            staged.write_text(encoded,encoding='utf-8')
            os.chmod(staged,0o600)
            os.link(staged,a.output)
    elif a.verify_receipt.read_text(encoding='utf-8') != encoded:
        raise RuntimeError('Exact shot receipt replay differs')
    print(json.dumps({k:v for k,v in receipt.items() if k!='attempts'},indent=2,sort_keys=True))


if __name__=='__main__':
    main()
