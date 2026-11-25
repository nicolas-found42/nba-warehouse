#!/usr/bin/env python3
"""Replay five original 2006 NBA preseason box pages and recover three missing line scores."""
from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256 as hash_bytes
from html import unescape
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import NamedTuple

import duckdb

from nba_warehouse import ROOT, SCRATCH, sql_string, verify
from recover_archive_periods import sha256
from recover_archive_periods_v23 import UNCHANGED as V23_UNCHANGED
from recover_legacy_attempts_v18 import verify_inherited
from recover_local_periods import difference, snapshot
from recover_pbp_attempts import open_config

PARENT = '1dc219dd3eea0bf152e29d22d34f19d3c21d5c299ff72beb64cbe088d8959286'
CACHE = Path.home()/'Library/Application Support/NBA Warehouse/evidence/wayback-oct2006-v25'
MANIFEST = '00b08ed1ac85f1ca693250a7a29aac03a36bfa8f28874a4cc5b79492fdc4f666'
CDX = 'eb525e9468d81224380d188d58eeae50d51052f483201fcea642484dbc4eac88'
SOURCE = 'archived_nba_preseason_line_score_v25'
RECEIPT = 'audit.preseason_period_receipt_v25'
CELLS = 'audit.preseason_period_cells_v25'
INPUT = 'audit.preseason_period_input_v25'
PINS = [('capture_manifest',MANIFEST),('cdx_response',CDX),('parent_v24',PARENT)]
FROZEN = {'period_availability_v7':'audit.preseason_prior_v7_v25',
          'period_availability_v23':'audit.preseason_prior_v23_v25'}
UNCHANGED = V23_UNCHANGED + ('audit.period_availability_v23','audit.local_period_recovery',
                            'audit.allstar_player_receipt_v24','audit.allstar_player_publication_v24')


class Fixture(NamedTuple):
    day: int
    slug: str
    home: int
    away: int
    home_label: str
    away_label: str
    home_logo: str
    away_logo: str
    disposition: str
    periods: int = 4


FIXTURES = {
 '0010600001': Fixture(5,'PHIBAR',12304,1610612755,'Winterthur FC','76ers','fcb','sixers','control'),
 '0010600007': Fixture(9,'TORWAS',1610612764,1610612761,'Wizards','Raptors','wizards','raptors','control'),
 '0010600016': Fixture(11,'TELPHX',1610612756,12303,'Suns','Maccabi Elite','suns','maccabi','admitted'),
 '0010600024': Fixture(12,'SEALAL',1610612747,1610612760,'Lakers','Supersonics','lakers','sonics','admitted'),
 '0010600025': Fixture(12,'SACDAL',1610612742,1610612758,'Mavericks','Kings','mavericks','kings','admitted'),
}
DDL = f'''
CREATE TABLE {RECEIPT} (
 game_id VARCHAR PRIMARY KEY, game_date DATE NOT NULL, home_id BIGINT NOT NULL, away_id BIGINT NOT NULL,
 home_label VARCHAR NOT NULL,away_label VARCHAR NOT NULL, home_final INTEGER NOT NULL,away_final INTEGER NOT NULL,
 home_periods INTEGER[4] NOT NULL,away_periods INTEGER[4] NOT NULL,
 source_url VARCHAR UNIQUE NOT NULL,sha256 VARCHAR NOT NULL, prior_status VARCHAR NOT NULL,
 prior_source VARCHAR, prior_candidates INTEGER NOT NULL,disposition VARCHAR NOT NULL CHECK(disposition IN ('admitted','control')),
 redistribution_cleared BOOLEAN NOT NULL CHECK(NOT redistribution_cleared));
CREATE TABLE {INPUT}(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);
CREATE VIEW {CELLS} AS
SELECT r.game_id,b.team_id,n::SMALLINT AS period_number,
 CASE b.side WHEN 'home' THEN r.home_periods[n] ELSE r.away_periods[n] END AS points
FROM {RECEIPT} r JOIN nba.bridge_game_team b USING(game_id),range(1,5) p(n)
WHERE disposition='admitted';
CREATE VIEW audit.period_availability_v25 AS
SELECT v.* REPLACE(CASE WHEN r.disposition='admitted' THEN 'published' ELSE v.status END AS status,
 CASE WHEN r.disposition='admitted' THEN '{SOURCE}' ELSE v.selected_source END AS selected_source,
 CASE WHEN r.disposition='admitted' THEN '{SOURCE}' ELSE v.recovery_disposition END AS recovery_disposition,
 CASE WHEN r.disposition='admitted' THEN 4::SMALLINT ELSE v.last_period END AS last_period,
 CASE WHEN r.disposition='admitted' THEN false ELSE v.derived_overtime END AS derived_overtime,
 CASE WHEN r.disposition='admitted' THEN 'agrees' ELSE v.overtime_status END AS overtime_status)
FROM audit.period_availability_v23 v LEFT JOIN {RECEIPT} r USING(game_id);
'''


