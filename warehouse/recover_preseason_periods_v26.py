#!/usr/bin/env python3
"""Admit the next exact archived 2006 preseason period cohort, including one OT."""
from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256 as hash_bytes
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import duckdb

from nba_warehouse import ROOT,SCRATCH,sql_string,verify
from recover_archive_periods import sha256
from recover_legacy_attempts_v18 import verify_inherited
from recover_local_periods import difference,snapshot
from recover_pbp_attempts import open_config
from recover_preseason_periods_v25 import CDX,FROZEN,Fixture,UNCHANGED as V25_UNCHANGED,read_lines

PARENT='37fde795de511e09c06707c08d23205b1e6fd9a949f4ec4c0bcbcc6b3acc32f8'
MANIFEST='ace02cd8ea1dadb731714a0f35bbd2cbf101e5cdb32fae319ee621eba1a49240'
CACHE=Path.home()/'Library/Application Support/NBA Warehouse/evidence/wayback-oct2006-v26'
SOURCE='archived_nba_preseason_line_score_v26'
RECEIPT='audit.preseason_period_receipt_v26'
CELLS='audit.preseason_period_cells_v26'
PINS=[('capture_manifest',MANIFEST),('cdx_response',CDX),('parent_v25',PARENT)]
UNCHANGED=V25_UNCHANGED+('audit.period_availability_v25','audit.preseason_period_receipt_v25','audit.preseason_period_cells_v25')
FROZEN_V26={v:f'audit.preseason_v26_prior_{v}' for v in FROZEN}
FIXTURES={
 '0010600017':Fixture(11,'BOSTOR',1610612761,1610612738,'Raptors','Celtics','raptors','celtics','admitted',5),
 '0010600030':Fixture(13,'CLEBOS',1610612738,1610612739,'Celtics','Cavaliers','celtics','cavaliers','admitted'),
 '0010600032':Fixture(13,'ATLHOU',1610612745,1610612737,'Rockets','Hawks','rockets','hawks','admitted'),
 '0010600034':Fixture(14,'NYKPHI',1610612755,1610612752,'76ers','Knicks','sixers','knicks','admitted'),
 '0010600058':Fixture(18,'INDDEN',1610612743,1610612754,'Nuggets','Pacers','nuggets','pacers','admitted'),
 '0010600070':Fixture(19,'MILDAL',1610612742,1610612749,'Mavericks','Bucks','mavericks','bucks','admitted'),
 '0010600076':Fixture(20,'NOKLAL',1610612747,1610612740,'Lakers','Hornets','lakers','hornets','admitted'),
 '0010600089':Fixture(22,'PHXLAL',1610612747,1610612756,'Lakers','Suns','lakers','suns','admitted'),
 '0010600092':Fixture(23,'SEAPHX',1610612756,1610612760,'Suns','Supersonics','suns','sonics','admitted'),
 '0010600093':Fixture(23,'UTAPOR',1610612757,1610612762,'Trail Blazers','Jazz','blazers','jazz','admitted'),
 '0010600094':Fixture(24,'INDCHA',1610612766,1610612754,'Bobcats','Pacers','bobcats','pacers','admitted'),
}
DDL=f'''
CREATE TABLE {RECEIPT}(
 game_id VARCHAR PRIMARY KEY, game_date DATE NOT NULL, home_id BIGINT NOT NULL, away_id BIGINT NOT NULL,
 home_label VARCHAR NOT NULL,away_label VARCHAR NOT NULL,home_final INTEGER NOT NULL,away_final INTEGER NOT NULL,
 home_periods INTEGER[] NOT NULL,away_periods INTEGER[] NOT NULL,source_url VARCHAR UNIQUE NOT NULL,
 sha256 VARCHAR NOT NULL,redistribution_cleared BOOLEAN NOT NULL CHECK(NOT redistribution_cleared));
CREATE TABLE audit.preseason_period_input_v26(role VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL);
CREATE VIEW {CELLS} AS SELECT r.game_id,b.team_id,n::SMALLINT AS period_number,
 CASE b.side WHEN 'home' THEN r.home_periods[n] ELSE r.away_periods[n] END AS points
FROM {RECEIPT} r JOIN nba.bridge_game_team b USING(game_id),LATERAL range(1,len(r.home_periods)+1) p(n);
CREATE VIEW audit.period_availability_v26 AS
SELECT v.* REPLACE(CASE WHEN r.game_id IS NOT NULL THEN 'published' ELSE v.status END AS status,
    CASE WHEN r.game_id IS NOT NULL THEN '{SOURCE}' ELSE v.selected_source END AS selected_source,
    CASE WHEN r.game_id IS NOT NULL THEN '{SOURCE}' ELSE v.recovery_disposition END AS recovery_disposition,
    coalesce(len(r.home_periods)::SMALLINT,v.last_period) AS last_period,
    coalesce(len(r.home_periods)>4,v.derived_overtime) AS derived_overtime,
    CASE WHEN r.game_id IS NOT NULL THEN 'agrees' ELSE v.overtime_status END AS overtime_status)
FROM audit.period_availability_v25 v LEFT JOIN {RECEIPT} r USING(game_id);
'''


