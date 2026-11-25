#!/usr/bin/env python3
"""Serial, cache-first Basketball-Reference box-page checks for approved personal use."""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import date
import email.utils
import fcntl
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

import duckdb

from nba_warehouse import ROOT, SCRATCH

ORIGIN = 'https://www.basketball-reference.com'
AGENT = 'NBAWarehouse-Research/1.0 (personal historical data validation)'
MIN_INTERVAL = 6.5
MAX_BYTES = 12*1024*1024
BOX_PATH = re.compile(r'/boxscores/([0-9]{8})[0-9]([A-Z]{3})\.html',re.ASCII)
ID = re.compile(r'[0-9]{10}',re.ASCII)
ABBR = re.compile(r'[A-Z]{3}',re.ASCII)
MONTHS = ('January','February','March','April','May','June','July','August','September','October','November','December')


@dataclass(frozen=True)
class Fixture:
    game_id: str
    day: date
    home_id: int
    away_id: int
    home_final: int
    away_final: int
    source_path: str
    expected_home: str
    expected_away: str


class BoxHTML(HTMLParser):
    def __init__(self, comments: bool=True) -> None:
        super().__init__(convert_charrefs=True)
        self.comments = comments
        self.tables: list[list[dict[str,str]]] = []
        self.canonical: list[str] = []
        self.heading: list[str] = []
        self.in_heading = False
        self.in_line = False
        self.in_body = False
        self.row: dict[str,str] | None = None
        self.stat: str | None = None
        self.text: list[str] = []

    def handle_comment(self, data: str) -> None:
        if self.comments and re.search(r'id=[\"\']line_score[\"\']',data):
            embedded = BoxHTML(comments=False)
            embedded.feed(data)
            self.tables.extend(embedded.tables)

    def handle_starttag(self, tag: str, attrs: list[tuple[str,str | None]]) -> None:
        a = dict(attrs)
        if tag=='link' and a.get('rel')=='canonical' and (url:=a.get('href')) is not None:
            self.canonical.append(url)
        if tag=='h1':
            self.in_heading = True
        if tag=='table' and a.get('id')=='line_score':
            self.in_line = True
            self.tables.append([])
        if not self.in_line:
            return
        if tag=='tbody':
            self.in_body = True
        elif tag=='tr' and self.in_body:
            self.row = {}
        elif tag in ('td','th') and self.row is not None:
            self.stat = a.get('data-stat')
            self.text = []

    def handle_data(self, data: str) -> None:
        if self.in_heading:
            self.heading.append(data)
        if self.stat is not None:
            self.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag=='h1':
            self.in_heading = False
        if not self.in_line:
            return
        if tag in ('td','th') and self.row is not None and self.stat is not None:
            if self.stat in self.row:
                raise ValueError('Duplicate cell in line-score row')
            self.row[self.stat] = ''.join(self.text).strip()
            self.stat = None
        elif tag=='tr' and self.row is not None:
            if self.row:
                self.tables[-1].append(self.row)
            self.row = None
        elif tag in ('tbody','table'):
            self.in_body = False
            if tag=='table':
                self.in_line = False


def parse_box(f: Fixture, raw: bytes) -> dict[str,object]:
    parser = BoxHTML()
    parser.feed(raw.decode('utf-8'))
    url = ORIGIN+f.source_path
    title_date = f'{MONTHS[f.day.month-1]} {f.day.day}, {f.day.year}'
    if parser.canonical!=[url] or not ''.join(parser.heading).strip().endswith(title_date):
        return {'status':'page_identity_disagrees'}
    if not parser.tables:
        return {'status':'line_score_unavailable'}
    if len(parser.tables)!=1 or len(parser.tables[0])!=2:
        return {'status':'invalid_line_score_shape'}
    away,home = parser.tables[0]
    labels = (home.get('team'),away.get('team'))
    out: dict[str,object] = {'source_home': labels[0], 'source_away': labels[1]}
    if labels!=(f.expected_home,f.expected_away):
        return {**out,'status':'designated_side_or_team_disagrees'}
    n = len(home)-2
    numbered = [str(i) for i in range(1,n+1)]
    labelled_overtime = numbered[:4] + [f'{i}OT' for i in range(1,n-3)]
    candidates = [numbered, labelled_overtime]
    if n == 5:
        candidates.append(numbered[:4] + ['OT'])
    columns = next((keys for keys in candidates if set(home) == set(away) == {'team', 'T', *keys}), None)
    if n not in range(4,11) or columns is None:
        return {**out,'status':'invalid_line_score_shape'}
    for row in (home,away):
        if any(re.fullmatch('[0-9]{1,3}',row[k]) is None for k in [*columns, 'T']):
            return {**out,'status':'invalid_line_score_cell'}
    h,a = ([int(row[k]) for k in columns] for row in (home,away))
    totals = int(home['T']),int(away['T'])
    out.update(home_periods=h,away_periods=a,source_home_final=totals[0],source_away_final=totals[1])
    if totals!=(f.home_final,f.away_final):
        return {**out,'status':'final_disagrees'}
    if (sum(h),sum(a))!=totals or any(x+y==0 for x,y in zip(h,a,strict=True)):
        return {**out,'status':'invalid_period_totals'}
    if any(sum(h[:i])!=sum(a[:i]) for i in range(4,n)):
        return {**out,'status':'untied_before_overtime'}
    return {**out,'status':'matched_fixture_exact_printed_periods'}


