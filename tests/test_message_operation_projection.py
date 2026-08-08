from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest
from typer.testing import CliRunner

from telegram_kol_research import authoritative_recognition, llm_chat
from telegram_kol_research.cli import app
from telegram_kol_research.config import load_message_operation_supervisor_config
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_operation_contracts import (
    persist_message_operation_projection,
    project_message_operation_contract,
    run_message_operation_shadow_once,
)
from telegram_kol_research.models import (
    ManagementMessageEnvelope,
    ManagementMessageTarget,
    MessageInstructionItem,
    MessageOperationContract,
    MessageOperationItem,
    RawMessage,
    RecognitionDecision,
    RuntimeIncident,
    SignalCandidate,
    StrategyLifecycle,
)


NOW = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)


def _decision(
    session,
    *,
    raw_message_id: int,
    event_type: str,
    management_action: str | None = None,
    resolution_status: str | None = None,
    status: str = "策略",
    comparison_status: str = "completed",
) -> RecognitionDecision:
    lifecycle_event = {"event_type": event_type}
    if management_action is not None:
        lifecycle_event["management_action"] = management_action
    payload = {"lifecycle_event": lifecycle_event}
    if resolution_status is not None:
        payload["resolution_status"] = resolution_status
    row = RecognitionDecision(
        raw_message_id=raw_message_id,
        input_kind="text",
        authoritative_model="fixture",
        authoritative_status=status,
        authoritative_payload_json=json.dumps(payload),
        agreement_status="agreed",
        differences_json="[]",
        automation_status="skipped",
        prompt_versions_json="{}",
        comparison_status=comparison_status,
    )
    session.add(row)
    session.flush()
    return row


def _message(
    session_factory,
    *,
    message_id: int,
    event_type: str,
    actions: tuple[str | None, ...] = (),
    instruction_statuses: tuple[str, ...] = (),
    retired: tuple[bool, ...] = (),
    resolution_status: str | None = None,
    status: str = "策略",
    comparison_status: str = "completed",
) -> int:
    with session_factory() as session:
        raw = RawMessage(
            chat_id=77,
            message_id=message_id,
            posted_at=NOW,
            text="bounded projection fixture",
        )
        session.add(raw)
        session.flush()
        _decision(
            session,
            raw_message_id=raw.id,
            event_type=event_type,
            management_action=actions[0] if len(actions) == 1 else None,
            resolution_status=resolution_status,
            status=status,
            comparison_status=comparison_status,
        )
        for index, action in enumerate(actions):
            candidate = SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type=event_type,
                management_action=action,
                review_status="approved",
            )
            session.add(candidate)
            session.flush()
            item = MessageInstructionItem(
                raw_message_id=raw.id,
                signal_candidate_id=candidate.id,
                sequence=index,
                instruction_kind=(
                    "entry" if event_type in {"entry_signal", "add_entry"}
                    else "management"
                ),
                idempotency_key=f"{raw.id:08d}{index:056d}",
                status=(
                    instruction_statuses[index]
                    if index < len(instruction_statuses)
                    else "pending"
                ),
                retired_at=(
                    NOW if index < len(retired) and retired[index] else None
                ),
            )
            session.add(item)
        session.commit()
        return raw.id


@pytest.mark.parametrize(
    ("event_type", "action", "intent_kind", "terminal_kind"),
    (
        ("entry_signal", None, "new_entry", "verified_entry"),
        ("add_entry", None, "add_entry", "verified_entry"),
        ("position_update", "partial_take_profit", "take_profit", "verified_execution"),
        ("position_update", "stop_loss", "stop_loss", "verified_protection"),
        ("position_update", "cancel_entry", "cancel", "verified_cancel"),
        ("exit_position", "full_exit", "exit", "verified_exit"),
    ),
)
def test_projection_maps_authoritative_instruction_classes_without_ai(
    tmp_path,
    event_type,
    action,
    intent_kind,
    terminal_kind,
):
    session_factory = create_session_factory(tmp_path / "projection.db")
    raw_message_id = _message(
        session_factory,
        message_id=100,
        event_type=event_type,
        actions=(action,),
    )

    projection = project_message_operation_contract(
        session_factory,
        raw_message_id=raw_message_id,
    )

    assert projection is not None
    assert projection.executable_intent is True
    assert projection.intent_kind == intent_kind
    assert projection.expected_terminal_kind == terminal_kind
    assert projection.model_calls == 0
    assert [item.intent_kind for item in projection.items] == [intent_kind]
    assert projection.deadline_at > NOW


def test_projection_preserves_order_for_multi_instruction_management(tmp_path):
    session_factory = create_session_factory(tmp_path / "multi.db")
    raw_message_id = _message(
        session_factory,
        message_id=101,
        event_type="position_update",
        actions=("partial_take_profit", "stop_loss"),
    )

    projection = project_message_operation_contract(
        session_factory,
        raw_message_id=raw_message_id,
    )

    assert projection is not None
    assert projection.intent_kind == "manage"
    assert projection.expected_terminal_kind == "verified_management"
    assert [item.intent_kind for item in projection.items] == [
        "take_profit",
        "stop_loss",
    ]
    assert [item.sequence for item in projection.items] == [1, 2]
    assert projection.model_calls == 0


