from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RuntimeIncident
from telegram_kol_research.runtime_agent_telegram_evidence import (
    RuntimeAgentTelegramEvidenceError,
    RuntimeAgentTelegramEvidenceRefresh,
)
from telegram_kol_research.runtime_incidents import record_runtime_incident


NOW = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)
COMPLETE_PROOF = {
    "probe_complete": True,
    "endpoint_reachable": True,
    "bot_identity_available": True,
    "target_chat_available": True,
}


def _record_incident(
    session_factory,
    *,
    source_kind: str,
    source_record_id: str,
    fingerprint: str = "e" * 64,
    incident_type: str = "notification_delivery_failure",
):
    return record_runtime_incident(
        session_factory,
        source_kind=source_kind,
        source_record_id=source_record_id,
        incident_type=incident_type,
        severity="medium",
        fingerprint=fingerprint,
        redacted_summary=json.dumps(
            {
                "component": "telegram_notification",
                "notification_status": "failed",
                "error_type": "TimeoutError",
            },
            sort_keys=True,
        ),
        occurred_at=NOW,
        feature_policy_version="runtime-incident-phase-6-v1",
        prompt_version="runtime-agent-prompt-v7",
        tool_policy_version="runtime-agent-tools-v2",
    )


def _runtime_notification_source(session_factory):
    source = _record_incident(
        session_factory,
        source_kind="worker",
        source_record_id="source",
        fingerprint="a" * 64,
        incident_type="provider_retry_exhausted",
    )
    with session_factory() as session:
        row = session.get(RuntimeIncident, source.id)
        row.notification_status = "failed"
        session.commit()
    return source


def _strategy_notification_source(session_factory, *, row_id: int = 91):
    with session_factory() as session:
        session.execute(
            text(
                """
                INSERT INTO strategy_management_notifications (
                    id, management_batch_id, state, payload_fingerprint,
                    payload_json, status, created_at, updated_at
                ) VALUES (
                    :id, 999, 'partial_failed', :fingerprint,
                    '{}', 'failed', :now, :now
                )
                """
            ),
            {
                "id": row_id,
                "fingerprint": f"{row_id:064x}",
                "now": NOW.replace(tzinfo=None),
            },
        )
        session.commit()
    return row_id


def test_refresh_fetches_system_operator_evidence_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    source = _runtime_notification_source(session_factory)
    incident = _record_incident(
        session_factory,
        source_kind="runtime_incident_notification",
        source_record_id=str(source.id),
    )
    calls = []
    refresh = RuntimeAgentTelegramEvidenceRefresh(
        session_factory,
        reader=lambda channel: calls.append(channel) or COMPLETE_PROOF,
    )

    assert refresh.refresh(
        incident_id=incident.id,
        idempotency_key="runtime-incident:2:fetch-evidence:v1",
        expected_fingerprint=incident.fingerprint,
    )

    assert calls == ["system_operator"]
    assert refresh.consume_verification(incident_id=incident.id) == {
        "evidence_fetched": True,
        "evidence_available": True,
        **COMPLETE_PROOF,
    }
    assert refresh.consume_verification(incident_id=incident.id) is None


