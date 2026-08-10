import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import httpx
import pytest


def test_composite_completion_notification_is_bounded_and_blocks_recovering_state():
    from telegram_kol_research.system_operator_bot import (
        format_composite_management_notification,
    )

    assert format_composite_management_notification(
        {"batch_id": 7, "overall_state": "recovery_required", "first_take_profit": "已撤销", "partial_close": "剩余 5", "protection": "主备止损已核验", "retained_take_profit_total": "5", "error": "Authorization: bearer secret-value"}
    ) is None
    rendered = format_composite_management_notification(
        {"batch_id": 7, "overall_state": "succeeded", "first_take_profit": "已消费", "partial_close": "剩余 5", "protection": "主备止损已核验", "retained_take_profit_total": "5"}
    )
    assert "第一止盈: 已消费" in rendered
    assert "剩余仓位: 剩余 5" in rendered
    assert "保留止盈总量: 5" in rendered
    assert len(rendered) <= 1200


def test_entry_revision_operator_notification_never_claims_planned_orders_changed():
    from telegram_kol_research.system_operator_bot import (
        format_entry_revision_operator_notification,
    )

    rendered = format_entry_revision_operator_notification(
        assembly_evidence={
            "mode": "live", "status": "assembled",
            "configured_risk_budget_usdt": 20, "risk_multiplier": "0.5",
            "effective_risk_budget_usdt": 10, "strategy_message_id": 9902,
        },
        revision_evidence={
            "status": "planned", "reason_code": "planned",
            "replacement_count": 2, "api_response": "secret",
        },
    )
    assert "等待执行入场修订" in rendered
    assert "订单已变更" not in rendered
    assert "secret" not in rendered

import telegram_kol_research.telegram_bot_commands as bot_commands_module
import telegram_kol_research.system_operator_bot as operator_bot_module
from telegram_kol_research.config import RuntimeIncidentConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_instruction_items import (
    create_message_instruction_items_in_session,
)
from telegram_kol_research.models import (
    MessageInstructionItem,
    RawMessage,
    RuntimeIncident,
    SignalCandidate,
)
from telegram_kol_research.runtime_incidents import record_runtime_incident
from telegram_kol_research.system_operator_bot import (
    SystemOperatorBotConfig,
    build_pending_entry_expiry_review_reply_markup,
    format_ai_recognition_conflict_review_message,
    format_semantic_disagreement_notification,
    format_pending_entry_expiry_review_message,
    format_position_attribution_incident_message,
    format_position_protection_incident_message,
    deliver_pending_position_attribution_incidents,
    load_notification_bot_config,
    load_system_operator_bot_config,
    send_semantic_disagreement_notification,
    system_operator_bot_enabled,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def test_operator_maintenance_tick_runs_bounded_entry_reconciler(monkeypatch):
    calls = []
    expected = SimpleNamespace(released=1, expired=0, incidents=0, skipped=0)
    monkeypatch.setattr(
        operator_bot_module,
        "reconcile_due_entry_admissions",
        lambda *args, **kwargs: calls.append((args, kwargs)) or expected,
    )

    result = operator_bot_module.run_operator_maintenance_tick(
        object(),
        now=NOW,
        entry_admission_limit=7,
    )

    assert result is expected
    assert calls[0][1] == {"now": NOW, "limit": 7}


def test_operator_maintenance_tick_runs_bounded_read_only_execution_reconciler(
    monkeypatch,
):
    entry_result = SimpleNamespace(released=0, expired=0, incidents=0, skipped=0)
    execution_calls = []
    monkeypatch.setattr(
        operator_bot_module,
        "reconcile_due_entry_admissions",
        lambda *args, **kwargs: entry_result,
    )
    monkeypatch.setattr(
        operator_bot_module,
        "reconcile_instruction_execution_contracts",
        lambda *args, **kwargs: execution_calls.append((args, kwargs)),
        raising=False,
    )

    result = operator_bot_module.run_operator_maintenance_tick(
        object(),
        now=NOW,
        entry_admission_limit=7,
        execution_contract_mode="shadow",
        execution_reconciliation_client=object(),
        execution_reconciliation_limit=9,
    )

    assert result is entry_result
    assert execution_calls[0][1] == {
        "client": execution_calls[0][1]["client"],
        "reconciled_at": NOW,
        "mode": "shadow",
        "limit": 9,
    }


def test_runtime_notification_loop_wires_execution_reconciliation_only_in_mode(
    monkeypatch,
):
    client = SimpleNamespace(close=lambda: None)
    calls = []
    monkeypatch.setattr(
        operator_bot_module,
        "load_trading_settings",
        lambda session_factory: SimpleNamespace(
            instruction_execution_contract_mode="shadow"
        ),
        raising=False,
    )

    def stop_after_tick(*args, **kwargs):
        calls.append(kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        operator_bot_module,
        "run_operator_maintenance_tick",
        stop_after_tick,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            operator_bot_module.run_runtime_incident_notification_loop(
                session_factory=object(),
                config=SimpleNamespace(),
                deepcoin_client_factory=lambda: client,
            )
        )

    assert calls[0]["execution_contract_mode"] == "shadow"
    assert calls[0]["execution_reconciliation_client"] is client


def test_runtime_notification_delivery_survives_maintenance_failure(monkeypatch):
    delivered = []
    monkeypatch.setattr(
        operator_bot_module,
        "load_trading_settings",
        lambda session_factory: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    async def stop_after_delivery(*args, **kwargs):
        delivered.append(True)
        raise asyncio.CancelledError

    monkeypatch.setattr(
        operator_bot_module,
        "deliver_runtime_incident_notifications",
        stop_after_delivery,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            operator_bot_module.run_runtime_incident_notification_loop(
                session_factory=object(),
                config=SimpleNamespace(),
            )
        )

    assert delivered == [True]


def test_transient_execution_readback_fact_is_recorded_and_recovered(
    tmp_path, monkeypatch
):
    from telegram_kol_research.models import RuntimeIncidentObservation
    import telegram_kol_research.config as config_module

    sf = create_session_factory(tmp_path / "transient-readback.db")
    config_calls = []

    def load_scanner_config(**kwargs):
        config_calls.append(kwargs)
        return SimpleNamespace(
            enabled=True,
            shadow_only=True,
            rules=frozenset({"instruction_execution_contradiction_v1"}),
        )

    monkeypatch.setattr(
        config_module,
        "load_runtime_scanner_config",
        load_scanner_config,
    )
    fact = SimpleNamespace(
        code="exchange_snapshot_incomplete",
        contract_id=7,
        message_instruction_item_id=8,
        raw_message_id=9,
    )
    operator_bot_module._record_execution_reconciliation_monitor_facts(
        sf,
        result=SimpleNamespace(facts=[fact]),
        observed_at=NOW,
    )
    operator_bot_module._record_execution_reconciliation_monitor_facts(
        sf,
        result=SimpleNamespace(facts=[]),
        observed_at=NOW + timedelta(minutes=1),
    )
    with sf() as session:
        assert session.query(RuntimeIncidentObservation).one().state == "observing"

    operator_bot_module._record_execution_reconciliation_monitor_facts(
        sf,
        result=SimpleNamespace(
            facts=[],
            resolved_transient_fact_keys=["7-exchange_snapshot_incomplete"],
        ),
        observed_at=NOW + timedelta(minutes=2),
    )

    with sf() as session:
        row = session.query(RuntimeIncidentObservation).one()
        assert row.object_id == "7-exchange_snapshot_incomplete"
        assert row.state == "resolved_without_incident"
        assert row.recovered_at is not None
    assert all("env_file_paths" not in call for call in config_calls)


def test_automatic_break_even_alert_contains_exact_non_sensitive_handoff():
    rendered = format_position_protection_incident_message({
        "incident_type": "automatic_break_even_recovery_required",
        "pos_id": "pos-1",
        "evidence": {
            "strategy_instance_id": "strategy-1",
            "convergence_id": 17,
            "trigger_type": "tp1_fill",
            "status": "recovery_required",
            "reason_code": "deferred_entry_cancel_outcome_unknown",
            "manual_action": "Read exchange state; do not retry writes.",
        },
    })

    assert "strategy-1" in rendered
    assert "17" in rendered
    assert "pos-1" in rendered
    assert "deferred_entry_cancel_outcome_unknown" in rendered
    assert "do not retry writes" in rendered
    assert "API" not in rendered


def test_source_deletion_operator_alert_is_exact_and_redacted():
    rendered = operator_bot_module.format_terminal_entry_cleanup_notification(
        SimpleNamespace(
            id=9,
            action="source_message_deletion_outcome",
            status="recovery_required",
            response_json=json.dumps(
                {
                    "exit_id": 7,
                    "lifecycle_id": 31,
                    "binding_id": 41,
                    "management_batch_id": 51,
                    "reason": "position_exit_batch_requires_recovery",
                    "flat_proof_confirmed": False,
                }
            ),
        )
    )

    assert "删除退出ID: 7" in rendered
    assert "生命周期: 31" in rendered
    assert "执行绑定: 41" in rendered
    assert "需要人工恢复处理" in rendered
    assert "API" not in rendered


def _record_runtime_incident(session_factory, **overrides):
    values = {
        "source_kind": "context_resolution_attempt",
        "source_record_id": "41",
        "incident_type": "context_worker_exhausted",
        "severity": "high",
        "fingerprint": "a" * 64,
        "redacted_summary": json.dumps(
            {
                "component": "context_resolution",
                "source_status": "exhausted",
                "reason_code": "context_reanalysis_exhausted",
            },
            sort_keys=True,
        ),
        "occurred_at": NOW,
        "feature_policy_version": "runtime-incident-phase-2-v1",
        "prompt_version": "none",
        "tool_policy_version": "none",
    }
    values.update(overrides)
    return record_runtime_incident(session_factory, **values)


def test_ai_agent_title_runtime_incident_has_fixed_bounded_redacted_labels(tmp_path):
    session_factory = create_session_factory(tmp_path / "runtime-format.db")
    incident = _record_runtime_incident(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.redacted_summary = json.dumps(
            {
                "component": "Authorization: bearer never-show-this",
                "source_status": "x" * 1800,
            }
        )
        session.commit()
        session.refresh(row)
        rendered = operator_bot_module.format_runtime_incident_notification(row)

    assert rendered.startswith("AI agent通知：（")
    assert rendered.endswith("）")
    assert f"事件ID: {incident.id}" in rendered
    assert "类型: context_worker_exhausted" in rendered
    assert "严重程度: high" in rendered
    assert "AI诊断: 未启用" in rendered
    assert "自动操作: 未执行" in rendered
    assert "never-show-this" not in rendered
    assert "[redacted]" in rendered
    assert 0 < len(rendered) <= operator_bot_module.RUNTIME_INCIDENT_MESSAGE_MAX_CHARS


def test_ai_agent_title_diagnosis_labels_hypothesis_and_no_action(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "runtime-diagnosis.db")
    incident = _record_runtime_incident(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.status = "diagnosed"
        row.diagnosis_json = json.dumps(
            {
                "hypothesis": "Provider retry exhaustion.",
                "confidence": "medium",
                "missing_evidence": ["current provider health"],
                "recommended_playbook": None,
                "auto_handle_eligible": False,
                "codex_handoff_required": True,
                "remaining_risk": "Source job unresolved.",
                "attempted_queries": ["get_incident_summary"],
            }
        )
        row.evidence_refs_json = f'["incident:{incident.id}"]'
        rendered = operator_bot_module.format_runtime_incident_diagnosis_notification(
            row
        )

    assert "AI诊断假设: Provider retry exhaustion." in rendered
    assert rendered.startswith("AI agent通知：（")
    assert rendered.endswith("）")
    assert "置信度: medium" in rendered
    assert "Codex交接: 需要" in rendered
    assert "自动操作: 未执行" in rendered
    assert 0 < len(rendered) <= operator_bot_module.RUNTIME_INCIDENT_MESSAGE_MAX_CHARS


def test_runtime_incident_diagnosis_notification_labels_shadow_policy(tmp_path):
    session_factory = create_session_factory(tmp_path / "runtime-shadow.db")
    incident = _record_runtime_incident(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.status = "diagnosed"
        row.playbook_name = "refresh_read_only_exchange_snapshot"
        row.recovery_status = "shadow_accepted"
        row.diagnosis_json = json.dumps(
            {
                "hypothesis": "Exchange evidence may be stale.",
                "confidence": "medium",
                "missing_evidence": ["fresh exchange state"],
                "recommended_playbook": "refresh_read_only_exchange_snapshot",
                "auto_handle_eligible": True,
                "codex_handoff_required": False,
                "remaining_risk": "State has not been refreshed.",
                "attempted_queries": ["get_incident_summary"],
                "shadow_playbook_policy": {
                    "mode": "shadow",
                    "policy_version": "runtime-shadow-policy-v1",
                    "nominated_playbook": "refresh_read_only_exchange_snapshot",
                    "playbook_version": 1,
                    "accepted": True,
                    "refusal_reasons": [],
                    "verification_query": "compare_local_exchange",
                    "would_execute": False,
                    "action_executed": False,
                },
            }
        )
        row.evidence_refs_json = f'["incident:{incident.id}"]'
        rendered = operator_bot_module.format_runtime_incident_diagnosis_notification(
            row
        )

    assert "影子预案: refresh_read_only_exchange_snapshot" in rendered
    assert "策略评估: 接受（仅影子）" in rendered
    assert "自动操作: 未执行" in rendered


def test_runtime_incident_notification_does_not_contradict_executed_shadow_record(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "runtime-invalid-shadow.db")
    incident = _record_runtime_incident(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.status = "diagnosed"
        row.diagnosis_json = json.dumps(
            {
                "hypothesis": "Invalid durable policy state.",
                "confidence": "low",
                "missing_evidence": [],
                "recommended_playbook": "rerun_production_audit",
                "auto_handle_eligible": False,
                "codex_handoff_required": True,
                "remaining_risk": "Requires manual verification.",
                "attempted_queries": [],
                "shadow_playbook_policy": {
                    "mode": "shadow",
                    "policy_version": "runtime-shadow-policy-v1",
                    "nominated_playbook": "rerun_production_audit",
                    "playbook_version": 1,
                    "accepted": True,
                    "refusal_reasons": [],
                    "verification_query": "get_service_audit_state",
                    "would_execute": False,
                    "action_executed": True,
                },
            }
        )
        rendered = operator_bot_module.format_runtime_incident_diagnosis_notification(
            row
        )

    assert "策略评估: 无效记录" in rendered
    assert "自动操作: 记录异常，需人工核验" in rendered
    assert "自动操作: 未执行" not in rendered


def test_runtime_incident_notification_reports_verified_low_risk_action(tmp_path):
    session_factory = create_session_factory(tmp_path / "runtime-action.db")
    incident = _record_runtime_incident(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.status = "diagnosed"
        row.recovery_status = "action_verified"
        row.diagnosis_json = json.dumps(
            {
                "hypothesis": "Snapshot was stale.",
                "confidence": "high",
                "missing_evidence": [],
                "recommended_playbook": "refresh_read_only_exchange_snapshot",
                "auto_handle_eligible": True,
                "codex_handoff_required": False,
                "remaining_risk": "Snapshot can become stale again.",
                "attempted_queries": ["get_incident_summary"],
                "recovery_playbook_policy": {
                    "mode": "execute",
                    "policy_version": "runtime-execution-policy-v1",
                    "nominated_playbook": "refresh_read_only_exchange_snapshot",
                    "playbook_version": 1,
                    "accepted": True,
                    "refusal_reasons": [],
                    "verification_query": "compare_local_exchange",
                    "would_execute": True,
                    "action_executed": True,
                    "verification_status": "verified",
                    "attempt_id": 1,
                    "evidence_references": [f"incident:{incident.id}"],
                },
            }
        )
        rendered = operator_bot_module.format_runtime_incident_diagnosis_notification(
            row
        )

    assert "执行预案: refresh_read_only_exchange_snapshot" in rendered
    assert "验证状态: verified" in rendered
    assert f"操作证据: incident:{incident.id}" in rendered
    assert "自动操作: 已执行并验证" in rendered
    assert "自动操作: 未执行" not in rendered


def test_runtime_incident_delivery_uses_diagnosis_report_for_diagnosed_row(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "runtime-diagnosis-send.db")
    incident = _record_runtime_incident(session_factory)
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        row.status = "diagnosed"
        row.diagnosis_json = json.dumps(
            {
                "hypothesis": "Provider retry exhaustion.",
                "confidence": "medium",
                "missing_evidence": [],
                "recommended_playbook": None,
                "auto_handle_eligible": False,
                "codex_handoff_required": True,
                "remaining_risk": "Source job unresolved.",
                "attempted_queries": ["get_incident_summary"],
            }
        )
        row.evidence_refs_json = f'["incident:{incident.id}"]'
        session.commit()
    delivered_text = []

    async def capture(**kwargs):
        delivered_text.append(kwargs["text"])

    monkeypatch.setattr(
        operator_bot_module,
        "send_system_operator_bot_message",
        capture,
    )

    delivered = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=RuntimeIncidentConfig(
                telegram_notifications_enabled=True
            ),
            claimed_at=NOW,
        )
    )

    assert delivered == 1
    assert delivered_text[0].startswith("AI agent通知：（")
    assert "AI诊断假设: Provider retry exhaustion." in delivered_text[0]


def test_runtime_incident_delivery_is_disabled_without_claiming(tmp_path):
    session_factory = create_session_factory(tmp_path / "runtime-disabled.db")
    incident = _record_runtime_incident(session_factory)

    delivered = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=RuntimeIncidentConfig(),
        )
    )

    assert delivered == 0
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.notification_status == "pending"
        assert row.notification_claim_token is None


def _seed_message_operation_stage1(session_factory, *, text="reduce by 50%"):
    from telegram_kol_research.message_operation_supervisor import (
        materialize_message_operation_stage1_outbox,
    )
    from telegram_kol_research.models import MessageOperationContract

    with session_factory() as session:
        raw = RawMessage(
            chat_id=77,
            message_id=9301,
            posted_at=NOW,
            text=text,
        )
        session.add(raw)
        session.flush()
        contract = MessageOperationContract(
            raw_message_id=raw.id,
            intent_kind="take_profit",
            expected_terminal_kind="verified_execution",
            status="violated",
            deadline_at=NOW,
            violation_code="no_operation_created",
            evidence_refs_json=f'["raw_message:{raw.id}"]',
            agent_requested=False,
            policy_version="message-operation-contract-v1",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(contract)
        session.commit()
        contract_id = contract.id
        raw_id = raw.id
    incident = record_runtime_incident(
        session_factory,
        source_kind="message_operation_violation",
        source_record_id="no_operation_created",
        incident_type="message_operation_failure",
        severity="high",
        fingerprint="f" * 64,
        redacted_summary=json.dumps(
            {
                "component": "message_operation_supervisor",
                "source_status": "violated",
                "reason_code": "no_operation_created",
                "impact": "requested operation not verified",
            },
            sort_keys=True,
        ),
        occurred_at=NOW,
        feature_policy_version="message-operation-contract-v1",
        prompt_version="none",
        tool_policy_version="none",
        affected_raw_message_id=raw_id,
        message_operation_contract_id=contract_id,
    )
    assert materialize_message_operation_stage1_outbox(
        session_factory,
        after_contract_id=0,
        created_at=NOW,
        limit=20,
    ) == 1
    return incident, raw_id


def test_message_operation_stage1_claim_recovers_stale_lease_and_counts_attempts(
    tmp_path,
):
    from telegram_kol_research.models import MessageOperationStage1Notification

    session_factory = create_session_factory(tmp_path / "stage1-claim.db")
    _seed_message_operation_stage1(session_factory)

    first = operator_bot_module.claim_next_message_operation_stage1_notification(
        session_factory, claimed_at=NOW, lease_seconds=30
    )
    before = operator_bot_module.claim_next_message_operation_stage1_notification(
        session_factory, claimed_at=NOW + timedelta(seconds=29), lease_seconds=30
    )
    after = operator_bot_module.claim_next_message_operation_stage1_notification(
        session_factory, claimed_at=NOW + timedelta(seconds=31), lease_seconds=30
    )

    assert first is not None
    assert before is None
    assert after is not None
    assert after["claim_token"] != first["claim_token"]
    with session_factory() as session:
        row = session.query(MessageOperationStage1Notification).one()
        assert row.attempt_count == 2
        assert row.status == "delivering"


def test_message_operation_stage1_claim_has_one_concurrent_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    session_factory = create_session_factory(tmp_path / "stage1-concurrent.db")
    _seed_message_operation_stage1(session_factory)
    barrier = __import__("threading").Barrier(2)

    def claim():
        barrier.wait()
        return operator_bot_module.claim_next_message_operation_stage1_notification(
            session_factory,
            claimed_at=NOW,
            lease_seconds=30,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _item: claim(), range(2)))

    assert sum(item is not None for item in claims) == 1


def test_message_operation_stage1_delivery_is_bounded_redacted_and_durable(
    tmp_path,
    monkeypatch,
):
    from telegram_kol_research.models import MessageOperationStage1Notification

    session_factory = create_session_factory(tmp_path / "stage1-delivery.db")
    incident, raw_id = _seed_message_operation_stage1(
        session_factory,
        text="Authorization: bearer never-show-this " + ("x" * 4000),
    )
    deliveries = []

    async def capture(**kwargs):
        deliveries.append(kwargs["text"])
        return 7788

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", capture
    )
    delivered = asyncio.run(
        operator_bot_module.deliver_message_operation_stage1_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            after_contract_id=0,
            claimed_at=NOW,
        )
    )

    assert delivered == 1
    assert len(deliveries) == 1
    assert deliveries[0].startswith("AI agent通知：（")
    assert f"事件ID: {incident.id}" in deliveries[0]
    assert f"源消息ID: {raw_id}" in deliveries[0]
    assert "take_profit" in deliveries[0]
    assert "no_operation_created" in deliveries[0]
    assert "只读AI调查进行中" in deliveries[0]
    assert "never-show-this" not in deliveries[0]
    assert len(deliveries[0]) <= operator_bot_module.RUNTIME_INCIDENT_MESSAGE_MAX_CHARS
    with session_factory() as session:
        row = session.query(MessageOperationStage1Notification).one()
        assert row.status == "delivered"
        assert row.telegram_message_id == "7788"
        assert row.delivered_at.replace(tzinfo=UTC) == NOW
        assert row.claim_token is None


def test_message_operation_stage1_failure_persists_bounded_error_and_retry(
    tmp_path,
    monkeypatch,
):
    from telegram_kol_research.models import MessageOperationStage1Notification

    session_factory = create_session_factory(tmp_path / "stage1-failure.db")
    incident, _ = _seed_message_operation_stage1(session_factory)

    async def fail(**_kwargs):
        raise httpx.ConnectError("secret transport detail")

    monkeypatch.setattr(operator_bot_module, "send_system_operator_bot_message", fail)
    delivered = asyncio.run(
        operator_bot_module.deliver_message_operation_stage1_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            after_contract_id=0,
            claimed_at=NOW,
            lease_seconds=30,
            runtime_config=RuntimeIncidentConfig(
                capture_types=frozenset({"notification_delivery_failure"})
            ),
        )
    )

    assert delivered == 0
    with session_factory() as session:
        row = session.query(MessageOperationStage1Notification).one()
        assert row.status == "failed"
        assert row.error_code == "ConnectError"
        assert row.next_attempt_at.replace(tzinfo=UTC) > NOW
        assert "secret" not in (row.error_code or "")
        assert row.claim_token is None
        notification_failure = session.query(RuntimeIncident).filter_by(
            incident_type="notification_delivery_failure"
        ).one()
        assert notification_failure.severity == "high"
        assert notification_failure.source_kind == (
            "message_operation_stage1_notification"
        )
        assert notification_failure.source_record_id == str(row.id)


