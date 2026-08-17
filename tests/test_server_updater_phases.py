from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS = "1" * 40
CANDIDATE = "2" * 40


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def updater_harness(tmp_path):
    application = tmp_path / "app"
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    for directory in (
        application / ".git",
        application / ".venv/bin",
        application / "data",
        application / "deploy",
        fake_bin,
        state,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    database = application / "data/research.db"
    database.write_bytes(b"sqlite")
    log = tmp_path / "events.log"

    _write_executable(
        fake_bin / "git",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf 'git %s\n' "$*" >>"$HARNESS_LOG"
if [ "${1:-}" = "-C" ]; then shift 2; fi
case "${1:-}" in
  fetch) exit 0 ;;
  rev-parse)
    case "${2:-}" in
      FETCH_HEAD) printf '%s\n' "$HARNESS_CANDIDATE" ;;
      HEAD) if [ -f "$HARNESS_STATE/head" ]; then cat "$HARNESS_STATE/head"; else printf '%s\n' "$HARNESS_PREVIOUS"; fi ;;
      refs/heads/*) if [ -f "$HARNESS_STATE/branch" ]; then cat "$HARNESS_STATE/branch"; else printf '%s\n' "$HARNESS_PREVIOUS"; fi ;;
      *) printf '%s\n' "$HARNESS_CANDIDATE" ;;
    esac
    ;;
  worktree)
    if [ "${2:-}" = "add" ]; then mkdir -p "${4}"; else rm -rf -- "${4:-}"; fi
    ;;
  checkout)
    if [ "${2:-}" = "--detach" ]; then
      printf '%s\n' "$3" >"$HARNESS_STATE/head"
    else
      if [ -f "$HARNESS_STATE/branch" ]; then cat "$HARNESS_STATE/branch" >"$HARNESS_STATE/head"; fi
    fi
    ;;
  merge)
    printf '%s\n' "$3" >"$HARNESS_STATE/head"
    printf '%s\n' "$3" >"$HARNESS_STATE/branch"
    ;;
  update-ref)
    printf '%s\n' "$3" >"$HARNESS_STATE/branch"
    ;;
esac
''',
    )
    _write_executable(
        fake_bin / "systemctl",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl %s\n' "$*" >>"$HARNESS_LOG"
case "${1:-}" in
  is-active)
    [ ! -f "$HARNESS_STATE/stopped" ]
    ;;
  stop)
    if [ "${HARNESS_STOP_STAYS_ACTIVE:-0}" != "1" ]; then touch "$HARNESS_STATE/stopped"; fi
    ;;
  start)
    if [ "${HARNESS_START_FAIL_ONCE:-0}" = "1" ] && [ ! -f "$HARNESS_STATE/start_failed" ]; then
      touch "$HARNESS_STATE/start_failed"
      exit 1
    fi
    rm -f "$HARNESS_STATE/stopped"
    ;;
  status|--no-pager) printf 'active\n' ;;
esac
''',
    )
    _write_executable(
        application / ".venv/bin/python",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf 'python %s\n' "$*" >>"$HARNESS_LOG"
if [[ " $* " = *" -m pip install "* ]]; then
  if [ "${HARNESS_PIP_FAIL_ONCE:-0}" = "1" ] && [ ! -f "$HARNESS_STATE/pip_failed" ]; then
    touch "$HARNESS_STATE/pip_failed"
    exit 1
  fi
  exit 0
fi
if [[ " $* " = *"telegram_kol_research.deployment_preflight_cli"* ]] || [[ " $* " = *" deployment-preflight "* ]] || [[ " $* " = *" verify-deployment-preflight "* ]]; then
  phase=final
  output=""
  previous=""
  for value in "$@"; do
    if [ "$previous" = "--phase" ]; then phase="$value"; fi
    if [ "$previous" = "--output" ]; then output="$value"; fi
    previous="$value"
  done
  [[ "$output" != *preliminary* ]] || phase=preliminary
  if [ -n "$output" ]; then
    mkdir -p "$(dirname "$output")"
    printf '{}\n' >"$output"
  fi
  if [[ " $* " = *" verify "* ]] || [[ " $* " = *" verify-deployment-preflight "* ]]; then exit 0; fi
  if [ "$phase" = "preliminary" ]; then exit "${HARNESS_PRELIMINARY_RESULT:-0}"; fi
  exit "${HARNESS_FINAL_RESULT:-0}"
fi
exit 0
''',
    )
    (application / ".venv/bin/activate").write_text(
        'PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd):$PATH"\n'
        "export PATH\n",
        encoding="utf-8",
    )
    _write_executable(
        fake_bin / "install",
        r'''#!/usr/bin/env bash
set -euo pipefail
printf 'install %s\n' "$*" >>"$HARNESS_LOG"
if [[ " $* " = *" -d "* ]] || [ "${1:-}" = "-d" ]; then exec /usr/bin/install "$@"; fi
exit 0
''',
    )
    _write_executable(
        fake_bin / "flock",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _write_executable(
        fake_bin / "mv",
        '#!/usr/bin/env bash\nprintf \'mv %s\\n\' "$*" >>"$HARNESS_LOG"\nexit 0\n',
    )

    def run(**overrides: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{fake_bin}:{environment['PATH']}",
                "APP_DIR": str(application),
                "DATABASE_PATH": str(database),
                "PREFLIGHT_DIR": str(tmp_path / "preflight"),
                "LOCK_PATH": str(tmp_path / "update.lock"),
                "STAGE_PARENT": str(tmp_path),
                "EXPECTED_COMMIT": CANDIDATE,
                "CHANGE_CLASS": "code",
                "BRANCH": "codex/test",
                "STOP_TIMEOUT_SECONDS": "0",
                "HARNESS_LOG": str(log),
                "HARNESS_STATE": str(state),
                "HARNESS_PREVIOUS": PREVIOUS,
                "HARNESS_CANDIDATE": CANDIDATE,
            }
        )
        environment.update(overrides)
        return subprocess.run(
            ["bash", str(ROOT / "deploy/telegram-kol-update")],
            text=True,
            capture_output=True,
            check=False,
            env=environment,
        )

    return run, log, state


def _events(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines()


def test_preliminary_block_never_stops_service(updater_harness):
    run, log, _ = updater_harness
    result = run(HARNESS_PRELIMINARY_RESULT="3")

    assert result.returncode == 3
    assert not any(line == "systemctl stop telegram-kol.service" for line in _events(log))


def test_final_block_restarts_old_service_without_checkout(updater_harness):
    run, log, state = updater_harness
    result = run(HARNESS_FINAL_RESULT="3")
    events = _events(log)

    assert result.returncode == 3
    assert events.count("systemctl stop telegram-kol.service") >= 1
    assert "systemctl start telegram-kol.service" in events
    assert not any(" checkout " in line for line in events)
    assert not (state / "stopped").exists()


def test_final_pass_mutates_only_after_final_verification(updater_harness):
    run, log, _ = updater_harness
    result = run()
    events = _events(log)

    assert result.returncode == 0, result.stderr
    preliminary = next(i for i, line in enumerate(events) if " collect " in line and " preliminary " in line)
    stop = events.index("systemctl stop telegram-kol.service")
    final = next(i for i, line in enumerate(events) if " collect " in line and " final " in line)
    verify = next(i for i, line in enumerate(events) if " verify " in line and " final " in line)
    checkout = next(i for i, line in enumerate(events) if " checkout codex/test" in line)
    pip_install = next(i for i, line in enumerate(events) if "-m pip install -e" in line)
    updater_install = next(i for i, line in enumerate(events) if line.startswith("install ") and "telegram-kol-update /usr/local/bin/telegram-kol-update.candidate." in line)
    start = max(i for i, line in enumerate(events) if line == "systemctl start telegram-kol.service")
    assert preliminary < stop < final < verify < checkout < pip_install < updater_install < start


def test_stop_timeout_aborts_before_final_collection(updater_harness):
    run, log, _ = updater_harness
    result = run(HARNESS_STOP_STAYS_ACTIVE="1")
    events = _events(log)

    assert result.returncode == 4
    assert not any(" collect " in line and " final " in line for line in events)
    assert "systemctl start telegram-kol.service" in events


@pytest.mark.parametrize("failure", ["HARNESS_PIP_FAIL_ONCE", "HARNESS_START_FAIL_ONCE"])
def test_install_or_start_failure_restores_previous_checkout_and_package(
    updater_harness, failure
):
    run, log, state = updater_harness
    result = run(**{failure: "1"})
    events = _events(log)

    assert result.returncode != 0
    assert (state / "head").read_text(encoding="utf-8").strip() == PREVIOUS
    assert (state / "branch").read_text(encoding="utf-8").strip() == PREVIOUS
    assert sum("-m pip install -e" in line for line in events) >= 2
    assert not (state / "stopped").exists()
