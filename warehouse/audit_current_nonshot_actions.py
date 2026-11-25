#!/usr/bin/env python3
"""Audit free throws, turnovers, steals, rebounds and substitutions for one held game."""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from hashlib import file_digest
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, NamedTuple

import duckdb

from audit_current_shot_attempts import ALIASES, AWAY, BOX, BOX_URL, HOME, INPUT_PINS, SCOPE, layout
from audit_current_shot_attempts import audit as shot_audit
from nba_warehouse import ROOT
from recover_local_periods import snapshot
from verify_current_scorer_discrepancy import GAME, LINE, PDF, SOURCE, clock_ms, nba_scoring

FT_TYPES = {97: (1, 1, False), 98: (1, 2, False), 99: (2, 2, False), 103: (1, 1, True)}
TURN_TYPES = {62: 'bad_pass', 63: 'lost_ball', 65: 'double_dribble', 70: 'shot_clock', 73: 'offensive_goaltending',
              84: 'offensive_foul', 86: 'out_of_bounds_step', 87: 'out_of_bounds_lost_ball', 90: 'out_of_bounds_bad_pass'}
TURN_LABELS = {v.replace('_', ' ').title(): v for v in TURN_TYPES.values()} | {'Foul': 'offensive_foul'}
TEAM_LABEL = {'Knicks': AWAY, 'TIMBERWOLVES': HOME}
FT = re.compile(r'\b(MISS )?([A-Z]\.\S+) (?:(Technical|Transition Take) )?Free Throw ([1-3]) of ([1-3])\b')
SUB = re.compile(r'\bSUB: ([A-Z]\.\S+) FOR ([A-Z]\.\S+)$')
REB = re.compile(r'\b([A-Z]\.\S+|Knicks|TIMBERWOLVES) REBOUND$')
TOV = re.compile(r'\b([A-Z]\.\S+|Knicks|TIMBERWOLVES) (.+?) TURNOVER(?: #\d+)?$')
STEAL = re.compile(r'\b([A-Z]\.\S+) STEAL$')
FAMILIES = ('free_throw', 'turnover', 'rebound', 'substitution')


class Key(NamedTuple):
    family: str
    period: int
    clock_ms: int
    team: int
    person: int | None
    other: int | None
    detail: str


@dataclass(frozen=True)
class Official:
    key: Key
    page: int
    line_number: int
    printed: str
    printed_qualifier: str = ''


@dataclass(frozen=True)
class Feed:
    key: Key
    action_number: int
    espn_play_id: int
    order: int
    raw_clock: str
    type_id: int
    text: str


def side(start: int) -> int:
    if start == 8:
        return HOME
    if start >= 65:
        return AWAY
    raise RuntimeError(f'Unrecognized gamebook description column: {start}')


def ft_key(ordinal: int, total: int, technical: bool, made: bool) -> str:
    return f'{ordinal}/{total}:technical={int(technical)}:made={int(made)}'