def test_message_operation_stage1_exhaustion_is_terminal_and_not_reclaimed(
    tmp_path,
    monkeypatch,
):
    from telegram_kol_research.models import MessageOperationStage1Notification

    session_factory = create_session_factory(tmp_path / "stage1-exhausted.db")
    incident, _ = _seed_message_operation_stage1(session_factory)

    async def fail(**_kwargs):
        raise httpx.ConnectError("temporary")

    monkeypatch.setattr(operator_bot_module, "send_system_operator_bot_message", fail)
    assert asyncio.run(
        operator_bot_module.deliver_message_operation_stage1_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            after_contract_id=0,
            claimed_at=NOW,
            lease_seconds=30,
            max_attempts=1,
        )
    ) == 0
    assert operator_bot_module.claim_next_message_operation_stage1_notification(
        session_factory,
        claimed_at=NOW + timedelta(minutes=5),
        lease_seconds=30,
        max_attempts=1,
    ) is None
    with session_factory() as session:
        row = session.query(MessageOperationStage1Notification).one()
        assert row.status == "exhausted"
        assert row.next_attempt_at is None


def test_message_operation_stage1_config_is_dormant_and_watermark_fails_closed():
    from telegram_kol_research.config import load_runtime_incident_config

    dormant = load_runtime_incident_config(environ={}, env_file_paths=[])
    enabled = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_ENABLED": "true",
            "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_AFTER_CONTRACT_ID": "41",
        },
        env_file_paths=[],
    )
    malformed = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_ENABLED": "true",
            "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE1_AFTER_CONTRACT_ID": "bad",
        },
        env_file_paths=[],
    )

    assert dormant.message_operation_stage1_enabled is False
    assert dormant.message_operation_stage1_after_contract_id == 2**63 - 1
    assert enabled.message_operation_stage1_enabled is True
    assert enabled.message_operation_stage1_after_contract_id == 41
    assert malformed.message_operation_stage1_after_contract_id == 2**63 - 1


def test_message_operation_stage2_config_is_dormant_and_watermark_fails_closed():
    from telegram_kol_research.config import load_runtime_incident_config

    dormant = load_runtime_incident_config(environ={}, env_file_paths=[])
    enabled = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE2_ENABLED": "true",
            "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE2_AFTER_HANDOFF_ID": "7",
        },
        env_file_paths=[],
    )
    malformed = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE2_ENABLED": "true",
            "TELEGRAM_KOL_MESSAGE_OPERATION_STAGE2_AFTER_HANDOFF_ID": "bad",
        },
        env_file_paths=[],
    )

    assert dormant.message_operation_stage2_enabled is False
    assert dormant.message_operation_stage2_after_handoff_id == 2**63 - 1
    assert enabled.message_operation_stage2_enabled is True
    assert enabled.message_operation_stage2_after_handoff_id == 7
    assert malformed.message_operation_stage2_after_handoff_id == 2**63 - 1


def test_stage2_dispatches_copyable_prompt_and_matching_json_document(
    tmp_path, monkeypatch
):
    from telegram_kol_research.models import RuntimeIncidentHandoffArtifact
    from telegram_kol_research.runtime_incident_handoff import (
        persist_runtime_incident_handoff,
    )

    session_factory = create_session_factory(tmp_path / "stage2-delivery.db")
    incident, _ = _seed_message_operation_stage1(session_factory)
    handoff = {
        "incident": {"id": incident.id, "incident_type": "message_operation_failure"},
        "evidence_references": [f"incident:{incident.id}"],
        "agent_hypothesis": {"text": "local state did not converge", "confidence": "low"},
        "codex_prompt": (
            f"请调查 incident_id={incident.id}；读取 AGENTS.md，独立验证，"
            "禁止未知写重试，添加回归测试并遵守服务器部署门槛。"
        ),
    }
    artifact = persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="diagnosed",
        handoff=handoff,
        created_at=NOW,
    )
    with session_factory() as session:
        from telegram_kol_research.models import MessageOperationStage1Notification
        session.query(MessageOperationStage1Notification).update(
            {"status": "delivered", "delivered_at": NOW}
        )
        session.commit()
    messages = []
    documents = []

    async def capture_message(**kwargs):
        messages.append(kwargs["text"])
        return 8801

    async def capture_document(**kwargs):
        documents.append(kwargs)
        return 8802

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", capture_message
    )
    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_document", capture_document
    )
    delivered = asyncio.run(
        operator_bot_module.deliver_runtime_incident_stage2_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            after_handoff_id=0,
            claimed_at=NOW,
        )
    )

    assert delivered == 1
    assert f"交接ID: {artifact.id}" in messages[0]
    assert artifact.content_fingerprint in messages[0]
    assert handoff["codex_prompt"] in messages[0]
    assert documents[0]["filename"] == f"runtime-incident-handoff-{artifact.id}.json"
    document = json.loads(documents[0]["content"])
    assert document["stable_handoff_id"] == artifact.id
    assert document["content_sha256"] == artifact.content_fingerprint
    with session_factory() as session:
        stored = session.get(RuntimeIncidentHandoffArtifact, artifact.id)
        assert stored.status == "delivered"
        assert stored.telegram_message_id == "8801"
        assert stored.telegram_document_message_id == "8802"


def test_stage2_retries_only_the_missing_document_after_partial_delivery(
    tmp_path, monkeypatch
):
    from telegram_kol_research.runtime_incident_handoff import (
        persist_runtime_incident_handoff,
    )

    session_factory = create_session_factory(tmp_path / "stage2-partial.db")
    incident, _ = _seed_message_operation_stage1(session_factory)
    persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="tool_failed",
        handoff={
            "incident": {"id": incident.id},
            "codex_prompt": f"请调查 incident_id={incident.id}，读取 AGENTS.md。",
        },
        created_at=NOW,
    )
    with session_factory() as session:
        from telegram_kol_research.models import MessageOperationStage1Notification
        session.query(MessageOperationStage1Notification).update(
            {"status": "delivered", "delivered_at": NOW}
        )
        session.commit()
    messages = []
    document_attempts = 0

    async def capture_message(**kwargs):
        messages.append(kwargs["text"])
        return 9901

    async def flaky_document(**_kwargs):
        nonlocal document_attempts
        document_attempts += 1
        if document_attempts == 1:
            raise httpx.ConnectError("temporary")
        return 9902

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", capture_message
    )
    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_document", flaky_document
    )

    assert asyncio.run(
        operator_bot_module.deliver_runtime_incident_stage2_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            after_handoff_id=0,
            claimed_at=NOW,
        )
    ) == 0
    assert asyncio.run(
        operator_bot_module.deliver_runtime_incident_stage2_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            after_handoff_id=0,
            claimed_at=NOW + timedelta(seconds=5),
        )
    ) == 1
    assert len(messages) == 1
    assert document_attempts == 2