def sources(cache: Path) -> list[tuple[str,str,str,bytes]]:
    captures=(cache/'captures.json').read_bytes()
    cdx=(cache.parent/'wayback-oct2006-v25/oct2006_box_cdx.json').read_bytes()
    if (hash_bytes(captures).hexdigest(),hash_bytes(cdx).hexdigest())!=(MANIFEST,CDX):
        raise RuntimeError('Exact scan and CDX manifests required')
    index={(u,t) for u,t,status in json.loads(cdx)[1:] if status=='200'}
    ledger=json.loads(captures)
    if sorted(r['game_id'] for r in ledger)!=sorted([*FIXTURES,'0010600026']):
        raise RuntimeError('Unexpected complete October 2006 candidate scope')
    out=[]
    for r in ledger:
        gid=r['game_id']
        if gid=='0010600026':
            if (r['status'],r['target_status'],r['observed_game_ids'])!=('held_missing_or_wrong_game',200,[]):
                raise RuntimeError('The recorded Efes–Golden State hold changed')
            continue
        f=FIXTURES[gid]; day=date(2006,10,f.day)
        path=f'/games/{day:%Y%m%d}/{f.slug}/boxscore.html'
        match=re.fullmatch(r'https://web\.archive\.org/web/(\d{14})id_/(http://www\.nba\.com:80'+re.escape(path)+r'\??)',r['final_url'])
        if (match is None or (match[2],match[1]) not in index or r['requested_url']!=r['final_url'] or
            r['indexed_url']!=r['final_url'] or r['target_status']!=200 or r['status']!='captured'
            or r['game_date']!=day.isoformat() or r['slug']!=f.slug or r['file']!=f'{gid}.html'):
            raise RuntimeError(f'Unverified archive identity: {gid}')
        p=cache/r['file']; raw=p.read_bytes()
        if p.is_symlink() or len(raw)!=r['bytes'] or hash_bytes(raw).hexdigest()!=r['sha256']:
            raise RuntimeError(f'Pinned original page bytes changed: {gid}')
        out.append((gid,r['final_url'],r['sha256'],raw))
    return out


