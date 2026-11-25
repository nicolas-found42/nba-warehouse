#!/usr/bin/env python3
"""Publish only evidence-backed fields from the January 21, 1954 All-Star box."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from hashlib import sha256 as hash_bytes
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

GAME = '0035300004'
TEAMS = {'East':1610616833,'West':1610616834}
PARENT = '2c06f033637a79d6ec246d7425011f2a6bd1dc484e9aabb3f698e685dab2ffb3'
CACHE = Path.home()/'Library/Application Support/NBA Warehouse/evidence/allstar-1954-v24'
NBA = 'https://web.archive.org/web/20091202232006/http://www.nba.com/history/allstar/boxscore_1954.html'
COMPILER = 'https://www.landofbasketball.com/allstargames/1954_nba_all_star_game.htm'
PAPER = 'https://archive.org/details/per_st-louis-post-dispatch_1954-01-22_76_21/page/n25/mode/1up'
SCAN = 'https://archive.org/download/per_st-louis-post-dispatch_1954-01-22_76_21/page/n25_w4000.jpg'
SOURCES = {
 'nba-1954-wayback.md':('d614ed7d12aa757c5e0de1c1666d8ad7a3e4310965c87d1aadc1654f6f1eba02',NBA,'archived_official_text_extract'),
 'land-of-basketball-1954.md':('08f3c4e3ffe076e83f1006ebda0c1fa9f5ab5609394f0b60aef8713dcc97a18e',COMPILER,'secondary_text_extract'),
 'slpd-1954-01-22-n25.jpg':('b13ce8fe24dcfef0c04a130781820890542bee4b968e8e12246e8006765ba9bd',SCAN,'original_scan'),
 'east-1954-hi.png':('3471ff875db1e45a65d3bbea7af024d33d033df4735dadf8e191fbe58d697c9f',PAPER,'review_crop'),
 'west-1954.png':('2eea699acb0eb7a4d33efebb513278fe8bab2f140365797d4fba7de25fe19b5c',PAPER,'review_crop'),
 'as1954-paper-person-cells.csv':('9a19c79027d6ac767a29289f08b86da65510991f45ca7061218f3e547e983661',PAPER,'human_transcription_and_reviewed_person_map'),
}
EXPECTED_PERIODS = {'East':[28,20,17,19,14],'West':[25,19,23,17,9]}
EXPECTED_TOTALS = {'East':(265,31,76,36,44,71,24,98),'West':(265,38,107,17,26,48,24,93)}
UNCHANGED = tuple(rel for rel in V23_UNCHANGED if rel!='nba.fact_player_game_stats') + (
    'nba.fact_game_period_score','audit.period_coverage','audit.period_availability_v23',
    'audit.player_box_availability_v17','audit.player_box_recovery_v17','audit.player_box_disposition',
)
RECEIPT = 'audit.allstar_player_receipt_v24'
PUBLICATION = 'audit.allstar_player_publication_v24'
INPUT = 'audit.allstar_player_input_v24'
PINS = sorted([('parent_v23',PARENT,None,'checkpointed_parent',False),
               *((name,digest,url,kind,False) for name,(digest,url,kind) in SOURCES.items())])
DDL = f'''
CREATE TABLE {RECEIPT} (
 game_id VARCHAR NOT NULL,person_id BIGINT NOT NULL,team_id BIGINT NOT NULL,
 nba_name VARCHAR NOT NULL,nba_club VARCHAR NOT NULL,canonical_name VARCHAR NOT NULL,paper_label VARCHAR NOT NULL,
 whole_minutes INTEGER NOT NULL,fgm INTEGER NOT NULL,fga INTEGER NOT NULL,ftm INTEGER NOT NULL,fta INTEGER NOT NULL,
 reb INTEGER NOT NULL,ast INTEGER NOT NULL,pts INTEGER NOT NULL,
 paper_fgm INTEGER NOT NULL,paper_ftm INTEGER NOT NULL,paper_pf INTEGER NOT NULL,compiler_pf INTEGER NOT NULL,
 foul_status VARCHAR NOT NULL CHECK(foul_status IN ('sources_agree','held_disputed')),
 PRIMARY KEY(game_id,person_id), CHECK(whole_minutes>0), CHECK(pts=2*fgm+ftm), CHECK(fgm=paper_fgm AND ftm=paper_ftm));
CREATE TABLE {PUBLICATION} AS SELECT * FROM nba.fact_player_game_stats LIMIT 0;
CREATE TABLE {INPUT} (name VARCHAR PRIMARY KEY,sha256 VARCHAR NOT NULL,source_url VARCHAR,
 evidence_kind VARCHAR NOT NULL,redistribution_cleared BOOLEAN NOT NULL CHECK(NOT redistribution_cleared));
CREATE VIEW audit.player_box_availability_v24 AS
SELECT old.* REPLACE(coalesce(n.rows,old.candidate_rows) AS candidate_rows,
       coalesce(n.rows,old.published_rows) AS published_rows,
       CASE WHEN n.rows IS NULL THEN old.status ELSE 'played_box_recovered_with_field_hold' END AS status),
       coalesce(n.held,0) AS held_field_cells
FROM audit.player_box_availability_v17 old LEFT JOIN (
 SELECT game_id,count(*) AS rows,count(*) FILTER(WHERE foul_status='held_disputed') AS held
 FROM {RECEIPT} GROUP BY game_id) n USING(game_id);
'''


class Box(NamedTuple):
    side: str
    club: str
    values: tuple[int,...]


def load(cache: Path) -> dict[str,bytes]:
    out = {}
    for name,(digest,_,_) in SOURCES.items():
        p = cache/name
        raw = p.read_bytes()
        if p.is_symlink() or hash_bytes(raw).hexdigest()!=digest:
            raise RuntimeError(f'Pinned 1954 evidence changed: {name}')
        out[name] = raw
    return out


def nba_boxes(text: str) -> dict[str,Box]:
    if (NBA not in text.replace(r'\_','_') or 'GAME 4: at New York, Jan. 21, 1954' not in text
        or 'West | 25 | 19 | 23 | 17 | 9 | 93' not in text or 'East | 28 | 20 | 17 | 19 | 14 | 98' not in text):
        raise RuntimeError('Unexpected archived NBA fixture')
    yield_rows = re.compile(r'\|\s*([A-Za-z][^|,\n]+), ([^|\n]+?)\s*\|' + r'\s*(\d+)\s*\|'*8)
    out: dict[str,Box] = {}
    for side,tail in (('West','**EAST ALL-STARS (98)**'),('East','**Score by Periods:**')):
        totals = EXPECTED_TOTALS[side]
        _,block = text.split(f'**{side.upper()} ALL-STARS ({totals[-1]})**',1)
        block = block.split(tail,1)[0]
        header = '| **Player, Team** | **Min.** | **FGM** | **FGA** | **FTM** | **FTA** | **Reb.** | **Ast.** | **Pts.** |'
        if header not in block or '| **Totals** | '+' | '.join(map(str,totals))+' |' not in block:
            raise RuntimeError(f'NBA box columns or printed totals changed: {side}')
        rows = yield_rows.findall(block)
        if len(rows)!=10 or tuple(sum(map(int,col)) for col in zip(*(r[2:] for r in rows),strict=True))!=totals:
            raise RuntimeError(f'NBA rows do not reconcile to the {side} printed totals')
        for name,club,*vals in rows:
            if name in out:
                raise RuntimeError(f'Duplicate printed NBA player: {name}')
            out[name] = Box(side,club,tuple(map(int,vals)))
    return out


def compiler_fouls(text: str, player_boxes: dict[str,Box]) -> dict[str,int]:
    if 'January 21, 1954' not in text or '93 | - | | 98  (OT)' not in text:
        raise RuntimeError('Unexpected compiler fixture')
    out = {}
    for side,end in (('West','### East Team'),('East','**Notes West Team**')):
        section = text.split(f'### {side} Team',1)[1].split(end,1)[0]
        raw = [tuple(c.strip().strip('*') for c in row.split('|')[1:-1]) for row in section.splitlines() if row.startswith('|')]
        for row in raw:
            if len(row)!=15 or not re.fullmatch(r'\d{2}:00',row[2]):
                continue
            name = 'Bobby Davies' if row[0]=='Bob Davies' else row[0]
            box = player_boxes[name]
            values = (int(row[2][:2]),*map(int,row[3].split('-')),*map(int,row[5].split('-')),
                      int(row[8]),int(row[9]),int(row[14]))
            if name in out or (side,values)!=(box.side,box.values) or not row[13].isdigit():
                raise RuntimeError(f'Compiler box match fails: {name}')
            out[name] = int(row[13])
    if out.keys()!=player_boxes.keys():
        raise RuntimeError('Compiler foul rows do not match the identified NBA players')
    return out


def derive(c: duckdb.DuckDBPyConnection, parent: Path, raw: dict[str,bytes]) -> None:
    c.execute(f'ATTACH {sql_string(str(parent))} AS prior (READ_ONLY)')
    fixture = c.execute('''SELECT game_date::VARCHAR,season_type,result_status,home_team_id,away_team_id,
         home_score,away_score,winner_team_id,is_overtime,
         EXISTS(SELECT 1 FROM prior.nba.fact_player_game_stats p WHERE p.game_id=g.game_id)
         FROM prior.nba.fact_game g WHERE game_id=?''',[GAME]).fetchone()
    if fixture!=('1954-01-21','AllStar','final',TEAMS['East'],TEAMS['West'],98,93,TEAMS['East'],True,False):
        raise RuntimeError(f'Parent fixture or existing player box differs: {fixture}')
    boxes = nba_boxes(raw['nba-1954-wayback.md'].decode())
    fouls = compiler_fouls(raw['land-of-basketball-1954.md'].decode(),boxes)
    c.execute(f'CREATE TEMP TABLE expected_as1954_receipts AS SELECT * FROM {RECEIPT} LIMIT 0')
    names: set[str] = set()
    ids: set[int] = set()
    paper_pf: Counter[str] = Counter()
    for r in csv.DictReader(raw['as1954-paper-person-cells.csv'].decode().splitlines()):
        name,side,pid = r['nba_name'],r['side'],int(r['person_id'])
        box = boxes[name]
        paper_fgm,paper_ftm,pf = (int(r[col]) for col in ('paper_fgm','paper_ftm','paper_pf'))
        minutes,fgm,fga,ftm,fta,reb,ast,pts = box.values
        identity = c.execute('''SELECT person_id FROM prior.nba.dim_person WHERE display_name=?
              AND from_year<=1954 AND (to_year IS NULL OR to_year+1>=1954)''',[r['canonical_name']]).fetchall()
        if (identity!=[(pid,)] or name in names or pid in ids or side!=box.side
            or (paper_fgm,paper_ftm)!=(fgm,ftm) or pts!=2*fgm+ftm or not 0<minutes<=53
            or not (0<=fgm<=fga and 0<=ftm<=fta and 0<=pf<=6 and 0<=fouls[name]<=6)
            or (pf!=fouls[name])!=(pid==76704)):
            raise RuntimeError(f'Paper/NBA/canonical player reconciliation failed: {name}')
        names.add(name); ids.add(pid); paper_pf[side]+=pf
        c.execute('INSERT INTO expected_as1954_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [GAME,pid,TEAMS[side],name,box.club,r['canonical_name'],r['paper_label'],*box.values,
             paper_fgm,paper_ftm,pf,fouls[name],'sources_agree' if pf==fouls[name] else 'held_disputed'])
    if names!=boxes.keys() or paper_pf!={'East':20,'West':34}:
        raise RuntimeError('Signed-off newspaper map omits a player or its foul totals do not reconcile')
    for side,team in TEAMS.items():
        totals = c.execute('''SELECT min,fgm,fga,ftm,fta,reb,ast,pts FROM prior.nba.fact_team_game_stats
             WHERE game_id=? AND team_id=?''',[GAME,team]).fetchone()
        periods = c.execute('''SELECT period_number,points FROM prior.nba.fact_game_period_score
             WHERE game_id=? AND team_id=? ORDER BY period_number''',[GAME,team]).fetchall()
        if totals!=EXPECTED_TOTALS[side] or periods!=list(enumerate(EXPECTED_PERIODS[side],1)):
            raise RuntimeError(f'1954 observations disagree with retained team/period facts: {side}')
    c.execute(f'CREATE TEMP TABLE expected_as1954_players AS SELECT * FROM {PUBLICATION} LIMIT 0')
    c.execute('''INSERT INTO expected_as1954_players BY NAME
       SELECT a.game_id,a.person_id,a.team_id,
         CASE b.side WHEN 'home' THEN g.away_team_id ELSE g.home_team_id END AS opponent_team_id,
         b.side='home' AS is_home,g.winner_team_id=a.team_id AS is_win,'played' AS participation_status,
         a.whole_minutes::DOUBLE AS reported_num_minutes,a.pts AS points,a.ast AS assists,a.fga,a.fgm,a.fta,a.ftm,a.reb,
         CASE a.foul_status WHEN 'sources_agree' THEN a.paper_pf END AS fouls_personal
       FROM expected_as1954_receipts a JOIN prior.nba.fact_game g USING(game_id)
       JOIN prior.nba.bridge_game_team b USING(game_id,team_id)''')


def replay(c: duckdb.DuckDBPyConnection, parent: Path, raw: dict[str,bytes]) -> int:
    derive(c,parent,raw)
    for rel,temp in ((RECEIPT,'expected_as1954_receipts'),(PUBLICATION,'expected_as1954_players')):
        if difference(c,f'SELECT * FROM {rel}',f'SELECT * FROM {temp}'):
            raise RuntimeError(f'Exact archival player-box replay differs: {rel}')
    if c.execute(f'SELECT * FROM {INPUT} ORDER BY name').fetchall()!=PINS:
        raise RuntimeError('Player box input pins differ')
    checks = verify(c)
    for rel in UNCHANGED:
        verify_inherited(c,rel)
    pred = f'WHERE game_id<>{sql_string(GAME)}'
    if difference(c,f'SELECT * FROM nba.fact_player_game_stats {pred}',f'SELECT * FROM prior.nba.fact_player_game_stats {pred}'):
        raise RuntimeError('Players outside the 1954 All-Star fixture changed')
    return checks


def run(action: str, parent: Path, cache: Path, output: Path) -> None:
    wal = f'{output}.wal'
    if action=='build' and (os.path.lexists(output) or os.path.lexists(wal)):
        raise FileExistsError(f'Will not overwrite {output} or its WAL')
    initial_parent = snapshot(parent)
    if sha256(parent)!=PARENT:
        raise RuntimeError('Expected exact checkpointed v23 parent')
    raw = load(cache)
    output_before = snapshot(output) if action=='verify-inputs' else None
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-v24-spill-') as spill:
        if action=='verify-inputs':
            with duckdb.connect(str(output),read_only=True,config=open_config(spill)) as c:
                checks = replay(c,parent,raw)
        else:
            with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-v24-') as work:
                candidate = Path(work)/'release.duckdb'
                shutil.copyfile(parent,candidate)
                with duckdb.connect(str(candidate),config=open_config(spill)) as c:
                    c.execute(DDL)
                    derive(c,parent,raw)
                    c.begin()
                    c.execute(f'INSERT INTO {RECEIPT} SELECT * FROM expected_as1954_receipts;'
                              f'INSERT INTO {PUBLICATION} SELECT * FROM expected_as1954_players;'
                              f'INSERT INTO nba.fact_player_game_stats SELECT * FROM {PUBLICATION};')
                    c.executemany(f'INSERT INTO {INPUT} VALUES (?,?,?,?,?)',PINS)
                    c.commit()
                    c.execute('CHECKPOINT')
                snapshot(candidate)
                with duckdb.connect(str(candidate),read_only=True,config=open_config(spill)) as c:
                    checks = replay(c,parent,raw)
                load(cache)
                if snapshot(parent)!=initial_parent or os.path.lexists(wal):
                    raise RuntimeError('Input changed or destination WAL exists; release not installed')
                os.link(candidate,output)
    if action=='verify-inputs':
        load(cache)
        if snapshot(output)!=output_before:
            raise RuntimeError('Release changed during replay')
    if snapshot(parent)!=initial_parent:
        raise RuntimeError('Parent changed during replay')
    print(f'{checks} quality checks passed; 20 reviewed players, six exact evidence pins and {len(UNCHANGED)} inherited relations replayed: {output}')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('build','verify-inputs'))
    p.add_argument('--parent',type=Path,default=ROOT/'data/nba-clean-v23.duckdb')
    p.add_argument('--cache',type=Path,default=CACHE)
    p.add_argument('--output',type=Path,required=True)
    a = p.parse_args()
    run(a.action,a.parent.resolve(strict=True),a.cache.resolve(strict=True),a.output.absolute())


if __name__=='__main__':
    main()
