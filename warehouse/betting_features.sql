CREATE OR REPLACE MACRO betting.decimal_price(price, style) AS
    CASE WHEN NOT isfinite(price) THEN NULL
         WHEN style = 'decimal' AND price > 1 THEN price
         WHEN style = 'american' AND price >= 100 THEN 1 + price / 100.0
         WHEN style = 'american' AND price <= -100 THEN 1 + 100.0 / abs(price) END;

CREATE OR REPLACE VIEW betting.price_pair AS
WITH teamrankings AS (
    SELECT 'odds_snapshot' AS source, 'teamrankings' AS book, game_id, 'moneyline' AS market, NULL::DOUBLE AS handicap,
           CASE WHEN regexp_matches(odds_date, '(Z|[+-][0-9]{2}:[0-9]{2})$') THEN try_cast(odds_date AS TIMESTAMPTZ) END AS observed_at,
           odds_phase AS phase, odds_date AS original_quote_time,
           coalesce(betting.decimal_price(decimal_home, 'decimal'), betting.decimal_price(moneyline_home, 'american')) AS decimal_home_or_over,
           coalesce(betting.decimal_price(decimal_away, 'decimal'), betting.decimal_price(moneyline_away, 'american')) AS decimal_away_or_under
    FROM betting.odds_snapshot WHERE match_status = 'matched'
), pinnacle_main AS (
    SELECT 'main_line', 'pinnacle', m.game_id, x.market, x.handicap, NULL::TIMESTAMPTZ,
           'timestamp_zone_unknown', m.snapshot_ts::VARCHAR,
           betting.decimal_price(x.first_price, 'decimal'), betting.decimal_price(x.second_price, 'decimal')
    FROM betting.main_line m JOIN core.dim_game g USING (game_id)
    CROSS JOIN LATERAL (VALUES
        ('moneyline', NULL, if(m.matched_team1_id = g.home_team_id, m.team1_moneyline, m.team2_moneyline),
                            if(m.matched_team1_id = g.home_team_id, m.team2_moneyline, m.team1_moneyline)),
        ('spread', if(m.matched_team1_id = g.home_team_id, m.team1_spread, m.team2_spread),
                   if(m.matched_team1_id = g.home_team_id, m.team1_spread_odds, m.team2_spread_odds),
                   if(m.matched_team1_id = g.home_team_id, m.team2_spread_odds, m.team1_spread_odds)),
        ('total', m.over_total, m.over_total_odds, m.under_total_odds)
    ) AS x(market, handicap, first_price, second_price)
    WHERE m.match_status = 'matched' AND (x.market = 'moneyline' OR
        (x.market = 'spread' AND m.team1_spread = -m.team2_spread) OR
        (x.market = 'total' AND m.over_total = m.under_total))
), team_names AS (
    SELECT team_id, lower(trim(display_name)) AS name FROM core.dim_team
    UNION SELECT team_id, lower(trim(full_name)) FROM core.dim_team_version
    UNION SELECT 1610612746, 'los angeles clippers'
), selections AS (
    SELECT m.game_id, m.snapshot_ts, CASE m.market WHEN 'Money Line – Game' THEN 'moneyline' ELSE 'spread' END AS market,
           t.team_id = g.home_team_id AS first_side,
           try_cast(regexp_extract(m.selection, ' ([+-]?[0-9]+[.]?[0-9]*)$', 1) AS DOUBLE) AS pts,
           betting.decimal_price(m.odds, 'decimal') AS price
    FROM betting.market_odds m JOIN core.dim_game g USING (game_id)
    JOIN team_names t ON t.team_id IN (g.home_team_id, g.away_team_id)
                     AND t.name = lower(trim(regexp_replace(m.selection, ' [+-][0-9.]+$', '')))
    WHERE m.match_status = 'matched' AND m.market IN ('Money Line – Game', 'Handicap – Game')
    UNION ALL
    SELECT m.game_id, m.snapshot_ts, 'total', lower(split_part(m.selection, ' ', 1)) = 'over',
           try_cast(regexp_extract(m.selection, ' ([+-]?[0-9]+[.]?[0-9]*)$', 1) AS DOUBLE), betting.decimal_price(m.odds, 'decimal')
    FROM betting.market_odds m WHERE m.match_status = 'matched' AND m.market = 'Total – Game'
      AND lower(split_part(m.selection, ' ', 1)) IN ('over', 'under')
), pinnacle_detail AS (
    SELECT 'market_odds', 'pinnacle', game_id, market,
           CASE WHEN market = 'spread' AND NOT first_side THEN -pts WHEN market <> 'moneyline' THEN pts END AS handicap,
           NULL::TIMESTAMPTZ, 'timestamp_zone_unknown', snapshot_ts::VARCHAR,
           min(price) FILTER (WHERE first_side), min(price) FILTER (WHERE NOT first_side)
    FROM selections GROUP BY ALL
    HAVING count(DISTINCT price) FILTER (WHERE first_side) = 1 AND count(DISTINCT price) FILTER (WHERE NOT first_side) = 1
), kaggle AS (
    SELECT source_table, book, game_id, market, handicap, observed_at, phase, original_quote_time,
           betting.decimal_price(home_or_over_price, price_format), betting.decimal_price(away_or_under_price, price_format)
    FROM betting.kaggle_odds WHERE match_status = 'matched'
), pairs AS (
    SELECT * FROM teamrankings UNION ALL SELECT * FROM pinnacle_main UNION ALL SELECT * FROM pinnacle_detail UNION ALL SELECT * FROM kaggle
)
SELECT DISTINCT p.*, g.game_datetime_est AT TIME ZONE 'America/New_York' AS tipoff_at,
       1 / decimal_home_or_over AS implied_home_or_over, 1 / decimal_away_or_under AS implied_away_or_under,
       1 / decimal_home_or_over + 1 / decimal_away_or_under - 1 AS overround,
       decimal_away_or_under / (decimal_home_or_over + decimal_away_or_under) AS fair_home_or_over,
       decimal_home_or_over / (decimal_home_or_over + decimal_away_or_under) AS fair_away_or_under
