"""Dry-run-first repair for proven historical convergence state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, not_, or_
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.execution_bindings import (
    load_deepcoin_execution_reconciliation_snapshot_read_only,
    position_history_row_proves_full_close,
)
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    PositionMutationIntent,
    PositionTakeProfitOrder,
    RepairConfirmationToken,
    SignalCandidate,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    TelegramSourceMessageEvent,
    TradeSignal,
    TriggerTakeProfitConvergence,
    utc_now,
)
from telegram_kol_research.position_attribution import (
    ATTRIBUTION_POLICY_VERSION,
    TERMINAL_ENTRY_LEG_STATES,
)


_ACTIVE_DELETION_STATES = frozenset(
    {"pending", "cancelling_entries", "closing_positions", "reconciling"}
)
_TERMINAL_LIFECYCLE_STATES = frozenset(
    {"exited", "expired", "invalidated", "cancelled"}
)
_TERMINAL_BINDING_STATES = frozenset(
    {"closed", "cancelled", "completed", "failed", "resolved", "superseded"}
)
_TP_CANDIDATE_STATUSES = frozenset({"submitted", "submit_unknown"})


class HistoricalStateRepairRefused(RuntimeError):
    """Raised when a supervised repair safety gate is not satisfied."""


@dataclass(frozen=True, slots=True)
class HistoricalStateRepairAction:
    kind: str
    target_id: int
    reason_code: str
    related_ids: tuple[int, ...] = ()
    evidence_json: str = "{}"


@dataclass(frozen=True, slots=True)
class HistoricalStateRepairFinding:
    kind: str
    target_id: int
    reason_code: str
    evidence_json: str = "{}"


@dataclass(frozen=True, slots=True)
class HistoricalStateRepairPlan:
    schema_version: int
    mode: str
    database_fingerprint: str
    exchange_fingerprint: str
    fingerprint: str
    confirmation_token: str
    actions: tuple[HistoricalStateRepairAction, ...]
    exclusions: tuple[HistoricalStateRepairFinding, ...]
    conflicts: tuple[HistoricalStateRepairFinding, ...]

    @property
    def action_count(self) -> int:
        return len(self.actions)

    @property
    def has_actions(self) -> bool:
        return bool(self.actions)


@dataclass(frozen=True, slots=True)
class HistoricalStateRepairResult:
    fingerprint: str
    applied_actions: int
    audit_event_id: int | None


def load_historical_state_repair_snapshot_read_only(
    session_factory,
    *,
    client,
):
    """Load the normal snapshot plus bounded exact history for repair candidates."""

    snapshot = load_deepcoin_execution_reconciliation_snapshot_read_only(
        session_factory,
        client=client,
    )
    with session_factory() as session:
        candidates: set[tuple[str, str]] = set()
        convergences = (
            session.query(TriggerTakeProfitConvergence)
            .filter(TriggerTakeProfitConvergence.venue == "deepcoin")
            .filter(TriggerTakeProfitConvergence.status == "submitted")
            .order_by(TriggerTakeProfitConvergence.id.asc())
            .all()
        )
        for convergence in convergences:
            leg = session.get(
                ExecutionOrderLeg,
                int(convergence.execution_order_leg_id),
            )
            binding = session.get(
                ExecutionBinding,
                int(convergence.execution_binding_id),
            )
            pos_id = str(convergence.pos_id or "").strip()
            if (
                leg is None
                or binding is None
                or not pos_id
                or str(leg.status or "").lower() not in TERMINAL_ENTRY_LEG_STATES
                or str(leg.attribution_status or "")
                not in {"attribution_conflict", "evidence_unavailable"}
                or str(leg.pos_id or "").strip() != pos_id
                or str(binding.status or "").lower()
                not in _TERMINAL_BINDING_STATES
            ):
                continue
            audits = (
                session.query(PositionAttributionAudit)
                .filter(PositionAttributionAudit.execution_order_leg_id == int(leg.id))
                .filter(PositionAttributionAudit.venue == "deepcoin")
                .filter(PositionAttributionAudit.pos_id == pos_id)
                .filter(PositionAttributionAudit.event_type == "ownership_verified")
                .filter(PositionAttributionAudit.new_state == "verified")
                .all()
            )
            if not any(_is_policy_v2_authority_audit(row) for row in audits):
                continue
            candidates.add((f"{str(binding.symbol).upper()}-USDT-SWAP", pos_id))

    history_reader = getattr(client, "list_position_history", None)
    for instrument_id, pos_id in sorted(candidates):
        source = f"position_history:{instrument_id}:{pos_id}"
        if history_reader is None:
            snapshot.errors[source] = "list_position_history unavailable"
            continue
        try:
            rows = history_reader(inst_id=instrument_id, pos_id=pos_id)
        except Exception as exc:
            snapshot.errors[source] = str(exc)
            continue
        if not isinstance(rows, list) or not all(
            isinstance(row, dict) for row in rows
        ):
            snapshot.errors[source] = "invalid list response schema"
            continue
        if any(_position_identity_ids(row) != {pos_id} for row in rows):
            snapshot.errors[source] = (
                "position history response identity mismatch: "
                f"expected {pos_id}"
            )
            continue
        known = {
            _canonical_json(row)
            for row in list(getattr(snapshot, "position_history", []) or [])
        }
        for row in rows:
            fingerprint = _canonical_json(row)
            if fingerprint in known:
                continue
            snapshot.position_history.append(row)
            known.add(fingerprint)
    return snapshot


def _is_policy_v2_authority_audit(row: PositionAttributionAudit) -> bool:
    try:
        evidence = json.loads(row.evidence_json or "{}")
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(evidence, dict)
        and evidence.get("policy_version") == ATTRIBUTION_POLICY_VERSION
    )


def build_historical_state_repair_plan(
    session_factory,
    *,
    snapshot,
    planned_at: datetime | None = None,
) -> HistoricalStateRepairPlan:
    """Build a deterministic local repair plan from a complete read-only snapshot."""

    del planned_at  # timestamps never participate in fingerprints
    exchange_evidence = _exchange_evidence(snapshot)
    exchange_fingerprint = _fingerprint(exchange_evidence)
    snapshot_errors = dict(getattr(snapshot, "errors", {}) or {})
    actions: list[HistoricalStateRepairAction] = []
    exclusions: list[HistoricalStateRepairFinding] = []
    conflicts: list[HistoricalStateRepairFinding] = []
    database_evidence: list[dict[str, Any]] = []

    if snapshot_errors:
        conflicts.append(
            HistoricalStateRepairFinding(
                kind="snapshot",
                target_id=0,
                reason_code="exchange_snapshot_incomplete",
                evidence_json=_canonical_json({"errors": snapshot_errors}),
            )
        )

    position_rows = list(getattr(snapshot, "positions", []) or [])
    order_rows = list(getattr(snapshot, "open_orders", []) or []) + list(
        getattr(snapshot, "pending_trigger_orders", []) or []
    )
    snapshot_identity_incomplete = bool(
        any(
            _position_row_is_live_or_unknown(row)
            and not _position_identity_ids(row)
            for row in position_rows
        )
        or any(not _order_identity_ids(row) for row in order_rows)
    )
    if snapshot_identity_incomplete:
        conflicts.append(
            HistoricalStateRepairFinding(
                kind="snapshot",
                target_id=0,
                reason_code="exchange_snapshot_identity_incomplete",
                evidence_json=_canonical_json(
                    {
                        "unidentified_live_position_count": sum(
                            1
                            for row in position_rows
                            if _position_row_is_live_or_unknown(row)
                            and not _position_identity_ids(row)
                        ),
                        "unidentified_order_count": sum(
                            1 for row in order_rows if not _order_identity_ids(row)
                        ),
                    }
                ),
            )
        )
    snapshot_blocked = bool(snapshot_errors or snapshot_identity_incomplete)
    live_pos_ids = _live_position_ids(position_rows)
    live_order_ids = _live_order_ids(order_rows)
    complete_instruments = {
        str(row.get("instrument_id") or "").upper()
        for row in list(getattr(snapshot, "pending_tpsl_observations", []) or [])
        if isinstance(row, dict) and row.get("complete") is True
    }

    with session_factory() as session:
        deletion_exits = (
            session.query(SourceMessageDeletionExit)
            .filter(SourceMessageDeletionExit.state.in_(tuple(_ACTIVE_DELETION_STATES)))
            .order_by(SourceMessageDeletionExit.id.asc())
            .all()
        )
        for deletion_exit in deletion_exits:
            event = session.get(
                TelegramSourceMessageEvent, int(deletion_exit.source_event_id)
            )
            lifecycle = (
                session.get(StrategyLifecycle, int(deletion_exit.target_lifecycle_id))
                if deletion_exit.target_lifecycle_id is not None
                else None
            )
            binding = (
                session.get(ExecutionBinding, int(deletion_exit.execution_binding_id))
                if deletion_exit.execution_binding_id is not None
                else None
            )
            legs = (
                session.query(ExecutionOrderLeg)
                .filter(
                    ExecutionOrderLeg.execution_binding_id == int(binding.id),
                    ExecutionOrderLeg.purpose == "entry",
                )
                .order_by(ExecutionOrderLeg.id.asc())
                .all()
                if binding is not None
                else []
            )
            exit_raw_message_id = (
                int(deletion_exit.raw_message_id)
                if deletion_exit.raw_message_id is not None
                else None
            )
            event_raw_message_id = (
                int(event.raw_message_id)
                if event is not None and event.raw_message_id is not None
                else None
            )
            raw_message_identity_conflict = bool(
                exit_raw_message_id is not None
                and event_raw_message_id is not None
                and exit_raw_message_id != event_raw_message_id
            )
            effective_raw_message_id = (
                exit_raw_message_id
                if exit_raw_message_id is not None
                else event_raw_message_id
            )
            candidate_count = (
                session.query(SignalCandidate)
                .filter(SignalCandidate.raw_message_id == effective_raw_message_id)
                .count()
                if effective_raw_message_id is not None
                else 0
            )
            trade_signal_count = (
                session.query(TradeSignal)
                .filter(
                    TradeSignal.chat_id == int(event.chat_id),
                    TradeSignal.message_id == int(event.message_id),
                )
                .count()
                if event is not None
                else 0
            )
            execution_event_count = (
                session.query(ExecutionEvent)
                .filter(
                    ExecutionEvent.chat_id == int(event.chat_id),
                    ExecutionEvent.message_id == int(event.message_id),
                    ExecutionEvent.action.not_in(
                        {
                            "source_message_deletion_outcome",
                            "terminal_entry_cleanup_outcome",
                        }
                    ),
                    not_(
                        and_(
                            ExecutionEvent.action == "auto_trade_skipped",
                            ExecutionEvent.status == "skipped",
                            ExecutionEvent.reason == "symbol_not_allowed",
                            ExecutionEvent.order_id.is_(None),
                            ExecutionEvent.client_order_id.is_(None),
                            ExecutionEvent.pos_id.is_(None),
                            ExecutionEvent.response_json.is_(None),
                        )
                    ),
                    or_(
                        ExecutionEvent.order_id.is_not(None),
                        ExecutionEvent.client_order_id.is_not(None),
                        ExecutionEvent.pos_id.is_not(None),
                        ExecutionEvent.request_json.is_not(None),
                        ExecutionEvent.response_json.is_not(None),
                    ),
                )
                .count()
                if event is not None
                else 0
            )
            source_lifecycle_ids = (
                [
                    int(row.id)
                    for row in session.query(StrategyLifecycle.id)
                    .filter(
                        StrategyLifecycle.chat_id == int(event.chat_id),
                        StrategyLifecycle.message_id == int(event.message_id),
                    )
                    .order_by(StrategyLifecycle.id.asc())
                    .all()
                ]
                if event is not None
                else []
            )
            source_binding_ids = (
                [
                    int(row.id)
                    for row in session.query(ExecutionBinding.id)
                    .filter(
                        ExecutionBinding.chat_id == int(event.chat_id),
                        ExecutionBinding.message_id == int(event.message_id),
                    )
                    .order_by(ExecutionBinding.id.asc())
                    .all()
                ]
                if event is not None
                else []
            )
            evidence = {
                "exit_id": int(deletion_exit.id),
                "state": str(deletion_exit.state),
                "attempt_count": int(deletion_exit.attempt_count or 0),
                "claim_token": deletion_exit.claim_token,
                "claimed_at": deletion_exit.claimed_at,
                "strategy_instance_id": deletion_exit.strategy_instance_id,
                "target_fingerprint": deletion_exit.target_fingerprint,
                "raw_message_id": deletion_exit.raw_message_id,
                "event_raw_message_id": event_raw_message_id,
                "effective_raw_message_id": effective_raw_message_id,
                "raw_message_identity_conflict": raw_message_identity_conflict,
                "event_id": int(event.id) if event is not None else None,
                "event_status": (
                    str(event.processing_status) if event is not None else None
                ),
                "event_chat_id": int(event.chat_id) if event is not None else None,
                "event_message_id": (
                    int(event.message_id) if event is not None else None
                ),
                "lifecycle_id": int(lifecycle.id) if lifecycle is not None else None,
                "lifecycle_status": (
                    str(lifecycle.lifecycle_status) if lifecycle is not None else None
                ),
                "lifecycle_execution_binding_id": (
                    int(lifecycle.execution_binding_id)
                    if lifecycle is not None
                    and lifecycle.execution_binding_id is not None
                    else None
                ),
                "binding_id": int(binding.id) if binding is not None else None,
                "binding_status": str(binding.status) if binding is not None else None,
                "binding_venue": str(binding.venue) if binding is not None else None,
                "binding_symbol": str(binding.symbol) if binding is not None else None,
                "binding_side": str(binding.side) if binding is not None else None,
                "binding_strategy_instance_id": (
                    str(binding.strategy_instance_id) if binding is not None else None
                ),
                "binding_order_id": binding.order_id if binding is not None else None,
                "binding_client_order_id": (
                    binding.client_order_id if binding is not None else None
                ),
                "binding_pos_id": binding.pos_id if binding is not None else None,
                "candidate_count": int(candidate_count),
                "trade_signal_count": int(trade_signal_count),
                "execution_event_count": int(execution_event_count),
                "source_lifecycle_ids": source_lifecycle_ids,
                "source_binding_ids": source_binding_ids,
                "legs": [_leg_evidence(row) for row in legs],
            }
            database_evidence.append({"source_deletion": evidence})
            if snapshot_blocked:
                continue
            if (
                event is not None
                and lifecycle is None
                and binding is None
                and candidate_count == 0
                and trade_signal_count == 0
                and execution_event_count == 0
                and not raw_message_identity_conflict
                and not source_lifecycle_ids
                and not source_binding_ids
            ):
                actions.append(
                    HistoricalStateRepairAction(
                        kind="source_deletion_exit",
                        target_id=int(deletion_exit.id),
                        reason_code="non_strategy_or_unlinked",
                        evidence_json=_canonical_json(evidence),
                    )
                )
                continue
            if (
                event is not None
                and lifecycle is not None
                and str(lifecycle.lifecycle_status or "").lower()
                in _TERMINAL_LIFECYCLE_STATES
                and binding is None
                and not legs
                and trade_signal_count == 0
                and execution_event_count == 0
                and not raw_message_identity_conflict
                and lifecycle.execution_binding_id is None
                and source_lifecycle_ids == [int(lifecycle.id)]
                and not source_binding_ids
            ):
                actions.append(
                    HistoricalStateRepairAction(
                        kind="source_deletion_exit",
                        target_id=int(deletion_exit.id),
                        reason_code="strategy_terminal_without_execution",
                        evidence_json=_canonical_json(evidence),
                    )
                )
                continue
            if not raw_message_identity_conflict and _terminal_strategy_identity(
                deletion_exit,
                lifecycle,
                binding,
                legs,
            ) and source_lifecycle_ids == [int(lifecycle.id)] and source_binding_ids == [
                int(binding.id)
            ]:
                exact_pos_ids = {
                    value for row in legs for value in _split_ids(row.pos_id)
                }
                exact_order_ids = {
                    value
                    for row in legs
                    for identity in (row.order_id, row.client_order_id)
                    for value in _split_ids(identity)
                }
                if exact_pos_ids & live_pos_ids or exact_order_ids & live_order_ids:
                    exclusions.append(
                        HistoricalStateRepairFinding(
                            kind="source_deletion_exit",
                            target_id=int(deletion_exit.id),
                            reason_code="exact_position_or_order_still_live",
                            evidence_json=_canonical_json(evidence),
                        )
                    )
                else:
                    actions.append(
                        HistoricalStateRepairAction(
                            kind="source_deletion_exit",
                            target_id=int(deletion_exit.id),
                            reason_code="strategy_already_terminal",
                            related_ids=tuple(int(row.id) for row in legs),
                            evidence_json=_canonical_json(
                                {
                                    **evidence,
                                    "verified_absent_pos_ids": sorted(exact_pos_ids),
                                    "verified_absent_order_ids": sorted(exact_order_ids),
                                }
                            ),
                        )
                    )
                continue
            conflicts.append(
                HistoricalStateRepairFinding(
                    kind="source_deletion_exit",
                    target_id=int(deletion_exit.id),
                    reason_code="source_deletion_identity_not_terminal",
                    evidence_json=_canonical_json(evidence),
                )
            )

        convergences = (
            session.query(TriggerTakeProfitConvergence)
            .filter(
                TriggerTakeProfitConvergence.venue == "deepcoin",
                or_(
                    TriggerTakeProfitConvergence.status.in_(
                        tuple(_TP_CANDIDATE_STATUSES)
                    ),
                    and_(
                        TriggerTakeProfitConvergence.status == "conflicted",
                        TriggerTakeProfitConvergence.reason_code
                        == "convergence_submit_rejected",
                    ),
                )
            )
            .order_by(TriggerTakeProfitConvergence.id.asc())
            .all()
        )
        for convergence in convergences:
            binding = session.get(
                ExecutionBinding, int(convergence.execution_binding_id)
            )
            leg = session.get(
                ExecutionOrderLeg, int(convergence.execution_order_leg_id)
            )
            orders = (
                session.query(PositionTakeProfitOrder)
                .filter(
                    PositionTakeProfitOrder.trigger_take_profit_convergence_id
                    == int(convergence.id)
                )
                .order_by(PositionTakeProfitOrder.id.asc())
                .all()
            )
            evidence = {
                "convergence_id": int(convergence.id),
                "venue": str(convergence.venue),
                "execution_binding_id": int(convergence.execution_binding_id),
                "execution_order_leg_id": int(
                    convergence.execution_order_leg_id
                ),
                "status": str(convergence.status),
                "reason_code": convergence.reason_code,
                "pos_id": str(convergence.pos_id),
                "binding": (
                    {
                        "id": int(binding.id),
                        "status": str(binding.status),
                        "strategy_instance_id": binding.strategy_instance_id,
                        "venue": str(binding.venue),
                        "order_id": binding.order_id,
                        "client_order_id": binding.client_order_id,
                        "pos_id": binding.pos_id,
                        "symbol": str(binding.symbol),
                        "updated_at": binding.updated_at,
                    }
                    if binding is not None
                    else None
                ),
                "leg": _leg_evidence(leg) if leg is not None else None,
                "orders": [
                    {
                        "id": int(row.id),
                        "venue": str(row.venue),
                        "execution_binding_id": int(row.execution_binding_id),
                        "execution_order_leg_id": int(row.execution_order_leg_id),
                        "convergence_id": int(
                            row.trigger_take_profit_convergence_id or 0
                        ),
                        "order_id": str(row.order_id),
                        "trigger_price": str(row.trigger_price),
                        "size_text": row.size_text,
                        "status": str(row.status),
                        "pos_id": str(row.pos_id),
                        "cancel_request_hash": _optional_text_hash(
                            row.cancel_request_json
                        ),
                        "cancel_response_hash": _optional_text_hash(
                            row.cancel_response_json
                        ),
                        "cancel_requested_at": row.cancel_requested_at,
                        "cancelled_at": row.cancelled_at,
                        "completed_at": row.completed_at,
                        "updated_at": row.updated_at,
                        "evidence_hash": _optional_text_hash(row.evidence_json),
                    }
                    for row in orders
                ],
                "error_type": _error_type(convergence.error_json),
                "desired_take_profits_hash": _optional_text_hash(
                    convergence.desired_take_profits_json
                ),
                "request_hash": _optional_text_hash(convergence.request_json),
                "response_hash": _optional_text_hash(convergence.response_json),
                "error_hash": _optional_text_hash(convergence.error_json),
                "reserved_at": convergence.reserved_at,
                "completed_at": convergence.completed_at,
                "updated_at": convergence.updated_at,
            }
            attribution_repair_proof = None
            attribution_repair_failure = None
            if (
                convergence.status == "submitted"
                and leg is not None
                and str(leg.attribution_status or "")
                in {"attribution_conflict", "evidence_unavailable"}
            ):
                (
                    attribution_repair_proof,
                    attribution_repair_failure,
                ) = _proven_take_profit_attribution_repair(
                    session,
                    convergence=convergence,
                    binding=binding,
                    leg=leg,
                    orders=orders,
                    snapshot=snapshot,
                )
                evidence["attribution_repair_proof"] = attribution_repair_proof
                evidence["attribution_repair_failure"] = attribution_repair_failure
            database_evidence.append({"take_profit_convergence": evidence})
            pos_id = str(convergence.pos_id or "").strip()
            if not pos_id:
                conflicts.append(
                    HistoricalStateRepairFinding(
                        kind="take_profit_convergence",
                        target_id=int(convergence.id),
                        reason_code="take_profit_position_identity_missing",
                        evidence_json=_canonical_json(evidence),
                    )
                )
                continue
            if snapshot_blocked:
                continue
            order_ids = {
                str(row.order_id).strip()
                for row in orders
                if str(row.order_id or "").strip()
            }
            if pos_id in live_pos_ids or order_ids & live_order_ids:
                exclusions.append(
                    HistoricalStateRepairFinding(
                        kind="take_profit_convergence",
                        target_id=int(convergence.id),
                        reason_code="exact_position_or_order_still_live",
                        evidence_json=_canonical_json(evidence),
                    )
                )
                continue
            instrument_id = (
                f"{str(binding.symbol).upper()}-USDT-SWAP"
                if binding is not None
                else ""
            )
            if instrument_id not in complete_instruments:
                conflicts.append(
                    HistoricalStateRepairFinding(
                        kind="take_profit_convergence",
                        target_id=int(convergence.id),
                        reason_code="pending_order_snapshot_incomplete",
                        evidence_json=_canonical_json(evidence),
                    )
                )
                continue
            if attribution_repair_proof is not None:
                actions.append(
                    HistoricalStateRepairAction(
                        kind="take_profit_attribution_repair",
                        target_id=int(convergence.id),
                        reason_code=(
                            "convergence_position_terminal_prior_authority_restored"
                        ),
                        related_ids=tuple(int(row.id) for row in orders),
                        evidence_json=_canonical_json(evidence),
                    )
                )
                continue
            if attribution_repair_failure is not None:
                exclusions.append(
                    HistoricalStateRepairFinding(
                        kind="take_profit_convergence",
                        target_id=int(convergence.id),
                        reason_code="take_profit_attribution_repair_not_proven",
                        evidence_json=_canonical_json(evidence),
                    )
                )
                continue
            if not _terminal_convergence_identity(convergence, binding, leg):
                exclusions.append(
                    HistoricalStateRepairFinding(
                        kind="take_profit_convergence",
                        target_id=int(convergence.id),
                        reason_code="take_profit_identity_not_terminal",
                        evidence_json=_canonical_json(evidence),
                    )
                )
                continue
            rejected = bool(
                (
                    convergence.status == "conflicted"
                    and convergence.reason_code == "convergence_submit_rejected"
                    and _error_type(convergence.error_json)
                    == "DeepcoinDefiniteRejection"
                )
                or (
                    convergence.status == "submit_unknown"
                    and convergence.reason_code == "convergence_submit_unknown"
                    and _error_type(convergence.error_json)
                    == "DeepcoinDefiniteRejection"
                )
            )
            if rejected and not orders:
                actions.append(
                    HistoricalStateRepairAction(
                        kind="take_profit_rejection",
                        target_id=int(convergence.id),
                        reason_code="convergence_submit_rejected_position_terminal",
                        evidence_json=_canonical_json(evidence),
                    )
                )
                continue
            if convergence.status == "submitted" and orders and all(
                _take_profit_order_matches(row, convergence) for row in orders
            ):
                actions.append(
                    HistoricalStateRepairAction(
                        kind="take_profit_convergence",
                        target_id=int(convergence.id),
                        reason_code="convergence_position_terminal",
                        related_ids=tuple(int(row.id) for row in orders),
                        evidence_json=_canonical_json(evidence),
                    )
                )
                continue
            conflicts.append(
                HistoricalStateRepairFinding(
                    kind="take_profit_convergence",
                    target_id=int(convergence.id),
                    reason_code="take_profit_state_not_repairable",
                    evidence_json=_canonical_json(evidence),
                )
            )

    actions.sort(key=lambda row: (row.kind, row.target_id))
    exclusions.sort(key=lambda row: (row.kind, row.target_id, row.reason_code))
    conflicts.sort(key=lambda row: (row.kind, row.target_id, row.reason_code))
    database_fingerprint = _fingerprint(database_evidence)
    plan_payload = {
        "schema_version": 1,
        "database_fingerprint": database_fingerprint,
        "exchange_fingerprint": exchange_fingerprint,
        "actions": [asdict(row) for row in actions],
        "exclusions": [asdict(row) for row in exclusions],
        "conflicts": [asdict(row) for row in conflicts],
    }
    fingerprint = _fingerprint(plan_payload)
    return HistoricalStateRepairPlan(
        schema_version=1,
        mode="dry_run",
        database_fingerprint=database_fingerprint,
        exchange_fingerprint=exchange_fingerprint,
        fingerprint=fingerprint,
        confirmation_token=_confirmation_token(fingerprint),
        actions=tuple(actions),
        exclusions=tuple(exclusions),
        conflicts=tuple(conflicts),
    )


def apply_historical_state_repair_plan(
    session_factory,
    *,
    snapshot_loader,
    expected_fingerprint: str,
    expected_action_count: int,
    confirmation_token: str,
    applied_at: datetime | None = None,
) -> HistoricalStateRepairResult:
    """Apply one freshly rebuilt plan using only local database transitions."""

    now = applied_at or utc_now()
    snapshot = snapshot_loader()
    plan = build_historical_state_repair_plan(
        session_factory,
        snapshot=snapshot,
        planned_at=now,
    )
    if str(expected_fingerprint) != plan.fingerprint:
        raise HistoricalStateRepairRefused("repair fingerprint does not match fresh plan")
    if int(expected_action_count) != plan.action_count:
        raise HistoricalStateRepairRefused("repair action count does not match fresh plan")
    if str(confirmation_token) != plan.confirmation_token:
        raise HistoricalStateRepairRefused("repair confirmation token is invalid")
    if plan.conflicts:
        raise HistoricalStateRepairRefused("repair plan has unresolved conflicts")
    if not plan.actions:
        raise HistoricalStateRepairRefused("repair plan has no actions")

    token_hash = sha256(str(confirmation_token).encode("utf-8")).hexdigest()
    try:
        with session_factory() as session:
            if (
                session.query(RepairConfirmationToken)
                .filter(RepairConfirmationToken.token_hash == token_hash)
                .one_or_none()
                is not None
            ):
                raise HistoricalStateRepairRefused(
                    "repair confirmation token was already consumed"
                )
            session.add(
                RepairConfirmationToken(
                    token_hash=token_hash,
                    action_kind="historical_state_repair",
                    action_id=plan.fingerprint,
                    pos_id="none",
                    consumed_at=now,
                )
            )
            session.flush()
            for action in plan.actions:
                if action.kind == "source_deletion_exit":
                    _apply_source_deletion_action(session, action=action, applied_at=now)
                elif action.kind in {
                    "take_profit_convergence",
                    "take_profit_rejection",
                }:
                    _apply_take_profit_action(
                        session,
                        action=action,
                        fingerprint=plan.fingerprint,
                        applied_at=now,
                    )
                elif action.kind == "take_profit_attribution_repair":
                    _apply_take_profit_attribution_repair_action(
                        session,
                        action=action,
                        fingerprint=plan.fingerprint,
                        applied_at=now,
                    )
                else:
                    raise HistoricalStateRepairRefused(
                        f"unsupported repair action kind: {action.kind}"
                    )
            audit = ExecutionEvent(
                action="historical_state_convergence_repair",
                status="completed",
                reason="supervised_historical_state_repair",
                response_json=_canonical_json(
                    {
                        "schema_version": plan.schema_version,
                        "fingerprint": plan.fingerprint,
                        "database_fingerprint": plan.database_fingerprint,
                        "exchange_fingerprint": plan.exchange_fingerprint,
                        "action_count": plan.action_count,
                        "action_counts": _action_counts(plan.actions),
                        "excluded_count": len(plan.exclusions),
                    }
                ),
                notification_status="not_needed",
                notification_fingerprint=sha256(
                    f"historical-state-repair:{plan.fingerprint}".encode("utf-8")
                ).hexdigest(),
                created_at=now,
            )
            session.add(audit)
            session.commit()
            return HistoricalStateRepairResult(
                fingerprint=plan.fingerprint,
                applied_actions=plan.action_count,
                audit_event_id=int(audit.id),
            )
    except IntegrityError as exc:
        raise HistoricalStateRepairRefused(
            "repair confirmation token was already consumed"
        ) from exc


def _apply_source_deletion_action(session, *, action, applied_at: datetime) -> None:
    row = session.get(SourceMessageDeletionExit, int(action.target_id))
    if row is None or row.state not in _ACTIVE_DELETION_STATES:
        raise HistoricalStateRepairRefused("source deletion state changed before apply")
    event = session.get(TelegramSourceMessageEvent, int(row.source_event_id))
    if event is None:
        raise HistoricalStateRepairRefused("source deletion event disappeared before apply")
    expected = _json_object(action.evidence_json)
    lifecycle = (
        session.get(StrategyLifecycle, int(row.target_lifecycle_id))
        if row.target_lifecycle_id is not None
        else None
    )
    binding = (
        session.get(ExecutionBinding, int(row.execution_binding_id))
        if row.execution_binding_id is not None
        else None
    )
    legs = (
        session.query(ExecutionOrderLeg)
        .filter(
            ExecutionOrderLeg.execution_binding_id == int(binding.id),
            ExecutionOrderLeg.purpose == "entry",
        )
        .order_by(ExecutionOrderLeg.id.asc())
        .all()
        if binding is not None
        else []
    )
    exit_raw_message_id = int(row.raw_message_id) if row.raw_message_id is not None else None
    event_raw_message_id = (
        int(event.raw_message_id) if event.raw_message_id is not None else None
    )
    raw_message_identity_conflict = bool(
        exit_raw_message_id is not None
        and event_raw_message_id is not None
        and exit_raw_message_id != event_raw_message_id
    )
    effective_raw_message_id = (
        exit_raw_message_id
        if exit_raw_message_id is not None
        else event_raw_message_id
    )
    candidate_count = (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == effective_raw_message_id)
        .count()
        if effective_raw_message_id is not None
        else 0
    )
    trade_signal_count = (
        session.query(TradeSignal)
        .filter(
            TradeSignal.chat_id == int(event.chat_id),
            TradeSignal.message_id == int(event.message_id),
        )
        .count()
    )
    execution_event_count = (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.chat_id == int(event.chat_id),
            ExecutionEvent.message_id == int(event.message_id),
            ExecutionEvent.action.not_in(
                {
                    "source_message_deletion_outcome",
                    "terminal_entry_cleanup_outcome",
                }
            ),
            not_(
                and_(
                    ExecutionEvent.action == "auto_trade_skipped",
                    ExecutionEvent.status == "skipped",
                    ExecutionEvent.reason == "symbol_not_allowed",
                    ExecutionEvent.order_id.is_(None),
                    ExecutionEvent.client_order_id.is_(None),
                    ExecutionEvent.pos_id.is_(None),
                    ExecutionEvent.response_json.is_(None),
                )
            ),
            or_(
                ExecutionEvent.order_id.is_not(None),
                ExecutionEvent.client_order_id.is_not(None),
                ExecutionEvent.pos_id.is_not(None),
                ExecutionEvent.request_json.is_not(None),
                ExecutionEvent.response_json.is_not(None),
            ),
        )
        .count()
    )
    source_lifecycle_ids = [
        int(item.id)
        for item in session.query(StrategyLifecycle.id)
        .filter(
            StrategyLifecycle.chat_id == int(event.chat_id),
            StrategyLifecycle.message_id == int(event.message_id),
        )
        .order_by(StrategyLifecycle.id.asc())
        .all()
    ]
    source_binding_ids = [
        int(item.id)
        for item in session.query(ExecutionBinding.id)
        .filter(
            ExecutionBinding.chat_id == int(event.chat_id),
            ExecutionBinding.message_id == int(event.message_id),
        )
        .order_by(ExecutionBinding.id.asc())
        .all()
    ]
    current = {
        "state": str(row.state),
        "attempt_count": int(row.attempt_count or 0),
        "claim_token": row.claim_token,
        "claimed_at": row.claimed_at,
        "strategy_instance_id": row.strategy_instance_id,
        "target_fingerprint": row.target_fingerprint,
        "raw_message_id": row.raw_message_id,
        "event_raw_message_id": event_raw_message_id,
        "effective_raw_message_id": effective_raw_message_id,
        "raw_message_identity_conflict": raw_message_identity_conflict,
        "event_id": int(event.id),
        "event_status": str(event.processing_status),
        "event_chat_id": int(event.chat_id),
        "event_message_id": int(event.message_id),
        "lifecycle_id": int(lifecycle.id) if lifecycle is not None else None,
        "lifecycle_status": (
            str(lifecycle.lifecycle_status) if lifecycle is not None else None
        ),
        "lifecycle_execution_binding_id": (
            int(lifecycle.execution_binding_id)
            if lifecycle is not None and lifecycle.execution_binding_id is not None
            else None
        ),
        "binding_id": int(binding.id) if binding is not None else None,
        "binding_status": str(binding.status) if binding is not None else None,
        "binding_venue": str(binding.venue) if binding is not None else None,
        "binding_symbol": str(binding.symbol) if binding is not None else None,
        "binding_side": str(binding.side) if binding is not None else None,
        "binding_strategy_instance_id": (
            str(binding.strategy_instance_id) if binding is not None else None
        ),
        "binding_order_id": binding.order_id if binding is not None else None,
        "binding_client_order_id": (
            binding.client_order_id if binding is not None else None
        ),
        "binding_pos_id": binding.pos_id if binding is not None else None,
        "candidate_count": int(candidate_count),
        "trade_signal_count": int(trade_signal_count),
        "execution_event_count": int(execution_event_count),
        "source_lifecycle_ids": source_lifecycle_ids,
        "source_binding_ids": source_binding_ids,
        "legs": [_leg_evidence(item) for item in legs],
    }
    for key, value in current.items():
        if _canonical_json(expected.get(key)) != _canonical_json(value):
            raise HistoricalStateRepairRefused(
                f"source deletion identity changed before apply: {key}"
            )
    row.state = "succeeded"
    row.claim_token = None
    row.claimed_at = None
    row.last_reason = action.reason_code
    row.last_error = None
    row.last_reconciled_at = applied_at
    row.completed_at = applied_at
    row.updated_at = applied_at
    if action.reason_code == "strategy_already_terminal":
        row.flat_proof_json = _canonical_json(
            {
                "source": "historical_state_repair",
                "plan_evidence": json.loads(action.evidence_json),
                "proved_at": applied_at.isoformat(),
            }
        )
    event.processing_status = (
        "ignored"
        if action.reason_code == "non_strategy_or_unlinked"
        else "completed"
    )
    event.reason_code = action.reason_code
    event.completed_at = applied_at
    event.updated_at = applied_at


def _apply_take_profit_action(
    session,
    *,
    action,
    fingerprint: str,
    applied_at: datetime,
) -> None:
    convergence = session.get(TriggerTakeProfitConvergence, int(action.target_id))
    if convergence is None:
        raise HistoricalStateRepairRefused("take-profit convergence disappeared before apply")
    expected = _json_object(action.evidence_json)
    binding = session.get(ExecutionBinding, int(convergence.execution_binding_id))
    leg = session.get(ExecutionOrderLeg, int(convergence.execution_order_leg_id))
    orders = (
        session.query(PositionTakeProfitOrder)
        .filter(
            PositionTakeProfitOrder.trigger_take_profit_convergence_id
            == int(convergence.id)
        )
        .order_by(PositionTakeProfitOrder.id.asc())
        .all()
    )
    current = {
        "convergence_id": int(convergence.id),
        "venue": str(convergence.venue),
        "execution_binding_id": int(convergence.execution_binding_id),
        "execution_order_leg_id": int(convergence.execution_order_leg_id),
        "status": str(convergence.status),
        "reason_code": convergence.reason_code,
        "pos_id": str(convergence.pos_id),
        "binding": (
            {
                "id": int(binding.id),
                "status": str(binding.status),
                "strategy_instance_id": binding.strategy_instance_id,
                "venue": str(binding.venue),
                "order_id": binding.order_id,
                "client_order_id": binding.client_order_id,
                "pos_id": binding.pos_id,
                "symbol": str(binding.symbol),
                "updated_at": binding.updated_at,
            }
            if binding is not None
            else None
        ),
        "leg": _leg_evidence(leg) if leg is not None else None,
        "desired_take_profits_hash": _optional_text_hash(
            convergence.desired_take_profits_json
        ),
        "request_hash": _optional_text_hash(convergence.request_json),
        "response_hash": _optional_text_hash(convergence.response_json),
        "error_hash": _optional_text_hash(convergence.error_json),
        "reserved_at": convergence.reserved_at,
        "completed_at": convergence.completed_at,
        "updated_at": convergence.updated_at,
        "orders": [
            {
                "id": int(item.id),
                "venue": str(item.venue),
                "execution_binding_id": int(item.execution_binding_id),
                "execution_order_leg_id": int(item.execution_order_leg_id),
                "convergence_id": int(
                    item.trigger_take_profit_convergence_id or 0
                ),
                "order_id": str(item.order_id),
                "trigger_price": str(item.trigger_price),
                "size_text": item.size_text,
                "status": str(item.status),
                "pos_id": str(item.pos_id),
                "cancel_request_hash": _optional_text_hash(
                    item.cancel_request_json
                ),
                "cancel_response_hash": _optional_text_hash(
                    item.cancel_response_json
                ),
                "cancel_requested_at": item.cancel_requested_at,
                "cancelled_at": item.cancelled_at,
                "completed_at": item.completed_at,
                "updated_at": item.updated_at,
                "evidence_hash": _optional_text_hash(item.evidence_json),
            }
            for item in orders
        ],
    }
    for key, value in current.items():
        if _canonical_json(expected.get(key)) != _canonical_json(value):
            raise HistoricalStateRepairRefused(
                f"take-profit convergence identity changed before apply: {key}"
            )
    for order_id in action.related_ids:
        row = session.get(PositionTakeProfitOrder, int(order_id))
        if row is None or int(row.trigger_take_profit_convergence_id or 0) != int(
            convergence.id
        ):
            raise HistoricalStateRepairRefused(
                "take-profit order identity changed before apply"
            )
        expected_order = next(
            (
                item
                for item in expected.get("orders", [])
                if int(item.get("id") or 0) == int(row.id)
            ),
            None,
        )
        current_order = {
            "id": int(row.id),
            "venue": str(row.venue),
            "execution_binding_id": int(row.execution_binding_id),
            "execution_order_leg_id": int(row.execution_order_leg_id),
            "convergence_id": int(row.trigger_take_profit_convergence_id or 0),
            "order_id": str(row.order_id),
            "trigger_price": str(row.trigger_price),
            "size_text": row.size_text,
            "status": str(row.status),
            "pos_id": str(row.pos_id),
            "cancel_request_hash": _optional_text_hash(row.cancel_request_json),
            "cancel_response_hash": _optional_text_hash(row.cancel_response_json),
            "cancel_requested_at": row.cancel_requested_at,
            "cancelled_at": row.cancelled_at,
            "completed_at": row.completed_at,
            "updated_at": row.updated_at,
            "evidence_hash": _optional_text_hash(row.evidence_json),
        }
        if expected_order is None or _canonical_json(expected_order) != _canonical_json(
            current_order
        ):
            raise HistoricalStateRepairRefused(
                "take-profit order state changed before apply"
            )
        if row.status == "active":
            evidence = _json_object(row.evidence_json)
            evidence["terminalization"] = {
                "source": "historical_state_repair",
                "reason_code": "position_terminal_order_absent",
                "plan_fingerprint": fingerprint,
                "observed_at": applied_at.isoformat(),
            }
            row.evidence_json = _canonical_json(evidence)
            row.status = "expired"
            row.completed_at = applied_at
            row.updated_at = applied_at
    convergence.status = "completed"
    convergence.reason_code = action.reason_code
    convergence.completed_at = applied_at
    convergence.updated_at = applied_at


def _apply_take_profit_attribution_repair_action(
    session,
    *,
    action,
    fingerprint: str,
    applied_at: datetime,
) -> None:
    convergence = session.get(TriggerTakeProfitConvergence, int(action.target_id))
    if convergence is None:
        raise HistoricalStateRepairRefused(
            "take-profit attribution convergence disappeared before apply"
        )
    expected = _json_object(action.evidence_json)
    expected_proof = expected.get("attribution_repair_proof")
    if not isinstance(expected_proof, dict):
        raise HistoricalStateRepairRefused(
            "take-profit attribution proof missing before apply"
        )
    binding = session.get(ExecutionBinding, int(convergence.execution_binding_id))
    leg = session.get(ExecutionOrderLeg, int(convergence.execution_order_leg_id))
    orders = (
        session.query(PositionTakeProfitOrder)
        .filter(
            PositionTakeProfitOrder.trigger_take_profit_convergence_id
            == int(convergence.id)
        )
        .order_by(PositionTakeProfitOrder.id.asc())
        .all()
    )
    history_row = expected_proof.get("position_history")
    current_proof, failure = _proven_take_profit_attribution_repair(
        session,
        convergence=convergence,
        binding=binding,
        leg=leg,
        orders=orders,
        snapshot=SimpleNamespace(
            position_history=[history_row] if isinstance(history_row, dict) else []
        ),
    )
    if failure is not None or _canonical_json(current_proof) != _canonical_json(
        expected_proof
    ):
        raise HistoricalStateRepairRefused(
            "take-profit attribution local proof changed before apply"
        )
    if leg is None:
        raise HistoricalStateRepairRefused(
            "take-profit attribution leg disappeared before apply"
        )
    prior_state = str(leg.attribution_status or "unassigned")

    _apply_take_profit_action(
        session,
        action=action,
        fingerprint=fingerprint,
        applied_at=applied_at,
    )

    restoration_evidence = {
        "evidence_type": "historical_authority_restored",
        "policy_version": ATTRIBUTION_POLICY_VERSION,
        "plan_fingerprint": fingerprint,
        "pos_id": str(leg.pos_id),
        "authority_audit_ids": [
            int(row["id"])
            for row in expected_proof.get("authority_audits", [])
            if isinstance(row, dict) and row.get("id") is not None
        ],
    }
    restoration_fingerprint = _fingerprint(
        {
            "venue": str(leg.venue),
            "execution_binding_id": int(leg.execution_binding_id),
            "execution_order_leg_id": int(leg.id),
            "pos_id": str(leg.pos_id),
            "event_type": "historical_authority_restored",
            "reason_code": action.reason_code,
            "plan_fingerprint": fingerprint,
        }
    )
    existing_audit = (
        session.query(PositionAttributionAudit.id)
        .filter(PositionAttributionAudit.fingerprint == restoration_fingerprint)
        .one_or_none()
    )
    if existing_audit is None:
        session.add(
            PositionAttributionAudit(
                execution_binding_id=int(leg.execution_binding_id),
                execution_order_leg_id=int(leg.id),
                venue=str(leg.venue),
                pos_id=str(leg.pos_id),
                event_type="historical_authority_restored",
                prior_state=prior_state,
                new_state="verified",
                fingerprint=restoration_fingerprint,
                evidence_json=_canonical_json(restoration_evidence),
                notification_status="not_needed",
                created_at=applied_at,
            )
        )
    leg.attribution_status = "verified"
    leg.attribution_evidence_json = _canonical_json(restoration_evidence)
    leg.last_verified_at = applied_at
    leg.updated_at = applied_at


def _terminal_strategy_identity(deletion_exit, lifecycle, binding, legs) -> bool:
    binding_strategy_instance_id = (
        str(binding.strategy_instance_id or "").strip()
        if binding is not None
        else ""
    )
    leg_pos_ids = {
        value for row in legs for value in _split_ids(row.pos_id)
    }
    leg_order_ids = {
        value for row in legs for value in _split_ids(row.order_id)
    }
    leg_client_order_ids = {
        value for row in legs for value in _split_ids(row.client_order_id)
    }
    return bool(
        lifecycle is not None
        and binding is not None
        and lifecycle.execution_binding_id == binding.id
        and str(lifecycle.lifecycle_status or "").lower()
        in _TERMINAL_LIFECYCLE_STATES
        and str(binding.status or "").lower() in _TERMINAL_BINDING_STATES
        and str(binding.venue or "").lower() == "deepcoin"
        and binding_strategy_instance_id
        and str(deletion_exit.strategy_instance_id or "").strip()
        == binding_strategy_instance_id
        and legs
        and all(
            int(row.execution_binding_id) == int(binding.id)
            and str(row.strategy_instance_id or "").strip()
            == binding_strategy_instance_id
            and str(row.venue or "").lower() == str(binding.venue or "").lower()
            and str(row.status or "").lower() in TERMINAL_ENTRY_LEG_STATES
            and (
                not str(row.pos_id or "").strip()
                or str(row.attribution_status or "") == "verified"
            )
            for row in legs
        )
        and _split_ids(binding.pos_id).issubset(leg_pos_ids)
        and _split_ids(binding.order_id).issubset(leg_order_ids)
        and _split_ids(binding.client_order_id).issubset(leg_client_order_ids)
        and bool(leg_pos_ids or leg_order_ids or leg_client_order_ids)
    )


def _terminal_convergence_identity(convergence, binding, leg) -> bool:
    binding_strategy_instance_id = (
        str(binding.strategy_instance_id or "").strip()
        if binding is not None
        else ""
    )
    convergence_pos_id = str(convergence.pos_id or "").strip()
    leg_pos_id = str(leg.pos_id or "").strip() if leg is not None else ""
    return bool(
        binding is not None
        and leg is not None
        and int(leg.execution_binding_id) == int(binding.id)
        and int(convergence.execution_binding_id) == int(binding.id)
        and int(convergence.execution_order_leg_id) == int(leg.id)
        and str(binding.status or "").lower() in _TERMINAL_BINDING_STATES
        and str(leg.purpose or "") == "entry"
        and str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES
        and str(leg.attribution_status or "") == "verified"
        and binding_strategy_instance_id
        and str(leg.strategy_instance_id or "").strip()
        == binding_strategy_instance_id
        and convergence_pos_id
        and leg_pos_id == convergence_pos_id
        and str(leg.venue or "").lower() == str(convergence.venue or "").lower()
        and str(convergence.venue or "").lower() == "deepcoin"
        and str(binding.venue or "").lower() == "deepcoin"
    )


def _proven_take_profit_attribution_repair(
    session,
    *,
    convergence,
    binding,
    leg,
    orders,
    snapshot,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return canonical proof only for one fully closed, formerly owned position."""

    if binding is None or leg is None:
        return None, "binding_or_leg_missing"
    pos_id = str(convergence.pos_id or "").strip()
    strategy_instance_id = str(binding.strategy_instance_id or "").strip()
    if not pos_id or not strategy_instance_id:
        return None, "immutable_identity_missing"
    if not (
        str(convergence.venue or "").lower() == "deepcoin"
        and str(binding.venue or "").lower() == "deepcoin"
        and str(leg.venue or "").lower() == "deepcoin"
        and int(convergence.execution_binding_id) == int(binding.id)
        and int(convergence.execution_order_leg_id) == int(leg.id)
        and int(leg.execution_binding_id) == int(binding.id)
        and str(leg.strategy_instance_id or "").strip() == strategy_instance_id
        and _split_ids(binding.pos_id) == {pos_id}
        and str(leg.pos_id or "").strip() == pos_id
        and str(leg.purpose or "") == "entry"
        and str(binding.status or "").lower() in _TERMINAL_BINDING_STATES
        and str(leg.status or "").lower() in TERMINAL_ENTRY_LEG_STATES
        and str(leg.attribution_status or "")
        in {"attribution_conflict", "evidence_unavailable"}
        and convergence.status == "submitted"
        and orders
        and all(_take_profit_order_matches(row, convergence) for row in orders)
    ):
        return None, "strategy_or_ledger_identity_mismatch"

    lifecycles = (
        session.query(StrategyLifecycle)
        .filter(StrategyLifecycle.execution_binding_id == int(binding.id))
        .order_by(StrategyLifecycle.id.asc())
        .all()
    )
    if len(lifecycles) != 1 or str(
        lifecycles[0].lifecycle_status or ""
    ).lower() not in _TERMINAL_LIFECYCLE_STATES:
        return None, "lifecycle_not_uniquely_terminal"

    audits = (
        session.query(PositionAttributionAudit)
        .filter(PositionAttributionAudit.execution_order_leg_id == int(leg.id))
        .order_by(
            PositionAttributionAudit.created_at.asc(),
            PositionAttributionAudit.id.asc(),
        )
        .all()
    )
    authority_audits = [
        row
        for row in audits
        if row.execution_binding_id == int(binding.id)
        and str(row.venue or "").lower() == "deepcoin"
        and str(row.pos_id or "").strip() == pos_id
        and str(row.event_type or "") == "ownership_verified"
        and str(row.new_state or "") == "verified"
        and _is_policy_v2_authority_audit(row)
    ]
    if not authority_audits:
        return None, "policy_v2_authority_missing"
    latest_authority_at = max(row.created_at for row in authority_audits)
    later_conflicts = [
        row
        for row in audits
        if str(row.event_type or "") == "attribution_conflict"
        and row.created_at >= latest_authority_at
    ]
    for row in later_conflicts:
        conflict_evidence = _json_object(row.evidence_json)
        candidate_leg_ids = conflict_evidence.get("candidate_leg_ids", [])
        candidate_position_ids = conflict_evidence.get(
            "candidate_position_ids", []
        )
        if (
            not isinstance(candidate_leg_ids, list)
            or not isinstance(candidate_position_ids, list)
            or any(str(value or "").strip() for value in candidate_leg_ids)
            or any(str(value or "").strip() for value in candidate_position_ids)
        ):
            return None, "later_competing_attribution_evidence"

    mutations = (
        session.query(PositionMutationIntent)
        .filter(
            PositionMutationIntent.venue == "deepcoin",
            PositionMutationIntent.operation == "close_position",
            PositionMutationIntent.execution_binding_id == int(binding.id),
            PositionMutationIntent.execution_order_leg_id == int(leg.id),
            PositionMutationIntent.strategy_instance_id == strategy_instance_id,
            PositionMutationIntent.pos_id == pos_id,
            PositionMutationIntent.status == "confirmed",
        )
        .order_by(PositionMutationIntent.id.asc())
        .all()
    )
    if len(mutations) != 1:
        return None, "confirmed_close_mutation_missing_or_ambiguous"
    mutation = mutations[0]
    if _json_position_ids(mutation.request_json) != {pos_id} or _json_position_ids(
        mutation.response_json
    ) != {pos_id}:
        return None, "close_mutation_payload_identity_mismatch"

    reservations = (
        session.query(BoundPositionCloseReservation)
        .filter(BoundPositionCloseReservation.pos_id == pos_id)
        .order_by(BoundPositionCloseReservation.id.asc())
        .all()
    )
    if not (
        len(reservations) == 1
        and int(reservations[0].execution_binding_id) == int(binding.id)
        and str(reservations[0].status or "") == "confirmed"
    ):
        return None, "confirmed_close_reservation_missing_or_mismatched"
    reservation = reservations[0]

    competing_leg_ids = sorted(
        int(row.id)
        for row in session.query(ExecutionOrderLeg)
        .filter(
            ExecutionOrderLeg.venue == "deepcoin",
            ExecutionOrderLeg.id != int(leg.id),
        )
        .all()
        if pos_id in _split_ids(row.pos_id)
    )
    if competing_leg_ids:
        return None, "position_owned_by_other_leg"

    history_rows = [
        row
        for row in list(getattr(snapshot, "position_history", []) or [])
        if isinstance(row, dict) and pos_id in _position_identity_ids(row)
    ]
    instrument_id = f"{str(binding.symbol).upper()}-USDT-SWAP"
    if not (
        len(history_rows) == 1
        and _position_identity_ids(history_rows[0]) == {pos_id}
        and position_history_row_proves_full_close(
            history_rows[0],
            instrument_id=instrument_id,
            position_side=str(binding.side or ""),
            pos_id=pos_id,
        )
    ):
        return None, "exact_full_close_history_not_proven"

    return (
        {
            "schema_version": 1,
            "strategy_instance_id": strategy_instance_id,
            "pos_id": pos_id,
            "instrument_id": instrument_id,
            "lifecycle": {
                "id": int(lifecycles[0].id),
                "status": str(lifecycles[0].lifecycle_status),
                "execution_binding_id": int(
                    lifecycles[0].execution_binding_id or 0
                ),
                "updated_at": lifecycles[0].updated_at,
            },
            "authority_audits": [_attribution_audit_evidence(row) for row in authority_audits],
            "later_conflict_audits": [
                _attribution_audit_evidence(row) for row in later_conflicts
            ],
            "close_mutation": _mutation_intent_evidence(mutation),
            "close_reservation": _close_reservation_evidence(reservation),
            "competing_leg_ids": competing_leg_ids,
            "position_history": history_rows[0],
            "position_history_hash": _fingerprint(history_rows[0]),
        },
        None,
    )


