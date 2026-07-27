"""Persist versioned normalized evidence extracted from Telegram messages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    MediaAsset,
    MessageEvidenceVersion,
    RawMessage,
    utc_now,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def build_message_input_fingerprint(
    raw_message: RawMessage,
    media_assets: Iterable[MediaAsset],
) -> str:
    """Return a stable fingerprint of editable text and attached media."""

    media_rows: list[dict[str, Any]] = []
    for asset in sorted(media_assets, key=lambda item: int(item.id or 0)):
        content_hash = None
        if asset.local_path:
            path = Path(asset.local_path)
            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                content_hash = digest.hexdigest()
        media_rows.append(
            {
                "id": asset.id,
                "telegram_file_id": asset.telegram_file_id,
                "kind": asset.kind,
                "mime_type": asset.mime_type,
                "content_sha256": content_hash,
            }
        )
    payload = {
        "raw_message_id": raw_message.id,
        "chat_id": raw_message.chat_id,
        "message_id": raw_message.message_id,
        "text": raw_message.text or "",
        "edit_date": (
            raw_message.edit_date.isoformat()
            if raw_message.edit_date is not None
            else None
        ),
        "media": media_rows,
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def save_message_evidence_version(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    input_fingerprint: str,
    model: str,
    prompt_versions: Mapping[str, Any],
    extraction_status: str,
    confidence: float,
    text_evidence: Mapping[str, Any],
    image_evidence: Mapping[str, Any],
    normalized_evidence: Mapping[str, Any],
) -> MessageEvidenceVersion:
    """Save one immutable version, returning an existing identical version."""

    with session_factory() as session:
        if session.get(RawMessage, int(raw_message_id)) is None:
            raise LookupError("raw message not found")
        existing = (
            session.query(MessageEvidenceVersion)
            .filter(
                MessageEvidenceVersion.raw_message_id == int(raw_message_id),
                MessageEvidenceVersion.input_fingerprint == input_fingerprint,
            )
            .one_or_none()
        )
        if existing is not None:
            session.expunge(existing)
            return existing

        now = utc_now()
        current_rows = (
            session.query(MessageEvidenceVersion)
            .filter(
                MessageEvidenceVersion.raw_message_id == int(raw_message_id),
                MessageEvidenceVersion.superseded_at.is_(None),
            )
            .all()
        )
        for row in current_rows:
            row.superseded_at = now
        last_version = (
            session.query(func.max(MessageEvidenceVersion.version))
            .filter(MessageEvidenceVersion.raw_message_id == int(raw_message_id))
            .scalar()
        )
        row = MessageEvidenceVersion(
            raw_message_id=int(raw_message_id),
            version=int(last_version or 0) + 1,
            input_fingerprint=str(input_fingerprint),
            model=str(model),
            prompt_versions_json=_canonical_json(dict(prompt_versions)),
            extraction_status=str(extraction_status),
            confidence=float(confidence),
            text_evidence_json=_canonical_json(dict(text_evidence)),
            image_evidence_json=_canonical_json(dict(image_evidence)),
            normalized_evidence_json=_canonical_json(dict(normalized_evidence)),
            created_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def load_current_message_evidence(
    session_factory: sessionmaker,
    raw_message_id: int,
) -> MessageEvidenceVersion | None:
    """Load the latest non-superseded evidence version."""

    with session_factory() as session:
        row = (
            session.query(MessageEvidenceVersion)
            .filter(
                MessageEvidenceVersion.raw_message_id == int(raw_message_id),
                MessageEvidenceVersion.superseded_at.is_(None),
            )
            .order_by(MessageEvidenceVersion.version.desc())
            .first()
        )
        if row is not None:
            session.expunge(row)
        return row
