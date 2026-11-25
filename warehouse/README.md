> **Superseded.** These release notes describe the incremental releases v1-v26 that preceded the single
> warehouse. Those files were retired on 2026-11-24 after verification; their contents live on in
> `data/nba-warehouse.duckdb` (core = v26 + later additions, audit = every ledger) and each file's
> per-table hashes are in `audit.legacy_release_fingerprint`. The commands below no longer run as written.

# NBA warehouse: games, box scores, periods, plays, and shots

**Latest historical build:** `../data/nba-clean-v26.duckdb` (DuckDB 2.0.0, personal/local use). This release adds **90 period rows across 11 October 2006 preseason games**, including Toronto–Boston overtime. **73,610 of 75,268** games have accepted periods, leaving **1,658** gaps (**1,431** before 1960). **119 of 120** 2006–07 preseason games have periods; Efes Pilsen at Golden State (`0010600026`) remains open. Use `audit.period_availability_v26` and `audit.preseason_period_receipt_v26`. The original pages are local evidence, not cleared for redistribution.

```sh
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/recover_preseason_periods_v26.py verify-inputs --output data/nba-clean-v26.duckdb
```

V25 added 24 quarter cells across three October 2006 preseason games; five original pages, including two unchanged controls, are recorded in `audit.preseason_period_receipt_v25`.

V24 added 20 played player rows for the 1954 All-Star Game (`0035300004`). Reported minutes are whole-minute observations and Ray Felix's disputed foul count remains NULL. Use `audit.allstar_player_receipt_v24` and `audit.player_box_availability_v24`; ten historical games still have no player box.

V23 added 16 rows for `0024800149` and `0024900027` by subtracting four cumulative boundaries printed by the *Evening Star*. The original boundary vectors and page/scan locators are in `audit.archive_period_receipt_v23`.

V22 checked seven dated Basketball-Reference pages and added 16 quarter rows after confirming crossed local payloads for `0025100086` and `0025200313`. Five other fixtures remain disputed in `audit.bref_period_receipt_v22`.

## Current season through November 23, 2026 (Eastern)

`../data/nba-2026-27-local-2026-11-23.duckdb` contains **258 regular-season finals**, including **37 Cup group games**; **2,094 team-period cells**, **516 team boxes**, **5,772 played and 1,062 DNP lines**, **128,048 plays** and **45,452 play-by-play field-goal attempts**. Preseason is not in this snapshot. Nine unmapped DNP rows and one participation contradiction are retained in the audit layer. One entire PBP stream, `0022600272`, is held because all four ESPN boundaries understate Minnesota by two points against the scorer report. The result, periods and boxes remain admitted. Player minutes are source-rounded observations only (`reported_num_minutes`); exact player minutes are not filled.

SportsDataverse republishes upstream ESPN observations. Its box/period/PBP/shot files are not independent sources. `audit.team_scorer_sample` compares 128 field cells against the prior four-game official sample. `audit.shot_box_conflict` records 258 player-game comparisons with different individual field-goal counts. `audit.shot_coordinates` retains 45,617 source-coordinate observations with original ESPN play IDs, including the held game's evidence and 16 ambiguous origins. They are not mixed with the historical NBA coordinate system. The artifacts are for local research; no redistribution or product use is cleared. The pipeline makes no live calls to ESPN or the NBA.

`verify_current_scorer_discrepancy.py` replays the pinned NBA gamebook for `0022600272`. It finds one missing ESPN score change (LaMelo Ball at Q1 `10:19`). The other 98 score changes match in order at period/clock/side/points grain. `audit_current_shot_attempts.py` also reconciles all 163 shared player-credited attempts and the two team heaves reported separately from player FGA in the official box. ESPN's individual attribution of the heaves to Mikal Bridges and Trey Lyles explains the two remaining shot-box conflicts. The private shot receipt is audit-only; it does not verify non-field-goal actions or coordinates, and the canonical stream stays held.

`audit_current_nonshot_actions.py` then compares free throws, turnovers with linked steals, rebounds, and substitutions. It finds one missing ESPN turnover/steal, one player-rebound attribution conflict, and widespread rebound clock differences despite shared missed-shot anchors. It verifies the 12 ESPN period-opening substitutions against the printed NBA lineups without counting them as in-quarter substitutions. This receipt is also audit-only; fouls, reviews and other administration actions have not yet been reconciled.

