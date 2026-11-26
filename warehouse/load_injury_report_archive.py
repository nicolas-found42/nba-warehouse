"""Keep independently published injury report transcripts and compare original PDF coverage."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from build_warehouse import DEFAULT_OUT, connect, warehouse_lock

RELEASE_URL = ('https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
               'nba_stats_injury_report')
ARCHIVE_SEASONS = {
    'adb988a85b4592f456574c57e472aceb6c8e0461eea4b9f6e34b6476d04f9209': 2019,
    'f5c08c6fe71f914433e8633de9da60a86add1cd99e4eace62c0397d85d5e42bc': 2020,
    '3a0d8b546ee36231e4717c0f01b49f56d3ffe982525ac0c26943c27cca091770': 2021,
    '9788575c97862f931a55e6582c0e6b2a5c0e0b49af2d34b4d364e73992485dc4': 2022,
    'a11e69bbd37ae8e9f77cd5b39b52177e6c0a46a9813558d5fe1b3f01f1516dff': 2023,
    '884da7cfacefaafa8be7e54f98fffa80b11b9dcc328a9a822fb3450f6534539c': 2024,
    'b48be65101c922683a745f6b81c87f019c9b6c990fa41dfc24863492037d1cf8': 2025,
    'c81116b17c0059c70d3592a83b8869b34aa5e56c9a5072628ab31c828ab501fa': 2026,
    '4164ea57a2ace2f5f414d01f4e1caad5e76cf99a75da1bb56a71c7e71de98c9e': 2027,
}


def publish(c, paths: list[Path]) -> None:
    if not paths:
        raise ValueError('provide at least one SportsDataverse release asset')
    sources = []
    for path in paths:
        with path.open('rb') as archive:
            digest = hashlib.file_digest(archive, 'sha256').hexdigest()
        season = ARCHIVE_SEASONS.get(digest)
        if season is None:
            raise ValueError(f'{path.name} differs from every verified SportsDataverse release asset')
        sources.append((f'{RELEASE_URL}/injury_report_{season}.parquet', digest, str(path)))

    source_rows = '''SELECT ?::VARCHAR AS source_url, ?::VARCHAR AS source_sha256,
        file_row_number AS source_row_number, * EXCLUDE (file_row_number)
        FROM read_parquet(?, file_row_number = true)'''
    c.execute('BEGIN')
    try:
        c.execute(f'''CREATE TABLE IF NOT EXISTS raw.sportsdataverse_nba_injury_archive AS
            {source_rows} LIMIT 0''', sources[0])
        for url, digest, path in sources:
            c.execute(f'''INSERT INTO raw.sportsdataverse_nba_injury_archive BY NAME
                {source_rows}
                WHERE NOT EXISTS (SELECT 1 FROM raw.sportsdataverse_nba_injury_archive WHERE source_sha256 = ?)''',
                [url, digest, path, digest])
        c.execute(Path(__file__).with_suffix('.sql').read_text())
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK')
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', nargs='+', type=Path, help='local copies of verified SportsDataverse Parquet release assets')
    parser.add_argument('--warehouse', type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    with warehouse_lock(), connect(args.warehouse) as c:
        publish(c, args.archive)
        for status, stamps, rows in c.execute('''SELECT original_pdf_status, count(DISTINCT report_datetime), count(*)
                FROM audit.injury_report_archive_copy GROUP BY ALL ORDER BY 1''').fetchall():
            print(f'{status}: {stamps} report times and {rows} source rows')


if __name__ == '__main__':
    main()
