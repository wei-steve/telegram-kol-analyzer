"""Bounded evidence-only MiMo backfill for historical Telegram messages."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.message_evidence import (
    build_message_input_fingerprint,
    persist_mimo_message_evidence,
)
from telegram_kol_research.models import (
    MediaAsset,
    MessageEvidenceVersion,
    RawMessage,
)
from telegram_kol_research.recognition_experiments import (
    MimoAuthoritativeResult,
    run_mimo_authoritative_for_message,
)


EvidenceBackfillItemStatus = Literal[
    "process",
    "skip_completed",
    "skip_failed",
    "skip_empty",
]


@dataclass(frozen=True, slots=True)
class EvidenceBackfillItem:
    raw_message_id: int
    chat_id: int
    message_id: int
    posted_at: datetime | None
    input_fingerprint: str
    input_kind: str
    status: EvidenceBackfillItemStatus


@dataclass(frozen=True, slots=True)
class EvidenceBackfillPlan:
    chat_ids: tuple[int, ...]
    start_at: datetime | None
    end_at: datetime | None
    limit: int
    retry_failed: bool
    items: tuple[EvidenceBackfillItem, ...]

    @property
    def planned(self) -> int:
        return sum(item.status == "process" for item in self.items)


@dataclass(frozen=True, slots=True)
class EvidenceBackfillResult:
    mode: Literal["dry_run", "apply"]
    considered: int
    planned: int
    succeeded: int
    failed: int
    skipped_completed: int
    skipped_failed: int
    skipped_empty: int
    rows: tuple[dict[str, Any], ...]


def _normalize_boundary(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _input_kind(raw_message: RawMessage, assets: Sequence[MediaAsset]) -> str:
    has_text = bool((raw_message.text or "").strip())
    has_image = any(
        asset.kind in {"photo", "image"}
        or str(asset.mime_type or "").lower().startswith("image/")
        for asset in assets
    )
    if has_text and has_image:
        return "text+image"
    if has_image:
        return "image"
    if has_text:
        return "text"
    if assets:
        return "media"
    return "empty"


def plan_mimo_evidence_backfill(
    session_factory: sessionmaker,
    *,
    chat_ids: Sequence[int],
    media_root: str | Path,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int = 100,
    retry_failed: bool = False,
) -> EvidenceBackfillPlan:
    """Plan a stable, bounded evidence-only backfill without invoking a model."""

    normalized_chat_ids = tuple(sorted({int(chat_id) for chat_id in chat_ids}))
    if not normalized_chat_ids:
        raise ValueError("at least one chat ID is required")
    if int(limit) <= 0:
        raise ValueError("limit must be positive")
    normalized_start = _normalize_boundary(start_at)
    normalized_end = _normalize_boundary(end_at)
    if (
        normalized_start is not None
        and normalized_end is not None
        and normalized_start > normalized_end
    ):
        raise ValueError("start_at must not be after end_at")

    items: list[EvidenceBackfillItem] = []
    with session_factory() as session:
        query = session.query(RawMessage).filter(
            RawMessage.chat_id.in_(normalized_chat_ids)
        )
        if normalized_start is not None:
            query = query.filter(RawMessage.posted_at >= normalized_start)
        if normalized_end is not None:
            query = query.filter(RawMessage.posted_at <= normalized_end)
        messages = (
            query.order_by(
                RawMessage.posted_at.asc(),
                RawMessage.message_id.asc(),
                RawMessage.id.asc(),
            )
            .limit(int(limit))
            .all()
        )
        for raw_message in messages:
            assets = (
                session.query(MediaAsset)
                .filter(MediaAsset.raw_message_id == int(raw_message.id))
                .order_by(MediaAsset.id.asc())
                .all()
            )
            input_kind = _input_kind(raw_message, assets)
            fingerprint = build_message_input_fingerprint(
                raw_message,
                assets,
                media_root=media_root,
            )
            current = (
                session.query(MessageEvidenceVersion)
                .filter(
                    MessageEvidenceVersion.raw_message_id == int(raw_message.id),
                    MessageEvidenceVersion.superseded_at.is_(None),
                )
                .order_by(MessageEvidenceVersion.version.desc())
                .first()
            )
            status: EvidenceBackfillItemStatus
            if input_kind == "empty":
                status = "skip_empty"
            elif current is None or current.input_fingerprint != fingerprint:
                status = "process"
            elif current.extraction_status == "completed":
                status = "skip_completed"
            elif retry_failed:
                status = "process"
            else:
                status = "skip_failed"
            items.append(
                EvidenceBackfillItem(
                    raw_message_id=int(raw_message.id),
                    chat_id=int(raw_message.chat_id),
                    message_id=int(raw_message.message_id),
                    posted_at=raw_message.posted_at,
                    input_fingerprint=fingerprint,
                    input_kind=input_kind,
                    status=status,
                )
            )
    return EvidenceBackfillPlan(
        chat_ids=normalized_chat_ids,
        start_at=normalized_start,
        end_at=normalized_end,
        limit=int(limit),
        retry_failed=bool(retry_failed),
        items=tuple(items),
    )


def run_mimo_evidence_backfill(
    session_factory: sessionmaker,
    *,
    plan: EvidenceBackfillPlan,
    ai_recognition_config: Any,
    media_root: str | Path,
    apply: bool,
    delay_seconds: float,
    mimo_runner: Callable[..., MimoAuthoritativeResult] = (
        run_mimo_authoritative_for_message
    ),
    sleeper: Callable[[float], Any] = time.sleep,
) -> EvidenceBackfillResult:
    """Execute a plan without applying recognition or resolving strategies."""

    if float(delay_seconds) < 0:
        raise ValueError("delay_seconds must not be negative")
    process_items = [item for item in plan.items if item.status == "process"]
    rows: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    if apply:
        for index, item in enumerate(process_items):
            try:
                result = mimo_runner(
                    session_factory,
                    raw_message_id=item.raw_message_id,
                    ai_recognition_config=ai_recognition_config,
                    media_root=media_root,
                )
            except Exception as exc:
                result = MimoAuthoritativeResult(
                    raw_message_id=item.raw_message_id,
                    payload={},
                    input_kind=item.input_kind,
                    model="mimo-v2.5",
                    status="识别失败",
                    error_message=str(exc),
                )
            evidence = persist_mimo_message_evidence(
                session_factory,
                raw_message_id=item.raw_message_id,
                payload=result.payload,
                input_kind=result.input_kind,
                model=result.model,
                prompt_versions=result.prompt_versions,
                error_message=result.error_message,
                media_root=media_root,
            )
            completed = (
                result.error_message is None
                and evidence.extraction_status == "completed"
            )
            if completed:
                succeeded += 1
            else:
                failed += 1
            rows.append(
                {
                    "raw_message_id": item.raw_message_id,
                    "chat_id": item.chat_id,
                    "message_id": item.message_id,
                    "status": "completed" if completed else "failed",
                    "evidence_version_id": int(evidence.id),
                    "extraction_status": evidence.extraction_status,
                    "error": (
                        str(result.error_message)[:256]
                        if result.error_message
                        else None
                    ),
                }
            )
            if index + 1 < len(process_items) and float(delay_seconds) > 0:
                sleeper(float(delay_seconds))

    return EvidenceBackfillResult(
        mode="apply" if apply else "dry_run",
        considered=len(plan.items),
        planned=len(process_items),
        succeeded=succeeded,
        failed=failed,
        skipped_completed=sum(
            item.status == "skip_completed" for item in plan.items
        ),
        skipped_failed=sum(item.status == "skip_failed" for item in plan.items),
        skipped_empty=sum(item.status == "skip_empty" for item in plan.items),
        rows=tuple(rows),
    )
