#!/bin/zsh
set -eu

cd "${0:A:h}"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/playwright install chromium

echo "Setup complete. Run: ./sync.sh"
