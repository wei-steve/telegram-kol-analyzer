from __future__ import annotations

from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import DeepcoinDefiniteRejection
from telegram_kol_research.execution_events import list_execution_events
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
    StrategyManagementLeg,
)
from telegram_kol_research.strategy_management_batches import (
    ManagementLegCreate,
    create_management_batch,
    load_management_batch,
    transition_batch,
    transition_leg,
)
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def _persist_close_batch(session_factory, *, sizes=("1", "2"), symbol="BTC"):
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=20, text="exit", posted_at=NOW)
        session.add(raw)
        session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text",
            authoritative_model="mimo",
            authoritative_status="非策略",
            authoritative_payload_json="{}",
            agreement_status="authoritative_only",
            differences_json="[]",
        )
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=10,
            symbol=symbol,
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
        )
        session.add_all([decision, lifecycle])
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:10:BTC:short",
            kol_id="alice",
            chat_id=100,
            message_id=10,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            pos_id="pos-1,pos-2",
            status="active",
            last_exchange_status="positions_verified",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        entry_legs = []
        for index, pos_id in enumerate(("pos-1", "pos-2")):
            leg = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=index,
                purpose="entry",
                order_kind="market",
                order_id=f"entry-{index}",
                pos_id=pos_id,
                venue="deepcoin",
                attribution_status="verified",
                attribution_evidence_json='{"policy_version":2}',
                status="active",
            )
            session.add(leg)
            entry_legs.append(leg)
        session.commit()
        ids = (raw.id, decision.id, lifecycle.id, binding.id)
        entry_ids = tuple(leg.id for leg in entry_legs)

    return create_management_batch(
        session_factory,
        idempotency_fingerprint="a" * 64,
        raw_message_id=ids[0],
        recognition_decision_id=ids[1],
        recognition_generation="generation-1",
        target_lifecycle_id=ids[2],
        strategy_instance_id="deepcoin:100:10:BTC:short",
        execution_binding_id=ids[3],
        intent="partial_take_profit",
        effective_action="partial_close",
        requested_fraction=0.5,
        effective_fraction=0.5,
        partial_round_before=0,
        target_fingerprint="b" * 64,
        target_snapshot={"identity": {"execution_binding_id": ids[3]}},
        legs=[
            ManagementLegCreate(
                execution_order_leg_id=entry_ids[index],
                pos_id=pos_id,
                leg_index=index,
                preflight_size=str(int(size) * 2),
                planned_close_size=size,
            )
            for index, (pos_id, size) in enumerate(zip(("pos-1", "pos-2"), sizes))
        ],
        planned_at=NOW,
    )


class _FakeClient:
    def __init__(self, session_factory, outcomes=None):
        self.session_factory = session_factory
        self.outcomes = list(outcomes or [
            {"code": "0", "data": {"ordId": "close-1"}},
            {"code": "0", "data": {"ordId": "close-2"}},
        ])
        self.calls = []

    def place_order(self, payload):
        with self.session_factory() as session:
            status = (
                session.query(StrategyManagementLeg.status)
                .filter(StrategyManagementLeg.client_order_id == payload["clOrdId"])
                .scalar()
            )
        self.calls.append((dict(payload), status))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _legacy_close_signal(session_factory, batch, **overrides):
    values = {
        "venue": "deepcoin",
        "source_type": "kol_management",
        "kol_id": "alice",
        "chat_id": 100,
        "message_id": 20,
        "symbol": "BTC",
        "side": "short",
        "action": "close_position",
        "strategy_instance_id": batch.strategy_instance_id,
        "payload": {
            "management_batch_id": batch.id,
            "binding_id": batch.execution_binding_id,
        },
    }
    values.update(overrides)
    return enqueue_trade_signal(session_factory, **values)


