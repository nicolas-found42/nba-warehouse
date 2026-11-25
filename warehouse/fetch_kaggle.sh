#!/bin/zsh
# Download every NBA/ABA/BAA Kaggle dataset listed in data/kaggle/_catalog.csv into data/kaggle/<owner>__<slug>/.
# Needs KAGGLE_USERNAME and KAGGLE_KEY in the environment. Already-downloaded datasets (.done marker) are skipped.
# The catalog came from `kaggle datasets list -s <term> --sort-by votes` over ~30 search terms, filtered to
# NBA/ABA/BAA titles; college, WNBA, international, fantasy and image/vision datasets were excluded.
set -eu
cd "${0:A:h}/.."
mkdir -p data/kaggle
fetch() {
  local ref="$1" d="data/kaggle/${1/\//__}"
  [ -f "$d/.done" ] && return 0
  mkdir -p "$d"
  for i in 1 2 3; do
    if uvx -q --python 3.12 --with six kaggle datasets download -d "$ref" -p "$d" --unzip -q >"$d/.log" 2>&1; then
      date -u +%FT%TZ > "$d/.done"; return 0
    fi
    sleep 10
  done
  echo "FAILED $ref" >> data/kaggle/_failed.txt
}
python3 - <<'PY' | while read -r ref; do fetch "$ref"; done
import csv, re
bad = re.compile(r'shot-class|topshot|detection|yolo|pose|segment|frames|court-detect|ball-track', re.I)
for r in csv.DictReader(open('warehouse/kaggle_catalog.csv')):
    if not bad.search(r['ref'] + r['title']) and int(r['size']) < 5e9:
        print(r['ref'])
PY