def official_actions(pages: list[str], pdf: Path, name_to_id: dict[str, tuple[int, int]]) -> tuple[list[Official], dict[tuple[int, int], set[int]]]:
    official_scores = {(r.page, r.printed): r for r in nba_scoring(pdf)}
    result: list[Official] = []
    starters: dict[tuple[int, int], set[int]] = {}
    for pno, page in enumerate(pages, 1):
        if 'OFFICIAL PLAY-BY-PLAY' not in page:
            continue
        quarter = re.search(r'(?m)^\s*([1-4])(?:st|nd|rd|th) QUARTER\s*$', page)
        if quarter is None or SCOPE not in page:
            raise RuntimeError(f'Wrong gamebook page {pno}')
        q = int(quarter[1])
        for lineno, line in enumerate(page.splitlines(), 1):
            if headers := re.fullmatch(r'(Knicks|TIMBERWOLVES) Starters: (.*)', line):
                team = TEAM_LABEL[headers[1]]
                people = [name_to_id[ALIASES[n]] for n in headers[2].split()]
                ids = {person for tid, person in people if tid == team}
                if len(people) != 5 or len(ids) != 5 or starters.setdefault((q, team), ids) != ids:
                    raise RuntimeError(f'Inconsistent period starters: {pno}:{lineno}')
                continue
            raw_clock = LINE.match(line)
            if raw_clock is None:
                continue
            clock = clock_ms(raw_clock[1])
            offs = raw_clock.start(2)
            chunks = [(m.group(), m.start()) for m in re.finditer(r'\S(?:.*?\S)?(?= {3,}|$)', line[offs:])]
            steal = [(m, offs+o+m.start()) for s,o in chunks if (m := STEAL.fullmatch(s))]
            turnover_count = 0
            for chunk, offset in chunks:
                start = offs + offset
                qualifier = ''
                if m := FT.fullmatch(chunk):
                    miss, shooter, subtype, n, total = m.groups()
                    tid, person = name_to_id[ALIASES[shooter]]
                    score = official_scores.get((pno, line))
                    if miss:
                        if score is not None or side(start) != tid:
                            raise RuntimeError(f'Missed FT contradicts score or designated side: {line}')
                    elif score is None or (score.home_delta, score.away_delta) != ((1, 0) if tid == HOME else (0, 1)):
                        raise RuntimeError(f'Made FT contradicts score or designated side: {line}')
                    qualifier = subtype or ''
                    k = Key('free_throw',q,clock,tid,person,None,ft_key(int(n),int(total),subtype=='Technical',not miss))
                elif m := REB.fullmatch(chunk):
                    actor = m[1]
                    tid, who = (TEAM_LABEL[actor], None) if actor in TEAM_LABEL else name_to_id[ALIASES[actor]]
                    if side(start) != tid:
                        raise RuntimeError(f'Rebound appears on other side: {line}')
                    k = Key('rebound',q,clock,tid,who,None,'')
                elif m := TOV.fullmatch(chunk):
                    actor = m[1]
                    tid, who = (TEAM_LABEL[actor], None) if actor in TEAM_LABEL else name_to_id[ALIASES[actor]]
                    if side(start) != tid or len(steal) > 1:
                        raise RuntimeError(f'Turnover side or steal ambiguous: {line}')
                    turnover_count += 1
                    other = None
                    if steal:
                        st, at = steal[0]; stealer_team, other = name_to_id[ALIASES[st[1]]]
                        if stealer_team == tid or stealer_team != side(at):
                            raise RuntimeError(f'Steal side inconsistent with turnover: {line}')
                    k = Key('turnover',q,clock,tid,who,other,TURN_LABELS[m[2]])
                elif m := SUB.fullmatch(chunk):
                    tid, entrant = name_to_id[ALIASES[m[1]]]
                    leaving_team, leaver = name_to_id[ALIASES[m[2]]]
                    if tid != leaving_team or tid != side(start) or entrant == leaver:
                        raise RuntimeError(f'Inconsistent substitution: {line}')
                    k = Key('substitution',q,clock,tid,entrant,leaver,'')
                else:
                    continue
                result.append(Official(k,pno,lineno,line,qualifier))
            if steal and turnover_count != 1:
                raise RuntimeError(f'Steal without exactly one matched turnover: {line}')
    if set(starters) != {(q,t) for q in range(1,5) for t in (HOME,AWAY)}:
        raise RuntimeError('Incomplete keyed period starters')
    return result, starters


def scoreboard_counts(page: str, title: str, roster: dict[str, tuple[int, int]]) -> tuple[dict[tuple[int,int], tuple[int,...]], dict[int, tuple[int,...]], dict[int,int], dict[int,int]]:
    if SCOPE not in page or not any(l.strip()==title for l in page.splitlines()):
        raise RuntimeError(f'Wrong scorer box: {title}')
    players: dict[tuple[int,int], tuple[int,...]] = {}; teams: dict[int, tuple[int,...]] = {}
    total_tov: dict[int,int] = {}; team_reb: dict[int,int] = {}
    tid: int | None = None
    for line in page.splitlines():
        if line.startswith(('VISITOR:', 'HOME:')):
            tid = {'VISITOR: New York Knicks': AWAY, 'HOME: MINNESOTA TIMBERWOLVES': HOME}[line.split(' (',1)[0]]
        elif m := re.search(r'TM REB: (\d+)\s+TOT TO: (\d+)\b',line):
            if tid is None or tid in total_tov:
                raise RuntimeError(f'Duplicate box totals: {line}')
            team_reb[tid], total_tov[tid] = int(m[1]),int(m[2])
        elif m := re.match(r'^\s*(.*?)\b\d{2,3}:\d{2}\s+((?:[-+]?\d+\s*){16})$',line):
            if tid is None:
                raise RuntimeError(f'Box row without team: {line}')
            cols = tuple(map(int,m[2].split()))
            # FTA, FTM, individual REB, TO, STL: explicit scoreboard columns, not derivations.
            fields = tuple(cols[i] for i in (5,4,8,12,11))
            prefix = m[1].strip()
            if not prefix:
                if tid in teams:
                    raise RuntimeError('Duplicate team subtotal')
                teams[tid] = fields
            else:
                name = re.fullmatch(r'\d{1,2}\s+(.+?)(?:\s+[FCG])?',prefix)
                if name is None:
                    raise RuntimeError(f'Unrecognized box player: {prefix}')
                key = roster['Terrence Shannon Jr.' if name[1]=='Terrence Shannon Jr' else name[1]]
                if key[0]!=tid or key in players:
                    raise RuntimeError(f'Wrong-side or duplicated box player: {prefix}')
                players[key] = fields
    if set(teams)!= {HOME,AWAY} or set(total_tov)!=set(teams) or set(team_reb)!=set(teams):
        raise RuntimeError(f'Missing box team totals: {title}')
    return players, teams, total_tov, team_reb


