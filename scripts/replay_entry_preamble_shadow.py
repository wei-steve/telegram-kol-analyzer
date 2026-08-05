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
        if preamble is not None and len(strategy) == 1:
            history = connection.execute(
                """
                SELECT id, message_id, posted_at
                FROM raw_messages WHERE chat_id = ?
                """,
                (int(strategy[0][0]),),
            ).fetchall()
            raw_by_id = {int(row[0]): row for row in history}
            raw_ids = tuple(raw_by_id)
            placeholders = ",".join("?" for _ in raw_ids)
            preamble_rows = connection.execute(
                f"""
                SELECT id, raw_message_id, symbol, side, risk_multiplier
                FROM entry_preambles
                WHERE status = 'pending' AND raw_message_id IN ({placeholders})
                """,
                raw_ids,
            ).fetchall()
            candidate_rows = connection.execute(
                f"""
                SELECT raw_message_id, symbol, side, event_type
                FROM signal_candidates
                WHERE raw_message_id IN ({placeholders})
                """,
                raw_ids,
            ).fetchall()
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            claim_rows = (
                connection.execute(
                    f"""
                    SELECT raw_message_id
                    FROM message_evidence_extraction_claims
                    WHERE raw_message_id IN ({placeholders})
                      AND lease_expires_at > CURRENT_TIMESTAMP
                    """,
                    raw_ids,
                ).fetchall()
                if "message_evidence_extraction_claims" in tables
                else []
            )
        else:
            raw_by_id = {}
            preamble_rows = []
            candidate_rows = []
            claim_rows = []
    if preamble is None:
        raise LookupError("preamble evidence not found")
    if len(strategy) != 1:
        raise LookupError("strategy must have exactly one entry candidate")
    if str(preamble[6]) != "pending":
        raise ValueError("preamble is not pending")
    if int(preamble[1]) != int(strategy[0][0]):
        raise ValueError("preamble and strategy are from different chats")
    facts: list[PriorMessageFact] = []
    for row in preamble_rows:
        raw = raw_by_id[int(row[1])]
        try:
            multiplier = Decimal(str(row[4]))
        except InvalidOperation:
            multiplier = Decimal("1")
        facts.append(
            PriorMessageFact(
                raw_message_id=int(row[1]),
                message_id=int(raw[1]),
                posted_at=_posted_at(raw[2]),
                kind="entry_preamble",
                symbol=str(row[2]),
                side=str(row[3]),
                preamble_id=int(row[0]),
                risk_multiplier=multiplier,
            )
        )
    for raw_id, symbol, side, event_type in candidate_rows:
        kind = {
            "entry_signal": "complete_entry",
            "strategy_revision": "replacement",
            "close_signal": "cancel_entry",
        }.get(str(event_type))
        if kind is None:
            continue
        raw = raw_by_id[int(raw_id)]
        facts.append(
            PriorMessageFact(
                raw_message_id=int(raw_id),
                message_id=int(raw[1]),
                posted_at=_posted_at(raw[2]),
                kind=kind,
                symbol=str(symbol or "") or None,
                side=str(side or "") or None,
            )
        )
    for (raw_id,) in claim_rows:
        raw = raw_by_id[int(raw_id)]
        facts.append(
            PriorMessageFact(
                raw_message_id=int(raw_id),
                message_id=int(raw[1]),
                posted_at=_posted_at(raw[2]),
                kind="unresolved",
            )
        )
    decision = select_entry_preamble(
        strategy_posted_at=_posted_at(strategy[0][2]),
        strategy_message_id=int(strategy[0][1]),
        strategy_raw_message_id=int(strategy_raw_message_id),
        symbol=str(strategy[0][3]),
        side=str(strategy[0][4]),
        prior_facts=facts,
    )
    proposed = configured_risk_usdt * decision.risk_multiplier
    return {
        "configured_risk_budget_usdt": _decimal_text(configured_risk_usdt),
        "decision": "proposed" if decision.status == "ready" else decision.status,
        "mode": "shadow",
        "preamble_message_id": (
            int(preamble[2]) if decision.preamble_id == int(preamble[0]) else None
        ),
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
