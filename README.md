# nba-warehouse

A personal, locally built NBA / BAA / ABA database: every game since November 1946 with final
scores, quarter scores, team and player box scores, play-by-play and shot charts (1996-97 onward),
plus draft, awards, coaches, rosters, referees, lineups, transactions, salaries and betting lines.

**This repository holds the code and documentation only.** The database itself (a single DuckDB
file, `data/nba-warehouse.duckdb`, ~45 GB) is built locally from sources whose terms do not allow
redistribution, so it is not published here. `data/` is a symlink to
`~/Library/Application Support/NBA Warehouse/data` (outside `~/Documents`, so the scheduled jobs can
read it without extra macOS permissions).

## What's in `core`

| Area | Tables | Coverage |
|---|---|---|
| Games, results, standings | `dim_game`, `fact_game_result`, `fact_regular_standings` | every NBA/BAA game since Nov 1946; current season refreshed daily |
| Quarter scores | `fact_game_period_score` | ~98% of games; gaps chased nightly |
| Box scores | `fact_team_game_stats`, `fact_player_game_stats` | >99.8% of games |
| Play-by-play, shots | `fact_play_event`, `fact_shot`, `field_goal_attempt` | 1996-97 onward (not recorded before) |
| ABA (1967-76) | `aba_game`, `aba_game_result`, `aba_regular_standings`, `aba_team_game_stats`, `aba_player_game_stats` | all 4,149 games; boxes as they are collected |
| People and teams | `dim_person`, `person_source_id`, `dim_team`, `team_source_id` | permanent ids; crosswalks to Basketball-Reference and other sources |
| Draft, combine, awards | `fact_draft_pick`, `fact_draft_combine`, `fact_award_vote`, `fact_player_honor`, `fact_all_star_selection`, `fact_player_award` | 1947-2025 |
| Rosters, transactions | `fact_roster_spell` (dated), `bridge_player_team_season`, `fact_transaction`, `bridge_transaction_party` | all seasons; NBA.com moves from 2015 |
| Salaries | `fact_player_salary` | 1984-85 to 2025-26 |
| Coaches, referees, lineups | `fact_coach_season`, `fact_game_official`, `fact_starting_lineup`, `fact_inactive_player`, `fact_rotation_stint` | see `audit.domain_coverage` |
| Tracking | `fact_tracking_season`, `fact_player_season_estimated`, `fact_season_matchup` | 2013-14 onward (not tracked before) |

Every promoted area has a row in `audit.domain_disposition` (source rows = published + duplicate +
held) and held rows in `audit.domain_held` with a reason. `audit.game_coverage` / `audit.domain_coverage`
label every game x area as present, partial, missing_recoverable, source_unavailable, disputed,
not_tracked or not_applicable; `audit.open_gaps` lists what is still being chased.

Identity judgments that rules could not settle (same-name players across sources) were made with
TypeSafe Jev (`warehouse/judge_identities.py`); only confident one-to-one "same person" verdicts were
linked, and every verdict with its probabilities is in `audit.jev_identity_judgment`.

## Layout of the database

One file, four layers:

| Schema | What it holds | Rule |
|---|---|---|
| `raw`, `raw_kaggle` | Every imported table exactly as received: the original snapshot, every Kaggle dataset, scraped pages, daily referee assignments | Never edited after load |
| `core` | Verified, matched records you query | Every row's keys resolve to `core` dimensions; nothing unmatched |
| `audit` | Why records are held, every competing value, source receipts, release history | Nothing is dropped silently |
| `betting` | Odds, fixture links, pregame price pairs and closing-line comparisons | Quotes need a verified timestamp and tip-off for pregame use |

**Definition of done:** every expected record is either verified or labelled — `not_tracked`,
`source_unavailable`, `disputed` or `not_applicable` — in `audit`. A gap is given up after two
independent sources fail.

**When sources disagree,** `core` takes the highest-ranked source and `audit` keeps every value:
1. Official NBA scorer reports and gamebooks
2. NBA.com statistics data
3. Archived nba.com history pages and contemporary newspapers
4. Basketball-Reference
5. Everything else

A value is held only when two sources of the same rank disagree and nothing higher exists.

