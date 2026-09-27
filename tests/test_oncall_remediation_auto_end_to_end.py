"""Real end-to-end tests for phase-4 auto-remediation.

See docs/plans/2026-09-27-codex-oncall-phase4-auto-remediation-spec.md and
docs/codex-oncall-status.md 9.5's known gaps (readback gating not enforced,
runtime not wired to the auto CAS path). This file closes those gaps with
the same "writable fake exchange, real planner, real executor" pipeline
tests/test_oncall_remediation_end_to_end.py already established for the
human-approved path -- nothing here stubs ``apply_position_management_
remediation_action``/``execute_management_batch`` except where explicitly
noted (scenario 5's injected apply-time failure).

Every scenario drives the *auto* path with zero button clicks: register ->
compute_requested_proposal (G-A + G-D, CAS straight to "executing") ->
execute_proposal (G-C rerun + real apply) -> finalize_executing_proposals
(real batch settlement read + real post-execution exchange readback).
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    OncallRemediationAudit,
    OncallRemediationControl,
    OncallRemediationProposal,
)
from telegram_kol_research.oncall_remediation import (
    ExecutionOutcome,
    ProposalOutcome,
    compute_requested_proposal,
    execute_proposal,
    finalize_executing_proposals,
    handle_text_command,
    register_proposal_request,
)
from telegram_kol_research.oncall_remediation_auto import AutoHealthInputs
import telegram_kol_research.oncall_remediation_runtime as runtime_module
from telegram_kol_research.strategy_management_batches import load_management_batch

from tests.oncall_remediation_fixtures import NOW, build_ready_remediation_target, client_for
from tests.test_oncall_remediation_auto import _auto_config, _health, _mark_transient_reason
from tests.test_oncall_remediation_end_to_end import (
    APPROVER_ID,
    CHAT_ID,
    _disable_planner_reconciliation,
    _enable_live_management,
    _group_config,
    _reconcile_close_to_succeeded,
)


def _register_and_promote(
    session_factory,
    *,
    config,
    raw_id,
    client,
    group_config,
    case_no=1,
    now=None,
):
    """register -> compute_requested_proposal, asserting the library's own
    single-flight CAS promoted straight to "executing" with no message sent
    (spec section 5: "从提案通过到进入 apply 之间不留等待窗")."""

    register = register_proposal_request(
        session_factory,
        config=config,
        case_key=f"auto-case-{case_no}",
        case_no=case_no,
        raw_message_id=raw_id,
        now=now or NOW,
    )
    assert register.state == "requested"
    proposal_id = register.proposal_id

    outcome = compute_requested_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=(now or NOW) + timedelta(minutes=1),
        auto_health=_health(),
    )
    assert outcome.state == "executing", outcome.refusal_reason
    assert outcome.should_send is False
    assert outcome.auto_execute_proposal_id == proposal_id
    return proposal_id, outcome


def _audit_rows(session_factory, *, proposal_id):
    with session_factory() as session:
        rows = (
            session.query(OncallRemediationAudit)
            .filter(OncallRemediationAudit.proposal_id == proposal_id)
            .order_by(OncallRemediationAudit.id)
            .all()
        )
        return [(row.phase, json.loads(row.payload_json)) for row in rows]


def _control(session_factory) -> OncallRemediationControl:
    with session_factory() as session:
        return session.get(OncallRemediationControl, 1)


# ---------------------------------------------------------------------------
# 1. full_exit, fully automatic, real pipeline, readback confirms
# ---------------------------------------------------------------------------


def test_full_exit_auto_reaches_succeeded_with_confirmed_readback(tmp_path, monkeypatch):
    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="full_exit"
    )
    _enable_live_management(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _auto_config(auto_actions=frozenset({"full_exit"}))

    proposal_id, _outcome = _register_and_promote(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )

    exec_outcome = execute_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1, seconds=5),
        auto_health=_health(),
    )
    assert exec_outcome.state == "executing", exec_outcome.text
    batch_id = exec_outcome.management_batch_id
    assert batch_id is not None
    # Real exchange write: one close order for the whole position.
    assert len(client.close_calls) == 1
    assert client.close_calls[0]["closePosId"] == pos_id

    batch = load_management_batch(session_factory, batch_id)
    assert batch.status == "reconciling"
    reconciled = _reconcile_close_to_succeeded(session_factory, batch_id=batch_id, client=client)
    assert reconciled.succeeded == 1

    outcomes = finalize_executing_proposals(
        session_factory,
        config=config,
        now=NOW + timedelta(minutes=6),
        deepcoin_client_factory=lambda: client,
    )
    assert len(outcomes) == 1
    assert outcomes[0].state == "succeeded", outcomes[0].text
    assert "第 1 笔" in outcomes[0].text
    assert "自动补救" in outcomes[0].text or "已补救" in outcomes[0].text

    control = _control(session_factory)
    assert control.auto_suspended is False

    rows = _audit_rows(session_factory, proposal_id=proposal_id)
    phases = [phase for phase, _ in rows]
    assert phases == ["pre_apply", "result"]
    result_payload = rows[1][1]
    assert result_payload["readback"]["outcome"] == "confirmed"
    assert result_payload["readback"]["still_open"] == []
    assert result_payload["exchange_traffic"], "exchange traffic must be captured"
    assert any(row.get("kind") == "position_mutation_intent" for row in result_payload["exchange_traffic"]) or any(
        row.get("kind") == "strategy_management_leg" for row in result_payload["exchange_traffic"]
    )


# ---------------------------------------------------------------------------
# 2. move_stop_to_break_even, both branches, real pipeline, readback confirms
# ---------------------------------------------------------------------------


def test_move_stop_to_break_even_auto_resting_stop_branch(tmp_path, monkeypatch):
    """Price above entry (long): the planner rests a break-even stop rather
    than closing at market. Batch settles synchronously inside apply()."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="move_stop_to_break_even", verified_stop_price="62000"
    )
    _enable_live_management(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    client = client_for(symbol, side, pos_id, pending=[pending_stop], quote_price="64500")
    group_config = _group_config(88)
    config = _auto_config(auto_actions=frozenset({"move_stop_to_break_even"}))

    proposal_id, _outcome = _register_and_promote(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )
    exec_outcome = execute_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1, seconds=5),
        auto_health=_health(),
    )
    assert exec_outcome.state == "executing", exec_outcome.text
    batch = load_management_batch(session_factory, exec_outcome.management_batch_id)
    assert batch.status == "succeeded"
    assert client.close_calls == []
    assert len(client.set_calls) == 1
    assert client.set_calls[0]["slTriggerPx"] == "64000"  # entry price itself

    outcomes = finalize_executing_proposals(
        session_factory,
        config=config,
        now=NOW + timedelta(minutes=6),
        deepcoin_client_factory=lambda: client,
    )
    assert outcomes[0].state == "succeeded", outcomes[0].text
    assert "挂保本止损" in outcomes[0].text
    assert "回读确认" in outcomes[0].text

    rows = _audit_rows(session_factory, proposal_id=proposal_id)
    readback = rows[1][1]["readback"]
    assert readback["outcome"] == "confirmed"
    assert readback["branch"] == "stop_placed"