The combined query's `nba.fact_regular_standings` adds `snapshot_kind` and `as_of_date`; current rows carry the cutoff, while historical rows have no snapshot cutoff. `nba.current_snapshot_scope` exposes the same date and feed scope.

```sh
A="$HOME/Library/Application Support/NBA Warehouse/evidence/sdv-2027-2026-11-24"
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/refresh_current_season.py verify-inputs --assets "$A" --cutoff 2026-11-23 --output data/nba-2026-27-local-2026-11-23.duckdb
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/query_with_current.py --current data/nba-2026-27-local-2026-11-23.duckdb 'SELECT season_year, count(*) FROM nba.fact_game GROUP BY 1 ORDER BY 1 DESC LIMIT 3'
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/verify_current_scorer_discrepancy.py
E="$HOME/Library/Application Support/NBA Warehouse/evidence/nba-official-2026-11-19-NYKMIN"
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/audit_current_shot_attempts.py --verify-receipt "$E/shot-attempt-reconciliation.json"
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/audit_current_nonshot_actions.py --verify-receipt "$E/nonshot-action-reconciliation-v2.json"
```

For the next completed Eastern date, use `warehouse/capture_current_assets.py --output <new-local-evidence-directory>` to capture and digest-check the eight public release assets, then run `refresh_current_season.py build` with the new cutoff, new output, and `--previous-release` pointing at the latest verified season release. Pass the same arguments to `verify-inputs` to replay offline. No background polling is configured. A correction to an already admitted final stops the build for adjudication rather than silently overwriting the old record.

## First paced period pass (v21)

`../data/nba-clean-v21.duckdb` examined four dated box pages and admitted **16 period rows**. One page disagreed on the designated home side and one disagreed with a local quarter cell; neither was admitted. **73,592 of 75,268** games had accepted periods. Historical ledger: `audit.period_availability_v21`.

```sh
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/recover_bref_periods_v21.py verify-inputs --output data/nba-clean-v21.duckdb
```

The collector `warehouse/fetch_bref_periods.py` accepts explicitly keyed period-gap queues, caches original pages, checks `robots.txt`, spaces requests by at least 6.5 seconds in one process at a time, and stops on blocks. `--offline` replays cached pages without requests. Parsed matches are candidates until admitted by a separate release gate.

## Keyed play and shot recovery (v20)

`../data/nba-clean-v20.duckdb` restored **842 original play events and 301 linked chart attempts** across two games. All 1,230 regular games in each of 2006–07 and 2007–08 now have events and shots. Query `nba.shot_event_link` for both the older locator-matched links and new links on the original `GAME_EVENT_ID`. Of the recovered shots, 88 have an ambiguous zero-origin; they have no published coordinates. The original archives are pinned in `audit.event_archive_v20` and are not cleared for redistribution.

```sh
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/recover_event_archive_v20.py verify-inputs --archive-dir "$HOME/.hermes/cache/scratch/nba-ev" --output data/nba-clean-v20.duckdb
```

## Final-score and period adjudications (v19)

`../data/nba-clean-v19.duckdb` corrects **three playoff finals** and adds **40 period-score rows across five games**. The winners and standings are unchanged. **73,590 of 75,268** games have accepted period scores; **1,678** do not. The five receipts in `audit.period_adjudication_v19` retain the old result and the conflicting NBA page header separately from the printed quarter values. Use `audit.period_availability_v19` for current coverage.

```sh
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/recover_period_adjudication_v19.py verify-inputs --output data/nba-clean-v19.duckdb
```

## Legacy regular-season attempts (v18)

`../data/nba-clean-v18.duckdb` adds **1,745 action-grain attempts across 11 games**. All **1,189** games in the **2000–01** regular season now have attempts in `nba.field_goal_attempt`; 1,178 still have charts. The 11-game key list is `audit.legacy_attempt_scope_v18`. The new attempts retain NBA action numbers, padded source labels, and original clocks; geometry is unavailable, and chart and link relations are unchanged.

```sh
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/recover_legacy_attempts_v18.py verify-inputs --output data/nba-clean-v18.duckdb
```

## Team and player box recovery (v17)

`../data/nba-clean-v17.duckdb` adds **20 partial team boxes** across 10 games and **88 traditional player boxes** across four All-Star games. All 1,230 regular-season games in each of 2024–25 and 2025–26 now have both team rows. The team fact covers **150,257 of 150,536** expected rows; **11 games** still have no player box.

