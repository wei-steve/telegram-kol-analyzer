from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

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
    _execution_projection,
    adapt_mimo_v2_to_current_payload,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    MessageEvidenceExtractionClaim,
    MessageInstructionItem,
    PositionProtectionLedger,
    RawMessage,
    RecoveryDecisionRecord,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
    TradeSignal,
)
from telegram_kol_research.source_message_deletion import (
    record_source_message_deleted,
)
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
EXECUTION_FIXTURES = {
    "entry": ("entry", {}),
    "cancel": ("cancel_pending_entry", {}),
    "full_exit": ("full_exit", {"exit_price": "68100"}),
    "partial_exit": ("partial_exit", {"management_fraction": 0.5}),
    "partial_take_profit": (
        "partial_take_profit",
        {"management_fraction": 0.5, "take_profit": "69000"},
    ),
    "move_stop": ("move_stop_to_protect", {"stop_loss": "68000"}),
    "hold": ("hold_update", {}),
    "revision": ("replace_entry", {}),
    "multi_action": (
        "partial_then_protect",
        {
            "management_fraction": 0.5,
            "take_profit": "69000",
            "stop_loss": "68000",
        },
    ),
}


def _intent_type(kind: str) -> str:
    return {
        "entry": "new_strategy",
        "cancel_pending_entry": "cancel_entry",
        "replace_entry": "strategy_revision",
        "full_exit": "exit",
        "partial_exit": "exit",
        "partial_take_profit": "position_management",
        "move_stop_to_protect": "position_management",
        "hold_update": "position_management",
    }[kind]


def _intent(
    kind: str,
    *,
    lifecycle_id: int | None,
    parameters: dict,
    strategy: dict | None = None,
) -> dict:
    return {
        "intent_type": _intent_type(kind),
        "action": {
            "kind": kind,
            "target": {
                "lifecycle_id": None if kind == "entry" else lifecycle_id,
                "thread_id": None,
            },
            "strategy": (
                deepcopy(strategy or STRATEGY)
                if kind in {"entry", "replace_entry"}
                else None
            ),
            "parameters": dict(parameters),
        },
        "reason": f"equivalent {kind}",
        "confidence": 0.96,
        "evidence_refs": ["text:observed_text"],
    }


def _v2_payload(
    fixture_name: str,
    *,
    lifecycle_id: int | None = None,
    strategy: dict | None = None,
) -> dict:
    kind, parameters = EXECUTION_FIXTURES[fixture_name]
    if kind == "partial_then_protect":
        intents = [
            _intent(
                "partial_take_profit",
                lifecycle_id=lifecycle_id,
                parameters={
                    "management_fraction": parameters["management_fraction"],
                    "take_profit": parameters["take_profit"],
                },
            ),
            _intent(
                "move_stop_to_protect",
                lifecycle_id=lifecycle_id,
                parameters={"stop_loss": parameters["stop_loss"]},
            ),
        ]
    else:
        intents = [
            _intent(
                kind,
                lifecycle_id=lifecycle_id,
                parameters=parameters,
                strategy=strategy,
            )
        ]
    parsed = parse_mimo_v2_payload(
        {
            "contract_version": "mimo-authoritative-v2",
            "summary": f"equivalent {fixture_name}",
            "confidence": 0.96,
            "intents": intents,
            "evidence": {
                "text": {
                    "observed_text": _source_text(fixture_name),
                    "fields": {},
                },
                "images": [],
                "conflicts": [],
            },
        }
    )
    return adapt_mimo_v2_to_current_payload(parsed).payload


