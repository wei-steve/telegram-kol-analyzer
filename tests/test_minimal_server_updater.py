from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "1" * 40
CANDIDATE = "2" * 40
OTHER = "3" * 40


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def updater_harness(tmp_path: Path):
    application = tmp_path / "app"
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    for directory in (
        application / ".git",
        application / ".venv/bin",
        application / "data/backups",
        fake_bin,
        state,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    database = application / "data/research.db"
    database.write_bytes(b"sqlite")
    durable_updater = tmp_path / "durable-updater"
    durable_updater.write_text("old updater\n", encoding="utf-8")
    durable_updater.chmod(0o755)
    worker_helper = tmp_path / "telegram-kol-worker-prepare-contract-cache"
    worker_unit = tmp_path / "telegram-kol-worker.service"
    monitor_service = tmp_path / "telegram-kol-monitor.service"
    monitor_diagnostic_service = tmp_path / "telegram-kol-monitor-diagnostic.service"
    monitor_test_notification_service = (
        tmp_path / "telegram-kol-monitor-test-notification.service"
    )
    worker_helper.write_text("old helper\n", encoding="utf-8")
    worker_helper.chmod(0o755)
    worker_unit.write_text("old unit\n", encoding="utf-8")
    worker_unit.chmod(0o644)
    monitor_service.write_text("old monitor unit\n", encoding="utf-8")
    monitor_service.chmod(0o644)
    monitor_diagnostic_service.write_text(
        "old diagnostic monitor unit\n", encoding="utf-8"
    )
    monitor_diagnostic_service.chmod(0o644)
    monitor_test_notification_service.write_text(
        "old test-notification monitor unit\n", encoding="utf-8"
    )
    monitor_test_notification_service.chmod(0o644)
    log = tmp_path / "events.log"
    monitor_env = tmp_path / "telegram-kol-monitor.env"
    monitor_env.write_text(
        "MONITOR_SECRET=must-not-be-printed\n"
        f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={OTHER}\n"
        "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=--expected-auto-trade-enabled\n"
        "MONITOR_SETTING=preserve-this-line\n",
        encoding="utf-8",
    )
    monitor_env.chmod(0o600)
    (state / "head").write_text(f"{PREVIOUS}\n", encoding="utf-8")
    (state / "branch").write_text(f"{PREVIOUS}\n", encoding="utf-8")
    (state / "monitor_timer_enabled").touch()
    (state / "monitor_timer_active").touch()

    _write_executable(
        fake_bin / "git",
        r'''#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-C" ]; then shift 2; fi
case "${1:-}" in
  fetch)
    printf 'fetch\n' >>"$HARNESS_LOG"
    exit "${HARNESS_FETCH_RC:-0}"
    ;;
  status)
    printf 'status\n' >>"$HARNESS_LOG"
    [ "${HARNESS_STATUS_FAIL:-0}" != "1" ] || exit 1
    [ "${HARNESS_DIRTY:-0}" != "1" ] || printf ' M tracked.py\n'
    ;;
  symbolic-ref)
    printf '%s\n' "${HARNESS_CURRENT_BRANCH:-codex/test}"
    ;;
  rev-parse)
    case "${2:-}" in
      FETCH_HEAD) printf '%s\n' "${HARNESS_REMOTE_HEAD:-$HARNESS_CANDIDATE}" ;;
      HEAD) cat "$HARNESS_STATE/head" ;;
      refs/heads/*)
        if [ -n "${HARNESS_BRANCH_HEAD:-}" ]; then
          printf '%s\n' "$HARNESS_BRANCH_HEAD"
        else
          cat "$HARNESS_STATE/branch"
        fi
        ;;
      *) printf '%s\n' "$HARNESS_CANDIDATE" ;;
    esac
    ;;
  worktree)
    if [ "${2:-}" = "add" ]; then
      printf 'worktree-add\n' >>"$HARNESS_LOG"
      [ "${HARNESS_WORKTREE_FAIL:-0}" != "1" ] || exit 1
      mkdir -p "${4}/deploy/systemd" "${4}/src/telegram_kol_research"
      printf 'candidate updater\n' >"${4}/deploy/telegram-kol-update"
      printf 'candidate helper\n' >"${4}/deploy/systemd/telegram-kol-worker-prepare-contract-cache"
      chmod 0755 "${4}/deploy/systemd/telegram-kol-worker-prepare-contract-cache"
      printf 'candidate unit\n' >"${4}/deploy/systemd/telegram-kol-worker.service"
      printf 'candidate monitor unit\n' >"${4}/deploy/systemd/telegram-kol-monitor.service"
      printf 'candidate diagnostic monitor unit\n' >"${4}/deploy/systemd/telegram-kol-monitor-diagnostic.service"
      printf 'candidate test-notification monitor unit\n' >"${4}/deploy/systemd/telegram-kol-monitor-test-notification.service"
      touch "$HARNESS_STATE/worktree_registered"
    else
      printf 'worktree-remove\n' >>"$HARNESS_LOG"
      [ -f "$HARNESS_STATE/worktree_registered" ] || exit 1
      if [ -n "${HARNESS_WORKTREE_REMOVE_SIGNAL:-}" ] && [ ! -f "$HARNESS_STATE/worktree_remove_signaled" ]; then
        touch "$HARNESS_STATE/worktree_remove_signaled"
        kill -"$HARNESS_WORKTREE_REMOVE_SIGNAL" "$PPID"
        exit 128
      fi
      [ "${HARNESS_WORKTREE_REMOVE_FAIL:-0}" != "1" ] || exit 1
      rm -f "$HARNESS_STATE/worktree_registered"
      /bin/rm -rf -- "${4:-}"
    fi
    ;;
  diff)
    printf 'schema-diff\n' >>"$HARNESS_LOG"
    [ "${HARNESS_DIFF_ERROR:-0}" != "1" ] || exit 2
    case "${HARNESS_CHANGED_PATH:-}" in
      src/telegram_kol_research/models.py|src/telegram_kol_research/db.py|migrations/*)
        exit 1
        ;;
      *) exit 0 ;;
    esac
    ;;
  checkout)
    if [ "${2:-}" = "--detach" ]; then
      printf 'rollback-checkout\n' >>"$HARNESS_LOG"
      [ "${HARNESS_ROLLBACK_CHECKOUT_FAIL:-0}" != "1" ] || exit 1
      printf '%s\n' "$3" >"$HARNESS_STATE/head"
    else
      printf 'checkout\n' >>"$HARNESS_LOG"
      if [ "${HARNESS_CHECKOUT_FAIL:-0}" = "1" ] && [ ! -f "$HARNESS_STATE/checkout_failed" ]; then
        touch "$HARNESS_STATE/checkout_failed"
        exit 1
      fi
      cat "$HARNESS_STATE/branch" >"$HARNESS_STATE/head"
    fi
    ;;
  merge)
    printf 'fast-forward\n' >>"$HARNESS_LOG"
    [ "${HARNESS_MERGE_FAIL:-0}" != "1" ] || exit 1
    printf '%s\n' "$3" >"$HARNESS_STATE/head"
    printf '%s\n' "$3" >"$HARNESS_STATE/branch"
    ;;
  update-ref)
    printf 'rollback-ref\n' >>"$HARNESS_LOG"
    [ "${HARNESS_ROLLBACK_REF_FAIL:-0}" != "1" ] || exit 1
    printf '%s\n' "$3" >"$HARNESS_STATE/branch"
    ;;
esac
''',
    )
    _write_executable(
        fake_bin / "systemctl",
        r'''#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
  daemon-reload)
    printf 'daemon-reload\n' >>"$HARNESS_LOG"
    count_file="$HARNESS_STATE/daemon_reload_count"
    count=0
    [ ! -f "$count_file" ] || count="$(cat "$count_file")"
    count=$((count + 1))
    printf '%s\n' "$count" >"$count_file"
    if [ "${HARNESS_DAEMON_RELOAD_FAIL:-0}" = "1" ] && [ "$count" -eq 1 ]; then
      exit 1
    fi
    ;;
  cat)
    case "${2:-}" in
      telegram-kol-ingest.service|telegram-kol-worker.service|telegram-kol-web.service)
        [ "${HARNESS_RUNTIME_TOPOLOGY:-monolith}" != "monolith" ] || exit 1
        ;;
    esac
    [ "${HARNESS_MONITOR_INSTALLED:-1}" = "1" ] || exit 1
    printf 'monitor-unit\n'
    ;;
  is-enabled)
    printf 'monitor-timer-enabled-check\n' >>"$HARNESS_LOG"
    if [ -f "$HARNESS_STATE/monitor_timer_enabled" ]; then
      printf 'enabled\n'
    else
      printf 'disabled\n'
      exit 1
    fi
    ;;
  show)
    log_plain_state=1
    if [ "${2:-}" = "telegram-kol-monitor.timer" ]; then
      if [ -f "$HARNESS_STATE/monitor_timer_active" ]; then
        unit_state=active
      else
        unit_state=inactive
      fi
      printf 'monitor-timer-%s\n' "$unit_state" >>"$HARNESS_LOG"
      log_plain_state=0
    elif [ "${2:-}" = "telegram-kol-monitor.service" ] || [ "${2:-}" = "telegram-kol-monitor-diagnostic.service" ] || [ "${2:-}" = "telegram-kol-monitor-test-notification.service" ]; then
      unit_state="${HARNESS_MONITOR_ONESHOT_STATE:-inactive}"
      printf 'monitor-oneshot-%s\n' "$unit_state" >>"$HARNESS_LOG"
      log_plain_state=0
    elif [ -f "$HARNESS_STATE/rollback_pending" ]; then
      if [ "${HARNESS_ROLLBACK_STOP_MODE:-normal}" = "stuck" ]; then
        unit_state=deactivating
      elif [ ! -f "$HARNESS_STATE/rollback_deactivating_seen" ]; then
        touch "$HARNESS_STATE/rollback_deactivating_seen"
        unit_state=deactivating
      else
        rm -f "$HARNESS_STATE/rollback_pending"
        touch "$HARNESS_STATE/stopped"
        unit_state=inactive
      fi
    elif [ -f "$HARNESS_STATE/pending_stop" ]; then
      if [ "${HARNESS_STOP_MODE:-normal}" = "stuck" ]; then
        unit_state=deactivating
      elif [ ! -f "$HARNESS_STATE/deactivating_seen" ]; then
        touch "$HARNESS_STATE/deactivating_seen"
        unit_state=deactivating
      else
        rm -f "$HARNESS_STATE/pending_stop"
        touch "$HARNESS_STATE/stopped"
        unit_state=inactive
      fi
    elif [ -f "$HARNESS_STATE/stopped" ]; then
      unit_state=inactive
    else
      unit_state=active
    fi
    [ "$log_plain_state" -ne 1 ] || printf '%s\n' "$unit_state" >>"$HARNESS_LOG"
    printf '%s\n' "$unit_state"
    ;;
  is-active)
    if [ "${2:-}" = "telegram-kol-monitor.timer" ]; then
      printf 'monitor-timer-active-check\n' >>"$HARNESS_LOG"
      if [ -f "$HARNESS_STATE/monitor_timer_active" ]; then
        printf 'active\n'
      else
        printf 'inactive\n'
        exit 3
      fi
      exit 0
    fi
    case "${@: -1}" in
      telegram-kol.service)
        case "${HARNESS_RUNTIME_TOPOLOGY:-monolith}" in
          split|partial) exit 3 ;;
        esac
        ;;
      telegram-kol-ingest.service|telegram-kol-worker.service|telegram-kol-web.service)
        [ "${HARNESS_RUNTIME_TOPOLOGY:-monolith}" != "monolith" ] || exit 3
        if [ "${HARNESS_RUNTIME_TOPOLOGY:-monolith}" = "partial" ] \
          && [ "${@: -1}" != "telegram-kol-worker.service" ]; then
          exit 3
        fi
        ;;
    esac
    head_value="$(cat "$HARNESS_STATE/head")"
    if [ -f "$HARNESS_STATE/candidate_started" ]; then
      printf 'is-active\n' >>"$HARNESS_LOG"
      [ "${HARNESS_CANDIDATE_ACTIVE_FAIL:-0}" != "1" ] || exit 1
    elif [ -f "$HARNESS_STATE/rollback_started" ]; then
      printf 'rollback-is-active\n' >>"$HARNESS_LOG"
      [ "${HARNESS_ROLLBACK_ACTIVE_FAIL:-0}" != "1" ] || exit 1
    else
      printf 'initial-is-active\n' >>"$HARNESS_LOG"
    fi
    [ ! -f "$HARNESS_STATE/stopped" ]
    [ ! -f "$HARNESS_STATE/pending_stop" ]
    [ ! -f "$HARNESS_STATE/rollback_pending" ]
    ;;
  stop)
    if [ "${2:-}" = "telegram-kol-monitor.timer" ]; then
      printf 'monitor-timer-stop\n' >>"$HARNESS_LOG"
      [ "${HARNESS_MONITOR_TIMER_STOP_FAIL:-0}" != "1" ] || exit 1
      rm -f "$HARNESS_STATE/monitor_timer_active"
      exit 0
    fi
    printf 'stop-unit:%s\n' "${2:-}" >>"$HARNESS_LOG"
    printf 'stop\n' >>"$HARNESS_LOG"
    if [ ! -f "$HARNESS_STATE/first_stop" ]; then
      touch "$HARNESS_STATE/first_stop"
      case "${HARNESS_STOP_MODE:-normal}" in
        normal) touch "$HARNESS_STATE/stopped" ;;
        fail) exit 1 ;;
        nonzero_inactive) touch "$HARNESS_STATE/stopped"; exit 1 ;;
        deactivating_once|stuck) touch "$HARNESS_STATE/pending_stop" ;;
        term) kill -TERM "$PPID"; exit 143 ;;
        int) kill -INT "$PPID"; exit 130 ;;
      esac
    else
      case "${HARNESS_ROLLBACK_STOP_MODE:-normal}" in
        normal) touch "$HARNESS_STATE/stopped" ;;
        deactivating_once|stuck) touch "$HARNESS_STATE/rollback_pending" ;;
        fail) exit 1 ;;
      esac
    fi
    ;;
  start)
    if [ "${2:-}" = "telegram-kol-monitor.timer" ]; then
      printf 'monitor-timer-start\n' >>"$HARNESS_LOG"
      [ "${HARNESS_MONITOR_TIMER_START_FAIL:-0}" != "1" ] || exit 1
      touch "$HARNESS_STATE/monitor_timer_active"
      exit 0
    fi
    printf 'start-unit:%s\n' "${2:-}" >>"$HARNESS_LOG"
    head_value="$(cat "$HARNESS_STATE/head")"
    if [ "$head_value" = "$HARNESS_CANDIDATE" ]; then
      printf 'start\n' >>"$HARNESS_LOG"
      [ "${HARNESS_CANDIDATE_START_FAIL:-0}" != "1" ] || exit 1
      touch "$HARNESS_STATE/candidate_started"
      rm -f "$HARNESS_STATE/rollback_started"
    else
      printf 'rollback-start\n' >>"$HARNESS_LOG"
      [ "${HARNESS_ROLLBACK_START_FAIL:-0}" != "1" ] || exit 1
      touch "$HARNESS_STATE/rollback_started"
      rm -f "$HARNESS_STATE/candidate_started"
    fi
    rm -f "$HARNESS_STATE/stopped" "$HARNESS_STATE/pending_stop" "$HARNESS_STATE/rollback_pending"
    ;;
  enable)
    printf 'monitor-timer-enable\n' >>"$HARNESS_LOG"
    [ "${HARNESS_MONITOR_TIMER_ENABLE_FAIL:-0}" != "1" ] || exit 1
    touch "$HARNESS_STATE/monitor_timer_enabled"
    ;;
  disable)
    printf 'monitor-timer-disable\n' >>"$HARNESS_LOG"
    [ "${HARNESS_MONITOR_TIMER_DISABLE_FAIL:-0}" != "1" ] || exit 1
    rm -f "$HARNESS_STATE/monitor_timer_enabled"
    ;;
esac
''',
    )
    _write_executable(
        application / ".venv/bin/python",
        r'''#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "telegram_kol_research.deployment_active_write_check" ]; then
  case "${PYTHONPATH:-}" in
    */telegram-kol-stage.*/src) ;;
    *) printf 'wrong-pythonpath\n' >>"$HARNESS_LOG"; exit 4 ;;
  esac
  count_file="$HARNESS_STATE/active_check_count"
  count=0
  [ ! -f "$count_file" ] || count="$(cat "$count_file")"
  count=$((count + 1))
  printf '%s\n' "$count" >"$count_file"
  printf 'active-check-%s\n' "$count" >>"$HARNESS_LOG"
  [ "$count" -le 2 ] || exit 99
  if [ "$count" -eq 1 ]; then
    rc="${HARNESS_PRESTOP_ACTIVE_RC:-0}"
    output="${HARNESS_PRESTOP_ACTIVE_OUTPUT:-}"
  else
    rc="${HARNESS_POSTSTOP_ACTIVE_RC:-0}"
    output="${HARNESS_POSTSTOP_ACTIVE_OUTPUT:-}"
  fi
  if [ -n "$output" ]; then printf '%s\n' "$output"; fi
  case "$rc" in
    0) [ -n "$output" ] || printf 'active_write_count=0\n' ;;
    3) [ -n "$output" ] || printf 'active_write_count=1\n' ;;
    *) printf 'ERROR active_write_check_failed\n' >&2 ;;
  esac
  exit "$rc"
fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
  if [ "$(cat "$HARNESS_STATE/head")" = "$HARNESS_CANDIDATE" ]; then
    printf 'pip-install\n' >>"$HARNESS_LOG"
    [ "${HARNESS_PIP_FAIL:-0}" != "1" ] || exit 1
  else
    printf 'rollback-pip\n' >>"$HARNESS_LOG"
    [ "${HARNESS_ROLLBACK_PIP_FAIL:-0}" != "1" ] || exit 1
  fi
  exit 0
fi
if [ "${1:-}" = "-c" ] && [[ "${2:-}" = *"create_session_factory"* ]]; then
  printf 'production-schema-bootstrap\n' >>"$HARNESS_LOG"
  [ "${3:-}" = "$DATABASE_PATH" ] || exit 65
  [ "$(cat "$HARNESS_STATE/head")" = "$HARNESS_CANDIDATE" ] || exit 66
  [ "${HARNESS_PRODUCTION_SCHEMA_BOOTSTRAP_FAIL:-0}" != "1" ] || exit 1
  exit 0
fi
if [ "${1:-}" = "-c" ] && [[ "${2:-}" = *"monitor-pin-rewrite"* ]]; then
  printf 'monitor-pin-rewrite\n' >>"$HARNESS_LOG"
  MONITOR_REWRITE_HEAD="$5" MONITOR_REWRITE_OPTION="$6" perl -0pe '
    s/^TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=[^\r\n]*/TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=$ENV{MONITOR_REWRITE_HEAD}/m;
    if (/^TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=/m) {
      s/^TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=[^\r\n]*/TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=$ENV{MONITOR_REWRITE_OPTION}/m;
    } else {
      s/^(TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=[^\r\n]*)/$1\nTELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=$ENV{MONITOR_REWRITE_OPTION}/m;
    }
  ' "$3" >"$4"
  exit 0
fi
if [ "${1:-}" = "-" ] && [ "${2:-}" = "schema" ]; then
  printf 'online-backup\n' >>"$HARNESS_LOG"
  [ "${HARNESS_SCHEMA_BACKUP_FAIL:-0}" != "1" ] || exit 1
  printf 'backup-quick-check\n' >>"$HARNESS_LOG"
  [ "${HARNESS_BACKUP_CHECK_FAIL:-0}" != "1" ] || exit 1
  printf 'disposable-migration\n' >>"$HARNESS_LOG"
  [ "${HARNESS_MIGRATION_FAIL:-0}" != "1" ] || exit 1
  printf 'migration-quick-check\n' >>"$HARNESS_LOG"
  [ "${HARNESS_MIGRATION_CHECK_FAIL:-0}" != "1" ] || exit 1
  printf 'watermark-compare\n' >>"$HARNESS_LOG"
  [ "${HARNESS_WATERMARK_FAIL:-0}" != "1" ] || exit 1
  touch "$4" "$5"
  exit 0
fi
exit 64
''',
    )
    _write_executable(
        fake_bin / "install",
        r'''#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = "-d" ] || [[ " $* " = *" -d "* ]]; then exec /usr/bin/install "$@"; fi
count=$#
eval "source=\${$((count - 1))}"
eval "target=\${$count}"
if [ "$target" = "$WORKER_CACHE_HELPER_PATH" ]; then
  if [[ "$source" = *telegram-kol-stage.*/deploy/systemd/* ]]; then
    printf 'worker-helper-install\n' >>"$HARNESS_LOG"
    [ "${HARNESS_WORKER_HELPER_INSTALL_FAIL:-0}" != "1" ] || exit 1
  else
    printf 'worker-helper-restore\n' >>"$HARNESS_LOG"
  fi
elif [ "$target" = "$WORKER_UNIT_PATH" ]; then
  if [[ "$source" = *telegram-kol-stage.*/deploy/systemd/* ]]; then
    printf 'worker-unit-install\n' >>"$HARNESS_LOG"
    [ "${HARNESS_WORKER_UNIT_INSTALL_FAIL:-0}" != "1" ] || exit 1
  else
    printf 'worker-unit-restore\n' >>"$HARNESS_LOG"
  fi
elif [ "$target" = "$MONITOR_SERVICE_PATH" ]; then
  if [[ "$source" = *telegram-kol-stage.*/deploy/systemd/* ]]; then
    printf 'monitor-unit-install\n' >>"$HARNESS_LOG"
    [ "${HARNESS_MONITOR_UNIT_INSTALL_FAIL:-0}" != "1" ] || exit 1
  else
    printf 'monitor-unit-restore\n' >>"$HARNESS_LOG"
  fi
elif [ "$target" = "$MONITOR_DIAGNOSTIC_SERVICE_PATH" ]; then
  if [[ "$source" = *telegram-kol-stage.*/deploy/systemd/* ]]; then
    printf 'monitor-diagnostic-unit-install\n' >>"$HARNESS_LOG"
    [ "${HARNESS_MONITOR_DIAGNOSTIC_UNIT_INSTALL_FAIL:-0}" != "1" ] || exit 1
  else
    printf 'monitor-diagnostic-unit-restore\n' >>"$HARNESS_LOG"
  fi
elif [ "$target" = "$MONITOR_TEST_NOTIFICATION_SERVICE_PATH" ]; then
  if [[ "$source" = *telegram-kol-stage.*/deploy/systemd/* ]]; then
    printf 'monitor-test-notification-unit-install\n' >>"$HARNESS_LOG"
    [ "${HARNESS_MONITOR_TEST_NOTIFICATION_UNIT_INSTALL_FAIL:-0}" != "1" ] || exit 1
  else
    printf 'monitor-test-notification-unit-restore\n' >>"$HARNESS_LOG"
  fi
elif [ "$target" = "$MONITOR_ENV_FILE" ]; then
  printf 'monitor-env-restore\n' >>"$HARNESS_LOG"
elif [[ "$target" = *.candidate.* ]]; then
  printf 'durable-updater-install\n' >>"$HARNESS_LOG"
  [ "${HARNESS_DURABLE_INSTALL_FAIL:-0}" != "1" ] || exit 1
else
  printf 'updater-restore\n' >>"$HARNESS_LOG"
  [ "${HARNESS_UPDATER_RESTORE_FAIL:-0}" != "1" ] || exit 1
fi
/bin/cp "$source" "$target"
chmod 0755 "$target"
''',
    )
    _write_executable(
        fake_bin / "mv",
        r'''#!/usr/bin/env bash
set -euo pipefail
target="${@: -1}"
if [ "$target" = "$MONITOR_ENV_FILE" ]; then
  source="${@: -2:1}"
  if grep -q "TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=$HARNESS_PREVIOUS" "$source"; then
    printf 'monitor-pin-previous\n' >>"$HARNESS_LOG"
    [ "${HARNESS_MONITOR_PIN_PREVIOUS_FAIL:-0}" != "1" ] || exit 1
  elif grep -q "TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=$HARNESS_CANDIDATE" "$source"; then
    printf 'monitor-pin-candidate\n' >>"$HARNESS_LOG"
    [ "${HARNESS_MONITOR_PIN_CANDIDATE_FAIL:-0}" != "1" ] || exit 1
  else
    printf 'monitor-pin-unknown\n' >>"$HARNESS_LOG"
    exit 1
  fi
  exec /bin/mv "$@"
fi
printf 'durable-updater-move\n' >>"$HARNESS_LOG"
[ "${HARNESS_DURABLE_MOVE_FAIL:-0}" != "1" ] || exit 1
if [ "${HARNESS_DURABLE_MOVE_FAIL_AFTER:-0}" = "1" ]; then
  /bin/mv "$@"
  exit 1
fi
exec /bin/mv "$@"
''',
    )
    _write_executable(
        fake_bin / "stat",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf '%s %s\n' "${HARNESS_MONITOR_UID:-$(id -u)}" "${HARNESS_MONITOR_MODE:-600}"
''',
    )
    _write_executable(
        fake_bin / "curl",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf 'http-health\n' >>"$HARNESS_LOG"
[ "${HARNESS_HTTP_FAIL:-0}" != "1" ]
''',
    )
    _write_executable(
        fake_bin / "flock",
        '#!/usr/bin/env bash\n[ "${HARNESS_FLOCK_FAIL:-0}" != "1" ]\n',
    )
    _write_executable(
        fake_bin / "rm",
        r'''#!/usr/bin/env bash
set -euo pipefail
case " $* " in
  *telegram-kol-updater-backup.*)
    if [ -n "${HARNESS_BACKUP_REMOVE_SIGNAL:-}" ] && [ ! -f "$HARNESS_STATE/backup_remove_signaled" ]; then
      touch "$HARNESS_STATE/backup_remove_signaled"
      /bin/rm "$@"
      kill -"$HARNESS_BACKUP_REMOVE_SIGNAL" "$PPID"
      exit 128
    fi
    [ "${HARNESS_FINAL_BACKUP_REMOVE_FAIL:-0}" != "1" ] || exit 1
    ;;
  *schema-dry-run-*)
    [ "${HARNESS_FINAL_DRY_RUN_REMOVE_FAIL:-0}" != "1" ] || exit 1
    ;;
esac
exec /bin/rm "$@"
''',
    )
    _write_executable(
        fake_bin / "timeout",
        '#!/usr/bin/env bash\nshift 2\nexec "$@"\n',
    )
    _write_executable(fake_bin / "sleep", "#!/usr/bin/env bash\nexit 0\n")

    def run(**overrides: str) -> subprocess.CompletedProcess[str]:
        log.write_text("", encoding="utf-8")
        installation = overrides.pop("HARNESS_MONITOR_INSTALLATION", "complete")
        prior_enabled = overrides.pop("HARNESS_MONITOR_PRIOR_ENABLED", "1")
        prior_active = overrides.pop("HARNESS_MONITOR_PRIOR_ACTIVE", "1")
        for marker, enabled in (
            (state / "monitor_timer_enabled", prior_enabled),
            (state / "monitor_timer_active", prior_active),
        ):
            if enabled == "1":
                marker.touch()
            elif marker.exists():
                marker.unlink()
        if monitor_env.is_symlink() or monitor_env.exists():
            monitor_env.unlink()
        if installation in {
            "complete",
            "env_only",
            "legacy_missing_expectation",
            "legacy_indented_expectation",
            "continued_managed_assignment",
            "malformed",
            "symlink",
            "no_final_newline",
            "duplicate_expectation",
            "invalid_expectation",
        }:
            monitor_env.write_text(
                "MONITOR_SECRET=must-not-be-printed\n"
                f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={OTHER}\n"
                "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=--expected-auto-trade-enabled\n"
                "MONITOR_SETTING=preserve-this-line\n",
                encoding="utf-8",
            )
            monitor_env.chmod(0o600)
        if installation == "legacy_missing_expectation":
            monitor_env.write_text(
                monitor_env.read_text(encoding="utf-8").replace(
                    "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION="
                    "--expected-auto-trade-enabled\n",
                    "",
                ),
                encoding="utf-8",
            )
        if installation == "legacy_indented_expectation":
            monitor_env.write_text(
                monitor_env.read_text(encoding="utf-8").replace(
                    "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=",
                    " TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=",
                ),
                encoding="utf-8",
            )
        if installation == "continued_managed_assignment":
            monitor_env.write_text(
                monitor_env.read_text(encoding="utf-8").replace(
                    "MONITOR_SECRET=must-not-be-printed\n",
                    "MONITOR_SECRET=must-not-be-printed\\\n",
                ),
                encoding="utf-8",
            )
        if installation == "no_final_newline":
            monitor_env.write_bytes(
                b"MONITOR_SECRET=must-not-be-printed\n"
                + f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={OTHER}\n".encode()
                + b"TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=--expected-auto-trade-enabled\n"
                + b"MONITOR_SETTING=preserve-without-final-newline"
            )
        if installation == "malformed":
            monitor_env.write_text(
                monitor_env.read_text(encoding="utf-8")
                + f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={PREVIOUS}\n",
                encoding="utf-8",
            )
        if installation == "duplicate_expectation":
            monitor_env.write_text(
                monitor_env.read_text(encoding="utf-8")
                + "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION="
                "--no-expected-auto-trade-enabled\n",
                encoding="utf-8",
            )
        if installation == "invalid_expectation":
            monitor_env.write_text(
                monitor_env.read_text(encoding="utf-8").replace(
                    "--expected-auto-trade-enabled", "--auto-trade-maybe"
                ),
                encoding="utf-8",
            )
        if installation == "symlink":
            target = tmp_path / "monitor-env-target"
            target.write_text(
                f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={OTHER}\n",
                encoding="utf-8",
            )
            monitor_env.unlink()
            monitor_env.symlink_to(target)
        overrides.setdefault(
            "HARNESS_MONITOR_INSTALLED",
            "0" if installation in {"absent", "env_only"} else "1",
        )
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "APP_DIR": str(application),
                "DATABASE_PATH": str(database),
                "LOCK_PATH": str(tmp_path / "update.lock"),
                "STAGE_PARENT": str(tmp_path),
                "UPDATER_PATH": str(durable_updater),
                "WORKER_CACHE_HELPER_PATH": str(worker_helper),
                "WORKER_UNIT_PATH": str(worker_unit),
                "UPDATER_TEST_MODE": "1",
                "MONITOR_ENV_FILE": str(monitor_env),
                "MONITOR_SERVICE_PATH": str(monitor_service),
                "MONITOR_DIAGNOSTIC_SERVICE_PATH": str(monitor_diagnostic_service),
                "MONITOR_TEST_NOTIFICATION_SERVICE_PATH": str(
                    monitor_test_notification_service
                ),
                "EXPECTED_COMMIT": CANDIDATE,
                "EXPECTED_AUTO_TRADE_STATE": "enabled",
                "BRANCH": "codex/test",
                "STOP_TIMEOUT_SECONDS": "1",
                "HARNESS_LOG": str(log),
                "HARNESS_STATE": str(state),
                "HARNESS_PREVIOUS": PREVIOUS,
                "HARNESS_CANDIDATE": CANDIDATE,
            }
        )
        environment.update(overrides)
        if environment.pop("HARNESS_ARTIFACTS_EXIST", "1") == "0":
            worker_helper.unlink(missing_ok=True)
            worker_unit.unlink(missing_ok=True)
        return subprocess.run(
            ["bash", str(ROOT / "deploy/telegram-kol-update")],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            timeout=15,
        )

    return run, log, state


