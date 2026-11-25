#!/usr/bin/env python3
"""Read-only scoring-event adjudication for the held 2026-11-19 Knicks/Timberwolves feed."""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
from hashlib import file_digest
import json
from pathlib import Path
import re
import subprocess
from typing import NamedTuple

import duckdb

from nba_warehouse import ROOT
from recover_local_periods import snapshot

GAME='0022600272'
SOURCE='https://statsdmz.nba.com/pdfs/20261119/20261119_NYKMIN_book.pdf'
PINS=(('parent','4dfa9a966799c7555649af189d187c23e3423f8ab29f7ccbabc5011e7cbd5e0c'),
      ('gamebook','b7c802ab4aadf92711fb89c91804c8844d2081bf36b36fa9ba273306fd29250e'))
PDF=Path.home()/'Library/Application Support/NBA Warehouse/evidence/nba-official-2026-11-19-NYKMIN/20261119_NYKMIN_book.pdf'
LINE=re.compile(r'^(\d{0,2}:\d{1,2}(?:\.\d)?)\s+(.+)$')
SCORE=re.compile(r'(?<!\S)(\d{1,3})-(\d{1,3})(?=\s+(?:[+\-]\d+\*?|TIE)\b)')
END=re.compile(r'End of ([1-4])(?:st|nd|rd|th) QUARTER \((\d+)-(\d+)\)')
BOUNDARIES=[(1,26,33),(2,50,65),(3,78,90),(4,99,102)]


class Scoring(NamedTuple):
    period: int
    clock_ms: int
    home_delta: int
    away_delta: int
    page: int
    printed: str
    home_after: int
    away_after: int


def clock_ms(value: str) -> int:
    if not re.fullmatch(r'\d{0,2}:\d{1,2}(?:\.\d)?',value):
        raise RuntimeError(f'Invalid game clock: {value}')
    mm,ss=value.split(':'); sec=Decimal(ss)
    if not 0<=sec<60 or not 0<=int(mm or '0')*60+sec<=720:
        raise RuntimeError(f'Clock outside regulation: {value}')
    return int((int(mm or '0')*60+sec)*1000)


def nba_scoring(pdf: Path) -> list[Scoring]:
    text=subprocess.check_output(['pdftotext','-layout',str(pdf),'-'],text=True,encoding='utf-8')
    out: list[Scoring]=[]; last=(0,0); boundaries=[]
    for pno,page in enumerate(text.split('\f'),1):
        if 'OFFICIAL PLAY-BY-PLAY' not in page:
            continue
        quarter=re.search(r'(?m)^\s*([1-4])(?:st|nd|rd|th) QUARTER\s*$',page)
        if quarter is None or 'New York Knicks at MINNESOTA TIMBERWOLVES' not in page or 'Thursday, November 19, 2026 Target Center' not in page:
            raise RuntimeError(f'Gamebook page fixture differs: {pno}')
        period=int(quarter[1])
        for line in page.splitlines():
            end=END.search(line)
            if end is not None:
                q,h,a=map(int,end.groups())
                if q!=period or (h,a)!=last:
                    raise RuntimeError(f'Official played boundary disagrees: {pno}')
                boundaries.append((q,h,a))
            e=LINE.match(line)
            s=SCORE.search(e[2]) if e else None
            if not e or not s:
                continue
            h,a=map(int,s.groups())
            if (h,a)==last:
                continue  # Possession headers can reprint the current score.
            dh,da=h-last[0],a-last[1]
            if min(dh,da)!=0 or not 1<=max(dh,da)<=3:
                raise RuntimeError(f'Unexpected scoring transition: {line}')
            last=h,a
            out.append(Scoring(period,clock_ms(e[1]),dh,da,pno,line,h,a))
    if boundaries!=BOUNDARIES or len(out)!=99 or any((a.period,-a.clock_ms)>(b.period,-b.clock_ms) for a,b in zip(out,out[1:])):
        raise RuntimeError('Official boundaries, scoring multiplicity, or ordering differs')
    return out