def _take_profit_order_matches(order, convergence) -> bool:
    return bool(
        str(order.order_id or "").strip()
        and int(order.execution_binding_id) == int(convergence.execution_binding_id)
        and int(order.execution_order_leg_id) == int(
            convergence.execution_order_leg_id
        )
        and str(order.pos_id or "").strip()
        == str(convergence.pos_id or "").strip()
        and str(order.venue).lower() == str(convergence.venue).lower()
        and str(order.status) == "active"
    )


def _exchange_evidence(snapshot) -> dict[str, Any]:
    return {
        "errors": dict(getattr(snapshot, "errors", {}) or {}),
        "positions": sorted(
            (_normalized_position(row) for row in getattr(snapshot, "positions", []) or []),
            key=_canonical_json,
        ),
        "open_orders": sorted(
            (_normalized_order(row) for row in getattr(snapshot, "open_orders", []) or []),
            key=_canonical_json,
        ),
        "pending_trigger_orders": sorted(
            (
                _normalized_order(row)
                for row in getattr(snapshot, "pending_trigger_orders", []) or []
            ),
            key=_canonical_json,
        ),
        "pending_tpsl_observations": sorted(
            (
                {
                    "instrument_id": str(row.get("instrument_id") or "").upper(),
                    "complete": row.get("complete") is True,
                    "order_ids": sorted(str(value) for value in row.get("order_ids", [])),
                }
                for row in getattr(snapshot, "pending_tpsl_observations", []) or []
                if isinstance(row, dict)
            ),
            key=_canonical_json,
        ),
        "position_history": sorted(
            (
                dict(row)
                for row in getattr(snapshot, "position_history", []) or []
                if isinstance(row, dict)
            ),
            key=_canonical_json,
        ),
    }


