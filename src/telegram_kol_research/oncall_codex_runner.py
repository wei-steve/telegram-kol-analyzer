"""The root-side loop that runs ``codex exec`` and writes the answer back.

Phase 2 of the Codex on-call remediation program
(``docs/plans/2026-09-20-codex-oncall-phase2-spec.md``, sections 3, 6.2 and 7).

Started as ``python -B -m telegram_kol_research.oncall_codex_runner`` -- never
through ``cli.py``, which would import the whole trading application into a
root process. Its entire import closure is the standard library plus
``oncall_codex``, and a test asserts exactly that.

**What this process can see.** Its systemd unit (section 7) gives it an empty
capability set and a whitelist namespace: the deployed source, the Python
runtime, the CA bundle, ``/root/.codex`` and the spool. It cannot see the
production database, any ``*.env``, ``config/``, ``data/`` or any other
project on the machine. So the only thing that can reach OpenAI is the case
file the watcher put in the spool, plus source code that is already public to
the person running this.

**What this process trusts.** Nothing in the spool. Directory names must match
``case-<digits>``; fixed file names are opened with ``O_NOFOLLOW`` so a
swapped symlink fails instead of redirecting a read; anything over 128 KB is
refused; the request is parsed field by field. The prompt is a module constant
in ``oncall_codex`` and no part of the case file is ever interpolated into it
or into the command line.

**What this process writes.** ``verdict.json`` and ``run.json`` in the case
directory, and ``health.json`` at the spool root -- all write-then-rename, so
the watcher never reads half a file. It never writes anywhere else, and the
verdict it hands back is re-validated in full on the other side.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

from telegram_kol_research.oncall_codex import (
    DEFAULT_CODEX_BIN,
    DEFAULT_SPOOL,
    FILE_CASE,
    FILE_HEALTH,
    FILE_REQUEST,
    FILE_RUN,
    FILE_SCHEMA,
    FILE_VERDICT,
    FILE_VERDICT_RAW,
    PROMPT_VERSION,
    RUN_FAILED,
    RUN_OK,
    SCHEMA_VERSION,
    CODEX_HOME,
    FAILURE_CONTRACT,
    FAILURE_TIMEOUT,
    RequestContractError,
    RunRecord,
    atomic_write,
    build_child_environment,
    build_codex_command,
    case_id_from_dir,
    classify_failure,
    extract_verdict_payload,
    parse_request,
    read_bounded_file,
    read_json_file,
    request_fingerprint,
    scrub_detail,
)


logger = logging.getLogger("telegram_kol_research.oncall_codex_runner")

BEIJING = ZoneInfo("Asia/Shanghai")

ENV_CODEX_BIN = "TELEGRAM_KOL_ONCALL_CODEX_BIN"
ENV_SPOOL = "TELEGRAM_KOL_ONCALL_CODEX_SPOOL"

#: How long a ``codex login status`` may take. It is a local file check.
LOGIN_STATUS_TIMEOUT = 30
#: The once-a-day proof that the credentials still buy an answer.
MINIMAL_EXEC_PROMPT = "Reply with exactly: OK"
MINIMAL_EXEC_TIMEOUT = 120

LOGIN_CHECK_INTERVAL = timedelta(hours=6)

#: One scan never looks at more than this many directories.
MAX_CASE_DIRS_PER_SCAN = 50

POLL_SECONDS = 10

#: SIGTERM first, then this long, then SIGKILL -- to the whole process group,
#: because ``codex`` spawns its own sandbox helpers and a lone SIGKILL to the
#: parent would leave them running.
KILL_GRACE_SECONDS = 5.0


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# Running codex
# --------------------------------------------------------------------------


class CodexOutcome:
    """One ``codex exec`` attempt: what came back and how long it took."""

    __slots__ = ("returncode", "output", "timed_out", "duration", "answer")

    def __init__(
        self,
        *,
        returncode: int,
        output: str,
        timed_out: bool,
        duration: float,
        answer: str,
    ):
        self.returncode = int(returncode)
        self.output = str(output)
        self.timed_out = bool(timed_out)
        self.duration = float(duration)
        self.answer = str(answer)


def run_command(
    command: Sequence[str],
    *,
    timeout: float,
    env: dict[str, str],
    cwd: Path | None = None,
) -> tuple[int, str, bool, float]:
    """Run a child in its own process group and kill the group on timeout."""

    started = time.monotonic()
    process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(env),
        cwd=str(cwd) if cwd is not None else None,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=float(timeout))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - already SIGKILLed
            stdout, stderr = "", ""
    duration = time.monotonic() - started
    return process.returncode or 0, f"{stderr}\n{stdout}", timed_out, duration


def _kill_process_group(process: subprocess.Popen) -> None:
    """SIGTERM the group, wait, then SIGKILL it -- unconditionally.

    Both signals go to the *group* because ``codex`` spawns its own sandbox
    helpers, and the SIGKILL is not skipped just because the parent has
    already died: a helper that outlived its parent is exactly the process
    this is here to reap.
    """

    try:
        group = os.getpgid(process.pid)
    except (OSError, ProcessLookupError):  # pragma: no cover - already gone
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return
    deadline = time.monotonic() + KILL_GRACE_SECONDS
    while time.monotonic() < deadline and process.poll() is None:
        time.sleep(0.05)
    try:
        os.killpg(group, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        return


def run_codex_exec(
    *,
    codex_bin: str,
    case_dir: Path,
    timeout: float,
    env: dict[str, str],
) -> CodexOutcome:
    """One diagnosis call. The answer is read back from ``verdict.raw.json``."""

    output_path = case_dir / FILE_VERDICT_RAW
    try:
        output_path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - a read-only spool is a bigger problem
        pass
    command = build_codex_command(
        codex_bin=codex_bin,
        case_dir=case_dir,
        schema_path=case_dir / FILE_SCHEMA,
        output_path=output_path,
    )
    returncode, output, timed_out, duration = run_command(
        command, timeout=timeout, env=env, cwd=case_dir
    )
    answer = ""
    if output_path.exists():
        try:
            answer = read_bounded_file(output_path).strip()
        except (OSError, RequestContractError):
            answer = ""
    return CodexOutcome(
        returncode=returncode,
        output=output,
        timed_out=timed_out,
        duration=duration,
        answer=answer,
    )


def codex_version(codex_bin: str, env: dict[str, str]) -> str | None:
    try:
        returncode, output, _timed_out, _duration = run_command(
            [str(codex_bin), "--version"], timeout=20, env=env
        )
    except OSError:
        return None
    if returncode != 0:
        return None
    return " ".join(output.split())[:64] or None


# --------------------------------------------------------------------------
# One case
# --------------------------------------------------------------------------


def process_case_dir(
    case_dir: Path,
    *,
    codex_bin: str,
    env: dict[str, str],
    now: datetime,
    version: str | None = None,
) -> RunRecord | None:
    """Answer one request, or return ``None`` when there is nothing to do."""

    case_id = case_id_from_dir(case_dir.name)
    if case_id is None:
        return None
    request_path = case_dir / FILE_REQUEST
    try:
        raw_request = read_bounded_file(request_path)
    except FileNotFoundError:
        return None
    except (OSError, RequestContractError) as exc:
        logger.warning("oncall runner rejected %s: %s", case_dir.name, exc)
        return None

    fingerprint = request_fingerprint(raw_request.encode("utf-8"))
    existing = _existing_run(case_dir)
    if existing is not None and existing.get("request_fingerprint") == fingerprint:
        return None

    try:
        payload = json.loads(raw_request)
        request = parse_request(payload, case_id=case_id)
    except (json.JSONDecodeError, RequestContractError, ValueError) as exc:
        logger.warning("oncall runner rejected request %s: %s", case_dir.name, exc)
        return _write_result(
            case_dir,
            RunRecord(
                case_id=case_id,
                attempt=0,
                status=RUN_FAILED,
                failure_class=FAILURE_CONTRACT,
                request_fingerprint=fingerprint,
                prompt_version=PROMPT_VERSION,
                duration_seconds=0.0,
                finished_at=now.isoformat(),
                detail=scrub_detail(str(exc)),
            ),
            verdict=None,
        )

    try:
        # Read only to prove it is a regular, bounded, non-symlinked file the
        # sandbox will be able to open; the content is never parsed here.
        read_bounded_file(case_dir / FILE_CASE)
        read_bounded_file(case_dir / FILE_SCHEMA)
    except (OSError, RequestContractError) as exc:
        logger.warning("oncall runner rejected case file %s: %s", case_dir.name, exc)
        return _write_result(
            case_dir,
            RunRecord(
                case_id=case_id,
                attempt=request.attempt,
                status=RUN_FAILED,
                failure_class=FAILURE_CONTRACT,
                request_fingerprint=fingerprint,
                prompt_version=PROMPT_VERSION,
                duration_seconds=0.0,
                finished_at=now.isoformat(),
                detail=scrub_detail(str(exc)),
            ),
            verdict=None,
        )

    outcome = run_codex_exec(
        codex_bin=codex_bin,
        case_dir=case_dir,
        timeout=request.timeout_seconds,
        env=env,
    )

    if outcome.timed_out or outcome.returncode != 0 or not outcome.answer:
        if outcome.timed_out:
            failure_class = FAILURE_TIMEOUT
        elif outcome.returncode != 0:
            failure_class = classify_failure(
                outcome.output, returncode=outcome.returncode
            )
        else:
            # Exit 0 with nothing written is Codex answering with silence: a
            # broken contract, not an outage, so it must not count towards
            # ``codex_down`` and take the diagnosis layer dark.
            failure_class = FAILURE_CONTRACT
        return _write_result(
            case_dir,
            RunRecord(
                case_id=case_id,
                attempt=request.attempt,
                status=RUN_FAILED,
                failure_class=failure_class,
                request_fingerprint=fingerprint,
                prompt_version=request.prompt_version,
                duration_seconds=outcome.duration,
                finished_at=now.isoformat(),
                codex_version=version,
                detail=scrub_detail(outcome.output),
            ),
            verdict=None,
        )

    try:
        verdict_payload = extract_verdict_payload(outcome.answer)
    except (json.JSONDecodeError, ValueError) as exc:
        return _write_result(
            case_dir,
            RunRecord(
                case_id=case_id,
                attempt=request.attempt,
                status=RUN_FAILED,
                failure_class=FAILURE_CONTRACT,
                request_fingerprint=fingerprint,
                prompt_version=request.prompt_version,
                duration_seconds=outcome.duration,
                finished_at=now.isoformat(),
                codex_version=version,
                detail=scrub_detail(f"{exc}: {outcome.answer}"),
            ),
            verdict=None,
        )

    # The answer is still untrusted: the watcher validates every field of it
    # before a word of it reaches a person. All this side claims is "codex
    # returned a JSON document".
    return _write_result(
        case_dir,
        RunRecord(
            case_id=case_id,
            attempt=request.attempt,
            status=RUN_OK,
            failure_class=None,
            request_fingerprint=fingerprint,
            prompt_version=request.prompt_version,
            duration_seconds=outcome.duration,
            finished_at=now.isoformat(),
            codex_version=version,
            detail="",
        ),
        verdict=verdict_payload,
    )


def _existing_run(case_dir: Path) -> dict | None:
    try:
        payload = read_json_file(case_dir / FILE_RUN)
    except (OSError, RequestContractError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_result(case_dir: Path, record: RunRecord, *, verdict) -> RunRecord:
    """``verdict.json`` first, ``run.json`` last: the watcher polls for the run."""

    if verdict is not None:
        atomic_write(
            case_dir / FILE_VERDICT,
            json.dumps(verdict, ensure_ascii=False, sort_keys=True),
        )
    atomic_write(
        case_dir / FILE_RUN,
        json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True),
    )
    logger.info(
        "oncall runner finished case=%s attempt=%s status=%s failure=%s seconds=%.1f",
        record.case_id,
        record.attempt,
        record.status,
        record.failure_class,
        record.duration_seconds,
    )
    return record


# --------------------------------------------------------------------------
# Health self-check (spec 6.5)
# --------------------------------------------------------------------------


def run_login_status(*, codex_bin: str, env: dict[str, str]) -> tuple[bool, str]:
    try:
        returncode, output, timed_out, _duration = run_command(
            [str(codex_bin), "login", "status"],
            timeout=LOGIN_STATUS_TIMEOUT,
            env=env,
        )
    except OSError as exc:
        return False, type(exc).__name__
    if timed_out:
        return False, "timeout"
    ok = returncode == 0 and "logged in" in output.lower()
    return ok, " ".join(output.split())[:200]


def run_minimal_exec(
    *, codex_bin: str, env: dict[str, str], workdir: Path
) -> tuple[bool, str]:
    """The once-a-day proof that the credentials still buy a real answer."""

    workdir.mkdir(parents=True, exist_ok=True)
    output_path = workdir / "health.out.txt"
    output_path.unlink(missing_ok=True)
    command = [
        str(codex_bin),
        "exec",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
        "-C",
        str(workdir),
        "-o",
        str(output_path),
        MINIMAL_EXEC_PROMPT,
    ]
    returncode, output, timed_out, _duration = run_command(
        command, timeout=MINIMAL_EXEC_TIMEOUT, env=env, cwd=workdir
    )
    if timed_out:
        return False, "timeout"
    answer = ""
    if output_path.exists():
        try:
            answer = output_path.read_text(encoding="utf-8").strip()
        except OSError:  # pragma: no cover
            answer = ""
    if returncode != 0 or "OK" not in answer:
        return False, " ".join(output.split())[:200]
    return True, answer[:64]


def maybe_run_health_check(
    *,
    spool: Path,
    codex_bin: str,
    env: dict[str, str],
    now: datetime,
    state: dict,
    force_login: bool = False,
) -> dict | None:
    """``login status`` every six hours, one real call per Beijing day.

    Returns the health payload when a check ran, ``None`` when it was not yet
    due. The split is deliberate (design 4.2, tightened 2026-09-19): the cheap
    check is local and costs no tokens, so it may run often; the one that
    actually spends tokens runs once a day. ``force_login`` is for start-up,
    and deliberately does *not* force the paid call -- a unit that restarts
    would otherwise spend a token every time it came back.
    """

    last_login = _parse_iso(state.get("last_login_check_at"))
    login_due = (
        force_login or last_login is None or (now - last_login) >= LOGIN_CHECK_INTERVAL
    )
    today = now.astimezone(BEIJING).strftime("%Y-%m-%d")
    exec_due = state.get("last_exec_date") != today
    if not login_due and not exec_due:
        return None

    payload: dict = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": now.isoformat(),
        "prompt_version": PROMPT_VERSION,
    }
    login_ok, login_detail = run_login_status(codex_bin=codex_bin, env=env)
    payload["login_ok"] = login_ok
    payload["login_detail"] = scrub_detail(login_detail, limit=200)
    state["last_login_check_at"] = now.isoformat()

    if exec_due and login_ok:
        exec_ok, exec_detail = run_minimal_exec(
            codex_bin=codex_bin, env=env, workdir=spool / "_health"
        )
        payload["exec_ok"] = exec_ok
        payload["exec_detail"] = scrub_detail(exec_detail, limit=200)
        payload["exec_date"] = today
        state["last_exec_date"] = today
    else:
        payload["exec_ok"] = None
        payload["exec_detail"] = ""

    available = bool(login_ok) and payload.get("exec_ok") is not False
    payload["available"] = available
    if not available:
        payload["failure_class"] = classify_failure(
            f"{login_detail}\n{payload.get('exec_detail', '')}",
            returncode=1,
        )
    else:
        payload["failure_class"] = None
    atomic_write(
        spool / FILE_HEALTH, json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )
    return payload


def _restore_health_state(spool: Path) -> dict:
    """Carry the day's paid check across a restart, so it stays one a day."""

    try:
        payload = read_json_file(spool / FILE_HEALTH)
    except (OSError, RequestContractError):
        return {}
    if not isinstance(payload, dict):
        return {}
    state: dict = {}
    if payload.get("checked_at"):
        state["last_login_check_at"] = str(payload["checked_at"])
    if payload.get("exec_date"):
        state["last_exec_date"] = str(payload["exec_date"])
    return state


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------


