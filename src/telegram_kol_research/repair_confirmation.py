"""Durable globally single-use confirmation tokens for repair writes."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import RepairConfirmationToken


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def consume_bound_entry_drain_confirmation_token(
    session_factory: sessionmaker,
    *,
    confirmation_token: str,
    authority_action_id: str,
    target_order_id: str,
    plan_sha256: str,
    evidence_sha256: str,
    generation: int,
    consumed_at: datetime,
) -> str:
    """Consume one raw token and durably bind it to the exact drain lease."""

    clean_action = str(authority_action_id or "").strip()
    clean_target = str(target_order_id or "").strip()
    if not clean_action or len(clean_action) > 128:
        raise ValueError("authority_action_id is invalid")
    if not clean_target or len(clean_target) > 128:
        raise ValueError("target_order_id is invalid")
    if not _SHA256.fullmatch(str(plan_sha256 or "")):
        raise ValueError("plan_sha256 is invalid")
    if not _SHA256.fullmatch(str(evidence_sha256 or "")):
        raise ValueError("evidence_sha256 is invalid")
    if isinstance(generation, bool) or int(generation) < 0:
        raise ValueError("generation is invalid")
    payload = {
        "authority_action_id": clean_action,
        "evidence_sha256": evidence_sha256,
        "generation": int(generation),
        "plan_sha256": plan_sha256,
        "target_order_id": clean_target,
    }
    binding = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    consume_repair_confirmation_token(
        session_factory,
        confirmation_token=confirmation_token,
        action_kind="drain_one_pending_entry",
        action_id=binding,
        pos_id=f"pending-entry:{clean_target}",
        consumed_at=consumed_at,
    )
    return binding


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
