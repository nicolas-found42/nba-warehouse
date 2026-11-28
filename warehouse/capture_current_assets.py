"""Capture released SportsDataverse Parquet assets without calling their upstream APIs."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.request import Request, urlopen

from observability import operation, stage
from refresh_current_season import ASSETS, DOWNLOAD, FIXTURE_SCHEDULE, input_assets

API = 'https://api.github.com/repos/sportsdataverse/sportsdataverse-data/releases/tags'
LIMIT = 100_000_000


def read(url: str, accept: str, limit: int) -> bytes:
    with urlopen(Request(url,headers={'User-Agent':'nba-warehouse-local-release-check','Accept':accept}),timeout=30) as r:
        raw: bytes = r.read(limit+1)
    if len(raw)>limit:
        raise ValueError(f'Response limit exceeded: {url}')
    return raw


def capture(season_end: int, output: Path) -> None:
    if os.path.lexists(output):
        raise FileExistsError(output)
    if not 2000<=season_end<=2100:
        raise ValueError('Season ending year outside supported naming convention')
    tags = dict.fromkeys(tag for tag,_ in ASSETS.values())
    inventories = {}
    for tag in tags:
        with stage('publisher_inventory', dependency='github_api', role=tag):
            inventories[tag] = json.loads(read(f'{API}/{tag}', 'application/vnd.github+json', 10_000_000))
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.sdv-capture-') as work:
        staged = Path(work)
        for role, (tag, stem) in ASSETS.items():
            with stage('publisher_asset', dependency='github_release', role=role) as step:
                name = f'{stem}_{season_end}.parquet'
                url = f'{DOWNLOAD}/{tag}/{name}'
                match = [x for x in inventories[tag]['assets'] if x['name']==name and x['browser_download_url']==url]
                if len(match)!=1:
                    raise ValueError(f'Expected exactly one published asset: {name}')
                row = match[0]
                pin = row.get('digest')
                size = row['size']
                if not isinstance(pin,str) or not re.fullmatch('sha256:[0-9a-f]{64}',pin) or type(size) is not int or not 8<=size<=LIMIT:
                    raise ValueError(f'No verifiable publisher digest or size: {name}')
                raw = read(url,'application/octet-stream',size)
                digest = sha256(raw).hexdigest()
                if raw[:4]!=b'PAR1' or raw[-4:]!=b'PAR1' or len(raw)!=size or pin!='sha256:'+digest:
                    raise ValueError(f'Asset changed during capture, or is not Parquet: {name}')
                (staged/name).write_bytes(raw)
                (staged/(name+'.asset.json')).write_text(json.dumps(dict(name=name,url=url,sha256=digest,
                    asset_id=row['id'],published_asset_updated_at=row['updated_at']),indent=2)+'\n')
                step.result['sha256'] = digest
        input_assets(staged,season_end)
        # A partial fetch never becomes an input set. No copy/replace of an existing receipt directory.
        output.mkdir(mode=0o700,exist_ok=False)
        try:
            for p in staged.iterdir():
                os.link(p,output/p.name)
        except BaseException:
            for p in staged.iterdir():
                (output/p.name).unlink(missing_ok=True)
            output.rmdir()
            raise
    print(f'{len(ASSETS)} publisher-digest-matched assets: {output}')


def append_fixture_schedule(bundle: Path, schedule: Path, url: str) -> None:
    """Retain the exact NBA schedule mirror already read by the pregame job."""
    target = bundle / FIXTURE_SCHEDULE
    captured_at = datetime.fromtimestamp(schedule.stat().st_mtime, timezone.utc).isoformat()
    raw = schedule.read_bytes()
    receipt = dict(name=target.name, url=url, sha256=sha256(raw).hexdigest(), captured_at=captured_at)
    metadata = target.with_suffix('.parquet.asset.json')
    with tempfile.TemporaryDirectory(dir=bundle, prefix='.fixture-schedule-') as work:
        source = Path(work) / target.name
        source.write_bytes(raw)
        source.with_suffix('.parquet.asset.json').write_text(json.dumps(receipt) + '\n')
        os.link(source, target)
        try:
            os.link(source.with_suffix('.parquet.asset.json'), metadata)
        except BaseException:
            target.unlink()
            raise


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--season-end',type=int,default=2027)
    p.add_argument('--output',type=Path,required=True)
    a = p.parse_args()
    with operation('asset_capture', a.output, 'publisher_capture', asset_set=a.output.name) as span:
        capture(a.season_end, a.output.absolute())
        span.result['assets'] = len(ASSETS)


if __name__=='__main__':
    main()
