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
KNICKS_1951_BOX = '/boxscores/195103310NYK.html'
KNICKS_1951_REPORT = 'https://www.loc.gov/resource/sn83045462/1951-04-01/ed-1/?sp=32'
KNICKS_1951_SCAN = 'a6fe3c6dea419324d683b5cd44fd4da1f963030f01dc736b263587c9a5ff636d'
BOSTON_1951_BOX = '/boxscores/195103220NYK.html'
BOSTON_1951_WITNESSES = (
    ('nytimes-1951-03-24-sitemap.html', '2bd46e3360c281081f1b98f5df957e61a64ae5a07c82e019ffb2704927f9ae8f',
     'https://www.nytimes.com/1951/03/24/archives/knick-five-to-meet-celtics-at-garden.html'),
    ('nytimes-1951-03-25-sitemap.html', 'eb1203bf1bd0fe2f5e6d29968b38c080c1b1bbea2a0004d0bdfe73a10d7a6718',
     'https://www.nytimes.com/1951/03/25/archives/knicks-turn-back-celtic-five-9278-new-york-pro-squad-reaches.html'),
)
PHILADELPHIA_1958_BOX = '/boxscores/195803260PHW.html'
PHILADELPHIA_1958_WITNESSES = (
    ('nytimes-1958-03-27-sitemap.html', '2baa3e6cbe66fddf4b1457a661a33ce359388afa318326e337897c446ad184bf',
     'https://www.nytimes.com/sitemap/1958/03/27/'),
    ('usatoday-ap-caption-tinyfish.json', '2e62af25040130c3c94cab3acdcc12675e8dfe2b7d819dab7bd7e896c794f3de',
     'https://celticswire.usatoday.com/story/sports/nba/celtics/2022/08/01/nba-boston-celtics-russell-photos/77160278007'),
)
PRESEASON = {
    OAKLAND: ('2004-10-27', 2004, 'GSW', 'LAC', 1610612744, 1610612746, (84, 78), (82, 78),
              '541929', 'Arena in Oakland', '241027009', 'https://www.latimes.com/archives/la-xpm-2004-oct-28-sp-nbarep28-story.html'),
    ANAHEIM: ('2005-10-25', 2005, 'LAL', 'UTA', 1610612747, 1610612762, (95, 71), (95, 85),
              '661261', 'Arrowhead Pond of Anaheim', '251025013', 'https://www.ocregister.com/2005/10/26/bryant-enjoys-limited-success/'),
    '0010400067': ('2004-10-22', 2004, 'UTA', 'SAC', 1610612762, 1610612758, (103, 88), (101, 88),
                   '541891', 'Delta Center', '241022026', 'https://deseret.com/2004/10/23/19857499/jazz-top-kings-but-sloan-not-so-happy'),
    '0010400082': ('2004-10-24', 2004, 'LAC', 'NOP', 1610612746, 1610612740, (97, 96), (97, 93),
                   '541906', 'Staples Center', '241024012', None),
    '0010400092': ('2004-10-26', 2004, 'PHI', 'UTA', 1610612755, 1610612762, (97, 88), (97, 86),
                   '541916', 'Wachovia Center', '241026020', 'https://deseret.com/2004/10/27/19858147/jazz-rotation-not-set-yet'),
    '0010400095': ('2004-10-26', 2004, 'PHX', 'OKC', 1610612756, 1610612760, (103, 95), (103, 93),
                   '541919', 'America West Arena', '241026021', 'https://spokesman.com/stories/2004/oct/27/suns-scorch-sonics'),
    '0010400108': ('2004-10-28', 2004, 'NYK', 'UTA', 1610612752, 1610612762, (93, 85), (89, 83),
                   '541932', 'Madison Square Garden', '241028018', 'https://deseret.com/2004/10/29/19858497/jazz-looking-for-point-guard'),
}
PINNED = {
    'usatoday-stats-541929.html': '983d378750dd8b716295ea4aac16964e743023ffd809d1af67822e497f2d8a07',
    'usatoday-stats-661261.html': '0f68cb361d96f9bf0790889e1b0175db51e8011a310e0c0835888479414fac76',
    'usatoday-stats-661281.html': 'fa9cc313370498448fb472c02041d0b4ad461918c126ca584aa8543890377d08',
    'espn-241027009.json': '744d2fb4a57933d5fe9a755081c3952173c22270ab7921bd21ee36a7e1a190b0',
    'espn-251025013.json': '61a0b6ca0344310108aa40e9014c3072faf2485e81fe3612abc86c4665c607a2',
    '195803150DET.html': '0bfdbbf15c820cc02e1aafcac1f7265b1305afbd63cae48753e52a68757ec56b',
    '195103310NYK.html': '3f5e114388df3786db05828aa91f4a1ebc8d3d4696d335ada03a56056ef99ad9',
    '195103220NYK.html': '03eb4a26eb90370285ddbc4e2ee64646271c2f968e011763496c49c1f97aa575',
    '195803260PHW.html': 'bb7198bb5bf8c5dcf21c92630c46b5df9d61c35803eb6bcf07a778bfdc02e7b5',
    'usatoday-stats-541891.html': '04f505f7bb3ed2948cac8a0eab0f51aa53118904671f40274678f511dbcd420b',
    'usatoday-stats-541906.html': '4bc71dbab4a2ef8a34139e68cc30380d1a954c58b95f2bef26335258fddd3061',
    'usatoday-stats-541916.html': '0c94a75f4999c36bd811c068609fe4e0d335ea440828fbf32a4de48c490aac18',
    'usatoday-stats-541919.html': '60ba4152dcbe88607d004d52b9821dfe2510443514429d1895233797f99958e4',
    'usatoday-stats-541932.html': '6dc499ca7fca4eebcd5c04b918593bf64958a085fd73c42a934b8d774b6365c8',
    'espn-241022026.json': '7d893e250dbfae58dae0ae41ce78ede6249fba70dbccb8c6538155d0a7cc17e3',
    'espn-241024012.json': '5ff6bbc121c87116863b4d3c488a949676abc13d54068698e93d8400aded3771',
    'espn-241026020.json': '80c355f3f36fe151d4de898c952c3adb16e128211c20860ee486e3f37f6dd2ee',
    'espn-241026021.json': '522ff0ceaca3f3cc96bee1cd13cecce8e9eac8cdc30c63289959e2d08169ef96',
    'espn-241028018.json': '6d7515b0d5f554d7858fe7a5440a3143aff6756cc6229390a93e7fa8ee13beea',
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
    if len(periods) != 2 or len(periods[0]) != len(periods[1]) or not 4 <= len(periods[0]) <= 10 or any(
            sum(line) != score for line, score in zip(periods, final)):
        raise ValueError(f'reviewed periods do not reconcile: {game}')
    c.execute('UPDATE core.fact_game_result SET home_score=?, away_score=?, is_overtime=? WHERE game_id=?',
              [*final, len(periods[0]) > 4, game])
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


def insert_early_team_box(c, raw, game, team, code, score, home, won):
    footer = re.search(rf'<table[^>]*id="box-{code}-game-basic".*?<tfoot>(.*?)</tfoot>', raw.decode(), re.S)
    fields = dict(re.findall(r'data-stat="(\w+)"[^>]*>\s*(\d+)\s*</td>', footer[1]))
    fg, fga, ftm, fta, pf, pts = [int(fields[k]) for k in ('fg', 'fga', 'ft', 'fta', 'pf', 'pts')]
    if pts != score or pts != 2 * fg + ftm or not (fg <= fga and ftm <= fta):
        raise ValueError(f'early team box does not reconcile: {game}, {code}')
    c.execute('''INSERT INTO core.fact_team_game_stats (game_id,team_id,is_home,is_win,pts,fgm,fga,ftm,fta,pf,fg_pct,ft_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', [game, team, home, won, pts, fg, fga, ftm, fta, pf, fg / fga, ftm / fta])


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
    review = [{"url": ORIGIN + DETROIT_BOX, "sha256": PINNED[path.name]}, {"url": DETROIT_REPORT}]
    with warehouse_transaction(c):
        if not begin_correction(c, DETROIT, day.isoformat(), home, away, (100, 93), review):
            return
        check_players(c, DETROIT, home, away, final)
        insert_early_team_box(c, raw, DETROIT, away, 'CIN', final[1], False, False)
        publish_periods(c, DETROIT, home, away, final, (result['home_periods'], result['away_periods']))
        c.execute("INSERT INTO audit.gap_attempt VALUES ('team_box',?,?,now(),'admitted',?)", [DETROIT, SOURCE, ORIGIN + DETROIT_BOX])
    print('Corrected 1958 Detroit 100–83 Cincinnati and admitted the four printed period pairs', flush=True)


def reconcile_knicks_1951(c):
    path = EVIDENCE / 'bref-cache' / KNICKS_1951_BOX.lstrip('/')
    if not path.is_file():
        return
    game, home, away, final = '0045000323', 1610612752, 1610612755, (77, 75)
    day = dt.date(1951, 3, 31)
    raw = read_evidence(path)
    result = parse_box(Fixture(game, day, home, away, *final, KNICKS_1951_BOX, 'NYK', 'SYR'), raw)
    if result['status'] != 'matched_fixture_exact_printed_periods' or any(sum(result[f'{side}_periods'][:4]) != 70
                                                                        for side in ('home', 'away')):
        raise ValueError('New York and Syracuse periods disagree with the contemporary regulation tie')
    review = [{"url": ORIGIN + KNICKS_1951_BOX, "sha256": PINNED[path.name]},
              {"url": KNICKS_1951_REPORT, "scan_sha256": KNICKS_1951_SCAN}]
    with warehouse_transaction(c):
        if not begin_correction(c, game, day.isoformat(), home, away, (97, 75), review):
            c.execute('UPDATE core.fact_team_game_stats SET plus_minus=-2 WHERE game_id=? AND team_id=? AND plus_minus=-22',
                      [game, away])
            return
        check_players(c, game, home, away, final)
        boxes = c.execute('SELECT team_id,pts,fgm,ftm FROM core.fact_team_game_stats WHERE game_id=? ORDER BY team_id', [game]).fetchall()
        if boxes != [(home, 97, 26, 25), (away, 75, 23, 29)]:
            raise ValueError('New York and Syracuse original team shooting rows disagree')
        c.execute('UPDATE core.fact_team_game_stats SET pts=77 WHERE game_id=? AND team_id=?', [game, home])
        c.execute('UPDATE core.fact_team_game_stats SET plus_minus=-2 WHERE game_id=? AND team_id=? AND plus_minus=-22', [game, away])
        publish_periods(c, game, home, away, final, (result['home_periods'], result['away_periods']))
    print('Corrected 1951 New York 77–75 Syracuse in overtime and admitted all five printed periods', flush=True)


def reconcile_boston_1951(c):
    path = EVIDENCE / 'bref-cache' / BOSTON_1951_BOX.lstrip('/')
    witnesses = EVIDENCE / 'newspapers' / 'playoff-1951-bos-nyk-date'
    if not path.is_file() or not all((witnesses / filename).is_file() for filename, _, _ in BOSTON_1951_WITNESSES):
        return
    review = [{"url": ORIGIN + BOSTON_1951_BOX, "sha256": PINNED[path.name], "reported_game_date": "1951-03-24"}]
    for filename, digest, url in BOSTON_1951_WITNESSES:
        body = (witnesses / filename).read_bytes()
        if hashlib.sha256(body).hexdigest() != digest or url.encode() not in body:
            raise ValueError('New York–Boston date witness disagrees with reviewed source')
        review.append({"url": url, "sitemap_sha256": digest})
    game, home, away, final = '0045000242', 1610612752, 1610612738, (92, 78)
    result = parse_box(Fixture(game, dt.date(1951, 3, 24), home, away, *final, BOSTON_1951_BOX, 'NYK', 'BOS'), read_evidence(path))
    if result['status'] != 'matched_fixture_exact_printed_periods':
        raise ValueError('reviewed 1951 New York–Boston periods disagree')
    with warehouse_transaction(c):
        if not begin_correction(c, game, '1951-03-22', home, away, final, review):
            return
        publish_periods(c, game, home, away, final, (result['home_periods'], result['away_periods']))
    print('Published March 24, 1951 New York–Boston periods. Original dimension date retained in the fixture', flush=True)


def reconcile_philadelphia_1958(c):
    path = EVIDENCE / 'bref-cache' / PHILADELPHIA_1958_BOX.lstrip('/')
    witnesses = EVIDENCE / 'newspapers' / 'warriors-1958-boston-final'
    if not path.is_file() or not all((witnesses / filename).is_file() for filename, _, _ in PHILADELPHIA_1958_WITNESSES):
        return
    review = [{"url": ORIGIN + PHILADELPHIA_1958_BOX, "sha256": PINNED[path.name]}]
    for filename, digest, url in PHILADELPHIA_1958_WITNESSES:
        if hashlib.sha256((witnesses / filename).read_bytes()).hexdigest() != digest:
            raise ValueError('Philadelphia–Boston final witness disagrees with reviewed source')
        review.append({"url": url, "evidence_sha256": digest})
    game, home, away, final = '0045700324', 1610612744, 1610612738, (112, 97)
    day = dt.date(1958, 3, 26)
    raw = read_evidence(path)
    result = parse_box(Fixture(game, day, home, away, *final, PHILADELPHIA_1958_BOX, 'PHW', 'BOS'), raw)
    if result['status'] != 'matched_fixture_exact_printed_periods':
        raise ValueError('reviewed 1958 Philadelphia–Boston periods disagree')
    with warehouse_transaction(c):
        if not begin_correction(c, game, day.isoformat(), home, away, (111, 97), review):
            return
        check_players(c, game, home, away, final)
        insert_early_team_box(c, raw, game, home, 'PHW', final[0], True, True)
        c.execute('UPDATE core.fact_team_game_stats SET plus_minus=-15 WHERE game_id=? AND team_id=? AND plus_minus=-14',
                  [game, away])
        publish_periods(c, game, home, away, final, (result['home_periods'], result['away_periods']))
        c.execute("INSERT INTO audit.gap_attempt VALUES ('team_box',?,?,now(),'admitted',?)", [game, SOURCE, ORIGIN + PHILADELPHIA_1958_BOX])
    print('Corrected 1958 Philadelphia 112–97 Boston and admitted the printed periods and team box', flush=True)


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


def add_box_player(c, game, person, team, opponent, home, won, player):
    c.execute('''INSERT INTO core.fact_player_game_stats (game_id,person_id,team_id,opponent_team_id,is_home,is_win,participation_status,
        points,fgm,fga,fg3m,fg3a,ftm,fta,oreb,dreb,reb,assists,steals,blocks,turnovers,fouls_personal)
        VALUES (?,?,?,?,?,?,'played',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        [game, person, team, opponent, home, won, player['points'], *stat_line(player)])


def revise_team_box(c, game, team, original_score, score, line):
    fgm, fga, tpm, tpa, ftm, fta, *_ = line
    c.execute('''UPDATE core.fact_team_game_stats SET pts=?,fgm=?,fga=?,fg3m=?,fg3a=?,ftm=?,fta=?,
        oreb=?,dreb=?,reb=?,ast=?,stl=?,blk=?,tov=?,pf=?,fg_pct=?,fg3_pct=?,ft_pct=?,min=NULL
        WHERE game_id=? AND team_id=? AND pts=?''',
        [score, *line, fgm / fga if fga else None, tpm / tpa if tpa else None, ftm / fta if fta else None,
         game, team, original_score])


def reconcile_preseason(c, game):
    day, season, hc, ac, home, away, old, final, eid, venue, espn_id, companion = PRESEASON[game]
    primary, espn = CACHE / f'usatoday-stats-{eid}.html', CACHE / f'espn-{espn_id}.json'
    if not primary.is_file() or not espn.is_file():
        return
    event, box = usa_box(read_evidence(primary), eid, day, season, home, away, hc, ac, final, venue)
    periods = espn_periods(read_evidence(espn), espn_id, day, hc, ac, final)
    usa_lines = {team['id']: [line['score'] for line in sorted(team['lineScores'], key=lambda row: row['period'])]
                 for team in event['teams']}
    usa_periods = tuple(usa_lines[event[side]['id']] for side in ('homeTeam', 'awayTeam'))
    quarters_agree = periods == usa_periods
    if quarters_agree == (game == '0010400108'):
        raise ValueError(f'the expected cross-source quarter agreement changed for {game}')
    review = [{"url": USA_STATS + eid, "sha256": PINNED[primary.name], "verified_venue": event['venue']['name'],
               "home_away_quarters": usa_periods},
              {"url": f'{BASE}/summary?event={espn_id}', "sha256": PINNED[espn.name], "home_away_quarters": periods}]
    if companion:
        review.append({"url": companion})
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
        elif game == ANAHEIM:
            humphries = player_from_box(c, box[away], 'Kris', 'Humphries', 2743, away, season)
            add_box_player(c, game, 2743, away, home, False, False, humphries)
            revise_team_box(c, game, away, old[1], final[1], stats[away])
        else:
            if game == '0010400092':
                mckie = player_from_box(c, box[home], 'Aaron', 'McKie', 243, home, season)
                add_box_player(c, game, 243, home, away, True, True, mckie)
            for team, old_score, score in zip((home, away), old, final):
                revise_team_box(c, game, team, old_score, score, stats[team])
        c.execute('''UPDATE core.fact_team_game_stats SET plus_minus = CASE WHEN is_home THEN ? ELSE -? END
            WHERE game_id = ? AND plus_minus = CASE WHEN is_home THEN ? ELSE -? END''',
            [final[0] - final[1], final[0] - final[1], game, old[0] - old[1], old[0] - old[1]])
        check_players(c, game, home, away, final)
        if quarters_agree:
            publish_periods(c, game, home, away, final, periods)
        else:
            c.execute('UPDATE core.fact_game_result SET home_score=?,away_score=? WHERE game_id=?', [*final, game])
            c.execute('''INSERT INTO audit.gap_attempt VALUES ('period_scores',?,?,now(),'mismatch',
                'USA TODAY and ESPN disagree on the 3rd and 4th quarters despite agreeing on the corrected final')''', [game, SOURCE])
        current = dict(c.execute('SELECT team_id,pts FROM core.fact_team_game_stats WHERE game_id = ?', [game]).fetchall())
        if current != dict(zip((home, away), final)):
            raise ValueError(f'corrected team boxes do not reconcile for {game}')
    print(f'Corrected preseason final {game} to {final[0]}–{final[1]}. '
          f'{"Printed periods admitted" if quarters_agree else "Differing quarter scores held"}', flush=True)


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
    reconcile_knicks_1951(c)
    reconcile_boston_1951(c)
    reconcile_philadelphia_1958(c)
    for game in PRESEASON:
        reconcile_preseason(c, game)
    reconcile_parker(c)


if __name__ == '__main__':
    with warehouse_lock(), connect(DEFAULT_OUT) as warehouse:
        load(warehouse)
        warehouse.execute('CHECKPOINT')
