"""Review fixes from the first real shadow proposal (P2, raw 19598).

1. A6c: a remediation must repeat what the main chain meant to do.
2. An apply() error after promotion with zero write evidence is ``failed``,
   not ``uncertain`` (it must not trip the auto suspension).
3. D1 reads the reason code out of ``error_json.message``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

import telegram_kol_research.oncall_remediation as remediation
import telegram_kol_research.oncall_remediation_auto as auto
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionEvent,
    PositionMutationIntent,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
)
from telegram_kol_research.oncall_remediation import (
    compute_requested_proposal,
    register_proposal_request,
)
from tests.oncall_remediation_fixtures import NOW, build_ready_remediation_target, client_for
from tests.test_oncall_remediation_end_to_end import (
    _config,
    _disable_planner_reconciliation,
    _enable_live_management,
    _group_config,
)

RAW_19598_TEXT = "比特币目前浮盈700点左右，上移止损83000附近做好成本保护，以太坊多单关注2700能否突破。"


def _candidate(management_action, event_type="position_update"):
    return SimpleNamespace(management_action=management_action, event_type=event_type)


@pytest.mark.parametrize(
    ("management_action", "event_type", "expected"),
    [
        ("move_stop_to_break_even", "position_update", {"move_stop_to_break_even"}),
        ("move_stop_to_protect", "position_update", {"move_stop_to_break_even"}),
        ("adjust_stop_loss", "position_update", {"adjust_stop_loss"}),
        ("risk_update", "position_update", {"adjust_stop_loss"}),
        ("exit_full", "close_signal", {"full_exit"}),
        ("full_exit", "close_signal", {"full_exit"}),
        ("exit_partial", "position_update", {"partial_take_profit"}),
        ("partial_take_profit", "position_update", {"partial_take_profit"}),
        # partial + protect = partial_then_break_even, not whitelisted
        ("partial_take_profit,move_stop_to_protect", "position_update", set()),
        (None, "close_signal", {"full_exit"}),
    ],
)
def test_candidate_intent_mapping(management_action, event_type, expected):
    assert remediation._intents_allowed_by_candidate(_candidate(management_action, event_type)) == frozenset(expected)


@pytest.mark.parametrize("management_action", [None, "hold_update", "something_new"])
def test_unmappable_candidate_intent_is_unverifiable(management_action):
    assert remediation._intents_allowed_by_candidate(_candidate(management_action)) is None


def test_raw_19598_break_even_with_explicit_price_is_refused_never_83000(tmp_path, monkeypatch):
    """Replay: the main chain meant break-even (strategy price 83150 by the
    user's rule); the remediation plan re-derives adjust_stop_loss -> 83000
    from the text. The proposal must be refused, never offered at 83000."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, _sid, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory,
        action_kind="move_stop_to_break_even",
        action_text=RAW_19598_TEXT,
        current_stop_loss_text="83000",
        verified_stop_price="62000",
    )
    _enable_live_management(session_factory)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    registered = register_proposal_request(
        session_factory, config=_config(), case_key="mgmt:19598:move_stop_to_break_even",
        case_no=32, raw_message_id=raw_id, now=NOW,
    )
    outcome = compute_requested_proposal(
        session_factory, config=_config(), proposal_id=registered.proposal_id, deepcoin_client=client,
        group_config=_group_config(88), now=NOW + timedelta(minutes=1),
    )
    assert outcome.state == "refused"
    assert outcome.refusal_reason == "intent_diverges_from_main_chain"
    assert outcome.keyboard is None
    assert "83000" not in (outcome.text or "")
    assert client.close_calls == client.set_calls == []