def test_close_legs_are_committed_reserved_before_exact_market_submission(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    client = _FakeClient(session_factory)

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    assert [status for _payload, status in client.calls] == ["reserved", "reserved"]
    assert [
        {key: payload[key] for key in ("closePosId", "sz", "ordType")}
        for payload, _status in client.calls
    ] == [
        {"closePosId": "pos-1", "sz": "1", "ordType": "market"},
        {"closePosId": "pos-2", "sz": "2", "ordType": "market"},
    ]
    client_ids = [payload["clOrdId"] for payload, _status in client.calls]
    assert len(set(client_ids)) == 2
    assert all(value.isalnum() and len(value) <= 20 for value in client_ids)
    stored = load_management_batch(session_factory, batch.id)
    assert [leg.status for leg in stored.legs] == ["submitted", "submitted"]
    assert [leg.exchange_order_id for leg in stored.legs] == ["close-1", "close-2"]
    assert [leg.response["code"] for leg in stored.legs] == ["0", "0"]
    events = list_execution_events(
        session_factory, strategy_instance_id=batch.strategy_instance_id
    )
    assert {
        (event.request["managementBatchId"], event.request["managementLegId"])
        for event in events
    } == {(batch.id, leg.id) for leg in stored.legs}
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        entries = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id).all()
        assert binding.status == "active"
        assert lifecycle.lifecycle_status == "entered"
        assert [entry.status for entry in entries] == ["active", "active"]


def test_definite_failure_continues_later_leg_and_is_partial_failed(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    client = _FakeClient(
        session_factory,
        [DeepcoinDefiniteRejection("exchange rejected"), {"code": "0", "data": {"ordId": "close-2"}}],
    )

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert len(client.calls) == 2
    assert result["status"] == "partial_failed"
    assert [leg.status for leg in load_management_batch(session_factory, batch.id).legs] == [
        "failed",
        "submitted",
    ]


def test_unexpected_exception_after_request_is_unknown_and_continues(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    client = _FakeClient(
        session_factory,
        [RuntimeError("client failed after send"), {"code": "0", "data": {"ordId": "close-2"}}],
    )

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert len(client.calls) == 2
    assert result["status"] == "reconciling"
    stored = load_management_batch(session_factory, batch.id)
    assert [leg.status for leg in stored.legs] == ["submit_unknown", "submitted"]
    assert stored.legs[0].client_order_id == client.calls[0][0]["clOrdId"]


@pytest.mark.parametrize(
    "symbol",
    ["BTC", "BTC-USDT", "BTCUSDT", "BTC_USDT", "BTC-USDT-SWAP"],
)
def test_close_payload_uses_canonical_deepcoin_instrument(symbol, tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, symbol=symbol)
    client = _FakeClient(session_factory)

    execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert {payload["instId"] for payload, _status in client.calls} == {
        "BTC-USDT-SWAP"
    }


def test_timeout_is_submit_unknown_never_retried_and_later_leg_continues(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    first = _FakeClient(
        session_factory,
        [TimeoutError("lost response"), {"code": "0", "data": {"ordId": "close-2"}}],
    )
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=first, executed_at=NOW
    )
    assert result["status"] == "reconciling"
    assert [leg.status for leg in load_management_batch(session_factory, batch.id).legs] == [
        "submit_unknown",
        "submitted",
    ]

    second = _FakeClient(session_factory)
    repeated = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=second, executed_at=NOW
    )
    assert repeated["status"] == "reconciling"
    assert repeated["reason"] == "batch_already_reconciling"
    assert second.calls == []


def test_process_interruption_immediately_before_call_leaves_durable_reservation(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    interrupted = _FakeClient(session_factory, [KeyboardInterrupt()])

    with pytest.raises(KeyboardInterrupt):
        execute_management_batch(
            session_factory,
            batch_id=batch.id,
            deepcoin_client=interrupted,
            executed_at=NOW,
        )

    stored = load_management_batch(session_factory, batch.id)
    assert stored.status == "executing"
    assert stored.legs[0].status == "reserved"
    assert stored.legs[0].request["closePosId"] == "pos-1"


def test_crash_left_reserved_leg_becomes_unknown_without_resubmission(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        build_management_client_order_id,
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    assert transition_batch(
        session_factory, batch.id, expected_statuses={"ready"}, new_status="executing"
    )
    first_leg = batch.legs[0]
    client_id = build_management_client_order_id(batch_id=batch.id, leg_id=first_leg.id)
    assert transition_leg(
        session_factory,
        first_leg.id,
        expected_statuses={"planned"},
        new_status="reserved",
        client_order_id=client_id,
        request={"closePosId": first_leg.pos_id, "clOrdId": client_id},
    )
    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-2"}}])

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    assert [payload["closePosId"] for payload, _status in client.calls] == ["pos-2"]
    assert [leg.status for leg in load_management_batch(session_factory, batch.id).legs] == [
        "submit_unknown",
        "submitted",
    ]


def test_crash_after_exchange_call_before_response_persistence_never_retries(
    tmp_path, monkeypatch
):
    import telegram_kol_research.strategy_management_executor as executor

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("1", "2"))
    client = _FakeClient(session_factory)
    real_transition = executor.transition_leg
    crashed = False

    def crash_before_response_persistence(*args, **kwargs):
        nonlocal crashed
        if kwargs.get("new_status") == "submitted" and not crashed:
            crashed = True
            raise RuntimeError("simulated database interruption")
        return real_transition(*args, **kwargs)

    monkeypatch.setattr(executor, "transition_leg", crash_before_response_persistence)
    with pytest.raises(RuntimeError, match="simulated database interruption"):
        executor.execute_management_batch(
            session_factory,
            batch_id=batch.id,
            deepcoin_client=client,
            executed_at=NOW,
        )
    assert len(client.calls) == 1
    assert load_management_batch(session_factory, batch.id).legs[0].status == "reserved"

    monkeypatch.setattr(executor, "transition_leg", real_transition)
    recovery_client = _FakeClient(
        session_factory, [{"code": "0", "data": {"ordId": "close-2"}}]
    )
    result = executor.execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=recovery_client,
        executed_at=NOW,
    )

    assert result["status"] == "reconciling"
    assert [payload["closePosId"] for payload, _status in recovery_client.calls] == [
        "pos-2"
    ]
    assert [leg.status for leg in load_management_batch(session_factory, batch.id).legs] == [
        "submit_unknown",
        "submitted",
    ]


def test_non_close_or_terminal_batch_is_explicitly_fail_closed(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    assert transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="blocked",
        reason_code="operator_blocked",
    )
    with pytest.raises(ManagementBatchExecutionError, match="batch_not_executable:blocked"):
        execute_management_batch(
            session_factory,
            batch_id=batch.id,
            deepcoin_client=_FakeClient(session_factory),
            executed_at=NOW,
        )


@pytest.mark.parametrize("mode", ["disabled", "shadow"])
def test_legacy_batch_delegation_requires_live_management_mode(mode, tmp_path):
    from telegram_kol_research.deepcoin_execution_actions import (
        DeepcoinExecutionActionError,
        execute_deepcoin_management_signal,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    signal = _legacy_close_signal(session_factory, batch)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "management_execution_mode": mode},
    )
    client = _FakeClient(session_factory)

    with pytest.raises(
        DeepcoinExecutionActionError, match="management_live_execution_disabled"
    ):
        execute_deepcoin_management_signal(
            session_factory, trade_signal=signal, deepcoin_client=client
        )

    assert client.calls == []
    assert load_management_batch(session_factory, batch.id).status == "ready"


