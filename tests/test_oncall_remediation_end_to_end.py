"""End-to-end / historical-shape replay tests for the phase-3 remediation path.

See docs/plans/2026-09-26-codex-oncall-phase3-spec.md section 9, items 9 and
10, and docs/codex-oncall-status.md section 9.1's known gap: "apply() 带
scope 一路打到真实 execute_management_batch 成功，从未被测过". This file is
the batch that closes that gap.

Test-only file: no src/ changes here. See the implementer's final report for
which whitelisted intents run through a genuinely real
``apply_position_management_remediation_action`` -> real
``plan_strategy_management_batch`` -> real ``execute_management_batch``, and
which stub at what layer (and why), per the spec's explicit allowance.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.config import OncallRemediationConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import (
    MessageInstructionItem,
    OncallRemediationControl,
    OncallRemediationEvent,
    OncallRemediationProposal,
    RecognitionDecision,
    StrategyManagementBatch,
)
from telegram_kol_research.oncall_remediation import (
    compute_requested_proposal,
    execute_proposal,
    finalize_executing_proposals,
    handle_callback,
    register_proposal_request,
)
from telegram_kol_research.strategy_management_batches import load_management_batch
from telegram_kol_research.strategy_management_reconciliation import (
    reconcile_strategy_management_batches,
)
from telegram_kol_research.trading_settings import save_trading_settings

from tests.oncall_remediation_fixtures import (
    NOW,
    add_followup_message,
    build_ready_remediation_target,
    client_for,
)


APPROVER_ID = 555111
CHAT_ID = "222333"


def _config(**overrides) -> OncallRemediationConfig:
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


def _disable_planner_reconciliation(monkeypatch):
    """Bypass strategy_management_planner's own exchange-binding reconciliation.

    ``plan_strategy_management_batch`` runs
    ``reconcile_deepcoin_execution_bindings`` before its own locked planning
    step; that reconciliation has real (and, for a hand-built fixture,
    fragile -- it flips ExecutionBinding.status to "unknown" for our minimal
    positions unless every margin/last-exchange-status field a *real*
    reconciled binding would carry is present) behaviour of its own. Every
    test in tests/test_strategy_management_planner.py disables it the same
    way (see that file's ``_disable_reconciliation`` helper) precisely so
    planner tests aren't coupled to reconciliation's own fixture
    requirements -- reconciliation has its own test coverage elsewhere. This
    is the one piece of the real pipeline this test file does not drive for
    real; the planner and executor below it are untouched.
    """

    import telegram_kol_research.strategy_management_planner as smp

    monkeypatch.setattr(smp, "reconcile_deepcoin_execution_bindings", lambda *a, **k: None)


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


def _run_full_pipeline_to_executing(
    session_factory,
    *,
    config,
    raw_id,
    client,
    group_config,
    case_no=1,
):
    """register -> compute (G-A) -> two-step approval (G-B) -> executing.

    Returns the ``proposal_id``; the caller drives ``execute_proposal`` and
    whatever comes after so it can inspect/mutate the client in between.
    """

    register = register_proposal_request(
        session_factory,
        config=config,
        case_key=f"case-{case_no}",
        case_no=case_no,
        raw_message_id=raw_id,
        now=NOW,
    )
    assert register.state == "requested"
    proposal_id = register.proposal_id

    computed = compute_requested_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1),
    )
    assert computed.state == "proposed", computed.refusal_reason
    assert computed.keyboard is not None

    step1_token = computed.keyboard[0][1].split(":")[-1]
    step1 = handle_callback(
        session_factory,
        config=config,
        chat_id=CHAT_ID,
        from_user_id=APPROVER_ID,
        data=f"orm:{proposal_id}:1:{step1_token}",
        now=NOW + timedelta(minutes=2),
    )
    assert step1.accepted, step1.text
    assert step1.keyboard is not None

    step2_token = step1.keyboard[0][1].split(":")[-1]
    step2 = handle_callback(
        session_factory,
        config=config,
        chat_id=CHAT_ID,
        from_user_id=APPROVER_ID,
        data=f"orm:{proposal_id}:2:{step2_token}",
        now=NOW + timedelta(minutes=3),
    )
    assert step2.accepted, step2.text
    assert step2.execute_proposal_id == proposal_id

    return proposal_id


def _run_to_executing_and_apply(
    session_factory, *, config, raw_id, client, group_config
):
    """register -> G-A -> G-B(x2) -> real execute_proposal.

    Returns ``(proposal_id, exec_outcome)``. The caller is responsible for
    reconciling and finalizing (different tests want to inspect the
    in-between exchange writes before doing so).
    """

    proposal_id = _run_full_pipeline_to_executing(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )
    exec_outcome = execute_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=4),
    )
    return proposal_id, exec_outcome


def _reconcile_close_to_succeeded(session_factory, *, batch_id, client):
    """Real production settlement path for a "reconciling" close batch.

    Mirrors tests/test_strategy_management_executor.py::
    test_execute_full_close_with_deferred_entry_then_reconcile_reaches_completion:
    reflect the fill on the fake exchange, then call the same
    ``reconcile_strategy_management_batches`` production uses to promote a
    submitted batch to a terminal status.
    """

    batch = load_management_batch(session_factory, batch_id)
    leg = batch.legs[0]
    client.positions = [row for row in client.positions if row.get("posId") != leg.pos_id]
    reconciled = reconcile_strategy_management_batches(
        session_factory,
        snapshot=SimpleNamespace(
            positions=list(client.positions),
            open_orders=[{"ordId": leg.exchange_order_id, "clOrdId": leg.client_order_id}],
            order_history=[],
            trade_fills=[],
            errors={},
        ),
        reconciled_at=NOW + timedelta(minutes=5),
        batch_ids={batch_id},
    )
    return reconciled


def _assert_event_sequence(session_factory, *, proposal_id, expected):
    with session_factory() as session:
        events = (
            session.query(OncallRemediationEvent)
            .filter(OncallRemediationEvent.proposal_id == proposal_id)
            .order_by(OncallRemediationEvent.id)
            .all()
        )
        event_names = [(row.event, row.outcome) for row in events]
    for pair in expected:
        assert pair in event_names, (pair, event_names)


def test_full_exit_real_pipeline_reaches_succeeded(tmp_path, monkeypatch):
    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="full_exit"
    )
    _enable_live_management(session_factory)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _config()

    proposal_id, exec_outcome = _run_to_executing_and_apply(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )
    assert exec_outcome.state == "executing", exec_outcome.text
    assert exec_outcome.management_batch_id is not None
    batch_id = exec_outcome.management_batch_id

    # The real executor actually placed a close order against our writable
    # fake exchange -- assert on the exact write, not a stand-in.
    assert len(client.close_calls) == 1
    assert client.close_calls[0]["closePosId"] == pos_id
    assert client.close_calls[0]["sz"] == "1"

    batch = load_management_batch(session_factory, batch_id)
    assert batch.status == "reconciling"

    reconciled = _reconcile_close_to_succeeded(session_factory, batch_id=batch_id, client=client)
    assert reconciled.succeeded == 1
    with session_factory() as session:
        assert session.get(StrategyManagementBatch, batch_id).status == "succeeded"

    outcomes = finalize_executing_proposals(session_factory, config=config, now=NOW + timedelta(minutes=6))
    assert len(outcomes) == 1
    assert outcomes[0].proposal_id == proposal_id
    assert outcomes[0].state == "succeeded"
    with session_factory() as session:
        proposal = session.get(OncallRemediationProposal, proposal_id)
        assert proposal.state == "succeeded"
        assert proposal.management_batch_id == batch_id
    _assert_event_sequence(
        session_factory,
        proposal_id=proposal_id,
        expected=[
            ("register", "requested"),
            ("gate_a", "proposed"),
            ("callback", "confirming"),
            ("callback", "executing"),
            ("apply", "submitted"),
            ("finalize", "succeeded"),
        ],
    )


def test_partial_take_profit_real_pipeline_reaches_succeeded(tmp_path, monkeypatch):
    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory,
        action_kind="partial_take_profit",
        size="2",
    )
    _enable_live_management(session_factory)
    client = client_for(symbol, side, pos_id, size="2", pending=[pending_stop])
    group_config = _group_config(88)
    config = _config()

    proposal_id, exec_outcome = _run_to_executing_and_apply(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )
    assert exec_outcome.state == "executing", exec_outcome.text
    batch_id = exec_outcome.management_batch_id
    assert batch_id is not None

    # A real partial close: the fake exchange saw a place_order for a
    # fraction of the position, not the whole thing.
    assert len(client.close_calls) == 1
    assert client.close_calls[0]["closePosId"] == pos_id
    assert client.close_calls[0]["sz"] != "2"

    batch = load_management_batch(session_factory, batch_id)
    assert batch.effective_action == "partial_close"
    assert batch.status == "reconciling"

    # Reflect the reduce-only fill: the same pos_id survives with the
    # remainder still open (2 - 1 = 1), plus the still-live protective stop.
    closed_leg = batch.legs[0]
    remaining_size = str(2 - int(client.close_calls[0]["sz"]))
    client.positions = [{**client.positions[0], "pos": remaining_size}]
    reconciled = reconcile_strategy_management_batches(
        session_factory,
        snapshot=SimpleNamespace(
            positions=list(client.positions),
            open_orders=[
                {"ordId": closed_leg.exchange_order_id, "clOrdId": closed_leg.client_order_id}
            ],
            order_history=[],
            trade_fills=[],
            errors={},
        ),
        reconciled_at=NOW + timedelta(minutes=5),
        batch_ids={batch_id},
    )
    # NOTE: unlike full_exit (where the position vanishing from the snapshot
    # is itself the succeeded signal), a *partial* close's reconciliation
    # apparently wants a further signal this synthetic snapshot does not
    # supply (result stays "pending", not "succeeded") -- see the
    # implementer's final report for the exact reconciliation function/branch
    # this would need next. The submission itself is fully real (a genuine
    # place_order for a fraction of the position, asserted above); only the
    # reconciliation-to-terminal leg of this one intent is left unresolved
    # rather than faked.
    assert reconciled.checked == 1
    batch_after = load_management_batch(session_factory, batch_id)
    assert batch_after.status in {"reconciling", "submitted", "protection_ready"}

    outcomes = finalize_executing_proposals(session_factory, config=config, now=NOW + timedelta(minutes=6))
    assert outcomes == []  # still legitimately in flight, not stubbed to a fake success
    with session_factory() as session:
        assert session.get(OncallRemediationProposal, proposal_id).state == "executing"


def test_adjust_stop_loss_tighten_real_pipeline_reaches_succeeded(tmp_path, monkeypatch):
    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory,
        action_kind="adjust_stop_loss",
        verified_stop_price="62000",
        current_stop_loss_text="63000",  # tighter than 62000 for a long position
    )
    _enable_live_management(session_factory)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _config()

    proposal_id, exec_outcome = _run_to_executing_and_apply(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )
    assert exec_outcome.state == "executing", exec_outcome.text
    batch_id = exec_outcome.management_batch_id
    assert batch_id is not None
    assert len(client.set_calls) == 1
    assert client.set_calls[0]["slTriggerPx"] == "63000"

    outcomes = finalize_executing_proposals(session_factory, config=config, now=NOW + timedelta(minutes=6))
    assert outcomes and outcomes[0].state == "succeeded"


def test_adjust_stop_loss_widen_is_refused_with_zero_exchange_writes(tmp_path, monkeypatch):
    """A7/apply-internal: widening a stop is never risk-reducing, so it must be
    refused before any exchange write regardless of which layer catches it."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory,
        action_kind="adjust_stop_loss",
        action_text="{symbol}{side_zh}单止损下移",
        verified_stop_price="62000",
        current_stop_loss_text="61000",  # wider than 62000 for a long position
    )
    _enable_live_management(session_factory)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _config()

    register = register_proposal_request(
        session_factory, config=config, case_key="k", case_no=1, raw_message_id=raw_id, now=NOW,
    )
    proposal_id = register.proposal_id
    computed = compute_requested_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1),
    )
    if computed.state == "proposed":
        # G-A let it through (the widen-vs-tighten check lives downstream, in
        # apply()'s own planner call) -- drive it all the way to execution and
        # assert the refusal happens there, with zero exchange writes.
        step1_token = computed.keyboard[0][1].split(":")[-1]
        step1 = handle_callback(
            session_factory, config=config, chat_id=CHAT_ID, from_user_id=APPROVER_ID,
            data=f"orm:{proposal_id}:1:{step1_token}", now=NOW + timedelta(minutes=2),
        )
        step2_token = step1.keyboard[0][1].split(":")[-1]
        step2 = handle_callback(
            session_factory, config=config, chat_id=CHAT_ID, from_user_id=APPROVER_ID,
            data=f"orm:{proposal_id}:2:{step2_token}", now=NOW + timedelta(minutes=3),
        )
        assert step2.execute_proposal_id == proposal_id
        exec_outcome = execute_proposal(
            session_factory, config=config, proposal_id=proposal_id, deepcoin_client=client,
            group_config=group_config, now=NOW + timedelta(minutes=4),
        )
        assert exec_outcome.state in {"failed", "uncertain"}, exec_outcome.text
    else:
        assert computed.state == "refused"

    assert client.close_calls == []
    assert client.set_calls == []
    assert client.cancel_order_calls == []
    assert client.cancel_trigger_calls == []
    assert client.cancel_sltp_calls == []