def _legacy_payload(
    fixture_name: str,
    *,
    lifecycle_id: int | None = None,
    strategy: dict | None = None,
) -> dict:
    # This fixture is deliberately constructed from the established contract,
    # not by copying the v2 adapter output.
    kind, parameters = EXECUTION_FIXTURES[fixture_name]
    if kind == "partial_then_protect":
        instruction_kind = "partial_take_profit"
        management_action = "partial_take_profit, move_stop_to_protect"
        event_type = "position_update"
    else:
        instruction_kind = kind
        event_type, management_action = {
            "entry": ("none", None),
            "replace_entry": ("none", None),
            "cancel_pending_entry": ("cancel_entry", "cancel_pending_entry"),
            "full_exit": ("exit_position", "exit_full"),
            "partial_exit": ("exit_position", "exit_partial"),
            "partial_take_profit": (
                "position_update",
                "partial_take_profit",
            ),
            "move_stop_to_protect": (
                "position_update",
                "move_stop_to_protect",
            ),
            "hold_update": ("position_update", "hold_update"),
        }[kind]
    strategy = (
        deepcopy(strategy or STRATEGY)
        if kind in {"entry", "replace_entry"}
        else None
    )
    lifecycle_event = {
        "event_type": event_type,
        "confidence": 0.0 if event_type == "none" else 0.96,
        "reason": (
            "equivalent partial_take_profit；"
            "equivalent move_stop_to_protect"
            if kind == "partial_then_protect"
            else f"equivalent {kind}"
        ),
    }
    if event_type != "none":
        lifecycle_event.update(
            {
                "management_action": management_action,
                "target_lifecycle_id": lifecycle_id,
                **parameters,
            }
        )
    return {
        "instructions": [
            {
                "kind": instruction_kind,
                "confidence": 0.96,
                "reason": f"equivalent {instruction_kind}",
                "strategy": strategy,
                "target": {
                    "lifecycle_id": (
                        None if kind == "entry" else lifecycle_id
                    ),
                    "thread_id": None,
                },
                "parameters": dict(parameters),
            }
        ],
        "recognition_result": "是策略" if kind == "entry" else "非策略",
        "reason": f"equivalent {fixture_name}",
        "summary": f"equivalent {fixture_name}",
        "strategy": strategy or {},
        "lifecycle_event": lifecycle_event,
        "confidence": 0.96,
        "input_reading": {"observed_text": _source_text(fixture_name)},
        "evidence": {
            "text": {"observed_text": _source_text(fixture_name), "fields": {}},
            "images": [],
            "conflicts": [],
        },
    }


def _source_text(fixture_name: str) -> str:
    return {
        "entry": "BTC long 68000-68200 SL 67500 TP 69000 70000",
        "cancel": "cancel the pending BTC entry",
        "full_exit": "close the BTC position fully",
        "partial_exit": "close half of the BTC position",
        "partial_take_profit": "take profit on half of BTC at 69000",
        "move_stop": "move BTC stop to 68000",
        "hold": "continue holding BTC",
        "revision": "replace BTC entry with updated levels",
        "multi_action": "take half profit and move stop to 68000",
    }[fixture_name]


@pytest.mark.parametrize("fixture_name", EXECUTION_FIXTURES)
def test_v2_adapter_matches_v1_execution_projection(fixture_name):
    lifecycle_id = None if fixture_name == "entry" else 790
    assert _execution_projection(
        _v2_payload(fixture_name, lifecycle_id=lifecycle_id)
    ) == _execution_projection(
        _legacy_payload(fixture_name, lifecycle_id=lifecycle_id)
    )


def _seed_projection_database(path, *, fixture_name: str):
    factory = create_session_factory(path)
    save_trading_settings(
        factory,
        {
            "multi_instruction_mode": "live",
            "multi_instruction_activation_after_raw_message_id": 0,
        },
    )
    with factory() as session:
        raw = RawMessage(
            chat_id=88,
            message_id=901,
            posted_at=NOW,
            text=_source_text(fixture_name),
            archived_target_group=True,
        )
        session.add(raw)
        lifecycle = None
        if fixture_name != "entry":
            lifecycle = StrategyLifecycle(
                chat_id=88,
                message_id=900,
                symbol="BTC",
                side="long",
                lifecycle_status=(
                    "pending_entry" if fixture_name in {"cancel", "revision"}
                    else "entered"
                ),
                signal_at=NOW,
                entered_at=(
                    None
                    if fixture_name in {"cancel", "revision"}
                    else NOW
                ),
                entry_range_low=68000,
                entry_range_high=68200,
                stop_loss=67500,
                take_profit="69000/70000",
            )
            session.add(lifecycle)
        session.commit()
        return factory, int(raw.id), int(lifecycle.id) if lifecycle else None


