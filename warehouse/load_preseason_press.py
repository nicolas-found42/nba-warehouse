"""Verify preseason referee crews reported by Hürriyet against the keyed fixtures."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock, warehouse_transaction
from fetch_wayback_nba import cache_bytes
from load_bref import NEW_OFFICIAL_ID_BASE
from promote_domains import OFFICIAL_NAME_CANON, create_press_official_identities

CACHE = Path.home() / 'Library' / 'Application Support' / 'NBA Warehouse' / 'evidence' / 'newspapers' / 'hurriyet'
FIXTURES = {
    '0010600011': ('2006-10-10', 1610612743, 'Denver Nuggets', 'Denver',
                   'https://www.hurriyet.com.tr/sporarena/efesin-gucu-nbali-denvera-yetmedi-118-102-5236475'),
    '0010600026': ('2006-10-12', 1610612744, 'Golden State Warriors', 'Golden State',
                   'https://www.hurriyet.com.tr/sporarena/golden-state-warriors-120-efes-pilsen-66-5251233'),
}
MEXICO = '0010300001'
MEXICO_SCORE_URL = 'https://deseret.com/2003/10/6/19788454/jazz-90-mavericks-85'
MEXICO_WITNESS_URL = 'https://eskimo.com/~pbender/mavs/mavs-news/older/03summer/mavs10-6'
INDIANA_BOX = '0010300012'
INDIANA_BOX_URL = 'https://deseret.com/2003/10/9/19789070/pacers-96-bulls-82'
CREW_EVIDENCE = CACHE.parent.parent / 'public-preseason-boxes'


def article(raw: bytes) -> dict:
    pages = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', raw.decode('utf-8'), re.S)
    return next(json.loads(page) for page in pages if '"articleBody"' in page)


def load(c) -> None:
    c.execute('''CREATE TABLE IF NOT EXISTS raw.press_game_official (
        game_id VARCHAR, official_id BIGINT, official_name VARCHAR, official_order INTEGER, source_url VARCHAR, sha256 VARCHAR,
        PRIMARY KEY (game_id, official_id, sha256))''')
    names = dict(c.execute(f'SELECT name, official_id FROM {OFFICIAL_NAME_CANON}').fetchall())
    for gid, (day, team_id, team_name, headline_name, url) in FIXTURES.items():
        path = CACHE / f'{gid}.html'
        if not path.is_file():
            continue
        raw = path.read_bytes()
        doc = article(raw)
        date = dt.datetime.fromisoformat(doc['datePublished']).date()
        title, body = doc['headline'], doc['articleBody']
        result = c.execute('''SELECT g.game_date::VARCHAR, g.home_team_id, g.away_team_id, r.home_score, r.away_score
            FROM core.dim_game g JOIN core.fact_game_result r USING (game_id) WHERE game_id = ?''', [gid]).fetchone()
        if result is None or result[:3] != (day, team_id, 12309) or date != dt.date.fromisoformat(day) + dt.timedelta(days=1):
            raise ValueError(f'newspaper date or participants disagree for {gid}')
        scores = tuple(map(int, re.findall(r'\d+', title)))
        if scores != result[3:] or headline_name not in title or 'Efes Pilsen:' not in body:
            raise ValueError(f'newspaper title or final disagrees for {gid}')
        crew = body.split('Hakemler:', 1)[1].split(team_name + ':', 1)[0]
        officials = [n.replace('*', '').strip() for n in crew.split(',')]
        ids = [names.get(re.sub(r'[^a-z]', '', n.lower())) for n in officials]
        if len(ids) != 3 or not all(ids) or len(set(ids)) != 3:
            raise ValueError(f'newspaper crew could not be resolved for {gid}')
        digest = hashlib.sha256(raw).hexdigest()
        with warehouse_transaction(c):
            c.executemany('INSERT OR IGNORE INTO raw.press_game_official VALUES (?,?,?,?,?,?)',
                          [(gid, oid, name, i + 1, url, digest) for i, (oid, name) in enumerate(zip(ids, officials))])
            c.execute('''INSERT INTO audit.gap_attempt
                SELECT 'officials', ?, 'newspaper_hurriyet', now(), 'admitted', ?
                WHERE NOT EXISTS (SELECT 1 FROM audit.gap_attempt WHERE domain = 'officials' AND game_id = ?
                    AND source = 'newspaper_hurriyet' AND outcome = 'admitted')''', [gid, url, gid])
        print(f'Newspaper crew verified: {gid}, {len(ids)} officials', flush=True)
    mexico_periods(c)
    indiana_team_box(c)
    barcelona_crew(c, names)


def verified_official(c, names: dict, first: str, last: str, source_url: str) -> int:
    key = re.sub(r'[^a-z]', '', (first + last).lower())
    if key in names:
        return names[key]
    create_press_official_identities(c)
    prior = c.execute('SELECT official_id FROM raw.press_official_identity WHERE first_name=? AND last_name=?',
                      [first, last]).fetchone()
    if prior:
        return prior[0]
    known = c.execute('''SELECT greatest(?,
        coalesce((SELECT max(official_id) FROM raw.press_official_identity),0),
        coalesce((SELECT max(official_id) FROM raw.bref_official_map WHERE how='new'),0))''',
        [NEW_OFFICIAL_ID_BASE]).fetchone()[0]
    official_id = known + 1
    c.execute('INSERT INTO raw.press_official_identity VALUES (?,?,?,?)', [official_id, first, last, source_url])
    return official_id


def barcelona_crew(c, names: dict) -> None:
    reporter = CREW_EVIDENCE / '20secondtimeout-20061005-phibar-crew.tinyfish.json'
    identity = CREW_EVIDENCE / 'ffbb-eddie-viator-official-bio.tinyfish.json'
    if not reporter.is_file() or not identity.is_file():
        return
    primary, witness = reporter.read_bytes(), identity.read_bytes()
    digest = hashlib.sha256(primary).hexdigest()
    if digest != '47e9b3eaf9434554da18d9c3e83d8f8a0b967fe1597cd6cd4636362b3c6074ab' or hashlib.sha256(witness).hexdigest() != (
            '8e935ed8d37c8aab0cb1ea067dbf5b5b887ea6275d2526d631da7757846e9e01'):
        raise ValueError('reviewed Barcelona crew evidence has changed')
    source, identity_source = json.loads(primary)['url'], json.loads(witness)['url']
    game = '0010600001'
    fixture = c.execute('''SELECT home_team_id,away_team_id,home_score,away_score FROM core.fact_game
        WHERE game_id=? AND game_date=DATE '2006-10-05' ''', [game]).fetchone()
    if fixture != (12304, 1610612755, 104, 99) or (names.get('joeforte'), names.get('davidjones')) != (1161, 1168):
        raise ValueError('reviewed Barcelona fixture or named NBA referees disagree')
    with warehouse_transaction(c):
        viator = verified_official(c, names, 'Eddie', 'Viator', identity_source)
        crew = ((1161, 'Joe Forte'), (1168, 'David Jones'), (viator, 'Eddie Viator'))
        c.executemany('INSERT OR IGNORE INTO raw.press_game_official VALUES (?,?,?,?,?,?)',
                      [(game, pid, name, i, source, digest) for i, (pid, name) in enumerate(crew, 1)])
        c.execute('''INSERT INTO audit.gap_attempt SELECT 'officials',?,'verified_press_crew',now(),'admitted',?
            WHERE NOT EXISTS (SELECT 1 FROM audit.gap_attempt WHERE domain='officials' AND game_id=?
                AND source='verified_press_crew' AND outcome='admitted')''', [game, source, game])
    print(f'Barcelona crew verified: {game}', flush=True)


def mexico_periods(c) -> None:
    box, witness = CACHE / 'deseret-mexico.html', CACHE / 'bender-mexico.html'
    if not box.is_file() or not witness.is_file() or c.execute(
            'SELECT 1 FROM core.fact_game_period_score WHERE game_id = ? LIMIT 1', [MEXICO]).fetchone():
        return
    raw, confirm = box.read_bytes(), witness.read_bytes()
    primary, comparison = raw.decode('utf-8'), confirm.decode('utf-8')
    if 'at Mexico City  (October 5)' not in comparison:
        raise ValueError('Mexico City witness does not identify the fixture')
    fixture = c.execute('''SELECT g.home_team_id,g.away_team_id,r.home_score,r.away_score
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
        WHERE g.game_id = ? AND g.game_date = DATE '2003-10-05' ''', [MEXICO]).fetchone()
    if fixture != (1610612762, 1610612742, 90, 85):
        raise ValueError('Mexico City score and designated sides disagree with the warehouse')
    scores = {}
    for team, team_id, final in (('Utah', fixture[0], fixture[2]), ('Dallas', fixture[1], fixture[3])):
        pattern = rf'{team}\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+[–—-]\s+{final}\b'
        found = {tuple(map(int, parts)) for parts in re.findall(r'<strong>' + pattern + r'</strong>', primary)}
        verified = {tuple(map(int, parts)) for parts in re.findall(r'(?m)^' + pattern, comparison)}
        if len(found) != 1 or found != verified or sum(next(iter(found))) != final:
            raise ValueError(f'printed Mexico City periods do not corroborate {team}')
        scores[team_id] = next(iter(found))
    with warehouse_transaction(c):
        c.execute('''CREATE TABLE IF NOT EXISTS audit.period_receipt_publication (
            game_id VARCHAR, source_url VARCHAR, sha256 VARCHAR, home_periods INTEGER[], away_periods INTEGER[],
            method VARCHAR, PRIMARY KEY (game_id, source_url))''')
        c.executemany('INSERT INTO audit.period_receipt_publication VALUES (?,?,?,?,?,\'printed_period_scores\')',
                      [(MEXICO, url, hashlib.sha256(body).hexdigest(), scores[fixture[0]], scores[fixture[1]])
                       for url, body in ((MEXICO_SCORE_URL, raw), (MEXICO_WITNESS_URL, confirm))])
        c.executemany('INSERT INTO core.fact_game_period_score VALUES (?,?,?,?)',
                      [(MEXICO, team_id, i, points) for team_id, line in scores.items() for i, points in enumerate(line, 1)])
        c.execute('''INSERT INTO audit.gap_attempt VALUES
            ('period_scores', ?, 'newspaper_deseret', now(), 'admitted', ?)''', [MEXICO, MEXICO_SCORE_URL])
    print(f'Printed period scores verified: {MEXICO}', flush=True)


def indiana_team_box(c) -> None:
    path = CACHE / 'deseret-indiana.html'
    if not path.is_file():
        return
    raw = path.read_bytes()
    page = raw.decode('utf-8')
    fixture = c.execute('''SELECT g.home_team_id,g.away_team_id,r.home_score,r.away_score
        FROM core.dim_game g JOIN core.fact_game_result r USING (game_id)
        WHERE g.game_id = ? AND g.game_date = DATE '2003-10-08' ''', [INDIANA_BOX]).fetchone()
    if fixture != (1610612754, 1610612741, 96, 82):
        raise ValueError('Indiana newspaper box does not match the warehouse fixture')
    digest = hashlib.sha256(raw).hexdigest()
    export = []
    for team, team_id, final, home in (('Chicago', fixture[1], fixture[3], False), ('Indiana', fixture[0], fixture[2], True)):
        row = re.search(rf'<strong>{team.upper()} \({final}\)</strong>[^<]*?Totals (\d+)-(\d+) (\d+)-(\d+) (\d+)\.</p>', page)
        three = re.search(rf'3-Point Goals[^<]*?{team} (\d+)-(\d+)', page)
        reb = re.search(rf'Rebounds[^<]*?{team} (\d+) \(', page)
        ast = re.search(rf'Assists[^<]*?{team} (\d+) \(', page)
        fouls = re.search(rf'Total Fouls[^<]*?{team} (\d+)\b', page, re.I)
        if not all((row, three, reb, ast, fouls)):
            raise ValueError(f'incomplete published team table for {team}')
        fgm, fga, ftm, fta, points = map(int, row.groups())
        tpm, tpa = map(int, three.groups())
        if 2 * fgm + tpm + ftm != points or points != final or not (tpm <= tpa <= fga and tpm <= fgm <= fga and ftm <= fta):
            raise ValueError(f'published team data does not reconcile for {team}')
        export.append((INDIANA_BOX, team_id, home, points > min(fixture[2:]), points, fgm, fga, tpm, tpa, ftm, fta,
                       int(reb[1]), int(ast[1]), int(fouls[1])))
    with warehouse_transaction(c):
        c.execute('''CREATE TABLE IF NOT EXISTS audit.team_box_receipt_publication (
            game_id VARCHAR, team_id BIGINT, source_url VARCHAR, sha256 VARCHAR,
            PRIMARY KEY (game_id, team_id, source_url))''')
        for row in export:
            c.execute('''INSERT INTO core.fact_team_game_stats (game_id,team_id,is_home,is_win,pts,fgm,fga,fg3m,fg3a,ftm,fta,reb,ast,pf)
                SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (
                    SELECT 1 FROM core.fact_team_game_stats WHERE game_id = ? AND team_id = ?)''', [*row, *row[:2]])
            c.execute('INSERT OR IGNORE INTO audit.team_box_receipt_publication VALUES (?,?,?,?)', [*row[:2], INDIANA_BOX_URL, digest])
        c.execute('''INSERT INTO audit.gap_attempt
            SELECT 'team_box', ?, 'newspaper_deseret', now(), 'admitted', ?
            WHERE NOT EXISTS (SELECT 1 FROM audit.gap_attempt WHERE domain='team_box' AND source='newspaper_deseret' AND game_id=?)''',
            [INDIANA_BOX, INDIANA_BOX_URL, INDIANA_BOX])
    print(f'Printed team boxes verified: {INDIANA_BOX}', flush=True)


def main() -> None:
    for gid, (_, _, _, _, url) in FIXTURES.items():
        target = CACHE / f'{gid}.html'
        if not target.is_file():
            with urlopen(Request(url, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=20) as response:
                raw = response.read(8_000_000)
            article(raw)
            cache_bytes(target, raw)
    for name, url in (('deseret-mexico.html', MEXICO_SCORE_URL), ('bender-mexico.html', MEXICO_WITNESS_URL),
                      ('deseret-indiana.html', INDIANA_BOX_URL)):
        target = CACHE / name
        if not target.is_file():
            with urlopen(Request(url, headers={'User-Agent': 'NBAWarehouse-Research/1.0'}), timeout=20) as response:
                cache_bytes(target, response.read(8_000_000))
    with warehouse_lock(), connect(DEFAULT_OUT) as c:
        load(c)
        c.execute('CHECKPOINT')


if __name__ == '__main__':
    main()
