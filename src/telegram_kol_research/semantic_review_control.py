"""Guarded planning and terminalization for disabled semantic reviews."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
import hashlib
import json

from sqlalchemy import func, text
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import RecognitionDecision, TradingSetting
from telegram_kol_research.trading_settings import (
    TRADING_SETTINGS_KEY,
    trading_settings_from_payload,
)


class SemanticReviewControlError(RuntimeError):
    """Raised when a semantic-review transition safety gate fails."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SemanticReviewTarget:
    raw_message_id: int
    comparison_status: str
    agreement_status: str
    comparison_next_attempt_at: str | None
    comparison_started_at: str | None
    comparison_claim_token: str | None
    comparison_attempts: int
    updated_at: str
    row_fingerprint: str

    def fingerprint_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("row_fingerprint")
        return payload


@dataclass(frozen=True, slots=True)
class SemanticReviewDisablePlan:
    database_identity: str
    cutoff: str
    status_counts: dict[str, int]
    running_count: int
    targets: tuple[SemanticReviewTarget, ...]
    quick_check: str
    provider_call_count: int = 0
    notification_count: int = 0
    exchange_write_count: int = 0
    plan_sha: str = ""

    def fingerprint_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("plan_sha")
        return payload

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SemanticReviewApplyResult:
    changed_count: int
    post_apply_sha: str
    provider_call_count: int = 0
    notification_count: int = 0
    exchange_write_count: int = 0


@dataclass(frozen=True, slots=True)
class SemanticReviewRollbackTarget:
    raw_message_id: int
    preimage: SemanticReviewTarget
    current_row_fingerprint: str


@dataclass(frozen=True, slots=True)
class SemanticReviewRollbackPlan:
    database_identity: str
    preimage_plan_sha: str
    targets: tuple[SemanticReviewRollbackTarget, ...]
    quick_check: str
    provider_call_count: int = 0
    notification_count: int = 0
    exchange_write_count: int = 0
    plan_sha: str = ""

    def fingerprint_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload.pop("plan_sha")
        return payload

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SemanticReviewRollbackResult:
    changed_count: int
    post_rollback_sha: str
    provider_call_count: int = 0
    notification_count: int = 0
    exchange_write_count: int = 0


def _row_fingerprint_payload(row: RecognitionDecision) -> dict[str, object]:
    payload: dict[str, object] = {}
    for column in RecognitionDecision.__table__.columns:
        value = getattr(row, column.name)
        payload[column.name] = _iso(value) if isinstance(value, datetime) else value
    return payload


def _target_from_row(row: RecognitionDecision) -> SemanticReviewTarget:
    values = {
        "raw_message_id": int(row.raw_message_id),
        "comparison_status": str(row.comparison_status),
        "agreement_status": str(row.agreement_status),
        "comparison_next_attempt_at": _iso(row.comparison_next_attempt_at),
        "comparison_started_at": _iso(row.comparison_started_at),
        "comparison_claim_token": row.comparison_claim_token,
        "comparison_attempts": int(row.comparison_attempts),
        "updated_at": _iso(row.updated_at),
    }
    return SemanticReviewTarget(
        **values,
        row_fingerprint=_fingerprint(_row_fingerprint_payload(row)),
    )


def build_semantic_review_disable_plan(
    session_factory: sessionmaker,
    *,
    cutoff: datetime,
) -> SemanticReviewDisablePlan:
    """Build a deterministic read-only plan for pending and failed reviews."""

    with session_factory() as session:
        quick_check = str(session.execute(text("PRAGMA quick_check")).scalar_one())
        status_counts = {
            str(status): int(count)
            for status, count in session.query(
                RecognitionDecision.comparison_status,
                func.count(RecognitionDecision.id),
            )
            .group_by(RecognitionDecision.comparison_status)
            .order_by(RecognitionDecision.comparison_status)
            .all()
        }
        rows = (
            session.query(RecognitionDecision)
            .filter(
                RecognitionDecision.comparison_status.in_(["pending", "failed"]),
                RecognitionDecision.updated_at <= cutoff,
            )
            .order_by(RecognitionDecision.raw_message_id)
            .all()
        )
        targets = tuple(_target_from_row(row) for row in rows)
        database_identity = str(session.get_bind().url.database or "")
    plan = SemanticReviewDisablePlan(
        database_identity=database_identity,
        cutoff=cutoff.isoformat(),
        status_counts=status_counts,
        running_count=status_counts.get("running", 0),
        targets=targets,
        quick_check=quick_check,
    )
    return replace(plan, plan_sha=_fingerprint(plan.fingerprint_payload()))