def _projection_snapshot(factory, raw_message_id: int) -> dict:
    with factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == raw_message_id)
            .order_by(SignalCandidate.id.asc())
            .all()
        )
        items = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.raw_message_id == raw_message_id)
            .order_by(MessageInstructionItem.sequence.asc())
            .all()
        )
        lifecycles = session.query(StrategyLifecycle).order_by(StrategyLifecycle.id).all()
        return {
            "candidates": [
                {
                    "symbol": row.symbol,
                    "side": row.side,
                    "entry": row.entry_text,
                    "stop_loss": row.stop_loss_text,
                    "take_profit": row.take_profit_text,
                    "event_type": row.event_type,
                    "management_action": row.management_action,
                    "targeted": row.target_lifecycle_id is not None,
                    "review_status": row.review_status,
                    "parse_source": row.parse_source,
                    "generation": row.recognition_generation,
                }
                for row in candidates
            ],
            "items": [
                {
                    "sequence": row.sequence,
                    "kind": row.instruction_kind,
                    "idempotency_key": row.idempotency_key,
                    "status": row.status,
                    "retired": row.retired_at is not None,
                }
                for row in items
            ],
            "lifecycles": [
                {
                    "symbol": row.symbol,
                    "side": row.side,
                    "status": row.lifecycle_status,
                    "entry_low": row.entry_range_low,
                    "entry_high": row.entry_range_high,
                    "stop_loss": row.stop_loss,
                    "take_profit": row.take_profit,
                    "management_action": row.management_action,
                }
                for row in lifecycles
            ],
        }


@pytest.mark.parametrize(
    "fixture_name",
    [name for name in EXECUTION_FIXTURES if name != "revision"],
)
def test_v2_adapter_matches_v1_authoritative_application_snapshot(
    tmp_path,
    fixture_name,
):
    v1_factory, v1_raw_id, v1_lifecycle_id = _seed_projection_database(
        tmp_path / f"{fixture_name}-v1.db",
        fixture_name=fixture_name,
    )
    v2_factory, v2_raw_id, v2_lifecycle_id = _seed_projection_database(
        tmp_path / f"{fixture_name}-v2.db",
        fixture_name=fixture_name,
    )
    v1 = apply_authoritative_mimo_payload(
        v1_factory,
        raw_message_id=v1_raw_id,
        payload=_legacy_payload(
            fixture_name,
            lifecycle_id=v1_lifecycle_id,
        ),
        model="mimo-v2.5",
        authoritative_generation="equivalent-generation",
    )
    v2 = apply_authoritative_mimo_payload(
        v2_factory,
        raw_message_id=v2_raw_id,
        payload=_v2_payload(
            fixture_name,
            lifecycle_id=v2_lifecycle_id,
        ),
        model="mimo-v2.5",
        authoritative_generation="equivalent-generation",
    )

    assert (v2.status, v2.reason) == (v1.status, v1.reason)
    assert _projection_snapshot(v2_factory, v2_raw_id) == _projection_snapshot(
        v1_factory,
        v1_raw_id,
    )


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
        self.cancel_orders: list[dict] = []
        self.cancel_trigger_orders: list[dict] = []
        self.positions: list[dict] = []
        self.trigger_pending: list[dict] = []
        self.open_orders: list[dict] = []

    def get_ticker_price(self, *, inst_id):
        return 68100.0

    def list_positions(self, *, inst_id=None):
        return list(self.positions)

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.trigger_pending)

    def list_open_orders(self, *, inst_id=None):
        return list(self.open_orders)

    def place_order(self, payload):
        self.orders.append(payload)
        return {
            "code": "0",
            "data": {"ordId": f"order-{len(self.orders)}", "posId": "pos-1"},
        }

    def trigger_order(self, payload):
        self.trigger_orders.append(payload)
        return {"code": "0", "data": {"ordId": f"trigger-{len(self.trigger_orders)}"}}

    def set_position_sltp(self, payload):
        self.protections.append(payload)
        return {"code": "0", "data": {"ordId": "sltp-1"}}

    def cancel_order(self, payload):
        self.cancel_orders.append(payload)
        return {"code": "0", "data": {"ordId": payload.get("ordId")}}

    def cancel_trigger_order(self, payload):
        self.cancel_trigger_orders.append(payload)
        return {"code": "0", "data": {"ordId": payload.get("ordId")}}


