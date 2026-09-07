"""Phase 5a guard: regular-order actions may only touch this system's own legs.

``list_open_orders`` used to call an undocumented V1 endpoint that returned an
empty list for live regular orders, so every call site that acts on a pending
regular order has never actually seen one.  Switching to V2 makes those rows
visible for the first time, which means a cancellation path can now reach an
object this system did not submit.

This module is the explicit fail-closed boundary for that: a row may enter an
exchange write only when its exchange order id is recorded in
``execution_order_legs`` as a regular order submitted by this system.  Anything
else is recorded — log line plus a runtime incident — and dropped from the
action set, never cancelled and never modified.

Only *pre-action* reads may be filtered through here.  A post-action
confirmation read ("is our order gone?") must stay unfiltered: dropping rows
there would turn a foreign object's presence into a false "cancel confirmed".
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import ExecutionOrderLeg, utc_now
from telegram_kol_research.runtime_incidents import record_runtime_incident

logger = logging.getLogger(__name__)

#: ``execution_order_legs.order_kind`` values that denote an order submitted
#: through ``POST /deepcoin/trade/order`` rather than the trigger endpoint.
#: ``market`` is the only kind production writes today; ``limit`` is reserved
#: for the phase 5 regular limit entry and is listed so the guard does not have
#: to be relaxed under time pressure when that lands.
REGULAR_ORDER_LEG_KINDS = frozenset({"market", "limit"})

GUARD_POLICY_VERSION = "open-order-action-guard-v1"
GUARD_INCIDENT_TYPE = "open_order_guard_blocked"


def is_regular_order_leg_kind(order_kind: Any) -> bool:
    """Return whether ``order_kind`` denotes a regular (non-trigger) order."""

    return str(order_kind or "").strip().lower() in REGULAR_ORDER_LEG_KINDS


def open_order_identity(row: dict[str, Any]) -> tuple[str, str]:
    """Return ``(order_id, client_order_id)`` as the exchange reported them."""

    order_id = str(
        row.get("ordId") or row.get("orderId") or row.get("order_id") or ""
    ).strip()
    client_order_id = str(
        row.get("clOrdId")
        or row.get("clientOrderId")
        or row.get("client_order_id")
        or ""
    ).strip()
    return order_id, client_order_id


@dataclass(frozen=True, slots=True)
class GuardedOpenOrders:
    """Rows split into those this system may act on and those it may not."""

    allowed: tuple[dict[str, Any], ...]
    blocked: tuple[dict[str, Any], ...]

    def __iter__(self):
        return iter(self.allowed)

    def __len__(self) -> int:
        return len(self.allowed)


def _system_regular_order_ids(
    session_factory: sessionmaker,
    *,
    order_ids: set[str],
    client_order_ids: set[str],
) -> tuple[set[str], set[str]]:
    if not order_ids and not client_order_ids:
        return set(), set()
    predicates = []
    if order_ids:
        predicates.append(ExecutionOrderLeg.order_id.in_(sorted(order_ids)))
    if client_order_ids:
        predicates.append(
            ExecutionOrderLeg.client_order_id.in_(sorted(client_order_ids))
        )
    with session_factory() as session:
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.venue == "deepcoin")
            .filter(or_(*predicates))
            .all()
        )
        allowed_order_ids = {
            str(leg.order_id)
            for leg in legs
            if leg.order_id and is_regular_order_leg_kind(leg.order_kind)
        }
        allowed_client_order_ids = {
            str(leg.client_order_id)
            for leg in legs
            if leg.client_order_id and is_regular_order_leg_kind(leg.order_kind)
        }
    return allowed_order_ids, allowed_client_order_ids


def _record_blocked_row(
    session_factory: sessionmaker,
    *,
    row: dict[str, Any],
    action: str,
    instrument_id: str | None,
    occurred_at: datetime,
) -> None:
    order_id, client_order_id = open_order_identity(row)
    logger.warning(
        "open_order_guard_blocked action=%s instrument=%s ord_id=%s cl_ord_id=%s",
        action,
        instrument_id or "",
        order_id or "(none)",
        client_order_id or "(none)",
    )
    fingerprint = hashlib.sha256(
        f"{GUARD_INCIDENT_TYPE}:{action}:{instrument_id or ''}:"
        f"{order_id}:{client_order_id}".encode()
    ).hexdigest()
    try:
        record_runtime_incident(
            session_factory,
            source_kind="deepcoin_open_order",
            source_record_id=order_id or client_order_id or "unknown",
            incident_type=GUARD_INCIDENT_TYPE,
            severity="high",
            fingerprint=fingerprint,
            redacted_summary=json.dumps(
                {
                    "component": "open_order_action_guard",
                    "reason_code": "not_a_system_regular_order_leg",
                    "operation": action,
                    "containment": "no_exchange_write_attempted",
                }
            ),
            occurred_at=occurred_at,
            feature_policy_version=GUARD_POLICY_VERSION,
            prompt_version="none",
            tool_policy_version="no-exchange-write",
            diagnosis_json=json.dumps(
                {
                    "observed_state": {
                        "instrument_id": instrument_id or "",
                        "ord_id": order_id,
                        "cl_ord_id": client_order_id,
                        "ord_type": str(row.get("ordType") or ""),
                        "state": str(row.get("state") or ""),
                    }
                }
            ),
            evidence_refs_json=json.dumps(
                [f"deepcoin_open_order:{order_id or client_order_id or 'unknown'}"]
            ),
        )
    except Exception:  # pragma: no cover - evidence must never break the guard
        logger.exception(
            "open_order_guard_incident_record_failed action=%s ord_id=%s",
            action,
            order_id or "(none)",
        )


def guard_regular_open_orders(
    session_factory: sessionmaker,
    *,
    rows: list[dict[str, Any]] | None,
    action: str,
    instrument_id: str | None = None,
    occurred_at: datetime | None = None,
) -> GuardedOpenOrders:
    """Keep only rows this system submitted as regular orders.

    Every dropped row is logged and written to ``runtime_incidents``.  Call this
    on pre-action reads only; see the module docstring.
    """

    candidates = [row for row in (rows or []) if isinstance(row, dict)]
    if not candidates:
        return GuardedOpenOrders(allowed=(), blocked=())

    identities = [open_order_identity(row) for row in candidates]
    allowed_order_ids, allowed_client_order_ids = _system_regular_order_ids(
        session_factory,
        order_ids={order_id for order_id, _ in identities if order_id},
        client_order_ids={
            client_order_id for _, client_order_id in identities if client_order_id
        },
    )

    now = occurred_at or utc_now()
    allowed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row, (order_id, client_order_id) in zip(candidates, identities):
        if (order_id and order_id in allowed_order_ids) or (
            client_order_id and client_order_id in allowed_client_order_ids
        ):
            allowed.append(row)
            continue
        blocked.append(row)
        _record_blocked_row(
            session_factory,
            row=row,
            action=action,
            instrument_id=instrument_id,
            occurred_at=now,
        )
    return GuardedOpenOrders(allowed=tuple(allowed), blocked=tuple(blocked))