`audit.team_box_recovery_v17` records team-total and player-line turnovers separately and identifies the source of each field group. The new team rows use traditional values from `fact_box_score_team` (`min` is five times the elapsed game minutes) and six situational fields from `stg_kaggle_team_box`; unsupported fields remain NULL. `audit.player_box_recovery_v17` holds 24 player rows from the 2004 All-Star game over conflicting minutes and four unnamed placeholders across two games. `audit.player_box_availability_v17` accounts for all 15 original zero-player-box games in this pass. These are local, same-source-family reconciliations, not independently verified scorer reports.

```sh
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/recover_boxes_v17.py verify-inputs --output data/nba-clean-v17.duckdb
```

## Playoff overtime flags and modern attempts (v16)

`../data/nba-clean-v16.duckdb` adds period scores for **199** games and **6,564** play-by-play attempts across **38** games. `nba.field_goal_attempt` has observations for all 2024–25 regular-season and 2025–26 playoff games on the spine; added attempts retain the original action and clock but have no coordinates. `nba.fact_shot` is unchanged. Of **75,268** historical games, **73,585** have accepted period scores; **1,683** do not.

```sh
uv run --offline --no-project --with duckdb==2.0.0 python warehouse/recover_v16.py verify-inputs --schedule data/evidence/nba-playoff-schedules-v16.json --output data/nba-clean-v16.duckdb
```

The 198 playoff flag adjudications use the cached Basketball-Reference schedules for overtime status and the local mirror for quarter values. Original flags are retained. The local evidence JSON contains parsed tables, not original HTML, and is not cleared for republication. Use `audit.period_availability_v16` for the latest ledger.

## 2019–20 field-goal attempts (v15, inherited by v16)

`../data/nba-clean-v15.duckdb` adds **11,819** separately attributed play-by-play attempts for **67** 2019–20 regular-season games. The unified relation has observations for **1,058 of 1,059** 2019–20 regular-season games; `0021900891` is held over an attempt-count discrepancy. This is not complete shot-chart coverage.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_pbp_attempts.py verify-inputs --output data/nba-clean-v15.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_pbp_attempts.py build --output data/nba-clean-v15-replay.duckdb
```

## Early periods and keyed third overtimes (v14, inherited by v15)

`../data/nba-clean-v14.duckdb` adds period scores for 835 early games plus two keyed triple-overtime fixtures. **73,386 of 75,268** games have accepted periods; **1,882** do not. `audit.mirror_v14_assessment` keeps the early holds and missing-source keys, and `audit.mirror_v14_tail` records the two NBA line scores. Earlier finals and accepted period rows are unchanged.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_mirror_v14.py verify-inputs --output data/nba-clean-v14.duckdb
```

## Larger local period cohorts (v13)

`../data/nba-clean-v13.duckdb` adds **25,514** team-period rows across **3,140** games: 2,869 regular-season games from 1970–1999 and 271 playoff games starting in 1954. These additions reconcile to the pinned local mirror, not independent game-by-game sources.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_mirror_v13.py verify-inputs --output data/nba-clean-v13.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_mirror_v13.py build --output data/nba-clean-v13-replay.duckdb
```

See `audit.mirror_v13_assessment` for the exact outcome per fixture, including 221 holds. `audit.period_availability_v13` exposes the latest disposition without changing earlier audit ledgers.

## Historical regular seasons and keyed overtime (v12)

`../data/nba-clean-v12.duckdb` adds **14,220** team-period rows across **1,756** games. The 1966–67 and 1970–71 regular seasons are complete on the existing spine. Another 648 games from 1960–62 passed the same source and structural controls; 28 were held with specific reasons. Six later overtime fixtures match exact keyed NBA period vectors. Finals and standings are unchanged.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_mirror_v12.py verify-inputs --output data/nba-clean-v12.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_mirror_v12.py build --output data/nba-clean-v12-replay.duckdb
```

`audit.mirror_v12_assessment` contains the outcome for every scoped fixture, including holds. The older mirror vectors are checked against the supplied snapshot, with bounded external spot checks, not independently verified game by game. The verifier accepts `--capture-dir` to check original NBA HTML bytes and parsed cells for the six keyed OT games when the saved captures are present. It does not fetch from the network. No redistribution rights are established.

