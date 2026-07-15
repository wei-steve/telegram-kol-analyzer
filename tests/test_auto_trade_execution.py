from datetime import UTC, datetime

import pytest

from telegram_kol_research.auto_trade_execution import _load_active_execution_binding
from telegram_kol_research.auto_trade_execution import _load_active_execution_bindings
from telegram_kol_research.auto_trade_execution import _extract_partial_close_fraction
from telegram_kol_research.auto_trade_execution import auto_process_message_trade_signal
from telegram_kol_research.execution_bindings import ExecutionBindingRecord, ExecutionOrderLegRecord, upsert_execution_binding, upsert_execution_order_leg
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.models import ExecutionBinding, ExecutionEvent, MediaAsset, RawMessage, RecoveryDecisionRecord, SignalCandidate, StrategyLifecycle
from telegram_kol_research.trading_settings import save_trading_settings


class _StaticContractSpecProvider:
    def get_contract_spec(self, instrument_id):
        if instrument_id == "ETH-USDT-SWAP":
            return DeepcoinContractSpec(
                instrument_id=instrument_id,
                contract_value=0.1,
                quantity_step=0.1,
                min_quantity=0.1,
                price_tick=0.01,
            )
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


class _FakeDeepcoinClient:
    def __init__(self):
        self.orders = []
        self.trigger_orders = []
        self.protections = []
        self.cancel_trigger_orders = []
        self.cancel_orders = []
        self.positions = []
        self.trigger_pending = []
        self.open_orders = []
        self.ticker_prices = {}

    def place_order(self, order_payload):
        self.orders.append(order_payload)
        data = {"ordId": f"order-{len(self.orders)}"}
        if order_payload.get("ordType") == "market":
            data["posId"] = f"pos-{len(self.orders)}"
        return {"code": "0", "data": data}

    def trigger_order(self, order_payload):
        self.trigger_orders.append(order_payload)
        return {"code": "0", "data": {"ordId": f"trigger-{len(self.trigger_orders)}"}}

    def set_position_sltp(self, protection_payload):
        self.protections.append(protection_payload)
        return {"code": "0", "data": {"ordId": "sltp-1"}}

    def replace_order_sltp(self, protection_payload):
        self.protections.append(protection_payload)
        return {"code": "0", "data": {"orderSysID": protection_payload["orderSysID"]}}

    def cancel_order(self, cancel_payload):
        self.cancel_orders.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def cancel_trigger_order(self, cancel_payload):
        self.cancel_trigger_orders.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def list_positions(self, *, inst_id=None):
        return self.positions

    def list_trigger_orders_pending(self, *, inst_id):
        return self.trigger_pending

    def list_open_orders(self, *, inst_id=None):
        return self.open_orders

    def get_ticker_price(self, *, inst_id):
        if inst_id in self.ticker_prices:
            return self.ticker_prices[inst_id]
        if inst_id == "ETH-USDT-SWAP":
            return 1585.0
        return 68100.0


def _verify_bound_position(session_factory, *, binding_id: int, pos_id: str) -> None:
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            pos_id=pos_id,
            status="active",
            attribution_status="verified",
            attribution_evidence={"policy_version": 2, "source": "test_fixture"},
            last_verified_at=datetime.now(UTC),
        ),
    )


def test_load_active_execution_binding_uses_kol_id_to_disambiguate(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="group:100#alice",
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            pos_id="pos-alice",
            status="active",
        ),
    )
    second_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="group:100#bob",
            chat_id=100,
            message_id=56,
            symbol="BTC",
            side="long",
            pos_id="pos-bob",
            status="active",
        ),
    )

    selected = _load_active_execution_binding(
        session_factory,
        chat_id=100,
        kol_id="group:100#bob",
        symbol="BTC",
        side="long",
    )

    assert selected is not None
    assert selected.id == second_id
    assert selected.id != first_id


def test_load_active_execution_bindings_returns_every_exact_kol_match_for_andy_management(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="group:100#andy",
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="short",
            pos_id="pos-andy-1",
            status="active",
        ),
    )
    second_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="group:100#andy",
            chat_id=100,
            message_id=56,
            symbol="BTC",
            side="short",
            pos_id="pos-andy-2",
            status="active",
        ),
    )
    upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="group:100#other-kol",
            chat_id=100,
            message_id=57,
            symbol="BTC",
            side="short",
            pos_id="pos-other",
            status="active",
        ),
    )

    matches = _load_active_execution_bindings(
        session_factory,
        chat_id=100,
        kol_id="group:100#andy",
        symbol="BTC",
        side="short",
    )

    assert [binding.id for binding in matches] == [first_id, second_id]
    assert _extract_partial_close_fraction("回成本了，注意保护成本，平加仓") == 0.5


