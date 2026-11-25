"""Build and replay a local, cutoff-scoped season release from eight pinned Parquet assets."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

import duckdb

from nba_warehouse import ROOT, WAREHOUSE, sql_string
from recover_archive_periods import sha256 as file_hash
from recover_local_periods import difference, snapshot

ASSETS = {
    'schedule': ('espn_nba_schedules','nba_schedule'),
    'game_map': ('nba_crosswalk','nba_schedule_crosswalk'),
    'team_map': ('nba_crosswalk','nba_team_crosswalk'),
    'player_map': ('nba_crosswalk','nba_player_crosswalk'),
    'team': ('espn_nba_team_boxscores','team_box'),
    'player': ('espn_nba_player_boxscores','player_box'),
    'play': ('espn_nba_pbp','play_by_play'),
    'shot': ('espn_nba_shots','shots'),
}
DOWNLOAD = 'https://github.com/sportsdataverse/sportsdataverse-data/releases/download'
ET = ZoneInfo('America/New_York')
CORE_BOX = {
    'fgm':'field_goals_made','fga':'field_goals_attempted',
    'fg3m':'three_point_field_goals_made','fg3a':'three_point_field_goals_attempted',
    'ftm':'free_throws_made','fta':'free_throws_attempted','oreb':'offensive_rebounds','dreb':'defensive_rebounds',
    'fg_pct':'field_goals_made/nullif(field_goals_attempted,0)',
    'fg3_pct':'three_point_field_goals_made/nullif(three_point_field_goals_attempted,0)',
    'ft_pct':'free_throws_made/nullif(free_throws_attempted,0)',
}
TEAM_BOX = {**CORE_BOX,'game_id':'gid','team_id':'nba_team','is_home':"side='home'",'is_win':'team_winner',
    'min':'5*(48+5*(periods-4))','pts':'team_score','reb':'total_rebounds','ast':'assists','tov':'total_turnovers',
    'stl':'steals','blk':'blocks','pf':'fouls','plus_minus':'score-opponent_score',
    'pts_off_tov':'try_cast(turnover_points AS INT)','pts_fb':'try_cast(fast_break_points AS INT)',
    'pts_paint':'try_cast(points_in_paint AS INT)','biggest_lead':'try_cast(largest_lead AS INT)',
    'lead_changes':'try_cast(lead_changes AS INT)'}
PLAYER_BOX = {**CORE_BOX,'game_id':'gid','person_id':'person','team_id':'nba_team','opponent_team_id':'nba_opponent',
    'is_home':"side='home'",'is_win':'team_winner','participation_status':"CASE WHEN did_not_play THEN 'dnp' ELSE 'played' END",
    'comment':'CASE WHEN did_not_play THEN reason END','reported_num_minutes':'minutes','points':'points',
    'assists':'assists','blocks':'blocks','steals':'steals','turnovers':'turnovers','reb':'rebounds',
    'fouls_personal':'fouls','plus_minus':'try_cast(plus_minus AS INT)'}
ITEM = r"\{'value': (\d+)(?:\.0)?, 'displayValue': '(\d+)', 'period': (\d+)(?:\.0)?\}"


@dataclass(frozen=True)
class Input:
    role: str
    path: Path
    url: str
    digest: str
    asset_id: int
    updated_at: str


def input_assets(root: Path, season_end: int) -> list[Input]:
    items = []
    for role,(tag,stem) in ASSETS.items():
        path = root/f'{stem}_{season_end}.parquet'
        row = json.loads(path.with_suffix('.parquet.asset.json').read_text())
        url = f'{DOWNLOAD}/{tag}/{path.name}'
        digest = file_hash(path)
        if (row['name'],row['url'],row['sha256']) != (path.name,url,digest) or type(row['asset_id']) is not int:
            raise ValueError(f'Source asset bytes or locator differ: {role}')
        items.append(Input(role,path,url,digest,row['asset_id'],row['published_asset_updated_at']))
    return items


def parse_periods(text: str, score: int, periods: int) -> list[int]:
    if not re.fullmatch(r'\['+ITEM+r'(?:\s+'+ITEM+r')*\]', text):
        raise ValueError('Invalid source quarter-vector serialization')
    records = [tuple(map(int,m)) for m in re.findall(ITEM,text)]
    points = [n for n,_,_ in records]
    if ([(n,n,i) for i,n in enumerate(points,1)]!=records or len(points)!=periods or
            periods not in range(4,15) or sum(points)!=score):
        raise ValueError('Quarter vectors do not match ordered cells and final')
    return points


def check(c: duckdb.DuckDBPyConnection, name: str, sql: str) -> None:
    rows = c.execute(sql).fetchmany(4)
    if rows:
        raise ValueError(f'{name}: {rows}')


def key(c: duckdb.DuckDBPyConnection, table: str, fields: str) -> None:
    check(c,table+' duplicate key',f'SELECT {fields}, count(*) FROM {table} GROUP BY {fields} HAVING count(*)<>1')
    check(c,table+' null key',f'SELECT {fields} FROM {table} WHERE '+ ' OR '.join(f'{n.strip()} IS NULL' for n in fields.split(',')))


def label_periods(c: duckdb.DuckDBPyConnection) -> None:
    rows = c.execute('''SELECT s.id,x.nba_game_id,x.nba_home_team_id,x.nba_away_team_id,
        s.home_score,s.away_score,s.home_linescores,s.away_linescores,s.status_period
        FROM source_schedule s LEFT JOIN source_game_map x ON s.id::VARCHAR=x.espn_game_id
        WHERE s.status_type_completed AND s.game_date <= (SELECT cutoff FROM config)''').fetchall()
    cells = []
    for espn,gid,hid,aid,hs,aws,htext,atext,n in rows:
        try:
            if gid is None or hid is None or aid is None or n is None or int(n)!=n:
                raise ValueError('No exact canonical fixture or period count')
            h,a = parse_periods(htext,hs,int(n)),parse_periods(atext,aws,int(n))
            if any(sum(h[:i])!=sum(a[:i]) for i in range(4,len(h))):
                raise ValueError('Overtime started without a tie')
        except (TypeError,ValueError) as exc:
            raise ValueError(f'Fixture {espn}/{gid}: {exc}') from exc
        cells += [(gid,team,i,pts) for team,vector in ((hid,h),(aid,a)) for i,pts in enumerate(vector,1)]
    c.execute('CREATE TEMP TABLE period_cells(game_id VARCHAR,team_id BIGINT,period_number SMALLINT,points INT,PRIMARY KEY(game_id,team_id,period_number))')
    if not cells:
        raise ValueError('No completed fixtures in cutoff')
    c.executemany('INSERT INTO period_cells VALUES (?,?,?,?)',cells)


def project_box(c: duckdb.DuckDBPyConnection, table: str, mapping: dict[str,str], query: str) -> None:
    fields = c.execute(f'DESCRIBE history.nba.{table}').fetchall()
    if not set(mapping) <= {r[0] for r in fields}:
        raise ValueError(f'Projection differs from historical contract: {table}')
    select = ','.join(f'({mapping.get(name,"NULL")})::{kind} AS {name}' for name,kind,*_ in fields)
    c.execute(f'CREATE TABLE nba.{table} AS SELECT {select} FROM {query}')


def validate_stage(c: duckdb.DuckDBPyConnection) -> None:
    for table, fields in (
        ('candidate','gid'),('candidate','id'),('sides','gid,team_id'),('map_person','athlete'),('map_person','person'),
        ('team_obs','gid,nba_team'),('player_obs','gid,athlete_id'),('play_obs','gid,id'),
        ('play_obs','gid,sequence_number'),('play_obs','gid,game_play_number')):
        key(c,table,fields)
    queries = {
      'scope': '''SELECT id FROM source_schedule WHERE season IS DISTINCT FROM (SELECT season_end FROM config)
          OR game_date IS NULL OR cast(cast(date AS TIMESTAMPTZ) AT TIME ZONE 'America/New_York' AS DATE) IS DISTINCT FROM game_date
          OR (game_date <= (SELECT cutoff FROM config) AND status_type_completed IS NOT TRUE)''',
      'fixture': '''SELECT gid,id FROM candidate WHERE NOT regexp_full_match(gid,'[0-9]{10}') OR phase IS NULL
          OR substr(gid,4,2) IS DISTINCT FROM printf('%02d',(season-1)%100) OR xseason IS DISTINCT FROM season
          OR xdate IS DISTINCT FROM game_date OR (xhome,xaway,xhid,xaid) IS DISTINCT FROM (home_id,away_id,hid,aid)
          OR game_match IS DISTINCT FROM 'both' OR neutral_site IS NULL OR cup_group IS NULL OR hid=aid
          OR (phase='Regular' AND season_type IS DISTINCT FROM 2) OR (cup_group AND phase<>'Regular')
          OR (phase='Playoffs' AND season_type IS DISTINCT FROM 3) OR status_type_name IS DISTINCT FROM 'STATUS_FINAL'
          OR least(home_score,away_score)<=0 OR home_score=away_score OR home_score IS NULL OR away_score IS NULL
          OR home_winner IS DISTINCT FROM (home_score>away_score) OR away_winner IS DISTINCT FROM (away_score>home_score)
          OR NOT EXISTS(SELECT 1 FROM history.nba.dim_team WHERE team_id=hid)
          OR NOT EXISTS(SELECT 1 FROM history.nba.dim_team WHERE team_id=aid)''',
      'historical collision': 'SELECT g.gid FROM candidate g JOIN history.nba.dim_game h ON g.gid=h.game_id',
      'missing team row': 'SELECT s.gid,s.team_id FROM sides s ANTI JOIN team_obs b ON (s.gid,s.team_id)=(b.gid,b.nba_team)',
      'team box': '''SELECT gid,nba_team FROM team_obs WHERE (game_date,season_type,team_home_away,team_score,opponent_team_score,opponent_team_id,team_winner)
          IS DISTINCT FROM (canonical_date,canonical_phase,side,score,opponent_score,provider_opponent,score>opponent_score)
          OR team_score IS DISTINCT FROM 2*field_goals_made+three_point_field_goals_made+free_throws_made
          OR total_turnovers IS DISTINCT FROM turnovers+team_turnovers OR total_rebounds IS DISTINCT FROM offensive_rebounds+defensive_rebounds
          OR field_goals_made>field_goals_attempted OR three_point_field_goals_made>three_point_field_goals_attempted
          OR three_point_field_goals_made>field_goals_made OR three_point_field_goals_attempted>field_goals_attempted
          OR free_throws_made>free_throws_attempted OR least(field_goals_made,free_throws_made,three_point_field_goals_made,turnovers,team_turnovers)<0''',
      'player side': '''SELECT gid,athlete_id FROM player_obs WHERE nba_team IS NULL OR did_not_play IS NULL OR
          (game_date,season_type,home_away,team_score,opponent_team_score,team_winner)
          IS DISTINCT FROM (canonical_date,canonical_phase,side,score,opponent_score,score>opponent_score)
          OR (person IS NOT NULL AND athlete_display_name IS DISTINCT FROM espn_name)
          OR (did_not_play AND (points IS NOT NULL OR minutes>0 OR reason IS NULL))
          OR (NOT did_not_play AND (points IS NULL OR points IS DISTINCT FROM 2*field_goals_made+three_point_field_goals_made+free_throws_made
              OR (minutes IS NULL AND (points<>0 OR starter IS NOT FALSE OR reason IS NULL
                  OR field_goals_attempted<>0 OR free_throws_attempted<>0 OR rebounds<>0 OR assists<>0
                  OR steals<>0 OR blocks<>0 OR turnovers<>0 OR fouls<>0))
              OR (minutes IS NOT NULL AND (NOT isfinite(minutes) OR minutes<0 OR minutes<>trunc(minutes)))
              OR field_goals_made>field_goals_attempted OR free_throws_made>free_throws_attempted
              OR three_point_field_goals_made>three_point_field_goals_attempted))''',
      'played team totals': '''WITH totals AS (SELECT gid,nba_team,count(*) AS played,sum(points) AS pts,sum(minutes) AS mins,
           sum(field_goals_made) AS fgm,sum(three_point_field_goals_made) AS fg3m,sum(free_throws_made) AS ftm
           FROM player_obs WHERE NOT did_not_play GROUP BY 1,2)
           SELECT t.gid,t.nba_team,played,mins FROM team_obs t LEFT JOIN totals p USING(gid,nba_team)
           WHERE (p.pts,p.fgm,p.fg3m,p.ftm) IS DISTINCT FROM (t.team_score,t.field_goals_made,t.three_point_field_goals_made,t.free_throws_made)
           OR abs(p.mins-5*(48+5*(t.periods-4)))>p.played*0.5+0.5''',
      'play keys and clock': '''SELECT gid,id FROM play_obs WHERE
          (game_date,season_type,home_team_id,away_team_id) IS DISTINCT FROM (canonical_date,canonical_phase,expected_home,expected_away)
          OR iso_clock IS NULL OR NOT regexp_full_match(iso_clock,'PT[0-9]+M[0-5][0-9]([.][0-9]+)?S')
          OR seconds_left IS NULL OR NOT isfinite(seconds_left) OR seconds_left NOT BETWEEN 0 AND CASE WHEN period_number<=4 THEN 720 ELSE 300 END
          OR period_number<=0 OR sequence_number<0 OR type_text IS NULL OR shooting_play IS NULL
          OR (team_id IS NOT NULL AND nba_team IS NULL) OR (is_goal AND (scoring_play IS NULL OR nba_team IS NULL OR athlete_id_1 IS NULL))''',
      'raw play continuity': '''SELECT gid FROM play_obs GROUP BY 1 HAVING min(game_play_number)<>1 OR max(game_play_number)<>count(*)''',
      'old finals preservation': '''SELECT o.game_id,g.gid FROM prior.audit.result_for_local_review o
          LEFT JOIN candidate g ON o.espn_game_id=g.id::VARCHAR
          WHERE o.game_date<=(SELECT cutoff FROM config) AND
          (g.gid,g.game_date,g.hid,g.aid,g.home_score,g.away_score,g.neutral_site,g.cup_group)
          IS DISTINCT FROM (o.game_id,o.game_date,o.home_id,o.away_id,o.home_score,o.away_score,o.neutral_site,o.cup_group_game)''',
      'old period sample': '''SELECT v.game_id,v.period FROM prior.audit.verified_period_score v
          JOIN candidate g ON v.game_id=g.gid LEFT JOIN period_cells h ON (v.game_id,v.period,g.hid)=(h.game_id,h.period_number,h.team_id)
          LEFT JOIN period_cells a ON (v.game_id,v.period,g.aid)=(a.game_id,a.period_number,a.team_id)
          WHERE (v.home_points,v.away_points) IS DISTINCT FROM (h.points,a.points)''',
    }
    for name,sql in queries.items():
        check(c,name,sql)
    # Match every chart observation to the shooting stream without using row order or a lossy set.
    shot_fields = ('game_id,season,period_number,clock_display_value,team_id,athlete_id_1,athlete_id_2,'
                   'type_id,type_text,scoring_play,score_value,coordinate_x,coordinate_y,coordinate_x_raw,coordinate_y_raw,athlete_name_1,athlete_name_2')
    if difference(c,f'SELECT {shot_fields} FROM source_shot SEMI JOIN candidate ON source_shot.game_id=candidate.id',
                    f'SELECT {shot_fields} FROM play_obs WHERE shooting_play'):
        raise ValueError('Chart and source-play vectors differ in exact multiset comparison')


def materialize(c: duckdb.DuckDBPyConnection, a: argparse.Namespace, assets: list[Input]) -> None:
    c.execute('CREATE SCHEMA nba; CREATE SCHEMA audit')
    for alias,path in [('history',a.history),('prior',a.prior)]:
        c.execute(f'ATTACH {sql_string(str(path))} AS {alias} (READ_ONLY)')
    c.execute('CREATE TEMP TABLE config(season_end INT, cutoff DATE)')
    c.execute('INSERT INTO config VALUES (?,?)',[a.season_end,a.cutoff])
    for asset in assets:
        c.execute(f'CREATE TEMP VIEW source_{asset.role} AS SELECT * FROM read_parquet({sql_string(str(asset.path))})')
    key(c,'source_schedule','id')
    # The provider crosswalk also has an NBA-only future Cup fixture. It has no ESPN join key.
    check(c,'duplicate ESPN fixture crosswalk', '''SELECT espn_game_id FROM source_game_map
        WHERE espn_game_id IS NOT NULL GROUP BY 1 HAVING count(*)<>1''')
    key(c,'source_team_map','espn_team_id')
    key(c,'source_team_map','nba_team_id')
    label_periods(c)
    c.execute((WAREHOUSE/'current_season.sql').read_text())
    validate_stage(c)
    if a.previous_release is not None:
        c.execute(f'ATTACH {sql_string(str(a.previous_release))} AS previous (READ_ONLY)')
        previous_scope = c.execute('SELECT season_end,cutoff FROM previous.audit.scope').fetchall()
        if len(previous_scope)!=1 or previous_scope[0][0]!=a.season_end or previous_scope[0][1]>a.cutoff:
            raise ValueError('Previous release has a different season or later cutoff')
        check(c,'previous release result changed', '''SELECT o.game_id FROM previous.nba.fact_game o
           LEFT JOIN candidate g ON o.game_id=g.gid
           WHERE (g.gid,g.game_date,g.hid,g.aid,g.home_score,g.away_score,g.phase)
              IS DISTINCT FROM (o.game_id,o.game_date,o.home_team_id,o.away_team_id,o.home_score,o.away_score,o.season_type)''')
    c.execute((WAREHOUSE/'current_season_publish.sql').read_text())
    project_box(c,'fact_team_game_stats',TEAM_BOX,'team_obs')
    project_box(c,'fact_player_game_stats',PLAYER_BOX,
                'player_obs WHERE person IS NOT NULL AND (did_not_play OR minutes IS NOT NULL)')
    c.execute('''CREATE TABLE audit.scope AS SELECT season_end,cutoff,'Regular-season feed scope, excluding preseason'::VARCHAR AS coverage_scope,
       FALSE AS redistribution_cleared FROM config;
       CREATE TABLE audit.source_asset(role VARCHAR PRIMARY KEY,url VARCHAR NOT NULL,sha256 VARCHAR NOT NULL,asset_id BIGINT,updated_at VARCHAR);
       CREATE TABLE audit.game_coverage AS
       WITH p AS (SELECT game_id,count(*) AS players FROM nba.fact_player_game_stats WHERE participation_status='played' GROUP BY 1),
            u AS (SELECT game_id,count(*) FILTER(WHERE person_id IS NULL) AS unmatched_player_rows,
              count(*) FILTER(WHERE NOT did_not_play AND reported_rounded_minutes IS NULL) AS ambiguous_player_rows
              FROM audit.player_source GROUP BY 1),
            b AS (SELECT game_id,count(*) AS source_events FROM audit.play_source GROUP BY 1),
            e AS (SELECT game_id,count(*) AS accepted_events FROM nba.fact_play_event GROUP BY 1),
            z AS (SELECT game_id,count(*) AS field_goal_attempts FROM nba.field_goal_attempt GROUP BY 1),
            x AS (SELECT game_id,count(*) AS shot_box_disagreements FROM audit.shot_box_conflict GROUP BY 1)
       SELECT g.game_id,g.game_date,coalesce(players,0) AS played_player_rows,coalesce(unmatched_player_rows,0) AS unmatched_player_rows,
         coalesce(ambiguous_player_rows,0) AS ambiguous_player_rows,
         coalesce(source_events,0) AS source_events,coalesce(accepted_events,0) AS accepted_events,
         coalesce(field_goal_attempts,0) AS field_goal_attempts,coalesce(shot_box_disagreements,0) AS shot_box_disagreements,
         CASE WHEN b.game_id IS NULL THEN 'source_stream_missing' WHEN e.game_id IS NULL THEN 'held_boundary_disagreement'
              ELSE 'reconciled_period_boundaries' END AS play_status
       FROM nba.dim_game g LEFT JOIN p USING(game_id) LEFT JOIN u USING(game_id) LEFT JOIN b USING(game_id)
       LEFT JOIN e USING(game_id) LEFT JOIN z USING(game_id) LEFT JOIN x USING(game_id);''')
    c.executemany('INSERT INTO audit.source_asset VALUES (?,?,?,?,?)',
                  [(x.role,x.url,x.digest,x.asset_id,x.updated_at) for x in assets]
                  + [(name,'local-checkpoint',file_hash(path),None,None)
                     for name,path in [('historical_contract',a.history),('prior_results',a.prior)]])
    if a.previous_release is not None:
        c.execute("INSERT INTO audit.source_asset VALUES ('previous_release','local-checkpoint',?,NULL,NULL)",
                  [file_hash(a.previous_release)])
    team_fields = ('fgm','fga','fg3m','fg3a','ftm','fta','oreb','dreb','reb','ast','pf','stl','tov','blk','pts')
    c.execute('CREATE TABLE audit.team_scorer_sample AS '+ ' UNION ALL '.join(
        f"SELECT t.game_id,t.team_id,'{k}' AS field,{('s.player_line_turnovers' if k=='tov' else 't.'+k)} AS asset_value, "
        f"b.gamebook_counts['{k}'] AS scorer_value,b.gamebook_url FROM nba.fact_team_game_stats t "
        'JOIN audit.team_source s USING(game_id,team_id) JOIN prior.audit.team_box_local_review b USING(game_id,team_id)'
        for k in team_fields)+ ''' UNION ALL SELECT t.game_id,t.team_id,'total_turnovers',t.tov,b.gamebook_total_turnovers,b.gamebook_url
        FROM nba.fact_team_game_stats t JOIN prior.audit.team_box_local_review b USING(game_id,team_id)''')
    check(c,'Team-box scorer sample differs','SELECT * FROM audit.team_scorer_sample WHERE asset_value IS DISTINCT FROM scorer_value')
    key(c,'nba.fact_player_game_stats','game_id,person_id')
    key(c,'nba.field_goal_attempt','attempt_key')
    check(c,'Published play actor mismatch', '''SELECT e.game_id,e.action_number FROM nba.fact_play_event e
        WHERE e.person_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM nba.fact_player_game_stats b
           WHERE (e.game_id,e.team_id,e.person_id)=(b.game_id,b.team_id,b.person_id) AND b.participation_status='played')''')
    for table, in c.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog=current_database() AND table_schema='nba'").fetchall():
        shape = [r[:2] for r in c.execute(f'DESCRIBE nba.{table}').fetchall()]
        original = [r[:2] for r in c.execute(f'DESCRIBE history.nba.{table}').fetchall()]
        if shape!=original:
            raise ValueError(f'Historical column shape differs: {table}')


def replay(c: duckdb.DuckDBPyConnection, output: Path) -> None:
    c.execute(f'ATTACH {sql_string(str(output))} AS replayed (READ_ONLY)')
    relations = "SELECT schema_name,relation_name FROM (SELECT database_name,schema_name,table_name AS relation_name FROM duckdb_tables() UNION ALL SELECT database_name,schema_name,view_name AS relation_name FROM duckdb_views()) WHERE schema_name IN ('nba','audit')"
    cols = "SELECT table_schema,table_name,column_name,ordinal_position,data_type FROM information_schema.columns WHERE table_schema IN ('nba','audit')"
    for query, field in [(relations,'database_name'),(cols,'table_catalog')]:
        if difference(c,query+f' AND {field}=current_database()',query+f" AND {field}='replayed'"):
            raise ValueError('Replay relation inventory or column contract differs')
    names = c.execute(relations+' AND database_name=current_database() ORDER BY 1,2').fetchall()
    for schema,table in names:
        full = f'{schema}.{table}'
        if difference(c,f'SELECT * FROM {full}',f'SELECT * FROM replayed.{full}'):
            raise ValueError(f'Exact checked replay failed: {full}')
    print(f'{len(names)} relations replayed exactly')


def run(a: argparse.Namespace) -> None:
    output: Path = a.output.absolute()
    if a.action=='build' and (os.path.lexists(output) or os.path.lexists(f'{output}.wal')):
        raise FileExistsError(output)
    if a.cutoff.year not in (a.season_end-1,a.season_end):
        raise ValueError('Cutoff is out of season')
    assets = input_assets(a.assets,a.season_end)
    cutoff_closed = datetime.combine(a.cutoff+timedelta(days=1),time(),tzinfo=ET)
    if cutoff_closed>datetime.now(timezone.utc) or any(datetime.fromisoformat(i.updated_at)<cutoff_closed for i in assets if i.role in {'schedule','team','player','play','shot'}):
        raise ValueError('Cutoff is not closed in Eastern time or a dynamic asset predates the end of cutoff')
    paths = (a.history,a.prior,*(p for i in assets for p in (i.path,i.path.with_suffix('.parquet.asset.json'))))
    if a.previous_release is not None:
        paths = (*paths,a.previous_release)
    if a.action=='verify-inputs':
        paths = (*paths,output)
    before = tuple(map(snapshot,paths))
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.nba-current-') as work:
        target = Path(work)/'release.duckdb' if a.action=='build' else output
        if a.action=='build':
            with duckdb.connect(str(target),config={'threads':2}) as c:
                materialize(c,a,assets)
                c.execute('CHECKPOINT')
        snapshot(target)
        with duckdb.connect(config={'threads':2}) as c:
            materialize(c,a,assets)
            replay(c,target)
            print(c.execute('''SELECT count(*) AS finals, sum(played_player_rows) AS player_rows,
                 sum(unmatched_player_rows) AS held_player_rows,sum(accepted_events) AS plays,
                 sum(field_goal_attempts) AS attempts,count(*) FILTER(WHERE play_status<>'reconciled_period_boundaries') AS held_streams
                 FROM audit.game_coverage''').fetchone())
        if before!=tuple(map(snapshot,paths)) or os.path.lexists(f'{output}.wal'):
            raise RuntimeError('Input or output WAL changed')
        if a.action=='build':
            os.link(target,output)
    print(output)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('build','verify-inputs'))
    p.add_argument('--cutoff',type=date.fromisoformat,required=True)
    p.add_argument('--season-end',type=int,default=2027)
    p.add_argument('--assets',type=Path,required=True)
    p.add_argument('--history',type=Path,default=ROOT/'data/nba-clean-v22.duckdb')
    p.add_argument('--prior',type=Path,default=ROOT/'data/nba-2026-27-box-check-2026-11-19.duckdb')
    p.add_argument('--previous-release',type=Path,help='Require exact preservation of this release’s prior final results')
    p.add_argument('--output',type=Path,required=True)
    run(p.parse_args())


if __name__=='__main__':
    main()