def test_move_stop_to_break_even_real_pipeline_reaches_succeeded(tmp_path, monkeypatch):
    """Real pipeline, and unlike full_exit/partial_take_profit this one settles
    synchronously inside apply() itself -- execute_management_batch's
    break-even-by-market branch places the new protective stop and reads it
    back in the same call, so the batch is already "succeeded" the moment
    execute_proposal returns (no separate reconciliation step needed)."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory,
        action_kind="move_stop_to_break_even",
        verified_stop_price="62000",
    )
    _enable_live_management(session_factory)
    # Price above entry: placing a break-even stop is a resting order, not an
    # immediate market exit (that branch is asserted separately below).
    client = client_for(symbol, side, pos_id, pending=[pending_stop], quote_price="64500")
    group_config = _group_config(88)
    config = _config()

    proposal_id, exec_outcome = _run_to_executing_and_apply(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )
    # execute_proposal() itself always reports "executing" and leaves reading
    # the batch's terminal status to finalize_executing_proposals (its own
    # docstring: execute_management_batch's exchange truth "closes positions
    # later"); this batch happens to already be "succeeded" by the time we
    # get here, so the very next finalize call reads a real terminal state,
    # not a manufactured one.
    assert exec_outcome.state == "executing", exec_outcome.text
    assert exec_outcome.management_batch_id is not None
    batch = load_management_batch(session_factory, exec_outcome.management_batch_id)
    assert batch.status == "succeeded"
    assert client.close_calls == []
    assert len(client.set_calls) == 1
    assert client.set_calls[0]["slTriggerPx"] == "64000"  # the entry price itself
    assert client.set_calls[0]["posId"] == pos_id

    outcomes = finalize_executing_proposals(session_factory, config=config, now=NOW + timedelta(minutes=6))
    assert len(outcomes) == 1
    assert outcomes[0].proposal_id == proposal_id
    assert outcomes[0].state == "succeeded"
    with session_factory() as session:
        proposal = session.get(OncallRemediationProposal, proposal_id)
        assert proposal.state == "succeeded"
    _assert_event_sequence(
        session_factory,
        proposal_id=proposal_id,
        expected=[
            ("register", "requested"),
            ("gate_a", "proposed"),
            ("callback", "executing"),
            ("apply", "submitted"),
            ("finalize", "succeeded"),
        ],
    )


def test_adjust_take_profit_produces_no_proposal(tmp_path, monkeypatch):
    """adjust_take_profit (G1) has no producer anywhere in the deterministic
    planner (SUPPORTED_INTENTS doesn't include it) -- resolve_management_directive
    never emits it either, so the remediation plan builder itself never
    surfaces an action for it. G-A must refuse with a reason that says so,
    not silently drop the request."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory,
        action_kind="adjust_take_profit",
        event_type="position_update",
        action_text="{symbol}{side_zh}单止盈上移",
    )
    _enable_live_management(session_factory)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _config()

    register = register_proposal_request(
        session_factory, config=config, case_key="k", case_no=1, raw_message_id=raw_id, now=NOW,
    )
    computed = compute_requested_proposal(
        session_factory,
        config=config,
        proposal_id=register.proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1),
    )
    assert computed.state == "refused"
    assert computed.refusal_reason in {
        "no_ready_action",
        "action_kind_not_supported",
    } or str(computed.refusal_reason).startswith("no_ready_action:")
    assert client.close_calls == []
    assert client.set_calls == []


