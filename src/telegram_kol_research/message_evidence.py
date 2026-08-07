"""Persist versioned normalized evidence extracted from Telegram messages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, or_, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    MediaAsset,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    RawMessage,
    utc_now,
)


@dataclass(frozen=True, slots=True)
class EntryPreambleEvidence:
    symbol: str
    side: str
    risk_multiplier: Decimal
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        multiplier = format(self.risk_multiplier, "f")
        if "." in multiplier:
            multiplier = multiplier.rstrip("0").rstrip(".")
        return {
            "kind": "entry_preamble",
            "symbol": self.symbol,
            "side": self.side,
            "risk_multiplier": multiplier,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def normalize_entry_preamble_evidence(value: Any) -> EntryPreambleEvidence | None:
    """Validate a model-produced, explicitly non-executable sizing fragment."""

    if not isinstance(value, Mapping):
        return None
    if str(value.get("kind") or "") != "entry_preamble":
        return None
    symbol = str(value.get("symbol") or "").strip().upper()
    side = str(value.get("side") or "").strip().lower()
    if not symbol or side not in {"long", "short"}:
        return None
    raw_multiplier = value.get("risk_multiplier")
    if isinstance(raw_multiplier, bool):
        return None
    try:
        risk_multiplier = Decimal(str(raw_multiplier).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        return None
    if (
        not risk_multiplier.is_finite()
        or risk_multiplier <= Decimal("0")
        or risk_multiplier > Decimal("1")
    ):
        return None
    raw_confidence = value.get("confidence")
    if isinstance(raw_confidence, bool):
        return None
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    reason = str(value.get("reason") or "").strip()
    if not reason:
        return None
    return EntryPreambleEvidence(
        symbol=symbol,
        side=side,
        risk_multiplier=risk_multiplier,
        confidence=confidence,
        reason=reason,
    )


@dataclass(frozen=True, slots=True)
class EntryStrategyFragmentEvidence:
    kind: str
    symbol: str
    side: str
    payload: dict[str, Any]
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "symbol": self.symbol,
            "side": self.side,
            **self.payload,
            "confidence": self.confidence,
            "reason": self.reason,
        }


def _canonical_positive_decimal(value: Any, *, maximum: Decimal | None = None) -> str | None:
    if isinstance(value, bool):
        return None
    try:
        decimal = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        return None
    if not decimal.is_finite() or decimal <= 0:
        return None
    if maximum is not None and decimal > maximum:
        return None
    normalized = format(decimal.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def normalize_entry_strategy_fragments(
    value: Any,
) -> tuple[EntryStrategyFragmentEvidence, ...]:
    """Return only bounded, explicit, non-executable entry fragments."""

    if not isinstance(value, list):
        return ()
    normalized: list[EntryStrategyFragmentEvidence] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        kind = str(item.get("kind") or "").strip()
        symbol = str(item.get("symbol") or "").strip().upper()
        side = str(item.get("side") or "").strip().lower()
        reason = str(item.get("reason") or "").strip()
        raw_confidence = item.get("confidence")
        if (
            not symbol
            or side not in {"long", "short"}
            or not reason
            or isinstance(raw_confidence, bool)
        ):
            continue
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            continue
        if not 0 <= confidence <= 1:
            continue
        payload: dict[str, Any]
        if kind == "risk_multiplier":
            multiplier = _canonical_positive_decimal(
                item.get("risk_multiplier"), maximum=Decimal("1")
            )
            if multiplier is None:
                continue
            payload = {"risk_multiplier": multiplier}
        elif kind == "leg_allocation":
            raw_allocations = item.get("allocations")
            if not isinstance(raw_allocations, list) or not 1 <= len(raw_allocations) <= 5:
                continue
            allocations = [
                _canonical_positive_decimal(part, maximum=Decimal("1"))
                for part in raw_allocations
            ]
            if any(part is None for part in allocations):
                continue
            if sum(Decimal(str(part)) for part in allocations) != Decimal("1"):
                continue
            payload = {"allocations": allocations}
        elif kind == "supplemental_entry":
            entry_price = _canonical_positive_decimal(item.get("entry_price"))
            if entry_price is None:
                continue
            payload = {"entry_price": entry_price}
        else:
            continue
        normalized.append(
            EntryStrategyFragmentEvidence(
                kind=kind,
                symbol=symbol,
                side=side,
                payload=payload,
                confidence=confidence,
                reason=reason,
            )
        )
    return tuple(normalized)


def claim_message_evidence_extraction(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    input_fingerprint: str,
    lease_seconds: float = 300.0,
) -> str | None:
    """Atomically claim one message/input for a bounded MiMo extraction."""

    if float(lease_seconds) <= 0:
        raise ValueError("lease_seconds must be positive")
    now = utc_now()
    expires_at = now + timedelta(seconds=float(lease_seconds))
    token = uuid4().hex
    with session_factory() as session:
        if session.get(RawMessage, int(raw_message_id)) is None:
            raise LookupError("raw message not found")
        inserted = session.execute(
            sqlite_insert(MessageEvidenceExtractionClaim)
            .values(
                raw_message_id=int(raw_message_id),
                input_fingerprint=str(input_fingerprint),
                claim_token=token,
                claimed_at=now,
                lease_expires_at=expires_at,
            )
            .on_conflict_do_nothing(
                index_elements=[MessageEvidenceExtractionClaim.raw_message_id]
            )
        )
        if int(inserted.rowcount or 0) == 1:
            session.commit()
            return token
        replaced = session.execute(
            update(MessageEvidenceExtractionClaim)
            .where(
                MessageEvidenceExtractionClaim.raw_message_id
                == int(raw_message_id),
                or_(
                    MessageEvidenceExtractionClaim.input_fingerprint
                    != str(input_fingerprint),
                    MessageEvidenceExtractionClaim.lease_expires_at <= now,
                ),
            )
            .values(
                input_fingerprint=str(input_fingerprint),
                claim_token=token,
                claimed_at=now,
                lease_expires_at=expires_at,
            )
        )
        session.commit()
        return token if int(replaced.rowcount or 0) == 1 else None


def release_message_evidence_extraction_claim(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    claim_token: str | None,
) -> bool:
    """Release only the exact claim generation owned by the caller."""

    if not claim_token:
        return False
    with session_factory() as session:
        result = session.execute(
            delete(MessageEvidenceExtractionClaim).where(
                MessageEvidenceExtractionClaim.raw_message_id
                == int(raw_message_id),
                MessageEvidenceExtractionClaim.claim_token == str(claim_token),
            )
        )
        session.commit()
        return int(result.rowcount or 0) == 1


def message_evidence_extraction_claim_is_current(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    input_fingerprint: str,
    claim_token: str,
) -> bool:
    """Return whether the caller still owns the exact unexpired claim."""

    now = utc_now()
    with session_factory() as session:
        return (
            session.query(MessageEvidenceExtractionClaim.raw_message_id)
            .filter(
                MessageEvidenceExtractionClaim.raw_message_id
                == int(raw_message_id),
                MessageEvidenceExtractionClaim.input_fingerprint
                == str(input_fingerprint),
                MessageEvidenceExtractionClaim.claim_token == str(claim_token),
                MessageEvidenceExtractionClaim.lease_expires_at > now,
            )
            .first()
            is not None
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
    *,
    media_root: str | Path | None = None,
) -> str:
    """Return a stable fingerprint of editable text and attached media."""

    media_rows: list[dict[str, Any]] = []
    for asset in sorted(media_assets, key=lambda item: int(item.id or 0)):
        content_hash = None
        if asset.local_path:
            path = Path(asset.local_path)
            if not path.is_absolute() and media_root is not None:
                path = Path(media_root) / path
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


def build_current_message_input_fingerprint(
    session_factory: sessionmaker,
    raw_message_id: int,
    *,
    media_root: str | Path,
) -> str:
    """Build the current fingerprint using a short database read."""

    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            raise LookupError("raw message not found")
        media_assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == int(raw_message_id))
            .order_by(MediaAsset.id.asc())
            .all()
        )
        return build_message_input_fingerprint(
            raw_message,
            media_assets,
            media_root=media_root,
        )


def normalize_mimo_evidence(
    payload: Mapping[str, Any],
    *,
    input_kind: str,
    error_message: str | None,
) -> tuple[str, float, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Normalize the source-separated MiMo evidence contract."""

    evidence = payload.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    text_evidence = evidence.get("text")
    if not isinstance(text_evidence, Mapping):
        input_reading = payload.get("input_reading")
        input_reading = (
            input_reading if isinstance(input_reading, Mapping) else {}
        )
        text_evidence = {
            "observed_text": str(input_reading.get("observed_text") or ""),
            "fields": {},
        }
    images = evidence.get("images")
    if not isinstance(images, list):
        images = []
    conflicts = evidence.get("conflicts")
    if not isinstance(conflicts, list):
        conflicts = []
    if error_message:
        extraction_status = (
            "image_unavailable" if "image" in input_kind else "failed"
        )
    else:
        extraction_status = "completed"
    try:
        confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))
    strategy = payload.get("strategy")
    lifecycle_event = payload.get("lifecycle_event")
    normalized_evidence = {
        "strategy": strategy if isinstance(strategy, Mapping) else {},
        "lifecycle_event": (
            lifecycle_event if isinstance(lifecycle_event, Mapping) else {}
        ),
        "conflicts": conflicts,
        "recognition_result": payload.get("recognition_result"),
        "reason": payload.get("reason"),
        "summary": payload.get("summary"),
        "confidence": confidence,
    }
    if "entry_context" in payload:
        entry_context = normalize_entry_preamble_evidence(
            payload.get("entry_context")
        )
        if entry_context is None:
            normalized_evidence[
                "entry_context_rejection_reason"
            ] = "entry_context_invalid"
        else:
            normalized_evidence["entry_context"] = entry_context.to_dict()
    if "entry_fragments" in payload:
        raw_fragments = payload.get("entry_fragments")
        entry_fragments = normalize_entry_strategy_fragments(raw_fragments)
        normalized_evidence["entry_fragments"] = [
            fragment.to_dict() for fragment in entry_fragments
        ]
        if isinstance(raw_fragments, list) and len(entry_fragments) != len(raw_fragments):
            normalized_evidence["entry_fragments_rejected_count"] = (
                len(raw_fragments) - len(entry_fragments)
            )
    return (
        extraction_status,
        confidence,
        dict(text_evidence),
        {"images": images},
        normalized_evidence,
    )


