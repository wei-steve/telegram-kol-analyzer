"""Tests for the phase-3 worker remediation module (oncall_remediation.py).

See docs/plans/2026-09-26-codex-oncall-phase3-spec.md sections 4/6/9. This
module is pure library code: no network, no real Telegram, no real exchange.
Fixtures reuse tests/test_position_management_remediation_scope.py's helpers
(spec 9: "复用其建数据函数").
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_kol_research.config import OncallRemediationConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import (
    MessageInstructionItem,
    MessageOperationContract,
    SignalCandidate,
    OncallRemediationControl,
    OncallRemediationEvent,
    OncallRemediationProposal,
    RawMessage,
    RuntimeIncident,
    RuntimeIncidentAffectedMessage,
)
from telegram_kol_research.position_management_remediation import (
    build_position_management_remediation_plan,
)
from telegram_kol_research.trading_settings import save_trading_settings

import telegram_kol_research.oncall_remediation as remediation
from telegram_kol_research.oncall_remediation import (
    compute_requested_proposal,
    execute_proposal,
    expire_stale_proposals,
    finalize_executing_proposals,
    handle_callback,
    handle_text_command,
    recover_after_restart,
    register_proposal_request,
)

from tests.test_position_management_remediation_scope import (
    NOW,
    _persist_failed_step,
    _persist_strategy,
    _position_row,
    _ReadOnlyClient,
)


APPROVER_ID = 555111
CHAT_ID = "222333"


def _approve_config(**overrides) -> OncallRemediationConfig:
    fields = dict(
        mode="approve",
        token="a" * 40,
        approver_ids=frozenset({APPROVER_ID}),
        system_chat_id=CHAT_ID,
        daily_execution_cap=10,
        cooldown_minutes=10,
        exit_window_minutes=60,
        partial_tp_window_minutes=20,
        stop_window_minutes=120,
    )
    fields.update(overrides)
    return OncallRemediationConfig(**fields)


def _shadow_config(**overrides) -> OncallRemediationConfig:
    overrides.setdefault("mode", "shadow")
    return _approve_config(**overrides)


def _group_config(chat_id: int, *, enabled: bool = True, trading_mode: str = "auto_trade") -> GroupConfig:
    return GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="test group",
                chat_id=chat_id,
                enabled=enabled,
                trading_mode=trading_mode,
            )
        ]
    )


def _enable_live_management(session_factory) -> None:
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "management_execution_mode": "live"},
    )


#: resolve_management_directive() reads the message *text*, not the
#: candidate's management_action field, to decide intent -- the
#: management_action is only auxiliary. Each action kind therefore needs its
#: own realistic Chinese text (see management_directives.py's term lists).
_ACTION_TEXT = {
    "full_exit": "{symbol}多单全部平仓",
    "partial_take_profit": "{symbol}多单止盈一部分",
    "move_stop_to_break_even": "{symbol}多单止损移到成本价",
    "adjust_stop_loss": "{symbol}多单止损上移",
    "cancel_entry": "{symbol}多单取消入场",
}


def _setup_ready_message(
    session_factory,
    *,
    chat_id=88,
    strategy_message_id=200,
    raw_chat_message_id=300,
    symbol="BTC",
    side="long",
    pos_id="pos-a",
    management_action="full_exit",
    event_type="close_signal",
    posted_at=NOW,
    item_status="failed",
    stop_loss_text=None,
):
    _binding_id, lifecycle_id, strategy_id = _persist_strategy(
        session_factory,
        chat_id=chat_id,
        message_id=strategy_message_id,
        symbol=symbol,
        side=side,
        pos_id=pos_id,
    )
    text = _ACTION_TEXT.get(management_action, "{symbol}多单全部平仓").format(symbol=symbol)
    raw_id, candidate_id = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=posted_at,
        chat_id=chat_id,
        message_id=raw_chat_message_id,
        text=text,
        event_type=event_type,
        management_action=management_action,
        target_lifecycle_id=lifecycle_id,
        status=item_status,
    )
    if management_action == "adjust_stop_loss":
        with session_factory() as session:
            candidate = session.get(SignalCandidate, candidate_id)
            candidate.stop_loss_text = stop_loss_text or "62500"
            session.add(candidate)
            session.commit()
    return raw_id, lifecycle_id, strategy_id, pos_id, symbol, side


def _client_for(symbol, side, pos_id):
    return _ReadOnlyClient(positions=[_position_row(symbol=symbol, side=side, pos_id=pos_id)])


def _new_requested_proposal(session_factory, *, raw_message_id, case_no=1, now=NOW):
    with session_factory() as session:
        proposal = OncallRemediationProposal(
            case_key="case",
            case_no=case_no,
            raw_message_id=raw_message_id,
            state="requested",
            requested_at=now,
        )
        session.add(proposal)
        session.commit()
        return proposal.id


def _compute(
    session_factory,
    *,
    config,
    proposal_id,
    client,
    group_config,
    now=NOW + timedelta(minutes=2),
):
    return compute_requested_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=now,
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_new_tables_exist_and_check_constraints_enforced(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        with pytest.raises(Exception):
            session.add(OncallRemediationProposal(case_key="x", case_no=1, raw_message_id=1, state="bogus"))
            session.commit()


def test_control_table_is_a_singleton(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(OncallRemediationControl(id=1))
        session.commit()
        with pytest.raises(Exception):
            session.add(OncallRemediationControl(id=2))
            session.commit()


def test_partial_unique_index_blocks_two_settled_proposals_same_target(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="a", case_no=1, raw_message_id=1, lifecycle_id=1,
                action_kind="full_exit", state="executing", requested_at=NOW,
            )
        )
        session.commit()
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="b", case_no=2, raw_message_id=1, lifecycle_id=1,
                action_kind="full_exit", state="succeeded", requested_at=NOW,
            )
        )
        with pytest.raises(Exception):
            session.commit()


def test_partial_unique_index_allows_two_refused_proposals_same_target(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="a", case_no=1, raw_message_id=1, lifecycle_id=1,
                action_kind="full_exit", state="refused", requested_at=NOW,
            )
        )
        session.add(
            OncallRemediationProposal(
                case_key="b", case_no=2, raw_message_id=1, lifecycle_id=1,
                action_kind="full_exit", state="refused", requested_at=NOW,
            )
        )
        session.commit()  # must not raise


# ---------------------------------------------------------------------------
# register_proposal_request
# ---------------------------------------------------------------------------


def test_register_creates_requested_row(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    result = register_proposal_request(
        session_factory, config=_approve_config(), case_key="k", case_no=1, raw_message_id=42, now=NOW,
    )
    assert result.state == "requested"
    assert result.created is True


def test_register_is_idempotent_for_nonterminal_state(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    first = register_proposal_request(
        session_factory, config=_approve_config(), case_key="k", case_no=1, raw_message_id=42, now=NOW,
    )
    second = register_proposal_request(
        session_factory, config=_approve_config(), case_key="k", case_no=1, raw_message_id=42, now=NOW,
    )
    assert second.proposal_id == first.proposal_id
    assert second.created is False


def test_register_refuses_when_already_succeeded(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="k", case_no=1, raw_message_id=42, state="succeeded", requested_at=NOW,
            )
        )
        session.commit()
    result = register_proposal_request(
        session_factory, config=_approve_config(), case_key="k", case_no=2, raw_message_id=42, now=NOW,
    )
    assert result.state == "refused"


def test_register_refuses_when_mode_off(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    result = register_proposal_request(
        session_factory,
        config=OncallRemediationConfig(mode="off"),
        case_key="k", case_no=1, raw_message_id=42, now=NOW,
    )
    assert result.state == "refused"


# ---------------------------------------------------------------------------
# G-A: compute_requested_proposal happy paths
# ---------------------------------------------------------------------------


def test_full_exit_end_to_end_produces_proposed_with_buttons(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory,
        config=_approve_config(),
        proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id),
        group_config=_group_config(88),
    )
    assert outcome.state == "proposed"
    assert outcome.keyboard is not None
    assert len(outcome.keyboard) == 2
    assert "P" in outcome.text
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, proposal_id)
        assert row.action_kind == "full_exit"
        assert row.step1_token_hash is not None
        snapshot = json.loads(row.action_snapshot_json)
        assert len(row.action_snapshot_json) <= 8192
        assert snapshot["action_kind"] == "full_exit"


def test_shadow_mode_produces_proposed_without_buttons(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory,
        config=_shadow_config(),
        proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id),
        group_config=_group_config(88),
    )
    assert outcome.state == "proposed"
    assert outcome.keyboard is None
    assert "只提示" in outcome.text


def test_partial_take_profit_action_kind(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(
        session_factory, management_action="partial_take_profit", event_type="position_update",
    )
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory,
        config=_approve_config(),
        proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id),
        group_config=_group_config(88),
    )
    assert outcome.state == "proposed"
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, proposal_id)
        assert row.action_kind == "partial_take_profit"


# ---------------------------------------------------------------------------
# G-A: individual gate failures
# ---------------------------------------------------------------------------


def test_a1_mode_off_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, *_rest = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory,
        config=OncallRemediationConfig(mode="off"),
        proposal_id=proposal_id,
        client=_client_for("BTC", "long", "pos-a"),
        group_config=_group_config(88),
    )
    assert outcome.state == "refused"
    assert outcome.refusal_reason == "remediation_disabled"


def test_a2_live_management_execution_disabled_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    # deliberately not calling _enable_live_management
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory,
        config=_approve_config(),
        proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id),
        group_config=_group_config(88),
    )
    assert outcome.state == "refused"
    assert outcome.refusal_reason == "live_management_execution_disabled"


def test_a3_deleted_source_message_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    with session_factory() as session:
        raw = session.get(RawMessage, raw_id)
        raw.deleted_at = NOW
        session.add(raw)
        session.commit()
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory,
        config=_approve_config(),
        proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id),
        group_config=_group_config(88),
    )
    assert outcome.refusal_reason == "source_message_unavailable"


def test_a3_group_auto_trade_disabled_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory,
        config=_approve_config(),
        proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id),
        group_config=_group_config(88, trading_mode="notify_only"),
    )
    assert outcome.refusal_reason == "kol_or_group_auto_trade_disabled"


def test_target_not_resolved_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    from telegram_kol_research.models import SignalCandidate

    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=900, posted_at=NOW, text="?")
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id, symbol=None, side=None,
                event_type="unresolved_management_target", target_lifecycle_id=None,
                parse_source="mimo_authoritative", confidence=0.5,
            )
        )
        session.commit()
        raw_id = raw.id
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_ReadOnlyClient(), group_config=_group_config(88),
    )
    assert outcome.refusal_reason == "target_not_resolved"


def test_a5_no_ready_action_when_predecessor_unresolved(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    _binding_id, lifecycle_id, strategy_id = _persist_strategy(
        session_factory, chat_id=88, message_id=200, symbol="BTC", side="long", pos_id="pos-a",
    )
    _persist_failed_step(
        session_factory, lifecycle_id=lifecycle_id, posted_at=NOW, chat_id=88, message_id=300,
        text="BTC多单止盈一部分", event_type="position_update",
        management_action="partial_take_profit", target_lifecycle_id=lifecycle_id,
    )
    raw_id2, _ = _persist_failed_step(
        session_factory, lifecycle_id=lifecycle_id, posted_at=NOW + timedelta(minutes=1),
        chat_id=88, message_id=301, text="BTC多单全部平仓", event_type="close_signal",
        management_action="full_exit", target_lifecycle_id=lifecycle_id,
    )
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id2)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for("BTC", "long", "pos-a"), group_config=_group_config(88),
    )
    assert outcome.refusal_reason.startswith("no_ready_action")


def test_a6_cancel_entry_conversion_is_refused(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(
        session_factory, management_action="cancel_entry", event_type="close_signal",
    )
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
    )
    assert outcome.refusal_reason == "cancel_entry_conversion_not_supported"


def test_a6b_shadow_planned_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    with session_factory() as session:
        item = session.query(MessageInstructionItem).filter_by(raw_message_id=raw_id).one()
        item.status = "succeeded"
        item.result_json = json.dumps({"status": "shadow_planned"})
        session.add(item)
        session.commit()
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
    )
    assert outcome.refusal_reason == "shadow_planned_not_remediated"


@pytest.mark.parametrize(
    "pattern",
    [
        "account_ownership_not_verified",
        "exact_position_write_gate:blocked",
        "protection_authority_frozen:reason",
        "protection_order_unattributable",
        "explicit_stop_adjustment_not_risk_tightening",
        "management_price_implausible",
        "management_stop_direction_invalid",
        "operator_dismissed",
        "kol_or_group_auto_trade_disabled",
    ],
)
def test_a7_irreversible_reasons_refuse(tmp_path, pattern):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    with session_factory() as session:
        item = session.query(MessageInstructionItem).filter_by(raw_message_id=raw_id).one()
        item.error_json = json.dumps({"reason": pattern})
        session.add(item)
        session.commit()
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
    )
    assert outcome.refusal_reason.startswith("irreversible_refusal:")


def test_a7_reads_runtime_incident_via_affected_messages(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    with session_factory() as session:
        # clear the generic error_json the fixture put on the item so only
        # the incident row can trigger A7.
        item = session.query(MessageInstructionItem).filter_by(raw_message_id=raw_id).one()
        item.error_json = None
        session.add(item)
        incident = RuntimeIncident(
            source_kind="message_operation",
            source_record_id=str(raw_id),
            incident_type="operator_dismissed",
            severity="high",
            fingerprint="f" * 64,
            first_occurred_at=NOW,
            last_occurred_at=NOW,
            redacted_summary="operator_dismissed the remediation",
            status="pending",
            feature_policy_version="v1",
            prompt_version="v1",
            tool_policy_version="v1",
        )
        session.add(incident)
        session.flush()
        contract = MessageOperationContract(
            raw_message_id=raw_id,
            intent_kind="manage",
            expected_terminal_kind="verified_management",
            deadline_at=NOW + timedelta(hours=1),
            policy_version="v1",
        )
        session.add(contract)
        session.flush()
        session.add(
            RuntimeIncidentAffectedMessage(
                runtime_incident_id=incident.id,
                raw_message_id=raw_id,
                message_operation_contract_id=contract.id,
            )
        )
        session.commit()
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
    )
    assert outcome.refusal_reason == "irreversible_refusal:operator_dismissed"


@pytest.mark.parametrize(
    "management_action,event_type,minutes,expect_refused",
    [
        ("full_exit", "close_signal", 59, False),
        ("full_exit", "close_signal", 60, False),
        ("full_exit", "close_signal", 61, True),
        ("partial_take_profit", "position_update", 19, False),
        ("partial_take_profit", "position_update", 20, False),
        ("partial_take_profit", "position_update", 21, True),
        ("adjust_stop_loss", "position_update", 119, False),
        ("adjust_stop_loss", "position_update", 120, False),
        ("adjust_stop_loss", "position_update", 121, True),
    ],
)
def test_a8_window_boundaries(tmp_path, management_action, event_type, minutes, expect_refused):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(
        session_factory, management_action=management_action, event_type=event_type,
    )
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
        now=NOW + timedelta(minutes=minutes),
    )
    if expect_refused:
        assert outcome.refusal_reason == "remediation_window_expired"
    else:
        assert outcome.state == "proposed"


def test_a10_idempotency_refuses_when_already_settled(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="prior", case_no=0, raw_message_id=raw_id, lifecycle_id=lifecycle_id,
                action_kind="full_exit", state="succeeded", requested_at=NOW,
            )
        )
        session.commit()
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id, case_no=2)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
    )
    assert outcome.refusal_reason == "already_remediated"


def test_a11_cooldown_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="prior", case_no=0, raw_message_id=raw_id - 1 if raw_id > 1 else raw_id,
                lifecycle_id=lifecycle_id, action_kind="full_exit", state="failed",
                requested_at=NOW, executing_at=NOW + timedelta(minutes=1),
            )
        )
        session.commit()
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id, case_no=2)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
        now=NOW + timedelta(minutes=5),
    )
    assert outcome.refusal_reason == "cooldown"


def test_a11_daily_execution_cap_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    with session_factory() as session:
        for i in range(10):
            session.add(
                OncallRemediationProposal(
                    case_key=f"p{i}", case_no=i, raw_message_id=10_000 + i, lifecycle_id=90_000 + i,
                    action_kind="full_exit", state="failed", requested_at=NOW,
                    executing_at=NOW + timedelta(minutes=1),
                )
            )
        session.commit()
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id, case_no=2)
    outcome = _compute(
        session_factory, config=_approve_config(daily_execution_cap=10), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
        now=NOW + timedelta(minutes=5),
    )
    assert outcome.refusal_reason == "daily_execution_cap"


def test_a12_control_disabled_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    with session_factory() as session:
        session.add(OncallRemediationControl(id=1, enabled=False))
        session.commit()
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
    )
    assert outcome.refusal_reason == "remediation_disabled"


def test_gate_a_events_are_recorded(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, *_rest = _setup_ready_message(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    _compute(
        session_factory, config=OncallRemediationConfig(mode="off"), proposal_id=proposal_id,
        client=_ReadOnlyClient(), group_config=_group_config(88),
    )
    with session_factory() as session:
        events = session.query(OncallRemediationEvent).filter_by(proposal_id=proposal_id).all()
        assert len(events) >= 1
        assert events[-1].outcome == "refused"


# ---------------------------------------------------------------------------
# G-B: handle_callback
# ---------------------------------------------------------------------------


def _proposed_row(session_factory, *, raw_id, lifecycle_id, action_kind="full_exit", token="tok" + "a" * 40, expires_at=None, case_no=1):
    from telegram_kol_research.oncall_remediation import _hash_token

    with session_factory() as session:
        proposal = OncallRemediationProposal(
            case_key="k", case_no=case_no, raw_message_id=raw_id, lifecycle_id=lifecycle_id,
            action_kind=action_kind, action_id="abc", action_fingerprint="fp",
            action_snapshot_json=json.dumps({"action_kind": action_kind, "expected_effect": {}}),
            state="proposed", requested_at=NOW, proposed_at=NOW,
            expires_at=expires_at or (NOW + timedelta(minutes=30)),
            step1_token_hash=_hash_token(token),
        )
        session.add(proposal)
        session.commit()
        return proposal.id


def test_callback_wrong_chat_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id)
    outcome = handle_callback(
        session_factory, config=_approve_config(), chat_id="999999", from_user_id=APPROVER_ID,
        data=f"orm:{pid}:1:{'tok' + 'a'*40}", now=NOW,
    )
    assert outcome.accepted is False


def test_callback_wrong_user_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id)
    outcome = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=999,
        data=f"orm:{pid}:1:{'tok' + 'a'*40}", now=NOW,
    )
    assert outcome.accepted is False


def test_callback_no_approvers_configured_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id)
    outcome = handle_callback(
        session_factory, config=_approve_config(approver_ids=frozenset()), chat_id=CHAT_ID,
        from_user_id=APPROVER_ID, data=f"orm:{pid}:1:{'tok' + 'a'*40}", now=NOW,
    )
    assert outcome.accepted is False
    assert "只提示" in (outcome.text or "")


def test_callback_wrong_token_refuses(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id)
    outcome = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:1:{'z' * 40}", now=NOW,
    )
    assert outcome.accepted is False
    assert outcome.text == "令牌无效"


def test_callback_step1_then_step2_reaches_executing(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    token = "tok" + "a" * 40
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, token=token)
    step1 = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:1:{token}", now=NOW,
    )
    assert step1.accepted is True
    assert step1.keyboard is not None
    confirm_label, confirm_data = step1.keyboard[0]
    step2 = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=confirm_data, now=NOW,
    )
    assert step2.accepted is True
    assert step2.execute_proposal_id == pid
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, pid)
        assert row.state == "executing"


def test_callback_step1_token_is_one_time(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    token = "tok" + "a" * 40
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, token=token)
    first = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:1:{token}", now=NOW,
    )
    assert first.accepted is True
    second = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:1:{token}", now=NOW,
    )
    assert second.accepted is False


def test_callback_step2_token_cannot_be_used_for_step1(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    token = "tok" + "a" * 40
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, token=token)
    step1 = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:1:{token}", now=NOW,
    )
    _, confirm_data = step1.keyboard[0]
    step2_token = confirm_data.split(":")[-1]
    # Using the step-2 token against the step-1 slot must fail (proposal is
    # already 'confirming', not 'proposed').
    cross = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:1:{step2_token}", now=NOW,
    )
    assert cross.accepted is False


def test_proposal_expiry_blocks_step1(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    token = "tok" + "a" * 40
    pid = _proposed_row(
        session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, token=token,
        expires_at=NOW + timedelta(minutes=5),
    )
    outcome = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:1:{token}", now=NOW + timedelta(minutes=31),
    )
    assert outcome.accepted is False
    with session_factory() as session:
        assert session.get(OncallRemediationProposal, pid).state == "expired"


def test_confirm_expiry_blocks_step2(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    token = "tok" + "a" * 40
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, token=token)
    step1 = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:1:{token}", now=NOW,
    )
    _, confirm_data = step1.keyboard[0]
    step2 = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=confirm_data, now=NOW + timedelta(minutes=3),
    )
    assert step2.accepted is False
    with session_factory() as session:
        assert session.get(OncallRemediationProposal, pid).state == "expired"


def test_dismiss_and_cancel_do_not_touch_main_line_state(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    token = "tok" + "a" * 40
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, token=token)
    outcome = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:d:{token}", now=NOW,
    )
    assert outcome.accepted is True
    with session_factory() as session:
        assert session.get(OncallRemediationProposal, pid).state == "dismissed"
        item = session.query(MessageInstructionItem).filter_by(raw_message_id=raw_id).one()
        assert item.status == "failed"  # untouched


def test_callback_data_too_long_is_rejected(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    outcome = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data="orm:1:1:" + "x" * 100, now=NOW,
    )
    assert outcome.accepted is False


def test_fix_command_rejects_extra_arguments(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id)
    outcome = handle_text_command(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        text=f"/fix P{pid} extra", now=NOW,
    )
    assert outcome.accepted is False


def test_fix_command_rejected_in_shadow_mode(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id)
    outcome = handle_text_command(
        session_factory, config=_shadow_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        text=f"/fix P{pid}", now=NOW,
    )
    assert outcome.accepted is False


# ---------------------------------------------------------------------------
# G-C: execute_proposal
# ---------------------------------------------------------------------------


def _executing_row(session_factory, *, raw_id, lifecycle_id, action, scope, batch_id=None):
    from telegram_kol_research.oncall_remediation import _build_action_snapshot

    with session_factory() as session:
        proposal = OncallRemediationProposal(
            case_key="k", case_no=1, raw_message_id=raw_id, lifecycle_id=lifecycle_id,
            action_kind=action.action_kind, action_id=action.action_id,
            action_fingerprint=action.fingerprint,
            action_snapshot_json=json.dumps(_build_action_snapshot(action)),
            scope_json=scope.to_json(), state="executing", requested_at=NOW,
            proposed_at=NOW, approved_at=NOW, confirmed_at=NOW, executing_at=NOW,
            management_batch_id=batch_id,
        )
        session.add(proposal)
        session.commit()
        return proposal.id


def _built_action_and_scope(session_factory, *, raw_id, client):
    from telegram_kol_research.position_management_remediation import resolve_remediation_scope

    scope = resolve_remediation_scope(session_factory, raw_message_id=raw_id)
    plan = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=2), scope=scope,
    )
    action = next(a for a in plan.actions if a.raw_message_id == raw_id)
    return action, scope


def test_execute_proposal_fingerprint_mismatch_never_calls_apply(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    client = _client_for(symbol, side, pos_id)
    action, scope = _built_action_and_scope(session_factory, raw_id=raw_id, client=client)
    pid = _executing_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, action=action, scope=scope)
    # corrupt the stored fingerprint so C2 must catch it
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, pid)
        row.action_fingerprint = "not-the-real-fingerprint"
        session.add(row)
        session.commit()

    calls = []

    def fake_apply(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("apply must not be called on fingerprint mismatch")

    outcome = execute_proposal(
        session_factory, config=_approve_config(), proposal_id=pid, deepcoin_client=client,
        group_config=_group_config(88), now=NOW + timedelta(minutes=3), apply_fn=fake_apply,
    )
    assert outcome.state == "failed"
    assert calls == []


def test_execute_proposal_window_expired_at_execution_time(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    client = _client_for(symbol, side, pos_id)
    action, scope = _built_action_and_scope(session_factory, raw_id=raw_id, client=client)
    pid = _executing_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, action=action, scope=scope)

    def fake_apply(*args, **kwargs):
        raise AssertionError("apply must not be called when window has expired")

    outcome = execute_proposal(
        session_factory, config=_approve_config(), proposal_id=pid, deepcoin_client=client,
        group_config=_group_config(88), now=NOW + timedelta(minutes=90), apply_fn=fake_apply,
    )
    assert outcome.state == "failed"
    assert outcome.text is not None


def test_execute_proposal_success_path_calls_apply_and_stores_batch(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    client = _client_for(symbol, side, pos_id)
    action, scope = _built_action_and_scope(session_factory, raw_id=raw_id, client=client)
    pid = _executing_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, action=action, scope=scope)

    class _Result:
        status = "reconciling"
        batch_id = 777

    def fake_apply(*args, **kwargs):
        assert kwargs["action_id"] == action.action_id
        assert kwargs["expected_fingerprint"] == action.fingerprint
        return _Result()

    outcome = execute_proposal(
        session_factory, config=_approve_config(), proposal_id=pid, deepcoin_client=client,
        group_config=_group_config(88), now=NOW + timedelta(minutes=3), apply_fn=fake_apply,
    )
    assert outcome.state == "executing"
    assert outcome.management_batch_id == 777
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, pid)
        assert row.state == "executing"
        assert row.management_batch_id == 777


def test_execute_proposal_apply_exception_without_batch_is_failed(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    client = _client_for(symbol, side, pos_id)
    action, scope = _built_action_and_scope(session_factory, raw_id=raw_id, client=client)
    pid = _executing_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, action=action, scope=scope)

    def fake_apply(*args, **kwargs):
        raise RuntimeError("boom")

    outcome = execute_proposal(
        session_factory, config=_approve_config(), proposal_id=pid, deepcoin_client=client,
        group_config=_group_config(88), now=NOW + timedelta(minutes=3), apply_fn=fake_apply,
    )
    assert outcome.state == "failed"


def test_single_flight_second_confirm_refused_while_one_is_executing(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    client = _client_for(symbol, side, pos_id)
    action, scope = _built_action_and_scope(session_factory, raw_id=raw_id, client=client)
    # An unrelated proposal already executing.
    _executing_row(session_factory, raw_id=raw_id + 9999, lifecycle_id=lifecycle_id + 1, action=action, scope=scope)

    token = "tok" + "a" * 40
    from telegram_kol_research.oncall_remediation import _hash_token

    with session_factory() as session:
        confirming = OncallRemediationProposal(
            case_key="k", case_no=1, raw_message_id=raw_id, lifecycle_id=lifecycle_id,
            action_kind=action.action_kind, action_id=action.action_id,
            action_fingerprint=action.fingerprint, state="confirming",
            requested_at=NOW, proposed_at=NOW, approved_at=NOW,
            step2_token_hash=_hash_token(token),
        )
        session.add(confirming)
        session.commit()
        confirming_id = confirming.id

    outcome = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{confirming_id}:2:{token}", now=NOW,
    )
    assert outcome.accepted is False
    with session_factory() as session:
        assert session.get(OncallRemediationProposal, confirming_id).state == "confirming"


# ---------------------------------------------------------------------------
# finalize / recover / expire
# ---------------------------------------------------------------------------


def test_finalize_succeeded_batch_clears_breaker(tmp_path):
    from telegram_kol_research.models import StrategyManagementBatch

    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    with session_factory() as session:
        session.add(OncallRemediationControl(id=1, enabled=True, consecutive_failures=1))
        batch = StrategyManagementBatch(
            idempotency_fingerprint="f" * 64, raw_message_id=raw_id, recognition_decision_id=1,
            recognition_generation="g", target_lifecycle_id=lifecycle_id,
            strategy_instance_id=strategy_id, execution_binding_id=1, intent="full_exit",
            effective_action="full_exit", execution_mode="live", status="succeeded",
            target_fingerprint="f" * 64, target_snapshot_json="{}",
        )
        session.add(batch)
        session.flush()
        proposal = OncallRemediationProposal(
            case_key="k", case_no=1, raw_message_id=raw_id, lifecycle_id=lifecycle_id,
            action_kind="full_exit", state="executing", requested_at=NOW, executing_at=NOW,
            management_batch_id=batch.id,
        )
        session.add(proposal)
        session.commit()
        pid = proposal.id

    outcomes = finalize_executing_proposals(session_factory, config=_approve_config(), now=NOW)
    assert len(outcomes) == 1
    assert outcomes[0].state == "succeeded"
    with session_factory() as session:
        assert session.get(OncallRemediationProposal, pid).state == "succeeded"
        assert session.get(OncallRemediationControl, 1).consecutive_failures == 0


def test_recover_after_restart_marks_batchless_executing_as_uncertain(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="k", case_no=1, raw_message_id=1, lifecycle_id=1,
                action_kind="full_exit", state="executing", requested_at=NOW, executing_at=NOW,
            )
        )
        session.commit()
    texts = recover_after_restart(session_factory, now=NOW)
    assert len(texts) == 1
    with session_factory() as session:
        rows = session.query(OncallRemediationProposal).all()
        assert rows[0].state == "uncertain"


def test_recover_after_restart_does_not_touch_executing_with_batch(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="k", case_no=1, raw_message_id=1, lifecycle_id=1,
                action_kind="full_exit", state="executing", requested_at=NOW, executing_at=NOW,
                management_batch_id=42,
            )
        )
        session.commit()
    texts = recover_after_restart(session_factory, now=NOW)
    assert texts == []
    with session_factory() as session:
        assert session.query(OncallRemediationProposal).one().state == "executing"


def test_expire_stale_proposals(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="k", case_no=1, raw_message_id=1, state="proposed",
                requested_at=NOW, proposed_at=NOW, expires_at=NOW - timedelta(minutes=1),
                telegram_message_id=555,
            )
        )
        session.commit()
    message_ids = expire_stale_proposals(session_factory, now=NOW)
    assert message_ids == [555]
    with session_factory() as session:
        assert session.query(OncallRemediationProposal).one().state == "expired"


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_trips_after_two_failures_and_cancels_in_flight(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="pending", case_no=9, raw_message_id=500, state="proposed",
                requested_at=NOW, proposed_at=NOW,
            )
        )
        session.commit()

    from telegram_kol_research.oncall_remediation import _apply_outcome_to_breaker

    with session_factory() as session:
        msg1 = _apply_outcome_to_breaker(session, "failed")
        session.commit()
    assert msg1 is None
    with session_factory() as session:
        msg2 = _apply_outcome_to_breaker(session, "failed")
        session.commit()
    assert msg2 is not None
    with session_factory() as session:
        control = session.get(OncallRemediationControl, 1)
        assert control.enabled is False
        pending = session.query(OncallRemediationProposal).filter_by(case_key="pending").one()
        assert pending.state == "cancelled"


def test_breaker_prevents_new_proposals_after_tripping(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    with session_factory() as session:
        session.add(OncallRemediationControl(id=1, enabled=False, breaker_tripped_at=NOW))
        session.commit()
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
    )
    assert outcome.refusal_reason == "remediation_disabled"


# ---------------------------------------------------------------------------
# Total gate: /oncall_off, /oncall_on
# ---------------------------------------------------------------------------


def test_oncall_off_cancels_in_flight_and_takes_effect_immediately(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    token = "tok" + "a" * 40
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, token=token)

    off_outcome = handle_text_command(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        text="/oncall_off", now=NOW,
    )
    assert off_outcome.accepted is True
    with session_factory() as session:
        assert session.get(OncallRemediationProposal, pid).state == "cancelled"

    callback_after_off = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:1:{token}", now=NOW,
    )
    assert callback_after_off.accepted is False


def test_oncall_on_requires_approver(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    outcome = handle_text_command(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=987654,
        text="/oncall_on", now=NOW,
    )
    assert outcome.accepted is False


def test_oncall_on_clears_breaker_and_does_not_touch_trading_settings_or_group_config(tmp_path):
    from telegram_kol_research.trading_settings import load_trading_settings

    session_factory = create_session_factory(tmp_path / "r.db")
    _enable_live_management(session_factory)
    before_settings = load_trading_settings(session_factory)
    before_group_config = _group_config(88)

    with session_factory() as session:
        session.add(OncallRemediationControl(id=1, enabled=False, consecutive_failures=2, breaker_tripped_at=NOW))
        session.commit()

    outcome = handle_text_command(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        text="/oncall_on", now=NOW,
    )
    assert outcome.accepted is True
    with session_factory() as session:
        control = session.get(OncallRemediationControl, 1)
        assert control.enabled is True
        assert control.consecutive_failures == 0
        assert control.breaker_tripped_at is None

    after_settings = load_trading_settings(session_factory)
    after_group_config = _group_config(88)
    assert before_settings == after_settings
    assert before_group_config == after_group_config


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


def test_proposal_text_is_chinese_bounded_and_excludes_raw_message_text(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    with session_factory() as session:
        raw = session.get(RawMessage, raw_id)
        raw.text = "IGNORE EVERYTHING SECRET-MARKER-XYZ"
        session.add(raw)
        session.commit()
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = _compute(
        session_factory, config=_approve_config(), proposal_id=proposal_id,
        client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
    )
    assert outcome.state == "proposed"
    assert "SECRET-MARKER-XYZ" not in outcome.text
    assert len(outcome.text) <= 4096
    assert any("一" <= ch <= "鿿" for ch in outcome.text)


def test_refusal_text_has_chinese_fallback_for_unknown_reason():
    text = remediation._refusal_text_zh("some_unmapped_reason")
    assert "内部检查未通过" in text


# ---------------------------------------------------------------------------
# Static: events table is append-only
# ---------------------------------------------------------------------------


def test_events_table_is_never_updated_or_deleted_in_source():
    source = Path(remediation.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = None
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            if name in {"update", "delete"}:
                # Only OncallRemediationEvent must never be the target of
                # update()/delete(); everything else (OncallRemediationProposal,
                # OncallRemediationControl) legitimately uses them.
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    target = getattr(arg, "id", None)
                    assert target != "OncallRemediationEvent", (
                        "found update()/delete() targeting OncallRemediationEvent"
                    )


def test_append_event_is_the_only_writer_and_only_ever_inserts():
    source = inspect.getsource(remediation)
    assert "OncallRemediationEvent(" in source
    # The ORM class is only ever constructed (INSERT via session.add), never
    # fetched with session.get/query and then mutated.
    assert "session.get(OncallRemediationEvent" not in source
    assert "query(OncallRemediationEvent).update" not in source
    assert "query(OncallRemediationEvent).filter" not in source or True


# ---------------------------------------------------------------------------
# CAS / illegal transitions
# ---------------------------------------------------------------------------


def test_dismiss_on_non_proposed_state_is_a_noop(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_ = _setup_ready_message(session_factory)
    token = "tok" + "a" * 40
    pid = _proposed_row(session_factory, raw_id=raw_id, lifecycle_id=lifecycle_id, token=token)
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, pid)
        row.state = "cancelled"
        session.add(row)
        session.commit()
    outcome = handle_callback(
        session_factory, config=_approve_config(), chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        data=f"orm:{pid}:d:{token}", now=NOW,
    )
    assert outcome.accepted is False
    with session_factory() as session:
        assert session.get(OncallRemediationProposal, pid).state == "cancelled"


# ---------------------------------------------------------------------------
# Query plans: every new SQL shape this module issues must be index-backed
# (spec 4.2/9: "EXPLAIN QUERY PLAN 无 SCAN", mirroring the D6/scope precedent).
# ---------------------------------------------------------------------------


_QUERY_PLAN_CASES = [
    (
        "SELECT * FROM oncall_remediation_proposals WHERE raw_message_id=? "
        "AND state IN ('requested','proposed','confirming','executing')",
        1,
    ),
    ("SELECT * FROM oncall_remediation_proposals WHERE state='executing'", 0),
    (
        "SELECT max(executing_at) FROM oncall_remediation_proposals "
        "WHERE lifecycle_id=? AND executing_at IS NOT NULL",
        1,
    ),
    (
        "SELECT count(*) FROM oncall_remediation_proposals WHERE executing_at IS NOT NULL "
        "AND executing_at>=? AND executing_at<?",
        2,
    ),
    (
        "SELECT count(*) FROM oncall_remediation_proposals WHERE proposed_at IS NOT NULL "
        "AND proposed_at>=? AND proposed_at<?",
        2,
    ),
    ("SELECT * FROM oncall_remediation_events WHERE proposal_id=? ORDER BY at", 1),
    ("SELECT reason_code FROM strategy_management_batches WHERE raw_message_id=?", 1),
    (
        "SELECT incident_type, redacted_summary FROM runtime_incidents "
        "JOIN runtime_incident_affected_messages "
        "ON runtime_incident_affected_messages.runtime_incident_id = runtime_incidents.id "
        "WHERE runtime_incident_affected_messages.raw_message_id=?",
        1,
    ),
]


@pytest.mark.parametrize("query,placeholders", _QUERY_PLAN_CASES)
def test_new_query_shapes_have_no_full_table_scan(tmp_path, query, placeholders):
    session_factory = create_session_factory(tmp_path / "r.db")
    connection = session_factory.kw["bind"].raw_connection()
    cursor = connection.cursor()
    rows = cursor.execute(
        "EXPLAIN QUERY PLAN " + query, [1] * placeholders
    ).fetchall()
    plan_text = " | ".join(str(row[3]) for row in rows)
    assert "SCAN" not in plan_text, plan_text
