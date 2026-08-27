#!/usr/bin/env bash
set -euo pipefail

SERVER="${SERVER:-root@43.167.220.225}"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/tecent.pem}"
BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:?set EXPECTED_COMMIT to the reviewed 40-character commit}"
EXPECTED_AUTO_TRADE_STATE="${EXPECTED_AUTO_TRADE_STATE:?set EXPECTED_AUTO_TRADE_STATE to enabled or disabled}"
UPDATER_TOPOLOGY_CONTRACT="dual-v1"
UPDATER_CACHE_ARTIFACT_CONTRACT="worker-cache-v1"
UPDATER_MONITOR_EXPECTATION_CONTRACT="monitor-expectation-v1"

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
if [[ ! "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]]; then
  echo "BRANCH contains unsupported characters." >&2
  exit 1
fi
if [[ "$EXPECTED_AUTO_TRADE_STATE" =~ ^(enabled|disabled)$ ]]; then
  :
else
  echo "EXPECTED_AUTO_TRADE_STATE must be enabled or disabled." >&2
  exit 1
fi
export SERVER KEY_PATH BRANCH EXPECTED_COMMIT EXPECTED_AUTO_TRADE_STATE UPDATER_TOPOLOGY_CONTRACT UPDATER_CACHE_ARTIFACT_CONTRACT UPDATER_MONITOR_EXPECTATION_CONTRACT
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/bootstrap_server_updater.sh"
