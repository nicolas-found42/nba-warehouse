# Faster sources for the remaining gaps (2026-11-25)

> Remaining work and known problems are tracked in GitHub issues: start with #8 (handoff), then #3-#7.

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

## Chasing to zero (owner goal, 2026-11-25)

The owner set the goal "chase all db gaps until 0 remain". Sources added in this pass, each used only for games with gaps and each with its own admission rule:

| Gap | Source | Rule | Loader |
|---|---|---|---|
| Quarter scores (preseason, 1960s) | NBA.com `boxscoresummaryv3` periods | Both teams' periods add up to the final; equal period counts; tied after regulation when there is overtime | `load_nba_gaps.periods` |
| Quarter scores | ESPN header line scores | Same rule | `load_espn.load` |
| Quarter scores 1946-66 | Library of Congress Chronicling America OCR (free, no key) | A "score by periods" passage naming both teams, adding up exactly to both finals. OCR is never corrected. | `fetch_newspaper_periods.py` |
| Play-by-play, shots, starters, referees, quarters (2001-2010) | Archived NBA.com game pages via the Internet Archive Wayback Machine (`nba.com/games/<date>/<AWYHOM>/`) | The game id is read from the box page. Plays are admitted only when the running score ends at the final. Shots have no coordinates (`unavailable`). Starters are box rows with a position, 5 resolved per team. Referees are resolved by name, else by jersey number plus surname. | `fetch_wayback_nba.py`, `load_wayback_nba.py` |
| Foreign-club player boxes | NBA.com `boxscoretraditionalv3` (no names published) | Players are added to `dim_person` with NBA.com ids when the team's player points add up to its final. Names come from an exact ESPN stat-line match; otherwise they are labelled "Unnamed player (NBA.com id ...)". | `load_nba_gaps.boxes`, `raw.nba_gap_person` |
| Team box | Sum of the warehouse's own player rows | Summed points equal the final; a stat is summed only when every player reports it | `load_nba_gaps.team_from_players` |
| Starters (1996-99) | Inferred from first-quarter NBA play-by-play | First appearance before any sub-in; exactly 5 per team | `load_nba_gaps.pbp_starters` |
| All-Star referees 1997-2003 | English Wikipedia infobox (revision id kept) | Exactly 3 names, resolved through `OFFICIAL_NAME_CANON` | `load_wiki_officials.py` |

Checked and not useful in this pass: NBA.com v2 endpoints (`playbyplayv2` errors, `boxscoretraditionalv2` has no starting positions or foreign-player names), Basketball-Reference All-Star pages (no referee line), and Basketball-Reference line scores for 1946-50 (blank or non-adding cells; completing them by subtraction would invent data).

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

## November 25 issue follow-up

The current per-game disposition is in `audit.gap_disposition`. It includes the most recent result and note for each
source. NBA statistics and copies of the same data count as one failed source. Timeouts, connection refusals and
access restrictions establish only that the requested route could not be checked on that run.

### Archived plays, shots and lineups

