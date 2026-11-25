#!/bin/zsh
# Entry point for the scheduled daily refresh (see com.nba-warehouse.daily-refresh.plist).
# Exits quietly if the warehouse is already current; exit 75 = publisher data not ready yet.
set -u
cd "${0:A:h}/../.."
mkdir -p data/refresh-reports
exec /opt/homebrew/bin/uv run -q --no-project --with duckdb==2.0.0 python warehouse/daily_refresh.py "$@"
