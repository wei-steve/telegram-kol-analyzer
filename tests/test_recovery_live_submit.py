import json
from datetime import UTC, datetime
from threading import Event, Thread

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.models import (
    EntryStrategyAssembly,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    RawMessage,
    SignalCandidate,
)
from telegram_kol_research.models import SourceMessageDeletionExit, StrategyLifecycle, TradeSignal, TriggerTakeProfitConvergence
from telegram_kol_research.models import TriggerProtectionIntent
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_live_submit import RecoveryLiveSubmitError
from telegram_kol_research.recovery_live_submit import build_deepcoin_market_order_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_place_order_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_position_sltp_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_position_sltp_payloads
from telegram_kol_research.recovery_live_submit import build_deepcoin_trigger_order_payload
from telegram_kol_research.recovery_live_submit import enqueue_recovery_trade_signal
from telegram_kol_research.recovery_live_submit import process_next_trade_signal_live
from telegram_kol_research.recovery_live_submit import process_trade_signal_live
from telegram_kol_research.recovery_live_submit import submit_recovery_order_live
from telegram_kol_research.recovery_live_submit import _load_matching_position_ids
from telegram_kol_research.deepcoin_execution_actions import _exact_exchange_order_id
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trade_signals import canonical_management_batch_id
from telegram_kol_research.source_message_deletion import record_source_message_deleted


class _StaticContractSpecProvider:
    def get_contract_spec(self, instrument_id):
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


def test_pending_entry_update_requires_an_exact_exchange_order_id():
    assert _exact_exchange_order_id({"ordId": "trigger-123", "clOrdId": "client-123"}) == "trigger-123"
    assert _exact_exchange_order_id({"clOrdId": "client-123"}) is None
    assert _exact_exchange_order_id({"id": "internal-123"}) is None


@pytest.mark.parametrize("payload", [None, [], "scalar", 42, 1.5, True])
def test_canonical_management_batch_id_rejects_non_mapping_payload(payload):
    assert canonical_management_batch_id(payload) is None


class _FakeDeepcoinClient:
    def __init__(self):
        self.payloads = []
        self.trigger_payloads = []
        self.protection_payloads = []
        self.position_protection_payloads = []
        self.cancel_payloads = []
        self.positions = []
        self.pending_tpsl = []

    def place_order(self, order_payload):
        self.payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": f"order-{len(self.payloads)}"}}

    def trigger_order(self, order_payload):
        self.trigger_payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": f"trigger-{len(self.trigger_payloads)}"}}

    def set_position_sltp(self, protection_payload):
        self.position_protection_payloads.append(protection_payload)
        self.pending_tpsl.append(
            {
                "ordId": "sltp-1",
                "instId": protection_payload["instId"],
                "posId": protection_payload["posId"],
                "posSide": protection_payload["posSide"],
                **(
                    {"slTriggerPx": protection_payload["slTriggerPx"]}
                    if protection_payload.get("slTriggerPx") not in (None, "")
                    else {"tpTriggerPx": protection_payload["tpTriggerPx"]}
                ),
                "sz": protection_payload.get("sz", "0"),
            }
        )
        return {"code": "0", "data": {"ordId": "sltp-1"}}

    def replace_order_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        return {"code": "0", "data": {"orderSysID": protection_payload["orderSysID"]}}

    def cancel_order(self, cancel_payload):
        self.cancel_payloads.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def list_positions(self, *, inst_id=None):
        return [
            {
                "avgPx": "64000",
                "mgnMode": "cross",
                "mrgPosition": "split",
                **row,
            }
            for row in self.positions
        ]

    def list_trigger_orders_pending(self, *, inst_id):
        return [
            row for row in self.pending_tpsl
            if row["instId"] == inst_id
        ]

    def get_ticker_price(self, *, inst_id):
        return 68100.0


class _ProtectionFailingDeepcoinClient(_FakeDeepcoinClient):
    def __init__(self):
        super().__init__()
        self.positions = [
            {
                "posId": "pos-market-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "9",
            }
        ]

    def place_order(self, order_payload):
        self.payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": "order-market-1", "posId": "pos-market-1"}}

    def set_position_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        raise RuntimeError("missing_take_profit_for_protection")


class _InsufficientMoneyDeepcoinClient(_FakeDeepcoinClient):
    def place_order(self, order_payload):
        self.payloads.append(order_payload)
        raise DeepcoinClientError("Deepcoin API error 36: InsufficientMoney")


def test_market_entry_fails_before_submit_when_position_baseline_is_unavailable():
    class _UnavailableBaselineClient(_FakeDeepcoinClient):
        def list_positions(self, *, inst_id=None):
            raise TimeoutError("positions unavailable")

    client = _UnavailableBaselineClient()
    with pytest.raises(
        RecoveryLiveSubmitError,
        match="pre_submit_position_snapshot_unavailable",
    ):
        _load_matching_position_ids(
            client,
            draft={
                "instrument_id": "BTC-USDT-SWAP",
                "margin_mode": "cross",
                "position_mode": "split",
            },
            side="short",
        )
    assert client.payloads == []


