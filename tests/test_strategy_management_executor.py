from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import (
    DeepcoinDefiniteRejection,
    DeepcoinRequestOutcomeUnknown,
)
from telegram_kol_research.execution_events import list_execution_events
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
)
from telegram_kol_research.protection_attribution import snapshot_protection_rows
from telegram_kol_research.protection_ledger import (
    list_verified_ledger_rows_for_positions,
    upsert_protection_ledger_row,
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


def _persist_close_batch(
    session_factory,
    *,
    sizes=("1", "2"),
    symbol="BTC",
    intent="partial_take_profit",
    effective_action="partial_close",
):
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
        intent=intent,
        effective_action=effective_action,
        requested_fraction=0.5,
        effective_fraction=0.5,
        partial_round_before=0,
        target_fingerprint="b" * 64,
        target_snapshot={
            "identity": {"execution_binding_id": ids[3]},
            "contract_spec": {
                "instrument_id": "BTC-USDT-SWAP",
                "quantity_step": 1,
                "min_quantity": 1,
            },
        },
        legs=[
            ManagementLegCreate(
                execution_order_leg_id=entry_ids[index],
                pos_id=pos_id,
                leg_index=index,
                preflight_size=(
                    size if effective_action == "full_exit" else str(int(size) * 2)
                ),
                planned_close_size=size,
                quantity_step="1",
            )
            for index, (pos_id, size) in enumerate(zip(("pos-1", "pos-2"), sizes))
        ],
        planned_at=NOW,
    )


def _persist_protection_batch(
    session_factory,
    *,
    action="adjust_stop_loss",
    stop_loss="65000",
    keep_close_plan=False,
):
    batch = _persist_close_batch(session_factory, sizes=("1", "2"), symbol="BTC")
    rows_by_pos = {
        "pos-1": [
            {
                "triggerOrderType": "TPSL",
                "ordId": "tp-1a",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "posId": "pos-1",
                "tpTriggerPx": "63000",
                "tpTriggerPxType": "mark",
                "tpOrdPx": "-1",
                "sz": "1",
                "cTime": "1000",
            },
            {
                "triggerOrderType": "TPSL",
                "ordId": "tp-1b",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "posId": "pos-1",
                "tpTriggerPx": "62000",
                "tpTriggerPxType": "last",
                "tpOrdPx": "61990",
                "sz": "1",
                "cTime": "1000",
            },
            {
                "triggerOrderType": "TPSL",
                "ordId": "sl-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "posId": "pos-1",
                "slTriggerPx": "65500",
                "slTriggerPxType": "index",
                "slOrdPx": "-1",
                "sz": "0",
                "cTime": "1000",
            },
        ],
        "pos-2": [
            {
                "triggerOrderType": "TPSL",
                "ordId": "tp-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "posId": "pos-2",
                "tpTriggerPx": "62500",
                "tpTriggerPxType": "last",
                "tpOrdPx": "-1",
                "sz": "4",
                "cTime": "1001",
            },
            {
                "triggerOrderType": "TPSL",
                "ordId": "sl-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "posId": "pos-2",
                "slTriggerPx": "65700",
                "slTriggerPxType": "last",
                "slOrdPx": "-1",
                "sz": "0",
                "cTime": "1001",
            },
        ],
    }
    with session_factory() as session:
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        stored_batch.intent = action
        stored_batch.effective_action = action
        stored_batch.effective_fraction = None
        stored_batch.requested_fraction = None
        legs = (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == batch.id)
            .order_by(StrategyManagementLeg.leg_index)
            .all()
        )
        for leg, pos_id, size, avg_px in zip(
            legs, ("pos-1", "pos-2"), ("2", "4"), ("64000", "64500")
        ):
            rows = rows_by_pos[pos_id]
            if not keep_close_plan:
                leg.planned_close_size = None
            leg.preflight_size = size
            leg.avg_entry_price = avg_px
            leg.old_tpsl_json = json.dumps(
                {
                    "status": "verified",
                    "order_ids": [row["ordId"] for row in rows],
                    "rows": rows,
                    "row_snapshots": snapshot_protection_rows(rows),
                }
            )
            leg.planned_tpsl_json = json.dumps(
                {"intent": action, "stop_loss_text": stop_loss}
            )
        session.commit()
    return load_management_batch(session_factory, batch.id), rows_by_pos


class _FakeClient:
    def __init__(self, session_factory, outcomes=None):
        self.session_factory = session_factory
        self.outcomes = list(outcomes or [
            {"code": "0", "data": {"ordId": "close-1"}},
            {"code": "0", "data": {"ordId": "close-2"}},
        ])
        self.calls = []

    def list_positions(self, *, inst_id=None):
        with self.session_factory() as session:
            legs = (
                session.query(StrategyManagementLeg)
                .order_by(StrategyManagementLeg.leg_index)
                .all()
            )
            binding = session.query(ExecutionBinding).first()
            return [
                {
                    "posId": leg.pos_id,
                    "instId": "BTC-USDT-SWAP",
                    "posSide": binding.side,
                    "pos": leg.preflight_size,
                }
                for leg in legs
            ]

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


