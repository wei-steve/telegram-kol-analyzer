"""Align revision batch 7 with what the exchange actually did (2026-09-09).

raw 15633 asked to replace a resting BTC short entry. The revision cancelled
two trigger orders; the first came back without a terminal confirmation, so
the batch froze in ``recovery_required`` and both legs stayed ``pending`` in
the ledger while the exchange had already made up its mind about one of them.

Read-only verification, run before this tool and recorded in the evidence
file:

* order ``1001125173252446`` (leg 591) is **gone from
  trigger-orders-pending** and appears in trigger-order-history with
  ``triggerTime = 0``, ``errorCode = 0``, ``uTime = 2026-09-09T09:51:05Z`` --
  cancelled before it ever triggered, zero fill;
* order ``1001125173252560`` (leg 592) is **still resting**: trigger 81910,
  size 6, stop 83000.

The user decided to keep 592. So this aligns the ledger with the exchange and
stops there: 591 becomes ``cancelled``, batch 7 becomes ``resolved`` with the
reason the operator gave, and **592 is not touched by a single field**.

**No exchange write.** Every fact here was already true on the exchange before
this ran; the ledger is what was wrong.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from telegram_kol_research.models import (
    ExecutionOrderLeg,
    PositionAttributionAudit,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
)

REPAIR_TAG = "revision_batch_7_alignment_2026_09_09"
AUDIT_EVENT_TYPE = "historical_cleanup"
NOT_NEEDED = "not_needed"
EVIDENCE_PATH = "/root/evidence/step9-fix/"

BATCH_ID = 7
CANCELLED_EXECUTION_LEG_ID = 591
CANCELLED_ORDER_ID = "1001125173252446"
KEPT_EXECUTION_LEG_ID = 592
KEPT_ORDER_ID = "1001125173252560"

#: The history row that settles it, quoted rather than re-fetched at apply
#: time: the fact is fixed, and a second fetch could disagree with the one a
#: person reviewed.
HISTORY_EVIDENCE = {
    "source": "trigger-order-history",
    "ord_id": CANCELLED_ORDER_ID,
    "u_time": "2026-09-09T09:51:05Z",
    "trigger_time": "0",
    "error_code": "0",
    "absent_from_pending": True,
}

TERMINAL_REASON = "revision_cancel_confirmed_by_history"
BATCH_RESOLUTION = "operator_kept_remaining_leg"


def _fingerprint(table: str, row_id: int, changes: list[tuple[str, Any, Any]]) -> str:
    payload = json.dumps(
        {
            "repair": REPAIR_TAG,
            "table": table,
            "row_id": int(row_id),
            "changes": [[name, before, after] for name, before, after in changes],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit(
    session,
    *,
    table: str,
    row_id: int,
    changes: list[tuple[str, Any, Any]],
    needed: bool,
) -> None:
    fingerprint = _fingerprint(table, row_id, changes)
    existing = (
        session.query(PositionAttributionAudit)
        .filter(PositionAttributionAudit.fingerprint == fingerprint)
        .one_or_none()
    )
    if existing is not None:
        return
    session.add(
        PositionAttributionAudit(
            venue="deepcoin",
            pos_id=f"{table}:{int(row_id)}",
            event_type=AUDIT_EVENT_TYPE,
            prior_state=(str(changes[0][1])[:32] if changes and changes[0][1] else None),
            new_state=(str(changes[0][2])[:32] if changes else NOT_NEEDED),
            fingerprint=fingerprint,
            evidence_json=json.dumps(
                {
                    "repair": REPAIR_TAG,
                    "evidence_path": EVIDENCE_PATH,
                    "table": table,
                    "row_id": int(row_id),
                    "needed": bool(needed),
                    "exchange_evidence": HISTORY_EVIDENCE,
                    "changes": [
                        {"field": name, "before": before, "after": after}
                        for name, before, after in changes
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    )


def build_plan(session) -> dict[str, Any]:
    """What would change, read from the database, deciding nothing yet."""

    plan: dict[str, Any] = {"changes": [], "skipped": [], "untouched": []}

    execution_leg = session.get(ExecutionOrderLeg, CANCELLED_EXECUTION_LEG_ID)
    if execution_leg is None:
        plan["skipped"].append(
            {"table": "execution_order_legs", "row_id": CANCELLED_EXECUTION_LEG_ID,
             "reason": "row_missing"}
        )
    elif str(execution_leg.order_id or "") != CANCELLED_ORDER_ID:
        # The row must be the one the exchange evidence is about, or the
        # evidence proves nothing about it.
        plan["skipped"].append(
            {"table": "execution_order_legs", "row_id": CANCELLED_EXECUTION_LEG_ID,
             "reason": "order_id_mismatch",
             "observed": str(execution_leg.order_id or "")}
        )
    elif str(execution_leg.status) == "cancelled":
        plan["skipped"].append(
            {"table": "execution_order_legs", "row_id": CANCELLED_EXECUTION_LEG_ID,
             "reason": "already_cancelled"}
        )
    else:
        plan["changes"].append(
            {
                "table": "execution_order_legs",
                "row_id": CANCELLED_EXECUTION_LEG_ID,
                "changes": [
                    ("status", str(execution_leg.status), "cancelled"),
                    ("terminal_reason", execution_leg.terminal_reason, TERMINAL_REASON),
                ],
            }
        )

    revision_leg = (
        session.query(StrategyRevisionLeg)
        .filter(
            StrategyRevisionLeg.revision_batch_id == BATCH_ID,
            StrategyRevisionLeg.execution_order_leg_id == CANCELLED_EXECUTION_LEG_ID,
        )
        .one_or_none()
    )
    if revision_leg is None:
        plan["skipped"].append(
            {"table": "strategy_revision_legs", "row_id": None, "reason": "row_missing"}
        )
    elif str(revision_leg.status) == "cancelled":
        plan["skipped"].append(
            {"table": "strategy_revision_legs", "row_id": int(revision_leg.id),
             "reason": "already_cancelled"}
        )
    else:
        plan["changes"].append(
            {
                "table": "strategy_revision_legs",
                "row_id": int(revision_leg.id),
                "changes": [("status", str(revision_leg.status), "cancelled")],
            }
        )

    batch = session.get(StrategyRevisionBatch, BATCH_ID)
    if batch is None:
        plan["skipped"].append(
            {"table": "strategy_revision_batches", "row_id": BATCH_ID,
             "reason": "row_missing"}
        )
    elif str(batch.status) == "resolved":
        plan["skipped"].append(
            {"table": "strategy_revision_batches", "row_id": BATCH_ID,
             "reason": "already_resolved"}
        )
    else:
        plan["changes"].append(
            {
                "table": "strategy_revision_batches",
                "row_id": BATCH_ID,
                "changes": [
                    ("status", str(batch.status), "resolved"),
                    ("reason_code", batch.reason_code, BATCH_RESOLUTION),
                    ("completed_at", str(batch.completed_at or ""), "<now>"),
                ],
            }
        )

    kept = session.get(ExecutionOrderLeg, KEPT_EXECUTION_LEG_ID)
    plan["untouched"].append(
        {
            "table": "execution_order_legs",
            "row_id": KEPT_EXECUTION_LEG_ID,
            "order_id": str(kept.order_id or "") if kept is not None else None,
            "status": str(kept.status) if kept is not None else None,
            "reason": "operator_kept_this_leg_still_resting_on_exchange",
        }
    )
    return plan


def apply_plan(session, plan: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Apply exactly what the plan lists, re-checking each row as it goes."""

    applied: list[dict[str, Any]] = []
    for change in plan["changes"]:
        table = change["table"]
        row_id = change["row_id"]
        fields = change["changes"]
        if table == "execution_order_legs":
            row = session.get(ExecutionOrderLeg, int(row_id))
            # Compare-and-set on the state the plan was built from: another
            # writer may have moved this row since.
            if row is None or str(row.status) != fields[0][1]:
                continue
            row.status = "cancelled"
            row.terminal_reason = TERMINAL_REASON
            row.attribution_evidence_json = json.dumps(
                {"source": REPAIR_TAG, **HISTORY_EVIDENCE},
                ensure_ascii=False,
                sort_keys=True,
            )
            row.updated_at = now
        elif table == "strategy_revision_legs":
            row = session.get(StrategyRevisionLeg, int(row_id))
            if row is None or str(row.status) != fields[0][1]:
                continue
            row.status = "cancelled"
            row.updated_at = now
        elif table == "strategy_revision_batches":
            row = session.get(StrategyRevisionBatch, int(row_id))
            if row is None or str(row.status) != fields[0][1]:
                continue
            row.status = "resolved"
            row.reason_code = BATCH_RESOLUTION
            row.completed_at = now
            row.updated_at = now
        else:
            continue
        _audit(session, table=table, row_id=int(row_id), changes=fields, needed=True)
        applied.append({"table": table, "row_id": int(row_id)})

    for skipped in plan["skipped"]:
        if skipped.get("row_id") is None:
            continue
        _audit(
            session,
            table=str(skipped["table"]),
            row_id=int(skipped["row_id"]),
            changes=[("status", None, NOT_NEEDED)],
            needed=False,
        )
    for untouched in plan["untouched"]:
        _audit(
            session,
            table=str(untouched["table"]),
            row_id=int(untouched["row_id"]),
            changes=[("status", str(untouched.get("status")), NOT_NEEDED)],
            needed=False,
        )
    session.flush()
    return {"applied": applied}