class _OrderProtectionFailingDeepcoinClient(_FakeDeepcoinClient):
    def __init__(self):
        super().__init__()
        self.positions = [
            {
                "posId": "unrelated-pos",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "7",
                "avgPx": "64000",
                "mrgPosition": "split",
                "mgnMode": "cross",
            }
        ]

    def replace_order_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        raise DeepcoinClientError("order_not_open")


class _DelayedFilledPositionDeepcoinClient(_OrderProtectionFailingDeepcoinClient):
    def __init__(self):
        super().__init__()
        self.position_calls = 0

    def list_positions(self, *, inst_id=None):
        self.position_calls += 1
        if self.position_calls == 1:
            return self.positions
        return [
            *self.positions,
            {
                "posId": "pos-filled-1",
                "instId": "BTC-USDT-SWAP",
                "posSide": "short",
                "pos": "7",
                "avgPx": "64000",
                "mrgPosition": "split",
                "mgnMode": "cross",
            },
        ]


def _persist_ready_item(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=55,
            sender_name="alice",
            posted_at=datetime(2026, 6, 12, 8, 0),
            text="BTC long 68000-68200 SL 67500 TP 69000 / 70000",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                entry_text="68000-68200",
                stop_loss_text="67500",
                take_profit_text="69000 / 70000",
                parse_source="text",
                confidence=0.9,
            )
        )
        session.commit()
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="alice",
                    chat_id=100,
                    message_id=55,
                    posted_at=datetime(2026, 6, 12, 8, 0),
                    symbol="BTC",
                    side="long",
                    entry_range=(68000.0, 68200.0),
                    stop_loss_text="67500",
                    take_profit_text="69000 / 70000",
                    trading_mode="auto_trade",
                    max_loss_usdt=100.0,
                ),
                decision=RecoveryDecision(
                    action="eligible_for_recovery_limit_order",
                    reason_codes=["recovery_checks_passed"],
                    entry_range=(68000.0, 68200.0),
                    max_loss_usdt=100.0,
                ),
            )
        ],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )
    apply_recovery_review_decision(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0, tzinfo=UTC),
    )
    confirm_recovery_order_dry_run(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
        persist_ready_confirmation=True,
        confirmed_at=datetime(2026, 6, 12, 20, 0, tzinfo=UTC),
    )


def _persist_lifecycle(
    session_factory,
    *,
    chat_id=100,
    message_id=55,
    symbol="BTC",
    side="long",
):
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=chat_id,
                message_id=message_id,
                symbol=symbol,
                side=side,
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 12, 8, 0),
                entry_range_low=68000.0,
                entry_range_high=68200.0,
                stop_loss=67500.0,
                take_profit="69000 / 70000",
            )
        )
        session.commit()


def _persist_ready_market_item(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=200,
            message_id=66,
            sender_name="bob",
            posted_at=datetime(2026, 6, 30, 8, 0),
            text="BTC 现价做空 59800 止损 61800 止盈 59000",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="short",
                event_type="entry_signal",
                entry_text="现价 59800",
                stop_loss_text="61800",
                take_profit_text="59000",
                parse_source="text_ai",
                confidence=0.95,
            )
        )
        session.commit()
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="bob",
                    chat_id=200,
                    message_id=66,
                    posted_at=datetime(2026, 6, 30, 8, 0),
                    symbol="BTC",
                    side="short",
                    entry_range=(59800.0, 59800.0),
                    stop_loss_text="61800",
                    take_profit_text="59000",
                    trading_mode="auto_trade",
                    max_loss_usdt=20.0,
                ),
                decision=RecoveryDecision(
                    action="eligible_for_recovery_limit_order",
                    reason_codes=["live_signal_auto_trade_market"],
                    entry_range=(59800.0, 59800.0),
                    max_loss_usdt=20.0,
                ),
            )
        ],
        run_at=datetime(2026, 6, 30, 8, 0, tzinfo=UTC),
    )
    apply_recovery_review_decision(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 30, 8, 1, tzinfo=UTC),
    )
    confirm_recovery_order_dry_run(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        contract_spec_provider=_StaticContractSpecProvider(),
        persist_ready_confirmation=True,
        confirmed_at=datetime(2026, 6, 30, 8, 2, tzinfo=UTC),
    )


def _replace_queued_order_legs(session_factory, signal_id, order_legs):
    with session_factory() as session:
        signal = session.get(TradeSignal, signal_id)
        payload = json.loads(signal.payload_json)
        payload["deepcoin_order_draft"]["order_legs"] = order_legs
        signal.payload_json = json.dumps(payload)
        session.commit()
    return payload["deepcoin_order_draft"]


def test_build_deepcoin_place_order_payload_maps_limit_leg():
    payload = build_deepcoin_place_order_payload(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "margin_mode": "cross",
        },
        {
            "side": "buy",
            "position_side": "long",
            "price": 68100.0,
            "quantity": 83.0,
            "client_order_id": "client-1",
        },
    )

    assert payload == {
        "instId": "BTC-USDT-SWAP",
        "tdMode": "cross",
        "side": "buy",
        "posSide": "long",
        "ordType": "limit",
        "px": "68100.0",
        "sz": "83.0",
        "clOrdId": "client-1",
        "mrgPosition": "split",
    }


