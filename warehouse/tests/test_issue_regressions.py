import datetime as dt
from pathlib import Path

import duckdb
import pytest

from fetch_newspaper_periods import cumulative_periods
from fetch_espn import fixture_key, fixture_matches
from injury_reports import ET, ReportRow, parse_report, publish, slots
from load_espn import names_by_stat_line


def test_newspaper_requires_every_boundary_and_tied_overtime():
    rows = [(1, 9, 11), (2, 29, 22), (3, 44, 44), (4, 53, 54)]
    assert cumulative_periods(rows, 53, 54) == [(9, 11), (20, 11), (15, 22), (9, 10)]
    with pytest.raises(ValueError, match='every period'):
        cumulative_periods(rows[:2] + rows[3:], 53, 54)
    with pytest.raises(ValueError, match='not tied'):
        cumulative_periods(rows + [(5, 58, 60)], 58, 60)


def test_injury_pdf_coordinates_survive_page_breaks_and_keep_team_notices():
    def line(y, words):
        return ''.join(f'<word xMin="{x}" yMin="{y}">{word}</word>' for x, word in words)

    header = [(10, 'Game'), (24, 'Date'), (70, 'Game'), (85, 'Time'), (125, 'Matchup'), (169, 'Team'),
              (290, 'Player'), (320, 'Name'), (450, 'Current'), (481, 'Status'), (540, 'Reason')]
    stamp = [(99, 'Injury'), (120, 'Report:'), (180, '11/24/26'), (220, '06:00'), (255, 'PM')]
    game = [(11, '11/24/2026'), (71, '07:00'), (126, 'DET@ORL'), (170, 'Detroit'), (210, 'Pistons'),
            (291, 'Example,'), (332, 'Player'), (451, 'Out'), (541, 'Injury')]
    second = [(170, 'Orlando'), (211, 'Magic'), (291, 'NOT'), (318, 'YET'), (336, 'SUBMITTED')]
    document = ('<doc><page>' + line(20, stamp) + line(40, header) + line(55, game) + '</page><page>'
                + line(20, stamp) + line(55, second) + '</page></doc>')
    published, rows = parse_report(document.encode())
    assert published == dt.datetime(2026, 11, 24, 18, tzinfo=ET)
    assert rows == [ReportRow(published.date(), 'DET@ORL', 'Detroit Pistons', 'Example, Player', 'Out', 'Injury'),
                    ReportRow(published.date(), 'DET@ORL', 'Orlando Magic', None, 'NOT YET SUBMITTED', '')]


def test_injury_pdf_filename_transition_is_midday():
    friday = {stamp.strftime('%H:%M'): name for stamp, name in slots(dt.date(2025, 12, 19))}
    assert friday['15:30'] == 'Injury-Report_2025-12-19_03PM.pdf'
    assert friday['16:45'] == 'Injury-Report_2025-12-19_04PM.pdf'
    monday = {stamp.strftime('%H:%M'): name for stamp, name in slots(dt.date(2025, 12, 22))}
    assert monday['08:45'] == 'Injury-Report_2025-12-22_08AM.pdf'
    assert monday['09:00'] == 'Injury-Report_2025-12-22_09_00AM.pdf'
    assert monday['09:15'] == 'Injury-Report_2025-12-22_09_15AM.pdf'


