"""Link Kaggle odds feeds with documented game identities and market columns."""
from __future__ import annotations

from build_warehouse import q

MONEY_HISTORY = 'ehallmar_nba_historical_stats_and_betting_data__nba_betting_'
ODDS_HISTORY = 'archangel0x01_basketball_nba_sample__'
TEAMRANKINGS = ('chevronronson_nba_stats_dataset__csv_game_odds', 'chevronronson_nba_stats_dataset__sql_nba_stats_sqlite_game_odds')
PINNACLE = 'minmaxer_pinnacle_closing_lines_nba_nhl__pinnacle_closing_lines_nba_nhl_2026_10'


def build_kaggle(c) -> None:
    c.execute(r'''CREATE OR REPLACE TEMP MACRO kaggle_utc_time(source_time) AS (
        CASE WHEN typeof(source_time) = 'TIMESTAMP WITH TIME ZONE'
               OR regexp_matches(trim(source_time::VARCHAR), '[ T][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]+)?)? ?([zZ]|[+-][0-9]{2}(:?[0-9]{2})?)$')
             THEN try_cast(source_time AS TIMESTAMPTZ)
             ELSE try_cast(source_time AS TIMESTAMP) AT TIME ZONE 'UTC' END
    )''')
    c.execute('''CREATE OR REPLACE TABLE betting.kaggle_odds (
        source_table VARCHAR, source_fixture VARCHAR, game_id VARCHAR, match_status VARCHAR, book VARCHAR,
        market VARCHAR, handicap DOUBLE, home_or_over_price DOUBLE, away_or_under_price DOUBLE,
        price_format VARCHAR, observed_at TIMESTAMPTZ, phase VARCHAR, original_quote_time VARCHAR)''')
    available = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw_kaggle'").fetchall()}
    for market, suffix, line in (('moneyline', 'money_line', 'NULL'), ('spread', 'spread', '''
        CASE WHEN try_cast(spread1 AS DOUBLE) = -try_cast(spread2 AS DOUBLE)
             THEN try_cast(CASE WHEN g.home_team_id = try_cast(s.team_id AS BIGINT) THEN spread1 ELSE spread2 END AS DOUBLE) END'''),
        ('total', 'totals', 'CASE WHEN try_cast(total1 AS DOUBLE) = try_cast(total2 AS DOUBLE) THEN try_cast(total1 AS DOUBLE) END')):
        table = MONEY_HISTORY + suffix
        if table not in available:
            continue
        price1 = 'price1' if market == 'total' else 'CASE WHEN g.home_team_id = try_cast(s.team_id AS BIGINT) THEN price1 ELSE price2 END'
        price2 = 'price2' if market == 'total' else 'CASE WHEN g.home_team_id = try_cast(s.team_id AS BIGINT) THEN price2 ELSE price1 END'
        c.execute(f'''INSERT INTO betting.kaggle_odds
            SELECT ?, s.game_id, g.game_id, CASE WHEN g.game_id IS NULL THEN 'unmatched' ELSE 'matched' END,
                   s.book_name, ?, {line}, try_cast({price1} AS DOUBLE), try_cast({price2} AS DOUBLE),
                   'american', NULL, 'unknown', NULL
            FROM raw_kaggle.{q(table)} s LEFT JOIN core.dim_game g ON g.game_id = lpad(s.game_id, 10, '0')
                AND least(g.home_team_id,g.away_team_id) = least(try_cast(s.team_id AS BIGINT), try_cast(s.a_team_id AS BIGINT))
                AND greatest(g.home_team_id,g.away_team_id) = greatest(try_cast(s.team_id AS BIGINT), try_cast(s.a_team_id AS BIGINT))
                AND try_cast(s.team_id AS BIGINT) IS NOT NULL AND try_cast(s.a_team_id AS BIGINT) IS NOT NULL''', [table, market])
    for table in TEAMRANKINGS:
        if table not in available:
            continue
        c.execute(f'''INSERT INTO betting.kaggle_odds
            SELECT ?, s.game_id, g.game_id, CASE WHEN g.game_id IS NULL THEN 'unmatched' ELSE 'matched' END,
                   'teamrankings', 'moneyline', NULL, try_cast(s.decimal_home AS DOUBLE), try_cast(s.decimal_away AS DOUBLE),
                   'decimal', CASE WHEN regexp_matches(s.odds_date, '(Z|[+-][0-9]{{2}}:[0-9]{{2}})$')
                                   THEN try_cast(s.odds_date AS TIMESTAMPTZ) END,
                   CASE WHEN s.odds_date IN ('open','close') THEN s.odds_date ELSE 'intraday' END, s.odds_date
            FROM raw_kaggle.{q(table)} s LEFT JOIN core.dim_game g ON g.game_id = lpad(s.game_id, 10, '0')
                AND g.game_date = try_cast(s.game_date AS DATE)''', [table])
    for suffix in ('', '_2'):
        fixtures = ODDS_HISTORY + 'matches' + suffix
        if fixtures not in available:
            continue
        c.execute(f'''CREATE OR REPLACE TEMP TABLE kaggle_fixture AS
            WITH matches AS (
                SELECT DISTINCT match_id::VARCHAR AS match_id, home_team, away_team, home_score, away_score,
                    kaggle_utc_time(tipoff_utc) AS tipoff_at
                FROM raw_kaggle.{q(fixtures)} WHERE competition = 'National Basketball Association'
            ), candidates AS (
                SELECT DISTINCT m.match_id, g.game_id FROM matches m
                JOIN team_name home ON home.name = lower(trim(m.home_team))
                JOIN team_name away ON away.name = lower(trim(m.away_team))
                JOIN core.dim_game g ON g.home_team_id = home.team_id AND g.away_team_id = away.team_id
                    AND g.game_date = (m.tipoff_at AT TIME ZONE 'America/New_York')::DATE
                    AND g.season_start + 1 BETWEEN home.valid_from AND home.valid_to + 1
                    AND g.season_start + 1 BETWEEN away.valid_from AND away.valid_to + 1
                JOIN core.fact_game_result r ON r.game_id = g.game_id
                    AND r.home_score = try_cast(m.home_score AS INTEGER) AND r.away_score = try_cast(m.away_score AS INTEGER)
            )
            SELECT match_id, CASE WHEN count(*) = 1 THEN min(game_id) END AS game_id,
                CASE WHEN count(*) = 1 THEN 'matched' ELSE 'ambiguous' END AS status
            FROM candidates GROUP BY match_id''')
        for market, table_market, line, first, second in (
            ('moneyline', 'moneyline', 'NULL', 'home_odds', 'away_odds'),
            ('spread', 'spread', 'try_cast(line AS DOUBLE)', 'home_odds', 'away_odds'),
            ('total', 'totals', 'try_cast(line AS DOUBLE)', 'over_odds', 'under_odds'),
        ):
            table = ODDS_HISTORY + f'odds_{table_market}' + suffix
            if table not in available:
                continue
            c.execute(f'''INSERT INTO betting.kaggle_odds
                SELECT ?, s.match_id::VARCHAR, f.game_id, coalesce(f.status, 'unmatched'), s.bookmaker, ?, {line},
                       try_cast(s.{first} AS DOUBLE), try_cast(s.{second} AS DOUBLE), 'decimal',
                       kaggle_utc_time(s.quote_time_utc), s.phase, s.quote_time_utc::VARCHAR
                FROM raw_kaggle.{q(table)} s LEFT JOIN kaggle_fixture f ON f.match_id = s.match_id::VARCHAR''', [table, market])
    if PINNACLE in available:
        load_pinnacle(c)
    for source, linked in c.execute('SELECT source_table, count(*) FROM betting.kaggle_odds GROUP BY 1').fetchall():
        if source == PINNACLE:
            raw = c.execute(f"SELECT 6 * count(*) FROM raw_kaggle.{q(source)} WHERE league = 'NBA'").fetchone()[0]
        else:
            raw = c.execute(f'SELECT count(*) FROM raw_kaggle.{q(source)}').fetchone()[0]
        if linked != raw:
            raise ValueError(f'Kaggle row conservation failed for {source}: {linked} vs {raw}')
    c.execute('''CREATE OR REPLACE VIEW betting.kaggle_match_summary AS
        SELECT source_table, market, match_status, count(*) AS rows, count(DISTINCT game_id) AS games
        FROM betting.kaggle_odds GROUP BY ALL''')