def against_box(events: list[Official], page: str, title: str, roster: dict[str, tuple[int,int]]) -> dict[str, object]:
    players, teams, total_tov, team_reb = scoreboard_counts(page,title,roster)
    actual: dict[tuple[int,int], list[int]] = defaultdict(lambda:[0]*5)
    team_take: Counter[int] = Counter(); team_boards: Counter[int] = Counter()
    for r in events:
        k = r.key
        if k.family=='substitution':
            continue
        if k.family=='turnover' and k.other is not None:
            actual[(AWAY if k.team==HOME else HOME, k.other)][4] += 1
        if k.person is None:
            if k.family not in ('turnover','rebound'):
                raise RuntimeError(f'Missing player on individual action: {r}')
            (team_take if k.family=='turnover' else team_boards)[k.team] += 1
            continue
        vec = actual[(k.team,k.person)]
        if k.family=='free_throw':
            vec[0] += 1; vec[1] += int(k.detail.endswith('made=1'))
        elif k.family=='rebound':
            vec[2] += 1
        else:
            vec[3] += 1
    for key, want in players.items():
        if tuple(actual.get(key,[0]*5)) != want:
            raise RuntimeError(f'{title} exact player FTA/FTM/REB/TO/STL mismatch: {key} {actual.get(key)} vs {want}')
    if set(actual)-set(players):
        raise RuntimeError(f'{title} unboxed actor: {set(actual)-set(players)}')
    for team, subtotal in teams.items():
        counts = tuple(sum(v[i] for k,v in actual.items() if k[0]==team) for i in range(5))
        if counts != subtotal or counts[3]+team_take[team] != total_tov[team]:
            raise RuntimeError(f'{title} team counts differ: {team} {counts} vs {subtotal}')
    # Box-reported team rebounds and printed play observations can have different accounting.
    return {'player_lines_checked':len(players),'player_fields':['fta','ftm','reb','turnovers','steals'],
            'box_team_total_turnovers':total_tov,'printed_team_only_rebounds':dict(team_boards),
            'box_team_only_rebounds':team_reb}


def source_actions(c: duckdb.DuckDBPyConnection, roster: dict[int, tuple[int, int]]) -> tuple[list[Feed], Counter[int]]:
    feed: list[Feed] = []; unscoped: Counter[int] = Counter()
    actions = c.execute('''SELECT action_number,espn_play_id,play_order,period,clock_display_value,espn_team_id,
        athlete_id_1,athlete_id_2,type_id,source_description FROM audit.play_source WHERE game_id=? ORDER BY play_order''',[GAME]).fetchall()
    if len(actions)!=447 or len({r[1] for r in actions})!=447:
        raise RuntimeError('Expected exact held source stream')
    team_key = {16:HOME,18:AWAY}
    for action,eid,order,q,clock,source_team,a,b,t,desc in actions:
        if t not in FT_TYPES and t not in TURN_TYPES and t not in (155,156,584):
            unscoped[t] += 1; continue
        tid = team_key[source_team]
        who = None if a is None else roster[a][1]
        if a is not None and roster[a][0]!=tid:
            raise RuntimeError(f'Provider actor on the other side: {eid}')
        other = None
        if t in FT_TYPES:
            n,total,technical = FT_TYPES[t]
            outcome = re.search(r'\b(makes|misses) (technical )?free throw\b',desc)
            if who is None or not outcome or bool(outcome[2])!=technical:
                raise RuntimeError(f'Unclear free throw: {eid}')
            family, detail = 'free_throw', ft_key(n,total,technical,outcome[1]=='makes')
        elif t in TURN_TYPES:
            family, detail = 'turnover', TURN_TYPES[t]
            if b is not None:
                st, other = roster[b]
                if st==tid or 'steals)' not in desc:
                    raise RuntimeError(f'Unclear provider stealer field: {eid}')
        elif t==584:
            if who is None or b is None or roster[b][0]!=tid:
                raise RuntimeError(f'Unclear provider substitution: {eid}')
            family, detail = 'substitution',''; other = roster[b][1]
        else:
            family, detail = 'rebound',''
        feed.append(Feed(Key(family,q,clock_ms(clock),tid,who,other,detail),action,eid,order,clock,t,desc))
    return feed,unscoped


