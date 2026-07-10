#!/usr/bin/env bash
set -euo pipefail

SERVER="${SERVER:-root@43.167.220.225}"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/tecent.pem}"
BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh is required but was not found in PATH." >&2
  exit 1
fi

if [ ! -r "$KEY_PATH" ]; then
  echo "SSH private key is not readable: $KEY_PATH" >&2
  exit 1
fi

remote="BRANCH=$(printf '%q' "$BRANCH") /usr/local/bin/telegram-kol-update"
exec ssh -i "$KEY_PATH" "$SERVER" "$remote"
