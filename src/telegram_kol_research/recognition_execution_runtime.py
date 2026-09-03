"""Process-wide admission and drain state for exchange-capable recognition work."""

from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from telegram_kol_research.authoritative_execution_attempts import (
    ExecutionOwnerIdentity,
)
from telegram_kol_research.models import (
    AuthoritativeExecutionAttempt,
    EntryAssemblyWakeupExecution,
)


def build_execution_owner_identity(runtime_role: str) -> ExecutionOwnerIdentity:
    if runtime_role not in {"worker", "all"}:
        raise RuntimeError("recognition_execution_not_owned_by_runtime_role")
    pid = os.getpid()
    boot_id = _read_text(Path("/proc/sys/kernel/random/boot_id")) or "unavailable"
    stat = _read_text(Path(f"/proc/{pid}/stat"))
    process_start_ticks = "unavailable"
    if stat:
        # comm may contain spaces and parentheses; fields after the final ')' are stable.
        tail = stat.rsplit(")", 1)[-1].strip().split()
        if len(tail) >= 20:
            process_start_ticks = tail[19]
    return ExecutionOwnerIdentity(
        runtime_role=runtime_role,
        instance_id=uuid.uuid4().hex,
        pid=pid,
        boot_id=boot_id,
        process_start_ticks=process_start_ticks,
        systemd_invocation_id=os.environ.get("INVOCATION_ID") or None,
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


@dataclass(frozen=True)
class RecognitionExecutionRegistrySnapshot:
    accepting: bool
    active: int
    tokens: tuple[str, ...]


class RecognitionExecutionRegistry:
    """Tracks the underlying callable, not the cancellable asyncio waiter."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._accepting = True
        self._tokens: set[str] = set()

    def stop_admission(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def start_admission(self) -> None:
        with self._condition:
            self._accepting = True

    def require_accepting(self) -> None:
        """Fail before a durable claim when process-wide drain has started."""

        with self._condition:
            if not self._accepting:
                raise RuntimeError("recognition_execution_draining")

    @contextmanager
    def admitted(self, token: str):
        token = str(token)
        with self._condition:
            if not self._accepting:
                raise RuntimeError("recognition_execution_draining")
            if token in self._tokens:
                raise RuntimeError("recognition_execution_token_already_active")
            self._tokens.add(token)
        try:
            yield
        finally:
            with self._condition:
                self._tokens.discard(token)
                self._condition.notify_all()

    def wait_drained(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._condition:
            while self._tokens:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def snapshot(self) -> RecognitionExecutionRegistrySnapshot:
        with self._condition:
            return RecognitionExecutionRegistrySnapshot(
                accepting=self._accepting,
                active=len(self._tokens),
                tokens=tuple(sorted(self._tokens)),
            )


def count_owned_durable_executions(session_factory, *, owner_instance_id: str) -> int:
    """Count active main and child fences owned by this exact process instance."""

    with session_factory() as session:
        main = (
            session.query(AuthoritativeExecutionAttempt)
            .filter(
                AuthoritativeExecutionAttempt.owner_instance_id
                == str(owner_instance_id),
                AuthoritativeExecutionAttempt.status.in_(
                    ("claimed", "executing", "outcome_recorded")
                ),
            )
            .count()
        )
        wake = (
            session.query(EntryAssemblyWakeupExecution)
            .filter(
                EntryAssemblyWakeupExecution.owner_instance_id
                == str(owner_instance_id),
                EntryAssemblyWakeupExecution.status.in_(
                    ("claimed", "executing", "outcome_recorded")
                ),
            )
            .count()
        )
        return int(main) + int(wake)


@contextmanager
def periodic_lease_heartbeat(
    callback,
    *,
    interval_seconds: float = 30.0,
):
    """Renew observability in a daemon thread; it grants no execution right."""

    stop = threading.Event()

    def run() -> None:
        while not stop.wait(max(0.1, float(interval_seconds))):
            try:
                callback()
            except Exception:
                # The exact side-effect CAS, not heartbeat freshness, is the
                # permission boundary. Scanner will surface renewal failure.
                return

    thread = threading.Thread(
        target=run,
        name="recognition-execution-heartbeat",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)