def test_move_stop_to_break_even_auto_market_close_branch(tmp_path, monkeypatch):
    """Price has already crossed the break-even/entry price: user ruling
    2026-09-27 section 11 item 5 allows the auto path to market-close
    instead of resting an unreachable stop."""

    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="move_stop_to_break_even", verified_stop_price="62000"
    )
    _enable_live_management(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    # Long position, entry 64000: a quote below entry means the break-even
    # price is already behind the market -- the market-close branch.
    client = client_for(symbol, side, pos_id, pending=[pending_stop], quote_price="63000")
    group_config = _group_config(88)
    config = _auto_config(auto_actions=frozenset({"move_stop_to_break_even"}))

    proposal_id, _outcome = _register_and_promote(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )
    exec_outcome = execute_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1, seconds=5),
        auto_health=_health(),
    )
    assert exec_outcome.state == "executing", exec_outcome.text
    batch_id = exec_outcome.management_batch_id
    batch = load_management_batch(session_factory, batch_id)
    # The market-close branch submits a close order through the same path
    # full_exit uses -- it settles asynchronously (reconciling), unlike the
    # resting-stop branch above which confirms synchronously inside apply().
    assert batch.status == "reconciling"
    assert len(client.close_calls) == 1
    assert client.close_calls[0]["closePosId"] == pos_id
    assert client.set_calls == []
    reconciled = _reconcile_close_to_succeeded(session_factory, batch_id=batch_id, client=client)
    assert reconciled.succeeded == 1

    outcomes = finalize_executing_proposals(
        session_factory,
        config=config,
        now=NOW + timedelta(minutes=6),
        deepcoin_client_factory=lambda: client,
    )
    assert outcomes[0].state == "succeeded", outcomes[0].text
    assert "市价平仓" in outcomes[0].text
    assert "回读确认" in outcomes[0].text

    rows = _audit_rows(session_factory, proposal_id=proposal_id)
    readback = rows[1][1]["readback"]
    assert readback["outcome"] == "confirmed"
    assert readback["branch"] == "market_closed"