class _ProtectionClient:
    def __init__(
        self,
        session_factory,
        rows_by_pos,
        *,
        set_outcomes=None,
        cancel_outcomes=None,
    ):
        self.session_factory = session_factory
        self.positions = [
            {
                "posId": "pos-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "2",
                "avgPx": "64000",
                "cTime": "1000",
            },
            {
                "posId": "pos-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "4",
                "avgPx": "64500",
                "cTime": "1001",
            },
        ]
        self.pending = [dict(row) for rows in rows_by_pos.values() for row in rows]
        self.set_outcomes = list(set_outcomes or [])
        self.cancel_outcomes = list(cancel_outcomes or [])
        self.cancel_calls = []
        self.set_calls = []
        self.pending_reads = 0
        self.close_calls = []

    def list_positions(self, *, inst_id=None):
        return [dict(row) for row in self.positions]

    def list_trigger_orders_pending(self, *, inst_id):
        self.pending_reads += 1
        return [dict(row) for row in self.pending]

    def place_order(self, payload):
        self.close_calls.append(dict(payload))
        return {
            "code": "0",
            "data": {"ordId": f"close-{len(self.close_calls)}"},
        }

    def cancel_position_sltp(self, payload):
        self.cancel_calls.append(dict(payload))
        if self.cancel_outcomes:
            outcome = self.cancel_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return {"code": "0", "data": {"ordId": payload["ordId"]}}

    def set_position_sltp(self, payload):
        self.set_calls.append(dict(payload))
        if self.set_outcomes:
            outcome = self.set_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return {"code": "0", "data": {"ordId": f"new-{len(self.set_calls)}"}}


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


def test_close_batch_accepts_verified_entry_subset_with_pending_range_leg(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("3", "2"))
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        binding.pos_id = "pos-1"
        second_batch_leg = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=batch.id, pos_id="pos-2")
            .one()
        )
        session.delete(second_batch_leg)
        second_entry = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=batch.execution_binding_id, pos_id="pos-2")
            .one()
        )
        second_entry.order_kind = "trigger_limit"
        second_entry.pos_id = None
        second_entry.status = "pending"
        second_entry.attribution_status = "unassigned"
        second_entry.attribution_evidence_json = None
        session.commit()

    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    assert [payload["closePosId"] for payload, _status in client.calls] == ["pos-1"]
    stored = load_management_batch(session_factory, batch.id)
    assert [leg.pos_id for leg in stored.legs] == ["pos-1"]
    assert stored.legs[0].status == "submitted"


@pytest.mark.parametrize(
    ("status", "attribution_status", "pos_id"),
    [
        ("partially_filled", "unassigned", None),
        ("pending", "unassigned", "pos-unsafe"),
        ("pending", "attribution_conflict", None),
        ("pending", "evidence_unavailable", None),
    ],
)
def test_close_batch_rejects_unsafe_deferred_range_leg(
    tmp_path, status, attribution_status, pos_id
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("3", "2"))
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        binding.pos_id = "pos-1"
        second_batch_leg = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=batch.id, pos_id="pos-2")
            .one()
        )
        session.delete(second_batch_leg)
        second_entry = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=batch.execution_binding_id, pos_id="pos-2")
            .one()
        )
        second_entry.order_kind = "trigger_limit"
        second_entry.pos_id = pos_id
        second_entry.status = status
        second_entry.attribution_status = attribution_status
        second_entry.attribution_evidence_json = None
        session.commit()

    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])

    with pytest.raises(
        ManagementBatchExecutionError, match="batch_entry_set_not_exact"
    ):
        execute_management_batch(
            session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
        )

    assert client.calls == []


@pytest.mark.parametrize("failure", ["snapshot_error", "size_drift", "missing", "extra"])
def test_sync_live_final_close_preflight_freezes_before_place_order(
    failure, tmp_path
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)

    class Client(_FakeClient):
        def list_positions(self, *, inst_id=None):
            if failure == "snapshot_error":
                raise RuntimeError("snapshot unavailable")
            rows = super().list_positions(inst_id=inst_id)
            if failure == "size_drift":
                rows[0]["pos"] = "999"
            elif failure == "missing":
                rows.pop()
            elif failure == "extra":
                rows.append({
                    "posId": "unowned-extra", "instId": "BTC-USDT-SWAP",
                    "posSide": "short", "pos": "1",
                })
            return rows

    client = Client(session_factory)
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert client.calls == []