def _events(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines()


def test_success_has_exact_short_authorization_order(updater_harness) -> None:
    run, log, _ = updater_harness

    result = run()

    assert result.returncode == 0, result.stderr
    relevant = [
        event
        for event in _events(log)
        if event
        in {
            "fetch",
            "worktree-add",
            "schema-diff",
            "active-check-1",
            "stop",
            "inactive",
            "active-check-2",
            "checkout",
            "fast-forward",
            "pip-install",
            "start",
            "is-active",
            "http-health",
            "durable-updater-install",
            "durable-updater-move",
            "worktree-remove",
        }
    ]
    assert relevant == [
        "fetch",
        "worktree-add",
        "schema-diff",
        "active-check-1",
        "stop",
        "inactive",
        "active-check-2",
        "checkout",
        "fast-forward",
        "pip-install",
        "start",
        "is-active",
        "http-health",
        "durable-updater-install",
        "durable-updater-move",
        "worktree-remove",
    ]


def test_split_topology_uses_ordered_three_unit_stop_and_start(updater_harness):
    run, log, _ = updater_harness

    result = run(HARNESS_RUNTIME_TOPOLOGY="split")

    assert result.returncode == 0, result.stderr
    events = _events(log)
    assert [event for event in events if event.startswith("stop-unit:")] == [
        "stop-unit:telegram-kol-ingest.service",
        "stop-unit:telegram-kol-web.service",
        "stop-unit:telegram-kol-worker.service",
    ]
    assert [event for event in events if event.startswith("start-unit:")] == [
        "start-unit:telegram-kol-worker.service",
        "start-unit:telegram-kol-web.service",
        "start-unit:telegram-kol-ingest.service",
    ]


def test_split_installs_worker_cache_artifacts_before_worker_start(updater_harness):
    run, log, _ = updater_harness

    result = run(HARNESS_RUNTIME_TOPOLOGY="split")

    assert result.returncode == 0, result.stderr
    events = _events(log)
    assert events.index("pip-install") < events.index("worker-helper-install")
    assert events.index("worker-helper-install") < events.index("worker-unit-install")
    assert events.index("worker-unit-install") < events.index("daemon-reload")
    assert events.index("daemon-reload") < events.index(
        "start-unit:telegram-kol-worker.service"
    )
    assert (log.parent / "telegram-kol-worker-prepare-contract-cache").read_text() == (
        "candidate helper\n"
    )
    assert (log.parent / "telegram-kol-worker.service").read_text() == (
        "candidate unit\n"
    )


def test_monolith_does_not_install_worker_cache_artifacts(updater_harness):
    run, log, _ = updater_harness

    result = run(HARNESS_RUNTIME_TOPOLOGY="monolith")

    assert result.returncode == 0, result.stderr
    assert not any("worker-helper" in event for event in _events(log))
    assert not any("worker-unit" in event for event in _events(log))
    assert "monitor-unit-install" in _events(log)
    assert _events(log).count("daemon-reload") == 1


@pytest.mark.parametrize(
    "failure",
    [
        {"HARNESS_WORKER_UNIT_INSTALL_FAIL": "1"},
        {"HARNESS_DAEMON_RELOAD_FAIL": "1"},
        {"HARNESS_CANDIDATE_START_FAIL": "1"},
    ],
)
def test_split_artifact_failures_restore_previous_helper_and_unit(
    updater_harness, failure: dict[str, str]
):
    run, log, _ = updater_harness

    result = run(HARNESS_RUNTIME_TOPOLOGY="split", **failure)

    assert result.returncode != 0
    assert (log.parent / "telegram-kol-worker-prepare-contract-cache").read_text() == (
        "old helper\n"
    )
    assert (log.parent / "telegram-kol-worker.service").read_text() == "old unit\n"
    events = _events(log)
    assert "worker-helper-restore" in events
    assert "worker-unit-restore" in events
    assert events.index("worker-helper-restore") < events.index("rollback-start")


def test_split_rollback_removes_only_new_artifact_targets(updater_harness):
    run, log, _ = updater_harness

    result = run(
        HARNESS_RUNTIME_TOPOLOGY="split",
        HARNESS_ARTIFACTS_EXIST="0",
        HARNESS_CANDIDATE_START_FAIL="1",
    )

    assert result.returncode == 4
    assert not (log.parent / "telegram-kol-worker-prepare-contract-cache").exists()
    assert not (log.parent / "telegram-kol-worker.service").exists()


@pytest.mark.parametrize("topology", ["both", "partial"])
def test_ambiguous_or_partial_topology_fails_before_deployment(
    updater_harness, topology: str
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_RUNTIME_TOPOLOGY=topology)

    assert result.returncode == 4
    assert "Ambiguous or incomplete runtime topology" in result.stderr
    assert "fetch" not in _events(log)
    assert not any(event.startswith("stop-unit:") for event in _events(log))


def test_success_uses_candidate_source_and_exactly_two_checks(
    updater_harness,
) -> None:
    run, log, state = updater_harness

    result = run()

    assert result.returncode == 0, result.stderr
    assert "wrong-pythonpath" not in _events(log)
    assert (state / "active_check_count").read_text().strip() == "2"


def test_monitor_pin_transaction_wraps_successful_cutover(updater_harness) -> None:
    run, log, state = updater_harness
    monitor_env = log.parent / "telegram-kol-monitor.env"

    result = run()

    assert result.returncode == 0, result.stderr
    events = _events(log)
    assert events.index("monitor-timer-stop") < events.index("stop")
    assert events.index("monitor-pin-previous") < events.index("checkout")
    assert events.index("http-health") < events.index("monitor-pin-candidate")
    assert events.index("monitor-pin-candidate") < events.index(
        "monitor-timer-enable"
    )
    assert events.index("monitor-timer-enable") < events.index(
        "monitor-timer-start"
    )
    assert monitor_env.read_text(encoding="utf-8") == (
        "MONITOR_SECRET=must-not-be-printed\n"
        f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={CANDIDATE}\n"
        "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=--expected-auto-trade-enabled\n"
        "MONITOR_SETTING=preserve-this-line\n"
    )
    assert (state / "monitor_timer_enabled").exists()
    assert (state / "monitor_timer_active").exists()


@pytest.mark.parametrize(
    ("state", "expected_option"),
    [
        ("enabled", "--expected-auto-trade-enabled"),
        ("disabled", "--no-expected-auto-trade-enabled"),
    ],
)
def test_legacy_monitor_env_adds_missing_governed_expectation(
    updater_harness, state: str, expected_option: str
) -> None:
    run, log, _ = updater_harness
    monitor_env = log.parent / "telegram-kol-monitor.env"

    result = run(
        HARNESS_MONITOR_INSTALLATION="legacy_missing_expectation",
        EXPECTED_AUTO_TRADE_STATE=state,
    )

    assert result.returncode == 0, result.stderr
    assert "checkout" in _events(log)
    assert monitor_env.read_text(encoding="utf-8") == (
        "MONITOR_SECRET=must-not-be-printed\n"
        f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={CANDIDATE}\n"
        "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION="
        f"{expected_option}\n"
        "MONITOR_SETTING=preserve-this-line\n"
    )
    assert "must-not-be-printed" not in result.stdout + result.stderr


def test_legacy_monitor_env_is_restored_byte_for_byte_after_failure(
    updater_harness,
) -> None:
    run, log, _ = updater_harness
    monitor_env = log.parent / "telegram-kol-monitor.env"
    original = (
        "MONITOR_SECRET=must-not-be-printed\n"
        f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={OTHER}\n"
        "MONITOR_SETTING=preserve-this-line\n"
    ).encode()

    result = run(
        HARNESS_MONITOR_INSTALLATION="legacy_missing_expectation",
        EXPECTED_AUTO_TRADE_STATE="disabled",
        HARNESS_HTTP_FAIL="1",
    )

    assert result.returncode == 4
    assert "monitor-pin-previous" in _events(log)
    assert "checkout" in _events(log)
    assert monitor_env.read_bytes() == original
    assert "must-not-be-printed" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "installation",
    ["legacy_indented_expectation", "continued_managed_assignment"],
)
def test_systemd_environmentfile_ambiguity_fails_before_checkout(
    updater_harness, installation: str
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_MONITOR_INSTALLATION=installation)

    assert result.returncode == 4
    assert "checkout" not in _events(log)
    assert "must-not-be-printed" not in result.stdout + result.stderr


