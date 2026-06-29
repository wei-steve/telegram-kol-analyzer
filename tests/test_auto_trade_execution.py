from datetime import UTC, datetime

from telegram_kol_research.auto_trade_execution import auto_process_message_trade_signal
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.models import ExecutionBinding, MediaAsset, RawMessage, SignalCandidate
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
        self.orders = []
        self.protections = []

    def place_order(self, order_payload):
        self.orders.append(order_payload)
        return {"code": "0", "data": {"ordId": f"order-{len(self.orders)}"}}

    def replace_order_sltp(self, protection_payload):
        self.protections.append(protection_payload)
        return {"code": "0", "data": {"OrderSysID": protection_payload["OrderSysID"]}}

    def cancel_order(self, cancel_payload):
        return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}


def _persist_candidate(session_factory, *, confidence=0.91, with_media=False):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=55,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 6, 12, 8, 0),
            text="BTC long 68000-68200 SL 67500 TP 69000/70000",
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


def test_auto_process_message_trade_signal_submits_live_order_with_protection(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(session_factory)
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
        processed_at=datetime(2026, 6, 12, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert len(fake_client.orders) == 2
    assert len(fake_client.protections) == 2
    assert fake_client.protections[0]["tpTriggerPx"] == "69000.0"
    assert fake_client.protections[0]["slTriggerPx"] == "67500.0"
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
    assert binding.strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert binding.margin_mode == "cross"
    assert binding.position_mode == "split"


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