def test_injury_as_of_uses_whole_latest_team_report_and_excludes_after_tip():
    c = duckdb.connect()
    c.execute('''CREATE SCHEMA raw; CREATE SCHEMA core; CREATE SCHEMA audit;
        CREATE TABLE core.dim_team(team_id BIGINT, abbreviation VARCHAR, display_name VARCHAR);
        INSERT INTO core.dim_team VALUES (1,'DET','Detroit Pistons'), (2,'ORL','Orlando Magic');
        CREATE TABLE core.dim_team_version(team_id BIGINT,full_name VARCHAR);
        CREATE TABLE core.dim_person(person_id BIGINT,display_name VARCHAR);
        INSERT INTO core.dim_person VALUES (3,'Player One'), (4,'Player Two');
        CREATE TABLE core.dim_game(game_id VARCHAR, game_date DATE, away_team_id BIGINT, home_team_id BIGINT,
            game_datetime_est TIMESTAMP, participant_status VARCHAR, result_status VARCHAR);
        INSERT INTO core.dim_game VALUES ('0022600001','2026-11-24',1,2,'2026-11-24 19:00','resolved','final');
        CREATE TABLE core.fact_player_game_stats(game_id VARCHAR,team_id BIGINT,person_id BIGINT,participation_status VARCHAR);
        INSERT INTO core.fact_player_game_stats VALUES ('0022600001',1,4,'played');''')
    day = dt.date(2026, 11, 24)
    reports = {Path(f'{i}.pdf'): (str(i), dt.datetime(2026, 11, 24, hour, tzinfo=ET), [
        ReportRow(day, 'DET@ORL', 'Detroit Pistons', name, status, '')])
        for i, (hour, name, status) in enumerate(((17, 'One, Player', 'Out'), (18,'Two, Player','Questionable'), (19,'Two, Player','Out')))}
    publish(c, reports)
    c.execute('''INSERT INTO raw.nba_roster_capture VALUES
        (1,3,'Player One',NULL,NULL,DATE '2026-11-24',TIMESTAMPTZ '2026-11-24 16:00:00-05','fixture','alpha'),
        (1,4,'Player Two',NULL,NULL,DATE '2026-11-24',TIMESTAMPTZ '2026-11-24 16:00:00-05','fixture','alpha');
        INSERT INTO raw.nba_roster_publication VALUES ('fixture','alpha',TIMESTAMPTZ '2026-11-24 16:02:00-05','fixture');''')
    for cutoff, result in (('17:00', [(3, 'Out')]), ('17:59', [(3, 'Out')]), ('18:00', [(4, 'Questionable')]),
                           ('20:00', [(4, 'Questionable')])):
        assert c.execute("SELECT person_id,status FROM core.injury_report_at('0022600001', ?::TIMESTAMPTZ)",
                         [f'{day} {cutoff}:00-05']).fetchall() == result
    assert c.execute('SELECT person_id,status,participation_status FROM core.injury_report_final_pregame').fetchall() == [
        (4, 'Questionable', 'played')]
    c.execute('''INSERT INTO raw.nba_initial_schedule VALUES
        ('0022600001','2026-11-24','2026-11-24 21:00:00-05',2,1,'release','2026-08-15 00:00:00-04','release');
        INSERT INTO raw.nba_roster_capture VALUES
        (1,9,'Player Nine',NULL,NULL,'2026-11-24','2026-11-24 19:30:00-05','late','late');
        INSERT INTO raw.nba_roster_publication VALUES ('late','late','2026-11-24 19:32:00-05','fixture');''')
    assert c.execute("SELECT fixture_source, hour(tipoff_at AT TIME ZONE 'America/New_York') FROM core.pregame_fixture_at('0022600001', TIMESTAMPTZ '2026-11-24 20:00:00-05')").fetchall() == [
        ('nba_initial_schedule_release', 21)]
    assert c.execute("SELECT person_id FROM core.roster_at('0022600001', TIMESTAMPTZ '2026-11-24 20:00:00-05') ORDER BY 1").fetchall() == [(3,), (4,)]
    assert c.execute("SELECT person_id,status FROM core.injury_report_at('0022600001', TIMESTAMPTZ '2026-11-24 20:00:00-05')").fetchall() == [(4, 'Questionable')]
    c.execute('DELETE FROM raw.nba_roster_capture')
    assert c.execute("SELECT player_name,person_id FROM core.injury_report_at('0022600001',TIMESTAMPTZ '2026-11-24 18:00:00-05')").fetchall() == [
        ('Two, Player', None)]  # eventual participation cannot resolve a pregame name
    c.execute('''INSERT INTO raw.nba_injury_report_mirror VALUES
      (2027,'2026-11-24 18:30:00-05','0022600001','2026-11-24','1','10','Out','Fixture reason',NULL,'mirror'),
      (2027,'2026-11-24 18:30:00-05','0022600001','2026-11-24','1','11','Out','Fixture reason',NULL,'mirror')''')
    assert c.execute("SELECT source_player_id,person_id FROM core.injury_report_at('0022600001',TIMESTAMPTZ '2026-11-24 18:30:00-05') ORDER BY 1").fetchall() == [
        ('10', None), ('11', None)]
    from injury_reports import CACHE

    same_timestamp = dt.datetime(2026, 11, 24, 18, tzinfo=ET)
    path = CACHE / 'pdf-mirror' / '2027' / '1.pdf'
    publish(c, {path: ('mirror-version', same_timestamp, [ReportRow(day, 'DET@ORL', 'Detroit Pistons',
                                                                  'Two, Player', 'Out', 'Mirror variation')])})
    assert c.execute("SELECT status FROM core.injury_report_at('0022600001',TIMESTAMPTZ '2026-11-24 18:05:00-05')").fetchall() == [
        ('Questionable',)]
    assert c.execute("SELECT disposition FROM audit.injury_report_document WHERE sha256='mirror-version'").fetchone() == (
        'lower_source_priority',)
    c.execute('''INSERT INTO core.dim_team VALUES (5,'SAN','San Antonio Spurs'), (6,'PHI','Philadelphia 76ers');
        INSERT INTO core.dim_game VALUES ('0022600002','2026-11-24',6,5,'2026-11-24 19:30','resolved','final');
        INSERT INTO raw.nba_injury_report_mirror VALUES
          (2027,'2026-11-24 18:00:00-05','0022600002','2026-11-24','5','21','Out','Unrelated game',NULL,'mirror');''')
    assert c.execute("SELECT game_id,team_id,status FROM core.injury_report_snapshot WHERE game_id='0022600002'").fetchall() == [
        ('0022600002', 5, 'Out')]
    assert c.execute("SELECT team_id,status FROM core.injury_report_at('0022600002',TIMESTAMPTZ '2026-11-24 18:05:00-05')").fetchall() == [
        (5, 'Out')]
    publish(c, {Path('3.pdf'): ('sas-alias', dt.datetime(2026, 11, 24, 18, 15, tzinfo=ET), [
        ReportRow(day, 'PHI@SAS', 'San Antonio Spurs', 'One, Player', 'Probable', 'Alias test')] )})
    assert c.execute("SELECT team_id,status FROM core.injury_report_at('0022600002',TIMESTAMPTZ '2026-11-24 18:30:00-05')").fetchall() == [
        (5, 'Probable')]


