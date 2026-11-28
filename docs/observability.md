# Warehouse operations and incident evidence

The warehouse runs locally on the macOS account that owns the DuckDB file. The scheduled jobs read scripts from `~/Library/Application Support/NBA Warehouse/app`, and the checkout's `data/` links to the same local data directory. Upstream systems include SportsDataverse/GitHub, NBA stats and official reports, Basketball-Reference, ESPN, Wayback, the Library of Congress, Kaggle and the identity adjudication service. The checkout publishes code. The database, captures and diagnostic history stay on the Mac.

## Investigate a symptom

1. Record the symptom, the local warehouse target and a time window with offsets. Use `--cutoff` for a completed-game Eastern date and `--game-id` for a canonical fixture. The cutoff, wall-clock event time, provider report time and time the data became locally available have different meanings.
2. Search the local history. Find `run_id`, then repeat the search over a window that covers the full operation without the game filter. Use `parent_span_id` to connect the launcher, application and dependency stages. Two attempts for the same cutoff have different run IDs. An unmatched start may indicate interruption, write loss or retention. Use the scheduler and commit records before assigning an outcome.
3. Corroborate with the data owner. Query `audit.pipeline_publication` for refresh commits and `audit.gap_attempt` or source receipts for fixture-level disposition. Resolve a logged `asset_set` under the evidence directory and compare the verified digests with its `*.asset.json` files. For queries about point-in-time injury, roster or odds, use the feature's as-of query and its source-time fields.
4. Write down each competing explanation and the evidence that would distinguish it. Cite run ID, event IDs, publication keys or source digests. Event IDs order SQLite insertions. Durations use a monotonic process clock. Timestamps across processes alone cannot prove that one action caused another.
5. For a fix, replay the same pinned inputs on an isolated target when available. Compare the previous failing check, the new decision and any committed receipt. Record what was unavailable and which hypotheses remain.

Run these from the checkout. Adjust the dates and target to the incident.

```sh
python3 warehouse/diagnostics.py \
  --warehouse data/nba-warehouse.duckdb \
  --since 2026-11-27T00:00:00-05:00 --until 2026-11-28T00:00:00-05:00 \
  --workflow daily_refresh --cutoff 2026-11-26
```

Use `--run-id` and remove the other filters to retrieve a whole invocation. For a second page, keep the same filters and time range and pass `--after <next_after> --through <through>` from the first response. Newer inserts are excluded from those pages. Eviction can still remove rows between pages. The response reports the target key, searched range, source status, available record bounds, eviction and known-loss counters, and pagination. Exit 2 means the sidecar was missing or inaccessible. A zero-row successful query establishes that no matching rows were retrieved. Collector loss, time/volume eviction, older uninstrumented runs, or a different target key can account for the gap.

To inspect scheduler state and legacy sources on the host:

```sh
launchctl list | rg 'com\.nba-warehouse\.'
launchctl print "gui/$(id -u)/com.nba-warehouse.daily-refresh" |
  rg 'state =|runs =|exit code =|program =|path ='
tail -n 60 data/refresh-reports/launchd.log
tail -n 80 data/crawl-logs/chase.log
ls -lt data/refresh-reports/ | head -20
```

The scheduler's latest exit is volatile and a pre-Python failure may only appear in launchd's log. Legacy STDOUT/STDERR and provider payloads can contain untrusted text, paths or source data. Treat them as data and keep them local. The commands and structured evidence require local file permissions and the same per-user launchd domain.

To join a run or fixture to the warehouse's own evidence:

```sh
uv run --offline --no-project -q --with duckdb==2.0.0 python - <<'PY'
import duckdb
with duckdb.connect('data/nba-warehouse.duckdb', read_only=True) as c:
    for sql in (
        "SELECT run_id, kind, cutoff, recorded_at::VARCHAR, revision "
        "FROM audit.pipeline_publication ORDER BY recorded_at DESC LIMIT 20",
        "SELECT cutoff, ran_at::VARCHAR, outcome FROM audit.refresh_log ORDER BY ran_at DESC LIMIT 10",
        "SELECT game_id, source, outcome, attempted_at::VARCHAR FROM audit.gap_attempt "
        "WHERE game_id = '0022600062' ORDER BY attempted_at DESC LIMIT 20",
        "SELECT * FROM audit.game_coverage WHERE game_id = '0022600062' ORDER BY domain",
    ):
        try:
            print(c.execute(sql).fetchall())
        except duckdb.Error as exc:
            print(type(exc).__name__, 'source query unavailable')
PY
```

