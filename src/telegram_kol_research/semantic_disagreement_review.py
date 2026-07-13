"""Semantic disagreement review integration and deterministic severity rules.

MiMo remains authoritative. This module builds and audits the independent
DeepSeek review request, processes retryable database work, and classifies
materiality without mutating or authorizing trading automation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import inspect
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable

import httpx
from sqlalchemy import and_, or_, update
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_recognition_config import (
    AiRecognitionConfig,
    load_ai_recognition_config,
)
from telegram_kol_research.models import (
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
    utc_now,
)
from telegram_kol_research.prompt_defaults import (
    SEMANTIC_DISAGREEMENT_REVIEW_PROMPT,
    seed_default_prompt_registry,
)
from telegram_kol_research.prompt_registry import (
    PromptInvocationRecord,
    record_prompt_invocation,
    resolve_active_prompt,
)
from telegram_kol_research.recognition_decisions import (
    claim_critical_notification,
    claim_next_semantic_review,
    complete_semantic_review,
    fail_semantic_review,
)
from telegram_kol_research.recognition_experiments import (
    _extract_chat_content,
)


logger = logging.getLogger(__name__)


ACTION_TYPES = frozenset(
    {
        "none",
        "entry",
        "entry_confirm",
        "cancel_entry",
        "exit_full",
        "exit_partial",
        "position_update",
    }
)
SEVERITIES = frozenset({"none", "normal", "critical"})
CONFLICT_TYPES = (
    "actionability",
    "action_family",
    "full_vs_partial_exit",
    "symbol",
    "side",
    "target_lifecycle",
    "stop_intent",
    "urgent_exit_missed",
    "execution_unresolved",
    "non_material_price_detail",
    "wording_only",
)
_CONFLICT_TYPE_SET = frozenset(CONFLICT_TYPES)
_MODEL_CRITICAL_CONFLICTS = frozenset(
    {
        "actionability",
        "action_family",
        "full_vs_partial_exit",
        "symbol",
        "side",
        "target_lifecycle",
        "stop_intent",
        "urgent_exit_missed",
        "execution_unresolved",
    }
)
_ACTIONABLE_TYPES = ACTION_TYPES - {"none"}
_PARTIAL_MANAGEMENT_ACTIONS = frozenset(
    {
        "partial_take_profit",
        "partial_exit",
        "reduce_position",
        "trim_position",
        "减仓",
        "部分止盈",
        "分批止盈",
    }
)
_PROTECTIVE_STOP_ACTIONS = frozenset(
    {
        "move_stop_to_protect",
        "move_stop_to_entry",
        "protective_stop",
        "protect_profit",
        "tighten_stop",
        "tighten_stop_loss",
    }
)
_WEAKENING_STOP_ACTIONS = frozenset(
    {
        "cancel_stop",
        "cancel_stop_loss",
        "loosen_stop",
        "loosen_stop_loss",
        "remove_stop",
        "remove_stop_loss",
        "widen_stop",
        "widen_stop_loss",
    }
)
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:万|w)?$", re.IGNORECASE)
_PRICE_SEPARATOR = re.compile(r"\s*(?:/|／|~|～|至|到|—|–|-)\s*")


@dataclass(frozen=True)
class SemanticReviewDecision:
    agreement_status: str
    severity: str
    conflict_types: tuple[str, ...]
    differences: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class SemanticReviewRun:
    raw_message_id: int
    model: str
    review_payload: dict[str, Any]
    auxiliary_payload: dict[str, Any]
    decision: SemanticReviewDecision
    prompt_versions: dict[str, int]


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _request_openai_compatible(
    *,
    url: str,
    json: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout) as client:
        response = client.post(url, json=json, headers=headers)
        response.raise_for_status()
        return response.json()


def _safe_active_strategy_context(
    session,
    *,
    chat_id: int,
    as_of: datetime,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = (
        session.query(StrategyLifecycle)
        .filter(
            StrategyLifecycle.chat_id == chat_id,
            StrategyLifecycle.signal_at <= as_of,
            or_(
                and_(
                    StrategyLifecycle.exited_at.is_(None),
                    StrategyLifecycle.lifecycle_status.in_(
                        ("pending_entry", "entered")
                    ),
                ),
                StrategyLifecycle.exited_at > as_of,
            ),
        )
        .order_by(StrategyLifecycle.signal_at.desc(), StrategyLifecycle.id.desc())
        .limit(max(limit, 1))
        .all()
    )
    return [
        {
            "lifecycle_id": row.id,
            "source_message_id": row.message_id,
            "symbol": row.symbol,
            "side": row.side,
            "lifecycle_status": _historical_lifecycle_status(row, as_of=as_of),
            "entry_range_low": row.entry_range_low,
            "entry_range_high": row.entry_range_high,
            "stop_loss": row.stop_loss,
            "take_profit": row.take_profit,
            "entry_price_actual": row.entry_price_actual,
            "management_action": row.management_action,
        }
        for row in rows
    ]


def _historical_lifecycle_status(
    lifecycle: StrategyLifecycle,
    *,
    as_of: datetime,
) -> str:
    if lifecycle.entered_at is not None and lifecycle.entered_at <= as_of:
        return "entered"
    if (
        lifecycle.lifecycle_status == "entered"
        and lifecycle.entered_at is None
        and lifecycle.exited_at is None
    ):
        return "entered"
    return "pending_entry"


def _parse_strict_json_object(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(
            "semantic review response must be a strict JSON object"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("semantic review response must be a strict JSON object")
    return payload


def run_deepseek_semantic_review(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    config: AiRecognitionConfig,
    requester: Callable[..., dict[str, Any]] | None = None,
) -> SemanticReviewRun:
    """Independently review a persisted MiMo decision without mutating automation."""

    seed_default_prompt_registry(session_factory, config)
    prompt = resolve_active_prompt(
        session_factory, SEMANTIC_DISAGREEMENT_REVIEW_PROMPT
    )
    provider = config.text_provider
    if not provider.is_configured:
        raise RuntimeError("DeepSeek semantic-review provider is not configured")

    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        decision_row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_message_id)
            .one_or_none()
        )
        if raw_message is None:
            raise LookupError("raw message not found")
        if decision_row is None:
            raise LookupError("recognition decision not found")
        authoritative_payload = json.loads(decision_row.authoritative_payload_json)
        message_time = raw_message.posted_at or raw_message.created_at
        context = {
            "source": {
                "raw_message_id": raw_message.id,
                "chat_id": raw_message.chat_id,
                "message_id": raw_message.message_id,
                "sender_id": raw_message.sender_id,
                "sender_name": raw_message.sender_name,
                "posted_at": str(raw_message.posted_at) if raw_message.posted_at else None,
                "text": raw_message.text or "",
            },
            "active_strategies": _safe_active_strategy_context(
                session,
                chat_id=raw_message.chat_id,
                as_of=message_time,
            ),
            "mimo": {
                "model": decision_row.authoritative_model,
                "status": decision_row.authoritative_status,
                "authoritative_payload": authoritative_payload,
                "input_reading": authoritative_payload.get("input_reading"),
            },
            "automation": {
                "automation_status": decision_row.automation_status,
                "automation_reason": decision_row.automation_reason,
            },
            "semantic_review_prompt": {
                "key": prompt.prompt_key,
                "version_id": prompt.version_id,
                "version_number": prompt.version_number,
            },
        }
        chat_id = raw_message.chat_id
        current_message_text = raw_message.text or ""
        input_kind = decision_row.input_kind

    request_payload = {
        "model": provider.model,
        "messages": [
            {"role": "system", "content": prompt.content},
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False, sort_keys=True),
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    invoke = requester or _request_openai_compatible
    prompt_versions = {prompt.prompt_key: prompt.version_id}
    error_message: str | None = None
    try:
        response = invoke(
            url=_chat_completions_url(provider.base_url),
            json=request_payload,
            headers=headers,
            timeout=provider.timeout_seconds,
        )
        review_payload = _parse_strict_json_object(_extract_chat_content(response))
        validate_review_payload(review_payload)
        automation = {
            "status": context["automation"]["automation_status"],
            "reason": context["automation"]["automation_reason"],
        }
        semantic_decision = decide_semantic_severity(
            mimo_payload=authoritative_payload,
            review_payload=review_payload,
            automation=automation,
            input_kind=input_kind,
            current_message_text=current_message_text,
        )
        return SemanticReviewRun(
            raw_message_id=raw_message_id,
            model=provider.model,
            review_payload=review_payload,
            auxiliary_payload=review_payload,
            decision=semantic_decision,
            prompt_versions=prompt_versions,
        )
    except Exception as exc:
        error_message = str(exc)
        raise
    finally:
        record_prompt_invocation(
            session_factory,
            PromptInvocationRecord(
                feature="semantic_disagreement_review",
                correlation_key=f"semantic-review:{raw_message_id}",
                raw_message_id=raw_message_id,
                chat_id=chat_id,
                model=provider.model,
                prompt_versions=prompt_versions,
                status="failed" if error_message else "completed",
                error_message=error_message,
            ),
        )


def _load_review_attempts(session_factory: sessionmaker, raw_message_id: int) -> int:
    with session_factory() as session:
        value = session.query(RecognitionDecision.comparison_attempts).filter_by(
            raw_message_id=raw_message_id
        ).scalar()
        if value is None:
            raise LookupError("recognition decision not found")
        return int(value)


async def _notify(notifier: Callable[..., Any], **kwargs: Any) -> None:
    if inspect.iscoroutinefunction(notifier):
        await notifier(**kwargs)
        return
    result = await asyncio.to_thread(notifier, **kwargs)
    if inspect.isawaitable(result):
        await result


def build_semantic_notification_payload(
    claimed_payload: dict[str, Any],
) -> dict[str, Any]:
    """Map an immutable claimed audit snapshot to the Telegram payload."""

    source = claimed_payload.get("source")
    source = source if isinstance(source, dict) else {}
    authoritative = claimed_payload.get("authoritative")
    authoritative = authoritative if isinstance(authoritative, dict) else {}
    authoritative_payload = authoritative.get("payload")
    authoritative_payload = (
        authoritative_payload if isinstance(authoritative_payload, dict) else {}
    )
    comparison = claimed_payload.get("comparison")
    comparison = comparison if isinstance(comparison, dict) else {}
    comparison_payload = comparison.get("payload")
    comparison_payload = (
        comparison_payload if isinstance(comparison_payload, dict) else {}
    )
    action = comparison_payload.get("independent_action")
    action = action if isinstance(action, dict) else {}
    evidence = comparison_payload.get("evidence")
    evidence = evidence if isinstance(evidence, list) else []
    conflict_types = comparison_payload.get("conflict_types")
    conflict_types = conflict_types if isinstance(conflict_types, list) else []
    posted_at = source.get("posted_at")
    if isinstance(posted_at, str) and posted_at:
        try:
            posted_at = datetime.fromisoformat(posted_at)
        except ValueError:
            posted_at = None
    automation = claimed_payload.get("automation")
    automation = automation if isinstance(automation, dict) else {}
    return {
        "chat_id": source.get("chat_id"),
        "message_id": source.get("message_id"),
        "sender_name": source.get("sender_name"),
        "posted_at": posted_at,
        "text": source.get("text") or "",
        "agreement_status": "disagreed",
        "conflict_types": list(conflict_types),
        "deepseek": {
            "status": action.get("action_type") or "none",
            "kind": "semantic_review",
            "reason": comparison_payload.get("reason") or "-",
            "evidence": list(evidence),
            "conflict_types": list(conflict_types),
        },
        "mimo": {
            "status": normalize_mimo_action(authoritative_payload)["action_type"],
            "kind": "authoritative",
            "reason": authoritative_payload.get("reason") or "-",
        },
        "automation": {
            "status": automation.get("status"),
            "reason": automation.get("reason"),
        },
    }


def sanitize_notifier_exception(exc: BaseException) -> str:
    """Return a bounded, non-secret notifier failure summary."""

    summary = re.sub(r"[^A-Za-z0-9_.]", "", type(exc).__name__)[:100]
    summary = summary or "Exception"
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and 100 <= status_code <= 599:
        summary += f" status={status_code}"
    return summary


def _update_critical_notification_delivery(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    fingerprint: str,
    status: str,
    error: str | None = None,
) -> bool:
    """Finish a reserved delivery without writing any automation fields."""

    if status not in {"sent", "failed"}:
        raise ValueError("notification delivery status must be sent or failed")
    with session_factory() as session:
        result = session.execute(
            update(RecognitionDecision)
            .where(
                RecognitionDecision.raw_message_id == raw_message_id,
                RecognitionDecision.notification_fingerprint == fingerprint,
                RecognitionDecision.notification_status == "scheduled",
            )
            .values(
                notification_status=status,
                notification_error=error,
                updated_at=utc_now(),
            )
        )
        session.commit()
        return result.rowcount == 1


async def run_semantic_review_once(
    session_factory: sessionmaker,
    *,
    config: AiRecognitionConfig,
    notifier: Callable[..., Any] | None,
    now: datetime,
    reviewer: Callable[..., SemanticReviewRun] = run_deepseek_semantic_review,
    max_attempts: int = 3,
    retry_delay_seconds: float = 1.0,
    stale_after: timedelta = timedelta(minutes=5),
    now_provider: Callable[[], datetime] = utc_now,
) -> bool:
    """Claim and process at most one semantic review row."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")
    claim = await asyncio.to_thread(
        claim_next_semantic_review,
        session_factory,
        now=now,
        stale_before=now - stale_after,
    )
    if claim is None:
        return False

    try:
        run = await asyncio.to_thread(
            reviewer,
            session_factory,
            raw_message_id=claim.raw_message_id,
            config=config,
        )
        final_comparison_payload = dict(run.review_payload)
        final_comparison_payload["conflict_types"] = list(
            run.decision.conflict_types
        )
        completed = await asyncio.to_thread(
            complete_semantic_review,
            session_factory,
            raw_message_id=claim.raw_message_id,
            claim_token=claim.token,
            model=run.model,
            auxiliary_payload=run.auxiliary_payload,
            comparison_payload=final_comparison_payload,
            agreement_status=run.decision.agreement_status,
            severity=run.decision.severity,
            differences=list(run.decision.differences),
            prompt_versions=run.prompt_versions,
            compared_at=now,
        )
        if not completed or run.decision.severity != "critical" or notifier is None:
            return True
        claimed_notification = await asyncio.to_thread(
            claim_critical_notification,
            session_factory,
            raw_message_id=claim.raw_message_id,
        )
        if claimed_notification is not None:
            notification_payload = build_semantic_notification_payload(
                claimed_notification.payload
            )
            try:
                await _notify(
                    notifier,
                    raw_message_id=claim.raw_message_id,
                    payload=notification_payload,
                )
            except Exception as exc:
                safe_error = sanitize_notifier_exception(exc)
                try:
                    await asyncio.to_thread(
                        _update_critical_notification_delivery,
                        session_factory,
                        raw_message_id=claim.raw_message_id,
                        fingerprint=claimed_notification.fingerprint,
                        status="failed",
                        error=safe_error,
                    )
                except Exception as status_exc:
                    logger.error(
                        "Critical semantic-review notification status write failed "
                        "for raw_message_id=%s: %s",
                        claim.raw_message_id,
                        sanitize_notifier_exception(status_exc),
                    )
                logger.error(
                    "Critical semantic-review notifier failed for raw_message_id=%s: %s",
                    claim.raw_message_id,
                    safe_error,
                )
            else:
                await asyncio.to_thread(
                    _update_critical_notification_delivery,
                    session_factory,
                    raw_message_id=claim.raw_message_id,
                    fingerprint=claimed_notification.fingerprint,
                    status="sent",
                )
        return True
    except Exception as exc:
        failure_at = now_provider()
        attempts = await asyncio.to_thread(
            _load_review_attempts, session_factory, claim.raw_message_id
        )
        next_attempt_number = attempts + 1
        next_attempt_at = None
        if next_attempt_number < max_attempts:
            next_attempt_at = failure_at + timedelta(
                seconds=retry_delay_seconds * next_attempt_number
            )
        await asyncio.to_thread(
            fail_semantic_review,
            session_factory,
            raw_message_id=claim.raw_message_id,
            claim_token=claim.token,
            error=str(exc),
            next_attempt_at=next_attempt_at,
        )
        logger.exception(
            "Semantic disagreement review failed for raw_message_id=%s",
            claim.raw_message_id,
        )
        return True


