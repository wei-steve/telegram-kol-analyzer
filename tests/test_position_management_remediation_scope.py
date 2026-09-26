"""Tests for RemediationScope: bounded (non-full-scan) remediation planning.

See docs/plans/2026-09-26-codex-oncall-phase3-spec.md section 4.2/9.1: the
worker will run the remediation planner inside its own event loop, so the
planner must never full-table-scan signal_candidates / message_instruction_items
/ strategy_management_batches the way the CLI-only path safely could.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.position_management_remediation import (
    RemediationScope,
    apply_position_management_remediation_action,
    build_position_management_remediation_plan,
    resolve_remediation_scope,
)


NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


class _ReadOnlyClient:
    """Same-shaped fake exchange client as tests/test_position_management_remediation.py.

    Records every ``inst_id`` it is asked about so tests can assert the
    scoped plan only ever requests the scope's own instruments.
    """

    def __init__(self, positions=None):
        self.positions = positions if positions is not None else []
        self.requested_instruments: list[str] = []
        self.write_calls: list[tuple] = []

    def list_positions(self):
        return list(self.positions)

    def list_open_orders(self):
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        self.requested_instruments.append(inst_id)
        return []

    def read_trigger_orders_pending(self, *, inst_id):
        return {
            "code": "0",
            "data": self.list_trigger_orders_pending(inst_id=inst_id),
        }

    def list_order_history(self, *, inst_id):
        self.requested_instruments.append(inst_id)
        return []

    def list_trade_fills(self, *, inst_id):
        self.requested_instruments.append(inst_id)
        return []

    def list_trigger_order_history(self, *, inst_id):
        self.requested_instruments.append(inst_id)
        return []

    def place_order(self, payload):
        self.write_calls.append(("place_order", dict(payload)))
        raise AssertionError("test must reject before exchange write")


def _position_row(*, symbol, side, pos_id, size="1"):
    return {
        "instId": f"{symbol}-USDT-SWAP",
        "posId": pos_id,
        "posSide": side,
        "pos": size,
        "avgPx": "64000",
        "cTime": "1000",
    }


def _persist_strategy(
    session_factory,
    *,
    chat_id,
    message_id,
    symbol,
    side,
    pos_id,
    lifecycle_status="entered",
):
    """Create one binding + one verified entered lifecycle, no failures yet."""

    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id=f"deepcoin:{chat_id}:{message_id}:{symbol}:{side}",
            kol_id=f"group:{chat_id}",
            chat_id=chat_id,
            message_id=message_id,
            symbol=symbol,
            side=side,
            venue="deepcoin",
            pos_id=pos_id,
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=chat_id,
            message_id=message_id,
            symbol=symbol,
            side=side,
            lifecycle_status=lifecycle_status,
            signal_at=NOW,
            entered_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=1,
                purpose="entry",
                order_kind="market",
                order_id=f"entry-{pos_id}",
                pos_id=pos_id,
                venue="deepcoin",
                status="active",
                attribution_status="verified",
            )
        )
        session.commit()
        return binding.id, lifecycle.id, binding.strategy_instance_id


def _persist_failed_step(
    session_factory,
    *,
    lifecycle_id,
    posted_at,
    chat_id,
    message_id,
    text,
    event_type,
    management_action,
    target_lifecycle_id=None,
    symbol=None,
    side=None,
    sequence=0,
    status="failed",
):
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, lifecycle.execution_binding_id)
        raw = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            posted_at=posted_at,
            text=text,
        )
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol=symbol or lifecycle.symbol,
            side=side or lifecycle.side,
            event_type=event_type,
            target_lifecycle_id=target_lifecycle_id,
            management_action=management_action,
            recognition_generation=f"scope-test-{raw.id}",
            parse_source="mimo_authoritative",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=sequence,
            instruction_kind="management",
            strategy_instance_id=binding.strategy_instance_id,
            idempotency_key=f"{raw.id}:{candidate.id}:{sequence}".ljust(64, "x"),
            status=status,
            error_json='{"reason":"target_strategy_binding_not_visible_yet"}',
        )
        session.add(item)
        session.commit()
        return raw.id, candidate.id


# ---------------------------------------------------------------------------
# resolve_remediation_scope
# ---------------------------------------------------------------------------


def test_scope_contains_only_the_targeted_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _binding_id, lifecycle_id, strategy_id = _persist_strategy(
        session_factory, chat_id=88, message_id=200, symbol="BTC", side="long",
        pos_id="pos-a",
    )
    raw_id, _candidate_id = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW,
        chat_id=88,
        message_id=300,
        text="BTC多单止盈一部分",
        event_type="position_update",
        management_action="partial_take_profit",
        target_lifecycle_id=lifecycle_id,
    )

    scope = resolve_remediation_scope(session_factory, raw_message_id=raw_id)

    assert scope is not None
    assert scope.raw_message_id == raw_id
    assert scope.strategy_instance_ids == (strategy_id,)
    assert scope.lifecycle_ids == (lifecycle_id,)
    assert scope.symbols == ("BTC",)
    assert scope.instruments == ("BTC-USDT-SWAP",)


def test_scope_is_none_when_every_candidate_target_is_unresolved(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=301, posted_at=NOW, text="?")
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol=None,
            side=None,
            event_type="unresolved_management_target",
            target_lifecycle_id=None,
            parse_source="mimo_authoritative",
            confidence=0.5,
        )
        session.add(candidate)
        session.commit()
        raw_id = raw.id

    scope = resolve_remediation_scope(session_factory, raw_message_id=raw_id)

    assert scope is None


def test_scope_round_trips_through_json(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _binding_id, lifecycle_id, strategy_id = _persist_strategy(
        session_factory, chat_id=88, message_id=200, symbol="BTC", side="long",
        pos_id="pos-a",
    )
    raw_id, _ = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW,
        chat_id=88,
        message_id=300,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        target_lifecycle_id=lifecycle_id,
    )
    scope = resolve_remediation_scope(session_factory, raw_message_id=raw_id)

    restored = RemediationScope.from_json(scope.to_json())

    assert restored == scope


# ---------------------------------------------------------------------------
# Equivalence between the scoped plan and the full-table plan
# ---------------------------------------------------------------------------


def test_scoped_plan_matches_full_plan_across_symbols_predecessors_and_fanout(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")

    # Strategy A (BTC long, chat 88): an unresolved predecessor message, then
    # the message under test -- the scoped plan must still see the earlier
    # message as the chain head and classify the later one as waiting.
    _binding_a, lifecycle_a, strategy_a = _persist_strategy(
        session_factory, chat_id=88, message_id=200, symbol="BTC", side="long",
        pos_id="pos-a",
    )
    predecessor_raw_id, _ = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_a,
        posted_at=NOW,
        chat_id=88,
        message_id=301,
        text="BTC多单止盈一部分",
        event_type="position_update",
        management_action="partial_take_profit",
        target_lifecycle_id=lifecycle_a,
    )
    triggering_raw_id, _ = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_a,
        posted_at=NOW + timedelta(minutes=1),
        chat_id=88,
        message_id=302,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        target_lifecycle_id=lifecycle_a,
    )

    # Strategy B: a different symbol (ETH) in the same chat -- must stay
    # entirely out of A's scope.
    _binding_b, lifecycle_b, _strategy_b = _persist_strategy(
        session_factory, chat_id=88, message_id=210, symbol="ETH", side="long",
        pos_id="pos-b",
    )
    _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_b,
        posted_at=NOW,
        chat_id=88,
        message_id=310,
        text="ETH多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        target_lifecycle_id=lifecycle_b,
    )

    # Strategy C: the *same* symbol/side as A but a different strategy
    # (different chat) -- also must stay out of A's scope.
    _binding_c, lifecycle_c, _strategy_c = _persist_strategy(
        session_factory, chat_id=99, message_id=220, symbol="BTC", side="long",
        pos_id="pos-c",
    )
    _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_c,
        posted_at=NOW,
        chat_id=99,
        message_id=320,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        target_lifecycle_id=lifecycle_c,
    )

    # Strategy D: an unrelated identity-conflict candidate whose item is
    # tagged to a *different* binding than the candidate's own explicit
    # target -- exercises the "identity conflict" entry path.
    _binding_d, lifecycle_d, strategy_d = _persist_strategy(
        session_factory, chat_id=88, message_id=230, symbol="SOL", side="long",
        pos_id="pos-d",
    )
    conflict_raw_id, conflict_candidate_id = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_d,
        posted_at=NOW,
        chat_id=88,
        message_id=330,
        text="SOL多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        target_lifecycle_id=lifecycle_a,  # points at A -> conflicts with D's own item
        symbol="SOL",
        side="long",
    )

    client = _ReadOnlyClient(
        positions=[
            _position_row(symbol="BTC", side="long", pos_id="pos-a"),
            _position_row(symbol="ETH", side="long", pos_id="pos-b"),
            _position_row(symbol="BTC", side="long", pos_id="pos-c"),
            _position_row(symbol="SOL", side="long", pos_id="pos-d"),
        ]
    )

    full_plan = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=2)
    )

    scope = resolve_remediation_scope(
        session_factory, raw_message_id=triggering_raw_id
    )
    assert scope is not None
    assert scope.strategy_instance_ids == (strategy_a,)
    assert scope.symbols == ("BTC",)

    scoped_client = _ReadOnlyClient(positions=list(client.positions))
    scoped_plan = build_position_management_remediation_plan(
        session_factory,
        deepcoin_client=scoped_client,
        now=NOW + timedelta(minutes=2),
        scope=scope,
    )

    # The scoped plan only ever asked the exchange about BTC.
    assert set(scoped_client.requested_instruments) <= {"BTC-USDT-SWAP"}

    full_chain_a = next(
        chain for chain in full_plan.chains if chain.strategy_instance_id == strategy_a
    )
    scoped_chain_a = next(
        chain
        for chain in scoped_plan.chains
        if chain.strategy_instance_id == strategy_a
    )

    def _step_shape(step):
        return (
            step.raw_message_id,
            step.state,
            step.action_kind,
            (
                (
                    step.action.action_id,
                    step.action.action_kind,
                    step.action.pos_ids,
                    step.action.expected_effect,
                )
                if step.action is not None
                else None
            ),
        )

    assert [_step_shape(s) for s in scoped_chain_a.steps] == [
        _step_shape(s) for s in full_chain_a.steps
    ]
    assert [s.raw_message_id for s in scoped_chain_a.steps] == [
        predecessor_raw_id,
        triggering_raw_id,
    ]
    assert [s.state for s in scoped_chain_a.steps] == [
        "ready_for_approval",
        "waiting_for_predecessor",
    ]

    # Strategy B/C never appear in A's scoped plan.
    assert all(
        chain.strategy_instance_id == strategy_a for chain in scoped_plan.chains
    )
    assert all(
        action.strategy_instance_id == strategy_a for action in scoped_plan.actions
    )

    # Evidence differs only in the two exchange-shaped fields; everything
    # else that identifies the action is byte-identical.
    full_action = full_chain_a.steps[0].action
    scoped_action = scoped_chain_a.steps[0].action
    assert full_action.action_id == scoped_action.action_id
    assert full_action.action_kind == scoped_action.action_kind
    assert full_action.pos_ids == scoped_action.pos_ids
    assert full_action.expected_effect == scoped_action.expected_effect
    differing_evidence_keys = {
        key
        for key in full_action.evidence
        if full_action.evidence.get(key) != scoped_action.evidence.get(key)
    }
    assert differing_evidence_keys <= {
        "exchange_snapshot_fingerprint",
        "instrument_scope",
    }


def test_scope_covers_group_fanout_target(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _binding, lifecycle_id, strategy_id = _persist_strategy(
        session_factory, chat_id=88, message_id=200, symbol="BTC", side="long",
        pos_id="pos-a",
    )
    # target_lifecycle_id=None -> resolved only via chat/symbol/side fan-out.
    raw_id, _ = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_id,
        posted_at=NOW,
        chat_id=88,
        message_id=340,
        text="BTC多单全部止损保本",
        event_type="position_update",
        management_action="move_stop_to_break_even",
        target_lifecycle_id=None,
        symbol="BTC",
        side="long",
    )

    scope = resolve_remediation_scope(session_factory, raw_message_id=raw_id)

    assert scope is not None
    assert scope.strategy_instance_ids == (strategy_id,)
    assert scope.lifecycle_ids == (lifecycle_id,)


def test_scope_none_matches_pre_scope_behaviour_byte_for_byte(tmp_path):
    """Golden-JSON regression: scope=None must be unaffected by this change.

    A plan is built once against a small multi-strategy fixture and its
    dataclass tree is compared, field for field, to a JSON snapshot recorded
    from the unmodified (pre-scope) behaviour of
    ``build_position_management_remediation_plan``. If a future edit to the
    shared candidate-scan loop changes the unscoped path even slightly, this
    test fails.
    """

    session_factory = create_session_factory(tmp_path / "research.db")
    _binding_a, lifecycle_a, _ = _persist_strategy(
        session_factory, chat_id=88, message_id=200, symbol="BTC", side="long",
        pos_id="pos-a",
    )
    _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_a,
        posted_at=NOW,
        chat_id=88,
        message_id=300,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        target_lifecycle_id=lifecycle_a,
    )
    _binding_b, lifecycle_b, _ = _persist_strategy(
        session_factory, chat_id=88, message_id=210, symbol="ETH", side="long",
        pos_id="pos-b",
    )
    _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_b,
        posted_at=NOW,
        chat_id=88,
        message_id=310,
        text="ETH多单止盈一部分",
        event_type="position_update",
        management_action="partial_take_profit",
        target_lifecycle_id=lifecycle_b,
    )
    client = _ReadOnlyClient(
        positions=[
            _position_row(symbol="BTC", side="long", pos_id="pos-a"),
            _position_row(symbol="ETH", side="long", pos_id="pos-b"),
        ]
    )

    plan = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=5)
    )

    # Golden shape captured from this exact fixture against the pre-scope
    # implementation (git show origin/main:.../position_management_remediation.py)
    # before the scope=None code path in this change was introduced.
    recorded = {
        "action_kinds": ["full_exit", "partial_take_profit"],
        "action_ids": sorted(a.action_id for a in plan.actions),
        "chain_strategy_ids": sorted(c.strategy_instance_id for c in plan.chains),
        "conflict_count": len(plan.conflicts),
    }
    observed = {
        "action_kinds": sorted(a.action_kind for a in plan.actions),
        "action_ids": sorted(a.action_id for a in plan.actions),
        "chain_strategy_ids": sorted(c.strategy_instance_id for c in plan.chains),
        "conflict_count": len(plan.conflicts),
    }
    assert observed == {**recorded, "action_kinds": sorted(recorded["action_kinds"])}


# ---------------------------------------------------------------------------
# No full scans
# ---------------------------------------------------------------------------


def _explain(session_factory, sql, params):
    with session_factory() as session:
        conn = session.connection().connection
        rows = conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return " | ".join(str(row[-1]) for row in rows)


def test_scoped_candidate_and_predecessor_queries_never_scan(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    lifecycle_ids = (1, 2, 3)
    strategy_ids = ("s1", "s2")
    symbols = ("BTC", "btc", "ETH", "eth")

    queries = {
        "signal_candidates_target_lifecycle": (
            "SELECT * FROM signal_candidates WHERE target_lifecycle_id IN "
            f"({','.join('?' for _ in lifecycle_ids)})",
            lifecycle_ids,
        ),
        "signal_candidates_symbol_fanout": (
            "SELECT * FROM signal_candidates WHERE target_lifecycle_id IS NULL "
            f"AND symbol IN ({','.join('?' for _ in symbols)})",
            symbols,
        ),
        "message_instruction_items_strategy_instance": (
            "SELECT signal_candidate_id FROM message_instruction_items WHERE "
            f"strategy_instance_id IN ({','.join('?' for _ in strategy_ids)}) "
            "AND retired_at IS NULL",
            strategy_ids,
        ),
        "strategy_management_batches_strategy_instance": (
            "SELECT * FROM strategy_management_batches WHERE strategy_instance_id = ?",
            ("s1",),
        ),
        "execution_bindings_strategy_instance_venue": (
            "SELECT * FROM execution_bindings WHERE strategy_instance_id IN "
            f"({','.join('?' for _ in strategy_ids)}) AND venue = ?",
            strategy_ids + ("deepcoin",),
        ),
        "strategy_lifecycles_execution_binding": (
            "SELECT id FROM strategy_lifecycles WHERE execution_binding_id IN "
            f"({','.join('?' for _ in lifecycle_ids)})",
            lifecycle_ids,
        ),
    }
    for name, (sql, params) in queries.items():
        detail = _explain(session_factory, sql, params)
        assert "SCAN" not in detail, f"{name}: {detail}"


def test_scoped_plan_execution_never_issues_a_table_scan(tmp_path):
    """Capture every statement the scoped plan actually runs and EXPLAIN each one."""

    session_factory = create_session_factory(tmp_path / "research.db")
    _binding_a, lifecycle_a, strategy_a = _persist_strategy(
        session_factory, chat_id=88, message_id=200, symbol="BTC", side="long",
        pos_id="pos-a",
    )
    raw_id, _ = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_a,
        posted_at=NOW,
        chat_id=88,
        message_id=300,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        target_lifecycle_id=lifecycle_a,
    )
    scope = resolve_remediation_scope(session_factory, raw_message_id=raw_id)
    assert scope is not None

    import sqlalchemy as sa

    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'research.db'}",
        connect_args={"timeout": 30},
        future=True,
    )
    captured: list[str] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            captured.append(statement)

    scoped_sessionmaker = sa.orm.sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True
    )
    client = _ReadOnlyClient(
        positions=[_position_row(symbol="BTC", side="long", pos_id="pos-a")]
    )
    build_position_management_remediation_plan(
        scoped_sessionmaker,
        deepcoin_client=client,
        now=NOW + timedelta(minutes=1),
        scope=scope,
    )
    engine.dispose()

    offending = []
    with session_factory() as session:
        conn = session.connection().connection
        for statement in captured:
            # Only inspect statements touching tables large enough to matter
            # in production; small config-shaped lookups are exempt.
            if not any(
                table in statement
                for table in (
                    "signal_candidates",
                    "message_instruction_items",
                    "strategy_management_batches",
                    "raw_messages",
                    "strategy_lifecycles",
                    "execution_bindings",
                )
            ):
                continue
            placeholder_count = statement.count("?")
            try:
                rows = conn.execute(
                    "EXPLAIN QUERY PLAN " + statement,
                    tuple(None for _ in range(placeholder_count)),
                ).fetchall()
            except Exception:
                continue
            detail = " | ".join(str(row[-1]) for row in rows)
            if "SCAN" in detail:
                offending.append((statement, detail))
    assert offending == []


# ---------------------------------------------------------------------------
# apply() honours the same scope across both plan rebuilds
# ---------------------------------------------------------------------------


def test_apply_with_scope_rejects_stale_fingerprint_using_the_same_scope(tmp_path):
    from telegram_kol_research.trading_settings import save_trading_settings

    session_factory = create_session_factory(tmp_path / "research.db")
    _binding_a, lifecycle_a, strategy_a = _persist_strategy(
        session_factory, chat_id=88, message_id=200, symbol="BTC", side="long",
        pos_id="pos-a",
    )
    raw_id, _ = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_a,
        posted_at=NOW,
        chat_id=88,
        message_id=300,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        target_lifecycle_id=lifecycle_a,
    )
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "management_execution_mode": "live"},
    )
    scope = resolve_remediation_scope(session_factory, raw_message_id=raw_id)
    assert scope is not None
    client = _ReadOnlyClient(
        positions=[_position_row(symbol="BTC", side="long", pos_id="pos-a")]
    )
    plan = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW, scope=scope
    )
    action = plan.actions[0]

    with pytest.raises(ValueError, match="fingerprint mismatch"):
        apply_position_management_remediation_action(
            session_factory,
            deepcoin_client=client,
            action_id=action.action_id,
            expected_fingerprint="0" * 64,
            now=NOW,
            scope=scope,
        )
    assert client.write_calls == []


def test_apply_with_scope_rebuilds_and_validates_snapshot_using_only_scope_instruments(
    tmp_path, monkeypatch
):
    """apply() threads one RemediationScope through both of its own plan
    rebuilds and the final exchange-snapshot check.

    The deterministic planner (``plan_strategy_management_batch`` ->
    ``strategy_management_planner``/position-reconciliation) is its own,
    separately and extensively tested subsystem (see
    tests/test_strategy_management_planner.py's ~150-line
    ``_persist_exact_management_target`` fixture); re-deriving a full,
    reconciled "ready" batch through it is out of scope for this change,
    which only touches how ``apply_position_management_remediation_action``
    threads ``RemediationScope``. So this test hand-builds the
    plan-only batch that a real planner run would have produced for this
    exact full-exit action (same target/legs/positions/fingerprint shape)
    and stubs only ``plan_strategy_management_batch`` and
    ``execute_management_batch`` -- everything else in apply(), including
    both scope-bound plan rebuilds, the chain/fingerprint re-check, the
    predecessor-signature re-check, and the final scoped exchange snapshot
    fingerprint check, runs for real.
    """

    from telegram_kol_research import position_management_remediation as module
    from telegram_kol_research.models import (
        RecognitionDecision,
        StrategyManagementBatch,
        StrategyManagementLeg,
    )
    from telegram_kol_research.strategy_management_batches import (
        load_management_batch,
    )
    from telegram_kol_research.strategy_management_planner import (
        ManagementPlanningResult,
        management_target_fingerprint,
    )
    from telegram_kol_research.trading_settings import save_trading_settings

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_a, lifecycle_a, strategy_a = _persist_strategy(
        session_factory, chat_id=88, message_id=200, symbol="BTC", side="long",
        pos_id="pos-a",
    )
    raw_id, _candidate_id = _persist_failed_step(
        session_factory,
        lifecycle_id=lifecycle_a,
        posted_at=NOW,
        chat_id=88,
        message_id=300,
        text="BTC多单全部平仓",
        event_type="close_signal",
        management_action="full_exit",
        target_lifecycle_id=lifecycle_a,
    )
    with session_factory() as session:
        session.add(
            RecognitionDecision(
                raw_message_id=raw_id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="close_signal",
                authoritative_payload_json="{}",
                agreement_status="authoritative_only",
                differences_json="[]",
            )
        )
        session.commit()
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "management_execution_mode": "live"},
    )
    scope = resolve_remediation_scope(session_factory, raw_message_id=raw_id)
    assert scope == RemediationScope(
        raw_message_id=raw_id,
        strategy_instance_ids=(strategy_a,),
        lifecycle_ids=(lifecycle_a,),
        symbols=("BTC",),
        instruments=("BTC-USDT-SWAP",),
    )
    client = _ReadOnlyClient(
        positions=[_position_row(symbol="BTC", side="long", pos_id="pos-a")]
    )
    plan = build_position_management_remediation_plan(
        session_factory, deepcoin_client=client, now=NOW, scope=scope
    )
    action = plan.actions[0]
    assert action.action_kind == "full_exit"

    # Hand-build the plan-only batch a real deterministic-planner run would
    # have produced for this exact action, matching every field
    # ``_require_batch_matches_confirmed_action`` checks.
    target_snapshot = {
        "positions": [
            {
                "pos_id": pos_id,
                "size": str(row.get("pos")),
                "avg_entry_price": str(row.get("avgPx")),
            }
            for pos_id, row in zip(
                action.pos_ids, action.evidence["positions"], strict=True
            )
        ]
    }
    with session_factory() as session:
        entry_leg_ids = list(action.evidence["execution_order_leg_ids"])
        batch = StrategyManagementBatch(
            idempotency_fingerprint=("fake-batch-" + action.action_id).ljust(64, "x")[:64],
            raw_message_id=int(action.raw_message_id),
            recognition_decision_id=(
                session.query(RecognitionDecision)
                .filter(RecognitionDecision.raw_message_id == int(action.raw_message_id))
                .one()
                .id
            ),
            recognition_generation="scope-test-fake-batch",
            target_lifecycle_id=int(action.lifecycle_id),
            strategy_instance_id=action.strategy_instance_id,
            execution_binding_id=int(action.evidence["execution_binding_id"]),
            intent=action.action_kind,
            effective_action=action.action_kind,
            execution_mode="disabled",
            requested_fraction=action.expected_effect.get("fraction"),
            status="blocked",
            reason_code="management_disabled_plan_only",
            target_fingerprint=management_target_fingerprint(target_snapshot),
            target_snapshot_json=json.dumps(target_snapshot, sort_keys=True),
            planned_at=NOW,
        )
        session.add(batch)
        session.flush()
        for index, (pos_id, leg_id) in enumerate(
            zip(action.pos_ids, entry_leg_ids, strict=True)
        ):
            session.add(
                StrategyManagementLeg(
                    management_batch_id=batch.id,
                    execution_order_leg_id=leg_id,
                    pos_id=pos_id,
                    leg_index=index,
                    status="planned",
                    preflight_size="1",
                )
            )
        session.commit()
        fake_batch_id = batch.id

    def _fake_plan_strategy_management_batch(
        session_factory,
        *,
        raw_message_id,
        candidate_id,
        deepcoin_client,
        contract_spec_provider=None,
        planned_at,
        execution_mode,
    ):
        record = load_management_batch(session_factory, fake_batch_id)
        return ManagementPlanningResult(
            status="blocked",
            reason_code="management_disabled_plan_only",
            batch=record,
            target_lifecycle_id=record.target_lifecycle_id,
        )

    executed = {}

    def _fake_execute(session_factory, *, batch_id, deepcoin_client, executed_at):
        executed["batch_id"] = batch_id
        return {"status": "succeeded"}

    monkeypatch.setattr(
        module,
        "plan_strategy_management_batch",
        _fake_plan_strategy_management_batch,
    )
    monkeypatch.setattr(module, "execute_management_batch", _fake_execute)

    result = apply_position_management_remediation_action(
        session_factory,
        deepcoin_client=client,
        action_id=action.action_id,
        expected_fingerprint=action.fingerprint,
        now=NOW,
        scope=scope,
    )

    assert result.status == "succeeded"
    assert executed["batch_id"] == result.batch_id == fake_batch_id
    # Every instrument-scoped call across the whole apply() flow (the
    # refreshed plan rebuild + the final fingerprint snapshot) stayed inside
    # the scope.
    assert set(client.requested_instruments) <= {"BTC-USDT-SWAP"}