@pytest.mark.parametrize(
    ("planned_size", "quantity_step", "min_quantity"),
    [("2.4", "1", 1), ("0.5", "0.1", 1)],
)
def test_final_close_preflight_rejects_invalid_contract_quantity_before_post(
    planned_size, quantity_step, min_quantity, tmp_path
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    with session_factory() as session:
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored_batch.target_snapshot_json)
        snapshot["contract_spec"]["quantity_step"] = float(quantity_step)
        snapshot["contract_spec"]["min_quantity"] = min_quantity
        stored_batch.target_snapshot_json = json.dumps(snapshot)
        first_leg = (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == batch.id)
            .order_by(StrategyManagementLeg.leg_index)
            .first()
        )
        first_leg.planned_close_size = planned_size
        first_leg.quantity_step = quantity_step
        session.commit()

    client = _FakeClient(session_factory)
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "close_final_preflight_failed"
    assert client.calls == []


def test_final_close_preflight_allows_strict_other_strategy_position(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    with session_factory() as session:
        other = ExecutionBinding(
            strategy_instance_id="deepcoin:200:20:BTC:short", kol_id="bob",
            chat_id=200, message_id=20, symbol="BTC", side="short",
            venue="deepcoin", pos_id="pos-other", status="active",
        )
        session.add(other)
        session.flush()
        session.add(ExecutionOrderLeg(
            execution_binding_id=other.id,
            strategy_instance_id=other.strategy_instance_id,
            leg_index=0, purpose="entry", order_kind="market",
            order_id="entry-other", pos_id="pos-other", venue="deepcoin",
            attribution_status="verified", status="active",
        ))
        session.commit()

    class Client(_FakeClient):
        def list_positions(self, *, inst_id=None):
            return super().list_positions(inst_id=inst_id) + [{
                "posId": "pos-other", "instId": "BTC-USDT-SWAP",
                "posSide": "short", "pos": "3",
            }]

    client = Client(session_factory)
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )
    assert result["status"] == "reconciling"
    assert len(client.calls) == 2


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


