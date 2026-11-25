import json, glob, sys
sys.path.insert(0, '.')
from jev_client import Jev
S = sys.argv[1]
CLASSES = [
 {'id': 'same_person', 'description': 'The Basketball-Reference player and the NBA.com identity are the same human being. Use when the evidence is consistent and at least one specific detail corroborates it beyond the shared name: the same season(s) of activity (BRef transactions or career years overlap the NBA box-score/inactive/draft years), the same draft year/team, or matching birth dates. Example: BRef signed by Utah in 2024 and NBA.com has 4 games in 2024-25.'},
 {'id': 'different_person', 'description': 'Two different people who share a name. Use when the evidence conflicts: different birth dates, or careers separated by many years with no overlap (e.g. BRef active in the 1960s ABA while the NBA.com identity was drafted or played in the 2000s). Takes precedence over same_person whenever any concrete detail contradicts.'},
 {'id': 'manual_review', 'description': 'Too little evidence either way: the shared name is the only link and there are no dates, seasons, teams or draft details on one side to corroborate or contradict.'},
]
out = []
with Jev() as j:
    for f in sorted(glob.glob(f'{S}/idbatch_*.json')):
        items = json.load(open(f))
        r = j.call('jev_classify', {'items': items, 'classes': CLASSES,
            'purpose': 'Decide whether a Basketball-Reference player id and an NBA.com person id with the same name refer to the same person, to link them in a sports warehouse without creating duplicate or merged identities.',
            'auto_accept': 0.85, 'minimum_margin': 0.5})
        for x in r['results']:
            out.append({'id': x['id'], 'class': x['classification'], 'decision': x['decision'], 'top': x.get('top_probability'), 'probabilities': x.get('probabilities')})
        print(f, r['summary'], flush=True)
json.dump(out, open(f'{S}/jev_id_results.json', 'w'))
