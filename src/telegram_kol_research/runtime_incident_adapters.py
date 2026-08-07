"""Best-effort adapters from durable technical failures to runtime incidents."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.config import (
    RuntimeIncidentConfig,
    load_runtime_incident_config,
)
from telegram_kol_research.runtime_incidents import record_runtime_incident


logger = logging.getLogger(__name__)

_SAFE_LABEL = re.compile(r"[^A-Za-z0-9._-]+")
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


def capture_runtime_incident_best_effort(
    adapter: Callable[..., Any],
    session_factory: sessionmaker,
    *,
    config_loader: Callable[[], RuntimeIncidentConfig] = (
        load_runtime_incident_config
    ),
    **kwargs: Any,
):
    """Fail open across both configuration loading and adapter execution."""

    try:
        config = config_loader()
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


def _safe_label(value: Any, *, fallback: str = "unknown", limit: int = 128) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return "redacted"
    normalized = _SAFE_LABEL.sub("_", text).strip("._-")
    return (normalized or fallback)[:limit]


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
    recorder: Callable[..., Any] | None = None,
):
    if not config.captures(incident_type):
        return None
    source_kind = _safe_label(source_kind, limit=64)
    source_record_id = _safe_label(source_record_id, limit=255)
    incident_type = _safe_label(incident_type, limit=64)
    try:
        return (recorder or record_runtime_incident)(
            session_factory,
            source_kind=source_kind,
            source_record_id=source_record_id,
            incident_type=incident_type,
            severity=severity,
            fingerprint=_fingerprint(
                incident_type=incident_type,
                source_kind=source_kind,
                source_record_id=source_record_id,
                summary=redacted_summary,
            ),
            redacted_summary=redacted_summary,
            occurred_at=occurred_at,
            feature_policy_version=config.feature_policy_version,
            prompt_version=config.prompt_version,
            tool_policy_version=config.tool_policy_version,
        )
    except Exception as exc:
        logger.warning(
            "Runtime incident capture failed open: type=%s source=%s error=%s",
            incident_type,
            source_kind,
            type(exc).__name__,
        )
        return None


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
    if str(current_health_status or "").lower() == "resolved_by_verified_replacement":
        return None
    normalized_severity = str(severity).lower()
    if normalized_severity not in {"high", "critical"}:
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
    recorder: Callable[..., Any] | None = None,
):
    return _capture(
        session_factory,
        config=config,
        source_kind=source_kind,
        source_record_id=source_record_id,
        incident_type="notification_delivery_failure",
        severity="medium",
        redacted_summary=_summary(
            component="telegram_notification",
            notification_status="failed",
            error_type=_safe_label(error_type),
        ),
        occurred_at=occurred_at,
        recorder=recorder,
    )