def test_monitor_pin_preserves_non_head_bytes_without_final_newline(
    updater_harness,
) -> None:
    run, log, _ = updater_harness
    monitor_env = log.parent / "telegram-kol-monitor.env"

    result = run(HARNESS_MONITOR_INSTALLATION="no_final_newline")

    assert result.returncode == 0, result.stderr
    assert monitor_env.read_bytes() == (
        b"MONITOR_SECRET=must-not-be-printed\n"
        + f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={CANDIDATE}\n".encode()
        + b"TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=--expected-auto-trade-enabled\n"
        + b"MONITOR_SETTING=preserve-without-final-newline"
    )


def test_disabled_expectation_is_atomic_with_candidate_head_and_all_monitor_units(
    updater_harness,
) -> None:
    run, log, _ = updater_harness
    monitor_env = log.parent / "telegram-kol-monitor.env"
    monitor_unit = log.parent / "telegram-kol-monitor.service"
    diagnostic_unit = log.parent / "telegram-kol-monitor-diagnostic.service"
    test_notification_unit = (
        log.parent / "telegram-kol-monitor-test-notification.service"
    )

    result = run(EXPECTED_AUTO_TRADE_STATE="disabled")

    assert result.returncode == 0, result.stderr
    assert monitor_env.read_text(encoding="utf-8").count(
        "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION="
    ) == 1
    assert (
        "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION="
        "--no-expected-auto-trade-enabled\n"
    ) in monitor_env.read_text(encoding="utf-8")
    assert monitor_unit.read_text(encoding="utf-8") == "candidate monitor unit\n"
    assert diagnostic_unit.read_text(encoding="utf-8") == (
        "candidate diagnostic monitor unit\n"
    )
    assert test_notification_unit.read_text(encoding="utf-8") == (
        "candidate test-notification monitor unit\n"
    )
    events = _events(log)
    assert events.index("monitor-unit-install") < events.index("daemon-reload")
    assert events.index("monitor-diagnostic-unit-install") < events.index(
        "daemon-reload"
    )
    assert events.index("monitor-test-notification-unit-install") < events.index(
        "daemon-reload"
    )
    assert events.index("daemon-reload") < events.index("monitor-pin-candidate")