def load_pinnacle(c) -> None:
    table = f'raw_kaggle.{q(PINNACLE)}'
    c.execute(f'''CREATE OR REPLACE TEMP TABLE pinnacle_fixture AS
        WITH candidates AS (
            SELECT DISTINCT p.game_id AS source_fixture, g.game_id FROM {table} p
            JOIN team_name h ON h.name = lower(trim(p.home_team))
            JOIN team_name a ON a.name = lower(trim(p.away_team))
            JOIN core.dim_game g ON g.home_team_id = h.team_id AND g.away_team_id = a.team_id
                AND g.game_date = (try_cast(p.start_time_utc AS TIMESTAMPTZ) AT TIME ZONE 'America/New_York')::DATE
                AND abs(epoch(g.game_datetime_est AT TIME ZONE 'America/New_York') - epoch(try_cast(p.start_time_utc AS TIMESTAMPTZ))) <= 1800
            WHERE p.league = 'NBA'
        ), unique_matches AS (
            SELECT source_fixture, min(game_id) AS game_id FROM candidates GROUP BY 1 HAVING count(*) = 1
        )
        SELECT source_fixture, game_id FROM unique_matches QUALIFY count(*) OVER (PARTITION BY game_id) = 1''')
    for market, sides in (('moneyline', ('home', 'away')), ('spread', ('home', 'away')), ('total', ('over', 'under'))):
        for phase in ('open', 'close'):
            prefix = f'{market}_{phase}'
            points = f'try_cast(s.{prefix}_line AS DOUBLE)' if market != 'moneyline' else 'NULL'
            c.execute(f'''INSERT INTO betting.kaggle_odds
                SELECT ?, s.game_id, f.game_id, CASE WHEN f.game_id IS NULL THEN 'unmatched' ELSE 'matched' END,
                    'pinnacle', ?, {points}, try_cast(s.{prefix}_{sides[0]} AS DOUBLE), try_cast(s.{prefix}_{sides[1]} AS DOUBLE),
                    'decimal', CASE WHEN regexp_matches(s.{prefix}_at, '(Z|[+-][0-9]{{2}}:[0-9]{{2}})$')
                                    THEN try_cast(s.{prefix}_at AS TIMESTAMPTZ) END, ?, s.{prefix}_at
                FROM {table} s LEFT JOIN pinnacle_fixture f ON f.source_fixture = s.game_id WHERE s.league = 'NBA' ''',
                [PINNACLE, market, phase])
