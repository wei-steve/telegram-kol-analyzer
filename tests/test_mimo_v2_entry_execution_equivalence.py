from __future__ import annotations

import json
from datetime import UTC, datetime

from telegram_kol_research.auto_trade_execution import (
    auto_process_message_trade_signal,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.message_recognition import (
    apply_authoritative_mimo_payload,
)
from telegram_kol_research.mimo_v2_contract import parse_mimo_v2_payload
from telegram_kol_research.mimo_v2_execution_adapter import (
    adapt_mimo_v2_to_current_payload,
)
from telegram_kol_research.models import ExecutionEvent, RawMessage, TradeSignal
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 11, 20, 0, tzinfo=UTC)
STRATEGY = {
    "symbol": "BTC",
    "side": "long",
    "entry": "68000-68200",
    "stop_loss": "67500",
    "take_profit": "69000/70000",
    "leverage": "10",
    "order_type": "limit",
}


class _ContractSpecs:
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
        self.orders: list[dict] = []
        self.trigger_orders: list[dict] = []
        self.protections: list[dict] = []

    def get_ticker_price(self, *, inst_id):
        return 68100.0

    def list_positions(self, *, inst_id=None):
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        return []

    def list_open_orders(self, *, inst_id=None):
        return []

    def place_order(self, payload):
        self.orders.append(payload)
        return {"code": "0", "data": {"ordId": "order-1", "posId": "pos-1"}}

    def trigger_order(self, payload):
        self.trigger_orders.append(payload)
        return {
            "code": "0",
            "data": {"ordId": f"trigger-{len(self.trigger_orders)}"},
        }

    def set_position_sltp(self, payload):
        self.protections.append(payload)
        return {"code": "0", "data": {"ordId": "sltp-1"}}


def _group_config() -> GroupConfig:
    return GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="equivalence",
                chat_id=100,
                enabled=True,
                trading_mode="auto_trade",
                max_loss_usdt=20.0,
                symbol_whitelist=["BTC"],
            )
        ]
    )


def _v1_payload() -> dict:
    return {
        "recognition_result": "是策略",
        "reason": "bounded structured reason",
        "summary": "bounded equivalence fixture",
        "confidence": 0.96,
        "strategy": dict(STRATEGY),
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "evidence": {
            "text": {"observed_text": "BTC long execution fixture", "fields": {}},
            "images": [],
            "conflicts": [],
        },
    }


def _v2_payload() -> dict:
    parsed = parse_mimo_v2_payload(
        {
            "contract_version": "mimo-authoritative-v2",
            "summary": "bounded equivalence fixture",
            "confidence": 0.96,
            "intents": [
                {
                    "intent_type": "new_strategy",
                    "action": {
                        "kind": "entry",
                        "target": {"lifecycle_id": None, "thread_id": None},
                        "strategy": dict(STRATEGY),
                        "parameters": {},
                    },
                    "reason": "bounded structured reason",
                    "confidence": 0.96,
                    "evidence_refs": ["text:observed_text"],
                }
            ],
            "evidence": {
                "text": {
                    "observed_text": "BTC long execution fixture",
                    "fields": {},
                },
                "images": [],
                "conflicts": [],
            },
        }
    )
    return adapt_mimo_v2_to_current_payload(parsed).payload


def _seed(database_path, *, payload: dict):
    factory = create_session_factory(database_path)
    with factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=55,
            sender_id=200,
            sender_name="Alice",
            posted_at=NOW,
            text="BTC long execution fixture",
            archived_target_group=True,
        )
        session.add(raw)
        session.commit()
        raw_message_id = raw.id
    save_trading_settings(
        factory,
        {
            "multi_instruction_mode": "live",
            "multi_instruction_activation_after_raw_message_id": 0,
            "auto_trade_enabled": True,
            "max_concurrent_positions": 4,
            "allowed_symbols": ["BTC"],
        },
    )
    result = apply_authoritative_mimo_payload(
        factory,
        raw_message_id=raw_message_id,
        payload=payload,
        model="mimo-v2.5",
        authoritative_generation="generation-equivalence",
    )
    assert result.status == "是策略"
    return factory, raw_message_id


def _snapshot(factory, raw_message_id: int) -> dict:
    client = _FakeDeepcoinClient()
    outcome = auto_process_message_trade_signal(
        factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )
    with factory() as session:
        trade_signal = session.query(TradeSignal).one()
        events = session.query(ExecutionEvent).order_by(ExecutionEvent.id).all()
    return _without_dynamic_values({
        "outcome": outcome,
        "orders": client.orders,
        "trigger_orders": client.trigger_orders,
        "protections": client.protections,
        "trade_signal": json.loads(trade_signal.payload_json),
        "events": [
            {
                "action": row.action,
                "status": row.status,
                "reason": row.reason,
                "request": json.loads(row.request_json or "{}"),
                "response": json.loads(row.response_json or "{}"),
            }
            for row in events
        ],
    })


def _without_dynamic_values(value):
    if isinstance(value, dict):
        return {
            key: _without_dynamic_values(item)
            for key, item in value.items()
            if key != "submitted_at"
        }
    if isinstance(value, list):
        return [_without_dynamic_values(item) for item in value]
    return value


def test_v2_entry_matches_v1_risk_order_draft_and_fake_deepcoin_requests(tmp_path):
    v1_factory, v1_message_id = _seed(
        tmp_path / "entry-v1.db",
        payload=_v1_payload(),
    )
    v2_factory, v2_message_id = _seed(
        tmp_path / "entry-v2.db",
        payload=_v2_payload(),
    )

    assert _snapshot(v2_factory, v2_message_id) == _snapshot(
        v1_factory,
        v1_message_id,
    )