@pytest.mark.parametrize("state", ["", "true", "ENABLED", "disabled\nBAD=1"])
def test_invalid_auto_trade_expectation_fails_before_fetch(
    updater_harness, state: str
) -> None:
    run, log, _ = updater_harness

    result = run(EXPECTED_AUTO_TRADE_STATE=state)

    assert result.returncode != 0
    assert "fetch" not in _events(log)


@pytest.mark.parametrize("fault", ["duplicate_expectation", "invalid_expectation"])
def test_malformed_monitor_expectation_fails_before_checkout(
    updater_harness, fault: str
) -> None:
    run, log, _ = updater_harness
    monitor_env = log.parent / "telegram-kol-monitor.env"
    original_run = run

    # Ask the harness to generate the baseline, then rewrite it through the
    # explicit malformed installation modes handled by the fixture.
    installation = fault
    result = original_run(HARNESS_MONITOR_INSTALLATION=installation)

    assert result.returncode == 4
    assert "checkout" not in _events(log)
    assert "must-not-be-printed" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "failure",
    [
        {"HARNESS_MONITOR_UNIT_INSTALL_FAIL": "1"},
        {"HARNESS_MONITOR_DIAGNOSTIC_UNIT_INSTALL_FAIL": "1"},
        {"HARNESS_MONITOR_TEST_NOTIFICATION_UNIT_INSTALL_FAIL": "1"},
    ],
)
def test_monitor_unit_failure_restores_all_old_units_and_old_env(
    updater_harness, failure: dict[str, str]
) -> None:
    run, log, _ = updater_harness
    monitor_env = log.parent / "telegram-kol-monitor.env"
    monitor_unit = log.parent / "telegram-kol-monitor.service"
    diagnostic_unit = log.parent / "telegram-kol-monitor-diagnostic.service"
    test_notification_unit = (
        log.parent / "telegram-kol-monitor-test-notification.service"
    )

    result = run(
        EXPECTED_AUTO_TRADE_STATE="disabled",
        **failure,
    )

    assert result.returncode == 4
    assert monitor_unit.read_text(encoding="utf-8") == "old monitor unit\n"
    assert diagnostic_unit.read_text(encoding="utf-8") == (
        "old diagnostic monitor unit\n"
    )
    assert test_notification_unit.read_text(encoding="utf-8") == (
        "old test-notification monitor unit\n"
    )
    assert (
        "TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION="
        "--expected-auto-trade-enabled\n"
    ) in monitor_env.read_text(encoding="utf-8")
    assert "monitor-unit-restore" in _events(log)
    assert "monitor-diagnostic-unit-restore" in _events(log)
    assert "monitor-test-notification-unit-restore" in _events(log)


