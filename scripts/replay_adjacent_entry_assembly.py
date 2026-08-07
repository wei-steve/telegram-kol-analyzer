#!/usr/bin/env python3
"""Read-only replay of durable adjacent-entry fragments (no exchange adapters)."""

from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sqlite3


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _compact(value: Decimal) -> str:
    return format(value, "f").rstrip("0").rstrip(".") if "." in format(value, "f") else format(value, "f")


def replay_database(
    database_path: str | Path,
    *,
    configured_risk_usdt: Decimal = Decimal("20"),
    chat_id: int | None = None,
    message_ids: set[int] | None = None,
) -> dict[str, object]:
    """Return canonical proposals from persisted evidence through SQLite mode=ro."""

    uri = f"file:{Path(database_path)}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(
            """
            SELECT target.chat_id, target.message_id, source.message_id,
                   f.fragment_kind, f.payload_json, f.source_relationship
            FROM entry_strategy_fragments AS f
            JOIN raw_messages AS source ON source.id = f.raw_message_id
            JOIN raw_messages AS target ON target.id = f.target_strategy_raw_message_id
            WHERE f.status IN ('pending','assembled','consumed')
            ORDER BY target.chat_id, target.message_id, source.message_id, f.id
            """
        ).fetchall()
    grouped: dict[tuple[int, int], list[tuple[int, str, str, str]]] = defaultdict(list)
    for target_chat, target_message, source_message, kind, payload, relationship in rows:
        if chat_id is not None and int(target_chat) != chat_id:
            continue
        if message_ids and int(target_message) not in message_ids:
            continue
        grouped[(int(target_chat), int(target_message))].append(
            (int(source_message), str(kind), str(payload), str(relationship))
        )
    records: list[dict[str, object]] = []
    for (target_chat, target_message), fragments in sorted(grouped.items()):
        multipliers: set[Decimal] = set()
        allocations: list[str] = []
        supplemental: list[str] = []
        source_ids: list[int] = []
        for source_id, kind, payload_json, _relationship in fragments:
            source_ids.append(source_id)
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            if kind == "risk_multiplier":
                value = _decimal(payload.get("risk_multiplier"))
                if value is not None and value <= 1:
                    multipliers.add(value)
            elif kind == "leg_allocation":
                values = payload.get("allocations")
                if isinstance(values, list):
                    allocations = [str(value) for value in values[:5]]
            elif kind == "supplemental_entry":
                value = _decimal(payload.get("price"))
                if value is not None:
                    supplemental.append(_compact(value))
        decision = "blocked" if len(multipliers) > 1 else "proposed"
        multiplier = next(iter(multipliers), Decimal("1")) if decision == "proposed" else Decimal("1")
        effective = configured_risk_usdt * multiplier
        records.append(
            {
                "chat_id": target_chat,
                "strategy_message_id": target_message,
                "source_message_ids": sorted(set(source_ids)),
                "decision": decision,
                "configured_risk_budget_usdt": _compact(configured_risk_usdt),
                "risk_multiplier": _compact(multiplier),
                "effective_risk_budget_usdt": _compact(effective),
                "entry_allocations": allocations,
                "supplemental_entry_prices": supplemental,
            }
        )
    return {"mode": "read_only", "records": records}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-path", required=True)
    parser.add_argument("--configured-risk-usdt", default="20")
    parser.add_argument("--chat-id", type=int)
    parser.add_argument("--message-id", type=int, action="append")
    args = parser.parse_args()
    configured = _decimal(args.configured_risk_usdt)
    if configured is None:
        parser.error("--configured-risk-usdt must be positive")
    result = replay_database(
        args.database_path,
        configured_risk_usdt=configured,
        chat_id=args.chat_id,
        message_ids=set(args.message_id or ()),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