def test_explicit_stop_replaces_every_position_and_preserves_each_take_profit(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        lifecycle.stop_loss = 65500
        session.commit()
    client = _ProtectionClient(session_factory, rows_by_pos)

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "succeeded"
    assert [leg["status"] for leg in result["legs"]] == ["succeeded", "succeeded"]
    assert [call["ordId"] for call in client.cancel_calls] == [
        "tp-1a",
        "tp-1b",
        "sl-1",
        "tp-2",
        "sl-2",
    ]
    assert [
        (call["posId"], call.get("tpTriggerPx"), call.get("slTriggerPx"))
        for call in client.set_calls
    ] == [
        ("pos-1", "63000", None),
        ("pos-1", "62000", None),
        ("pos-1", None, "65000"),
        ("pos-2", "62500", None),
        ("pos-2", None, "65000"),
    ]
    assert client.set_calls[0]["tpTriggerPxType"] == "mark"
    assert client.set_calls[1]["tpOrdPx"] == "61990"
    assert client.set_calls[0]["sz"] == "1"
    assert "sz" not in client.set_calls[2]
    assert client.set_calls[3]["sz"] == "4"
    assert "sz" not in client.set_calls[4]
    assert all(call["instType"] == "SWAP" for call in client.cancel_calls)
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        assert lifecycle.stop_loss == 65000
        assert lifecycle.management_signal_message_id == 20
        assert lifecycle.management_action == "protection_update_confirmed"


def test_protection_batch_records_replacement_orders_in_ledger(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    client = _ProtectionClient(session_factory, rows_by_pos)

    execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    with session_factory() as session:
        rows = list_verified_ledger_rows_for_positions(session, ["pos-1", "pos-2"])

    assert [
        (row.pos_id, row.order_id, row.purpose, row.trigger_price, row.size_text)
        for row in rows
    ] == [
        ("pos-1", "new-1", "take_profit", "63000", "1"),
        ("pos-1", "new-2", "take_profit", "62000", "1"),
        ("pos-1", "new-3", "stop_loss", "65000", "0"),
        ("pos-2", "new-4", "take_profit", "62500", "4"),
        ("pos-2", "new-5", "stop_loss", "65000", "0"),
    ]
    assert {row.execution_binding_id for row in rows} == {batch.execution_binding_id}
    assert {row.evidence_source for row in rows} == {"management_tpsl_replacement"}


def test_protection_preflight_accepts_ledger_confirmed_unscoped_rows(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    with session_factory() as session:
        for leg in batch.legs:
            entry_leg = session.get(ExecutionOrderLeg, leg.execution_order_leg_id)
            rows = rows_by_pos[leg.pos_id]
            for row in rows:
                upsert_protection_ledger_row(
                    session,
                    venue="deepcoin",
                    execution_binding_id=batch.execution_binding_id,
                    execution_order_leg_id=entry_leg.id,
                    strategy_instance_id=batch.strategy_instance_id,
                    pos_id=leg.pos_id,
                    instrument_id="BTC-USDT-SWAP",
                    side="short",
                    order_id=row["ordId"],
                    purpose=(
                        "take_profit"
                        if row.get("tpTriggerPx") not in {None, "", "0"}
                        else "stop_loss"
                    ),
                    trigger_price=row.get("tpTriggerPx") or row.get("slTriggerPx"),
                    size_text=row.get("sz"),
                    status="verified",
                    evidence_source="entry_protection_response",
                    evidence={"match": "exact_written_order"},
                    seen_at=NOW,
                )
        session.commit()
    unscoped_rows_by_pos = {}
    for pos_id, rows in rows_by_pos.items():
        unscoped_rows_by_pos[pos_id] = [
            {
                key: value
                for key, value in row.items()
                if key not in {"posId", "cTime"}
            }
            for row in rows
        ]
    client = _ProtectionClient(session_factory, unscoped_rows_by_pos)

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "succeeded"
    assert [call["ordId"] for call in client.cancel_calls] == [
        "tp-1a",
        "tp-1b",
        "sl-1",
        "tp-2",
        "sl-2",
    ]


def test_break_even_uses_each_positions_own_average_entry(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(
        session_factory, action="move_stop_to_break_even", stop_loss=None
    )
    client = _ProtectionClient(session_factory, rows_by_pos)

    execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    stops = [call for call in client.set_calls if "slTriggerPx" in call]
    assert [(call["posId"], call["slTriggerPx"]) for call in stops] == [
        ("pos-1", "64000"),
        ("pos-2", "64500"),
    ]


def test_second_partial_promoted_to_full_exit_never_creates_new_stop(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    with session_factory() as session:
        stored = session.get(StrategyManagementBatch, batch.id)
        stored.intent = "partial_take_profit"
        stored.effective_action = "full_exit"
        stored.partial_round_before = 1
        for leg in (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == batch.id)
            .all()
        ):
            leg.planned_tpsl_json = json.dumps(
                {"intent": "move_stop_to_break_even"}
            )
        session.commit()

    class Client(_FakeClient):
        def __init__(self, factory):
            super().__init__(factory)
            self.protection_payloads = []

        def set_position_sltp(self, payload):
            self.protection_payloads.append(dict(payload))
            return {"code": "0", "data": {"ordId": "should-not-exist"}}

    client = Client(session_factory)
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    assert client.protection_payloads == []


def test_partial_then_break_even_waits_for_close_confirmation_before_protection(
    tmp_path
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch
    from telegram_kol_research.strategy_management_reconciliation import (
        reconcile_strategy_management_batches,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(
        session_factory,
        action="partial_then_break_even",
        stop_loss=None,
        keep_close_plan=True,
    )
    client = _ProtectionClient(session_factory, rows_by_pos)

    close_result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert close_result["status"] == "reconciling"
    assert len(client.close_calls) == 2
    assert client.cancel_calls == []
    assert client.set_calls == []

    stored = load_management_batch(session_factory, batch.id)
    order_rows = [
        {
            "ordId": leg.exchange_order_id,
            "clOrdId": leg.client_order_id,
            "instId": "BTC-USDT-SWAP",
        }
        for leg in stored.legs
    ]
    snapshot = SimpleNamespace(
        positions=[
            {
                "posId": "pos-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "1",
                "avgPx": "64000",
                "cTime": "1000",
            },
            {
                "posId": "pos-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "2",
                "avgPx": "64500",
                "cTime": "1001",
            },
        ],
        open_orders=[],
        order_history=order_rows,
        trade_fills=[],
        errors={},
    )
    reconciled = reconcile_strategy_management_batches(
        session_factory, snapshot=snapshot, reconciled_at=NOW
    )
    assert reconciled.pending == 1
    assert load_management_batch(session_factory, batch.id).status == "protection_ready"
    assert client.cancel_calls == []
    assert client.set_calls == []

    client.positions = list(snapshot.positions)
    protection_result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert protection_result["status"] == "succeeded"
    stops = [row for row in client.set_calls if "slTriggerPx" in row]
    assert [(row["posId"], row["slTriggerPx"]) for row in stops] == [
        ("pos-1", "64000"),
        ("pos-2", "64500"),
    ]


def test_partial_then_break_even_replaces_protection_after_exchange_resizes_old_tpsl(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch
    from telegram_kol_research.strategy_management_reconciliation import (
        reconcile_strategy_management_batches,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(
        session_factory,
        action="partial_then_break_even",
        stop_loss=None,
        keep_close_plan=True,
    )
    client = _ProtectionClient(session_factory, rows_by_pos)

    close_result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )
    assert close_result["status"] == "reconciling"

    stored = load_management_batch(session_factory, batch.id)
    order_rows = [
        {
            "ordId": leg.exchange_order_id,
            "clOrdId": leg.client_order_id,
            "instId": "BTC-USDT-SWAP",
        }
        for leg in stored.legs
    ]
    snapshot = SimpleNamespace(
        positions=[
            {
                "posId": "pos-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "1",
                "avgPx": "64000",
                "cTime": "1000",
            },
            {
                "posId": "pos-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "2",
                "avgPx": "64500",
                "cTime": "1001",
            },
        ],
        open_orders=[],
        order_history=order_rows,
        trade_fills=[],
        errors={},
    )
    reconcile_strategy_management_batches(
        session_factory, snapshot=snapshot, reconciled_at=NOW
    )
    assert load_management_batch(session_factory, batch.id).status == "protection_ready"

    client.positions = list(snapshot.positions)
    for row in client.pending:
        if row.get("posId") == "pos-2" and row.get("ordId") == "tp-2":
            row["sz"] = "2"

    protection_result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert protection_result["status"] == "succeeded"
    assert [row["ordId"] for row in client.cancel_calls] == [
        "tp-1a",
        "tp-1b",
        "sl-1",
        "tp-2",
        "sl-2",
    ]
    assert [
        (row["posId"], row.get("tpTriggerPx"), row.get("slTriggerPx"), row.get("sz"))
        for row in client.set_calls
        if row["posId"] == "pos-2"
    ] == [
        ("pos-2", "62500", None, "2"),
        ("pos-2", None, "64500", None),
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "errors",
        "missing",
        "size_drift",
        "extra",
        "wrong_instrument",
        "wrong_side",
        "missing_pos_id",
    ],
)
def test_all_planned_restart_snapshot_must_match_exact_frozen_positions(
    tmp_path, mutation
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        validate_management_restart_snapshot,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("1", "2"))
    positions = [
        {
            "posId": "pos-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "pos": "2",
        },
        {
            "posId": "pos-2",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "pos": "4",
        },
    ]
    errors = {}
    if mutation == "errors":
        errors["order_history:BTC-USDT-SWAP"] = "timeout"
    elif mutation == "missing":
        positions.pop()
    elif mutation == "size_drift":
        positions[0]["pos"] = "1"
    elif mutation == "extra":
        positions.append(
            {
                "posId": "pos-extra",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "1",
            }
        )
    elif mutation == "wrong_instrument":
        positions[0]["instId"] = "ETH-USDT-SWAP"
    elif mutation == "wrong_side":
        positions[0]["posSide"] = "long"
    elif mutation == "missing_pos_id":
        positions.append(
            {
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "1",
            }
        )

    with pytest.raises(ManagementBatchExecutionError, match="restart_snapshot"):
        validate_management_restart_snapshot(
            session_factory,
            batch_id=batch.id,
            snapshot=SimpleNamespace(positions=positions, errors=errors),
        )


def test_all_planned_restart_size_drift_freezes_batch_with_zero_exchange_write(
    tmp_path,
):
    from telegram_kol_research.strategy_management_worker import (
        run_strategy_management_worker_tick,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("1", "2"))
    assert transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    snapshot = SimpleNamespace(
        positions=[
            {
                "posId": "pos-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "1",
            },
            {
                "posId": "pos-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "4",
            },
        ],
        errors={},
    )
    client = _FakeClient(session_factory)

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        snapshot_loader=lambda *_args, **_kwargs: snapshot,
        processed_at=NOW,
    )

    stored = load_management_batch(session_factory, batch.id)
    assert result.recovered == 1
    assert stored.status == "recovery_required"
    assert stored.reason_code == "management_restart_snapshot_validation_failed"
    assert client.calls == []


@pytest.mark.parametrize("mutation", ["size_drift", "manual_close", "snapshot_error"])
def test_fresh_ready_claim_revalidates_exchange_before_first_write(tmp_path, mutation):
    from telegram_kol_research.strategy_management_worker import (
        run_strategy_management_worker_tick,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("1", "2"))
    positions = [
        {
            "posId": "pos-1",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "pos": "2",
        },
        {
            "posId": "pos-2",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "pos": "4",
        },
    ]
    errors = {}
    if mutation == "size_drift":
        positions[0]["pos"] = "1"
    elif mutation == "manual_close":
        positions.pop(0)
    else:
        errors["positions"] = "timeout"
    client = _FakeClient(session_factory)

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        snapshot_loader=lambda *_args, **_kwargs: SimpleNamespace(
            positions=positions, errors=errors
        ),
        processed_at=NOW,
    )

    stored = load_management_batch(session_factory, batch.id)
    assert result.recovered == 1
    assert stored.status == "recovery_required"
    assert stored.reason_code == "management_restart_snapshot_validation_failed"
    assert client.calls == []


@pytest.mark.parametrize(
    "owner_case",
    [
        "valid",
        "binding_other_venue",
        "entry_other_venue",
        "same_strategy",
        "null_strategy",
        "binding_open",
        "unverified_entry",
        "entry_strategy_mismatch",
        "pos_not_bound",
        "multiple_owners",
    ],
)
def test_restart_validator_only_ignores_strict_other_strategy_owner(
    tmp_path, owner_case
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        validate_management_restart_snapshot,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("1", "2"))
    with session_factory() as session:
        other_strategy = (
            batch.strategy_instance_id
            if owner_case == "same_strategy"
            else None
            if owner_case == "null_strategy"
            else "deepcoin:200:20:BTC:short"
        )
        other = ExecutionBinding(
            strategy_instance_id=other_strategy,
            kol_id="bob",
            chat_id=200,
            message_id=20,
            symbol="BTC",
            side="short",
            venue="gate" if owner_case == "binding_other_venue" else "deepcoin",
            pos_id="different-pos" if owner_case == "pos_not_bound" else "pos-other",
            status="open" if owner_case == "binding_open" else "active",
        )
        session.add(other)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=other.id,
                strategy_instance_id=(
                    "deepcoin:mismatch:BTC:short"
                    if owner_case == "entry_strategy_mismatch"
                    else other.strategy_instance_id
                ),
                leg_index=0,
                purpose="entry",
                order_kind="market",
                order_id="entry-other",
                pos_id="pos-other",
                venue="gate" if owner_case == "entry_other_venue" else "deepcoin",
                attribution_status=(
                    "unassigned" if owner_case == "unverified_entry" else "verified"
                ),
                status="active",
            )
        )
        if owner_case == "multiple_owners":
            duplicate = ExecutionBinding(
                strategy_instance_id="gate:300:30:BTC:short",
                kol_id="carol",
                chat_id=300,
                message_id=30,
                symbol="BTC",
                side="short",
                venue="gate",
                pos_id="pos-other",
                status="active",
            )
            session.add(duplicate)
            session.flush()
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=duplicate.id,
                    strategy_instance_id=duplicate.strategy_instance_id,
                    leg_index=0,
                    purpose="entry",
                    order_kind="market",
                    order_id="entry-other-gate",
                    pos_id="pos-other",
                    venue="gate",
                    attribution_status="verified",
                    status="active",
                )
            )
        session.commit()
    snapshot = SimpleNamespace(
        positions=[
            {
                "posId": "pos-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "2",
            },
            {
                "posId": "pos-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "4",
            },
            {
                "posId": "pos-other",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "3",
            },
        ],
        errors={},
    )

    if owner_case == "valid":
        validate_management_restart_snapshot(
            session_factory, batch_id=batch.id, snapshot=snapshot
        )
    else:
        with pytest.raises(ManagementBatchExecutionError, match="restart_snapshot"):
            validate_management_restart_snapshot(
                session_factory, batch_id=batch.id, snapshot=snapshot
            )

    from telegram_kol_research.strategy_management_worker import (
        run_strategy_management_worker_tick,
    )

    client = _FakeClient(session_factory)
    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        snapshot_loader=lambda *_args, **_kwargs: snapshot,
        processed_at=NOW,
    )

    if owner_case == "valid":
        assert result.executed == 1
        assert [payload["closePosId"] for payload, _status in client.calls] == [
            "pos-1",
            "pos-2",
        ]
        with session_factory() as session:
            other_binding = session.query(ExecutionBinding).filter_by(chat_id=200).one()
            other_leg = session.query(ExecutionOrderLeg).filter_by(
                execution_binding_id=other_binding.id
            ).one()
            assert other_binding.status == "active"
            assert other_leg.status == "active"
    else:
        assert result.recovered == 1
        assert load_management_batch(session_factory, batch.id).status == "recovery_required"
        assert client.calls == []