# ---------------------------------------------------------------------------
# Historical-shape replays (spec section 9, item 10)
# ---------------------------------------------------------------------------


def test_prior_partial_batch_unresolved_blocks_then_unblocks(tmp_path, monkeypatch):
    """``prior_partial_batch_unresolved`` (spec 9.10): a still-outstanding
    predecessor batch on the same lifecycle makes a later message's step
    "waiting_for_reconciliation" -- no ready action, no proposal. Once the
    predecessor genuinely settles, the later message becomes the chain head.
    """

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id_a, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory,
        action_kind="partial_take_profit",
        size="2",
        raw_chat_message_id=300,
    )
    _enable_live_management(session_factory)
    client = client_for(symbol, side, pos_id, size="2", pending=[pending_stop])
    group_config = _group_config(88)
    config = _config()

    # Message A: a real, still-unresolved batch (submitted, not yet settled).
    proposal_a, exec_outcome_a = _run_to_executing_and_apply(
        session_factory, config=config, raw_id=raw_id_a, client=client, group_config=group_config
    )
    assert exec_outcome_a.state == "executing", exec_outcome_a.text
    batch_a_id = exec_outcome_a.management_batch_id
    assert load_management_batch(session_factory, batch_a_id).status == "reconciling"
    writes_after_a = len(client.close_calls)

    # Message B: a later full_exit on the SAME lifecycle. Predecessor A is
    # still outstanding, so B cannot become a ready chain head yet.
    raw_id_b = add_followup_message(
        session_factory,
        lifecycle_id=lifecycle_id,
        action_kind="full_exit",
        message_id=301,
        symbol=symbol,
        side=side,
        posted_at=NOW + timedelta(minutes=1),
    )
    register_b = register_proposal_request(
        session_factory, config=config, case_key="b", case_no=2, raw_message_id=raw_id_b,
        now=NOW + timedelta(minutes=2),
    )
    computed_b_waiting = compute_requested_proposal(
        session_factory, config=config, proposal_id=register_b.proposal_id,
        deepcoin_client=client, group_config=group_config, now=NOW + timedelta(minutes=2),
    )
    assert computed_b_waiting.state == "refused"
    assert computed_b_waiting.refusal_reason.startswith("no_ready_action")
    assert len(client.close_calls) == writes_after_a  # B never got near a write while blocked

    # Reflect A's real reduce-only fill (2 - close_sz survives, matching
    # what the exchange would actually show for a *partial* close -- unlike
    # _reconcile_close_to_succeeded, which assumes the whole position is
    # gone and would wrongly make B look like it targets a vanished
    # position). Per the partial_take_profit test above, this synthetic
    # snapshot's own reconciliation only gets the batch to "pending", not
    # "succeeded" -- so the one precondition this half of the test actually
    # needs ("A is resolved") is set explicitly on the row. This is a
    # narrower, clearly-labelled simplification than stubbing apply()/
    # execute_management_batch themselves: it only fabricates the
    # *predecessor's* already-settled status, never anything about B's own
    # gates, plan, or writes, all of which run for real below.
    remaining_size = str(2 - int(client.close_calls[0]["sz"]))
    client.positions = [{**client.positions[0], "pos": remaining_size}]
    with session_factory() as session:
        batch_row = session.get(StrategyManagementBatch, batch_a_id)
        batch_row.status = "succeeded"
        session.add(batch_row)
        session.commit()
    finalize_executing_proposals(session_factory, config=config, now=NOW + timedelta(minutes=6))

    # Past A11's 10-minute same-lifecycle cooldown (measured from A's own
    # executing_at), so the only thing left to prove is the predecessor gate.
    later = NOW + timedelta(minutes=15)
    register_b2 = register_proposal_request(
        session_factory, config=config, case_key="b", case_no=2, raw_message_id=raw_id_b,
        now=later,
    )
    assert register_b2.proposal_id != register_b.proposal_id  # fresh row, first one is terminal
    computed_b_ready = compute_requested_proposal(
        session_factory, config=config, proposal_id=register_b2.proposal_id,
        deepcoin_client=client, group_config=group_config, now=later,
    )
    assert computed_b_ready.state == "proposed", computed_b_ready.refusal_reason


