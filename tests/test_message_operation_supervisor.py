from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import telegram_kol_research.message_operation_supervisor as supervisor_module
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_operation_supervisor import (
    ContractOutcomeEvidence,
    collect_message_operation_evidence,
    evaluate_message_operation_contract,
    run_message_operation_outcome_shadow_once,
    run_message_operation_supervisor_cycle,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ManagementMessageTarget,
    MessageInstructionItem,
    MessageOperationContract,
    MessageOperationItem,
    RawMessage,
    RecognitionDecision,
    RuntimeIncident,
    SignalCandidate,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)
CASES = json.loads(
    (
        Path(__file__).parent
        / "fixtures"
        / "message_operation_incidents"
        / "cases.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["name"])
def test_redacted_message_operation_incident_corpus(case):
    contract = SimpleNamespace(
        deadline_at=NOW + timedelta(seconds=case["deadline_offset_seconds"]),
        status="observing",
    )
    evidence = ContractOutcomeEvidence.from_mapping(case["evidence"])

    result = evaluate_message_operation_contract(
        contract=contract,
        evidence=evidence,
        observed_at=NOW,
    )

    assert result.status == case["expected_status"]
    assert result.violation_code == case["expected_violation_code"]
    assert result.should_create_incident is case["should_create_incident"]


def test_verified_success_requires_every_item_to_verify():
    contract = SimpleNamespace(deadline_at=NOW + timedelta(minutes=1), status="observing")
    evidence = ContractOutcomeEvidence.from_mapping(
        {
            "items": [
                {
                    "instruction_key": "message_instruction:1",
                    "expected_descendant_kind": "management_item",
                    "expected_terminal_kind": "verified_management",
                    "state": "verified",
                    "observed_terminal_kind": "verified_management",
                    "evidence_refs": ["message_instruction:1"],
                },
                {
                    "instruction_key": "message_instruction:2",
                    "expected_descendant_kind": "management_item",
                    "expected_terminal_kind": "verified_management",
                    "state": "observing",
                    "evidence_refs": ["message_instruction:2"],
                },
            ]
        }
    )

    observing = evaluate_message_operation_contract(
        contract=contract, evidence=evidence, observed_at=NOW
    )
    verified = evaluate_message_operation_contract(
        contract=contract,
        evidence=ContractOutcomeEvidence.from_mapping(
            {
                "items": [
                    {**item, "state": "verified", "observed_terminal_kind": "verified_management"}
                    for item in evidence.to_mappings()
                ]
            }
        ),
        observed_at=NOW,
    )

    assert observing.status == "observing"
    assert observing.should_create_incident is False
    assert verified.status == "verified"
    assert verified.violation_code is None


@pytest.mark.parametrize(
    ("state", "expected_status"),
    (("duplicate_verified", "duplicate_verified"),
     ("superseded_verified", "superseded_verified")),
)
def test_proven_duplicate_and_supersession_close_without_incident(
    state, expected_status
):
    contract = SimpleNamespace(deadline_at=NOW - timedelta(seconds=1), status="observing")
    evidence = ContractOutcomeEvidence.from_mapping(
        {
            "items": [
                {
                    "instruction_key": "message_instruction:3",
                    "expected_descendant_kind": "management_item",
                    "expected_terminal_kind": "verified_management",
                    "state": state,
                    "evidence_refs": ["message_instruction:3"],
                }
            ]
        }
    )

    result = evaluate_message_operation_contract(
        contract=contract, evidence=evidence, observed_at=NOW
    )

    assert result.status == expected_status
    assert result.should_create_incident is False


def test_missing_descendant_observes_until_deadline_then_violates():
    evidence = ContractOutcomeEvidence.from_mapping(
        {
            "items": [
                {
                    "instruction_key": "message_instruction:4",
                    "expected_descendant_kind": "management_item",
                    "expected_terminal_kind": "verified_management",
                    "state": "missing",
                    "evidence_refs": ["message_instruction:4"],
                }
            ]
        }
    )

    before = evaluate_message_operation_contract(
        contract=SimpleNamespace(deadline_at=NOW + timedelta(seconds=1), status="observing"),
        evidence=evidence,
        observed_at=NOW,
    )
    after = evaluate_message_operation_contract(
        contract=SimpleNamespace(deadline_at=NOW - timedelta(seconds=1), status="observing"),
        evidence=evidence,
        observed_at=NOW,
    )

    assert before.status == "observing"
    assert after.status == "violated"
    assert after.violation_code == "missing_management_descendant"


def _seed_contract(
    session_factory,
    *,
    deadline_at: datetime,
    message_id: int = 9100,
    item_status: str = "pending",
    error_json: str | None = None,
    with_target: bool = False,
    target_admission: str = "admitted",
    target_execution: str = "pending",
) -> int:
    with session_factory() as session:
        raw = RawMessage(
            chat_id=77,
            message_id=message_id,
            posted_at=NOW,
            text="bounded supervisor fixture",
        )
        session.add(raw)
        session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model="fixture",
            authoritative_status="策略",
            authoritative_payload_json="{}",
            agreement_status="agreed",
            differences_json="[]",
            automation_status="partial_failed" if error_json else "completed",
            prompt_versions_json="{}",
            comparison_status="completed",
        )
        session.add(decision)
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
        instruction = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="management",
            strategy_instance_id=f"deepcoin:77:{message_id}:BTC:long",
            idempotency_key=f"{raw.id:08d}{0:056d}",
            status=item_status,
            error_json=error_json,
        )
        session.add(instruction)
        session.flush()
        instruction_key = f"message_instruction:{instruction.id}"
        expected_descendant = "management_item"
        if with_target:
            target = ManagementMessageTarget(
                envelope_id=1,
                raw_message_id=raw.id,
                target_lifecycle_id=1,
                target_ordinal=1,
                symbol="BTC",
                side="long",
                normalized_action="partial_take_profit",
                parameters_json="{}",
                parameter_fingerprint="a" * 64,
                collision_group_fingerprint="b" * 64,
                admission_state=target_admission,
                execution_state=target_execution,
                signal_candidate_id=candidate.id,
                message_instruction_item_id=instruction.id,
            )
            session.add(target)
            session.flush()
            instruction_key = f"management_target:{target.id}"
            expected_descendant = "management_target"
        contract = MessageOperationContract(
            raw_message_id=raw.id,
            intent_kind="take_profit",
            expected_terminal_kind="verified_execution",
            status="observing",
            deadline_at=deadline_at,
            evidence_refs_json=f'["raw_message:{raw.id}"]',
            agent_requested=False,
            policy_version="message-operation-contract-v1",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(contract)
        session.flush()
        session.add(
            MessageOperationItem(
                contract_id=contract.id,
                sequence=1,
                instruction_key=instruction_key,
                instruction_kind="take_profit",
                authoritative_instruction_id=instruction_key,
                expected_descendant_kind=expected_descendant,
                expected_terminal_kind="verified_execution",
                status="observing",
                evidence_refs_json=f'["{instruction_key}"]',
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
        return contract.id


def test_collector_classifies_exact_safety_refusal_without_incident(tmp_path):
    session_factory = create_session_factory(tmp_path / "safety-refusal.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        item_status="failed",
        error_json=(
            '{"type":"RecoveryLiveSubmitError","message":'
            '"signal_enqueue_blocked:missing_ready_confirmation,'
            'contract_size_unverified"}'
        ),
    )

    evidence = collect_message_operation_evidence(
        session_factory, contract_id=contract_id
    )
    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )

    assert evidence.items[0].state == "safety_refusal"
    assert result == {
        "errors": 0,
        "evaluated": 1,
        "observing": 0,
        "verified": 0,
        "violated": 1,
        "duplicate_verified": 0,
        "superseded_verified": 0,
        "incidents_created": 0,
        "model_calls": 0,
        "rechecked_verified": 0,
    }
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        item = session.query(MessageOperationItem).filter_by(contract_id=contract_id).one()
        assert contract.status == "violated"
        assert contract.violation_code == "action_refused"
        assert contract.runtime_incident_id is None
        assert contract.agent_requested is False
        assert item.status == "violated"
        assert item.observed_terminal_kind == "verified_refusal"
        assert session.query(RuntimeIncident).count() == 0


def test_stage1_outbox_materializes_once_per_affected_message_above_watermark(
    tmp_path,
):
    from telegram_kol_research.config import RuntimeIncidentConfig
    from telegram_kol_research.message_operation_supervisor import (
        materialize_message_operation_stage1_outbox,
    )
    from telegram_kol_research.models import (
        MessageOperationStage1Notification,
        RuntimeIncidentAffectedMessage,
    )
    from telegram_kol_research.runtime_incident_adapters import (
        capture_message_operation_failure,
    )

    session_factory = create_session_factory(tmp_path / "stage1-outbox.db")
    contract_ids = [
        _seed_contract(
            session_factory,
            deadline_at=NOW - timedelta(seconds=1),
            message_id=message_id,
        )
        for message_id in (9201, 9202, 9203)
    ]
    config = RuntimeIncidentConfig(
        capture_types=frozenset({"message_operation_failure"})
    )
    incidents = []
    raw_ids = []
    for contract_id in contract_ids:
        with session_factory() as session:
            contract = session.get(MessageOperationContract, contract_id)
            contract.status = "violated"
            contract.violation_code = "no_operation_created"
            raw_ids.append(contract.raw_message_id)
            session.commit()
        incidents.append(
            capture_message_operation_failure(
                session_factory,
                config=config,
                contract_id=contract_id,
                raw_message_id=raw_ids[-1],
                violation_code="no_operation_created",
                evidence_refs=(),
                occurred_at=NOW,
                shadow_only=False,
            )
        )

    assert incidents[0].id == incidents[1].id
    created = materialize_message_operation_stage1_outbox(
        session_factory,
        after_contract_id=0,
        created_at=NOW,
        limit=2,
    )
    repeated = materialize_message_operation_stage1_outbox(
        session_factory,
        after_contract_id=0,
        created_at=NOW,
        limit=2,
    )
    exhausted = materialize_message_operation_stage1_outbox(
        session_factory,
        after_contract_id=0,
        created_at=NOW,
        limit=2,
    )

    assert (created, repeated, exhausted) == (2, 1, 0)
    with session_factory() as session:
        rows = session.query(MessageOperationStage1Notification).order_by(
            MessageOperationStage1Notification.raw_message_id
        ).all()
        assert [row.raw_message_id for row in rows] == sorted(raw_ids)
        assert all(row.runtime_incident_id == incidents[0].id for row in rows)
        assert all(row.status == "pending" for row in rows)
        assert session.query(RuntimeIncidentAffectedMessage).count() == 3
        assert session.query(RuntimeIncident).count() == 1
        assert session.get(RuntimeIncident, incidents[0].id).agent_attempt_count == 0

    assert materialize_message_operation_stage1_outbox(
        session_factory,
        after_contract_id=max(contract_ids),
        created_at=NOW,
        limit=20,
    ) == 0


def test_stage1_contract_watermark_allows_new_message_on_old_coalesced_incident(
    tmp_path,
):
    from telegram_kol_research.config import RuntimeIncidentConfig
    from telegram_kol_research.message_operation_supervisor import (
        materialize_message_operation_stage1_outbox,
    )
    from telegram_kol_research.models import MessageOperationStage1Notification
    from telegram_kol_research.runtime_incident_adapters import (
        capture_message_operation_failure,
    )

    session_factory = create_session_factory(tmp_path / "stage1-coalesced.db")
    config = RuntimeIncidentConfig(
        capture_types=frozenset({"message_operation_failure"})
    )

    def violate_and_capture(message_id):
        contract_id = _seed_contract(
            session_factory,
            deadline_at=NOW - timedelta(seconds=1),
            message_id=message_id,
        )
        with session_factory() as session:
            contract = session.get(MessageOperationContract, contract_id)
            contract.status = "violated"
            contract.violation_code = "no_operation_created"
            raw_id = contract.raw_message_id
            session.commit()
        incident = capture_message_operation_failure(
            session_factory,
            config=config,
            contract_id=contract_id,
            raw_message_id=raw_id,
            violation_code="no_operation_created",
            evidence_refs=(),
            occurred_at=NOW,
            shadow_only=False,
        )
        return contract_id, raw_id, incident

    old_contract_id, old_raw_id, old_incident = violate_and_capture(9251)
    new_contract_id, new_raw_id, new_incident = violate_and_capture(9252)

    assert new_incident.id == old_incident.id
    assert new_contract_id > old_contract_id
    assert materialize_message_operation_stage1_outbox(
        session_factory,
        after_contract_id=old_contract_id,
        created_at=NOW,
        limit=20,
    ) == 1
    with session_factory() as session:
        row = session.query(MessageOperationStage1Notification).one()
        assert row.runtime_incident_id == old_incident.id
        assert row.raw_message_id == new_raw_id
        assert row.raw_message_id != old_raw_id
        assert row.message_operation_contract_id == new_contract_id


def test_coverage_snapshot_reports_every_pipeline_stage_and_silent_gap(tmp_path):
    assert hasattr(supervisor_module, "build_message_operation_coverage_snapshot")
    from telegram_kol_research.config import RuntimeIncidentConfig
    from telegram_kol_research.message_operation_supervisor import (
        materialize_message_operation_stage1_outbox,
    )
    from telegram_kol_research.runtime_incident_adapters import (
        capture_message_operation_failure,
    )

    session_factory = create_session_factory(tmp_path / "coverage.db")
    verified_id = _seed_contract(
        session_factory,
        deadline_at=NOW - timedelta(minutes=2),
        message_id=9261,
    )
    violated_id = _seed_contract(
        session_factory,
        deadline_at=NOW - timedelta(minutes=2),
        message_id=9262,
    )
    with session_factory() as session:
        verified = session.get(MessageOperationContract, verified_id)
        verified.status = "verified"
        violated = session.get(MessageOperationContract, violated_id)
        violated.status = "violated"
        violated.violation_code = "no_operation_created"
        violated_raw_id = violated.raw_message_id
        missing_raw = RawMessage(
            chat_id=77,
            message_id=9263,
            posted_at=NOW - timedelta(minutes=2),
            text="missing contract",
        )
        session.add(missing_raw)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=missing_raw.id,
                input_kind="text",
                authoritative_model="fixture",
                authoritative_status="策略",
                authoritative_payload_json=(
                    '{"lifecycle_event":{"event_type":"position_update",'
                    '"management_action":"partial_take_profit"}}'
                ),
                agreement_status="agreed",
                differences_json="[]",
                automation_status="completed",
                prompt_versions_json="{}",
                comparison_status="completed",
            )
        )
        session.commit()
        missing_raw_id = missing_raw.id

    incident = capture_message_operation_failure(
        session_factory,
        config=RuntimeIncidentConfig(
            capture_types=frozenset({"message_operation_failure"})
        ),
        contract_id=violated_id,
        raw_message_id=violated_raw_id,
        violation_code="no_operation_created",
        evidence_refs=(),
        occurred_at=NOW - timedelta(minutes=2),
        shadow_only=False,
    )
    assert materialize_message_operation_stage1_outbox(
        session_factory,
        after_contract_id=0,
        created_at=NOW - timedelta(minutes=2),
    ) == 1

    snapshot = supervisor_module.build_message_operation_coverage_snapshot(
        session_factory,
        after_raw_message_id=0,
        supervisor_last_success_at=NOW - timedelta(seconds=30),
        observed_at=NOW,
        limit=100,
    )

    assert snapshot == {
        "schema_version": 1,
        "coverage_enabled": True,
        "scan_truncated": False,
        "executable_messages_total": 3,
        "contracts_created_total": 2,
        "contracts_verified_total": 1,
        "contracts_violated_total": 1,
        "executable_without_contract_total": 1,
        "violations_without_stage1_total": 0,
        "stage1_pending": 1,
        "stage1_delivered": 0,
        "stage1_failed": 0,
        "agent_pending": 1,
        "agent_diagnosed": 0,
        "agent_failed": 0,
        "agent_timed_out": 0,
        "incidents_without_terminal_stage2_total": 1,
        "handoffs_persisted_total": 0,
        "stage2_pending": 0,
        "stage2_delivered": 0,
        "stage2_failed": 0,
        "oldest_nonterminal_age_seconds": 120,
        "supervisor_last_success_at": "2026-08-09T01:59:30+00:00",
    }
    assert missing_raw_id > 0
    assert incident.id > 0