def _persist_candidate(
    session_factory,
    *,
    confidence=0.91,
    with_media=False,
    text="BTC long 68000-68200 SL 67500 TP 69000/70000",
    entry_text="68000-68200",
    stop_loss_text="67500",
    take_profit_text="69000 / 70000",
    symbol="BTC",
    side="long",
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=55,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 6, 12, 8, 0),
            text=text,
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol=symbol,
                side=side,
                event_type="entry_signal",
                entry_text=entry_text,
                stop_loss_text=stop_loss_text,
                take_profit_text=take_profit_text,
                parse_source="mimo_direct" if with_media else "text_ai",
                confidence=confidence,
            )
        )
        if with_media:
            session.add(
                MediaAsset(
                    raw_message_id=raw.id,
                    kind="photo",
                    local_path="data/media/example.jpg",
                )
            )
        session.commit()
        return raw.id


def _group_config():
    return GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="Auto Group",
                chat_id=100,
                enabled=True,
                trading_mode="auto_trade",
                max_loss_usdt=20.0,
                symbol_whitelist=["BTC", "ETH"],
            )
        ]
    )


@pytest.mark.parametrize(
    ("mode", "auto_trade_enabled", "expected_status", "expected_reason"),
    [
        ("shadow", False, "shadow_planned", None),
        ("live", True, "blocked", "management_executor_unavailable"),
    ],
)
def test_management_planning_never_writes_before_batch_executor_exists(
    tmp_path,
    monkeypatch,
    mode,
    auto_trade_enabled,
    expected_status,
    expected_reason,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        entry = RawMessage(
            chat_id=100,
            message_id=54,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 7, 15, 7, 55, tzinfo=UTC),
            text="BTC short",
            archived_target_group=True,
        )
        management = RawMessage(
            chat_id=100,
            message_id=55,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
            text="BTC short exit",
            archived_target_group=True,
        )
        session.add_all([entry, management])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=54,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=entry.posted_at,
        )
        session.add(lifecycle)
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:54:BTC:short",
            kol_id="group:100",
            chat_id=100,
            message_id=54,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="pos-shadow",
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        session.add(
            SignalCandidate(
                raw_message_id=management.id,
                symbol="BTC",
                side="short",
                event_type="close_signal",
                target_lifecycle_id=lifecycle.id,
                management_action="full_exit",
                recognition_generation="shadow-generation",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        from telegram_kol_research.models import ExecutionOrderLeg, RecognitionDecision

        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=0,
                purpose="entry",
                order_kind="market",
                order_id="pos-shadow",
                pos_id="pos-shadow",
                venue="deepcoin",
                attribution_status="verified",
                attribution_evidence_json='{"policy_version": 2}',
                status="active",
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=management.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="success",
                authoritative_payload_json="{}",
                agreement_status="authoritative_only",
                differences_json="[]",
            )
        )
        session.commit()
        raw_message_id = management.id

    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": auto_trade_enabled,
            "management_execution_mode": mode,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-shadow",
            "posSide": "short",
            "pos": "10",
            "avgPx": "62000",
            "mgnMode": "cross",
            "posMode": "split",
        }
    ]
    import telegram_kol_research.strategy_management_planner as planner

    monkeypatch.setattr(
        planner, "reconcile_deepcoin_execution_bindings", lambda *args, **kwargs: None
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 15, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == expected_status
    if expected_reason is None:
        assert result["management_action"] == "full_exit"
    else:
        assert result["reason"] == expected_reason
        from telegram_kol_research.models import StrategyManagementBatch

        with session_factory() as session:
            batch = session.get(StrategyManagementBatch, result["batch_id"])
            assert batch.status == "blocked"
            assert batch.reason_code == "management_executor_unavailable"
    assert isinstance(result["batch_id"], int)
    assert fake_client.orders == []
    assert fake_client.trigger_orders == []
    assert fake_client.protections == []
    assert fake_client.cancel_orders == []
    assert fake_client.cancel_trigger_orders == []


@pytest.mark.parametrize(
    ("group_config", "allowed_symbols", "expected_reason"),
    [
        (
            GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="Disabled",
                        chat_id=100,
                        enabled=True,
                        trading_mode="notify_only",
                    )
                ]
            ),
            ["BTC", "ETH"],
            "kol_or_group_auto_trade_disabled",
        ),
        (_group_config(), ["ETH"], "symbol_not_allowed"),
    ],
)
def test_management_planning_preserves_runtime_risk_gates(
    tmp_path, monkeypatch, group_config, allowed_symbols, expected_reason
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=99,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
            text="BTC exit",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="short",
                event_type="close_signal",
                management_action="full_exit",
                recognition_generation="risk-gate-generation",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        raw_id = raw.id
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": False,
            "management_execution_mode": "shadow",
            "allowed_symbols": allowed_symbols,
        },
    )
    import telegram_kol_research.auto_trade_execution as auto_module

    monkeypatch.setattr(
        auto_module,
        "plan_strategy_management_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("planner must not run after a failed runtime gate")
        ),
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_id,
        group_config=group_config,
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == expected_reason


