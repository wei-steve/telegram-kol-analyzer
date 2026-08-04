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
    PositionProtectionLedger,
    RawMessage,
    RecognitionDecision,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
    StrategyManagementMarketDecision,
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
from telegram_kol_research.strategy_management_reconciliation import (
    reconcile_strategy_management_batches,
)
from telegram_kol_research.strategy_management_executor import _planned_stop_price
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def test_break_even_stop_uses_exact_live_average_not_context_support() -> None:
    batch = SimpleNamespace(effective_action="move_stop_to_break_even")
    leg = SimpleNamespace(
        avg_entry_price="64478.5",
        planned_tpsl={
            "intent": "move_stop_to_break_even",
            "stop_loss_text": "63600",
            "stop_price_source": "historical_context",
        },
    )

    assert _planned_stop_price(batch=batch, leg=leg) == "64478.5"


def test_break_even_stop_accepts_only_explicit_current_message_price() -> None:
    batch = SimpleNamespace(effective_action="partial_then_break_even")
    leg = SimpleNamespace(
        avg_entry_price="64478.5",
        planned_tpsl={
            "intent": "partial_then_break_even",
            "stop_loss_text": "64500",
            "stop_price_source": "current_message_text",
        },
    )

    assert _planned_stop_price(batch=batch, leg=leg) == "64500"


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
            "identity": {
                "execution_binding_id": ids[3],
                "deferred_entry_leg_ids": [],
            },
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
            for row in rows:
                purpose = (
                    "take_profit"
                    if row.get("tpTriggerPx")
                    else "stop_loss"
                )
                upsert_protection_ledger_row(
                    session,
                    venue="deepcoin",
                    execution_binding_id=stored_batch.execution_binding_id,
                    execution_order_leg_id=leg.execution_order_leg_id,
                    strategy_instance_id=stored_batch.strategy_instance_id,
                    pos_id=pos_id,
                    instrument_id="BTC-USDT-SWAP",
                    side="short",
                    order_id=row["ordId"],
                    purpose=purpose,
                    trigger_price=(
                        row.get("tpTriggerPx") or row.get("slTriggerPx")
                    ),
                    size_text=row.get("sz"),
                    status="verified",
                    evidence_source="test_exact_owner",
                    evidence={"source": "management_fixture"},
                    seen_at=NOW,
                )
        session.commit()
    return load_management_batch(session_factory, batch.id), rows_by_pos


def _persist_market_break_even_batch(session_factory):
    batch, rows_by_pos = _persist_protection_batch(
        session_factory,
        action="move_stop_to_break_even",
        stop_loss=None,
    )
    with session_factory() as session:
        stored = session.get(StrategyManagementBatch, batch.id)
        stored.effective_action = "break_even_by_market"
        for leg in (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == batch.id)
            .all()
        ):
            leg.old_tpsl_json = None
            leg.planned_tpsl_json = None
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
        self.trigger_pending = []
        self.open_orders = []
        self.cancel_trigger_calls = []
        self.cancel_order_calls = []
        self.cancel_trigger_outcomes = []
        self.call_log = []

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
                    "avgPx": "64000",
                    "mgnMode": binding.margin_mode,
                    "mrgPosition": binding.position_mode,
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
        request = dict(payload)
        self.calls.append((request, status))
        self.call_log.append(("place_order", request))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.trigger_pending)

    def list_open_orders(self, *, inst_id=None):
        return list(self.open_orders)

    def cancel_trigger_order(self, payload):
        request = dict(payload)
        self.cancel_trigger_calls.append(request)
        self.call_log.append(("cancel_trigger_order", request))
        if self.cancel_trigger_outcomes:
            outcome = self.cancel_trigger_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return {"code": "0", "data": {"ordId": payload.get("ordId")}}

    def cancel_order(self, payload):
        request = dict(payload)
        self.cancel_order_calls.append(request)
        self.call_log.append(("cancel_order", request))
        return {"code": "0", "data": {"ordId": payload.get("ordId")}}


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
                "mgnMode": "cross",
                "mrgPosition": "split",
                "cTime": "1000",
            },
            {
                "posId": "pos-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "4",
                "avgPx": "64500",
                "mgnMode": "cross",
                "mrgPosition": "split",
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
        self.call_log = []
        self.quote = {
            "instrument_id": "BTC-USDT-SWAP",
            "price": "64200",
            "price_field": "last",
        }
        self.quote_reads = []
        self.trigger_pending = []
        self.open_orders = []
        self.cancel_trigger_calls = []
        self.cancel_order_calls = []

    def list_positions(self, *, inst_id=None):
        return [dict(row) for row in self.positions]

    def list_trigger_orders_pending(self, *, inst_id):
        self.pending_reads += 1
        return [dict(row) for row in self.pending]

    def get_ticker_quote(self, *, inst_id):
        self.quote_reads.append(inst_id)
        return None if self.quote is None else dict(self.quote)

    def list_open_orders(self, *, inst_id=None):
        return [dict(row) for row in self.open_orders]

    def cancel_trigger_order(self, payload):
        self.cancel_trigger_calls.append(dict(payload))
        self.call_log.append(("cancel_trigger_order", str(payload.get("ordId"))))
        return {"code": "0", "data": {"ordId": payload.get("ordId")}}

    def cancel_order(self, payload):
        self.cancel_order_calls.append(dict(payload))
        self.call_log.append(("cancel_order", str(payload.get("ordId"))))
        return {"code": "0", "data": {"ordId": payload.get("ordId")}}

    def place_order(self, payload):
        self.close_calls.append(dict(payload))
        self.call_log.append(("place_order", str(payload.get("posId"))))
        return {
            "code": "0",
            "data": {"ordId": f"close-{len(self.close_calls)}"},
        }

    def cancel_position_sltp(self, payload):
        self.cancel_calls.append(dict(payload))
        self.call_log.append(("cancel_position_sltp", str(payload.get("ordId"))))
        if self.cancel_outcomes:
            outcome = self.cancel_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            response = outcome
        else:
            response = {"code": "0", "data": {"ordId": payload["ordId"]}}
        self.pending = [
            row
            for row in self.pending
            if str(row.get("ordId")) != str(payload.get("ordId"))
        ]
        return response

    def set_position_sltp(self, payload):
        self.set_calls.append(dict(payload))
        self.call_log.append(("set_position_sltp", str(payload.get("posId"))))
        if self.set_outcomes:
            outcome = self.set_outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            response = outcome
        else:
            response = {"code": "0", "data": {"ordId": f"new-{len(self.set_calls)}"}}
        order_id = _response_order_id_for_test(response)
        if order_id:
            self.pending.append(
                {
                    "ordId": order_id,
                    "instId": payload["instId"],
                    "posId": payload["posId"],
                    "posSide": payload["posSide"],
                    **(
                        {"slTriggerPx": payload["slTriggerPx"]}
                        if payload.get("slTriggerPx") not in (None, "")
                        else {"tpTriggerPx": payload["tpTriggerPx"]}
                    ),
                    "sz": payload.get("sz", "0"),
                }
            )
        return response


def _response_order_id_for_test(response):
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        return data.get("ordId")
    return None


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


def test_break_even_market_reservation_persists_mixed_per_position_actions(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)

    decision = reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )

    assert [(row["pos_id"], row["action"]) for row in decision.decisions] == [
        ("pos-1", "full_exit"),
        ("pos-2", "set_break_even"),
    ]
    assert decision.quote_price == "64200"
    assert decision.quote_price_field == "last"
    assert client.quote_reads == ["BTC-USDT-SWAP"]
    assert client.pending_reads == 1
    assert client.call_log == []


@pytest.mark.parametrize("quote", [None, {"instrument_id": "ETH-USDT-SWAP", "price": "64200", "price_field": "last"}])
def test_break_even_market_reservation_blocks_unsafe_quote_without_writes(
    tmp_path, quote
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.quote = quote

    with pytest.raises(
        ManagementBatchExecutionError,
        match="break_even_market_quote_unavailable",
    ):
        reserve_break_even_market_actions(
            session_factory,
            batch=batch,
            deepcoin_client=client,
            observed_at=NOW,
        )

    assert client.call_log == []
    with session_factory() as session:
        assert session.query(StrategyManagementMarketDecision).count() == 0


def test_break_even_market_reservation_requires_protection_only_for_allowed_legs(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.pending = [
        row for row in client.pending if str(row.get("posId")) != "pos-2"
    ]

    with pytest.raises(
        ManagementBatchExecutionError,
        match="protection_preflight_rows_ambiguous_or_drifted",
    ):
        reserve_break_even_market_actions(
            session_factory,
            batch=batch,
            deepcoin_client=client,
            observed_at=NOW,
        )

    assert client.call_log == []
    with session_factory() as session:
        assert session.query(StrategyManagementMarketDecision).count() == 0


def test_break_even_market_reservation_all_full_exit_needs_no_protection_rows(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.quote["price"] = "65000"
    client.pending = []

    decision = reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )

    assert {row["action"] for row in decision.decisions} == {"full_exit"}
    assert client.pending_reads == 0
    assert client.call_log == []


def test_break_even_market_reservation_rejects_position_drift_without_writes(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.positions[0]["pos"] = "3"

    with pytest.raises(
        ManagementBatchExecutionError,
        match="protection_preflight_position_economics_drift",
    ):
        reserve_break_even_market_actions(
            session_factory,
            batch=batch,
            deepcoin_client=client,
            observed_at=NOW,
        )

    assert client.call_log == []
    with session_factory() as session:
        assert session.query(StrategyManagementMarketDecision).count() == 0


def test_break_even_market_reservation_retry_reuses_decision_without_ticker(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    first = reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )
    client.get_ticker_quote = lambda *, inst_id: (_ for _ in ()).throw(
        AssertionError("reserved decision must not reread ticker")
    )

    second = reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )

    assert second == first
    assert client.call_log == []


def test_break_even_market_reservation_retry_rejects_live_economics_drift(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )
    client.positions[0]["pos"] = "3"
    client.get_ticker_quote = lambda *, inst_id: (_ for _ in ()).throw(
        AssertionError("reserved decision must not reread ticker")
    )

    with pytest.raises(
        ManagementBatchExecutionError,
        match="protection_preflight_position_economics_drift",
    ):
        reserve_break_even_market_actions(
            session_factory,
            batch=batch,
            deepcoin_client=client,
            observed_at=NOW,
        )

    assert client.call_log == []


def test_break_even_market_reservation_retry_rejects_protection_drift(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )
    for row in client.pending:
        if row.get("ordId") == "sl-2":
            row["slTriggerPx"] = "66000"

    with pytest.raises(
        ManagementBatchExecutionError,
        match="protection_preflight_rows_ambiguous_or_drifted",
    ):
        reserve_break_even_market_actions(
            session_factory,
            batch=batch,
            deepcoin_client=client,
            observed_at=NOW,
        )

    assert client.call_log == []


def test_break_even_by_market_submitted_restart_hands_off_to_reconciliation(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )
    close_leg = next(leg for leg in batch.legs if leg.pos_id == "pos-1")
    transition_leg(
        session_factory,
        close_leg.id,
        expected_statuses={"planned"},
        new_status="submitted",
        transitioned_at=NOW,
        client_order_id="TMRESTART",
        exchange_order_id="close-restart",
    )
    client.positions = [client.positions[1]]
    client.call_log.clear()

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "reconciling"
    assert result["reason"] == "break_even_market_restart_requires_reconciliation"
    assert client.call_log == []


def test_break_even_by_market_corrupt_decision_freezes_executor(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
        reserve_break_even_market_actions,
    )
    from telegram_kol_research.strategy_management_market_decisions import (
        BreakEvenMarketDecisionConflict,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )
    with session_factory() as session:
        row = session.query(StrategyManagementMarketDecision).one()
        row.decision_fingerprint = "d" * 64
        session.commit()

    with pytest.raises(BreakEvenMarketDecisionConflict):
        execute_management_batch(
            session_factory,
            batch_id=batch.id,
            deepcoin_client=client,
            executed_at=NOW,
        )

    stored = load_management_batch(session_factory, batch.id)
    assert stored.status == "recovery_required"
    assert stored.reason_code == "break_even_market_decision_missing_or_invalid"
    assert client.call_log == []


def test_break_even_all_protection_partial_post_write_restart_freezes(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.quote["price"] = "63000"
    reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )
    transition_leg(
        session_factory,
        batch.legs[0].id,
        expected_statuses={"planned"},
        new_status="succeeded",
        transitioned_at=NOW,
    )
    client.call_log.clear()

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "break_even_market_post_write_recovery_required"
    assert [leg["status"] for leg in result["legs"]] == [
        "succeeded",
        "planned",
    ]
    assert client.call_log == []


def test_break_even_all_protection_completed_restart_finalizes_lifecycle(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.quote["price"] = "63000"
    reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )
    for leg in batch.legs:
        transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"planned"},
            new_status="succeeded",
            transitioned_at=NOW,
        )
    client.call_log.clear()

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "succeeded"
    assert result["reason"] == "all_position_protection_replaced"
    assert client.call_log == []
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, batch.target_lifecycle_id)
        assert lifecycle.management_action == "protection_update_confirmed"


