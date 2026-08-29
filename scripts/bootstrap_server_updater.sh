#!/usr/bin/env bash
set -euo pipefail

ACTION="${DEPLOYMENT_ACTION:-}"
case "$ACTION" in
  stage|activate) ;;
  *) echo "DEPLOYMENT_ACTION must be stage or activate." >&2; exit 2 ;;
esac

SERVER="${SERVER:-root@43.167.220.225}"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/tecent.pem}"
BRANCH="${BRANCH:-codex/deepcoin-auto-trading-v1}"
EXPECTED_COMMIT="${EXPECTED_COMMIT:?set EXPECTED_COMMIT to the reviewed 40-character commit}"
ACTION_MANIFEST="${ACTION_MANIFEST:?set ACTION_MANIFEST to the local action manifest}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

command -v ssh >/dev/null 2>&1 || { echo "ssh is required." >&2; exit 2; }
[ -r "$KEY_PATH" ] || { echo "SSH private key is not readable: $KEY_PATH" >&2; exit 2; }
if [[ ! "$SERVER" =~ ^([A-Za-z_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*$ ]] \
  || [[ "$SERVER" == *..* ]] || [[ "$SERVER" == *.-* ]] || [[ "$SERVER" == *-. ]]; then
  echo "Invalid SERVER." >&2
  exit 2
fi
[[ "$EXPECTED_COMMIT" =~ ^[0-9a-fA-F]{40}$ ]] || { echo "Invalid EXPECTED_COMMIT." >&2; exit 2; }
[[ "$BRANCH" =~ ^[A-Za-z0-9._/-]+$ ]] && [[ "$BRANCH" != *..* ]] || { echo "Invalid BRANCH." >&2; exit 2; }
[ -f "$ACTION_MANIFEST" ] || { echo "ACTION_MANIFEST is unavailable." >&2; exit 2; }
[ "$(wc -c <"$ACTION_MANIFEST" | tr -d ' ')" -le 65536 ] || { echo "ACTION_MANIFEST is too large." >&2; exit 2; }
EXPECTED_COMMIT="$(printf '%s' "$EXPECTED_COMMIT" | tr '[:upper:]' '[:lower:]')"

safe_remote_path() {
  [[ "$1" =~ ^/[A-Za-z0-9._/-]+$ ]] && [[ "$1" != *..* ]] && [[ "$1" != *//* ]]
}

SOURCE_REPO="${SOURCE_REPO:-/opt/telegram-kol-analyzer}"
RELEASE_ROOT="${RELEASE_ROOT:-/opt/telegram-kol-releases}"
SERVICE_DROPIN_ROOT="${SERVICE_DROPIN_ROOT:-/etc/systemd/system}"
DATABASE_PATH="${DATABASE_PATH:-/opt/telegram-kol-analyzer/data/research.db}"
for remote_path in "$SOURCE_REPO" "$RELEASE_ROOT" "$SERVICE_DROPIN_ROOT" "$DATABASE_PATH"; do
  safe_remote_path "$remote_path" || { echo "Unsafe remote path." >&2; exit 2; }
done

ROLLBACK_COMMIT="${ROLLBACK_COMMIT:-}"
ACTIVATION_AUTHORIZATION="${ACTIVATION_AUTHORIZATION:-}"
ACTIVATION_AUTHORIZATION_CONSUMED="${ACTIVATION_AUTHORIZATION_CONSUMED:-}"
ACTIVATION_SOURCE_MODE="${ACTIVATION_SOURCE_MODE:-immutable}"
if [ "$ACTION" = "activate" ]; then
  [[ "$ROLLBACK_COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid ROLLBACK_COMMIT." >&2; exit 2; }
  safe_remote_path "$ACTIVATION_AUTHORIZATION" || { echo "Invalid ACTIVATION_AUTHORIZATION." >&2; exit 2; }
  safe_remote_path "$ACTIVATION_AUTHORIZATION_CONSUMED" || { echo "Invalid ACTIVATION_AUTHORIZATION_CONSUMED." >&2; exit 2; }
  [[ "$ACTIVATION_SOURCE_MODE" == "immutable" || "$ACTIVATION_SOURCE_MODE" == "stopped_legacy" ]] \
    || { echo "Invalid ACTIVATION_SOURCE_MODE." >&2; exit 2; }
fi

if command -v shasum >/dev/null 2>&1; then
  MANIFEST_SHA256="$(shasum -a 256 "$ACTION_MANIFEST" | awk '{print $1}')"
else
  MANIFEST_SHA256="$(sha256sum "$ACTION_MANIFEST" | awk '{print $1}')"
fi
MANIFEST_BASE64="$(base64 <"$ACTION_MANIFEST" | tr -d '\r\n')"
BUNDLE_BASE64="-"
BUNDLE_SHA256="-"
temporary=""
cleanup() {
  if [ -n "$temporary" ] && [ -d "$temporary" ]; then
    rm -rf -- "$temporary"
  fi
}
trap cleanup EXIT

if [ "$ACTION" = "stage" ]; then
  temporary="$(mktemp -d "${TMPDIR:-/tmp}/telegram-kol-stage-bootstrap.XXXXXX")"
  bundle="$temporary/stager.tar"
  git -C "$ROOT" cat-file -e "$EXPECTED_COMMIT^{commit}"
  git -C "$ROOT" archive --format=tar --output="$bundle" "$EXPECTED_COMMIT" \
    deploy/telegram-kol-stage \
    src/telegram_kol_research/__init__.py \
    src/telegram_kol_research/deployment_action_plan.py
  if command -v shasum >/dev/null 2>&1; then
    BUNDLE_SHA256="$(shasum -a 256 "$bundle" | awk '{print $1}')"
  else
    BUNDLE_SHA256="$(sha256sum "$bundle" | awk '{print $1}')"
  fi
  BUNDLE_BASE64="$(base64 <"$bundle" | tr -d '\r\n')"
fi

REMOTE_SCRIPT=""
read -r -d '' REMOTE_SCRIPT <<'REMOTE' || true
set -euo pipefail
action="$1"; expected_commit="$2"; branch="$3"
manifest_sha256="$4"; bundle_sha256="$5"
rollback_commit="$6"; authorization="$7"; authorization_consumed="$8"
source_repo="$9"; release_root="${10}"; service_dropin_root="${11}"; database_path="${12}"
source_mode="${13}"
IFS= read -r manifest_base64
IFS= read -r bundle_base64
manifest_base64="${manifest_base64%$'\r'}"
bundle_base64="${bundle_base64%$'\r'}"
temporary="$(mktemp -d /run/telegram-kol-action.XXXXXX)"
chmod 0700 "$temporary"
trap 'rm -rf -- "$temporary"' EXIT
printf '%s' "$manifest_base64" | base64 -d >"$temporary/action-manifest.json"
[ "$(sha256sum "$temporary/action-manifest.json" | awk '{print $1}')" = "$manifest_sha256" ]

case "$action" in
  stage)
    printf '%s' "$bundle_base64" | base64 -d >"$temporary/stager.tar"
    [ "$(sha256sum "$temporary/stager.tar" | awk '{print $1}')" = "$bundle_sha256" ]
    mkdir "$temporary/control"
    tar -xf "$temporary/stager.tar" -C "$temporary/control"
    PYTHONPATH="$temporary/control/src" \
      EXPECTED_COMMIT="$expected_commit" \
      BRANCH="$branch" \
      ACTION_MANIFEST="$temporary/action-manifest.json" \
      SOURCE_REPO="$source_repo" \
      RELEASE_ROOT="$release_root" \
      /opt/telegram-kol-analyzer/.venv/bin/python \
      "$temporary/control/deploy/telegram-kol-stage"
    ;;
  activate)
    updater="$release_root/$rollback_commit/deploy/telegram-kol-update"
    [ -x "$updater" ] || { echo "Immutable activation dispatcher is unavailable." >&2; exit 4; }
    DEPLOYMENT_ACTION=activate \
      EXPECTED_COMMIT="$expected_commit" \
      ROLLBACK_COMMIT="$rollback_commit" \
      ACTION_MANIFEST="$temporary/action-manifest.json" \
      ACTIVATION_AUTHORIZATION="$authorization" \
      ACTIVATION_AUTHORIZATION_CONSUMED="$authorization_consumed" \
      ACTIVATION_SOURCE_MODE="$source_mode" \
      RELEASE_ROOT="$release_root" \
      SERVICE_DROPIN_ROOT="$service_dropin_root" \
      DATABASE_PATH="$database_path" \
      "$updater"
    ;;
  *) echo "Remote action must be stage or activate." >&2; exit 2 ;;
esac
REMOTE
REMOTE_SCRIPT_BASE64="$(printf '%s' "$REMOTE_SCRIPT" | base64 | tr -d '\r\n')"
REMOTE_COMMAND="bash -c \"\$(printf '%s' '$REMOTE_SCRIPT_BASE64' | base64 -d)\" -- \
'$ACTION' '$EXPECTED_COMMIT' '$BRANCH' '$MANIFEST_SHA256' '$BUNDLE_SHA256' \
'$ROLLBACK_COMMIT' '$ACTIVATION_AUTHORIZATION' '$ACTIVATION_AUTHORIZATION_CONSUMED' \
'$SOURCE_REPO' '$RELEASE_ROOT' '$SERVICE_DROPIN_ROOT' '$DATABASE_PATH' \
'$ACTIVATION_SOURCE_MODE'"
printf '%s\n%s\n' "$MANIFEST_BASE64" "$BUNDLE_BASE64" \
  | ssh -i "$KEY_PATH" "$SERVER" "$REMOTE_COMMAND"
