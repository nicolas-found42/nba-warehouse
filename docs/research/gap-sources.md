# Faster sources for the remaining gaps (2026-11-25)

The goal was to replace per-page fetching with whole-season or bulk files that are free and need no key (Kaggle allowed). Source rank is unchanged: NBA scorer reports > NBA.com > archived nba.com and newspapers > Basketball-Reference > everything else. A lower-ranked source only fills games that no higher source covers. Every bulk match is one-to-one, and every quarter-score line must add up to the official final.

## Sources adopted

| Gap | Source | Provenance | How it is matched | Loader |
|---|---|---|---|---|
| ABA player box (blank fields) | [BOXTOP ABA v3.1](https://raw.githubusercontent.com/mhamel12/boxtopaba/master/releases/ABA_Boxtop_v3_1_csv.zip) (CC BY-NC-SA 3.0) | Michael Hamel's Boxtop project, which is also the basis of Basketball-Reference's ABA boxes | Date + Basketball-Reference player id | `load_aba_bulk.py` |
| ABA player box (blank fields) | [nbastats.net gamelogsABA.xlsx](http://nbastats.net/01NbaAbaNblAbl/05-GameLogs/gamelogsABA.xlsx) | nbastats.net research | Date + last name + points, with first initial breaking ties | `load_aba_bulk.py` |
| ABA player box (missing fields) | Basketball-Reference player game logs, about 1,260 pages instead of 4,149 box pages | Basketball-Reference | Basketball-Reference ids | `crawl_bref.py aba_gamelogs` |
| Starting lineups | [llimllib/nba_data `player_game_status`](https://basketball-data.billmill.org/nba/stats/player_game_status/season=2022/data.parquet), one file per season, about 60 KB each | NBA.com `boxscoretraditionalv3` starter flag | NBA game ids | `load_nba_gaps.llimllib_starters` |
| Referees 2016-2026 | Kaggle `kyleknowles1/nba-referee-assignments-2016-2025` (CC BY-NC-SA 4.0) | NBA referee ids | NBA game ids | `load_espn_officials.kaggle_referees` |
| Referees 1996-2002 | [sportsdataverse `espn_nba_officials`](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/espn_nba_officials) (MIT) | **ESPN**, so it is labelled `espn_sdv` | ESPN schedule date (Eastern) + final scores | `load_espn_officials.py` |
| Referees, crews with fewer than 3 | Basketball-Reference box pages, about 800 | Basketball-Reference | Date + home team | `crawl_bref.py partial_crews`, `load_bref.officials` |
| Quarter scores, team box 1946-1990 | nbastats.net `GameResults.xlsx` and `02-GameStatistics-Team.xlsx` (the server truncates the second; the readable part runs to 2018) | nbastats.net research | Date + both final scores | `load_nbastats_history.py` |
| Starters, play-by-play, shots | NBA.com `boxscoretraditionalv3` / `playbyplayv3`, per game | NBA.com | NBA game ids | `fetch_nba_gaps.py`, `load_nba_gaps.py` |
| Referees, starters, shots | Kaggle eoinamoore (already loaded in step 1) | NBA.com live data | NBA game ids | `promote_domains.py`, `fill_local_gaps.py` |

## Referee name matching (fixed 2026-11-25)

After the Basketball-Reference crew pages loaded, 686 games still had fewer than 3 officials. 795 of the 799 pages name all 3 officials, so the loss was in matching names to referee ids, not in the source:

- Several referees carry two or three `official_id`s: the NBA box-score id plus duplicates from Kaggle and ESPN rows (for example Jacyn Goble 1627962 and 203219, Sean Corbin 1151 and 1962936541). The old matcher dropped every ambiguous name. `promote_domains.OFFICIAL_NAME_CANON` now resolves a name to the id NBA box scores use most, then the id the Kaggle assignments use most. Names are compared as lowercase letters only, so "ShaRae" matches "Sha'Rae".
- Basketball-Reference ids map through `raw.bref_official_map` (`load_bref.official_map`). The map uses explicit aliases where names differ: Tommy Nunez Sr. → 1181, James Capers Jr. → 1148, Sean Corbin → 1151. Six 1990s referees with no NBA id get synthetic ids from 1689000001: Rusty Herring, Lee Jones, Woody Mayfield, Paul Mihalak, Jake O'Donnell and Ed T. Rush. Ed T. Rush worked 30 games alongside Eddie F. Rush (1190), so they are different people.
- Where a game's crew comes only from ESPN and Basketball-Reference has the full crew, Basketball-Reference replaces it because it ranks higher. For example, ESPN lists "Tommy Nunez Jr." for 1997-2000 games where Basketball-Reference lists Tommy Nunez Sr.

Result: games with fewer than 3 officials fell from 686 to 37.

## Last fills and gap labelling (2026-11-25)

- **NBA.com `boxscoresummaryv3`** (`fetch_nba_gaps.py officials`, `load_nba_gaps.officials`) names the referees for 159 of the 225 games that still had none, mostly 2003-2016 preseason. The v2 summary returned nothing for these games. It ranks as NBA.com (`nba_summary_v3`).
- **Basketball-Reference box pages** now also give a full crew to regular-season and playoff games that had no referees at all (about 104 games).
- **NBA.com `boxscoretraditionalv3`** filled 3 team-games and 63 player rows. Rows are admitted only when points match the final and every player is a known person (`load_nba_gaps.boxes`).
- **`label_gaps.py`** (run by `chase_gaps.py finalize`) checks each open gap against named bulk sources whose coverage period includes the game. It logs `not_found` only when the game is absent, which lets the two-failed-sources rule label it.

Final gap ledger (`audit.game_coverage`):

| Domain | Status | Games | What was checked |
|---|---|---|---|
| Play-by-play and shot charts | source_unavailable | 635 each (before ESPN) | NBA.com `playbyplayv3`, Kaggle eoinamoore. Mostly 2003-2010 preseason. |
| Quarter scores | source_unavailable | 357 | Basketball-Reference box pages, nbastats.net. Mostly 1946-50. |
| Referees | source_unavailable | 66 | NBA box summary v2 and v3, Kaggle eoinamoore. Mostly preseason; ESPN's API has some. |
| Starting lineups | source_unavailable | 49 | NBA.com `boxscoretraditionalv3`, llimllib, NBA starters |
| Player box | partial / source_unavailable | 19 / 7 | NBA.com v3, eoinamoore, llimllib. Partials are mostly preseason games against foreign clubs, whose players have no NBA ids. |
| Team box | partial / source_unavailable | 6 / 6 | NBA.com v3, nbastats.net, eoinamoore |
| Games with fewer than 3 referees | (not a coverage domain) | 37 | Mostly old preseason games, and current-season games still being reported |

No gap remains `missing_recoverable`.

## Checked and not useful

- Basketball-Reference player game-log pages are disallowed by robots.txt. They were fetched on the owner's decision, at the site's limit of 20 per minute.
- NBA.com `boxscoresummaryv2` returns no officials for 1996-2001 games.
- hoopR-nba-data and shufinskiy/nba_data have no preseason games.
- Kaggle szymonjwiak, wyattowalsh and eoinamoore play-by-play only start at 2013-14 for preseason.
- llimllib `game_events` has no preseason.
- Kaggle deocheng has empty quarter and FGA columns before 1990.
- No bulk file has full referee crews for the 1996-99 games where only 1-2 officials are known. Basketball-Reference box pages are the only source for those.

## ESPN public API (owner-approved 2026-11-25)

The owner approved ESPN's public API (`site.api.espn.com`, no key) as the lowest-ranked source, used only for games no other source covers.

- `fetch_espn.py` fetches one scoreboard per gap date and matches each gap game to exactly one ESPN event on date plus both final scores (605 of 635 games). It then fetches one summary per matched event. Everything is cached under `evidence/espn-api/`.
- `load_espn.py` (run by `chase_gaps.py finalize`) loads:
  - **Play-by-play and shots** only when the log's running score reaches the official final for both teams. 431 games were admitted and 70 inconsistent logs were rejected (logged `mismatch`). In admitted games, made shots equal the box score's field goals made for 85% of team-games, and are within 2 for 93%. ESPN puts the rim at (25, 0) in feet. That point is also its placeholder, so shots there are stored as `ambiguous_zero`, like NBA.com's (0, 0).
  - **Referees** by name through `OFFICIAL_NAME_CANON`, only for games where no source names anyone. Source `espn_api`, 42 games.
  - **Starters** only for teams no other source names starters for. ESPN does not flag starters for most 2005-07 preseason games.
- Players are resolved per game by name against that game's warehouse box score, then through `raw.bridge_player_espn_base`.
- `audit.espn_api_fill` records which games' plays and shots came from ESPN. `fact_shot` keys hash source `espn_api`.

Gap ledger after ESPN:

| Domain | Status | Games |
|---|---|---|
| Play-by-play / shot charts | source_unavailable | 204 / 204 |
| Quarter scores | source_unavailable | 357 |
| Referees | source_unavailable | 24 |
| Starting lineups | source_unavailable | 46 |
| Player box | partial / source_unavailable | 19 / 7 |
| Team box | partial / source_unavailable | 6 / 6 |