def _normalized_position(row: Any) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    identity_ids = sorted(_position_identity_ids(row))
    size_values = {
        key: str(row[key])
        for key in ("pos", "size", "sz", "positionSize", "position_size")
        if key in row
    }
    return {
        "inst_id": str(row.get("instId") or row.get("inst_id") or "").upper(),
        "identity_ids": identity_ids,
        "side": str(row.get("posSide") or row.get("side") or "").lower(),
        "size_values": size_values,
    }


def _normalized_order(row: Any) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    identity_ids = sorted(
        {
            str(row.get(key)).strip()
            for key in (
                "ordId",
                "orderId",
                "order_id",
                "algoId",
                "triggerOrderId",
                "orderSysID",
                "OrderSysID",
                "id",
                "clOrdId",
                "clientOrderId",
                "client_order_id",
            )
            if str(row.get(key) or "").strip()
        }
    )
    return {
        "inst_id": str(row.get("instId") or row.get("inst_id") or "").upper(),
        "order_id": str(
            row.get("ordId")
            or row.get("orderId")
            or row.get("order_id")
            or row.get("algoId")
            or row.get("triggerOrderId")
            or row.get("orderSysID")
            or row.get("OrderSysID")
            or row.get("id")
            or ""
        ),
        "client_order_id": str(
            row.get("clOrdId")
            or row.get("clientOrderId")
            or row.get("client_order_id")
            or ""
        ),
        "pos_id": str(row.get("posId") or row.get("pos_id") or ""),
        "identity_ids": identity_ids,
    }