## 1975–77 regular-season period coverage (v11)

`../data/nba-clean-v11.duckdb` adds **13,278** team-period rows for all **1,640** 1975–76 and 1976–77 regular-season fixtures. It uses the supplied team-box mirror and a separately attributed NBA line score for one game's third overtime. Finals and standings are unchanged.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_mirror_periods.py verify-inputs --output data/nba-clean-v11.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_mirror_periods.py build --output data/nba-clean-v11-replay.duckdb
```

`verify-inputs` compares exact source keys and cells to the read-only snapshot, not just to the database's own checksums. `audit.period_availability_v11` distinguishes mirror-only and mirror-plus-official publication.

## 2026 All-Star weekend (v10)

`../data/nba-clean-v10.duckdb` adds 16 team-period rows for all seven 2026 All-Star weekend fixtures. Every 2025–26 fixture on the warehouse spine has its period scores. `nba.event_period_format` and `nba.event_period_score` distinguish untimed target-score overtime from timed regulation. This changes neither the fixture spine nor standings.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/nba_warehouse.py verify data/nba-clean-v10.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_event_periods.py verify-inputs --output data/nba-clean-v10.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_event_periods.py build --output data/nba-clean-v10-replay.duckdb
```

`audit.event_period_receipt` distinguishes six official scorer reports from the keyed NBA Game 2 box page with play-boundary corroboration. `audit.event_period_input` pins the v9 file and the transcribed receipt CSV, not the original bytes of the NBA pages. `audit.period_availability_v10` exposes remaining gaps and disagreements between raw and derived overtime flags. See the [research note](../docs/research/nba-warehouse-cleanup.md) for exact sources and the verification boundary.

## 2025–26 regular-season period coverage (v9)

`../data/nba-clean-v9.duckdb` adds 80 team-quarter rows from ten NBA scorer reports, bringing accepted periods to all 1,230 games of the 2025–26 regular season. `audit.official_period_receipt` preserves the official values; `audit.official_period_comparison` records how locally cached ESPN markers agree or differ. No previous finals, standings or period vectors were changed.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_official_periods.py verify-inputs --output data/nba-clean-v9.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_official_periods.py build --output data/nba-clean-v9-replay.duckdb
```

## Historical period conflicts (v8)

`../data/nba-clean-v8.duckdb` adds 40 team-quarter rows: the fifth recovered 1961 Finals line score and all four period conflicts held in v7. `audit.period_adjudication` preserves exact receipts and evidence types; `audit.period_availability_v8` distinguishes derived overtime from raw reporting. V8 has 65,996 games with accepted periods and 9,272 without.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/adjudicate_periods.py verify-inputs --output data/nba-clean-v8.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/adjudicate_periods.py build --output data/nba-clean-v8-replay.duckdb
```

## Archival period integrity baseline (v7)

`../data/nba-clean-v7.duckdb` adds 32 team-quarter scores for four 1961 NBA Finals games, transcribed from contemporaneous newspaper scans. Game 4 remained uncovered in v7. `audit.period_availability_v7` covers the full spine and distinguishes derived overtime from the raw flag. The ordinary verifier checks exact cells against a stored baseline; `verify-inputs` additionally compares against the pinned v6 file and external transcription. Neither comparison certifies the historical truth of every preexisting vector.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_archive_periods.py verify-inputs --output data/nba-clean-v7.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_archive_periods.py build --output data/nba-clean-v7-replay.duckdb
```

## Local line-score recovery (v6)

`../data/nba-clean-v6.duckdb` preserves the v5 game spine, finals, and regular standings, and recovers **2,918** period-score rows for **355** previously uncovered games by resolving reversed home/away fields from the supplied snapshot. Coverage in v6 is **65,987** games with accepted periods and **9,281** without. All **903** reviewed candidates have a disposition in `audit.local_period_recovery`; previous conflicts and suspect padded scores are not filled.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/nba_warehouse.py verify data/nba-clean-v6.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/recover_local_periods.py --output data/nba-clean-v6-replay.duckdb
```

## All-Star and standings baseline (v5)

