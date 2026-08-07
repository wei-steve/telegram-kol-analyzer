#!/usr/bin/env python3
"""Read-only replay of durable adjacent-entry fragments (no exchange adapters)."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3


def _decimal(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def _compact(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _text_fragments(text: str) -> list[tuple[str, dict[str, str]]]:
    """Extract only explicit historical sizing phrases used by approved replay."""

    compact = re.sub(r"\s+", "", text or "")
    result: list[tuple[str, dict[str, str]]] = []
    percent = re.search(r"(?<!\d)(\d{1,3}(?:\.\d+)?)%仓位", compact)
    if percent:
        value = Decimal(percent.group(1)) / Decimal("100")
        if 0 < value <= 1:
            result.append(("risk_multiplier", {"risk_multiplier": _compact(value)}))
    elif "半仓" in compact:
        result.append(("risk_multiplier", {"risk_multiplier": "0.5"}))
    elif "全仓操作" in compact or "正常仓位操作" in compact:
        result.append(("risk_multiplier", {"risk_multiplier": "1"}))
    supplement = re.search(r"补仓[^0-9]{0,8}(\d+(?:\.\d+)?)", compact)
    if supplement:
        result.append(("supplemental_entry", {"entry_price": supplement.group(1)}))
    return result


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
        predicates = ["c.event_type = 'entry_signal'"]
        parameters: list[object] = []
        if chat_id is not None:
            predicates.append("target.chat_id = ?")
            parameters.append(chat_id)
        if message_ids:
            placeholders = ",".join("?" for _ in message_ids)
            predicates.append(f"target.message_id IN ({placeholders})")
            parameters.extend(sorted(message_ids))
        targets = connection.execute(
            f"""
            SELECT target.id, target.chat_id, target.message_id, target.posted_at,
                   c.symbol, c.side
            FROM signal_candidates AS c
            JOIN raw_messages AS target ON target.id = c.raw_message_id
            WHERE {' AND '.join(predicates)}
            ORDER BY target.chat_id, target.message_id LIMIT 501
            """,
            parameters,
        ).fetchall()
        if len(targets) > 500:
            raise RuntimeError("replay_target_limit_exceeded")
        raw_by_id: dict[int, tuple[object, ...]] = {}
        for _, target_chat, _, target_posted_at, _, _ in targets:
            nearby = connection.execute(
                "SELECT id, chat_id, message_id, posted_at, text FROM raw_messages "
                "WHERE chat_id=? AND datetime(posted_at) BETWEEN "
                "datetime(?, '-30 minutes') AND datetime(?, '+30 minutes') "
                "ORDER BY posted_at, message_id, id LIMIT 102",
                (target_chat, target_posted_at, target_posted_at),
            ).fetchall()
            if len(nearby) > 101:
                raise RuntimeError("replay_message_window_limit_exceeded")
            raw_by_id.update({int(row[0]): row for row in nearby})
        target_ids = [int(row[0]) for row in targets]
        relevant_raw_ids = sorted(raw_by_id)
        fragment_rows = []
        if target_ids and relevant_raw_ids:
            target_marks = ",".join("?" for _ in target_ids)
            raw_marks = ",".join("?" for _ in relevant_raw_ids)
            fragment_rows = connection.execute(
                "SELECT f.raw_message_id, f.target_strategy_raw_message_id, "
                "f.chat_id, f.symbol, f.side, f.fragment_kind, f.payload_json "
                "FROM entry_strategy_fragments AS f "
                "WHERE f.status IN ('pending','assembled','consumed') AND "
                f"(f.target_strategy_raw_message_id IN ({target_marks}) OR "
                f"(f.target_strategy_raw_message_id IS NULL AND f.raw_message_id IN ({raw_marks}))) "
                "ORDER BY f.id LIMIT 2001",
                [*target_ids, *relevant_raw_ids],
            ).fetchall()
            if len(fragment_rows) > 2000:
                raise RuntimeError("replay_fragment_limit_exceeded")

    grouped: dict[tuple[int, int], list[tuple[int, str, dict[str, str]]]] = defaultdict(list)
    for target_id, target_chat, target_message, target_posted_at, symbol, side in targets:
        key = (int(target_chat), int(target_message))
        for raw_id, explicit_target_id, fragment_chat, fragment_symbol, fragment_side, kind, payload_json in fragment_rows:
            if int(fragment_chat) != int(target_chat):
                continue
            if explicit_target_id is not None and int(explicit_target_id) != int(target_id):
                continue
            source = raw_by_id.get(int(raw_id))
            if source is None:
                continue
            if explicit_target_id is None:
                if (
                    str(fragment_symbol or "").upper() != str(symbol or "").upper()
                    or str(fragment_side or "").lower() != str(side or "").lower()
                ):
                    continue
                try:
                    source_time = datetime.fromisoformat(str(source[3])).timestamp()
                except ValueError:
                    continue
                eligible = []
                for candidate in targets:
                    if (
                        int(candidate[1]) != int(fragment_chat)
                        or str(candidate[4] or "").upper()
                        != str(fragment_symbol or "").upper()
                        or str(candidate[5] or "").lower()
                        != str(fragment_side or "").lower()
                    ):
                        continue
                    try:
                        candidate_time = datetime.fromisoformat(
                            str(candidate[3])
                        ).timestamp()
                    except ValueError:
                        continue
                    distance = abs(candidate_time - source_time)
                    if distance <= 1800:
                        eligible.append((distance, int(candidate[2]), int(candidate[0])))
                eligible.sort()
                if not eligible or eligible[0][2] != int(target_id):
                    continue
            try:
                payload = json.loads(payload_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                grouped[key].append((int(source[2]), str(kind), payload))
        try:
            target_time = datetime.fromisoformat(str(target_posted_at)).timestamp()
        except ValueError:
            continue
        existing_fragments = {
            (source_id, kind, json.dumps(payload, sort_keys=True))
            for source_id, kind, payload in grouped[key]
        }
        for _, source_chat, source_message_id, source_posted_at, text in raw_by_id.values():
            if int(source_chat) != int(target_chat):
                continue
            try:
                source_time = datetime.fromisoformat(str(source_posted_at)).timestamp()
            except ValueError:
                continue
            if abs(source_time - target_time) > 1800:
                continue
            for kind, payload in _text_fragments(str(text or "")):
                identity = (
                    int(source_message_id), kind, json.dumps(payload, sort_keys=True)
                )
                if identity not in existing_fragments:
                    grouped[key].append((int(source_message_id), kind, payload))
                    existing_fragments.add(identity)
    records: list[dict[str, object]] = []
    for (target_chat, target_message), fragments in sorted(grouped.items()):
        multipliers: set[Decimal] = set()
        allocations: list[str] = []
        supplemental: list[str] = []
        source_ids: list[int] = []
        for source_id, kind, payload in fragments:
            source_ids.append(source_id)
            if kind == "risk_multiplier":
                value = _decimal(payload.get("risk_multiplier"))
                if value is not None and value <= 1:
                    multipliers.add(value)
            elif kind == "leg_allocation":
                values = payload.get("allocations")
                if isinstance(values, list):
                    allocations = [str(value) for value in values[:5]]
            elif kind == "supplemental_entry":
                value = _decimal(payload.get("entry_price"))
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
