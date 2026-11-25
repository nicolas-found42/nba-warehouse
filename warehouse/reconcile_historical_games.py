"""Apply reviewed historical fixture corrections with the displaced facts retained."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from fetch_bref_periods import Fixture, ORIGIN, parse_box
from fetch_espn import BASE, fixture_key
from load_usatoday_preseason import event_info, fixture_sides

EVIDENCE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence'
CACHE = EVIDENCE / 'public-preseason-boxes'
USA_STATS = 'https://sportsdata.usatoday.com/basketball/nba/stats/'
SOURCE = 'verified_historical_fixture_correction'
DETROIT = '0045700221'
OAKLAND = '0010400105'
ANAHEIM = '0010500098'
PARKER = '0010500119'
DETROIT_BOX = '/boxscores/195803150DET.html'
DETROIT_REPORT = 'https://www.nba.com/pistons/news/pistons-digest-sorting-out-rotations-back-end-on-pistons-to-do-list-with-a-month-to-go'
PRESEASON = {
    OAKLAND: ('2004-10-27', 2004, 'GSW', 'LAC', 1610612744, 1610612746, (84, 78), (82, 78),
              '541929', 'Arena in Oakland', '241027009', 'https://www.latimes.com/archives/la-xpm-2004-oct-28-sp-nbarep28-story.html'),
    ANAHEIM: ('2005-10-25', 2005, 'LAL', 'UTA', 1610612747, 1610612762, (95, 71), (95, 85),
              '661261', 'Arrowhead Pond of Anaheim', '251025013', 'https://www.ocregister.com/2005/10/26/bryant-enjoys-limited-success/'),
}
PINNED = {
    'usatoday-stats-541929.html': '983d378750dd8b716295ea4aac16964e743023ffd809d1af67822e497f2d8a07',
    'usatoday-stats-661261.html': '0f68cb361d96f9bf0790889e1b0175db51e8011a310e0c0835888479414fac76',
    'usatoday-stats-661281.html': 'fa9cc313370498448fb472c02041d0b4ad461918c126ca584aa8543890377d08',
    'espn-241027009.json': '744d2fb4a57933d5fe9a755081c3952173c22270ab7921bd21ee36a7e1a190b0',
    'espn-251025013.json': '61a0b6ca0344310108aa40e9014c3072faf2485e81fe3612abc86c4665c607a2',
    '195803150DET.html': '0bfdbbf15c820cc02e1aafcac1f7265b1305afbd63cae48753e52a68757ec56b',
}


def read_evidence(path: Path) -> bytes:
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PINNED[path.name]:
        raise ValueError(f'reviewed evidence has changed: {path.name}')
    return raw


def begin_correction(c, game, day, home, away, old_score, review, *, scoring=True):
    if c.execute('SELECT 1 FROM audit.historical_fixture_correction WHERE game_id = ?', [game]).fetchone():
        return False
    fixture = c.execute('''SELECT g.game_date::VARCHAR,g.home_team_id,g.away_team_id,r.home_score,r.away_score
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id) WHERE g.game_id = ?''', [game]).fetchone()
    if fixture != (day, home, away, *old_score):
        raise ValueError(f'expected original fixture and final are missing: {game}')
    affected = ('fact_play_event', 'fact_shot', 'fact_game_period_score') if scoring else ('fact_play_event', 'fact_shot')
    for table in affected:
        if c.execute(f'SELECT 1 FROM core.{table} WHERE game_id = ? LIMIT 1', [game]).fetchone():
            raise ValueError(f'correction would invalidate existing {table} for {game}')
    c.execute('''INSERT INTO audit.historical_fixture_correction
        SELECT ?, now(), ?::JSON, to_json(g), to_json(r),
            (SELECT to_json(list(t ORDER BY team_id)) FROM core.fact_team_game_stats t WHERE game_id = ?),
            (SELECT to_json(list(p ORDER BY person_id)) FROM core.fact_player_game_stats p WHERE game_id = ?)
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id) WHERE g.game_id = ?''',
        [game, json.dumps(review), game, game, game])
    return True


def stat_line(s):
    fg, three, ft, reb = (s[k] for k in ('fieldGoals', 'threePointFieldGoals', 'freeThrows', 'rebounds'))
    return (fg['made'], fg['attempted'], three['made'], three['attempted'], ft['made'], ft['attempted'],
            reb['offensive'], reb['defensive'], reb['total'], s['assists'], s['steals'], s['blockedShots'],
            s['turnovers'] if isinstance(s['turnovers'], int) else s['turnovers']['total'], s['personalFouls'])


def check_scoring(line, points):
    fgm, fga, tpm, tpa, ftm, fta, *_ = line
    if not (tpm <= fgm <= fga and tpm <= tpa <= fga and ftm <= fta and points == 2 * fgm + tpm + ftm):
        raise ValueError('published shooting and scoring disagree')


def publish_periods(c, game, home, away, final, periods):
    if len(periods) != 2 or any(len(line) != 4 or sum(line) != score for line, score in zip(periods, final)):
        raise ValueError(f'reviewed periods do not reconcile: {game}')
    c.execute('UPDATE core.fact_game_result SET home_score = ?, away_score = ? WHERE game_id = ?', [*final, game])
    c.executemany('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)',
                  [(game, team, number, points) for team, line in zip((home, away), periods)
                   for number, points in enumerate(line, 1)])
    c.execute("INSERT INTO audit.gap_attempt VALUES ('period_scores',?,?,now(),'admitted','Source receipts in audit.historical_fixture_correction')",
              [game, SOURCE])


def check_players(c, game, home, away, final):
    observed = dict(c.execute('''SELECT team_id, sum(points) FROM core.fact_player_game_stats
        WHERE game_id = ? GROUP BY team_id''', [game]).fetchall())
    if observed != dict(zip((home, away), final)):
        raise ValueError(f'player points disagree for {game}: {observed}')


def reconcile_detroit(c):
    path = EVIDENCE / 'bref-cache' / 'boxscores' / '195803150DET.html'
    if not path.is_file():
        return
    home, away, final = 1610612765, 1610612758, (100, 83)
    day = dt.date(1958, 3, 15)
    raw = read_evidence(path)
    result = parse_box(Fixture(DETROIT, day, home, away, *final, DETROIT_BOX, 'DET', 'CIN'), raw)
    if result['status'] != 'matched_fixture_exact_printed_periods':
        raise ValueError('cannot confirm the reviewed Detroit and Cincinnati quarters')
    footer = re.search(r'<table[^>]*id="box-CIN-game-basic".*?<tfoot>(.*?)</tfoot>', raw.decode(), re.S)
    fields = dict(re.findall(r'data-stat="(\w+)"[^>]*>\s*(\d+)\s*</td>', footer[1]))
    fg, fga, ftm, fta, pf, pts = [int(fields[k]) for k in ('fg', 'fga', 'ft', 'fta', 'pf', 'pts')]
    if (pts, 2 * fg + ftm) != (83, 83) or not (fg <= fga and ftm <= fta):
        raise ValueError('Cincinnati team box does not reconcile independently')
    review = [{"url": ORIGIN + DETROIT_BOX, "sha256": PINNED[path.name]}, {"url": DETROIT_REPORT}]
    with warehouse_transaction(c):
        if not begin_correction(c, DETROIT, day.isoformat(), home, away, (100, 93), review):
            return
        check_players(c, DETROIT, home, away, final)
        c.execute('''INSERT INTO core.fact_team_game_stats (game_id,team_id,is_home,is_win,pts,fgm,fga,ftm,fta,pf,fg_pct,ft_pct)
            VALUES (?,?,false,false,?,?,?,?,?,?,?,?)''', [DETROIT, away, pts, fg, fga, ftm, fta, pf, fg / fga, ftm / fta])
        publish_periods(c, DETROIT, home, away, final, (result['home_periods'], result['away_periods']))
        c.execute("INSERT INTO audit.gap_attempt VALUES ('team_box',?,?,now(),'admitted',?)", [DETROIT, SOURCE, ORIGIN + DETROIT_BOX])
    print('Corrected 1958 Detroit 100–83 Cincinnati and admitted the four printed period pairs', flush=True)


def espn_periods(espn, event_id, day, home_code, away_code, final):
    header = json.loads(espn)['header']
    comp = header['competitions'][0]
    date = dt.datetime.fromisoformat(comp['date']).astimezone(ZoneInfo('America/New_York')).date().isoformat()
    if header['id'] != event_id or date != day or fixture_key(comp) != (home_code, away_code, *final):
        raise ValueError('ESPN line-score fixture disagrees with the reviewed event')
    values = {side['homeAway']: [int(p['displayValue']) for p in side['linescores']] for side in comp['competitors']}
    return values['home'], values['away']


def usa_box(raw, event_id, day, season, home, away, hc, ac, final, venue):
    event = event_info(raw, event_id)
    sides = fixture_sides(event, (day, season, home, away, hc, ac, *final, venue))
    if not sides or sides.get(event['homeTeam']['id']) != home or event['eventType']['id'] != '0' or event['venue']['name'] != venue:
        raise ValueError(f'USA TODAY event {event_id} does not match the fixture')
    return event, {sides[b['teamID']]: b for b in event['boxScores']}


def player_from_box(c, team_box, first, last, pid, season_team, season):
    matches = [p for p in team_box['playerStats'] if (p['player']['firstName'], p['player']['lastName']) == (first, last)]
    identity = c.execute('''SELECT DISTINCT d.display_name FROM core.dim_person d
        JOIN core.fact_player_game_stats p USING (person_id) JOIN core.dim_game g USING (game_id)
        WHERE d.person_id = ? AND p.team_id = ? AND g.season_start = ?''', [pid, season_team, season]).fetchall()
    if len(matches) != 1 or identity != [(f'{first} {last}',)]:
        raise ValueError('reviewed player identity does not reconcile')
    result = matches[0]
    check_scoring(stat_line(result), result['points'])
    return result


def reconcile_preseason(c, game):
    day, season, hc, ac, home, away, old, final, eid, venue, espn_id, companion = PRESEASON[game]
    primary, espn = CACHE / f'usatoday-stats-{eid}.html', CACHE / f'espn-{espn_id}.json'
    if not primary.is_file() or not espn.is_file():
        return
    event, box = usa_box(read_evidence(primary), eid, day, season, home, away, hc, ac, final, venue)
    periods = espn_periods(read_evidence(espn), espn_id, day, hc, ac, final)
    review = [{"url": USA_STATS + eid, "sha256": PINNED[primary.name]}, {"url": f'{BASE}/summary?event={espn_id}',
               "sha256": PINNED[espn.name]}, {"url": companion, "verified_venue": event['venue']['name']}]
    stats = {team: stat_line(box[team]['teamStats']) for team in (home, away)}
    for team, points in zip((home, away), final):
        check_scoring(stats[team], points)
        if sum(p['points'] for p in box[team]['playerStats']) != points:
            raise ValueError(f'published individual points differ from team total: {team}')
    with warehouse_transaction(c):
        if not begin_correction(c, game, day, home, away, old, review):
            return
        if game == OAKLAND:
            c.execute('''INSERT INTO core.fact_team_game_stats (game_id,team_id,is_home,is_win,pts,fgm,fga,fg3m,fg3a,ftm,fta,
                oreb,dreb,reb,ast,stl,blk,tov,pf) VALUES (?,?,true,true,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [game, home, final[0], *stats[home]])
            c.execute("INSERT INTO audit.gap_attempt VALUES ('team_box',?,?,now(),'admitted',?)", [game, SOURCE, USA_STATS + eid])
        else:
            humphries = player_from_box(c, box[away], 'Kris', 'Humphries', 2743, away, season)
            c.execute('''INSERT INTO core.fact_player_game_stats (game_id,person_id,team_id,opponent_team_id,is_home,is_win,participation_status,
                points,fgm,fga,fg3m,fg3a,ftm,fta,oreb,dreb,reb,assists,steals,blocks,turnovers,fouls_personal)
                VALUES (?,2743,?,?,false,false,'played',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                [game, away, home, humphries['points'], *stat_line(humphries)])
            fgm, fga, tpm, tpa, ftm, fta, *_ = stats[away]
            c.execute('''UPDATE core.fact_team_game_stats SET pts=?,fgm=?,fga=?,fg3m=?,fg3a=?,ftm=?,fta=?,
                oreb=?,dreb=?,reb=?,ast=?,stl=?,blk=?,tov=?,pf=?,fg_pct=?,fg3_pct=?,ft_pct=?,min=NULL
                WHERE game_id=? AND team_id=? AND pts=?''', [final[1], *stats[away], fgm / fga, tpm / tpa, ftm / fta, game, away, old[1]])
            c.execute('''UPDATE core.fact_team_game_stats SET plus_minus = CASE WHEN is_home THEN 10 ELSE -10 END
                WHERE game_id = ? AND abs(plus_minus) = 24''', [game])
        check_players(c, game, home, away, final)
        publish_periods(c, game, home, away, final, periods)
        current = dict(c.execute('SELECT team_id,pts FROM core.fact_team_game_stats WHERE game_id = ?', [game]).fetchall())
        if current != dict(zip((home, away), final)):
            raise ValueError(f'corrected team boxes do not reconcile for {game}')
    print(f'Corrected preseason final {game} to {final[0]}–{final[1]} and admitted printed periods', flush=True)


def reconcile_parker(c):
    event_id, home, away, day, final = '661281', 1610612747, 1610612758, '2005-10-28', (105, 103)
    path = CACHE / f'usatoday-stats-{event_id}.html'
    if not path.is_file():
        return
    _, box = usa_box(read_evidence(path), event_id, day, 2005, home, away, 'LAL', 'SAC', final, 'Thomas & Mack Center')
    player = player_from_box(c, box[home], 'Smush', 'Parker', 2470, home, 2005)
    published = (*stat_line(player), player['points'])
    if not player['isGameStarted'] or player['totalSecondsPlayed'] <= 0 or any(
            p['player']['lastName'] == 'Parada' for p in box[home]['playerStats']):
        raise ValueError('authenticated Parker line does not prove the correction')
    review = [{"url": USA_STATS + event_id, "sha256": PINNED[path.name]},
              {"url": 'https://www.ocregister.com/2005/10/29/stars-starting-to-align/'}]
    with warehouse_transaction(c):
        if not begin_correction(c, PARKER, day, home, away, final, review, scoring=False):
            return
        old = c.execute('''SELECT fgm,fga,fg3m,fg3a,ftm,fta,oreb,dreb,reb,assists,steals,blocks,turnovers,fouls_personal,points
            FROM core.fact_player_game_stats WHERE game_id = ? AND team_id = ? AND person_id = 2847''', [PARKER, home]).fetchone()
        dependent = c.execute('SELECT 1 FROM core.fact_starting_lineup WHERE game_id=? AND person_id IN (2470,2847)', [PARKER]).fetchone()
        if old != published or dependent:
            raise ValueError('original row does not describe the same player performance')
        minutes = player['totalSecondsPlayed'] / 60
        c.execute('''UPDATE core.fact_player_game_stats SET person_id=2470,num_minutes=?,reported_num_minutes=?,
            starting_position=? WHERE game_id=? AND team_id=? AND person_id=2847''',
            [minutes, minutes, player['startingPosition']['abbreviation'], PARKER, home])
        check_players(c, PARKER, home, away, final)
    print('Restored the reported Smush Parker identity and minutes for the October 28, 2005 Lakers box', flush=True)


def load(c):
    c.execute('''CREATE TABLE IF NOT EXISTS audit.historical_fixture_correction (
        game_id VARCHAR PRIMARY KEY, corrected_at TIMESTAMPTZ, sources JSON, previous_fixture JSON, previous_final JSON,
        previous_team_box JSON, previous_player_box JSON)''')
    reconcile_detroit(c)
    for game in PRESEASON:
        reconcile_preseason(c, game)
    reconcile_parker(c)


if __name__ == '__main__':
    with warehouse_lock(), connect(DEFAULT_OUT) as warehouse:
        load(warehouse)
        warehouse.execute('CHECKPOINT')
