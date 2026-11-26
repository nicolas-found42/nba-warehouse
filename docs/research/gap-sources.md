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
  scoring. Four have matching quarter vectors at both sources. The Salt Lake Tribune’s contemporary box settles
  the Utah–New York second-half discrepancy and matches USA TODAY. New York led 65–60 after three quarters.
  Philadelphia’s corrected Utah box restores Aaron McKie’s published one point and leaves his playing time unresolved.
  [Sacramento–Utah](https://sportsdata.usatoday.com/basketball/nba/stats/541891)
  [New Orleans–Clippers](https://sportsdata.usatoday.com/basketball/nba/stats/541906)
  [Utah–Philadelphia](https://sportsdata.usatoday.com/basketball/nba/stats/541916)
  [Seattle–Phoenix](https://sportsdata.usatoday.com/basketball/nba/stats/541919)
  [Utah–New York](https://sportsdata.usatoday.com/basketball/nba/stats/541932)
  [Salt Lake Tribune box](https://archive.sltrib.com/article.php?id=2433748&itype=NGPSID)
- The March 31, 1951 New York–Syracuse playoff was 77–75 in overtime. A contemporary Associated Press box
  confirms the final and 70–70 regulation score, and the original player points already reconcile. The cached
  line score uses `1OT` for the fifth period, as does the March 26, 1950 Syracuse–New York game. The parser now
  accepts contiguous overtime labels and requires a tie before each additional period.
  [1951 Evening Star, image 32](https://www.loc.gov/resource/sn83045462/1951-04-01/ed-1/?sp=32)
  [1950 Evening Star, image 18](https://www.loc.gov/resource/sn83045462/1950-03-27/ed-1/?sp=18)
- The public Associated Press and Daily Iowan accounts agree that Waterloo’s 86–84 result against Philadelphia required
  overtime, but the available period line gives the losing side 86 points. The Cleveland–Pittsburgh 1946 line lacks the
  overtime cells. Both lines remain held. The remaining 182 quarter-score gaps have four failed-source entries per game and
  explanatory notes in `audit.gap_attempt`.
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
- The São Paulo federation identifies Marcos Fornies Benito on the 2015 Orlando–Flamengo crew. The archived NBA
  report credits calls in that exact game to Benito and names the two NBA officials, Kevin Cutler and Derrick Stafford.
  [São Paulo federation](https://fpb.com.br/o-arbitro-paulista-marcos-benito-fez-historia/)
  [Archived NBA original URL](http://nba.com/games/20151017/ORLFLA/gameinfo.html)
- The old lineup feed reported more than five starters on many team sides. The warehouse replaces an invalid set
  with the NBA’s explicit starter flags only when exactly five distinct members match played rows for that game
  and team. It preserves positions with a single recorded value. `audit.starting_lineup_shape` records the exact named
  fives, incomplete team shapes and counts. The original feed remains available.
  [NBA starter-flag mirror example](https://basketball-data.billmill.org/nba/stats/player_game_status/season=2019/data.parquet)
- The NBA’s archived January 12, 2007 box, five NBA final scorer reports and seven Basketball-Reference boxes with
  matching ESPN starter flags resolve 13 remaining malformed fives. Each of these teams had a starter with a sub-minute
  appearance that the old feed rounded to zero. The receipt names the five players in printed order, with positions
  when published and the underlying capture digest. The five NBA gamebooks are verified text extracts.
  [Golden State original box](https://web.archive.org/web/20070115211315/http://www.nba.com:80/games/20070112/MIAGSW/boxscore.html)
  [Utah–Clippers scorer report](https://statsdmz.nba.com/pdfs/20170415/20170415_UTALAC_book.pdf)
  [Minnesota original starter section](https://www.basketball-reference.com/boxscores/200405230MIN.html)
- The 34 remaining lineup gaps and 26 incomplete or missing officiating crews each have at least two independent failed
  sources with notes in `audit.gap_disposition`. There are 40,296 games with two exact five-player lineups.
  For partly documented player boxes the published team facts and club
  scoring receipts remain intact. Their unfilled side is likewise labelled `source_unavailable` after two independent
  failures.
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
- Twelve blank NBA.com player names were resolved from same-fixture stat lines and roster remainders. Archived
  October 2016 NBA player directories supply 12 more exact numeric ID links. FC Barcelona and the Israeli league
  establish the spellings Màxim Esteban and Ofek Avital. The audit retains the matching historical NBA labels.
  Seven 2015 identities remain unlinked. Milan and Real Madrid legacy IDs are absent from the retrieved directories.
  Fenerbahçe’s three anonymous IDs have identical DNP fields. Two gamebooks name the players, while the archived
  NBA roster has no link from those names to the anonymous IDs.
  [October 9 NBA directory](https://web.archive.org/web/20161009035904id_/http://www.nba.com:80/players/petteri/koponen/1962937467)
  [October 12 NBA directory](https://web.archive.org/web/20161012211755id_/http://www.nba.com:80/players)
  [Barcelona report, page 52](https://fcbarcelona.com/fcbarcelona/document/2020/03/03/6bb9c864-7e88-480c-9646-32fbe6da0b51/MEMORIA_CLUB_2018_19_ENG.pdf#page=52)
  [Ofek Avital’s league profile](https://basket.co.il/player.asp?PlayerId=10519&lang=en)
  [Fenerbahçe–Brooklyn gamebook](https://statsdmz.nba.com/pdfs/20151005/20151005_FENBKN_book.pdf)
  [Fenerbahçe–Oklahoma City gamebook](https://statsdmz.nba.com/pdfs/20151009/20151009_FENOKC_book.pdf)
- Deseret News prints the complete Chicago and Indiana team shooting lines for October 8, 2003. Unprinted fields remain
  null. Philadelphia's March 3, 1950 box includes a documented own basket credited to the team. The published shooting
  totals account for 64 points and the opposition basket contributes two to the official 66. [Chicago–Indiana box](https://deseret.com/2003/10/9/19789070/pacers-96-bulls-82)
  [Philadelphia–Minneapolis box](https://www.basketball-reference.com/boxscores/195003030PHW.html)
  [Contemporary scoring research](https://sports-reference.com/blog/2019/03/own-goals-added-to-nba-aba-box-scores)

### Pregame source limits

- Billmill’s continuously dated NBA team roster captures start November 5, 2026. Its recorded batch start precedes
  the publication of team rows by roughly two minutes in the checked files. As-of roster queries use the source
  object’s verified publication timestamp or the local receipt time. Historical roster releases add two earlier
  snapshots with 773 linked player names from 844 printed names. `audit.nba_roster_release_name` includes the
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
- Dated NBA schedules corroborate the two early October-opening quotes and the postponed Miami–Chicago game.
  The Guangzhou name and two neutral-site All-Star pairings now resolve to their published game IDs. Two
  Stars–Stripes games have identical participants and venue, so the Pinnacle quote remains unresolved. The
  legacy source times remain excluded from strict pregame eligibility.
  [NBA opening schedule](https://pr.nba.com/2025-26-nba-regular-season-schedule)
  [Miami–Chicago reschedule](https://pr.nba.com/heat-bulls-schedule-adjustments)
- The opening-night evidence resolves another 1,550 detailed Pinnacle quotes. Nineteen September snapshots have an exact
  event pin in the original crosswalk. Twelve additional Golden State–Lakers snapshots repeat a confirmed 50-quote payload
  exactly. Three Houston–Oklahoma City snapshots with a changed payload await an independent fixture citation.
  [NBA opening schedule](https://pr.nba.com/2025-26-nba-regular-season-schedule)
- Twelve repeated matchups have distinct historical spread or total lines for the two possible dates and a separate
  Pinnacle event already matched to the alternate fixture. The main-line review applies to the exact original snapshot
  keys. Three additional detailed snapshots repeat all 18 quotes of the reviewed Golden State–San Antonio or
  Washington–Atlanta fixture. The 41 main lines and 6,732 detailed prices with a same-side disagreement and only a
  provisional time-window link remain in `betting.pinnacle_match_review`, with both candidates recorded.
  [Cleveland–Miami, November 10](https://www.fanduel.com/research/nba-predictions-odds-11-10-2025)
  [Chicago–Atlanta, December 21](https://www.fanduel.com/research/nba-predictions-odds-12-21-2025)
  [Minnesota–Memphis, January 31](https://www.fanduel.com/research/nba-predictions-odds-1-31-2026)
  [Detroit–Washington, March 17](https://www.fanduel.com/research/nba-predictions-odds-3-17-2026)
  [Miami–Cleveland, March 25](https://www.fanduel.com/research/nba-predictions-odds-3-25-2026)
  [Golden State–San Antonio, November 12](https://www.fanduel.com/research/nba-predictions-odds-11-12-2025)
  [Washington–Atlanta, February 24](https://www.fanduel.com/research/nba-predictions-odds-2-24-2026)
  [Stars–Stripes contemporaneous preview](https://www.si.com/betting/usa-stars-vs-usa-stripes-odds-spread-total-and-prediction-for-2026-nba-all-star-game)

- Billmill serves copies of the original NBA PDFs, including the first 2018 report and samples through the
  2025–26 playoffs. Wayback CDX gives another list of official PDF URLs. The combined 2019–26 index contains
  16,084 filenames, all with parsed rows in the warehouse. Both indices are sampled. A separate direct sweep checks
  standard report slots for each NBA game day and the preceding day. Original and
  retrieval URLs and the byte digest are retained in `raw.nba_injury_report_retrieval`. The report’s printed
  Eastern timestamp controls eligibility. The provider changed legacy filenames to quarter-hour suffixes at
  9:00 a.m. Eastern on December 22, 2025.
  [Billmill publisher workflow](https://github.com/llimllib/nba_data/blob/d3941f7b3105973ef77bf21409eb6bd9c2b65903/.github/workflows/injuries.yml#L93-L100)
  [Example Wayback PDF index](https://web.archive.org/cdx/search/cdx?url=ak-static.cms.nba.com%2Freferee%2Finjury%2FInjury-Report_2025-12%2A&output=json&filter=statuscode%3A200&collapse=urlkey&fl=timestamp%2Coriginal%2Cstatuscode&limit=20)
  [Final hourly filename example](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-12-22_08AM.pdf)
  [First minute-qualified example](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2025-12-22_09_00AM.pdf)
- The July 5, 2021 report labelled `08PM` prints 9:55 p.m. Eastern in both the direct PDF and archived copy.
  The warehouse records that printed time with the original filename and retrieval provenance.
  [Original NBA PDF](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2021-07-05_08PM.pdf)
- The NBA index, Billmill's 2026–27 data and archived NBA index pages identify 520 October–November 2026 filenames.
  All 520 are verified. The direct regular-season sweep published 3,417 PDFs from October 19 through November 23,
  and the daily capture continued on November 24 and 25. An October 1 archive copy labelled 12:45 p.m. prints
  a 12:56 p.m. Eastern timestamp. The warehouse keeps
  the original filename and retrieval route and uses the printed time. The five rows list Boston at Detroit on October 1.
  Neither team’s preseason schedule includes that fixture, so the rows stay unmatched. The NBA records the teams’ regular
  season opener on October 20.
  [Current NBA index](https://www.nba.com/players/injury-report)
  [Archived October index](https://web.archive.org/web/20261024021942/https://official.nba.com/nba-injury-report-2026-27-season/)
  [July original PDF](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-07-08_12_45PM.pdf)
  [October 1 original replay](https://web.archive.org/web/20261002063922id_/https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-10-01_12_45PM.pdf)
  [Boston preseason schedule](https://www.nba.com/celtics/news/celtics-announce-2026-27-preseason-schedule)
  [Detroit schedule](https://www.nba.com/pistons/news/detroit-pistons-announce-2026-27-season-schedule-presented-by-ticketmaster)
- The July sweep published 1,498 original Summer League reports across 2022–26, including all 77 files in the
  initial 2026 July archive. A supplemental annual archive index contributed 129 verified filenames. On November 25,
  the CDN returned explicit access-denied pages for several 2026 Summer League filenames previously verified as
  reachable. Capture now stops contacting the NBA PDF origin for that run when it returns this access-denied page
  or HTTP 429. Daily refresh can publish already retrieved PDFs and continue with the schedule and rosters.
  The remaining July 2026 publication slots await verification. The local evidence folder retains the
  exact URLs, response bodies and digests alongside the earlier valid PDFs.
  [2022 Summer League schedule](https://www.nba.com/news/2022-nba-summer-league-schedules-las-vegas-san-francisco-and-salt-lake-city)
  [2026 Summer League schedule](https://www.nba.com/news/2026-nba-summer-league-schedule)
  [Verified July 17 original](https://ak-static.cms.nba.com/referee/injury/Injury-Report_2026-07-17_11_30AM.pdf)
- Nine SportsDataverse release assets for ending seasons 2019–2027 contain 1,376,456 rows at 14,259 report times.
  The local original PDFs corroborate 13,447 times using their printed Eastern timestamps. The additional 812 times
  contribute 60,094 rows of parsed evidence in `audit.injury_report_archive_copy`. They include 17 distinct July 2026
  report times and 340 team notices. The assets contain no PDF bytes or original filename field. The two July 2026
  reports with matching originals agree with all 18 published team-notice rows. The loader records each asset's
  publisher URL, SHA-256 and row position and keeps the publisher's candidate IDs in the raw rows.
  `audit.injury_report_archive_resolution` corroborates 38,064 named rows and 7,598 team notices against warehouse
  fixtures. It holds 12,910 names and 1,522 rows for which it cannot corroborate a fixture.
  [SportsDataverse release](https://github.com/sportsdataverse/sportsdataverse-data/releases/tag/nba_stats_injury_report)
  [Publisher PDF timestamp parser](https://github.com/sportsdataverse/sportsdataverse-py/blob/0013f41b4b28ed9104f37917dc56cf89e5183c13/sportsdataverse/nba/nba_official_injury.py#L198)
- The NBA's daily Summer League game indexes provide official IDs for the 82 distinct matchups in the July 2026
  SportsDataverse notices. The audit matches exact game date and designated away and home teams. It attaches NBA
  game IDs to all 340 independently transcribed notices at missing PDF times and to 1,682 rows from cached NBA PDFs.
  The published daily index receipts and game links support `audit.injury_report_summer_fixture`.
  [July 4 game index](https://www.nba.com/games?date=2026-07-04)
  [July 9 game index](https://www.nba.com/games?date=2026-07-09)

- The NBA PDF cache keeps printed timestamps, statuses and notices. The season mirror covers 2019–2027 and contains
  selected pregame snapshots. Strict pregame queries corroborate identities with the roster available at the cutoff.
  Daily refresh rechecks missing URLs for the current and previous publication days and retries transient connection
  failures. Ordinary public retries recovered original PDFs after temporary CDN or archive responses failed to serve a PDF.
  Final-report reconciliation can use the later game box and official inactive list to verify a game-team identity.
  The final view exposes available box-score participation and official inactive status in separate columns and keeps
  unresolved source player IDs. The direct historical PDF backfill is resumable, with each completed month published
  separately. The November 25 audit counted 41,963 original PDF filenames and 3,064,235 raw report rows, all with
  retrieval provenance. The capture spans December 17, 2018 through November 25, 2026.
  [NBA report source](https://www.nba.com/players/injury-report)
  [Mirror build and selection rules](https://github.com/llimllib/nba_data/blob/main/pipeline/injury_report.py)
- The PDF parser preserves injury reason text printed above a vertically centred player row and when a reason continues
  on the next page. An original-PDF comparison preserved every row key on 308 sampled documents. All 38 changed
  reason rows with an exact SportsDataverse counterpart agreed with its transcription after whitespace and punctuation
  normalization. The same checks confirmed the four original files that exposed the omissions.
- Jock Landale was the only player in the November 25 audit whose NBA injury report named one team and whose only
  official game record was with the opponent. Utah listed him as out with a trade pending in 80 reports. Atlanta announced his
  acquisition from Utah at 10:30 a.m. Eastern on February 5, 2026, and Utah's 12:15 p.m. Eastern announcement confirmed
  the trade. The Utah injury entry disappeared at 1 p.m. and was absent from both final reports. Landale played for
  Atlanta. The historical snapshots preserve his printed team and identity, and the final view joins participation
  on the exact game, player and team.
  [Atlanta acquisition](https://www.nba.com/hawks/news/atlanta-hawks-acquire-center-jock-landale)
  [Utah confirmation](https://www.nba.com/jazz/news/utah-jazz-acquire-lonzo-ball-and-two-future-second-round-draft-picks)
  [Utah game recap](https://www.nba.com/jazz/news/utah-jazz-fall-at-the-buzzer-to-atlanta-hawks-three-takeaways)
- The roster mirror exposes actual capture timestamps in its current-season raw files. The initial import covers
  November 5–25, 2026 across all 30 teams. Historical opening-night lists and date-only transaction evidence are useful
  anchors with limited intraday resolution. Schedule captures are versioned so an earlier cutoff still sees the original
  schedule. [Roster schema](https://github.com/llimllib/nba_data/blob/d3941f7b3105973ef77bf21409eb6bd9c2b65903/docs/DATA_DICTIONARY.md#L656-L675)
  [Raw NBA roster capture](https://basketball-data.billmill.org/nba/raw/stats/2027/team_rosters/2026-11-24.json.gz)
  [NBA movement feed](https://stats.nba.com/js/data/playermovement/NBA_Player_Movement.json)
- The NBA opening-night roster PDFs for 2019–20 through 2023–24 each list all 30 teams. The warehouse uses the first
  corroborated publication time for each pinned edition and preserves unlinked names and printed roster statuses in
  `audit.nba_roster_release_name`. The July 2020 restart and October 2018 opening rosters are also available.
  [2019 original roster PDF](https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2019/10/2019-20-Regular-Season-Rosters.pdf)
  [2020 release](https://pr.nba.com/rosters-set-for-2020-21-nba-regular-season/)
  [2021 original roster PDF](https://s3.amazonaws.com/ukathletics.com/documents/2021/10/19/2021_22_Opening_Day_Rosters_10_18_21.pdf)
  [2022 release](https://pr.nba.com/rosters-set-for-2022-23-nba-regular-season/)
  [2023 PDF publication](https://thisistucson.com/nba-opening-rosters-2023-24/pdf_b52fd976-71ff-11ee-8fbf-d7479459d4aa.html)
- NBA Communications published the complete 2024–25 opening roster on October 21, 2024. The three original
  graphics cover 30 teams with 522 printed names and 495 linked player IDs. The verified roster cutoff is
  6:15:10 p.m. Eastern on October 21. One Toronto entry has an ambiguous printed contract mark and stays in the
  printed inactive-or-two-way category.
  [2024 NBA Communications roster release](https://x.com/NBAPR/status/1848488220805198060)
- The NBA's 2025–26 opening-roster release covers all 30 teams and 522 names. The warehouse linked 498 to player
  IDs. The local audit keeps all printed names and statuses. The exact attachment was available by October 20 at
  8:20:24 p.m. Eastern. The opening-night Houston–Oklahoma City roster query returns each team's 17 matched names
  at that timestamp, with zero rows at the preceding second.
  [2025 PDF publication](https://tucson.com/nba-opening-rosters-2025-26/pdf_4ff8d6d8-1df5-4f17-aadb-dacba08d7860.html)
- NBA Communications' 2026–27 opening roster shows 518 names across 30 teams. The Milwaukee Bucks published a
  complete 18-player revision at 11:36:56 p.m. Eastern on October 19 after announcing Pete Nance's two-way signing.
  The latest opening snapshots together have 519 names and 493 linked player IDs. The warehouse uses the complete
  Milwaukee roster from its table publication time, 5 minutes and 20 seconds after the signing announcement.
  [2026 NBA Communications roster release](https://x.com/NBAPR/status/2112301822576865466)
  [Milwaukee's finalized roster](https://www.nba.com/bucks/news/milwaukee-bucks-finalize-2026-27-opening-night-roster)
- The NBA’s original 2021–22 through 2026–27 by-date schedules and its 2020–21 second-half release link by
  exact date and designated teams. The 2021–22 release marks 19 games as pending a tip time. The 2020–21 second-half
  release marks 21. Their times remain unknown in the as-of fixture view until a later dated schedule supplies them.
  The end of the NBA’s published day bounds the 2022 and 2025 releases. Injury and roster
  observations must also predate the recorded tip when a completed game started earlier than originally scheduled.
  The 2026–27 by-date PDF was publicly linked by 3:13:47 p.m. Eastern on August 13. Its 1,200 games all match the
  warehouse fixture identities, and both teams' separately published schedules corroborate every tip time.
  [2021 schedule release](https://pr.nba.com/2021-22-nba-schedule-75th-anniversary-season/)
  [2020–21 second-half schedule release](https://pr.nba.com/2020-21-nba-second-half-schedule/)
  [2022 original schedule](https://pr.nba.com/wp-content/uploads/sites/46/2022/08/2022-23-NBA-Schedule-By-Date-8-17-22.pdf)
  [2023 schedule release](https://pr.nba.com/2023-24-nba-regular-season-schedule/)
  [2024 schedule release](https://pr.nba.com/2024-25-nba-regular-season-schedule)
  [2025 schedule release](https://pr.nba.com/2025-26-nba-regular-season-schedule)
  [2026 schedule release](https://www.nba.com/news/2026-27-schedule-team-by-team-index)
- Cleveland at Milwaukee on March 9, 2025 has two different tips in the original August 15 schedule package.
  The by-date PDF lists 7 p.m. Eastern and both team schedules list 8 p.m. Eastern. The as-of fixture keeps
  the dated matchup with an unknown tip. `audit.nba_initial_schedule_tip_conflict` retains both values and the source PDFs.
  [NBA schedule by date](https://ak-static.cms.nba.com/wp-content/uploads/sites/46/2024/08/2024-25-NBA-Regular-Season-Schedule-By-Date.pdf)
  [NBA schedules by team](https://pr.nba.com/wp-content/uploads/sites/46/2024/08/2024-25-NBA-Regular-Season-Schedule-By-Team.pdf)
- The original Pinnacle source dictionary does not specify timezone or quote publication time. Its dashboard groups the
  field as first and last seen by matchup text, which can span several events. A publisher capture of the Pinnacle
  browsing interface shows a GMT header clock. Pinnacle documents that the website timezone is configurable.
  The capture contains no quote timestamps or fixture IDs, and no public producer source connects its header clock
  to the export's `snapshot_ts` field. `betting.pinnacle_match_review` retains ambiguous links, unmatched rows and disagreements
  with the publisher crosswalk. Timestamped Kaggle feeds can enter the pregame views only for verified fixture links and
  reported times strictly before tip-off, including when the publisher labels a quote as a close. Six sampled exact event
  URLs served identical page shells without fixture metadata. Wayback Availability and CDX returned successful empty
  results for all 65 unresolved main-line event URLs. The local evidence folder retains each response and its digest.
  [Publisher dictionary](https://github.com/nickth3man/basketball-data-index/blob/6e577d63bc4cc3a34c296a7b3d7ad42ef98c31a1/csv/nba/data_dictionary.csv#L364-L401)
  [Publisher dashboard](https://github.com/nickth3man/basketball-data-index/blob/6e577d63bc4cc3a34c296a7b3d7ad42ef98c31a1/projects/db-viz/pages/09_Odds_and_Betting.py#L61-L98)
  [Publisher's Pinnacle interface capture](https://github.com/nickth3man/basketball-data-index/blob/f3963e4eba37bfd86b8552c14d349b9766c5276c/html/debug_page.html#L79)
  [Pinnacle timezone settings](https://support.pinnacle.com/hc/en-us/articles/42376057586321-How-do-I-change-the-Time-Zone-on-the-website)
  [Sample Pinnacle event](https://www.pinnacle.com/en/basketball/nba/usa-stars-vs-usa-stripes/1624285681/)
- The two Kaggle NBA sample exports encode the same UTC fixture and quote times as text and typed timestamps.
  Interpreting both fixture fields as UTC before converting to the NBA game date restored 1,700,111 fixture links in
  the typed export. Both copies now link their moneyline, spread and totals rows to the same 1,171 fixtures. The raw
  price and quote-time columns agree after type normalization.
