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

## Checked and not useful

- Basketball-Reference player game-log pages are disallowed by robots.txt. They were fetched on the owner's decision, at the site's limit of 20 per minute.
- NBA.com `boxscoresummaryv2` returns no officials for 1996-2001 games.
- hoopR-nba-data and shufinskiy/nba_data have no preseason games.
- Kaggle szymonjwiak, wyattowalsh and eoinamoore play-by-play only start at 2013-14 for preseason.
- llimllib `game_events` has no preseason.
- Kaggle deocheng has empty quarter and FGA columns before 1990.
- No bulk file has full referee crews for the 1996-99 games where only 1-2 officials are known. Basketball-Reference box pages are the only source for those.

## Open (needs the owner's decision on ESPN)

The ESPN public API (`site.api.espn.com/.../summary?event=<id>`, no key) is the only source found for three gaps:

- **Preseason play-by-play and shots, 2003-2013:** about 90-100% of games from 2005 on, and 70-75% for 2003-05. Some coordinates are placeholders at (25, 0).
- **Preseason referee crews:** many 2004-2010 games still have none.
- **Preseason player box scores:** 27 of 28 games.

Each needs one request per game. The scope rule keeps ESPN to cross-checks unless the owner approves it.