def test_build_deepcoin_position_sltp_payload_allows_stop_loss_without_take_profit():
    payload = build_deepcoin_position_sltp_payload(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "margin_mode": "cross",
            "position_mode": "split",
            "stop_loss": 61800.0,
            "take_profit_legs": [],
            "order_legs": [{"position_side": "short"}],
        },
        pos_id="pos-btc-short",
    )

    assert payload == {
        "instType": "SWAP",
        "instId": "BTC-USDT-SWAP",
        "posSide": "short",
        "mrgPosition": "split",
        "tdMode": "cross",
        "slTriggerPx": "61800.0",
        "slTriggerPxType": "last",
        "slOrdPx": "-1",
        "posId": "pos-btc-short",
    }


def test_build_deepcoin_trigger_order_payload_is_stop_only_for_staged_take_profit():
    payload = build_deepcoin_trigger_order_payload(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "margin_mode": "cross",
            "position_mode": "split",
            "stop_loss": 67500.0,
            "take_profit_legs": [
                {"price": 69000.0, "allocation_pct": 50.0},
                {"price": 70000.0, "allocation_pct": 50.0},
            ],
            "order_legs": [{"position_side": "long"}],
        },
        {
            "side": "buy",
            "position_side": "long",
            "price": 68100.0,
            "quantity": 83.0,
            "client_order_id": "TKFG8248E1",
        },
    )

    assert payload["orderType"] == "limit"
    assert payload["triggerPrice"] == "68100.0"
    assert not any(key.startswith("tp") for key in payload)
    assert payload["slTriggerPx"] == "67500.0"
    assert payload["mrgPosition"] == "split"
    assert payload["clOrdId"] == "TKFG8248E1"


def test_build_deepcoin_market_order_and_position_sltp_payloads():
    draft = {
        "instrument_id": "BTC-USDT-SWAP",
        "margin_mode": "cross",
        "position_mode": "split",
        "stop_loss": 67500.0,
        "take_profit_legs": [{"price": 69000.0, "allocation_pct": 100.0}],
        "order_legs": [{"position_side": "long"}],
    }

    order_payload = build_deepcoin_market_order_payload(
        draft,
        {
            "side": "buy",
            "position_side": "long",
            "quantity": 83.0,
            "client_order_id": "client-1",
        },
    )
    protection_payload = build_deepcoin_position_sltp_payload(draft, pos_id="pos-1")

    assert order_payload["ordType"] == "market"
    assert "px" not in order_payload
    assert protection_payload["posId"] == "pos-1"
    assert protection_payload["tpOrdPx"] == "-1"
    assert protection_payload["slOrdPx"] == "-1"


def test_build_deepcoin_position_sltp_payloads_split_multi_take_profit_by_size():
    payloads = build_deepcoin_position_sltp_payloads(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "margin_mode": "cross",
            "position_mode": "split",
            "stop_loss": 67500.0,
            "take_profit_legs": [
                {"price": 69000.0, "allocation_pct": 40.0},
                {"price": 70000.0, "allocation_pct": 30.0},
                {"price": 71000.0, "allocation_pct": 30.0},
            ],
            "order_legs": [{"position_side": "long"}],
            "contract_spec": {
                "instrument_id": "BTC-USDT-SWAP",
                "contract_value": 0.001,
                "quantity_step": 1.0,
                "min_quantity": 1.0,
                "price_tick": 0.1,
            },
        },
        pos_id="pos-1",
        position_size=83.0,
    )

    assert payloads == [
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "tdMode": "cross",
            "slTriggerPx": "67500.0",
            "slTriggerPxType": "last",
            "slOrdPx": "-1",
            "posId": "pos-1",
        },
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "tdMode": "cross",
            "tpTriggerPx": "69000.0",
            "tpTriggerPxType": "last",
            "tpOrdPx": "-1",
            "sz": "33",
            "posId": "pos-1",
        },
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "tdMode": "cross",
            "tpTriggerPx": "70000.0",
            "tpTriggerPxType": "last",
            "tpOrdPx": "-1",
            "sz": "24",
            "posId": "pos-1",
        },
        {
            "instType": "SWAP",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mrgPosition": "split",
            "tdMode": "cross",
            "tpTriggerPx": "71000.0",
            "tpTriggerPxType": "last",
            "tpOrdPx": "-1",
            "sz": "26",
            "posId": "pos-1",
        },
    ]


def test_position_sltp_payloads_support_four_stage_btc_take_profits():
    payloads = build_deepcoin_position_sltp_payloads(
        {
            "instrument_id": "BTC-USDT-SWAP", "margin_mode": "cross",
            "position_mode": "split", "stop_loss": 61800.0,
            "take_profit_legs": [
                {"price": 67100.0, "allocation_pct": 40.0},
                {"price": 68500.0, "allocation_pct": 20.0},
                {"price": 70300.0, "allocation_pct": 20.0},
                {"price": 72000.0, "allocation_pct": 20.0},
            ],
            "order_legs": [{"position_side": "long"}],
            "contract_spec": {"quantity_step": 1.0, "min_quantity": 1.0},
        },
        pos_id="pos-4", position_size=25.0,
    )

    assert [payload["sz"] for payload in payloads[1:]] == ["10", "5", "5", "5"]
    assert [payload["tpTriggerPx"] for payload in payloads[1:]] == [
        "67100.0", "68500.0", "70300.0", "72000.0",
    ]