def clock_batch(lineup: set[int], entries: list[Key]) -> set[int]:
    entered = {k.person for k in entries if k.person is not None}
    exited = {k.other for k in entries if k.other is not None}
    if (len(entered)!=len(entries) or len(exited)!=len(entries) or not exited<=lineup or entered & lineup):
        raise RuntimeError(f'Invalid simultaneous lineup transition: {entries} from {sorted(lineup)}')
    return (lineup-exited)|entered


def starters_control(official: list[Official], feed: list[Feed], starters: dict[tuple[int,int], set[int]]) -> list[dict[str,object]]:
    by_clock: dict[tuple[int,int], dict[int,list[Key]]] = defaultdict(lambda:defaultdict(list))
    for r in official:
        k=r.key
        if k.family=='substitution':
            by_clock[(k.period,k.team)][k.clock_ms].append(k)
    checked=[]
    for t in (HOME,AWAY):
        prior_end=None
        for q in range(1,5):
            start=starters[q,t]
            opening=[r for r in feed if r.key.family=='substitution' and r.key.team==t and r.key.period==q and r.key.clock_ms==720000]
            if q==1:
                if opening:
                    raise RuntimeError('Unexpected Q1 opening-provider change')
            elif prior_end is None or clock_batch(prior_end,[r.key for r in opening])!=start:
                raise RuntimeError(f'Opening substitutions fail official lineup control: Q{q}: {t}')
            else:
                checked.append({'period':q,'team':t,'prior_end':sorted(prior_end),'official_starters':sorted(start),
                                'espn_opening_substitutions':[r.espn_play_id for r in opening]})
            lineup=start
            for clock in sorted(by_clock[q,t],reverse=True):
                lineup=clock_batch(lineup,by_clock[q,t][clock])
            prior_end=lineup
    return checked


class MissAnchor(NamedTuple):
    period: int
    clock: int
    shared_miss_id: int | None


