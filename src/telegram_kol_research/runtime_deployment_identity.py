from __future__ import annotations

from datetime import UTC, datetime
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Mapping


_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(slots=True)
class RuntimeAuthorityStatus:
    """Process-local proof from successful authority-owning worker cycles."""

    max_age_seconds: float = 90.0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _management_at: datetime | None = field(default=None, init=False)
    _break_even_at: datetime | None = field(default=None, init=False)
    _reconcile_at: datetime | None = field(default=None, init=False)
    _management_enabled: bool = field(default=False, init=False)
    _rescue_enabled: bool = field(default=False, init=False)
    _management_ok: bool = field(default=False, init=False)
    _break_even_ok: bool = field(default=False, init=False)
    _reconcile_ok: bool = field(default=False, init=False)

    def record_management_cycle(
        self,
        *,
        management_enabled: bool,
        rescue_enabled: bool,
        observed_at: datetime,
    ) -> None:
        with self._lock:
            self._management_at = observed_at.astimezone(UTC)
            self._management_enabled = management_enabled is True
            self._rescue_enabled = rescue_enabled is True
            self._management_ok = True

    def record_management_failure(self, *, observed_at: datetime) -> None:
        with self._lock:
            self._management_at = observed_at.astimezone(UTC)
            self._management_ok = False

    def record_break_even_cycle(self, *, observed_at: datetime) -> None:
        with self._lock:
            self._break_even_at = observed_at.astimezone(UTC)
            self._break_even_ok = True

    def record_break_even_failure(self, *, observed_at: datetime) -> None:
        with self._lock:
            self._break_even_at = observed_at.astimezone(UTC)
            self._break_even_ok = False

    def record_reconcile_cycle(self, *, observed_at: datetime) -> None:
        with self._lock:
            self._reconcile_at = observed_at.astimezone(UTC)
            self._reconcile_ok = True

    def record_reconcile_failure(self, *, observed_at: datetime) -> None:
        with self._lock:
            self._reconcile_at = observed_at.astimezone(UTC)
            self._reconcile_ok = False

    def snapshot(self, *, now: datetime) -> dict[str, bool]:
        observed_now = now.astimezone(UTC)
        with self._lock:
            management_at = self._management_at
            break_even_at = self._break_even_at
            reconcile_at = self._reconcile_at
            management_enabled = self._management_enabled
            rescue_enabled = self._rescue_enabled
            management_ok = self._management_ok
            break_even_ok = self._break_even_ok
            reconcile_ok = self._reconcile_ok

        def fresh(value: datetime | None) -> bool:
            if value is None or value > observed_now:
                return False
            return (observed_now - value).total_seconds() <= self.max_age_seconds

        management = management_ok and management_enabled and fresh(management_at)
        reconciliation = reconcile_ok and fresh(reconcile_at)
        protection = (
            management
            and rescue_enabled
            and break_even_ok
            and fresh(break_even_at)
            and reconciliation
        )
        rescue = management and rescue_enabled and reconciliation
        return {
            "management": management,
            "protection": protection,
            "close": management,
            "tpsl": management,
            "rescue": rescue,
        }

    def evidence(self, *, now: datetime) -> dict[str, Any]:
        """Expose redacted freshness inputs behind the capability projection."""

        observed_now = now.astimezone(UTC)
        with self._lock:
            rows = {
                "management_cycle": (
                    self._management_at,
                    self._management_ok,
                    {
                        "effective_management_enabled": self._management_enabled,
                        "effective_rescue_enabled": self._rescue_enabled,
                    },
                ),
                "break_even_cycle": (
                    self._break_even_at,
                    self._break_even_ok,
                    {},
                ),
                "reconcile_cycle": (
                    self._reconcile_at,
                    self._reconcile_ok,
                    {},
                ),
            }
        projected: dict[str, Any] = {
            "max_age_seconds": float(self.max_age_seconds),
        }
        for name, (observed_at, successful, extra) in rows.items():
            age_seconds = None
            if observed_at is not None and observed_at <= observed_now:
                age_seconds = max(
                    0.0,
                    (observed_now - observed_at).total_seconds(),
                )
            projected[name] = {
                "age_seconds": age_seconds,
                "fresh": (
                    age_seconds is not None
                    and age_seconds <= self.max_age_seconds
                ),
                "successful": successful,
                **extra,
            }
        management = projected["management_cycle"]
        break_even = projected["break_even_cycle"]
        reconcile = projected["reconcile_cycle"]
        projected["close_cycle"] = dict(management)
        projected["tpsl_cycle"] = dict(management)
        projected["protection_cycle"] = {
            "age_seconds": max(
                value
                for value in (
                    management["age_seconds"],
                    break_even["age_seconds"],
                    reconcile["age_seconds"],
                )
                if value is not None
            ) if all(
                row["age_seconds"] is not None
                for row in (management, break_even, reconcile)
            ) else None,
            "fresh": all(
                row["fresh"] is True
                for row in (management, break_even, reconcile)
            ),
            "successful": all(
                row["successful"] is True
                for row in (management, break_even, reconcile)
            ),
        }
        projected["rescue_cycle"] = {
            "age_seconds": max(
                value
                for value in (
                    management["age_seconds"],
                    reconcile["age_seconds"],
                )
                if value is not None
            ) if all(
                row["age_seconds"] is not None
                for row in (management, reconcile)
            ) else None,
            "fresh": all(
                row["fresh"] is True for row in (management, reconcile)
            ),
            "successful": all(
                row["successful"] is True
                for row in (management, reconcile)
            ),
        }
        return projected