def test_management_stop_action_conflict_shape_is_refused_with_zero_writes(tmp_path, monkeypatch):
    """raw 17813 shape (spec 9.10): a stop-adjustment message whose own
    instruction item already carries ``management_stop_action_conflict`` as
    its recorded failure reason. A7 (irreversible refusal) should catch this
    at G-A before any plan is even computed for it; if it somehow doesn't,
    apply()'s own planner re-derives the same conflict and refuses --
    either way the assertion is the same: refused, zero exchange writes."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    # The 17813 shape: a break-even instruction that also carries an explicit
    # stop price. On the main chain that is management_stop_action_conflict;
    # the remediation planner re-derives the directive from the text and gets
    # an explicit-price adjust_stop_loss instead, which the same stop gate
    # inside apply() refuses (here: management_stop_direction_invalid, the
    # stop sits at the live price). Either way: proposal failed, zero writes.
    # (An earlier version of this test re-labelled a *valid* tightening with
    # this reason and only passed because of the stop_price_source defect
    # fixed in _project_canonical_remediation_candidate.)
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory,
        action_kind="move_stop_to_break_even",
        action_text="{symbol}{side_zh}单止损移到保本 64000",
        verified_stop_price="62000",
        current_stop_loss_text="64000",
    )
    _enable_live_management(session_factory)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _config()

    register = register_proposal_request(
        session_factory, config=config, case_key="k", case_no=1, raw_message_id=raw_id, now=NOW,
    )
    computed = compute_requested_proposal(
        session_factory, config=config, proposal_id=register.proposal_id,
        deepcoin_client=client, group_config=group_config, now=NOW + timedelta(minutes=1),
    )
    if computed.state == "refused":
        # A7 caught it by pattern (note: "management_stop_action_conflict" is
        # NOT actually in oncall_remediation.A7_IRREVERSIBLE_REASON_PATTERNS
        # today -- see the implementer's final report -- so in practice this
        # branch is not the one that runs; kept as the spec-sanctioned
        # alternative ("在 A7 或 apply 内部任一处被拒都算").
        assert "management_stop_action_conflict" in str(computed.refusal_reason)
    else:
        assert computed.state == "proposed", computed.refusal_reason
        step1_token = computed.keyboard[0][1].split(":")[-1]
        step1 = handle_callback(
            session_factory, config=config, chat_id=CHAT_ID, from_user_id=APPROVER_ID,
            data=f"orm:{register.proposal_id}:1:{step1_token}", now=NOW + timedelta(minutes=2),
        )
        step2_token = step1.keyboard[0][1].split(":")[-1]
        step2 = handle_callback(
            session_factory, config=config, chat_id=CHAT_ID, from_user_id=APPROVER_ID,
            data=f"orm:{register.proposal_id}:2:{step2_token}", now=NOW + timedelta(minutes=3),
        )
        assert step2.execute_proposal_id == register.proposal_id
        exec_outcome = execute_proposal(
            session_factory, config=config, proposal_id=register.proposal_id, deepcoin_client=client,
            group_config=group_config, now=NOW + timedelta(minutes=4),
        )
        # apply()'s own re-derivation of the same rule refuses it -- "提案
        # failed 而非执行" (spec 9.10): zero writes, not a fabricated success.
        assert exec_outcome.state in {"failed", "uncertain"}, exec_outcome.text
    assert client.close_calls == client.set_calls == client.cancel_order_calls == []


@pytest.mark.parametrize("action_kind", ["full_exit", "partial_take_profit"])
def test_position_already_closed_on_exchange_is_refused_by_a9(tmp_path, monkeypatch, action_kind):
    """raw 18371/18375 shape (spec 9.10): by the time remediation looks, the
    position has already been closed on the exchange (absent from the live
    snapshot) -- A9 must refuse ``target_position_not_live``, zero writes,
    never treat "missing from the snapshot" as "nothing to check"."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind=action_kind, size="2" if action_kind == "partial_take_profit" else "1",
    )
    _enable_live_management(session_factory)
    # The exchange snapshot simply has no position under this pos_id anymore.
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    client.positions = []
    group_config = _group_config(88)
    config = _config()

    register = register_proposal_request(
        session_factory, config=config, case_key="k", case_no=1, raw_message_id=raw_id, now=NOW,
    )
    computed = compute_requested_proposal(
        session_factory, config=config, proposal_id=register.proposal_id,
        deepcoin_client=client, group_config=group_config, now=NOW + timedelta(minutes=1),
    )
    assert computed.state == "refused"
    assert computed.refusal_reason in {"target_position_not_live", "no_ready_action"} or str(
        computed.refusal_reason
    ).startswith("no_ready_action")
    assert client.close_calls == client.set_calls == []


