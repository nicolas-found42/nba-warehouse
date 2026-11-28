#!/bin/zsh
# Nightly gap chasing (resumable): period gaps and ABA box pages via Basketball-Reference, then finalize.
set -u
cd "${0:A:h}/../.."
mkdir -p data/crawl-logs
exec /opt/homebrew/bin/python3 warehouse/launch_job.py chase_gaps