def test_monitor_waits_for_every_installed_oneshot(updater_harness) -> None:
    run, log, _ = updater_harness

    result = run()

    assert result.returncode == 0, result.stderr
    assert _events(log).count("monitor-oneshot-inactive") == 3


def test_absent_monitor_preserves_existing_updater_behavior(updater_harness) -> None:
    run, log, state = updater_harness

    result = run(HARNESS_MONITOR_INSTALLATION="absent")

    assert result.returncode == 0, result.stderr
    assert (state / "head").read_text().strip() == CANDIDATE
    assert not any(event.startswith("monitor-") for event in _events(log))


def test_monitor_restores_prior_disabled_inactive_timer_state(
    updater_harness,
) -> None:
    run, log, state = updater_harness

    result = run(
        HARNESS_MONITOR_PRIOR_ENABLED="0",
        HARNESS_MONITOR_PRIOR_ACTIVE="0",
    )

    assert result.returncode == 0, result.stderr
    events = _events(log)
    assert "monitor-timer-disable" in events
    assert "monitor-timer-start" not in events
    assert not (state / "monitor_timer_enabled").exists()
    assert not (state / "monitor_timer_active").exists()


@pytest.mark.parametrize(
    ("installation", "overrides"),
    (
        ("malformed", {}),
        ("symlink", {}),
        ("env_only", {}),
        ("timer_only", {}),
        ("complete", {"HARNESS_MONITOR_MODE": "640"}),
        ("complete", {"HARNESS_MONITOR_UID": "99999"}),
    ),
)
def test_invalid_partial_monitor_installation_fails_before_checkout_mutation(
    updater_harness,
    installation: str,
    overrides: dict[str, str],
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_MONITOR_INSTALLATION=installation, **overrides)

    assert result.returncode == 4
    events = _events(log)
    assert "stop" not in events
    assert "checkout" not in events
    assert "must-not-be-printed" not in result.stdout + result.stderr