# ---------------------------------------------------------------------------
# 3. Readback mismatch -> uncertain + immediate auto suspension
# ---------------------------------------------------------------------------


def test_readback_mismatch_suspends_auto_and_downgrades_next_request(tmp_path, monkeypatch):
    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="full_exit"
    )
    _enable_live_management(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _auto_config(auto_actions=frozenset({"full_exit"}))

    proposal_id, _outcome = _register_and_promote(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )
    exec_outcome = execute_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1, seconds=5),
        auto_health=_health(),
    )
    batch_id = exec_outcome.management_batch_id
    _reconcile_close_to_succeeded(session_factory, batch_id=batch_id, client=client)

    # Simulate a snapshot that disagrees with the batch's own terminal
    # status: the exchange still reports the position open. This can happen
    # for real (e.g. a reconciliation race, or a venue-side rollback) --
    # readback exists precisely to catch it.
    client.positions.append(
        {
            "instId": f"{symbol}-USDT-SWAP",
            "posId": pos_id,
            "posSide": side,
            "pos": "1",
            "avgPx": "64000",
            "mgnMode": "cross",
            "mrgPosition": "split",
        }
    )

    outcomes = finalize_executing_proposals(
        session_factory,
        config=config,
        now=NOW + timedelta(minutes=6),
        deepcoin_client_factory=lambda: client,
    )
    assert outcomes[0].state == "uncertain", outcomes[0].text
    assert "结果未知" in outcomes[0].text or "回读不符" in outcomes[0].text
    assert "自动补救已暂停" in outcomes[0].text

    control = _control(session_factory)
    assert control.auto_suspended is True

    rows = _audit_rows(session_factory, proposal_id=proposal_id)
    readback = rows[1][1]["readback"]
    assert readback["outcome"] == "mismatch"
    assert pos_id in readback["still_open"]

    # A brand new case for the same lifecycle is now downgraded to a
    # button-carrying proposal, not auto-executed (spec section 11 item 4).
    raw_id_2, *_rest = build_ready_remediation_target(
        session_factory,
        action_kind="full_exit",
        strategy_message_id=201,
        raw_chat_message_id=301,
        pos_id="pos-b",
        symbol="ETH",
        side="short",
    )
    _mark_transient_reason(session_factory, raw_id=raw_id_2)
    client2 = client_for("ETH", "short", "pos-b")
    register2 = register_proposal_request(
        session_factory,
        config=config,
        case_key="auto-case-2",
        case_no=2,
        raw_message_id=raw_id_2,
        now=NOW + timedelta(minutes=10),
    )
    outcome2 = compute_requested_proposal(
        session_factory,
        config=config,
        proposal_id=register2.proposal_id,
        deepcoin_client=client2,
        group_config=group_config,
        now=NOW + timedelta(minutes=11),
        auto_health=_health(),
    )
    assert outcome2.state == "proposed"
    assert outcome2.auto_execute_proposal_id is None
    assert outcome2.keyboard is not None
    assert "未自动执行" in (outcome2.text or "")
    assert client2.close_calls == []


# ---------------------------------------------------------------------------
# 4. /auto_on recovers automatic execution
# ---------------------------------------------------------------------------