def test_auto_process_message_trade_signal_submits_live_order_with_protection(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
            "max_market_entry_deviation_pct": 0.01,
        },
    )
    fake_client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 6, 12, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert result["entry_execution_type"] == "limit"
    assert fake_client.orders == []
    assert len(fake_client.trigger_orders) == 2
    assert fake_client.trigger_orders[0]["orderType"] == "limit"
    assert fake_client.trigger_orders[0]["triggerPrice"] == "68200.0"
    assert [order["tpTriggerPx"] for order in fake_client.trigger_orders] == [
        69000.0,
        69000.0,
    ]
    assert fake_client.trigger_orders[0]["slTriggerPx"] == 67500.0
    assert fake_client.protections == []
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
    assert binding.strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert binding.margin_mode == "cross"
    assert binding.position_mode == "split"


def test_auto_process_range_entry_uses_half_market_half_midpoint_limit_when_near_edge(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="ETH long 1565-1585 SL 1545 TP 1605/1625/1645",
        entry_text="1565-1585",
        stop_loss_text="1545",
        take_profit_text="1605/1625/1645",
        symbol="ETH",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
            "max_market_entry_deviation_pct": 0.15,
        },
    )
    fake_client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 1, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert len(fake_client.orders) == 1
    assert len(fake_client.trigger_orders) == 1
    assert fake_client.orders[0]["ordType"] == "market"
    assert fake_client.orders[0]["sz"] == "2.5"
    assert fake_client.trigger_orders[0]["orderType"] == "limit"
    assert fake_client.trigger_orders[0]["triggerPrice"] == "1575.0"
    assert [order["sz"] for order in fake_client.trigger_orders] == ["3.3"]
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        events = session.query(ExecutionEvent).order_by(ExecutionEvent.id.asc()).all()
    assert binding.symbol == "ETH"
    assert binding.order_id == "order-1,trigger-1"
    assert [event.action for event in events] == [
        "open_market_position",
        "set_position_tpsl",
        "set_position_tpsl",
        "set_position_tpsl",
        "set_position_tpsl",
        "create_trigger_entry",
    ]


def test_auto_process_message_trade_signal_uses_symbol_specific_risk_budget(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="ETH long 1565-1585 SL 1545 TP 1605/1625/1645",
        entry_text="1565-1585",
        stop_loss_text="1545",
        take_profit_text="1605/1625/1645",
        symbol="ETH",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "symbol_max_loss_usdt": {"ETH": 15},
            "allowed_symbols": ["BTC", "ETH"],
            "max_market_entry_deviation_pct": 0.01,
        },
    )
    fake_client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 1, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    with session_factory() as session:
        decision = session.query(RecoveryDecisionRecord).one()
    assert decision.symbol == "ETH"
    assert decision.max_loss_usdt == 15.0


def test_auto_process_message_trade_signal_blocks_media_when_vision_auto_trade_disabled(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(session_factory, with_media=True)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "allow_vision_auto_trade": False},
    )
    fake_client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result == {"status": "skipped", "reason": "vision_auto_trade_disabled"}
    assert fake_client.orders == []
    with session_factory() as session:
        event = session.query(ExecutionEvent).one()
    assert event.action == "auto_trade_skipped"
    assert event.status == "skipped"
    assert event.reason == "vision_auto_trade_disabled"
    assert event.chat_id == 100
    assert event.message_id == 55
    assert event.symbol == "BTC"
    assert event.side == "long"