def persist_mimo_message_evidence(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    payload: Mapping[str, Any],
    input_kind: str,
    model: str,
    prompt_versions: Mapping[str, Any],
    error_message: str | None,
    media_root: str | Path,
    expected_input_fingerprint: str | None = None,
) -> MessageEvidenceVersion:
    """Persist the evidence produced by one authoritative MiMo read."""

    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            raise LookupError("raw message not found")
        media_assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == int(raw_message_id))
            .order_by(MediaAsset.id.asc())
            .all()
        )
        input_fingerprint = expected_input_fingerprint or (
            build_message_input_fingerprint(
                raw_message,
                media_assets,
                media_root=media_root,
            )
        )
    (
        extraction_status,
        confidence,
        text_evidence,
        image_evidence,
        normalized_evidence,
    ) = normalize_mimo_evidence(
        payload,
        input_kind=input_kind,
        error_message=error_message,
    )
    return save_message_evidence_version(
        session_factory,
        raw_message_id=raw_message_id,
        input_fingerprint=input_fingerprint,
        model=model,
        prompt_versions=prompt_versions,
        extraction_status=extraction_status,
        confidence=confidence,
        text_evidence=text_evidence,
        image_evidence=image_evidence,
        normalized_evidence=normalized_evidence,
    )


def finalize_claimed_mimo_message_evidence(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    claim_token: str,
    expected_input_fingerprint: str,
    payload: Mapping[str, Any],
    input_kind: str,
    model: str,
    prompt_versions: Mapping[str, Any],
    error_message: str | None,
    media_root: str | Path,
) -> MessageEvidenceVersion | None:
    """Atomically validate the input/claim and persist one MiMo result."""

    (
        extraction_status,
        confidence,
        text_evidence,
        image_evidence,
        normalized_evidence,
    ) = normalize_mimo_evidence(
        payload,
        input_kind=input_kind,
        error_message=error_message,
    )
    with session_factory() as session:
        # Production uses SQLite. An immediate transaction prevents Telegram
        # ingestion from editing the source row between validation and commit.
        session.execute(text("BEGIN IMMEDIATE"))
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            session.rollback()
            raise LookupError("raw message not found")
        media_assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == int(raw_message_id))
            .order_by(MediaAsset.id.asc())
            .all()
        )
        current_fingerprint = build_message_input_fingerprint(
            raw_message,
            media_assets,
            media_root=media_root,
        )
        claim = (
            session.query(MessageEvidenceExtractionClaim)
            .filter(
                MessageEvidenceExtractionClaim.raw_message_id
                == int(raw_message_id),
                MessageEvidenceExtractionClaim.claim_token == str(claim_token),
                MessageEvidenceExtractionClaim.input_fingerprint
                == str(expected_input_fingerprint),
                MessageEvidenceExtractionClaim.lease_expires_at > utc_now(),
            )
            .one_or_none()
        )
        if (
            claim is None
            or current_fingerprint != str(expected_input_fingerprint)
        ):
            session.rollback()
            return None
        row = _save_message_evidence_version_in_session(
            session,
            raw_message_id=raw_message_id,
            input_fingerprint=expected_input_fingerprint,
            model=model,
            prompt_versions=prompt_versions,
            extraction_status=extraction_status,
            confidence=confidence,
            text_evidence=text_evidence,
            image_evidence=image_evidence,
            normalized_evidence=normalized_evidence,
        )
        session.delete(claim)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


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
        row = _save_message_evidence_version_in_session(
            session,
            raw_message_id=raw_message_id,
            input_fingerprint=input_fingerprint,
            model=model,
            prompt_versions=prompt_versions,
            extraction_status=extraction_status,
            confidence=confidence,
            text_evidence=text_evidence,
            image_evidence=image_evidence,
            normalized_evidence=normalized_evidence,
        )
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def _save_message_evidence_version_in_session(
    session,
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
        if (
            existing.extraction_status != "completed"
            and str(extraction_status) == "completed"
        ):
            session.execute(
                update(MessageEvidenceVersion)
                .where(
                    MessageEvidenceVersion.id == int(existing.id),
                    MessageEvidenceVersion.extraction_status != "completed",
                )
                .values(
                    model=str(model),
                    prompt_versions_json=_canonical_json(dict(prompt_versions)),
                    extraction_status="completed",
                    confidence=float(confidence),
                    text_evidence_json=_canonical_json(dict(text_evidence)),
                    image_evidence_json=_canonical_json(dict(image_evidence)),
                    normalized_evidence_json=_canonical_json(
                        dict(normalized_evidence)
                    ),
                    superseded_at=None,
                )
            )
            session.flush()
            session.refresh(existing)
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
    for current in current_rows:
        current.superseded_at = now
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
    session.flush()
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
