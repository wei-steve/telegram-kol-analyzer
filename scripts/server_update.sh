#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/telegram-kol-analyzer}"
BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"

cd "$APP_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

if [ ! -x .venv/bin/python ]; then
  python3.12 -m venv .venv
fi

. .venv/bin/activate
python -m pip install -e .

systemctl restart telegram-kol.service
systemctl --no-pager --full status telegram-kol.service | head -n 18