def test_break_even_reserved_protection_restart_never_writes(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.quote["price"] = "63000"
    reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )
    transition_leg(
        session_factory,
        batch.legs[0].id,
        expected_statuses={"planned"},
        new_status="reserved",
        transitioned_at=NOW,
    )
    client.call_log.clear()

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "recovery_required"
    assert [leg["status"] for leg in result["legs"]] == [
        "recovery_required",
        "planned",
    ]
    assert client.call_log == []


def test_break_even_post_write_unknown_has_priority_over_restored(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.quote["price"] = "63000"
    reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )
    for leg, status in zip(
        batch.legs, ("restored", "recovery_required"), strict=True
    ):
        transition_leg(
            session_factory,
            leg.id,
            expected_statuses={"planned"},
            new_status=status,
            transitioned_at=NOW,
        )
    client.call_log.clear()

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "break_even_market_post_write_recovery_required"
    assert client.call_log == []


def test_break_even_by_market_executes_mixed_close_and_protection_legs(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    client = _ProtectionClient(session_factory, rows_by_pos)

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "reconciling"
    assert [
        (call["closePosId"], call["sz"], call["ordType"])
        for call in client.close_calls
    ] == [("pos-1", "2", "market")]
    assert [call["ordId"] for call in client.cancel_calls] == [
        "tp-2",
        "sl-2",
    ]
    assert {call["posId"] for call in client.set_calls} == {"pos-2"}
    stop_payloads = [
        call for call in client.set_calls if call.get("slTriggerPx") not in (None, "")
    ]
    assert len(stop_payloads) == 1
    assert stop_payloads[0]["slTriggerPx"] == "64500"
    stored = load_management_batch(session_factory, batch.id)
    assert {leg.pos_id: leg.status for leg in stored.legs} == {
        "pos-1": "submitted",
        "pos-2": "succeeded",
    }


def test_break_even_by_market_all_allowed_replaces_each_positions_protection(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.quote["price"] = "63000"

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "succeeded"
    assert client.close_calls == []
    assert len(client.cancel_calls) == 5
    assert len(client.set_calls) == 5
    stop_by_pos = {
        call["posId"]: call["slTriggerPx"]
        for call in client.set_calls
        if call.get("slTriggerPx") not in (None, "")
    }
    assert stop_by_pos == {"pos-1": "64000", "pos-2": "64500"}
    assert {leg.status for leg in load_management_batch(session_factory, batch.id).legs} == {
        "succeeded"
    }


def test_break_even_by_market_all_invalid_market_closes_exact_positions(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.quote["price"] = "65000"
    client.pending = []

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "reconciling"
    assert [
        (call["closePosId"], call["sz"]) for call in client.close_calls
    ] == [("pos-1", "2"), ("pos-2", "4")]
    assert client.cancel_calls == []
    assert client.set_calls == []


def test_break_even_by_market_cancels_deferred_entry_before_market_close(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    deferred_ids, _ = _configure_deferred_full_exit(session_factory, batch)
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.positions = [client.positions[0]]
    client.pending = [
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "deferred-order-1",
            "clOrdId": "deferred-client-1",
            "posSide": "short",
        }
    ]

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "reconciling"
    assert client.cancel_trigger_calls == [
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "deferred-order-1",
            "clOrdId": "deferred-client-1",
        }
    ]
    assert [call["closePosId"] for call in client.close_calls] == ["pos-1"]
    cancel_index = next(
        index
        for index, (operation, _value) in enumerate(client.call_log)
        if operation == "cancel_trigger_order"
    )
    close_index = next(
        index
        for index, (operation, _value) in enumerate(client.call_log)
        if operation == "place_order"
    )
    assert cancel_index < close_index
    with session_factory() as session:
        deferred = session.get(ExecutionOrderLeg, deferred_ids[0])
        assert deferred.status == "cancelled"


def test_break_even_by_market_unknown_close_is_never_resubmitted(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.quote["price"] = "65000"
    client.pending = []
    calls = []

    def place_order(payload):
        calls.append(dict(payload))
        if len(calls) == 1:
            raise DeepcoinRequestOutcomeUnknown("timeout")
        return {"code": "0", "data": {"ordId": "close-2"}}

    client.place_order = place_order

    first = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )
    second = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert first["status"] == "reconciling"
    assert second["reason"] == "batch_already_reconciling"
    assert len(calls) == 2
    assert {
        leg.pos_id: leg.status
        for leg in load_management_batch(session_factory, batch.id).legs
    } == {"pos-1": "submit_unknown", "pos-2": "submitted"}


def test_break_even_by_market_reserved_close_restart_becomes_submit_unknown(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
        reserve_break_even_market_actions,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    transition_batch(
        session_factory,
        batch.id,
        expected_statuses={"ready"},
        new_status="executing",
        transitioned_at=NOW,
    )
    batch = load_management_batch(session_factory, batch.id)
    client = _ProtectionClient(session_factory, rows_by_pos)
    reserve_break_even_market_actions(
        session_factory,
        batch=batch,
        deepcoin_client=client,
        observed_at=NOW,
    )
    close_leg = next(leg for leg in batch.legs if leg.pos_id == "pos-1")
    transition_leg(
        session_factory,
        close_leg.id,
        expected_statuses={"planned"},
        new_status="reserved",
        transitioned_at=NOW,
        client_order_id="TMRESERVED",
        request={"closePosId": "pos-1", "sz": "2"},
    )
    client.close_calls.clear()
    client.call_log.clear()

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "reconciling"
    assert client.close_calls == []
    assert {
        leg.pos_id: leg.status
        for leg in load_management_batch(session_factory, batch.id).legs
    } == {"pos-1": "submit_unknown", "pos-2": "planned"}
    assert client.call_log == []


def test_break_even_by_market_rejected_protection_restores_only_that_leg(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    client = _ProtectionClient(
        session_factory,
        rows_by_pos,
        set_outcomes=[
            {"code": "0", "data": {"ordId": "new-tp"}},
            DeepcoinDefiniteRejection("invalid stop"),
            {"code": "0", "data": {"ordId": "restore-tp"}},
            {"code": "0", "data": {"ordId": "restore-sl"}},
        ],
    )

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "partial_failed"
    assert {
        leg.pos_id: leg.status
        for leg in load_management_batch(session_factory, batch.id).legs
    } == {"pos-1": "submitted", "pos-2": "restored"}
    assert [call["ordId"] for call in client.cancel_calls] == [
        "tp-2",
        "sl-2",
        "new-tp",
    ]
    with session_factory() as session:
        rows = (
            session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.pos_id == "pos-2")
            .filter(PositionProtectionLedger.status == "verified")
            .order_by(PositionProtectionLedger.order_id)
            .all()
        )
        assert [row.order_id for row in rows] == ["restore-sl", "restore-tp"]
        assert {row.evidence_source for row in rows} == {
            "management_tpsl_restore"
        }


def test_break_even_by_market_unknown_protection_write_never_compensates(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    client = _ProtectionClient(
        session_factory,
        rows_by_pos,
        set_outcomes=[DeepcoinRequestOutcomeUnknown("response lost")],
    )

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "reconciling"
    assert {
        leg.pos_id: leg.status
        for leg in load_management_batch(session_factory, batch.id).legs
    } == {"pos-1": "submitted", "pos-2": "recovery_required"}
    assert [call["ordId"] for call in client.cancel_calls] == ["tp-2", "sl-2"]
    assert len(client.set_calls) == 1


def test_break_even_by_market_protection_recovery_keeps_close_reconcilable(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_market_break_even_batch(session_factory)
    client = _ProtectionClient(
        session_factory,
        rows_by_pos,
        set_outcomes=[DeepcoinRequestOutcomeUnknown("response lost")],
    )
    execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )
    submitted = load_management_batch(session_factory, batch.id)
    close_leg = next(leg for leg in submitted.legs if leg.pos_id == "pos-1")

    result = reconcile_strategy_management_batches(
        session_factory,
        snapshot=SimpleNamespace(
            positions=[client.positions[1]],
            open_orders=[],
            order_history=[
                {
                    "ordId": close_leg.exchange_order_id,
                    "clOrdId": close_leg.client_order_id,
                }
            ],
            trade_fills=[],
            errors={},
        ),
        reconciled_at=NOW,
        batch_ids={batch.id},
    )

    assert result.frozen == 1
    stored = load_management_batch(session_factory, batch.id)
    assert stored.status == "recovery_required"
    assert stored.reason_code == "break_even_market_protection_not_confirmed"
    assert {leg.pos_id: leg.status for leg in stored.legs} == {
        "pos-1": "confirmed",
        "pos-2": "recovery_required",
    }
    with session_factory() as session:
        entries = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id
                == batch.execution_binding_id
            )
            .order_by(ExecutionOrderLeg.leg_index)
            .all()
        )
        assert [entry.status for entry in entries] == ["closed", "active"]


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


def test_closed_remediation_gate_never_rewrites_reconciling_batch(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    with session_factory() as session:
        stored = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored.target_snapshot_json)
        snapshot["remediation_confirmation"] = {
            "action_id": "test-action",
            "action_fingerprint": "f" * 64,
            "exchange_snapshot_fingerprint": "e" * 64,
            "instrument_scope": ["BTC-USDT-SWAP"],
        }
        stored.target_snapshot_json = json.dumps(snapshot)
        stored.execution_mode = "live"
        stored.status = "reconciling"
        session.commit()

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=_FakeClient(session_factory),
        executed_at=NOW,
    )

    assert result["status"] == "reconciling"
    assert result["reason"] == "batch_already_reconciling"
    assert load_management_batch(session_factory, batch.id).status == "reconciling"


def test_protection_recovery_full_exit_requires_exact_immutable_bypass_marker(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("1", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    with session_factory() as session:
        stored = session.get(StrategyManagementBatch, batch.id)
        stored.reason_code = "protection_recovery_bypassed_for_full_exit"
        snapshot = json.loads(stored.target_snapshot_json)
        snapshot["protection_recovery_bypass"] = {
            "version": 1,
            "reason": "protection_recovery_required",
            "allowed_action": "full_exit",
            "target_lifecycle_id": stored.target_lifecycle_id,
            "execution_binding_id": stored.execution_binding_id,
            "target_pos_ids": ["pos-invalid"],
        }
        stored.target_snapshot_json = json.dumps(snapshot)
        session.commit()

    client = _FakeClient(session_factory)
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert client.calls == []
    assert result["status"] == "recovery_required"
    assert result["reason"] == "close_final_preflight_failed"


def test_protection_recovery_full_exit_closes_only_marked_positions(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("1", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    with session_factory() as session:
        stored = session.get(StrategyManagementBatch, batch.id)
        stored.reason_code = "protection_recovery_bypassed_for_full_exit"
        snapshot = json.loads(stored.target_snapshot_json)
        snapshot["protection_recovery_bypass"] = {
            "version": 1,
            "reason": "protection_recovery_required",
            "allowed_action": "full_exit",
            "target_lifecycle_id": stored.target_lifecycle_id,
            "execution_binding_id": stored.execution_binding_id,
            "target_pos_ids": ["pos-1", "pos-2"],
        }
        stored.target_snapshot_json = json.dumps(snapshot)
        session.commit()

    client = _FakeClient(session_factory)
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    assert [payload["closePosId"] for payload, _ in client.calls] == [
        "pos-1",
        "pos-2",
    ]


def test_invalid_protection_recovery_bypass_never_cancels_deferred_entry(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("1", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    _configure_deferred_full_exit(session_factory, batch)
    with session_factory() as session:
        stored = session.get(StrategyManagementBatch, batch.id)
        stored.reason_code = "protection_recovery_bypassed_for_full_exit"
        snapshot = json.loads(stored.target_snapshot_json)
        snapshot["protection_recovery_bypass"] = {
            "version": 1,
            "reason": "protection_recovery_required",
            "allowed_action": "full_exit",
            "target_lifecycle_id": stored.target_lifecycle_id,
            "execution_binding_id": stored.execution_binding_id,
            "target_pos_ids": ["pos-invalid"],
        }
        stored.target_snapshot_json = json.dumps(snapshot)
        session.commit()

    client = _FakeClient(session_factory)
    client.trigger_pending = [{"ordId": "deferred-order-1", "clOrdId": "deferred-client-1"}]
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert client.cancel_trigger_calls == []
    assert client.cancel_order_calls == []
    assert client.calls == []


def test_close_batch_accepts_verified_entry_subset_with_pending_range_leg(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory, sizes=("3", "2"), intent="full_exit", effective_action="full_exit"
    )
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
        pending_leg_id = second_entry.id

    with session_factory() as session:
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored_batch.target_snapshot_json)
        snapshot["identity"]["deferred_entry_leg_ids"] = [pending_leg_id]
        stored_batch.target_snapshot_json = json.dumps(snapshot)
        session.commit()

    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])
    client.trigger_pending = [
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "entry-1",
            "posSide": "short",
        }
    ]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    assert [payload["closePosId"] for payload, _status in client.calls] == ["pos-1"]
    assert client.cancel_trigger_calls == [
        {"instId": "BTC-USDT-SWAP", "ordId": "entry-1"}
    ]
    first_close_submission = next(
        index
        for index, (operation, _payload) in enumerate(client.call_log)
        if operation == "place_order"
    )
    assert all(
        index < first_close_submission
        for index, (operation, _payload) in enumerate(client.call_log)
        if operation in {"cancel_trigger_order", "cancel_order"}
    )
    stored = load_management_batch(session_factory, batch.id)
    assert [leg.pos_id for leg in stored.legs] == ["pos-1"]
    assert stored.legs[0].status == "submitted"
    with session_factory() as session:
        pending_leg = session.get(ExecutionOrderLeg, pending_leg_id)
        assert pending_leg.status == "cancelled"
        assert pending_leg.terminal_reason == "management_full_close_cancelled_unfilled_entry_leg"


def test_partial_take_profit_cancels_deferred_range_entry_before_closing_filled_leg(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("1", "2"))
    deferred_ids, _ = _configure_deferred_full_exit(session_factory, batch)
    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])
    client.trigger_pending = [
        {"ordId": "deferred-order-1", "clOrdId": "deferred-client-1"}
    ]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    assert client.cancel_trigger_calls == [
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "deferred-order-1",
            "clOrdId": "deferred-client-1",
        }
    ]
    assert [payload["closePosId"] for payload, _status in client.calls] == ["pos-1"]
    assert client.call_log[0][0] == "cancel_trigger_order"
    with session_factory() as session:
        deferred = session.get(ExecutionOrderLeg, deferred_ids[0])
        assert deferred.status == "cancelled"