def test_projection_uses_exact_multi_target_records(tmp_path):
    session_factory = create_session_factory(tmp_path / "targets.db")
    raw_message_id = _message(
        session_factory,
        message_id=102,
        event_type="position_update",
        actions=("partial_take_profit", "partial_take_profit"),
    )
    with session_factory() as session:
        envelope = ManagementMessageEnvelope(
            raw_message_id=raw_message_id,
            decision_fingerprint="d" * 64,
            normalized_action="partial_take_profit",
            shared_parameters_json='{"management_fraction":0.5}',
            projection_mode="live",
        )
        session.add(envelope)
        session.flush()
        candidates = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == raw_message_id)
            .order_by(SignalCandidate.id)
            .all()
        )
        items = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.raw_message_id == raw_message_id)
            .order_by(MessageInstructionItem.id)
            .all()
        )
        for index, (candidate, item, symbol) in enumerate(
            zip(candidates, items, ("BTC", "ETH"), strict=True)
        ):
            lifecycle = StrategyLifecycle(
                chat_id=77,
                message_id=90 + index,
                symbol=symbol,
                side="long",
                lifecycle_status="entered",
                signal_at=NOW,
            )
            session.add(lifecycle)
            session.flush()
            candidate.target_lifecycle_id = lifecycle.id
            session.add(
                ManagementMessageTarget(
                    envelope_id=envelope.id,
                    raw_message_id=raw_message_id,
                    target_lifecycle_id=lifecycle.id,
                    target_ordinal=index,
                    symbol=symbol,
                    side="long",
                    normalized_action="partial_take_profit",
                    parameters_json='{"management_fraction":0.5}',
                    parameter_fingerprint=str(index + 1) * 64,
                    collision_group_fingerprint=str(index + 3) * 64,
                    admission_state="admitted",
                    execution_state="pending",
                    signal_candidate_id=candidate.id,
                    message_instruction_item_id=item.id,
                )
            )
        session.commit()

    projection = project_message_operation_contract(
        session_factory,
        raw_message_id=raw_message_id,
    )

    assert projection is not None
    assert len(projection.items) == 2
    assert [item.target_lifecycle_id for item in projection.items] == [1, 2]
    assert all("management_target:" in " ".join(item.evidence_references) for item in projection.items)


def test_unresolved_executable_projection_never_invents_target(tmp_path):
    session_factory = create_session_factory(tmp_path / "unresolved.db")
    raw_message_id = _message(
        session_factory,
        message_id=103,
        event_type="position_update",
        actions=("partial_take_profit",),
        resolution_status="unresolved",
    )

    projection = project_message_operation_contract(
        session_factory,
        raw_message_id=raw_message_id,
    )

    assert projection is not None
    assert projection.intent_kind == "unresolved_executable"
    assert len(projection.items) == 1
    assert projection.items[0].target_lifecycle_id is None
    assert projection.items[0].authoritative_instruction_id.startswith(
        "recognition_decision:"
    )


def test_ordinary_chat_has_no_contract_projection(tmp_path):
    session_factory = create_session_factory(tmp_path / "ordinary.db")
    raw_message_id = _message(
        session_factory,
        message_id=104,
        event_type="none",
        status="非策略",
    )

    assert project_message_operation_contract(
        session_factory,
        raw_message_id=raw_message_id,
    ) is None


def test_projection_preserves_duplicate_and_superseded_dispositions(tmp_path):
    session_factory = create_session_factory(tmp_path / "dispositions.db")
    duplicate_id = _message(
        session_factory,
        message_id=105,
        event_type="position_update",
        actions=("partial_take_profit",),
        instruction_statuses=("duplicate",),
    )
    superseded_id = _message(
        session_factory,
        message_id=106,
        event_type="position_update",
        actions=("stop_loss",),
        retired=(True,),
    )

    duplicate = project_message_operation_contract(
        session_factory, raw_message_id=duplicate_id
    )
    superseded = project_message_operation_contract(
        session_factory, raw_message_id=superseded_id
    )

    assert duplicate is not None
    assert superseded is not None
    assert duplicate.items[0].source_disposition == "duplicate"
    assert superseded.items[0].source_disposition == "superseded"


