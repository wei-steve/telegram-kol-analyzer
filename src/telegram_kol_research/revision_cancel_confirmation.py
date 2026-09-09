"""Ask the exchange what a lost cancel receipt actually did.

Phase 6-pre-5. When a revision cancels a resting entry and the POST receipt is
lost, the write is ``submit_unknown`` and hard rule 2 forbids resending it. The
batch therefore froze at ``recovery_required`` -- which is in
``TERMINAL_REVISION_STATES``, so it never advanced again. On 2026-09-09 batch 7
sat like that while the exchange had long since made up its mind: order
``...446`` was cancelled and gone, order ``...560`` was still resting. A person
had to read the exchange and align the ledger by hand.

Nothing here resends anything on a guess. It asks two independent questions --
is the order still in ``trigger-orders-pending``, and does
``trigger-order-history`` hold a terminal row for it -- and only a definite
answer produces an action:

* still resting -> the cancel did not take effect; the original intent may be
  retried exactly once, under an idempotency key naming the order;
* gone from pending **and** present in history -> the cancel took effect; the
  ledger is what is wrong, and it is corrected without touching the exchange;
* anything else, including either read failing -> unknown, which stays
  ``recovery_required`` and raises an alert. Hard rule 4: an unreadable
  exchange produces "unknown", never "zero".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: The reason code an operator already saw once, in the one-off that aligned
#: batch 7 by hand. Reused so the automated path and that repair read alike.
CONFIRMED_TERMINAL_REASON = "revision_cancel_confirmed_by_history"

CANCEL_RETRY_KEY_PREFIX = "cancel-retry"

CONFIRMED_CANCELLED = "confirmed_cancelled"
STILL_RESTING = "still_resting"
UNREADABLE = "unreadable"


def cancel_retry_idempotency_key(order_id: str) -> str:
    return f"{CANCEL_RETRY_KEY_PREFIX}:{order_id}"


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    status: str
    order_id: str
    evidence: Mapping[str, Any] | None = None
    reason: str = ""


def confirm_unknown_cancel_outcome(
    deepcoin_client: Any,
    *,
    inst_id: str,
    order_id: str,
) -> CancelOutcome:
    """Two reads, and no verdict unless both of them succeed.

    The pending read comes first because "still there" settles the question on
    its own. The history read is only consulted to tell "cancelled" apart from
    "we could not see it" -- absence from pending is not by itself evidence of
    anything, which is the mistake that would resend a cancel for an order that
    had actually filled.
    """

    clean_order_id = str(order_id or "").strip()
    clean_inst_id = str(inst_id or "").strip().upper()
    if not clean_order_id or not clean_inst_id:
        return CancelOutcome(UNREADABLE, clean_order_id, reason="cancel_target_unknown")

    try:
        pending = deepcoin_client.list_trigger_orders_pending(inst_id=clean_inst_id)
    except Exception:
        logger.warning(
            "revision_cancel_pending_read_failed ord_id=%s", clean_order_id,
            exc_info=True,
        )
        return CancelOutcome(
            UNREADABLE, clean_order_id, reason="pending_snapshot_unavailable"
        )
    if not isinstance(pending, list):
        return CancelOutcome(
            UNREADABLE, clean_order_id, reason="pending_snapshot_unavailable"
        )

    resting = next(
        (
            row
            for row in pending
            if isinstance(row, Mapping)
            and str(row.get("ordId") or row.get("orderId") or "") == clean_order_id
        ),
        None,
    )
    if resting is not None:
        return CancelOutcome(
            STILL_RESTING,
            clean_order_id,
            evidence={
                "source": "trigger-orders-pending",
                "trigger_order_type": str(resting.get("triggerOrderType") or ""),
                "sz": str(resting.get("sz") or ""),
            },
            reason="cancel_did_not_take_effect",
        )

    try:
        history = deepcoin_client.get_trigger_order_history_by_id(
            inst_id=clean_inst_id, order_id=clean_order_id
        )
    except Exception:
        logger.warning(
            "revision_cancel_history_read_failed ord_id=%s", clean_order_id,
            exc_info=True,
        )
        return CancelOutcome(
            UNREADABLE, clean_order_id, reason="history_unavailable"
        )
    if not isinstance(history, Mapping) or not history:
        # Gone from pending and absent from history is not "cancelled" -- it is
        # a gap in what we can see, and acting on it would be a guess.
        return CancelOutcome(
            UNREADABLE, clean_order_id, reason="absent_from_pending_and_history"
        )

    trigger_time = str(history.get("triggerTime") or "0")
    if trigger_time not in ("", "0"):
        # It triggered before the cancel landed. That is a fill question, not a
        # cancel question, and this module must not answer it.
        return CancelOutcome(
            UNREADABLE,
            clean_order_id,
            evidence={
                "source": "trigger-order-history",
                "trigger_time": trigger_time,
                "u_time": str(history.get("uTime") or ""),
            },
            reason="order_triggered_before_cancel",
        )
    return CancelOutcome(
        CONFIRMED_CANCELLED,
        clean_order_id,
        evidence={
            "source": "trigger-order-history",
            "u_time": str(history.get("uTime") or ""),
            "trigger_time": trigger_time,
            "error_code": str(history.get("errorCode") or ""),
            "absent_from_pending": True,
        },
        reason="cancel_confirmed_by_history",
    )


# --------------------------------------------------------------------------
# The reconcile half: act on the answer, exactly once, and never on a guess.
# --------------------------------------------------------------------------

RECOVERY_REASON = "revision_cancel_outcome_unknown"
UNREADABLE_INCIDENT_TYPE = "revision_cancel_outcome_unresolved"
STALE_INCIDENT_TYPE = "revision_batch_too_stale_to_resume"

#: How old a frozen batch may be and still be resumed automatically.
#:
#: A revision carries a trading intention -- cancel these resting entries and
#: put these ones up instead -- and that intention has a shelf life. Unfreezing
#: a batch means the ordinary advance path will place its replacement orders,
#: at the prices somebody chose when the batch was planned.
#:
#: This is not hypothetical. When this phase was about to deploy, production
#: held three batches frozen since 2026-08-17..08-21 whose replacements were
#: BTC longs at 60000-73000 while BTC was trading near 80000. Resuming them
#: would have placed three sets of orders nobody had asked for in three weeks.
#: A-3d hit the same shape and had to void an instruction item by hand before
#: deploying its reconciler.
#:
#: Six hours matches ENTRY_ADMISSION_EXECUTION_DEADLINE: the same horizon the
#: rest of the system already uses for "this entry intention is no longer
#: current".
STALE_BATCH_HORIZON = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class RevisionCancelReconcileResult:
    examined: int = 0
    confirmed: int = 0
    retried: int = 0
    alerted: int = 0
    skipped: int = 0


def reconcile_unknown_revision_cancels(
    session_factory,
    *,
    deepcoin_client: Any,
    now,
    limit: int = 5,
    incident_reporter=None,
) -> RevisionCancelReconcileResult:
    """Unfreeze a batch whose cancel receipt was lost, or say why it cannot be.

    Selection is deliberately narrow: only ``recovery_required`` batches whose
    reason is the lost receipt, and only while ``completed_at`` is unset. A
    batch an operator settled by hand is ``resolved``, never selected here, and
    ``advance_strategy_revision`` refuses it too as of this phase -- batch 7's
    remaining leg is one the user asked to keep, and nothing automated may
    cancel it.

    **This issues no exchange write.** When the order turns out to be still
    resting, the batch goes back to ``planned`` and the ordinary advance path
    performs the retry through the same audited writer it always used. Building
    a second cancel path here would mean a second set of boundary rules to keep
    correct; the idempotency key lives on the leg instead, so the retry can
    happen exactly once.
    """

    import json
    from datetime import datetime

    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        StrategyRevisionBatch,
        StrategyRevisionLeg,
    )

    observed_at = now if isinstance(now, datetime) else datetime.now(tz=None)
    bounded = max(0, min(int(limit), 20))
    if bounded == 0:
        return RevisionCancelReconcileResult()

    with session_factory() as session:
        batch_ids = [
            int(row_id)
            for (row_id,) in session.query(StrategyRevisionBatch.id)
            .filter(
                StrategyRevisionBatch.status == "recovery_required",
                StrategyRevisionBatch.reason_code == RECOVERY_REASON,
                StrategyRevisionBatch.completed_at.is_(None),
            )
            .order_by(StrategyRevisionBatch.id)
            .limit(bounded)
            .all()
        ]
    if not batch_ids:
        return RevisionCancelReconcileResult()

    counts = {"examined": 0, "confirmed": 0, "retried": 0, "alerted": 0, "skipped": 0}
    for batch_id in batch_ids:
        counts["examined"] += 1
        try:
            _reconcile_one_batch(
                session_factory,
                deepcoin_client=deepcoin_client,
                batch_id=batch_id,
                now=observed_at,
                counts=counts,
                incident_reporter=incident_reporter,
                json=json,
                models=(
                    ExecutionBinding,
                    ExecutionOrderLeg,
                    StrategyRevisionBatch,
                    StrategyRevisionLeg,
                ),
            )
        except Exception:
            counts["skipped"] += 1
            logger.warning(
                "revision_cancel_reconcile_failed batch_id=%s", batch_id,
                exc_info=True,
            )
    return RevisionCancelReconcileResult(**counts)


def _reconcile_one_batch(
    session_factory,
    *,
    deepcoin_client,
    batch_id,
    now,
    counts,
    incident_reporter,
    json,
    models,
):
    ExecutionBinding, ExecutionOrderLeg, StrategyRevisionBatch, StrategyRevisionLeg = models

    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if batch is None or batch.status != "recovery_required":
            counts["skipped"] += 1
            return
        planned_at = batch.planned_at
        if planned_at is not None and planned_at.tzinfo is None and now.tzinfo:
            planned_at = planned_at.replace(tzinfo=now.tzinfo)
        stale = planned_at is None or (now - planned_at) > STALE_BATCH_HORIZON
        batch_planned_at = str(batch.planned_at or "")
        binding = session.get(ExecutionBinding, int(batch.execution_binding_id))
        symbol = str(getattr(binding, "symbol", "") or "").upper()
        unknown_legs = [
            (
                int(leg.id),
                str(leg.order_id or ""),
                str(leg.error_json or ""),
                int(leg.execution_order_leg_id),
            )
            for leg in session.query(StrategyRevisionLeg)
            .filter(
                StrategyRevisionLeg.revision_batch_id == int(batch_id),
                StrategyRevisionLeg.action == "cancel_pending",
                StrategyRevisionLeg.status == "submit_unknown",
            )
            .order_by(StrategyRevisionLeg.id)
            .all()
        ]
    if not symbol or not unknown_legs:
        counts["skipped"] += 1
        return
    if stale:
        # Confirming the cancel would return the batch to ``planned``, and the
        # advance path would then place its replacement orders at prices chosen
        # this long ago. Say so and leave it frozen; only a person can decide
        # whether an intention this old should still be acted on.
        _report_stale(
            session_factory,
            batch_id=batch_id,
            planned_at=batch_planned_at,
            now=now,
            incident_reporter=incident_reporter,
        )
        counts["alerted"] += 1
        return
    inst_id = f"{symbol}-USDT-SWAP"

    resolved_any = False
    retryable_any = False
    for leg_id, order_id, error_json, execution_leg_id in unknown_legs:
        outcome = confirm_unknown_cancel_outcome(
            deepcoin_client, inst_id=inst_id, order_id=order_id
        )
        if outcome.status == CONFIRMED_CANCELLED:
            _apply_confirmed_cancel(
                session_factory,
                revision_leg_id=leg_id,
                execution_leg_id=execution_leg_id,
                outcome=outcome,
                now=now,
                json=json,
                models=(ExecutionOrderLeg, StrategyRevisionLeg),
            )
            resolved_any = True
            counts["confirmed"] += 1
            continue
        if outcome.status == STILL_RESTING:
            if _cancel_retry_already_used(error_json, order_id=order_id, json=json):
                # One retry, ever. A second would be a resend of a write whose
                # outcome we still cannot see.
                _report_unresolved(
                    session_factory,
                    batch_id=batch_id,
                    order_id=order_id,
                    reason="cancel_retry_already_used",
                    now=now,
                    incident_reporter=incident_reporter,
                )
                counts["alerted"] += 1
                continue
            _stamp_cancel_retry(
                session_factory,
                revision_leg_id=leg_id,
                order_id=order_id,
                now=now,
                json=json,
                model=StrategyRevisionLeg,
            )
            retryable_any = True
            counts["retried"] += 1
            continue
        _report_unresolved(
            session_factory,
            batch_id=batch_id,
            order_id=order_id,
            reason=outcome.reason or "cancel_outcome_unreadable",
            now=now,
            incident_reporter=incident_reporter,
        )
        counts["alerted"] += 1

    if resolved_any or retryable_any:
        _return_batch_to_planned(
            session_factory,
            batch_id=batch_id,
            now=now,
            model=StrategyRevisionBatch,
        )


def _apply_confirmed_cancel(
    session_factory, *, revision_leg_id, execution_leg_id, outcome, now, json, models
):
    """The exchange already did this. Only the ledger was wrong."""

    ExecutionOrderLeg, StrategyRevisionLeg = models
    with session_factory() as session:
        revision_leg = session.get(StrategyRevisionLeg, int(revision_leg_id))
        if revision_leg is None or revision_leg.status != "submit_unknown":
            session.rollback()
            return
        revision_leg.status = "cancelled"
        revision_leg.error_json = None
        revision_leg.response_json = json.dumps(
            {"confirmed_by": dict(outcome.evidence or {})},
            ensure_ascii=False,
            sort_keys=True,
        )
        revision_leg.updated_at = now
        execution_leg = session.get(ExecutionOrderLeg, int(execution_leg_id))
        if execution_leg is not None and execution_leg.status not in {
            "cancelled",
            "filled",
        }:
            execution_leg.status = "cancelled"
            execution_leg.terminal_reason = CONFIRMED_TERMINAL_REASON
            execution_leg.updated_at = now
        session.commit()


def _cancel_retry_already_used(error_json, *, order_id, json) -> bool:
    try:
        parsed = json.loads(error_json or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    retry = parsed.get("cancel_retry")
    return (
        isinstance(retry, dict)
        and str(retry.get("key") or "") == cancel_retry_idempotency_key(order_id)
    )


def _stamp_cancel_retry(session_factory, *, revision_leg_id, order_id, now, json, model):
    """Record the one retry before it happens, not after.

    Stamped first so a crash between here and the advance cannot produce a
    second retry: the key is the durable fact, the retry is the consequence.
    """

    with session_factory() as session:
        leg = session.get(model, int(revision_leg_id))
        if leg is None or leg.status != "submit_unknown":
            session.rollback()
            return
        try:
            existing = json.loads(leg.error_json or "{}")
        except (TypeError, ValueError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        existing["cancel_retry"] = {
            "key": cancel_retry_idempotency_key(order_id),
            "at": now.isoformat(),
        }
        leg.error_json = json.dumps(existing, ensure_ascii=False, sort_keys=True)
        leg.status = "pending"
        leg.updated_at = now
        session.commit()


def _return_batch_to_planned(session_factory, *, batch_id, now, model):
    """``planned`` rather than mid-cancel: the advance re-runs every check."""

    with session_factory() as session:
        batch = session.get(model, int(batch_id))
        if batch is None or batch.status != "recovery_required":
            session.rollback()
            return
        batch.status = "planned"
        batch.reason_code = None
        batch.advance_claim_token = None
        batch.advance_claimed_at = None
        batch.updated_at = now
        session.commit()


def _report_unresolved(
    session_factory, *, batch_id, order_id, reason, now, incident_reporter
):
    """Neither read could settle it, so a person has to look."""

    try:
        if incident_reporter is not None:
            incident_reporter(batch_id=batch_id, order_id=order_id, reason=reason, now=now)
            return
        import hashlib
        import json as _json

        from telegram_kol_research.runtime_incidents import record_runtime_incident

        record_runtime_incident(
            session_factory,
            source_kind="strategy_revision_batch",
            source_record_id=str(batch_id),
            incident_type=UNREADABLE_INCIDENT_TYPE,
            severity="high",
            fingerprint=hashlib.sha256(
                f"{UNREADABLE_INCIDENT_TYPE}:{batch_id}:{order_id}:{reason}".encode()
            ).hexdigest(),
            redacted_summary=_json.dumps(
                {
                    "component": "revision_cancel_confirmation",
                    "reason_code": str(reason)[:64],
                    "operation": f"revision_batch_{int(batch_id)}",
                    "impact": "cancel_outcome_unknown_batch_frozen",
                    "containment": "no_exchange_write_batch_stays_recovery_required",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            occurred_at=now,
            feature_policy_version="phase-6-pre-5-cancel-confirmation-v1",
            prompt_version="none",
            tool_policy_version="no-exchange-write",
            evidence_refs_json=_json.dumps([f"deepcoin_order:{order_id}"]),
        )
    except Exception:  # pragma: no cover - evidence must not break the loop
        logger.warning(
            "revision_cancel_unresolved_alert_failed batch_id=%s", batch_id,
            exc_info=True,
        )


def _report_stale(session_factory, *, batch_id, planned_at, now, incident_reporter):
    """A frozen batch too old to resume is a decision, not a defect."""

    try:
        if incident_reporter is not None:
            incident_reporter(
                batch_id=batch_id, order_id="", reason="batch_too_stale_to_resume",
                now=now,
            )
            return
        import hashlib
        import json as _json

        from telegram_kol_research.runtime_incidents import record_runtime_incident

        record_runtime_incident(
            session_factory,
            source_kind="strategy_revision_batch",
            source_record_id=str(batch_id),
            incident_type=STALE_INCIDENT_TYPE,
            severity="high",
            fingerprint=hashlib.sha256(
                f"{STALE_INCIDENT_TYPE}:{batch_id}".encode()
            ).hexdigest(),
            redacted_summary=_json.dumps(
                {
                    "component": "revision_cancel_confirmation",
                    "reason_code": "batch_too_stale_to_resume",
                    "operation": f"revision_batch_{int(batch_id)}",
                    "impact": "frozen_revision_intent_older_than_horizon",
                    "containment": "left_frozen_no_exchange_write",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            occurred_at=now,
            feature_policy_version="phase-6-pre-5-cancel-confirmation-v1",
            prompt_version="none",
            tool_policy_version="no-exchange-write",
            evidence_refs_json=_json.dumps([f"strategy_revision_batch:{batch_id}"]),
        )
    except Exception:  # pragma: no cover
        logger.warning(
            "revision_batch_stale_alert_failed batch_id=%s", batch_id, exc_info=True
        )