def _task_running(task: Any) -> bool:
    if task is None:
        return False
    try:
        return not task.done() and not task.cancelled()
    except Exception:
        return False


def validate_runtime_authority_scope(
    identities: Mapping[str, Mapping[str, Any]],
) -> None:
    """Prove exactly one worker owner from bounded numeric cycle evidence."""

    owners: list[str] = []
    for role, identity in identities.items():
        capabilities = identity.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise ValueError("runtime authority evidence invalid")
        if capabilities.get("global_exchange_authority") is True:
            owners.append(role)
    if owners != ["worker"]:
        raise ValueError("runtime authority evidence invalid")
    worker = identities.get("worker")
    if not isinstance(worker, Mapping):
        raise ValueError("runtime authority evidence invalid")
    capabilities = worker.get("capabilities")
    required = (
        "global_exchange_authority",
        "management",
        "protection",
        "close",
        "tpsl",
        "rescue",
    )
    if not isinstance(capabilities, Mapping) or any(
        capabilities.get(name) is not True for name in required
    ):
        raise ValueError("runtime authority evidence invalid")
    evidence = worker.get("authority_evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("runtime authority evidence invalid")
    max_age = evidence.get("max_age_seconds")
    if (
        isinstance(max_age, bool)
        or not isinstance(max_age, (int, float))
        or not 1 <= float(max_age) <= 300
    ):
        raise ValueError("runtime authority evidence invalid")
    for name in ("management_cycle", "break_even_cycle", "reconcile_cycle"):
        cycle = evidence.get(name)
        age = cycle.get("age_seconds") if isinstance(cycle, Mapping) else None
        if (
            not isinstance(cycle, Mapping)
            or cycle.get("fresh") is not True
            or cycle.get("successful") is not True
            or isinstance(age, bool)
            or not isinstance(age, (int, float))
            or not 0 <= float(age) <= float(max_age)
        ):
            raise ValueError("runtime authority evidence invalid")
    management = evidence["management_cycle"]
    if (
        management.get("effective_management_enabled") is not True
        or management.get("effective_rescue_enabled") is not True
    ):
        raise ValueError("runtime authority evidence invalid")


def read_self_process_start_ticks() -> int | None:
    try:
        raw = Path("/proc/self/stat").read_text(encoding="ascii")
        suffix = raw[raw.rindex(")") + 2 :].split()
        value = int(suffix[19])
    except (OSError, UnicodeError, ValueError, IndexError):
        return None
    return value if value > 0 else None


def _loaded_release_evidence(
    *,
    module_path: Path,
    expected_commit: str,
    expected_manifest_sha256: str,
) -> tuple[bool, str | None, str | None]:
    if (
        not _SHA1_RE.fullmatch(expected_commit)
        or not _SHA256_RE.fullmatch(expected_manifest_sha256)
    ):
        return False, None, None
    try:
        resolved = module_path.resolve(strict=True)
        package_dir = resolved.parent
        source_dir = package_dir.parent
        release = source_dir.parent
        if (
            package_dir.name != "telegram_kol_research"
            or source_dir.name != "src"
            or release.name != expected_commit
        ):
            return False, None, None
        manifest_path = release / ".telegram-kol-release.json"
        encoded = manifest_path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != expected_manifest_sha256:
            return False, None, None
        payload = json.loads(encoded.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("contract") != "immutable-release-v1"
            or payload.get("schema_version") != 1
            or payload.get("commit") != expected_commit
        ):
            return False, None, None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, None, None
    return True, expected_commit, expected_manifest_sha256


def build_runtime_deployment_identity(
    *,
    runtime_role: str,
    command_role: str | None = None,
    loaded_cwd: str | Path | None = None,
    module_path: str | Path,
    expected_commit: str,
    expected_manifest_sha256: str,
    tasks: Mapping[str, Any],
    authority_snapshot: Mapping[str, Any] | None = None,
    authority_evidence: Mapping[str, Any] | None = None,
    process_start_ticks: int | None = None,
    entry_admission_frozen: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    verified, release_commit, manifest_sha = _loaded_release_evidence(
        module_path=Path(module_path),
        expected_commit=expected_commit,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    observed_command_role = str(command_role or runtime_role)
    observed_cwd = str(
        Path(loaded_cwd) if loaded_cwd is not None else Path.cwd()
    )
    if observed_command_role != runtime_role or not Path(observed_cwd).is_absolute():
        verified = False
        release_commit = None
        manifest_sha = None
    observed_authority = authority_snapshot or {}
    management = (
        _task_running(tasks.get("strategy_management_worker"))
        and observed_authority.get("management") is True
    )
    protection = all(
        _task_running(tasks.get(name))
        for name in (
            "strategy_management_worker",
            "break_even_convergence_worker",
            "deepcoin_reconcile",
            "lifecycle_monitor",
        )
    ) and observed_authority.get("protection") is True
    rescue = (
        _task_running(tasks.get("worker_command_worker"))
        and observed_authority.get("rescue") is True
    )
    worker_owner = runtime_role in {"all", "worker"} and management and protection and rescue
    if not verified:
        worker_owner = management = protection = rescue = False
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    start_ticks = (
        process_start_ticks
        if process_start_ticks is not None
        else read_self_process_start_ticks()
    )
    message_processing_running = _task_running(
        tasks.get("message_processing_worker")
    )
    effective_entry_freeze = (
        entry_admission_frozen is True and not message_processing_running
    )
    return {
        "contract": "runtime-deployment-identity-v1",
        "runtime_role": runtime_role,
        "command_role": observed_command_role,
        "loaded_cwd": observed_cwd,
        "release_commit": release_commit,
        "manifest_sha256": manifest_sha,
        "loaded_artifact_verified": verified,
        "pid": os.getpid(),
        "process_start_ticks": start_ticks,
        "observed_at": observed_at.isoformat(),
        "entry_admission_frozen": effective_entry_freeze,
        "authority_evidence": dict(authority_evidence or {}),
        "health": {
            "event_loop": True,
            "ingest_live_listener": _task_running(tasks.get("live_listener")),
            "ingest_reconcile": _task_running(tasks.get("reconcile")),
            "worker_command": _task_running(tasks.get("worker_command_worker")),
            "message_processing": message_processing_running,
        },
        "capabilities": {
            "global_exchange_authority": worker_owner,
            "management": management,
            "protection": protection,
            "close": management and observed_authority.get("close") is True,
            "tpsl": management and observed_authority.get("tpsl") is True,
            "rescue": rescue,
        },
    }


def main() -> int:
    payload = build_runtime_deployment_identity(
        runtime_role=os.environ.get("TELEGRAM_KOL_RUNTIME_ROLE", "unknown"),
        module_path=Path(__file__),
        expected_commit=os.environ.get("TELEGRAM_KOL_RELEASE_COMMIT", ""),
        expected_manifest_sha256=os.environ.get(
            "TELEGRAM_KOL_RELEASE_MANIFEST_SHA256", ""
        ),
        tasks={},
    )
    if payload["loaded_artifact_verified"] is not True:
        return 4
    print(
        json.dumps(
            {
                "contract": payload["contract"],
                "loaded_artifact_verified": True,
                "release_commit": payload["release_commit"],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
