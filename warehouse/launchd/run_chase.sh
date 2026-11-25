#!/bin/zsh
# Nightly gap chasing (resumable): period gaps and ABA box pages via Basketball-Reference, then finalize.
set -u
cd "${0:A:h}/../.."
mkdir -p data/crawl-logs
exec /opt/homebrew/bin/uv run -q --no-project --with duckdb==2.0.0 python warehouse/chase_gaps.py all
