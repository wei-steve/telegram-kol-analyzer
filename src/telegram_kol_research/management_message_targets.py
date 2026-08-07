"""Dormant projection and state helpers for multi-target management messages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from telegram_kol_research.models import (
    ManagementMessageEnvelope,
    ManagementMessageTarget,
)


ADMISSION_STATES = frozenset(
    {"identified", "validating", "admitted", "refused"}
)
EXECUTION_STATES = frozenset(
    {
        "not_started",
        "pending",
        "executing",
        "submitted",
        "confirmed",
        "failed",
        "submit_unknown",
        "recovery_required",
    }
)
ATTENTION_STATES = frozenset({"submit_unknown", "recovery_required"})
_EXECUTION_TRANSITIONS = {
    "not_started": frozenset({"pending", "failed"}),
    "pending": frozenset({"executing", "failed"}),
    "executing": frozenset(
        {
            "submitted",
            "confirmed",
            "failed",
            "submit_unknown",
            "recovery_required",
        }
    ),
    "submitted": frozenset(
        {"confirmed", "failed", "submit_unknown", "recovery_required"}
    ),
    "submit_unknown": frozenset({"confirmed", "failed", "recovery_required"}),
    "recovery_required": frozenset({"confirmed", "failed"}),
    "confirmed": frozenset(),
    "failed": frozenset(),
}
_PARAMETER_KEYS = (
    "management_fraction",
    "take_profit",
    "stop_loss",
    "exit_price",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def target_idempotency_fingerprint(
    *,
    raw_message_id: int,
    lifecycle_id: int,
    action: str,
    parameters: Mapping[str, Any],
) -> str:
    canonical = _canonical_json(dict(parameters))
    stable = f"{int(raw_message_id)}\0{int(lifecycle_id)}\0{action}\0{canonical}"
    return _sha256(stable)


def derive_envelope_status(states: Collection[str]) -> str:
    normalized = tuple(str(state) for state in states)
    if any(state in ATTENTION_STATES for state in normalized):
        return "attention_required"
    if normalized and all(state == "confirmed" for state in normalized):
        return "succeeded"
    if "confirmed" in normalized:
        return "partial_success"
    return "failed"


def _decision_parameters(decision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: decision[key]
        for key in _PARAMETER_KEYS
        if decision.get(key) is not None
    }


def _get_or_create_envelope(
    session: Session,
    *,
    raw_message_id: int,
    decision_fingerprint: str,
    action: str,
    parameters_json: str,
    projection_mode: str,
) -> ManagementMessageEnvelope:
    envelope = (
        session.query(ManagementMessageEnvelope)
        .filter(
            ManagementMessageEnvelope.raw_message_id == int(raw_message_id),
            ManagementMessageEnvelope.decision_fingerprint
            == decision_fingerprint,
        )
        .one_or_none()
    )
    if envelope is not None:
        return envelope
    try:
        with session.begin_nested():
            envelope = ManagementMessageEnvelope(
                raw_message_id=int(raw_message_id),
                decision_fingerprint=decision_fingerprint,
                normalized_action=action,
                shared_parameters_json=parameters_json,
                projection_mode=projection_mode,
            )
            session.add(envelope)
            session.flush()
            return envelope
    except IntegrityError:
        envelope = (
            session.query(ManagementMessageEnvelope)
            .filter(
                ManagementMessageEnvelope.raw_message_id == int(raw_message_id),
                ManagementMessageEnvelope.decision_fingerprint
                == decision_fingerprint,
            )
            .one_or_none()
        )
        if envelope is None:
            raise
        return envelope


def project_management_targets_in_session(
    session: Session,
    *,
    raw_message_id: int,
    decision: Mapping[str, Any],
    decision_fingerprint: str,
    projection_mode: str,
) -> list[ManagementMessageTarget]:
    """Project declared targets without authorizing or creating executable work."""

    if len(decision_fingerprint) != 64:
        raise ValueError("decision fingerprint must be 64 characters")
    if projection_mode not in {"shadow", "live"}:
        raise ValueError("unsupported projection mode")
    action = str(decision.get("management_action") or "").strip()
    if not action:
        raise ValueError("management action is required")
    raw_targets = decision.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("explicit management targets are required")

    parameters = _decision_parameters(decision)
    parameters_json = _canonical_json(parameters)
    parameter_fingerprint = _sha256(parameters_json)
    envelope = _get_or_create_envelope(
        session,
        raw_message_id=raw_message_id,
        decision_fingerprint=decision_fingerprint,
        action=action,
        parameters_json=parameters_json,
        projection_mode=projection_mode,
    )

    projected: list[ManagementMessageTarget] = []
    for ordinal, raw_target in enumerate(raw_targets):
        if not isinstance(raw_target, Mapping):
            raise ValueError("management target must be an object")
        lifecycle_id = raw_target.get("target_lifecycle_id")
        if isinstance(lifecycle_id, bool) or not isinstance(lifecycle_id, int):
            raise ValueError("target lifecycle id is required")
        symbol = str(raw_target.get("symbol") or "").strip().upper()
        side = str(raw_target.get("side") or "").strip().lower()
        if not symbol or side not in {"long", "short"}:
            raise ValueError("target symbol and side are required")
        target = (
            session.query(ManagementMessageTarget)
            .filter(
                ManagementMessageTarget.raw_message_id == int(raw_message_id),
                ManagementMessageTarget.target_lifecycle_id == lifecycle_id,
                ManagementMessageTarget.normalized_action == action,
                ManagementMessageTarget.parameter_fingerprint
                == parameter_fingerprint,
            )
            .one_or_none()
        )
        if target is None:
            try:
                with session.begin_nested():
                    target = ManagementMessageTarget(
                        envelope_id=envelope.id,
                        raw_message_id=int(raw_message_id),
                        target_lifecycle_id=lifecycle_id,
                        target_ordinal=ordinal,
                        symbol=symbol,
                        side=side,
                        normalized_action=action,
                        parameters_json=parameters_json,
                        parameter_fingerprint=parameter_fingerprint,
                        collision_group_fingerprint=_sha256(
                            f"lifecycle\0{lifecycle_id}"
                        ),
                        admission_state="identified",
                        execution_state="not_started",
                    )
                    session.add(target)
                    session.flush()
            except IntegrityError:
                target = (
                    session.query(ManagementMessageTarget)
                    .filter(
                        ManagementMessageTarget.raw_message_id
                        == int(raw_message_id),
                        ManagementMessageTarget.target_lifecycle_id
                        == lifecycle_id,
                        ManagementMessageTarget.normalized_action == action,
                        ManagementMessageTarget.parameter_fingerprint
                        == parameter_fingerprint,
                    )
                    .one_or_none()
                )
                if target is None:
                    raise
        projected.append(target)
    return projected


def transition_target_execution_state_in_session(
    session: Session,
    *,
    target_id: int,
    new_state: str,
    now: datetime,
) -> ManagementMessageTarget:
    target = session.get(ManagementMessageTarget, int(target_id))
    if target is None:
        raise LookupError("management message target not found")
    current_state = str(target.execution_state)
    if new_state not in EXECUTION_STATES:
        raise ValueError("unsupported execution state")
    if new_state not in _EXECUTION_TRANSITIONS[current_state]:
        raise ValueError(
            f"invalid execution transition: {current_state} -> {new_state}"
        )
    target.execution_state = new_state
    target.updated_at = now
    if new_state == "executing" and target.execution_started_at is None:
        target.execution_started_at = now
    if new_state in {"confirmed", "failed"}:
        target.terminal_at = now
    session.flush()
    return target