def fixtures(db: Path, seed: Path) -> list[Fixture]:
    result: list[Fixture] = []
    with duckdb.connect(str(db),read_only=True,config={'threads':2}) as c, seed.open(newline='') as f:
        seen: set[str] = set()
        for row in csv.DictReader(f):
            gid,path = row['game_id'],row['source_path']
            m = BOX_PATH.fullmatch(path)
            labels = row['expected_home'],row['expected_away']
            if not ID.fullmatch(gid) or gid in seen or m is None or not all(ABBR.fullmatch(x) for x in labels):
                raise ValueError(f'Malformed or duplicate fixture key: {gid}')
            v = c.execute('''SELECT g.game_date,g.home_team_id,g.away_team_id,g.home_score,g.away_score
                             FROM nba.fact_game g JOIN audit.period_coverage p USING(game_id)
                             WHERE g.game_id=? AND g.result_status='final' AND p.status<>'published' ''',[gid]).fetchone()
            if v is None or len(v)!=5:
                raise ValueError(f'Not an exact dated period-gap fixture: {gid}')
            day,home_id,away_id,home_final,away_final = v
            if not isinstance(day,date) or m[1]!=day.strftime('%Y%m%d'):
                raise ValueError(f'Box path date differs from the fixture: {gid}')
            seen.add(gid)
            result.append(Fixture(gid,day,home_id,away_id,home_final,away_final,path,labels[0],labels[1]))
    if not result:
        raise ValueError('Queue is empty')
    return result


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class StopFetch(RuntimeError):
    pass


def atomic_write(target: Path, raw: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as f:
        tmp = Path(f.name)
        f.write(raw)
    try:
        os.replace(tmp,target)
    finally:
        tmp.unlink(missing_ok=True)


class Client:
    def __init__(self, cache: Path, shared_rate_dir: Path) -> None:
        self.cache = cache
        self.opener = build_opener(NoRedirect())
        self.state = shared_rate_dir/'next-request-epoch'
        self.robots = RobotFileParser()

    def fetch(self,path: str) -> bytes | None:
        if self.state.exists():
            wait = float(self.state.read_text()) - time.time()
            if wait>MIN_INTERVAL+1:
                raise StopFetch('Server cooldown in effect; no request sent')
            time.sleep(max(0,wait))
        atomic_write(self.state,str(time.time()+MIN_INTERVAL).encode())
        try:
            with self.opener.open(Request(ORIGIN+path,headers={'User-Agent':AGENT}),timeout=25) as r:
                raw: bytes = r.read(MAX_BYTES+1)
                if len(raw)>MAX_BYTES:
                    raise StopFetch('Response exceeds the capture size limit')
                return raw
        except HTTPError as e:
            if e.code==404:
                return None
            if e.code in (403,429,503):
                retry = e.headers.get('Retry-After','')
                try:
                    requested = time.time()+int(retry) if retry.isdigit() else email.utils.parsedate_to_datetime(retry).timestamp()
                except (ValueError,TypeError,OverflowError):
                    requested = 0.0
                atomic_write(self.state,str(max(time.time()+86400,requested)).encode())
            raise StopFetch(f'HTTP {e.code} for {path}; stopped without retry or bypass') from e

    def load_robots(self) -> None:
        peer = self.cache/'robots.txt'
        if not peer.exists() or time.time()-peer.stat().st_mtime>86400:
            raw = self.fetch('/robots.txt')
            if not raw or b'User-agent:' not in raw:
                raise StopFetch('Could not validate robots.txt; no box pages requested')
            atomic_write(peer,raw)
        self.robots.parse(peer.read_text().splitlines())
        delay = self.robots.crawl_delay(AGENT)
        if delay is not None and float(delay)>MIN_INTERVAL:
            raise StopFetch(f'Robots crawl delay {delay}s exceeds the configured interval')


def run(db: Path,seed: Path,cache: Path,out: Path,offline: bool) -> None:
    scope = fixtures(db,seed)
    cache.mkdir(parents=True,exist_ok=True)
    if os.path.lexists(out):
        raise FileExistsError(out)
    results: list[dict[str,object]] = []
    shared_rate_dir = SCRATCH/'nba-bref-request-budget'
    shared_rate_dir.mkdir(parents=True,exist_ok=True)
    with (shared_rate_dir/'fetch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        client = Client(cache,shared_rate_dir)
        if not offline:
            client.load_robots()
        for f in scope:
            cached = cache/Path(f.source_path).name
            raw: bytes | None
            if cached.exists():
                raw = cached.read_bytes()
            elif offline:
                raise StopFetch(f'No cached bytes for {f.game_id}')
            else:
                if not client.robots.can_fetch(AGENT,ORIGIN+f.source_path):
                    raise StopFetch(f'Robots disallows {f.source_path}')
                raw = client.fetch(f.source_path)
                if raw and any(marker in raw[:10000].lower() for marker in (b'<title>just a moment',b'access denied',b'cf-challenge')):
                    atomic_write(client.state,str(time.time()+86400).encode())
                    raise StopFetch('Access challenge; stopped without retry or bypass')
                if raw:
                    atomic_write(cached,raw)
            result = dict(asdict(f))
            result.update(source_url=ORIGIN+f.source_path,sha256=sha256(raw).hexdigest() if raw else None)
            result.update(parse_box(f,raw) if raw is not None else {'status':'http_404'})
            results.append(result)
            atomic_write(out,(json.dumps(results,indent=2,default=str)+'\n').encode())
            print(f'{f.game_id}: {result["status"]}',flush=True)
    print(f'{len(results)}/{len(scope)} reviewed fixtures; personal/local only: {out}')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',type=Path,default=ROOT/'data/nba-clean-v20.duckdb')
    p.add_argument('--seed',type=Path,default=ROOT/'warehouse/bref_period_seed.csv')
    p.add_argument('--cache',type=Path,default=SCRATCH/'nba-bref-personal')
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--offline',action='store_true')
    a=p.parse_args()
    run(a.db.resolve(strict=True),a.seed.resolve(strict=True),a.cache,a.output,a.offline)


if __name__=='__main__':
    main()