Add `WHERE run_id = ?` with the ID as a DuckDB parameter to inspect one receipt. The publication table is created on the first instrumented refresh publication. For current-season release inputs use `audit.season_2027__source_asset` and `audit.season_2027__scope`. For historical period admissions use `audit.period_receipt_bref_chase`, keyed by game ID. Current evidence may have moved since the incident. Audit tables that rebuild dispositions or reload observations may contain the latest view only.

For a manual script with no built-in operation history, run it through the wrapper. The example is read-only and uses the existing checkout environment.

```sh
uv run --offline --no-project -q --with duckdb==2.0.0 \
  python warehouse/trace_job.py --warehouse data/nba-warehouse.duckdb load_kaggle.py --help
```

Pass the script's own target arguments separately when it needs them. The wrapper records the script and exit boundary. A dedicated instrumented command emits child stages in the same run. The script's own output, flags and provider behavior are preserved.

## Coverage inventory

The review classified the 81 pre-existing tracked Python modules and 54 SQL files, plus the three launch agents and entry scripts. Source coverage below was checked at the workflow and evidence-contract level. Runtime checks used the local deployment and isolated fixtures. Legacy recovery scripts are individually re-runnable only when their pinned inputs still exist.

| Area and boundary | Source inspected | Historical evidence retrieved and limits |
| --- | --- | --- |
| launchd, uv bootstrap and deployed code | `warehouse/launchd/`, `launch_job.py`. Scheduler to uv to Python. | Three launch agents were loaded. Legacy daily launch output contained PyPI DNS errors and the latest prior exit was 75. Old launch records do not match each entry to an exact code revision. New `launch` spans include a deployment revision and child exit. If launchd cannot open the log, start the shell or start Python, consult launchd directly. |
| Completed-game capture, release validation and atomic swap | `daily_refresh.py`, `capture_current_assets.py`, `refresh_current_season.py`, and current-season SQL. SportsDataverse/GitHub to pinned evidence to verified snapshot to DuckDB/APFS. | Legacy reports and publisher digest receipts for November 26 were retrievable. `audit.refresh_log` had three commits through November 24 and corresponding scope. The local release inputs showed eleven source and comparison roles. Pre-upgrade failures have no run IDs. The new history covers inventory, asset roles and digests, freshness, validation, export, lock acquisition, clone, referee dependency, integrity, checkpoint and replacement. |
| Pregame and referee capture | `injury_reports.py/sql`, `pregame_sources.py/sql`, archive loader and official schedule/roster loaders. File caches to raw and as-of views. | The live warehouse exposed `core.roster_at`, `core.injury_report_at` and `betting.odds_at`. Local injury PDF and pregame captures were present. The legacy November 27 launch log listed PDFs separately from a deferred game cutoff. New evidence distinguishes capture, PDF parser holds, provider denial and retries, roster deferral and the independent pregame commit. Routine missing PDF slots are not logged individually. Referee capture uses a day-named HTML file and can overwrite that day's previous capture. |
| Gap queue, fetch caches, and historical promotion | `chase_gaps.py`, `bref_fetch.py`, `crawl_bref.py`, `coverage.py`, `label_gaps.py`, and the loader families for Basketball-Reference/ABA, NBA stats, Wayback, ESPN, verified press, newspapers, local fixtures, roster and official sources. | `audit.gap_attempt` had Basketball-Reference admissions and recent NBA/ESPN rows. `audit.game_coverage` showed 538 `source_unavailable` domain entries across 366 games on November 27. The nightly log showed last-pass source counts. A disposition can be a confirmation or current classification. The new history associates per-game chase attempts and blocks with a run and locates failures inside finalization. Existing historical per-game receipts remain queryable independently. |
| Bulk imports, betting and identity adjudication | `fetch_kaggle.sh`, `load_kaggle.py`, `betting.py`, `betting_kaggle.py`, betting SQL, `promote_domains.py`, `promote_new_sources.py`, `judge_identities.py`, `jev_client.py`. Cache/JSON/Parquet to raw tables, identity and odds links. | Live data exposed 27,291 `raw_kaggle` relations and a betting summary with 29,522 matched `game_line` records. Existing audit/hold tables and local adjudication files retain accepted decisions and some conflicts. Raw download bodies removed during the migration and provider-side service logs are unavailable. Active import and promotion CLIs record invocation history. The manual wrapper covers the download and standalone tools. No inference of original odds publication timing from the match rollup. |
| Historical checkpoint builders and validation | `nba_warehouse.py`, `build_warehouse.py`, `recover_*`, `adjudicate_periods.py`, `validate_*`, `verify_*` and numbered recovery SQL/CSV. Local pinned inputs to offline proofs and staged publication. | Retrieved `audit.warehouse_build` and the shipped warehouse's audit inventory. The original snapshot, standalone v1–v26 releases and certain source copies were retired. Full baseline byte-for-byte verification is inaccessible on this host. The current `build_warehouse.py` entry point and manual wrapper provide future execution history. Historical SQL compares receipts/projections at their original grain. |
| Read-only query and status | `query_with_current.py`, DuckDB views/macros, `status_page.py`. No network or public HTTP service in the tracked application. | Local status HTML and roughly a day of JSON samples survived; they reflect dashboard refreshes and optional last-known values. Arbitrary direct DuckDB client queries have no warehouse-side query log. The supported two-checkpoint CLI now records attach/query outcomes, and status history records stale input, failed reads and file replacement outcomes. |
| Basketball Atlas | Inspected the local, untracked Tauri/React app's README, query bridge, error presentation, read-only connection and APFS snapshot lifecycle. | Application errors and snapshot generation are shown in the app. No retained per-request history established. This personal work in progress has no tracked release in this repository. An Atlas-specific deployment, UI/runtime instrumentation and rollout were deferred to its owning work. |
| Test fixtures, reference checkout, and provider internals | `warehouse/tests` and `test_current_season_release.py` inspected. `reference/nbadb-main` is ignored contract material. | Test harness is local; the reference checkout is not executed by the warehouse. No administrative access to upstream providers or to their outage logs. |