def test_first_schedule_capture_remains_queryable_when_the_fixture_changes():
    c = duckdb.connect()
    c.execute('''CREATE SCHEMA core; CREATE SCHEMA raw;
        CREATE TABLE core.dim_person(person_id BIGINT);
        CREATE TABLE core.dim_team(team_id BIGINT, display_name VARCHAR, abbreviation VARCHAR);
        INSERT INTO core.dim_team VALUES (1,'Detroit','DET'), (2,'Orlando','ORL');
        CREATE TABLE core.dim_game(game_id VARCHAR,game_date DATE,home_team_id BIGINT,away_team_id BIGINT,
            game_datetime_est TIMESTAMP,participant_status VARCHAR,result_status VARCHAR);''')
    import pregame_sources
    pregame_sources.publish(c)
    c.execute('''INSERT INTO raw.nba_pregame_schedule VALUES
        ('0022600001','2026-11-24','2026-11-24 19:00:00-05',1,2,'scheduled','fixture','2026-11-24 12:00:00-05','a'),
        ('0022600001','2026-11-24','2026-11-24 21:00:00-05',1,2,'scheduled','fixture','2026-11-24 16:00:00-05','b');
        INSERT INTO core.dim_game VALUES ('0022600001','2026-11-24',1,2,'2026-11-24 21:00:00','resolved','final');''')
    for cutoff, expected in (('11:00', []), ('15:00', [(19,)]), ('17:00', [(21,)])):
        assert c.execute("SELECT hour(tipoff_at AT TIME ZONE 'America/New_York') FROM core.pregame_fixture_at('0022600001',?::TIMESTAMPTZ)",
                         [f'2026-11-24 {cutoff}:00-05']).fetchall() == expected


