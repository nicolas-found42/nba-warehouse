#!/bin/zsh
# Install (or reinstall) the daily refresh as a per-user launchd agent.
#
# macOS privacy protection stops background jobs from reading ~/Documents, so the job runs from a deployed
# copy of the scripts under ~/Library/Application Support/NBA Warehouse/app, and the warehouse data lives in
# ~/Library/Application Support/NBA Warehouse/data (the repo's data/ is a symlink to it). Re-run after
# changing any warehouse script.
set -eu
repo="${0:A:h:h:h}"
base="$HOME/Library/Application Support/NBA Warehouse"
app="$base/app"
dest="$HOME/Library/LaunchAgents/com.nba-warehouse.daily-refresh.plist"
mkdir -p "$app/warehouse/launchd" "$base/data/refresh-reports"
rsync -a --delete --exclude __pycache__ --include '*/' --include '*.py' --include '*.sql' --include '*.csv' --include '*.sh' \
  --exclude '*' "$repo/warehouse/" "$app/warehouse/"
ln -sfn "$base/data" "$app/data"
hours=($(python3 -c "
from datetime import datetime; from zoneinfo import ZoneInfo
d=datetime.now(ZoneInfo('America/New_York')).replace(minute=0,second=0,microsecond=0)
print(*[(d.replace(hour=h)).astimezone().hour for h in (6,9,12)])"))
sed -e "s#__REPO__#$app#g" -e "s/__H1__/${hours[1]}/" -e "s/__H2__/${hours[2]}/" -e "s/__H3__/${hours[3]}/" \
  "$repo/warehouse/launchd/com.nba-warehouse.daily-refresh.plist.template" > "$dest"
launchctl bootout "gui/$(id -u)" "$dest" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$dest"
echo "installed $dest -> $app (local hours ${hours[*]})"
# nightly gap chasing at 01:00 local time
chase="$HOME/Library/LaunchAgents/com.nba-warehouse.chase-gaps.plist"
sed -e "s#__REPO__#$app#g" -e "s/__H1__/1/" "$repo/warehouse/launchd/com.nba-warehouse.chase-gaps.plist.template" > "$chase"
launchctl bootout "gui/$(id -u)" "$chase" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$chase"
echo "installed $chase (01:00 nightly)"