def scan_once(
    *,
    spool: Path,
    codex_bin: str,
    env: dict[str, str],
    now: datetime,
    version: str | None = None,
) -> list[RunRecord]:
    """Answer every pending request in the spool. One at a time, in order."""

    records: list[RunRecord] = []
    try:
        names = sorted(entry.name for entry in os.scandir(spool) if entry.is_dir())
    except OSError as exc:
        logger.warning("oncall runner cannot read the spool: %s", type(exc).__name__)
        return records
    for name in names[:MAX_CASE_DIRS_PER_SCAN]:
        if case_id_from_dir(name) is None:
            continue
        case_dir = Path(spool) / name
        try:
            record = process_case_dir(
                case_dir, codex_bin=codex_bin, env=env, now=now, version=version
            )
        except Exception:  # noqa: BLE001 - one bad case never stops the loop
            logger.exception("oncall runner failed on %s", name)
            continue
        if record is not None:
            records.append(record)
    return records


def run_runner_loop(
    *,
    spool: Path,
    codex_bin: str = DEFAULT_CODEX_BIN,
    poll_seconds: int = POLL_SECONDS,
    once: bool = False,
    clock: Callable[[], datetime] = _now,
    sleeper: Callable[[float], None] = time.sleep,
    parent_env: dict[str, str] | None = None,
    home: str = CODEX_HOME,
) -> dict:
    """The runner's main loop. One bad round never ends the process."""

    spool_path = Path(spool)
    spool_path.mkdir(parents=True, exist_ok=True)
    env = build_child_environment(
        parent_env if parent_env is not None else os.environ, home=home
    )
    version = codex_version(codex_bin, env)
    state = _restore_health_state(spool_path)
    rounds = 0
    answered = 0
    while True:
        rounds += 1
        now = clock()
        try:
            maybe_run_health_check(
                spool=spool_path,
                codex_bin=codex_bin,
                env=env,
                now=now,
                state=state,
                force_login=rounds == 1,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:  # noqa: BLE001
            logger.exception("oncall runner health check failed")
        try:
            answered += len(
                scan_once(
                    spool=spool_path,
                    codex_bin=codex_bin,
                    env=env,
                    now=now,
                    version=version,
                )
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:  # noqa: BLE001
            logger.exception("oncall runner round failed")
        if once:
            break
        sleeper(max(1, int(poll_seconds)))
    return {"rounds": rounds, "answered": answered, "codex_version": version}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="oncall-codex-runner",
        description="Answer on-call diagnosis requests from the spool.",
    )
    parser.add_argument(
        "--spool",
        default=os.environ.get(ENV_SPOOL) or DEFAULT_SPOOL,
        help="the shared spool directory",
    )
    parser.add_argument(
        "--codex-bin",
        default=os.environ.get(ENV_CODEX_BIN) or DEFAULT_CODEX_BIN,
        help="path to the codex executable",
    )
    parser.add_argument("--poll-seconds", type=int, default=POLL_SECONDS)
    parser.add_argument("--once", action="store_true", help="one scan, then exit")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    summary = run_runner_loop(
        spool=Path(args.spool),
        codex_bin=args.codex_bin,
        poll_seconds=args.poll_seconds,
        once=args.once,
    )
    logger.info("oncall runner stopped rounds=%s answered=%s", summary["rounds"], summary["answered"])
    return 0


if __name__ == "__main__":  # pragma: no cover - the unit's entry point
    sys.exit(main())
