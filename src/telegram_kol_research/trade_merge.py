"""Merge related parsed candidates into normalized trade ideas."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import RawMessage, SignalCandidate, TradeIdea, TradeUpdate


def _sort_events(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(candidates, key=lambda item: item.get("message_id", 0))


def merge_candidate_batch(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge candidate events into trade ideas, prioritizing reply-chain linkage."""

    trades: list[dict[str, Any]] = []
    trade_by_message_id: dict[int, dict[str, Any]] = {}

    for candidate in _sort_events(candidates):
        reply_to_message_id = candidate.get("reply_to_message_id")
        target_trade: dict[str, Any] | None = None

        if reply_to_message_id is not None:
            target_trade = trade_by_message_id.get(reply_to_message_id)

        if target_trade is None:
            matching_trades = [
                trade
                for trade in trades
                if trade.get("symbol") == candidate.get("symbol")
                and trade.get("side") == candidate.get("side")
                and trade.get("source_id") == candidate.get("source_id")
            ]
            if len(matching_trades) == 1:
                target_trade = matching_trades[0]
            elif len(matching_trades) > 1:
                candidate = {**candidate, "needs_review": True}

        if target_trade is None:
            target_trade = {
                "source_id": candidate.get("source_id"),
                "chat_id": candidate.get("chat_id"),
                "symbol": candidate.get("symbol"),
                "side": candidate.get("side"),
                "review_status": "pending" if candidate.get("needs_review") else "confirmed",
                "events": [],
            }
            trades.append(target_trade)

        target_trade["events"].append(candidate)
        if candidate.get("message_id") is not None:
            trade_by_message_id[candidate["message_id"]] = target_trade

        if candidate.get("needs_review"):
            target_trade["review_status"] = "pending"

    return trades


def persist_trade_ideas_from_candidates(session_factory: sessionmaker) -> dict[str, int]:
    """Merge persisted candidates into trade ideas and trade updates."""

    inserted_trade_ideas = 0
    inserted_trade_updates = 0

    with session_factory() as session:
        rows = (
            session.query(SignalCandidate, RawMessage)
            .join(RawMessage, SignalCandidate.raw_message_id == RawMessage.id)
            .order_by(RawMessage.chat_id, RawMessage.message_id)
            .all()
        )
        candidate_batches: dict[tuple[int | None, int | None], list[dict[str, Any]]] = {}

        for candidate, raw_message in rows:
            key = (raw_message.chat_id, candidate.source_id)
            candidate_batches.setdefault(key, []).append(
                {
                    "candidate_id": candidate.id,
                    "raw_message_id": raw_message.id,
                    "chat_id": raw_message.chat_id,
                    "message_id": raw_message.message_id,
                    "reply_to_message_id": raw_message.reply_to_message_id,
                    "source_id": candidate.source_id,
                    "symbol": candidate.symbol,
                    "side": candidate.side,
                    "event_type": candidate.event_type,
                    "confidence": candidate.confidence,
                }
            )

        for batch in candidate_batches.values():
            merged_trades = merge_candidate_batch(batch)
            for merged_trade in merged_trades:
                events = merged_trade["events"]
                primary_event = events[0]
                trade_idea = (
                    session.query(TradeIdea)
                    .filter(TradeIdea.primary_signal_candidate_id == primary_event["candidate_id"])
                    .one_or_none()
                )
                if trade_idea is None:
                    trade_idea = TradeIdea(
                        source_id=merged_trade.get("source_id"),
                        primary_signal_candidate_id=primary_event["candidate_id"],
                        chat_id=merged_trade.get("chat_id"),
                        symbol=merged_trade.get("symbol"),
                        side=merged_trade.get("side"),
                        status="open",
                        confidence=max(event.get("confidence", 0.0) for event in events),
                    )
                    session.add(trade_idea)
                    session.flush()
                    inserted_trade_ideas += 1
                else:
                    trade_idea.source_id = merged_trade.get("source_id")
                    trade_idea.chat_id = merged_trade.get("chat_id")
                    trade_idea.symbol = merged_trade.get("symbol")
                    trade_idea.side = merged_trade.get("side")
                    trade_idea.confidence = max(event.get("confidence", 0.0) for event in events)

                for event in events[1:]:
                    existing_update = (
                        session.query(TradeUpdate)
                        .filter(TradeUpdate.raw_message_id == event["raw_message_id"])
                        .one_or_none()
                    )
                    if existing_update is None:
                        session.add(
                            TradeUpdate(
                                trade_idea_id=trade_idea.id,
                                raw_message_id=event["raw_message_id"],
                                update_type=event["event_type"],
                                note=None,
                            )
                        )
                        inserted_trade_updates += 1

        session.commit()

    return {
        "inserted_trade_ideas": inserted_trade_ideas,
        "inserted_trade_updates": inserted_trade_updates,
    }