`../data/nba-clean-v5.duckdb` contains **75,268** played games, including the three verified 2025 All-Star tournament games; **1,723** regular-season team standings across 80 seasons; and **39,068** additional overtime shot-to-play links. Original snapshot keys and data remain separate from the additions.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/nba_warehouse.py verify data/nba-clean-v5.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/nba_warehouse.py build --with-events --with-recovery --output data/nba-clean-v5-replay.duckdb
```

- `nba.dim_event_edition` and `nba.bridge_event_fixture` distinguish the edition, event component, and fixture stage. The recovered games were one-period exhibitions at a neutral site and do not count toward regular-season standings. `audit.fixture_addition` and the supplemental box ledgers identify all three scorer reports and 52 admitted local player boxes. `reported_num_minutes` preserves the alternate `mm.ss` encoding; `num_minutes` is decimal minutes. Conflicting plus-minus is withheld.
- `nba.fact_regular_standings` is one row per team/season, from finalized games eligible for regular-season standings. It does not assign conference or playoff rank; ties do not have reliable tiebreak data here.
- In v5, `nba.fact_shot.seconds_remaining` uses the same within-period scale as the play feed. The raw overtime clock difference is `420 * (period - 4)` seconds. The match rule still requires a one-to-one result both ways; an unresolved locator is not a proven duplicate. The source fingerprint still keys the unmodified payload.

## 2026–27 result comparison (local sidecar)

`../data/nba-2026-27-results-check-2026-11-19.duckdb` is a **local audit**, not a v5 replacement or a data release. `audit.result_for_local_review` has 226 regular-season finals dated October 20–November 19, 2026 (Eastern dates). The November 18 parent accounted for all 222 finals in the pinned schedule export: 214 agreed with reciprocal team logs; eight needed official NBA receipts. The November 19 extension preserves those records and adds four finals and their 16 quarter rows (`audit.verified_period_score`). `audit.regular_standings_as_of` is calculated from those 226 results only, not a final season table. It includes 28 Cup group games and keeps the Mexico City game's designated home team separate from the neutral-site flag.

```sql
ATTACH 'data/nba-2026-27-results-check-2026-11-19.duckdb' AS current_season (READ_ONLY);
SELECT * FROM current_season.audit.result_for_local_review ORDER BY game_date, game_id;
SELECT * FROM current_season.audit.regular_standings_as_of ORDER BY wins DESC, team_id;
```

To replay the November 18 parent, pass the pinned team-page API response and the three SportsDataverse CSVs to `warehouse/validate_current_results.py`. Its `audit.revisions` preserves Wikimedia revisions, and `warehouse/current_result_receipts_2026-11-18.csv` records exception evidence. `warehouse/fetch_team_revisions.py` requests the **latest** revisions, not a replay of the original snapshot. The comparison assets are listed in the [research note](../docs/research/nba-warehouse-cleanup.md). Replay the extension with `warehouse/extend_current_results.py --help`; it requires the parent, v5, the locally archived daily scoreboards, the pinned team crosswalk, and `warehouse/current_result_receipts_2026-11-19.csv`. Both files pin input bytes in `audit.inputs`. The extension adds no player or team boxes, play-by-play, or shots, and does not establish bulk redistribution rights.

### November 19 team box-score sample

`../data/nba-2026-27-box-check-2026-11-19.duckdb` extends the local results audit without changing its finals or standings. Use `audit.team_box_compared` for eight compared team rows and `audit.team_box_game_coverage` for the full denominator. Two conflicting website/gamebook rebound fields are retained separately; `rebounds_agreed` is null for those rows. Player-line turnovers and total turnovers are separate measures. No individual player facts were admitted.

`warehouse/validate_current_boxes.py` builds or verifies the sample with `--parent`, `--sources`, `--receipts`, and `--output`. `--sources` needs the four pinned box-page and gamebook text extractions named `<game_id>.box.txt` and `<game_id>.gamebook.txt`; hashes are in `audit.team_box_input`, and source URLs are in `warehouse/current_result_receipts_2026-11-19.csv`. This is a local audit, not a redistribution clearance.

## Play and shot baseline (v4)

**V4 data:** `../data/nba-clean-v4.duckdb` (DuckDB 2.0.0). Adds **18,742,559** play events and **6,489,715** shot observations to the unchanged v3 core. **5,665,562** shots have a reciprocal one-to-one play link; an unlinked observation is not a missing shot. These links are local matches, not provider-certified event IDs.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/nba_warehouse.py verify data/nba-clean-v4.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/nba_warehouse.py build --with-events --output data/nba-clean-v4-replay.duckdb
```

