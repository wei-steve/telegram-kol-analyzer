#!/usr/bin/env bash
set -euo pipefail

SERVER="${SERVER:-root@43.167.220.225}"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/tecent.pem}"
BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:?set EXPECTED_COMMIT to the reviewed 40-character commit}"
UPDATER_TOPOLOGY_CONTRACT="${UPDATER_TOPOLOGY_CONTRACT:-dual-v1}"
UPDATER_CACHE_ARTIFACT_CONTRACT="${UPDATER_CACHE_ARTIFACT_CONTRACT:-worker-cache-v1}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v ssh >/dev/null 2>&1 || { echo "ssh is required." >&2; exit 1; }
[ -r "$KEY_PATH" ] || { echo "SSH private key is not readable: $KEY_PATH" >&2; exit 1; }
[[ "$EXPECTED_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] || { echo "Invalid EXPECTED_COMMIT." >&2; exit 1; }
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "Invalid BRANCH." >&2; exit 1; }
[ "$UPDATER_TOPOLOGY_CONTRACT" = "dual-v1" ] || { echo "Invalid updater topology contract." >&2; exit 1; }
[ "$UPDATER_CACHE_ARTIFACT_CONTRACT" = "worker-cache-v1" ] || { echo "Invalid updater cache artifact contract." >&2; exit 1; }

EXPECTED_COMMIT="$(printf '%s' "$EXPECTED_COMMIT" | tr '[:upper:]' '[:lower:]')"
if command -v shasum >/dev/null 2>&1; then
  UPDATER_SHA256="$(shasum -a 256 "$ROOT/deploy/telegram-kol-update" | awk '{print $1}')"
else
  UPDATER_SHA256="$(sha256sum "$ROOT/deploy/telegram-kol-update" | awk '{print $1}')"
fi

exec ssh -i "$KEY_PATH" "$SERVER" bash -s -- \
  "$EXPECTED_COMMIT" "$BRANCH" "$UPDATER_SHA256" "$UPDATER_TOPOLOGY_CONTRACT" "$UPDATER_CACHE_ARTIFACT_CONTRACT" <<'REMOTE'
set -euo pipefail
expected_commit="$1"; branch="$2"; expected_sha="$3"; topology_contract="$4"; cache_artifact_contract="$5"
app_dir="/opt/telegram-kol-analyzer"
temporary="$(mktemp -d /run/telegram-kol-update.bootstrap.XXXXXX)"
chmod 0700 "$temporary"
trap 'rm -rf -- "$temporary"' EXIT
git -C "$app_dir" fetch origin "$branch"
[ "$(git -C "$app_dir" rev-parse FETCH_HEAD)" = "$expected_commit" ]
git -C "$app_dir" show "$expected_commit:deploy/telegram-kol-update" >"$temporary/updater"
chmod 0700 "$temporary/updater"
[ "$(sha256sum "$temporary/updater" | awk '{print $1}')" = "$expected_sha" ]
[ "$topology_contract" = "dual-v1" ]
[ "$cache_artifact_contract" = "worker-cache-v1" ]
grep -Fq 'resolve_managed_topology()' "$temporary/updater"
grep -Fq 'install_worker_cache_artifacts' "$temporary/updater"
grep -Fq 'telegram-kol-worker-prepare-contract-cache' "$temporary/updater"
grep -Fq 'telegram-kol-ingest.service' "$temporary/updater"
grep -Fq 'telegram-kol-worker.service' "$temporary/updater"
grep -Fq 'telegram-kol-web.service' "$temporary/updater"
EXPECTED_COMMIT="$expected_commit" BRANCH="$branch" bash "$temporary/updater"
REMOTE