def _live_position_ids(rows) -> set[str]:
    values = set()
    for row in rows:
        pos_ids = _position_identity_ids(row)
        if not pos_ids:
            continue
        if _position_row_is_live_or_unknown(row):
            values.update(pos_ids)
    return values


def _position_identity_ids(row) -> set[str]:
    if not isinstance(row, dict):
        return set()
    return {
        str(row[key]).strip()
        for key in (
            "posId",
            "pos_id",
            "PositionID",
            "positionId",
            "position_id",
            "id",
        )
        if row.get(key) not in (None, "") and str(row[key]).strip()
    }


def _position_row_is_live_or_unknown(row) -> bool:
    if not isinstance(row, dict):
        return True
    raw_sizes = [
        row[key]
        for key in ("pos", "size", "sz", "positionSize", "position_size")
        if key in row
    ]
    if not raw_sizes:
        return True
    for raw_size in raw_sizes:
        try:
            size = Decimal(str(raw_size).strip())
        except (InvalidOperation, TypeError, ValueError):
            return True
        if not size.is_finite() or abs(size) > 0:
            return True
    return False


def _order_identity_ids(row) -> set[str]:
    if not isinstance(row, dict):
        return set()
    return set(_normalized_order(row)["identity_ids"])


def _live_order_ids(rows) -> set[str]:
    values = set()
    for row in rows:
        values.update(_order_identity_ids(row))
    return values