def rebound_anchors(official: list[Official], feed: list[Feed], shots: object) -> dict[str,object]:
    if not isinstance(shots,list):
        raise RuntimeError('Expected exact replay of prior shot observations')
    nba: dict[tuple[int,int],MissAnchor] = {}
    espn: dict[tuple[int,int],MissAnchor] = {}
    receipts: dict[int,dict[str,Any]] = {}
    for row in shots:
        a,b = row['official'],row['espn']
        shot_id: int | None = b['espn_play_id'] if row['disposition']=='matching_credited_attempt' and not a['made'] else None
        nba[a['page'],a['line']] = MissAnchor(a['period'],a['clock_ms'],shot_id)
        if b is not None:
            espn[b['play_order'],0] = MissAnchor(b['period'],clock_ms(b['clock_display_value']),shot_id)
        if shot_id is not None:
            receipts[shot_id] = row
    # A subsequent free throw or turnover invalidates the preceding FGA anchor.
    for r in official:
        if r.key.family in ('free_throw','turnover'):
            nba[r.page,r.line_number] = MissAnchor(r.key.period,r.key.clock_ms,None)
    for e in feed:
        if e.key.family in ('free_throw','turnover'):
            espn[e.order,0] = MissAnchor(e.key.period,e.key.clock_ms,None)
    nk,ek=sorted(nba),sorted(espn)

    def preceding(order: tuple[int,int], key: Key, locators: list[tuple[int,int]],
                  timeline: dict[tuple[int,int],MissAnchor]) -> int:
        at = bisect_left(locators,order)-1
        if at < 0:
            raise RuntimeError(f'Rebound has no prior action: {key}')
        anchor = timeline[locators[at]]
        if anchor.period!=key.period or anchor.clock<key.clock_ms or anchor.shared_miss_id is None:
            raise RuntimeError(f'Rebound has no preceding shared miss in its period: {key}')
        return anchor.shared_miss_id

    nr: dict[int,Official] = {}; er: dict[int,Feed] = {}
    for r in official:
        if r.key.family=='rebound' and r.key.person is not None:
            sid=preceding((r.page,r.line_number),r.key,nk,nba)
            if sid in nr:
                raise RuntimeError(f'Two official player rebounds share one miss: {sid}')
            nr[sid]=r
    for e in feed:
        if e.key.family=='rebound' and e.key.person is not None:
            sid=preceding((e.order,0),e.key,ek,espn)
            if sid in er:
                raise RuntimeError(f'Two provider player rebounds share one miss: {sid}')
            er[sid]=e
    if nr.keys()!=er.keys() or len(nr)!=79:
        raise RuntimeError('The player rebound anchor sets disagree')
    attribution_conflicts=[]; links=[]; offsets: Counter[int]=Counter()
    for sid,a in nr.items():
        b=er[sid]; shot=receipts[sid]
        clock_delta=b.key.clock_ms-a.key.clock_ms; offsets[clock_delta]+=1
        same=(a.key.team,a.key.person)==(b.key.team,b.key.person)
        link={'miss_espn_id':sid,'official_miss_page':shot['official']['page'],'official_miss_line':shot['official']['line'],
              'espn_minus_nba_clock_ms':clock_delta,'same_attribution':same,'official_rebound':asdict(a),'espn_rebound':asdict(b)}
        if not same:
            attribution_conflicts.append(link)
        links.append(link)
    return {'player_rebounds_with_shared_miss':len(links),'espn_minus_nba_clock_ms_counts':dict(offsets),
            'attribution_conflicts':attribution_conflicts,'player_links':links}


