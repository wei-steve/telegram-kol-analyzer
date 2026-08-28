"""Fail-closed coordinators for explicit Deepcoin maintenance actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Protocol

from telegram_kol_research.entry_authority_seed import (
    SeedPlanRefused,
    SeedResult,
    apply_entry_authority_seed_plan,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class MaintenanceBlocked(RuntimeError):
    """The action crossed a boundary that forbids automatic recovery."""


@dataclass(frozen=True, slots=True)
class SingleOrderDrainRequest:
    action_id: str
    order_id: str
    plan_sha256: str
    evidence_sha256: str
    confirmation_token: str

    def __post_init__(self) -> None:
        for field_name in ("action_id", "order_id", "confirmation_token"):
            value = str(getattr(self, field_name) or "").strip()
            if not value or len(value) > 128:
                raise ValueError(f"{field_name} is invalid")
            object.__setattr__(self, field_name, value)
        for field_name in ("plan_sha256", "evidence_sha256"):
            value = str(getattr(self, field_name) or "").strip()
            if not _SHA256.fullmatch(value):
                raise ValueError(f"{field_name} is invalid")
            object.__setattr__(self, field_name, value)


class DrainAuthority(Protocol):
    def acquire(self, request: SingleOrderDrainRequest) -> None: ...

    def mark_write_boundary(self) -> None: ...

    def block(self, *, reason_code: str) -> None: ...


class DrainExchange(Protocol):
    def cancel_trigger_order(self, *, order_id: str) -> object: ...


class DrainGuard(Protocol):
    def enter(self, *, action_id: str) -> object: ...

    def prove_quiescent(self) -> None: ...

    def block(self, *, reason_code: str) -> object: ...


class DrainTerminalizer(Protocol):
    def terminalize(self) -> object: ...


class SeedActionReceipt(Protocol):
    fingerprint: str


class SeedActionGuard(DrainGuard, Protocol):
    def enter(self, *, action_id: str) -> SeedActionReceipt: ...

    def mark_safe_to_restore(
        self,
        *,
        expected_fingerprint: str,
    ) -> SeedActionReceipt: ...

    def restore(self, *, expected_fingerprint: str) -> None: ...


def run_entry_authority_seed(
    *,
    action_id: str,
    database_path: Path,
    backup_path: Path,
    expected_fingerprint: str,
    guard: SeedActionGuard,
    now: datetime,
) -> SeedResult:
    """Coordinate persistent inhibition around one exact L3 seed plan."""

    receipt = guard.enter(action_id=action_id)
    try:
        guard.prove_quiescent()
        result = apply_entry_authority_seed_plan(
            database_path,
            backup_path=backup_path,
            expected_fingerprint=expected_fingerprint,
            guard=guard,
            now=now,
        )
    except SeedPlanRefused:
        _restore_seed_guard(guard=guard, receipt=receipt)
        raise
    except Exception as exc:
        try:
            guard.block(reason_code="entry_authority_seed_action_unknown")
        except Exception:
            pass
        raise MaintenanceBlocked("entry_authority_seed_action_unknown") from exc
    if result.status == "blocked":
        raise MaintenanceBlocked(result.reason_code or "entry_authority_seed_blocked")
    _restore_seed_guard(guard=guard, receipt=receipt)
    return result


def run_single_order_drain(
    *,
    request: SingleOrderDrainRequest,
    authority: DrainAuthority,
    exchange: DrainExchange,
    guard: DrainGuard,
    terminalizer: DrainTerminalizer,
) -> None:
    """Prove the post-write unknown boundary before implementing success."""

    guard.enter(action_id=request.action_id)
    guard.prove_quiescent()
    authority.acquire(request)
    authority.mark_write_boundary()
    try:
        exchange.cancel_trigger_order(order_id=request.order_id)
    except Exception as exc:
        _block_after_possible_write(
            authority=authority,
            guard=guard,
            reason_code="exchange_outcome_unknown",
        )
        raise MaintenanceBlocked("exchange_outcome_unknown") from exc

    # Task 5 will add exact exchange confirmation and local terminalization.
    # Until then, a returned response is not sufficient evidence of success.
    _ = terminalizer
    _block_after_possible_write(
        authority=authority,
        guard=guard,
        reason_code="single_order_drain_success_path_unimplemented",
    )
    raise MaintenanceBlocked("single_order_drain_success_path_unimplemented")


def _block_after_possible_write(
    *,
    authority: DrainAuthority,
    guard: DrainGuard,
    reason_code: str,
) -> None:
    authority_error: Exception | None = None
    try:
        authority.block(reason_code=reason_code)
    except Exception as exc:  # pragma: no cover - covered with v2 authority
        authority_error = exc
    try:
        guard.block(reason_code=reason_code)
    except Exception as guard_error:  # pragma: no cover - guard tests own this
        if authority_error is None:
            authority_error = guard_error
    if authority_error is not None:
        raise MaintenanceBlocked(reason_code) from authority_error


def _restore_seed_guard(
    *,
    guard: SeedActionGuard,
    receipt: SeedActionReceipt,
) -> None:
    try:
        safe_receipt = guard.mark_safe_to_restore(
            expected_fingerprint=receipt.fingerprint,
        )
        guard.restore(expected_fingerprint=safe_receipt.fingerprint)
    except Exception as exc:
        try:
            guard.block(reason_code="entry_authority_seed_runtime_restore_failed")
        except Exception:
            pass
        raise MaintenanceBlocked(
            "entry_authority_seed_runtime_restore_failed"
        ) from exc
