from load_wayback_nba import box_players, legacy_line_score, pbp_rows, score_for_tag


def test_archive_score_tags_follow_the_printed_team_through_overtime():
    page = '''
      <tr class="pbpRowO"><td>Start of 4th Quarter</td></tr>
      <tr class=pbpRowE><td>&nbsp;</td><td>0:00 [NOH 84-84]</td><td>Player Free Throw</td></tr>
      <tr class="pbpRowO"><td>Start of Overtime</td></tr>
      <tr class="pbpRowE"><td>Player Jump Shot: Made</td><td>4:59 [NJN 86–84]</td><td>&nbsp;</td></tr>
      <tr class="pbpRowO"><td>&nbsp;</td><td>0:01 [NOP 87-86]</td><td>Player 3pt Shot: Made</td></tr>
    '''
    rows = pbp_rows(page)
    assert [(period, clock, side) for period, clock, _, _, _, side, _ in rows] == [
        (4, '0:00', 'home'), (5, '4:59', 'away'), (5, '0:01', 'home'),
    ]
    assert [score_for_tag(code, x, y, 'BKN', 'NOH') for _, _, code, x, y, _, _ in rows] == [
        (84, 84), (84, 86), (87, 86),
    ]
    assert score_for_tag('OTHER', '87', '86', 'BKN', 'NOH') is None


def test_legacy_box_parse_keeps_played_overtime_and_rejects_missing_cells():
    page = '''
      <table class="gSGTablenull"><tr><td><a class=gSGPlayerLink href=/playerfile/away_player/>Away Player</td><td>G</td></tr></table>
      <table class="gSGTablenull"><tr><td><a class=gSGPlayerLink href=/playerfile/home_player/>Home Player</td><td>F</td></tr></table>
      <table><tr><td><div>SCORE BY PERIOD</div></td></tr>
      <tr><td class=gSGTeamLink>Away</td><td>20</td><td>21</td><td>16</td><td>20</td><td>0</td><td>0</td><td>0</td><td>0</td><td>77</td></tr>
      <tr><td class=gSGTeamLink>Home</td><td>18</td><td>23</td><td>15</td><td>21</td><td>5</td><td>0</td><td>0</td><td>0</td><td>82</td></tr></table>
    '''
    assert [(p['side'], p['slug'], p['pos']) for p in box_players(page)] == [
        ('away', 'away_player', 'G'), ('home', 'home_player', 'F'),
    ]
    assert legacy_line_score(page) == [[20, 21, 16, 20, 0, 77], [18, 23, 15, 21, 5, 82]]
    assert legacy_line_score(page.replace('<td>16</td>', '<td></td>')) == []