- Older play-by-play can name the scoring side only in the matchup folder and scoring tags. Recovering that orientation
  admitted 28 additional archived fixtures. The loader still checks the final and records the local file digest for new
  admissions. Shot rows include resolved shooters only, with geometry unavailable. Some early pages are static
  SportsTicker boxes and carry a printed quarter table and named positions. Other captures are empty layouts or have
  truncated play logs. [Wayback CDX](https://web.archive.org/cdx/search/cdx?url=nba.com/games/20061005/*&output=json)
- Score tags retain their printed values. Inconsistent intermediate score changes are recorded in
  `audit.nba_archive_score_anomaly`. Older pages can use uppercase `OFFICIALS` in a game-keyed element. That layout
  exposes some two-person crews and improves the evidence for those gaps. A cached CDX index or successfully retrieved
  page is required to count an archive failure.
- In the five All-Star gaps, the 1997 archive is a final box score, the original 2000 ESPN GameFlow asset is missing,
  and the 2001 original box and recap have no event list. The 2002 archived live page carries the final and the last
  14 events. The 2003 capture does not contain a complete event list. ESPN data for 2002 still reads 0–0 at the end,
  and its 2003 cumulative scores diverge from the published period totals from the first period onward.
  [1997 NBA box](https://web.archive.org/web/20121016144317/http:/www.nba.com/history/allstar/boxscore_1997.html)
  [2000 ESPN GameFlow](https://assets.espn.go.com/premium/nba/2000/20000213/gameflow/easwes.html)
  [2001 ESPN box](https://assets.espn.go.com/nba/2001/20010211/boxscore/weseas.html)
  [2002 live capture](https://web.archive.org/web/20020407104932id_/http://www.nba.com:80/games/20020210/WSTEST/livestats.html)
  [2003 live capture](https://web.archive.org/web/20030211145742id_/http://www.nba.com:80/games/20030209/WSTEST/livestats.html)
- The November 25 coverage rebuild reports 144 unavailable fixtures in each of plays and shots. In each domain,
  140 have three independently counted failed sources and four have two. Each fixture has a per-game attempt note
  in `audit.gap_disposition`.

### Period scores

- Pittsburgh at Detroit on November 20, 1946 has a complete set of printed cumulative boundaries across two newspapers.
  The four quarter scores are derived from those observations and recorded with scan digests in
  `audit.period_receipt_newspaper_boundary`. Two Chicago–New York games yielded partial period evidence only.
  [Pittsburgh Post-Gazette, p. 18](https://news.google.com/newspapers?id=hygjAAAAIBAJ&sjid=AGoDAAAAIBAJ&pg=1056%2C1625520)
  [The Pittsburgh Press, p. 35](https://news.google.com/newspapers?id=p1kbAAAAIBAJ&sjid=1EwEAAAAIBAJ&pg=992%2C2167811)
- The Deseret News printed all four quarters for Dallas–Utah on October 5, 2003. The contemporary Dallas report prints
  the same team vectors, with the date and Mexico City venue. `audit.period_receipt_publication` holds both digests.
  [Deseret News box score](https://deseret.com/2003/10/6/19788454/jazz-90-mavericks-85)
  [Dallas report](https://eskimo.com/~pbender/mavs/mavs-news/older/03summer/mavs10-6)
- Two October 23, 2004 preseason games had the same 91–89 final. Their ESPN event records identify the designated
  teams and include complete line scores. The October 12, 2005 Hawaii preseason game has an exact venue and local
  date match. A duplicate ESPN event for that game carries the previous game's quarter totals and was rejected.
  [Toronto at Chicago](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=241023004)
  [Dallas at Milwaukee](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=241023015)
  [Golden State at Los Angeles](https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=251013013)
- Three historical finals had corroborated errors. Detroit beat Cincinnati 100–83 on March 15, 1958. Golden State
  beat the Clippers 82–78 on October 27, 2004. The Lakers beat Utah 95–85 on October 25, 2005. Their four
  printed quarters reconcile to the corrected finals. Cincinnati and Golden State also have verified team boxes.
  Utah’s missing Kris Humphries line accounts for all 14 omitted points. His playing time is unresolved. The
  corrected Anaheim venue is recorded in the source receipt. The historical dimension still carries the old venue.
  `audit.historical_fixture_correction` preserves the original fixture, result and boxes and lists the source URLs.
  [Detroit Pistons account](https://www.nba.com/pistons/news/pistons-digest-sorting-out-rotations-back-end-on-pistons-to-do-list-with-a-month-to-go)
  [Detroit box](https://www.basketball-reference.com/boxscores/195803150DET.html)
  [Golden State box](https://sportsdata.usatoday.com/basketball/nba/stats/541929)
  [Utah box](https://sportsdata.usatoday.com/basketball/nba/stats/661261)
- Five more 2004 preseason finals agree across exact USA TODAY and ESPN games and the published team and player
  scoring. Four have matching quarter vectors at both sources. Utah–New York ended 83–89 and both sources agree
  through halftime. Their third- and fourth-quarter values disagree, so those periods are held. Philadelphia’s
  corrected Utah box restores Aaron McKie’s published one point and leaves his playing time unresolved.
  [Sacramento–Utah](https://sportsdata.usatoday.com/basketball/nba/stats/541891)
  [New Orleans–Clippers](https://sportsdata.usatoday.com/basketball/nba/stats/541906)
  [Utah–Philadelphia](https://sportsdata.usatoday.com/basketball/nba/stats/541916)
  [Seattle–Phoenix](https://sportsdata.usatoday.com/basketball/nba/stats/541919)
  [Utah–New York](https://sportsdata.usatoday.com/basketball/nba/stats/541932)
- The March 31, 1951 New York–Syracuse playoff was 77–75 in overtime. A contemporary Associated Press box
  confirms the final and 70–70 regulation score, and the original player points already reconcile. The cached
  line score uses `1OT` for the fifth period, as does the March 26, 1950 Syracuse–New York game. The parser now
  accepts contiguous overtime labels and requires a tie before each additional period.
  [1951 Evening Star, image 32](https://www.loc.gov/resource/sn83045462/1951-04-01/ed-1/?sp=32)
  [1950 Evening Star, image 18](https://www.loc.gov/resource/sn83045462/1950-03-27/ed-1/?sp=18)
- Philadelphia’s March 26, 1958 playoff ended 112–97 against Boston. The Associated Press caption and the next
  day’s New York Times report agree with the printed quarter and shooting totals. The original 111–97 entry
  is preserved in the correction receipt. Boston–New York’s 92–78 game has a contemporaneously verified March
  24, 1951 playing date. The ledger identifies the date discrepancy and preserves the old fixture date.
  [1958 Philadelphia box](https://www.basketball-reference.com/boxscores/195803260PHW.html)
  [March 27, 1958 New York Times index](https://www.nytimes.com/sitemap/1958/03/27/)
  [Boston–New York contemporary result](https://www.nytimes.com/1951/03/25/archives/knicks-turn-back-celtic-five-9278-new-york-pro-squad-reaches.html)
- The broadened Chronicling America query timed out on its first Washington game. The earlier per-game searches and
  Basketball-Reference or bulk checks remain in the ledger. Google News Archive supplied readable Pittsburgh issues.
  Brooklyn Newsstand points to its Newspapers.com partner. Exact-page checks on the New York and California archive
  routes encountered access controls. Most early gaps still have blank, incomplete or non-reconciling source lines.
  `audit.period_candidate_newspaper` keeps OCR leads pending scan verification.
  [Chronicling America](https://www.loc.gov/collections/chronicling-america/)
  [Brooklyn Newsstand](https://www.bklynlibrary.org/online-resources/brooklyn-newsstand)
  [NYS Historic Newspapers](https://nyshistoricnewspapers.org/)
  [California Digital Newspaper Collection](https://cdnc.ucr.edu/)

### Crews, club boxes and unnamed players

- USA TODAY's preseason summaries publish named 12:00 first-quarter starting-lineup events and referee names. The
  loader confirms the date, team-keyed final and player identities before staging those names. A known outside-venue
  home-designation difference requires the same venue and identical team-keyed scores. Seven international fixture
  searches produced no matching USA TODAY summaries. Two broadcast leads remain unverified for visible starter cards.
  [2005 lineup example](https://sportsdata.usatoday.com/basketball/nba/summary/661118)
  [2006 lineup example](https://sportsdata.usatoday.com/basketball/nba/summary/713521)
- Later exact USA TODAY checks found both starting fives in twelve 2004 games with partial records and printed crews
  in ten more 2006 games. A complete USA TODAY crew can supply members to an earlier partial crew when it contains
  every already selected official. Joe Lindsay and Pat Adams have separately verified identities from Mark Lindsay
  and Bennie Adams. Barcelona–Philadelphia’s crew is Joe Forte, David Jones and Eddie Viator.
  [Utah–Philadelphia lineup](https://sportsdata.usatoday.com/basketball/nba/summary/541916)
  [USA TODAY 2006 crew example](https://sportsdata.usatoday.com/basketball/nba/summary/713511)
  [NC State’s Joe and Mark Lindsay crew](https://stats.gopack.com/sports/m-baskbl/2004-2005/ncsu1126.html)
  [Contemporary Pat Adams report](https://www.courant.com/1995/10/19/only-5646-see-celtics-lose-2/)
  [Barcelona crew](https://20secondtimeout.blogspot.com/2006/10/philadelphia-76ers-open-nba-europe.html)
- Ten 2026–27 NBA final books supply 30 exact officiating assignments. The source receipt retains the published
  order, names and uniform numbers. Five officials have verified league identities and persistent warehouse IDs.
  Andy Nagy retains his existing NBA ID. Current-season uniform numbers stay on the assignments because historical
  numbers can belong to a different current official. The retrieved game receipts are text extractions of the NBA
  PDFs. Direct retrieval of the game PDF bytes timed out.
  [2026–27 league officiating roster](https://cms.nba.com/official/wp-content/uploads/sites/4/2026/10/2026-27-NBA-Officiating-Staff.pdf)
  [Indiana–Milwaukee final book](https://statsdmz.nba.com/pdfs/20261026/20261026_INDMIL.pdf)
  [Spurs–ASVEL broadcast](https://www.youtube.com/watch?v=QHSwrRIeCFo)
  [Maccabi–Toronto broadcast](https://www.youtube.com/watch?v=iPwq5MuosLs)
- Hürriyet prints independently dated, score-matched three-person crews for the Efes–Denver and Efes–Golden State
  preseason games. Their published scoring summaries do not contain full player boxes. The Denver quarter totals
  disagree within the article and were held. [Denver report](https://www.hurriyet.com.tr/sporarena/efesin-gucu-nbali-denvera-yetmedi-118-102-5236475)
  [Golden State report](https://www.hurriyet.com.tr/sporarena/golden-state-warriors-120-efes-pilsen-66-5251233)
- CSKA's October 11, 2006 box supplies all 11 played CSKA rows, shows its five starters and corroborates the missing
  Steven Smith line for Philadelphia. The two teams' player points total 71 and 85. The October 7 game corroborates ten
  CSKA player identities on the old NBA IDs. The club's season profile and the contemporary roster establish the
  Pashutin/Pachoutine alias. One DNP lacks a verified NBA ID and remains in `audit.club_box_receipt`.
  [October 11 box](https://old.cskabasket.ru/gm/?a=boxscore&id=360&lang=en)
  [October 7 box](https://old.cskabasket.ru/gm/?a=boxscore&id=332&lang=en)
  [Pashutin club profile](https://old.cskabasket.ru/team/?lang=en&player=1&season=55)
  [Contemporary roster](https://www.solobasket.com/internacional/euroliga-2006-2007-cska-moscow)
- An NBA archive has the full October 5, 2006 Barcelona–Philadelphia manual box. Both teams’ player points and
  minutes sum to their printed totals. Sport independently confirms both starting fives plus the 13 points for Andre
  Iguodala and four for Mario Kasun. Their inconsistent shooting fields stay null. The separate Lyon archive
  supplies San Antonio’s 115 points and 238 minutes of reported player time. Its ASVEL player attribution is
  inconsistent, so the raw rows remain held in `audit.manual_box_player_receipt`.
  [Barcelona manual box](https://web.archive.org/web/20070630213129id_/http://www.nba.com/games/20061005/PHIBAR/manual_boxscore.html)
  [Sport match report](https://www.sport.es/es/noticias/baloncesto/barca-hizo-historia-costa-sixers-5529093)
  [ASVEL manual box](https://web.archive.org/web/20061103004013id_/http://www.nba.com/games/20061005/SASLYV/manual_boxscore.html)
  [Eurosport match report](https://www.eurosport.fr/basketball/nba/2006-2007/_sto979120/story.shtml)
  Eurosport’s nine verified ASVEL scorers sum to 90. Their points are available in `audit.club_partial_scorer`.
  Individual shooting and playing time remain held.
- USA TODAY’s October 28, 2005 box names Smush Parker as the Lakers player with five points, three assists and
  24:26 of playing time. The Orange County Register confirms the identity and shooting line. The warehouse’s data
  for that performance had been attributed to Adam Parada. [USA TODAY box](https://sportsdata.usatoday.com/basketball/nba/stats/661281)
  [Orange County Register box](https://www.ocregister.com/2005/10/29/stars-starting-to-align/)
- Twelve blank NBA.com player names were resolved from cached same-fixture NBA and ESPN stat lines or unique roster
  remainders. The NBA IDs are preserved. Nineteen still lack reproducible links, including DNP groups without names,
  jersey numbers or distinctive stats. The legacy Petteri Koponen profile appears in the index under the exact NBA ID.
  A fresh fetch was blocked. The ESPN fixture validator uses verified numerical identities for eight foreign clubs
  whose abbreviated labels differ. [Indexed NBA profile](http://nba.com/players/petteri/koponen/1962937467)
- Deseret News prints the complete Chicago and Indiana team shooting lines for October 8, 2003. Unprinted fields remain
  null. Philadelphia's March 3, 1950 box includes a documented own basket credited to the team. The published shooting
  totals account for 64 points and the opposition basket contributes two to the official 66. [Chicago–Indiana box](https://deseret.com/2003/10/9/19789070/pacers-96-bulls-82)
  [Philadelphia–Minneapolis box](https://www.basketball-reference.com/boxscores/195003030PHW.html)
  [Contemporary scoring research](https://sports-reference.com/blog/2019/03/own-goals-added-to-nba-aba-box-scores)

### Pregame source limits

- Billmill’s continuously dated NBA team roster captures start November 5, 2026. Its recorded batch start precedes
  the publication of team rows by roughly two minutes in the checked files. As-of roster queries use the source
  object’s verified publication timestamp or the local receipt time. Historical roster releases add two earlier
  snapshots with 773 linked player names from 843 printed names. `audit.nba_roster_release_name` includes the
  unlinked names. These dated opening and restart lists establish roster availability for their effective dates.
  [Billmill roster publication code](https://github.com/llimllib/nba_data/blob/d3941f7b3105973ef77bf21409eb6bd9c2b65903/pipeline/stats.py#L438-L485)
  [2018–19 opening rosters](https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2018/10/2018-19-Team-Rosters.pdf)
  [2020 restart rosters](https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2020/07/Restart-Rosters-7-19-20.pdf)

- The original Pinnacle imports cast their timestamps without a timezone. The producer’s pipeline contains no
  timezone conversion. The export’s observed team order is away then home. Unique matches by that order include
  17 main lines and 2,034 detailed rows. The warehouse preserves the other candidates, earlier crosswalk and
  source-order agreement in `betting.pinnacle_match_review`. The clock remains unknown for those export rows.
  [Producer timestamp casts](https://github.com/nickth3man/basketball-data-index/blob/6e577d63bc4cc3a34c296a7b3d7ad42ef98c31a1/projects/test-db/build.py#L552-L625)
  [Boston at Philadelphia](https://www.nba.com/game/bos-vs-phi-0042500116)
  [Houston at the Lakers](https://www.nba.com/game/hou-vs-lal-0042500175)

- Billmill serves copies of the original NBA PDFs, including the first 2018 report and samples through the
  2025–26 playoffs. Wayback CDX gives another list of official PDF URLs. The combined 2019–26 index contains
  16,084 candidate filenames. Both indices are sampled and leave historical coverage incomplete. Original and
  retrieval URLs and the byte digest are retained in `raw.nba_injury_report_retrieval`. The report’s printed
  Eastern timestamp controls eligibility. The provider changed legacy filenames to quarter-hour suffixes at
  9:00 a.m. Eastern on December 22, 2025.
  [Billmill publisher workflow](https://github.com/llimllib/nba_data/blob/d3941f7b3105973ef77bf21409eb6bd9c2b65903/.github/workflows/injuries.yml#L93-L100)
  [Example Wayback PDF index](https://web.archive.org/cdx/search/cdx?url=ak-static.cms.nba.com%2Freferee%2Finjury%2FInjury-Report_2025-12%2A&output=json&filter=statuscode%3A200&collapse=urlkey&fl=timestamp%2Coriginal%2Cstatuscode&limit=20)
  [Final hourly filename example](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-12-22_08AM.pdf)
  [First minute-qualified example](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-12-22_09_00AM.pdf)

- The NBA PDF cache keeps printed timestamps, statuses and notices. The season mirror covers 2019–2027 and contains
  selected pregame snapshots. Strict pregame queries corroborate identities with the roster available at the cutoff.
  Final-report reconciliation can use the later game box and keeps unresolved source player IDs. The direct historical
  PDF backfill is resumable, with each completed month published separately. Further historical PDFs remain to be fetched.
  [NBA report source](https://www.nba.com/players/injury-report)
  [Mirror build and selection rules](https://github.com/llimllib/nba_data/blob/main/pipeline/injury_report.py)
- The roster mirror exposes actual capture timestamps in its current-season raw files. The initial import covers
  November 5–25, 2026 across all 30 teams. Historical opening-night lists and date-only transaction evidence are useful
  anchors with limited intraday resolution. Schedule captures are versioned so an earlier cutoff still sees the original
  schedule. [Roster schema](https://github.com/llimllib/nba_data/blob/d3941f7b3105973ef77bf21409eb6bd9c2b65903/docs/DATA_DICTIONARY.md#L656-L675)
  [Raw NBA roster capture](https://basketball-data.billmill.org/nba/raw/stats/2027/team_rosters/2026-11-24.json.gz)
  [NBA movement feed](https://stats.nba.com/js/data/playermovement/NBA_Player_Movement.json)
- The original Pinnacle source dictionary does not specify timezone or quote publication time. Its dashboard groups the
  field as first and last seen. `betting.pinnacle_match_review` retains ambiguous links, unmatched rows and disagreements
  with the publisher crosswalk. Timestamped Kaggle feeds can enter the pregame views only for verified fixture links and
  reported times strictly before tip-off, including when the publisher labels a quote as a close.
  [Publisher dictionary](https://github.com/nickth3man/basketball-data-index/blob/6e577d63bc4cc3a34c296a7b3d7ad42ef98c31a1/csv/nba/data_dictionary.csv#L364-L401)
  [Publisher dashboard](https://github.com/nickth3man/basketball-data-index/blob/6e577d63bc4cc3a34c296a7b3d7ad42ef98c31a1/projects/db-viz/pages/09_Odds_and_Betting.py#L61-L98)