@pytest.mark.parametrize("missing_segment", ["message", "document"])
def test_stage2_retries_when_telegram_does_not_return_a_durable_message_id(
    tmp_path, monkeypatch, missing_segment
):
    from telegram_kol_research.models import (
        MessageOperationStage1Notification,
        RuntimeIncidentHandoffArtifact,
    )
    from telegram_kol_research.runtime_incident_handoff import (
        persist_runtime_incident_handoff,
    )

    session_factory = create_session_factory(
        tmp_path / f"stage2-missing-{missing_segment}.db"
    )
    incident, _ = _seed_message_operation_stage1(session_factory)
    artifact = persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="diagnosed",
        handoff={
            "incident": {"id": incident.id},
            "codex_prompt": f"请调查 incident_id={incident.id}，读取 AGENTS.md。",
        },
        created_at=NOW,
    )
    with session_factory() as session:
        session.query(MessageOperationStage1Notification).update(
            {"status": "delivered", "delivered_at": NOW}
        )
        session.commit()
    document_calls = []

    async def message_sender(**_kwargs):
        return None if missing_segment == "message" else 701

    async def document_sender(**kwargs):
        document_calls.append(kwargs)
        return None if missing_segment == "document" else 702

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", message_sender
    )
    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_document", document_sender
    )

    assert asyncio.run(
        operator_bot_module.deliver_runtime_incident_stage2_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            after_handoff_id=0,
            claimed_at=NOW,
        )
    ) == 0
    with session_factory() as session:
        stored = session.get(RuntimeIncidentHandoffArtifact, artifact.id)
        assert stored.status == "failed"
        assert stored.telegram_document_message_id is None
        assert stored.telegram_message_id == (
            "701" if missing_segment == "document" else None
        )
    assert len(document_calls) == (1 if missing_segment == "document" else 0)


def test_stage2_claim_waits_for_every_stage1_row_to_finish(tmp_path):
    from telegram_kol_research.models import MessageOperationStage1Notification
    from telegram_kol_research.runtime_incident_handoff import (
        persist_runtime_incident_handoff,
    )

    session_factory = create_session_factory(tmp_path / "stage2-ordering.db")
    incident, _ = _seed_message_operation_stage1(session_factory)
    persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="diagnosed",
        handoff={
            "incident": {"id": incident.id},
            "codex_prompt": f"请调查 incident_id={incident.id}，读取 AGENTS.md。",
        },
        created_at=NOW,
    )

    assert operator_bot_module.claim_next_runtime_incident_stage2_notification(
        session_factory, after_handoff_id=0, claimed_at=NOW
    ) is None
    with session_factory() as session:
        row = session.query(MessageOperationStage1Notification).one()
        row.status = "failed"
        row.next_attempt_at = NOW + timedelta(seconds=5)
        session.commit()
    assert operator_bot_module.claim_next_runtime_incident_stage2_notification(
        session_factory, after_handoff_id=0, claimed_at=NOW
    ) is None
    with session_factory() as session:
        row = session.query(MessageOperationStage1Notification).one()
        row.status = "exhausted"
        row.next_attempt_at = None
        session.commit()
    assert operator_bot_module.claim_next_runtime_incident_stage2_notification(
        session_factory, after_handoff_id=0, claimed_at=NOW
    ) is not None


def test_main_dispatcher_keeps_stage2_dormant_when_stage1_is_disabled(
    tmp_path, monkeypatch
):
    from telegram_kol_research.runtime_incident_handoff import (
        persist_runtime_incident_handoff,
    )

    session_factory = create_session_factory(tmp_path / "stage2-stage1-disabled.db")
    incident, _ = _seed_message_operation_stage1(session_factory)
    persist_runtime_incident_handoff(
        session_factory,
        incident_id=incident.id,
        outcome_kind="diagnosed",
        handoff={
            "incident": {"id": incident.id},
            "codex_prompt": f"请调查 incident_id={incident.id}，读取 AGENTS.md。",
        },
        created_at=NOW,
    )
    sends = []

    async def capture(**kwargs):
        sends.append(kwargs)
        return 1

    monkeypatch.setattr(operator_bot_module, "send_system_operator_bot_message", capture)
    monkeypatch.setattr(operator_bot_module, "send_system_operator_bot_document", capture)
    delivered = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=RuntimeIncidentConfig(
                message_operation_stage1_enabled=False,
                message_operation_stage2_enabled=True,
                message_operation_stage2_after_handoff_id=0,
                telegram_notifications_enabled=False,
            ),
            claimed_at=NOW,
        )
    )

    assert delivered == 0
    assert sends == []


def test_main_runtime_dispatcher_delivers_stage1_without_agent_or_legacy_selector(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "stage1-main-loop.db")
    incident, _ = _seed_message_operation_stage1(session_factory)
    deliveries = []

    async def capture(**kwargs):
        deliveries.append(kwargs["text"])
        return 9901

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", capture
    )
    delivered = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=RuntimeIncidentConfig(
                message_operation_stage1_enabled=True,
                message_operation_stage1_after_contract_id=0,
                agent_enabled=False,
                telegram_notifications_enabled=False,
                telegram_notification_types=frozenset(),
            ),
            claimed_at=NOW,
        )
    )

    assert delivered == 1
    assert len(deliveries) == 1
    assert "消息操作异常（第1阶段）" in deliveries[0]
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.agent_attempt_count == 0
        assert row.notification_status == "pending"


def test_stage1_watermark_excludes_preexisting_outbox_rows(tmp_path, monkeypatch):
    from telegram_kol_research.models import MessageOperationStage1Notification

    session_factory = create_session_factory(tmp_path / "stage1-watermark.db")
    incident, _ = _seed_message_operation_stage1(session_factory)
    from telegram_kol_research.models import MessageOperationContract
    with session_factory() as session:
        contract_id = session.query(MessageOperationContract.id).scalar()
    deliveries = []

    async def capture(**kwargs):
        deliveries.append(kwargs["text"])
        return 9902

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", capture
    )
    delivered = asyncio.run(
        operator_bot_module.deliver_message_operation_stage1_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            after_contract_id=contract_id,
            claimed_at=NOW,
        )
    )

    assert delivered == 0
    assert deliveries == []
    with session_factory() as session:
        row = session.query(MessageOperationStage1Notification).one()
        assert row.status == "pending"
        assert row.attempt_count == 0


def test_runtime_incident_delivery_claims_only_exact_notification_types(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "runtime-type-filter.db")
    capture_only = _record_runtime_incident(
        session_factory,
        source_record_id="monitor-adapter",
        incident_type="monitor_adapter_failure",
        fingerprint="b" * 64,
    )
    allowed = _record_runtime_incident(
        session_factory,
        source_record_id="management-partial",
        incident_type="management_partial_failed",
        fingerprint="c" * 64,
    )
    deliveries = []

    async def capture(**kwargs):
        deliveries.append(kwargs["text"])

    monkeypatch.setattr(
        operator_bot_module,
        "send_system_operator_bot_message",
        capture,
    )
    delivered = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=RuntimeIncidentConfig(
                telegram_notifications_enabled=True,
                telegram_notification_types=frozenset(
                    {"management_partial_failed"}
                ),
            ),
            claimed_at=NOW,
        )
    )

    assert delivered == 1
    assert f"事件ID: {allowed.id}" in deliveries[0]
    with session_factory() as session:
        assert session.get(RuntimeIncident, allowed.id).notification_status == "delivered"
        assert session.get(RuntimeIncident, capture_only.id).notification_status == "pending"


def test_runtime_incident_notification_watermark_claims_only_later_exact_type(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "runtime-watermark.db")
    historical = _record_runtime_incident(
        session_factory,
        incident_type="severe_protection_incident",
    )
    other_type = _record_runtime_incident(
        session_factory,
        source_record_id="42",
        incident_type="management_partial_failed",
        fingerprint="b" * 64,
    )
    later = _record_runtime_incident(
        session_factory,
        source_record_id="43",
        incident_type="severe_protection_incident",
        fingerprint="c" * 64,
    )

    claim = operator_bot_module.claim_next_runtime_incident_notification(
        session_factory,
        claimed_at=NOW,
        notification_types=frozenset({"severe_protection_incident"}),
        after_incident_id=historical.id,
    )

    assert claim is not None
    assert claim["incident"].id == later.id
    with session_factory() as session:
        assert session.get(RuntimeIncident, historical.id).notification_status == "pending"
        assert session.get(RuntimeIncident, other_type.id).notification_status == "pending"


def test_runtime_incident_notification_watermark_leaves_only_history_pending(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "runtime-watermark-only-old.db")
    historical = _record_runtime_incident(
        session_factory,
        incident_type="severe_protection_incident",
    )

    claim = operator_bot_module.claim_next_runtime_incident_notification(
        session_factory,
        claimed_at=NOW,
        notification_types=frozenset({"severe_protection_incident"}),
        after_incident_id=historical.id,
    )

    assert claim is None
    with session_factory() as session:
        row = session.get(RuntimeIncident, historical.id)
        assert row.notification_status == "pending"
        assert row.notification_claim_token is None


def test_runtime_incident_notification_watermark_excludes_old_retry_states(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "runtime-watermark-retry.db")
    failed = _record_runtime_incident(
        session_factory,
        incident_type="severe_protection_incident",
    )
    stale = _record_runtime_incident(
        session_factory,
        source_record_id="42",
        incident_type="severe_protection_incident",
        fingerprint="b" * 64,
    )
    with session_factory() as session:
        session.get(RuntimeIncident, failed.id).notification_status = "failed"
        stale_row = session.get(RuntimeIncident, stale.id)
        stale_row.notification_status = "delivering"
        stale_row.notification_claimed_at = NOW - timedelta(seconds=31)
        session.commit()
    later = _record_runtime_incident(
        session_factory,
        source_record_id="43",
        incident_type="severe_protection_incident",
        fingerprint="c" * 64,
    )

    claim = operator_bot_module.claim_next_runtime_incident_notification(
        session_factory,
        claimed_at=NOW,
        lease_seconds=30,
        notification_types=frozenset({"severe_protection_incident"}),
        after_incident_id=stale.id,
    )

    assert claim is not None
    assert claim["incident"].id == later.id
    with session_factory() as session:
        assert session.get(RuntimeIncident, failed.id).notification_status == "failed"
        assert session.get(RuntimeIncident, stale.id).notification_status == "delivering"


def test_runtime_incident_notification_watermark_none_preserves_oldest_first(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "runtime-watermark-legacy.db")
    historical = _record_runtime_incident(session_factory)
    _record_runtime_incident(
        session_factory,
        source_record_id="42",
        fingerprint="b" * 64,
    )

    claim = operator_bot_module.claim_next_runtime_incident_notification(
        session_factory,
        claimed_at=NOW,
        after_incident_id=None,
    )

    assert claim is not None
    assert claim["incident"].id == historical.id


def test_runtime_incident_delivery_watermark_skips_history_then_delivers_new(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "runtime-watermark-delivery.db")
    historical = _record_runtime_incident(
        session_factory,
        incident_type="severe_protection_incident",
    )
    deliveries = []

    async def capture(**kwargs):
        deliveries.append(kwargs["text"])

    monkeypatch.setattr(
        operator_bot_module,
        "send_system_operator_bot_message",
        capture,
    )
    runtime_config = RuntimeIncidentConfig(
        telegram_notifications_enabled=True,
        telegram_notification_types=frozenset({"severe_protection_incident"}),
        telegram_notification_after_incident_id=historical.id,
    )

    first = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=runtime_config,
            claimed_at=NOW,
        )
    )
    later = _record_runtime_incident(
        session_factory,
        source_record_id="42",
        incident_type="severe_protection_incident",
        fingerprint="b" * 64,
    )
    second = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=runtime_config,
            claimed_at=NOW,
        )
    )

    assert (first, second) == (0, 1)
    assert len(deliveries) == 1
    assert f"事件ID: {later.id}" in deliveries[0]
    with session_factory() as session:
        assert session.get(RuntimeIncident, historical.id).notification_status == "pending"


def test_runtime_incident_notification_retries_failure_and_deduplicates_success(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "runtime-retry.db")
    incident = _record_runtime_incident(session_factory)
    attempts = []

    async def fail_once(**kwargs):
        attempts.append(kwargs["text"])
        if len(attempts) == 1:
            raise httpx.ConnectError("temporary Telegram failure")

    monkeypatch.setattr(
        operator_bot_module,
        "send_system_operator_bot_message",
        fail_once,
    )
    runtime_config = RuntimeIncidentConfig(
        telegram_notifications_enabled=True,
        notification_lease_seconds=30,
    )

    first = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=runtime_config,
            claimed_at=NOW,
        )
    )
    second = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=runtime_config,
            claimed_at=NOW + timedelta(seconds=31),
        )
    )
    third = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=runtime_config,
            claimed_at=NOW + timedelta(seconds=31),
        )
    )

    assert (first, second, third) == (0, 1, 0)
    assert len(attempts) == 2
    with session_factory() as session:
        row = session.get(RuntimeIncident, incident.id)
        assert row.notification_status == "delivered"
        assert row.notification_claim_token is None
        assert row.notified_at is not None


def test_failed_runtime_notification_backs_off_without_starving_newer_incident(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "runtime-fairness.db")
    first = _record_runtime_incident(session_factory)
    second = _record_runtime_incident(
        session_factory,
        source_record_id="42",
        fingerprint="b" * 64,
    )
    attempts = []

    async def fail_first(**kwargs):
        attempts.append(kwargs["text"])
        if len(attempts) == 1:
            raise httpx.ConnectError("Telegram unavailable")

    monkeypatch.setattr(
        operator_bot_module,
        "send_system_operator_bot_message",
        fail_first,
    )
    runtime_config = RuntimeIncidentConfig(
        telegram_notifications_enabled=True,
        notification_lease_seconds=30,
    )

    assert asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=runtime_config,
            claimed_at=NOW,
        )
    ) == 0
    assert asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=runtime_config,
            claimed_at=NOW,
        )
    ) == 1
    assert asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("token", "chat"),
            runtime_config=runtime_config,
            claimed_at=NOW + timedelta(seconds=31),
        )
    ) == 1

    assert f"事件ID: {first.id}" in attempts[0]
    assert f"事件ID: {second.id}" in attempts[1]
    assert f"事件ID: {first.id}" in attempts[2]