- `nba.fact_play_event` uses `(game_id, action_number)`. Action zero is legal; clocks and numbering gaps do not establish chronological or coverage completeness. Person/team actor references are assigned only where the game context supports them; use `actor_kind` to distinguish unresolved IDs from actions where no actor was reported. `reported_home_score` and `reported_away_score` are feed observations and can lag official score corrections. Use `nba.fact_game_result` for finals; `event_score_audit.sql` measures discrepancies.
- `nba.fact_shot` uses a fingerprint over the independent local shot-observation payload. This is not `GAME_EVENT_ID`. Geometry is expressed in tenths of a foot, with the reason it was withheld in `geometry_status`. An ambiguous zero is not assumed to be a genuine origin coordinate or a nonexistent shot.
- `nba.bridge_shot_event` links shots only when game, team, player, period, whole-second clock, value, outcome, and legacy position agree, with uniqueness both ways. `nba.shot_event_status` separates matched, ambiguous, geometry-unavailable, and no accepted match. The audit schemas retain held keys, multiplicity, and counts by game. V4 does not infer lineups, possessions, or missing plays.

## Games and period-score core (v3)

**Core build:** `../data/nba-clean-v3.duckdb` (DuckDB 2.0.0). All **75,265** original game keys now have two resolved participants and a final result. The release has **150,231** team boxes, **1,703,262** player boxes, and **533,722** validated period scores across **65,629** games, including **1,220 of 1,230** 2025-26 regular-season games. This is coverage of the supplied snapshot, not a full NBA refresh or a redistributable dataset. `nba.game_overtime` distinguishes the derived indicator from the unreliable source flag; `audit.period_coverage` records the status and source for every game.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/nba_warehouse.py verify data/nba-clean-v3.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/nba_warehouse.py build --with-periods --output data/nba-clean-v3-replay.duckdb
```

The period build compares explicit quarter scores, legacy backups, line scores, and play-by-play start/end score markers. It requires contiguous periods on both sides, ties before each overtime, agreement with the final result by team, and agreement between qualifying sources. It never treats missing or zero-filled periods as scores. V3 also fixes pre-three-point-era scoring validation, preserves the confirmed 1954 doubleheader, and records the four manually verified fixture-score repairs. The original and prior release files remain unchanged.

## Original game-and-box build

**Data:** `../data/nba-clean-v1.duckdb` (DuckDB 2.0.0). This is a local derivative of `../data/nba.duckdb` release `3.0.0`.

## Query the warehouse

Use `nba` for analysis and `audit` to inspect coverage or dispositions. `nba.fact_game` has exactly one row for each of the **75,265** input game IDs. It preserves a game even when there isn't enough evidence to publish its participants or result. Use `nba.fact_game_result` when only admitted finals should count.

```sql
SELECT game_id, game_date, season_year, home_score, away_score
FROM nba.fact_game
WHERE result_status = 'final'
ORDER BY game_date DESC, game_id DESC
LIMIT 10;

SELECT reason, count(*) AS held_rows
FROM audit.player_box_disposition
WHERE disposition = 'held'
GROUP BY reason ORDER BY held_rows DESC;