def test_position_sltp_payloads_reject_undersized_five_stage_position():
    with pytest.raises(RecoveryLiveSubmitError, match="minimum"):
        build_deepcoin_position_sltp_payloads(
            {
                "instrument_id": "ETH-USDT-SWAP", "margin_mode": "cross",
                "position_mode": "split", "stop_loss": 1800.0,
                "take_profit_legs": [
                    {"price": 1900.0, "allocation_pct": 40.0},
                    {"price": 1920.0, "allocation_pct": 15.0},
                    {"price": 1940.0, "allocation_pct": 15.0},
                    {"price": 1960.0, "allocation_pct": 15.0},
                    {"price": 1980.0, "allocation_pct": 15.0},
                ],
                "order_legs": [{"position_side": "long"}],
                "contract_spec": {"quantity_step": 0.1, "min_quantity": 0.1},
            },
            pos_id="pos-too-small", position_size=0.3,
        )


def test_submit_recovery_order_live_blocks_when_auto_trade_is_disabled(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)

    try:
        submit_recovery_order_live(
            session_factory,
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            deepcoin_client=_FakeDeepcoinClient(),
            contract_spec_provider=_StaticContractSpecProvider(),
        )
    except RecoveryLiveSubmitError as exc:
        assert str(exc) == "auto_trade_disabled"
    else:
        raise AssertionError("expected disabled auto-trade to block live submit")


def test_submit_recovery_order_live_places_orders_and_persists_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    _persist_lifecycle(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _FakeDeepcoinClient()

    result = submit_recovery_order_live(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=datetime(2026, 6, 12, 21, 0, tzinfo=UTC),
    )

    assert result["submitted"] is True
    assert result["order_count"] == 2
    assert fake_client.payloads == []
    assert fake_client.protection_payloads == []
    assert fake_client.trigger_payloads[0]["tdMode"] == "cross"
    assert fake_client.trigger_payloads[0]["mrgPosition"] == "split"
    assert fake_client.trigger_payloads[0]["orderType"] == "limit"
    assert [payload["triggerPrice"] for payload in fake_client.trigger_payloads] == [
        "68290.0",
        "68090.0",
    ]
    assert all(not any(key.startswith("tp") for key in payload) for payload in fake_client.trigger_payloads)
    assert fake_client.trigger_payloads[0]["slTriggerPx"] == "67500.0"
    assert fake_client.trigger_payloads[0]["slTriggerPxType"] == "last"
    assert fake_client.trigger_payloads[0]["slOrdPx"] == "-1"
    assert "posId" not in fake_client.trigger_payloads[0]
    assert fake_client.position_protection_payloads == []
    assert result["warnings"] == []
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        events = session.query(ExecutionEvent).order_by(ExecutionEvent.id.asc()).all()
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.leg_index.asc()).all()
        lifecycle = session.query(StrategyLifecycle).one()
    assert binding.status == "open"
    assert binding.order_id == "trigger-1,trigger-2"
    assert binding.client_order_id == "TK649760E806ACF61,TK729D11F4739D2A2"
    assert binding.strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert lifecycle.execution_binding_id == binding.id
    assert [event.action for event in events] == [
        "create_trigger_entry",
        "create_trigger_entry",
    ]
    assert events[0].execution_binding_id == binding.id
    assert events[0].trade_signal_id == result["signal_id"]
    assert events[0].order_id == "trigger-1"
    assert [(leg.leg_index, leg.order_id, leg.client_order_id, leg.status) for leg in legs] == [
        (1, "trigger-1", "TK649760E806ACF61", "open"),
        (2, "trigger-2", "TK729D11F4739D2A2", "open"),
    ]
    assert {leg.execution_binding_id for leg in legs} == {binding.id}
    assert {leg.strategy_instance_id for leg in legs} == {"deepcoin:100:55:BTC:long"}