def test_monitor_timer_stop_failure_never_reaches_application_stop(
    updater_harness,
) -> None:
    run, log, state = updater_harness

    result = run(HARNESS_MONITOR_TIMER_STOP_FAIL="1")

    assert result.returncode == 4
    assert "stop" not in _events(log)
    assert "checkout" not in _events(log)
    assert (state / "monitor_timer_active").exists()


def test_monitor_predeploy_pin_failure_restores_timer_without_checkout(
    updater_harness,
) -> None:
    run, log, state = updater_harness

    result = run(HARNESS_MONITOR_PIN_PREVIOUS_FAIL="1")

    assert result.returncode == 4
    events = _events(log)
    assert "monitor-timer-stop" in events
    assert "checkout" not in events
    assert "stop" not in events
    assert "monitor-timer-start" in events
    assert (state / "monitor_timer_active").exists()


@pytest.mark.parametrize(
    "fault",
    (
        "HARNESS_CHECKOUT_FAIL",
        "HARNESS_PIP_FAIL",
        "HARNESS_CANDIDATE_START_FAIL",
        "HARNESS_HTTP_FAIL",
        "HARNESS_MONITOR_PIN_CANDIDATE_FAIL",
    ),
)
def test_post_normalization_failure_rolls_back_pin_and_timer(
    updater_harness, fault: str
) -> None:
    run, log, state = updater_harness
    monitor_env = log.parent / "telegram-kol-monitor.env"

    result = run(**{fault: "1"})

    assert result.returncode == 4
    assert (state / "head").read_text().strip() == PREVIOUS
    assert (
        f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={OTHER}"
        in monitor_env.read_text(encoding="utf-8")
    )
    assert (state / "monitor_timer_enabled").exists()
    assert (state / "monitor_timer_active").exists()
    events = _events(log)
    assert events.index("monitor-pin-previous") < events.index("checkout")
    assert events.index("rollback-is-active") < len(events) - 1 - events[
        ::-1
    ].index("monitor-timer-start")


