"""Dry-run-first repair for proven historical convergence state."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
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
from telegram_kol_research.position_attribution import TERMINAL_ENTRY_LEG_STATES


_ACTIVE_DELETION_STATES = frozenset(
    {"pending", "cancelling_entries", "closing_positions", "reconciling"}
)
_TERMINAL_LIFECYCLE_STATES = frozenset(
    {"exited", "expired", "invalidated", "cancelled"}
)
_TERMINAL_BINDING_STATES = frozenset(
    {"closed", "cancelled", "completed", "failed", "resolved", "superseded"}
)
_TP_CANDIDATE_STATUSES = frozenset({"submitted", "submit_unknown", "conflicted"})


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

    live_pos_ids = _live_position_ids(getattr(snapshot, "positions", []) or [])
    live_order_ids = _live_order_ids(
        list(getattr(snapshot, "open_orders", []) or [])
        + list(getattr(snapshot, "pending_trigger_orders", []) or [])
    )
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
            candidate_count = (
                session.query(SignalCandidate)
                .filter(SignalCandidate.raw_message_id == deletion_exit.raw_message_id)
                .count()
                if deletion_exit.raw_message_id is not None
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
            evidence = {
                "exit_id": int(deletion_exit.id),
                "state": str(deletion_exit.state),
                "attempt_count": int(deletion_exit.attempt_count or 0),
                "claim_token": deletion_exit.claim_token,
                "claimed_at": deletion_exit.claimed_at,
                "strategy_instance_id": deletion_exit.strategy_instance_id,
                "target_fingerprint": deletion_exit.target_fingerprint,
                "event_id": int(event.id) if event is not None else None,
                "event_status": (
                    str(event.processing_status) if event is not None else None
                ),
                "lifecycle_id": int(lifecycle.id) if lifecycle is not None else None,
                "lifecycle_status": (
                    str(lifecycle.lifecycle_status) if lifecycle is not None else None
                ),
                "binding_id": int(binding.id) if binding is not None else None,
                "binding_status": str(binding.status) if binding is not None else None,
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
                "legs": [_leg_evidence(row) for row in legs],
            }
            database_evidence.append({"source_deletion": evidence})
            if snapshot_errors:
                continue
            if (
                event is not None
                and lifecycle is None
                and binding is None
                and candidate_count == 0
                and trade_signal_count == 0
                and execution_event_count == 0
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
            if _terminal_strategy_identity(
                deletion_exit,
                lifecycle,
                binding,
                legs,
            ):
                exact_pos_ids = {
                    str(row.pos_id).strip() for row in legs if str(row.pos_id or "").strip()
                }
                exact_order_ids = {
                    str(value).strip()
                    for row in legs
                    for value in (row.order_id, row.client_order_id)
                    if str(value or "").strip()
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
                TriggerTakeProfitConvergence.status.in_(
                    tuple(_TP_CANDIDATE_STATUSES)
                )
            )
            .order_by(TriggerTakeProfitConvergence.id.asc())
            .all()
        )
        for convergence in convergences:
            if not str(convergence.pos_id or "").strip():
                continue
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
                        "order_id": str(row.order_id),
                        "status": str(row.status),
                        "pos_id": str(row.pos_id),
                        "updated_at": row.updated_at,
                        "evidence_hash": _optional_text_hash(row.evidence_json),
                    }
                    for row in orders
                ],
                "error_type": _error_type(convergence.error_json),
                "request_hash": _optional_text_hash(convergence.request_json),
                "response_hash": _optional_text_hash(convergence.response_json),
                "error_hash": _optional_text_hash(convergence.error_json),
                "updated_at": convergence.updated_at,
            }
            database_evidence.append({"take_profit_convergence": evidence})
            if snapshot_errors:
                continue
            pos_id = str(convergence.pos_id)
            order_ids = {
                str(row.order_id) for row in orders if str(row.order_id or "").strip()
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
            if not _terminal_convergence_identity(convergence, binding, leg):
                conflicts.append(
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
    current = {
        "state": str(row.state),
        "attempt_count": int(row.attempt_count or 0),
        "claim_token": row.claim_token,
        "claimed_at": row.claimed_at,
        "strategy_instance_id": row.strategy_instance_id,
        "target_fingerprint": row.target_fingerprint,
        "event_id": int(event.id),
        "event_status": str(event.processing_status),
        "lifecycle_id": int(lifecycle.id) if lifecycle is not None else None,
        "lifecycle_status": (
            str(lifecycle.lifecycle_status) if lifecycle is not None else None
        ),
        "binding_id": int(binding.id) if binding is not None else None,
        "binding_status": str(binding.status) if binding is not None else None,
        "binding_strategy_instance_id": (
            str(binding.strategy_instance_id) if binding is not None else None
        ),
        "binding_order_id": binding.order_id if binding is not None else None,
        "binding_client_order_id": (
            binding.client_order_id if binding is not None else None
        ),
        "binding_pos_id": binding.pos_id if binding is not None else None,
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
    current = {
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
        "request_hash": _optional_text_hash(convergence.request_json),
        "response_hash": _optional_text_hash(convergence.response_json),
        "error_hash": _optional_text_hash(convergence.error_json),
        "updated_at": convergence.updated_at,
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
            "order_id": str(row.order_id),
            "status": str(row.status),
            "pos_id": str(row.pos_id),
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
        and binding_strategy_instance_id
        and str(deletion_exit.strategy_instance_id or "").strip()
        == binding_strategy_instance_id
        and legs
        and all(
            int(row.execution_binding_id) == int(binding.id)
            and str(row.strategy_instance_id or "").strip()
            == binding_strategy_instance_id
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
    )


def _terminal_convergence_identity(convergence, binding, leg) -> bool:
    binding_strategy_instance_id = (
        str(binding.strategy_instance_id or "").strip()
        if binding is not None
        else ""
    )
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
        and str(leg.pos_id or "") == str(convergence.pos_id or "")
        and str(leg.venue or "").lower() == str(convergence.venue or "").lower()
    )


def _take_profit_order_matches(order, convergence) -> bool:
    return bool(
        int(order.execution_binding_id) == int(convergence.execution_binding_id)
        and int(order.execution_order_leg_id) == int(
            convergence.execution_order_leg_id
        )
        and str(order.pos_id) == str(convergence.pos_id)
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
    }


def _normalized_position(row: Any) -> dict[str, str]:
    row = row if isinstance(row, dict) else {}
    return {
        "inst_id": str(row.get("instId") or row.get("inst_id") or "").upper(),
        "pos_id": str(row.get("posId") or row.get("pos_id") or ""),
        "side": str(row.get("posSide") or row.get("side") or "").lower(),
        "size": str(row.get("pos") or row.get("size") or row.get("sz") or ""),
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
        if not isinstance(row, dict):
            continue
        pos_id = str(row.get("posId") or row.get("pos_id") or "").strip()
        try:
            size = Decimal(str(row.get("pos") or row.get("size") or row.get("sz") or "0"))
        except (InvalidOperation, ValueError):
            size = Decimal("1")
        if pos_id and abs(size) > 0:
            values.add(pos_id)
    return values


def _live_order_ids(rows) -> set[str]:
    values = set()
    for row in rows:
        normalized = _normalized_order(row)
        values.update(normalized["identity_ids"])
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
        "order_id": row.order_id,
        "client_order_id": row.client_order_id,
        "pos_id": row.pos_id,
        "updated_at": row.updated_at,
    }


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