def main() -> None:
    a=argparse.ArgumentParser(description=__doc__)
    a.add_argument('--parent',type=Path,default=ROOT/'data/nba-2026-27-local-2026-11-23.duckdb')
    a.add_argument('--gamebook',type=Path,default=PDF)
    a.add_argument('--output',type=Path,help='Optional new local JSON receipt; never overwrites')
    args=a.parse_args(); p=args.parent.resolve(strict=True);pdf=args.gamebook.resolve(strict=True)
    before=(snapshot(p),snapshot(pdf))
    for path,(role,want) in zip((p,pdf),PINS,strict=True):
        with path.open('rb') as f:
            if file_digest(f,'sha256').hexdigest()!=want:
                raise RuntimeError(f'Exact {role} pin required')
    official=nba_scoring(pdf)
    with duckdb.connect(str(p),read_only=True,config={'threads':2}) as c:
        fixture=c.execute("SELECT game_date::VARCHAR,home_team_id,away_team_id,home_score,away_score FROM nba.fact_game WHERE game_id=?",[GAME]).fetchone()
        if fixture!=('2026-11-19',1610612750,1610612752,99,102):
            raise RuntimeError('Expected the exact Target Center fixture')
        sql='''WITH scored AS (SELECT period,clock_display_value,play_order,
            home_score-coalesce(lag(home_score) OVER w,0) h,away_score-coalesce(lag(away_score) OVER w,0) a
            FROM audit.play_source WHERE game_id=? WINDOW w AS (ORDER BY play_order))
            SELECT period,clock_display_value,h,a FROM scored WHERE h<>0 OR a<>0 ORDER BY play_order'''
        feed=[(int(q),clock_ms(clock),int(h),int(a)) for q,clock,h,a in c.execute(sql,[GAME]).fetchall()]
        hold=c.execute('SELECT (SELECT count(*) FROM audit.play_source WHERE game_id=?),(SELECT count(*) FROM nba.fact_play_event WHERE game_id=?)',[GAME,GAME]).fetchone()
        if hold!=(447,0) or len(feed)!=98 or any(min(h,a)!=0 or not 1<=max(h,a)<=3 for _,_,h,a in feed):
            raise RuntimeError('Expected held 447-action stream and 98 valid provider scoring changes')
        key=c.execute('SELECT person_id FROM audit.player_source WHERE game_id=? AND espn_athlete_id=4432816 AND athlete_display_name=\'LaMelo Ball\'',[GAME]).fetchall()
        if key!=[(1630163,)]:
            raise RuntimeError('Unexpected local LaMelo Ball identity')
    difference=Counter(x[:4] for x in official)-Counter(feed)
    reverse=Counter(feed)-Counter(x[:4] for x in official)
    extra=[x for x in official if x[:4] in difference]
    if difference!={(1,619000,2,0):1} or reverse or len(extra)!=1 or extra[0].page!=9 or 'L.Ball Driving Finger Roll Layup' not in extra[0].printed:
        raise RuntimeError(f'Unexpected scored event difference: {difference}; {reverse}')
    if [x[:4] for x in official if x != extra[0]] != feed:
        raise RuntimeError('Provider order differs after the official-only action is removed')
    if (snapshot(p),snapshot(pdf))!=before:
        raise RuntimeError('An input changed during replay')
    result={'game_id':GAME,'source':SOURCE,'sha256':dict(PINS),'provider_scoring_events':len(feed),'official_scoring_events':len(official),
            'official_cumulative_home_away':BOUNDARIES,'official_only':extra[0]._asdict(),'person_id':1630163,
            'comparison_grain':'ordered period, displayed clock in milliseconds and home/away score change',
            'play_feed_disposition':'hold_full_espn_stream; do not invent a provider event ID', 'redistribution_cleared':False}
    encoded=json.dumps(result,indent=2)+'\n'
    if args.output:
        with args.output.open('x',encoding='utf-8') as f: f.write(encoded)
    print(encoded,end='')


if __name__=='__main__':
    main()
