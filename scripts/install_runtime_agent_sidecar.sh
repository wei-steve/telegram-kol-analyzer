#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "install_runtime_agent_sidecar.sh must run as root." >&2
  exit 1
fi
if [[ "$#" -ne 0 ]]; then
  echo "Usage: $0" >&2
  exit 2
fi

PRODUCTION_ROOT="/opt/telegram-kol-analyzer"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
UNIT_NAME="telegram-kol-runtime-agent.service"
UNIT_SOURCE="$PRODUCTION_ROOT/deploy/systemd/$UNIT_NAME"
UNIT_DEST="/etc/systemd/system/$UNIT_NAME"
ENV_FILE="$PRODUCTION_ROOT/config/runtime_incident_agent.env"
AGENT_USER="telegram-kol-agent"
AGENT_GROUP="telegram-kol-agent"
DATA_DIRECTORY="$PRODUCTION_ROOT/data"
DATABASE_PATH="$DATA_DIRECTORY/research.db"
MONITOR_STATE_DIRECTORY="/var/lib/telegram-kol-monitor"
MONITOR_STATE_PATH="$MONITOR_STATE_DIRECTORY/state.json"
PRIVATE_WORKSPACE="/var/lib/telegram-kol-runtime-agent"

if [[ "$PROJECT_ROOT" != "$PRODUCTION_ROOT" ]]; then
  echo "Run this installer only from $PRODUCTION_ROOT." >&2
  exit 1
fi
if [[ ! -f "$UNIT_SOURCE" || ! -x "$PRODUCTION_ROOT/.venv/bin/telegram-kol-research" ]]; then
  echo "Reviewed sidecar unit or installed CLI is missing." >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing runtime incident environment file $ENV_FILE." >&2
  exit 1
fi
if [[ "$(stat -c %u "$ENV_FILE")" != "0" || "$(stat -c %a "$ENV_FILE")" != "600" ]]; then
  echo "Runtime incident environment file must be root-owned mode 0600." >&2
  exit 1
fi
if ! command -v setfacl >/dev/null 2>&1; then
  echo "setfacl is required for least-privilege database access." >&2
  exit 1
fi
if [[ ! -d "$DATA_DIRECTORY" || ! -f "$DATABASE_PATH" ]]; then
  echo "Production data directory or database is missing." >&2
  exit 1
fi

active_status=0
systemctl is-active --quiet "$UNIT_NAME" || active_status=$?
case "$active_status" in
  0)
    echo "Stop $UNIT_NAME before installing or upgrading." >&2
    exit 1
    ;;
  3|4)
    ;;
  *)
    echo "Unable to prove $UNIT_NAME is inactive." >&2
    exit 1
    ;;
esac

enabled_status=0
systemctl is-enabled --quiet "$UNIT_NAME" || enabled_status=$?
case "$enabled_status" in
  0)
    echo "Disable $UNIT_NAME before installing or upgrading." >&2
    exit 1
    ;;
  1|3|4)
    ;;
  *)
    echo "Unable to prove $UNIT_NAME is disabled." >&2
    exit 1
    ;;
esac

# Preflight complete; mutations may begin.
if ! getent group "$AGENT_GROUP" >/dev/null; then
  groupadd --system "$AGENT_GROUP"
fi
if ! id "$AGENT_USER" >/dev/null 2>&1; then
  useradd --system \
    --gid "$AGENT_GROUP" \
    --home-dir /nonexistent \
    --shell /usr/sbin/nologin \
    "$AGENT_USER"
fi
if [[ "$(id -u "$AGENT_USER")" -eq 0 || "$(id -gn "$AGENT_USER")" != "$AGENT_GROUP" ]]; then
  echo "Existing Agent identity is not the dedicated unprivileged account." >&2
  exit 1
fi

setfacl -x "d:u:$AGENT_USER" "$DATA_DIRECTORY" 2>/dev/null || true
setfacl -m "d:g::---,d:o::---" "$DATA_DIRECTORY"
setfacl -m "u:$AGENT_USER:-wx" "$DATA_DIRECTORY"
chmod +t "$DATA_DIRECTORY"
setfacl -m "u:$AGENT_USER:rw-" "$DATABASE_PATH"
for sqlite_sidecar in \
  "$DATABASE_PATH-wal" \
  "$DATABASE_PATH-shm" \
  "$DATABASE_PATH-journal"
do
  if [[ -e "$sqlite_sidecar" ]]; then
    setfacl -m "u:$AGENT_USER:rw-" "$sqlite_sidecar"
  fi
done
while IFS= read -r -d '' data_file; do
  case "$data_file" in
    "$DATABASE_PATH"|"$DATABASE_PATH-wal"|"$DATABASE_PATH-shm"|"$DATABASE_PATH-journal")
      continue
      ;;
  esac
  setfacl -m "u:$AGENT_USER:---" "$data_file"
  if runuser -u "$AGENT_USER" -- test -r "$data_file" \
    || runuser -u "$AGENT_USER" -- test -w "$data_file"; then
    echo "Agent identity can access non-allowlisted production data." >&2
    exit 1
  fi
done < <(find "$DATA_DIRECTORY" -maxdepth 1 -type f -print0)
if ! runuser -u "$AGENT_USER" -- test -w "$DATA_DIRECTORY"; then
  echo "Agent identity cannot create SQLite sidecar files." >&2
  exit 1
fi
if runuser -u "$AGENT_USER" -- test -r "$DATA_DIRECTORY"; then
  echo "Agent identity can enumerate the production data directory." >&2
  exit 1
fi
if ! runuser -u "$AGENT_USER" -- test -w "$DATABASE_PATH"; then
  echo "Agent identity cannot update the runtime incident ledger." >&2
  exit 1
fi
if ! runuser -u "$AGENT_USER" -- test -x "$PRODUCTION_ROOT/.venv/bin/telegram-kol-research"; then
  echo "Agent identity cannot execute the installed CLI." >&2
  exit 1
fi
if runuser -u "$AGENT_USER" -- test -w "$PRODUCTION_ROOT/src"; then
  echo "Agent identity can write reviewed source." >&2
  exit 1
fi
if runuser -u "$AGENT_USER" -- test -w "$PRODUCTION_ROOT/config"; then
  echo "Agent identity can write reviewed configuration." >&2
  exit 1
fi
if [[ -d "$MONITOR_STATE_DIRECTORY" ]]; then
  setfacl -m "u:$AGENT_USER:--x" "$MONITOR_STATE_DIRECTORY"
fi
if [[ -f "$MONITOR_STATE_PATH" ]]; then
  setfacl -m "u:$AGENT_USER:r--" "$MONITOR_STATE_PATH"
fi

install -o root -g root -m 0644 "$UNIT_SOURCE" "$UNIT_DEST"
install -d -o "$AGENT_USER" -g "$AGENT_GROUP" -m 0700 "$PRIVATE_WORKSPACE"
systemctl daemon-reload

echo "Installed $UNIT_NAME in disabled, inactive state."