def _group_config(*, max_loss_usdt: float = 20.0) -> GroupConfig:
    return GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="equivalence",
                chat_id=88,
                enabled=True,
                trading_mode="auto_trade",
                max_loss_usdt=max_loss_usdt,
                symbol_whitelist=["BTC"],
            )
        ]
    )


def _seed_entry_execution(path, *, payload: dict):
    factory = create_session_factory(path)
    with factory() as session:
        raw = RawMessage(
            chat_id=88,
            message_id=901,
            sender_id=200,
            sender_name="Alice",
            posted_at=NOW,
            text=_source_text("entry"),
            archived_target_group=True,
        )
        session.add(raw)
        session.commit()
        raw_message_id = int(raw.id)
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
    apply_authoritative_mimo_payload(
        factory,
        raw_message_id=raw_message_id,
        payload=payload,
        model="mimo-v2.5",
        authoritative_generation="equivalent-generation",
    )
    return factory, raw_message_id


def _without_dynamic_values(value):
    if isinstance(value, dict):
        return {
            key: _without_dynamic_values(item)
            for key, item in value.items()
            if key not in {"submitted_at", "created_at", "updated_at"}
        }
    if isinstance(value, list):
        return [_without_dynamic_values(item) for item in value]
    return value


def _entry_execution_snapshot(factory, raw_message_id: int) -> dict:
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
        binding = session.query(ExecutionBinding).one()
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.leg_index).all()
        events = session.query(ExecutionEvent).order_by(ExecutionEvent.id).all()
        candidate = session.query(SignalCandidate).one()
        risk_decision = session.query(RecoveryDecisionRecord).one()
    return _without_dynamic_values(
        {
            "outcome": outcome,
            "orders": client.orders,
            "trigger_orders": client.trigger_orders,
            "protections": client.protections,
            "trade_signal": json.loads(trade_signal.payload_json),
            "binding": {
                "strategy_instance_id": binding.strategy_instance_id,
                "symbol": binding.symbol,
                "side": binding.side,
                "status": binding.status,
                "payload": json.loads(binding.payload_json),
            },
            "legs": [
                {
                    "index": leg.leg_index,
                    "purpose": leg.purpose,
                    "kind": leg.order_kind,
                    "status": leg.status,
                    "client_order_id": leg.client_order_id,
                }
                for leg in legs
            ],
            "candidate": {
                "entry": candidate.entry_text,
                "stop_loss": candidate.stop_loss_text,
                "take_profit": candidate.take_profit_text,
            },
            "risk_decision": {
                "action": risk_decision.action,
                "reasons": json.loads(risk_decision.reason_codes_json),
                "entry": risk_decision.entry_range_text,
                "stop_loss": risk_decision.stop_loss_text,
                "max_loss_usdt": risk_decision.max_loss_usdt,
                "review_status": risk_decision.review_status,
            },
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
        }
    )


def test_v2_entry_matches_v1_risk_draft_idempotency_and_fake_requests(tmp_path):
    v1_factory, v1_message_id = _seed_entry_execution(
        tmp_path / "entry-execution-v1.db",
        payload=_legacy_payload("entry"),
    )
    v2_factory, v2_message_id = _seed_entry_execution(
        tmp_path / "entry-execution-v2.db",
        payload=_v2_payload("entry"),
    )

    assert _entry_execution_snapshot(
        v2_factory,
        v2_message_id,
    ) == _entry_execution_snapshot(v1_factory, v1_message_id)


def _run_entry_once(factory, raw_message_id: int):
    client = _FakeDeepcoinClient()
    outcome = auto_process_message_trade_signal(
        factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )
    return outcome, client