def _leg_evidence(row) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "binding_id": int(row.execution_binding_id),
        "strategy_instance_id": row.strategy_instance_id,
        "venue": str(row.venue),
        "purpose": str(row.purpose),
        "status": str(row.status),
        "attribution_status": str(row.attribution_status),
        "attribution_evidence_hash": _optional_text_hash(
            row.attribution_evidence_json
        ),
        "terminal_reason": row.terminal_reason,
        "last_verified_at": row.last_verified_at,
        "order_id": row.order_id,
        "client_order_id": row.client_order_id,
        "pos_id": row.pos_id,
        "request_hash": _optional_text_hash(row.request_json),
        "response_hash": _optional_text_hash(row.response_json),
        "updated_at": row.updated_at,
    }


def _attribution_audit_evidence(row: PositionAttributionAudit) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "execution_binding_id": (
            int(row.execution_binding_id)
            if row.execution_binding_id is not None
            else None
        ),
        "execution_order_leg_id": (
            int(row.execution_order_leg_id)
            if row.execution_order_leg_id is not None
            else None
        ),
        "venue": str(row.venue),
        "pos_id": row.pos_id,
        "event_type": str(row.event_type),
        "prior_state": row.prior_state,
        "new_state": str(row.new_state),
        "fingerprint": str(row.fingerprint),
        "evidence_hash": _optional_text_hash(row.evidence_json),
        "created_at": row.created_at,
    }