def test_monitor_timer_restore_failure_rolls_back_application_and_pin(
    updater_harness,
) -> None:
    run, log, state = updater_harness
    monitor_env = log.parent / "telegram-kol-monitor.env"

    result = run(HARNESS_MONITOR_TIMER_START_FAIL="1")

    assert result.returncode == 4
    assert (state / "head").read_text().strip() == PREVIOUS
    assert (
        f"TELEGRAM_KOL_MONITOR_EXPECTED_HEAD={OTHER}"
        in monitor_env.read_text(encoding="utf-8")
    )
    assert "must-not-be-printed" not in result.stdout + result.stderr


def test_updater_has_no_retired_gate_mechanism_or_event(updater_harness) -> None:
    run, log, _ = updater_harness

    result = run()

    assert result.returncode == 0, result.stderr
    combined = "\n".join(_events(log)) + (ROOT / "deploy/telegram-kol-update").read_text()
    forbidden = (
        "deployment_preflight_cli",
        "surface",
        "snapshot",
        "watermark_artifact",
        "preliminary",
        "final_artifact",
        "fingerprint",
        "WARN",
        "BLOCK",
    )
    assert all(value not in combined for value in forbidden)


@pytest.mark.parametrize(("rc", "expected_rc"), (("3", 3), ("4", 4)))
def test_prestop_nonzero_refuses_without_stop_or_mutation(
    updater_harness,
    rc: str,
    expected_rc: int,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_PRESTOP_ACTIVE_RC=rc)

    assert result.returncode == expected_rc
    events = _events(log)
    assert "stop" not in events
    assert "checkout" not in events
    assert "pip-install" not in events
    assert "start" not in events


def test_malformed_zero_output_fails_closed_before_stop(updater_harness) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_PRESTOP_ACTIVE_OUTPUT="active_write_count=00")

    assert result.returncode == 4
    assert "stop" not in _events(log)


@pytest.mark.parametrize(("rc", "expected_rc"), (("3", 3), ("4", 4)))
def test_poststop_nonzero_restarts_old_service_without_mutation(
    updater_harness,
    rc: str,
    expected_rc: int,
) -> None:
    run, log, state = updater_harness

    result = run(HARNESS_POSTSTOP_ACTIVE_RC=rc)

    assert result.returncode == expected_rc
    events = _events(log)
    assert events.index("inactive") < events.index("active-check-2")
    assert "checkout" not in events
    assert "pip-install" not in events
    assert "rollback-start" in events
    assert "rollback-is-active" in events
    assert not (state / "stopped").exists()


def test_delayed_stop_waits_for_inactive_before_second_check(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_STOP_MODE="deactivating_once")

    assert result.returncode == 0, result.stderr
    events = _events(log)
    assert events.index("deactivating") < events.index("inactive")
    assert events.index("inactive") < events.index("active-check-2")
    assert events.index("active-check-2") < events.index("checkout")


def test_permanent_deactivating_is_hard_failure_without_checkout(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_STOP_MODE="stuck")

    assert result.returncode == 4
    events = _events(log)
    assert "deactivating" in events
    assert "active-check-2" not in events
    assert "checkout" not in events


@pytest.mark.parametrize("mode", ("fail", "nonzero_inactive", "term", "int"))
def test_stop_fault_reasserts_old_service_without_checkout(
    updater_harness,
    mode: str,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_STOP_MODE=mode)

    expected = {"fail": 4, "nonzero_inactive": 4, "term": 143, "int": 130}
    assert result.returncode == expected[mode]
    events = _events(log)
    assert "checkout" not in events
    assert "rollback-start" in events


@pytest.mark.parametrize(
    "changed_path",
    (
        "src/telegram_kol_research/models.py",
        "src/telegram_kol_research/db.py",
        "migrations/001_example.py",
    ),
)
def test_schema_paths_run_backup_migration_checks_before_first_active_check(
    updater_harness,
    changed_path: str,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_CHANGED_PATH=changed_path)

    assert result.returncode == 0, result.stderr
    events = _events(log)
    schema_events = [
        "online-backup",
        "backup-quick-check",
        "disposable-migration",
        "migration-quick-check",
        "watermark-compare",
    ]
    assert [event for event in events if event in schema_events] == schema_events
    assert events.index("watermark-compare") < events.index("active-check-1")