def test_runtime_incident_notification_claim_has_one_concurrent_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    session_factory = create_session_factory(tmp_path / "runtime-claim.db")
    _record_runtime_incident(session_factory)
    barrier = __import__("threading").Barrier(2)

    def claim():
        barrier.wait()
        return operator_bot_module.claim_next_runtime_incident_notification(
            session_factory,
            claimed_at=NOW,
            lease_seconds=30,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(), range(2)))

    assert sum(item is not None for item in claims) == 1


def test_runtime_incident_notification_stale_claim_is_recoverable(tmp_path):
    session_factory = create_session_factory(tmp_path / "runtime-stale-claim.db")
    _record_runtime_incident(session_factory)

    first = operator_bot_module.claim_next_runtime_incident_notification(
        session_factory,
        claimed_at=NOW,
        lease_seconds=30,
    )
    before_expiry = operator_bot_module.claim_next_runtime_incident_notification(
        session_factory,
        claimed_at=NOW + timedelta(seconds=29),
        lease_seconds=30,
    )
    after_expiry = operator_bot_module.claim_next_runtime_incident_notification(
        session_factory,
        claimed_at=NOW + timedelta(seconds=31),
        lease_seconds=30,
    )

    assert first is not None
    assert before_expiry is None
    assert after_expiry is not None
    assert after_expiry["claim_token"] != first["claim_token"]


def test_message_instruction_summary_reports_management_failure_and_entry_attempt():
    payload = {
        "message_id": 901,
        "chat_id": -10088,
        "items": [
            {
                "sequence": 0,
                "instruction_kind": "management",
                "strategy_instance_id": "deepcoin:-10088:811:BTC:short",
                "status": "failed",
                "reason": "management_close_order_not_found",
            },
            {
                "sequence": 1,
                "instruction_kind": "entry",
                "strategy_instance_id": "deepcoin:-10088:901:BTC:long",
                "status": "submitted",
                "result": {"reason": "entry_order_submitted"},
            },
        ],
    }

    text = operator_bot_module.format_message_instruction_summary(payload)

    assert "仓位管理: failed" in text
    assert "新策略开仓: submitted" in text
    assert "后续开仓已继续尝试" in text
    assert "#0" in text
    assert "deepcoin:-10088:811:BTC:short" in text


def test_message_instruction_summary_keeps_multi_target_management_outcomes_separate():
    payload = {
        "message_id": 3366,
        "chat_id": -10088,
        "items": [
            {
                "sequence": 0,
                "instruction_kind": "management",
                "strategy_instance_id": "deepcoin:-10088:3365:BTC:short",
                "status": "unknown",
                "reason": "deferred_entry_cancel_preflight_failed",
            },
            {
                "sequence": 1,
                "instruction_kind": "management",
                "strategy_instance_id": "deepcoin:-10088:3359:ETH:short",
                "status": "submitted",
                "result": {"batch_id": 102},
            },
        ],
    }

    text = operator_bot_module.format_message_instruction_summary(payload)

    assert "#0 仓位管理: unknown" in text
    assert "#1 仓位管理: submitted" in text
    assert "deepcoin:-10088:3365:BTC:short" in text
    assert "deepcoin:-10088:3359:ETH:short" in text


def test_message_instruction_summary_sanitizes_persisted_reason_and_splits():
    payload = {
        "items": [
            {
                "sequence": 0,
                "instruction_kind": "management",
                "strategy_instance_id": "strategy-0",
                "status": "failed",
                "reason": "Authorization: secret-value " + "x" * 500,
            },
            *[
                {
                    "sequence": index,
                    "instruction_kind": "entry",
                    "strategy_instance_id": f"strategy-{index}-" + "s" * 255,
                    "status": "submitted",
                    "result": {"reason": "entry_order_submitted"},
                }
                for index in range(1, 30)
            ],
        ],
    }

    chunks = operator_bot_module.split_message_instruction_summary(
        payload,
        max_chars=500,
    )

    assert "[redacted]" in "\n".join(chunks)
    assert "secret-value" not in "\n".join(chunks)
    assert len(chunks) > 1
    assert all(0 < len(chunk) < 4096 for chunk in chunks)
    assert all(chunk.startswith("AI agent通知：（") for chunk in chunks)
    assert all(chunk.endswith("）") for chunk in chunks)


def test_grouped_target_summary_lists_confirmed_and_refused_targets():
    text = operator_bot_module.format_message_instruction_summary(
        {
            "message_id": 3366,
            "chat_id": -10088,
            "targets": [
                {
                    "target_ordinal": 0,
                    "symbol": "BTC",
                    "side": "short",
                    "admission_state": "admitted",
                    "execution_state": "confirmed",
                    "reason_code": None,
                },
                {
                    "target_ordinal": 1,
                    "symbol": "ETH",
                    "side": "short",
                    "admission_state": "refused",
                    "execution_state": "not_started",
                    "reason_code": "target_not_verified",
                },
            ],
            "items": [],
        }
    )

    assert text.startswith("AI agent通知：（")
    assert "BTC short: confirmed" in text
    assert "ETH short: refused" in text
    assert "target_not_verified" in text


def test_message_instruction_summary_notification_failure_retries_claim_once(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES",
        "notification_delivery_failure",
    )
    session_factory = create_session_factory(tmp_path / "summary-delivery.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=901, text="ETH long")
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="ETH",
                side="long",
                event_type="entry_signal",
            )
        )
        session.flush()
        item = create_message_instruction_items_in_session(
            session,
            raw_message_id=raw.id,
        )[0]
        item.status = "succeeded"
        item.result_json = '{"status":"completed"}'
        raw_id, item_id = raw.id, item.id
        session.commit()

    attempts = 0

    async def flaky_sender(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary Telegram failure")

    monkeypatch.setattr(
        operator_bot_module,
        "send_message_instruction_summary_notification",
        flaky_sender,
    )
    config = SystemOperatorBotConfig(bot_token="token", chat_id="chat")

    first = asyncio.run(
        operator_bot_module.deliver_message_instruction_summary_notification(
            session_factory,
            config=config,
            raw_message_id=raw_id,
            claimed_at=NOW,
        )
    )
    second = asyncio.run(
        operator_bot_module.deliver_message_instruction_summary_notification(
            session_factory,
            config=config,
            raw_message_id=raw_id,
            claimed_at=NOW,
        )
    )
    third = asyncio.run(
        operator_bot_module.deliver_message_instruction_summary_notification(
            session_factory,
            config=config,
            raw_message_id=raw_id,
            claimed_at=NOW,
        )
    )

    assert (first, second, third) == (False, True, False)
    assert attempts == 2
    with session_factory() as session:
        assert session.get(MessageInstructionItem, item_id).status == "succeeded"
        incident = session.query(RuntimeIncident).one()
        assert incident.incident_type == "notification_delivery_failure"


def test_pending_summary_scan_skips_ineligible_prefix(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "summary-scan.db")
    ready_raw_id = None
    with session_factory() as session:
        for index in range(7):
            raw = RawMessage(chat_id=88, message_id=1000 + index, text="ETH long")
            session.add(raw)
            session.flush()
            session.add(
                SignalCandidate(
                    raw_message_id=raw.id,
                    symbol="ETH",
                    side="long",
                    event_type="entry_signal",
                )
            )
            session.flush()
            item = create_message_instruction_items_in_session(
                session,
                raw_message_id=raw.id,
            )[0]
            if index == 6:
                item.status = "succeeded"
                item.result_json = '{"status":"completed"}'
                item.summary_notification_status = "failed"
                ready_raw_id = raw.id
        session.commit()

    delivered: list[int] = []

    async def fake_deliver(*args, **kwargs):
        delivered.append(kwargs["raw_message_id"])
        return True

    monkeypatch.setattr(
        operator_bot_module,
        "deliver_message_instruction_summary_notification",
        fake_deliver,
    )

    count = asyncio.run(
        operator_bot_module.deliver_pending_message_instruction_summaries(
            session_factory,
            config=SystemOperatorBotConfig(bot_token="token", chat_id="chat"),
            claimed_at=NOW,
            limit=1,
        )
    )

    assert count == 1
    assert delivered == [ready_raw_id]


def test_management_notification_formatter_has_exact_identity_and_safety_labels():
    message = operator_bot_module.format_strategy_management_notification(
        {
            "batch_id": 88,
            "state": "recovery_required",
            "mode": "shadow",
            "source_chat_id": -10088,
            "source_chat_title": "陈哥群",
            "source_message_id": 901,
            "raw_message_id": 71,
            "lifecycle_id": 11,
            "strategy_instance_id": "deepcoin:-10088:811:BTC:short",
            "execution_binding_id": 12,
            "intent": "adjust_stop_loss",
            "effective_action": "replace_stop_loss",
            "reason": "restore_failed",
            "notification_id": 701,
            "legs": [
                {
                    "leg_id": 3,
                    "execution_order_leg_id": 4,
                    "pos_id": "pos-1",
                    "leg_index": 0,
                    "status": "recovery_required",
                    "planned_close_size": None,
                    "error_summary": {"stage": "restore", "reason_code": "restore_failed"},
                }
            ],
        }
    )

    for expected in (
        "batch #88", "-10088", "#901", "raw=71", "lifecycle=11",
        "deepcoin:-10088:811:BTC:short", "binding=12", "adjust_stop_loss",
        "replace_stop_loss", "pos-1", "未调用交易 API", "禁止自动重试",
        "通知ID: 701",
    ):
        assert expected in message


def test_management_bypass_notification_identifies_exact_full_exit():
    message = operator_bot_module.format_strategy_management_notification(
        {
            "batch_id": 89,
            "state": "reconciling",
            "mode": "live",
            "source_chat_id": -10089,
            "source_message_id": 902,
            "raw_message_id": 72,
            "lifecycle_id": 12,
            "strategy_instance_id": "deepcoin:-10089:812:BTC:short",
            "execution_binding_id": 13,
            "intent": "full_exit",
            "effective_action": "full_exit",
            "reason": "close_submissions_pending_reconciliation",
            "protection_recovery_bypass": {
                "reason": "protection_recovery_required",
                "allowed_action": "full_exit",
                "target_pos_ids": ["pos-89"],
            },
            "legs": [],
        }
    )

    assert "【保护异常旁路全平】" in message
    assert "pos-89" in message
    assert "protection_recovery_required" in message


@pytest.mark.parametrize(
    "state", ["blocked", "partial_failed", "submit_unknown", "recovery_required"]
)
def test_management_notification_formatter_covers_every_alert_state(state):
    message = operator_bot_module.format_strategy_management_notification(
        {
            "batch_id": 1, "state": state, "mode": "live",
            "source_chat_id": -1, "source_message_id": 2, "raw_message_id": 3,
            "lifecycle_id": 4, "strategy_instance_id": "deepcoin:-1:2:BTC:long",
            "execution_binding_id": 5, "intent": "full_exit",
            "effective_action": "full_exit", "reason": "safe_reason", "legs": [],
        }
    )
    assert state in message


def test_deferred_entry_cancel_recovery_notification_names_blocked_order(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding, ExecutionEvent, ExecutionOrderLeg, RawMessage,
        StrategyManagementBatch,
    )

    sf = create_session_factory(tmp_path / "deferred-entry-cancel-alert.db")
    with sf() as session:
        raw = RawMessage(chat_id=-10001, message_id=101, text="full exit")
        session.add(raw); session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:-10001:101:BTC:long", kol_id="kol",
            chat_id=-10001, message_id=101, symbol="BTC", side="long", status="open",
        )
        session.add(binding); session.flush()
        entry = ExecutionOrderLeg(
            execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
            leg_index=0, purpose="entry", order_kind="trigger_limit",
            order_id="deferred-order-1", client_order_id="deferred-client-1",
            status="pending", attribution_status="unassigned",
        )
        session.add(entry); session.flush()
        batch = StrategyManagementBatch(
            idempotency_fingerprint="a" * 64, raw_message_id=raw.id,
            recognition_decision_id=1, recognition_generation="g1",
            target_lifecycle_id=1, strategy_instance_id=binding.strategy_instance_id,
            execution_binding_id=binding.id, intent="full_exit", effective_action="full_exit",
            partial_round_before=0, status="recovery_required",
            reason_code="deferred_entry_cancel_preflight_failed", target_fingerprint="b" * 64,
            target_snapshot_json=json.dumps({
                "identity": {"deferred_entry_leg_ids": [entry.id]},
            }),
            planned_at=NOW,
        )
        session.add(batch); session.flush()
        session.add(ExecutionEvent(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            venue="deepcoin",
            action="strategy_management_deferred_entry_cancel_diagnostic",
            status="failed",
            source_message_id=raw.id,
            reason="exchange_order_id_alias_conflict",
            after_json=json.dumps({
                "execution_order_leg_id": entry.id,
                "live_match_source": "pending_trigger_orders",
                "match_type": "trigger",
                "status": "unresolved",
                "reason": "exchange_order_id_alias_conflict",
            }),
            created_at=NOW,
        ))
        session.flush()
        payload = operator_bot_module._management_payload_for_batch(session, batch)

    text = operator_bot_module.format_strategy_management_notification(payload)

    assert "未成交进场腿撤单未完成" in text
    assert f"批次ID: {batch.id}" in text
    assert binding.strategy_instance_id in text
    assert f"腿: {entry.id}" in text
    assert f"订单: {entry.order_id}" in text
    assert f"客户订单: {entry.client_order_id}" in text
    assert "live=pending_trigger_orders" in text
    assert "type=trigger" in text
    assert "status=unresolved" in text
    assert "reason=exchange_order_id_alias_conflict" in text
    assert "请勿启用替代策略" in text


