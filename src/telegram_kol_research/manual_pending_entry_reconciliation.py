"""Reconcile pending entries after an operator cancels them at Deepcoin."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Callable, Iterable

from sqlalchemy import text

from telegram_kol_research.deepcoin_maintenance_evidence import (
    DeepcoinMaintenanceEvidenceRefused,
    build_deepcoin_maintenance_evidence,
    deepcoin_order_ids,
    require_fresh_deepcoin_maintenance_evidence,
    require_fresh_deepcoin_maintenance_observed_at,
)
from telegram_kol_research.entry_revision_exchange_authority import (
    ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
    is_canonical_idle_entry_revision_exchange_authority,
)
from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLeg,
    StrategyLifecycle,
    TradingSetting,
    TriggerProtectionIntent,
    TriggerTakeProfitConvergence,
)
from telegram_kol_research.reviewed_pending_entry_targets import (
    ReviewedPendingEntryTarget,
)


_GOVERNED_INSTRUMENTS = (
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
)


@dataclass(frozen=True, slots=True)
class ManualPendingEntryReconciliationPlan:
    status: str
    reason_code: str | None
    target_order_ids: tuple[str, ...]
    evidence_sha256: str
    evidence_observed_at: datetime | None
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ManualPendingEntryReconciliationResult:
    status: str
    terminalized_count: int
    authority_seeded: bool
    backup_path: Path


def build_manual_pending_entry_reconciliation_plan(
    session_factory,
    *,
    deepcoin_client,
    targets: Iterable[ReviewedPendingEntryTarget],
    runtime_guard: Callable[[], None] | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ManualPendingEntryReconciliationPlan:
    """Prove all exchange entries are gone and local targets are exact."""

    observed_at = _timestamp(now) if now is not None else None
    reviewed = tuple(targets)
    order_ids = tuple(target.order_id for target in reviewed)
    if not _canonical_targets_valid(reviewed):
        return _blocked("canonical_target_set_invalid", order_ids)
    if not _runtime_is_stopped(runtime_guard):
        return _blocked("maintenance_runtime_not_stopped", order_ids)
    evidence = build_deepcoin_maintenance_evidence(
        deepcoin_client,
        instruments=_GOVERNED_INSTRUMENTS,
        target_order_id="manual-cancel-all",
        expected_target_pending_count=0,
        observed_at=observed_at,
        clock=clock,
    )
    freshness_now = observed_at or _clock_now(clock)
    try:
        require_fresh_deepcoin_maintenance_evidence(evidence, now=freshness_now)
    except DeepcoinMaintenanceEvidenceRefused:
        return _blocked(
            evidence.reason_code or "exchange_snapshot_unknown",
            order_ids,
            evidence_sha256=evidence.fingerprint,
            evidence_observed_at=evidence.observed_at,
        )
    if evidence.positions:
        return _blocked("live_position_present", order_ids, evidence.fingerprint)
    if evidence.regular_orders:
        return _blocked("regular_order_present", order_ids, evidence.fingerprint)
    if evidence.pending_triggers:
        return _blocked("pending_trigger_present", order_ids, evidence.fingerprint)
    target_by_order_id = {target.order_id: target for target in reviewed}
    target_order_ids = set(target_by_order_id)
    target_client_order_ids = {target.client_order_id for target in reviewed}
    if any(
        deepcoin_order_ids(row, response_kind="fill") & target_order_ids
        or _deepcoin_client_order_ids(row) & target_client_order_ids
        for row in evidence.fills
    ):
        return _blocked("target_fill_present", order_ids, evidence.fingerprint)
    for order_id in order_ids:
        matches = [
            (row, deepcoin_order_ids(row, response_kind="trigger_history"))
            for row in evidence.trigger_history
            if order_id
            in deepcoin_order_ids(row, response_kind="trigger_history")
        ]
        if not matches:
            return _blocked(
                "target_cancelled_history_missing", order_ids, evidence.fingerprint
            )
        if len(matches) != 1:
            return _blocked(
                "target_cancelled_history_not_unique",
                order_ids,
                evidence.fingerprint,
            )
        row, identities = matches[0]
        if identities != {order_id}:
            return _blocked(
                "target_history_identity_conflict", order_ids, evidence.fingerprint
            )
        target = target_by_order_id[order_id]
        if str(row.get("instId") or "").strip() != target.instrument_id:
            return _blocked(
                "target_history_instrument_mismatch",
                order_ids,
                evidence.fingerprint,
            )
        if str(row.get("state") or row.get("status") or "").strip().lower() not in {
            "cancelled",
            "canceled",
        }:
            return _blocked(
                "target_history_not_cancelled", order_ids, evidence.fingerprint
            )

    completed_count = 0
    with session_factory() as session:
        if _reviewed_binding_scope_changed(session, reviewed):
            return _blocked(
                "reviewed_local_state_changed",
                order_ids,
                evidence.fingerprint,
            )
        for target in reviewed:
            if _target_completed(session, target):
                completed_count += 1
                continue
            reason = _target_reason(session, target)
            if reason is not None:
                return _blocked(reason, order_ids, evidence.fingerprint)
        authority = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
            .one_or_none()
        )
        if authority is not None and not (
            is_canonical_idle_entry_revision_exchange_authority(
                authority.value_json
            )
        ):
            return _blocked(
                "entry_revision_exchange_authority_not_idle",
                order_ids,
                evidence.fingerprint,
            )
        if completed_count == len(reviewed) and authority is None:
            return _blocked(
                "entry_revision_exchange_authority_missing",
                order_ids,
                evidence.fingerprint,
            )

    if completed_count not in {0, len(reviewed)}:
        return _blocked(
            "partial_local_terminalization",
            order_ids,
            evidence.fingerprint,
        )
    fingerprint = _fingerprint(
        {
            "evidence_sha256": evidence.fingerprint,
            "targets": [_canonical_target_payload(target) for target in reviewed],
        }
    )
    return ManualPendingEntryReconciliationPlan(
        status="completed" if completed_count == len(reviewed) else "ready",
        reason_code=None,
        target_order_ids=order_ids,
        evidence_sha256=evidence.fingerprint,
        evidence_observed_at=evidence.observed_at,
        fingerprint=fingerprint,
    )


def apply_manual_pending_entry_reconciliation(
    session_factory,
    *,
    database_path: Path,
    backup_path: Path,
    deepcoin_client,
    targets: Iterable[ReviewedPendingEntryTarget],
    expected_fingerprint: str,
    runtime_guard: Callable[[], None] | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> ManualPendingEntryReconciliationResult:
    """Back up SQLite and terminalize all reviewed targets in one transaction."""

    reviewed = tuple(targets)
    fresh = build_manual_pending_entry_reconciliation_plan(
        session_factory,
        deepcoin_client=deepcoin_client,
        targets=reviewed,
        runtime_guard=runtime_guard,
        now=now,
        clock=clock,
    )
    if fresh.status != "ready" or fresh.fingerprint != expected_fingerprint:
        raise ValueError(fresh.reason_code or "manual_reconciliation_plan_drift")
    _require_runtime_stopped(runtime_guard)
    _require_session_database_path(session_factory, Path(database_path))
    _require_write_boundary_freshness(fresh, now=now, clock=clock)
    _create_verified_backup(Path(database_path), Path(backup_path))
    _require_runtime_stopped(runtime_guard)
    observed_at = _require_write_boundary_freshness(fresh, now=now, clock=clock)

    authority_seeded = False
    with session_factory() as session:
        try:
            session.execute(text("BEGIN IMMEDIATE"))
            if _reviewed_binding_scope_changed(session, reviewed):
                raise ValueError("reviewed_local_state_changed")
            for target in reviewed:
                reason = _target_reason(session, target)
                if reason is not None:
                    raise ValueError(reason)
            authority = (
                session.query(TradingSetting)
                .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
                .one_or_none()
            )
            if authority is None:
                session.add(
                    TradingSetting(
                        key=ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
                        value_json=_canonical_json(
                            {
                                "generation": 0,
                                "released_at": observed_at.isoformat(),
                                "schema_version": 2,
                                "state": "idle",
                            }
                        ),
                        updated_at=observed_at,
                    )
                )
                authority_seeded = True
            elif not is_canonical_idle_entry_revision_exchange_authority(
                authority.value_json
            ):
                raise ValueError("entry_revision_exchange_authority_not_idle")

            affected_bindings: set[int] = set()
            for target in reviewed:
                _terminalize_target(session_factory, session, target, observed_at)
                affected_bindings.add(target.execution_binding_id)
            session.flush()
            for binding_id in affected_bindings:
                _terminalize_binding(session, binding_id, observed_at)
            session.flush()
            if any(not _target_completed(session, target) for target in reviewed):
                raise ValueError("local_terminalization_incomplete")
            session.commit()
        except Exception:
            session.rollback()
            raise

    return ManualPendingEntryReconciliationResult(
        status="completed",
        terminalized_count=len(reviewed),
        authority_seeded=authority_seeded,
        backup_path=Path(backup_path),
    )


def _target_reason(session, target: ReviewedPendingEntryTarget) -> str | None:
    leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
    binding = session.get(ExecutionBinding, target.execution_binding_id)
    lifecycle = session.get(StrategyLifecycle, target.lifecycle_id)
    lifecycle_count = (
        session.query(StrategyLifecycle)
        .filter_by(execution_binding_id=target.execution_binding_id)
        .count()
    )
    intents = (
        session.query(TriggerProtectionIntent)
        .filter_by(
            venue="deepcoin",
            execution_order_leg_id=target.execution_order_leg_id,
        )
        .all()
    )
    protection = (
        session.query(PositionProtectionLeg)
        .filter_by(
            venue="deepcoin",
            execution_order_leg_id=target.execution_order_leg_id,
        )
        .all()
    )
    convergence = (
        session.query(TriggerTakeProfitConvergence)
        .filter_by(
            venue="deepcoin",
            execution_order_leg_id=target.execution_order_leg_id,
        )
        .all()
    )
    events = (
        session.query(ExecutionEvent)
        .filter_by(
            action="reconcile_manual_pending_entry_cancel",
            order_id=target.order_id,
        )
        .count()
    )
    if events:
        return "manual_reconciliation_already_recorded"
    request = _request_object(leg.request_json if leg is not None else None)
    symbol = target.instrument_id.split("-", 1)[0]
    expected_side = _side_from_entry_and_stop(target)
    expected_strategy = target.strategy_instance_id
    if not (
        leg is not None
        and binding is not None
        and lifecycle is not None
        and lifecycle_count == 1
        and request is not None
        and expected_side is not None
        and binding.venue == "deepcoin"
        and binding.symbol == symbol
        and binding.side == expected_side
        and binding.chat_id == target.chat_id
        and binding.message_id == target.message_id
        and binding.pos_id is None
        and lifecycle.symbol == symbol
        and lifecycle.side == expected_side
        and lifecycle.chat_id == binding.chat_id
        and lifecycle.message_id == binding.message_id
        and leg.execution_binding_id == target.execution_binding_id
        and leg.venue == "deepcoin"
        and leg.strategy_instance_id == binding.strategy_instance_id
        and binding.strategy_instance_id == expected_strategy
        and leg.order_id == target.order_id
        and leg.client_order_id == target.client_order_id
        and leg.pos_id is None
        and leg.attribution_status == "unassigned"
        and leg.purpose == "entry"
        and leg.order_kind == "trigger_limit"
        and str(leg.status or "").lower() in {"pending", "open", "submitted"}
        and str(binding.status or "").lower() in {"open", "active"}
        and lifecycle.execution_binding_id == target.execution_binding_id
        and lifecycle.lifecycle_status == "pending_entry"
        and _request_matches_target(request, target)
        and len(intents) == 1
        and intents[0].execution_binding_id == target.execution_binding_id
        and intents[0].execution_order_leg_id == target.execution_order_leg_id
        and intents[0].parent_trigger_order_id == target.order_id
        and intents[0].adopted_order_id is None
        and intents[0].request_fingerprint == target.request_fingerprint
        and intents[0].recovery_state in {"pending", "retrying"}
        and _protection_matches(
            protection,
            target,
            allowed_statuses={"planned", "waiting_fill"},
        )
        and len(convergence) == 1
        and convergence[0].execution_binding_id == target.execution_binding_id
        and convergence[0].execution_order_leg_id == target.execution_order_leg_id
        and convergence[0].pos_id is None
        and convergence[0].status
        in {"waiting_backup_stop", "waiting_position", "ready"}
    ):
        return "reviewed_local_state_changed"
    return None


def _target_completed(session, target: ReviewedPendingEntryTarget) -> bool:
    leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
    binding = session.get(ExecutionBinding, target.execution_binding_id)
    lifecycle = session.get(StrategyLifecycle, target.lifecycle_id)
    lifecycle_count = (
        session.query(StrategyLifecycle)
        .filter_by(execution_binding_id=target.execution_binding_id)
        .count()
    )
    intents = (
        session.query(TriggerProtectionIntent)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .all()
    )
    protection = (
        session.query(PositionProtectionLeg)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .all()
    )
    convergence = (
        session.query(TriggerTakeProfitConvergence)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .all()
    )
    events = (
        session.query(ExecutionEvent)
        .filter_by(
            action="reconcile_manual_pending_entry_cancel",
            execution_binding_id=target.execution_binding_id,
            order_id=target.order_id,
            status="confirmed",
        )
        .all()
    )
    request = _request_object(leg.request_json if leg is not None else None)
    symbol = target.instrument_id.split("-", 1)[0]
    expected_side = _side_from_entry_and_stop(target)
    expected_strategy = target.strategy_instance_id
    return bool(
        leg is not None
        and binding is not None
        and lifecycle is not None
        and lifecycle_count == 1
        and request is not None
        and expected_side is not None
        and binding.venue == "deepcoin"
        and binding.symbol == symbol
        and binding.side == expected_side
        and binding.chat_id == target.chat_id
        and binding.message_id == target.message_id
        and binding.strategy_instance_id == expected_strategy
        and binding.pos_id is None
        and lifecycle.id == target.lifecycle_id
        and lifecycle.execution_binding_id == target.execution_binding_id
        and lifecycle.chat_id == binding.chat_id
        and lifecycle.message_id == binding.message_id
        and lifecycle.symbol == symbol
        and lifecycle.side == expected_side
        and leg.execution_binding_id == target.execution_binding_id
        and leg.strategy_instance_id == binding.strategy_instance_id
        and leg.venue == "deepcoin"
        and leg.order_id == target.order_id
        and leg.client_order_id == target.client_order_id
        and leg.pos_id is None
        and leg.attribution_status == "unassigned"
        and leg.purpose == "entry"
        and leg.order_kind == "trigger_limit"
        and leg.status == "cancelled"
        and leg.terminal_reason == "operator_cancelled_unfilled_entry_leg"
        and leg.last_verified_at is not None
        and _request_matches_target(request, target)
        and binding.status == "cancelled"
        and binding.last_exchange_status == "operator_cancelled_pending_entries"
        and lifecycle.lifecycle_status == "expired"
        and lifecycle.exit_reason == "expired"
        and lifecycle.exited_at is not None
        and lifecycle.management_action == "operator_cancelled_pending_entries"
        and lifecycle.expiry_review_next_at is None
        and len(intents) == 1
        and intents[0].execution_binding_id == target.execution_binding_id
        and intents[0].execution_order_leg_id == target.execution_order_leg_id
        and intents[0].parent_trigger_order_id == target.order_id
        and intents[0].adopted_order_id is None
        and intents[0].request_fingerprint == target.request_fingerprint
        and intents[0].recovery_state == "resolved"
        and intents[0].recovery_disposition == "terminal"
        and intents[0].last_reason_code == "parent_trigger_cancelled_before_entry"
        and intents[0].next_attempt_at is None
        and _protection_matches(
            protection,
            target,
            allowed_statuses={"cancelled"},
        )
        and len(convergence) == 1
        and convergence[0].execution_binding_id == target.execution_binding_id
        and convergence[0].execution_order_leg_id == target.execution_order_leg_id
        and convergence[0].pos_id is None
        and convergence[0].status == "completed"
        and convergence[0].reason_code == "parent_trigger_cancelled_before_entry"
        and convergence[0].completed_at is not None
        and _terminal_event_matches(
            events,
            target=target,
            symbol=symbol,
            side=expected_side,
        )
    )


def _terminalize_target(session_factory, session, target, observed_at) -> None:
    leg = session.get(ExecutionOrderLeg, target.execution_order_leg_id)
    leg.status = "cancelled"
    leg.terminal_reason = "operator_cancelled_unfilled_entry_leg"
    leg.last_verified_at = observed_at
    leg.updated_at = observed_at
    intent = (
        session.query(TriggerProtectionIntent)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .one()
    )
    intent.recovery_state = "resolved"
    intent.recovery_disposition = "terminal"
    intent.last_reason_code = "parent_trigger_cancelled_before_entry"
    intent.next_attempt_at = None
    intent.updated_at = observed_at
    for row in (
        session.query(PositionProtectionLeg)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .all()
    ):
        row.status = "cancelled"
        row.updated_at = observed_at
    convergence = (
        session.query(TriggerTakeProfitConvergence)
        .filter_by(venue="deepcoin", execution_order_leg_id=target.execution_order_leg_id)
        .one()
    )
    convergence.status = "completed"
    convergence.reason_code = "parent_trigger_cancelled_before_entry"
    convergence.completed_at = observed_at
    convergence.updated_at = observed_at
    record_execution_event(
        session_factory,
        ExecutionEventRecord(
            action="reconcile_manual_pending_entry_cancel",
            status="confirmed",
            execution_binding_id=target.execution_binding_id,
            venue="deepcoin",
            symbol=target.instrument_id.split("-")[0],
            side=_side_from_entry_and_stop(target),
            order_id=target.order_id,
            reason="operator_confirmed_all_entry_orders_cancelled",
            after={"pending": False, "terminalized": True},
            created_at=observed_at,
        ),
        session=session,
    )


def _terminalize_binding(session, binding_id: int, observed_at: datetime) -> None:
    binding = session.get(ExecutionBinding, binding_id)
    entry_legs = (
        session.query(ExecutionOrderLeg)
        .filter_by(execution_binding_id=binding_id, purpose="entry")
        .all()
    )
    if not entry_legs or any(
        str(row.status or "").lower()
        not in {"cancelled", "canceled", "expired", "rejected"}
        for row in entry_legs
    ):
        return
    binding.status = "cancelled"
    binding.last_exchange_status = "operator_cancelled_pending_entries"
    binding.updated_at = observed_at
    lifecycle = (
        session.query(StrategyLifecycle)
        .filter_by(execution_binding_id=binding_id)
        .one()
    )
    lifecycle.lifecycle_status = "expired"
    lifecycle.exit_reason = "expired"
    lifecycle.exited_at = observed_at
    lifecycle.management_action = "operator_cancelled_pending_entries"
    lifecycle.management_note = "All unfilled entry orders were cancelled at Deepcoin."
    lifecycle.expiry_review_next_at = None
    lifecycle.updated_at = observed_at


def _create_verified_backup(database_path: Path, backup_path: Path) -> None:
    database_path = Path(database_path)
    backup_path = Path(backup_path)
    try:
        source_metadata = database_path.lstat()
    except OSError as exc:
        raise ValueError("backup_source_invalid") from exc
    if not stat.S_ISREG(source_metadata.st_mode) or stat.S_ISLNK(
        source_metadata.st_mode
    ):
        raise ValueError("backup_source_invalid")
    parent_descriptor = None
    try:
        parent_metadata = backup_path.parent.lstat()
    except OSError as exc:
        raise ValueError("backup_parent_unsafe") from exc
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        raise ValueError("backup_parent_unsafe")
    parent_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        parent_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    try:
        parent_descriptor = os.open(backup_path.parent, parent_flags)
        opened_parent = os.fstat(parent_descriptor)
        if (
            opened_parent.st_dev != parent_metadata.st_dev
            or opened_parent.st_ino != parent_metadata.st_ino
        ):
            raise ValueError("backup_parent_unsafe")
        try:
            os.stat(
                backup_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise ValueError("backup_path_invalid")
    except Exception:
        if parent_descriptor is not None:
            os.close(parent_descriptor)
        raise

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(
            backup_path.name,
            flags,
            0o600,
            dir_fd=parent_descriptor,
        )
    except OSError as exc:
        os.close(parent_descriptor)
        raise ValueError("backup_path_invalid") from exc
    created_metadata = None
    try:
        created_metadata = os.fstat(descriptor)
        os.fchmod(descriptor, 0o600)
    except OSError as exc:
        try:
            current = os.stat(
                backup_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                created_metadata is not None
                and current.st_dev == created_metadata.st_dev
                and current.st_ino == created_metadata.st_ino
            ):
                os.unlink(backup_path.name, dir_fd=parent_descriptor)
        except OSError:
            pass
        os.close(descriptor)
        os.close(parent_descriptor)
        raise ValueError("backup_metadata_invalid") from exc
    source = None
    destination = None
    try:
        source_fds_before = _open_fd_inode_count(source_metadata)
        source = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        if _open_fd_inode_count(source_metadata) <= source_fds_before:
            raise ValueError("backup_source_invalid")
        destination = sqlite3.connect(":memory:")
        source.backup(destination)
        result = destination.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise ValueError("backup_quick_check_failed")
        if destination.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("backup_foreign_key_check_failed")
        serialized = destination.serialize()
        if (
            len(serialized) < 100
            or not serialized.startswith(b"SQLite format 3\x00")
            or serialized[18:20] not in {b"\x01\x01", b"\x02\x02"}
        ):
            raise ValueError("backup_quick_check_failed")
        payload_buffer = bytearray(serialized)
        # Online backup has already merged every WAL frame. Mark the standalone
        # snapshot as rollback-journal format so reopening never seeks a WAL.
        payload_buffer[18:20] = b"\x01\x01"
        payload = bytes(payload_buffer)
        os.lseek(descriptor, 0, os.SEEK_SET)
        written = 0
        view = memoryview(payload)
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError("backup write failed")
            written += count
        os.ftruncate(descriptor, len(payload))
        os.fsync(descriptor)
        os.fsync(parent_descriptor)
        persisted = _read_exact_descriptor(descriptor, len(payload))
        verification = sqlite3.connect(":memory:")
        try:
            verification.deserialize(persisted)
            if verification.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ValueError("backup_quick_check_failed")
            if verification.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("backup_foreign_key_check_failed")
        finally:
            verification.close()
        final_source_metadata = database_path.lstat()
        final_metadata = os.stat(
            backup_path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        final_path_metadata = backup_path.lstat()
        descriptor_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(final_source_metadata.st_mode)
            or stat.S_ISLNK(final_source_metadata.st_mode)
            or final_source_metadata.st_dev != source_metadata.st_dev
            or final_source_metadata.st_ino != source_metadata.st_ino
            or not stat.S_ISREG(final_metadata.st_mode)
            or stat.S_ISLNK(final_metadata.st_mode)
            or final_metadata.st_dev != created_metadata.st_dev
            or final_metadata.st_ino != created_metadata.st_ino
            or final_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(final_metadata.st_mode) != 0o600
            or final_path_metadata.st_dev != final_metadata.st_dev
            or final_path_metadata.st_ino != final_metadata.st_ino
            or descriptor_metadata.st_dev != final_metadata.st_dev
            or descriptor_metadata.st_ino != final_metadata.st_ino
            or descriptor_metadata.st_size != len(payload)
        ):
            raise ValueError("backup_metadata_invalid")
    except Exception:
        try:
            current = os.stat(
                backup_path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                current.st_dev == created_metadata.st_dev
                and current.st_ino == created_metadata.st_ino
            ):
                os.unlink(backup_path.name, dir_fd=parent_descriptor)
        except OSError:
            pass
        raise
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()
        os.close(descriptor)
        os.close(parent_descriptor)


def _open_fd_inode_count(metadata) -> int:
    root = Path("/proc/self/fd")
    if not root.is_dir():
        root = Path("/dev/fd")
    count = 0
    try:
        names = tuple(root.iterdir())
    except OSError as exc:
        raise ValueError("backup_source_invalid") from exc
    for path in names:
        try:
            descriptor = int(path.name)
            opened = os.fstat(descriptor)
        except (OSError, ValueError):
            continue
        if opened.st_dev == metadata.st_dev and opened.st_ino == metadata.st_ino:
            count += 1
    return count


def _read_exact_descriptor(descriptor: int, size: int) -> bytes:
    chunks = []
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, size - offset, offset)
        if not chunk:
            raise ValueError("backup_quick_check_failed")
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _canonical_targets_valid(
    reviewed: tuple[ReviewedPendingEntryTarget, ...],
) -> bool:
    if not reviewed:
        return False
    if len({target.order_id for target in reviewed}) != len(reviewed):
        return False
    if len({target.execution_order_leg_id for target in reviewed}) != len(reviewed):
        return False
    for target in reviewed:
        symbol = target.instrument_id.split("-", 1)[0]
        side = _side_from_entry_and_stop(target)
        if (
            side is None
            or target.strategy_instance_id
            != f"deepcoin:{target.chat_id}:{target.message_id}:{symbol}:{side}"
        ):
            return False
    return all(
        len(
            {
                (target.lifecycle_id, target.chat_id, target.message_id, target.strategy_instance_id)
                for target in reviewed
                if target.execution_binding_id == binding_id
            }
        )
        == 1
        for binding_id in {target.execution_binding_id for target in reviewed}
    )


def _canonical_target_payload(target: ReviewedPendingEntryTarget) -> dict[str, object]:
    return {
        "order_id": target.order_id,
        "instrument_id": target.instrument_id,
        "lifecycle_id": target.lifecycle_id,
        "execution_binding_id": target.execution_binding_id,
        "execution_order_leg_id": target.execution_order_leg_id,
        "chat_id": target.chat_id,
        "message_id": target.message_id,
        "strategy_instance_id": target.strategy_instance_id,
        "client_order_id": target.client_order_id,
        "trigger_price": target.trigger_price,
        "size": target.size,
        "embedded_stop_price": target.embedded_stop_price,
        "take_profit_prices": target.take_profit_prices,
        "request_fingerprint": target.request_fingerprint,
    }


def _reviewed_binding_scope_changed(session, reviewed) -> bool:
    reviewed_leg_ids_by_binding: dict[int, set[int]] = {}
    for target in reviewed:
        reviewed_leg_ids_by_binding.setdefault(target.execution_binding_id, set()).add(
            target.execution_order_leg_id
        )
    active_statuses = {"pending", "open", "submitted"}
    for binding_id, reviewed_leg_ids in reviewed_leg_ids_by_binding.items():
        active_leg_ids = {
            row.id
            for row in session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id, purpose="entry")
            .all()
            if str(row.status or "").lower() in active_statuses
        }
        if active_leg_ids - reviewed_leg_ids:
            return True
    return False


def _require_session_database_path(session_factory, database_path: Path) -> None:
    try:
        bound = Path(session_factory.kw["bind"].url.database).resolve(strict=True)
        requested = database_path.resolve(strict=True)
    except (KeyError, OSError, TypeError) as exc:
        raise ValueError("database_path_mismatch") from exc
    if bound != requested:
        raise ValueError("database_path_mismatch")


def _runtime_is_stopped(runtime_guard: Callable[[], None] | None) -> bool:
    try:
        _require_runtime_stopped(runtime_guard)
    except ValueError:
        return False
    return True


def _require_runtime_stopped(
    runtime_guard: Callable[[], None] | None,
) -> None:
    if runtime_guard is None:
        raise ValueError("maintenance_runtime_not_stopped")
    try:
        runtime_guard()
    except Exception as exc:
        raise ValueError("maintenance_runtime_not_stopped") from exc


def _request_object(value_json: str | None) -> dict[str, object] | None:
    try:
        value = json.loads(value_json)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _request_matches_target(
    request: dict[str, object],
    target: ReviewedPendingEntryTarget,
) -> bool:
    expected_side = _side_from_entry_and_stop(target)
    expected_order_side = (
        "buy"
        if expected_side == "long"
        else "sell"
        if expected_side == "short"
        else None
    )
    return bool(
        expected_side is not None
        and _one_text(request, "instId", "instrument_id")
        == target.instrument_id
        and _one_text(request, "side") == expected_order_side
        and _one_text(request, "posSide") == expected_side
        and _same_decimal(
            _one_text(request, "triggerPrice", "triggerPx"),
            target.trigger_price,
        )
        and _same_decimal(
            _one_text(request, "sz", "size", "quantity"),
            target.size,
        )
        and _same_decimal(
            _one_text(
                request,
                "slTriggerPx",
                "slTriggerPrice",
                "closeSLTriggerPrice",
            ),
            target.embedded_stop_price,
        )
    )


def _one_text(request: dict[str, object], *keys: str) -> str | None:
    values = {
        clean
        for key in keys
        if (clean := str(request.get(key) or "").strip())
    }
    return next(iter(values)) if len(values) == 1 else None


def _deepcoin_client_order_ids(row: dict[str, object]) -> set[str]:
    return {
        clean
        for key in ("clOrdId", "clientOrderId", "client_order_id")
        if (clean := str(row.get(key) or "").strip())
    }


def _same_decimal(left: str | None, right: str) -> bool:
    if left is None:
        return False
    try:
        return Decimal(left) == Decimal(right)
    except InvalidOperation:
        return False


def _side_from_entry_and_stop(
    target: ReviewedPendingEntryTarget,
) -> str | None:
    try:
        trigger = Decimal(target.trigger_price)
        stop = Decimal(target.embedded_stop_price)
    except InvalidOperation:
        return None
    if stop < trigger:
        return "long"
    if stop > trigger:
        return "short"
    return None


def _primary_protection_matches(
    rows: list[PositionProtectionLeg],
    target: ReviewedPendingEntryTarget,
) -> bool:
    primary = [row for row in rows if row.role == "primary_stop"]
    return bool(
        len(primary) == 1
        and primary[0].leg_index == 1
        and _same_decimal(
            primary[0].planned_trigger_price,
            target.embedded_stop_price,
        )
    )


def _protection_matches(
    rows: list[PositionProtectionLeg],
    target: ReviewedPendingEntryTarget,
    *,
    allowed_statuses: set[str],
) -> bool:
    if len(rows) != 2 + len(target.take_profit_prices):
        return False
    take_profits = [row for row in rows if row.role == "take_profit"]
    expected_take_profits = {
        index: price
        for index, price in enumerate(target.take_profit_prices, start=1)
    }
    actual_take_profits = {row.leg_index: row.planned_trigger_price for row in take_profits}
    return bool(
        {row.role for row in rows} == {"primary_stop", "backup_stop", "take_profit"}
        if target.take_profit_prices
        else {row.role for row in rows} == {"primary_stop", "backup_stop"}
    ) and bool(
        _primary_protection_matches(rows, target)
        and len(take_profits) == len(target.take_profit_prices)
        and set(actual_take_profits) == set(expected_take_profits)
        and all(
            _same_decimal(actual_take_profits[index], price)
            for index, price in expected_take_profits.items()
        )
        and all(
            row.execution_binding_id == target.execution_binding_id
            and row.execution_order_leg_id == target.execution_order_leg_id
            and row.parent_entry_order_id == target.order_id
            and row.pos_id is None
            and row.exchange_order_id is None
            and row.status in allowed_statuses
            for row in rows
        )
    )


def _terminal_event_matches(
    rows: list[ExecutionEvent],
    *,
    target: ReviewedPendingEntryTarget,
    symbol: str,
    side: str,
) -> bool:
    if len(rows) != 1:
        return False
    row = rows[0]
    return bool(
        row.execution_binding_id == target.execution_binding_id
        and row.venue == "deepcoin"
        and row.symbol == symbol
        and row.side == side
        and row.order_id == target.order_id
        and row.reason == "operator_confirmed_all_entry_orders_cancelled"
        and _request_object(row.after_json)
        == {"pending": False, "terminalized": True}
    )


def _blocked(
    reason,
    order_ids,
    evidence_sha256="",
    evidence_observed_at: datetime | None = None,
):
    return ManualPendingEntryReconciliationPlan(
        status="blocked",
        reason_code=reason,
        target_order_ids=tuple(order_ids),
        evidence_sha256=evidence_sha256,
        evidence_observed_at=evidence_observed_at,
        fingerprint="",
    )


def _require_write_boundary_freshness(
    plan: ManualPendingEntryReconciliationPlan,
    *,
    now: datetime | None,
    clock: Callable[[], datetime] | None,
) -> datetime:
    if plan.evidence_observed_at is None:
        raise ValueError("exchange_snapshot_stale_at_write_boundary")
    observed_now = _timestamp(now) if now is not None else _clock_now(clock)
    try:
        require_fresh_deepcoin_maintenance_observed_at(
            plan.evidence_observed_at,
            now=observed_now,
        )
    except DeepcoinMaintenanceEvidenceRefused as exc:
        raise ValueError("exchange_snapshot_stale_at_write_boundary") from exc
    return observed_now


def _clock_now(clock: Callable[[], datetime] | None) -> datetime:
    return _timestamp((clock or (lambda: datetime.now(UTC)))())


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)
