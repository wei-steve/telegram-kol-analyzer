"""DeepSeek-backed second-pass strategy-thread resolution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import httpx
from sqlalchemy.orm import sessionmaker

from telegram_kol_research.ai_recognition_config import (
    AiProviderConfig,
    AiRecognitionConfig,
)
from telegram_kol_research.context_resolution_prompt import (
    CONTEXT_RESOLUTION_PROMPT_VERSION,
    CONTEXT_RESOLUTION_SYSTEM_PROMPT,
    build_context_resolution_request,
    render_context_resolution_user_prompt,
)
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageEvidenceVersion,
    RawMessage,
    utc_now,
)
from telegram_kol_research.management_directives import (
    multi_target_action_policy,
)
from telegram_kol_research.runtime_incident_adapters import (
    capture_context_worker_state,
    capture_runtime_incident_best_effort,
)


DECISIONS = frozenset(
    {
        "new_thread",
        "revise_thread",
        "manage_thread",
        "cancel_thread",
        "exit_thread",
        "hold",
        "unresolved",
    }
)
MANAGEMENT_ACTIONS = frozenset(
    {
        "cancel_pending_entry",
        "exit_full",
        "exit_partial",
        "partial_take_profit",
        "move_stop_to_protect",
        "hold_update",
        "risk_update",
        "replace_entry",
    }
)
CONFLICT_TYPES = frozenset(
    {
        "text_image_conflict",
        "reply_target_conflict",
        "multiple_candidates",
        "target_ambiguous",
        "entry_or_revision",
        "exchange_state_conflict",
    }
)
REANALYSIS_TRIGGERS = frozenset(
    {
        "message_edited",
        "reply_target_available",
        "exchange_state_changed",
        "strategy_state_changed",
        "evidence_version_changed",
    }
)
TARGET_REQUIRED_DECISIONS = frozenset(
    {"revise_thread", "manage_thread", "cancel_thread", "exit_thread"}
)
ALLOWED_ACTIONS_BY_DECISION = {
    "new_thread": frozenset({None}),
    "revise_thread": frozenset({None, "replace_entry"}),
    "manage_thread": frozenset(
        {
            None,
            "partial_take_profit",
            "move_stop_to_protect",
            "hold_update",
            "risk_update",
        }
    ),
    "cancel_thread": frozenset({None, "cancel_pending_entry"}),
    "exit_thread": frozenset({None, "exit_full", "exit_partial"}),
    "hold": frozenset({None}),
    "unresolved": frozenset({None}),
}


CONTEXT_RESOLUTION_ERROR_CODES = frozenset(
    {
        "context_contract_invalid",
        "unknown_decision",
        "target_outside_candidate_set",
        "target_required",
        "target_not_allowed",
        "multi_target_action_not_allowed",
        "fanout_not_allowed",
        "unknown_management_action",
        "management_action_incompatible",
        "confidence_invalid",
        "message_evidence_outside_context",
        "network_error",
        "malformed_json",
        "resolved_lifecycle_missing",
    }
)
_TARGET_NOT_ALLOWED_CORRECTION = """
纠错：上一次响应违反 target_not_allowed。
如果 decision 是 new_thread、hold 或 unresolved，target_thread_ids 必须为 []。
只有 revise_thread、manage_thread、cancel_thread、exit_thread 可以携带候选目标。
不要修改 decision 来绕过校验；请保持原本语义并修正字段组合。
""".strip()


class ContextResolutionError(ValueError):
    """Raised with a closed, persistence-safe context failure code."""

    def __init__(self, code: str):
        normalized = (
            code
            if code in CONTEXT_RESOLUTION_ERROR_CODES
            else "context_contract_invalid"
        )
        self.code = normalized
        super().__init__(normalized)


@dataclass(frozen=True, slots=True)
class ContextResolutionDecision:
    decision: str
    target_thread_ids: tuple[int, ...]
    management_action: str | None
    confidence: float
    supporting_message_ids: tuple[int, ...]
    opposing_message_ids: tuple[int, ...]
    conflict_types: tuple[str, ...]
    risk_reducing_fanout_allowed: bool
    reanalysis_triggers: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "target_thread_ids": list(self.target_thread_ids),
            "management_action": self.management_action,
            "confidence": self.confidence,
            "supporting_message_ids": list(self.supporting_message_ids),
            "opposing_message_ids": list(self.opposing_message_ids),
            "conflict_types": list(self.conflict_types),
            "risk_reducing_fanout_allowed": self.risk_reducing_fanout_allowed,
            "reanalysis_triggers": list(self.reanalysis_triggers),
            "reason": self.reason,
        }


def _fanout_allowed(decision: str, management_action: str | None) -> bool:
    effective_action = management_action
    if effective_action is None:
        effective_action = {
            "cancel_thread": "cancel_pending_entry",
            "exit_thread": "exit_full",
        }.get(decision)
    policy = multi_target_action_policy(effective_action)
    return policy.fanout_allowed and (
        (
            decision == "cancel_thread"
            and policy.action == "cancel_pending_entry"
        )
        or (
            decision == "exit_thread"
            and policy.action in {"exit_full", "exit_partial"}
        )
        or (
            decision == "manage_thread"
            and policy.action == "partial_take_profit"
        )
    )


def _int_tuple(value: Any, *, field: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ContextResolutionError("context_contract_invalid")
    try:
        result = tuple(int(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ContextResolutionError("context_contract_invalid") from exc
    if len(result) != len(set(result)):
        raise ContextResolutionError("context_contract_invalid")
    return result


def _closed_tuple(value: Any, *, field: str, allowed: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ContextResolutionError("context_contract_invalid")
    result = tuple(str(item) for item in value)
    if any(item not in allowed for item in result):
        raise ContextResolutionError("context_contract_invalid")
    if len(result) != len(set(result)):
        raise ContextResolutionError("context_contract_invalid")
    return result


def parse_context_resolution_decision(
    payload: Mapping[str, Any],
    *,
    allowed_thread_ids: set[int],
    allowed_message_ids: set[int],
) -> ContextResolutionDecision:
    """Validate the model response against supplied IDs and closed values."""

    if not isinstance(payload, Mapping):
        raise ContextResolutionError("context_contract_invalid")
    decision = str(payload.get("decision") or "")
    if decision not in DECISIONS:
        raise ContextResolutionError("unknown_decision")
    target_thread_ids = _int_tuple(
        payload.get("target_thread_ids"),
        field="target_thread_ids",
    )
    if any(thread_id not in allowed_thread_ids for thread_id in target_thread_ids):
        raise ContextResolutionError("target_outside_candidate_set")
    if decision in TARGET_REQUIRED_DECISIONS and not target_thread_ids:
        raise ContextResolutionError("target_required")
    if decision not in TARGET_REQUIRED_DECISIONS and target_thread_ids:
        raise ContextResolutionError("target_not_allowed")
    fanout = payload.get("risk_reducing_fanout_allowed")
    if not isinstance(fanout, bool):
        raise ContextResolutionError("context_contract_invalid")
    management_action_value = payload.get("management_action")
    management_action = (
        None if management_action_value is None else str(management_action_value)
    )
    if management_action is not None and management_action not in MANAGEMENT_ACTIONS:
        raise ContextResolutionError("unknown_management_action")
    if management_action not in ALLOWED_ACTIONS_BY_DECISION[decision]:
        raise ContextResolutionError("management_action_incompatible")
    if len(target_thread_ids) > 1 and (
        not _fanout_allowed(decision, management_action) or not fanout
    ):
        raise ContextResolutionError("multi_target_action_not_allowed")
    if fanout and not _fanout_allowed(decision, management_action):
        raise ContextResolutionError("fanout_not_allowed")
    try:
        confidence = float(payload.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ContextResolutionError("confidence_invalid") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ContextResolutionError("confidence_invalid")
    supporting = _int_tuple(
        payload.get("supporting_message_ids"),
        field="supporting_message_ids",
    )
    opposing = _int_tuple(
        payload.get("opposing_message_ids"),
        field="opposing_message_ids",
    )
    if any(item not in allowed_message_ids for item in supporting + opposing):
        raise ContextResolutionError("message_evidence_outside_context")
    conflicts = _closed_tuple(
        payload.get("conflict_types"),
        field="conflict_types",
        allowed=CONFLICT_TYPES,
    )
    triggers = _closed_tuple(
        payload.get("reanalysis_triggers"),
        field="reanalysis_triggers",
        allowed=REANALYSIS_TRIGGERS,
    )
    return ContextResolutionDecision(
        decision=decision,
        target_thread_ids=target_thread_ids,
        management_action=management_action,
        confidence=confidence,
        supporting_message_ids=supporting,
        opposing_message_ids=opposing,
        conflict_types=conflicts,
        risk_reducing_fanout_allowed=fanout,
        reanalysis_triggers=triggers,
        reason=str(payload.get("reason") or "").strip(),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _rejected_response_diagnostic(
    payload: Mapping[str, Any],
    *,
    error_class: str,
) -> str:
    decision = str(payload.get("decision") or "")
    targets = payload.get("target_thread_ids")
    return _canonical_json(
        {
            "decision": decision if decision in DECISIONS else None,
            "error_class": error_class,
            "target_thread_count": (
                len(targets) if isinstance(targets, list) else None
            ),
        }
    )


def _collect_ids(value: Any, key_names: set[str]) -> set[int]:
    found: set[int] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in key_names and item is not None:
                try:
                    found.add(int(item))
                except (TypeError, ValueError):
                    pass
            found.update(_collect_ids(item, key_names))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_collect_ids(item, key_names))
    return found


def _select_provider(config: AiRecognitionConfig) -> AiProviderConfig:
    requested = str(config.context_resolution_model_id or "")
    for model in config.ai_models:
        if model.id == requested and model.supports_text:
            return model.provider
    return config.text_provider


def _completion_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def _default_model_caller(
    *,
    provider: AiProviderConfig,
    system_prompt: str,
    request_payload: dict[str, Any],
) -> str:
    if not provider.is_configured:
        raise RuntimeError("context resolution provider is not configured")
    headers = {"Content-Type": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    payload = {
        "model": provider.model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": render_context_resolution_user_prompt(request_payload),
            },
        ],
    }
    with httpx.Client(timeout=provider.timeout_seconds) as client:
        response = client.post(
            _completion_url(provider.base_url),
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    return str(data["choices"][0]["message"]["content"])


def _decode_model_payload(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise json.JSONDecodeError("model response is not JSON text", "", 0)
    text = value.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```")
        text = text.removesuffix("```").strip()
    decoded = json.loads(text)
    if not isinstance(decoded, Mapping):
        raise json.JSONDecodeError("model response is not an object", text, 0)
    return decoded


def _current_evidence_version_id(session_factory, raw_message_id: int) -> int | None:
    with session_factory() as session:
        row = (
            session.query(MessageEvidenceVersion.id)
            .filter(
                MessageEvidenceVersion.raw_message_id == int(raw_message_id),
                MessageEvidenceVersion.superseded_at.is_(None),
            )
            .order_by(MessageEvidenceVersion.version.desc())
            .first()
        )
    return int(row[0]) if row is not None else None


def _upsert_attempt(
    session_factory,
    *,
    raw_message_id: int,
    evidence_version_id: int | None,
    context_fingerprint: str,
    model: str,
    request_payload: dict[str, Any],
    decision: ContextResolutionDecision | None,
    status: str,
    error_class: str | None,
    attempts: int,
    rejected_response_diagnostic_json: str | None = None,
) -> int:
    from telegram_kol_research.context_resolution_worker import (
        build_context_state_fingerprint,
    )

    now = utc_now()
    state_fingerprint = build_context_state_fingerprint(
        session_factory,
        int(raw_message_id),
        candidate_thread_ids=_collect_ids(
            request_payload.get("candidate_strategy_threads"),
            {"thread_id", "strategy_thread_id"},
        ),
    )
    with session_factory() as session:
        row = (
            session.query(ContextResolutionAttempt)
            .filter(
                ContextResolutionAttempt.raw_message_id == int(raw_message_id),
                ContextResolutionAttempt.context_fingerprint == context_fingerprint,
            )
            .one_or_none()
        )
        if row is None:
            row = ContextResolutionAttempt(
                raw_message_id=int(raw_message_id),
                message_evidence_version_id=evidence_version_id,
                context_fingerprint=context_fingerprint,
                state_fingerprint=state_fingerprint,
                model=model,
                prompt_versions_json=_canonical_json(
                    {"context_resolution": CONTEXT_RESOLUTION_PROMPT_VERSION}
                ),
                request_summary_json=_canonical_json(request_payload),
                decision_json=None,
                rejected_response_diagnostic_json=(
                    rejected_response_diagnostic_json
                ),
                status=status,
                error_class=error_class,
                reanalysis_triggers_json="[]",
                attempts=int(attempts),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.message_evidence_version_id = evidence_version_id
            row.state_fingerprint = state_fingerprint
            row.model = model
            row.request_summary_json = _canonical_json(request_payload)
            row.status = status
            row.error_class = error_class
            row.attempts = int(attempts)
            row.updated_at = now
            if rejected_response_diagnostic_json is not None:
                row.rejected_response_diagnostic_json = (
                    rejected_response_diagnostic_json
                )
        if decision is not None:
            row.decision_json = _canonical_json(decision.to_dict())
            row.reanalysis_triggers_json = _canonical_json(
                list(decision.reanalysis_triggers)
            )
        session.commit()
        return int(row.id)


def resolve_contextual_strategy(
    session_factory: sessionmaker,
    *,
    raw_message_id: int,
    ai_recognition_config: AiRecognitionConfig,
    evidence: Any,
    context_window: Any,
    candidates: Any,
    first_pass_payload: Any,
    exchange_state: Any,
    model_caller: Callable[..., Any] = _default_model_caller,
) -> ContextResolutionDecision:
    """Call DeepSeek at most twice, preserving strict contract validation."""

    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            raise LookupError("raw message not found")
        current_message = {
            "raw_message_id": int(raw_message.id),
            "chat_id": int(raw_message.chat_id),
            "message_id": int(raw_message.message_id),
            "posted_at": (
                raw_message.posted_at.isoformat()
                if raw_message.posted_at is not None
                else None
            ),
            "text": raw_message.text,
            "reply_to_message_id": raw_message.reply_to_message_id,
        }
    request_payload = build_context_resolution_request(
        current_message=current_message,
        evidence=evidence,
        context_window=context_window,
        candidates=candidates,
        exchange_state=exchange_state,
        first_pass_payload=first_pass_payload,
    )
    evidence_version_id = _current_evidence_version_id(
        session_factory,
        int(raw_message_id),
    )
    provider = _select_provider(ai_recognition_config)
    fingerprint_payload = {
        "raw_message_id": int(raw_message_id),
        "evidence_version_id": evidence_version_id,
        "request": request_payload,
        "provider": {
            "model_id": ai_recognition_config.context_resolution_model_id,
            "model": provider.model,
            "base_url": provider.base_url.rstrip("/"),
        },
    }
    context_fingerprint = "sha256:" + hashlib.sha256(
        _canonical_json(fingerprint_payload).encode("utf-8")
    ).hexdigest()
    allowed_thread_ids = _collect_ids(
        request_payload.get("candidate_strategy_threads"),
        {"thread_id", "strategy_thread_id"},
    )
    allowed_message_ids = _collect_ids(
        {
            "current": request_payload.get("current_message"),
            "context": request_payload.get("message_context"),
            "candidates": request_payload.get("candidate_strategy_threads"),
        },
        {"message_id", "source_message_id", "root_message_id"},
    )
    with session_factory() as session:
        completed = (
            session.query(ContextResolutionAttempt)
            .filter(
                ContextResolutionAttempt.raw_message_id == int(raw_message_id),
                ContextResolutionAttempt.context_fingerprint == context_fingerprint,
                ContextResolutionAttempt.status == "completed",
            )
            .one_or_none()
        )
        if completed is not None and completed.decision_json:
            return parse_context_resolution_decision(
                json.loads(completed.decision_json),
                allowed_thread_ids=allowed_thread_ids,
                allowed_message_ids=allowed_message_ids,
            )
        exhausted = (
            session.query(ContextResolutionAttempt)
            .filter(
                ContextResolutionAttempt.raw_message_id == int(raw_message_id),
                ContextResolutionAttempt.context_fingerprint == context_fingerprint,
                ContextResolutionAttempt.status == "exhausted",
            )
            .one_or_none()
        )
        if exhausted is not None:
            raise ContextResolutionError(
                str(exhausted.error_class or "context_contract_invalid")
            )
    prior_error_code: str | None = None
    for attempt_number in (1, 2):
        failure: ContextResolutionError | None = None
        decoded: Mapping[str, Any] | None = None
        try:
            raw_result = model_caller(
                provider=provider,
                system_prompt=(
                    CONTEXT_RESOLUTION_SYSTEM_PROMPT
                    if prior_error_code != "target_not_allowed"
                    else CONTEXT_RESOLUTION_SYSTEM_PROMPT
                    + "\n\n"
                    + _TARGET_NOT_ALLOWED_CORRECTION
                ),
                request_payload=request_payload,
            )
        except Exception as exc:
            failure = ContextResolutionError("network_error")
            failure.__cause__ = exc
        else:
            try:
                decoded = _decode_model_payload(raw_result)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                failure = ContextResolutionError("malformed_json")
                failure.__cause__ = exc
            else:
                try:
                    decision = parse_context_resolution_decision(
                        decoded,
                        allowed_thread_ids=allowed_thread_ids,
                        allowed_message_ids=allowed_message_ids,
                    )
                except ContextResolutionError as exc:
                    failure = exc
        if failure is not None:
            terminal = attempt_number == 2
            attempt_id = _upsert_attempt(
                session_factory,
                raw_message_id=raw_message_id,
                evidence_version_id=evidence_version_id,
                context_fingerprint=context_fingerprint,
                model=provider.model,
                request_payload=request_payload,
                decision=None,
                status="exhausted" if terminal else "retry_pending",
                error_class=failure.code,
                attempts=attempt_number,
                rejected_response_diagnostic_json=(
                    _rejected_response_diagnostic(
                        decoded,
                        error_class=failure.code,
                    )
                    if decoded is not None
                    else None
                ),
            )
            if not terminal:
                prior_error_code = failure.code
                continue
            capture_runtime_incident_best_effort(
                capture_context_worker_state,
                session_factory,
                attempt_id=attempt_id,
                raw_message_id=raw_message_id,
                status="exhausted",
                occurred_at=utc_now(),
                error_type=failure.code,
            )
            raise failure
        _upsert_attempt(
            session_factory,
            raw_message_id=raw_message_id,
            evidence_version_id=evidence_version_id,
            context_fingerprint=context_fingerprint,
            model=provider.model,
            request_payload=request_payload,
            decision=decision,
            status="completed",
            error_class=None,
            attempts=attempt_number,
        )
        return decision
    raise AssertionError("unreachable")
