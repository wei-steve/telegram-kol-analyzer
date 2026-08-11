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
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v ssh >/dev/null 2>&1 || { echo "ssh is required." >&2; exit 1; }
[ -r "$KEY_PATH" ] || { echo "SSH private key is not readable: $KEY_PATH" >&2; exit 1; }
[[ "$EXPECTED_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] || { echo "Invalid EXPECTED_COMMIT." >&2; exit 1; }
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "Invalid BRANCH." >&2; exit 1; }
case "$CHANGE_CLASS" in code|schema_compatible|execution_writer|live_promotion) ;; *) exit 1 ;; esac

EXPECTED_COMMIT="$(printf '%s' "$EXPECTED_COMMIT" | tr '[:upper:]' '[:lower:]')"
if command -v shasum >/dev/null 2>&1; then
  UPDATER_SHA256="$(shasum -a 256 "$ROOT/deploy/telegram-kol-update" | awk '{print $1}')"
else
UPDATER_SHA256="$(sha256sum "$ROOT/deploy/telegram-kol-update" | awk '{print $1}')"
fi
shadow_arg="${REVIEWED_SHADOW_EVIDENCE_PATH:-__EMPTY__}"
previous_arg="${PREVIOUS_LIVE_SNAPSHOT_PATH:-__EMPTY__}"
authorization_arg="${LIVE_PROMOTION_AUTHORIZATION:-__EMPTY__}"

# The server extracts the helper from the exact reviewed commit and verifies it
# against the reviewed workstation copy before installing or executing it.
exec ssh -i "$KEY_PATH" "$SERVER" bash -s -- \
  "$EXPECTED_COMMIT" "$BRANCH" "$UPDATER_SHA256" "$CHANGE_CLASS" \
  "$shadow_arg" "$previous_arg" "$authorization_arg" <<'REMOTE'
set -euo pipefail
expected_commit="$1"; branch="$2"; expected_sha="$3"; change_class="$4"
shadow_path="$5"; previous_snapshot="$6"; authorization="$7"
[ "$shadow_path" != "__EMPTY__" ] || shadow_path=""
[ "$previous_snapshot" != "__EMPTY__" ] || previous_snapshot=""
[ "$authorization" != "__EMPTY__" ] || authorization=""
app_dir="/opt/telegram-kol-analyzer"
temporary="$(mktemp /run/telegram-kol-update.bootstrap.XXXXXX)"
trap 'rm -f "$temporary"' EXIT
git -C "$app_dir" fetch origin "$branch"
[ "$(git -C "$app_dir" rev-parse FETCH_HEAD)" = "$expected_commit" ]
git -C "$app_dir" show "$expected_commit:deploy/telegram-kol-update" >"$temporary"
[ "$(sha256sum "$temporary" | awk '{print $1}')" = "$expected_sha" ]
install -o root -g root -m 0755 "$temporary" /usr/local/bin/telegram-kol-update
EXPECTED_COMMIT="$expected_commit" CHANGE_CLASS="$change_class" BRANCH="$branch" \
REVIEWED_SHADOW_EVIDENCE_PATH="$shadow_path" \
PREVIOUS_LIVE_SNAPSHOT_PATH="$previous_snapshot" \
LIVE_PROMOTION_AUTHORIZATION="$authorization" \
  /usr/local/bin/telegram-kol-update
REMOTE
