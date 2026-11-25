# NBA warehouse cleanup: audit and implementation plan

**Snapshot:** `data/nba.duckdb`, 18,623,246,336 bytes, warehouse release `3.0.0`. Audited read-only with DuckDB 2.0.0; **526 tables and 32 views**. This is a researched plan, not a migrated database. No source data was changed or refreshed.

## Recommendation

Build a new, versioned DuckDB from the existing warehouse, not from scratch from `reference/`. Start with **`fact_game` as the candidate game spine**, repair entity keys and game participation, and publish one authoritative result per grain. Use `reference/nbadb-main` to supply schema contracts, lineage patterns, and quality tests. Keep the original database and all rejected or superseded evidence outside the analyst-facing output.

**“No extra unmatched data” should mean zero unresolved required references, zero duplicate canonical keys, and zero unexplained source rows.** It should not mean zero missing values. An unrecorded historical stat, a scheduled game never played, and a wrongly linked box score require different decisions. Keep those decisions and reference evidence available in a separate audit artifact. Do not fill gaps with zeros or make them disappear through inner joins.

## What the audit found

Counts below are exact on the snapshot unless described as candidate counts. The SQL appendix reproduces the central findings. Eight research agents cross-checked the initial scouts and exchanged findings on game dates, player-to-team matching, reference adapters, and release checks. Where their heuristics disagreed, the plan uses scope-specific SQL or first-party evidence rather than picking a count.

