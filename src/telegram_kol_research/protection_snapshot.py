"""Redacted completeness evidence for Deepcoin pending-TPSL reads."""

from __future__ import annotations

import json
from typing import Any, Iterable

from telegram_kol_research.models import PendingTpslSnapshotObservation


_PAGINATION_KEYS = frozenset({"cursor", "nextcursor", "page", "total", "hasmore"})


def observe_pending_tpsl(
    *,
    instrument_id: str,
    response: dict[str, Any],
    expected_order_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Return safe evidence; never infer that an incomplete list means absent TPSL."""

    normalized_keys = {str(key).lower() for key in response}
    unknown_pagination = bool(normalized_keys.intersection(_PAGINATION_KEYS))
    data = response.get("data")
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        return {
            "instrument_id": str(instrument_id).upper(),
            "complete": False,
            "reason": "invalid_pending_tpsl_schema",
            "order_ids": [],
            "expected_order_ids_visible": False,
        }
    order_ids = sorted({str(row.get("ordId") or row.get("orderId") or "").strip() for row in data} - {""})
    expected = {str(value).strip() for value in expected_order_ids if str(value).strip()}
    return {
        "instrument_id": str(instrument_id).upper(),
        "complete": not unknown_pagination,
        "reason": "pagination_metadata_unsupported" if unknown_pagination else None,
        "response_count": len(data),
        "order_ids": order_ids,
        "expected_order_ids_visible": expected.issubset(set(order_ids)) and not unknown_pagination,
    }


def record_pending_tpsl_observation(
    session_factory,
    *,
    observation: dict[str, Any],
    venue: str = "deepcoin",
) -> int:
    """Append a redacted pending-TPSL observation for later recovery audit."""

    order_ids = observation.get("order_ids")
    if not isinstance(order_ids, list):
        order_ids = []
    response_count = observation.get("response_count")
    with session_factory() as session:
        row = PendingTpslSnapshotObservation(
            venue=str(venue).lower(),
            instrument_id=str(observation.get("instrument_id") or "").upper(),
            response_count=(int(response_count) if isinstance(response_count, int) else None),
            order_ids_json=json.dumps(
                sorted({str(value) for value in order_ids if str(value)}),
                ensure_ascii=False,
            ),
            complete=bool(observation.get("complete")),
            reason=(str(observation["reason"]) if observation.get("reason") else None),
        )
        session.add(row)
        session.commit()
        return int(row.id)