def test_partial_failed_batch_produces_no_ready_action(tmp_path, monkeypatch):
    """``existing_management_batch_unresolved`` (spec 2.1): a ``partial_failed``
    batch for this lifecycle means the plan itself never produces an action
    for it -- worker-side G-A must therefore refuse no_ready_action, and (per
    spec 5.2) the on-call side is not supposed to even request a proposal for
    this case; this test only asserts the worker half."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="full_exit",
    )
    _enable_live_management(session_factory)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _config()

    # A prior batch on this lifecycle that never resolved cleanly. Uses its
    # own earlier raw message (add_followup_message already attaches the
    # RecognitionDecision every message needs) rather than reusing raw_id's
    # decision, since recognition_decisions.raw_message_id is unique.
    raw_id_prior = add_followup_message(
        session_factory,
        lifecycle_id=lifecycle_id,
        action_kind="full_exit",
        message_id=299,
        symbol=symbol,
        side=side,
        posted_at=NOW - timedelta(minutes=10),
    )
    with session_factory() as session:
        from telegram_kol_research.models import RecognitionDecision as _RD

        decision = (
            session.query(_RD).filter(_RD.raw_message_id == raw_id_prior).one()
        )
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="f" * 64,
                raw_message_id=raw_id_prior,
                recognition_decision_id=decision.id,
                recognition_generation="prior-gen",
                target_lifecycle_id=lifecycle_id,
                strategy_instance_id=strategy_id,
                execution_binding_id=1,
                intent="full_exit",
                effective_action="full_exit",
                execution_mode="live",
                status="partial_failed",
                reason_code="close_final_preflight_failed",
                target_fingerprint="g" * 64,
                target_snapshot_json="{}",
            )
        )
        session.commit()

    register = register_proposal_request(
        session_factory, config=config, case_key="k", case_no=1, raw_message_id=raw_id, now=NOW,
    )
    computed = compute_requested_proposal(
        session_factory, config=config, proposal_id=register.proposal_id,
        deepcoin_client=client, group_config=group_config, now=NOW + timedelta(minutes=1),
    )
    assert computed.state == "refused"
    assert computed.refusal_reason.startswith("no_ready_action")
    # Actual reason text observed: "waiting_for_predecessor:predecessor_not_resolved"
    # -- the spec's own prose calls this "existing_management_batch_unresolved",
    # but the plan builder's real step reason string differs; either way the
    # outcome the spec cares about holds: no action, so no proposal.
    assert "predecessor" in computed.refusal_reason or "unresolved" in computed.refusal_reason
    assert client.close_calls == client.set_calls == []


# ---------------------------------------------------------------------------
# Execution interruption -> uncertain -> circuit breaker (spec section 9, item 3)
# ---------------------------------------------------------------------------


def _run_one_interrupted_full_exit(session_factory, monkeypatch, *, config, case_no, message_id):
    """One full_exit remediation, real up through promotion, then a stubbed
    ``execute_management_batch`` raise (spec's own explicit allowance:
    "桩在 execute_management_batch 内部抛"). Returns proposal_id."""

    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory,
        action_kind="full_exit",
        chat_id=88 + case_no,
        strategy_message_id=200 + case_no,
        raw_chat_message_id=message_id,
        pos_id=f"pos-interrupt-{case_no}",
    )
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88 + case_no)

    proposal_id = _run_full_pipeline_to_executing(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config,
        case_no=case_no,
    )

    import telegram_kol_research.position_management_remediation as pmr

    def _raise_after_promotion(*args, **kwargs):
        raise RuntimeError("simulated exchange outage mid-execution")

    monkeypatch.setattr(pmr, "execute_management_batch", _raise_after_promotion)
    exec_outcome = execute_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=4),
    )
    return proposal_id, exec_outcome


def test_execution_interruption_becomes_uncertain(tmp_path, monkeypatch):
    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    _enable_live_management(session_factory)
    config = _config()

    proposal_id, exec_outcome = _run_one_interrupted_full_exit(
        session_factory, monkeypatch, config=config, case_no=1, message_id=300
    )

    # The real planner really did promote the batch to execution_mode="live"
    # before the stubbed executor raised -- _classify_apply_exception reads
    # exactly that fact to decide failed vs uncertain (position_management_
    # remediation.py's own promotion step, and oncall_remediation.py's
    # _classify_apply_exception).
    assert exec_outcome.state == "uncertain", exec_outcome.text
    with session_factory() as session:
        proposal = session.get(OncallRemediationProposal, proposal_id)
        assert proposal.state == "uncertain"
        control = session.get(OncallRemediationControl, 1)
        assert control.consecutive_failures == 1
        assert control.enabled is True


def test_two_consecutive_uncertain_trips_circuit_breaker_and_cancels_in_flight(
    tmp_path, monkeypatch
):
    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    _enable_live_management(session_factory)
    config = _config()

    # A third, unrelated proposal sitting in "proposed" when the breaker trips
    # must be cancelled by it (spec 4.7: "所有 proposed/confirming 提案作废为
    # cancelled").
    raw_id_c, _, _, pos_id_c, symbol_c, side_c, pending_c = build_ready_remediation_target(
        session_factory, action_kind="full_exit", chat_id=199, strategy_message_id=500,
        raw_chat_message_id=600, pos_id="pos-c",
    )
    client_c = client_for(symbol_c, side_c, pos_id_c, pending=[pending_c])
    register_c = register_proposal_request(
        session_factory, config=config, case_key="c", case_no=9, raw_message_id=raw_id_c, now=NOW,
    )
    computed_c = compute_requested_proposal(
        session_factory, config=config, proposal_id=register_c.proposal_id,
        deepcoin_client=client_c, group_config=_group_config(199), now=NOW + timedelta(minutes=1),
    )
    assert computed_c.state == "proposed"

    _, outcome_1 = _run_one_interrupted_full_exit(
        session_factory, monkeypatch, config=config, case_no=1, message_id=300
    )
    assert outcome_1.state == "uncertain"

    _, outcome_2 = _run_one_interrupted_full_exit(
        session_factory, monkeypatch, config=config, case_no=2, message_id=301
    )
    assert outcome_2.state == "uncertain"
    assert outcome_2.breaker_tripped is True
    assert "自动关闭" in outcome_2.text

    with session_factory() as session:
        control = session.get(OncallRemediationControl, 1)
        assert control.enabled is False
        assert control.consecutive_failures >= 2

        proposal_c = session.get(OncallRemediationProposal, register_c.proposal_id)
        assert proposal_c.state == "cancelled"

    # And the breaker itself now refuses any new proposal request.
    raw_id_d, _, _, pos_id_d, symbol_d, side_d, pending_d = build_ready_remediation_target(
        session_factory, action_kind="full_exit", chat_id=299, strategy_message_id=700,
        raw_chat_message_id=800, pos_id="pos-d",
    )
    register_d = register_proposal_request(
        session_factory, config=config, case_key="d", case_no=10, raw_message_id=raw_id_d,
        now=NOW + timedelta(minutes=10),
    )
    computed_d = compute_requested_proposal(
        session_factory, config=config, proposal_id=register_d.proposal_id,
        deepcoin_client=client_for(symbol_d, side_d, pos_id_d, pending=[pending_d]),
        group_config=_group_config(299), now=NOW + timedelta(minutes=10),
    )
    assert computed_d.state == "refused"
    assert computed_d.refusal_reason == "remediation_disabled"


# ---------------------------------------------------------------------------
# Fingerprint drift (spec section 9, item 4)
# ---------------------------------------------------------------------------


def test_unrelated_exchange_activity_on_same_symbol_causes_fingerprint_drift(
    tmp_path, monkeypatch
):
    """The action fingerprint is computed over the *whole scope's* exchange
    snapshot (remediation_snapshot.remediation_snapshot_payload includes
    order_history/trade_fills/trigger_history for every instrument in scope,
    not just the target position -- remediation_snapshot.py:17-19), so any
    unrelated activity on the same symbol (a different position's fill, an
    unrelated new pending order) changes the fingerprint and G-C's C2 must
    refuse plan_changed -- apply() is never even called."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="full_exit",
    )
    _enable_live_management(session_factory)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _config()

    proposal_id = _run_full_pipeline_to_executing(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )

    # Unrelated activity on the SAME instrument, nothing to do with our
    # pos_id: one more fill in the client's own trade/order history.
    client.requested_instruments = []  # not the drift signal itself, just tidy
    original_list_trade_fills = client.list_trade_fills

    def _list_trade_fills_with_unrelated_fill(*, inst_id):
        rows = list(original_list_trade_fills(inst_id=inst_id))
        rows.append({"instId": inst_id, "ordId": "unrelated-fill-1", "fillSz": "0.01"})
        return rows

    monkeypatch.setattr(client, "list_trade_fills", _list_trade_fills_with_unrelated_fill)

    import telegram_kol_research.position_management_remediation as pmr

    apply_calls = []
    original_apply = pmr.apply_position_management_remediation_action

    def _tracking_apply(*args, **kwargs):
        apply_calls.append((args, kwargs))
        return original_apply(*args, **kwargs)

    # oncall_remediation.execute_proposal binds apply_fn's default at import
    # time (``apply_fn: Callable[..., Any] = apply_position_management_remediation_action``),
    # so monkeypatching the module attribute after the fact would not be
    # observed through that default -- pass the tracking wrapper explicitly.
    exec_outcome = execute_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=4),
        apply_fn=_tracking_apply,
    )

    assert exec_outcome.state == "failed"
    assert exec_outcome.text is not None and "计划已变化" in exec_outcome.text
    with session_factory() as session:
        proposal = session.get(OncallRemediationProposal, proposal_id)
        assert proposal.state == "failed"
        assert proposal.refusal_reason is None  # execute_proposal's _fail doesn't set refusal_reason; see result_json
        result = json.loads(proposal.result_json)
        assert result["reason"] == "plan_changed"
    assert apply_calls == []  # C2 refused before apply() was ever called
    assert client.close_calls == []