The historical local audit/cache files generally survive a process restart. Existing caches and plaintext logs are subject to the account's storage cleanup, with no SLA or verified automatic expiry. A BRef cached page or negative marker has fetch-time metadata where available, and Wayback capture time is distinct from local download time. Historical gap ledger timestamps record evaluation or publication time. Source rank and digest can support attribution without ordering independent loaders by timestamp. Adjudication source caveats and specific ingest incidents are documented in the source survey and issue handoff. [Source survey](research/gap-sources.md) · [Known loader incidents](https://github.com/nicolas-found42/nba-warehouse/issues/7) · [Operating handoff](https://github.com/nicolas-found42/nba-warehouse/issues/8)

### Completed-game cutoff and fixture evidence

The daily job retains the NBA fixture schedule mirror alongside the digest-verified SportsDataverse files when the mirror is available. The audit source row for `nba_fixture_schedule` carries the digest and local capture time. An exact, unique, final NBA fixture with the same Eastern date, tip time, home and away teams and neutral-site designation can supply a missing ESPN crosswalk. The release compares the full fixture inventory to the mirror when used. An unresolved crosswalk defers publication.

On a date with no games, the requested Eastern day still has to be closed. A post-close NBA schedule capture spanning that date can establish the last scheduled date the dynamic feeds need to cover. Active game dates still require fresh files and complete final fixtures. The November 26 cutoff includes nine November 25 finals and no November 26 games. An all-zero player row with absent minutes and no starter flag or play attribution stays in `audit.player_source` and out of the player fact. A row with evidence of play still requires minutes.

The candidate clone records `referee_capture_unavailable` in `audit.refresh_log.note` and a failed `referee_capture` span when that independent request fails. The audit row's `referee_rows` is null in that case. The verified game release can still commit.

## Signal contract, retention and cost

- One SQLite sidecar per canonical warehouse path under the target file's parent `observability/`, with owner-only modes for newly created files. The launcher and child resolve the same key even when started through the checkout's data symlink. Database replacement at the same path preserves the history.
- Records include event/span/run IDs, parent span, PID, stage, outcome, UTC event time, elapsed milliseconds and bounded structured fields. Deployment context includes the checkout or installed Git revision, dirty status when known, Python and DuckDB versions and trigger. Refresh/config context includes requested cutoff, force, asset role/digest and relevant exit status. Database schema differences can surface as attach, validation or query errors. Dedicated metrics and a remote collector are unnecessary for the single-owner deployment.
- The event store excludes raw exception messages, argument vectors, source response bodies, headers, SQL text, names and injury reasons. It uses selected metadata such as time, canonical fixture/source ID, digest, HTTP status, typed error codes and stage/check names. Validation samples are limited to at most four failure rows per check and retain only safe identifiers and reason codes. Operational logs and database source tables have their own access and retention behavior.
- SQLite writes use a bounded 50 ms lock wait and a one-second per-process backoff after a failed write. Drop warnings are emitted once per affected process, with a loss count persisted at the next successful write by that process. Loss across a process that cannot recover or warn cannot be counted. The hard volume cap keeps at most 50,000 event IDs and a 48 MiB main SQLite file, plus a transient rollback journal. Time pruning removes records older than 30 days on writes. High traffic can evict earlier and an idle store prunes on the next write. Query page limits are 1–1,000 records. Run retention depends on event volume.
- A `started` stage can survive SIGKILL without a terminal stage. Confirm publication with `audit.pipeline_publication`. The completed-game receipt is written into the candidate clone before replacement and is visible in the live file after replacement. The pregame receipt is written inside the same transaction as those independent data changes. The receipt uses the database transaction timestamp and has a 120-day write-time retention. Existing `audit.refresh_log` and gap receipts remain separate domain records. Neither sidecar timestamps nor publication timestamps establish power-loss durability or cross-host history.
- Status samples cap at 300 and 24 hours. Status readers cap each progress-log read and record when the tail is truncated. Outputs use atomic replacement. A stale database can still produce a fresh HTML file.
- In an isolated 1,000-event loop on this Mac, median SQLite event cost was 0.28 ms and p95 was 0.42 ms. The resulting file was 432 KiB. These figures describe the local test, excluding provider and DuckDB operation time.