def test_partial_take_profit_allows_already_cancelled_unfilled_entry_leg(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        _require_exact_entry_legs,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory, sizes=("1", "2"))
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        binding.pos_id = "pos-1"
        cancelled_entry = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=batch.execution_binding_id,
            pos_id="pos-2",
        ).one()
        cancelled_entry.pos_id = None
        cancelled_entry.status = "cancelled"
        cancelled_entry.attribution_status = "unassigned"
        cancelled_entry.terminal_reason = "operator_cancelled_unfilled_entry_leg"
        session.query(StrategyManagementLeg).filter_by(
            management_batch_id=batch.id,
            pos_id="pos-2",
        ).delete()
        session.commit()

    _require_exact_entry_legs(session_factory, load_management_batch(session_factory, batch.id))


def test_partial_then_break_even_protection_allows_snapshotted_management_cancelled_entry(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        _require_exact_entry_legs,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("1", "2"),
        intent="partial_then_break_even",
        effective_action="partial_then_break_even",
    )
    deferred_ids, _ = _configure_deferred_full_exit(session_factory, batch)
    with session_factory() as session:
        deferred = session.get(ExecutionOrderLeg, deferred_ids[0])
        deferred.status = "cancelled"
        deferred.terminal_reason = (
            "management_full_close_cancelled_unfilled_entry_leg"
        )
        session.commit()

    _require_exact_entry_legs(
        session_factory,
        load_management_batch(session_factory, batch.id),
    )


def test_full_close_does_not_submit_when_deferred_entry_leg_is_not_live(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory, sizes=("3", "2"), intent="full_exit", effective_action="full_exit"
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        binding.pos_id = "pos-1"
        second_batch_leg = session.query(StrategyManagementLeg).filter_by(
            management_batch_id=batch.id, pos_id="pos-2"
        ).one()
        session.delete(second_batch_leg)
        deferred = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=batch.execution_binding_id, pos_id="pos-2"
        ).one()
        deferred.order_kind = "trigger_limit"
        deferred.pos_id = None
        deferred.status = "pending"
        deferred.attribution_status = "unassigned"
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored_batch.target_snapshot_json)
        snapshot["identity"]["deferred_entry_leg_ids"] = [deferred.id]
        stored_batch.target_snapshot_json = json.dumps(snapshot)
        session.commit()

    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "deferred_entry_cancel_preflight_failed"
    assert client.calls == []


def test_full_close_cancels_only_the_stored_deferred_entry_regular_order(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory, sizes=("3", "2"), intent="full_exit", effective_action="full_exit"
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        binding.pos_id = "pos-1"
        second_batch_leg = session.query(StrategyManagementLeg).filter_by(
            management_batch_id=batch.id, pos_id="pos-2"
        ).one()
        session.delete(second_batch_leg)
        deferred = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=batch.execution_binding_id, pos_id="pos-2"
        ).one()
        deferred.order_kind = "limit"
        deferred.order_id = None
        deferred.client_order_id = "entry-client-1"
        deferred.pos_id = None
        deferred.status = "pending"
        deferred.attribution_status = "unassigned"
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored_batch.target_snapshot_json)
        snapshot["identity"]["deferred_entry_leg_ids"] = [deferred.id]
        stored_batch.target_snapshot_json = json.dumps(snapshot)
        session.commit()

    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])
    client.open_orders = [
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "entry-regular-1",
            "clOrdId": "entry-client-1",
            "posSide": "short",
        },
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "unrelated-1",
            "clOrdId": "entry-client-10",
            "posSide": "short",
        },
    ]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    assert client.cancel_order_calls == [
        {
            "instId": "BTC-USDT-SWAP",
            "clOrdId": "entry-client-1",
            "mrgPosition": "split",
        }
    ]
    assert all(call.get("ordId") != "unrelated-1" for call in client.cancel_order_calls)
    assert [payload["closePosId"] for payload, _ in client.calls] == ["pos-1"]
    first_close_submission = next(
        index
        for index, (operation, _payload) in enumerate(client.call_log)
        if operation == "place_order"
    )
    assert all(
        index < first_close_submission
        for index, (operation, _payload) in enumerate(client.call_log)
        if operation in {"cancel_trigger_order", "cancel_order"}
    )


def test_full_close_cancels_every_snapshot_deferred_entry_leg_before_close_submission(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory, sizes=("3", "2"), intent="full_exit", effective_action="full_exit"
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        binding.pos_id = "pos-1"
        second_batch_leg = session.query(StrategyManagementLeg).filter_by(
            management_batch_id=batch.id, pos_id="pos-2"
        ).one()
        session.delete(second_batch_leg)
        deferred_trigger = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=batch.execution_binding_id, pos_id="pos-2"
        ).one()
        deferred_trigger.order_kind = "trigger_limit"
        deferred_trigger.order_id = "entry-trigger-1"
        deferred_trigger.pos_id = None
        deferred_trigger.status = "pending"
        deferred_trigger.attribution_status = "unassigned"
        deferred_regular = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=2,
            purpose="entry",
            order_kind="limit",
            client_order_id="entry-client-2",
            venue="deepcoin",
            attribution_status="unassigned",
            status="pending",
        )
        session.add(deferred_regular)
        session.flush()
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored_batch.target_snapshot_json)
        snapshot["identity"]["deferred_entry_leg_ids"] = [
            deferred_trigger.id,
            deferred_regular.id,
        ]
        stored_batch.target_snapshot_json = json.dumps(snapshot)
        session.commit()
        deferred_leg_ids = {deferred_trigger.id, deferred_regular.id}

    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])
    client.trigger_pending = [
        {"instId": "BTC-USDT-SWAP", "ordId": "entry-trigger-1", "posSide": "short"},
        {"instId": "BTC-USDT-SWAP", "ordId": "entry-trigger-10", "posSide": "short"},
    ]
    client.open_orders = [
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "entry-regular-2",
            "clOrdId": "entry-client-2",
            "posSide": "short",
        },
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "unrelated-2",
            "clOrdId": "entry-client-20",
            "posSide": "short",
        },
    ]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    assert client.cancel_trigger_calls == [
        {"instId": "BTC-USDT-SWAP", "ordId": "entry-trigger-1"}
    ]
    assert client.cancel_order_calls == [
        {
            "instId": "BTC-USDT-SWAP",
            "clOrdId": "entry-client-2",
            "mrgPosition": "split",
        }
    ]
    assert [payload["closePosId"] for payload, _ in client.calls] == ["pos-1"]
    first_close_submission = next(
        index
        for index, (operation, _payload) in enumerate(client.call_log)
        if operation == "place_order"
    )
    cancellation_calls = [
        (operation, payload)
        for operation, payload in client.call_log[:first_close_submission]
        if operation in {"cancel_trigger_order", "cancel_order"}
    ]
    assert cancellation_calls == [
        ("cancel_trigger_order", {"instId": "BTC-USDT-SWAP", "ordId": "entry-trigger-1"}),
        (
            "cancel_order",
            {
                "instId": "BTC-USDT-SWAP",
                "clOrdId": "entry-client-2",
                "mrgPosition": "split",
            },
        ),
    ]
    with session_factory() as session:
        deferred_legs = [session.get(ExecutionOrderLeg, leg_id) for leg_id in deferred_leg_ids]
        assert {leg.id for leg in deferred_legs} == deferred_leg_ids
        assert all(leg.status == "cancelled" for leg in deferred_legs)
        assert all(
            leg.terminal_reason == "management_full_close_cancelled_unfilled_entry_leg"
            for leg in deferred_legs
        )


def test_full_close_does_not_submit_when_deferred_entry_order_match_is_ambiguous(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory, sizes=("3", "2"), intent="full_exit", effective_action="full_exit"
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        binding.pos_id = "pos-1"
        second_batch_leg = session.query(StrategyManagementLeg).filter_by(
            management_batch_id=batch.id, pos_id="pos-2"
        ).one()
        session.delete(second_batch_leg)
        deferred = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=batch.execution_binding_id, pos_id="pos-2"
        ).one()
        deferred.order_kind = "trigger_limit"
        deferred.pos_id = None
        deferred.status = "pending"
        deferred.attribution_status = "unassigned"
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored_batch.target_snapshot_json)
        snapshot["identity"]["deferred_entry_leg_ids"] = [deferred.id]
        stored_batch.target_snapshot_json = json.dumps(snapshot)
        session.commit()

    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])
    client.trigger_pending = [
        {"instId": "BTC-USDT-SWAP", "ordId": "entry-1", "posSide": "short"},
        {"instId": "BTC-USDT-SWAP", "ordId": "entry-1", "posSide": "short"},
    ]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "deferred_entry_cancel_preflight_failed"
    assert client.calls == []


def _configure_deferred_full_exit(
    session_factory,
    batch,
    *,
    include_extra_deferred=False,
):
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        binding.pos_id = "pos-1"
        second_batch_leg = session.query(StrategyManagementLeg).filter_by(
            management_batch_id=batch.id, pos_id="pos-2"
        ).one()
        session.delete(second_batch_leg)
        deferred = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=batch.execution_binding_id, pos_id="pos-2"
        ).one()
        deferred.order_kind = "trigger_limit"
        deferred.order_id = "deferred-order-1"
        deferred.client_order_id = "deferred-client-1"
        deferred.pos_id = None
        deferred.status = "pending"
        deferred.attribution_status = "unassigned"
        deferred_ids = [deferred.id]
        if include_extra_deferred:
            extra = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="deferred-order-2",
                client_order_id="deferred-client-2",
                venue="deepcoin",
                attribution_status="unassigned",
                status="pending",
            )
            session.add(extra)
            session.flush()
            deferred_ids.append(extra.id)
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored_batch.target_snapshot_json)
        snapshot["identity"]["deferred_entry_leg_ids"] = list(deferred_ids)
        stored_batch.target_snapshot_json = json.dumps(snapshot)
        binding_before = {
            "status": binding.status,
            "order_id": binding.order_id,
            "client_order_id": binding.client_order_id,
            "pos_id": binding.pos_id,
        }
        session.commit()
    return deferred_ids, binding_before


