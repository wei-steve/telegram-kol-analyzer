"""Best-effort adapters from durable technical failures to runtime incidents."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.config import (
    RuntimeIncidentConfig,
    load_runtime_incident_config,
)
from telegram_kol_research.message_operation_types import (
    MESSAGE_OPERATION_VIOLATIONS,
)
from telegram_kol_research.models import (
    ManagementMessageTarget,
    MessageOperationContract,
    utc_now,
)
from telegram_kol_research.runtime_incidents import record_runtime_incident


logger = logging.getLogger(__name__)

_SAFE_LABEL = re.compile(r"[^A-Za-z0-9._-]+")
_STABLE_EVIDENCE_REF = re.compile(
    r"[a-z][a-z0-9_-]{1,31}:[A-Za-z0-9._-]{1,128}\Z"
)
_SENSITIVE_MARKERS = (
    "authorization",
    "bearer",
    "credential",
    "password",
    "passphrase",
    "secret",
    "token",
    "api_key",
    "apikey",
    "dc-access",
)
_MANAGEMENT_INCIDENTS = {
    "submit_unknown": ("management_submit_unknown", "critical"),
    "partial_failed": ("management_partial_failed", "high"),
    "recovery_required": ("management_recovery_required", "critical"),
}
_SHADOW_OBSERVATION_ONLY_MANAGEMENT_REASONS = frozenset(
    {"protection_recovery_required"}
)
MANAGEMENT_TARGET_INCIDENT_TYPES = frozenset(
    {
        "management_target_refused",
        "management_target_orchestration_failed",
        "management_target_visibility_exhausted",
        "management_target_drift",
        "management_target_collision",
    }
)
MANAGEMENT_ENVELOPE_INCIDENT_TYPES = frozenset(
    {"unclassified_operation_failure"}
)


def capture_runtime_incident_best_effort(
    adapter: Callable[..., Any],
    session_factory: sessionmaker,
    *,
    config_loader: Callable[[], RuntimeIncidentConfig] | None = None,
    **kwargs: Any,
):
    """Fail open across both configuration loading and adapter execution."""

    try:
        if config_loader is not None:
            config = config_loader()
        elif os.environ.get("TELEGRAM_KOL_RUNTIME_ROLE") in {
            "ingest",
            "worker",
            "web",
        }:
            config = load_runtime_incident_config(environment_only=True)
        else:
            config = load_runtime_incident_config()
        return adapter(
            session_factory,
            config=config,
            **kwargs,
        )
    except Exception as exc:
        logger.warning(
            "Runtime incident source adapter failed open: adapter=%s error=%s",
            getattr(adapter, "__name__", "unknown"),
            type(exc).__name__,
        )
        return None


def capture_recognition_execution_state(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    family: str,
    row_id: int,
    raw_message_id: int | None,
    phase: str,
    action: str,
    occurred_at: datetime,
):
    """Route a secret-free lease/orphan finding through the incident ledger."""

    return _capture(
        session_factory,
        config=config,
        source_kind="recognition_execution",
        source_record_id=f"{family}:{int(row_id)}",
        incident_type="recognition_execution_orphan",
        severity=(
            "critical"
            if phase in {"executing", "execution_uncertain", "uncertain"}
            else "high"
        ),
        redacted_summary=_summary(
            component=_safe_label(family, limit=64),
            operation=_safe_label(action, limit=64),
            source_status=_safe_label(phase, limit=32),
            impact=(
                f"raw_message_id:{int(raw_message_id)}"
                if raw_message_id is not None
                else "raw_message_id:not_recorded"
            ),
        ),
        occurred_at=occurred_at,
    )


def _safe_label(value: Any, *, fallback: str = "unknown", limit: int = 128) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return "redacted"
    normalized = _SAFE_LABEL.sub("_", text).strip("._-")
    return (normalized or fallback)[:limit]


def _safe_sentence(value: Any, *, limit: int = 256) -> str:
    """Redact and bound free-form text while keeping word breaks as spaces.

    ``_safe_label`` joins every run of punctuation and whitespace with an
    underscore, which turns an ordinary error message into a single 60-character
    mixed-class token. That is exactly the shape ``runtime_incidents`` rejects
    as an apparent opaque secret, and the rejection is swallowed by ``_capture``
    -- so the incident would silently never exist, which is the failure this
    step is here to remove. Spaces keep each word its own short token, while a
    real credential blob has no spaces and stays one long token, so the
    heuristic still catches what it is for.
    """

    text = str(value or "").strip()
    if any(marker in text.lower() for marker in _SENSITIVE_MARKERS):
        return "redacted"
    normalized = " ".join(_SAFE_LABEL.sub(" ", text).split())
    return (normalized or "unknown")[:limit]


def _summary(**values: Any) -> str:
    return json.dumps(
        {
            key: value
            for key, value in values.items()
            if value not in (None, "")
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _fingerprint(
    *,
    incident_type: str,
    source_kind: str,
    source_record_id: str,
    summary: str,
) -> str:
    stable = "\0".join(
        (incident_type, source_kind, source_record_id, summary)
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _capture(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    source_kind: str,
    source_record_id: str,
    incident_type: str,
    severity: str,
    redacted_summary: str,
    occurred_at: datetime,
    evidence_refs_json: str | None = None,
    affected_raw_message_id: int | None = None,
    message_operation_contract_id: int | None = None,
    recorder: Callable[..., Any] | None = None,
):
    if not config.captures(incident_type):
        return None
    source_kind = _safe_label(source_kind, limit=64)
    source_record_id = _safe_label(source_record_id, limit=255)
    incident_type = _safe_label(incident_type, limit=64)
    try:
        capture_kwargs = {
            "source_kind": source_kind,
            "source_record_id": source_record_id,
            "incident_type": incident_type,
            "severity": severity,
            "fingerprint": _fingerprint(
                incident_type=incident_type,
                source_kind=source_kind,
                source_record_id=source_record_id,
                summary=redacted_summary,
            ),
            "redacted_summary": redacted_summary,
            "occurred_at": occurred_at,
            "feature_policy_version": config.feature_policy_version,
            "prompt_version": config.prompt_version,
            "tool_policy_version": config.tool_policy_version,
        }
        if evidence_refs_json is not None:
            capture_kwargs["evidence_refs_json"] = evidence_refs_json
        if affected_raw_message_id is not None:
            capture_kwargs["affected_raw_message_id"] = affected_raw_message_id
        if message_operation_contract_id is not None:
            capture_kwargs["message_operation_contract_id"] = (
                message_operation_contract_id
            )
        return (recorder or record_runtime_incident)(
            session_factory,
            **capture_kwargs,
        )
    except Exception as exc:
        logger.warning(
            "Runtime incident capture failed open: type=%s source=%s error=%s",
            incident_type,
            source_kind,
            type(exc).__name__,
        )
        return None


def capture_message_operation_failure(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    contract_id: int,
    raw_message_id: int,
    violation_code: str,
    evidence_refs: tuple[str, ...] | list[str],
    occurred_at: datetime,
    shadow_only: bool,
):
    """Reuse the incident ledger for one contract violation after shadow review."""

    if shadow_only:
        return None
    if (
        type(contract_id) is not int
        or contract_id < 1
        or type(raw_message_id) is not int
        or raw_message_id < 1
        or violation_code not in MESSAGE_OPERATION_VIOLATIONS
    ):
        raise ValueError("invalid message operation failure identity")
    if not isinstance(evidence_refs, (tuple, list)):
        raise ValueError("invalid message operation evidence references")
    normalized_refs = tuple(
        dict.fromkeys(
            (
                f"message_operation_contract:{contract_id}",
                f"raw_message:{raw_message_id}",
                *evidence_refs,
            )
        )
    )
    if (
        len(normalized_refs) > 32
        or not all(
            isinstance(reference, str)
            and _STABLE_EVIDENCE_REF.fullmatch(reference)
            for reference in normalized_refs
        )
    ):
        raise ValueError("invalid message operation evidence references")
    evidence_refs_json = json.dumps(
        normalized_refs,
        ensure_ascii=True,
        sort_keys=False,
        separators=(",", ":"),
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        if contract is None or contract.raw_message_id != raw_message_id:
            raise ValueError("message operation contract identity mismatch")
        if (
            contract.status != "violated"
            or contract.violation_code != violation_code
        ):
            raise ValueError("message operation terminal violation mismatch")
    incident = _capture(
        session_factory,
        config=config,
        source_kind="message_operation_violation",
        source_record_id=violation_code,
        incident_type="message_operation_failure",
        severity="high",
        redacted_summary=_summary(
            component="message_operation_supervisor",
            source_status="violated",
            reason_code=violation_code,
            operation="coalesced_message_operation",
        ),
        occurred_at=occurred_at,
        evidence_refs_json=evidence_refs_json,
        affected_raw_message_id=raw_message_id,
        message_operation_contract_id=contract_id,
    )
    return incident


def capture_context_worker_state(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    attempt_id: int,
    raw_message_id: int,
    status: str,
    occurred_at: datetime,
    error_type: str | None,
    recorder: Callable[..., Any] | None = None,
):
    """Capture only committed resolver/worker exhaustion, never an intermediate outcome."""

    if str(status).lower() != "exhausted":
        return None
    return _capture(
        session_factory,
        config=config,
        source_kind="context_resolution_attempt",
        source_record_id=str(attempt_id),
        incident_type="context_worker_exhausted",
        severity="high",
        redacted_summary=_summary(
            worker_kind="context_resolution",
            source_status="exhausted",
            error_type=_safe_label(error_type),
            reason_code="context_reanalysis_exhausted",
            operation=f"raw_message_{int(raw_message_id)}",
        ),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def _capture_with_minimal_fallback(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    source_kind: str,
    source_record_id: str,
    incident_type: str,
    severity: str,
    detailed_summary: str,
    minimal_summary: str,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None,
):
    """Record the detailed summary, or a fixed-label one if it is refused.

    ``record_runtime_incident`` enforces a closed field set, a length bound and
    an apparent-secret heuristic, and ``_capture`` swallows every rejection. For
    an alert whose whole purpose is to break a silence, "the summary was too
    interesting so nothing was recorded" is the worst possible outcome. The
    fallback carries only fixed labels and integers, so it cannot trip any of
    those checks, and an operator still learns the incident happened.
    """

    recorded = _capture(
        session_factory,
        config=config,
        source_kind=source_kind,
        source_record_id=source_record_id,
        incident_type=incident_type,
        severity=severity,
        redacted_summary=detailed_summary,
        occurred_at=occurred_at,
        recorder=recorder,
    )
    if recorded is not None or minimal_summary == detailed_summary:
        return recorded
    logger.warning(
        "Runtime incident detailed summary refused; retrying minimal: "
        "type=%s source=%s",
        incident_type,
        source_kind,
    )
    return _capture(
        session_factory,
        config=config,
        source_kind=source_kind,
        source_record_id=source_record_id,
        incident_type=incident_type,
        severity=severity,
        redacted_summary=minimal_summary,
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_authoritative_execution_uncertain(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    attempt_id: int,
    raw_message_id: int,
    occurred_at: datetime,
    error_class: str | None,
    error_summary: str | None,
    recorder: Callable[..., Any] | None = None,
):
    """Capture an attempt frozen past the side-effect boundary.

    ``uncertain`` is the one authoritative outcome that is never replayed: the
    exchange may or may not have acted, so only a human can settle it. Before
    A-2 the freeze left no incident at all, so raw 15006 and 15204 sat unknown
    with nobody told.
    """

    if not config.captures("authoritative_execution_uncertain"):
        return None
    fixed = {
        "component": "authoritative_execution",
        "source_status": "uncertain",
        "operation": f"raw_message_{int(raw_message_id)}",
        "raw_message_id": int(raw_message_id),
        "attempt_id": int(attempt_id),
    }
    return _capture_with_minimal_fallback(
        session_factory,
        config=config,
        source_kind="authoritative_execution_attempt",
        source_record_id=str(int(attempt_id)),
        incident_type="authoritative_execution_uncertain",
        severity="high",
        detailed_summary=_summary(
            **fixed,
            error_type=_safe_label(error_class),
            error_summary=_safe_sentence(error_summary),
        ),
        minimal_summary=_summary(**fixed),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_deferred_instruction_expired(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    raw_message_id: int,
    deferred_minutes: int,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None = None,
):
    """Capture an instruction that outlived its source-deletion deferral.

    The message was recognised, its instruction items were created, and then
    the barrier held it and nothing ever came back. It has a decision row, so
    the authoritative gap recovery does not consider it missing; before A-3
    that produced exactly zero events and zero alerts for 29 items, the oldest
    from 2026-07-22. The instruction is never executed late, so this incident
    is the entire operator-facing outcome.
    """

    if not config.captures("deferred_instruction_expired"):
        return None
    fixed = {
        "component": "deferred_instruction",
        "source_status": "expired",
        "reason_code": "waiting_source_deletion_exit",
        "operation": f"raw_message_{int(raw_message_id)}",
        "raw_message_id": int(raw_message_id),
    }
    return _capture_with_minimal_fallback(
        session_factory,
        config=config,
        source_kind="recognition_decision",
        source_record_id=str(int(raw_message_id)),
        incident_type="deferred_instruction_expired",
        severity="high",
        detailed_summary=_summary(
            **fixed,
            impact=_safe_label(f"deferred_over_{int(deferred_minutes)}_minutes"),
        ),
        minimal_summary=_summary(**fixed),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_entry_admission_expired(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    message_instruction_item_id: int,
    raw_message_id: int,
    chat_id: int,
    defer_reason_code: str,
    deadline_at: datetime | None,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None = None,
):
    """Capture an entry that reached its execution deadline without submitting.

    The entry was recognised and admitted, then held because its adjacent
    context was still incomplete, and then the deadline passed. Nothing else
    reports it: the message has a decision row and an instruction item, so gap
    recovery does not see it as missing, and the item's own terminal state is
    ``failed`` with no operator-facing consequence. Seven of these expired
    between 2026-08-17 and 2026-09-04 -- all in ``auto_trade`` groups, all
    silent, one of them the entry behind a lifecycle that still read
    ``entered`` on the dashboard.
    """

    if not config.captures("entry_admission_expired"):
        return None
    fixed = {
        "component": "entry_admission",
        "source_status": "expired",
        "reason_code": _safe_label(defer_reason_code),
        "operation": f"instruction_item_{int(message_instruction_item_id)}",
        "raw_message_id": int(raw_message_id),
    }
    return _capture_with_minimal_fallback(
        session_factory,
        config=config,
        source_kind="message_instruction_item",
        source_record_id=str(int(message_instruction_item_id)),
        incident_type="entry_admission_expired",
        severity="high",
        detailed_summary=_summary(
            **fixed,
            chat_id=int(chat_id),
            deadline_at=_deadline_label(deadline_at),
            impact="entry_never_submitted",
        ),
        minimal_summary=_summary(**fixed),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_management_recovery_timeout(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    management_batch_id: int,
    strategy_instance_id: str,
    target_lifecycle_id: int,
    effective_action: str,
    recovery_reason_code: str | None,
    timeout_minutes: int,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None = None,
):
    """Capture a management batch that outlived ``recovery_required``.

    ``recovery_required`` is an active batch status, so the batch kept holding
    its strategy's freeze and nothing ever came back for it: batch 158 froze
    one strategy from 2026-09-04 to 2026-09-07 without a single event. The
    timeout blocks the batch -- it is never re-run -- which lifts the freeze,
    so this incident is the entire operator-facing outcome of that decision.
    """

    if not config.captures("management_recovery_timeout"):
        return None
    fixed = {
        "component": "strategy_management_batch",
        "source_status": "recovery_timeout",
        "reason_code": _safe_label(recovery_reason_code or "recovery_required"),
        "operation": f"management_batch_{int(management_batch_id)}",
    }
    return _capture_with_minimal_fallback(
        session_factory,
        config=config,
        source_kind="strategy_management_batch",
        source_record_id=str(int(management_batch_id)),
        incident_type="management_recovery_timeout",
        severity="high",
        detailed_summary=_summary(
            **fixed,
            strategy_instance_id=_safe_label(strategy_instance_id),
            lifecycle_id=int(target_lifecycle_id),
            effective_action=_safe_label(effective_action),
            impact=_safe_label(
                f"blocked_after_{int(timeout_minutes)}_minutes_freeze_released"
            ),
        ),
        minimal_summary=_summary(**fixed),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_source_deletion_exit_stuck(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    deletion_exit_id: int,
    state: str,
    reason_code: str | None,
    timeout_minutes: int,
    lane_released: bool,
    release_reason: str | None,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None = None,
):
    """Capture a source-deletion exit parked in ``recovery_required``.

    The worker never re-claims that state, but the barrier keeps reading the
    exit as a live hold, so the whole chat+symbol+side lane stays sealed. A-3
    found five of them holding 28 instructions, the oldest since 2026-08-14.
    ``lane_released`` says whether the exchange proved the position and orders
    were already gone -- when it did not, the lane is still held on purpose and
    only a person can settle it.
    """

    if not config.captures("source_deletion_exit_stuck"):
        return None
    fixed = {
        "component": "source_message_deletion_exit",
        "source_status": _safe_label(state),
        "reason_code": _safe_label(reason_code or "recovery_required"),
        "operation": f"deletion_exit_{int(deletion_exit_id)}",
    }
    return _capture_with_minimal_fallback(
        session_factory,
        config=config,
        source_kind="source_message_deletion_exit",
        source_record_id=str(int(deletion_exit_id)),
        incident_type="source_deletion_exit_stuck",
        severity="high",
        detailed_summary=_summary(
            **fixed,
            impact=_safe_label(
                f"lane_{'released' if lane_released else 'still_held'}"
                f"_after_{int(timeout_minutes)}_minutes"
            ),
            release_reason=_safe_label(release_reason or "not_released"),
        ),
        minimal_summary=_summary(**fixed),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_management_target_needs_confirmation(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    raw_message_id: int,
    chat_id: int,
    candidate_count: int,
    reason_code: str,
    candidate_digest: str,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None = None,
):
    """Capture a management instruction whose target nobody can settle.

    Two production messages are why this exists. 峰哥 raw 15155 resolved to
    ``target_ambiguous`` between two candidates, one of them a lifecycle that
    had been simulated into existence after its entry failed; 大镖客 raw 15201
    was matched to a position that had closed four days earlier, by price
    description rather than by a reply. Both went nowhere and neither produced
    a single alert.

    The user's 2026-09-07 decision is to notify rather than re-point an
    instruction at a different position. This incident is that notification,
    and it is the whole operator-facing outcome: nothing is executed.
    """

    if not config.captures("management_target_needs_confirmation"):
        return None
    fixed = {
        "component": "management_target",
        "source_status": "awaiting_user_confirmation",
        "reason_code": _safe_label(reason_code),
        "operation": f"raw_message_{int(raw_message_id)}",
        "raw_message_id": int(raw_message_id),
    }
    return _capture_with_minimal_fallback(
        session_factory,
        config=config,
        source_kind="message_instruction_target",
        source_record_id=str(int(raw_message_id)),
        incident_type="management_target_needs_confirmation",
        severity="high",
        detailed_summary=_summary(
            **fixed,
            chat_id=int(chat_id),
            candidate_count=int(candidate_count),
            candidates=_safe_sentence(candidate_digest),
            impact="not_executed_awaiting_user",
        ),
        minimal_summary=_summary(**fixed),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def _deadline_label(deadline_at: datetime | None) -> str:
    """A bare minute-resolution instant, carried in its own summary field.

    It must stay bare. ``record_runtime_incident`` runs an opaque-secret
    heuristic over every summary string, and this instant welded into a longer
    label reads to it as one high-entropy token: 400 spread-out deadlines were
    checked in composite form and every one of them got the whole detailed
    summary refused. Alone it is ordinary date text and passes.
    """

    if deadline_at is None:
        return "unset"
    return f"{deadline_at.strftime('%Y-%m-%dT%H:%M')}Z"


def capture_background_task_restart_exhausted(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    task_name: str,
    consecutive_failures: int,
    error_type: str | None,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None = None,
):
    """Capture a supervised task that gave up restarting.

    Reached only after the backoff ladder failed ``consecutive_failures`` times
    in a row, which means the task is now permanently down until the process is
    restarted -- exactly the 6h48m silence of 2026-09-06.
    """

    if not config.captures("background_task_restart_exhausted"):
        return None
    fixed = {
        "component": "background_task_supervisor",
        "source_status": "restart_exhausted",
        "consecutive_failures": int(consecutive_failures),
    }
    return _capture_with_minimal_fallback(
        session_factory,
        config=config,
        source_kind="background_task",
        source_record_id=_safe_label(task_name, limit=255),
        incident_type="background_task_restart_exhausted",
        severity="critical",
        detailed_summary=_summary(
            **fixed,
            task_name=_safe_label(task_name),
            error_type=_safe_label(error_type),
        ),
        minimal_summary=_summary(**fixed),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_provider_failure(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    source_kind: str,
    source_record_id: str,
    provider_status: str,
    error_type: str | None,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None = None,
):
    """Capture a provider/runtime failure without provider bodies or messages."""

    return _capture(
        session_factory,
        config=config,
        source_kind=source_kind,
        source_record_id=source_record_id,
        incident_type="provider_retry_exhausted",
        severity="high",
        redacted_summary=_summary(
            component="model_provider",
            provider_status=_safe_label(provider_status),
            error_type=_safe_label(error_type),
        ),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_management_state(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    batch_id: int,
    status: str,
    reason_code: str | None,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None = None,
):
    if (
        str(status).lower() == "blocked"
        and str(reason_code or "").lower()
        in _SHADOW_OBSERVATION_ONLY_MANAGEMENT_REASONS
    ):
        return None
    mapping = _MANAGEMENT_INCIDENTS.get(str(status).lower())
    if mapping is None:
        return None
    incident_type, severity = mapping
    return _capture(
        session_factory,
        config=config,
        source_kind="strategy_management_batch",
        source_record_id=str(batch_id),
        incident_type=incident_type,
        severity=severity,
        redacted_summary=_summary(
            component="strategy_management",
            source_status=str(status).lower(),
            reason_code=_safe_label(reason_code),
        ),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def _capture_management_failure(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    source_kind: str,
    source_record_id: int,
    incident_type: str,
    reason_code: str | None,
    severity: str,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None,
):
    return _capture(
        session_factory,
        config=config,
        source_kind=source_kind,
        source_record_id=str(int(source_record_id)),
        incident_type=incident_type,
        severity=str(severity).strip().lower(),
        redacted_summary=_summary(
            component=source_kind,
            source_status="terminal_failure",
            reason_code=_safe_label(reason_code),
        ),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_management_target_failure(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    target_id: int,
    incident_type: str,
    reason_code: str | None,
    severity: str,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None = None,
):
    """Capture one committed target failure without affecting sibling work."""

    if incident_type not in MANAGEMENT_TARGET_INCIDENT_TYPES:
        raise ValueError("unsupported management target incident type")
    incident = _capture_management_failure(
        session_factory,
        config=config,
        source_kind="management_message_target",
        source_record_id=target_id,
        incident_type=incident_type,
        reason_code=reason_code,
        severity=severity,
        occurred_at=occurred_at,
        recorder=recorder,
    )
    if incident is None:
        return None
    try:
        with session_factory() as session:
            target = session.get(ManagementMessageTarget, int(target_id))
            if target is not None:
                target.latest_runtime_incident_id = int(incident.id)
                target.updated_at = utc_now()
                session.commit()
    except Exception as exc:
        logger.warning(
            "Management target incident link failed open: target_id=%s error=%s",
            int(target_id),
            type(exc).__name__,
        )
    return incident


def capture_management_envelope_failure(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    envelope_id: int,
    incident_type: str,
    reason_code: str | None,
    severity: str,
    occurred_at: datetime,
    recorder: Callable[..., Any] | None = None,
):
    """Capture a committed whole-message infrastructure failure."""

    if incident_type not in MANAGEMENT_ENVELOPE_INCIDENT_TYPES:
        raise ValueError("unsupported management envelope incident type")
    return _capture_management_failure(
        session_factory,
        config=config,
        source_kind="management_message_envelope",
        source_record_id=envelope_id,
        incident_type=incident_type,
        reason_code=reason_code,
        severity=severity,
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_monitor_state(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    checked_at: datetime,
    reason_codes: tuple[str, ...] | list[str],
    adapter_failures: tuple[str, ...] | list[str],
    recorder: Callable[..., Any] | None = None,
) -> tuple[Any, ...]:
    """Capture only monitor execution failures, not a normal abnormal audit."""

    normalized_reasons = {_safe_label(reason) for reason in reason_codes}
    captured = []
    for reason_code, incident_type in (
        ("adapter_failure", "monitor_adapter_failure"),
        ("audit_incomplete", "monitor_audit_incomplete"),
    ):
        if reason_code not in normalized_reasons:
            continue
        row = _capture(
            session_factory,
            config=config,
            source_kind="production_safety_monitor",
            source_record_id=reason_code,
            incident_type=incident_type,
            severity="high",
            redacted_summary=_summary(
                component="production_safety_monitor",
                source_status="incomplete",
                reason_code=reason_code,
                error_code=",".join(
                    sorted(_safe_label(item) for item in adapter_failures)
                )
                or "unknown",
            ),
            occurred_at=checked_at,
            recorder=recorder,
        )
        if row is not None:
            captured.append(row)
    return tuple(captured)


def capture_protection_state(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    source_record_id: str,
    severity: str,
    reason_code: str | None,
    occurred_at: datetime,
    current_health_status: str | None = None,
    recorder: Callable[..., Any] | None = None,
):
    if str(current_health_status or "").lower() in {
        "resolved_by_verified_replacement",
        "resolved_by_verified_attribution",
    }:
        return None
    normalized_severity = str(severity).lower()
    actionable_medium = bool(
        normalized_severity == "medium"
        and str(reason_code or "").lower()
        in {
            "native_stop_visible_ownership_unverified",
            "native_stop_ownership_management_blocked",
        }
    )
    if normalized_severity not in {"high", "critical"} and not actionable_medium:
        return None
    return _capture(
        session_factory,
        config=config,
        source_kind="position_protection_incident",
        source_record_id=source_record_id,
        incident_type="severe_protection_incident",
        severity=normalized_severity,
        redacted_summary=_summary(
            component="position_protection",
            source_status="recovery_required",
            reason_code=_safe_label(reason_code),
        ),
        occurred_at=occurred_at,
        recorder=recorder,
    )


def capture_notification_failure(
    session_factory: sessionmaker,
    *,
    config: RuntimeIncidentConfig,
    source_kind: str,
    source_record_id: str,
    error_type: str | None,
    occurred_at: datetime,
    severity: str = "medium",
    recorder: Callable[..., Any] | None = None,
):
    return _capture(
        session_factory,
        config=config,
        source_kind=source_kind,
        source_record_id=source_record_id,
        incident_type="notification_delivery_failure",
        severity=severity,
        redacted_summary=_summary(
            component="telegram_notification",
            notification_status="failed",
            error_type=_safe_label(error_type),
        ),
        occurred_at=occurred_at,
        recorder=recorder,
    )
