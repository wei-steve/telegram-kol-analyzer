#!/usr/bin/env python3
"""Read-only replay of one persisted entry-preamble/strategy pair."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3

from telegram_kol_research.entry_strategy_assembly import (
    PriorMessageFact,
    select_entry_preamble,
)


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _posted_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def replay_entry_preamble_shadow(
    database_path: str | Path,
    *,
    preamble_raw_message_id: int,
    strategy_raw_message_id: int,
    configured_risk_usdt: Decimal,
) -> dict[str, object]:
    if not configured_risk_usdt.is_finite() or configured_risk_usdt <= 0:
        raise ValueError("configured risk must be a positive finite decimal")
    path = Path(database_path).resolve()
    uri = f"file:{path}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        preamble = connection.execute(
            """
            SELECT p.id, p.chat_id, p.message_id, p.symbol, p.side,
                   p.risk_multiplier, p.status, r.posted_at
            FROM entry_preambles AS p
            JOIN raw_messages AS r ON r.id = p.raw_message_id
            WHERE p.raw_message_id = ?
            """,
            (int(preamble_raw_message_id),),
        ).fetchone()
        strategy = connection.execute(
            """
            SELECT r.chat_id, r.message_id, r.posted_at,
                   c.symbol, c.side
            FROM raw_messages AS r
            JOIN signal_candidates AS c ON c.raw_message_id = r.id
            WHERE r.id = ? AND c.event_type = 'entry_signal'
            ORDER BY c.id
            """,
            (int(strategy_raw_message_id),),
        ).fetchall()
    if preamble is None:
        raise LookupError("preamble evidence not found")
    if len(strategy) != 1:
        raise LookupError("strategy must have exactly one entry candidate")
    if str(preamble[6]) != "pending":
        raise ValueError("preamble is not pending")
    if int(preamble[1]) != int(strategy[0][0]):
        raise ValueError("preamble and strategy are from different chats")
    try:
        multiplier = Decimal(str(preamble[5]))
    except InvalidOperation as exc:
        raise ValueError("persisted multiplier is invalid") from exc
    decision = select_entry_preamble(
        strategy_posted_at=_posted_at(strategy[0][2]),
        strategy_message_id=int(strategy[0][1]),
        strategy_raw_message_id=int(strategy_raw_message_id),
        symbol=str(strategy[0][3]),
        side=str(strategy[0][4]),
        prior_facts=[
            PriorMessageFact(
                raw_message_id=int(preamble_raw_message_id),
                message_id=int(preamble[2]),
                posted_at=_posted_at(preamble[7]),
                kind="entry_preamble",
                symbol=str(preamble[3]),
                side=str(preamble[4]),
                preamble_id=int(preamble[0]),
                risk_multiplier=multiplier,
            )
        ],
    )
    proposed = configured_risk_usdt * decision.risk_multiplier
    return {
        "configured_risk_budget_usdt": _decimal_text(configured_risk_usdt),
        "decision": "proposed" if decision.status == "ready" else decision.status,
        "mode": "shadow",
        "preamble_message_id": int(preamble[2]),
        "proposed_effective_risk_budget_usdt": _decimal_text(proposed),
        "reason_codes": [decision.reason_code] if decision.reason_code else [],
        "risk_multiplier": _decimal_text(decision.risk_multiplier),
        "side": str(strategy[0][4]).lower(),
        "strategy_message_id": int(strategy[0][1]),
        "symbol": str(strategy[0][3]).upper(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-path", required=True)
    parser.add_argument("--preamble-raw-message-id", required=True, type=int)
    parser.add_argument("--strategy-raw-message-id", required=True, type=int)
    parser.add_argument("--configured-risk-usdt", required=True)
    args = parser.parse_args()
    try:
        configured = Decimal(args.configured_risk_usdt)
    except InvalidOperation as exc:
        raise SystemExit("configured risk must be a decimal") from exc
    payload = replay_entry_preamble_shadow(
        args.database_path,
        preamble_raw_message_id=args.preamble_raw_message_id,
        strategy_raw_message_id=args.strategy_raw_message_id,
        configured_risk_usdt=configured,
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