def test_auto_process_message_trade_signal_submits_market_order_then_position_sltp(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="BTC 现价开多 SL 67500 TP 69000",
        entry_text="现价入场",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "long",
            "pos": "33",
            "mrgPosition": "split",
            "mgnMode": "cross",
            "uTime": "1",
        }
    ]

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 6, 12, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert result["entry_execution_type"] == "market"
    assert fake_client.orders[0]["ordType"] == "market"
    assert fake_client.protections[0]["posId"] == "pos-1"
    assert fake_client.protections[0]["slTriggerPx"] == "67500.0"
    assert [payload.get("tpTriggerPx") for payload in fake_client.protections] == [
        None,
        "69000.0",
        "70000.0",
    ]
    assert [payload.get("sz") for payload in fake_client.protections] == [None, "16", "17"]
    with session_factory() as session:
        events = session.query(ExecutionEvent).order_by(ExecutionEvent.id.asc()).all()
    assert [event.action for event in events] == [
        "open_market_position",
        "set_position_tpsl",
        "set_position_tpsl",
        "set_position_tpsl",
    ]
    assert events[1].pos_id == "pos-1"
    assert '"stop_loss": "67500.0"' in (events[1].after_json or "")
    assert '"take_profit": "69000.0"' in (events[2].after_json or "")
    assert '"take_profit": "70000.0"' in (events[3].after_json or "")


def test_auto_process_message_trade_signal_accepts_nearby_single_entry_price(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="BTC短线做多 进场点位：59500附近 止损点位：58100 止盈点位：61800",
        entry_text="59500附近",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 6, 30, 11, 57, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert result["entry_execution_type"] == "limit"
    assert fake_client.orders == []
    assert len(fake_client.trigger_orders) == 1
    assert fake_client.trigger_orders[0]["orderType"] == "limit"


def test_auto_process_nearby_single_entry_uses_market_when_price_is_close(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="米娅BTC短线合约交易策略 做多 进场点位：59600附近 止损点位：58100 止盈点位：61800",
        entry_text="59600附近",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.ticker_prices["BTC-USDT-SWAP"] = 59680.0
    fake_client.positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-nearby",
            "posSide": "long",
            "pos": "13",
            "mrgPosition": "split",
            "mgnMode": "cross",
            "uTime": "1",
        }
    ]

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 2, 9, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert result["entry_execution_type"] == "market"
    assert len(fake_client.orders) == 1
    assert fake_client.orders[0]["ordType"] == "market"
    assert fake_client.trigger_orders == []
    assert fake_client.protections[0]["posId"] == "pos-1"


def test_auto_process_nearby_single_entry_keeps_limit_when_price_is_far(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="米娅BTC短线合约交易策略 做多 进场点位：59600附近 止损点位：58100 止盈点位：61800",
        entry_text="59600附近",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.ticker_prices["BTC-USDT-SWAP"] = 60300.0

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 2, 9, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert result["entry_execution_type"] == "limit"
    assert fake_client.orders == []
    assert len(fake_client.trigger_orders) == 1
    assert fake_client.trigger_orders[0]["orderType"] == "limit"


def test_auto_process_message_trade_signal_expands_btc_wan_shorthand_prices(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text=(
            "比特币\n方向：做多\n入场：5.89-5.93附近入场\n"
            "止盈：点位1：6万附近 点位2：6.07附近 点位3：6.23\n"
            "止损：小幅跌破前低5.78一点。"
        ),
        entry_text="5.89-5.93附近",
        stop_loss_text="5.78",
        take_profit_text="6万附近 / 6.07附近 / 6.23",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.get_ticker_price = lambda *, inst_id: 59195.0

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 6, 30, 18, 10, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert fake_client.orders == []
    assert [order["triggerPrice"] for order in fake_client.trigger_orders] == [
        "59300.0",
        "59100.0",
    ]
    assert fake_client.protections == []
    assert fake_client.trigger_orders[0]["slTriggerPx"] == 57800.0
    assert [order["tpTriggerPx"] for order in fake_client.trigger_orders] == [
        60000.0,
        60000.0,
    ]


def test_auto_process_message_trade_signal_skips_lifecycle_entry_confirmation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="兄弟们，跟上节奏，直接进场",
        entry_text=None,
    )
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        candidate.parse_source = "lifecycle_ai"
        session.commit()
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result == {"status": "skipped", "reason": "lifecycle_event_not_new_entry"}
    assert fake_client.orders == []
    assert fake_client.trigger_orders == []


def test_auto_process_message_trade_signal_blocks_low_confidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(session_factory, confidence=0.5)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "min_ai_confidence": 0.75},
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    assert result == {"status": "skipped", "reason": "confidence_below_minimum"}


