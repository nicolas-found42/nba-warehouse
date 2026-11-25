"""Fetch one serial batch of team-season wikitext; collect evidence, not finals."""
import argparse
import gzip
import json
from pathlib import Path
import urllib.parse
import urllib.request

TEAMS = {
    'Atlanta Hawks':1610612737, 'Boston Celtics':1610612738, 'Cleveland Cavaliers':1610612739,
    'New Orleans Pelicans':1610612740, 'Chicago Bulls':1610612741, 'Dallas Mavericks':1610612742,
    'Denver Nuggets':1610612743, 'Golden State Warriors':1610612744, 'Houston Rockets':1610612745,
    'Los Angeles Clippers':1610612746, 'Los Angeles Lakers':1610612747, 'Miami Heat':1610612748,
    'Milwaukee Bucks':1610612749, 'Minnesota Timberwolves':1610612750, 'Brooklyn Nets':1610612751,
    'New York Knicks':1610612752, 'Orlando Magic':1610612753, 'Indiana Pacers':1610612754,
    'Philadelphia 76ers':1610612755, 'Phoenix Suns':1610612756, 'Portland Trail Blazers':1610612757,
    'Sacramento Kings':1610612758, 'San Antonio Spurs':1610612759, 'Oklahoma City Thunder':1610612760,
    'Toronto Raptors':1610612761, 'Utah Jazz':1610612762, 'Memphis Grizzlies':1610612763,
    'Washington Wizards':1610612764, 'Detroit Pistons':1610612765, 'Charlotte Hornets':1610612766,
}
API = 'https://en.wikipedia.org/w/api.php'


def fetch(output: Path, user_agent: str) -> None:
    q = {'action': 'query', 'format': 'json', 'formatversion': '2', 'prop': 'revisions',
         'rvprop': 'ids|timestamp|content', 'rvslots': 'main', 'redirects': '1', 'maxlag':'5',
         'titles': '|'.join(f'2026–27 {name} season' for name in TEAMS)}
    request = urllib.request.Request(f'{API}?{urllib.parse.urlencode(q)}',
                                     headers={'User-Agent': user_agent, 'Accept-Encoding': 'gzip'})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        if response.headers.get('Content-Encoding') == 'gzip':
            raw = gzip.decompress(raw)
    result = json.loads(raw)
    if 'error' in result or 'continue' in result:
        raise RuntimeError('Incomplete MediaWiki batch (stopped rather than retrying): ' + str(result))
    pages = result['query']['pages']
    if len(pages)!=len(TEAMS) or any(p.get('missing') or not p.get('revisions') for p in pages):
        raise RuntimeError('Missing team pages: ' + str([(p['title'],p.get('missing')) for p in pages]))
    with output.open('xb') as f:
        f.write(raw)
    print(f'{len(pages)} revisions pinned in {output}')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--user-agent',required=True)
    args=parser.parse_args()
    fetch(args.output,args.user_agent)