def test_identity_drift_recovery_notification_renders_persisted_missing_and_extra_legs(
    tmp_path, monkeypatch,
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding, ExecutionEvent, ExecutionOrderLeg, RawMessage,
        StrategyManagementBatch,
    )

    sf = create_session_factory(tmp_path / "deferred-entry-identity-alert.db")
    with sf() as session:
        raw = RawMessage(chat_id=-10001, message_id=102, text="full exit")
        session.add(raw); session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:-10001:102:BTC:long", kol_id="kol",
            chat_id=-10001, message_id=102, symbol="BTC", side="long", status="open",
        )
        session.add(binding); session.flush()
        extra = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1, purpose="entry", order_kind="trigger_limit",
            order_id="unsnap-order-1", client_order_id="unsnap-client-1",
            status="pending", attribution_status="unassigned",
        )
        session.add(extra); session.flush()
        missing_leg_id = extra.id + 1000
        batch = StrategyManagementBatch(
            idempotency_fingerprint="c" * 64, raw_message_id=raw.id,
            recognition_decision_id=1, recognition_generation="g1",
            target_lifecycle_id=1, strategy_instance_id=binding.strategy_instance_id,
            execution_binding_id=binding.id, intent="full_exit", effective_action="full_exit",
            partial_round_before=0, status="recovery_required",
            reason_code="deferred_entry_cancel_preflight_failed", target_fingerprint="d" * 64,
            target_snapshot_json=json.dumps({
                "identity": {"deferred_entry_leg_ids": [missing_leg_id]},
            }),
            planned_at=NOW,
        )
        session.add(batch); session.flush()
        for diagnostic in (
            {
                "execution_order_leg_id": missing_leg_id,
                "identity_state": "snapshot_leg_missing",
                "live_match_source": "not_checked", "match_type": "identity",
                "status": "unresolved", "reason": "snapshot_deferred_entry_leg_missing",
            },
            {
                "execution_order_leg_id": extra.id,
                "order_id": extra.order_id, "client_order_id": extra.client_order_id,
                "identity_state": "unsnapshotted_pending",
                "live_match_source": "not_checked", "match_type": "identity",
                "status": "unresolved", "reason": "unsnapshotted_pending_entry_leg",
            },
        ):
            session.add(ExecutionEvent(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                venue="deepcoin",
                action="strategy_management_deferred_entry_cancel_diagnostic",
                status="failed", source_message_id=raw.id,
                reason=diagnostic["reason"], after_json=json.dumps(diagnostic),
                created_at=NOW,
            ))
        session.flush()
        payload = operator_bot_module._management_payload_for_batch(session, batch)

    monkeypatch.setattr(
        operator_bot_module,
        "_management_payload_for_batch",
        lambda *_args, **_kwargs: pytest.fail("formatter must not read the database"),
    )
    text = operator_bot_module.format_strategy_management_notification(payload)

    assert f"腿: {missing_leg_id}" in text
    assert "身份: snapshot_leg_missing" in text
    assert "订单: 不可用(快照腿缺失或漂移)" in text
    assert f"腿: {extra.id}" in text
    assert "身份: unsnapshotted_pending" in text
    assert "订单: unsnap-order-1" in text
    assert "客户订单: unsnap-client-1" in text
    assert "- -" not in text.split("未成交进场腿:", 1)[1].split("仓位/腿结果:", 1)[0]
    assert all(
        len(chunk) < 4096
        for chunk in operator_bot_module.split_strategy_management_notification(payload)
    )


def test_management_notification_splits_maximum_identifiers_below_telegram_limit():
    max_id = 9_223_372_036_854_775_807
    payload = {
        "notification_id": max_id,
        "batch_id": max_id,
        "state": "recovery_required",
        "mode": "live",
        "source_chat_id": -max_id,
        "source_message_id": max_id,
        "raw_message_id": max_id,
        "lifecycle_id": max_id,
        "strategy_instance_id": "s" * 255,
        "execution_binding_id": max_id,
        "intent": "full_exit",
        "effective_action": "full_exit",
        "reason": "deferred_entry_cancel_preflight_failed",
        "deferred_entry_legs": [
            {
                "execution_order_leg_id": max_id - index,
                "order_id": f"order-{index}-" + "o" * 120,
                "client_order_id": f"client-{index}-" + "c" * 120,
                "cancellation_diagnostic": {
                    "live_match_source": "pending_trigger_orders",
                    "match_type": "trigger",
                    "status": "unresolved",
                    "reason": "exchange_order_id_alias_conflict",
                },
            }
            for index in range(20)
        ],
        "legs": [
            {
                "leg_id": max_id - index,
                "execution_order_leg_id": max_id - index,
                "pos_id": "p" * 120,
                "leg_index": index,
                "status": "submit_unknown",
                "planned_close_size": "9" * 64,
                "client_order_id": "c" * 120,
                "exchange_order_id": "o" * 120,
                "error_summary": {"reason": "management_close_order_not_found"},
            }
            for index in range(20)
        ],
    }

    first = operator_bot_module.split_strategy_management_notification(payload)
    second = operator_bot_module.split_strategy_management_notification(payload)

    assert first == second
    assert len(first) > 1
    assert all(0 < len(chunk) < 4096 for chunk in first)
    assert "\n".join(first) == operator_bot_module.format_strategy_management_notification(payload)


def test_management_notification_dedup_retry_and_concurrent_claim(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from telegram_kol_research.models import (
        ExecutionBinding, ExecutionOrderLeg, RawMessage, RecognitionDecision,
        StrategyLifecycle, StrategyManagementBatch, StrategyManagementLeg,
        StrategyManagementNotification,
    )
    from telegram_kol_research.db import create_session_factory

    sf = create_session_factory(tmp_path / "management-notify.db")
    with sf() as session:
        raw = RawMessage(chat_id=-10088, message_id=901, text="move stop")
        session.add(raw); session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw.id, input_kind="text", authoritative_model="mimo",
            authoritative_status="是策略", authoritative_payload_json="{}",
            agreement_status="authoritative_only", differences_json="[]",
        )
        session.add(decision); session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:-10088:811:BTC:short", kol_id="kol",
            chat_id=-10088, message_id=811, symbol="BTC", side="short", status="open",
        )
        session.add(binding); session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=-10088, message_id=811, symbol="BTC", side="short",
            lifecycle_status="entered", signal_at=operator_bot_module.datetime.now(operator_bot_module.UTC),
            execution_binding_id=binding.id,
        )
        session.add(lifecycle); session.flush()
        entry = ExecutionOrderLeg(
            execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
            leg_index=0, purpose="entry", order_kind="conditional", pos_id="pos-1",
            attribution_status="verified", status="active",
        )
        session.add(entry); session.flush()
        batch = StrategyManagementBatch(
            idempotency_fingerprint="a" * 64, raw_message_id=raw.id,
            recognition_decision_id=decision.id, recognition_generation="g1",
            target_lifecycle_id=lifecycle.id, strategy_instance_id=binding.strategy_instance_id,
            execution_binding_id=binding.id, intent="adjust_stop_loss",
            effective_action="replace_stop_loss", partial_round_before=0,
            status="recovery_required", reason_code="restore_failed",
            target_fingerprint="b" * 64, target_snapshot_json='{"mode":"shadow"}',
            planned_at=operator_bot_module.datetime.now(operator_bot_module.UTC),
        )
        session.add(batch); session.flush()
        session.add(StrategyManagementLeg(
            management_batch_id=batch.id, execution_order_leg_id=entry.id,
            pos_id="pos-1", leg_index=0, status="recovery_required",
            planned_close_size="0.01", last_error=json.dumps({
                "stage": "replace_protection", "reason_code": "restore_failed",
                "type": "DeepcoinError", "message": "https://private.invalid/raw-body-content",
                "token": "top-secret-token", "cookie": "session-cookie",
                "headers": {"Authorization": "Bearer-never"},
            }),
        ))
        session.commit(); batch_id = batch.id

    sent = []
    async def fail_once(**kwargs):
        if not sent:
            sent.append("failed")
            raise RuntimeError("telegram down")
        sent.append(kwargs["text"])
    monkeypatch.setattr(operator_bot_module, "send_system_operator_bot_message", fail_once)
    config = operator_bot_module.SystemOperatorBotConfig("token", "chat")
    assert operator_bot_module.asyncio.run(
        operator_bot_module.deliver_strategy_management_notifications(sf, config=config)
    ) == 0
    assert operator_bot_module.asyncio.run(
        operator_bot_module.deliver_strategy_management_notifications(sf, config=config)
    ) == 1
    assert operator_bot_module.asyncio.run(
        operator_bot_module.deliver_strategy_management_notifications(sf, config=config)
    ) == 0

    with sf() as session:
        rows = session.query(StrategyManagementNotification).all()
        assert len(rows) == 1
        assert rows[0].status == "delivered"
        assert rows[0].claimed_at is not None
        assert rows[0].lease_expires_at is None
        payload_text = rows[0].payload_json.lower()
        for forbidden in (
            "private.invalid", "top-secret-token", "session-cookie",
            "bearer-never", "raw-body-content", '"message"', '"headers"',
        ):
            assert forbidden not in payload_text
        batch = session.get(StrategyManagementBatch, batch_id)
        batch.reason_code = "restore_failed_after_cancel"
        session.commit()

    barrier = __import__("threading").Barrier(2)
    winners = []
    def enqueue_and_claim():
        barrier.wait()
        claim = operator_bot_module.claim_next_strategy_management_notification(sf)
        winners.append(claim is not None)
    operator_bot_module.enqueue_strategy_management_notifications(sf)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: enqueue_and_claim(), range(2)))
    assert sorted(winners) == [False, True]

    with sf() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        batch.status = "partial_failed"
        session.commit()
    operator_bot_module.enqueue_strategy_management_notifications(sf)
    with sf() as session:
        assert session.query(StrategyManagementNotification).count() == 3


def test_management_notification_claim_lease_recovers_expired_delivery(tmp_path):
    from datetime import timedelta
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import StrategyManagementNotification

    sf = create_session_factory(tmp_path / "lease.db")
    with sf() as session:
        session.add(StrategyManagementNotification(
            management_batch_id=1, state="blocked", payload_fingerprint="a" * 64,
            payload_json='{"batch_id":1,"state":"blocked"}', status="pending",
        ))
        session.commit()
    first = operator_bot_module.claim_next_strategy_management_notification(
        sf, claimed_at=NOW, lease_seconds=30
    )
    assert first is not None
    assert operator_bot_module.claim_next_strategy_management_notification(
        sf, claimed_at=NOW + timedelta(seconds=29), lease_seconds=30
    ) is None
    reclaimed = operator_bot_module.claim_next_strategy_management_notification(
        sf, claimed_at=NOW + timedelta(seconds=31), lease_seconds=30
    )
    assert reclaimed is not None
    assert reclaimed["claim_token"] != first["claim_token"]