def test_auto_process_message_trade_signal_closes_position_from_close_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:55:BTC:long",
            kol_id="alice",
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="pos-1",
            status="active",
        )
        session.add(binding)
        session.flush()
        binding_id = binding.id
        raw = RawMessage(
            chat_id=100,
            message_id=56,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 6, 12, 8, 5),
            text="BTC leave now",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type="close_signal",
                parse_source="lifecycle_ai",
                confidence=0.95,
            )
        )
        session.commit()
        raw_message_id = raw.id
    _verify_bound_position(session_factory, binding_id=binding_id, pos_id="pos-1")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "long",
            "pos": "33",
        }
    ]

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        processed_at=datetime(2026, 6, 12, 8, 6, tzinfo=UTC),
    )

    assert result == {
        "status": "skipped",
        "reason": "management_execution_disabled",
    }
    assert fake_client.orders == []


def test_auto_process_close_signal_does_not_steal_live_position_from_other_chat(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="short",
            lifecycle_status="exited",
            exit_reason="kol_signal",
            signal_at=datetime(2026, 7, 2, 12, 34),
            entered_at=datetime(2026, 7, 2, 12, 34),
            exited_at=datetime(2026, 7, 2, 13, 7),
            entry_range_low=61351,
            entry_range_high=61351,
            entry_price_actual=61351,
            stop_loss=62300,
            take_profit="59588",
            exit_signal_message_id=56,
        )
        session.add(lifecycle)
        session.add(
            ExecutionBinding(
                strategy_instance_id="deepcoin:999:3888:BTC:short",
                kol_id="wrong-kol",
                chat_id=999,
                message_id=3888,
                symbol="BTC",
                side="short",
                venue="deepcoin",
                pos_id="pos-sanjie",
                status="stale",
                last_exchange_status="expired_pending_entry_not_attributed",
            )
        )
        session.add(
            StrategyLifecycle(
                chat_id=999,
                message_id=3888,
                symbol="BTC",
                side="short",
                lifecycle_status="expired",
                exit_reason="expired",
                signal_at=datetime(2026, 6, 30, 2, 51),
                exited_at=datetime(2026, 6, 30, 8, 51),
                entry_range_low=60300,
                entry_range_high=60800,
                stop_loss=61300,
                take_profit="59600/58900/58200",
            )
        )
        raw = RawMessage(
            chat_id=100,
            message_id=56,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 7, 2, 13, 7),
            text="比特币空单，提前小损200点出局！",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="short",
                event_type="close_signal",
                parse_source="lifecycle_ai",
                confidence=0.95,
            )
        )
        session.commit()
        raw_message_id = raw.id

    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-sanjie",
            "posSide": "short",
            "pos": "10",
            "avgPx": "61351",
        }
    ]

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        processed_at=datetime(2026, 7, 2, 13, 8, tzinfo=UTC),
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "management_execution_disabled"
    assert fake_client.orders == []
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).filter_by(chat_id=100, message_id=55).one()
        other_lifecycle = (
            session.query(StrategyLifecycle).filter_by(chat_id=999, message_id=3888).one()
        )
        stale_binding = session.query(ExecutionBinding).filter_by(chat_id=999, message_id=3888).one()

    assert lifecycle.execution_binding_id is None
    assert stale_binding.status == "stale"
    assert stale_binding.last_exchange_status == "expired_pending_entry_not_attributed"
    assert other_lifecycle.lifecycle_status == "expired"
    assert other_lifecycle.execution_binding_id is None


