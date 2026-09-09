"""Mandatory, bounded evidence for invalid management fractions."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping

from telegram_kol_research.management_directives import (
    validate_management_fraction_inputs,
)
from telegram_kol_research.models import utc_now
from telegram_kol_research.runtime_incidents import record_runtime_incident


logger = logging.getLogger(__name__)

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
    session_factory,
    *,
    raw_message_id,
    error=None,
    authoritative_generation=None,
    chat_id=None,
    group_trading_mode_provider=None,
):
    """Capture after commit, independent of optional AI capture. Never log content.

    A-8c. The record is written either way; only its deliverability depends on
    the group. A notify_only group executes nothing, so a refused fraction
    there costs nothing and the row is written ``suppressed`` rather than left
    sitting ``pending`` forever, pretending to be on its way to somebody.

    An unknown mode is treated as notify_only. Silence about a message that
    could not have traded is cheap; paging somebody about one is not, and the
    A-8 inventory is a long account of what unwanted alerts cost.
    """

    mode = _resolve_group_mode(chat_id, group_trading_mode_provider)
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
                # The mode rides in ``impact`` rather than a field of its own:
                # ``_SUMMARY_FIELDS`` is a closed vocabulary, and what a reader
                # needs from it is what the mode *means* for this row.
                "impact": (
                    "auto_trade_deliverable"
                    if mode == AUTO_TRADE
                    else f"records_only_group_mode_{mode}"
                ),
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
    if mode != AUTO_TRADE and incident is not None:
        _suppress_delivery(session_factory, incident_id=int(incident.id))
    return incident


AUTO_TRADE = "auto_trade"
#: What a row that will never be delivered says about itself, instead of
#: waiting in ``pending`` for a claimer that is not coming.
SUPPRESSED = "suppressed"


def _resolve_group_mode(chat_id, group_trading_mode_provider) -> str:
    """The group's trading mode, or ``unknown`` -- never a guess, never a query.

    The chat comes from the caller's own context and the mode from the
    provider the caller already holds; this adds no lookup of its own. Any
    failure answers ``unknown``, which is treated as notify_only downstream.
    """

    if chat_id is None or group_trading_mode_provider is None:
        return "unknown"
    try:
        return str(group_trading_mode_provider(int(chat_id)) or "") or "unknown"
    except Exception:
        logger.warning(
            "management fraction gate could not read the group mode chat_id=%s",
            chat_id,
            exc_info=True,
        )
        return "unknown"


def _suppress_delivery(session_factory, *, incident_id: int) -> None:
    from telegram_kol_research.models import RuntimeIncident

    with session_factory() as session:
        row = session.get(RuntimeIncident, incident_id)
        if row is not None and str(row.notification_status) == "pending":
            row.notification_status = SUPPRESSED
            row.updated_at = utc_now()
            session.commit()
