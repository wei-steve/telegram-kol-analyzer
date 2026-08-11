#!/usr/bin/env bash
set -euo pipefail

SERVER="${SERVER:-root@43.167.220.225}"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/tecent.pem}"
BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:?set EXPECTED_COMMIT to the reviewed 40-character commit}"
CHANGE_CLASS="${CHANGE_CLASS:?set CHANGE_CLASS to code, schema_compatible, execution_writer, or live_promotion}"
REVIEWED_SHADOW_EVIDENCE_PATH="${REVIEWED_SHADOW_EVIDENCE_PATH:-}"
PREVIOUS_LIVE_SNAPSHOT_PATH="${PREVIOUS_LIVE_SNAPSHOT_PATH:-}"
LIVE_PROMOTION_AUTHORIZATION="${LIVE_PROMOTION_AUTHORIZATION:-}"

if ! command -v ssh >/dev/null 2>&1; then
  echo "ssh is required but was not found in PATH." >&2
  exit 1
fi

if [ ! -r "$KEY_PATH" ]; then
  echo "SSH private key is not readable: $KEY_PATH" >&2
  exit 1
fi

if [[ ! "$EXPECTED_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
  echo "EXPECTED_COMMIT must be a full 40-character hexadecimal commit." >&2
  exit 1
fi
case "$CHANGE_CLASS" in
  code|schema_compatible|execution_writer|live_promotion) ;;
  *)
    echo "Unsupported CHANGE_CLASS: $CHANGE_CLASS" >&2
    exit 1
    ;;
esac
if [[ ! "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]]; then
  echo "BRANCH contains unsupported characters." >&2
  exit 1
fi
export SERVER KEY_PATH BRANCH EXPECTED_COMMIT CHANGE_CLASS
export REVIEWED_SHADOW_EVIDENCE_PATH PREVIOUS_LIVE_SNAPSHOT_PATH
export LIVE_PROMOTION_AUTHORIZATION
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bootstrap_server_updater.sh"
