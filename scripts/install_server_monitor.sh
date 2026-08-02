#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "install_server_monitor.sh must run as root." >&2
  exit 1
fi

enable_timer=false
case "$#:$*" in
  "0:")
    ;;
  "1:--enable")
    enable_timer=true
    ;;
  *)
    echo "Usage: $0 [--enable]" >&2
    exit 2
    ;;
esac

PRODUCTION_ROOT="/opt/telegram-kol-analyzer"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
MONITOR_USER="telegram-kol-monitor"
MONITOR_GROUP="telegram-kol-monitor"
SERVICE_SOURCE="$PRODUCTION_ROOT/deploy/systemd/telegram-kol-monitor.service"
TIMER_SOURCE="$PRODUCTION_ROOT/deploy/systemd/telegram-kol-monitor.timer"
TEST_NOTIFICATION_SOURCE="$PRODUCTION_ROOT/deploy/systemd/telegram-kol-monitor-test-notification.service"
DIAGNOSTIC_SOURCE="$PRODUCTION_ROOT/deploy/systemd/telegram-kol-monitor-diagnostic.service"
SERVICE_DEST="/etc/systemd/system/telegram-kol-monitor.service"
TIMER_DEST="/etc/systemd/system/telegram-kol-monitor.timer"
TEST_NOTIFICATION_DEST="/etc/systemd/system/telegram-kol-monitor-test-notification.service"
DIAGNOSTIC_DEST="/etc/systemd/system/telegram-kol-monitor-diagnostic.service"
CREDENTIAL_FILE="/etc/telegram-kol-monitor.credentials"
ENV_FILE="/etc/telegram-kol-monitor.env"
STATE_DIRECTORY="/var/lib/telegram-kol-monitor"
STATE_FILE="$STATE_DIRECTORY/state.json"

if [[ "$PROJECT_ROOT" != "$PRODUCTION_ROOT" ]]; then
  echo "Run this installer only from the fixed production checkout $PRODUCTION_ROOT." >&2
  exit 1
fi
git_root="$(git -C "$PRODUCTION_ROOT" rev-parse --show-toplevel)"
if [[ "$(cd "$git_root" && pwd -P)" != "$PRODUCTION_ROOT" ]]; then
  echo "The production path is not the validated Git checkout root." >&2
  exit 1
fi
expected_head="$(git -C "$PRODUCTION_ROOT" rev-parse --verify HEAD)"
if [[ ! "$expected_head" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Unable to capture a valid reviewed Git HEAD." >&2
  exit 1
fi

timer_active_status=0
systemctl is-active --quiet telegram-kol-monitor.timer || timer_active_status=$?
case "$timer_active_status" in
  0)
    echo "Stop telegram-kol-monitor.timer before installing or upgrading." >&2
    exit 1
    ;;
  3|4)
    ;;
  *)
    echo "Unable to prove telegram-kol-monitor.timer is inactive." >&2
    exit 1
    ;;
esac

for monitor_unit in \
  telegram-kol-monitor.service \
  telegram-kol-monitor-diagnostic.service \
  telegram-kol-monitor-test-notification.service
do
  unit_active_status=0
  systemctl is-active --quiet "$monitor_unit" || unit_active_status=$?
  case "$unit_active_status" in
    0)
      echo "Stop $monitor_unit before installing or upgrading." >&2
      exit 1
      ;;
    3|4)
      ;;
    *)
      echo "Unable to prove $monitor_unit is inactive." >&2
      exit 1
      ;;
  esac
done

timer_enabled_status=0
systemctl is-enabled --quiet telegram-kol-monitor.timer || timer_enabled_status=$?
case "$timer_enabled_status" in
  0|1|3|4)
    ;;
  *)
    echo "Unable to prove telegram-kol-monitor.timer enablement state." >&2
    exit 1
    ;;
esac
if [[ "$enable_timer" == false && "$timer_enabled_status" -eq 0 ]]; then
  echo "Disable telegram-kol-monitor.timer before an install-only upgrade." >&2
  exit 1
fi

if [[ ! -f "$CREDENTIAL_FILE" ]]; then
  echo "Missing root-owned monitor credential file $CREDENTIAL_FILE." >&2
  exit 1
fi
if [[ "$(stat -c %u "$CREDENTIAL_FILE")" != "0" || "$(stat -c %a "$CREDENTIAL_FILE")" != "600" ]]; then
  echo "Monitor credential file must be owned by root with mode 0600." >&2
  exit 1
fi
if grep -Ev '^(#.*|[[:space:]]*|TELEGRAM_KOL_SYSTEM_BOT_TOKEN=[A-Za-z0-9:_.-]+|TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID=-?[0-9]+|TELEGRAM_KOL_SYSTEM_BOT_TIMEOUT_SECONDS=[0-9]+([.][0-9]+)?)$' "$CREDENTIAL_FILE" >/dev/null; then
  echo "Monitor credential file contains a non-allowlisted key or value." >&2
  exit 1
