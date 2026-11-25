# nba-warehouse

A personal, locally built NBA / BAA / ABA database: every game since November 1946 with final
scores, quarter scores, team and player box scores, play-by-play and shot charts (1996-97 onward),
plus draft, awards, coaches, rosters, referees, lineups, transactions, salaries and betting lines.

**This repository holds the code and documentation only.** The database itself (a single DuckDB
file, `data/nba-warehouse.duckdb`) is built locally from sources whose terms do not allow
redistribution, so it is not published here.

## Layout of the database

One file, four layers:

| Schema | What it holds | Rule |
|---|---|---|
| `raw`, `raw_kaggle` | Every imported table exactly as received: the original snapshot, every Kaggle dataset, scraped pages, daily referee assignments | Never edited after load |
| `core` | Verified, matched records you query | Every row's keys resolve to `core` dimensions; nothing unmatched |
| `audit` | Why records are held, every competing value, source receipts, release history | Nothing is dropped silently |
| `betting` | Odds rows linked to `core` games and teams | Nothing computed yet (see issues) |

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
# 1. single warehouse from the original snapshot + the last historical release + current season
uv run --no-project --with duckdb==2.0.0 python warehouse/build_warehouse.py build
uv run --no-project --with duckdb==2.0.0 python warehouse/build_warehouse.py verify

# 2. Kaggle datasets (needs KAGGLE_USERNAME / KAGGLE_KEY)
warehouse/fetch_kaggle.sh
uv run --no-project --with duckdb==2.0.0 python warehouse/load_kaggle.py

# 3. betting links, non-game domains
uv run --no-project --with duckdb==2.0.0 python warehouse/betting.py
uv run --no-project --with duckdb==2.0.0 python warehouse/promote_domains.py
```

### Daily refresh

`warehouse/daily_refresh.py` runs at 06:00 Eastern (launchd, retried at 09:00 and 12:00). It loads
the previous day's games, runs every check on a copy-on-write clone of the warehouse, and replaces
the warehouse only if all checks pass. Failures leave the warehouse unchanged and write a report
to `data/refresh-reports/`. It also snapshots the day's referee assignments.

## History

The warehouse grew out of 26 incremental releases (`nba-clean-v1` … `v26`), documented in
[warehouse/README.md](warehouse/README.md) and [the research note](docs/research/nba-warehouse-cleanup.md).
Those files were retired once the single warehouse was verified; `audit.legacy_release_fingerprint`
keeps each one's per-table row counts and content hashes, and `audit.legacy_release_file` its SHA-256.

## Sources and terms

Data comes from free sources used for personal research at polite request rates: NBA.com statistics
and official PDFs, archived nba.com pages (Wayback Machine), Library of Congress newspaper scans,
Basketball-Reference (≥ 6.5 s between requests, robots.txt respected), SportsDataverse releases and
public Kaggle datasets. Sites that refuse automated agents (e.g. via robots.txt or bot protection)
are not used. Each source's own terms apply to the data; this repository's license covers the code.