def test_projection_does_not_reach_any_provider_entry_point(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "no-provider.db")
    raw_message_id = _message(
        session_factory,
        message_id=107,
        event_type="position_update",
        actions=("partial_take_profit",),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("provider or recognition path must not be called")

    monkeypatch.setattr(llm_chat, "request_structured_chat_turn", forbidden)
    monkeypatch.setattr(llm_chat, "request_grounded_chat_answer", forbidden)
    monkeypatch.setattr(
        authoritative_recognition,
        "process_authoritative_message",
        forbidden,
    )

    projection = project_message_operation_contract(
        session_factory,
        raw_message_id=raw_message_id,
    )

    assert projection is not None
    assert projection.model_calls == 0


def test_projection_persistence_is_idempotent_and_bounded(tmp_path):
    session_factory = create_session_factory(tmp_path / "persist.db")
    raw_message_id = _message(
        session_factory,
        message_id=108,
        event_type="position_update",
        actions=("partial_take_profit", "stop_loss"),
    )
    projection = project_message_operation_contract(
        session_factory,
        raw_message_id=raw_message_id,
    )
    assert projection is not None

    first = persist_message_operation_projection(session_factory, projection)
    second = persist_message_operation_projection(session_factory, projection)

    assert second.id == first.id
    assert json.loads(first.evidence_refs_json) == list(
        projection.evidence_references
    )
    with session_factory() as session:
        assert session.query(MessageOperationContract).count() == 1
        assert session.query(MessageOperationItem).count() == 2
        assert session.query(RuntimeIncident).count() == 0


def test_shadow_cycle_scans_only_terminal_rows_above_watermark(tmp_path):
    session_factory = create_session_factory(tmp_path / "cycle.db")
    below = _message(
        session_factory,
        message_id=109,
        event_type="position_update",
        actions=("partial_take_profit",),
    )
    ordinary = _message(
        session_factory,
        message_id=110,
        event_type="none",
        status="非策略",
    )
    executable = _message(
        session_factory,
        message_id=111,
        event_type="exit_position",
        actions=("full_exit",),
    )
    _message(
        session_factory,
        message_id=112,
        event_type="position_update",
        actions=("stop_loss",),
        comparison_status="execution_running",
    )

    result = run_message_operation_shadow_once(
        session_factory,
        after_raw_message_id=below,
        limit=20,
        now=NOW,
    )

    assert result == {
        "contracts_created": 1,
        "errors": 0,
        "messages_scanned": 2,
        "model_calls": 0,
        "ordinary_skipped": 1,
    }
    with session_factory() as session:
        contract = session.query(MessageOperationContract).one()
        assert contract.raw_message_id == executable
        assert contract.raw_message_id != ordinary
        assert session.query(RuntimeIncident).count() == 0


def test_supervisor_config_is_disabled_and_fail_closed_by_default():
    config = load_message_operation_supervisor_config(
        environ={}, env_file_paths=[]
    )

    assert config.enabled is False
    assert config.shadow_only is True
    assert config.after_raw_message_id == 2**63 - 1
    assert config.batch_limit == 50


@pytest.mark.parametrize("watermark", ("bad", "-1", str(2**63)))
def test_supervisor_config_malformed_watermark_fails_closed(watermark):
    config = load_message_operation_supervisor_config(
        environ={
            "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_ENABLED": "true",
            "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_AFTER_RAW_MESSAGE_ID": watermark,
            "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_BATCH_LIMIT": "1000",
        },
        env_file_paths=[],
    )

    assert config.enabled is True
    assert config.after_raw_message_id == 2**63 - 1
    assert config.batch_limit == 100


def test_shadow_cli_refuses_non_shadow_configuration(tmp_path, monkeypatch):
    database = tmp_path / "not-shadow.db"
    create_session_factory(database)
    monkeypatch.setenv(
        "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_ENABLED", "true"
    )
    monkeypatch.setenv(
        "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_SHADOW_ONLY", "false"
    )

    result = CliRunner().invoke(
        app,
        [
            "message-operation-supervisor",
            "--database-path",
            str(database),
            "--shadow",
            "--once",
        ],
    )

    assert result.exit_code != 0
    with create_session_factory(database)() as session:
        assert session.query(MessageOperationContract).count() == 0


def test_shadow_cli_requires_enablement_explicit_shadow_once_and_existing_db(
    tmp_path,
    monkeypatch,
):
    missing = tmp_path / "missing.db"
    result = CliRunner().invoke(
        app,
        [
            "message-operation-supervisor",
            "--database-path",
            str(missing),
            "--shadow",
            "--once",
        ],
    )
    assert result.exit_code == 0
    assert '"status":"disabled"' in result.stdout
    assert not missing.exists()

    database = tmp_path / "enabled.db"
    session_factory = create_session_factory(database)
    raw_message_id = _message(
        session_factory,
        message_id=113,
        event_type="position_update",
        actions=("partial_take_profit",),
    )
    monkeypatch.setenv(
        "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_ENABLED", "true"
    )
    monkeypatch.setenv(
        "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_SHADOW_ONLY", "true"
    )
    monkeypatch.setenv(
        "TELEGRAM_KOL_MESSAGE_OPERATION_SUPERVISOR_AFTER_RAW_MESSAGE_ID",
        str(raw_message_id - 1),
    )

    refused = CliRunner().invoke(
        app,
        ["message-operation-supervisor", "--database-path", str(database)],
    )
    assert refused.exit_code != 0

    accepted = CliRunner().invoke(
        app,
        [
            "message-operation-supervisor",
            "--database-path",
            str(database),
            "--shadow",
            "--once",
        ],
    )
    assert accepted.exit_code == 0
    payload = json.loads(accepted.stdout)
    assert payload["status"] == "shadow"
    assert payload["contracts_created"] == 1
    assert payload["model_calls"] == 0
