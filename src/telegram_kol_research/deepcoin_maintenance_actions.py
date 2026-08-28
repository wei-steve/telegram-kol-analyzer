"""Fail-closed coordinators for explicit Deepcoin maintenance actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Iterable, Protocol

from telegram_kol_research.entry_authority_seed import (
    SeedPlanRefused,
    SeedResult,
    apply_entry_authority_seed_plan,
)
from telegram_kol_research.reviewed_pending_entry_cancel import (
    ReviewedPendingEntryCancelPlan,
    ReviewedPendingEntryCancelResult,
    ReviewedPendingEntryTarget,
    apply_reviewed_pending_entry_cancel_plan,
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


class DrainGuard(Protocol):
    def enter(self, *, action_id: str) -> object: ...

    def prove_quiescent(self) -> None: ...

    def block(self, *, reason_code: str) -> object: ...


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
    session_factory,
    plan: ReviewedPendingEntryCancelPlan,
    request: SingleOrderDrainRequest,
    deepcoin_client,
    targets: Iterable[ReviewedPendingEntryTarget],
    guard: DrainGuard,
    now: datetime,
) -> ReviewedPendingEntryCancelResult:
    """Dispatch one already planned exact drain; never iterate over targets."""

    matching = [
        action
        for action in plan.actions
        if action.order_id == request.order_id
        and action.action_id == request.action_id
    ]
    if len(matching) != 1:
        raise ValueError("exactly one drain action must match the request")
    if plan.fingerprint != request.plan_sha256:
        raise ValueError("drain plan hash mismatch")
    if plan.evidence_fingerprint != request.evidence_sha256:
        raise ValueError("drain evidence hash mismatch")
    return apply_reviewed_pending_entry_cancel_plan(
        session_factory,
        plan,
        deepcoin_client=deepcoin_client,
        targets=tuple(targets),
        order_id=request.order_id,
        action_id=request.action_id,
        expected_fingerprint=request.plan_sha256,
        expected_evidence_fingerprint=request.evidence_sha256,
        confirmation_token=request.confirmation_token,
        guard=guard,
        now=now,
    )


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