def test_entry_submit_rechecks_source_after_planning_before_exchange_write(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "delete-race.db")
    _persist_ready_item(session_factory)
    _persist_lifecycle(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _FakeDeepcoinClient()
    original_builder = build_deepcoin_trigger_order_payload
    deleted = False

    def delete_after_planning(draft, leg):
        nonlocal deleted
        payload = original_builder(draft, leg)
        if not deleted:
            deleted = True
            record_source_message_deleted(
                session_factory,
                chat_id=100,
                message_id=55,
            )
        return payload

    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit.build_deepcoin_trigger_order_payload",
        delete_after_planning,
    )

    with pytest.raises(RecoveryLiveSubmitError, match="source_message_deleted"):
        submit_recovery_order_live(
            session_factory,
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            deepcoin_client=fake_client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert fake_client.trigger_payloads == []
    assert fake_client.payloads == []


def test_source_deletion_waits_until_exchange_identity_is_durably_ledgered(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "ledger-window.db")
    _persist_ready_item(session_factory)
    _persist_lifecycle(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    deletion_started = Event()
    deletion_finished = Event()
    deletion_thread = None

    class Client(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            nonlocal deletion_thread
            self.trigger_payloads.append(order_payload)
            if deletion_thread is None:
                def delete_source():
                    deletion_started.set()
                    record_source_message_deleted(
                        session_factory,
                        chat_id=100,
                        message_id=55,
                    )
                    deletion_finished.set()

                deletion_thread = Thread(target=delete_source)
                deletion_thread.start()
                assert deletion_started.wait(timeout=1)
            return {
                "code": "0",
                "data": {"ordId": f"trigger-{len(self.trigger_payloads)}"},
            }

    original_normalize = __import__(
        "telegram_kol_research.recovery_live_submit",
        fromlist=["_normalized_trigger_order_id"],
    )._normalized_trigger_order_id

    def assert_deletion_is_still_serialized(response):
        assert not deletion_finished.wait(timeout=0.05)
        return original_normalize(response)

    monkeypatch.setattr(
        "telegram_kol_research.recovery_live_submit._normalized_trigger_order_id",
        assert_deletion_is_still_serialized,
    )

    result = submit_recovery_order_live(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        deepcoin_client=Client(),
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    deletion_thread.join(timeout=1)

    assert result["submitted"] is True
    assert deletion_finished.is_set()
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        deletion_exit = session.query(SourceMessageDeletionExit).one()
        assert deletion_exit.execution_binding_id == binding.id
        assert deletion_exit.strategy_instance_id == binding.strategy_instance_id


def test_process_next_trade_signal_live_consumes_pending_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    fake_client = _FakeDeepcoinClient()

    result = process_next_trade_signal_live(
        session_factory,
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["signal_id"] == signal.id
    assert result["order_count"] == 2
    with session_factory() as session:
        assert session.query(ExecutionBinding).count() == 1
        assert session.query(TradeSignal).filter_by(id=signal.id).one().status == "submitted"


@pytest.mark.parametrize("assembly_is_finalized", [True, False])
def test_process_next_rejects_unsynchronized_or_unfinalized_v2_entry_signal(
    tmp_path,
    assembly_is_finalized,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    assembly_fingerprint = ("b" if assembly_is_finalized else "a") * 64
    assembly_evidence = (
        {
            "order_draft_snapshot": {"order_legs": [{"price": 68000}]},
            "final_entry_leg_count": 1,
        }
        if assembly_is_finalized
        else {}
    )
    with session_factory() as session:
        raw = session.query(RawMessage).filter_by(chat_id=100, message_id=55).one()
        candidate = session.query(SignalCandidate).filter_by(
            raw_message_id=raw.id
        ).one()
        assembly = EntryStrategyAssembly(
            strategy_raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            strategy_instance_id=str(signal.strategy_instance_id),
            risk_multiplier="1",
            evidence_json=json.dumps(assembly_evidence, sort_keys=True),
            fingerprint=assembly_fingerprint,
        )
        session.add(assembly)
        session.flush()
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        stale_evidence = {
            "assembly_id": assembly.id,
            "strategy_instance_id": signal.strategy_instance_id,
            "assembly_fingerprint": "a" * 64,
        }
        payload["deepcoin_order_draft"][
            "entry_preamble_assembly"
        ] = stale_evidence
        if not assembly_is_finalized:
            payload["entry_preamble_assembly"] = dict(stale_evidence)
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()
    client = _FakeDeepcoinClient()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^entry_assembly_signal_not_synchronized$",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.payloads == []
    assert client.trigger_payloads == []
    with session_factory() as session:
        assert session.get(TradeSignal, signal.id).status == "failed"
        assert session.query(ExecutionBinding).count() == 0
        assert session.query(TriggerProtectionIntent).count() == 0


def test_process_next_reloads_v2_assembly_after_loading_pending_signal(
    tmp_path,
    monkeypatch,
):
    import telegram_kol_research.recovery_live_submit as live_submit_module

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    with session_factory() as session:
        raw = session.query(RawMessage).filter_by(chat_id=100, message_id=55).one()
        candidate = session.query(SignalCandidate).filter_by(
            raw_message_id=raw.id
        ).one()
        assembly = EntryStrategyAssembly(
            strategy_raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            strategy_instance_id=str(signal.strategy_instance_id),
            risk_multiplier="1",
            evidence_json=json.dumps(
                {
                    "order_draft_snapshot": {
                        "order_legs": [{"price": 68000}]
                    },
                    "final_entry_leg_count": 1,
                },
                sort_keys=True,
            ),
            fingerprint="a" * 64,
        )
        session.add(assembly)
        session.flush()
        assembly_id = assembly.id
        row = session.get(TradeSignal, signal.id)
        payload = json.loads(row.payload_json)
        evidence = {
            "assembly_id": assembly_id,
            "strategy_instance_id": signal.strategy_instance_id,
            "assembly_fingerprint": "a" * 64,
        }
        payload["entry_preamble_assembly"] = dict(evidence)
        payload["deepcoin_order_draft"][
            "entry_preamble_assembly"
        ] = dict(evidence)
        row.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        session.commit()

    real_load = live_submit_module.load_trade_signal

    def load_then_finalize_concurrently(factory, signal_id):
        loaded = real_load(factory, signal_id)
        with factory() as session:
            assembly = session.get(EntryStrategyAssembly, assembly_id)
            assembly.fingerprint = "b" * 64
            session.commit()
        return loaded

    monkeypatch.setattr(
        live_submit_module,
        "load_trade_signal",
        load_then_finalize_concurrently,
    )
    client = _FakeDeepcoinClient()

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^entry_assembly_signal_not_synchronized$",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert client.payloads == []
    assert client.trigger_payloads == []


def test_legacy_management_audit_is_bounded_redacted_and_read_only(tmp_path):
    from telegram_kol_research.trade_signals import audit_pending_legacy_management_signals

    session_factory = create_session_factory(tmp_path / "research.db")
    legacy = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=700,
        symbol="BTC",
        side="short",
        action="adjust_stop_loss",
        payload={"binding_id": 12, "api_secret": "must-not-leak", "stop_loss": 62000},
    )
    enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:200",
        chat_id=200,
        message_id=701,
        symbol="ETH",
        side="long",
        action="close_position",
        payload={"binding_id": 13, "management_batch_id": 55},
    )
    enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="group:100",
        chat_id=100,
        message_id=702,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={"api_secret": "entry-secret"},
    )
    for offset, invalid_reference in enumerate([" 1", "01", True, 1.0], start=1):
        enqueue_trade_signal(
            session_factory,
            venue="deepcoin",
            source_type="kol_management",
            kol_id="group:100",
            chat_id=100,
            message_id=710 + offset,
            symbol="BTC",
            side="short",
            action="close_position",
            payload={"binding_id": 12, "management_batch_id": invalid_reference},
        )
    enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=720,
        symbol="BTC",
        side="short",
        action="close_position",
        payload={"binding_id": 12, "batch_id": 1},
    )

    before = [(row.id, row.status, row.payload_json) for row in _all_trade_signals(session_factory)]
    report = audit_pending_legacy_management_signals(session_factory, limit=10)
    after = [(row.id, row.status, row.payload_json) for row in _all_trade_signals(session_factory)]

    assert report == {
        "total": 6,
        "returned": 6,
        "truncated": False,
        "scan_truncated": False,
        "by_action": {"adjust_stop_loss": 1, "close_position": 5},
        "by_status": {"pending": 6},
        "items": [
            {
                "id": legacy.id,
                "action": "adjust_stop_loss",
                "status": "pending",
                "source_type": "kol_management",
                "chat_id": 100,
                "message_id": 700,
            },
            *[
                {
                    "id": legacy_id,
                    "action": "close_position",
                    "status": "pending",
                    "source_type": "kol_management",
                    "chat_id": 100,
                    "message_id": message_id,
                }
                for legacy_id, message_id in _legacy_signal_ids_and_messages(
                    session_factory
                )
            ],
        ],
    }
    assert "secret" not in json.dumps(report).lower()
    assert before == after


def test_recovery_dispatch_rejects_automatic_legacy_management_before_generic_action(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=703,
        symbol="BTC",
        side="short",
        action="close_position",
        payload={"binding_id": 12},
    )
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    import telegram_kol_research.recovery_live_submit as live_submit

    monkeypatch.setattr(
        live_submit,
        "execute_deepcoin_management_signal",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("legacy management must not reach generic dispatcher")
        ),
    )

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="legacy_management_signal_requires_batch",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=signal.id,
            deepcoin_client=_FakeDeepcoinClient(),
        )

    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row.status == "failed"
        assert row.attempts == 1