def test_v2_and_v1_duplicate_idempotency_replay_without_new_exchange_calls(
    tmp_path,
):
    snapshots = []
    for label, payload in (
        ("v1", _legacy_payload("entry")),
        ("v2", _v2_payload("entry")),
    ):
        factory, raw_message_id = _seed_entry_execution(
            tmp_path / f"duplicate-{label}.db",
            payload=payload,
        )
        first, first_client = _run_entry_once(factory, raw_message_id)
        second, second_client = _run_entry_once(factory, raw_message_id)
        snapshots.append(
            {
                "first": _without_dynamic_values(first),
                "second": _without_dynamic_values(second),
                "first_calls": [
                    first_client.orders,
                    first_client.trigger_orders,
                    first_client.protections,
                ],
                "replay_calls": [
                    second_client.orders,
                    second_client.trigger_orders,
                    second_client.protections,
                ],
            }
        )

    assert snapshots[1] == snapshots[0]
    assert snapshots[0]["replay_calls"] == [[], [], []]


def test_v2_and_v1_defer_identically_while_adjacent_context_is_pending(
    tmp_path,
):
    snapshots = []
    for label, payload in (
        ("v1", _legacy_payload("entry")),
        ("v2", _v2_payload("entry")),
    ):
        factory, raw_message_id = _seed_entry_execution(
            tmp_path / f"deferred-{label}.db",
            payload=payload,
        )
        with factory() as session:
            current = session.get(RawMessage, raw_message_id)
            later = RawMessage(
                chat_id=current.chat_id,
                message_id=current.message_id + 1,
                posted_at=current.posted_at + timedelta(seconds=1),
                text="50% risk",
            )
            session.add(later)
            session.flush()
            session.add(
                MessageEvidenceExtractionClaim(
                    raw_message_id=later.id,
                    input_fingerprint="sha256:later",
                    claim_token="later-active",
                    claimed_at=NOW,
                    lease_expires_at=NOW + timedelta(minutes=5),
                )
            )
            session.commit()
        save_trading_settings(
            factory,
            {"entry_message_assembly_v2_mode": "live"},
        )
        outcome, client = _run_entry_once(factory, raw_message_id)
        snapshots.append(
            {
                "outcome": _without_dynamic_values(outcome),
                "calls": [client.orders, client.trigger_orders, client.protections],
            }
        )

    assert snapshots[1] == snapshots[0]
    assert snapshots[0]["calls"] == [[], [], []]
    assert snapshots[0]["outcome"]["status"] == "in_progress"
    assert snapshots[0]["outcome"]["items"][0]["result"] == {
        "status": "deferred",
        "reason": "adjacent_entry_context_pending",
    }


def _persist_authoritative_decision(factory, *, raw_message_id: int, payload: dict):
    with factory() as session:
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message_id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status=str(payload["recognition_result"]),
                authoritative_payload_json=json.dumps(payload),
                agreement_status="authoritative_only",
                differences_json="[]",
                comparison_status="completed",
            )
        )
        session.commit()


def _seed_full_exit_execution(path, *, payload_builder):
    factory, raw_message_id, lifecycle_id = _seed_projection_database(
        path,
        fixture_name="full_exit",
    )
    with factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:88:900:BTC:long",
            kol_id="group:88",
            chat_id=88,
            message_id=900,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            pos_id="pos-exit",
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                leg_index=0,
                purpose="entry",
                order_kind="market",
                order_id="entry-order",
                pos_id="pos-exit",
                venue="deepcoin",
                attribution_status="verified",
                attribution_evidence_json='{"policy_version":2}',
                status="active",
            )
        )
        session.commit()
    payload = payload_builder("full_exit", lifecycle_id=lifecycle_id)
    apply_authoritative_mimo_payload(
        factory,
        raw_message_id=raw_message_id,
        payload=payload,
        model="mimo-v2.5",
        authoritative_generation="equivalent-generation",
    )
    _persist_authoritative_decision(
        factory,
        raw_message_id=raw_message_id,
        payload=payload,
    )
    save_trading_settings(
        factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "allowed_symbols": ["BTC"],
        },
    )
    return factory, raw_message_id