async def run_semantic_review_loop(
    *,
    session_factory: sessionmaker,
    config_path: Path,
    notifier: Callable[..., Any] | None,
    poll_interval_seconds: float = 1.0,
    max_attempts: int = 3,
) -> None:
    """Continuously process review rows while isolating per-item failures."""

    while True:
        try:
            config = await asyncio.to_thread(load_ai_recognition_config, config_path)
            processed = await run_semantic_review_once(
                session_factory,
                config=config,
                notifier=notifier,
                now=utc_now(),
                max_attempts=max_attempts,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            processed = False
            logger.exception("Semantic disagreement review loop iteration failed")
        if not processed:
            await asyncio.sleep(poll_interval_seconds)


def normalize_price(value: Any) -> Decimal | tuple[Decimal, ...] | str | None:
    """Return a stable representation for scalar, range, and list prices."""

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple, set)):
        items: list[Decimal | str] = []
        for item in value:
            normalized = normalize_price(item)
            if normalized is None:
                continue
            if isinstance(normalized, tuple):
                items.extend(normalized)
            else:
                items.append(normalized)
        return _collapse_price_items(items)
    if isinstance(value, (int, float, Decimal)):
        return _decimal_or_text(value)

    text = _compact_text(value)
    if not text:
        return None
    text = text.replace(",", "")
    text = re.sub(r"(?:附近|左右|about|approx(?:imately)?)$", "", text).strip()
    if _NUMBER.fullmatch(text):
        return _decimal_or_text(text)
    parts = _PRICE_SEPARATOR.split(text)
    if len(parts) > 1 and all(_NUMBER.fullmatch(part) for part in parts):
        return _collapse_price_items([_decimal_or_text(part) for part in parts])
    return text


