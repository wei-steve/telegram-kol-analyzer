"""Bounded evidence-only MiMo backfill for historical Telegram messages."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import and_, or_
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.message_evidence import (
    build_message_input_fingerprint,
    claim_message_evidence_extraction,
    finalize_claimed_mimo_message_evidence,
    release_message_evidence_extraction_claim,
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
    scanned: int = 0
    skipped_completed: int = 0
    skipped_failed: int = 0
    skipped_empty: int = 0
    scan_limit: int = 1000
    scan_cursor: str | None = None
    next_scan_cursor: str | None = None

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
    rows: tuple[dict[str, Any], ...] = ()
    skipped_claimed: int = 0
    resume_required: bool = False


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
    scan_limit: int = 1000,
    scan_cursor: str | None = None,
) -> EvidenceBackfillPlan:
    """Plan a stable, bounded evidence-only backfill without invoking a model."""

    normalized_chat_ids = tuple(sorted({int(chat_id) for chat_id in chat_ids}))
    if not normalized_chat_ids:
        raise ValueError("at least one chat ID is required")
    if int(limit) <= 0:
        raise ValueError("limit must be positive")
    if int(scan_limit) <= 0:
        raise ValueError("scan_limit must be positive")
    decoded_cursor = _decode_scan_cursor(scan_cursor)
    normalized_start = _normalize_boundary(start_at)
    normalized_end = _normalize_boundary(end_at)
    if (
        normalized_start is not None
        and normalized_end is not None
        and normalized_start > normalized_end
    ):
        raise ValueError("start_at must not be after end_at")

    items: list[EvidenceBackfillItem] = []
    scanned = 0
    skipped_completed = 0
    skipped_failed = 0
    skipped_empty = 0
    with session_factory() as session:
        query = session.query(RawMessage).filter(
            RawMessage.chat_id.in_(normalized_chat_ids)
        )
        if normalized_start is not None:
            query = query.filter(RawMessage.posted_at >= normalized_start)
        if normalized_end is not None:
            query = query.filter(RawMessage.posted_at <= normalized_end)
        if decoded_cursor is not None:
            cursor_posted_at, cursor_message_id, cursor_raw_id = decoded_cursor
            if cursor_posted_at is None:
                query = query.filter(
                    or_(
                        and_(
                            RawMessage.posted_at.is_(None),
                            or_(
                                RawMessage.message_id > cursor_message_id,
                                and_(
                                    RawMessage.message_id == cursor_message_id,
                                    RawMessage.id > cursor_raw_id,
                                ),
                            ),
                        ),
                        RawMessage.posted_at.is_not(None),
                    )
                )
            else:
                query = query.filter(
                    RawMessage.posted_at.is_not(None),
                    or_(
                        RawMessage.posted_at > cursor_posted_at,
                        and_(
                            RawMessage.posted_at == cursor_posted_at,
                            or_(
                                RawMessage.message_id > cursor_message_id,
                                and_(
                                    RawMessage.message_id == cursor_message_id,
                                    RawMessage.id > cursor_raw_id,
                                ),
                            ),
                        ),
                    ),
                )
        messages = query.order_by(
            RawMessage.posted_at.asc().nullsfirst(),
            RawMessage.message_id.asc(),
            RawMessage.id.asc(),
        ).limit(int(scan_limit)).yield_per(200)
        process_count = 0
        last_cursor_values: tuple[datetime | None, int, int] | None = None
        for raw_message in messages:
            scanned += 1
            last_cursor_values = (
                raw_message.posted_at,
                int(raw_message.message_id),
                int(raw_message.id),
            )
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
            if status == "process":
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
                process_count += 1
                if process_count >= int(limit):
                    break
            elif status == "skip_completed":
                skipped_completed += 1
            elif status == "skip_failed":
                skipped_failed += 1
            else:
                skipped_empty += 1
    return EvidenceBackfillPlan(
        chat_ids=normalized_chat_ids,
        start_at=normalized_start,
        end_at=normalized_end,
        limit=int(limit),
        retry_failed=bool(retry_failed),
        items=tuple(items),
        scanned=scanned,
        skipped_completed=skipped_completed,
        skipped_failed=skipped_failed,
        skipped_empty=skipped_empty,
        scan_limit=int(scan_limit),
        scan_cursor=scan_cursor,
        next_scan_cursor=(
            _encode_scan_cursor(*last_cursor_values)
            if scanned > 0
            and (scanned >= int(scan_limit) or process_count >= int(limit))
            else None
        ),
    )


def _encode_scan_cursor(
    posted_at: datetime | None,
    message_id: int,
    raw_message_id: int,
) -> str:
    payload = json.dumps(
        {
            "posted_at": posted_at.isoformat() if posted_at is not None else None,
            "message_id": int(message_id),
            "raw_message_id": int(raw_message_id),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_scan_cursor(
    cursor: str | None,
) -> tuple[datetime | None, int, int] | None:
    if cursor is None:
        return None
    try:
        encoded = str(cursor).strip()
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        )
        posted_value = payload.get("posted_at")
        posted_at = (
            _normalize_boundary(datetime.fromisoformat(str(posted_value)))
            if posted_value is not None
            else None
        )
        message_id = int(payload["message_id"])
        raw_message_id = int(payload["raw_message_id"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid scan cursor") from exc
    return posted_at, message_id, raw_message_id


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
    runtime_skipped_completed = 0
    runtime_skipped_failed = 0
    runtime_skipped_empty = 0
    skipped_claimed = 0
    invoked = 0
    if apply:
        for item in process_items:
            current_status, current_fingerprint = _current_execution_state(
                session_factory,
                item=item,
                media_root=media_root,
                retry_failed=plan.retry_failed,
            )
            if current_status == "skip_completed":
                runtime_skipped_completed += 1
                continue
            if current_status == "skip_failed":
                runtime_skipped_failed += 1
                continue
            if current_status == "skip_empty":
                runtime_skipped_empty += 1
                continue
            claim_token = claim_message_evidence_extraction(
                session_factory,
                raw_message_id=item.raw_message_id,
                input_fingerprint=current_fingerprint,
            )
            if claim_token is None:
                skipped_claimed += 1
                continue
            if invoked > 0 and float(delay_seconds) > 0:
                sleeper(float(delay_seconds))
            invoked += 1
            try:
                runner_exception = False
                try:
                    result = mimo_runner(
                        session_factory,
                        raw_message_id=item.raw_message_id,
                        ai_recognition_config=ai_recognition_config,
                        media_root=media_root,
                    )
                except Exception as exc:
                    runner_exception = True
                    result = MimoAuthoritativeResult(
                        raw_message_id=item.raw_message_id,
                        payload={},
                        input_kind=item.input_kind,
                        model="mimo-v2.5",
                        status="识别失败",
                        error_message=str(exc),
                    )
                _, post_fingerprint = _current_execution_state(
                    session_factory,
                    item=item,
                    media_root=media_root,
                    retry_failed=True,
                )
                if post_fingerprint != current_fingerprint:
                    failed += 1
                    rows.append(
                        {
                            "raw_message_id": item.raw_message_id,
                            "chat_id": item.chat_id,
                            "message_id": item.message_id,
                            "status": "stale_input",
                            "error_code": "message_input_changed",
                        }
                    )
                    continue
                try:
                    evidence = finalize_claimed_mimo_message_evidence(
                        session_factory,
                        raw_message_id=item.raw_message_id,
                        claim_token=claim_token,
                        expected_input_fingerprint=current_fingerprint,
                        payload=result.payload,
                        input_kind=result.input_kind,
                        model=result.model,
                        prompt_versions=result.prompt_versions,
                        error_message=result.error_message,
                        media_root=media_root,
                    )
                except Exception:
                    failed += 1
                    rows.append(
                        {
                            "raw_message_id": item.raw_message_id,
                            "chat_id": item.chat_id,
                            "message_id": item.message_id,
                            "status": "persistence_failed",
                            "error_code": "evidence_persistence_failed",
                        }
                    )
                    continue
                if evidence is None:
                    failed += 1
                    rows.append(
                        {
                            "raw_message_id": item.raw_message_id,
                            "chat_id": item.chat_id,
                            "message_id": item.message_id,
                            "status": "stale_claim",
                            "error_code": "evidence_finalize_refused",
                        }
                    )
                    continue
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
                        "error_code": (
                            None
                            if completed
                            else (
                                "mimo_exception"
                                if runner_exception
                                else "mimo_failed"
                            )
                        ),
                    }
                )
            finally:
                release_message_evidence_extraction_claim(
                    session_factory,
                    raw_message_id=item.raw_message_id,
                    claim_token=claim_token,
                )

    return EvidenceBackfillResult(
        mode="apply" if apply else "dry_run",
        considered=plan.scanned,
        planned=len(process_items),
        succeeded=succeeded,
        failed=failed,
        skipped_completed=plan.skipped_completed + runtime_skipped_completed,
        skipped_failed=plan.skipped_failed + runtime_skipped_failed,
        skipped_empty=plan.skipped_empty + runtime_skipped_empty,
        rows=tuple(rows),
        skipped_claimed=skipped_claimed,
        resume_required=(
            skipped_claimed > 0
            or any(
                row.get("status") in {"stale_input", "stale_claim"}
                for row in rows
            )
            or any(
                row.get("status") == "persistence_failed" for row in rows
            )
        ),
    )


def _current_execution_state(
    session_factory: sessionmaker,
    *,
    item: EvidenceBackfillItem,
    media_root: str | Path,
    retry_failed: bool,
) -> tuple[EvidenceBackfillItemStatus, str]:
    """Recheck an item immediately before a potentially billable model call."""

    with session_factory() as session:
        raw_message = session.get(RawMessage, int(item.raw_message_id))
        if raw_message is None:
            return "skip_failed", item.input_fingerprint
        assets = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id == int(item.raw_message_id))
            .order_by(MediaAsset.id.asc())
            .all()
        )
        if _input_kind(raw_message, assets) == "empty":
            return "skip_empty", item.input_fingerprint
        fingerprint = build_message_input_fingerprint(
            raw_message,
            assets,
            media_root=media_root,
        )
        current = (
            session.query(MessageEvidenceVersion)
            .filter(
                MessageEvidenceVersion.raw_message_id
                == int(item.raw_message_id),
                MessageEvidenceVersion.superseded_at.is_(None),
            )
            .order_by(MessageEvidenceVersion.version.desc())
            .first()
        )
        if current is None or current.input_fingerprint != fingerprint:
            return "process", fingerprint
        if current.extraction_status == "completed":
            return "skip_completed", fingerprint
        return (
            ("process" if retry_failed else "skip_failed"),
            fingerprint,
        )
