"""Adversarial checks of the real current-season release and public replay path."""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import shutil
import tempfile

import duckdb

from nba_warehouse import ROOT, SCRATCH
from refresh_current_season import parse_periods, run


def rejected(action: str, args: argparse.Namespace, expect: str) -> None:
    args.action = action
    try:
        run(args)
    except (ValueError,FileExistsError) as exc:
        if expect not in str(exc):
            raise AssertionError(f'Wrong rejection: {exc}') from exc
        print(f'Rejected as expected: {exc}')
    else:
        raise AssertionError(f'Missing rejection: {expect}')


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--assets',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--history',type=Path,default=ROOT/'data/nba-clean-v22.duckdb')
    p.add_argument('--prior',type=Path,default=ROOT/'data/nba-2026-27-box-check-2026-11-19.duckdb')
    p.add_argument('--previous-release',type=Path)
    p.add_argument('--cutoff',type=date.fromisoformat,default=date(2026,11,23))
    p.add_argument('--season-end',type=int,default=2027)
    args = p.parse_args()
    artifact = args.output
    args.action = 'verify-inputs'
    run(args)
    rejected('build',args,str(artifact.absolute()))
    # Boundary checks use the provider's exact serialization and a same-total period swap.
    cells = "[{'value': 25.0, 'displayValue': '25', 'period': 1.0}\n {'value': 26.0, 'displayValue': '26', 'period': 2.0}\n {'value': 28.0, 'displayValue': '28', 'period': 3.0}\n {'value': 29.0, 'displayValue': '29', 'period': 4.0}]"
    assert parse_periods(cells,108,4)==[25,26,28,29]
    try:
        parse_periods(cells.replace("'period': 2.0","'period': 1.0"),108,4)
    except ValueError as exc:
        assert 'ordered cells' in str(exc)
    else:
        raise AssertionError('Duplicated period id passed')
    with tempfile.TemporaryDirectory(dir=SCRATCH,prefix='nba-season-test-') as tmp:
        root = Path(tmp)
        mutations = [
          ("UPDATE nba.fact_game_period_score SET points=points+CASE period_number WHEN 1 THEN 1 ELSE -1 END "
           "WHERE game_id='0022600289' AND team_id=1610612754 AND period_number IN (1,2)", 'nba.fact_game_period_score'),
          ("UPDATE nba.fact_player_game_stats SET reported_num_minutes=reported_num_minutes+1 WHERE (game_id,person_id) IN "
           "(SELECT game_id,person_id FROM nba.fact_player_game_stats WHERE reported_num_minutes>0 ORDER BY 1,2 LIMIT 1)", 'nba.fact_player_game_stats'),
          ("DELETE FROM audit.shot_coordinates WHERE (game_id,action_number) IN "
           "(SELECT game_id,action_number FROM audit.shot_coordinates ORDER BY 1,2 LIMIT 1)", 'audit.shot_coordinates'),
          ("UPDATE audit.game_source SET cup_group=NOT cup_group WHERE game_id IN ('0022600043','0022600289')", 'audit.game_source'),
        ]
        for i,(sql,relation) in enumerate(mutations):
            args.output = root/f'mutant-{i}.duckdb'
            shutil.copyfile(artifact,args.output)
            with duckdb.connect(str(args.output)) as connection:
                connection.execute(sql+'; CHECKPOINT')
            rejected('verify-inputs',args,'Exact checked replay failed: '+relation)
        args.output = root/'destination.duckdb'
        args.output.with_suffix('.duckdb.wal').touch()
        rejected('build',args,str(args.output))
    print('Current season acceptance checks passed on unchanged release')


if __name__=='__main__':
    main()