def test_process_next_rejects_legacy_management_before_client_factory(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    signal = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=704,
        symbol="BTC",
        side="short",
        action="close_position",
        payload={"binding_id": 12},
    )
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    factory_calls = []

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="legacy_management_signal_requires_batch",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client_factory=lambda: factory_calls.append("called")
            or (_ for _ in ()).throw(AssertionError("factory must not run")),
        )

    assert factory_calls == []
    with session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row.status == "failed"
        assert row.attempts == 1


def test_process_next_entry_creates_deferred_client(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    client = _FakeDeepcoinClient()
    factory_calls = []

    result = process_next_trade_signal_live(
        session_factory,
        deepcoin_client_factory=lambda: factory_calls.append("called") or client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert factory_calls == ["called"]
    assert result["submitted"] is True


def test_non_mapping_legacy_payload_fails_without_factory_then_queue_continues(
    tmp_path
):
    session_factory = create_session_factory(tmp_path / "research.db")
    legacy = enqueue_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=705,
        symbol="BTC",
        side="short",
        action="close_position",
        payload={"binding_id": 12},
    )
    with session_factory() as session:
        row = session.get(TradeSignal, legacy.id)
        row.payload_json = "[]"
        session.commit()
    _persist_ready_item(session_factory)
    entry = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    client = _FakeDeepcoinClient()
    factory_calls = []

    with pytest.raises(
        RecoveryLiveSubmitError,
        match="legacy_management_signal_requires_batch",
    ):
        process_next_trade_signal_live(
            session_factory,
            deepcoin_client_factory=lambda: factory_calls.append("called") or client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert factory_calls == []
    with session_factory() as session:
        legacy_row = session.get(TradeSignal, legacy.id)
        assert legacy_row.status == "failed"
        assert legacy_row.attempts == 1
        assert session.get(TradeSignal, entry.id).status == "pending"

    result = process_next_trade_signal_live(
        session_factory,
        deepcoin_client_factory=lambda: factory_calls.append("called") or client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["submitted"] is True
    assert factory_calls == ["called"]


def test_audit_lists_non_mapping_pending_management_payloads_read_only(tmp_path):
    from telegram_kol_research.trade_signals import audit_pending_legacy_management_signals

    session_factory = create_session_factory(tmp_path / "research.db")
    payload_json_values = ["[]", '"scalar"', "null", "123"]
    signal_ids = []
    for index, payload_json in enumerate(payload_json_values, start=1):
        signal = enqueue_trade_signal(
            session_factory,
            venue="deepcoin",
            source_type="kol_management",
            kol_id="group:100",
            chat_id=100,
            message_id=730 + index,
            symbol="BTC",
            side="short",
            action="close_position",
            payload={},
        )
        signal_ids.append(signal.id)
        with session_factory() as session:
            row = session.get(TradeSignal, signal.id)
            row.payload_json = payload_json
            session.commit()

    report = audit_pending_legacy_management_signals(session_factory)

    assert report["total"] == 4
    assert [item["id"] for item in report["items"]] == signal_ids
    with session_factory() as session:
        rows = session.query(TradeSignal).order_by(TradeSignal.id).all()
        assert [row.status for row in rows] == ["pending"] * 4
        assert [row.payload_json for row in rows] == payload_json_values


def _all_trade_signals(session_factory):
    with session_factory() as session:
        return session.query(TradeSignal).order_by(TradeSignal.id).all()


def _legacy_signal_ids_and_messages(session_factory):
    with session_factory() as session:
        rows = (
            session.query(TradeSignal)
            .filter(TradeSignal.message_id >= 711)
            .filter(TradeSignal.message_id <= 720)
            .order_by(TradeSignal.id)
            .all()
        )
        return [(row.id, row.message_id) for row in rows]


def test_process_live_coalesces_equivalent_legacy_trigger_legs_before_submission(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    first_leg, second_leg = signal.payload["deepcoin_order_draft"]["order_legs"]
    legacy_legs = [
        dict(first_leg),
        {
            **first_leg,
            "client_order_id": second_leg["client_order_id"],
        },
    ]
    queued_draft = _replace_queued_order_legs(
        session_factory,
        signal.id,
        legacy_legs,
    )
    fake_client = _FakeDeepcoinClient()

    result = process_next_trade_signal_live(
        session_factory,
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["order_count"] == 1
    assert len(fake_client.trigger_payloads) == 1
    assert fake_client.trigger_payloads[0]["sz"] == str(
        first_leg["quantity"] + first_leg["quantity"]
    )
    assert "merged_from_leg_indices" not in fake_client.trigger_payloads[0]
    assert result["deepcoin_order_draft"] == queued_draft
    assert len(result["deepcoin_order_draft"]["order_legs"]) == 2
    with session_factory() as session:
        legs = session.query(ExecutionOrderLeg).all()
    assert len(legs) == 1
    assert json.loads(legs[0].request_json)["merged_from_leg_indices"] == [1, 2]


def test_process_live_preserves_distinct_price_legacy_trigger_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    signal = enqueue_recovery_trade_signal(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )
    first_leg, second_leg = signal.payload["deepcoin_order_draft"]["order_legs"]
    assert first_leg["price"] != second_leg["price"]
    legacy_legs = [
        {
            **first_leg,
            "allocation_pct": 50.0,
            "risk_budget_usdt": 50.0,
            "quantity": 63.0,
            "base_asset_estimate": 0.063,
        },
        {
            **second_leg,
            "allocation_pct": 50.0,
            "risk_budget_usdt": 50.0,
            "quantity": 84.0,
            "base_asset_estimate": 0.084,
        },
    ]
    queued_draft = _replace_queued_order_legs(
        session_factory,
        signal.id,
        legacy_legs,
    )
    fake_client = _FakeDeepcoinClient()

    result = process_next_trade_signal_live(
        session_factory,
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["order_count"] == 2
    assert [payload["triggerPrice"] for payload in fake_client.trigger_payloads] == [
        str(first_leg["price"]),
        str(second_leg["price"]),
    ]
    assert [payload["sz"] for payload in fake_client.trigger_payloads] == [
        "63.0",
        "84.0",
    ]
    assert result["deepcoin_order_draft"] == queued_draft
    with session_factory() as session:
        assert session.query(ExecutionOrderLeg).count() == 2


def test_market_submit_persists_binding_when_position_protection_fails(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _ProtectionFailingDeepcoinClient()

    result = submit_recovery_order_live(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=datetime(2026, 6, 30, 8, 3, tzinfo=UTC),
    )

    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()

    assert result["submitted"] is True
    assert "position_protection_failed_after_entry_submitted" in result["warnings"]
    assert binding.status == "active"
    assert binding.last_exchange_status == "position_active_protection_failed"
    assert binding.order_id == "order-market-1"
    assert binding.pos_id == "pos-market-1"


def test_market_submit_defers_take_profit_until_verified_backup_stop(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(session_factory, chat_id=200, message_id=66, symbol="BTC", side="short")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _FakeDeepcoinClient()
    fake_client.positions = [{
        "posId": "pos-market-1", "instId": "BTC-USDT-SWAP", "posSide": "short", "pos": "9",
    }]
    fake_client.place_order = lambda payload: {"code": "0", "data": {"ordId": "order-market-1", "posId": "pos-market-1"}}

    result = submit_recovery_order_live(
        session_factory, chat_id=200, message_id=66, symbol="BTC", side="short",
        deepcoin_client=fake_client, contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=datetime(2026, 6, 30, 8, 3, tzinfo=UTC),
    )

    assert result["submitted"] is True
    assert len(fake_client.position_protection_payloads) == 1
    payload = fake_client.position_protection_payloads[0]
    assert "slTriggerPx" in payload
    assert "tpTriggerPx" not in payload
    with session_factory() as session:
        convergence = session.query(TriggerTakeProfitConvergence).one()
    assert convergence.status == "waiting_position"


def test_market_submit_failure_invalidates_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_market_item(session_factory)
    _persist_lifecycle(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
    )
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _InsufficientMoneyDeepcoinClient()

    try:
        submit_recovery_order_live(
            session_factory,
            chat_id=200,
            message_id=66,
            symbol="BTC",
            side="short",
            deepcoin_client=fake_client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=datetime(2026, 6, 30, 8, 3, tzinfo=UTC),
        )
    except DeepcoinClientError:
        pass
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("expected DeepcoinClientError")

    with session_factory() as session:
        signal = session.query(TradeSignal).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert signal.status == "failed"
    assert signal.last_error == "Deepcoin API error 36: InsufficientMoney"
    assert lifecycle.lifecycle_status == "invalidated"
    assert lifecycle.exit_reason == "auto_trade_failed"
    assert lifecycle.exited_at is not None


def test_limit_submit_uses_stop_only_trigger_protection(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    _persist_lifecycle(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _OrderProtectionFailingDeepcoinClient()

    result = submit_recovery_order_live(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=datetime(2026, 6, 12, 21, 0, tzinfo=UTC),
    )

    assert result["submitted"] is True
    assert "order_protection_failed_after_entry_submitted" not in result["warnings"]
    assert fake_client.payloads == []
    assert fake_client.protection_payloads == []
    assert fake_client.trigger_payloads[0]["orderType"] == "limit"
    assert all(not any(key.startswith("tp") for key in payload) for payload in fake_client.trigger_payloads)
    assert fake_client.trigger_payloads[0]["slTriggerPx"] == "67500.0"
    assert fake_client.position_protection_payloads == []
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()
    assert binding.status == "open"
    assert binding.order_id == "trigger-1,trigger-2"
    assert binding.last_exchange_status == "submitted"
    assert lifecycle.execution_binding_id == binding.id


def test_trigger_parent_event_is_durable_before_later_submission_bookkeeping_crashes(
    tmp_path, monkeypatch
):
    import telegram_kol_research.recovery_live_submit as submitter

    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_item(session_factory)
    _persist_lifecycle(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _OrderProtectionFailingDeepcoinClient()
    monkeypatch.setattr(
        submitter,
        "_record_submitted_order_events",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash after parent submit")),
    )

    with pytest.raises(RuntimeError, match="crash after parent submit"):
        submit_recovery_order_live(
            session_factory,
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            deepcoin_client=fake_client,
            contract_spec_provider=_StaticContractSpecProvider(),
            submitted_at=datetime(2026, 6, 12, 21, 0, tzinfo=UTC),
        )

    with session_factory() as session:
        intents = session.query(TriggerProtectionIntent).all()
        parent_events = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "create_trigger_entry")
            .order_by(ExecutionEvent.order_id.asc())
            .all()
        )
    assert [intent.parent_trigger_order_id for intent in intents] == ["trigger-1", "trigger-2"]
    assert [event.order_id for event in parent_events] == ["trigger-1", "trigger-2"]
    assert all(event.request_json for event in parent_events)


def test_market_submit_uses_filled_position_id_even_when_different_from_order_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_market_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _DelayedFilledPositionDeepcoinClient()

    result = submit_recovery_order_live(
        session_factory,
        chat_id=200,
        message_id=66,
        symbol="BTC",
        side="short",
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        submitted_at=datetime(2026, 6, 12, 21, 0, tzinfo=UTC),
        max_order_legs=1,
    )

    assert result["submitted"] is True
    assert fake_client.position_protection_payloads[0]["posId"] == "pos-filled-1"
    assert fake_client.position_calls == 4
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
    assert binding.pos_id == "pos-filled-1"


def test_process_next_trade_signal_live_returns_none_without_pending_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})

    assert process_next_trade_signal_live(
        session_factory,
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
    ) is None
