from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from telegram_kol_research.config import (
    load_message_operation_supervisor_config,
    load_runtime_incident_config,
)
from telegram_kol_research.message_operation_supervisor import (
    materialize_message_operation_stage1_outbox,
    run_message_operation_supervisor_cycle,
)
from telegram_kol_research.models import (
    MessageInstructionItem,
    MessageOperationContract,
    MessageOperationItem,
    MessageOperationStage1Notification,
    RawMessage,
    RecognitionDecision,
    RuntimeIncident,
    RuntimeIncidentAffectedMessage,
    SignalCandidate,
)
from telegram_kol_research.web_app import create_web_app


NOW = datetime(2026, 8, 9, 3, 0, tzinfo=UTC)
TOKEN = "p" * 43


def _write_runtime_env(path, *, capture_types: str) -> None:
    path.write_text(
        "\n".join(
            (
                "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_ENABLED=true",
                "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_SHADOW_ONLY=true",
                "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_AFTER_RAW_MESSAGE_ID=0",
                "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_BATCH_LIMIT=10",
                f"TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES={capture_types}",
                "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_ENABLED=true",
                "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_AFTER_CONTRACT_ID=0",
                f"TELEGRAM_KOL_RUNTIME_MONITOR_CAPTURE_TOKEN={TOKEN}",
            )
        ),
        encoding="utf-8",
    )


def _load_env_configs(path):
    paths = [path]
    return (
        load_message_operation_supervisor_config(
            environ={}, env_file_paths=paths
        ),
        load_runtime_incident_config(environ={}, env_file_paths=paths),
    )


def _seed_natural_expired_operation(session_factory, *, message_id: int) -> int:
    with session_factory() as session:
        raw = RawMessage(
            chat_id=77,
            message_id=message_id,
            posted_at=NOW - timedelta(minutes=10),
            text="bounded policy lifecycle fixture",
        )
        session.add(raw)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="fixture",
                authoritative_status="策略",
                authoritative_payload_json=json.dumps(
                    {
                        "lifecycle_event": {
                            "event_type": "position_update",
                            "management_action": "partial_take_profit",
                        }
                    }
                ),
                agreement_status="agreed",
                differences_json="[]",
                automation_status="completed",
                prompt_versions_json="{}",
                comparison_status="completed",
            )
        )
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="long",
            event_type="position_update",
            management_action="partial_take_profit",
            review_status="approved",
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=raw.id,
                signal_candidate_id=candidate.id,
                sequence=0,
                instruction_kind="management",
                strategy_instance_id=f"deepcoin:77:{message_id}:BTC:long",
                idempotency_key=f"{raw.id:08d}{0:056d}",
                status="pending",
            )
        )
        session.commit()
        return int(raw.id)


def test_enabled_env_policy_without_required_capture_fails_before_projection(
    tmp_path,
):
    env_path = tmp_path / "runtime_incident_agent.env"
    _write_runtime_env(env_path, capture_types="monitor_adapter_failure")
    supervisor_config, runtime_config = _load_env_configs(env_path)
    app = create_web_app(
        tmp_path / "invalid-policy.db",
        runtime_incident_config=runtime_config,
        message_operation_supervisor_config=supervisor_config,
        message_operation_supervisor_interval_seconds=3600,
        now_provider=lambda: NOW,
    )
    _seed_natural_expired_operation(app.state.session_factory, message_id=9401)

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        time.sleep(0.05)
        response = client.get(
            "/api/runtime-incidents/message-operation-coverage",
            headers={"x-monitor-capture-token": TOKEN},
        )

        assert app.state.message_operation_supervisor_task is None
        assert app.state.message_operation_supervisor_policy_status == (
            "invalid_missing_message_operation_failure_capture"
        )
        assert response.status_code == 200
        assert response.json()["coverage_enabled"] is False
        assert response.json()["supervisor_policy_status"] == (
            "invalid_missing_message_operation_failure_capture"
        )

    with app.state.session_factory() as session:
        assert session.query(MessageOperationContract).count() == 0
        assert session.query(MessageOperationItem).count() == 0
        assert session.query(RuntimeIncident).count() == 0


def test_valid_env_policy_natural_violation_reaches_stage1_and_repeats_cleanly(
    tmp_path,
):
    env_path = tmp_path / "runtime_incident_agent.env"
    _write_runtime_env(env_path, capture_types="message_operation_failure")
    supervisor_config, runtime_config = _load_env_configs(env_path)
    cycle_results: list[dict[str, int]] = []

    def recording_runner(session_factory, **kwargs):
        result = run_message_operation_supervisor_cycle(session_factory, **kwargs)
        cycle_results.append(result)
        return result

    app = create_web_app(
        tmp_path / "valid-policy.db",
        runtime_incident_config=runtime_config,
        message_operation_supervisor_config=supervisor_config,
        message_operation_supervisor_runner=recording_runner,
        message_operation_supervisor_interval_seconds=3600,
        now_provider=lambda: NOW,
    )
    raw_id = _seed_natural_expired_operation(
        app.state.session_factory, message_id=9402
    )

    with TestClient(app):
        deadline = time.monotonic() + 2
        while app.state.message_operation_supervisor_last_success_at is None:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert app.state.message_operation_supervisor_policy_status == "valid"
        assert app.state.message_operation_supervisor_cursor == raw_id
        assert len(cycle_results) == 1
        assert cycle_results[0]["model_calls"] == 0
        assert cycle_results[0]["outcome_model_calls"] == 0
        assert cycle_results[0]["violations_captured"] == 1
        assert cycle_results[0]["capture_errors"] == 0

    assert materialize_message_operation_stage1_outbox(
        app.state.session_factory,
        after_contract_id=runtime_config.message_operation_stage1_after_contract_id,
        created_at=NOW,
    ) == 1

    repeated = run_message_operation_supervisor_cycle(
        app.state.session_factory,
        after_raw_message_id=raw_id,
        capture_after_raw_message_id=supervisor_config.after_raw_message_id,
        limit=supervisor_config.batch_limit,
        observed_at=NOW,
        runtime_incident_config=runtime_config,
    )
    assert repeated["model_calls"] == 0
    assert repeated["outcome_model_calls"] == 0
    assert repeated["violations_captured"] == 0
    assert repeated["capture_errors"] == 0
    assert materialize_message_operation_stage1_outbox(
        app.state.session_factory,
        after_contract_id=runtime_config.message_operation_stage1_after_contract_id,
        created_at=NOW,
    ) == 0

    with app.state.session_factory() as session:
        contract = session.query(MessageOperationContract).one()
        incident = session.query(RuntimeIncident).one()
        affected = session.query(RuntimeIncidentAffectedMessage).one()
        stage1 = session.query(MessageOperationStage1Notification).one()
        assert contract.status == "violated"
        assert contract.runtime_incident_id == incident.id
        assert affected.runtime_incident_id == incident.id
        assert affected.message_operation_contract_id == contract.id
        assert affected.raw_message_id == raw_id
        assert stage1.runtime_incident_id == incident.id
        assert stage1.message_operation_contract_id == contract.id
        assert incident.agent_attempt_count == 0