def read_inputs(cache: Path) -> list[tuple[str,str,str,bytes]]:
    manifest,cdx = ((cache/n).read_bytes() for n in ('captures.json','oct2006_box_cdx.json'))
    if (hash_bytes(manifest).hexdigest(),hash_bytes(cdx).hexdigest())!=(MANIFEST,CDX):
        raise RuntimeError('Expected exact preseason capture and CDX manifests')
    indexed = {(original,ts) for original,ts,code in json.loads(cdx)[1:] if code=='200'}
    files: list[tuple[str,str,str,bytes]] = []
    for r in json.loads(manifest):
        if r['status']!='captured':
            continue
        gid, url, digest = r['game_id'],r['source_url'],r['sha256']
        f = FIXTURES[gid]
        d = date(2006,10,f.day)
        original = f'http://www.nba.com:80/games/{d:%Y%m%d}/{f.slug}/boxscore.html'
        match = re.fullmatch(r'https://web\.archive\.org/web/(\d{14})id_/'+re.escape(original),url)
        if (r['file']!=f'{gid}.html' or r['game_date']!=d.isoformat() or r['slug']!=f.slug
            or match is None or (original,match[1]) not in indexed):
            raise RuntimeError(f'Capture identity differs: {gid}')
        p = cache/r['file']
        raw = p.read_bytes()
        if p.is_symlink() or len(raw)!=r['bytes'] or hash_bytes(raw).hexdigest()!=digest:
            raise RuntimeError(f'Pinned original box page differs: {gid}')
        files.append((gid,url,digest,raw))
    if sorted(x[0] for x in files)!=sorted(FIXTURES):
        raise RuntimeError('Expected exact five captured game IDs')
    return files


def read_lines(gid: str, raw: bytes, f: Fixture) -> tuple[list[int],list[int],int,int]:
    text = raw.decode('latin1')
    blocks = re.findall(r'class="gameStatsTop"\s*>(.*?)<div\s+class="gameStats"',text,re.S)
    if len(blocks)!=1 or re.findall(r'Oct\s+(\d+)\s+SCORES',text)!=[str(f.day)]:
        raise RuntimeError(f'Original scoreboard/date differs: {gid}')
    top = blocks[0]
    if re.findall(r'id="visitor_team_score-(\d{10})"',top)!=[gid] or re.findall(
            r'class="teamLogoTop" id="([^"]+)"[^>]*>\s*<p>(visitor|home)</p>',top)!=[(f.away_logo,'visitor'),(f.home_logo,'home')]:
        raise RuntimeError(f'Original game key or sides differ: {gid}')
    tables = re.findall(r'<table[^>]*\bclass\s*=\s*["\']?miniBoxscore["\']?[^>]*>(.*?)</table>',top,re.S|re.I)
    if len(tables)!=1:
        raise RuntimeError(f'Original mini box table not unique: {gid}')
    rows = [[unescape(re.sub('<[^>]+>','',x)).strip() for x in re.findall(r'<td\b[^>]*>(.*?)</td>',r,re.S|re.I)]
            for r in re.findall(r'<tr\b[^>]*>(.*?)</tr>',tables[0],re.S|re.I)]
    header = ['FINAL','1','2','3','4'] + (['OT'] if f.periods==5 else []) + ['']*(8-f.periods)+['T']
    stop = f.periods+1
    if (f.periods not in (4,5) or len(rows)!=3 or any(len(r)!=10 for r in rows) or rows[0]!=header
        or [r[0] for r in rows[1:]]!=[f.away_label,f.home_label]
        or any(any(r[stop:9]) or any(not re.fullmatch(r'[0-9]{1,3}',n) for n in r[1:stop]+r[9:]) for r in rows[1:])):
        raise RuntimeError(f'Unexpected period labels, overtime slots or sides: {gid}')
    away,home = (list(map(int,r[1:stop])) for r in rows[1:]); a,h = (int(r[-1]) for r in rows[1:])
    if (sum(away)!=a or sum(home)!=h or any(aq+hq<=0 for aq,hq in zip(away,home,strict=True)) or a==h
        or (f.periods==5 and sum(away[:4])!=sum(home[:4]))):
        raise RuntimeError(f'Printed totals do not reconcile: {gid}')
    return home,away,h,a


