#!/bin/zsh
set -eu

cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo "Run ./setup.sh first."
  exit 1
fi
.venv/bin/python sync_journalclub.py "$@"