def test_schema_candidate_bootstraps_production_once_before_split_start(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(
        HARNESS_CHANGED_PATH="src/telegram_kol_research/models.py",
        HARNESS_RUNTIME_TOPOLOGY="split",
    )

    assert result.returncode == 0, result.stderr
    events = _events(log)
    assert events.count("production-schema-bootstrap") == 1
    assert events.index("pip-install") < events.index("production-schema-bootstrap")
    first_start = next(
        index
        for index, event in enumerate(events)
        if event.startswith("start-unit:")
    )
    assert events.index("production-schema-bootstrap") < first_start


def test_production_schema_bootstrap_failure_rolls_back_before_candidate_start(
    updater_harness,
) -> None:
    run, log, state = updater_harness

    result = run(
        HARNESS_CHANGED_PATH="src/telegram_kol_research/models.py",
        HARNESS_RUNTIME_TOPOLOGY="split",
        HARNESS_PRODUCTION_SCHEMA_BOOTSTRAP_FAIL="1",
    )

    assert result.returncode == 4
    assert (state / "head").read_text().strip() == PREVIOUS
    assert (state / "branch").read_text().strip() == PREVIOUS
    events = _events(log)
    assert events.count("production-schema-bootstrap") == 1
    bootstrap = events.index("production-schema-bootstrap")
    assert "start" not in events
    assert events.index("rollback-start") > bootstrap


@pytest.mark.parametrize(
    "changed_path",
    ("src/telegram_kol_research/web.py", "docs/server-deployment.md"),
)
def test_nonschema_paths_skip_all_schema_work(
    updater_harness,
    changed_path: str,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_CHANGED_PATH=changed_path)

    assert result.returncode == 0, result.stderr
    events = _events(log)
    assert "online-backup" not in events
    assert "disposable-migration" not in events
    assert "watermark-compare" not in events
    assert "production-schema-bootstrap" not in events


@pytest.mark.parametrize(
    "fault",
    (
        "HARNESS_SCHEMA_BACKUP_FAIL",
        "HARNESS_BACKUP_CHECK_FAIL",
        "HARNESS_MIGRATION_FAIL",
        "HARNESS_MIGRATION_CHECK_FAIL",
        "HARNESS_WATERMARK_FAIL",
    ),
)
def test_schema_fault_never_reaches_active_check_or_stop(
    updater_harness,
    fault: str,
) -> None:
    run, log, _ = updater_harness

    result = run(
        HARNESS_CHANGED_PATH="src/telegram_kol_research/models.py",
        **{fault: "1"},
    )

    assert result.returncode == 4
    events = _events(log)
    assert "active-check-1" not in events
    assert "stop" not in events


@pytest.mark.parametrize(
    ("fault", "value"),
    (
        ("HARNESS_REMOTE_HEAD", OTHER),
        ("HARNESS_FETCH_RC", "1"),
        ("HARNESS_FLOCK_FAIL", "1"),
        ("HARNESS_STATUS_FAIL", "1"),
        ("HARNESS_DIRTY", "1"),
        ("HARNESS_CURRENT_BRANCH", "codex/wrong"),
        ("HARNESS_BRANCH_HEAD", OTHER),
        ("HARNESS_WORKTREE_FAIL", "1"),
        ("HARNESS_DIFF_ERROR", "1"),
    ),
)
def test_precondition_fault_never_stops_or_mutates(
    updater_harness,
    fault: str,
    value: str,
) -> None:
    run, log, _ = updater_harness

    result = run(**{fault: value})

    assert result.returncode == 4
    events = _events(log)
    assert "stop" not in events
    assert "checkout" not in events


def test_failed_worktree_add_removes_the_exact_empty_stage_directory(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(HARNESS_WORKTREE_FAIL="1")

    assert result.returncode == 4
    assert not list(log.parent.glob("telegram-kol-stage.*"))


@pytest.mark.parametrize(
    "fault",
    (
        "HARNESS_CHECKOUT_FAIL",
        "HARNESS_MERGE_FAIL",
        "HARNESS_PIP_FAIL",
        "HARNESS_CANDIDATE_START_FAIL",
        "HARNESS_CANDIDATE_ACTIVE_FAIL",
        "HARNESS_HTTP_FAIL",
        "HARNESS_DURABLE_INSTALL_FAIL",
        "HARNESS_DURABLE_MOVE_FAIL",
    ),
)
def test_poststop_fault_restores_old_checkout_package_updater_and_service(
    updater_harness,
    fault: str,
) -> None:
    run, log, state = updater_harness

    result = run(**{fault: "1"})

    assert result.returncode == 4
    assert (state / "head").read_text().strip() == PREVIOUS
    assert (state / "branch").read_text().strip() == PREVIOUS
    events = _events(log)
    assert "rollback-start" in events
    assert "rollback-is-active" in events
    if fault in {"HARNESS_DURABLE_MOVE_FAIL"}:
        assert "updater-restore" in events


def test_rollback_waits_for_inactive_before_restoring_files(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(
        HARNESS_PIP_FAIL="1",
        HARNESS_ROLLBACK_STOP_MODE="deactivating_once",
    )

    assert result.returncode != 0
    events = _events(log)
    rollback_stop = len(events) - 1 - events[::-1].index("stop")
    deactivating = events.index("deactivating", rollback_stop + 1)
    inactive = events.index("inactive", deactivating + 1)
    checkout = events.index("rollback-checkout")
    assert rollback_stop < deactivating < inactive < checkout


def test_rollback_never_restores_files_while_unit_is_deactivating(
    updater_harness,
) -> None:
    run, log, state = updater_harness

    result = run(
        HARNESS_PIP_FAIL="1",
        HARNESS_ROLLBACK_STOP_MODE="stuck",
    )

    assert result.returncode == 4
    events = _events(log)
    assert "deactivating" in events
    assert "rollback-checkout" not in events
    assert (state / "head").read_text().strip() == CANDIDATE
    assert "ROLLBACK FAILED" in result.stderr


@pytest.mark.parametrize(
    "fault",
    (
        "HARNESS_ROLLBACK_CHECKOUT_FAIL",
        "HARNESS_ROLLBACK_REF_FAIL",
        "HARNESS_ROLLBACK_PIP_FAIL",
        "HARNESS_ROLLBACK_START_FAIL",
        "HARNESS_ROLLBACK_ACTIVE_FAIL",
        "HARNESS_UPDATER_RESTORE_FAIL",
    ),
)
def test_rollback_failure_is_hard_failure(
    updater_harness,
    fault: str,
) -> None:
    run, _, _ = updater_harness

    result = run(HARNESS_DURABLE_MOVE_FAIL="1", **{fault: "1"})

    assert result.returncode == 4
    assert "ROLLBACK FAILED" in result.stderr


@pytest.mark.parametrize(
    ("fault", "extra"),
    (
        ("HARNESS_WORKTREE_REMOVE_FAIL", {}),
        (
            "HARNESS_FINAL_DRY_RUN_REMOVE_FAIL",
            {"HARNESS_CHANGED_PATH": "src/telegram_kol_research/models.py"},
        ),
    ),
)
def test_final_cleanup_failure_rolls_back_instead_of_applying_candidate(
    updater_harness,
    fault: str,
    extra: dict[str, str],
) -> None:
    run, _, state = updater_harness

    result = run(**{fault: "1"}, **extra)

    assert result.returncode == 4
    assert (state / "head").read_text().strip() == PREVIOUS
    assert (state / "branch").read_text().strip() == PREVIOUS


def test_backup_cleanup_failure_is_best_effort_after_finalization(
    updater_harness,
) -> None:
    run, log, state = updater_harness

    result = run(HARNESS_FINAL_BACKUP_REMOVE_FAIL="1")

    assert result.returncode == 0
    assert (state / "head").read_text().strip() == CANDIDATE
    assert (state / "branch").read_text().strip() == CANDIDATE
    assert (log.parent / "durable-updater").read_text() == "candidate updater\n"
    assert list(log.parent.glob("telegram-kol-updater-backup.*"))


def test_hard_rollback_failure_preserves_recovery_materials(
    updater_harness,
) -> None:
    run, log, _ = updater_harness

    result = run(
        HARNESS_DURABLE_MOVE_FAIL_AFTER="1",
        HARNESS_UPDATER_RESTORE_FAIL="1",
    )

    assert result.returncode == 4
    assert "ROLLBACK FAILED" in result.stderr
    assert list(log.parent.glob("telegram-kol-updater-backup.*"))
    assert list(log.parent.glob("telegram-kol-stage.*"))
    assert "worktree-remove" not in _events(log)
    assert (log.parent / "durable-updater").read_text() == "candidate updater\n"


@pytest.mark.parametrize(("signal", "expected"), (("TERM", 143), ("INT", 130)))
def test_signal_during_final_cleanup_rolls_back_candidate(
    updater_harness,
    signal: str,
    expected: int,
) -> None:
    run, _, state = updater_harness

    result = run(HARNESS_WORKTREE_REMOVE_SIGNAL=signal)

    assert result.returncode == expected
    assert (state / "head").read_text().strip() == PREVIOUS
    assert (state / "branch").read_text().strip() == PREVIOUS


@pytest.mark.parametrize("signal", ("TERM", "INT"))
def test_signal_during_backup_cleanup_never_removes_durable_updater(
    updater_harness,
    signal: str,
) -> None:
    run, log, state = updater_harness

    result = run(HARNESS_BACKUP_REMOVE_SIGNAL=signal)

    assert (state / "backup_remove_signaled").exists()
    if signal == "TERM":
        assert result.returncode != 0
    assert (state / "head").read_text().strip() == CANDIDATE
    assert (state / "branch").read_text().strip() == CANDIDATE
    assert (log.parent / "durable-updater").read_text() == "candidate updater\n"


def test_http_health_is_bounded_and_uses_required_endpoint() -> None:
    source = (ROOT / "deploy/telegram-kol-update").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8000/api/trading-settings" in source
    assert "--max-time 2" in source
    assert "-o /dev/null" in source
    assert "20" in source
    assert "sleep 0.5" in source