def test_disabled_coverage_snapshot_is_database_free_clean_rollback():
    def unavailable_session_factory():
        raise AssertionError("disabled coverage must not read the database")

    snapshot = supervisor_module.build_message_operation_coverage_snapshot(
        unavailable_session_factory,
        after_raw_message_id=0,
        supervisor_last_success_at=None,
        observed_at=NOW,
        coverage_enabled=False,
    )
    assert snapshot["coverage_enabled"] is False
    assert snapshot["scan_truncated"] is False
    assert snapshot["supervisor_last_success_at"] is None
    assert all(
        value == 0
        for key, value in snapshot.items()
        if key.endswith("_total")
        or key.startswith("stage1_")
        or key.startswith("stage2_")
        or key.startswith("agent_")
        or key == "oldest_nonterminal_age_seconds"
    )


def test_multi_instruction_completeness_flags_missing_and_hidden_siblings(tmp_path):
    session_factory = create_session_factory(tmp_path / "multi-completeness.db")
    save_trading_settings(
        session_factory,
        {
            "multi_instruction_mode": "live",
            "multi_instruction_activation_after_raw_message_id": 0,
        },
    )
    with session_factory() as session:
        raw = RawMessage(chat_id=77, message_id=9301, text="cancel old; enter long")
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
                        "instructions": [
                            {"kind": "cancel_pending_entry"},
                            {"kind": "entry"},
                        ]
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
            side="short",
            event_type="close_signal",
            management_action="cancel_pending_entry",
            parse_source="mimo_authoritative",
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=raw.id,
                signal_candidate_id=candidate.id,
                sequence=0,
                instruction_kind="management",
                idempotency_key="a" * 64,
                status="failed",
            )
        )
        contract = MessageOperationContract(
            raw_message_id=raw.id,
            intent_kind="manage",
            expected_terminal_kind="verified_management",
            status="verified",
            deadline_at=NOW,
            evidence_refs_json="[]",
            agent_requested=False,
            policy_version="message-operation-contract-v1",
        )
        session.add(contract)
        session.commit()
        raw_message_id = raw.id

    violations = supervisor_module.audit_multi_instruction_completeness(
        session_factory,
        raw_message_id=raw_message_id,
    )

    assert {row["violation_code"] for row in violations} == {
        "missing_instruction_projection",
        "hidden_instruction_failure",
    }
    assert {row["severity"] for row in violations} == {"high"}

    with session_factory() as session:
        sibling = SignalCandidate(
            raw_message_id=raw_message_id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
        )
        session.add(sibling)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=raw_message_id,
                signal_candidate_id=sibling.id,
                sequence=1,
                instruction_kind="entry",
                idempotency_key="b" * 64,
                status="pending",
            )
        )
        session.commit()

    sibling_violations = supervisor_module.audit_multi_instruction_completeness(
        session_factory,
        raw_message_id=raw_message_id,
    )
    assert {row["violation_code"] for row in sibling_violations} == {
        "unevaluated_sibling_instruction",
        "hidden_instruction_failure",
    }
    assert supervisor_module.apply_multi_instruction_completeness_violations(
        session_factory,
        after_raw_message_id=0,
        limit=10,
        observed_at=NOW,
    ) == 1
    with session_factory() as session:
        contract = session.query(MessageOperationContract).one()
        assert contract.status == "violated"
        assert contract.violation_code == "unevaluated_sibling_instruction"