def test_dabiaoke_4168_full_exit_cancels_delayed_leg_before_exact_live_close(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "dabiaoke-4168-exit.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("8", "14"),
        intent="full_exit",
        effective_action="full_exit",
    )
    deferred_ids, _ = _configure_deferred_full_exit(session_factory, batch)
    with session_factory() as session:
        unrelated = ExecutionBinding(
            strategy_instance_id="deepcoin:100:99:BTC:short",
            kol_id="alice",
            chat_id=100,
            message_id=99,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="pos-unrelated",
            status="active",
        )
        session.add(unrelated)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=unrelated.id,
                strategy_instance_id=unrelated.strategy_instance_id,
                leg_index=0,
                purpose="entry",
                order_kind="market",
                order_id="entry-unrelated",
                pos_id="pos-unrelated",
                venue="deepcoin",
                attribution_status="verified",
                status="active",
            )
        )
        session.commit()

    class MixedStrategyClient(_FakeClient):
        def list_positions(self, *, inst_id=None):
            return [
                *super().list_positions(inst_id=inst_id),
                {
                    "posId": "pos-unrelated",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "short",
                    "pos": "5",
                    "avgPx": "64100",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
                },
            ]

    client = MixedStrategyClient(
        session_factory,
        [{"code": "0", "data": {"ordId": "close-pos-live"}}],
    )
    client.trigger_pending = [
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "deferred-order-1",
            "clOrdId": "deferred-client-1",
            "posSide": "short",
        }
    ]

    first = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )
    repeated = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert first["status"] == "reconciling"
    assert repeated["status"] == "reconciling"
    assert client.cancel_trigger_calls == [
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "deferred-order-1",
            "clOrdId": "deferred-client-1",
        }
    ]
    assert [payload["closePosId"] for payload, _status in client.calls] == ["pos-1"]
    assert all(
        payload["closePosId"] != "pos-unrelated"
        for payload, _status in client.calls
    )
    cancel_index = next(
        index
        for index, (operation, _payload) in enumerate(client.call_log)
        if operation == "cancel_trigger_order"
    )
    close_index = next(
        index
        for index, (operation, _payload) in enumerate(client.call_log)
        if operation == "place_order"
    )
    assert cancel_index < close_index
    with session_factory() as session:
        delayed = session.get(ExecutionOrderLeg, deferred_ids[0])
        assert delayed.status == "cancelled"


def test_full_close_marks_definite_deferred_cancel_rejection_as_race_candidate(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory, sizes=("3", "2"), intent="full_exit", effective_action="full_exit"
    )
    _configure_deferred_full_exit(session_factory, batch)
    client = _FakeClient(session_factory)
    client.trigger_pending = [
        {"ordId": "deferred-order-1", "clOrdId": "deferred-client-1"}
    ]
    client.cancel_trigger_outcomes = [DeepcoinDefiniteRejection("not pending")]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "deferred_entry_cancel_race_detected"
    assert client.calls == []


def test_full_close_rejects_unsnapshotted_eligible_deferred_entry_before_any_cancel(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    deferred_ids, _ = _configure_deferred_full_exit(session_factory, batch)
    with session_factory() as session:
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored_batch.target_snapshot_json)
        snapshot["identity"]["deferred_entry_leg_ids"] = []
        stored_batch.target_snapshot_json = json.dumps(snapshot)
        session.commit()

    client = _FakeClient(session_factory)
    client.trigger_pending = [
        {"ordId": "deferred-order-1", "clOrdId": "deferred-client-1"}
    ]

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "deferred_entry_cancel_preflight_failed"
    assert client.cancel_trigger_calls == []
    assert client.cancel_order_calls == []
    assert client.calls == []
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, deferred_ids[0]).status == "pending"
    diagnostics = list_execution_events(
        session_factory,
        execution_binding_id=batch.execution_binding_id,
        action="strategy_management_deferred_entry_cancel_diagnostic",
    )
    assert [event.after for event in diagnostics] == [
        {
            "execution_order_leg_id": deferred_ids[0],
            "order_id": "deferred-order-1",
            "client_order_id": "deferred-client-1",
            "identity_state": "unsnapshotted_pending",
            "live_match_source": "not_checked",
            "match_type": "identity",
            "status": "unresolved",
            "reason": "unsnapshotted_pending_entry_leg",
        }
    ]


def test_full_close_identity_drift_cap_keeps_unsnapshotted_pending_leg_identifiers(
    tmp_path,
):
    """Live pending entries remain actionable when stale snapshot drift exceeds the cap."""

    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "identity-drift-cap.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    deferred_ids, _ = _configure_deferred_full_exit(session_factory, batch)
    stale_snapshot_leg_ids = list(range(10_000, 10_021))
    with session_factory() as session:
        stored_batch = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored_batch.target_snapshot_json)
        snapshot["identity"]["deferred_entry_leg_ids"] = stale_snapshot_leg_ids
        stored_batch.target_snapshot_json = json.dumps(snapshot)
        session.commit()

    client = _FakeClient(session_factory)
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert client.cancel_trigger_calls == []
    assert client.cancel_order_calls == []
    assert client.calls == []
    diagnostics = list_execution_events(
        session_factory,
        execution_binding_id=batch.execution_binding_id,
        action="strategy_management_deferred_entry_cancel_diagnostic",
    )
    assert len(diagnostics) == 20
    diagnostics_by_leg_id = {
        event.after["execution_order_leg_id"]: event.after for event in diagnostics
    }
    assert diagnostics_by_leg_id[deferred_ids[0]] == {
        "execution_order_leg_id": deferred_ids[0],
        "order_id": "deferred-order-1",
        "client_order_id": "deferred-client-1",
        "identity_state": "unsnapshotted_pending",
        "live_match_source": "not_checked",
        "match_type": "identity",
        "status": "unresolved",
        "reason": "unsnapshotted_pending_entry_leg",
    }
    assert sum(
        event.after.get("omitted_identity_drift_count", 0)
        for event in diagnostics
    ) == 2


