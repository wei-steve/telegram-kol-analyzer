"""Fail-closed bridge from the legacy worker to leased cancellation authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any, Iterable

from telegram_kol_research.models import StrategyRevisionBatch, TradingSetting


LEGACY_RUNTIME_DRAIN_BRIDGE_KEY = "legacy_runtime_drain_bridge"
_SCHEMA_VERSION = 1
_STATES = frozenset(
    {
        "frozen",
        "fenced",
        "cancelling",
        "unknown_locked",
        "drained",
        "released_for_deploy",
    }
)
_DOCUMENT_KEYS = frozenset(
    {
        "schema_version",
        "state",
        "bridge_token",
        "production_sha",
        "worker_pid",
        "worker_start_ticks",
        "frozen_at",
        "freeze_raw_message_id",
        "original_auto_trade_enabled",
        "original_entry_revision_v2_mode",
        "reviewed_order_ids",
        "fenced_batch_ids",
        "completed_order_ids",
        "write_boundary_reached",
        "updated_at",
    }
)
_TERMINAL_REVISION_BATCH_STATES = frozenset(
    {"succeeded", "blocked", "failed", "recovery_required"}
)
_SHA = re.compile(r"[0-9a-f]{40}")
_ORDER_ID = re.compile(r"[0-9]{1,64}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True, slots=True)
class LegacyRuntimeIdentity:
    production_sha: str
    worker_pid: int
    worker_start_ticks: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "production_sha",
            _sha(self.production_sha, field_name="production_sha"),
        )
        if isinstance(self.worker_pid, bool) or int(self.worker_pid) <= 0:
            raise ValueError("worker_pid must be positive")
        if (
            isinstance(self.worker_start_ticks, bool)
            or int(self.worker_start_ticks) <= 0
        ):
            raise ValueError("worker_start_ticks must be positive")
        object.__setattr__(self, "worker_pid", int(self.worker_pid))
        object.__setattr__(
            self, "worker_start_ticks", int(self.worker_start_ticks)
        )


@dataclass(frozen=True, slots=True)
class LegacyRuntimeDrainBridgePlan:
    mode: str
    state: str
    fingerprint: str
    conflicts: tuple[dict[str, str], ...]
    fenced_batch_ids: tuple[int, ...]
    completed_order_ids: tuple[str, ...]


def build_legacy_runtime_drain_bridge_plan(
    session_factory,
    *,
    runtime_identity: LegacyRuntimeIdentity,
    expected_production_sha: str,
    reviewed_order_ids: Iterable[str],
    planned_at: datetime,
) -> LegacyRuntimeDrainBridgePlan:
    """Build one deterministic read-only projection of the legacy bridge."""

    expected_sha = _sha(
        expected_production_sha,
        field_name="expected_production_sha",
    )
    reviewed = _reviewed_order_ids(reviewed_order_ids)
    observed_at = _aware_utc(planned_at, field_name="planned_at")
    conflicts: list[dict[str, str]] = []
    state = "absent"
    fenced_batch_ids: tuple[int, ...] = ()
    completed_order_ids: tuple[str, ...] = ()
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == LEGACY_RUNTIME_DRAIN_BRIDGE_KEY)
            .one_or_none()
        )
        document = None if row is None else _bridge_document(row.value_json)
        if row is not None and document is None:
            state = "invalid"
            conflicts.append({"reason": "legacy_bridge_state_invalid"})
        elif document is not None:
            state = str(document["state"])
            fenced_batch_ids = tuple(document["fenced_batch_ids"])
            completed_order_ids = tuple(document["completed_order_ids"])
            if document["production_sha"] != expected_sha:
                conflicts.append({"reason": "legacy_bridge_production_sha_drift"})
            if (
                document["worker_pid"] != runtime_identity.worker_pid
                or document["worker_start_ticks"]
                != runtime_identity.worker_start_ticks
            ):
                conflicts.append({"reason": "legacy_bridge_worker_identity_drift"})
            if tuple(document["reviewed_order_ids"]) != reviewed:
                conflicts.append({"reason": "legacy_bridge_reviewed_set_drift"})

        active_batch_ids = tuple(
            int(row_id)
            for (row_id,) in session.query(StrategyRevisionBatch.id)
            .filter(
                StrategyRevisionBatch.status.not_in(
                    _TERMINAL_REVISION_BATCH_STATES
                )
            )
            .order_by(StrategyRevisionBatch.id.asc())
            .all()
        )

    payload = {
        "active_batch_ids": list(active_batch_ids),
        "completed_order_ids": list(completed_order_ids),
        "expected_production_sha": expected_sha,
        "fenced_batch_ids": list(fenced_batch_ids),
        "planned_at": observed_at.isoformat(),
        "reviewed_order_ids": list(reviewed),
        "runtime_identity": {
            "production_sha": runtime_identity.production_sha,
            "worker_pid": runtime_identity.worker_pid,
            "worker_start_ticks": runtime_identity.worker_start_ticks,
        },
        "state": state,
    }
    return LegacyRuntimeDrainBridgePlan(
        mode="dry_run",
        state=state,
        fingerprint=hashlib.sha256(_canonical_json(payload).encode()).hexdigest(),
        conflicts=tuple(conflicts),
        fenced_batch_ids=fenced_batch_ids,
        completed_order_ids=completed_order_ids,
    )


def _bridge_document(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != _DOCUMENT_KEYS:
        return None
    if value.get("schema_version") != _SCHEMA_VERSION:
        return None
    if value.get("state") not in _STATES:
        return None
    if not isinstance(value.get("bridge_token"), str) or not _TOKEN.fullmatch(
        value["bridge_token"]
    ):
        return None
    try:
        production_sha = _sha(
            value.get("production_sha"), field_name="production_sha"
        )
        frozen_at = _aware_utc_text(value.get("frozen_at"))
        updated_at = _aware_utc_text(value.get("updated_at"))
        reviewed = _reviewed_order_ids(value.get("reviewed_order_ids", ()))
        fenced = _positive_unique_ints(value.get("fenced_batch_ids"))
        completed = _completed_order_ids(
            value.get("completed_order_ids"), reviewed=reviewed
        )
    except ValueError:
        return None
    worker_pid = value.get("worker_pid")
    worker_start_ticks = value.get("worker_start_ticks")
    watermark = value.get("freeze_raw_message_id")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < minimum
        for item, minimum in (
            (worker_pid, 1),
            (worker_start_ticks, 1),
            (watermark, 0),
        )
    ):
        return None
    if type(value.get("original_auto_trade_enabled")) is not bool:
        return None
    if value.get("original_entry_revision_v2_mode") not in {
        "disabled",
        "shadow",
        "live",
    }:
        return None
    if type(value.get("write_boundary_reached")) is not bool:
        return None
    return {
        **value,
        "production_sha": production_sha,
        "frozen_at": frozen_at.isoformat(),
        "updated_at": updated_at.isoformat(),
        "reviewed_order_ids": list(reviewed),
        "fenced_batch_ids": list(fenced),
        "completed_order_ids": list(completed),
    }


def _sha(value: Any, *, field_name: str) -> str:
    text = str(value or "")
    if not _SHA.fullmatch(text):
        raise ValueError(f"{field_name} must be a lowercase full SHA")
    return text


def _reviewed_order_ids(values: Iterable[str]) -> tuple[str, ...]:
    try:
        result = tuple(str(value) for value in values)
    except TypeError as exc:
        raise ValueError("reviewed_order_ids must be iterable") from exc
    if (
        len(result) != 7
        or len(set(result)) != len(result)
        or any(not _ORDER_ID.fullmatch(value) for value in result)
    ):
        raise ValueError("reviewed_order_ids must contain seven unique ids")
    return result


def _positive_unique_ints(values: Any) -> tuple[int, ...]:
    if not isinstance(values, list):
        raise ValueError("batch ids must be a list")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise ValueError("batch ids must be positive integers")
    if len(values) != len(set(values)) or values != sorted(values):
        raise ValueError("batch ids must be sorted and unique")
    return tuple(values)


def _completed_order_ids(
    values: Any,
    *,
    reviewed: tuple[str, ...],
) -> tuple[str, ...]:
    if not isinstance(values, list):
        raise ValueError("completed order ids must be a list")
    completed = tuple(str(value) for value in values)
    if (
        len(completed) != len(set(completed))
        or any(value not in reviewed for value in completed)
        or list(completed) != sorted(completed)
    ):
        raise ValueError("completed order ids must be a sorted reviewed subset")
    return completed


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _aware_utc_text(value: Any) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("timestamp must be bounded text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp is invalid") from exc
    return _aware_utc(parsed, field_name="timestamp")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
