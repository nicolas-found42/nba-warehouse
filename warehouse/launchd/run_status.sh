#!/bin/zsh
# Regenerate the progress page (data/status/index.html). Scheduled every 5 minutes.
set -u
cd "${0:A:h}/../.."
exec /opt/homebrew/bin/uv run -q --no-project --with duckdb==2.0.0 python warehouse/status_page.py