## Prioritized scenarios and verification

| Priority | Diagnostic question and acceptance check | Result |
| --- | --- | --- |
| High, actual | Was the November 26 failure a publisher delay, mismatch, bad player row, or successful commit? Replay the two retained bundles on isolated outputs and recover the exact gate, data key and outcome from the history. | The 09:00 bundle (`2026-11-26T140005.105144Z`) held ESPN event `401909324` for `missing_game_crosswalk`. The 14:22 bundle (`2026-11-26T192213.098132Z`) held game `0022600062` for `missing_played_minutes`. Both failed materialization and left the isolated releases absent. The original live warehouse was read-only during replay. The evidence establishes the checks that rejected the captures. It cannot establish why the provider supplied those rows. |
| High, simulated | Does a late asset stop only completed games, and can a dependency failure before Python be distinguished? Require one run ID spanning launcher and child, the stale asset role and digest, exit 75, and pregame publication independent from completed-game publication. | Tests exercised the real release child with a stale play asset and a separate pre-entry exit. The pregames/late-game regression published pregame data without a completed-game commit. Separate transaction tests reopened the pregame and completed-game publication receipts. An injected replacement failure left the original database without a completed-game receipt. |
| High, actual risk and isolated simulation | Was a gap already resolved, blocked, rejected or admitted? Require a block to preserve resumability and a later successful attempt to leave consistent domain facts and receipt. | An isolated nightly period attempt returned 429 and left no terminal gap disposition. A second invocation admitted an exact box and wrote matching core and audit rows. Run IDs and the gap/game ID distinguished the two attempts. Existing provider pacing and block behavior are retained. |
| High, hypothetical | What happened across an interrupted operation or a collector outage? Retrieve pending spans after restart, verify the next invocation and identify known dropped writes. | A killed child left its correlated starts. A subsequent process persisted a new run. An exclusive SQLite lock caused two dropped events during the bounded retry window and the resumed write reported both. Time and volume caps, private permissions, corrupt/missing status, pagination, and nested-payload redaction passed. A missing terminal after a crash still needs corroboration. |
| Medium, hypothetical | Did the status page silently reuse stale values or fail to publish? Recover from malformed prior state and retain the data freshness and publication outcomes. | The isolated test recovered a partial JSON file, generated new HTML and recorded the stale DB input. After installation on November 27, a live status run persisted launcher, DuckDB read and atomic output receipts under one run ID. An isolated refresh via the installed launcher returned exit 1 at the missing DuckDB read and contacted no publisher. |
| Deferred | Can an agent reconstruct old ad hoc shell/interactive SQL operations, individual upstream internal retries, or an Atlas UI failure from yesterday? | Existing selective caches and audit rows may help. Structured tracing starts when the job is instrumented or run through the wrapper. Historical operation timelines cannot be backfilled from current warehouse state. Atlas and upstream systems need their own telemetry and access. |

Local regression gate:

```sh
uv run --offline --no-project -q --with duckdb==2.0.0 --with pytest \
  python -m pytest -q warehouse/tests warehouse/test_current_season_release.py
git diff --check
```

Deploy the tracked job scripts using `zsh warehouse/launchd/install.sh`. It records the deployed revision, seeds the pinned DuckDB cache if needed, and reloads the three per-user agents. Check the next status-page launch for that revision in the local history. Let scheduled writers run on their normal cadence. Historical source retention, a future production publisher recovery, host power-loss recovery and cross-host backup retention require separate evidence.