def test_live_supervisor_cycle_captures_natural_violation_for_stage1(tmp_path):
    from telegram_kol_research.config import RuntimeIncidentConfig
    from telegram_kol_research.message_operation_supervisor import (
        materialize_message_operation_stage1_outbox,
    )
    from telegram_kol_research.models import RuntimeIncidentAffectedMessage

    session_factory = create_session_factory(tmp_path / "natural-chain.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW - timedelta(seconds=1),
        message_id=9271,
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw_message_id = contract.raw_message_id

    result = run_message_operation_supervisor_cycle(
        session_factory,
        after_raw_message_id=0,
        capture_after_raw_message_id=0,
        limit=10,
        observed_at=NOW,
        runtime_incident_config=RuntimeIncidentConfig(
            capture_types=frozenset({"message_operation_failure"})
        ),
    )

    assert result["outcome_violated"] == 1
    assert result["violations_captured"] == 1
    assert result["capture_errors"] == 0
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        assert contract.runtime_incident_id is not None
        relation = session.query(RuntimeIncidentAffectedMessage).filter_by(
            message_operation_contract_id=contract_id,
            raw_message_id=raw_message_id,
        ).one()
        assert relation.runtime_incident_id == contract.runtime_incident_id
    assert materialize_message_operation_stage1_outbox(
        session_factory,
        after_contract_id=0,
        created_at=NOW,
    ) == 1

def test_collector_verifies_confirmed_management_target(tmp_path):
    session_factory = create_session_factory(tmp_path / "confirmed-target.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        item_status="succeeded",
        with_target=True,
        target_execution="confirmed",
    )

    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        session.add(
            ExecutionEvent(
                source_message_id=raw.id,
                chat_id=raw.chat_id,
                message_id=raw.message_id,
                strategy_instance_id=f"deepcoin:77:{raw.message_id}:BTC:long",
                action="partial_take_profit",
                status="confirmed",
                reason="exchange_readback_confirmed",
                created_at=NOW,
            )
        )
        session.commit()

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )

    assert result["verified"] == 1
    assert result["incidents_created"] == 0
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        assert contract.status == "verified"
        assert contract.runtime_incident_id is None
        assert session.query(RuntimeIncident).count() == 0


