"""Close three revision batches the exchange settled three weeks ago.

Batches 3, 4 and 6 (raw 11246 / 11432 / 12076, all BTC long in chat
-1002805019371) froze at ``recovery_required / revision_cancel_outcome_unknown``
between 2026-08-17 and 2026-08-21 when a cancel receipt was lost. Nothing has
touched them since.

Phase 6-pre-5 gives frozen batches a way to resume, which is why these had to
be looked at before it deployed: resuming them would have handed the advance
path three replacement intents -- BTC longs at 60000-73000, planned when BTC
was there and now far below a market near 80000 -- and it would have placed
those orders. The phase now refuses to resume anything older than six hours,
so this repair is no longer load-bearing for safety; it is here so three dead
batches stop being scanned and alerted on forever.

**Read-only verification, done before this tool and recorded in the evidence
file:**

* order ``1001124794741637`` and its sibling ``1001124794741754`` are **absent
  from trigger-orders-pending**, have **no fills**, and there is **no BTC long
  position** on the account -- three independent facts, and together they
  settle exposure: nothing of this batch can still execute;
* both orders are also **absent from trigger-order-history**, and that proves
  nothing either way. These are three-week-old orders and the history endpoint
  has a retention window, so absence there is "cannot see", not "was
  cancelled". By this repository's own rule 4 that is unknown, and it is
  recorded as unknown rather than dressed up as confirmation;
* what actually settles *how* they ended is the ledger, not the exchange:
  ``execution_order_legs`` 506 has been ``cancelled`` since **2026-08-31
  14:40Z** with ``operator_cancelled_unfilled_entry_leg``, 507 since
  2026-08-19, and their binding is ``closed``.

So the execution layer finished three weeks ago and only the revision-batch
layer never caught up. This aligns that layer with a fact that has been true
since 2026-08-31, and stops.

**No exchange write.** Nothing here asks the venue for anything.
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

REPAIR_TAG = "stale_revision_batch_void_2026_09_09"
AUDIT_EVENT_TYPE = "historical_cleanup"
NOT_NEEDED = "not_needed"
EVIDENCE_PATH = "/root/evidence/phase-6-pre-5/"

BATCH_IDS = (3, 4, 6)
BATCH_RESOLUTION = "stale_batch_voided_2026_09_09"
REVISION_LEG_TERMINAL = "cancelled"

#: Quoted rather than re-fetched at apply time: the facts are fixed, and a
#: second fetch could disagree with the one a person reviewed.
EXCHANGE_EVIDENCE = {
    "orders": ["1001124794741637", "1001124794741754"],
    "absent_from_pending": True,
    "fills": 0,
    "btc_long_positions": 0,
    "absent_from_history": True,
    "history_absence_means": "cannot_see_not_cancelled_retention_window",
    "ledger_terminal_since": "2026-08-31T14:40:59Z",
    "ledger_terminal_reason": "operator_cancelled_unfilled_entry_leg",
    "checked_at": "2026-09-09T16:10Z",
}


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


def _audit(session, *, table: str, row_id: int, changes, needed: bool) -> None:
    fingerprint = _fingerprint(table, row_id, list(changes))
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
            prior_state=(
                str(changes[0][1])[:32] if changes and changes[0][1] else None
            ),
            new_state=(str(changes[0][2])[:32] if changes else NOT_NEEDED),
            fingerprint=fingerprint,
            evidence_json=json.dumps(
                {
                    "repair": REPAIR_TAG,
                    "evidence_path": EVIDENCE_PATH,
                    "table": table,
                    "row_id": int(row_id),
                    "needed": bool(needed),
                    "exchange_evidence": EXCHANGE_EVIDENCE,
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

    plan: dict[str, Any] = {"changes": [], "skipped": []}
    for batch_id in BATCH_IDS:
        batch = session.get(StrategyRevisionBatch, int(batch_id))
        if batch is None:
            plan["skipped"].append(
                {"table": "strategy_revision_batches", "row_id": batch_id,
                 "reason": "row_missing"}
            )
            continue
        if str(batch.status) != "recovery_required":
            # Only the frozen shape this repair is about. Anything else has
            # been moved by somebody and is not ours to overwrite.
            plan["skipped"].append(
                {"table": "strategy_revision_batches", "row_id": batch_id,
                 "reason": "not_recovery_required",
                 "observed": str(batch.status)}
            )
            continue
        plan["changes"].append(
            {
                "table": "strategy_revision_batches",
                "row_id": int(batch_id),
                "changes": [
                    ("status", str(batch.status), "resolved"),
                    ("reason_code", batch.reason_code, BATCH_RESOLUTION),
                    ("completed_at", str(batch.completed_at or ""), "<now>"),
                ],
            }
        )
        for revision_leg in (
            session.query(StrategyRevisionLeg)
            .filter(StrategyRevisionLeg.revision_batch_id == int(batch_id))
            .order_by(StrategyRevisionLeg.id)
            .all()
        ):
            if str(revision_leg.status) in {"cancelled", "terminal", "retained"}:
                plan["skipped"].append(
                    {"table": "strategy_revision_legs", "row_id": int(revision_leg.id),
                     "reason": "already_terminal",
                     "observed": str(revision_leg.status)}
                )
                continue
            execution_leg = session.get(
                ExecutionOrderLeg, int(revision_leg.execution_order_leg_id)
            )
            # The revision leg is only settled by the execution leg being
            # settled. That is the fact this repair rests on; without it there
            # is nothing here but a guess about a three-week-old order.
            if execution_leg is None or str(execution_leg.status) != "cancelled":
                plan["skipped"].append(
                    {"table": "strategy_revision_legs", "row_id": int(revision_leg.id),
                     "reason": "execution_leg_not_cancelled",
                     "observed": (
                         str(execution_leg.status) if execution_leg else "missing"
                     )}
                )
                continue
            plan["changes"].append(
                {
                    "table": "strategy_revision_legs",
                    "row_id": int(revision_leg.id),
                    "changes": [
                        ("status", str(revision_leg.status), REVISION_LEG_TERMINAL)
                    ],
                }
            )
    return plan


def apply_plan(session, plan: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Apply exactly what the plan lists, re-checking each row as it goes."""

    applied: list[dict[str, Any]] = []
    for change in plan["changes"]:
        table = change["table"]
        row_id = int(change["row_id"])
        fields = change["changes"]
        if table == "strategy_revision_batches":
            row = session.get(StrategyRevisionBatch, row_id)
            # Compare-and-set on the state the plan was built from.
            if row is None or str(row.status) != fields[0][1]:
                continue
            row.status = "resolved"
            row.reason_code = BATCH_RESOLUTION
            row.completed_at = now
            row.updated_at = now
        elif table == "strategy_revision_legs":
            row = session.get(StrategyRevisionLeg, row_id)
            if row is None or str(row.status) != fields[0][1]:
                continue
            row.status = REVISION_LEG_TERMINAL
            row.error_json = json.dumps(
                {"repair": REPAIR_TAG, "evidence": EXCHANGE_EVIDENCE},
                ensure_ascii=False,
                sort_keys=True,
            )
            row.updated_at = now
        else:
            continue
        _audit(session, table=table, row_id=row_id, changes=fields, needed=True)
        applied.append({"table": table, "row_id": row_id})

    for skipped in plan["skipped"]:
        if skipped.get("row_id") is None:
            continue
        _audit(
            session,
            table=str(skipped["table"]),
            row_id=int(skipped["row_id"]),
            changes=[("status", skipped.get("observed"), NOT_NEEDED)],
            needed=False,
        )
    return {"applied": applied, "applied_count": len(applied)}
