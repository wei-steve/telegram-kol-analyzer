"""Fail-closed coordinators for explicit Deepcoin maintenance actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Protocol

from telegram_kol_research.entry_revision_exchange_authority import (
    acquire_entry_revision_exchange_authority,
    block_entry_revision_exchange_authority,
    release_entry_revision_exchange_authority,
)

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


@dataclass(slots=True)
class EntryRevisionBootstrapAuthorityAdapter:
    """Bind bootstrap orchestration to the exact v2 generation-CAS row."""

    session_factory: Any
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def acquire_bootstrap(self, plan: Any):
        return acquire_entry_revision_exchange_authority(
            self.session_factory,
            owner_kind="immutable_control_bootstrap",
            owner_id=f"bootstrap:{plan.action_id}",
            acquired_at=_observed_now(self.clock),
            require_cancel_quiescence=False,
            expected_generation=int(plan.expected_generation),
            action_id=str(plan.action_id),
            plan_sha256=str(plan.fingerprint),
            evidence_sha256=str(plan.evidence_sha256),
        )

    def release_bootstrap(
        self,
        *,
        token: str,
        generation: int,
        released_at: datetime,
    ) -> bool:
        result = release_entry_revision_exchange_authority(
            self.session_factory,
            token=token,
            owner_kind="immutable_control_bootstrap",
            expected_generation=generation,
            released_at=released_at,
        )
        return bool(result.released)

    def block_bootstrap(
        self,
        *,
        token: str,
        generation: int,
        reason_code: str,
        blocked_at: datetime,
    ) -> None:
        result = block_entry_revision_exchange_authority(
            self.session_factory,
            token=token,
            owner_kind="immutable_control_bootstrap",
            expected_generation=generation,
            reason_code=reason_code,
            blocked_at=blocked_at,
        )
        if not result.blocked:
            raise MaintenanceBlocked(
                result.reason_code or "bootstrap_authority_block_unknown"
            )

    def no_exchange_write_round_trip(
        self,
        plan: Any,
        *,
        expected_generation: int,
    ) -> int:
        acquired_at = _observed_now(self.clock)
        acquisition = acquire_entry_revision_exchange_authority(
            self.session_factory,
            owner_kind="authority_self_test",
            owner_id=f"bootstrap-self-test:{plan.action_id}",
            acquired_at=acquired_at,
            require_cancel_quiescence=False,
            expected_generation=int(expected_generation),
            action_id=str(plan.action_id),
            plan_sha256=str(plan.fingerprint),
            evidence_sha256=str(plan.evidence_sha256),
        )
        if not acquisition.acquired or not acquisition.token:
            raise MaintenanceBlocked(
                acquisition.reason_code or "authority_self_test_acquire_unknown"
            )
        generation = int(acquisition.generation or -1)
        released_at = _observed_now(self.clock)
        released = release_entry_revision_exchange_authority(
            self.session_factory,
            token=acquisition.token,
            owner_kind="authority_self_test",
            expected_generation=generation,
            released_at=released_at,
        )
        if released.released:
            return generation
        blocked = block_entry_revision_exchange_authority(
            self.session_factory,
            token=acquisition.token,
            owner_kind="authority_self_test",
            expected_generation=generation,
            reason_code="authority_self_test_release_unknown",
            blocked_at=_observed_now(self.clock),
        )
        if not blocked.blocked:
            raise MaintenanceBlocked("authority_self_test_compensation_failed")
        raise MaintenanceBlocked(
            released.reason_code or "authority_self_test_release_unknown"
        )


def run_entry_authority_seed(
    *,
    action_id: str,
    database_path: Path,
    backup_path: Path,
    expected_fingerprint: str,
    guard: SeedActionGuard,
    now: datetime,
    authorization_expires_at: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SeedResult:
    """Coordinate persistent inhibition around one exact L3 seed plan."""

    receipt = guard.enter(action_id=action_id)
    try:
        guard.prove_quiescent()
        if authorization_expires_at is not None and _observed_now(clock) >= (
            _aware_timestamp(authorization_expires_at)
        ):
            raise SeedPlanRefused("maintenance_authorization_expired")
        result = apply_entry_authority_seed_plan(
            database_path,
            backup_path=backup_path,
            expected_fingerprint=expected_fingerprint,
            guard=guard,
            now=now,
            authorization_expires_at=authorization_expires_at,
            clock=clock,
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
    authorization_expires_at: datetime,
    now: datetime | None = None,
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
        authorization_expires_at=authorization_expires_at,
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
        reason = str(exc)
        if reason in {
            "maintenance_restore_failed",
            "maintenance_restore_compensation_failed",
        }:
            raise MaintenanceBlocked(reason) from exc
        try:
            guard.block(reason_code="entry_authority_seed_runtime_restore_failed")
        except Exception:
            pass
        raise MaintenanceBlocked(
            "entry_authority_seed_runtime_restore_failed"
        ) from exc


def _observed_now(clock: Callable[[], datetime] | None) -> datetime:
    return _aware_timestamp((clock or (lambda: datetime.now(UTC)))())


def _aware_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("maintenance authorization timestamp is invalid")
    return value.astimezone(UTC)