def _semantic_review_enabled_in_session(session) -> bool:
    stored = session.query(TradingSetting).filter_by(key=TRADING_SETTINGS_KEY).one_or_none()
    payload = json.loads(stored.value_json) if stored is not None else {}
    return trading_settings_from_payload(payload).semantic_review_enabled


def _current_targets(session, cutoff: datetime) -> tuple[SemanticReviewTarget, ...]:
    rows = (
        session.query(RecognitionDecision)
        .filter(
            RecognitionDecision.comparison_status.in_(["pending", "failed"]),
            RecognitionDecision.updated_at <= cutoff,
        )
        .order_by(RecognitionDecision.raw_message_id)
        .all()
    )
    return tuple(_target_from_row(row) for row in rows)


def apply_semantic_review_disable_plan(
    session_factory: sessionmaker,
    plan: SemanticReviewDisablePlan,
    *,
    expected_plan_sha: str,
    applied_at: datetime,
) -> SemanticReviewApplyResult:
    """Atomically terminalize the exact planned pending/failed review rows."""

    if expected_plan_sha != plan.plan_sha:
        raise SemanticReviewControlError("expected plan SHA does not match plan")
    if plan.quick_check != "ok":
        raise SemanticReviewControlError("plan database quick_check is not ok")

    with session_factory() as session:
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            database_identity = str(session.get_bind().url.database or "")
            if database_identity != plan.database_identity:
                raise SemanticReviewControlError("semantic review database drift detected")
            if _semantic_review_enabled_in_session(session):
                raise SemanticReviewControlError("semantic review is enabled")
            running_count = (
                session.query(func.count(RecognitionDecision.id))
                .filter(RecognitionDecision.comparison_status == "running")
                .scalar()
            )
            if int(running_count or 0) != 0:
                raise SemanticReviewControlError("running semantic reviews exist")

            cutoff = datetime.fromisoformat(plan.cutoff)
            current_targets = _current_targets(session, cutoff)
            if current_targets != plan.targets:
                if current_targets:
                    raise SemanticReviewControlError(
                        "semantic review target drift detected"
                    )
                planned_ids = [target.raw_message_id for target in plan.targets]
                applied_rows = (
                    session.query(RecognitionDecision)
                    .filter(RecognitionDecision.raw_message_id.in_(planned_ids))
                    .order_by(RecognitionDecision.raw_message_id)
                    .all()
                    if planned_ids
                    else []
                )
                if [row.raw_message_id for row in applied_rows] != planned_ids or any(
                    row.comparison_status != "completed"
                    or row.agreement_status != "review_disabled"
                    or row.comparison_next_attempt_at is not None
                    or row.comparison_started_at is not None
                    or row.comparison_claim_token is not None
                    for row in applied_rows
                ):
                    raise SemanticReviewControlError(
                        "semantic review target drift detected"
                    )
                post_targets = tuple(_target_from_row(row) for row in applied_rows)
                post_apply_sha = _fingerprint(
                    [asdict(target) for target in post_targets]
                )
                session.commit()
                return SemanticReviewApplyResult(
                    changed_count=0,
                    post_apply_sha=post_apply_sha,
                )

            rows_by_raw_id = {
                row.raw_message_id: row
                for row in session.query(RecognitionDecision)
                .filter(
                    RecognitionDecision.raw_message_id.in_(
                        [target.raw_message_id for target in plan.targets]
                    )
                )
                .all()
            }
            for target in plan.targets:
                row = rows_by_raw_id[target.raw_message_id]
                row.comparison_status = "completed"
                row.agreement_status = "review_disabled"
                row.comparison_next_attempt_at = None
                row.comparison_started_at = None
                row.comparison_claim_token = None
                row.updated_at = applied_at
            session.flush()
            post_targets = tuple(
                _target_from_row(rows_by_raw_id[target.raw_message_id])
                for target in plan.targets
            )
            post_apply_sha = _fingerprint([asdict(target) for target in post_targets])
            session.commit()
        except Exception:
            session.rollback()
            raise

    return SemanticReviewApplyResult(
        changed_count=len(plan.targets),
        post_apply_sha=post_apply_sha,
    )