@pytest.mark.parametrize(
    "override",
    [
        {"message_id": 21},
        {"strategy_instance_id": "deepcoin:100:999:BTC:short"},
        {"payload": {"management_batch_id": 1, "binding_id": 999}},
    ],
)
def test_legacy_batch_delegation_rejects_signal_identity_mismatch(override, tmp_path):
    from telegram_kol_research.deepcoin_execution_actions import (
        DeepcoinExecutionActionError,
        execute_deepcoin_management_signal,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    if "payload" in override:
        override = {
            "payload": {
                **override["payload"],
                "management_batch_id": batch.id,
            }
        }
    signal = _legacy_close_signal(session_factory, batch, **override)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "management_execution_mode": "live"},
    )
    client = _FakeClient(session_factory)

    with pytest.raises(
        DeepcoinExecutionActionError, match="management_signal_batch_identity_mismatch"
    ):
        execute_deepcoin_management_signal(
            session_factory, trade_signal=signal, deepcoin_client=client
        )

    assert client.calls == []
    assert load_management_batch(session_factory, batch.id).status == "ready"


def test_matching_legacy_signal_delegates_valid_live_batch(tmp_path):
    from telegram_kol_research.deepcoin_execution_actions import (
        execute_deepcoin_management_signal,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    signal = _legacy_close_signal(session_factory, batch)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "management_execution_mode": "live"},
    )
    client = _FakeClient(session_factory)

    result = execute_deepcoin_management_signal(
        session_factory, trade_signal=signal, deepcoin_client=client
    )

    assert result["status"] == "reconciling"
    assert len(client.calls) == 2