@pytest.mark.parametrize("drift", ["deleted", "reassigned"])
def test_full_close_persists_snapshot_deferred_entry_identity_drift(
    tmp_path, drift,
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / f"{drift}.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    deferred_ids, _ = _configure_deferred_full_exit(session_factory, batch)
    with session_factory() as session:
        deferred = session.get(ExecutionOrderLeg, deferred_ids[0])
        if drift == "deleted":
            session.delete(deferred)
        else:
            deferred.strategy_instance_id = "deepcoin:other:strategy:BTC:short"
        session.commit()

    client = _FakeClient(session_factory)
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "deferred_entry_cancel_preflight_failed"
    assert client.cancel_trigger_calls == []
    assert client.cancel_order_calls == []
    assert client.calls == []
    diagnostics = list_execution_events(
        session_factory,
        execution_binding_id=batch.execution_binding_id,
        action="strategy_management_deferred_entry_cancel_diagnostic",
    )
    expected_state = "snapshot_leg_missing" if drift == "deleted" else "snapshot_leg_reassigned"
    expected_reason = (
        "snapshot_deferred_entry_leg_missing"
        if drift == "deleted"
        else "snapshot_deferred_entry_leg_reassigned"
    )
    assert [event.after for event in diagnostics] == [
        {
            "execution_order_leg_id": deferred_ids[0],
            "identity_state": expected_state,
            "live_match_source": "not_checked",
            "match_type": "identity",
            "status": "unresolved",
            "reason": expected_reason,
        }
    ]


def test_full_close_missing_later_snapshot_deferred_entry_cancels_nothing(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    deferred_ids, _ = _configure_deferred_full_exit(
        session_factory, batch, include_extra_deferred=True
    )
    client = _FakeClient(session_factory)
    client.trigger_pending = [
        {"ordId": "deferred-order-1", "clOrdId": "deferred-client-1"}
    ]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert client.cancel_trigger_calls == []
    assert client.cancel_order_calls == []
    assert client.calls == []
    with session_factory() as session:
        assert {
            session.get(ExecutionOrderLeg, leg_id).status for leg_id in deferred_ids
        } == {"pending"}


def test_full_close_later_deferred_entry_leg_missing_after_exchange_preflight_cancels_nothing(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    deferred_ids, _ = _configure_deferred_full_exit(
        session_factory, batch, include_extra_deferred=True
    )

    class DeleteLaterLegDuringExchangePreflight(_FakeClient):
        def list_open_orders(self, *, inst_id=None):
            with self.session_factory() as session:
                session.delete(session.get(ExecutionOrderLeg, deferred_ids[1]))
                session.commit()
            return []

    client = DeleteLaterLegDuringExchangePreflight(session_factory)
    client.trigger_pending = [
        {"ordId": "deferred-order-1", "clOrdId": "deferred-client-1"},
        {"ordId": "deferred-order-2", "clOrdId": "deferred-client-2"},
    ]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert client.cancel_trigger_calls == []
    assert client.cancel_order_calls == []
    assert client.calls == []
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, deferred_ids[0]).status == "pending"


def test_full_close_rejects_one_exchange_row_reused_across_two_deferred_entry_legs(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    deferred_ids, _ = _configure_deferred_full_exit(
        session_factory, batch, include_extra_deferred=True
    )
    with session_factory() as session:
        second = session.get(ExecutionOrderLeg, deferred_ids[1])
        second.order_id = "different-order"
        second.client_order_id = "deferred-client-1"
        session.commit()
    client = _FakeClient(session_factory)
    client.trigger_pending = [
        {"ordId": "deferred-order-1", "clOrdId": "deferred-client-1"}
    ]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert client.cancel_trigger_calls == []
    assert client.cancel_order_calls == []
    assert client.calls == []


def test_deferred_entry_match_rejects_conflicting_order_id_aliases(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    _configure_deferred_full_exit(session_factory, batch)
    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])
    client.trigger_pending = [
        {
            "ordId": "unrelated-preferred-id",
            "orderId": "deferred-order-1",
            "clOrdId": "deferred-client-1",
        }
    ]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert result["reason"] == "deferred_entry_cancel_preflight_failed"
    assert client.cancel_trigger_calls == []
    assert client.cancel_order_calls == []
    assert client.calls == []
    diagnostics = list_execution_events(
        session_factory,
        execution_binding_id=batch.execution_binding_id,
        action="strategy_management_deferred_entry_cancel_diagnostic",
    )
    assert len(diagnostics) == 1
    assert diagnostics[0].after == {
        "execution_order_leg_id": diagnostics[0].after["execution_order_leg_id"],
        "live_match_source": "pending_trigger_orders",
        "match_type": "trigger",
        "status": "unresolved",
        "reason": "exchange_order_id_alias_conflict",
    }


def test_deferred_entry_cancel_uses_only_aliases_that_established_ownership(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    _configure_deferred_full_exit(session_factory, batch)
    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])
    client.trigger_pending = [
        {
            "ordId": "deferred-order-1",
            "clOrdId": "unrelated-client-id",
        }
    ]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    assert client.cancel_trigger_calls == [
        {"instId": "BTC-USDT-SWAP", "ordId": "deferred-order-1"}
    ]


def test_deferred_entry_leg_and_event_write_roll_back_together_on_event_failure(
    monkeypatch, tmp_path
):
    import telegram_kol_research.strategy_management_executor as executor

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    deferred_ids, _ = _configure_deferred_full_exit(session_factory, batch)
    client = _FakeClient(session_factory)
    client.trigger_pending = [
        {"ordId": "deferred-order-1", "clOrdId": "deferred-client-1"}
    ]

    def fail_event_write(*_args, **_kwargs):
        raise RuntimeError("simulated execution event write failure")

    monkeypatch.setattr(executor, "record_execution_event", fail_event_write)
    result = executor.execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert client.cancel_trigger_calls
    assert client.calls == []
    with session_factory() as session:
        deferred = session.get(ExecutionOrderLeg, deferred_ids[0])
        assert deferred.status == "pending"
        assert deferred.terminal_reason is None


def test_deferred_entry_event_is_exact_and_binding_identity_is_unchanged(tmp_path):
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    deferred_ids, binding_before = _configure_deferred_full_exit(session_factory, batch)
    exchange_row = {
        "instId": "BTC-USDT-SWAP",
        "ordId": "deferred-order-1",
        "clOrdId": "deferred-client-1",
        "state": "live",
    }
    client = _FakeClient(session_factory, [{"code": "0", "data": {"ordId": "close-1"}}])
    client.trigger_pending = [exchange_row]

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "reconciling"
    events = list_execution_events(
        session_factory,
        execution_binding_id=batch.execution_binding_id,
        action="strategy_management_cancel_deferred_trigger_entry",
    )
    assert len(events) == 1
    event = events[0]
    assert event.created_at.replace(tzinfo=UTC) == NOW
    assert event.before == exchange_row
    assert event.request == {
        "instId": "BTC-USDT-SWAP",
        "ordId": "deferred-order-1",
        "clOrdId": "deferred-client-1",
    }
    assert event.response == {"code": "0", "data": {"ordId": "deferred-order-1"}}
    assert event.order_id == "deferred-order-1"
    assert event.client_order_id == "deferred-client-1"
    with session_factory() as session:
        binding = session.get(ExecutionBinding, batch.execution_binding_id)
        assert {
            "status": binding.status,
            "order_id": binding.order_id,
            "client_order_id": binding.client_order_id,
            "pos_id": binding.pos_id,
        } == binding_before
        deferred = session.get(ExecutionOrderLeg, deferred_ids[0])
        assert deferred.last_verified_at.replace(tzinfo=UTC) == NOW


def test_execute_full_close_with_deferred_entry_then_reconcile_reaches_completion(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import execute_management_batch
    from telegram_kol_research.strategy_management_reconciliation import (
        reconcile_strategy_management_batches,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    deferred_ids, _ = _configure_deferred_full_exit(session_factory, batch)
    client = _FakeClient(
        session_factory, [{"code": "0", "data": {"ordId": "close-pos-1"}}]
    )
    client.trigger_pending = [
        {"ordId": "deferred-order-1", "clOrdId": "deferred-client-1"}
    ]

    executed = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )
    submitted = load_management_batch(session_factory, batch.id)
    reconciled = reconcile_strategy_management_batches(
        session_factory,
        snapshot=SimpleNamespace(
            positions=[],
            open_orders=[
                {
                    "ordId": submitted.legs[0].exchange_order_id,
                    "clOrdId": submitted.legs[0].client_order_id,
                }
            ],
            order_history=[],
            trade_fills=[],
            errors={},
        ),
        reconciled_at=NOW,
        batch_ids={batch.id},
    )

    final = load_management_batch(session_factory, batch.id)
    assert executed["status"] == "reconciling"
    assert reconciled.succeeded == 1
    assert final.status == "succeeded"
    with session_factory() as session:
        deferred = session.get(ExecutionOrderLeg, deferred_ids[0])
        assert deferred.status == "cancelled"
        assert deferred.terminal_reason == (
            "management_full_close_cancelled_unfilled_entry_leg"
        )


def test_deferred_cancel_failure_transition_conflict_is_explicit(monkeypatch, tmp_path):
    import telegram_kol_research.strategy_management_executor as executor

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(
        session_factory,
        sizes=("3", "2"),
        intent="full_exit",
        effective_action="full_exit",
    )
    _configure_deferred_full_exit(session_factory, batch)

    class FailingCancelClient(_FakeClient):
        def cancel_trigger_order(self, payload):
            super().cancel_trigger_order(payload)
            raise RuntimeError("cancel unavailable")

    client = FailingCancelClient(session_factory)
    client.trigger_pending = [
        {"ordId": "deferred-order-1", "clOrdId": "deferred-client-1"}
    ]
    monkeypatch.setattr(executor, "transition_batch", lambda *_args, **_kwargs: False)

    with pytest.raises(
        executor.ManagementBatchExecutionError,
        match="management_batch_deferred_cancel_transition_conflict",
    ):
        executor.execute_management_batch(
            session_factory,
            batch_id=batch.id,
            deepcoin_client=client,
            executed_at=NOW,
        )

    assert client.calls == []


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


def test_independent_position_success_is_never_replayed_after_sibling_failure(
    tmp_path,
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    first = _FakeClient(
        session_factory,
        [
            {"code": "0", "data": {"ordId": "close-1"}},
            DeepcoinDefiniteRejection("second position rejected"),
        ],
    )

    result = execute_management_batch(
        session_factory,
        batch_id=batch.id,
        deepcoin_client=first,
        executed_at=NOW,
    )

    assert result["status"] == "partial_failed"
    assert [leg.status for leg in load_management_batch(
        session_factory, batch.id
    ).legs] == ["submitted", "failed"]

    restarted = _FakeClient(session_factory)
    with pytest.raises(
        ManagementBatchExecutionError,
        match="batch_not_executable:partial_failed",
    ):
        execute_management_batch(
            session_factory,
            batch_id=batch.id,
            deepcoin_client=restarted,
            executed_at=NOW,
        )
    assert restarted.calls == []


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


def test_partial_then_break_even_uses_explicit_stop_and_ignores_zero_combined_side(
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
        stop_loss="64100",
        keep_close_plan=True,
    )
    rows_by_pos["pos-1"] = [
        {
            "triggerOrderType": "TPSL",
            "ordId": "legacy-tp-zero-sl",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "posId": "pos-1",
            "tpTriggerPx": "63000",
            "tpTriggerPxType": "mark",
            "tpOrdPx": "-1",
            "slTriggerPx": "0",
            "slTriggerPxType": "last",
            "slOrdPx": "-1",
            "sz": "1",
            "cTime": "1000",
        },
        {
            "triggerOrderType": "TPSL",
            "ordId": "legacy-sl-zero-tp",
            "instId": "BTC-USDT-SWAP",
            "posSide": "short",
            "posId": "pos-1",
            "tpTriggerPx": "0",
            "tpTriggerPxType": "last",
            "tpOrdPx": "-1",
            "slTriggerPx": "65500",
            "slTriggerPxType": "index",
            "slOrdPx": "-1",
            "sz": "0",
            "cTime": "1000",
        },
    ]
    legacy_snapshots = [
        {
            "order_id": "legacy-tp-zero-sl",
            "purpose": "combined",
            "take_profit": {
                "trigger_price": "63000",
                "trigger_type": "mark",
                "order_price": "-1",
            },
            "stop_loss": {
                "trigger_price": "0",
                "trigger_type": "last",
                "order_price": "-1",
            },
            "size": "1",
            "full_position": False,
        },
        {
            "order_id": "legacy-sl-zero-tp",
            "purpose": "combined",
            "take_profit": {
                "trigger_price": "0",
                "trigger_type": "last",
                "order_price": "-1",
            },
            "stop_loss": {
                "trigger_price": "65500",
                "trigger_type": "index",
                "order_price": "-1",
            },
            "size": "0",
            "full_position": True,
        },
    ]
    with session_factory() as session:
        leg = (
            session.query(StrategyManagementLeg)
            .filter(
                StrategyManagementLeg.management_batch_id == batch.id,
                StrategyManagementLeg.pos_id == "pos-1",
            )
            .one()
        )
        leg.avg_entry_price = "64103.8"
        leg.old_tpsl_json = json.dumps(
            {
                "status": "verified",
                "order_ids": ["legacy-tp-zero-sl", "legacy-sl-zero-tp"],
                "rows": rows_by_pos["pos-1"],
                "row_snapshots": legacy_snapshots,
            }
        )
        for row, purpose in zip(
            rows_by_pos["pos-1"],
            ("take_profit", "stop_loss"),
        ):
            upsert_protection_ledger_row(
                session,
                venue="deepcoin",
                execution_binding_id=batch.execution_binding_id,
                execution_order_leg_id=leg.execution_order_leg_id,
                strategy_instance_id=batch.strategy_instance_id,
                pos_id="pos-1",
                instrument_id="BTC-USDT-SWAP",
                side="short",
                order_id=row["ordId"],
                purpose=purpose,
                trigger_price=(
                    row.get("tpTriggerPx") or row.get("slTriggerPx")
                ),
                size_text=row.get("sz"),
                status="verified",
                evidence_source="test_exact_owner",
                evidence={"source": "legacy_combined_fixture"},
                seen_at=NOW,
            )
        session.commit()

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
                    "avgPx": "64103.8",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
                    "cTime": "1000",
            },
            {
                "posId": "pos-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "2",
                    "avgPx": "64500",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
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
    protection_result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert protection_result["status"] == "succeeded"
    assert [
        (row["posId"], row.get("tpTriggerPx"), row.get("slTriggerPx"), row.get("sz"))
        for row in client.set_calls
        if row["posId"] == "pos-1"
    ] == [
        ("pos-1", None, "64100", None),
    ]
    assert all(row.get("slTriggerPx") != "64103.8" for row in client.set_calls)


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
    rows_by_pos["pos-1"] = [
        row for row in rows_by_pos["pos-1"] if row["ordId"] != "tp-1b"
    ]
    with session_factory() as session:
        stored = session.get(StrategyManagementBatch, batch.id)
        pos_one_leg = (
            session.query(StrategyManagementLeg)
            .filter_by(management_batch_id=batch.id, pos_id="pos-1")
            .one()
        )
        pos_one_leg.old_tpsl_json = json.dumps(
            {
                "status": "verified",
                "order_ids": [row["ordId"] for row in rows_by_pos["pos-1"]],
                "rows": rows_by_pos["pos-1"],
                "row_snapshots": snapshot_protection_rows(
                    rows_by_pos["pos-1"]
                ),
            }
        )
        snapshot = json.loads(stored.target_snapshot_json)
        snapshot["protection_recovery"] = {
            "version": 1,
            "mode": "replace_after_reduction",
            "positions": [
                {
                    "pos_id": pos_id,
                    "execution_order_leg_id": next(
                        leg.execution_order_leg_id
                        for leg in batch.legs
                        if leg.pos_id == pos_id
                    ),
                    "owned_order_ids": [
                        row["ordId"] for row in rows_by_pos[pos_id]
                    ],
                }
                for pos_id in ("pos-1", "pos-2")
            ],
        }
        stored.target_snapshot_json = json.dumps(snapshot)
        session.commit()
    client = _ProtectionClient(session_factory, rows_by_pos)

    close_result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert close_result["status"] == "reconciling"
    assert len(client.close_calls) == 2
    assert [call[0] for call in client.call_log[:4]] == [
        "cancel_position_sltp",
        "cancel_position_sltp",
        "cancel_position_sltp",
        "cancel_position_sltp",
    ]
    assert [call[0] for call in client.call_log[4:]] == [
        "place_order",
        "place_order",
    ]
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
                    "mgnMode": "cross",
                    "mrgPosition": "split",
                    "cTime": "1000",
            },
            {
                "posId": "pos-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "2",
                    "avgPx": "64500",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
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
    assert len(client.cancel_calls) == 4
    assert client.set_calls == []

    client.positions = list(snapshot.positions)
    protection_result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert protection_result["status"] == "succeeded"
    assert len(client.cancel_calls) == 4
    stops = [row for row in client.set_calls if "slTriggerPx" in row]
    assert [(row["posId"], row["slTriggerPx"]) for row in stops] == [
        ("pos-1", "64000"),
        ("pos-2", "64500"),
    ]
    take_profits = [row for row in client.set_calls if "tpTriggerPx" in row]
    assert [
        (row["posId"], row["tpTriggerPx"], row["sz"])
        for row in take_profits
    ] == [
        ("pos-2", "62500", "2"),
    ]


def test_close_preflight_falls_back_from_inline_price_to_exact_ledger(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        _preflight_exact_protection_rows,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(
        session_factory,
        action="partial_then_break_even",
        stop_loss=None,
        keep_close_plan=True,
    )
    with session_factory() as session:
        for leg in batch.legs:
            for row in rows_by_pos[leg.pos_id]:
                purpose = (
                    "take_profit" if row.get("tpTriggerPx") else "stop_loss"
                )
                trigger_price = row.get("tpTriggerPx") or row.get("slTriggerPx")
                upsert_protection_ledger_row(
                    session,
                    venue="deepcoin",
                    execution_binding_id=batch.execution_binding_id,
                    execution_order_leg_id=leg.execution_order_leg_id,
                    strategy_instance_id=batch.strategy_instance_id,
                    pos_id=leg.pos_id,
                    instrument_id="BTC-USDT-SWAP",
                    side="short",
                    order_id=row["ordId"],
                    purpose=purpose,
                    trigger_price=trigger_price,
                    size_text=row["sz"],
                    status="verified",
                    evidence_source="official_ui_supervised",
                    evidence={"match": "reviewed_current_order"},
                    seen_at=NOW,
                )
        session.commit()

    client = _ProtectionClient(session_factory, rows_by_pos)
    for position in client.positions:
        position["slTriggerPx"] = "65500"
        position["tpTriggerPx"] = ""
    for index, row in enumerate(client.pending):
        row.pop("posId", None)
        row["cTime"] = str(100_000 + index * 10_000)

    current = _preflight_exact_protection_rows(
        session_factory=session_factory,
        batch=batch,
        live_positions=client.positions,
        pending=client.pending,
    )

    assert {
        row["order_id"] for row in current["pos-1"]
    } == {"tp-1a", "tp-1b", "sl-1"}
    assert {
        row["order_id"] for row in current["pos-2"]
    } == {"tp-2", "sl-2"}


def test_partial_then_break_even_cancel_unknown_never_submits_close(tmp_path):
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
        snapshot = json.loads(stored.target_snapshot_json)
        snapshot["protection_recovery"] = {
            "version": 1,
            "mode": "replace_after_reduction",
            "positions": [
                {
                    "pos_id": leg.pos_id,
                    "execution_order_leg_id": leg.execution_order_leg_id,
                    "owned_order_ids": [
                        row["ordId"] for row in rows_by_pos[leg.pos_id]
                    ],
                }
                for leg in batch.legs
            ],
        }
        stored.target_snapshot_json = json.dumps(snapshot)
        session.commit()
    client = _ProtectionClient(
        session_factory,
        rows_by_pos,
        cancel_outcomes=[DeepcoinRequestOutcomeUnknown("response lost")],
    )

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert client.close_calls == []
    events = list_execution_events(
        session_factory,
        action="strategy_management_protection_precancel",
    )
    assert [event.status for event in events] == ["reserved"]


@pytest.mark.parametrize(
    ("close_gate_after_cancel", "expected_cancel_count", "expected_leg_statuses"),
    [
        (1, 1, ["planned", "planned"]),
        (5, 5, ["recovery_required", "planned"]),
    ],
)
def test_remediation_gate_is_rechecked_before_every_exchange_write(
    close_gate_after_cancel,
    expected_cancel_count,
    expected_leg_statuses,
    tmp_path,
):
    from telegram_kol_research.execution_bindings import _load_reconcile_snapshot
    from telegram_kol_research.position_management_remediation import (
        _fingerprint,
        _snapshot_payload,
    )
    from telegram_kol_research.strategy_management_executor import execute_management_batch

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(
        session_factory,
        action="partial_then_break_even",
        stop_loss=None,
        keep_close_plan=True,
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        },
    )

    class GateClosingClient(_ProtectionClient):
        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id):
            return []

        def list_trade_fills(self, *, inst_id):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return []

        def cancel_position_sltp(self, payload):
            response = super().cancel_position_sltp(payload)
            if len(self.cancel_calls) == close_gate_after_cancel:
                save_trading_settings(
                    session_factory,
                    {
                        "auto_trade_enabled": True,
                        "management_execution_mode": "disabled",
                    },
                )
            return response

    client = GateClosingClient(session_factory, rows_by_pos)
    exchange_snapshot = _load_reconcile_snapshot(
        client, instruments={"BTC-USDT-SWAP"}
    )
    with session_factory() as session:
        stored = session.get(StrategyManagementBatch, batch.id)
        snapshot = json.loads(stored.target_snapshot_json)
        snapshot["protection_recovery"] = {
            "version": 1,
            "mode": "replace_after_reduction",
            "positions": [
                {
                    "pos_id": leg.pos_id,
                    "execution_order_leg_id": leg.execution_order_leg_id,
                    "owned_order_ids": [
                        row["ordId"] for row in rows_by_pos[leg.pos_id]
                    ],
                }
                for leg in batch.legs
            ],
        }
        snapshot["remediation_confirmation"] = {
            "action_id": "test-action",
            "action_fingerprint": "f" * 64,
            "exchange_snapshot_fingerprint": _fingerprint(
                _snapshot_payload(exchange_snapshot)
            ),
            "instrument_scope": ["BTC-USDT-SWAP"],
        }
        stored.target_snapshot_json = json.dumps(snapshot)
        stored.execution_mode = "live"
        session.commit()

    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "recovery_required"
    assert len(client.cancel_calls) == expected_cancel_count
    assert client.close_calls == []
    assert [leg["status"] for leg in result["legs"]] == expected_leg_statuses


def test_rejected_close_restores_protection_with_durable_ledger(tmp_path):
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
        snapshot = json.loads(stored.target_snapshot_json)
        snapshot["protection_recovery"] = {
            "version": 1,
            "mode": "replace_after_reduction",
            "positions": [
                {
                    "pos_id": leg.pos_id,
                    "execution_order_leg_id": leg.execution_order_leg_id,
                    "owned_order_ids": [
                        row["ordId"] for row in rows_by_pos[leg.pos_id]
                    ],
                }
                for leg in batch.legs
            ],
        }
        stored.target_snapshot_json = json.dumps(snapshot)
        session.commit()

    class FirstCloseRejectedClient(_ProtectionClient):
        def place_order(self, payload):
            if not self.close_calls:
                self.close_calls.append(dict(payload))
                self.call_log.append(("place_order", str(payload.get("posId"))))
                raise DeepcoinDefiniteRejection("close rejected")
            return super().place_order(payload)

    client = FirstCloseRejectedClient(session_factory, rows_by_pos)
    result = execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert result["status"] == "partial_failed"
    restore_events = list_execution_events(
        session_factory,
        action="strategy_management_protection_restore",
    )
    assert [event.status for event in restore_events].count("reserved") == 3
    assert [event.status for event in restore_events].count("succeeded") == 3
    with session_factory() as session:
        verified = list_verified_ledger_rows_for_positions(session, ["pos-1"])
    assert {row.order_id for row in verified} == {"new-1", "new-2", "new-3"}


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
                    "mgnMode": "cross",
                    "mrgPosition": "split",
                    "cTime": "1000",
            },
            {
                "posId": "pos-2",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "2",
                    "avgPx": "64500",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
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
        if row["posId"] == "pos-1"
    ] == [
        ("pos-1", "62000", None, "1"),
        ("pos-1", None, "64000", None),
    ]
    assert [
        (row["posId"], row.get("tpTriggerPx"), row.get("slTriggerPx"), row.get("sz"))
        for row in client.set_calls
        if row["posId"] == "pos-2"
    ] == [
        ("pos-2", "62500", None, "2"),
        ("pos-2", None, "64500", None),
    ]


def test_partial_protection_consumes_completed_take_profit_stage():
    from telegram_kol_research.strategy_management_executor import (
        _resize_protection_rows_for_remaining_position,
    )

    batch = SimpleNamespace(
        target_snapshot={
            "contract_spec": {"quantity_step": "1", "min_quantity": "1"}
        }
    )
    leg = SimpleNamespace(
        preflight_size="3",
        planned_close_size="1",
        quantity_step="1",
    )
    rows = [
        {"purpose": "stop_loss", "size": "3", "trigger_price": "61000"},
        {"purpose": "take_profit", "size": "1", "trigger_price": "65600"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "67100"},
    ]

    resized = _resize_protection_rows_for_remaining_position(
        batch=batch,
        leg=leg,
        rows=rows,
    )

    assert resized == [
        {"purpose": "stop_loss", "size": "2", "trigger_price": "61000"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "67100"},
    ]


def test_partial_protection_consumes_production_ladder_first_stage():
    from telegram_kol_research.strategy_management_executor import (
        _resize_protection_rows_for_remaining_position,
    )

    batch = SimpleNamespace(
        target_snapshot={
            "contract_spec": {"quantity_step": "1", "min_quantity": "1"}
        }
    )
    leg = SimpleNamespace(
        preflight_size="8",
        planned_close_size="4",
        quantity_step="1",
    )
    rows = [
        {"purpose": "stop_loss", "size": "8", "trigger_price": "65500"},
        {"purpose": "take_profit", "size": "4", "trigger_price": "63800"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "63100"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "62400"},
    ]

    resized = _resize_protection_rows_for_remaining_position(
        batch=batch,
        leg=leg,
        rows=rows,
    )

    assert resized == [
        {"purpose": "stop_loss", "size": "4", "trigger_price": "65500"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "63100"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "62400"},
    ]


def test_partial_protection_consumes_part_of_next_take_profit_stage():
    from telegram_kol_research.strategy_management_executor import (
        _resize_protection_rows_for_remaining_position,
    )

    batch = SimpleNamespace(
        target_snapshot={
            "contract_spec": {"quantity_step": "1", "min_quantity": "1"}
        }
    )
    leg = SimpleNamespace(
        preflight_size="8",
        planned_close_size="5",
        quantity_step="1",
    )
    rows = [
        {"purpose": "stop_loss", "size": "8", "trigger_price": "65500"},
        {"purpose": "take_profit", "size": "4", "trigger_price": "63800"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "63100"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "62400"},
    ]

    resized = _resize_protection_rows_for_remaining_position(
        batch=batch,
        leg=leg,
        rows=rows,
    )

    assert resized == [
        {"purpose": "stop_loss", "size": "3", "trigger_price": "65500"},
        {"purpose": "take_profit", "size": "1", "trigger_price": "63100"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "62400"},
    ]


def test_partial_protection_consumes_every_existing_take_profit_stage():
    from telegram_kol_research.strategy_management_executor import (
        _resize_protection_rows_for_remaining_position,
    )

    batch = SimpleNamespace(
        target_snapshot={
            "contract_spec": {"quantity_step": "1", "min_quantity": "1"}
        }
    )
    leg = SimpleNamespace(
        preflight_size="8",
        planned_close_size="6",
        quantity_step="1",
    )
    rows = [
        {"purpose": "stop_loss", "size": "8", "trigger_price": "65500"},
        {"purpose": "take_profit", "size": "4", "trigger_price": "63800"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "63100"},
    ]

    resized = _resize_protection_rows_for_remaining_position(
        batch=batch,
        leg=leg,
        rows=rows,
    )

    assert resized == [
        {"purpose": "stop_loss", "size": "2", "trigger_price": "65500"}
    ]


def test_partial_protection_rejects_take_profit_total_exceeding_preflight_size():
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        _resize_protection_rows_for_remaining_position,
    )

    batch = SimpleNamespace(
        target_snapshot={
            "contract_spec": {"quantity_step": "1", "min_quantity": "1"}
        }
    )
    leg = SimpleNamespace(
        preflight_size="4",
        planned_close_size="1",
        quantity_step="1",
    )
    rows = [
        {"purpose": "stop_loss", "size": "4", "trigger_price": "61000"},
        {"purpose": "take_profit", "size": "3", "trigger_price": "65000"},
        {"purpose": "take_profit", "size": "2", "trigger_price": "66000"},
    ]

    with pytest.raises(
        ManagementBatchExecutionError,
        match="protection_take_profit_total_exceeds_preflight_size",
    ):
        _resize_protection_rows_for_remaining_position(
            batch=batch,
            leg=leg,
            rows=rows,
        )


