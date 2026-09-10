"""Return a live position to management (A-10a option (a); PREPARED, NOT RUN).

pos 1001125178552543 is a BTC short of three lots in an auto_trade group. It
has been on the exchange since 2026-09-08 01:23:07 with both its stops armed,
and since 2026-09-08 01:23:03 -- four seconds before it existed -- the ledger
has said it is closed. The sweep that wrote it off ran later in that same
round, stamped with the round's opening timestamp, and its positions read did
not list a position the round had already claimed from the order side and
already armed a stop for. Nothing has managed it since: A-7's
gate builds its verified set from bindings that are ``active``, so a KOL
instruction naming this position is refused as unverifiable, and neither
break-even nor take-profit convergence will touch it.

This puts three rows back the way the exchange says they should be. It does
**not** write to the exchange, and it does not re-run anything: the position is
already protected, and the point is only that the system is allowed to see it
again.

**Not to be run without an explicit approval**, and only through a runner that
takes a fresh backup, rehearses on a copy, and re-reads the exchange
immediately before applying -- if the position has closed in the meantime, this
must not run at all, and the guard below refuses it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    StrategyLifecycle,
)

REPAIR_TAG = "reclaim_binding_343_2026_09_09"
AUDIT_EVENT_TYPE = "historical_cleanup"
EVIDENCE_PATH = "/root/evidence/step10a-reclaim/"

POS_ID = "1001125178552543"
BINDING_ID = 343
LEG_ID = 589
LIFECYCLE_ID = 1109
INSTRUMENT_ID = "BTC-USDT-SWAP"
#: The two stops the ledger already calls verified. Both must still be on the
#: exchange when this runs: reclaiming an unprotected position would be a
#: worse state than leaving it alone.
EXPECTED_STOP_ORDER_IDS = ("1001125178552542", "1001125178555463")


class ReclaimRefused(RuntimeError):
    """The exchange no longer looks the way this repair assumes."""


def verify_exchange_preconditions(client: Any) -> dict[str, Any]:
    """Re-read the venue and refuse unless it still says what A-10a found.

    Three things must hold: the position is open with the size we recorded,
    and both stop orders are still pending. Anything else and the repair is
    not the right repair any more.
    """

    positions = client.list_positions()
    if not positions:
        raise ReclaimRefused("positions snapshot is empty; read proves nothing")
    live = None
    for row in positions:
        if str(row.get("posId") or "") == POS_ID:
            live = row
            break
    if live is None:
        raise ReclaimRefused(f"position {POS_ID} is no longer open")
    size = str(live.get("pos") or "").strip()
    if not size or size.lstrip("-").rstrip("0").rstrip(".") == "":
        raise ReclaimRefused(f"position {POS_ID} has no live size ({size!r})")

    pending = client.list_trigger_orders_pending(inst_id=INSTRUMENT_ID)
    # These rows carry no ``posId``; they are matched by order id, which is
    # what the ledger recorded. Reading them by position would find nothing --
    # the mistake A-10a nearly made.
    present = {
        str(row.get("ordId") or row.get("orderId") or "")
        for row in pending
        if isinstance(row, dict)
    }
    missing = [order_id for order_id in EXPECTED_STOP_ORDER_IDS if order_id not in present]
    if missing:
        raise ReclaimRefused(f"stop orders no longer pending: {','.join(missing)}")
    return {
        "pos_id": POS_ID,
        "live_size": size,
        "avg_price": str(live.get("avgPx") or ""),
        "stops_present": list(EXPECTED_STOP_ORDER_IDS),
        "verified_at": None,
    }


def build_plan(session) -> dict[str, Any]:
    """What would change, read from the database, deciding nothing."""

    plan: dict[str, Any] = {"changes": [], "skipped": []}
    leg = session.get(ExecutionOrderLeg, LEG_ID)
    if leg is None or str(leg.pos_id or "") != POS_ID:
        plan["skipped"].append({"table": "execution_order_legs", "row_id": LEG_ID,
                                "reason": "row_missing_or_pos_id_mismatch"})
    elif str(leg.status) != "manually_closed":
        plan["skipped"].append({"table": "execution_order_legs", "row_id": LEG_ID,
                                "reason": f"unexpected_status:{leg.status}"})
    else:
        plan["changes"].append({
            "table": "execution_order_legs", "row_id": LEG_ID,
            "changes": [("status", str(leg.status), "active"),
                        ("terminal_reason", leg.terminal_reason, None)],
        })

    binding = session.get(ExecutionBinding, BINDING_ID)
    if binding is None or str(binding.pos_id or "") != POS_ID:
        plan["skipped"].append({"table": "execution_bindings", "row_id": BINDING_ID,
                                "reason": "row_missing_or_pos_id_mismatch"})
    elif str(binding.status) != "closed":
        plan["skipped"].append({"table": "execution_bindings", "row_id": BINDING_ID,
                                "reason": f"unexpected_status:{binding.status}"})
    else:
        plan["changes"].append({
            "table": "execution_bindings", "row_id": BINDING_ID,
            "changes": [("status", str(binding.status), "active"),
                        ("last_exchange_status", binding.last_exchange_status,
                         "position_ownership_verified")],
        })

    lifecycle = session.get(StrategyLifecycle, LIFECYCLE_ID)
    if lifecycle is None or int(lifecycle.execution_binding_id or 0) != BINDING_ID:
        plan["skipped"].append({"table": "strategy_lifecycles", "row_id": LIFECYCLE_ID,
                                "reason": "row_missing_or_binding_mismatch"})
    elif str(lifecycle.lifecycle_status) != "exited":
        plan["skipped"].append({"table": "strategy_lifecycles", "row_id": LIFECYCLE_ID,
                                "reason": f"unexpected_status:{lifecycle.lifecycle_status}"})
    else:
        plan["changes"].append({
            "table": "strategy_lifecycles", "row_id": LIFECYCLE_ID,
            "changes": [("lifecycle_status", str(lifecycle.lifecycle_status), "entered"),
                        ("exit_reason", lifecycle.exit_reason, None),
                        ("exited_at", str(lifecycle.exited_at or ""), None)],
        })
    return plan


def apply_plan(session, plan: dict[str, Any], *, now: datetime,
               exchange_evidence: dict[str, Any]) -> dict[str, Any]:
    """Apply exactly what the plan lists, re-checking each row as it goes."""

    applied: list[dict[str, Any]] = []
    for change in plan["changes"]:
        table, row_id, fields = change["table"], int(change["row_id"]), change["changes"]
        if table == "execution_order_legs":
            row = session.get(ExecutionOrderLeg, row_id)
            if row is None or str(row.status) != fields[0][1]:
                continue
            row.status = "active"
            row.terminal_reason = None
            row.updated_at = now
        elif table == "execution_bindings":
            row = session.get(ExecutionBinding, row_id)
            if row is None or str(row.status) != fields[0][1]:
                continue
            row.status = "active"
            row.last_exchange_status = "position_ownership_verified"
            row.recovered_at = now
            row.updated_at = now
        elif table == "strategy_lifecycles":
            row = session.get(StrategyLifecycle, row_id)
            if row is None or str(row.lifecycle_status) != fields[0][1]:
                continue
            row.lifecycle_status = "entered"
            row.exit_reason = None
            row.exited_at = None
            row.updated_at = now
        else:
            continue
        _audit(session, table=table, row_id=row_id, changes=fields,
               exchange_evidence=exchange_evidence)
        applied.append({"table": table, "row_id": row_id})
    session.flush()
    return {"applied": applied}


def _audit(session, *, table: str, row_id: int, changes: list[tuple[str, Any, Any]],
           exchange_evidence: dict[str, Any]) -> None:
    fingerprint = hashlib.sha256(
        json.dumps({"repair": REPAIR_TAG, "table": table, "row_id": row_id},
                   sort_keys=True).encode("utf-8")
    ).hexdigest()
    if (
        session.query(PositionAttributionAudit)
        .filter(PositionAttributionAudit.fingerprint == fingerprint)
        .one_or_none()
        is not None
    ):
        return
    session.add(
        PositionAttributionAudit(
            venue="deepcoin",
            pos_id=f"{table}:{row_id}",
            event_type=AUDIT_EVENT_TYPE,
            prior_state=str(changes[0][1])[:32] if changes and changes[0][1] else None,
            new_state=str(changes[0][2])[:32] if changes and changes[0][2] else "cleared",
            fingerprint=fingerprint,
            evidence_json=json.dumps(
                {
                    "repair": REPAIR_TAG,
                    "evidence_path": EVIDENCE_PATH,
                    "table": table,
                    "row_id": row_id,
                    "why": (
                        "written off by a single positions snapshot on "
                        "2026-09-08T01:23:03Z; the position never left the "
                        "exchange (A-10a)"
                    ),
                    "exchange_evidence": exchange_evidence,
                    "changes": [
                        {"field": name, "before": before, "after": after}
                        for name, before, after in changes
                    ],
                    "no_exchange_write": True,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
    )
