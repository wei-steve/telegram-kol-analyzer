"""Exact, operator-confirmed convergence for paused management history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionMutationIntent,
    StrategyManagementBatch,
    StrategyManagementLeg,
)


_ACTIONABLE_BATCH_STATES = frozenset({"partial_failed", "recovery_required"})
_TERMINAL_ORDER_STATES = frozenset(
    {"filled", "fully_filled", "completed", "succeeded", "closed"}
)
_ORDER_ID_KEYS = ("ordId", "orderId", "order_id", "id")
_CLIENT_ORDER_ID_KEYS = ("clOrdId", "clientOrderId", "client_order_id")
_POSITION_ID_KEYS = ("posId", "pos_id", "positionId", "position_id")
_ORDER_STATE_KEYS = ("state", "status", "orderStatus", "order_status")


class ManagementHistoryRecoveryConflict(RuntimeError):
    """The approved evidence no longer describes the durable source row."""


@dataclass(frozen=True, slots=True)
class ManagementHistoryRecoveryDecision:
    batch_id: int
    status: str
    decision: str | None
    reason_code: str
    evidence_fingerprint: str
    evidence: Mapping[str, Any]
    source_fingerprint: str


@dataclass(frozen=True, slots=True)
class ManagementHistoryRecoveryApplyResult:
    batch_id: int
    status: str
    reason_code: str


def plan_management_history_recovery(
    session_factory: sessionmaker,
    *,
    batch_id: int,
    snapshot: Any,
    planned_at: datetime | None = None,
) -> ManagementHistoryRecoveryDecision:
    """Plan one convergence decision without submitting or mutating anything."""

    _ = planned_at
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, int(batch_id))
        if batch is None:
            return _refusal(int(batch_id), "management_batch_missing")
        legs = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=batch.id)
            .order_by(StrategyManagementLeg.leg_index, StrategyManagementLeg.id)
            .all()
        )
        source_payload = _source_payload(batch, legs)
        source_fingerprint = _fingerprint(source_payload)
        if batch.status not in _ACTIONABLE_BATCH_STATES:
            return _refusal(
                int(batch.id),
                "management_batch_not_actionable",
                source_fingerprint=source_fingerprint,
            )
        if not legs:
            return _refusal(
                int(batch.id),
                "management_batch_legs_missing",
                source_fingerprint=source_fingerprint,
            )
        if not _snapshot_complete(snapshot):
            return _refusal(
                int(batch.id),
                "exchange_snapshot_incomplete",
                source_fingerprint=source_fingerprint,
            )
        if not _durable_identity_is_exact(session, batch, legs):
            return _refusal(
                int(batch.id),
                "durable_exact_identity_mismatch",
                source_fingerprint=source_fingerprint,
            )

        if (
            batch.status == "partial_failed"
            and batch.reason_code
            in {
                "protection_replacement_failed_and_restored",
                "close_rejected_protection_restored",
            }
            and all(
                str(leg.status) in {"planned", "failed", "restored"}
                for leg in legs
            )
            and all(
                not _position_present(snapshot, str(leg.pos_id)) for leg in legs
            )
        ):
            decision_name = "terminal_position_absent"
            exchange_evidence = {
                "exact_order_matches": 0,
                "positions_complete": True,
                "exact_positions_absent": len(legs),
            }
        elif all(str(leg.status) == "planned" for leg in legs):
            if any(leg.exchange_order_id or leg.client_order_id for leg in legs):
                return _refusal(
                    int(batch.id),
                    "planned_leg_contains_order_identity",
                    source_fingerprint=source_fingerprint,
                )
            if _has_durable_close_submission(session, batch, legs):
                return _refusal(
                    int(batch.id),
                    "durable_submission_evidence_present",
                    source_fingerprint=source_fingerprint,
                )
            decision_name = "terminal_no_submission"
            exchange_evidence = {"exact_order_matches": 0, "positions_complete": True}
        else:
            matches = []
            for leg in legs:
                match = _exact_terminal_order_match(snapshot, leg)
                if match is None:
                    match = _exact_terminal_position_history_match(
                        snapshot,
                        session=session,
                        batch=batch,
                        leg=leg,
                    )
                if match is None:
                    return _refusal(
                        int(batch.id),
                        "exact_terminal_order_evidence_missing",
                        source_fingerprint=source_fingerprint,
                    )
                if _position_present(snapshot, str(leg.pos_id)):
                    return _refusal(
                        int(batch.id),
                        "exact_position_still_present",
                        source_fingerprint=source_fingerprint,
                    )
                matches.append(match)
            used_position_history = any(
                item["state"] == "position_history_closed" for item in matches
            )
            decision_name = (
                "terminal_position_history_confirmed"
                if used_position_history
                else "terminal_exchange_confirmed"
            )
            exchange_evidence = {
                "exact_order_matches": len(matches),
                "positions_complete": True,
                "terminal_states": sorted({item["state"] for item in matches}),
            }

        evidence = {
            "schema_version": 1,
            "batch_id": int(batch.id),
            "decision": decision_name,
            "source_fingerprint": source_fingerprint,
            "leg_count": len(legs),
            "pos_refs": [_redacted_ref("pos", leg.pos_id) for leg in legs],
            "exchange": exchange_evidence,
        }
        return ManagementHistoryRecoveryDecision(
            batch_id=int(batch.id),
            status="ready",
            decision=decision_name,
            reason_code="ready",
            evidence_fingerprint=_fingerprint(evidence),
            evidence=evidence,
            source_fingerprint=source_fingerprint,
        )


def apply_management_history_recovery(
    session_factory: sessionmaker,
    *,
    decision: ManagementHistoryRecoveryDecision,
    expected_fingerprint: str,
    applied_at: datetime | None = None,
) -> ManagementHistoryRecoveryApplyResult:
    """Apply one previously planned terminal decision with durable CAS checks."""

    now = applied_at or datetime.now(UTC)
    if decision.status != "ready" or decision.decision is None:
        raise ManagementHistoryRecoveryConflict("recovery decision is not ready")
    if expected_fingerprint != decision.evidence_fingerprint:
        raise ManagementHistoryRecoveryConflict("recovery evidence fingerprint mismatch")

    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, int(decision.batch_id))
        if batch is None:
            raise ManagementHistoryRecoveryConflict("management batch disappeared")
        legs = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=batch.id)
            .order_by(StrategyManagementLeg.leg_index, StrategyManagementLeg.id)
            .all()
        )
        event_fingerprint = _fingerprint(
            {"action": "management_history_recovery", "evidence": expected_fingerprint}
        )
        existing = (
            session.query(ExecutionEvent)
            .filter_by(notification_fingerprint=event_fingerprint)
            .one_or_none()
        )
        if batch.status in {"resolved", "succeeded"} and existing is not None:
            return ManagementHistoryRecoveryApplyResult(
                batch_id=int(batch.id),
                status="already_resolved",
                reason_code=str(batch.reason_code or "history_recovery_confirmed"),
            )
        if _fingerprint(_source_payload(batch, legs)) != decision.source_fingerprint:
            raise ManagementHistoryRecoveryConflict("management history changed after plan")
        if batch.status not in _ACTIONABLE_BATCH_STATES:
            raise ManagementHistoryRecoveryConflict("management batch is no longer actionable")

        before_status = str(batch.status)
        if decision.decision == "terminal_no_submission":
            for leg in legs:
                if leg.status == "planned":
                    leg.status = "failed"
                    leg.last_error = json.dumps(
                        {"reason": "history_no_submission_confirmed"},
                        sort_keys=True,
                    )
                    leg.updated_at = now
            batch.status = "resolved"
            batch.reason_code = "history_no_submission_confirmed"
        elif decision.decision in {
            "terminal_exchange_confirmed",
            "terminal_position_history_confirmed",
        }:
            for leg in legs:
                leg.status = "confirmed"
                leg.updated_at = now
            batch.status = "succeeded"
            batch.reason_code = (
                "history_position_close_confirmed"
                if decision.decision == "terminal_position_history_confirmed"
                else "history_exchange_result_confirmed"
            )
        elif decision.decision == "terminal_position_absent":
            for leg in legs:
                if leg.status == "planned":
                    leg.status = "failed"
                    leg.last_error = json.dumps(
                        {"reason": "history_exact_position_absent"},
                        sort_keys=True,
                    )
                leg.updated_at = now
            batch.status = "resolved"
            batch.reason_code = "history_exact_position_absent"
        else:
            raise ManagementHistoryRecoveryConflict("unsupported recovery decision")
        batch.reconciled_at = now
        batch.completed_at = now
        batch.updated_at = now
        session.add(
            ExecutionEvent(
                execution_binding_id=batch.execution_binding_id,
                strategy_instance_id=batch.strategy_instance_id,
                venue="deepcoin",
                action="management_history_recovery",
                status="resolved",
                reason=batch.reason_code,
                before_json=json.dumps(
                    {"batch_status": before_status}, sort_keys=True
                ),
                after_json=json.dumps(
                    {"batch_status": batch.status}, sort_keys=True
                ),
                request_json=json.dumps(
                    {"evidence_fingerprint": expected_fingerprint}, sort_keys=True
                ),
                notification_fingerprint=event_fingerprint,
                created_at=now,
            )
        )
        session.commit()
        return ManagementHistoryRecoveryApplyResult(
            batch_id=int(batch.id),
            status="resolved",
            reason_code=str(batch.reason_code),
        )


def _snapshot_complete(snapshot: Any) -> bool:
    required = (
        "positions",
        "open_orders",
        "pending_trigger_orders",
        "order_history",
        "trade_fills",
        "trigger_history",
        "pending_tpsl_observations",
        "errors",
    )
    if any(not hasattr(snapshot, field) for field in required):
        return False
    if getattr(snapshot, "errors", None):
        return False
    observations = getattr(snapshot, "pending_tpsl_observations", ())
    return all(
        isinstance(item, Mapping) and item.get("complete") is True
        for item in observations
    )


def _durable_identity_is_exact(session, batch, legs) -> bool:
    for leg in legs:
        entry = session.get(ExecutionOrderLeg, int(leg.execution_order_leg_id))
        if (
            entry is None
            or int(entry.execution_binding_id) != int(batch.execution_binding_id)
            or str(entry.strategy_instance_id or "") != str(batch.strategy_instance_id)
            or str(entry.pos_id or "") != str(leg.pos_id)
            or str(entry.attribution_status or "") != "verified"
        ):
            return False
    return True


def _has_durable_close_submission(session, batch, legs) -> bool:
    leg_ids = [int(leg.execution_order_leg_id) for leg in legs]
    if (
        session.query(PositionMutationIntent)
        .filter(
            PositionMutationIntent.execution_order_leg_id.in_(leg_ids),
            PositionMutationIntent.operation.like("%close%"),
        )
        .first()
        is not None
    ):
        return True
    pos_ids = [str(leg.pos_id) for leg in legs]
    return (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.execution_binding_id == batch.execution_binding_id,
            ExecutionEvent.pos_id.in_(pos_ids),
            ExecutionEvent.action == "strategy_management_close_submit",
        )
        .first()
        is not None
    )


def _exact_terminal_order_match(snapshot: Any, leg) -> dict[str, str] | None:
    order_id = str(leg.exchange_order_id or "")
    client_order_id = str(leg.client_order_id or "")
    if not order_id and not client_order_id:
        return None
    rows = [
        *list(getattr(snapshot, "open_orders", ())),
        *list(getattr(snapshot, "order_history", ())),
        *list(getattr(snapshot, "trade_fills", ())),
    ]
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_order_id = _first_text(row, *_ORDER_ID_KEYS)
        row_client_order_id = _first_text(row, *_CLIENT_ORDER_ID_KEYS)
        if order_id and row_order_id != order_id:
            continue
        if client_order_id and row_client_order_id not in {None, client_order_id}:
            continue
        row_pos_id = _first_text(row, *_POSITION_ID_KEYS)
        if row_pos_id is not None and row_pos_id != str(leg.pos_id):
            continue
        state = str(_first_text(row, *_ORDER_STATE_KEYS) or "").lower()
        if state in _TERMINAL_ORDER_STATES:
            return {"state": state}
    return None


def _position_present(snapshot: Any, pos_id: str) -> bool:
    return any(
        isinstance(row, Mapping)
        and _first_text(row, *_POSITION_ID_KEYS) == pos_id
        for row in getattr(snapshot, "positions", ())
    )


def _exact_terminal_position_history_match(
    snapshot: Any,
    *,
    session,
    batch,
    leg,
) -> dict[str, str] | None:
    if not _durable_submission_response_matches(leg):
        return None
    try:
        planned_size = Decimal(str(leg.planned_close_size))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not planned_size.is_finite() or planned_size <= 0:
        return None
    planned_at_ms = int(_aware_utc(batch.planned_at).timestamp() * 1000)
    for row in getattr(snapshot, "position_history", ()):
        if not isinstance(row, Mapping):
            continue
        if _first_text(row, *_POSITION_ID_KEYS) != str(leg.pos_id):
            continue
        try:
            position_size = Decimal(str(row.get("pos")))
            closed_size = Decimal(str(row.get("closePos")))
            updated_at_ms = int(str(row.get("uTime")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if (
            not position_size.is_finite()
            or not closed_size.is_finite()
            or position_size <= 0
            or closed_size <= 0
        ):
            continue
        if updated_at_ms >= planned_at_ms and _has_exact_cumulative_close_chain(
            session,
            batch=batch,
            leg=leg,
            position_size=position_size,
            closed_size=closed_size,
        ):
            return {"state": "position_history_closed"}
    return None


def _has_exact_cumulative_close_chain(
    session,
    *,
    batch,
    leg,
    position_size: Decimal,
    closed_size: Decimal,
) -> bool:
    rows = (
        session.query(StrategyManagementLeg, StrategyManagementBatch)
        .join(
            StrategyManagementBatch,
            StrategyManagementBatch.id
            == StrategyManagementLeg.management_batch_id,
        )
        .filter(
            StrategyManagementBatch.execution_binding_id
            == batch.execution_binding_id,
            StrategyManagementLeg.execution_order_leg_id
            == leg.execution_order_leg_id,
            StrategyManagementLeg.pos_id == leg.pos_id,
            StrategyManagementBatch.planned_at <= batch.planned_at,
        )
        .order_by(StrategyManagementBatch.planned_at, StrategyManagementLeg.id)
        .all()
    )
    if not rows or int(rows[-1][0].id) != int(leg.id):
        return False
    expected_preflight = position_size
    for index, (candidate, _candidate_batch) in enumerate(rows):
        try:
            preflight = Decimal(str(candidate.preflight_size))
            close_size = Decimal(str(candidate.planned_close_size))
        except (InvalidOperation, TypeError, ValueError):
            return False
        if not preflight.is_finite() or not close_size.is_finite():
            return False
        is_current = int(candidate.id) == int(leg.id)
        if (
            preflight != expected_preflight
            or close_size <= 0
            or close_size > preflight
            or (
                not is_current
                and str(candidate.status) not in {"confirmed", "succeeded"}
            )
            or (is_current and index != len(rows) - 1)
        ):
            return False
        expected_preflight -= close_size
    return expected_preflight == 0 and position_size == closed_size


def _durable_submission_response_matches(leg) -> bool:
    try:
        payload = json.loads(leg.response_json or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, Mapping) or str(payload.get("code")) != "0":
        return False
    data = payload.get("data")
    if not isinstance(data, Mapping) or str(data.get("sCode")) not in {"", "0"}:
        return False
    return (
        _first_text(data, *_ORDER_ID_KEYS) == str(leg.exchange_order_id or "")
        and _first_text(data, *_CLIENT_ORDER_ID_KEYS)
        == str(leg.client_order_id or "")
    )


def _source_payload(batch, legs) -> dict[str, Any]:
    return {
        "batch_id": int(batch.id),
        "status": str(batch.status),
        "reason_code": str(batch.reason_code or ""),
        "updated_at": _isoformat(batch.updated_at),
        "execution_binding_id": int(batch.execution_binding_id),
        "legs": [
            {
                "id": int(leg.id),
                "execution_order_leg_id": int(leg.execution_order_leg_id),
                "pos_id": str(leg.pos_id),
                "status": str(leg.status),
                "client_order_id": str(leg.client_order_id or ""),
                "exchange_order_id": str(leg.exchange_order_id or ""),
                "updated_at": _isoformat(leg.updated_at),
            }
            for leg in legs
        ],
    }


def _refusal(
    batch_id: int,
    reason_code: str,
    *,
    source_fingerprint: str = "",
) -> ManagementHistoryRecoveryDecision:
    evidence = {
        "schema_version": 1,
        "batch_id": int(batch_id),
        "decision": "refused",
        "reason_code": reason_code,
    }
    return ManagementHistoryRecoveryDecision(
        batch_id=int(batch_id),
        status="refused",
        decision=None,
        reason_code=reason_code,
        evidence_fingerprint=_fingerprint(evidence),
        evidence=evidence,
        source_fingerprint=source_fingerprint,
    )


def _fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redacted_ref(kind: str, value: Any) -> str:
    return f"{kind}:{hashlib.sha256(f'{kind}:{value}'.encode()).hexdigest()[:10]}"


def _first_text(row: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _isoformat(value: datetime) -> str:
    return _aware_utc(value).isoformat()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