def test_ready_snapshot_loader_exception_freezes_with_zero_exchange_write(tmp_path):
    from telegram_kol_research.strategy_management_worker import (
        run_strategy_management_worker_tick,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("1", "2"))
    client = _FakeClient(session_factory)

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        snapshot_loader=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("snapshot unavailable")
        ),
        processed_at=NOW,
    )

    stored = load_management_batch(session_factory, batch.id)
    assert result.recovered == 1
    assert stored.status == "recovery_required"
    assert client.calls == []


@pytest.mark.parametrize("mutation", ["binding_closed", "entry_wrong_strategy"])
def test_ready_claim_identity_drift_is_terminal_blocked_with_zero_exchange_write(
    tmp_path, mutation
):
    from telegram_kol_research.strategy_management_worker import (
        run_strategy_management_worker_tick,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("1", "2"))
    with session_factory() as session:
        if mutation == "binding_closed":
            binding = session.get(ExecutionBinding, batch.execution_binding_id)
            binding.status = "closed"
        else:
            entry = (
                session.query(ExecutionOrderLeg)
                .filter_by(execution_binding_id=batch.execution_binding_id, purpose="entry")
                .order_by(ExecutionOrderLeg.id)
                .first()
            )
            entry.strategy_instance_id = "deepcoin:other:10:BTC:short"
        session.commit()
    client = _FakeClient(session_factory)

    result = run_strategy_management_worker_tick(
        session_factory,
        deepcoin_client_factory=lambda: client,
        processed_at=NOW,
    )

    stored = load_management_batch(session_factory, batch.id)
    assert result.failed == 1
    assert stored.status == "blocked"
    assert stored.reason_code == "management_pre_submit_validation_failed"
    assert client.calls == []