def test_cancelled_management_delivery_is_reclaimable_only_after_lease(
    tmp_path, monkeypatch
):
    from datetime import timedelta
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import StrategyManagementNotification

    sf = create_session_factory(tmp_path / "cancelled-lease.db")
    with sf() as session:
        session.add(StrategyManagementNotification(
            management_batch_id=1, state="submit_unknown", payload_fingerprint="b" * 64,
            payload_json='{"batch_id":1,"state":"submit_unknown"}', status="pending",
        ))
        session.commit()
    async def cancelled(**_kwargs):
        raise asyncio.CancelledError
    monkeypatch.setattr(operator_bot_module, "send_system_operator_bot_message", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(operator_bot_module.deliver_strategy_management_notifications(
            sf, config=SystemOperatorBotConfig("token", "chat"),
            claimed_at=NOW, lease_seconds=30,
        ))
    assert operator_bot_module.claim_next_strategy_management_notification(
        sf, claimed_at=NOW + timedelta(seconds=29), lease_seconds=30
    ) is None
    assert operator_bot_module.claim_next_strategy_management_notification(
        sf, claimed_at=NOW + timedelta(seconds=31), lease_seconds=30
    ) is not None


def test_management_submit_unknown_outbox_survives_disabled_bot_and_later_success(
    tmp_path, monkeypatch
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import RawMessage, StrategyManagementNotification
    from telegram_kol_research.strategy_management_batches import (
        ManagementLegCreate, create_management_batch, transition_batch,
    )

    sf = create_session_factory(tmp_path / "transient-management-alert.db")
    with sf() as session:
        raw = RawMessage(chat_id=-909, message_id=51, text="close")
        session.add(raw); session.commit(); raw_id = raw.id
    batch = create_management_batch(
        sf, idempotency_fingerprint="9" * 64, raw_message_id=raw_id,
        recognition_decision_id=91, recognition_generation="g1",
        target_lifecycle_id=92, strategy_instance_id="deepcoin:-909:41:BTC:short",
        execution_binding_id=93, intent="full_take_profit", effective_action="full_exit",
        requested_fraction=None, effective_fraction=1.0, partial_round_before=0,
        target_fingerprint="8" * 64, target_snapshot={"positions": []},
        legs=[ManagementLegCreate(
            execution_order_leg_id=94, pos_id="pos-transient", leg_index=0,
            status="submit_unknown", planned_close_size="0.02",
            last_error={"reason": "submission_outcome_unknown"},
        )], status="submit_unknown", reason_code="submission_outcome_unknown",
    )
    # No notifier ran while disabled; the alert event is already durable.
    assert transition_batch(
        sf, batch.id, expected_statuses={"submit_unknown"}, new_status="succeeded"
    )
    with sf() as session:
        event = session.query(StrategyManagementNotification).one()
        assert event.state == "submit_unknown"
        assert event.status == "pending"

    sent = []
    async def fake_send(**kwargs):
        sent.append(kwargs["text"])
    monkeypatch.setattr(operator_bot_module, "send_system_operator_bot_message", fake_send)
    delivered = asyncio.run(operator_bot_module.deliver_strategy_management_notifications(
        sf, config=operator_bot_module.SystemOperatorBotConfig("token", "chat"),
        group_labels={-909: "峰哥群"},
    ))
    assert delivered == 1
    assert len(sent) == 1
    assert "submit_unknown" in sent[0]
    assert "峰哥群" in sent[0]
    with sf() as session:
        assert session.query(StrategyManagementNotification).count() == 1


@pytest.mark.parametrize(
    "state", ["blocked", "partial_failed", "submit_unknown", "recovery_required"]
)
def test_every_management_alert_state_is_persisted_on_create_and_transition(
    tmp_path, state
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import RawMessage, StrategyManagementNotification
    from telegram_kol_research.strategy_management_batches import (
        ManagementLegCreate, create_management_batch, transition_batch,
    )

    sf = create_session_factory(tmp_path / f"outbox-{state}.db")
    raw_ids = []
    with sf() as session:
        for number in (1, 2):
            raw = RawMessage(chat_id=-700, message_id=number, text=state)
            session.add(raw); session.flush(); raw_ids.append(raw.id)
        session.commit()

    def make(number, status):
        return create_management_batch(
            sf, idempotency_fingerprint=f"{number}" * 64,
            raw_message_id=raw_ids[number - 1], recognition_decision_id=number,
            recognition_generation="g", target_lifecycle_id=number,
            strategy_instance_id=f"deepcoin:-700:{number}:BTC:short",
            execution_binding_id=number, intent="full_take_profit",
            effective_action="full_exit", requested_fraction=None,
            effective_fraction=1.0, partial_round_before=0,
            target_fingerprint=("a" if number == 1 else "b") * 64,
            target_snapshot={"positions": []},
            legs=[ManagementLegCreate(
                execution_order_leg_id=number, pos_id=f"pos-{number}",
                leg_index=0, status=state if status == state else "planned",
            )], status=status, reason_code=f"reason_{state}",
        )

    make(1, state)
    ready = make(2, "ready")
    assert transition_batch(
        sf, ready.id, expected_statuses={"ready"}, new_status=state,
        reason_code=f"reason_{state}",
    )
    with sf() as session:
        events = session.query(StrategyManagementNotification).all()
        assert len(events) == 2
        assert {event.state for event in events} == {state}
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    StrategyLifecycle,
    TradeSignal,
)
from telegram_kol_research.telegram_bot_commands import (
    _bot_http_timeout,
    _format_callback_resolution_text,
    process_system_operator_callback_data,
    process_system_operator_command,
)
from datetime import UTC, datetime


def test_load_system_operator_bot_config_uses_dedicated_env_vars():
    config = load_system_operator_bot_config(
        {
            "TELEGRAM_KOL_SYSTEM_BOT_TOKEN": "system-token",
            "TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID": "987654",
            "TELEGRAM_KOL_SYSTEM_BOT_TIMEOUT_SECONDS": "12",
        },
        env_file_paths=[],
    )

    assert config.bot_token == "system-token"
    assert config.chat_id == "987654"
    assert config.timeout_seconds == 12
    assert system_operator_bot_enabled(config)


def test_load_notification_bot_config_uses_separate_env_vars():
    config = load_notification_bot_config(
        {
            "TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN": "notification-token",
            "TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID": "987654",
            "TELEGRAM_KOL_NOTIFICATION_BOT_TIMEOUT_SECONDS": "12",
            "TELEGRAM_KOL_SYSTEM_BOT_TOKEN": "decision-token",
        },
        env_file_paths=[],
    )

    assert config.bot_token == "notification-token"
    assert config.chat_id == "987654"
    assert config.timeout_seconds == 12


def test_load_system_operator_bot_config_explicit_empty_paths_reads_no_checkout_env(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TELEGRAM_KOL_SYSTEM_BOT_TOKEN=checkout-secret\n"
        "TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID=123\n"
        "DEEPCOIN_API_SECRET=must-not-be-read\n",
        encoding="utf-8",
    )

    config = load_system_operator_bot_config(environ={}, env_file_paths=[])

    assert config.bot_token == ""
    assert config.chat_id == ""


def test_format_pending_entry_expiry_review_message_includes_operator_choices():
    message = format_pending_entry_expiry_review_message(
        {
            "lifecycle_id": 442,
            "chat_id": -1001,
            "message_id": 442,
            "symbol": "BTC",
            "side": "short",
            "max_age_hours": 6,
            "entry_range_low": 62900,
            "entry_range_high": 63200,
            "stop_loss": 64200,
            "take_profit": "61000",
            "pending_order_ids": ["order-pending"],
        }
    )

    assert "待入场策略超时复核" in message
    assert "#442" in message
    assert "BTC short" in message
    assert "62900-63200" in message
    assert "order-pending" in message
    assert "/expiry_continue" not in message


def test_system_operator_bot_disabled_without_dedicated_destination():
    assert not system_operator_bot_enabled(
        SystemOperatorBotConfig(bot_token="", chat_id="", timeout_seconds=10)
    )


def test_format_position_attribution_incident_message_is_read_only_and_actionable():
    message = format_position_attribution_incident_message(
        {
            "venue": "deepcoin",
            "pos_id": "pos-conflict",
            "state": "attribution_conflict",
            "candidate_leg_ids": [12, 18],
            "evidence_source_errors": {"trade_fills": "HTTP 502"},
        }
    )

    assert "仓位归属异常" in message
    assert "pos-conflict" in message
    assert "归属冲突" in message
    assert "12, 18" in message
    assert "trade_fills: HTTP 502" in message
    assert "自动管理已冻结" in message
    assert "不会自动平仓" in message


def test_position_attribution_incident_delivery_is_deduplicated_and_durable(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            PositionAttributionAudit(
                venue="deepcoin",
                pos_id="pos-conflict",
                event_type="attribution_conflict",
                new_state="attribution_conflict",
                fingerprint="fingerprint-1",
                evidence_json='{"candidate_leg_ids":[12,18]}',
                notification_status="pending",
            )
        )
        session.commit()

    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs["text"])

    monkeypatch.setattr(operator_bot_module, "send_system_operator_bot_message", fake_send)
    config = SystemOperatorBotConfig(bot_token="token", chat_id="chat")

    assert asyncio.run(
        deliver_pending_position_attribution_incidents(
            session_factory,
            config=config,
            delivered_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )
    ) == 1
    assert asyncio.run(
        deliver_pending_position_attribution_incidents(
            session_factory,
            config=config,
            delivered_at=datetime(2026, 7, 14, 12, 1, tzinfo=UTC),
        )
    ) == 0
    assert len(sent) == 1

    with session_factory() as session:
        first = session.query(PositionAttributionAudit).one()
        assert first.notification_status == "delivered"
        session.add(
            PositionAttributionAudit(
                venue="deepcoin",
                pos_id="pos-conflict",
                event_type="attribution_conflict",
                new_state="attribution_conflict",
                fingerprint="fingerprint-2",
                evidence_json='{"candidate_leg_ids":[12,18,21]}',
                notification_status="pending",
            )
        )
        session.commit()

    assert asyncio.run(
        deliver_pending_position_attribution_incidents(
            session_factory,
            config=config,
            delivered_at=datetime(2026, 7, 14, 12, 2, tzinfo=UTC),
        )
    ) == 1
    assert len(sent) == 2


def test_cleanup_notification_delivery_is_durable_and_deduplicated(
    tmp_path, monkeypatch
):
    from telegram_kol_research import execution_events as execution_events_module
    from telegram_kol_research.models import ExecutionEvent

    session_factory = create_session_factory(tmp_path / "cleanup-notification.db")
    event_id = execution_events_module.enqueue_terminal_entry_cleanup_notification(
        session_factory,
        lifecycle_id=625,
        binding_id=208,
        status="resolved",
        leg_ids=(901,),
        order_ids=("1001124388622177",),
        reason="primary_position_closed",
        created_at=NOW,
    )
    duplicate_id = (
        execution_events_module.enqueue_terminal_entry_cleanup_notification(
            session_factory,
            lifecycle_id=625,
            binding_id=208,
            status="resolved",
            leg_ids=(901,),
            order_ids=("1001124388622177",),
            reason="primary_position_closed",
            created_at=NOW + timedelta(seconds=1),
        )
    )
    assert duplicate_id == event_id

    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs["text"])
        return 7788

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", fake_send
    )
    config = SystemOperatorBotConfig(bot_token="token", chat_id="chat")
    delivered = asyncio.run(
        operator_bot_module.deliver_terminal_entry_cleanup_notifications(
            session_factory,
            config=config,
            delivered_at=NOW + timedelta(minutes=1),
        )
    )
    assert delivered == 1
    assert len(sent) == 1
    assert "挂单清理" in sent[0]
    assert "1001124388622177" in sent[0]

    with session_factory() as session:
        row = session.get(ExecutionEvent, event_id)
        assert row.notification_status == "delivered"
        assert row.notification_message_id == "7788"
        assert row.notification_attempts == 1
        assert row.notified_at == (NOW + timedelta(minutes=1)).replace(tzinfo=None)

    assert asyncio.run(
        operator_bot_module.deliver_terminal_entry_cleanup_notifications(
            session_factory,
            config=config,
            delivered_at=NOW + timedelta(minutes=2),
        )
    ) == 0


def test_cleanup_notification_fingerprint_is_stable_across_payload_versions(
    tmp_path,
):
    from telegram_kol_research import execution_events as execution_events_module
    from telegram_kol_research.models import ExecutionEvent

    session_factory = create_session_factory(tmp_path / "cleanup-version-dedup.db")
    identity_payload = {
        "binding_id": 208,
        "lifecycle_id": 625,
        "leg_ids": [901],
        "order_ids": ["1001124388622177"],
        "reason": "primary_position_closed",
        "status": "resolved",
    }
    old_fingerprint = hashlib.sha256(
        json.dumps(
            identity_payload,
            ensure_ascii=True,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    with session_factory() as session:
        old = ExecutionEvent(
            execution_binding_id=208,
            venue="deepcoin",
            action="terminal_entry_cleanup_outcome",
            status="resolved",
            reason="primary_position_closed",
            response_json=json.dumps(identity_payload, sort_keys=True),
            notification_status="pending",
            notification_fingerprint=old_fingerprint,
            notification_attempts=0,
            created_at=NOW,
        )
        session.add(old)
        session.commit()
        old_id = old.id

    event_id = execution_events_module.enqueue_terminal_entry_cleanup_notification(
        session_factory,
        lifecycle_id=625,
        binding_id=208,
        status="resolved",
        leg_ids=(901,),
        order_ids=("1001124388622177",),
        reason="primary_position_closed",
        created_at=NOW + timedelta(seconds=1),
    )

    assert event_id == old_id
    with session_factory() as session:
        reused = session.get(ExecutionEvent, old_id)
        assert (
            json.loads(reused.response_json)["notification_policy_version"]
            == "terminal-entry-cleanup-v2"
        )
        assert (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action == "terminal_entry_cleanup_outcome"
            )
            .count()
            == 1
        )


def test_cleanup_notification_failure_is_bounded_and_retries_after_restart(
    tmp_path, monkeypatch
):
    from telegram_kol_research import execution_events as execution_events_module
    from telegram_kol_research.models import ExecutionEvent

    database_path = tmp_path / "cleanup-notification-retry.db"
    session_factory = create_session_factory(database_path)
    event_id = execution_events_module.enqueue_terminal_entry_cleanup_notification(
        session_factory,
        lifecycle_id=626,
        binding_id=209,
        status="blocked",
        leg_ids=(902,),
        order_ids=("order-2",),
        reason="primary_position_closed",
        created_at=NOW,
    )

    async def fail_send(**_kwargs):
        raise RuntimeError("secret=" + "x" * 1000)

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", fail_send
    )
    config = SystemOperatorBotConfig(bot_token="token", chat_id="chat")
    assert asyncio.run(
        operator_bot_module.deliver_terminal_entry_cleanup_notifications(
            session_factory,
            config=config,
            delivered_at=NOW,
        )
    ) == 0

    with session_factory() as session:
        row = session.get(ExecutionEvent, event_id)
        assert row.notification_status == "failed"
        assert row.notification_error == "RuntimeError"
        assert row.notification_attempts == 1
        assert row.notification_next_attempt_at is not None

    restarted_factory = create_session_factory(database_path)

    async def succeed_send(**_kwargs):
        return 8899

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", succeed_send
    )
    assert asyncio.run(
        operator_bot_module.deliver_terminal_entry_cleanup_notifications(
            restarted_factory,
            config=config,
            delivered_at=NOW + timedelta(minutes=10),
        )
    ) == 1

    with restarted_factory() as session:
        row = session.get(ExecutionEvent, event_id)
        assert row.notification_status == "delivered"
        assert row.notification_attempts == 2
        assert row.notification_message_id == "8899"


def test_cleanup_notification_suppresses_legacy_non_cancellable_backstop_noise(
    tmp_path, monkeypatch
):
    from telegram_kol_research import execution_events as execution_events_module
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionEvent,
        ExecutionOrderLeg,
    )

    session_factory = create_session_factory(tmp_path / "cleanup-suppress.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:legacy",
            chat_id=1,
            message_id=2,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="unknown",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            leg_index=1,
            purpose="entry",
            order_kind="unknown",
            order_id="legacy-order",
            status="unknown",
        )
        session.add(leg)
        session.commit()
        binding_id = binding.id
        leg_id = leg.id
    event_id = execution_events_module.enqueue_terminal_entry_cleanup_notification(
        session_factory,
        lifecycle_id=1,
        binding_id=binding_id,
        status="unknown",
        leg_ids=(leg_id,),
        order_ids=("legacy-order",),
        reason="terminal_lifecycle_entry_exposure",
        created_at=NOW,
    )
    with session_factory() as session:
        event = session.get(ExecutionEvent, event_id)
        payload = json.loads(event.response_json)
        payload.pop("notification_policy_version")
        event.response_json = json.dumps(payload, sort_keys=True)
        session.commit()
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", fake_send
    )
    assert asyncio.run(
        operator_bot_module.deliver_terminal_entry_cleanup_notifications(
            session_factory,
            config=SystemOperatorBotConfig(bot_token="token", chat_id="chat"),
            delivered_at=NOW,
        )
    ) == 0
    assert sent == []
    with session_factory() as session:
        event = session.get(ExecutionEvent, event_id)
        assert event.notification_status == "not_needed"
        assert event.notification_error == "non_cancellable_entry_state"


def test_fresh_cleanup_unknown_is_delivered_if_leg_later_becomes_filled(
    tmp_path, monkeypatch
):
    from telegram_kol_research import execution_events as execution_events_module
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionEvent,
        ExecutionOrderLeg,
    )

    session_factory = create_session_factory(tmp_path / "cleanup-fresh-filled.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:fresh",
            chat_id=3,
            message_id=4,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="unknown",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            leg_index=1,
            purpose="entry",
            order_kind="limit",
            order_id="fresh-order",
            status="pending",
        )
        session.add(leg)
        session.commit()
        binding_id = binding.id
        leg_id = leg.id
    event_id = execution_events_module.enqueue_terminal_entry_cleanup_notification(
        session_factory,
        lifecycle_id=2,
        binding_id=binding_id,
        status="unknown",
        leg_ids=(leg_id,),
        order_ids=("fresh-order",),
        reason="terminal_lifecycle_entry_exposure",
        created_at=NOW,
    )
    with session_factory() as session:
        session.get(ExecutionOrderLeg, leg_id).status = "filled"
        session.commit()
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs["text"])
        return 999

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", fake_send
    )
    assert asyncio.run(
        operator_bot_module.deliver_terminal_entry_cleanup_notifications(
            session_factory,
            config=SystemOperatorBotConfig(bot_token="token", chat_id="chat"),
            delivered_at=NOW,
        )
    ) == 1
    assert len(sent) == 1
    with session_factory() as session:
        assert (
            session.get(ExecutionEvent, event_id).notification_status
            == "delivered"
        )


def test_format_ai_recognition_conflict_review_message_includes_both_model_results():
    message = format_ai_recognition_conflict_review_message(
        {
            "chat_title": "比特币飞扬 11分组",
            "chat_id": -1002960443256,
            "message_id": 3885,
            "posted_at": datetime(2026, 7, 8, 15, 44, 58, tzinfo=UTC),
            "text": "今日两次BTC策略都没有入场，取消吧",
            "deepseek": {
                "status": "非策略",
                "kind": "non_strategy",
                "reason": "DeepSeek 未识别为生命周期事件",
            },
            "mimo": {
                "status": "取消入场",
                "kind": "strategy_related",
                "reason": "MiMo 认为是取消未入场挂单",
            },
        }
    )

    assert "AI识别分歧告警" in message
    assert "比特币飞扬 11分组" in message
    assert "#3885" in message
    assert "DeepSeek: 非策略 / non_strategy" in message
    assert "MiMo: 取消入场 / strategy_related" in message
    assert "权威结果: MiMo" in message
    assert "已按 MiMo 结果继续" in message
    assert "已暂停" not in message
    assert "今日两次BTC策略都没有入场" in message


def test_format_semantic_disagreement_notification_is_critical_and_evidence_backed():
    message = format_semantic_disagreement_notification(
        {
            "chat_title": "峰哥高级会员群-11分组",
            "chat_id": -1001,
            "message_id": 8401,
            "posted_at": datetime(2026, 7, 13, 8, 1, tzinfo=UTC),
            "text": "现价62800附近出局，空仓等待。",
            "mimo": {
                "status": "exit_full",
                "reason": "原文要求全部出局",
            },
            "deepseek": {
                "status": "exit_partial",
                "reason": "独立复核认为只是部分止盈",
                "evidence": ["现价62800附近出局", "空仓等待"],
            },
            "automation": {
                "status": "submitted",
                "reason": "close_position",
            },
            "conflict_types": ["full_vs_partial_exit"],
        }
    )

    assert "【AI语义严重分歧】" in message
    assert "原始来源: 峰哥高级会员群-11分组 / -1001 / #8401" in message
    assert "权威结果: MiMo / exit_full / 原文要求全部出局" in message
    assert "自动化结果: submitted / close_position" in message
    assert "复核结果: DeepSeek / exit_partial / 独立复核认为只是部分止盈" in message
    assert "冲突类型: full_vs_partial_exit" in message
    assert "依据: 现价62800附近出局；空仓等待" in message
    assert "已按MiMo结果继续，未等待人工复核" in message
    assert "消息已处理，不需要审批" in message


