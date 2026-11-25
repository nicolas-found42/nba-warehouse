"""Capture released SportsDataverse Parquet assets without calling their upstream APIs."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.request import Request, urlopen

from refresh_current_season import ASSETS, DOWNLOAD, input_assets

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
    inventories = {tag:json.loads(read(f'{API}/{tag}','application/vnd.github+json',10_000_000)) for tag in tags}
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.sdv-capture-') as work:
        stage = Path(work)
        for tag, stem in ASSETS.values():
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
            (stage/name).write_bytes(raw)
            (stage/(name+'.asset.json')).write_text(json.dumps(dict(name=name,url=url,sha256=digest,
                asset_id=row['id'],published_asset_updated_at=row['updated_at']),indent=2)+'\n')
        input_assets(stage,season_end)
        # A partial fetch never becomes an input set. No copy/replace of an existing receipt directory.
        output.mkdir(mode=0o700,exist_ok=False)
        try:
            for p in stage.iterdir():
                os.link(p,output/p.name)
        except BaseException:
            for p in stage.iterdir():
                (output/p.name).unlink(missing_ok=True)
            output.rmdir()
            raise
    print(f'{len(ASSETS)} publisher-digest-matched assets: {output}')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--season-end',type=int,default=2027)
    p.add_argument('--output',type=Path,required=True)
    a = p.parse_args()
    capture(a.season_end,a.output.absolute())


if __name__=='__main__':
    main()