| Area | Measured evidence | Cleanup decision |
|---|---|---|
| Game spine | `fact_game` has **75,265** distinct keys, all with typed dates, across **80 seasons**. It spans **1946-11-01 to 2026-06-13**. Four rows still lack a final score; it is the best starting point, not a clean release. | Build from `fact_game`, verify fixture status and scope, derive one consistent game dimension and result. Never call this snapshot current; **2026-27 is absent** even though the NBA lists 2026-27 Cup dates.[13] |
| All-Star dates and sides | `dim_game` stores the ID as the date for **55** All-Star rows; **33** of them also have a century-shifted season. The same keys have typed dates and both scores in `fact_game`; `fact_game_result` lacks all 55. There are also **4** non-ISO timestamp strings that cast to date, and **6** game-side reversals against `fact_game`. For `0035100001`, use `1951-52`, not `2051-52`: `fact_game` dates it to 1952-02-11 and its 108–91 score matches the NBA archive. Use modern controls as well (2001 and 2022).[4][5][8] | Repair the dimension from verified typed values and side evidence, not from ID prefix/century arithmetic. A **different** set of 33 rows in `fact_game` labels July 22–28, 2020 bubble scrimmages as `2020-21`; check fixtures and classify as **2019-20 exhibition**, not regular season.[12] |
| Scheduled vs played | `0042500406` and `0042500407` are the only non-null game-map targets missing from the current spine. The approved ESPN/hoopR decisions treated optional fixtures as games. NBA records show Game 5 (`0042500405`) was played, 94–90, and the Finals ended 4–1.[9][10] | Classify Games 6 and 7 `scheduled_not_played`; do not add played rows or request missing box scores for them. |
| True All-Star gap | The **2024-25** edition has no All-Star rows in `fact_game`. NBA lists **three** played games (`0032400011`, `0032400021`, `0032400031`), and all three are among the 11 box-only game IDs missing from the spine. The finals recap lists 41–25, but that was just the final.[11] | Restore three played games from permitted evidence under **one** edition key; link the existing player boxes after validating participants. The other **eight** box-only game IDs need their own evidence. Do not promote them from boxes alone. |
| Person/team join fan-out | `fact_player_game_stats` has **1,703,605** rows. Joining `dim_player` on ID alone returns **10,102,874** rows and leaves **1,334** without a profile. `dim_team` has **113** history rows for **84** IDs; joining either without an as-of condition can multiply facts. Joining both team sides gives **294,841**, not 75,265. | Use unique person and team dimensions. Both `dim_person` and `dim_all_players` cover the existing player-game fact. `dim_all_players` also resolves the **104** player IDs from **215** crosswalk rows absent from `dim_person`. Use it to seed identity coverage and `dim_person` as an enrichment input, but review flagged synthetic and duplicate identities; neither lookup proves the person is who the source says. |
| Crosswalk and provenance | `map_source_identity` contains **136,275** rows and **3,236** intentional exclusions with null target. **1,798** game bridge keys have no identity-map record (**1,797** Kaggle, **1** Basketball-Reference). **1,976** games lack a `fact_game_provenance` row. `bridge_game_espn` has **110** rows without game keys, but flags **zero** unresolved. | Use one namespaced, domain-typed crosswalk with reviewed reasons, evidence, and effective ranges. Validate manual decisions as well as automatic ones. Review intentional exclusions; do not relabel them all as source defects. Treat these 1,976 as a provenance gap, not automatically a games gap. |
| Box-score overlap and missing team | `fact_player_game_stats` and `fact_player_game_boxscore` share **1,667,315** `(game_id, person_id)` keys; **111** points disagree. The latter has **109,254** rows across **4,063** games without team or opponent. **108,546** have an exact key match with a non-null team in the former; **708** have no such match. | Use `fact_player_game_stats` as the starting box fact, compare shared cells, and preserve any genuinely additive evidence. Do not union the facts or assign the remaining 708 from a player's current roster. Verify the game-day team and check conflicts even on exact keys. |
| Period scores | All **1,230** 2025-26 regular-season games have zeros for both teams in both Q1 and Q4 in `fact_game_scoring`. `0022500002` gives a false period sum of 57–62 vs 109–119 in `fact_game`. The period tables that only model four quarters cannot cover overtime. | Do not use those sums for game scores. Rebuild as `(game_id, team_id, period_number)`, including all overtime, and check totals only when period coverage is certified complete. |
| Play-by-play and shots | `fact_pbp_events` has **18,742,563** rows and **39,165** games; its v3 companion has **18,722,958** rows with additional fields. `fact_shot_chart` has **6,490,494** rows and no `game_event_id`, unlike the [reference schema](../../reference/nbadb-main/src/nbadb/schemas/star/fact_shot_chart.py#L12). A game/player/period/whole-second-clock/shot-value candidate test yields **6,404,124** shots with one candidate, **8,374** with multiple, and **77,996** with none. Of the apparent unique matches, **20** rows share **10** candidate events. Adding exact legacy coordinates and requiring uniqueness on both sides leaves **6,409,948** one-to-one *candidates*, not verified provider IDs. | Preserve canonical play and independently verified shot grains. Use direct provider event IDs first. For locators, check event role, units and outcomes, require uniqueness in both directions, and record match method. Never expose a many-to-many accidental join or pretend coordinate equality supplies a source ID. Keep unresolved links out of the published bridge without deleting otherwise verified shot facts. |

### Coverage contract

- Scope the core to the NBA and its BAA predecessor from 1946-47. Label the BAA years separately without duplicating games across both labels. Model ABA separately if desired; it is not missing NBA coverage.[17]
- Keep competition tags separate from standings eligibility. The Cup audit shows **66** regular-season Cup games plus **one** final in each of the three locally covered seasons; all but the championship count toward NBA regular-season standings. Keep Play-In separate from playoffs. Broken phase labels should be corrected from evidence, not prefixes.[13][14]
- Keep a game key and an edition key for modern All-Star formats; count each only for the matching question. The NBA records three tournament games in 2025 and four in 2026. Distinguish Rising Stars exhibition games from the main tournament rather than deduplicating by date.[11][18]
- Track `present`, `partial`, `missing_recoverable`, `not_tracked`, `source_unavailable`, and `not_applicable` by domain × season × phase where applicable, and by game for domain fixes. Use the local `dim_stat_availability` and `map_game_domain_coverage` as seeds, not proof that the rest is complete. Model DNP, roster-only, played, and unknown explicitly. `num_minutes = 0` is not a safe participation test; even some rows with positive points have zero-filled historical minutes.
- Once game/box/event facts pass, add draft, rosters and transactions where evidenced, standings, officials, arenas, tracking, lineups, awards, and other in-scope domains from a prioritized coverage manifest. Keep betting, media, and snapshots out of the default analytical core; assess each separately. Recompute season/career aggregates from canonical facts, not a union of copies.

## Use the reference repo as a specification, not as the migration

| Reuse | How to use it here |
|---|---|
| [Game transform](../../reference/nbadb-main/src/nbadb/transform/dimensions/dim_game.py#L11), [team transform](../../reference/nbadb-main/src/nbadb/transform/dimensions/dim_team.py#L10), and [season rollup guards](../../reference/nbadb-main/src/nbadb/transform/derived/agg_player_season.py#L83) | Use one game row and one team key in analytical joins, with dated team-label history separately. Adapt local `Regular`/`Preseason`/`AllStar` values explicitly. The reference's game transform expects staging fields not present verbatim; its team-ambiguity guard correctly rejects this warehouse's unfiltered history. |
| [Staging map](../../reference/nbadb-main/src/nbadb/orchestrate/staging_map.py), [year coverage](../../reference/nbadb-main/src/nbadb/core/table_year_coverage.py#L36), [backfill planner](../../reference/nbadb-main/src/nbadb/orchestrate/backfill.py#L102) | Use endpoint dependency and per-season availability patterns. Normalize input formats, but a valid-looking `2051-52` still needs calendar evidence. Work from our verified gaps, not the reference extraction journal. The local league-log staging slice spans just **4,159 games across three seasons**, far short of the 80-season spine. |
| [Shot transform](../../reference/nbadb-main/src/nbadb/transform/facts/fact_shot_chart.py#L16), [shot zone derivation](../../reference/nbadb-main/src/nbadb/transform/derived/agg_shot_zones.py#L89), and [statistical possessions](../../reference/nbadb-main/src/nbadb/transform/derived/fact_statistical_possession.py) | Port explicit 0/1/many match states and downstream tests, not assumed event keys. Apply possessions and lineup logic only where a complete, ordered event stream is verified. The local v3 companion has legacy shot-location units that the canonical projection omits; preserve units and provenance separately. |
| [Quality implementation](../../reference/nbadb-main/src/nbadb/transform/quality.py#L166), [quality tests](../../reference/nbadb-main/tests/unit/transform/test_quality.py), [pipeline tests](../../reference/nbadb-main/tests/unit/transform/test_pipeline.py), and [lineage docs](../../reference/nbadb-main/docs/content/docs/lineage) | Port assertions and discovery patterns. Inventory local inputs and publish an allowlist with declared dependencies. Do not run upstream `init`, `backfill`, or the [replace-table loader](../../reference/nbadb-main/src/nbadb/load/duckdb_loader.py#L46) against the supplied file. |

**Rights:** The reference [code license](../../reference/nbadb-main/LICENSE) is MIT; that is not permission to reuse the underlying feeds. The NBA's statistics terms require prominent attribution and expressly restrict services with comprehensive, regularly updated statistics databases without consent. Kaggle has separate contributor obligations, and the reference [publication review](../../reference/nbadb-main/artifacts/assurance/complete-nba-api-sink/rights-evidence.json) marks publication blocked. Check permitted use and redistribution for each provider before automating collection or shipping any dataset. No new source data was imported for this audit.[6][7]

## Build and release gates

1. **Inventory and freeze the inputs.** Record input identity, engine version, objective scope, table/schema inventory, expected coverage, receipt IDs, and source priority per domain/field/era. Profile exact counts where they control a gate; DuckDB's catalog estimate for `map_source_identity` is over 1.6 million versus 136,275 actual. Use the existing layer, authority, coverage, and quarantine registries as inputs, not release attestations.
2. **Build the game and identity core.** Preserve ten-character NBA game IDs and namespace external IDs with source and entity type. Separate franchise history from team identity, and team identity from event-specific squads. Track a source mapping's effective range and review evidence; never join on a bare external/canonical ID across domains. Build `bridge_game_team` with unique `(game_id, side)` and `(game_id, team_id)`. Require exactly two verified participants for published head-to-head facts. Schedule candidates stay separate from verified played games. Match team/player history with an as-of range and fail if it gives multiple matches.
3. **Build boxes and reconcile.** Begin with the observed player box grain `(game_id, person_id)`; keep team ID as validated membership and do not collapse a conflicting two-team claim. Enforce `(game_id, team_id)` for team boxes. Explicitly distinguish played, DNP and known missing stats. Verify basketball scoring using `2*FGM + FG3M + FTM` when the source's field goals made includes threes and all components are available. Compare scores by verified team, not position in an array; compare player totals, period totals, and team scores only on eligible populations. The NBA scoring rule assigns two or three points per field goal.[15]
4. **Build plays, shots and enrichments.** Version normalizers for provider vocabulary, clocks, shot outcomes and actor roles. Team rebounds/timeouts are team actions, not missing players. Use `(game_id, action_number)` where the source contract supports it, and never treat legacy `event_num` as equivalent without a proved crosswalk. Require reciprocal uniqueness for optional shot links. Record a confidence method and do not coerce missing coordinates into zero. Backfill only verified, in-scope shortages from permitted sources.
5. **Account for every source record.** Key a disposition by run, receipt/source relation, and a durable row identifier, or a fingerprint plus multiplicity for indistinguishable rows. Choose one terminal disposition per row: `published_primary`, `used_as_evidence`, `superseded`, `duplicate_ingest`, `excluded_scope`, `scheduled_not_played`, or `held` with a reason such as `ambiguous_identity`, `missing_required_key`, or `conflicting_stat`. Store target/source pointers and evidence where applicable. Assert that per-receipt input multiplicity equals the sum of these categories; a clean layer with zero orphans can still fail for unexplained data loss.
6. **Publish only a verified separate file.** Attach the original read-only and write to a versioned output created from explicit DDL.[1] Enforce critical primary, foreign and non-null keys; don't assume CTAS carries over constraints.[2] Supplement with checks for cross-table cardinality and invalid one-to-many paths. Repeat with the same inputs and verify identical projections using `EXCEPT ALL` in both directions.[3] Checkpoint and close the output, verify no WAL sidecar, reopen read-only and rerun gates, then switch consumers to the tested version.[16] Keep the previous release for rollback. Do not copy a live database without its WAL or replace the original file.

### First implementation milestone

Produce a **game-and-box pilot** in a new local DuckDB, not a full rebuild. Test at least: 1946-47 BAA game/team history, the 1952 century-shifted All-Star record, the July 2020 restart scrimmages, a 2024-25 All-Star tournament with three games, the 2026 Finals' five played and two unplayed fixtures, and the 2025-26 zero-filled periods. Success means no key duplication or fan-out, no required orphan references, reproducible decisions for every held/excluded row, observed score/side agreement on the verified subset, and unchanged input. Then expand across seasons and domains. No record with an unresolved identity or missing required team/game should appear in certified analytical facts.

## Reproducible read-only SQL

Open `data/nba.duckdb` read-only. These queries audit the supplied snapshot; none repairs it.

```sql
-- Game key and time window: 75,265 / 75,265, 1946-11-01 to 2026-06-13.
SELECT count(*) AS rows, count(DISTINCT game_id) AS ids, min(game_date), max(game_date)
FROM fact_game;

-- Fan-out: 1,703,605 source rows -> 10,102,874 naive join rows; 1,334 missing profiles.
SELECT (SELECT count(*) FROM fact_player_game_stats) AS source_rows,
       (SELECT count(*) FROM fact_player_game_stats f JOIN dim_player p
        ON f.person_id = p.player_id) AS naive_join_rows,
       (SELECT count(*) FROM fact_player_game_stats f ANTI JOIN dim_player p
        ON f.person_id = p.player_id) AS facts_without_player_profile;

-- All-Star dimension holes: 55 / 55 / 55 / 33 / 0.
SELECT count(*) AS games, count(f.game_date) AS typed_dates,
       count(*) FILTER (WHERE f.home_score IS NOT NULL AND f.away_score IS NOT NULL)
         AS with_scores,
       count(*) FILTER (WHERE try_cast(left(g.season_year, 4) AS INTEGER) > 2025)
         AS apparent_future_seasons,
       count(r.game_id) AS in_result_table
FROM dim_game g JOIN fact_game f USING (game_id)
LEFT JOIN fact_game_result r USING (game_id)
WHERE try_cast(g.game_date AS DATE) IS NULL;

-- Separate issue: 33 July 2020 restart fixtures assigned to 2020-21.
SELECT count(*) FROM fact_game
WHERE season_type = 'Preseason' AND season_year = '2020-21'
  AND game_date BETWEEN DATE '2020-07-22' AND DATE '2020-07-28';

-- Game bridge keys missing from the map: 1 BRef, 1,797 Kaggle.
SELECT b.source_system, count(*) AS missing_bridge_keys
FROM bridge_game_source_id b ANTI JOIN map_source_identity m
  ON m.entity_type = 'game' AND m.source_system = b.source_system
 AND m.source_key = b.source_game_id
GROUP BY 1 ORDER BY 1;

-- Dangling played-game targets: 0042500406 and 0042500407.
SELECT canonical_id, source_system, source_key, resolution_status
FROM map_source_identity m
WHERE entity_type = 'game' AND canonical_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM fact_game g WHERE g.game_id = m.canonical_id)
ORDER BY 1;

-- Box overlap: 1,667,315 shared keys, 111 point conflicts, 108,546 team candidates.
SELECT count(*) AS shared_keys,
       count(*) FILTER (WHERE a.points IS DISTINCT FROM b.points) AS pts_conflicts,
       count(*) FILTER (WHERE a.team_id IS NOT NULL AND b.team_id IS NULL
                        AND b.opponent_team_id IS NULL) AS recoverable_team_candidates
FROM fact_player_game_stats a JOIN fact_player_game_boxscore b
  ON a.game_id = b.game_id AND a.person_id = b.player_id;

-- Missing Q1 and Q4 represented as zeros in 1,230 regular-season games.
SELECT count(*) FROM (
  SELECT s.game_id FROM fact_game_scoring s JOIN fact_game g USING (game_id)
  WHERE g.season_year = '2025-26' AND g.season_type = 'Regular'
  GROUP BY 1 HAVING count(*) FILTER (WHERE s.period IN (1, 4) AND s.pts = 0) = 4
);
```

## Free-resource research and stress-test log (v2)

**Standard:** sports-data release auditor. A source needs verifiable coverage and a known upstream, not just free downloads. A local row needs a declared grain, reproducible provenance, and passed domain checks. A release must distinguish reviewed coverage from unavailable or ambiguous values; it cannot certify games or stats that were never recorded. Five independent researchers are checking schedules, boxes, events, specialty domains, and the release gates.

| Candidate | What was verified / admission decision |
|---|---|
| Local `data/nba.duckdb` | Already in scope. Prefer exact game/team/action keys and reconcile overlap before introducing any outside source. |
| Moore historical Kaggle export | The maintainer documents that the pipeline copies NBA.com data and makes nightly CSV exports. Useful coverage lead, not independent confirmation of an NBA source or a grant from the NBA.[19][20] |
| SportsDataverse hoopR releases | The publisher documents public no-key file downloads and distinguishes ESPN and NBA Stats capture paths. The play-by-play release docs list 2002–2026 season ending years. The upstream data terms still need review; a release URL is not a data-use license.[22][23] |
| Shufinskiy's play and shot archive | Publisher documents NBA Stats data and shots from 1996-97, plus derivative `pbpstats.com` and `data.nba.com` feeds. The archive's Apache label does not establish upstream NBA permissions. A comparison against NBA-derived local rows is not independent coverage evidence.[21] |
| NBL official recaps | Example: the league confirms the Breakers/Jazz exhibition finished 87–116. Use primary recaps for targeted game-level verification, not as a bulk historical database.[24] |

**ST-PER-01 (high, period-score completeness, fixed for the validated subset):** The original `fact_game_scoring` had zero-filled Q1/Q4 in 1,230 2025-26 regular-season games. At first, exact quarter and line score validation recovered 56,143 games overall, but none of the 2025-26 regular-season games. A second pass required exact play-by-play start/end boundaries (one of each per period, ordered actions, end clock zero, continuity from `0–0`) and compared them against explicit score sources. The published slice is **526,668 rows for 64,755 games**, covering **1,215/1,230** 2025-26 regular-season games. For `0022500002`, recovered period points are home `22, 32, 25, 30` and away `28, 27, 35, 29`, totaling 109–119, not the zero-filled 57–62. All 25 build checks passed. The fresh pass also required ties before overtime and withheld two games whose individually plausible sources disagree. No missing period was replaced by a synthetic value.

**Open after v2:** 72 final results unavailable under current admission rules; 8,982 games lack an eligible period candidate, 1,454 have candidates that fail, and two have conflicting qualifying candidates. The larger play/shot/lineup and specialty domains remain separate release surfaces. This is not a complete warehouse yet.

## Basketball-Reference validation: existing extracts, not a live crawl

Sports Reference says it blocks traffic above **20 requests/minute** on its non-FBref/non-Stathead sites; Basketball-Reference’s `robots.txt` returned HTTP 200 and lists a **3-second crawl delay**, plus restrictions on game-log, split, on/off, lineup, and shooting paths.[25][28] This ceiling is not a bulk-use license. Terms §5 separately restrict use in a competing or materially substituting database and automated access that adversely affects the site; the Data Use page discusses its bulk data constraints.[26][27] No live bulk crawl was started. A bounded audit using the BRef extracts already in the supplied snapshot ran entirely in DuckDB; no raw source rows were sent to a model API and neither input file was modified.

`data/nba-bref-validation-v2.duckdb` (built by `warehouse/validate_bref.py` and `warehouse/validate_bref.sql`) stores the dispositions and differences for **1,770 team-seasons** and **25,319 player-seasons**, covering source season-ending years 1950–2026. It validates NBA **regular-season aggregates**, not individual games or the entire database. Team counts, wins/losses, and points scored/allowed match for **1,642** rows; 25 totals disagree, 98 reference team IDs are unmapped, and five comparative team-seasons have no local key. Player games and points match under the audit's completeness checks for **18,365** rows; 643 point totals disagree, 169 game counts differ, 6,120 have uncertain local box coverage, 15 lack a local player-season, and seven have explicitly excluded person mappings.

**ST-BREF-01 (high; bogus playoff split, prevented):** the team's reference `is_playoffs` flag does not define the team-season split here: for sample seasons on both sides of the flag, total games equaled regular-season wins plus losses. The audit checks that equality row by row before comparing to regular-season game results rather than interpreting the flag as playoff totals.

**ST-BREF-02 (high; traded-player double count, prevented):** only the explicit all-teams total is used when both aggregate and stint rows exist. For the 2024–25 traded-player fixture `doncilu01`, three source rows collapse to one comparison row; zero points and games deltas still do not earn a match when participation is uncertain. A fresh replay matched all four persisted audit tables with `EXCEPT ALL` in both directions and confirmed an overwrite attempt was rejected.

**Remaining scope:** the staged BRef data does not validate early BAA seasons, game-level results or boxes, period scoring, play-by-play, shot locations, or tracking simply because aggregate totals match. No scheduler or background scraper is running. To extend this audit with fresh BRef data, obtain bulk-use permission or an authorized export with the required grain; keep all blocked or unsupported areas separate from validated coverage.

## Swarm follow-up: v3 reconciliation

All five researchers returned. Their findings were leads, not automatic fixes. The build confirmed exact game ID, date, side IDs, and final scores in the local legacy game table for 68 previously unresolved entries. This restored 34 missing local team/squad IDs without merging lookalike names across namespaces. The four previously missing preseason scores were verified against official game pages. The three Breakers fixtures have direct evidence in the audit.[32][33][34]
The Portland game page confirms the final and identifies Ulm as `15025` via its official team asset.[35]

**ST-GAME-03 (false duplicate):** same day and both teams is a collision signal, not a uniqueness constraint. The NBA records the 65–54 game; AP independently confirms the Milwaukee–Baltimore doubleheader and the two distinct scores, 64–54 and 65–54. Both source IDs stay valid under one explicit, cited exception.[29][30]

**ST-BOX-03 (false success):** pre-three-point-era checks now use `2*FGM + FTM`; they no longer skip historical rows because `FG3M` is null. A source-record collision on Wesley Matthews's 2009 preseason appearance keeps the played line and holds the conflicting zero-minute record, without merging people on name.[36][37]

**ST-PER-02 (false rejection):** `_bak_fact_game_quarter_scores` is a candidate, not a replacement; never take the maximum conflicting points across sources. The reported overtime flag conflicts with reconciled period evidence on 348 games and is null on 411 more where validated periods exist. `nba.game_overtime` exposes both. A tampered copy that moved points from overtime to Q4 without altering the final score failed the independent tie-before-overtime check. V3 publishes **533,722** period scores on **65,629** games; four conflicting candidate sets remain held.

**Observed output:** V3 passed 34 checks and published final scores and participants for all **75,265 input games**, not all possible NBA games. The v3 offline Basketball-Reference comparison matches 1,642 team-seasons and 18,355 player-seasons. Its ten fewer matches than v2 do not mean a regression: rows with historical scoring holds now count as uncertain, rather than matching despite invalid detail.

**Still open:** verified external fixtures outside the original spine (three 2025 All-Star tournament games), the 2026-27 season, remaining historical period gaps, event/shot/lineup and secondary-domain release gates. The PBP Stats endpoint documentation is a useful interface lead, but it does not establish an independent grant of NBA data rights.[31] No Basketball-Reference bulk scrape was started.

## V4: event and shot release

**ST-EVENT-01 (high; role and key false negatives):** the same source column carries person and team actor IDs. A plain person join is not a role test. V4 requires an exact game roster/team match before emitting a person ID and explicit team-action context before emitting a team actor. A total of **24,824** zero action numbers are legitimate keys in distinct games; only the **four** source events lacking a valid period were held. The curated event count is **18,742,559** across **39,165** games. There are **49,148** unresolved reported actor IDs, represented as absent canonical actor references rather than guessed people; the original IDs remain available by source event key.

**ST-SHOT-01 (high; one-way match false green):** the [reference schema](../../reference/nbadb-main/src/nbadb/schemas/star/fact_shot_chart.py#L8) expects an explicit zero/one/many match status and a provider event ID; the latter is absent locally. V4 validates the match in both directions using game, team, player, period, whole-second clock, value, outcome, and coordinate agreement with the keyed v3 companion event. It publishes **6,489,715** shots and **5,665,562** links, with **16** ambiguous, **742,206** geometry-unavailable and **81,931** otherwise-unlinked published observations. It holds **646** duplicate locator rows and **133** rows with unresolved game-day membership, accounting for all source inputs by key and multiplicity. A one-unit change to a stored link coordinate on a disposable copy failed validation without changing counts, uniqueness, or foreign keys.

**ST-EVENT-02 (high; score recency false green):** the extra [final-score comparison](../../warehouse/event_score_audit.sql) found **9,081** matches with a single explicit end score, one repeated but agreeing marker (`0022500104`) and one discrepancy (`0022500029`). The NBA published a postgame correction from 114 to 115 for Washington after discovering an incorrectly recorded missed free throw; the local event stream still reports 114. The official final remains 115–148; no event was silently rewritten.[38] The Denver final for the repeated marker remains 133–111.[39] The same check distinguishes the **30,082** games with events but no qualifying end marker from the **36,100** games with no local events, rather than claiming validation for either group.

**Release evidence:** v4 passed **47** build checks on reopen, and comparison with v3 verified the complete multiset of every one of its 20 core tables was unchanged. Source timestamps and file sizes were unchanged. This release does not backfill the two missing post-1996 regular-season play feeds (`0020600975`, `0020700753`), 33 2025-26 playoff games lacking a shot feed, or the historical eras without comparable local records. The targeted five-agent follow-up covers localized event/shot critiques and remaining standings, All-Star, and open-license evidence. No live bulk crawler was started.

## V5: All-Star recovery, standings, and overtime shot clocks

All five follow-up scouts returned. The three Feb. 16, 2025 All-Star tournament fixtures were absent from the spine, but their 52 basic box records were in the alternate local table. Official scorer reports verify the date, sides, one-period format, and finals. V5 adds **25–41** for the designated-home Global Stars in the final, not 41–25 in home-first order. The official home/away score pairs for the semifinals are **41–32** and **42–35**.[40][41][42]

**ST-AS-01 (high; wrong duration or fake quarters):** alternate box minutes are `mm.ss`, so 7.10 is 7:10, not 7.1 decimal minutes. All 52 exact source keys enter a supplemental ledger; the six team totals match the scorer reports to the second (3,035 / 3,870 / 3,200 seconds per team). The seven DNP entries remain DNP and conflicting plus-minus is withheld. A copy with one second moved between two players kept the team's total intact but failed the individual-minute check. The 2025 suffix `0031` is the final, while the analogous 2026 suffix is round robin, as the NBA schedule confirms.[18]

**ST-STAND-01 (high; standings scope and tie-breaks):** finalized games produce **1,723** team-seasons across **80** seasons, with **68,522** wins and losses and balanced points. All **1,667** usable Basketball-Reference team-season controls match the W/L/G derived from the unchanged regular-season inputs, although 25 still disagree on points. The build retains both 1954 doubleheader games and counts Cup group/knockout games but not the championship. Some historical season tables require era-specific rules: the inaugural BAA standings include a 61st regular-season decision for Chicago and St. Louis rather than assuming a uniform 60-game season.[30][13][45]

**ST-SHOT-02 (high; overtime clock defect):** in the supplied shot source, period 5 spans 420–720 seconds, period 6 starts at 840, period 7 at 1,260, and period 8 at 1,680; the play clock remains 0–300. Subtracting `420 * (period - 4)` recovered **39,068** reciprocal overtime links and preserved every v4 link. V5 has **5,704,630** solid local links. The looser locator suggested by one scout is not an independent confirmation of coordinates, outcomes, or value. Equal payloads can represent distinct attempts, so V5 relabels its 646 held rows `ambiguous_repeated_locator` and keeps their multiplicities instead of claiming duplicate ingestion. Zero origin remains ambiguous: the source also labels 615 zero-origin, zero-distance rows as three-point attempts, so raw geometry is not a sufficient resolution rule.

**Source boundary and remaining gaps:** Wikidata's structured data is released under CC0, and the Lakers–Celtics sample from Jan. 10, 1975 matched v5 at 103–97 with explicit home and away roles. The Wikidata item is still a compiled record and this test is not an end-to-end check of the whole Wikidata corpus.[43][44] No no-cost independent feed established blanket coverage or permission for missing historical quarters, transactions, or tracking. The source-vintage issues in play events, player identity collisions, missing 2025 Rising Stars fixtures, and absent 2026-27 inputs are still open. Neither a bulk Basketball-Reference crawl nor third-party mirrored import ran.

## 2026–27: result-only audit through November 18, 2026

**Status:** Local review only, separate from v5. All **222 completed regular-season games in the pinned ESPN schedule export** through November 18 have a keyed, corroborated final. Neither the export nor the editorial comparison is a proof of independent season-wide coverage. The NBA's November 19 page and the pinned export show four scheduled, unfinalized games; no November 19 rows are admitted. The NBA sets Oct. 20 as the opener.[48][51][52]

**Primary evidence and replay:** One MediaWiki API batch supplied 30 exact team-page revisions (sample pages [49][50]); the `audit.revisions` table has the permanent `oldid` locators. The audit credits the Wikipedia contributors. The request used a serial batch with `maxlag=5`; Wikipedia's CC BY-SA text terms do not confer rights to the ESPN evidence.[46][47]

The three SportsDataverse assets below matched the publisher's GitHub file digests.[48][54] Files are comparison inputs, not an upstream redistribution grant. The sidecar stores five input digests and `redistribution_cleared=false`.

| Pinned asset | GitHub asset ID | SHA-256 |
| --- | --- | --- |
| `nba_schedule_2027.csv` | `767613163` | `78832783dce3ef6e9b72eaf11ff4d249f3847f52788daee228b055f63969547a` |
| `nba_schedule_crosswalk_2027.csv` | `768416588` | `b36c19123d063a6e760362629479232453722414adbfe0acd614cb38ef3d7145` |
| `nba_team_crosswalk_2027.csv` | `768414532` | `9010703174e833be3280f1136fdd65dbdae82de4fa8908df1b00763ab2db880e` |

**ST-CURRENT-01 — false match (high):** A shared game ID is not enough. For `0022600107`, a third unrelated Phoenix row uses the same ID as Houston–Atlanta; matching by key alone can corrupt both games. The Warriors' side of the Phoenix game has a score but no ID. Five other IDs have contradictory editorial scores or side orientation; Nets–Bulls lacks the mirror crosswalk. A reciprocal pair demands exactly two sides, opposite designations, swapped opponents, same date, and inverse scores. The final must match the ESPN crosswalk on those same fields or have a specific NBA receipt agreeing with the ESPN result. The **214** reciprocal matches and **8** NBA receipt adjudications are separate evidence routes (`audit.result_for_local_review`). No original editorial row was rewritten: **428** matching observations, **13** non-reciprocal, **2** without a provider link at the reported key, and **1** with no NBA ID account for all **444** raw editorial observations. The Oct. 24 ID collision and the two corrected scores on Oct. 31 and Nov. 4 reconcile to the NBA daily scoreboards.[55][56][57] The Nov. 13 Cup fixture and Nov. 15 results reconcile to the league's schedule and box-score records.[58][59][60] The Nov. 18 scoreboard supplies the missing Nets–Bulls key.[53] All eight adjudications are pinned in `warehouse/current_result_receipts_2026-11-18.csv`.

**ST-CURRENT-02 — scope drift (high):** No cut-off inference from UTC calendar date; the tipoff timestamp is converted to Eastern time and checked against the provider's published game date. All 30 teams resolve to the v5 dimension, and no candidate NBA ID overlaps v5. The comparison includes **28** Cup group games (these count in the regular season), no championship, and exactly one neutral venue. For `0022600189`, Denver won 114–102 against designated-home Indiana, in Mexico City. The standings table is explicitly as-of: **30** teams, **444** team-games, **222** wins and **222** losses, and **50,495** points both for and against.[13][58][61]

**Fresh pass (data-release reviewer):** The sidecar was reopened read-only; all **12** November 18 finals matched the official daily scoreboard on exact key, sides, and score.[53] A replay with the same inputs produced ten equal relations by symmetric `EXCEPT ALL`. A change to a receipt's score that preserved total points, an official receipt that reused a valid NBA key, an out-of-scope November 19 cutoff, and an attempt to overwrite the existing output were each rejected. Strict type checking passed. Missing observations at game-box, period, play, shot, and transaction grains remain open; this is not a complete current-season import.

## Follow-up: current-season finals through November 19, 2026

All five workers in `deleg_92785ff3` returned. The later ESPN daily scoreboards supersede the pinned schedule snapshot for final status. `data/nba-2026-27-results-check-2026-11-19.duckdb` is a separate local audit with **226** finals and **452** team decisions. All **222** prior records survived unchanged and matched the newer capture on provider key, date, sides, score, tipoff and competition flags. ESPN standings and daily finals are separate artifacts of the same upstream; Yahoo shows the same four Nov. 19 results but that is not proof of independent season-wide provenance.[62][63][73]

| NBA ID | Visitor | Home | Final (visitor–home) | Scorer report |
| --- | --- | --- | --- | --- |
| `0022600270` | Golden State | Memphis | 98–126 | NBA gamebook [64] |
| `0022600271` | Portland | Milwaukee | 124–114 | NBA gamebook [65] |
| `0022600272` | New York | Minnesota | 102–99 | NBA gamebook [66] |
| `0022600273` | LA Clippers | San Antonio | 112–124 | NBA gamebook [67] |

**ST-CURRENT-03 — stale status and false-green period totals (high):** All four affected games have a listed tipoff of `2026-11-20T01:00Z` on the November 19 Eastern league date.[62] Their NBA keys, designated sides and finals were confirmed on the league box pages. Their **16** quarter rows agree, value for value and on both sides, with the four scorer reports above and the scoreboard capture. The eight team W–L records printed in the gamebooks match the derived records. A same-total transfer from Memphis Q2 to Q1 failed the build; a replay matched six relations. Eight checks passed on the checkpointed file, including no v5 overlap and no team double-bookings. Existing v5 and November 18 files are attached read-only and fingerprinted before and after the build.

**Schedule projection is not a completeness denominator:** the league's release assigns dates/opponents to 80 of 82 games per team; the remaining matchups depend on Cup group play. Do not publish TBD participant IDs or infer a regular decision from a provider's generic regular-season tag. The Cup championship belongs to a separate competition result at Hinkle Fieldhouse, not a regular-season win in Las Vegas.[13][68][69]

**Historical period recoverability:** v5 has **9,636** games without accepted period vectors (9,002 unavailable locally, 630 invalid, 4 conflicting). Six sampled historical vectors, including all five 1961 Finals games, reconcile to existing finals in v5. All five Finals games have `reported_overtime=true` in v5 but four-period vectors in the specialist receipts; derived OT was unknown in v5. The data stays outside v5 until admitted with adequate provenance. RealGM and Basketball-Reference agreement is corroboration, not separately established upstream lineage.[74][75][76]

The other samples are Finals Games 4–5 and the December 25, 1960 Knicks–Nationals game; the latter reconciles to Syracuse 162, New York 100. A marker for an absent period is not evidence that the game went to overtime.[77][78][79]

**Injuries and transactions:** official injury reports establish pregame status at the stated report time, not who actually played. For example, LJ Cryer was questionable at 5:30 p.m. and logged 12:19 in the final November 19 scorer report.[64][72]

The structured injury mirror documents `NOT YET SUBMITTED` sentinel rows and inferred person joins; those are not missing roster players or proof of identity.[70]

No complete, rights-clear transaction archive was established. ESPN/Disney's terms limit automated database use; do not configure ongoing ESPN collection or publish the comparison extracts without clearance.[71]

## V6: recover locally recorded period scores without changing finals

**ST-PERIOD-03 (high; swapped sides):** `warehouse/periods.sql` required the line feed's home/away orientation to match the curated game, even when the source had the correct scores for the correct team IDs in reverse. For `0024600005`, the line score lists Washington as home, contrary to the league's Washington-at-Detroit box score. The recovery attaches each period vector to the owning team ID. It requires exact game ID/date, reversed team IDs and finals, a complete vector that reconciles to the final, exact per-team scoreboard vectors, and agreement on the last period from both cached summaries. The checks improve local consistency; these NBA projections are not separate independent archives.[80]

**ST-PERIOD-04 (high; arithmetic alone can admit bad data):** `0010600016` has equal first-three-quarter values for each team, with the remainder in Q4. The final adds up, but that pattern can be a balanced fill, so this row stays held without claiming it is proven synthetic. The NBA page confirms the underlying Phoenix–Tel Aviv exhibition identity, not the validity of the cached vector.[83]

An older same-total source conflict also stays unresolved; dates and team owners are not repaired heuristically. Exact repeated source projections are collapsed without inflating evidence. For `0024600026` the old snapshots agree on five periods and a tie after four, and the NBA box confirms the 83–74 overtime result. For `0012200062`, the scorer report confirms Indiana's 29/31/32/22 and Houston's 39/42/22/19 exactly, despite the local play stream ending early.[81][82]

**Result and fresh pass:** `audit.local_period_recovery` accounts for **903** reviewed games: **355** admitted and **548** not admitted. **306** additions are from the first eight seasons (1946–47 through 1953–54). In all, **2,918** new observations bring this release to **536,646** team-period rows across **65,987** games. **9,281** games still lack accepted periods (8,651 unsupported, 626 invalid, four source conflicts). All **63** warehouse checks passed. Replay reproduced five period and audit relations exactly, preserved all previously published period rows, and verified unchanged game keys, participants, finals and standings. A point moved between Indiana's first two periods was rejected even though the final score was unchanged. Removing the audit ledger or trying to overwrite the existing release was also rejected.

The six 1960–61 historical samples were not imported into v6. Five workers in `deleg_cdcbf8a1` subsequently completed the archival and technical follow-ups below.

## V7: archival quarters and exact-vector verification

**ST-PERIOD-05 (high; newspaper OCR and ambiguous overtime):** *The Evening Star* printed the AP quarter totals for 1961 Finals Games 1, 2, 3 and 5. The publication HTML returned 403 to the extractor; the source's issue metadata and digitized newspaper crops already retrieved by the scout were checked directly. Each four-period vector reconciles to the exact fixture date, NBA key, series number, both team IDs and both finals. Only the transcribed score facts and locators were committed to the local warehouse; no newspaper scans or articles are included.

| Game | NBA ID | Designated home quarters | Visitor quarters | Archive |
|---|---|---|---|---|
| 1 | `0046000411` | Boston 30/26/35/38 | St. Louis 26/22/25/22 | Apr. 3 [84] |
| 2 | `0046000412` | Boston 27/26/31/32 | St. Louis 32/20/23/33 | Apr. 6 [85] |
| 3 | `0046000413` | St. Louis 28/38/32/26 | Boston 31/35/31/23 | Apr. 9 [86] |
| 5 | `0046000415` | Boston 33/29/37/22 | St. Louis 39/22/23/28 | Apr. 12 [87] |

Derived overtime is false for these four games; the reported flags remain true and visibly disagree. Game 4's derived overtime stayed unknown in v7 because the scan's fine-print line was missed in the first pass. The v8 correction below supersedes that finding. The Christmas game also stays outside this import. No blanket permission to reproduce the archive is inferred; the Library of Congress notes that later issues may contain copyrighted third-party material.[88]

The archival route is useful but not an automatic parser. In 1957 Finals Game 7, OCR reads a St. Louis quarter as 20 and gives a six-point shortfall; the accepted local fourth quarter is 26, and the Boston vector agrees with the reported 125–123 double-overtime result. The game already has a published vector and was not written again.[89]

**ST-PERIOD-06 (high; same-total edits without a baseline):** a regulation game's recorded final does not identify its quarter scores. The v6 guards could detect a changed recovered score if its ledger stayed intact but could miss changes to older period rows, or edits to both a recovered row and its ledger. V7 fingerprints every accepted vector using an ordered sequence of team ID, period and points, and compares exact keys and digests. The separate `verify-inputs` command uses the SHA-256-pinned v6 parent and reads the external transcription, rather than trusting a resealable internal baseline. This protects lineage, not just sums. The ordinary command rejected a Q2→Q3 edit on `0010500001`; after updating that game's internal fingerprint to match the altered rows, `verify-inputs` still rejected the changed history.

**Fresh pass:** all **69** checks passed; a replay reproduced six relations. The historical spine still has **75,268** games, with **536,678** accepted team-period rows across **65,991** games; **9,277** remain without an accepted vector (8,647 unsupported, 626 invalid and four conflicting). The coverage view accounts for each game and shows **352** reported-versus-derived overtime disagreements and **716** games with a derived vector but no reported overtime flag. These indicators are not automatic corrections of the raw flag. V6 and raw inputs were not modified.

**Unadmitted technical leads:** a scout's v5-local ESPN comparison found 12 potentially usable vectors, but matched via date and designated teams, not an authoritative provider-key crosswalk for those seasons. Its maximum-score method did not independently verify clock boundaries. No new fetch or import was made. NBA PBP markers, scoring-play sums and legacy snapshots can corroborate many existing rows, but appended duplicates and late buzzer-beater rows can shift points between periods without changing the final. The scout reports disagree on the boundary behavior of `0021700793`; all four conflicting games remain held. Adjudication needs a fixture-level receipt or a proven independent period split, not majority vote over correlated projections.

## 2026–27 team box-score sample: four November 19 fixtures

**ST-BOX-01 (high; field disagreement hidden by correct finals):** the NBA box web pages and the official scorer reports are two league representations, not independent origins. The eight team-side counting vectors match after separating the website's `REB` field from scorer/player-line rebounds. In Clippers–Spurs, the website displays **40** and **47**, while `OREB+DREB`, the player-line sums and the scorer report give **34** and **36**. Adding the separately reported team rebounds gives 40 and 47 respectively, but this is not assumed as a global website rule. Both reported values are retained and `rebounds_agreed` is null for those two sides.[67][93]

The scorer report's player-line turnovers and total turnovers are separately named; they differ on **seven of eight** team-sides. Shooter counts and points reconcile to the exact fixture and totals; anonymous numeric website rows independently reproduce the player-line counts within the same upstream. Displayed player minutes sum to within one second of the scorer's team time; no minutes were adjusted. The Golden State–Memphis, Portland–Milwaukee, and New York–Minnesota website boxes are the other three comparisons.[90][91][92]

The direct gamebook download timed out, so the comparison used the already retrieved scorer-report text.[64][65][66]

**Result and fresh pass:** `data/nba-2026-27-box-check-2026-11-19.duckdb` contains eight compared team boxes across four games. The 226 existing finals, standings and period rows were preserved. `audit.team_box_game_coverage` marks 222 games `not_sampled`; it does not convert missing boxes to zeros. A replay matched seven relations. The verifier rejected an altered assist total with the final score unchanged, and no-clobber rejected an existing output path. No person-level records were added: not all rendered player rows carry a stable player ID. The new file is a local audit, not a publication or comprehensive box-score feed.

## V8: resolve five historical quarter-score gaps with fixture-level receipts

Four of five `deleg_de968920` assignments returned evidence. The crosswalk worker failed with a model concurrency error. The parent verified the two league gamebooks, the two contemporary newspaper articles and the archival Game 4 crop; no bulk collection was performed.

| Game | Home quarters | Visitor quarters | Evidence |
|---|---|---|---|
| `0021700793` | New Orleans 23/41/22/23 | Utah 34/36/29/34 | NBA scorer report [96] |
| `0022400072` | Portland 21/29/22/32 | Golden State 21/41/38/40 | NBA scorer report [97] |
| `0029600106` | Philadelphia 24/28/20/17 | Cleveland 23/25/31/26 | Contemporary cumulative scores [94] |
| `0029600674` | Seattle 15/21/26/33 | Utah 18/15/35/31 | Contemporary box with Utah Q4 reconciled, not verbatim [95] |
| `0046000414` | St. Louis 31/25/26/22 | Boston 28/24/39/28 | Digitized agate score block below the AP article [88] |

**ST-PERIOD-07 — no universal source priority:** the 2018 and 2024 scorer reports contradict stale play-boundary vectors that preserve each final. For the two 1996–97 games, the disputed `pts` values contradict shooting counts in their own rows, and the corrected four cells reproduce `2*FGM+3PM+FTM`. The 1997 newspaper prints Utah Q4 as **30** even though the final is **99**; the first three printed quarters imply **31**, which also follows from 9 FGM, 0 3PM, and 13 FTM. The inferred cell is explicitly tagged, not cited as a direct transcription.[95]

**Fresh pass:** 73 checks passed, and six replayed relations matched. Reintroducing Utah's stale 32/38 split failed despite preserving the final; resealing the internal receipt and vector hash still failed against the external CSV. Exact v7 game, score, standings and previously admitted period data were preserved. V8 has **536,718** accepted team-period rows for **65,996** games. **9,272** games still lack periods: **8,646** without a supported source and **626** with no admissible candidate. No previously held `valid_sources_disagree` case remains; this is not a claim that all published vectors have independent verification. All five 1961 Finals games now show four periods and `derived_overtime=false`, while the five raw OT flags remain true.

**Pending recovery (v8):** 17 residual bridge rows carried the `crosswalk_exact` label, but only ten had actual ESPN game IDs. The other seven have null provider IDs and are All-Star fixtures. Four older fixtures have date/team/score matches only. The normalized schedule stores a reported last period, not per-period scores.

## V9: finish 2025–26 regular-season period coverage

**ST-PERIOD-08 (high; crosswalk labels and stale boundary totals):** `crosswalk_exact` is not sufficient without a non-null provider ID and a one-to-one key on both sides. The ten linked regular-season games were checked against the original NBA scorer reports. All 80 team-quarter cells reconcile to their source and final; exact NBA/ESPN keys, dates, teams, status and both finals independently reconcile to the local schedule and stored provider bridge. No new ESPN retrieval ran. The returned official reports, not the locally cached ESPN marker vectors, provide the admitted values.

| NBA game ID | Home Q1–Q4 | Away Q1–Q4 | Official scorer report |
|---|---|---|---|
| `0022500029` | 26/34/27/28 | 39/37/33/39 | [98] |
| `0022500055` | 44/24/20/24 | 30/37/43/34 | [99] |
| `0022500379` | 41/37/28/29 | 32/41/29/41 | [100] |
| `0022500443` | 25/35/24/32 | 27/32/29/24 | [101] |
| `0022500693` | 26/26/45/29 | 25/22/32/34 | [102] |
| `0022500753` | 20/21/38/26 | 21/21/29/28 | [103] |
| `0022500849` | 36/37/26/25 | 35/22/34/26 | [104] |
| `0022501017` | 21/36/30/28 | 26/24/23/28 | [105] |
| `0022501031` | 26/26/30/23 | 15/42/24/27 | [106] |
| `0022501127` | 31/27/34/35 | 38/33/40/27 | [107] |

**Comparison:** local zero-clock `End Period` markers agree on seven games. They understate Washington by one point against Cleveland and the Lakers by two points at Utah. Milwaukee–Phoenix has an unusable marker shape: only three period ends qualify, so differencing a later cumulative value would fold two periods together. The candidate vectors and dispositions stay visible in `audit.official_period_comparison`.[98][100][106]

**Fresh pass:** all **78** checks passed; a replay matched six relations. Moving one Washington point between quarters with the same final, substituting a different ESPN ID, and trying to overwrite v9 were all rejected. The v8 spine, finals, standings and all previously accepted vectors were preserved. V9 covers **1,230 of 1,230** 2025–26 regular-season games and **66,006** historical games overall, with **536,798** team-period rows. **9,262** historical games lack an accepted vector. The only remaining 2025–26 exceptions are seven All-Star weekend fixtures; their format requires its own scorer-report evidence, not a four-quarter assumption. Background batch `deleg_ec13127b` has five assignments for independent gamebook and All-Star review; these results were verified separately and do not rely on completed agents.

## V10: recover the seven 2026 All-Star weekend period vectors

**ST-PERIOD-09 (high; wrong fixture or period type):** The NBA's Rising Stars rules set a target of 40 for each semifinal and 25 for the championship. The main All-Star tournament used one 12-minute period per game, with untimed overtime to a target of five when tied. These are not four-quarter fixtures.[111][112]

| NBA game ID | Stage | Home periods | Away periods | Official evidence |
|---|---|---|---|---|
| `0032500004` | Rising Stars semifinal | 40 | 34 | [108] |
| `0032500005` | Rising Stars semifinal | 36 | 41 | [109] |
| `0032500006` | Rising Stars final | 24 | 25 | [110] |
| `0032500011` | All-Star round robin | 32 / 5 OT | 32 / 3 OT | [113] |
| `0032500021` | All-Star round robin | 42 | 40 | [116] |
| `0032500031` | All-Star round robin | 48 | 45 | [114] |
| `0032500041` | All-Star final | 21 | 47 | [115] |

The Stripes–Stars round-robin game and final share the same date and designated sides. The `STRSTP` scorer report is the 21–47 final, not the 42–40 round-robin game. The latter uses its separately keyed NBA box page and the local play stream's single-period boundary; no separate scorer report for it was established. This is corroboration within NBA-origin data, not a second independent provider.[115][116]

The release joins by `game_id`, checks all 16 cells against official transcriptions and ordered local play boundaries, and keeps reported OT separate from derived OT. Five inherited raw `true` flags disagree with the new period-derived flags. The release verifier and replay pin the external transcription and v9 file; they do not hash the original NBA page/PDF bytes. The displayed format and phase labels are checked independently from the scores so a relabeled target-score period cannot pass on unchanged totals.

**Fresh pass:** 87 checks passed; nine relations replayed identically. The ordinary verifier rejected a same-final point move from untimed OT into regulation, timed/regulation labels replacing the true period labels, and a deleted receipt table. `verify-inputs` rejected substituting the championship PDF for the round-robin box; the builder rejected an overwrite. V9 still passes its 78 checks. The 2025–26 spine has no period gaps. Overall, **66,013 of 75,268** games have accepted periods and **9,255** do not: 8,646 have no supported source in the assessed data, and 609 have no valid candidate. This is not validation of all boxes, plays, or shots.

**Next evidence boundary:** Five fresh agents in batch `deleg_87c8236d` cover early BAA, early 1960s, the 1975–77 gap, later invalid local period candidates, and independent review of the All-Star gate. No findings from that batch were used to admit these fixtures.

## V11: recover 1,640 games from the supplied team-box mirror

**ST-PERIOD-10 (high; aggregate overtime hides missing cells):** The prior selector had no candidate for either regular season: the raw `fact_game_line_score` contains no `00275` or `00276` fixture, but `main.stg_kaggle_team_box` has both team keys, date, designated side, opponent, and final for all 738 and 902 games, respectively. Ingest metadata names `data/external/TeamStatistics.csv` under `kaggle_nba`; the source dimension records derivative provenance. We verified 142 cells in 16 retained NBA game-page captures, including two overtimes in `0027500014`, against the local mirror. That confirms sampled fidelity to the same upstream family, not season-wide independent corroboration.[20][117][118]

The first build rejected `0027500097`: Washington and Philadelphia were tied after regulation and both recorded overtimes, but their combined OT totals exceeded the two observed slots. The NBA's keyed game summary identifies **three** OTs and prints Washington's third-OT **7** and Philadelphia's **8**. Only those two missing cells were transcribed, not inferred by assuming that the entire remainder belonged to one period. The release separately attributes that vector as `local_team_box_mirror_plus_official_tail`.[119]

**Fresh pass, fixture-auditor perspective:** 98 checks passed, ten relations reproduced exactly, and the original snapshot and v10 stayed unchanged. Same-final cell shifts, fractional source values that would round to unchanged integers, deleted third-OT rows, a renamed All-Star squad, and a missing receipt manifest entry were rejected. A same-final mutation that also resealed the stored mirror and baseline passed internal consistency but was rejected by `verify-inputs` against the original read-only source. The new ledger pins the prior DuckDB, supplied DuckDB, and official transcription CSV by SHA-256. It does not pin the original NBA HTML bytes or authorize republication.[6]

**Coverage:** 13,278 new rows, 550,092 overall; **67,653 of 75,268** games covered. Both scoped regular seasons are complete on the existing spine. Of the **7,615** remaining period gaps, 7,006 still have no supported source in the current pipeline and 609 have no valid candidate. Finals, team IDs, standings, and existing period vectors did not change. The 24 missing raw overtime flags in this cohort were preserved; the published vectors supply the derived flags.

**Five-agent batch disposition (`deleg_87c8236d`, five returned):** The 1975–77 mirror is admitted with the OT3 correction above. For 1960–62, the sampled Washington newspaper issues corroborated some finals and OT counts, not the requested quarter cells. A fresh local query found mirror rows for all 676 of those season keys but also missing regulation cells and nonreconciling totals; none were admitted. The same query found complete four-quarter inputs and matching sums for all 1,102 regular-season games of 1966–67 and 1970–71, pending identity, vector and OT gates. Later composite regulation-plus-OT candidates and early BAA secondary transcriptions are leads, not published facts. The All-Star review's team-name false green is fixed in the shared gate; raw versus derived OT and incomplete external artifact pinning remain disclosed rather than silently rewritten.

## V12: four historical regular seasons and six keyed overtime fixes

**ST-PERIOD-11 (high; clean totals do not validate all periods):** Audit the entire four-season regular cohort, not just rows that survived the unpivot. Require two reciprocal team rows, exact IDs, date and finals, finite integral cells, both sides' explicit OT slots, equal slot shape, tie before each overtime, and no paired zero period. Compare the published row multisets to the source ledger and compare that ledger to the SHA-256-pinned read-only snapshot. Neither the parent finals nor a balanced alteration of both vectors independently proves the underlying period values.

| Regular season | Fixture keys | Admitted | Held | Team-period rows added |
|---|---:|---:|---:|---:|
| 1960–61 | 316 | 303 | 13 | 2,454 |
| 1961–62 | 360 | 345 | 15 | 2,788 |
| 1966–67 | 405 | 405 | 0 | 3,282 |
| 1970–71 | 697 | 697 | 0 | 5,634 |

The 1966–67 sample matched 28 cells in three Baltimore home-game line scores, including a genuine `30/30/30/30` team vector and a double overtime. It does not validate all 405 games independently.[120][121][122]

Two keyed 1970–71 NBA game summaries matched all 18 checked cells. Boston–Baltimore `0027000523` had one overtime even though the original warehouse flag was missing. These NBA pages and the mirror are correlated, not independent providers.[129][130]

The supplied mirror also matches 40 of 40 newspaper-backed cells from the already admitted 1961 Finals. That control is out-of-family but not drawn from the regular season. The 28 remaining 1960–62 holds are 16 nonreconciling totals, five missing quarter vectors, five zero-filled vectors, one truncated overtime sequence (`0026100105`), and one OT aggregate that duplicates the game total (`0026000023`). `0026100243` remains held: both summed Lakers quarters and their made-shot arithmetic say 127, but the existing Lakers final is 123. Neither the conflicting final nor any period was changed.

**ST-PERIOD-12 (high; a missing play-feed marker is not a missing game):** Six later fixtures use exact NBA `periods[]` instead of splicing backup quarters and PBP. Home and away vectors match the local mirror. The original HTML bytes, game key, Eastern date, side IDs, finals, ordinal and `periodType` were checked against the CSV pins. The 2005 San Antonio–Milwaukee fixture has OT **9–7** in the keyed line score despite the missing end action. Miami–Sacramento has two distinct OTs, **10–10** then **8–11** in home/away order. These receipts add 62 cells; they are not represented as independently sourced scorer reports.[123][124][125]

The other keyed receipts are Houston–New Jersey, Lakers–Kings and Phoenix–San Antonio. A later play-feed row carrying `0–0` is not a substitute for an ordered period boundary. The release neither edits nor backfills the play stream.[126][127][128]

**Fresh pass, fixture-auditor perspective:** 107 checks passed; 13 relations reproduced exactly. The shared verifier rejected a same-final quarter change with a resealed fact, ledger and baseline for a keyed-OT fixture; it also rejected reciprocal side reversal and relabeling a held incomplete OT as published. Original HTML with changed bytes failed capture verification. The builder refused overwrite; v11 and the original input were unchanged. There are **564,312** team-period rows across **69,409 of 75,268** games, up **1,756** games. Of **5,859** period gaps, 5,228 have no recognized source in the current pipeline and 631 have an invalid candidate. A valid mirror-shaped observation is not redistribution clearance.[6][20]

**Five-agent batch disposition (`deleg_54c24e8d`, five returned):** Three older-season investigations assessed the four regular-season cohorts; the fourth agent checked the six later fixtures. The fifth investigated two 1947 Finals vectors and the March 25, 1948 tiebreaker. Those fixtures stayed held: the scout found contemporary final-score reporting, but no accessible primary page with those quarter cells. A separate shape inventory found thousands of still-unassessed mirrored rows, including 2,879 remaining regular fixtures in season-start decades 1970–1999 with two non-null four-quarter vectors. Shape alone is not an admission gate.

## V13: apply the unchanged mirror gate to two larger gap cohorts

**ST-PERIOD-13 (high; accepted finals can hide impossible period splits):** Freeze the exact v12 gap keys in the two scopes, then apply the v12 assessment without relaxing side, date, final, period, or OT checks. This is admission from the supplied derivative feed, not independent corroboration or clearance to redistribute.[6][20]

| Missing period scope in v12 | Assessed | Admitted | Held | Team-period rows added |
|---|---:|---:|---:|---:|
| Regular season-start years 1970–1999 | 2,879 | 2,869 | 10 | 23,282 |
| Playoffs from season-start 1954 onward | 482 | 271 | 211 | 2,232 |

The regular-season holds include four one-point discrepancies before overtime even though the vectors sum to their finals, two missing OT tails, two source identity/final mismatches, and two unreconciled vectors. The playoff holds are 198 raw OT flag disagreements, ten cell-total discrepancies, and three fixture/final mismatches. The 198 are not silently treated as regular-time games. Five of the new hold dispositions overlap failed v6 reversed-line candidates; the v6 ledgers are preserved and only the current assessment moved from unsupported to invalid. The verifier allows that precise later hold outcome, not a blanket override of v6.

**Fresh pass, fixture-auditor perspective:** 116 checks passed; 12 relations reproduced exactly. Same-final point shifts, an attempted promotion of an untied OT fixture, and an unjustified change to an overlapping v6 hold were rejected. Resealing both the mirror ledger and the cells passed internal consistency; exact comparison to the pinned raw snapshot rejected it. The parent and original database are unchanged. **72,549 of 75,268** historical games now have accepted period vectors (589,826 rows). Of the **2,719** remaining, 1,871 have no recognized source in the current pipeline and 848 have an invalid candidate.

**Five-agent batch disposition (`deleg_608a5239`, five returned):** The regular and playoff cohorts were integrated as above. A fresh local query found 427 missing early fixtures with two complete-shaped mirror rows in the 1946–59 scoped range, contradicting the scout's claim of no mirror period keys there; none were admitted on shape alone. Three 1960–62 RealGM transcriptions remain secondary evidence. `0026000023` has a home/away contradiction between RealGM's title and the warehouse, `0026100105` has a plausible printed third OT, and `0026100243` has a plausible printed OT vector; the v13 player-box sums separately support the existing 123–122 final but not that period split.[131][132][133]

**Non-period follow-up:** In the installed release, all 85 2025–26 playoff games have one zero-clock end marker per period and keyed final scores; **52 of 85** have published shot charts. Do not confuse shot-chart coverage with play-by-play coverage. The next local shot candidate pool is 64 2019–20 regular games with 14,293 staged observations and 11,301 non-free-throw observations. Two repeated tip-shot payloads account for four of those rows; the staging schema has no play ID, and `score_value=0` on missed attempts does not determine their shot value. Those candidates still need identity, multiplicity, two-/three-point value and geometry checks; no shot rows were added in v13.

## V14: early mirror periods and two original NBA third-overtime receipts

**ST-PERIOD-14 (high; a missing transformed table is not a missing source):** Reassessed 2,306 gaps in 1946–69 regular games and pre-1954 playoffs, excluding the 28 already recorded v12 holds. The raw `main.stg_kaggle_team_box` has exactly two keyed sides on 1,196 fixtures, not zero. The unchanged mirror gate admits 835 games (6,776 rows), rejects 361 with explicit candidate defects, and accounts for the 1,110 with no mirror key. The NBA's single-game line score confirms the one-sided zero in overtime for `0045000221`; it was not manufactured or dropped as padding.[136]

**ST-PERIOD-15 (high; missing overtime ordinal):** Official line scores for `0027700297` and `0029200096` match six exact mirrored periods on both sides and identify a seventh. Home/away OT3 cells are **15/13** for Milwaukee–New York and **8/7** for Philadelphia–Miami. Both games are tied at the end of regulation, OT1, and OT2. The old v13 holds stay in the historical assessment; only the exact keyed receipts change the current disposition. No arena label or aggregate OT remainder is used as proof.[134][135]

**Fresh pass:** 127 checks passed and 14 relations reproduced. A same-final edit to the OT2/OT3 split, a deleted official receipt, a removed early candidate vector, and a wrong receipt locator were rejected. The overwrite guard held. **73,386 of 75,268** games now have accepted periods (596,630 rows); **1,882** remain (859 with no supported source, 1,023 with no valid candidate). The new external receipt CSV is pinned; original NBA HTML bytes are not part of the release.

## V15: recover attempts without inventing shot charts

**ST-SHOT-01 (high; a repeated locator can represent multiple real shots):** Frozen scope is the 68 regular-season games in 2019–20 with no published shot-chart row. Use the existing NBA `fact_play_event` action key rather than assigning identity from an ESPN payload or sorting same-clock attempts. Two blocked or missed events at an identical clock can be separate keyed actions. For `0021900889`, actions 381 and 383 both survive; for `0021900925`, actions 491 and 493 both survive. The 379 admitted unclassified missed-shot actions have a typed `2pt`/`3pt` source action and explicit miss description; `NULL` outcome alone does not mean a miss.

**ST-SHOT-02 (high; event totals and location claims are separate):** Require one zero-clock end per period and a matching final, exact actor/team membership, and FGA, FGM, 3PA and 3PM agreement at both team–game and player–game grains. The 67 admitted games contribute **11,819 attempts**; hold all 183 candidate events for `0021900891` because one player's PBP has an extra missed three relative to both the player and team boxes. `nba.field_goal_attempt` exposes original chart attempts and added action-grain attempts with distinct source labels. The supplement retains fractional clocks and publishes no coordinates or distance. Existing shot-chart facts, shot links, and chart coverage are not expanded by this view.

**Fresh pass:** 134 checks passed and eight relations reproduced on the supplemented scope. The verifier rejected a two-/three-point swap that preserved player box counts, deletion of one valid same-clock tip, relabeling unavailable geometry, and publication of the held fixture. Against the SHA-256-pinned v14 file, the scoped events and box inputs, all chart rows, and the period facts match exactly. The combined attempt view now has observations for **1,058 of 1,059** 2019–20 regular games; it is not independent validation of NBA-origin facts, complete spatial coverage, or an ESPN import.

**Five-agent batch disposition (`deleg_48bd5e7f`, five returned):** The early mirror and two keyed OT findings informed v14. The 1960 Philadelphia–Detroit aggregate and side conflict remains held. The 198 playoff flag holds have a promising secondary schedule comparison but remain held pending replay of the full dated fixture receipts. Both shot investigations informed v15; the ESPN observations remain comparison evidence only. Three games without any ESPN bridge were recovered from their actual NBA events, while a bridged game without an ESPN shot capture remains held on the NBA event/box discrepancy.

## V16: adjudicate playoff overtime flags and extend modern attempt coverage

**ST-PERIOD-16 (high; a bad reported flag can block an otherwise valid vector):** Five cached Basketball-Reference playoff schedules contain 235 games. The release joins 232 by exact date, designated home and away franchise after explicit historical aliases, and both final scores; no score-only fallback or date tolerance is used. All 198 scoped flag conflicts match unmarked schedule rows. All 15 marked OT controls match the exact warehouse keys and overtime count, including one 2OT game. The parsed tables and their pins are retained locally; original HTML is unavailable, and no live Basketball-Reference crawl ran.[139][140][141]

For 1994 and 1996 the same gate uses the two remaining cached schedules. The 198 four-quarter vectors still come from the local mirror; the schedule comparison corroborates regulation length, not the individual quarterly values. The 198 raw overtime flags are still `true`. The three unmatched final-score conflicts (`0046000222`, `0046800323`, `0049500046`) remain held and no finals were edited.[142][143]

The keyed NBA line score for `0026100105` supplies 14 exact cells including third overtime **14–18** (home/away). Its first six cells per side match the mirror. `0026100243` remains held: the contemporary results line confirms overtime and the existing 123–122 final, but does not print the period split.[137][144]

**ST-SHOT-03 (high; a chart gap and a missing FGA are different):** The unchanged candidate and box reconciliation gates add **5,663** action-grain attempts for 33 2025–26 playoff games and **901** for five 2024–25 regular-season games. All 6,564 have explicit field-goal outcome and value; geometry remains unavailable. The unified view now has observations for all 85 playoff games and all 1,230 regular-season games in those respective cohorts; the chart table still covers only 52 and 1,225 respectively. NBA-origin events and NBA-origin boxes are consistency checks, not independent sources.

The prior 2019–20 hold `0021900891` stays held. Event 476 is a zero-clock Morris three-point miss with an unresolved field-goal flag; zero-clock attempts elsewhere are valid, so that clock is not an exclusion rule. The official Final Box lists Morris at **2-for-8 and 1-for-5 from three**, rather than the event-derived nine attempts and six threes. The discrepancy is logged, not resolved by deleting an action or assuming when it was released.[138]

**Fresh pass, fixture-auditor perspective:** V16 passed 149 checks and 16 relations reproduced on an offline rebuild. The gates rejected changing a 2OT marker to OT without changing the count of marked games, moving a point between quarters without changing the final, deleting one of two genuine same-clock attempts, and shrinking the modern scope. Swapping the OT markers in the archived input was rejected by its table digest even with counts preserved. The overwrite guard held. The release has **598,228** team-period rows across **73,585** games, leaving **1,683** period gaps. The attempt supplement totals **18,383** actions in **105** games. The v15 parent still passes its 134 checks.

**Five-agent batch disposition (`deleg_bcb55dad`, five returned):** The reviewed cached schedules, early OT3 and modern attempt findings are integrated. The 2019–20 hold has a named event and scorer-report evidence but remains unresolved. The team-box audit identified 20 modern pairs as an actionable next recovery; no team-box facts were changed in this release.

## V17: recover historical boxes without changing field conventions

**ST-BOX-01 (high; compatible keys do not imply compatible fields):** The ten v16 regular-season games without any team box in 2024–25 and 2025–26 contribute 20 rows. Deduplicate `data/nba.duckdb:main.fact_box_score_team` on the complete numeric payload and game/team key, ignoring display metadata that duplicates across franchise versions; a numeric disagreement blocks admission. Exact designated side, date, both finals, all 80 quarter observations, and traditional counting totals reconcile against the keyed v16 game, period, and player facts. `tov` remains the team total. Seven sides have one or two more turnovers than the player-line count; Miami in `0022400147` is 14 versus 13. The team ledger retains both. `min` is 240 team-player minutes, not the provider's 48 elapsed minutes. Six named situational cells use `main.stg_kaggle_team_box`; advanced and other unsupported fields stay NULL. The observations share an upstream source family and are not independent validation.

**ST-BOX-02 (high; four named rosters are not five):** Replaying `main.fact_player_game_boxscore` against the keyed traditional and staging siblings yields 116 candidate rows across five All-Star games. Publish 88 rows for `0035100001`, `0035200001`, `0039900001`, and `0030400001`. Both sides of each admitted game total exactly 240 minutes and match the existing final; shared counting stats also match all four team rows available for the two older games. Use the spine date and sides, not the traditional sibling's bad century labels (`2051-52`, `2052-53`, `2099-00`). Era-inapplicable fields and unsubstantiated zero plus-minus are withheld. In `0030300001`, Paul Pierce's `13.6` decodes to an invalid seconds component and the West minutes sum to 241:52; all 24 named rows stay held. Across that game and `0039900001` four unidentified placeholders stay held, not promoted to DNPs. A prior scout reported a 2005 Iverson one-attempt PBP discrepancy; no play or box cell was changed to force agreement.

**Fresh pass, data-lineage auditor:** V17 passed 158 checks; 18 relations replayed against the pinned v16 and original DuckDB inputs, including both complete box facts and all original disposition ledgers. The verifier rejected substituting player-line turnovers, deleting a team row, offsetting individual minutes without changing team totals, and removing a required ledger. Input replay rejected populating an unsupported rating even after the publication ledger was edited to match. No-clobber held. The result has 150,257 of 150,536 team rows and 1,703,402 player rows. Eleven games still have no player rows (ten locally unavailable and one held); this is not a player-side completeness claim. No finals or period scores were altered; 1,683 period gaps remain.

**Five-agent batch disposition (`deleg_a461d722`, five returned):** Team and player findings informed V17. Three playoff finals have conflicting local and external observations; none was corrected in V17. A report that the 1996 game was tied after the third quarter corroborates the cumulative boundary, not the earlier individual quarters. Two earlier line-score header conflicts and the legacy event-action cohort need exact receipt replay before admission. A further five-agent pass (`deleg_de2e8ed2`) is checking those gaps, the 2004 minutes, and remaining plausible local team boxes.

## V18: recover explicit legacy attempts without extending the shot chart

**ST-SHOT-04 (high; short-label selection silently drops legacy shots):** The exact raw gap is 11 regular-season fixtures from **2000–01**, not 1999–00. A date-only filter would also capture charted games. Compare 4,908 original action keys and normalize padding only for classification; keep the raw action label and ISO clock on the published row. Value and outcome are explicit, never derived from the description or the fallback for a modern three. Each attempt is between its period's unique start and end markers. One later-appended rebound is not an attempt and does not invalidate any made or missed shot. Cross-check using both team and player boxes via full joins (22 and 262 rows), period-by-period cumulative continuity, and the exact order and score of all 88 boundary rows.

**Fresh pass, event-lineage auditor:** V18 passed 168 checks and reproduced the exact delta and 14 inherited relations, including the full chart and shot-event link multisets. Missing scope and action rows, a changed shot value, and a normalized-but-nonoriginal clock string were rejected. A same-player swap of the two shot values preserved shooting totals and was resealed in the event source; replay rejected it against the original raw actions. V18 publishes 1,745 attempts (764 made, 277 three-point attempts, 102 made threes), bringing the PBP supplement to 20,128 attempts in 116 games. The unified view covers all 1,189 regular games in that season; shot-chart coverage remains 1,178. This is within-provider consistency, not independent event verification or a location backfill.

## V19: fix bad summary finals without treating quarter cells as inference

**ST-PERIOD-17 (high; even a league page can contradict its own line score):** For `0024600016` and `0025700109`, the league pages print the exact four quarters on each side but give Chicago 64 and Boston 116 in the Final column.[145][146]
The exact team-keyed vectors corroborate the original local line cells after correcting their side orientation. Player scoring arithmetic and the compiled boxes support the already published 62–54 and 98–115 (home–away); neither final was changed.[153][154]

Three playoff finals were corrected in home–away order: `0046000222` from 120–118 to **127–118**, `0046800323` from 99–80 to **99–86**, and `0049500046` from 96–102 to **96–103**. Each league page prints four explicit quarters on both sides whose sums differ from its summary final.[147][148][149]
Local player-line shooting arithmetic and the pinned playoff schedules corroborate the corrected totals.[139][141][143]
The cohort review also identified the dated AP report (1961, final and half), Atlanta Journal-Constitution caption (1969, final only), and Chicago Tribune report (1996, final, half, and end of Q3). These receipts constrain cumulative scores, not every quarterly cell; the individual cells are attributed to the NBA pages, and the local mirror shares their upstream source. The LOC HTML returned HTTP 403 on parent refetch; the parent replay uses the pinned schedule and transcribed league cells, not a claim to have retrieved the original newspaper scan.[150][151][152]

**Fresh pass, results-lineage auditor:** V19 passed 176 checks plus 19 parent-relation comparisons. It preserves historical hold classifications by evaluating the v13 mirror against the finals available in v13, while the current schedule comparison matches 235/235 playoff fixtures. The verifier rejected a same-final quarter shift, the old Orlando final, and a deleted receipt. Replay rejected a shift resealed in both the receipt ledger and the period baseline; no-clobber held. Regular-season standings, all winners, fixture IDs, raw flags, box facts and chart geometry are unchanged. The release has 598,268 period rows across 73,590 games; 1,678 games remain without accepted periods.

**Five-agent batch disposition (`deleg_de2e8ed2`, five returned):** Legacy attempts informed V18; both early header cases and three playoff conflicts informed V19. The 2004 All-Star comparison has minute-level values for all 24 named players, including alternative values for Brad Miller and Andrei Kirilenko, but the exact timing source is a compiled box, not a retrieved NBA scorer report; no player cells were changed in these releases.[155] The five preseason team-box fixtures are a separate candidate slice: the prior scout's claim of universal agreement was contradicted by team-only turnovers on two sides; the follow-up (`deleg_b408c703`) is checking field definitions and further source gaps.

## V20: restore keyed play and chart observations for two games

**ST-EVENT-02 (high; an event identifier survives repeated coordinates):** Four pinned local archives from the same NBA Stats feed family provide 429 events and 151 charts for `0020600975` and 413 events and 150 charts for `0020700753`. Join original `EVENTNUM` to `GAME_EVENT_ID` only within the exact game. Roster and game-team keys resolve primary actors; score pairs are visitor–home in the archive, not designated-home first. Both sides' field goals, threes, free throws, and period boundaries reconcile to the local release; every chart action matches the original event by team, person, period, clock, value, and outcome. For 88 zero-origin observations the attempt and link survive but coordinates and distance remain NULL. This repairs missing capture, not an independent validation source or permission to redistribute.[21]

**Fresh pass, event-lineage auditor:** V20 passed 189 checks and replayed the four original archives plus 15 inherited relations. It rejected a deleted native link, a public projection omitting the new links, an action-ID swap preserving link counts, and a modified raw field not used in the curated projection. The no-clobber guard held. All 1,230 regular games in each of 2006–07 and 2007–08 now have published plays and chart shots. The inherited fixture, final, period, standings, and box facts are unchanged.

**Remaining coverage:** The v20 per-game inventory has 1,678 period gaps (1,437 before 1960), 279 missing team rows, 11 games without player rows, and 18 games with rows for only one side. Before 1996–97 there are 35,196 games outside the current event/chart coverage. From 1996–97 onward, 905 games lack PBP and 1,129 lack charts. An opportunity to corroborate box scores or periods is not an opportunity to reconstruct an absent event stream or court location.

**Basketball-Reference scope:** The bot policy states a 20-request-per-minute threshold for sites other than FBref and Stathead; that is a traffic limit, not authorization to make a substitute database or use site content for model prompting. No live Basketball-Reference backfill was started. If use is authorized for this project, match precisely scoped box or period cells by exact fixture/side first, cache and attribute them, and stop on blocks. Existing local aggregate comparisons remain separate and do not establish game-level coverage.[25][26][27]

**Five-agent batch disposition (`deleg_b408c703`):** Three returned reports and two were interrupted. The keyed archive findings informed V20. The preseason mirror boxes and early side-orientation receipts are not yet admitted. The interrupted 1951 All-Star investigation left a candidate transcription that still needs full image verification. Five follow-up researchers are checking primary-source fills without polling Basketball-Reference (`deleg_801af6ee`).

## V21: paced Basketball-Reference period checks for personal use

**ST-PERIOD-18 (high; a same-score box is not enough):** A single cache-first collector checks specific historical box pages against the final spine. The observed first-pass cache timestamps are 6.52–6.59 seconds apart; the collector shares a host-wide lock and cooldown across its cache directories, checks `robots.txt`, rejects unexpected redirects, and stops on a block. Approval for this personal project does not grant redistribution rights. Provider traffic and usage rules remain separate.[25][26][27]

The November 15 and 22, 1951 boxes match exact dates, historical team abbreviations, designated sides, finals, and all local period cells once the mismatched source columns are rebound. They add 16 cells for `0025100035` and `0025100052` without changing either final.[156][157]
The 1957 neutral-site comparison gives Boston rather than Syracuse the designated home side, so `0025600127` stays held. The 1947 box prints Pittsburgh Q4 as 14, one more than the local cell; `0024600169` remains held pending stronger confirmation rather than selecting the value just because it closes the final.[158][159]

**Fresh pass:** V21 passed 194 checks; exact HTML digests and parsed receipts replayed offline. A changed page date, a same-final quarter shift, and the same shift resealed in both the receipt and baseline were rejected. A second live queue returned seven exact fixture-and-final matches with printed periods; those observations still need comparison with local cells and an admission decision. V21 leaves 1,676 period gaps. Team/player box coverage and event/chart coverage did not change.

## V22: review the seven keyed historical receipts

**ST-PERIOD-19 (high; exact finals are necessary, not sufficient):** The local team IDs and final headers match `0025100086` and `0025200313`, but each team's local quarter vector belongs to the opposite side. The separately printed line scores corroborate the full reorientation; the finals and teams do not change.[160][161]
The other five receipts pass the fixture and final checks and disagree with at least one local quarter cell even after reorientation (`0025400246`, `0025500205`, `0025500208`, `0025500269`, `0025900028`). V22 holds these rather than guessing an individual cell from a final score.

**Fresh pass, score-lineage auditor:** 199 checks and exact replay from seven private original receipts passed. The gate rejected a same-final quarter transfer and a one-byte change to an original page. Unchanged data: source snapshot, results, standings, box scores, plays and shots. V22 has 598,300 period rows for 73,594 games; 1,674 games still lack periods (1,433 before 1960), 279 team rows are missing, and 11 games have no player box. There is no global BRef row-level validation claim.

## Repeatable local current-season refresh through November 23, 2026 (Eastern)

**ST-CURRENT-01 (high; presence is not adjudication):** Direct NBA HTML and CDN JSON probes returned HTTP 403. The alternative uses eight publicly released, digest-checked SportsDataverse Parquets (schedule, three crosswalks, team boxes, player boxes, plays, and shots). These are snapshot assets, not a promise of a live or licensed feed.[48][54][162]
Player boxes and plays use ESPN upstream; the chart's common fields match the shooting rows in the play feed as an exact multiset, so the chart is not independent corroboration.[23][163][164]
The workspace's 226 previously admitted finals match the new schedule exactly (key, date, both team sides and scores, Cup group, neutral flag). The new cutoff includes 32 additional finals (9, 6, 7 and 10 from November 20 through 23). The ESPN-to-NBA crosswalk for Portland's Yang Hansen is supported by the keyed NBA player page and the November 19 scorer report; no global name-reversal heuristic was added.[165][167]

The release holds all 447 source plays for `0022600272`: each source period boundary understates Minnesota by two points, ending at 97 instead of 99. The separate box and line score agree with the NBA scorer's 102–99 final and remain admitted.[166]
The pinned gamebook replay in `warehouse/verify_current_scorer_discrepancy.py` finds 99 score changes to the feed's 98. The extra change is Minnesota's LaMelo Ball layup at Q1 `10:19`, on page 9; after omitting that entry, the score changes agree in exact order by period, displayed clock, side and points. This does not establish every shooter, non-scoring play, attempt or coordinate, so the full stream remains held.[166]

**ST-CURRENT-03 (shot attribution):** `warehouse/audit_current_shot_attempts.py` replays all 166 field-goal observations for that fixture from the pinned gamebook. Of 164 player-credited attempts, 163 match the provider in sequence at exact period, clock, side, shooter, value and outcome; the missing Ball layup is the other. The two remaining observations are missed **team heaves**: the Knicks at Q1 `0:00.2` (71 feet, page 10) and Timberwolves at Q2 `0:00.1` (69 feet, page 13). The NBA scorer lists one separate `TM FGA` for each team; ESPN attributes those events as missed player threes to Mikal Bridges (`401910021165`) and Trey Lyles (`401910021326`).[166][66]
The exact reconstruction explains all three entries in this game's `audit.shot_box_conflict`. It matches FGA, FGM, 3PA and 3PM on 75 individual quarter lines, 19 final player lines and both teams. An offline replay rejected a changed PDF and a same-quarter shooter swap in both sides of the receipt that preserved the aggregates. This is a local audit receipt, not admission of canonical plays, attempts or coordinates.[166][66]

**ST-CURRENT-04 (non-shot events):** The next audit replays 221 provider actions across four families. All 28 free throws and 54 in-period substitutions match by equivalent event, keyed player(s) and clock. The 12 provider-only substitutions at later quarter starts exactly reproduce the two teams' six gamebook opening lineups when applied to the previous period's reconstructed closing lineups; they are not missing in-period actions. ESPN matches 32 of 33 official turnovers and omits OG Anunoby's bad pass and Jaden McDaniels's steal at Q1 `11:10` (page 9).[166]

**ST-CURRENT-05 (rebounds and event clocks):** The audits link all 79 player rebounds individually to a shared preceding missed shot, rejecting nonunique links and intervening turnovers or free throws. The sources agree on 78 rebounders and on only one rebound clock. On page 16, Tyler Kolek's Q4 `10:42` miss is followed by a `10:38` rebound credited to Jaden McDaniels. ESPN credits Trey Lyles at `10:42` (`401910021525`). The NBA play pages list 14 team rebounds, while the NBA final box lists 16 separately from individual rebounds; do not infer the two missing timeline entries from the box. The gamebook's individual FTA, FTM, REB, TO and STL match all 75 quarter and 19 final player lines; total turnovers separately match each team's official box. The local receipt replay rejects a same-count substitution swap and changed PDF. No canonical actions changed.[166][66]

The other 257 play streams reconcile at the boundaries. Totals: 258 finals, 2,094 period cells, 516 team boxes, 5,772 played and 1,062 DNP lines, 128,048 plays, and 45,452 accepted field-goal attempts. Nine DNPs lack an exact person crosswalk and one zero-stat player has contradictory participation fields; all ten are held. Source player minutes are whole-minute observations only. On the eight prior official team sides, the refreshed team box matches 128 checked field cells, including both player-line and team-total turnovers where compared.[166][167]

**ST-CURRENT-02 (high; geometry and scorer attribution differ):** Keep 45,617 provider coordinate observations separate from canonical NBA geometry; 16 at `(0,0)` are ambiguous and the held play stream's coordinates are audit-only. There are 258 player-game FGA/FGM/3PA/3PM conflicts across 179 games. The league's heave statistic is a plausible explanation for some extra attempts, not a license to remove every last-second attempt or to explain made-shot differences.[168]

**Fresh pass, ingest auditor:** The capture command reproduced all eight asset digests and metadata. The verified cutoff build replayed 25 relations by exact multisets and schema; the harness rejected a same-final cell shift, a single-player minute edit, a missing coordinate and a Cup flag swap with unchanged count. It refused to overwrite a release or a path with a WAL. A November 19 backtest and November 23 extension preserved the exact existing finals. The shared NBA query schema returns 75,526 historical-plus-current games and keeps the sources read-only. No scheduled collector, independent full-game validation, preseason coverage, or commercial/redistribution clearance is claimed. The NBA and Disney upstream terms and the mirror's accessibility are separate questions.[6][71]

## V23: two Washington Capitols games from printed boundaries

The *Evening Star*'s January 2, 1949 report (p. 34) identifies Washington–Chicago at Uline Arena and gives Washington–Chicago cumulative scores `20–17`, `39–41`, `65–61`, `92–90` for `0024800149`.[169]
The November 9, 1949 report (p. 42) identifies Washington–Minneapolis at Uline Arena and gives the Washington–Minneapolis cumulative sequence `24–27`, `36–40`, `55–49`, `66–68` for `0024900027`.[170]

**ST-PERIOD-20 (high; provenance and missingness):** A wide crop misread `65–61` as `65–64`; the native-resolution crop resolves the digit. The newspaper supplies the venue for orientation; an absent canonical arena is not contrary evidence and remains absent. Both games had no v22 period rows and zero candidate sources. V23 stores the four printed observations per side, derives the quarter values by subtraction, and leaves finals, box rows, events and shots unchanged.[169][170]

**Fresh pass, archival replay:** The v23 parent-delta gate preserves the inherited availability evidence and compares the current coverage outside the two-game scope. All 204 checks passed, with 21 unchanged relations matched and all 24 manifest-listed files replayed. The gate rejected a final-preserving quarter transfer even after both its in-database receipt and fingerprint were edited, a changed scan byte, an existing output, and a preexisting destination WAL. Coverage is 598,316 team-period rows across 73,596 of 75,268 games; 1,672 games remain without periods (1,431 before 1960). No claim of archive-wide coverage or redistribution clearance.

## V24: keyed player boxes for the January 21, 1954 All-Star Game

For `0035300004`, all 20 individual made-field-goal and made-free-throw lines agree between the archived NBA box and the January 22 *St. Louis Post-Dispatch* printed box; each line's scoring arithmetic matches the NBA points.[171][172]
The archived NBA rows also give minutes, attempts, rebounds and assists, and reconcile to the 98–93 OT final and both retained team boxes. Andy Phillip played; Jack Molinas is not a played row. Whole minutes are reported values, not second-precise observations.[171][173]

**ST-BOX-03 (high; independent evidence can disagree at one cell):** Ray Felix's personal fouls are 6 in the newspaper and 4 in the compiled box. Publish neither value for the disputed player field; keep both original observations and the `held_disputed` disposition. The 19 agreeing foul cells are retained. No team foul total was altered.[172][173]

**Fresh pass, archival box auditor:** Six evidence files are pinned; 210 checks and 26 retained-relation comparisons passed, including a standalone replay. The replay rejects publishing a disputed foul and substituting two individual minutes that preserve 265 team minutes, even if the stored evidence and publication projection are edited with the fact. Twenty played rows added, leaving ten historical finals with no player box; periods unchanged.

## V25: dated NBA preseason line scores, October 2006

The archived original NBA boxes carry an embedded NBA `visitor_team_score-<game_id>` identifier, dated scoreboard headings, explicit sides, and a printed four-quarter table. All five inspected pages match the date, designated team keys, and finals. Two positive controls (`0010600001`, `0010600007`) match all 16 existing quarter cells; neither is changed.[174][175]

| Game | Designated home: printed quarters | Visitor: printed quarters |
| --- | --- | --- |
| `0010600016` | Phoenix `30/30/30/29` | Maccabi Tel Aviv `25/25/25/27` [176] |
| `0010600024` | Lakers `23/35/15/31` | Seattle SuperSonics `31/26/19/25` [177] |
| `0010600025` | Dallas `18/23/22/27` | Sacramento `25/24/34/28` [178] |

**ST-PERIOD-21 (high; plausible synthetic fill versus actual printed evidence):** Phoenix and Maccabi each have three repeated quarter scores. Preserve the old `possible_balanced_fill` hold as prior evidence and publish only the independently retained archival NBA page's exact cells. This is provenance for the printed values, not an independent scorer corroboration or permission to redistribute. The conflicting venue claims and absence of Maccabi player rows are not resolved by this period evidence.[176]

**Fresh pass, release auditor:** 216 checks, standalone replay of five original pages, and 25 unchanged parent relations passed. A real wrong fixture from the same date and a compensated quarter edit with resealed in-database evidence were rejected. The initial index did not yield an exact 200 for `0010600017`; the bounded capture stopped on a changed archival replay locator for `0010600026`. Of 120 season preseason games, 108 now have periods and 12 do not. The historical total is 73,599 of 75,268, leaving 1,669. No boxes, finals, arena fields, events, or shots changed.

## V26: eleven more October 2006 preseason line scores

The October 11 Boston at Toronto capture uses the archive index's literal trailing `?`. Its printed fourth-quarter boundary is 102–102; Toronto won overtime 16–10 for a 118–112 final.[179]
Ten more keyed archive pages report regulation line scores: `0010600030`, `0010600032`, and `0010600034`.[181][182][183]
The next three are `0010600058`, `0010600070`, and `0010600076`.[184][185][186]
The final group includes `0010600089`, `0010600092`, and `0010600093`.[187][188][189]
The `0010600094` capture supplies the Pacers–Bobcats scores.[190]
The alternative Efes Pilsen–Golden State capture returned HTTP 200 but contained no embedded game ID. It does not establish any period cells for `0010600026`.[180]

**Fresh pass, release auditor:** 221 checks and a standalone replay passed for the 11 original NBA pages and 28 unchanged relations. A same-final Toronto score edit that broke the regulation tie and a missing overtime cell were rejected. The 90 admitted rows bring coverage to 119 of 120 games in the 2006–07 preseason and 73,610 of 75,268 historically, with 1,658 period gaps. No results, boxes, plays or shots were changed by this historical release.

## Sources

[1] https://duckdb.org/docs/stable/sql/statements/attach.html
[2] https://duckdb.org/docs/stable/sql/constraints.html
[3] https://duckdb.org/docs/stable/sql/query_syntax/setops.html
[4] https://www.nba.com/news/history-nba-all-star-game
[5] https://www.nba.com/news/history-all-star-recap-2001
[6] https://www.nba.com/termsofuse
[7] https://www.kaggle.com/terms
[8] https://www.nba.com/game/drt-vs-lbn-0032100001
[9] https://www.nba.com/news/live-updates-2026-nba-finals-game-5-knicks-seek-first-title-since-1973
[10] https://www.nba.com/news/2026-nba-playoffs-schedule
[11] https://www.nba.com/news/nightly-pulse-nba-news-highlights-from-feb-16 — Nightly Pulse: NBA news & highlights from Feb. 16
[12] https://www.nba.com/news/nba-releases-restart-scrimmage-schedule — Full schedule: NBA restart scrimmages
[13] https://www.nba.com/news/nba-cup-101 — Emirates NBA Cup 101: Rules, format and how it works
[14] https://www.nba.com/news/nba-play-in-tournament — Everything to know about 2026 SoFi NBA Play-In Tournament
[15] https://official.nba.com/rule-no-5-scoring-and-timing — RULE NO. 5: Scoring and Timing
[16] https://duckdb.org/docs/stable/sql/statements/checkpoint.html — CHECKPOINT Statement
[17] https://www.nba.com/news/what-were-the-original-nba-teams — What were the original NBA teams?
[18] https://www.nba.com/news/2026-nba-all-star-game-new-format — 2026 NBA All-Star Game to feature new U.S. vs. World competition
[19] https://github.com/eoinmooremath/nba-data-pipeline
[20] https://www.kaggle.com/datasets/eoinamoore/historical-nba-data-and-player-box-scores
[21] https://github.com/shufinskiy/nba_data — shufinskiy/nba_data mirror: data provenance and packaging license
[22] https://github.com/sportsdataverse/hoopR-nba-data
[23] https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_pbp
[24] https://nbl.com.au/news/breakers-take-fight-to-jazz-in-nblxnba
[25] https://www.sports-reference.com/bot-traffic.html
[26] https://www.sports-reference.com/data_use.html
[27] https://www.sports-reference.com/termsofuse.html
[28] https://www.basketball-reference.com/robots.txt
[29] https://www.nba.com/game/0025300306
[30] https://apnews.com/e04b7da2ef27449e8c761aec9d351afb
[31] https://api.pbpstats.com/docs
[32] https://www.nba.com/game/nzb-vs-uta-0012400002
[33] https://www.nba.com/game/nzb-vs-phi-0012400011
[34] https://www.nba.com/game/nzb-vs-okc-0012400029
[35] https://www.nba.com/game/ulm-vs-por-0012400060
[36] https://www.nba.com/game/0010900001/box-score
[37] https://www.denverpost.com/2009/10/01/nuggets-season-opener-leaves-lot-to-be-desired
[38] https://www.nba.com/game/0022500029
[39] https://www.nba.com/game/0022500104
[40] https://statsdmz.nba.com/pdfs/20250216/20250216_KENCHK.pdf
[41] https://statsdmz.nba.com/pdfs/20250216/20250216_CANSHQ.pdf
[42] https://statsdmz.nba.com/pdfs/20250216/20250216_SHQCHK.pdf
[43] https://www.wikidata.org/wiki/Wikidata:Licensing
[44] https://www.wikidata.org/wiki/Special:EntityData/Q138891942.json
[45] https://en.wikipedia.org/wiki/1946%E2%80%9347_BAA_season
[46] https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use
[47] https://www.mediawiki.org/wiki/API:Etiquette
[48] https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_schedules
[49] https://en.wikipedia.org/wiki/2026%E2%80%9327_New_York_Knicks_season
[50] https://en.wikipedia.org/wiki/2026%E2%80%9327_Charlotte_Hornets_season
[51] https://www.nba.com/news/2026-27-nba-regular-season-schedule
[52] https://www.nba.com/games?date=2026-11-19
[53] https://www.nba.com/games?date=2026-11-18
[54] https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nba_crosswalk
[55] https://www.nba.com/games?date=2026-10-24
[56] https://www.nba.com/games?date=2026-10-31
[57] https://www.nba.com/games?date=2026-11-04
[58] https://www.nba.com/news/emirates-nba-cup-key-dates-schedule
[59] https://www.nba.com/game/cha-vs-lac-0022600238/box-score
[60] https://www.nba.com/games?date=2026-11-15
[61] https://www.nba.com/game/den-vs-ind-0022600189
[62] https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20261119
[63] https://site.api.espn.com/apis/v2/sports/basketball/nba/standings?season=2027
[64] https://statsdmz.nba.com/pdfs/20261119/20261119_GSWMEM.pdf
[65] https://statsdmz.nba.com/pdfs/20261119/20261119_PORMIL.pdf
[66] https://statsdmz.nba.com/pdfs/20261119/20261119_NYKMIN.pdf
[67] https://statsdmz.nba.com/pdfs/20261119/20261119_LACSAS.pdf
[68] https://pr.nba.com/2026-27-nba-regular-season-schedule
[69] https://pr.nba.com/hinkle-fieldhouse-emirates-nba-cup-2026-championship
[70] https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nba_stats_injury_report
[71] https://disneytermsofuse.com/english
[72] https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-11-19_05_30PM.pdf
[73] https://sports.yahoo.com/nba/scoreboard/?date=2026-11-19
[74] https://basketball.realgm.com/nba/boxscore/1961-04-02/St-Louis-at-Boston/232782
[75] https://basketball.realgm.com/nba/boxscore/1961-04-05/St-Louis-at-Boston/232783
[76] https://basketball.realgm.com/nba/boxscore/1961-04-08/Boston-at-St-Louis/232784
[77] https://basketball.realgm.com/nba/boxscore/1961-04-09/Boston-at-St-Louis/232785
[78] https://basketball.realgm.com/nba/boxscore/1961-04-11/St-Louis-at-Boston/232786
[79] https://basketball.realgm.com/nba/boxscore/1960-12-25/New-York-at-Syracuse/177338
[80] https://www.nba.com/game/0024600005/box-score
[81] https://www.nba.com/game/0024600026/box-score
[82] https://statsdmz.nba.com/pdfs/20221014/20221014_HOUIND.pdf
[83] https://www.nba.com/game/0010600016/box-score
[84] https://www.loc.gov/resource/sn83045462/1961-04-03/ed-1/?sp=15
[85] https://www.loc.gov/resource/sn83045462/1961-04-06/ed-1/?sp=57
[86] https://www.loc.gov/resource/sn83045462/1961-04-09/ed-1/?sp=41
[87] https://www.loc.gov/resource/sn83045462/1961-04-12/ed-1/?sp=65
[88] https://www.loc.gov/resource/sn83045462/1961-04-10/ed-1/?sp=17
[89] https://www.loc.gov/resource/sn83045462/1957-04-14/ed-1/?sp=68
[90] https://www.nba.com/game/gsw-vs-mem-0022600270/box-score
[91] https://www.nba.com/game/por-vs-mil-0022600271/box-score
[92] https://www.nba.com/game/nyk-vs-min-0022600272/box-score
[93] https://www.nba.com/game/lac-vs-sas-0022600273/box-score
[94] https://www.mcall.com/1996/11/16/cavaliers-give-the-sixers-the-ills-cleveland-rallies-for-the-win-as-allen-iverson-separates-his-shoulder-in-the-fourth-period
[95] https://www.spokesman.com/stories/1997/feb/06/jazz-make-it-3-0-against-sonics-malone-stockton
[96] https://statsdmz.nba.com/pdfs/20180205/20180205_UTANOP.pdf
[97] https://statsdmz.nba.com/pdfs/20241023/20241023_GSWPOR.pdf
[98] https://statsdmz.nba.com/pdfs/20251107/20251107_CLEWAS.pdf
[99] https://statsdmz.nba.com/pdfs/20251121/20251121_OKCUTA.pdf
[100] https://statsdmz.nba.com/pdfs/20251218/20251218_LALUTA.pdf
[101] https://statsdmz.nba.com/pdfs/20251228/20251228_MEMWAS.pdf
[102] https://statsdmz.nba.com/pdfs/20260130/20260130_CLEPHX.pdf
[103] https://statsdmz.nba.com/pdfs/20260207/20260207_GSWLAL.pdf
[104] https://statsdmz.nba.com/pdfs/20260226/20260226_MIAPHI.pdf
[105] https://statsdmz.nba.com/pdfs/20260320/20260320_GSWDET.pdf
[106] https://statsdmz.nba.com/pdfs/20260321/20260321_MILPHX.pdf
[107] https://statsdmz.nba.com/pdfs/20260403/20260403_ORLDAL.pdf
[108] https://statsdmz.nba.com/pdfs/20260213/20260213_AUSMEL.pdf
[109] https://statsdmz.nba.com/pdfs/20260213/20260213_VINTMC.pdf
[110] https://statsdmz.nba.com/pdfs/20260213/20260213_VINMEL.pdf
[111] https://www.nba.com/news/2026-nba-rising-stars
[112] https://cdn.nba.com/manage/2026/02/2026-NBA-All-Star-Game-Rules.pdf
[113] https://statsdmz.nba.com/pdfs/20260215/20260215_WLDSTR.pdf
[114] https://statsdmz.nba.com/pdfs/20260215/20260215_WLDSTP.pdf
[115] https://statsdmz.nba.com/pdfs/20260215/20260215_STRSTP.pdf
[116] https://www.nba.com/game/0032500021/box-score
[117] https://www.nba.com/game/0027500014
[118] https://www.nba.com/game/0027600488
[119] https://www.nba.com/game/0027500097
[120] https://www.basketball-reference.com/boxscores/196610150BAL.html
[121] https://www.basketball-reference.com/boxscores/196611170BAL.html
[122] https://www.basketball-reference.com/boxscores/196701040BAL.html
[123] https://www.nba.com/game/0020200400
[124] https://www.nba.com/game/0020500356
[125] https://www.nba.com/game/0029600158
[126] https://www.nba.com/game/0029800101
[127] https://www.nba.com/game/0040100317
[128] https://www.nba.com/game/0040200141
[129] https://www.nba.com/game/bos-vs-blt-0027000523
[130] https://www.nba.com/game/0027000525
[131] https://basketball.realgm.com/nba/boxscore/1960-11-05/Philadelphia-at-Detroit/177236
[132] https://basketball.realgm.com/nba/boxscore/1961-12-08/LA-Lakers-at-Philadelphia/176959
[133] https://basketball.realgm.com/nba/boxscore/1962-01-31/LA-Lakers-at-Detroit/177095
[134] https://www.nba.com/game/0027700297
[135] https://www.nba.com/game/0029200096
[136] https://www.nba.com/game/0045000221
[137] https://www.nba.com/game/0026100105
[138] https://statsdmz.nba.com/pdfs/20200229/20200229_LALMEM.pdf
[139] https://www.basketball-reference.com/playoffs/NBA_1961_games.html
[140] https://www.basketball-reference.com/playoffs/NBA_1965_games.html
[141] https://www.basketball-reference.com/playoffs/NBA_1969_games.html
[142] https://www.basketball-reference.com/playoffs/NBA_1994_games.html
[143] https://www.basketball-reference.com/playoffs/NBA_1996_games.html
[144] https://www.loc.gov/resource/sn83045462/1962-02-01/ed-1/?sp=26
[145] https://www.nba.com/game/0024600016
[146] https://www.nba.com/game/0025700109
[147] https://www.nba.com/game/det-vs-lal-0046000222
[148] https://www.nba.com/game/lal-vs-atl-0046800323
[149] https://www.nba.com/game/orl-vs-atl-0049500046
[150] https://dlg.usg.edu/record/gsu_ajc_11230
[151] https://www.chicagotribune.com/1996/05/13/magic-103-hawks-96
[152] https://www.loc.gov/resource/sn83045462/1961-03-16/ed-1/?sp=16
[153] https://www.basketball-reference.com/boxscores/194611090CHS.html
[154] https://www.basketball-reference.com/boxscores/195712220CIN.html
[155] https://www.basketball-reference.com/allstar/NBA_2004.html
[156] https://www.basketball-reference.com/boxscores/195111150SYR.html
[157] https://www.basketball-reference.com/boxscores/195111220BOS.html
[158] https://www.basketball-reference.com/boxscores/195701040BOS.html
[159] https://www.basketball-reference.com/boxscores/194701200PIT.html
[160] https://www.basketball-reference.com/boxscores/195112060MLH.html
[161] https://www.basketball-reference.com/boxscores/195303030MLH.html
[162] https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_team_boxscores
[163] https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_player_boxscores
[164] https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_shots
[165] https://www.nba.com/stats/player/1642905
[166] https://statsdmz.nba.com/pdfs/20261119/20261119_NYKMIN_book.pdf
[167] https://statsdmz.nba.com/pdfs/20261119/20261119_PORMIL_book.pdf
[168] https://www.nba.com/news/nbas-heave-rule-will-allow-deep-end-of-quarter-shots-without-hurting-shooting-percentages
[169] https://www.loc.gov/resource/sn83045462/1949-01-02/ed-1/?sp=34
[170] https://www.loc.gov/resource/sn83045462/1949-11-09/ed-1/?sp=42
[171] https://web.archive.org/web/20091202232006/http://www.nba.com/history/allstar/boxscore_1954.html
[172] https://archive.org/details/per_st-louis-post-dispatch_1954-01-22_76_21/page/n25/mode/1up
[173] https://www.landofbasketball.com/allstargames/1954_nba_all_star_game.htm
[174] https://web.archive.org/web/20061113000052id_/http://www.nba.com:80/games/20061005/PHIBAR/boxscore.html
[175] https://web.archive.org/web/20061113000132id_/http://www.nba.com:80/games/20061009/TORWAS/boxscore.html
[176] https://web.archive.org/web/20061103223310id_/http://www.nba.com:80/games/20061011/TELPHX/boxscore.html
[177] https://web.archive.org/web/20061103223321id_/http://www.nba.com:80/games/20061012/SEALAL/boxscore.html
[178] https://web.archive.org/web/20061105005744id_/http://www.nba.com:80/games/20061012/SACDAL/boxscore.html
[179] https://web.archive.org/web/20061103000137id_/http://www.nba.com:80/games/20061011/BOSTOR/boxscore.html?
[180] https://web.archive.org/web/20061117103747id_/http://www.nba.com/games/20061012/EFPGSW/boxscore.html
[181] https://web.archive.org/web/20061113000256id_/http://www.nba.com:80/games/20061013/CLEBOS/boxscore.html
[182] https://web.archive.org/web/20061108065131id_/http://www.nba.com:80/games/20061013/ATLHOU/boxscore.html
[183] https://web.archive.org/web/20061105005852id_/http://www.nba.com:80/games/20061014/NYKPHI/boxscore.html
[184] https://web.archive.org/web/20061106051009id_/http://www.nba.com:80/games/20061018/INDDEN/boxscore.html
[185] https://web.archive.org/web/20061106051113id_/http://www.nba.com:80/games/20061019/MILDAL/boxscore.html
[186] https://web.archive.org/web/20061107223040id_/http://www.nba.com:80/games/20061020/NOKLAL/boxscore.html
[187] https://web.archive.org/web/20061108065238id_/http://www.nba.com:80/games/20061022/PHXLAL/boxscore.html
[188] https://web.archive.org/web/20061108065251id_/http://www.nba.com:80/games/20061023/SEAPHX/boxscore.html
[189] https://web.archive.org/web/20061117080336id_/http://www.nba.com:80/games/20061023/UTAPOR/boxscore.html
[190] https://web.archive.org/web/20061107223225id_/http://www.nba.com:80/games/20061024/INDCHA/boxscore.html