def test_format_semantic_disagreement_notification_truncates_source_and_evidence():
    source = "原文" + "甲" * 2_000 + "SOURCE_END"
    evidence = "证据" + "乙" * 2_000 + "EVIDENCE_END"

    message = format_semantic_disagreement_notification(
        {
            "text": source,
            "mimo": {"status": "exit_full"},
            "deepseek": {"status": "exit_partial", "evidence": [evidence]},
            "automation": {"status": "submitted", "reason": "close_position"},
            "conflict_types": ["full_vs_partial_exit"],
        }
    )

    assert "SOURCE_END" not in message
    assert "EVIDENCE_END" not in message
    assert message.count("...") >= 2
    assert len(message) < 3_000


def test_send_semantic_disagreement_notification_is_read_only(monkeypatch):
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(operator_bot_module, "send_system_operator_bot_message", fake_send)
    config = SystemOperatorBotConfig(bot_token="token", chat_id="chat")

    asyncio.run(
        send_semantic_disagreement_notification(
            config=config,
            payload={
                "text": "全部出局",
                "mimo": {"status": "exit_full"},
                "deepseek": {"status": "none", "evidence": ["全部出局"]},
                "automation": {"status": "submitted", "reason": "close_position"},
                "conflict_types": ["urgent_exit_missed"],
            },
        )
    )

    assert len(sent) == 1
    assert sent[0]["config"] is config
    assert sent[0].get("reply_markup") is None
    assert "inline_keyboard" not in sent[0]


def test_bot_http_timeout_allows_long_polling_read_to_finish():
    timeout = _bot_http_timeout(10)

    assert timeout.read >= 35
    assert timeout.connect == 10


def test_callback_resolution_text_keeps_strategy_context_after_button_click():
    original_message = "\n".join(
        [
            "\u3010\u5f85\u5165\u573a\u7b56\u7565\u8d85\u65f6\u590d\u6838\u3011",
            "\u7fa4\u7ec4: \u7c73\u5a05 VIP 11\u5206\u7ec4",
            "\u7fa4ID: -1002370796392",
            "\u7b56\u7565\u4ee3\u7801: #3251",
            "\u5185\u90e8ID: 354",
            "\u4ea4\u6613\u5bf9: BTC short",
            "\u539f\u7b56\u7565\u65f6\u95f4: 2026-07-05 09:32:29 Asia/Shanghai",
            "\u8d85\u65f6\u65f6\u95f4: 2026-07-05 15:32:29 Asia/Shanghai",
            "\u5165\u573a\u533a\u95f4: 62900-63200",
            "\u6b62\u635f: 64200",
            "\u6b62\u76c8: 61000",
        ]
    )

    message = _format_callback_resolution_text(
        callback_data="expiry_continue:354",
        response_text="\u7b56\u7565 #354 \u5df2\u7ee7\u7eed\u7b49\u5f85\u3002",
        operator_name="weichang tan",
        original_message_text=original_message,
    )

    assert "\u2705 \u5df2\u5904\u7406\uff1a\u7ee7\u7eed\u7b49\u5f85" in message
    assert "\u64cd\u4f5c\u4eba: weichang tan" in message
    assert "\u7fa4\u7ec4: \u7c73\u5a05 VIP 11\u5206\u7ec4" in message
    assert "\u539f\u7b56\u7565\u65f6\u95f4: 2026-07-05 09:32:29 Asia/Shanghai" in message
    assert "\u4ea4\u6613\u5bf9: BTC short" in message
    assert "\u5165\u573a\u533a\u95f4: 62900-63200" in message
    assert "\u6b62\u635f: 64200" in message
    assert "\u6b62\u76c8: 61000" in message


def test_format_pending_entry_expiry_review_message_shows_strategy_code_and_internal_id():
    message = format_pending_entry_expiry_review_message(
        {
            "lifecycle_id": 354,
            "chat_id": -1002370796392,
            "chat_title": "\u7c73\u5a05 VIP 11\u5206\u7ec4",
            "message_id": 3251,
            "symbol": "ETH",
            "side": "short",
            "max_age_hours": 6,
            "signal_at": datetime(2026, 7, 4, 15, 54, 12, tzinfo=UTC),
            "expiry_at": datetime(2026, 7, 4, 21, 54, 12, tzinfo=UTC),
            "entry_range_low": 1830,
            "entry_range_high": 1850,
            "stop_loss": 1860,
            "take_profit": "1785/1735/1670",
        }
    )

    assert "\u7b56\u7565\u4ee3\u7801: #3251" in message
    assert "\u5185\u90e8ID: 354" in message
    assert "\u7fa4\u7ec4: \u7c73\u5a05 VIP 11\u5206\u7ec4" in message
    assert "\u7fa4ID: -1002370796392" in message
    assert "\u539f\u7b56\u7565\u65f6\u95f4: 2026-07-04 23:54:12 Asia/Shanghai" in message
    assert "\u8d85\u65f6\u65f6\u95f4: 2026-07-05 05:54:12 Asia/Shanghai" in message


def test_format_pending_entry_expiry_review_message_shows_repeated_review_context():
    message = format_pending_entry_expiry_review_message(
        {
            "lifecycle_id": 354,
            "chat_id": -1002370796392,
            "chat_title": "\u7c73\u5a05 VIP 11\u5206\u7ec4",
            "message_id": 3251,
            "symbol": "BTC",
            "side": "short",
            "max_age_hours": 6,
            "signal_at": datetime(2026, 7, 4, 15, 54, 12, tzinfo=UTC),
            "expiry_at": datetime(2026, 7, 5, 3, 54, 12, tzinfo=UTC),
            "previous_review_at": datetime(2026, 7, 4, 21, 54, 12, tzinfo=UTC),
            "review_reason": "\u4e0a\u6b21\u4eba\u5de5\u9009\u62e9\u7ee7\u7eed\u7b49\u5f85\u540e\u53c8\u8d85\u8fc7 6 \u5c0f\u65f6",
            "entry_range_low": 62900,
            "entry_range_high": 63200,
            "stop_loss": 64200,
            "take_profit": "61000",
        }
    )

    assert "\u4e0a\u6b21\u4eba\u5de5\u7ee7\u7eed\u7b49\u5f85: 2026-07-05 05:54:12 Asia/Shanghai" in message
    assert "\u539f\u56e0: \u4e0a\u6b21\u4eba\u5de5\u9009\u62e9\u7ee7\u7eed\u7b49\u5f85\u540e\u53c8\u8d85\u8fc7 6 \u5c0f\u65f6" in message


def test_build_pending_entry_expiry_review_reply_markup_uses_lifecycle_id_callbacks():
    markup = build_pending_entry_expiry_review_reply_markup({"lifecycle_id": 354})

    assert markup == {
        "inline_keyboard": [
            [{"text": "\u7ee7\u7eed\u7b49\u5f85", "callback_data": "expiry_continue:354"}],
            [
                {
                    "text": "\u8fc7\u671f\u5e76\u64a4\u5355",
                    "callback_data": "expiry_expire_cancel:354",
                },
                {
                    "text": "\u66f4\u65b0\u72b6\u6001",
                    "callback_data": "expiry_refresh:354",
                },
            ],
        ]
    }


@pytest.mark.parametrize(
    ("callback_data", "expected"),
    [
        ("expiry_refresh:354", True),
        ("expiry_expire_cancel:354", True),
        ("expiry_continue:354", False),
        ("expiry_expire_keep:354", False),
    ],
)
def test_expiry_callback_deepcoin_client_requirement(callback_data, expected):
    assert (
        bot_commands_module._expiry_callback_needs_deepcoin_client(callback_data)
        is expected
    )


def test_refresh_expiry_review_status_reports_active_and_pending_legs(
    tmp_path, monkeypatch
):
    refreshed_at = datetime(2026, 8, 7, 6, 30, tzinfo=UTC)
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="miya",
            chat_id=88,
            message_id=3251,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            status="active",
            order_id="entry-filled,entry-pending",
            pos_id="pos-filled",
            strategy_instance_id="deepcoin:88:3251:ETH:short",
        )
        session.add(binding)
        session.flush()
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id="entry-filled",
                    pos_id="pos-filled",
                    status="active",
                    attribution_status="verified",
                ),
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=2,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id="entry-pending",
                    status="pending",
                    attribution_status="unassigned",
                ),
            ]
        )
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3251,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
            entered_at=datetime(2026, 8, 7, 1, 0, tzinfo=UTC),
            execution_binding_id=binding.id,
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    calls = []

    def fake_reconcile(factory, *, client, recovered_at):
        calls.append((factory, client, recovered_at))

    monkeypatch.setattr(
        bot_commands_module,
        "reconcile_deepcoin_execution_bindings_read_only",
        fake_reconcile,
        raising=False,
    )
    deepcoin_client = object()

    result = bot_commands_module.refresh_expiry_review_status(
        session_factory,
        str(lifecycle_id),
        deepcoin_client=deepcoin_client,
        now=refreshed_at,
    )

    assert calls == [(session_factory, deepcoin_client, refreshed_at)]
    assert result.keep_actions is True
    assert "策略状态：已入场" in result.status_text
    assert "入场进度：1/2 条腿已入场，1/2 条腿挂单中" in result.status_text
    assert "第1腿：已入场" in result.status_text
    assert "仓位 ID: pos-filled" in result.status_text
    assert "第2腿：挂单中" in result.status_text
    assert "订单 ID: entry-pending" in result.status_text
    assert "更新时间：2026-08-07 14:30:00 Asia/Shanghai" in result.status_text


def _create_expiry_refresh_lifecycle(session_factory, *, lifecycle_status, leg_specs):
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="miya",
            chat_id=99,
            message_id=4001,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="active",
            strategy_instance_id="deepcoin:99:4001:BTC:long",
        )
        session.add(binding)
        session.flush()
        for index, spec in enumerate(leg_specs, start=1):
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=index,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id=spec.get("order_id", f"order-{index}"),
                    pos_id=spec.get("pos_id"),
                    status=spec["status"],
                    attribution_status=spec.get("attribution_status", "unassigned"),
                )
            )
        lifecycle = StrategyLifecycle(
            chat_id=99,
            message_id=4001,
            symbol="BTC",
            side="long",
            lifecycle_status=lifecycle_status,
            signal_at=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
            execution_binding_id=binding.id,
            management_action="expiry_review_requested",
            exit_reason="cancelled" if lifecycle_status == "exited" else None,
        )
        session.add(lifecycle)
        session.commit()
        return lifecycle.id