def derive(c: duckdb.DuckDBPyConnection, parent: Path, files: list[tuple[str,str,str,bytes]]) -> None:
    c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    c.execute(f'CREATE TEMP TABLE expected_preseason_v25 AS SELECT * FROM {RECEIPT} LIMIT 0')
    for gid,url,digest,raw in files:
        f = FIXTURES[gid]
        home,away,h,a = read_lines(gid,raw,f)
        day = date(2006,10,f.day)
        old = c.execute('''SELECT g.game_date,g.season_type,g.home_team_id,g.away_team_id,g.home_score,g.away_score,
           g.is_overtime,p.status,p.selected_source,p.candidate_sources,p.valid_sources FROM prior.nba.fact_game g
           JOIN prior.audit.period_coverage p USING(game_id) WHERE g.game_id=?''',[gid]).fetchone()
        if old is None or old[:7]!=(day,'Preseason',f.home,f.away,h,a,False):
            raise RuntimeError(f'Archive and pinned fixture disagree: {gid}')
        actual = c.execute('SELECT team_id,period_number,points FROM prior.nba.fact_game_period_score WHERE game_id=? ORDER BY 1,2',[gid]).fetchall()
        expected = sorted((team,i,n) for team,periods in ((f.home,home),(f.away,away)) for i,n in enumerate(periods,1))
        if f.disposition=='control':
            if old[7]!='published' or actual!=expected:
                raise RuntimeError(f'Printed control does not match retained cells: {gid}')
        elif actual or old[7:]!=('no_supported_source',None,0,0):
            raise RuntimeError(f'Target is not an exact empty prior: {gid}')
        c.execute('INSERT INTO expected_preseason_v25 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [gid,day,f.home,f.away,f.home_label,f.away_label,h,a,home,away,url,digest,*old[7:10],f.disposition,False])


def replay(c: duckdb.DuckDBPyConnection, parent: Path, files: list[tuple[str,str,str,bytes]]) -> int:
    derive(c,parent,files)
    if difference(c,f'SELECT * FROM {RECEIPT}','SELECT * FROM expected_preseason_v25'):
        raise RuntimeError('Exact archived preseason page replay differs')
    if c.execute(f'SELECT * FROM {INPUT} ORDER BY role').fetchall()!=PINS:
        raise RuntimeError('Preseason input pins differ')
    scope = "(SELECT game_id FROM expected_preseason_v25 WHERE disposition='admitted')"
    for view,frozen in FROZEN.items():
        if difference(c,f'SELECT * FROM {frozen}',f'SELECT * FROM prior.audit.{view} SEMI JOIN {scope} USING(game_id)'):
            raise RuntimeError(f'Inherited availability snapshot differs: {view}')
    for rel in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline','nba.game_overtime'):
        suffix = f' ANTI JOIN {scope} USING(game_id)'
        if difference(c,f'SELECT * FROM {rel}{suffix}',f'SELECT * FROM prior.{rel}{suffix}'):
            raise RuntimeError(f'Out-of-scope period recovery change: {rel}')
    if difference(c,f'SELECT * FROM audit.period_availability_v25 ANTI JOIN {scope} USING(game_id)',
                  f'SELECT * FROM prior.audit.period_availability_v23 ANTI JOIN {scope} USING(game_id)'):
        raise RuntimeError('Out-of-scope availability change')
    checks = verify(c)
    for relation in UNCHANGED:
        verify_inherited(c,relation)
    return checks


def stage(c: duckdb.DuckDBPyConnection, parent: Path, files: list[tuple[str,str,str,bytes]]) -> None:
    c.begin()
    c.execute(DDL)
    derive(c,parent,files)
    c.execute(f'INSERT INTO {RECEIPT} SELECT * FROM expected_preseason_v25')
    for view,frozen in FROZEN.items():
        name = f'audit.{view}'
        c.execute(f'''CREATE TABLE {frozen} AS SELECT * FROM prior.{name}
            SEMI JOIN (SELECT game_id FROM {RECEIPT} WHERE disposition='admitted') USING(game_id)''')
        result = c.execute('SELECT sql FROM duckdb_views() WHERE database_name=current_database() AND schema_name=\'audit\' AND view_name=?',[view]).fetchone()
        prefix = f'CREATE VIEW {name} AS '
        if result is None or not str(result[0]).startswith(prefix):
            raise RuntimeError(f'Unexpected inherited availability definition: {name}')
        body = str(result[0]).removeprefix(prefix).removesuffix(';')
        c.execute(f'''CREATE OR REPLACE VIEW {name} AS
          SELECT * FROM ({body}) ANTI JOIN {frozen} USING(game_id) UNION ALL SELECT * FROM {frozen}''')
    c.execute(f'''INSERT INTO nba.fact_game_period_score SELECT * FROM {CELLS};
       INSERT INTO audit.period_vector_baseline
       SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
             ORDER BY team_id,period_number))) FROM {CELLS} GROUP BY game_id;
       UPDATE audit.period_coverage SET status='published',candidate_sources=1,valid_sources=1,
            selected_source=? WHERE game_id IN (SELECT game_id FROM {RECEIPT} WHERE disposition='admitted');''',[SOURCE])
    c.executemany(f'INSERT INTO {INPUT} VALUES (?,?)',PINS)
    c.commit()
    c.execute('CHECKPOINT')


def run(action: str, parent: Path, cache: Path, output: Path) -> None:
    wal = f'{output}.wal'
    if action=='build' and (os.path.lexists(output) or os.path.lexists(wal)):
        raise FileExistsError(f'Will not overwrite {output} or its WAL')
    before = snapshot(parent)
    if sha256(parent)!=PARENT:
        raise RuntimeError('Expected exact checkpointed v24 parent')
    files = read_inputs(cache)
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-v25-spill-') as spill:
        if action=='verify-inputs':
            output_before = snapshot(output)
            with duckdb.connect(str(output),read_only=True,config=open_config(spill)) as c:
                checks = replay(c,parent,files)
            if snapshot(output)!=output_before or read_inputs(cache)!=files:
                raise RuntimeError('Release or receipt changed during offline replay')
        else:
            with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v25-') as work:
                candidate = Path(work)/'release.duckdb'
                shutil.copyfile(parent,candidate)
                with duckdb.connect(str(candidate),config=open_config(spill)) as c:
                    stage(c,parent,files)
                snapshot(candidate)
                with duckdb.connect(str(candidate),read_only=True,config=open_config(spill)) as c:
                    checks = replay(c,parent,files)
                if snapshot(parent)!=before or os.path.lexists(wal) or read_inputs(cache)!=files:
                    raise RuntimeError('Input changed or destination WAL exists')
                os.link(candidate,output)
    if snapshot(parent)!=before:
        raise RuntimeError('Parent changed during verification')
    print(f'{checks} quality checks passed; five archived pages and {len(UNCHANGED)} inherited relations replayed: {output}')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('build','verify-inputs'))
    p.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v24.duckdb')
    p.add_argument('--cache',type=Path,default=CACHE)
    p.add_argument('--output',type=Path,required=True)
    a = p.parse_args()
    run(a.action,a.parent.resolve(strict=True),a.cache.resolve(strict=True),a.output.absolute())


if __name__=='__main__':
    main()