def test_confirmed_management_target_without_exchange_proof_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "local-target-only.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW - timedelta(seconds=1),
        item_status="succeeded",
        with_target=True,
        target_execution="confirmed",
    )

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )

    assert result["violated"] == 1
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        assert contract.violation_code == "local_success_unverified"


def test_prior_strategy_event_cannot_verify_current_management_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale-event.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW - timedelta(seconds=1),
        item_status="succeeded",
        with_target=True,
        target_execution="confirmed",
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        session.add(
            ExecutionEvent(
                source_message_id=None,
                strategy_instance_id=f"deepcoin:77:{raw.message_id}:BTC:long",
                action="entry",
                status="confirmed",
                created_at=NOW - timedelta(days=1),
            )
        )
        session.commit()

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )

    assert result["violated"] == 1
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        assert contract.violation_code == "local_success_unverified"


def test_signal_candidate_entry_uses_exact_exchange_binding_proof(tmp_path):
    session_factory = create_session_factory(tmp_path / "candidate-entry.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        item_status="pending",
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        instruction = session.query(MessageInstructionItem).filter_by(
            raw_message_id=raw.id
        ).one()
        candidate = session.get(SignalCandidate, instruction.signal_candidate_id)
        item = session.query(MessageOperationItem).filter_by(
            contract_id=contract_id
        ).one()
        item.instruction_key = f"signal_candidate:{candidate.id}"
        item.authoritative_instruction_id = item.instruction_key
        item.instruction_kind = "new_entry"
        item.expected_descendant_kind = "execution_binding"
        item.expected_terminal_kind = "verified_entry"
        contract.intent_kind = "new_entry"
        contract.expected_terminal_kind = "verified_entry"
        session.delete(instruction)
        session.add(
            ExecutionBinding(
                strategy_instance_id=None,
                kol_id="fixture",
                chat_id=raw.chat_id,
                message_id=raw.message_id,
                symbol="BTC",
                side="long",
                status="active",
                last_exchange_status="position_ownership_verified",
            )
        )
        session.commit()

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )

    assert result["verified"] == 1
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        assert contract.status == "verified"


