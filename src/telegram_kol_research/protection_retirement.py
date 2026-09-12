"""A-17: a closed binding's protection rows stop claiming to protect anything.

On 2026-09-11 the user closed two BTC longs by hand at 78611.6. The manual-close
sweep noticed within ninety seconds and marked both bindings ``closed`` -- and
left every protection row exactly as it was. Six exchange orders had been
voided with the positions; in ``position_protection_ledger`` all six were still
``verified``, and the protection legs still said ``verified`` with order ids
that no longer existed.

Measured before this was written: 553 ledger rows still ``verified`` and 919
protection legs still non-terminal, under 147 closed bindings, back to
2026-07-19. About twelve modules read ledger rows by ``status == "verified"``.
Whether any of them can reach a closed binding's row is not traced here (that
is A-17b), so this step only stops the pile growing: from now on, every place
that closes a binding retires that binding's protection in the same session.
The history is left alone on purpose.

``retired`` is a new terminal value. Neither table has a CHECK constraint on
``status``, and neither has a column for a retirement reason, so the reason is
merged into the row's existing evidence JSON as added keys -- never replacing
what is there, because a ledger row's ``evidence_json`` is the proof of the
protection it recorded and must survive the retirement.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from telegram_kol_research.models import (
    PositionProtectionLeg,
    PositionProtectionLedger,
)

RETIRED_STATUS = "retired"

#: Ledger states that claim a protection is in force. Written as the set being
#: retired *from*, not as "everything but X": a new state added later must be
#: a deliberate decision here, not silently swept up.
RETIRABLE_LEDGER_STATUSES = frozenset({"verified", "protected"})

#: Leg states that are still waiting on, or claiming, a live order. ``filled``
#: and ``cancelled`` are left as they are -- each already says what happened,
#: and overwriting ``filled`` would erase that a take profit actually traded.
RETIRABLE_LEG_STATUSES = frozenset(
    {"planned", "waiting_fill", "protection_recovery_pending", "verified"}
)


def _merged_json(existing: Any, **added: Any) -> str:
    """Return ``existing`` as a JSON object with ``added`` keys merged in."""

    payload: dict[str, Any] = {}
    if existing:
        try:
            parsed = json.loads(str(existing))
        except (TypeError, ValueError):
            # Unparseable evidence is kept verbatim under its own key rather
            # than dropped: retiring a row must never destroy what it held.
            payload = {"unparsed_evidence": str(existing)}
        else:
            payload = parsed if isinstance(parsed, dict) else {"evidence": parsed}
    payload.update(added)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def retire_protection_for_closed_binding(
    session,
    *,
    execution_binding_id: int,
    reason: str,
    retired_at: datetime,
    closed_by: str | None = None,
) -> tuple[int, int]:
    """Retire a closed binding's still-active ledger rows and protection legs.

    Returns ``(ledger_rows_retired, legs_retired)``. Runs inside the caller's
    session and does not commit: the binding and its protection change in the
    same transaction, or not at all.
    """

    stamp = retired_at.isoformat()
    # Which close site did it, so A-17b and any later reader can tell the
    # sweep from a management full close without joining back to the binding.
    added = {"retired_reason": reason, "retired_at": stamp}
    if closed_by:
        added["retired_by"] = closed_by
    ledger_rows = (
        session.query(PositionProtectionLedger)
        .filter(PositionProtectionLedger.execution_binding_id == int(execution_binding_id))
        .filter(PositionProtectionLedger.status.in_(sorted(RETIRABLE_LEDGER_STATUSES)))
        .all()
    )
    for row in ledger_rows:
        row.evidence_json = _merged_json(
            row.evidence_json,
            **added,
            retired_from_status=str(row.status),
        )
        row.status = RETIRED_STATUS
        row.updated_at = retired_at
    legs = (
        session.query(PositionProtectionLeg)
        .filter(PositionProtectionLeg.execution_binding_id == int(execution_binding_id))
        .filter(PositionProtectionLeg.status.in_(sorted(RETIRABLE_LEG_STATUSES)))
        .all()
    )
    for leg in legs:
        leg.readback_evidence_json = _merged_json(
            leg.readback_evidence_json,
            **added,
            retired_from_status=str(leg.status),
        )
        leg.status = RETIRED_STATUS
        leg.updated_at = retired_at
    return len(ledger_rows), len(legs)