@pytest.mark.parametrize(
    ("quantity_step", "min_quantity"),
    [("0", "1"), ("-1", "1"), ("1", "0"), ("1", "-1")],
)
def test_partial_protection_rejects_nonpositive_contract_quantities(
    quantity_step, min_quantity
):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        _resize_protection_rows_for_remaining_position,
    )

    batch = SimpleNamespace(
        target_snapshot={
            "contract_spec": {
                "quantity_step": quantity_step,
                "min_quantity": min_quantity,
            }
        }
    )
    leg = SimpleNamespace(
        preflight_size="4",
        planned_close_size="1",
        quantity_step=quantity_step,
    )
    rows = [
        {"purpose": "stop_loss", "size": "4", "trigger_price": "61000"},
        {"purpose": "take_profit", "size": "4", "trigger_price": "65000"},
    ]

    with pytest.raises(
        ManagementBatchExecutionError,
        match="protection_remaining_contract_spec_invalid",
    ):
        _resize_protection_rows_for_remaining_position(
            batch=batch,
            leg=leg,
            rows=rows,
        )


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
    with session_factory() as session:
        for leg in batch.legs:
            for row in rows_by_pos[leg.pos_id]:
                purpose = (
                    "take_profit" if row.get("tpTriggerPx") else "stop_loss"
                )
                upsert_protection_ledger_row(
                    session,
                    venue="deepcoin",
                    execution_binding_id=batch.execution_binding_id,
                    execution_order_leg_id=leg.execution_order_leg_id,
                    strategy_instance_id=batch.strategy_instance_id,
                    pos_id=leg.pos_id,
                    instrument_id="BTC-USDT-SWAP",
                    side="short",
                    order_id=row["ordId"],
                    purpose=purpose,
                    trigger_price=row.get("tpTriggerPx")
                    or row.get("slTriggerPx"),
                    size_text=row["sz"],
                    status="verified",
                    evidence_source="official_ui_supervised",
                    evidence={"match": "reviewed_current_order"},
                    seen_at=NOW,
                )
        session.commit()
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
    with session_factory() as session:
        old_rows = session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.order_id.in_(
                {"tp-1a", "tp-1b", "sl-1", "tp-2", "sl-2"}
            )
        ).all()
        restored = session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.order_id.in_(
                {"restore-tp2", "restore-sl2"}
            )
        ).all()
    assert {row.status for row in old_rows} == {"cancelled"}
    assert {row.order_id for row in restored} == {
        "restore-tp2",
        "restore-sl2",
    }
    assert {row.status for row in restored} == {"verified"}
    assert {row.evidence_source for row in restored} == {
        "management_tpsl_restore"
    }


