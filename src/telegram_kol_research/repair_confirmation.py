"""Durable globally single-use confirmation tokens for repair writes."""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import RepairConfirmationToken


def consume_repair_confirmation_token(
    session_factory: sessionmaker,
    *,
    confirmation_token: str,
    action_kind: str,
    action_id: str,
    pos_id: str,
    consumed_at: datetime,
) -> None:
    clean_token = str(confirmation_token or "").strip()
    if len(clean_token) < 8:
        raise ValueError("confirmation_token is required")
    token_hash = hashlib.sha256(clean_token.encode("utf-8")).hexdigest()
    with session_factory() as session:
        session.add(
            RepairConfirmationToken(
                token_hash=token_hash,
                action_kind=str(action_kind),
                action_id=str(action_id),
                pos_id=str(pos_id),
                consumed_at=consumed_at,
            )
        )
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ValueError(
                "confirmation_token already consumed"
            ) from exc


def require_repair_confirmation_token_unused(
    session_factory: sessionmaker,
    *,
    confirmation_token: str,
) -> None:
    clean_token = str(confirmation_token or "").strip()
    if len(clean_token) < 8:
        raise ValueError("confirmation_token is required")
    token_hash = hashlib.sha256(clean_token.encode("utf-8")).hexdigest()
    with session_factory() as session:
        if (
            session.query(RepairConfirmationToken)
            .filter(RepairConfirmationToken.token_hash == token_hash)
            .first()
            is not None
        ):
            raise ValueError("confirmation_token already consumed")