FROM pairs p JOIN core.dim_game g USING (game_id)
WHERE decimal_home_or_over IS NOT NULL AND decimal_away_or_under IS NOT NULL
  AND (market = 'moneyline' OR isfinite(handicap));

CREATE OR REPLACE VIEW betting.eligible_pregame_prices AS
SELECT *, count(*) OVER (PARTITION BY game_id, source, book, market, handicap, observed_at) AS quotes_at_timestamp
FROM betting.price_pair
WHERE observed_at < tipoff_at AND phase IN ('prematch', 'intraday', 'open', 'close');

CREATE OR REPLACE MACRO betting.odds_at(fixture, known_at) AS TABLE
SELECT * EXCLUDE (quotes_at_timestamp) FROM betting.eligible_pregame_prices
WHERE game_id = fixture AND observed_at <= known_at AND quotes_at_timestamp = 1
QUALIFY observed_at = max(observed_at) OVER (PARTITION BY source, book, market, handicap);

CREATE OR REPLACE VIEW betting.closing_price AS
SELECT * EXCLUDE (quotes_at_timestamp) FROM betting.eligible_pregame_prices
WHERE quotes_at_timestamp = 1
QUALIFY observed_at = max(observed_at) OVER (PARTITION BY game_id, source, book, market, handicap);

-- Source-labeled open/close rows without a quote time are reference data only.
CREATE OR REPLACE VIEW betting.reported_open_close AS
SELECT * FROM betting.price_pair WHERE phase IN ('open', 'close');

-- Ex-post comparison for the same book, market and points line.
CREATE OR REPLACE VIEW betting.closing_line_value AS
SELECT p.game_id, p.source, p.book, p.market, p.handicap, p.observed_at, c.observed_at AS closing_at,
       p.decimal_home_or_over / c.decimal_home_or_over - 1 AS clv_home_or_over,
       p.decimal_away_or_under / c.decimal_away_or_under - 1 AS clv_away_or_under
FROM betting.eligible_pregame_prices p JOIN betting.closing_price c
  ON p.game_id = c.game_id AND p.source = c.source AND p.book = c.book AND p.market = c.market
 AND p.handicap IS NOT DISTINCT FROM c.handicap AND p.observed_at < c.observed_at
WHERE p.quotes_at_timestamp = 1;