def test_main_chain_batch_intent_overrides(tmp_path, monkeypatch):
    """Even when the candidate would allow it, a main-chain batch for the same
    message and target with a different intent refuses the remediation."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, _sid, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="full_exit",
    )
    action = SimpleNamespace(
        raw_message_id=raw_id, lifecycle_id=lifecycle_id, action_kind="full_exit", evidence={"candidate_id": None},
    )
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="i" * 64, raw_message_id=raw_id, recognition_decision_id=1,
                recognition_generation="main-gen", target_lifecycle_id=lifecycle_id,
                strategy_instance_id="s", execution_binding_id=lifecycle.execution_binding_id,
                intent="partial_take_profit", effective_action="partial_close", status="blocked",
                execution_mode="live", target_snapshot_json="{}", target_fingerprint="t" * 64,
                planned_at=NOW, updated_at=NOW,
            )
        )
        session.commit()
    assert remediation._main_chain_intent_refusal(session_factory, action=action) == "intent_diverges_from_main_chain"


# --------------------------------------------------------------------------
# 2. failed vs uncertain after promotion
# --------------------------------------------------------------------------


def _live_batch(session_factory, *, raw_id, lifecycle_id, status="blocked"):
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        batch = StrategyManagementBatch(
            idempotency_fingerprint="b" * 64, raw_message_id=raw_id, recognition_decision_id=1,
            recognition_generation="remediation:x", target_lifecycle_id=lifecycle_id,
            strategy_instance_id="s", execution_binding_id=lifecycle.execution_binding_id,
            intent="move_stop_to_break_even", effective_action="break_even_by_market", status=status,
            reason_code="protection_rows_unattributed_on_exchange", execution_mode="live",
            target_snapshot_json="{}", target_fingerprint="t" * 64,
            planned_at=NOW, updated_at=NOW + timedelta(seconds=5),
        )
        session.add(batch)
        session.commit()
        return batch.id, lifecycle.execution_binding_id


def _setup(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = build_ready_remediation_target(session_factory, action_kind="move_stop_to_break_even")
    return session_factory, raw_id, lifecycle_id


def test_blocked_live_batch_with_zero_writes_is_failed(tmp_path):
    session_factory, raw_id, lifecycle_id = _setup(tmp_path)
    _live_batch(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id)
    assert remediation._classify_apply_exception(session_factory, raw_message_id=raw_id, executing_at=NOW) == "failed"


def test_no_live_batch_is_failed(tmp_path):
    session_factory, raw_id, _lifecycle_id = _setup(tmp_path)
    assert remediation._classify_apply_exception(session_factory, raw_message_id=raw_id, executing_at=NOW) == "failed"


def test_live_batch_in_any_other_state_is_uncertain(tmp_path):
    session_factory, raw_id, lifecycle_id = _setup(tmp_path)
    _live_batch(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, status="submit_unknown")
    assert remediation._classify_apply_exception(session_factory, raw_message_id=raw_id, executing_at=NOW) == "uncertain"


def test_blocked_batch_with_a_leg_request_is_uncertain(tmp_path):
    session_factory, raw_id, lifecycle_id = _setup(tmp_path)
    batch_id, _binding_id = _live_batch(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id)
    real = remediation._batch_has_write_evidence
    remediation._batch_has_write_evidence = lambda *a, **k: True
    try:
        assert remediation._classify_apply_exception(session_factory, raw_message_id=raw_id, executing_at=NOW) == "uncertain"
    finally:
        remediation._batch_has_write_evidence = real


def test_write_evidence_flips_on_a_leg_with_a_request(tmp_path):
    from telegram_kol_research.models import ExecutionOrderLeg

    session_factory, raw_id, lifecycle_id = _setup(tmp_path)
    batch_id, binding_id = _live_batch(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id)
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        assert remediation._batch_has_write_evidence(session, batch=batch, since=NOW) is False
    assert remediation._classify_apply_exception(session_factory, raw_message_id=raw_id, executing_at=NOW) == "failed"

    with session_factory() as session:
        entry_leg = session.query(ExecutionOrderLeg).filter(ExecutionOrderLeg.execution_binding_id == binding_id).first()
        session.add(
            StrategyManagementLeg(
                management_batch_id=batch_id, execution_order_leg_id=entry_leg.id, pos_id=str(entry_leg.pos_id),
                leg_index=0, status="blocked", request_json=json.dumps({"instId": "BTC-USDT-SWAP"}),
                created_at=NOW, updated_at=NOW,
            )
        )
        session.commit()
    with session_factory() as session:
        batch = session.get(StrategyManagementBatch, batch_id)
        assert remediation._batch_has_write_evidence(session, batch=batch, since=NOW) is True
    assert remediation._classify_apply_exception(session_factory, raw_message_id=raw_id, executing_at=NOW) == "uncertain"


def test_evidence_columns_exist():
    for model, columns in (
        (StrategyManagementLeg, {"management_batch_id", "request_json", "exchange_order_id"}),
        (PositionMutationIntent, {"execution_binding_id", "created_at"}),
        (ExecutionEvent, {"execution_binding_id", "created_at", "request_json"}),
    ):
        assert columns <= set(model.__table__.columns.keys()), model


def test_evidence_read_error_is_uncertain(tmp_path, monkeypatch):
    session_factory, raw_id, lifecycle_id = _setup(tmp_path)
    _live_batch(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id)

    def boom(*args, **kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(remediation, "_batch_has_write_evidence", boom)
    assert remediation._classify_apply_exception(session_factory, raw_message_id=raw_id, executing_at=NOW) == "uncertain"


# --------------------------------------------------------------------------
# 3. D1 reads error_json.message
# --------------------------------------------------------------------------


def test_d1_reads_reason_code_from_message():
    blob = json.dumps({"message": "protection_rows_unattributed_on_exchange:1001:2002,3003", "type": "ManagementBatchExecutionError"})
    assert auto._extract_reasons_from_blob(blob) == {"protection_rows_unattributed_on_exchange"}


def test_d1_transient_code_in_message_is_recognised():
    blob = json.dumps({"message": "close_final_preflight_failed:detail", "type": "X"})
    assert auto._extract_reasons_from_blob(blob) == {"close_final_preflight_failed"}


def test_d1_free_text_message_stays_whole_and_non_transient():
    blob = json.dumps({"message": "Something Weird happened"})
    assert auto._extract_reasons_from_blob(blob) == {"Something Weird happened"}
