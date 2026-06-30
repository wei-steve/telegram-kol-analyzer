from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.models import ExecutionBinding, RawMessage, SignalCandidate
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_live_submit import RecoveryLiveSubmitError
from telegram_kol_research.recovery_live_submit import build_deepcoin_market_order_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_order_sltp_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_place_order_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_position_sltp_payload
from telegram_kol_research.recovery_live_submit import build_deepcoin_trigger_order_payload
from telegram_kol_research.recovery_live_submit import enqueue_recovery_trade_signal
from telegram_kol_research.recovery_live_submit import process_next_trade_signal_live
from telegram_kol_research.recovery_live_submit import submit_recovery_order_live
from telegram_kol_research.recovery_order_confirmation import confirm_recovery_order_dry_run
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal
from telegram_kol_research.trading_settings import save_trading_settings


class _StaticContractSpecProvider:
    def get_contract_spec(self, instrument_id):
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


class _FakeDeepcoinClient:
    def __init__(self):
        self.payloads = []
        self.trigger_payloads = []
        self.protection_payloads = []
        self.cancel_payloads = []
        self.positions = []

    def place_order(self, order_payload):
        self.payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": f"order-{len(self.payloads)}"}}

    def trigger_order(self, order_payload):
        self.trigger_payloads.append(order_payload)
        return {"code": "0", "data": {"ordId": f"trigger-{len(self.trigger_payloads)}"}}

    def set_position_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        return {"code": "0", "data": {"ordId": "sltp-1"}}

    def replace_order_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        return {"code": "0", "data": {"orderSysID": protection_payload["orderSysID"]}}

    def cancel_order(self, cancel_payload):
        self.cancel_payloads.append(cancel_payload)
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    def list_positions(self, *, inst_id=None):
        return self.positions

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
        return {"code": "0", "data": {"ordId": "order-market-1"}}

    def set_position_sltp(self, protection_payload):
        self.protection_payloads.append(protection_payload)
        raise RuntimeError("missing_take_profit_for_protection")


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


def test_build_deepcoin_order_sltp_payload_uses_first_take_profit_and_stop_loss():
    payload = build_deepcoin_order_sltp_payload(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "stop_loss": 67500.0,
            "take_profit_legs": [
                {"price": 69000.0, "allocation_pct": 50.0},
                {"price": 70000.0, "allocation_pct": 50.0},
            ],
        },
        order_id="order-1",
    )

    assert payload == {
        "instId": "BTC-USDT-SWAP",
        "orderSysID": "order-1",
        "tpTriggerPx": "69000.0",
        "slTriggerPx": "67500.0",
    }


def test_build_deepcoin_trigger_order_payload_embeds_take_profit_and_stop_loss():
    payload = build_deepcoin_trigger_order_payload(
        {
            "instrument_id": "BTC-USDT-SWAP",
            "margin_mode": "cross",
            "position_mode": "split",
            "stop_loss": 67500.0,
            "take_profit_legs": [{"price": 69000.0, "allocation_pct": 100.0}],
            "order_legs": [{"position_side": "long"}],
        },
        {
            "side": "buy",
            "position_side": "long",
            "price": 68100.0,
            "quantity": 83.0,
        },
    )

    assert payload["orderType"] == "limit"
    assert payload["triggerPrice"] == "68100.0"
    assert payload["tpTriggerPx"] == 69000.0
    assert payload["slTriggerPx"] == 67500.0
    assert payload["mrgPosition"] == "split"


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
    assert fake_client.trigger_payloads[0]["tdMode"] == "cross"
    assert fake_client.trigger_payloads[0]["mrgPosition"] == "split"
    assert fake_client.trigger_payloads[0]["tpTriggerPx"] == 69000.0
    assert fake_client.trigger_payloads[0]["slTriggerPx"] == 67500.0
    assert fake_client.protection_payloads == []
    assert result["warnings"] == ["only_first_take_profit_submitted_for_order_sltp"]
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
    assert binding.status == "open"
    assert binding.order_id == "trigger-1,trigger-2"
    assert binding.client_order_id == "TK649760E806ACF61,TK729D11F4739D2A2"
    assert binding.strategy_instance_id == "deepcoin:100:55:BTC:long"


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


def test_market_submit_persists_binding_when_position_protection_fails(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _persist_ready_market_item(session_factory)
    save_trading_settings(session_factory, {"auto_trade_enabled": True})
    fake_client = _ProtectionFailingDeepcoinClient()

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
    except Exception as exc:
        assert "missing_take_profit_for_protection" in str(exc)
    else:
        raise AssertionError("expected protection setup failure")

    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()

    assert binding.status == "active"
    assert binding.last_exchange_status == "position_active_protection_failed"
    assert binding.order_id == "order-market-1"
    assert binding.pos_id == "pos-market-1"


def test_process_next_trade_signal_live_returns_none_without_pending_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    save_trading_settings(session_factory, {"auto_trade_enabled": True})

    assert process_next_trade_signal_live(
        session_factory,
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
    ) is None