def test_roster_cutoff_uses_published_time_after_capture_start():
    c = duckdb.connect()
    c.execute('''CREATE SCHEMA core; CREATE SCHEMA raw;
        CREATE TABLE core.dim_person(person_id BIGINT);
        CREATE TABLE core.dim_team(team_id BIGINT, display_name VARCHAR, abbreviation VARCHAR);
        INSERT INTO core.dim_team VALUES (1,'Detroit','DET'), (2,'Orlando','ORL');
        CREATE TABLE core.dim_game(game_id VARCHAR,game_date DATE,home_team_id BIGINT,away_team_id BIGINT,
            game_datetime_est TIMESTAMP,participant_status VARCHAR,result_status VARCHAR);
        INSERT INTO core.dim_game VALUES ('fixture','2026-11-24',2,1,'2026-11-24 19:00','resolved','scheduled');''')
    import pregame_sources
    pregame_sources.publish(c)
    c.execute('''INSERT INTO raw.nba_roster_capture VALUES
        (1,3,'Player One',NULL,NULL,'2026-11-24','2026-11-24 18:30:00-05','roster','digest');
        INSERT INTO raw.nba_roster_publication VALUES ('roster','digest','2026-11-24 18:32:00-05','fixture');''')
    assert c.execute("SELECT person_id FROM core.roster_at('fixture',TIMESTAMPTZ '2026-11-24 18:31:00-05')").fetchall() == []
    assert c.execute("SELECT person_id FROM core.roster_at('fixture',TIMESTAMPTZ '2026-11-24 18:32:00-05')").fetchall() == [(3,)]


def test_manual_box_keeps_reported_points_when_shooting_is_inconsistent():
    from load_wayback_manual_box import SCOREBOOKS, export_player, parse_table

    values = ['<B>A.Iguodala</B>', '24:18', '3-5', '2-2', '1-2', '0', '4', '4', '5', '5', '0', '2', '0', '13']
    table = '<tr>' + ''.join(f'<td>{value}</td>' for value in values) + '</tr>'
    (player,) = parse_table(table)
    assert player.starter_mark and player.seconds == 1458 and player.pts == 13
    assert not player.shooting_reconciles()
    row = export_player(SCOREBOOKS[0], player, 2738, 1610612755, 12304, False, False)
    assert row[9] == 13 and row[14:20] == (None,) * 6


def test_box_identity_requires_reciprocal_matches_and_the_full_stat_line(monkeypatch):
    stats = {'minutes':'1', 'points':'0', 'fieldGoalsMade-fieldGoalsAttempted':'0-0',
             'threePointFieldGoalsMade-threePointFieldGoalsAttempted':'0-0', 'freeThrowsMade-freeThrowsAttempted':'0-0',
             'rebounds':'0','assists':'0','turnovers':'0','steals':'0','blocks':'0','offensiveRebounds':'0',
             'defensiveRebounds':'0','fouls':'0','plusMinus':'+2'}
    monkeypatch.setattr('load_espn.espn_athletes', lambda *_: [('1', 'Example', stats, None)])
    from load_espn import STAT_FIELDS, SHOOTING_FIELDS
    nba = {name: 0 for name in STAT_FIELDS.values()}
    nba.update({name: 0 for pair in SHOOTING_FIELDS.values() for name in pair})
    nba.update(minutes='0:59', plusMinusPoints=2.0)
    assert names_by_stat_line('game', True, [{'personId': 5, 'statistics': nba}]) == {5: 'Example'}
    assert names_by_stat_line('game', True, [{'personId': 5, 'statistics': nba}, {'personId': 6, 'statistics': nba}]) == {}
    assert names_by_stat_line('game', True, [{'personId': 5, 'statistics': {**nba, 'plusMinusPoints': -2}}]) == {}
    assert names_by_stat_line('game', True, [{'personId': 5, 'statistics': {**nba, 'plusMinusPoints': 2.1}}]) == {}


def test_espn_team_orientation_distinguishes_equal_score_games():
    def competition(home, away):
        return {'competitors': [{'homeAway': 'home', 'team': {'abbreviation': home}, 'score': '89'},
                                {'homeAway': 'away', 'team': {'abbreviation': away}, 'score': '91'}]}

    assert fixture_key(competition('CHI', 'TOR')) == ('CHI', 'TOR', 89, 91)
    assert fixture_key(competition('MIL', 'DAL')) == ('MIL', 'DAL', 89, 91)
    assert fixture_key(competition('TOR', 'CHI')) != fixture_key(competition('CHI', 'TOR'))