def test_auto_on_after_suspension_resumes_automatic_execution(tmp_path, monkeypatch):
    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(OncallRemediationControl(id=1, auto_suspended=True, auto_suspend_reason="test"))
        session.commit()

    config = _auto_config(auto_actions=frozenset({"full_exit"}))
    outcome = handle_text_command(
        session_factory,
        config=config,
        chat_id=CHAT_ID,
        from_user_id=APPROVER_ID,
        text="/auto_on",
        now=NOW,
    )
    assert outcome.accepted
    control = _control(session_factory)
    assert control.auto_suspended is False

    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="full_exit"
    )
    _enable_live_management(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)

    proposal_id, _outcome = _register_and_promote(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )
    exec_outcome = execute_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1, seconds=5),
        auto_health=_health(),
    )
    assert exec_outcome.state == "executing", exec_outcome.text
    assert len(client.close_calls) == 1
    _reconcile_close_to_succeeded(session_factory, batch_id=exec_outcome.management_batch_id, client=client)
    outcomes = finalize_executing_proposals(
        session_factory,
        config=config,
        now=NOW + timedelta(minutes=6),
        deepcoin_client_factory=lambda: client,
    )
    assert outcomes[0].state == "succeeded", outcomes[0].text
    assert _control(session_factory).auto_suspended is False


# ---------------------------------------------------------------------------
# 5. A real execution failure suspends auto after exactly one occurrence
# ---------------------------------------------------------------------------


def test_apply_time_failure_suspends_auto_immediately(tmp_path, monkeypatch):
    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="full_exit"
    )
    _enable_live_management(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _auto_config(auto_actions=frozenset({"full_exit"}))

    proposal_id, _outcome = _register_and_promote(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )

    # Real plan promotion happens inside apply_position_management_
    # remediation_action itself (it is the code that flips the batch to
    # "live" before calling execute_management_batch); patching
    # execute_management_batch at the module the remediation code imports
    # it from means the batch row already exists ("live") by the time this
    # raises, which is exactly the "apply 提升为 live 后...抛错" scenario
    # (spec section 11 item 3) -- _classify_apply_exception finds that live
    # batch row and classifies this "uncertain", not "failed".
    import telegram_kol_research.position_management_remediation as pmr

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated exchange failure after promotion")

    monkeypatch.setattr(pmr, "execute_management_batch", _boom)

    exec_outcome = execute_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1, seconds=5),
        auto_health=_health(),
    )
    assert exec_outcome.state == "uncertain", exec_outcome.text
    assert "自动补救已暂停" in (exec_outcome.text or "")

    control = _control(session_factory)
    assert control.auto_suspended is True
    assert control.auto_suspend_reason == "real_execution_uncertain"


# ---------------------------------------------------------------------------
# 6. Dormancy: shadow / approve / auto-with-empty-actions never auto-execute
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,auto_actions",
    [
        ("shadow", frozenset()),
        ("approve", frozenset()),
        ("auto", frozenset()),  # auto with nothing whitelisted -> shadow (config.effective_mode)
    ],
)
def test_dormant_modes_never_auto_execute_or_write_to_exchange(tmp_path, monkeypatch, mode, auto_actions):
    _disable_planner_reconciliation(monkeypatch)
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="full_exit"
    )
    _enable_live_management(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _auto_config(mode=mode, auto_actions=auto_actions)
    assert config.effective_mode != "auto" or not config.auto_actions

    register = register_proposal_request(
        session_factory,
        config=config,
        case_key="dormant-case",
        case_no=1,
        raw_message_id=raw_id,
        now=NOW,
    )
    outcome = compute_requested_proposal(
        session_factory,
        config=config,
        proposal_id=register.proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1),
        auto_health=_health(),
    )
    assert outcome.auto_execute_proposal_id is None
    assert outcome.state != "executing"
    assert client.close_calls == []
    assert client.set_calls == []
    assert client.cancel_trigger_calls == []
    assert client.cancel_sltp_calls == []


# ---------------------------------------------------------------------------
# 7. Runtime wiring: the background loop drives auto_execute_proposal_id
#    exactly once and never blocks the event loop.
# ---------------------------------------------------------------------------


