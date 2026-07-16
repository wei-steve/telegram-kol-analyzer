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

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SERVICE_SOURCE="$PROJECT_ROOT/deploy/systemd/telegram-kol-monitor.service"
TIMER_SOURCE="$PROJECT_ROOT/deploy/systemd/telegram-kol-monitor.timer"
SERVICE_DEST="/etc/systemd/system/telegram-kol-monitor.service"
TIMER_DEST="/etc/systemd/system/telegram-kol-monitor.timer"
ENV_FILE="/etc/telegram-kol-monitor.env"
STATE_DIRECTORY="/var/lib/telegram-kol-monitor"

expected_head="$(git -C "$PROJECT_ROOT" rev-parse HEAD)"
if [[ ! "$expected_head" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Unable to capture a valid reviewed Git HEAD." >&2
  exit 1
fi

env_source="$(mktemp)"
trap 'rm -f "$env_source"' EXIT
chmod 0600 "$env_source"
printf 'TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=%s\n' "$expected_head" > "$env_source"

install -d -o root -g root -m 0700 "$STATE_DIRECTORY"
install -o root -g root -m 0600 "$env_source" "$ENV_FILE"
install -o root -g root -m 0644 "$SERVICE_SOURCE" "$SERVICE_DEST"
install -o root -g root -m 0644 "$TIMER_SOURCE" "$TIMER_DEST"
systemctl daemon-reload

if [[ "$enable_timer" == true ]]; then
  systemctl enable --now telegram-kol-monitor.timer
fi

echo "Installed telegram-kol-monitor for expected HEAD $expected_head."
if [[ "$enable_timer" == false ]]; then
  echo "Timer remains disabled; run the staged health and notification checks before --enable."
fi