def derive(c: duckdb.DuckDBPyConnection,parent: Path,files: list[tuple[str,str,str,bytes]]) -> None:
    c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    c.execute(f'CREATE TEMP TABLE expected_v26 AS SELECT * FROM {RECEIPT} LIMIT 0')
    for gid,url,digest,raw in files:
        f=FIXTURES[gid]; day=date(2006,10,f.day); home,away,h,a=read_lines(gid,raw,f)
        stored=c.execute('''SELECT g.game_date,g.season_type,g.home_team_id,g.away_team_id,g.home_score,g.away_score,
            g.is_overtime,p.status,p.selected_source,p.candidate_sources,p.valid_sources,
            EXISTS(SELECT 1 FROM prior.nba.fact_game_period_score s WHERE s.game_id=g.game_id)
            FROM prior.nba.fact_game g JOIN prior.audit.period_coverage p USING(game_id) WHERE g.game_id=?''',[gid]).fetchone()
        if stored!=(day,'Preseason',f.home,f.away,h,a,f.periods==5,'no_supported_source',None,0,0,False):
            raise RuntimeError(f'Archive page disagrees with exact missing-period fixture: {gid} {stored}')
        c.execute('INSERT INTO expected_v26 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [gid,day,f.home,f.away,f.home_label,f.away_label,h,a,home,away,url,digest,False])


def replay(c: duckdb.DuckDBPyConnection,parent: Path,files: list[tuple[str,str,str,bytes]]) -> int:
    derive(c,parent,files)
    if difference(c,f'SELECT * FROM {RECEIPT}','SELECT * FROM expected_v26'):
        raise RuntimeError('Exact second preseason archive replay differs')
    if c.execute('SELECT * FROM audit.preseason_period_input_v26 ORDER BY role').fetchall()!=PINS:
        raise RuntimeError('Second preseason manifest differs')
    for view,frozen in FROZEN_V26.items():
        if difference(c,f'SELECT * FROM {frozen}',f'SELECT * FROM prior.audit.{view} SEMI JOIN expected_v26 USING(game_id)'):
            raise RuntimeError(f'Inherited review snapshot differs: {view}')
    skip=' ANTI JOIN expected_v26 USING(game_id)'
    for rel in ('nba.fact_game_period_score','audit.period_coverage','audit.period_vector_baseline','nba.game_overtime'):
        if difference(c,f'SELECT * FROM {rel}{skip}',f'SELECT * FROM prior.{rel}{skip}'):
            raise RuntimeError(f'Outside-scope period difference: {rel}')
    if difference(c,f'SELECT * FROM audit.period_availability_v26{skip}',f'SELECT * FROM prior.audit.period_availability_v25{skip}'):
        raise RuntimeError('Out-of-scope availability difference')
    checks=verify(c)
    for rel in UNCHANGED:
        verify_inherited(c,rel)
    return checks


def stage(c: duckdb.DuckDBPyConnection,parent: Path,files: list[tuple[str,str,str,bytes]]) -> None:
    c.begin()
    c.execute(DDL)
    derive(c,parent,files)
    c.execute(f'INSERT INTO {RECEIPT} SELECT * FROM expected_v26')
    for view,new in FROZEN_V26.items():
        name=f'audit.{view}'; prefix=f'CREATE VIEW {name} AS '
        s=c.execute("SELECT sql FROM duckdb_views() WHERE database_name=current_database() AND schema_name='audit' AND view_name=?",[view]).fetchone()
        if s is None or not str(s[0]).startswith(prefix):
            raise RuntimeError(f'Unexpected v25 availability view: {view}')
        c.execute(f'CREATE TABLE {new} AS SELECT * FROM prior.{name} SEMI JOIN expected_v26 USING(game_id)')
        body=str(s[0]).removeprefix(prefix).removesuffix(';')
        c.execute(f'CREATE OR REPLACE VIEW {name} AS SELECT * FROM ({body}) ANTI JOIN {new} USING(game_id) UNION ALL SELECT * FROM {new}')
    c.execute(f'''INSERT INTO nba.fact_game_period_score SELECT * FROM {CELLS};
      INSERT INTO audit.period_vector_baseline
      SELECT game_id,count(*),sha256(to_json(list(struct_pack(t:=team_id,p:=period_number,s:=points)
          ORDER BY team_id,period_number))) FROM {CELLS} GROUP BY game_id;
      UPDATE audit.period_coverage SET status='published',candidate_sources=1,valid_sources=1,selected_source=?
        WHERE game_id IN (SELECT game_id FROM {RECEIPT});''',[SOURCE])
    c.executemany('INSERT INTO audit.preseason_period_input_v26 VALUES (?,?)',PINS)
    c.commit()
    c.execute('CHECKPOINT')


def run(action: str,parent: Path,cache: Path,output: Path) -> None:
    wal=f'{output}.wal'
    if action=='build' and (os.path.lexists(output) or os.path.lexists(wal)):
        raise FileExistsError(f'Will not overwrite {output} or its WAL')
    parent_before=snapshot(parent)
    if sha256(parent)!=PARENT:
        raise RuntimeError('Expected exact v25 checkpoint')
    files=sources(cache)
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-v26-spill-') as spill:
        if action=='verify-inputs':
            target_before=snapshot(output)
            with duckdb.connect(str(output),read_only=True,config=open_config(spill)) as c:
                checks=replay(c,parent,files)
            if snapshot(output)!=target_before or sources(cache)!=files:
                raise RuntimeError('Release or receipts changed during verification')
        else:
            with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v26-') as w:
                staged=Path(w)/'release.duckdb';shutil.copyfile(parent,staged)
                with duckdb.connect(str(staged),config=open_config(spill)) as c:
                    stage(c,parent,files)
                snapshot(staged)
                with duckdb.connect(str(staged),read_only=True,config=open_config(spill)) as c:
                    checks=replay(c,parent,files)
                if sources(cache)!=files or snapshot(parent)!=parent_before or os.path.lexists(wal):
                    raise RuntimeError('Input changed or output WAL exists')
                os.link(staged,output)
    if snapshot(parent)!=parent_before:
        raise RuntimeError('Parent changed during verification')
    print(f'{checks} checks passed; 11 exact archived boxes; {len(UNCHANGED)} unchanged relations: {output}')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('build','verify-inputs'))
    p.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v25.duckdb')
    p.add_argument('--cache',type=Path,default=CACHE)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    run(a.action,a.parent.resolve(strict=True),a.cache.resolve(strict=True),a.output.absolute())