def test_background_loop_executes_auto_proposal_exactly_once_without_blocking(monkeypatch):
    """Wiring-layer test (mirrors tests/test_oncall_remediation_wiring.py's
    own ``test_execute_proposal_locked_does_not_block_the_event_loop``):
    ``compute_requested_proposal``/``execute_proposal`` are monkeypatched at
    the ``oncall_remediation_runtime`` module boundary to isolate the wiring
    behaviour (call count, no double-execution, event loop responsiveness)
    from the real planner/executor already exercised for real above."""

    import time as time_module

    calls: list[str] = []

    def fake_compute(*_args, **kwargs):
        calls.append("compute")
        return ProposalOutcome(
            proposal_id=kwargs["proposal_id"],
            state="executing",
            refusal_reason=None,
            text=None,
            keyboard=None,
            should_send=False,
            auto_execute_proposal_id=kwargs["proposal_id"],
        )

    def slow_execute(*_args, **kwargs):
        calls.append("execute")
        time_module.sleep(0.3)
        return ExecutionOutcome(
            proposal_id=kwargs["proposal_id"], state="executing", management_batch_id=1, text=None
        )

    monkeypatch.setattr(runtime_module, "compute_requested_proposal", fake_compute)
    monkeypatch.setattr(runtime_module, "execute_proposal", slow_execute)

    async def scenario():
        gaps: list[float] = []

        async def heartbeat():
            last = asyncio.get_event_loop().time()
            while True:
                await asyncio.sleep(0.03)
                now = asyncio.get_event_loop().time()
                gaps.append(now - last)
                last = now

        heartbeat_task = asyncio.create_task(heartbeat())
        await runtime_module._process_one_requested_proposal(
            proposal_id=1,
            config=_auto_config(auto_actions=frozenset({"full_exit"})),
            session_factory=lambda: None,
            deepcoin_client_factory=lambda: object(),
            group_config_provider=lambda: None,
            bot_config=None,
            now_provider=lambda: NOW,
            auto_health_provider=lambda: AutoHealthInputs(process_started_at=NOW - timedelta(hours=1)),
        )
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        return gaps

    gaps = asyncio.run(scenario())
    assert calls == ["compute", "execute"]
    assert gaps, "heartbeat should have ticked at least once"
    assert max(gaps) < 0.2, f"event loop was blocked for {max(gaps):.3f}s"


# ---------------------------------------------------------------------------
# 8. CLI: oncall-remediation-audit matches the /audit report byte-for-byte
# ---------------------------------------------------------------------------


def test_cli_oncall_remediation_audit_matches_library_report(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import telegram_kol_research.cli as cli_module
    from telegram_kol_research.oncall_remediation import render_remediation_audit_report

    _disable_planner_reconciliation(monkeypatch)
    database_path = tmp_path / "r.db"
    session_factory = create_session_factory(database_path)
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side, pending_stop = build_ready_remediation_target(
        session_factory, action_kind="full_exit"
    )
    _enable_live_management(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    client = client_for(symbol, side, pos_id, pending=[pending_stop])
    group_config = _group_config(88)
    config = _auto_config(auto_actions=frozenset({"full_exit"}))
    proposal_id, _outcome = _register_and_promote(
        session_factory, config=config, raw_id=raw_id, client=client, group_config=group_config
    )
    execute_proposal(
        session_factory,
        config=config,
        proposal_id=proposal_id,
        deepcoin_client=client,
        group_config=group_config,
        now=NOW + timedelta(minutes=1, seconds=5),
        auto_health=_health(),
    )

    expected = render_remediation_audit_report(session_factory, proposal_id=proposal_id)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "oncall-remediation-audit",
            str(proposal_id),
            "--database-path",
            str(database_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == expected.strip()


# ---------------------------------------------------------------------------
# 9. D8's daily-cap query uses the new composite index, not a table scan
# ---------------------------------------------------------------------------


def test_d8_daily_cap_query_uses_composite_index_not_a_scan(tmp_path):
    from telegram_kol_research.oncall_remediation_auto import check_d8_auto_limits

    session_factory = create_session_factory(tmp_path / "r.db")
    config = _auto_config(auto_actions=frozenset({"full_exit"}))
    check_d8_auto_limits(
        session_factory,
        lifecycle_id=1,
        chat_id=88,
        now=NOW,
        config=config,
        exclude_proposal_id=None,
    )
    with session_factory() as session:
        plan_rows = session.execute(
            __import__("sqlalchemy").text(
                "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM oncall_remediation_proposals "
                "WHERE execution_origin = 'auto' AND executing_at >= '2026-01-01' "
                "AND executing_at < '2026-01-02'"
            )
        ).fetchall()
    plan_text = " ".join(str(row) for row in plan_rows)
    assert "SCAN" not in plan_text, plan_text