def test_restore_failure_marks_recovery_required_and_continues_independent_legs(
    tmp_path,
):
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
        "succeeded",
    ]
    assert [call["ordId"] for call in client.cancel_calls] == [
        "tp-1a",
        "tp-1b",
        "sl-1",
        "tp-2",
        "sl-2",
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
        "succeeded",
    ]
    # The uncertain leg is never restored or retried, while the independent
    # sibling still completes from its own durable state.
    assert [call["ordId"] for call in client.cancel_calls] == [
        "tp-1a",
        "tp-1b",
        "sl-1",
        "tp-2",
        "sl-2",
    ]
    assert len(client.set_calls) == 3


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


def test_explicit_stop_on_triggered_market_side_blocks_before_any_write(tmp_path):
    from telegram_kol_research.strategy_management_executor import (
        ManagementBatchExecutionError,
        execute_management_batch,
    )

    session_factory = create_session_factory(tmp_path / "market-side-stop.db")
    batch, rows_by_pos = _persist_protection_batch(
        session_factory,
        action="adjust_stop_loss",
        stop_loss="64000",
    )
    client = _ProtectionClient(session_factory, rows_by_pos)
    client.quote["price"] = "64200"

    with pytest.raises(
        ManagementBatchExecutionError,
        match="explicit_stop_market_side_invalid",
    ):
        execute_management_batch(
            session_factory,
            batch_id=batch.id,
            deepcoin_client=client,
            executed_at=NOW,
        )

    assert client.cancel_calls == []
    assert client.set_calls == []