def test_refresh_expiry_review_status_all_entered_removes_actions(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id = _create_expiry_refresh_lifecycle(
        session_factory,
        lifecycle_status="entered",
        leg_specs=[
            {"status": "active", "pos_id": "pos-1", "attribution_status": "verified"},
            {"status": "active", "pos_id": "pos-2", "attribution_status": "verified"},
        ],
    )
    monkeypatch.setattr(
        bot_commands_module,
        "reconcile_deepcoin_execution_bindings_read_only",
        lambda *a, **k: None,
    )

    result = bot_commands_module.refresh_expiry_review_status(
        session_factory,
        str(lifecycle_id),
        deepcoin_client=object(),
        now=datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
    )

    assert result.keep_actions is False
    assert "入场进度：2/2 条腿已入场" in result.status_text


def test_refresh_expiry_review_status_entered_and_cancelled_removes_actions(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id = _create_expiry_refresh_lifecycle(
        session_factory,
        lifecycle_status="entered",
        leg_specs=[
            {"status": "active", "pos_id": "pos-1", "attribution_status": "verified"},
            {"status": "cancelled"},
        ],
    )
    monkeypatch.setattr(
        bot_commands_module,
        "reconcile_deepcoin_execution_bindings_read_only",
        lambda *a, **k: None,
    )

    result = bot_commands_module.refresh_expiry_review_status(
        session_factory,
        str(lifecycle_id),
        deepcoin_client=object(),
        now=datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
    )

    assert result.keep_actions is False
    assert "第2腿：已取消" in result.status_text


def test_refresh_expiry_review_status_group_cancelled_lifecycle_removes_actions(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id = _create_expiry_refresh_lifecycle(
        session_factory,
        lifecycle_status="exited",
        leg_specs=[{"status": "pending"}],
    )
    monkeypatch.setattr(
        bot_commands_module,
        "reconcile_deepcoin_execution_bindings_read_only",
        lambda *a, **k: None,
    )

    result = bot_commands_module.refresh_expiry_review_status(
        session_factory,
        str(lifecycle_id),
        deepcoin_client=object(),
        now=datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
    )

    assert result.keep_actions is False
    assert "策略状态：已离场" in result.status_text


def test_refresh_expiry_review_status_partial_fill_keeps_actions(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id = _create_expiry_refresh_lifecycle(
        session_factory,
        lifecycle_status="entered",
        leg_specs=[
            {
                "status": "partially_filled",
                "pos_id": "pos-partial",
                "attribution_status": "verified",
            }
        ],
    )
    monkeypatch.setattr(
        bot_commands_module,
        "reconcile_deepcoin_execution_bindings_read_only",
        lambda *a, **k: None,
    )

    result = bot_commands_module.refresh_expiry_review_status(
        session_factory,
        str(lifecycle_id),
        deepcoin_client=object(),
        now=datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
    )

    assert result.keep_actions is True
    assert "入场进度：0/1 条腿已入场，1/1 条腿部分成交" in result.status_text
    assert "第1腿：部分成交" in result.status_text


def test_refresh_expiry_review_status_attribution_conflict_keeps_actions(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id = _create_expiry_refresh_lifecycle(
        session_factory,
        lifecycle_status="entered",
        leg_specs=[
            {
                "status": "active",
                "pos_id": "pos-conflict",
                "attribution_status": "attribution_conflict",
            }
        ],
    )
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        binding.last_exchange_status = "position_attribution_conflict"
        session.commit()
    monkeypatch.setattr(
        bot_commands_module,
        "reconcile_deepcoin_execution_bindings_read_only",
        lambda *a, **k: None,
    )

    result = bot_commands_module.refresh_expiry_review_status(
        session_factory,
        str(lifecycle_id),
        deepcoin_client=object(),
        now=datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
    )

    assert result.keep_actions is True
    assert "入场进度：0/1 条腿已入场，1/1 条腿状态待确认" in result.status_text
    assert "第1腿：归属待确认" in result.status_text


def test_refresh_expiry_review_status_missing_binding_keeps_actions_conservatively(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=4002,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id
    monkeypatch.setattr(
        bot_commands_module,
        "reconcile_deepcoin_execution_bindings_read_only",
        lambda *a, **k: None,
    )

    result = bot_commands_module.refresh_expiry_review_status(
        session_factory,
        str(lifecycle_id),
        deepcoin_client=object(),
        now=datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
    )

    assert result.keep_actions is True
    assert "入场进度：状态待确认" in result.status_text


def test_replace_expiry_refresh_status_keeps_only_latest_section():
    original = (
        "【待入场策略超时复核】\n内部ID: 789\n\n"
        "【最新策略状态】\n策略状态：待入场\n更新时间：旧"
    )

    refreshed = bot_commands_module._replace_expiry_refresh_status(
        original,
        "策略状态：已入场\n更新时间：新",
    )

    assert refreshed.count("【最新策略状态】") == 1
    assert "更新时间：旧" not in refreshed
    assert "更新时间：新" in refreshed


@pytest.mark.parametrize("keep_actions", [True, False])
def test_refresh_callback_response_preserves_or_removes_actions(keep_actions):
    class FakeResponse:
        def __init__(self):
            self.request = httpx.Request("POST", "https://example.test")

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self):
            self.posts = []

        async def post(self, url, json):
            self.posts.append((url, json))
            return FakeResponse()

    client = FakeClient()
    result = bot_commands_module.ExpiryReviewRefreshResult(
        answer_text="策略 #789 状态已更新。",
        status_text="策略状态：已入场",
        keep_actions=keep_actions,
    )

    asyncio.run(
        bot_commands_module._finish_expiry_refresh_callback_response(
            client,
            "https://api.telegram.org/bot-token",
            callback_query_id="callback-1",
            chat_id="123",
            message_id=456,
            lifecycle_id=789,
            result=result,
            original_message_text="【待入场策略超时复核】\n内部ID: 789",
        )
    )

    edit_payload = client.posts[-1][1]
    assert edit_payload["text"].count("【最新策略状态】") == 1
    if keep_actions:
        assert edit_payload["reply_markup"] == (
            build_pending_entry_expiry_review_reply_markup({"lifecycle_id": 789})
        )
    else:
        assert edit_payload["reply_markup"] == {"inline_keyboard": []}


def test_refresh_expiry_review_status_failure_keeps_actions(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=4002,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 8, 7, 0, 0, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    def fail_reconcile(*args, **kwargs):
        raise RuntimeError("Authorization: secret-value")

    monkeypatch.setattr(
        bot_commands_module, "reconcile_deepcoin_execution_bindings_read_only", fail_reconcile
    )

    result = bot_commands_module.refresh_expiry_review_status(
        session_factory,
        str(lifecycle_id),
        deepcoin_client=object(),
        now=datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
    )

    assert result.keep_actions is True
    assert "更新失败，未改变策略或挂单状态" in result.status_text
    assert "secret-value" not in result.status_text


def test_refresh_expiry_review_status_terminal_failure_removes_actions(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    lifecycle_id = _create_expiry_refresh_lifecycle(
        session_factory,
        lifecycle_status="exited",
        leg_specs=[{"status": "pending"}],
    )

    def fail_reconcile(*args, **kwargs):
        raise RuntimeError("Deepcoin unavailable")

    monkeypatch.setattr(
        bot_commands_module,
        "reconcile_deepcoin_execution_bindings_read_only",
        fail_reconcile,
    )

    result = bot_commands_module.refresh_expiry_review_status(
        session_factory,
        str(lifecycle_id),
        deepcoin_client=object(),
        now=datetime(2026, 8, 7, 6, 30, tzinfo=UTC),
    )

    assert result.keep_actions is False
    assert "更新失败，未改变策略或挂单状态" in result.status_text


def test_process_expiry_continue_keeps_pending_and_suppresses_repeat_review(tmp_path):
    continued_at = datetime(2026, 7, 3, 0, 0, tzinfo=UTC)
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_command(
        session_factory,
        f"/expiry_continue {lifecycle_id}",
        now=continued_at,
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "继续等待" in response
    assert lifecycle.lifecycle_status == "pending_entry"
    assert lifecycle.management_action == "expiry_review_continued"
    assert lifecycle.expiry_review_next_at == (
        continued_at + timedelta(hours=3)
    ).replace(tzinfo=None)


def test_process_expiry_continue_accepts_strategy_code_message_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3251,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_command(
        session_factory,
        "/expiry_continue #3251",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "\u7ee7\u7eed\u7b49\u5f85" in response
    assert lifecycle.management_action == "expiry_review_continued"


def test_process_system_operator_callback_data_dispatches_expiry_action(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3251,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_callback_data(
        session_factory,
        f"expiry_continue:{lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "\u7ee7\u7b49" in response or "\u7ee7\u7eed\u7b49\u5f85" in response
    assert lifecycle.management_action == "expiry_review_continued"


def test_process_expiry_continue_does_not_revert_already_entered_strategy(tmp_path):
    continued_at = datetime(2026, 7, 3, 0, 0, tzinfo=UTC)
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3251,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            entered_at=datetime(2026, 7, 2, 21, 0, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_callback_data(
        session_factory,
        f"expiry_continue:{lifecycle_id}",
        now=continued_at,
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "继续等待" in response
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entered_at is not None
    assert lifecycle.management_action == "expiry_review_continued"
    assert lifecycle.expiry_review_next_at == (
        continued_at + timedelta(hours=3)
    ).replace(tzinfo=None)


def test_process_entered_expiry_expire_cancel_cancels_only_pending_entry_leg(tmp_path):
    class FakeDeepcoinClient:
        def __init__(self):
            self.cancel_payloads = []
            self.trigger_history = []

        def list_trigger_orders_pending(self, inst_id):
            if self.cancel_payloads:
                return []
            return [
                {
                    "instId": inst_id,
                    "ordId": "entry-pending",
                    "triggerOrderType": "Conditional",
                    "side": "sell",
                    "posSide": "short",
                }
            ]

        def list_open_orders(self, inst_id):
            return []

        def cancel_trigger_order(self, cancel_payload):
            self.cancel_payloads.append(dict(cancel_payload))
            self.trigger_history.append(
                {
                    "ordId": cancel_payload.get("ordId"),
                    "state": "canceled",
                }
            )
            return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return list(self.trigger_history)

        def list_trade_fills(self, *, inst_id=None):
            return []

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="miya",
            chat_id=88,
            message_id=3251,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            status="active",
            order_id="entry-live,entry-pending",
            pos_id="pos-live",
            position_mode="split",
            strategy_instance_id="deepcoin:88:3251:ETH:short",
        )
        session.add(binding)
        session.flush()
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id="entry-live",
                    pos_id="pos-live",
                    venue="deepcoin",
                    status="active",
                    attribution_status="verified",
                ),
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=2,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id="entry-pending",
                    venue="deepcoin",
                    status="pending",
                    attribution_status="unassigned",
                ),
            ]
        )
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3251,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            entered_at=datetime(2026, 7, 2, 21, 0, tzinfo=UTC),
            execution_binding_id=binding.id,
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id

    fake_client = FakeDeepcoinClient()
    response = process_system_operator_callback_data(
        session_factory,
        f"expiry_expire_cancel:{lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
        deepcoin_client=fake_client,
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)
        trade_signal_count = session.query(TradeSignal).count()
        legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == binding_id)
            .order_by(ExecutionOrderLeg.leg_index)
            .all()
        )
        events = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.execution_binding_id == binding_id)
            .order_by(ExecutionEvent.id)
            .all()
        )

    assert "已撤销未触发入场挂单" in response
    assert fake_client.cancel_payloads == [
        {"instId": "ETH-USDT-SWAP", "ordId": "entry-pending"}
    ]
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entered_at is not None
    assert lifecycle.exited_at is None
    assert lifecycle.management_action == "expiry_pending_leg_cancelled"
    assert binding.status == "active"
    assert legs[0].status == "active"
    assert legs[1].status == "cancelled"
    assert trade_signal_count == 1
    assert [(event.action, event.status, event.order_id) for event in events] == [
        ("cancel_trigger_entry", "submitted", "entry-pending")
    ]


def test_process_entered_expiry_expire_keep_preserves_live_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3251,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            entered_at=datetime(2026, 7, 2, 21, 0, tzinfo=UTC),
            management_action="expiry_review_requested",
            expiry_review_next_at=datetime(2026, 7, 3, 3, 0, tzinfo=UTC),
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_callback_data(
        session_factory,
        f"expiry_expire_keep:{lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "持仓策略保持已入场" in response
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entered_at is not None
    assert lifecycle.exited_at is None
    assert lifecycle.management_action == "expiry_pending_leg_keep_order"
    assert lifecycle.expiry_review_next_at is None


def test_process_expiry_expire_cancel_does_not_expire_while_live_binding_needs_cancel(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="mia",
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="open",
            order_id="order-1",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            execution_binding_id=binding.id,
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_command(
        session_factory,
        f"/expiry_expire_cancel {lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "已请求撤销交易所挂单" in response
    assert lifecycle.lifecycle_status == "pending_entry"
    assert lifecycle.exit_reason is None
    assert lifecycle.management_action == "expiry_cancel_requested"


def test_process_expiry_expire_cancel_executes_deepcoin_cancel_when_client_is_available(tmp_path):
    class FakeDeepcoinClient:
        def __init__(self):
            self.cancel_payloads = []

        def list_trigger_orders_pending(self, inst_id):
            return []

        def list_open_orders(self, inst_id):
            return [{"instId": inst_id, "ordId": "order-1"}]

        def cancel_order(self, cancel_payload):
            self.cancel_payloads.append(cancel_payload)
            return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="mia",
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="open",
            order_id="order-1",
            position_mode="split",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            execution_binding_id=binding.id,
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id

    fake_client = FakeDeepcoinClient()
    response = process_system_operator_command(
        session_factory,
        f"/expiry_expire_cancel {lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
        deepcoin_client=fake_client,
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)

    assert "已撤销交易所挂单并标记过期" in response
    assert fake_client.cancel_payloads == [
        {"instId": "BTC-USDT-SWAP", "ordId": "order-1", "mrgPosition": "split"}
    ]
    assert binding.status == "cancelled"
    assert lifecycle.lifecycle_status == "expired"
    assert lifecycle.management_action == "expiry_cancelled_and_expired"


def test_process_expiry_expire_cancel_without_live_binding_marks_expired(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_command(
        session_factory,
        f"/expiry_expire_cancel {lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
        deepcoin_client=object(),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "未找到本地 live 挂单" in response
    assert "已标记过期" in response
    assert lifecycle.lifecycle_status == "expired"
    assert lifecycle.exit_reason == "expired"
    assert lifecycle.exited_at == datetime(2026, 7, 3, 0, 0)
    assert lifecycle.management_action == "expiry_expired_no_live_order"


def test_callback_response_edits_message_when_callback_answer_fails():
    class FakeResponse:
        def __init__(self, *, status_code=200):
            self.status_code = status_code
            self.request = httpx.Request("POST", "https://example.test")

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "bad request",
                    request=self.request,
                    response=httpx.Response(self.status_code, request=self.request),
                )

    class FakeClient:
        def __init__(self):
            self.posts = []

        async def post(self, url, json):
            self.posts.append((url, json))
            if url.endswith("/answerCallbackQuery"):
                return FakeResponse(status_code=400)
            return FakeResponse()

    client = FakeClient()

    asyncio.run(
        bot_commands_module._finish_system_operator_callback_response(
            client,
            "https://api.telegram.org/bot-token",
            callback_query_id="callback-1",
            chat_id="123",
            message_id=456,
            callback_data="expiry_expire_cancel:789",
            response_text="策略 #789 未找到本地 live 挂单，已标记过期并停止跟踪。",
            operator_name="operator",
            original_message_text="【待入场策略超时复核】\n内部ID: 789",
        )
    )

    assert [url.rsplit("/", 1)[-1] for url, _ in client.posts] == [
        "answerCallbackQuery",
        "editMessageText",
    ]
    assert "已处理：过期并撤单" in client.posts[1][1]["text"]
    assert "已标记过期并停止跟踪" in client.posts[1][1]["text"]


def test_process_expiry_expire_keep_marks_expired_without_cancelling_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            management_action="expiry_review_requested",
            expiry_review_next_at=datetime(2026, 7, 3, 3, 0, tzinfo=UTC),
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_command(
        session_factory,
        f"/expiry_expire_keep {lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "已标记过期" in response
    assert lifecycle.lifecycle_status == "expired"
    assert lifecycle.exit_reason == "expired"
    assert lifecycle.expiry_review_next_at is None


def test_telegram_evidence_probe_is_concurrent_bounded_and_proxy_free(
    monkeypatch,
):
    observed = {}

    class Response:
        status_code = 200

        def json(self):
            return {"ok": True, "result": {"secret": "not-returned"}}

    async def scenario():
        get_started = asyncio.Event()
        post_started = asyncio.Event()

        class Client:
            def __init__(self, *, timeout, trust_env):
                observed["timeout"] = timeout
                observed["trust_env"] = trust_env

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def get(self, url):
                observed["get_url"] = url
                get_started.set()
                await asyncio.wait_for(post_started.wait(), timeout=0.2)
                return Response()

            async def post(self, url, *, json):
                observed["post_url"] = url
                observed["payload"] = json
                post_started.set()
                await asyncio.wait_for(get_started.wait(), timeout=0.2)
                return Response()

        monkeypatch.setattr(operator_bot_module.httpx, "AsyncClient", Client)
        return await operator_bot_module.probe_system_operator_bot_evidence(
            config=SystemOperatorBotConfig(
                bot_token="secret-token",
                chat_id="secret-chat",
                timeout_seconds=99,
            )
        )

    result = asyncio.run(scenario())

    assert result == {
        "probe_complete": True,
        "endpoint_reachable": True,
        "bot_identity_available": True,
        "target_chat_available": True,
    }
    assert observed["timeout"] == 5.0
    assert observed["trust_env"] is False
    assert observed["payload"] == {"chat_id": "secret-chat"}
    assert "secret-token" not in str(result)
    assert "secret-chat" not in str(result)


def test_telegram_evidence_probe_enforces_wall_clock_deadline(
    monkeypatch,
):
    cancelled = []

    class Client:
        def __init__(self, *, timeout, trust_env):
            assert timeout == 0.02
            assert trust_env is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(("get", url))

        async def post(self, url, *, json):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(("post", url, json))

    monkeypatch.setattr(operator_bot_module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(
        operator_bot_module,
        "_TELEGRAM_EVIDENCE_TIMEOUT_SECONDS",
        0.02,
    )
    started_at = time.monotonic()

    with pytest.raises(TimeoutError):
        asyncio.run(
            operator_bot_module.probe_system_operator_bot_evidence(
                config=SystemOperatorBotConfig(
                    bot_token="secret-token",
                    chat_id="secret-chat",
                    timeout_seconds=99,
                )
            )
        )

    assert time.monotonic() - started_at < 0.2
    assert {call[0] for call in cancelled} == {"get", "post"}