def test_executor_uses_durable_protection_leg_evidence_when_reason_is_missing(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(
        session_factory,
        action="partial_then_break_even",
        stop_loss=None,
        keep_close_plan=True,
    )
    with session_factory() as session:
        stored = session.get(StrategyManagementBatch, batch.id)
        stored.status = "executing"
        stored.reason_code = None
        legs = session.query(StrategyManagementLeg).filter_by(
            management_batch_id=batch.id
        )
        for leg in legs:
            leg.status = "succeeded"
        session.commit()
    client = _ProtectionClient(session_factory, rows_by_pos)

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "succeeded"
    assert client.close_calls == []
    assert client.cancel_calls == []
    assert client.set_calls == []


@pytest.mark.parametrize("drift", ["price", "missing", "ambiguous"])
def test_protection_preflight_drift_or_ambiguity_has_zero_cancels(drift, tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    client = _ProtectionClient(session_factory, rows_by_pos)
    if drift == "price":
        client.pending[0]["tpTriggerPx"] = "63100"
    elif drift == "missing":
        client.pending.pop()
    else:
        ambiguous = dict(client.pending[-1])
        ambiguous["ordId"] = "sl-extra"
        ambiguous["slTriggerPx"] = "65800"
        client.pending.append(ambiguous)

    with pytest.raises(ManagementBatchExecutionError, match="protection_preflight"):
        execute_management_batch(
            session_factory,
            batch_id=batch.id,
            deepcoin_client=client,
            executed_at=NOW,
        )

    assert client.cancel_calls == []
    assert client.set_calls == []


def test_replacement_failure_restores_complete_position_and_stops_later_legs(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    client = _ProtectionClient(
        session_factory,
        rows_by_pos,
        set_outcomes=[
            {"code": "0", "data": {"ordId": "new-1a"}},
            {"code": "0", "data": {"ordId": "new-1b"}},
            {"code": "0", "data": {"ordId": "new-sl1"}},
            DeepcoinDefiniteRejection("replacement rejected"),
            {"code": "0", "data": {"ordId": "restore-tp2"}},
            {"code": "0", "data": {"ordId": "restore-sl2"}},
        ],
    )

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "partial_failed"
    assert [leg["status"] for leg in result["legs"]] == ["succeeded", "restored"]
    assert [call["posId"] for call in client.set_calls] == [
        "pos-1",
        "pos-1",
        "pos-1",
        "pos-2",
        "pos-2",
        "pos-2",
    ]
    assert client.set_calls[-1]["slTriggerPx"] == "65700"


def test_restore_failure_marks_recovery_required_and_stops_later_legs(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    client = _ProtectionClient(
        session_factory,
        rows_by_pos,
        set_outcomes=[
            DeepcoinDefiniteRejection("replacement rejected"),
            DeepcoinDefiniteRejection("restore rejected"),
        ],
    )

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert [leg["status"] for leg in result["legs"]] == [
        "recovery_required",
        "planned",
    ]
    assert [call["ordId"] for call in client.cancel_calls] == [
        "tp-1a",
        "tp-1b",
        "sl-1",
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        DeepcoinRequestOutcomeUnknown("response lost"),
        {"code": "0", "data": {}},
    ],
    ids=["request_unknown", "success_missing_order_id"],
)
def test_unknown_protection_replacement_never_cancels_or_restores(outcome, tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    client = _ProtectionClient(
        session_factory,
        rows_by_pos,
        set_outcomes=[outcome],
    )

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert [leg["status"] for leg in result["legs"]] == [
        "recovery_required",
        "planned",
    ]
    # Only the known old IDs were cancelled before the replacement call. No
    # speculative cancel of a new ID and no restore write is permitted.
    assert [call["ordId"] for call in client.cancel_calls] == [
        "tp-1a",
        "tp-1b",
        "sl-1",
    ]
    assert len(client.set_calls) == 1


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("extra", "batch_entry_set_not_exact"),
        ("null_pos", "batch_entry_set_not_exact"),
        ("pending", "batch_entry_set_not_exact"),
        ("wrong_strategy", "batch_entry_set_not_exact"),
        ("terminal", "batch_entry_set_not_exact"),
        ("binding_extra", "binding_position_set_drift"),
        ("live_extra", "live_position_set_drift"),
    ],
)
def test_protection_entry_or_binding_set_drift_has_zero_cancels(
    mutation, error, tmp_path
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    with session_factory() as session:
        entries = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == batch.execution_binding_id)
            .filter(ExecutionOrderLeg.purpose == "entry")
            .order_by(ExecutionOrderLeg.id)
            .all()
        )
        if mutation == "extra":
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=batch.execution_binding_id,
                    strategy_instance_id=batch.strategy_instance_id,
                    leg_index=9,
                    purpose="entry",
                    order_kind="market",
                    order_id="entry-extra",
                    pos_id="pos-extra",
                    venue="deepcoin",
                    attribution_status="verified",
                    status="active",
                )
            )
        elif mutation == "null_pos":
            entries[1].pos_id = None
        elif mutation == "pending":
            entries[1].status = "pending"
        elif mutation == "wrong_strategy":
            entries[1].strategy_instance_id = "deepcoin:other"
        elif mutation == "terminal":
            entries[1].status = "closed"
            entries[1].terminal_reason = "manual"
        elif mutation == "binding_extra":
            binding = session.get(ExecutionBinding, batch.execution_binding_id)
            binding.pos_id = "pos-1,pos-2,pos-extra"
        session.commit()
    client = _ProtectionClient(session_factory, rows_by_pos)
    if mutation == "live_extra":
        client.positions.append(
            {
                "posId": "pos-extra",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "1",
                "avgPx": "64000",
            }
        )

    with pytest.raises(ManagementBatchExecutionError, match=error):
        execute_management_batch(
            session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
        )

    assert client.cancel_calls == []
    assert client.set_calls == []


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
    batch = _persist_close_batch(
        session_factory,
        intent="full_exit",
        effective_action="full_exit",
    )
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