def test_preexisting_active_binding_cannot_verify_new_add_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "add-entry-binding.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        item_status="succeeded",
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        item = session.query(MessageOperationItem).filter_by(
            contract_id=contract_id
        ).one()
        instruction = session.query(MessageInstructionItem).filter_by(
            raw_message_id=raw.id
        ).one()
        item.instruction_kind = "add_entry"
        item.expected_descendant_kind = "execution_binding"
        item.expected_terminal_kind = "verified_entry"
        contract.intent_kind = "add_entry"
        contract.expected_terminal_kind = "verified_entry"
        session.add(
            ExecutionBinding(
                strategy_instance_id=instruction.strategy_instance_id,
                kol_id="fixture",
                chat_id=77,
                message_id=8000,
                symbol="BTC",
                side="long",
                status="active",
                last_exchange_status="position_ownership_verified",
                created_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1),
            )
        )
        session.commit()

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )

    assert result["observing"] == 1
    assert result["verified"] == 0
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        assert contract.status == "observing"


def test_verified_contract_is_rechecked_for_later_reconciliation_disproof(tmp_path):
    session_factory = create_session_factory(tmp_path / "later-disproof.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        item_status="succeeded",
        with_target=True,
        target_execution="confirmed",
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        session.add(
            ExecutionEvent(
                source_message_id=raw.id,
                chat_id=raw.chat_id,
                message_id=raw.message_id,
                strategy_instance_id=f"deepcoin:77:{raw.message_id}:BTC:long",
                action="partial_take_profit",
                status="confirmed",
                reason="exchange_readback_confirmed",
                created_at=NOW,
            )
        )
        session.commit()

    first = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        session.add(
            ExecutionEvent(
                source_message_id=raw.id,
                chat_id=raw.chat_id,
                message_id=raw.message_id,
                strategy_instance_id=f"deepcoin:77:{raw.message_id}:BTC:long",
                action="reconcile",
                status="failed",
                reason="reconciliation_disproved_success",
                created_at=NOW + timedelta(seconds=1),
            )
        )
        session.commit()
    second = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW + timedelta(seconds=1)
    )

    assert first["verified"] == 1
    assert second["rechecked_verified"] == 1
    assert second["violated"] == 1
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        assert contract.status == "violated"
        assert contract.violation_code == "reconciliation_disproved_success"


