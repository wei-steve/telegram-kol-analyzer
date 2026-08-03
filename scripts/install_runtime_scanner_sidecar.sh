#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 || "$#" -ne 0 ]]; then
  echo "Usage: sudo $0" >&2
  exit 2
fi

ROOT=/opt/telegram-kol-analyzer
UNIT=telegram-kol-runtime-scanner.service
SOURCE="$ROOT/deploy/systemd/$UNIT"
DEST="/etc/systemd/system/$UNIT"
ENV_FILE=/etc/telegram-kol-runtime-scanner.env

test "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)" = "$ROOT"
test -f "$SOURCE"
test -x "$ROOT/.venv/bin/telegram-kol-research"
if systemctl is-active --quiet "$UNIT"; then
  echo "Stop $UNIT before installing or upgrading." >&2
  exit 1
fi
if systemctl is-enabled --quiet "$UNIT"; then
  echo "Disable $UNIT before installing or upgrading." >&2
  exit 1
fi

getent passwd telegram-kol-agent >/dev/null
install -o root -g telegram-kol-agent -m 0640 /dev/null "$ENV_FILE"
printf '%s\n' \
  'TELEGRAM_KOL_RUNTIME_SCANNER_ENABLED=false' \
  'TELEGRAM_KOL_RUNTIME_SCANNER_SHADOW_ONLY=true' \
  'TELEGRAM_KOL_RUNTIME_SCANNER_RULES=' \
  'TELEGRAM_KOL_RUNTIME_SCANNER_INTERVAL_SECONDS=60' >"$ENV_FILE"
install -o root -g root -m 0644 "$SOURCE" "$DEST"
systemctl daemon-reload
systemctl disable "$UNIT" >/dev/null 2>&1
echo "Installed $UNIT disabled and inactive."