@pytest.mark.parametrize("mode", ["disabled", "shadow"])
def test_automated_stop_adjustment_requires_live_batch_mode_with_zero_writes(
    mode, tmp_path
):
    from telegram_kol_research.deepcoin_execution_actions import (
        DeepcoinExecutionActionError,
        execute_deepcoin_management_signal,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    signal = _legacy_close_signal(
        session_factory,
        batch,
        action="adjust_stop_loss",
        payload={
            "management_batch_id": batch.id,
            "binding_id": batch.execution_binding_id,
        },
    )
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "management_execution_mode": mode},
    )
    client = _ProtectionClient(session_factory, rows_by_pos)

    with pytest.raises(
        DeepcoinExecutionActionError, match="management_live_execution_disabled"
    ):
        execute_deepcoin_management_signal(
            session_factory, trade_signal=signal, deepcoin_client=client
        )

    assert client.cancel_calls == []
    assert client.set_calls == []


def test_automated_stop_adjustment_without_batch_fails_closed(tmp_path):
    from telegram_kol_research.deepcoin_execution_actions import (
        DeepcoinExecutionActionError,
        execute_deepcoin_management_signal,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    signal = _legacy_close_signal(
        session_factory,
        batch,
        action="adjust_stop_loss",
        payload={"binding_id": batch.execution_binding_id},
    )
    client = _ProtectionClient(session_factory, rows_by_pos)

    with pytest.raises(
        DeepcoinExecutionActionError, match="legacy_management_signal_requires_batch"
    ):
        execute_deepcoin_management_signal(
            session_factory, trade_signal=signal, deepcoin_client=client
        )

    assert client.cancel_calls == []
    assert client.set_calls == []


def test_live_automated_stop_adjustment_delegates_to_exact_batch(tmp_path):
    from telegram_kol_research.deepcoin_execution_actions import (
        execute_deepcoin_management_signal,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    signal = _legacy_close_signal(
        session_factory,
        batch,
        action="adjust_stop_loss",
        payload={
            "management_batch_id": batch.id,
            "binding_id": batch.execution_binding_id,
        },
    )
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "management_execution_mode": "live"},
    )
    client = _ProtectionClient(session_factory, rows_by_pos)

    result = execute_deepcoin_management_signal(
        session_factory, trade_signal=signal, deepcoin_client=client
    )

    assert result["status"] == "succeeded"
    assert len(client.cancel_calls) == 5
    assert len(client.set_calls) == 5