def test_later_same_action_other_message_does_not_disprove_verified_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "unrelated-disproof.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        item_status="succeeded",
        with_target=True,
        target_execution="confirmed",
    )
    strategy_id = "deepcoin:77:9100:BTC:long"
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        session.add(
            ExecutionEvent(
                source_message_id=raw.id,
                strategy_instance_id=strategy_id,
                action="strategy_management_close_submit",
                status="confirmed",
                created_at=NOW,
            )
        )
        session.commit()
    run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )
    with session_factory() as session:
        session.add(
            ExecutionEvent(
                source_message_id=999_999,
                strategy_instance_id=strategy_id,
                action="strategy_management_close_submit",
                status="failed",
                reason="exchange_readback_mismatch",
                created_at=NOW + timedelta(seconds=1),
            )
        )
        session.commit()

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW + timedelta(seconds=1)
    )

    assert result["rechecked_verified"] == 1
    assert result["verified"] == 1
    assert result["violated"] == 0
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        assert contract.status == "verified"
        assert contract.violation_code is None


def test_same_strategy_action_success_verifies_only_its_source_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "two-source-success.db")
    first_contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        message_id=9201,
        item_status="succeeded",
    )
    second_contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        message_id=9202,
        item_status="succeeded",
    )
    shared_strategy = "deepcoin:77:shared-entry:BTC:long"
    with session_factory() as session:
        contracts = [
            session.get(MessageOperationContract, first_contract_id),
            session.get(MessageOperationContract, second_contract_id),
        ]
        raw_ids = [contract.raw_message_id for contract in contracts]
        instructions = session.query(MessageInstructionItem).filter(
            MessageInstructionItem.raw_message_id.in_(raw_ids)
        ).all()
        for instruction in instructions:
            instruction.strategy_instance_id = shared_strategy
        second_raw = session.get(RawMessage, contracts[1].raw_message_id)
        session.add(
            ExecutionEvent(
                source_message_id=second_raw.id,
                strategy_instance_id=shared_strategy,
                action="strategy_management_close_submit",
                status="confirmed",
                created_at=NOW,
            )
        )
        session.commit()

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )

    assert result["verified"] == 1
    assert result["observing"] == 1
    with session_factory() as session:
        first = session.get(MessageOperationContract, first_contract_id)
        second = session.get(MessageOperationContract, second_contract_id)
        assert first.status == "observing"
        assert second.status == "verified"


