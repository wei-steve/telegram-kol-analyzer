"""A-16b: a second entry for a price we are already in must be asked about.

2026-09-10, 陈哥's group: message 10434 was the entry card ("BTC, around
77000, long, stop 75700, take-profit 79800-81900") and 10435, three minutes
later, opened with "BTC 再挂一笔限价中长线多单" and then enumerated the
resulting state as *two* positions -- the 78200 short-term one and the 77000
long-term one. The system read it as a second 77000 entry and opened one, so
that price carried 30 contracts instead of 15. 陈哥's own inventories on
2026-09-11 (messages 10443 and 10448) both say two BTC longs, 78200 and 77000
-- never three, never two at 77000.

Whether 10435 meant "place another" or was restating 10434 is a reading of
natural language, and the system is not the right thing to settle it. What the
system can do is notice that it is about to double an exposure it already has,
and ask. A-16-0 measured the shape: this is the only occurrence in the whole
database, so the question costs almost nothing and the answer is worth 15
contracts.

Deliberately narrow, and every conjunct is here rather than in a caller so
that the whole rule can be read in one place: the same chat, the same symbol,
the same side, the same entry price (compared as a number, never as text --
6f-1 spent three months on ``"75700.0" != "75700"``), and within two hours.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)

logger = logging.getLogger(__name__)

DUPLICATE_ENTRY_MARKER = "duplicate_entry_unconfirmed"
DUPLICATE_ENTRY_WINDOW = timedelta(hours=2)
#: Entry kinds this rule applies to: the ones the system opens by itself.
DUPLICATE_ENTRY_ORDER_KINDS = frozenset({"limit", "trigger_limit", "market"})


def _decimal_or_none(value: Any) -> Decimal | None:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _request_entry_price(leg: ExecutionOrderLeg) -> Decimal | None:
    """The price the venue was actually asked for, as a number."""

    try:
        request = json.loads(str(leg.request_json or "{}"))
    except (TypeError, ValueError):
        return None
    if not isinstance(request, dict):
        return None
    for key in ("px", "price", "triggerPx"):
        parsed = _decimal_or_none(request.get(key))
        if parsed is not None:
            return parsed
    return None


def find_duplicate_entry_binding(
    session,
    *,
    raw_message_id: int,
    candidate: SignalCandidate,
    now: datetime,
) -> ExecutionBinding | None:
    """Return an existing binding this entry would duplicate, or None.

    Numbers are compared as numbers. The window is measured from the existing
    entry leg's own ``created_at``, not from the message, because the thing
    being protected is the exposure, and the exposure starts when the leg was
    submitted.
    """

    raw_message = session.get(RawMessage, int(raw_message_id))
    if raw_message is None:
        return None
    symbol = str(candidate.symbol or "").upper()
    side = str(candidate.side or "").lower()
    if not symbol or not side:
        return None
    entry_price = _decimal_or_none(candidate.entry_text)
    if entry_price is None:
        return None
    cutoff = now - DUPLICATE_ENTRY_WINDOW
    rows = (
        session.query(ExecutionBinding, ExecutionOrderLeg)
        .join(
            ExecutionOrderLeg,
            ExecutionOrderLeg.execution_binding_id == ExecutionBinding.id,
        )
        .filter(
            ExecutionBinding.chat_id == int(raw_message.chat_id),
            ExecutionOrderLeg.purpose == "entry",
            ExecutionOrderLeg.created_at >= cutoff,
        )
        .order_by(ExecutionOrderLeg.id.asc())
        .all()
    )
    for binding, leg in rows:
        if str(binding.symbol or "").upper() != symbol:
            continue
        if str(binding.side or "").lower() != side:
            continue
        if str(leg.order_kind or "") not in DUPLICATE_ENTRY_ORDER_KINDS:
            continue
        if int(binding.message_id or 0) == int(raw_message.message_id or -1):
            # The same message's own leg is not a duplicate of itself.
            continue
        existing_price = _request_entry_price(leg)
        if existing_price is None or existing_price != entry_price:
            continue
        return binding
    return None


def park_duplicate_entry(
    session_factory: sessionmaker,
    *,
    message_instruction_item_id: int,
    duplicate_of_execution_binding_id: int,
    duplicate_of_message_id: int,
    duplicate_symbol: str,
    duplicate_side: str,
    now: datetime,
) -> bool:
    """Park the item awaiting confirmation instead of opening a position.

    Returns whether it was parked. Never raises into the execution path: an
    item left ``executing`` because the park threw would be worse than the
    duplicate it was trying to prevent, so a failure here is logged and the
    caller proceeds exactly as it does today.

    Takes plain values rather than the ORM rows. The first version took an
    ``ExecutionBinding``, and the caller -- which had already closed its
    session -- built a four-attribute stand-in class to satisfy it. That
    stand-in is narrower than the real row, so the day this function reads a
    fifth field it raises ``AttributeError`` in production while every test
    passes, because the tests pass real rows. B line hit the same shape on
    2026-09-12 with a test stub that omitted ``max_pages`` and hid a page
    budget that made a whole module structurally unable to fire.
    """

    from telegram_kol_research.management_target_verification import (
        AWAITING_CONFIRMATION,
    )

    try:
        with session_factory() as session:
            row = session.get(
                MessageInstructionItem, int(message_instruction_item_id)
            )
            if row is None:
                return False
            result: dict[str, Any] = {}
            if row.result_json:
                try:
                    parsed = json.loads(str(row.result_json))
                    result = parsed if isinstance(parsed, dict) else {}
                except (TypeError, ValueError):
                    result = {}
            result[DUPLICATE_ENTRY_MARKER] = True
            result["duplicate_of_execution_binding_id"] = int(
                duplicate_of_execution_binding_id
            )
            result["duplicate_of_message_id"] = int(duplicate_of_message_id)
            result["duplicate_symbol"] = str(duplicate_symbol or "")
            result["duplicate_side"] = str(duplicate_side or "")
            row.result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
            row.status = AWAITING_CONFIRMATION
            row.last_progress_at = now
            row.updated_at = now
            session.commit()
        return True
    except Exception:
        logger.warning(
            "duplicate entry park failed item=%s binding=%s",
            int(message_instruction_item_id),
            int(duplicate_of_execution_binding_id),
            exc_info=True,
        )
        return False