fi
if [[ "$(grep -c '^TELEGRAM_KOL_SYSTEM_BOT_TOKEN=' "$CREDENTIAL_FILE")" -ne 1 || "$(grep -c '^TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID=' "$CREDENTIAL_FILE")" -ne 1 ]]; then
  echo "Monitor credential file must contain exactly one bot token and chat ID." >&2
  exit 1
fi
if [[ -e "$STATE_FILE" || -L "$STATE_FILE" ]]; then
  if [[ ! -f "$STATE_FILE" || -L "$STATE_FILE" ]]; then
    echo "Existing monitor state path must be a regular non-symlink file." >&2
    exit 1
  fi
fi

# Preflight complete; mutations may begin.
if ! getent group "$MONITOR_GROUP" >/dev/null; then
  groupadd --system "$MONITOR_GROUP"
fi
if ! id "$MONITOR_USER" >/dev/null 2>&1; then
  useradd --system \
    --gid "$MONITOR_GROUP" \
    --home-dir "$STATE_DIRECTORY" \
    --shell /usr/sbin/nologin \
    "$MONITOR_USER"
fi
if [[ "$(id -u "$MONITOR_USER")" -eq 0 || "$(id -gn "$MONITOR_USER")" != "$MONITOR_GROUP" ]]; then
  echo "Existing monitor identity is not the dedicated unprivileged account." >&2
  exit 1
fi
if ! runuser -u "$MONITOR_USER" -- test -x "$PRODUCTION_ROOT/.venv/bin/telegram-kol-research"; then
  echo "Monitor identity cannot execute the production virtualenv CLI." >&2
  exit 1
fi
if ! runuser -u "$MONITOR_USER" -- test -r "$PRODUCTION_ROOT/data/research.db"; then
  echo "Monitor identity cannot read the production database." >&2
  exit 1
fi

state_directory_locked=false
restore_state_directory() {
  if [[ "$state_directory_locked" == true ]]; then
    chown "$MONITOR_USER:$MONITOR_GROUP" "$STATE_DIRECTORY"
    chmod 0700 "$STATE_DIRECTORY"
    state_directory_locked=false
  fi
}

# The monitor identity owns this directory during normal operation. Take it
# back temporarily while the units are proven inactive so the pathname cannot
# be swapped between validation and the descriptor-based metadata repair.
install -d -o root -g root -m 0700 "$STATE_DIRECTORY"
state_directory_locked=true
trap restore_state_directory EXIT
if [[ -e "$STATE_FILE" || -L "$STATE_FILE" ]]; then
  python3 - "$STATE_FILE" "$(id -u "$MONITOR_USER")" "$(id -g "$MONITOR_USER")" <<'PY'
# BEGIN_STATE_METADATA_REPAIR
import os
import stat
import sys

path = sys.argv[1]
uid = int(sys.argv[2])
gid = int(sys.argv[3])
flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
if hasattr(os, "O_CLOEXEC"):
    flags |= os.O_CLOEXEC
fd = os.open(path, flags)
try:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SystemExit("Existing monitor state must be one regular file.")
    os.fchown(fd, uid, gid)
    os.fchmod(fd, 0o600)
    after = os.fstat(fd)
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or after.st_uid != uid
        or after.st_gid != gid
        or stat.S_IMODE(after.st_mode) != 0o600
    ):
        raise SystemExit("Unable to converge monitor state metadata.")
finally:
    os.close(fd)
# END_STATE_METADATA_REPAIR
PY
fi
restore_state_directory
trap - EXIT

env_source="$(mktemp)"
trap 'rm -f "$env_source"' EXIT
chmod 0600 "$env_source"
grep '^TELEGRAM_KOL_SYSTEM_BOT_' "$CREDENTIAL_FILE" > "$env_source"
printf 'TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=%s\n' "$expected_head" >> "$env_source"

install -o root -g root -m 0600 "$env_source" "$ENV_FILE"
install -o root -g root -m 0644 "$SERVICE_SOURCE" "$SERVICE_DEST"
install -o root -g root -m 0644 "$TIMER_SOURCE" "$TIMER_DEST"
install -o root -g root -m 0644 "$TEST_NOTIFICATION_SOURCE" "$TEST_NOTIFICATION_DEST"
install -o root -g root -m 0644 "$DIAGNOSTIC_SOURCE" "$DIAGNOSTIC_DEST"
systemctl daemon-reload

if [[ "$enable_timer" == true ]]; then
  systemctl enable --now telegram-kol-monitor.timer
fi

echo "Installed telegram-kol-monitor for expected HEAD $expected_head."
if [[ "$enable_timer" == false ]]; then
  echo "Timer is disabled and inactive; complete the staged checks before --enable."
fi