@pytest.mark.parametrize(
    ("intent_kind", "descendant", "terminal", "production_action"),
    (
        ("new_entry", "execution_binding", "verified_entry", "create_trigger_entry"),
        (
            "take_profit",
            "position_mutation_intent",
            "verified_execution",
            "strategy_management_close_submit",
        ),
        (
            "stop_loss",
            "protection_revision",
            "verified_protection",
            "adjust_position_tpsl",
        ),
        (
            "cancel",
            "position_mutation_intent",
            "verified_cancel",
            "cancel_revision_entry_leg",
        ),
        (
            "exit",
            "position_mutation_intent",
            "verified_exit",
            "close_bound_position_market",
        ),
    ),
)
def test_persisted_production_event_vocabulary_is_intent_scoped(
    tmp_path, intent_kind, descendant, terminal, production_action
):
    session_factory = create_session_factory(tmp_path / f"{intent_kind}.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        item_status="succeeded",
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        item = session.query(MessageOperationItem).filter_by(
            contract_id=contract_id
        ).one()
        item.instruction_kind = intent_kind
        item.expected_descendant_kind = descendant
        item.expected_terminal_kind = terminal
        contract.intent_kind = intent_kind
        contract.expected_terminal_kind = terminal
        session.add(
            ExecutionEvent(
                source_message_id=raw.id,
                chat_id=raw.chat_id,
                message_id=raw.message_id,
                strategy_instance_id=f"deepcoin:77:{raw.message_id}:BTC:long",
                action=production_action,
                status="confirmed",
                reason="exchange_readback_confirmed",
                created_at=NOW,
            )
        )
        session.commit()

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )

    assert result["verified"] == 1


def test_one_confirmed_item_cannot_verify_another_item(tmp_path):
    session_factory = create_session_factory(tmp_path / "multi-item-scope.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        item_status="succeeded",
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="long",
            event_type="position_update",
            management_action="stop_loss",
            review_status="approved",
        )
        session.add(candidate)
        session.flush()
        instruction = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=1,
            instruction_kind="management",
            strategy_instance_id=f"deepcoin:77:{raw.message_id}:BTC:long",
            idempotency_key=f"{raw.id:08d}{1:056d}",
            status="succeeded",
        )
        session.add(instruction)
        session.flush()
        session.add(
            MessageOperationItem(
                contract_id=contract_id,
                sequence=2,
                instruction_key=f"message_instruction:{instruction.id}",
                instruction_kind="stop_loss",
                authoritative_instruction_id=f"message_instruction:{instruction.id}",
                expected_descendant_kind="protection_revision",
                expected_terminal_kind="verified_protection",
                status="observing",
                evidence_refs_json=f'["message_instruction:{instruction.id}"]',
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ExecutionEvent(
                source_message_id=raw.id,
                strategy_instance_id=instruction.strategy_instance_id,
                action="strategy_management_close_submit",
                status="confirmed",
                created_at=NOW,
            )
        )
        session.commit()

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )

    assert result["observing"] == 1
    with session_factory() as session:
        items = session.query(MessageOperationItem).filter_by(
            contract_id=contract_id
        ).order_by(MessageOperationItem.sequence).all()
        assert [item.status for item in items] == ["verified", "observing"]