def _full_exit_execution_snapshot(factory, raw_message_id: int) -> dict:
    client = _FakeDeepcoinClient()
    client.positions = [
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-exit",
            "posSide": "long",
            "pos": "10",
            "avgPx": "68100",
            "mgnMode": "cross",
            "posMode": "split",
        }
    ]
    outcome = auto_process_message_trade_signal(
        factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )
    with factory() as session:
        batch = session.query(StrategyManagementBatch).one()
        lifecycle = session.query(StrategyLifecycle).one()
        binding = session.query(ExecutionBinding).one()
        events = session.query(ExecutionEvent).order_by(ExecutionEvent.id).all()
    return _without_dynamic_values(
        {
            "outcome": outcome,
            "orders": client.orders,
            "batch": {
                "idempotency": batch.idempotency_fingerprint,
                "intent": batch.intent,
                "action": batch.effective_action,
                "status": batch.status,
                "reason": batch.reason_code,
                "target": json.loads(batch.target_snapshot_json),
            },
            "lifecycle": {
                "status": lifecycle.lifecycle_status,
                "management_action": lifecycle.management_action,
            },
            "binding": {
                "status": binding.status,
                "strategy_instance_id": binding.strategy_instance_id,
                "pos_id": binding.pos_id,
            },
            "events": [
                {
                    "action": row.action,
                    "status": row.status,
                    "reason": row.reason,
                    "request": json.loads(row.request_json or "{}"),
                }
                for row in events
            ],
        }
    )


def test_v2_full_exit_matches_v1_batch_identity_and_fake_close_request(tmp_path):
    v1_factory, v1_raw_id = _seed_full_exit_execution(
        tmp_path / "full-exit-v1.db",
        payload_builder=_legacy_payload,
    )
    v2_factory, v2_raw_id = _seed_full_exit_execution(
        tmp_path / "full-exit-v2.db",
        payload_builder=_v2_payload,
    )

    assert _full_exit_execution_snapshot(
        v2_factory,
        v2_raw_id,
    ) == _full_exit_execution_snapshot(v1_factory, v1_raw_id)


def test_v2_and_v1_missing_verified_entry_defer_identically(tmp_path):
    snapshots = []
    for label, payload_builder in (
        ("v1", _legacy_payload),
        ("v2", _v2_payload),
    ):
        factory, raw_message_id, lifecycle_id = _seed_projection_database(
            tmp_path / f"missing-entry-{label}.db",
            fixture_name="full_exit",
        )
        payload = payload_builder("full_exit", lifecycle_id=lifecycle_id)
        apply_authoritative_mimo_payload(
            factory,
            raw_message_id=raw_message_id,
            payload=payload,
            model="mimo-v2.5",
            authoritative_generation="equivalent-generation",
        )
        _persist_authoritative_decision(
            factory,
            raw_message_id=raw_message_id,
            payload=payload,
        )
        save_trading_settings(
            factory,
            {
                "auto_trade_enabled": True,
                "management_execution_mode": "live",
                "allowed_symbols": ["BTC"],
            },
        )
        client = _FakeDeepcoinClient()
        outcome = auto_process_message_trade_signal(
            factory,
            raw_message_id=raw_message_id,
            group_config=_group_config(),
            deepcoin_client=client,
            contract_spec_provider=_ContractSpecs(),
            processed_at=NOW,
        )
        snapshots.append(
            {
                "outcome": _without_dynamic_values(outcome),
                "calls": [client.orders, client.trigger_orders, client.protections],
            }
        )

    assert snapshots[1] == snapshots[0]
    assert snapshots[0]["calls"] == [[], [], []]
    serialized = json.dumps(snapshots[0]["outcome"], sort_keys=True)
    assert "target_strategy_binding_not_visible_yet" in serialized


def _seed_one_verified_position(factory):
    with factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:88:800:ETH:long",
            kol_id="group:88",
            chat_id=88,
            message_id=800,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            status="active",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            pos_id="pos-existing",
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json='{"policy_version":2}',
            status="active",
            last_verified_at=NOW,
        )
        session.add(leg)
        session.flush()
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id="pos-existing",
                instrument_id="ETH-USDT-SWAP",
                side="long",
                order_id="stop-existing",
                purpose="stop_loss",
                trigger_price="3500",
                size_text="0",
                status="verified",
                evidence_source="equivalence_fixture",
                evidence_json="{}",
                last_verified_at=NOW,
            )
        )
        session.commit()