def _persist_composite_consumption_component(session_factory):
    from telegram_kol_research.models import StrategyManagementComponent
    from telegram_kol_research.strategy_management_contracts import (
        ManagementInstructionContract,
        management_contract_fingerprint,
        serialize_management_contract,
    )

    contract = ManagementInstructionContract(
        version=2,
        target_lifecycle_id=1,
        strategy_instance_id="strategy-composite",
        symbol="BTC",
        side="long",
        close_fraction="0.5",
        stop_mode="actual_entry_price",
        stop_price=None,
        stop_price_source=None,
        take_profit_consumption="consume_first_stage",
        cancel_deferred_entries=True,
        required_components=(
            "consume_take_profit_stage",
            "converge_partial_close",
            "replace_remaining_protection",
        ),
        current_message_text="止盈50%，止损移动至开仓价",
    )
    contract_json = serialize_management_contract(contract)
    fingerprint = management_contract_fingerprint(contract)
    with session_factory() as session:
        raw = RawMessage(chat_id=701, message_id=2, text="manage", posted_at=NOW)
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
            id=1,
            chat_id=701,
            message_id=1,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
        )
        binding = ExecutionBinding(
            strategy_instance_id="strategy-composite",
            kol_id="miya",
            chat_id=701,
            message_id=1,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            pos_id="pos-composite",
            status="active",
        )
        session.add_all([decision, lifecycle, binding])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        entry_leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            pos_id="pos-composite",
            venue="deepcoin",
            attribution_status="verified",
            response_json='{"data":{"posId":"pos-composite"}}',
            status="active",
        )
        session.add(entry_leg)
        session.flush()
        batch = StrategyManagementBatch(
            idempotency_fingerprint="composite-batch",
            raw_message_id=raw.id,
            recognition_decision_id=decision.id,
            recognition_generation="composite-generation",
            target_lifecycle_id=lifecycle.id,
            strategy_instance_id=binding.strategy_instance_id,
            execution_binding_id=binding.id,
            intent="partial_then_break_even",
            effective_action="partial_then_break_even",
            execution_mode="live",
            requested_fraction=0.5,
            effective_fraction=0.5,
            management_contract_json=contract_json,
            management_contract_fingerprint=fingerprint,
            contract_version=2,
            status="ready",
            target_fingerprint="target-composite",
            target_snapshot_json=json.dumps(
                {
                    "positions": [
                        {
                            "pos_id": "pos-composite",
                            "trusted_start_size": "10",
                            "target_remaining_size": "5",
                            "avg_entry_price": "64000",
                            "quantity_step": "1",
                            "min_quantity": "1",
                        }
                    ]
                }
            ),
            planned_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(batch)
        session.flush()
        management_leg = StrategyManagementLeg(
            management_batch_id=batch.id,
            execution_order_leg_id=entry_leg.id,
            pos_id="pos-composite",
            leg_index=0,
            status="planned",
            preflight_size="10",
            planned_close_size="5",
            avg_entry_price="64000",
            quantity_step="1",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(management_leg)
        session.flush()
        components = []
        for sequence, kind in enumerate(contract.required_components):
            component = StrategyManagementComponent(
                management_batch_id=batch.id,
                strategy_management_leg_id=management_leg.id,
                strategy_management_leg_scope=management_leg.id,
                component_kind=kind,
                sequence=sequence,
                status="pending",
                idempotency_key=f"component:{kind}",
                desired_json=json.dumps(
                    {
                        "contract_fingerprint": fingerprint,
                        "pos_id": "pos-composite",
                        "execution_order_leg_id": entry_leg.id,
                        "trusted_start_size": "10",
                        "target_remaining_size": "5",
                        "avg_entry_price": "64000",
                        "quantity_step": "1",
                        "min_quantity": "1",
                        "component_kind": kind,
                    },
                    sort_keys=True,
                ),
                evidence_json="[]",
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(component)
            components.append(component)
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=entry_leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id="pos-composite",
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id="tp-first",
                purpose="take_profit",
                trigger_price="65000",
                size_text="5",
                status="verified",
                evidence_source="native_tpsl_pending_readback",
                evidence_json="{}",
                first_seen_at=NOW,
                last_seen_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
        return batch.id, components[0].id


class _CompositeConsumptionClient:
    def __init__(self, outcome="success"):
        self.outcomes = [outcome]
        self.pending = [
            {
                "ordId": "tp-first",
                "posId": "pos-composite",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "triggerOrderType": "TPSL",
                "tpTriggerPx": "65000",
                "sz": "5",
            }
        ]
        self.history = []
        self.fills = []
        self.cancel_calls = []
        self.close_calls = []
        self.on_cancel = None

    def list_positions(self, *, inst_id=None):
        return [
            {
                "posId": "pos-composite",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "10",
                "avgPx": "64000",
                "mgnMode": "cross",
                "mrgPosition": "split",
            }
        ]

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending)

    def list_trigger_orders_history(self, *, inst_id):
        return list(self.history)

    def list_order_history(self, *, inst_id):
        return list(self.history)

    def list_trade_fills(self, *, inst_id):
        return list(self.fills)

    def cancel_position_sltp(self, payload):
        if self.on_cancel is not None:
            self.on_cancel()
        self.cancel_calls.append(dict(payload))
        outcome = self.outcomes.pop(0) if self.outcomes else "success"
        if outcome == "unknown":
            raise DeepcoinRequestOutcomeUnknown("cancel timeout")
        if outcome == "filled_race":
            self.pending = []
            self.history = [{"ordId": "tp-first", "state": "filled", "posId": "pos-composite"}]
            self.fills = [{"ordId": "tp-first", "posId": "pos-composite", "fillSz": "5"}]
            raise DeepcoinDefiniteRejection("already filled")
        if outcome == "rejected":
            raise DeepcoinDefiniteRejection("cancel rejected")
        self.pending = []
        return {"code": "0", "data": {"ordId": "tp-first"}}


def _execute_consumption(session_factory, batch_id, component_id, client):
    from telegram_kol_research.strategy_management_composite_executor import (
        execute_take_profit_consumption_component,
    )

    return execute_take_profit_consumption_component(
        session_factory,
        batch_id=batch_id,
        component_id=component_id,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )


def test_composite_pending_first_tp_is_cancelled_before_any_close(tmp_path):
    session_factory = create_session_factory(tmp_path / "consume.db")
    batch_id, component_id = _persist_composite_consumption_component(session_factory)
    client = _CompositeConsumptionClient()
    observed = {}

    def observe_durable_prewrite_state():
        from telegram_kol_research.models import StrategyManagementComponent

        with session_factory() as session:
            component = session.get(StrategyManagementComponent, component_id)
            observed["status"] = component.status
            observed["desired"] = json.loads(component.desired_json)

    client.on_cancel = observe_durable_prewrite_state

    result = _execute_consumption(session_factory, batch_id, component_id, client)

    assert result.status == "confirmed"
    assert len(client.cancel_calls) == 1
    assert client.close_calls == []
    assert observed["status"] == "submitting"
    assert observed["desired"]["take_profit_consumption_execution"][
        "cancel_order_ids"
    ] == ["tp-first"]


def test_composite_cancel_unknown_awaits_exchange_and_restart_never_resubmits(tmp_path):
    session_factory = create_session_factory(tmp_path / "consume-unknown.db")
    batch_id, component_id = _persist_composite_consumption_component(session_factory)
    client = _CompositeConsumptionClient("unknown")

    first = _execute_consumption(session_factory, batch_id, component_id, client)
    second = _execute_consumption(session_factory, batch_id, component_id, client)

    assert first.status == second.status == "awaiting_exchange"
    assert len(client.cancel_calls) == 1
    assert client.close_calls == []


def test_composite_tp_fill_during_cancel_counts_as_fulfilled(tmp_path):
    session_factory = create_session_factory(tmp_path / "consume-fill.db")
    batch_id, component_id = _persist_composite_consumption_component(session_factory)
    client = _CompositeConsumptionClient("filled_race")

    result = _execute_consumption(session_factory, batch_id, component_id, client)

    assert result.status == "confirmed"
    assert result.proven_filled_quantity == "5"
    assert len(client.cancel_calls) == 1
    assert client.close_calls == []


def test_composite_rejected_cancel_retries_only_with_fresh_pending_proof(tmp_path):
    session_factory = create_session_factory(tmp_path / "consume-retry.db")
    batch_id, component_id = _persist_composite_consumption_component(session_factory)
    client = _CompositeConsumptionClient("rejected")

    first = _execute_consumption(session_factory, batch_id, component_id, client)
    client.pending = []
    second = _execute_consumption(session_factory, batch_id, component_id, client)
    client.pending = [_CompositeConsumptionClient().pending[0]]
    client.outcomes = ["success"]
    third = _execute_consumption(session_factory, batch_id, component_id, client)

    assert first.status == "recovery_required"
    assert second.status == "recovery_required"
    assert len(client.cancel_calls) == 2
    assert third.status == "confirmed"


def test_composite_incomplete_exchange_snapshot_never_writes(tmp_path):
    session_factory = create_session_factory(tmp_path / "consume-incomplete.db")
    batch_id, component_id = _persist_composite_consumption_component(session_factory)
    client = _CompositeConsumptionClient()
    client.list_trade_fills = None

    result = _execute_consumption(session_factory, batch_id, component_id, client)

    assert result.status == "recovery_required"
    assert result.reason_code == "take_profit_exchange_snapshot_incomplete"
    assert client.cancel_calls == []
    assert client.close_calls == []


def _prepare_composite_close_component(session_factory):
    from telegram_kol_research.models import StrategyManagementComponent

    batch_id, _ = _persist_composite_consumption_component(session_factory)
    with session_factory() as session:
        components = (
            session.query(StrategyManagementComponent)
            .filter(StrategyManagementComponent.management_batch_id == batch_id)
            .order_by(StrategyManagementComponent.sequence.asc())
            .all()
        )
        components[0].status = "confirmed"
        components[0].completed_at = NOW
        session.commit()
        return batch_id, components[1].id


class _CompositeCloseClient(_CompositeConsumptionClient):
    def __init__(self, outcome="confirmed", *, current_size="10"):
        super().__init__()
        self.close_outcome = outcome
        self.current_size = current_size

    def list_positions(self, *, inst_id=None):
        rows = super().list_positions(inst_id=inst_id)
        rows[0]["pos"] = self.current_size
        return rows

    def place_order(self, payload):
        self.close_calls.append(dict(payload))
        if self.close_outcome == "unknown":
            raise DeepcoinRequestOutcomeUnknown("close timeout")
        if self.close_outcome == "rejected":
            raise DeepcoinDefiniteRejection("close rejected")
        if self.close_outcome == "partial_unknown":
            self.current_size = "7"
            raise DeepcoinRequestOutcomeUnknown("partial close timeout")
        if self.close_outcome == "confirmed":
            self.current_size = "5"
        return {"code": "0", "data": {"ordId": "close-composite"}}


def _execute_composite_close(session_factory, batch_id, component_id, client):
    from telegram_kol_research.strategy_management_composite_executor import (
        execute_partial_close_component,
    )

    return execute_partial_close_component(
        session_factory,
        batch_id=batch_id,
        component_id=component_id,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
    )


def test_composite_close_submits_exact_delta_and_confirms_target_remaining(tmp_path):
    session_factory = create_session_factory(tmp_path / "close-confirmed.db")
    batch_id, component_id = _prepare_composite_close_component(session_factory)
    client = _CompositeCloseClient()

    result = _execute_composite_close(
        session_factory, batch_id, component_id, client
    )

    assert result.status == "confirmed"
    assert [call["sz"] for call in client.close_calls] == ["5"]
    assert client.close_calls[0]["closePosId"] == "pos-composite"


def test_composite_close_unknown_never_retries_on_restart(tmp_path):
    session_factory = create_session_factory(tmp_path / "close-unknown.db")
    batch_id, component_id = _prepare_composite_close_component(session_factory)
    client = _CompositeCloseClient("unknown")

    first = _execute_composite_close(session_factory, batch_id, component_id, client)
    second = _execute_composite_close(session_factory, batch_id, component_id, client)

    assert first.status == second.status == "awaiting_exchange"
    assert len(client.close_calls) == 1


def test_composite_close_http_acceptance_without_target_evidence_stays_awaiting(tmp_path):
    session_factory = create_session_factory(tmp_path / "close-accepted.db")
    batch_id, component_id = _prepare_composite_close_component(session_factory)
    client = _CompositeCloseClient("accepted")

    first = _execute_composite_close(session_factory, batch_id, component_id, client)
    second = _execute_composite_close(session_factory, batch_id, component_id, client)

    assert first.status == second.status == "awaiting_exchange"
    assert len(client.close_calls) == 1


def test_composite_close_definite_rejection_retries_from_fresh_unchanged_position(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "close-rejected.db")
    batch_id, component_id = _prepare_composite_close_component(session_factory)
    client = _CompositeCloseClient("rejected")

    first = _execute_composite_close(session_factory, batch_id, component_id, client)
    client.close_outcome = "confirmed"
    second = _execute_composite_close(session_factory, batch_id, component_id, client)

    assert first.status == "recovery_required"
    assert second.status == "confirmed"
    assert [call["sz"] for call in client.close_calls] == ["5", "5"]
    assert client.close_calls[0]["clOrdId"] != client.close_calls[1]["clOrdId"]


@pytest.mark.parametrize(
    ("current_size", "reason"),
    [
        ("11", "position_size_increased_after_snapshot"),
        ("4", "position_below_target_remaining"),
    ],
)
def test_composite_close_refuses_position_drift_without_write(
    tmp_path, current_size, reason
):
    session_factory = create_session_factory(tmp_path / f"close-{current_size}.db")
    batch_id, component_id = _prepare_composite_close_component(session_factory)
    client = _CompositeCloseClient(current_size=current_size)

    result = _execute_composite_close(
        session_factory, batch_id, component_id, client
    )

    assert result.status == "operator_required"
    assert result.reason_code == reason
    assert client.close_calls == []


def _prepare_composite_protection_component(session_factory, *, retained_size="5"):
    from telegram_kol_research.models import StrategyManagementComponent

    batch_id, _ = _persist_composite_consumption_component(session_factory)
    with session_factory() as session:
        components = (
            session.query(StrategyManagementComponent)
            .filter(StrategyManagementComponent.management_batch_id == batch_id)
            .order_by(StrategyManagementComponent.sequence.asc())
            .all()
        )
        for component in components[:2]:
            component.status = "confirmed"
            component.completed_at = NOW
        existing_tp = session.query(PositionProtectionLedger).filter_by(
            order_id="tp-first"
        ).one()
        existing_tp.status = "cancelled"
        owner = {
            "venue": "deepcoin",
            "execution_binding_id": existing_tp.execution_binding_id,
            "execution_order_leg_id": existing_tp.execution_order_leg_id,
            "strategy_instance_id": existing_tp.strategy_instance_id,
            "pos_id": existing_tp.pos_id,
            "instrument_id": existing_tp.instrument_id,
            "side": existing_tp.side,
            "evidence_source": "test",
            "evidence_json": "{}",
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            "created_at": NOW,
            "updated_at": NOW,
        }
        session.add_all(
            [
                PositionProtectionLedger(
                    **owner,
                    order_id="tp-retained",
                    purpose="take_profit",
                    trigger_price="66000",
                    size_text=retained_size,
                    status="verified",
                ),
                PositionProtectionLedger(
                    **owner,
                    order_id="stop-old-primary",
                    purpose="stop_loss",
                    trigger_price="62000",
                    size_text="5",
                    status="verified",
                ),
                PositionProtectionLedger(
                    **owner,
                    order_id="stop-old-backup",
                    purpose="backup_stop",
                    trigger_price="61800",
                    size_text="5",
                    status="verified",
                ),
            ]
        )
        session.commit()
        return batch_id, components[2].id


class _CompositeProtectionClient(_CompositeCloseClient):
    def __init__(self, *, duplicate_ids=False, readback_failure=False, cancel_rejected=False):
        super().__init__(current_size="5")
        self.duplicate_ids = duplicate_ids
        self.readback_failure = readback_failure
        self.cancel_rejected = cancel_rejected
        self.events = []
        self.pending = [
            {
                "ordId": "tp-retained", "posId": "pos-composite",
                "instId": "BTC-USDT-SWAP", "posSide": "long",
                "triggerOrderType": "TPSL", "tpTriggerPx": "66000", "sz": "5",
            },
            {
                "ordId": "stop-old-primary", "posId": "pos-composite",
                "instId": "BTC-USDT-SWAP", "posSide": "long",
                "triggerOrderType": "TPSL", "slTriggerPx": "62000", "sz": "5",
            },
            {
                "ordId": "stop-old-backup", "posId": "pos-composite",
                "instId": "BTC-USDT-SWAP", "posSide": "long",
                "triggerOrderType": "TPSL", "slTriggerPx": "61800", "sz": "5",
            },
        ]
        self._set_count = 0

    def list_positions(self, *, inst_id=None):
        rows = super().list_positions(inst_id=inst_id)
        rows[0]["markPx"] = "65000"
        return rows

    def set_position_sltp(self, payload):
        self._set_count += 1
        role = "primary" if self._set_count == 1 else "backup"
        self.events.append(f"set_{role}")
        order_id = "stop-new" if self.duplicate_ids else f"stop-new-{role}"
        if not self.readback_failure:
            self.pending.append(
                {
                    "ordId": order_id, "posId": "pos-composite",
                    "instId": "BTC-USDT-SWAP", "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "slTriggerPx": payload["slTriggerPx"], "sz": payload["sz"],
                }
            )
        return {"code": "0", "data": {"ordId": order_id}}

    def list_trigger_orders_pending(self, *, inst_id):
        self.events.append("readback")
        return list(self.pending)

    def cancel_position_sltp(self, payload):
        order_id = payload["ordId"]
        self.events.append(f"cancel_{order_id}")
        if self.cancel_rejected:
            raise DeepcoinDefiniteRejection("cancel rejected")
        self.pending = [row for row in self.pending if row["ordId"] != order_id]
        return {"code": "0", "data": {"ordId": order_id}}


def _execute_composite_protection(session_factory, batch_id, component_id, client):
    from telegram_kol_research.strategy_management_composite_executor import (
        execute_protection_replacement_component,
    )

    return execute_protection_replacement_component(
        session_factory,
        batch_id=batch_id,
        component_id=component_id,
        deepcoin_client=client,
        live_execution_gate=lambda: True,
        now_provider=lambda: NOW,
        price_tick="0.1",
        backup_buffer_bps="20",
    )


def test_composite_protection_creates_and_owns_both_stops_before_cancelling_old(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "protection-order.db")
    batch_id, component_id = _prepare_composite_protection_component(session_factory)
    client = _CompositeProtectionClient()

    result = _execute_composite_protection(
        session_factory, batch_id, component_id, client
    )

    assert result.status == "confirmed"
    assert client.events[:6] == [
        "set_primary", "readback", "set_backup", "readback",
        "cancel_stop-old-backup", "readback",
    ] or client.events[:6] == [
        "set_primary", "readback", "set_backup", "readback",
        "cancel_stop-old-primary", "readback",
    ]
    first_cancel = next(i for i, event in enumerate(client.events) if event.startswith("cancel_"))
    assert client.events.index("set_backup") < first_cancel
    with session_factory() as session:
        new_rows = session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.order_id.in_(
                ("stop-new-primary", "stop-new-backup")
            )
        ).all()
        assert {row.purpose for row in new_rows} == {"stop_loss", "backup_stop"}
        assert all(row.status == "verified" for row in new_rows)


def test_composite_protection_readback_failure_retains_old_stops(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-readback.db")
    batch_id, component_id = _prepare_composite_protection_component(session_factory)
    client = _CompositeProtectionClient(readback_failure=True)

    result = _execute_composite_protection(
        session_factory, batch_id, component_id, client
    )

    assert result.status == "awaiting_exchange"
    assert not any(event.startswith("cancel_") for event in client.events)


def test_composite_protection_duplicate_new_order_id_retains_old_stops(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-duplicate.db")
    batch_id, component_id = _prepare_composite_protection_component(session_factory)
    client = _CompositeProtectionClient(duplicate_ids=True)

    result = _execute_composite_protection(
        session_factory, batch_id, component_id, client
    )

    assert result.status == "operator_required"
    assert result.reason_code == "duplicate_new_stop_order_id"
    assert not any(event.startswith("cancel_") for event in client.events)


def test_composite_protection_old_cancel_failure_keeps_new_verified_stops(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-cancel.db")
    batch_id, component_id = _prepare_composite_protection_component(session_factory)
    client = _CompositeProtectionClient(cancel_rejected=True)

    result = _execute_composite_protection(
        session_factory, batch_id, component_id, client
    )

    assert result.status == "recovery_required"
    with session_factory() as session:
        new_rows = session.query(PositionProtectionLedger).filter(
            PositionProtectionLedger.order_id.in_(
                ("stop-new-primary", "stop-new-backup")
            )
        ).all()
        assert len(new_rows) == 2
        assert all(row.status == "verified" for row in new_rows)


def test_composite_protection_refuses_oversized_retained_tp_before_any_write(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-oversized.db")
    batch_id, component_id = _prepare_composite_protection_component(
        session_factory, retained_size="6"
    )
    client = _CompositeProtectionClient()

    result = _execute_composite_protection(
        session_factory, batch_id, component_id, client
    )

    assert result.status == "operator_required"
    assert result.reason_code == "retained_take_profit_exceeds_position"
    assert client.events == []