def test_same_action_event_requires_exact_item_strategy_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "same-action-scope.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        item_status="succeeded",
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        first_instruction = session.query(MessageInstructionItem).filter_by(
            raw_message_id=raw.id
        ).one()
        first_instruction.strategy_instance_id = "deepcoin:77:entry-a:BTC:long"
        second_candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="ETH",
            side="long",
            event_type="position_update",
            management_action="partial_take_profit",
            review_status="approved",
        )
        session.add(second_candidate)
        session.flush()
        second_instruction = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=second_candidate.id,
            sequence=1,
            instruction_kind="management",
            strategy_instance_id="deepcoin:77:entry-b:ETH:long",
            idempotency_key=f"{raw.id:08d}{2:056d}",
            status="succeeded",
        )
        session.add(second_instruction)
        session.flush()
        session.add(
            MessageOperationItem(
                contract_id=contract_id,
                sequence=2,
                instruction_key=f"message_instruction:{second_instruction.id}",
                instruction_kind="take_profit",
                authoritative_instruction_id=(
                    f"message_instruction:{second_instruction.id}"
                ),
                expected_descendant_kind="position_mutation_intent",
                expected_terminal_kind="verified_execution",
                status="observing",
                evidence_refs_json=(
                    f'["message_instruction:{second_instruction.id}"]'
                ),
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            ExecutionEvent(
                source_message_id=raw.id,
                strategy_instance_id=first_instruction.strategy_instance_id,
                action="strategy_management_close_submit",
                status="confirmed",
                created_at=NOW,
            )
        )
        session.commit()

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )

    assert result["observing"] == 1
    with session_factory() as session:
        items = session.query(MessageOperationItem).filter_by(
            contract_id=contract_id
        ).order_by(MessageOperationItem.sequence).all()
        assert [item.status for item in items] == ["verified", "observing"]


def test_high_cardinality_exchange_history_keeps_decisive_ref_bounded(tmp_path):
    session_factory = create_session_factory(tmp_path / "bounded-history.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(minutes=1),
        item_status="succeeded",
    )
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        raw = session.get(RawMessage, contract.raw_message_id)
        for index in range(40):
            session.add(
                ExecutionEvent(
                    source_message_id=raw.id,
                    strategy_instance_id=f"deepcoin:77:{raw.message_id}:BTC:long",
                    action="unrelated_audit",
                    status="submitted",
                    created_at=NOW + timedelta(microseconds=index),
                )
            )
        decisive = ExecutionEvent(
            source_message_id=raw.id,
            strategy_instance_id=f"deepcoin:77:{raw.message_id}:BTC:long",
            action="strategy_management_close_submit",
            status="confirmed",
            created_at=NOW + timedelta(seconds=1),
        )
        session.add(decisive)
        session.commit()
        decisive_id = decisive.id

    evidence = collect_message_operation_evidence(
        session_factory, contract_id=contract_id
    )

    assert len(evidence.items[0].evidence_refs) <= 32
    assert f"execution_event:{decisive_id}" in evidence.items[0].evidence_refs


def test_missing_descendant_stays_observing_then_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "missing-descendant.db")
    contract_id = _seed_contract(
        session_factory,
        deadline_at=NOW + timedelta(seconds=1),
    )
    with session_factory() as session:
        session.query(MessageInstructionItem).delete()
        session.commit()

    before = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW
    )
    after = run_message_operation_outcome_shadow_once(
        session_factory, limit=10, observed_at=NOW + timedelta(seconds=2)
    )

    assert before["observing"] == 1
    assert after["violated"] == 1
    with session_factory() as session:
        contract = session.get(MessageOperationContract, contract_id)
        assert contract.violation_code == "missing_management_descendant"
        assert session.query(RuntimeIncident).count() == 0


def test_outcome_shadow_cycle_is_bounded(tmp_path):
    session_factory = create_session_factory(tmp_path / "bounded.db")
    _seed_contract(
        session_factory, deadline_at=NOW + timedelta(minutes=1), message_id=9100
    )
    _seed_contract(
        session_factory, deadline_at=NOW + timedelta(minutes=1), message_id=9101
    )

    result = run_message_operation_outcome_shadow_once(
        session_factory, limit=1, observed_at=NOW
    )

    assert result["evaluated"] == 1