def test_espn_foreign_club_match_checks_numeric_id_and_designated_side():
    competition = {'competitors': [
        {'homeAway': 'home', 'team': {'abbreviation': 'TOR', 'id': '28'}, 'score': '100'},
        {'homeAway': 'away', 'team': {'abbreviation': 'SAN', 'id': '109850'}, 'score': '90'},
    ]}
    toronto, san_lorenzo = (1610612761, 'TOR', 100), (12330, None, 90)
    assert fixture_matches(competition, toronto, san_lorenzo)
    assert not fixture_matches(competition, san_lorenzo, toronto)
    competition['competitors'][1]['team']['id'] = '24'
    assert not fixture_matches(competition, toronto, san_lorenzo)


def test_usatoday_fixture_allows_only_verified_neutral_venue_reversal():
    from load_usatoday_preseason import fixture_sides

    fixture = ('2006-10-15', 2006, 1610612756, 1610612747, 'PHX', 'LAL', 99, 91, None)
    event = {
        'startDate': [{'type': 'Local', 'simplifiedFull': '2006-10-15'}],
        'season': {'year': 2006}, 'homeTeam': {'id': '13', 'abbreviation': 'LAL'},
        'awayTeam': {'id': '21', 'abbreviation': 'Pho'}, 'teams': [{'id': '13', 'score': 91}, {'id': '21', 'score': 99}],
        'venue': {'name': 'Thomas & Mack Center'},
    }
    assert fixture_sides(event, fixture) is None
    assert fixture_sides(event, fixture, neutral_arena='Thomas & Mack Center') == {'13': 1610612747, '21': 1610612756}
    event['teams'][0]['score'] = 99
    assert fixture_sides(event, fixture, neutral_arena='Thomas & Mack Center') is None


def test_odds_cutoffs_closing_conflicts_and_price_conversion():
    c = duckdb.connect()
    c.execute('''CREATE SCHEMA betting;
        CREATE TABLE betting.price_pair (game_id VARCHAR, source VARCHAR, book VARCHAR, market VARCHAR, handicap DOUBLE,
            observed_at TIMESTAMPTZ, tipoff_at TIMESTAMPTZ, phase VARCHAR, decimal_home_or_over DOUBLE, decimal_away_or_under DOUBLE);
        INSERT INTO betting.price_pair VALUES
          ('game','a','book','moneyline',NULL,'2026-11-24 17:00:00Z','2026-11-24 20:00:00Z','prematch',2.4,1.6),
          ('game','a','book','moneyline',NULL,'2026-11-24 19:58:00Z','2026-11-24 20:00:00Z','prematch',2.1,1.7),
          ('game','a','book','moneyline',NULL,'2026-11-24 19:58:00Z','2026-11-24 20:00:00Z','prematch',2.2,1.7),
          ('game','a','book','moneyline',NULL,'2026-11-24 19:59:00Z','2026-11-24 20:00:00Z','prematch',2,1.8),
          ('game','a','book','moneyline',NULL,'2026-11-24 20:00:00Z','2026-11-24 20:00:00Z','close',1.8,2),
          ('game','a','book','moneyline',NULL,NULL,'2026-11-24 20:00:00Z','close',1.7,2.2),
          ('game','b','book','moneyline',NULL,'2026-11-24 18:00:00Z','2026-11-24 20:00:00Z','intraday',2,1.8),
          ('game','b','book','moneyline',NULL,'2026-11-24 18:00:00Z','2026-11-24 20:00:00Z','intraday',2.2,1.7);''')
    views = (Path(__file__).resolve().parents[1] / 'betting_features.sql').read_text()
    c.execute(views.split('CREATE OR REPLACE VIEW betting.price_pair', 1)[0])
    c.execute('CREATE OR REPLACE VIEW betting.eligible_pregame_prices' + views.split('CREATE OR REPLACE VIEW betting.eligible_pregame_prices', 1)[1])
    assert c.execute("SELECT betting.decimal_price(-200,'american'), betting.decimal_price(140,'american'), betting.decimal_price(0,'american')").fetchone() == (1.5, 2.4, None)
    for clock, price in [('16:59', []), ('17:00', [(2.4,)]), ('19:58', [(2.4,)]), ('19:59', [(2.0,)]), ('20:00', [(2.0,)])]:
        assert c.execute("SELECT decimal_home_or_over FROM betting.odds_at('game', ?::TIMESTAMPTZ)",
                         [f'2026-11-24 {clock}:00Z']).fetchall() == price
    assert c.execute('SELECT source, round(clv_home_or_over, 3) FROM betting.closing_line_value').fetchall() == [('a', 0.2)]