def _mutation_intent_evidence(row: PositionMutationIntent) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "idempotency_key": str(row.idempotency_key),
        "venue": str(row.venue),
        "operation": str(row.operation),
        "strategy_instance_id": str(row.strategy_instance_id),
        "execution_binding_id": int(row.execution_binding_id),
        "execution_order_leg_id": int(row.execution_order_leg_id),
        "pos_id": str(row.pos_id),
        "status": str(row.status),
        "authority_fingerprint": str(row.authority_fingerprint),
        "request_fingerprint": str(row.request_fingerprint),
        "request_hash": _optional_text_hash(row.request_json),
        "response_hash": _optional_text_hash(row.response_json),
        "error_hash": _optional_text_hash(row.error_json),
        "confirmed_at": row.confirmed_at,
        "updated_at": row.updated_at,
    }


def _close_reservation_evidence(
    row: BoundPositionCloseReservation,
) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "pos_id": str(row.pos_id),
        "execution_binding_id": int(row.execution_binding_id),
        "status": str(row.status),
        "last_error_hash": _optional_text_hash(row.last_error),
        "updated_at": row.updated_at,
    }


def _json_position_ids(value: str | None) -> set[str]:
    try:
        parsed = json.loads(str(value or "null"))
    except (TypeError, json.JSONDecodeError):
        return set()
    found: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {
                    "posId",
                    "pos_id",
                    "PositionID",
                    "positionId",
                    "position_id",
                }:
                    normalized = str(child or "").strip()
                    if normalized:
                        found.add(normalized)
                else:
                    visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(parsed)
    return found


def _split_ids(value: Any) -> set[str]:
    return {
        item
        for raw in str(value or "").split(",")
        if (item := raw.strip())
    }


def _error_type(value: str | None) -> str | None:
    return str(_json_object(value).get("type") or "") or None


def _optional_text_hash(value: str | None) -> str | None:
    return sha256(str(value).encode("utf-8")).hexdigest() if value is not None else None


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _action_counts(actions) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in actions:
        counts[row.kind] = counts.get(row.kind, 0) + 1
    return dict(sorted(counts.items()))


def _confirmation_token(fingerprint: str) -> str:
    return sha256(f"historical-state-repair:{fingerprint}".encode("utf-8")).hexdigest()[:16]


def _fingerprint(value: Any) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