def test_refresh_maps_strategy_notification_to_notification_bot(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    source_id = _strategy_notification_source(session_factory)
    incident = _record_incident(
        session_factory,
        source_kind="strategy_management_notification",
        source_record_id=str(source_id),
    )
    calls = []
    refresh = RuntimeAgentTelegramEvidenceRefresh(
        session_factory,
        reader=lambda channel: calls.append(channel) or COMPLETE_PROOF,
    )

    assert refresh.refresh(
        incident_id=incident.id,
        idempotency_key="bounded-idempotency",
        expected_fingerprint=incident.fingerprint,
    )
    assert calls == ["notification"]


@pytest.mark.parametrize(
    ("source_kind", "source_record_id", "incident_type", "fingerprint"),
    [
        (
            "runtime_incident_notification",
            "999",
            "notification_delivery_failure",
            "e" * 64,
        ),
        (
            "system_operator_bot",
            "1",
            "notification_delivery_failure",
            "e" * 64,
        ),
        (
            "runtime_incident_notification",
            "1",
            "provider_retry_exhausted",
            "e" * 64,
        ),
        (
            "runtime_incident_notification",
            "1",
            "notification_delivery_failure",
            "f" * 64,
        ),
    ],
)
def test_refresh_refuses_unreachable_or_mismatched_sources(
    tmp_path,
    source_kind,
    source_record_id,
    incident_type,
    fingerprint,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    source = _runtime_notification_source(session_factory)
    actual_source_id = (
        str(source.id) if source_record_id == "1" else source_record_id
    )
    incident = _record_incident(
        session_factory,
        source_kind=source_kind,
        source_record_id=actual_source_id,
        incident_type=incident_type,
    )
    calls = []
    refresh = RuntimeAgentTelegramEvidenceRefresh(
        session_factory,
        reader=lambda channel: calls.append(channel) or COMPLETE_PROOF,
    )

    with pytest.raises(RuntimeAgentTelegramEvidenceError):
        refresh.refresh(
            incident_id=incident.id,
            idempotency_key="bounded-idempotency",
            expected_fingerprint=fingerprint,
        )

    assert calls == []
    assert refresh.consume_verification(incident_id=incident.id) is None


def test_refresh_requires_failed_source_status(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    source = _runtime_notification_source(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, source.id)
        row.notification_status = "delivered"
        session.commit()
    incident = _record_incident(
        session_factory,
        source_kind="runtime_incident_notification",
        source_record_id=str(source.id),
    )
    refresh = RuntimeAgentTelegramEvidenceRefresh(
        session_factory,
        reader=lambda _: COMPLETE_PROOF,
    )

    with pytest.raises(RuntimeAgentTelegramEvidenceError):
        refresh.refresh(
            incident_id=incident.id,
            idempotency_key="bounded-idempotency",
            expected_fingerprint=incident.fingerprint,
        )


@pytest.mark.parametrize(
    "proof",
    [
        {},
        {**COMPLETE_PROOF, "probe_complete": 1},
        {**COMPLETE_PROOF, "endpoint_reachable": "yes"},
        {**COMPLETE_PROOF, "extra": True},
    ],
)
def test_refresh_refuses_malformed_or_expanded_proof(tmp_path, proof):
    session_factory = create_session_factory(tmp_path / "research.db")
    source = _runtime_notification_source(session_factory)
    incident = _record_incident(
        session_factory,
        source_kind="runtime_incident_notification",
        source_record_id=str(source.id),
    )
    refresh = RuntimeAgentTelegramEvidenceRefresh(
        session_factory,
        reader=lambda _: proof,
    )

    with pytest.raises(RuntimeAgentTelegramEvidenceError):
        refresh.refresh(
            incident_id=incident.id,
            idempotency_key="bounded-idempotency",
            expected_fingerprint=incident.fingerprint,
        )


def test_refresh_records_completed_but_unavailable_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    source = _runtime_notification_source(session_factory)
    incident = _record_incident(
        session_factory,
        source_kind="runtime_incident_notification",
        source_record_id=str(source.id),
    )
    proof = {
        **COMPLETE_PROOF,
        "target_chat_available": False,
    }
    refresh = RuntimeAgentTelegramEvidenceRefresh(
        session_factory,
        reader=lambda _: proof,
    )

    assert refresh.refresh(
        incident_id=incident.id,
        idempotency_key="bounded-idempotency",
        expected_fingerprint=incident.fingerprint,
    )
    verification = refresh.consume_verification(incident_id=incident.id)
    assert verification["evidence_fetched"] is True
    assert verification["evidence_available"] is False


def test_refresh_bounds_unconsumed_proofs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    source = _runtime_notification_source(session_factory)
    refresh = RuntimeAgentTelegramEvidenceRefresh(
        session_factory,
        reader=lambda _: COMPLETE_PROOF,
    )
    incidents = []
    for index in range(33):
        incident = _record_incident(
            session_factory,
            source_kind="runtime_incident_notification",
            source_record_id=str(source.id),
            fingerprint=f"{index + 10:064x}",
        )
        assert refresh.refresh(
            incident_id=incident.id,
            idempotency_key=f"bounded-{index}",
            expected_fingerprint=incident.fingerprint,
        )
        incidents.append(incident)

    assert refresh.consume_verification(
        incident_id=incidents[0].id
    ) is None
    assert refresh.consume_verification(
        incident_id=incidents[-1].id
    )["evidence_available"] is True