def test_v2_and_v1_position_risk_limit_refuses_before_exchange(tmp_path):
    snapshots = []
    for label, payload in (
        ("v1", _legacy_payload("entry")),
        ("v2", _v2_payload("entry")),
    ):
        factory, raw_message_id = _seed_entry_execution(
            tmp_path / f"position-risk-{label}.db",
            payload=payload,
        )
        save_trading_settings(factory, {"max_concurrent_positions": 1})
        _seed_one_verified_position(factory)
        client = _FakeDeepcoinClient()
        outcome = auto_process_message_trade_signal(
            factory,
            raw_message_id=raw_message_id,
            group_config=_group_config(),
            deepcoin_client=client,
            contract_spec_provider=_ContractSpecs(),
            processed_at=NOW,
        )
        snapshots.append(
            {
                "outcome": _without_dynamic_values(outcome),
                "calls": [client.orders, client.trigger_orders, client.protections],
            }
        )

    assert snapshots[1] == snapshots[0]
    assert snapshots[0]["calls"] == [[], [], []]
    assert "group_position_limit_reached" in json.dumps(
        snapshots[0]["outcome"],
        sort_keys=True,
    )


@pytest.mark.parametrize("payload_builder", (_legacy_payload, _v2_payload))
def test_source_deletion_refusal_makes_zero_exchange_calls(
    tmp_path,
    payload_builder,
):
    factory, raw_message_id = _seed_entry_execution(
        tmp_path / f"deleted-{payload_builder.__name__}.db",
        payload=payload_builder("entry"),
    )
    record_source_message_deleted(factory, chat_id=88, message_id=901)
    client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )

    assert result == {"status": "blocked", "reason": "source_message_deleted"}
    assert client.orders == client.trigger_orders == client.protections == []


@pytest.mark.parametrize("payload_builder", (_legacy_payload, _v2_payload))
def test_disabled_management_refusal_makes_zero_exchange_calls(
    tmp_path,
    payload_builder,
):
    factory, raw_message_id, lifecycle_id = _seed_projection_database(
        tmp_path / f"disabled-{payload_builder.__name__}.db",
        fixture_name="full_exit",
    )
    apply_authoritative_mimo_payload(
        factory,
        raw_message_id=raw_message_id,
        payload=payload_builder("full_exit", lifecycle_id=lifecycle_id),
        model="mimo-v2.5",
        authoritative_generation="equivalent-generation",
    )
    save_trading_settings(
        factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "disabled",
            "allowed_symbols": ["BTC"],
        },
    )
    client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_ContractSpecs(),
        processed_at=NOW,
    )

    assert result["status"] == "completed"
    assert len(result["items"]) == 1
    assert result["items"][0]["status"] == "succeeded"
    assert result["items"][0]["result"] == {
        "status": "skipped",
        "reason": "management_execution_disabled",
    }
    assert client.orders == client.trigger_orders == client.protections == []


@pytest.mark.parametrize("payload_builder", (_legacy_payload, _v2_payload))
def test_target_ambiguity_refuses_before_candidate_or_exchange(
    tmp_path,
    payload_builder,
):
    factory = create_session_factory(
        tmp_path / f"ambiguous-{payload_builder.__name__}.db"
    )
    save_trading_settings(
        factory,
        {
            "multi_instruction_mode": "live",
            "multi_instruction_activation_after_raw_message_id": 0,
        },
    )
    with factory() as session:
        raw = RawMessage(
            chat_id=88,
            message_id=901,
            posted_at=NOW,
            text="close half",
        )
        session.add(raw)
        for message_id in (899, 900):
            session.add(
                StrategyLifecycle(
                    chat_id=88,
                    message_id=message_id,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="entered",
                    signal_at=NOW,
                    entered_at=NOW,
                )
            )
        session.commit()
        raw_id = int(raw.id)

    recognition = apply_authoritative_mimo_payload(
        factory,
        raw_message_id=raw_id,
        payload=payload_builder("partial_exit", lifecycle_id=None),
        model="mimo-v2.5",
        authoritative_generation="equivalent-generation",
    )

    assert recognition.status == "识别失败"
    assert recognition.reason == "MiMo lifecycle event could not be applied safely"
    with factory() as session:
        assert session.query(SignalCandidate).count() == 0
        assert session.query(MessageInstructionItem).count() == 0
