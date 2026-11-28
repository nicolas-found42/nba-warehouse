#!/bin/zsh
# Regenerate the progress page (data/status/index.html). Scheduled every 5 minutes.
set -u
cd "${0:A:h}/../.."
exec /opt/homebrew/bin/python3 warehouse/launch_job.py status_page