def evidence(parent: Path, pdf: Path, box: Path) -> dict[str,object]:
    paths=(parent,pdf,box); before=[snapshot(p) for p in paths]
    for path,(role,digest) in zip(paths,INPUT_PINS,strict=True):
        with path.open('rb') as f:
            if file_digest(f,'sha256').hexdigest()!=digest:
                raise RuntimeError(f'Exact {role} pin required')
    with duckdb.connect(str(parent),read_only=True,config={'threads':2}) as c:
        if c.execute('SELECT game_date::VARCHAR,home_team_id,away_team_id,home_score,away_score FROM nba.fact_game WHERE game_id=?',[GAME]).fetchone()!=('2026-11-19',HOME,AWAY,99,102):
            raise RuntimeError('Wrong held fixture')
        rows=c.execute('''SELECT p.espn_athlete_id,p.athlete_display_name,b.team_id,b.person_id
            FROM audit.player_source p JOIN nba.fact_player_game_stats b USING(game_id,person_id)
            WHERE game_id=? AND b.participation_status='played' AND NOT p.did_not_play''',[GAME]).fetchall()
        roster={n:(t,p) for e,n,t,p in rows}
        roster_ids={e:(t,p) for e,n,t,p in rows}
        if set(roster)!=set(ALIASES.values()) or len(rows)!=len(roster) or len(roster_ids)!=len(roster):
            raise RuntimeError('Expected exact played roster with one-to-one identity')
        feed,unscoped=source_actions(c,roster_ids)
        if c.execute('SELECT count(*) FROM nba.fact_play_event WHERE game_id=?',[GAME]).fetchone()!=(0,):
            raise RuntimeError('Game stream no longer held')
    pages=layout(pdf); scorer=layout(box)
    if len(pages)!=18 or len(scorer)!=1 or scoreboard_counts(scorer[0],'FINAL BOX',roster)!=scoreboard_counts(pages[0],'FINAL BOX',roster):
        raise RuntimeError('Standalone and embedded final scorer box totals differ')
    official,starters=official_actions(pages,pdf,roster)
    checks={f'Q{q}':against_box([r for r in official if r.key.period==q],pages[p-1],name,roster)
            for q,p,name in ((1,2,'1st QUARTER ONLY'),(2,3,'2nd QUARTER ONLY'),(3,5,'3rd QUARTER ONLY'),(4,7,'4th QUARTER ONLY'))}
    checks['final']=against_box(official,scorer[0],'FINAL BOX',roster)
    opening=starters_control(official,feed,starters)
    rebounds=rebound_anchors(official,feed,shot_audit(parent,pdf,box)['attempts'])
    opening_ids={r.espn_play_id for r in feed if r.key.family=='substitution' and r.key.period>1 and r.key.clock_ms==720000}
    ob: dict[Key,list[Official]]=defaultdict(list); fb: dict[Key,list[Feed]]=defaultdict(list)
    for source_row in official: ob[source_row.key].append(source_row)
    for provider_row in feed: fb[provider_row.key].append(provider_row)
    matched: list[dict[str,dict[str,Any]]] = []
    left: list[dict[str,Any]] = []
    ambiguous=[]; ambiguous_keys:set[Key]=set()
    for event_key,events in ob.items():
        source_rows=fb.get(event_key,[])
        if len(events)==len(source_rows)==1:
            matched.append({'official':asdict(events[0]),'espn':asdict(source_rows[0])})
        elif source_rows:
            ambiguous_keys.add(event_key)
            ambiguous.append({'official':[asdict(r) for r in events], 'espn':[asdict(x) for x in source_rows]})
        else:
            left.extend(asdict(a) for a in events)
    opening_rows = [asdict(r) for r in feed if r.espn_play_id in opening_ids]
    right = [asdict(r) for r in feed if r.key not in ob and r.espn_play_id not in opening_ids]
    if len(matched)+len(left)+sum(len(ob[k]) for k in ambiguous_keys)!=len(official) or len(matched)+len(right)+len(opening_rows)+sum(len(fb[k]) for k in ambiguous_keys)!=len(feed):
        raise RuntimeError('Missing or multiply accounted observations')
    if [snapshot(p) for p in paths]!=before:
        raise RuntimeError('Input changed during replay')
    summaries={f:{'official':sum(r.key.family==f for r in official),'espn':sum(r.key.family==f for r in feed),
                  'exact_matches':sum(item['official']['key'][0]==f for item in matched),
                  'official_only':sum(item['key'][0]==f for item in left),
                  'espn_only_not_opening_change':sum(item['key'][0]==f for item in right),
                  'ambiguous_key_count':sum(k.family==f for k in ambiguous_keys)} for f in FAMILIES}
    return {'game_id':GAME,'sha256':dict(INPUT_PINS),'gamebook_url':SOURCE,'final_box_url':BOX_URL,
            'disposition':'audit_only_no_canonical_actions','redistribution_cleared':False,
            'key_fields':Key._fields,'family_summary':summaries,'scorer_checks':checks,'provider_unscoped_type_counts':dict(unscoped),
            'period_opening_lineups':opening,'espn_opening_substitutions':opening_rows,'exact':matched,
            'official_only_at_exact_clock':left,'espn_only_at_exact_clock':right,'ambiguous':ambiguous,
            'player_rebound_miss_anchor_audit':rebounds}


def main() -> None:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--parent',type=Path,default=ROOT/'data/nba-2026-27-local-2026-11-23.duckdb')
    p.add_argument('--gamebook',type=Path,default=PDF);p.add_argument('--final-box',type=Path,default=BOX)
    args=p.add_mutually_exclusive_group(required=True);args.add_argument('--output',type=Path);args.add_argument('--verify-receipt',type=Path)
    a=p.parse_args(); output: Path | None=a.output
    if output is not None and os.path.lexists(output):
        raise FileExistsError(f'Will not overwrite {output}')
    result=evidence(a.parent.resolve(strict=True),a.gamebook.resolve(strict=True),a.final_box.resolve(strict=True))
    encoded=json.dumps(result,indent=2,sort_keys=True,ensure_ascii=False)+'\n'
    if output is not None:
        with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nonshot-receipt-') as d:
            staged=Path(d)/'receipt.json';staged.write_text(encoded,encoding='utf-8');os.chmod(staged,0o600);os.link(staged,output)
    elif a.verify_receipt.read_text(encoding='utf-8')!=encoded:
        raise RuntimeError('Exact non-shot receipt replay differs')
    print(json.dumps({k:result[k] for k in ('game_id','family_summary','scorer_checks')},indent=2,sort_keys=True))


if __name__=='__main__':
    main()