SELECT * FROM audit.season_coverage WHERE season_year = '2025-26';
```

| Relation | Grain and usage |
|---|---|
| `nba.dim_game`, `nba.fact_game` | One row per game. For unresolved participant pairs, IDs are withheld rather than guessed; the audit keeps the source IDs. `counts_toward_regular_season` is independent of `competition_tag`. |
| `nba.bridge_game_team` | One row per game and side, only for resolved pairs. Both `(game_id, side)` and `(game_id, team_id)` are unique. |
| `nba.dim_team`, `nba.dim_team_version`, `nba.game_team_labels` | Stable ID, source history, and a one-row-per-participant as-of projection. The projection admits a season label only if exactly one history row covers it. Do not join the history directly on bare `team_id`. |
| `nba.dim_person` | One row per admitted local person ID, based on `dim_all_players`, not the source's multirow `dim_player`. The flagged synthetic-ID band in this snapshot isn't admitted until reviewed. |
| `nba.fact_team_game_stats` | One row per team per game. Must link to an admitted participant. Its side comes from the bridge. Attempts and their associated rate are withheld where makes exceed attempts; made counts and other valid values remain. |
| `nba.fact_player_game_stats` | One row per person per game. Both team and opponent must link to admitted participants. Use `participation_status = 'played'` for appearances; do not count every box row as an appearance. `reported_num_minutes` preserves the input; `num_minutes` is null if reported zero conflicts with recorded activity. |
| `audit.game_disposition`, `audit.team_box_disposition`, `audit.player_box_disposition` | One disposition per primary input key. Held rows carry a reason; those keys and the input manifest locate the original data. Audit source IDs are deliberately not required to resolve to curated IDs. |
| `audit.known_fixture`, `audit.season_coverage`, `audit.box_points_comparison` | Known fixture exceptions, coverage counts by season/phase, and player-point vs. team-point diagnostics. A missing domain isn't a zero. |

### What this build admitted

- **75,193** final results. There are **69** unresolved team pairs and **4** input games without a score (one game is in both groups). All 75,265 still have a spine record.
- **150,170** team box rows and **1,701,950** player box rows. The audit accounts for **134** held team rows and **1,655** held player rows. Player holds: 1,350 unresolved game-team pairs, 300 flagged synthetic-range identities and 5 scoring/stat conflicts. No matching by name or current roster.
- **9,273** admitted team box rows have at least one invalid attempt field withheld. **91,909** admitted player rows have an indeterminate minutes value because zero was reported alongside activity. Stat-era availability is enforced for specified scoring and defensive fields; other specialty stats are carried as supplied, not independently certified.
- `audit.box_points_comparison` shows **24** disagreements and **16,414** team-box rows with insufficient player-side evidence under its comparison rules. This is a diagnostic, not a claim that the player boxes are complete.
- `fact_game` replaces the malformed legacy game-date input and the build labels the 33 July 2020 restart scrimmages `2019-20` / `Exhibition`. The two unplayed 2026 Finals slots and the three 2025 All-Star tournament games absent from the spine are recorded separately. No external rows were fetched or added.

V1 does not publish period scoring, play-by-play, shots, or the other specialty feeds. It does not repair missing game results or cover 2026-27. See the [research plan](../docs/research/nba-warehouse-cleanup.md) for evidence and next phases. The alternate and raw relations remain in the original input, not in the new database; only the three primary input relations are counted in the per-row ledgers.

## Offline Basketball-Reference audit

The comparison file `../data/nba-bref-validation-v3.duckdb` holds regular-season aggregate checks made against the local Basketball-Reference extracts already in the source database. It is separate from both inputs. `comparison_inventory` summarizes results; `team_season` and `player_season` retain keyed differences and reasons. Matching season totals do not validate individual games, periods, or events.

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/validate_bref.py --output data/nba-bref-validation-replay.duckdb
```

## Rebuild and verify

From the project root:

```sh
uv run --no-project --with duckdb==2.0.0 python warehouse/nba_warehouse.py verify data/nba-clean-v1.duckdb
uv run --no-project --with duckdb==2.0.0 python warehouse/nba_warehouse.py build --output data/nba-clean-replay.duckdb
```

`build` attaches the source read-only, stages a new file on the output volume, checks exact input-to-audit key accounting and `verify.sql`, checkpoints, closes, reopens read-only, and installs the file without overwriting. Do not point this at a live DuckDB writer. For this snapshot, an independent rebuild matched all 14 persisted tables with `EXCEPT ALL` in both directions. An invalid source-count expectation and attempts to overwrite the source or destination failed closed.

**Contract sources, not executable dependencies:** the reference [game schema](../reference/nbadb-main/src/nbadb/schemas/star/dim_game.py), [game/team bridge](../reference/nbadb-main/src/nbadb/transform/facts/bridge_game_team.py), and [player-game grain](../reference/nbadb-main/src/nbadb/schemas/star/fact_player_game_traditional.py) inform `schema.sql`. The reference [quality monitor](../reference/nbadb-main/src/nbadb/transform/quality.py) and [quality tests](../reference/nbadb-main/tests/unit/transform/test_quality.py) inform the null-key, grain, and relationship checks in `verify.sql`; the local fixtures cover the snapshot-specific failures. No reference loader or extractor is invoked.

TypeSafe isn't needed for deterministic keys, game-side assignment, or scoring rules. Flagged identities are held for review instead of being auto-approved by a model. No warehouse data was sent to a model API.