def normalize_mimo_action(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a MiMo entry/lifecycle payload to semantic-review vocabulary."""

    strategy = payload.get("strategy")
    strategy = strategy if isinstance(strategy, dict) else {}
    lifecycle = payload.get("lifecycle_event")
    lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
    management = _normalize_management_action(lifecycle.get("management_action"))
    event_type = _token(lifecycle.get("event_type"))

    if event_type in {"exit_position", "exit_full", "full_exit", "close_position"}:
        action_type = "exit_full"
    elif event_type in {"position_update", "update_position"}:
        action_type = (
            "exit_partial"
            if _is_partial_management(management)
            else "position_update"
        )
    elif event_type in {"entry_confirm", "confirm_entry"}:
        action_type = "entry_confirm"
    elif event_type in {"cancel_entry", "entry_cancel"}:
        action_type = "cancel_entry"
    elif event_type in {"exit_partial", "partial_exit"}:
        action_type = "exit_partial"
    elif _is_entry(payload.get("recognition_result")):
        action_type = "entry"
    else:
        action_type = "none"

    source = lifecycle if action_type not in {"entry", "none"} else strategy
    return _normalized_action(
        action_type=action_type,
        source=source,
        management_action=management,
    )


def validate_review_payload(payload: dict[str, Any]) -> None:
    """Validate the closed JSON contract emitted by semantic review."""

    if not isinstance(payload, dict):
        raise ValueError("review payload must be an object")
    required = {
        "independent_action",
        "evidence",
        "conflict_types",
        "material_disagreement",
        "suggested_severity",
        "confidence",
        "reason",
    }
    if set(payload) != required:
        raise ValueError("review payload fields do not match the closed contract")
    action = payload["independent_action"]
    action_fields = {
        "action_type",
        "target_lifecycle_id",
        "symbol",
        "side",
        "stop_loss",
        "take_profit",
        "management_action",
    }
    if not isinstance(action, dict) or set(action) != action_fields:
        raise ValueError("independent_action fields do not match the closed contract")
    if not isinstance(action.get("action_type"), str) or _token(
        action.get("action_type")
    ) not in ACTION_TYPES:
        raise ValueError("invalid independent_action.action_type")
    for field in ("target_lifecycle_id", "symbol", "side", "stop_loss", "take_profit"):
        value = action[field]
        if isinstance(value, bool) or (
            value is not None and not isinstance(value, (str, int, float))
        ):
            raise ValueError(f"independent_action.{field} must be a scalar or null")
    management_action = action["management_action"]
    if management_action is not None and not isinstance(management_action, str):
        raise ValueError("independent_action.management_action must be a string or null")
    evidence = payload["evidence"]
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise ValueError("evidence must be a list of strings")
    conflicts = payload["conflict_types"]
    if (
        not isinstance(conflicts, list)
        or not all(isinstance(item, str) for item in conflicts)
        or any(_token(item) not in _CONFLICT_TYPE_SET for item in conflicts)
    ):
        raise ValueError("conflict_types contains an unsupported value")
    if not isinstance(payload["material_disagreement"], bool):
        raise ValueError("material_disagreement must be a boolean")
    if not isinstance(payload["suggested_severity"], str) or _token(
        payload["suggested_severity"]
    ) not in SEVERITIES:
        raise ValueError("invalid suggested_severity")
    confidence = payload["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be an int or float")
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if not isinstance(payload["reason"], str):
        raise ValueError("reason must be a string")


def decide_semantic_severity(
    *,
    mimo_payload: dict[str, Any],
    review_payload: dict[str, Any],
    automation: dict[str, Any],
    input_kind: str,
    current_message_text: str,
    critical_confidence: float = 0.80,
) -> SemanticReviewDecision:
    """Classify disagreement while preserving deterministic safety floors."""

    validate_review_payload(review_payload)
    if not isinstance(current_message_text, str):
        raise ValueError("current_message_text must be a string")
    if not 0 <= critical_confidence <= 1:
        raise ValueError("critical_confidence must be between 0 and 1")

    mimo = normalize_mimo_action(mimo_payload)
    review = _normalize_review_action(review_payload["independent_action"])
    differences = _action_differences(mimo, review)
    deterministic_conflicts = _deterministic_conflicts(
        mimo,
        review,
        review_payload=review_payload,
        automation=automation,
        current_message_text=current_message_text,
    )
    review_conflicts = tuple(
        _token(item) for item in review_payload["conflict_types"]
    )
    conflicts = _ordered_unique((*deterministic_conflicts, *review_conflicts))

    code_critical = bool(deterministic_conflicts)
    model_critical = _allows_model_critical(
        review_payload,
        review_conflicts=review_conflicts,
        input_kind=input_kind,
        current_message_text=current_message_text,
        critical_confidence=critical_confidence,
    )
    review_claims_difference = bool(
        review_payload["material_disagreement"]
        or any(conflict != "wording_only" for conflict in review_conflicts)
    )
    disagreed = bool(differences or review_claims_difference or deterministic_conflicts)

    if code_critical:
        severity = "critical"
        reason = "Deterministic material conflict: " + ", ".join(
            deterministic_conflicts
        )
    elif model_critical:
        severity = "critical"
        reason = str(review_payload["reason"]).strip() or "Supported semantic critical escalation"
    elif disagreed:
        severity = "normal"
        reason = str(review_payload["reason"]).strip() or "Non-critical semantic difference"
    else:
        severity = "none"
        reason = "Normalized meanings are equivalent"

    return SemanticReviewDecision(
        agreement_status="disagreed" if disagreed else "agreed",
        severity=severity,
        conflict_types=conflicts,
        differences=differences,
        reason=reason,
    )


def _normalize_review_action(action: dict[str, Any]) -> dict[str, Any]:
    management = _normalize_management_action(action.get("management_action"))
    action_type = _token(action.get("action_type"))
    if action_type == "position_update" and _is_partial_management(management):
        action_type = "exit_partial"
    return _normalized_action(
        action_type=action_type,
        source=action,
        management_action=management,
    )


def _normalized_action(
    *,
    action_type: str,
    source: dict[str, Any],
    management_action: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "action_type": action_type,
        "target_lifecycle_id": _normalize_identifier(source.get("target_lifecycle_id")),
        "symbol": _normalize_symbol(source.get("symbol")),
        "side": _normalize_side(source.get("side")),
        "stop_loss": normalize_price(source.get("stop_loss")),
        "take_profit": normalize_price(source.get("take_profit")),
        "management_action": management_action,
    }


def _action_differences(
    mimo: dict[str, Any], review: dict[str, Any]
) -> tuple[str, ...]:
    fields = (
        "action_type",
        "target_lifecycle_id",
        "symbol",
        "side",
        "stop_loss",
        "take_profit",
        "management_action",
    )
    return tuple(field for field in fields if mimo[field] != review[field])


def _deterministic_conflicts(
    mimo: dict[str, Any],
    review: dict[str, Any],
    *,
    review_payload: dict[str, Any],
    automation: dict[str, Any],
    current_message_text: str,
) -> tuple[str, ...]:
    mimo_action = mimo["action_type"]
    review_action = review["action_type"]
    conflicts: list[str] = []
    if (mimo_action in _ACTIONABLE_TYPES) != (review_action in _ACTIONABLE_TYPES):
        conflicts.append("actionability")
        if mimo_action == "exit_full" and review_action == "none":
            conflicts.append("urgent_exit_missed")
    elif mimo_action in _ACTIONABLE_TYPES and mimo_action != review_action:
        if {mimo_action, review_action} == {"exit_full", "exit_partial"}:
            conflicts.append("full_vs_partial_exit")
        else:
            conflicts.append("action_family")

    if mimo_action in _ACTIONABLE_TYPES and review_action in _ACTIONABLE_TYPES:
        for field, conflict in (
            ("symbol", "symbol"),
            ("side", "side"),
            ("target_lifecycle_id", "target_lifecycle"),
        ):
            if mimo[field] != review[field]:
                conflicts.append(conflict)
    if _opposite_stop_intent(
        mimo["management_action"], review["management_action"]
    ):
        conflicts.append("stop_intent")
    if (
        _token(automation.get("status") or automation.get("execution_status"))
        in {"skipped", "failed"}
        and _supports_urgent_or_risk_reduction(review)
        and _has_grounded_evidence(review_payload, current_message_text)
    ):
        conflicts.append("execution_unresolved")
    return _ordered_unique(conflicts)


def _allows_model_critical(
    review_payload: dict[str, Any],
    *,
    review_conflicts: tuple[str, ...],
    input_kind: str,
    current_message_text: str,
    critical_confidence: float,
) -> bool:
    if _token(review_payload["suggested_severity"]) != "critical":
        return False
    if not review_payload["material_disagreement"]:
        return False
    if not any(item in _MODEL_CRITICAL_CONFLICTS for item in review_conflicts):
        return False
    evidence = review_payload["evidence"]
    if not any(item.strip() for item in evidence):
        return False
    if not _has_grounded_evidence(review_payload, current_message_text):
        return False
    if float(review_payload["confidence"]) < critical_confidence:
        return False
    if (
        "image" in _token(input_kind) or _token(input_kind) in {"photo", "media"}
    ) and not current_message_text.strip():
        return False
    return True


def _opposite_stop_intent(
    mimo_actions: tuple[str, ...], review_actions: tuple[str, ...]
) -> bool:
    return bool(
        (
            _PROTECTIVE_STOP_ACTIONS.intersection(mimo_actions)
            and _WEAKENING_STOP_ACTIONS.intersection(review_actions)
        )
        or (
            _WEAKENING_STOP_ACTIONS.intersection(mimo_actions)
            and _PROTECTIVE_STOP_ACTIONS.intersection(review_actions)
        )
    )


def _supports_urgent_or_risk_reduction(action: dict[str, Any]) -> bool:
    if action["action_type"] in {"exit_full", "exit_partial", "cancel_entry"}:
        return True
    return bool(_PROTECTIVE_STOP_ACTIONS.intersection(action["management_action"]))


def _has_grounded_evidence(
    review_payload: dict[str, Any], current_text: str
) -> bool:
    haystack = _evidence_text(current_text)
    if not haystack:
        return False
    return any(
        (needle := _evidence_text(item)) and needle in haystack
        for item in review_payload["evidence"]
    )


def _evidence_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _normalize_management_action(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item) for item in value]
    else:
        raw_items = re.split(r"[,，、;/|]+", str(value))
    normalized = {_token(item).replace(" ", "_") for item in raw_items if _token(item)}
    return tuple(sorted(normalized))


def _is_partial_management(actions: tuple[str, ...]) -> bool:
    return bool(_PARTIAL_MANAGEMENT_ACTIONS.intersection(actions))


def _normalize_identifier(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return str(value).lower()
    normalized = normalize_price(value)
    if isinstance(normalized, Decimal):
        return format(normalized, "f")
    return _compact_text(value)


def _normalize_symbol(value: Any) -> str | None:
    if value is None:
        return None
    symbol = re.sub(r"[\s_\-/]", "", str(value)).upper()
    symbol = re.sub(r"(?:USDT|USD|PERP)$", "", symbol)
    return symbol or None


def _normalize_side(value: Any) -> str | None:
    side = _token(value).replace(" ", "")
    if side in {"long", "buy", "多", "多单", "做多"}:
        return "long"
    if side in {"short", "sell", "空", "空单", "做空"}:
        return "short"
    return side or None


def _is_entry(value: Any) -> bool:
    return _token(value).replace(" ", "") in {
        "是策略",
        "策略",
        "actionable",
        "entry",
        "true",
        "yes",
    }


def _decimal_or_text(value: Any) -> Decimal | str:
    text = str(value).strip().lower().replace(",", "")
    multiplier = Decimal("10000") if text.endswith(("万", "w")) else Decimal("1")
    if multiplier != 1:
        text = text[:-1]
    try:
        number = Decimal(text) * multiplier
    except (InvalidOperation, ValueError):
        return _compact_text(value)
    if not number.is_finite():
        return _compact_text(value)
    return number.normalize()


def _collapse_price_items(
    items: list[Decimal | str],
) -> Decimal | tuple[Decimal, ...] | str | None:
    if not items:
        return None
    if len(items) == 1:
        return items[0]
    if all(isinstance(item, Decimal) for item in items):
        return tuple(sorted(set(items)))
    normalized = tuple(sorted({str(item) for item in items}))
    return normalized[0] if len(normalized) == 1 else " / ".join(normalized)


def _ordered_unique(values: Any) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _compact_text(value: Any) -> str:
    return " ".join(str(value).strip().lower().split())


def _token(value: Any) -> str:
    return _compact_text(value).replace("-", "_")
