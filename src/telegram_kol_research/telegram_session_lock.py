"""Cross-process locking for a Telethon session file."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import fcntl
except ModuleNotFoundError:
    fcntl = None

try:
    import msvcrt
except ModuleNotFoundError:
    msvcrt = None


class TelegramSessionLockError(RuntimeError):
    """Raised when another process already owns the Telegram session."""


@dataclass(frozen=True)
class TelegramSessionLockOwner:
    """Best-effort details about the process that owns a Telegram session lock."""

    pid: int
    status: str | None
    command: str | None

    def format_for_humans(self) -> str:
        status_text = self.status or "unknown"
        command_text = self.command or "unknown command"
        return f"owner pid={self.pid} status={status_text} command={command_text}"


class TelegramSessionLock:
    """Non-blocking exclusive lock next to a Telethon session file."""

    def __init__(self, session_path: str | Path):
        self.session_path = Path(session_path)
        self.lock_path = self.session_path.with_name(f"{self.session_path.name}.lock")
        self._file = None

    def __enter__(self) -> "TelegramSessionLock":
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.lock_path.open("a+", encoding="utf-8")
        try:
            self._lock_file()
        except (BlockingIOError, OSError) as exc:
            self._file.close()
            self._file = None
            owner = describe_session_lock_owner(self.session_path)
            owner_text = (
                f" {owner.format_for_humans()}." if owner is not None else ""
            )
            raise TelegramSessionLockError(
                f"Telegram session {self.session_path} is already in use by another process. "
                "Close the other web/sync process before starting a new Telegram client."
                f"{owner_text}"
            ) from exc

        self._file.seek(0)
        self._file.truncate()
        self._file.write(f"pid={os.getpid()}\n")
        self._file.flush()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._file is not None:
            self._unlock_file()
            self._file.close()
            self._file = None
        return False

    def _lock_file(self) -> None:
        if fcntl is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        if msvcrt is None:
            raise RuntimeError("No supported file locking implementation is available")
        self._file.seek(0)
        msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(self) -> None:
        if fcntl is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            return
        if msvcrt is not None:
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)


def acquire_telegram_session_lock(session_path: str | Path) -> TelegramSessionLock:
    """Create a lock context for the configured Telethon session file."""

    return TelegramSessionLock(session_path)


def describe_session_lock_owner(
    session_path: str | Path,
    *,
    process_status: Callable[[int], str | None] | None = None,
    process_command: Callable[[int], str | None] | None = None,
) -> TelegramSessionLockOwner | None:
    """Return best-effort owner details from the session lock file."""

    session = Path(session_path)
    lock_path = session.with_name(f"{session.name}.lock")
    pid = _read_lock_pid(lock_path)
    if pid is None:
        return None
    status_reader = process_status or _read_process_status
    command_reader = process_command or _read_process_command
    return TelegramSessionLockOwner(
        pid=pid,
        status=status_reader(pid),
        command=command_reader(pid),
    )


def release_session_lock_owner(
    session_path: str | Path,
    *,
    expected_pid: int,
    current_command: str | None = None,
    process_status: Callable[[int], str | None] | None = None,
    process_command: Callable[[int], str | None] | None = None,
    kill_process: Callable[[int], None] | None = None,
) -> TelegramSessionLockOwner | None:
    """Terminate the lock owner when the caller confirms the exact project PID."""

    owner = describe_session_lock_owner(
        session_path,
        process_status=process_status,
        process_command=process_command,
    )
    if owner is None or owner.pid != expected_pid or owner.pid == os.getpid():
        return None
    active_command = current_command or " ".join([sys.executable, *sys.argv])
    if not _looks_like_same_project_entrypoint(owner.command or "", active_command):
        return None
    terminator = kill_process or _terminate_process
    terminator(owner.pid)
    return owner


def reap_stopped_session_lock_owner(
    session_path: str | Path,
    *,
    current_command: str | None = None,
    process_status: Callable[[int], str | None] | None = None,
    process_command: Callable[[int], str | None] | None = None,
    kill_process: Callable[[int], None] | None = None,
) -> bool:
    """Terminate a stopped stale owner for this project's Telegram session lock.

    This is deliberately conservative: only a stopped process whose command looks
    like this project's telegram-kol-research entrypoint is reaped.
    """

    session = Path(session_path)
    lock_path = session.with_name(f"{session.name}.lock")
    pid = _read_lock_pid(lock_path)
    if pid is None or pid == os.getpid():
        return False

    status_reader = process_status or _read_process_status
    command_reader = process_command or _read_process_command
    terminator = kill_process or _terminate_process
    status = status_reader(pid)
    if status != "T":
        return False
    command = command_reader(pid) or ""
    active_command = current_command or " ".join([sys.executable, *sys.argv])
    if not _looks_like_same_project_entrypoint(command, active_command):
        return False

    terminator(pid)
    return True


def _read_lock_pid(lock_path: Path) -> int | None:
    if not lock_path.exists():
        return None
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("pid="):
            continue
        try:
            return int(line.split("=", 1)[1])
        except ValueError:
            return None
    return None


def _read_process_status(pid: int) -> str | None:
    try:
        output = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "state="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return output.strip()[:1] or None


def _read_process_command(pid: int) -> str | None:
    try:
        output = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return output.strip() or None


def _terminate_process(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)
    if hasattr(signal, "SIGCONT"):
        os.kill(pid, signal.SIGCONT)


def _looks_like_same_project_entrypoint(command: str, current_command: str) -> bool:
    if not (
        "telegram-kol-research" in command
        or "telegram_kol_research.cli" in command
    ):
        return False
    command_parts = set(Path(part).name for part in command.split())
    current_parts = set(Path(part).name for part in current_command.split())
    return (
        bool(command_parts & current_parts)
        or "telegram-kol-research" in current_command
        or "telegram_kol_research.cli" in current_command
    )