def test_auto_process_close_signal_does_not_recover_ambiguous_positions(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="short",
                lifecycle_status="exited",
                exit_reason="kol_signal",
                signal_at=datetime(2026, 7, 2, 12, 34),
                entered_at=datetime(2026, 7, 2, 12, 34),
                exited_at=datetime(2026, 7, 2, 13, 7),
                entry_price_actual=61351,
                exit_signal_message_id=56,
            )
        )
        raw = RawMessage(
            chat_id=100,
            message_id=56,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 7, 2, 13, 7),
            text="比特币空单，提前小损200点出局！",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="short",
                event_type="close_signal",
                parse_source="lifecycle_ai",
                confidence=0.95,
            )
        )
        session.commit()
        raw_message_id = raw.id

    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-a",
            "posSide": "short",
            "pos": "10",
            "avgPx": "61351",
        },
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-b",
            "posSide": "short",
            "pos": "10",
            "avgPx": "61351",
        },
    ]

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        processed_at=datetime(2026, 7, 2, 13, 8, tzinfo=UTC),
    )

    assert result == {
        "status": "skipped",
        "reason": "management_execution_disabled",
    }
    assert fake_client.orders == []


def test_auto_process_message_trade_signal_does_not_guess_filled_binding_before_close(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            ExecutionBinding(
                strategy_instance_id="deepcoin:100:55:BTC:long",
                kol_id="alice",
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                order_id="order-filled",
                client_order_id="client-filled",
                pos_id=None,
                status="open",
            )
        )
        raw = RawMessage(
            chat_id=100,
            message_id=56,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 6, 12, 8, 5),
            text="BTC leave now",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type="close_signal",
                parse_source="lifecycle_ai",
                confidence=0.95,
            )
        )
        session.commit()
        raw_message_id = raw.id
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-filled",
            "posSide": "long",
            "pos": "33",
        }
    ]

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        processed_at=datetime(2026, 6, 12, 8, 6, tzinfo=UTC),
    )

    assert result == {
        "status": "skipped",
        "reason": "management_execution_disabled",
    }
    assert fake_client.orders == []
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
    assert binding.pos_id is None
    assert binding.status == "open"
    assert binding.last_exchange_status is None


def test_auto_process_message_trade_signal_partially_closes_profit_percent(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:55:BTC:short",
            kol_id="alice",
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="pos-1",
            status="active",
        )
        session.add(binding)
        session.flush()
        binding_id = binding.id
        raw = RawMessage(
            chat_id=100,
            message_id=56,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 6, 30, 20, 57),
            text="走70%仓位利润，汇报 吃肉了 #BTC",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="short",
                event_type="position_update",
                stop_loss_text="62100",
                take_profit_text="58388/57388",
                parse_source="lifecycle_ai",
                confidence=0.95,
            )
        )
        session.commit()
        raw_message_id = raw.id
    _verify_bound_position(session_factory, binding_id=binding_id, pos_id="pos-1")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "short",
            "pos": "7",
        }
    ]

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        processed_at=datetime(2026, 6, 30, 20, 58, tzinfo=UTC),
    )

    assert result == {
        "status": "skipped",
        "reason": "management_execution_disabled",
    }
    assert fake_client.orders == []


def test_auto_process_message_trade_signal_adjusts_stop_loss_from_position_update(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:55:BTC:long",
            kol_id="alice",
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="pos-1",
            status="active",
        )
        session.add(binding)
        session.flush()
        binding_id = binding.id
        raw = RawMessage(
            chat_id=100,
            message_id=57,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 6, 12, 8, 10),
            text="BTC SL moved to 68050",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type="position_update",
                stop_loss_text="68050",
                parse_source="lifecycle_ai",
                confidence=0.95,
            )
        )
        session.commit()
        raw_message_id = raw.id
    _verify_bound_position(session_factory, binding_id=binding_id, pos_id="pos-1")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "long",
            "pos": "33",
            "cTime": "1000",
        }
    ]
    fake_client.trigger_pending = [
        {
            "triggerOrderType": "TPSL",
            "ordId": "tp-old",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "posId": "pos-1",
            "tpTriggerPx": "69000",
            "sz": "33",
            "cTime": "1000",
        },
        {
            "triggerOrderType": "TPSL",
            "ordId": "sl-old",
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "posId": "pos-1",
            "slTriggerPx": "67500",
            "sz": "33",
            "cTime": "1000",
        },
    ]

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        processed_at=datetime(2026, 6, 12, 8, 11, tzinfo=UTC),
    )

    assert result == {
        "status": "skipped",
        "reason": "management_execution_disabled",
    }
    assert fake_client.cancel_trigger_orders == []
    assert fake_client.protections == []