**Leagues:** NBA and BAA by default (the NBA's official record book). ABA rows carry
`league = 'ABA'`; the `*_all_leagues` views include them.

## Building it

Requires [uv](https://docs.astral.sh/uv/) and DuckDB 2.0.0 (pulled in by `uv`).

```sh
# 1. one-time migration (already done; its inputs - the original snapshot and releases v1-v26 - were
#    retired after verification, see "History")
uv run --no-project --with duckdb==2.0.0 python warehouse/build_warehouse.py build

# 2. Kaggle datasets (needs KAGGLE_USERNAME / KAGGLE_KEY)
warehouse/fetch_kaggle.sh
uv run --no-project --with duckdb==2.0.0 python warehouse/load_kaggle.py

# 3. betting links, non-game domains
uv run --no-project --with duckdb==2.0.0 python warehouse/betting.py
uv run --no-project --with duckdb==2.0.0 python warehouse/promote_domains.py

# 4. new sources: Basketball-Reference pages (paced), NBA.com tracking, then promotion
python3 warehouse/crawl_bref.py transactions aba_schedule
uv run --no-project --with nba_api --with duckdb==2.0.0 python warehouse/fetch_nba_tracking.py fetch
uv run --no-project --with nba_api --with duckdb==2.0.0 python warehouse/fetch_nba_tracking.py load
uv run --no-project --with duckdb==2.0.0 python warehouse/load_bref.py
uv run --no-project --with duckdb==2.0.0 python warehouse/promote_new_sources.py
uv run --no-project --with duckdb==2.0.0 python warehouse/coverage.py
```

### Gap chasing

`warehouse/chase_gaps.py all` runs nightly at 01:00 (launchd): it works through games without quarter
scores via Basketball-Reference box pages (admitting a line score only when it is the same fixture, sums
to the official final and is tied before any overtime), caches ABA box pages, then loads and promotes
them and refreshes the coverage ledger. It is resumable, one request every 6.5 s, and stops on any block.
Install both scheduled jobs with `warehouse/launchd/install.sh`.

### Daily refresh

`warehouse/daily_refresh.py` runs at 06:00 Eastern (launchd, retried at 09:00 and 12:00). It loads
the previous day's games, runs every check on a copy-on-write clone of the warehouse, and replaces
the completed-game release only if all checks pass. Failures write a report to `data/refresh-reports/`.
The job also snapshots the day's referee assignments. It publishes NBA injury reports, upcoming fixtures and
captured rosters even when the completed-game release is delayed.

### Pregame queries

Use a timezone-bearing cutoff, such as `2026-11-25T16:00:00-05:00`, with `betting.odds_at(game_id, cutoff)`,
`core.injury_report_at(game_id, cutoff)` and `core.roster_at(game_id, cutoff)`. `core.pregame_fixture` includes official
schedule entries ahead of the results refresh. `core.injury_report_final_pregame` adds the separately observed participation
status after the game. The as-of injury query links identities through rosters available at that cutoff. The final report
can use the later game box. `audit.injury_report_resolution` records rows still missing a verified game, team or person match.
`core.pregame_fixture_at(game_id, cutoff)` uses versioned schedule captures and the original August 2025 NBA
release for exactly matched 2025–26 fixtures. Unlinked schedule entries remain in `audit.nba_initial_schedule_resolution`.

`betting.price_pair` covers full-game moneyline, spread and totals. It exposes decimal prices, implied probabilities,
overround and two-sided vig removal, with original values in the source tables. `betting.closing_price` uses the last
unambiguous quote strictly before the scheduled tip-off for the same source, book, market and points line.
`betting.closing_line_value` is an after-the-fact comparison against that quote. Publisher-labelled opens and closes with
no quote time are visible through `betting.reported_open_close`. The original Pinnacle files lack a verified timezone,
so their prices remain visible in `betting.price_pair` and are withheld from the cutoff queries.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/injury_reports.py mirror
uv run --no-project --with duckdb==2.0.0 python warehouse/injury_reports.py rosters
uv run --no-project --with duckdb==2.0.0 python warehouse/load_nba_roster_releases.py
uv run --no-project --with duckdb==2.0.0 python warehouse/load_nba_schedule_releases.py
uv run --no-project --with duckdb==2.0.0 python warehouse/injury_reports.py backfill --from-date 2018-12-17 --through 2019-06-14
uv run --no-project --with duckdb==2.0.0 python warehouse/injury_reports.py indexed-pdfs --from-date 2021-07-01 --through 2026-06-30
uv run --no-project --with duckdb==2.0.0 python warehouse/betting.py
```

Official PDF capture uses Poppler's `pdftotext`. `indexed-pdfs` uses the downloaded seasonal mirror tables to
find original PDF filenames. `archive-pdfs --index PATH` accepts a local official and Wayback PDF catalogue.
`audit-archive --index PATH` checks which catalogue filenames have parsed rows. `cached` replays downloaded
originals, mirror copies and archive captures within the selected date range.
Historical and daily source coverage is documented in the
[research note](docs/research/gap-sources.md).

## History

The warehouse grew out of 26 incremental releases (`nba-clean-v1` … `v26`), documented in
[warehouse/README.md](warehouse/README.md) and [the research note](docs/research/nba-warehouse-cleanup.md).
Those files were retired once the single warehouse was verified; `audit.legacy_release_fingerprint`
keeps each one's per-table row counts and content hashes, and `audit.legacy_release_file` its SHA-256.

## Sources and terms

Data comes from free sources used for personal research at polite request rates: NBA.com statistics
and official PDFs, archived nba.com pages (Wayback Machine), Library of Congress newspaper scans,
Basketball-Reference (≥ 6.5 s between requests, robots.txt respected), SportsDataverse releases and
328 public Kaggle datasets (list in `warehouse/kaggle_catalog.csv`). HoopsHype's robots.txt refuses AI
agents, so it is not crawled; HoopsHype figures appear only via a public Kaggle dataset. Sites that refuse automated agents (e.g. via robots.txt or bot protection)
are not used. Each source's own terms apply to the data; this repository's license covers the code.
