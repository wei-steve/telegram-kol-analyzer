"""Persistence helpers for restart recovery dry-run decisions."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import RecoveryDecisionRecord
from telegram_kol_research.recovery_scan import RecoveryEvaluation


ALLOWED_RECOVERY_REVIEW_STATUSES = {"approved_for_order", "ignored"}


def persist_recovery_evaluations(
    session_factory: sessionmaker,
    evaluations: list[RecoveryEvaluation],
    *,
    run_at: datetime,
) -> dict[str, int]:
    """Upsert recovery evaluation results for later review."""

    upserted = 0
    normalized_run_at = _storage_time(run_at)
    with session_factory() as session:
        for evaluation in evaluations:
            signal = evaluation.signal
            decision = evaluation.decision
            symbol = (signal.symbol or "").upper()
            side = (signal.side or "").lower()
            record = (
                session.query(RecoveryDecisionRecord)
                .filter(
                    RecoveryDecisionRecord.chat_id == signal.chat_id,
                    RecoveryDecisionRecord.message_id == signal.message_id,
                    RecoveryDecisionRecord.symbol == symbol,
                    RecoveryDecisionRecord.side == side,
                )
                .one_or_none()
            )
            if record is None:
                record = RecoveryDecisionRecord(
                    kol_id=signal.kol_id,
                    chat_id=signal.chat_id,
                    message_id=signal.message_id,
                    symbol=symbol,
                    side=side,
                    action=decision.action,
                    reason_codes_json="[]",
                    run_at=normalized_run_at,
                )
                session.add(record)
                session.flush()

            record.kol_id = signal.kol_id
            record.action = decision.action
            record.reason_codes_json = json.dumps(
                decision.reason_codes,
                ensure_ascii=False,
                separators=(",", ": "),
            )
            record.entry_range_text = _format_entry_range(decision.entry_range)
            record.stop_loss_text = signal.stop_loss_text
            record.max_loss_usdt = decision.max_loss_usdt
            record.run_at = normalized_run_at
            record.updated_at = datetime.now(UTC)
            upserted += 1

        session.commit()

    return {"upserted": upserted}


def list_recovery_decisions(
    session_factory: sessionmaker,
    *,
    limit: int = 100,
) -> list[dict[str, object]]:
    """Return recent recovery decisions with decoded reason codes."""

    with session_factory() as session:
        rows = (
            session.query(RecoveryDecisionRecord)
            .order_by(RecoveryDecisionRecord.run_at.desc(), RecoveryDecisionRecord.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "kol_id": row.kol_id,
                "chat_id": row.chat_id,
                "message_id": row.message_id,
                "symbol": row.symbol,
                "side": row.side,
                "action": row.action,
                "reason_codes": json.loads(row.reason_codes_json or "[]"),
                "entry_range_text": row.entry_range_text,
                "stop_loss_text": row.stop_loss_text,
                "max_loss_usdt": row.max_loss_usdt,
                "review_status": row.review_status,
                "reviewed_at": row.reviewed_at,
                "review_note": row.review_note,
            }
            for row in rows
        ]


def apply_recovery_review_decision(
    session_factory: sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    review_status: str,
    note: str | None = None,
    reviewed_at: datetime | None = None,
) -> dict[str, object]:
    """Record a manual recovery review decision without placing an order."""

    if review_status not in ALLOWED_RECOVERY_REVIEW_STATUSES:
        raise ValueError(f"unsupported recovery review status: {review_status}")

    normalized_symbol = symbol.upper()
    normalized_side = side.lower()
    normalized_reviewed_at = _storage_time(reviewed_at or datetime.now(UTC))
    with session_factory() as session:
        record = (
            session.query(RecoveryDecisionRecord)
            .filter(
                RecoveryDecisionRecord.chat_id == chat_id,
                RecoveryDecisionRecord.message_id == message_id,
                RecoveryDecisionRecord.symbol == normalized_symbol,
                RecoveryDecisionRecord.side == normalized_side,
            )
            .one_or_none()
        )
        if record is None:
            raise LookupError("recovery decision not found")

        record.review_status = review_status
        record.reviewed_at = normalized_reviewed_at
        record.review_note = note
        record.updated_at = datetime.now(UTC)
        session.commit()
        return _row_to_dict(record)


def _format_entry_range(entry_range: tuple[float, float] | None) -> str | None:
    if entry_range is None:
        return None
    return f"{entry_range[0]:g}-{entry_range[1]:g}"


def _row_to_dict(row: RecoveryDecisionRecord) -> dict[str, object]:
    return {
        "kol_id": row.kol_id,
        "chat_id": row.chat_id,
        "message_id": row.message_id,
        "symbol": row.symbol,
        "side": row.side,
        "action": row.action,
        "reason_codes": json.loads(row.reason_codes_json or "[]"),
        "entry_range_text": row.entry_range_text,
        "stop_loss_text": row.stop_loss_text,
        "max_loss_usdt": row.max_loss_usdt,
        "review_status": row.review_status,
        "reviewed_at": row.reviewed_at,
        "review_note": row.review_note,
    }


def _storage_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
