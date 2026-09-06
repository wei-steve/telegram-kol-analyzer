"""Mandatory, bounded evidence for invalid management fractions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from telegram_kol_research.management_directives import (
    validate_management_fraction_inputs,
)
from telegram_kol_research.models import utc_now
from telegram_kol_research.runtime_incidents import record_runtime_incident


def validate_management_fraction_payload(payload, text):
    lifecycle = payload.get("lifecycle_event")
    decisions = []
    if isinstance(lifecycle, Mapping) and (
        lifecycle.get("event_type")
        in {
            "position_update",
            "exit_position",
            "exit_full",
            "full_exit",
            "close_position",
            "cancel_entry",
        }
        or lifecycle.get("management_action")
        or any(
            key in lifecycle
            for key in ("management_fraction", "close_fraction", "fraction")
        )
    ):
        decisions.append(lifecycle)
        if isinstance(lifecycle.get("targets"), list):
            decisions.extend(
                target for target in lifecycle["targets"] if isinstance(target, Mapping)
            )
    rows = payload.get("instructions")
    for row in rows if isinstance(rows, (list, tuple)) else []:
        if isinstance(row, Mapping) and row.get("kind") not in {
            "entry",
            "replace_entry",
        }:
            parameters = row.get("parameters")
            if isinstance(parameters, Mapping):
                decisions.append(parameters)
    for decision in decisions:
        validate_management_fraction_inputs(decision, text)


def record_fraction_rejection(
    session_factory, *, raw_message_id, error=None, authoritative_generation=None
):
    """Capture after commit, independent of optional AI capture. Never log content."""
    source_kind = "raw_message"
    source_id = str(raw_message_id)
    fingerprint = hashlib.sha256(
        f"management_fraction_rejected:{source_kind}:{source_id}:{authoritative_generation or ''}".encode()
    ).hexdigest()
    incident = record_runtime_incident(
        session_factory,
        source_kind=source_kind,
        source_record_id=source_id,
        incident_type="management_fraction_rejected",
        severity="high",
        fingerprint=fingerprint,
        redacted_summary=json.dumps(
            {
                "component": "management_directives",
                "reason_code": "management_fraction_invalid",
            }
        ),
        occurred_at=utc_now(),
        feature_policy_version="management-fraction-gate-v1",
        prompt_version="none",
        tool_policy_version="no-exchange-write",
        diagnosis_json=json.dumps(
            {
                "observed_state": {
                    "source": error.source if error is not None else "fraction_inputs",
                    "classification": error.classification
                    if error is not None
                    else "invalid",
                    "default_applied": False,
                }
            }
        ),
        evidence_refs_json=json.dumps([f"raw_message:{raw_message_id}"]),
    )
    return incident