def build_semantic_review_rollback_plan(
    session_factory: sessionmaker,
    *,
    preimage_plan: SemanticReviewDisablePlan,
) -> SemanticReviewRollbackPlan:
    """Build an immutable rollback plan for the exact terminalized rows."""

    target_ids = [target.raw_message_id for target in preimage_plan.targets]
    with session_factory() as session:
        quick_check = str(session.execute(text("PRAGMA quick_check")).scalar_one())
        rows = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id.in_(target_ids))
            .order_by(RecognitionDecision.raw_message_id)
            .all()
            if target_ids
            else []
        )
        if [row.raw_message_id for row in rows] != target_ids:
            raise SemanticReviewControlError("rollback target set drift detected")
        targets: list[SemanticReviewRollbackTarget] = []
        for preimage, row in zip(preimage_plan.targets, rows, strict=True):
            if (
                row.comparison_status != "completed"
                or row.agreement_status != "review_disabled"
                or row.comparison_next_attempt_at is not None
                or row.comparison_started_at is not None
                or row.comparison_claim_token is not None
            ):
                raise SemanticReviewControlError("rollback post-apply state drift detected")
            targets.append(
                SemanticReviewRollbackTarget(
                    raw_message_id=int(row.raw_message_id),
                    preimage=preimage,
                    current_row_fingerprint=_fingerprint(
                        _row_fingerprint_payload(row)
                    ),
                )
            )
        database_identity = str(session.get_bind().url.database or "")
    plan = SemanticReviewRollbackPlan(
        database_identity=database_identity,
        preimage_plan_sha=preimage_plan.plan_sha,
        targets=tuple(targets),
        quick_check=quick_check,
    )
    return replace(plan, plan_sha=_fingerprint(plan.fingerprint_payload()))


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def apply_semantic_review_rollback_plan(
    session_factory: sessionmaker,
    plan: SemanticReviewRollbackPlan,
    *,
    expected_plan_sha: str,
) -> SemanticReviewRollbackResult:
    """Atomically restore only fields changed by the guarded apply."""

    if expected_plan_sha != plan.plan_sha:
        raise SemanticReviewControlError("expected rollback plan SHA does not match plan")
    if plan.quick_check != "ok":
        raise SemanticReviewControlError("rollback plan database quick_check is not ok")

    target_ids = [target.raw_message_id for target in plan.targets]
    with session_factory() as session:
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            rows = (
                session.query(RecognitionDecision)
                .filter(RecognitionDecision.raw_message_id.in_(target_ids))
                .order_by(RecognitionDecision.raw_message_id)
                .all()
                if target_ids
                else []
            )
            if [row.raw_message_id for row in rows] != target_ids:
                raise SemanticReviewControlError("rollback target set drift detected")
            for target, row in zip(plan.targets, rows, strict=True):
                if _fingerprint(_row_fingerprint_payload(row)) != (
                    target.current_row_fingerprint
                ):
                    raise SemanticReviewControlError("rollback row drift detected")

            for target, row in zip(plan.targets, rows, strict=True):
                preimage = target.preimage
                row.comparison_status = preimage.comparison_status
                row.agreement_status = preimage.agreement_status
                row.comparison_next_attempt_at = _parse_optional_datetime(
                    preimage.comparison_next_attempt_at
                )
                row.comparison_started_at = _parse_optional_datetime(
                    preimage.comparison_started_at
                )
                row.comparison_claim_token = preimage.comparison_claim_token
                row.updated_at = datetime.fromisoformat(preimage.updated_at)
            session.flush()
            post_rollback_sha = _fingerprint(
                [_row_fingerprint_payload(row) for row in rows]
            )
            session.commit()
        except Exception:
            session.rollback()
            raise

    return SemanticReviewRollbackResult(
        changed_count=len(plan.targets),
        post_rollback_sha=post_rollback_sha,
    )
