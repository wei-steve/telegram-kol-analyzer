import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from telegram_kol_research.auto_trade_execution import _count_group_effective_positions
from telegram_kol_research.auto_trade_execution import _load_active_execution_binding
from telegram_kol_research.auto_trade_execution import _load_active_execution_bindings
from telegram_kol_research.auto_trade_execution import _extract_partial_close_fraction
from telegram_kol_research.auto_trade_execution import auto_process_message_trade_signal
from telegram_kol_research.auto_trade_execution import disabled_management_message_needs_no_client
from telegram_kol_research.deepcoin_client import DeepcoinDefiniteRejection
from telegram_kol_research.deepcoin_client import DeepcoinClientError
from telegram_kol_research.deepcoin_client import DeepcoinCredentials
from telegram_kol_research.deepcoin_client import DeepcoinRequestOutcomeUnknown
from telegram_kol_research.execution_bindings import ExecutionBindingRecord, ExecutionOrderLegRecord, upsert_execution_binding, upsert_execution_order_leg
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpecLookup
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.message_instruction_items import create_message_instruction_items_in_session
from telegram_kol_research.models import EntryPreamble, EntryStrategyAssembly, EntryStrategyFragment, ExecutionBinding, ExecutionEvent, ExecutionOrderLeg, MediaAsset, MessageEvidenceExtractionClaim, MessageEvidenceVersion, MessageInstructionItem, PositionProtectionLeg, PositionProtectionLedger, RawMessage, RecoveryDecisionRecord, SignalCandidate, StrategyLifecycle, StrategyManagementBatch, TradeSignal, TriggerProtectionIntent, TriggerTakeProfitConvergence
from telegram_kol_research.recovery_live_submit import RecoveryLiveSubmitError
from telegram_kol_research.recovery_live_submit import _trigger_protection_lock_key
from telegram_kol_research.recovery_live_submit import _trigger_protection_request_fingerprint
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.source_message_deletion import record_source_message_deleted


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


class _CapabilityContractSpecProvider:
    def __init__(self, reason):
        self.reason = reason
        self.snapshot = SimpleNamespace(
            source_digest_sha256="a" * 64,
            fetched_at=datetime(2026, 8, 8, 8, 0, tzinfo=UTC),
            expires_at=datetime(2026, 8, 9, 8, 0, tzinfo=UTC),
        )

    def lookup_contract_spec(self, instrument_id):
        spec = None
        state = None
        if self.reason == "available":
            state = "live"
            spec = DeepcoinContractSpec(
                instrument_id=instrument_id,
                contract_value=0.1,
                quantity_step=1,
                min_quantity=1,
                price_tick=0.001,
            )
        elif self.reason == "venue_instrument_not_live":
            state = "suspend"
        return DeepcoinContractSpecLookup(
            instrument_id=instrument_id,
            reason=self.reason,
            venue_state=state,
            contract_spec=spec,
        )

    def get_contract_spec(self, instrument_id):
        return self.lookup_contract_spec(instrument_id).contract_spec


def test_auto_trade_blocks_deleted_source_before_any_exchange_call(tmp_path):
    session_factory = create_session_factory(tmp_path / "deleted-source.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=3428,
            text="ETH long",
            archived_target_group=True,
        )
        session.add(raw)
        session.commit()
        raw_id = raw.id
    record_source_message_deleted(
        session_factory,
        chat_id=100,
        message_id=3428,
    )
    client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_id,
        group_config=GroupConfig(groups=[]),
        deepcoin_client=client,
    )

    assert result == {"status": "blocked", "reason": "source_message_deleted"}
    assert client.orders == []
    assert client.trigger_orders == []


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


class _SequencedProtectionDeepcoinClient(_FakeDeepcoinClient):
    def set_position_sltp(self, protection_payload):
        self.protections.append(protection_payload)
        order_ids = []
        if protection_payload.get("slTriggerPx"):
            order_ids.append(f"sltp-{len(self.trigger_pending) + 1}")
            self.trigger_pending.append(
                {
                    "instId": protection_payload.get("instId"),
                    "ordId": order_ids[-1],
                    "posId": protection_payload.get("posId"),
                    "posSide": protection_payload.get("posSide"),
                    "triggerOrderType": "TPSL",
                    "slTriggerPrice": protection_payload.get("slTriggerPx"),
                    "sz": protection_payload.get("sz", "0"),
                }
            )
        if protection_payload.get("tpTriggerPx"):
            order_ids.append(f"sltp-{len(self.trigger_pending) + 1}")
            self.trigger_pending.append(
                {
                    "instId": protection_payload.get("instId"),
                    "ordId": order_ids[-1],
                    "posId": protection_payload.get("posId"),
                    "posSide": protection_payload.get("posSide"),
                    "triggerOrderType": "TPSL",
                    "tpTriggerPrice": protection_payload.get("tpTriggerPx"),
                    "sz": protection_payload.get("sz", "0"),
                }
            )
        return {"code": "0", "data": {"ordId": order_ids[0]}}


class _TickerForbiddenDeepcoinClient(_FakeDeepcoinClient):
    def __init__(self):
        super().__init__()
        self.ticker_calls = 0

    def get_ticker_price(self, *, inst_id):
        self.ticker_calls += 1
        raise AssertionError("position limit must be checked before ticker access")


class _CombinedProtectionDeepcoinClient(_FakeDeepcoinClient):
    def set_position_sltp(self, protection_payload):
        self.protections.append(protection_payload)
        self.trigger_pending.append(
            {
                "instId": protection_payload.get("instId"),
                "ordId": "combined-sltp-1",
                "posId": protection_payload.get("posId"),
                "posSide": protection_payload.get("posSide"),
                "triggerOrderType": "TPSL",
                "slTriggerPrice": protection_payload.get("slTriggerPx"),
                "tpTriggerPrice": protection_payload.get("tpTriggerPx"),
                "sz": protection_payload.get("sz", "0"),
            }
        )
        return {"code": "0", "data": {"ordId": "combined-sltp-1"}}


def _persist_same_message_instruction_items(
    session_factory, *, management_action="full_exit"
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=903,
            text="close the old BTC strategy and open a new ETH strategy",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        management = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="short",
            event_type="close_signal",
            management_action=management_action,
            parse_source="mimo_authoritative",
            confidence=0.99,
        )
        entry = SignalCandidate(
            raw_message_id=raw.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            entry_text="1580-1590",
            stop_loss_text="1550",
            take_profit_text="1650",
            parse_source="mimo_authoritative",
            confidence=0.99,
        )
        session.add_all([management, entry])
        session.flush()
        create_message_instruction_items_in_session(session, raw_message_id=raw.id)
        identifiers = raw.id, management.id, entry.id
        session.commit()
        return identifiers


@pytest.mark.parametrize(
    ("management_action", "management_execution_mode"),
    [("full_exit", "disabled"), ("hold_update", "live")],
)
def test_same_message_entry_still_requires_client_when_management_needs_no_client(
    tmp_path, management_action, management_execution_mode
):
    session_factory = create_session_factory(tmp_path / "dual-client-gate.db")
    raw_message_id, _, _ = _persist_same_message_instruction_items(
        session_factory,
        management_action=management_action,
    )
    save_trading_settings(
        session_factory,
        {"management_execution_mode": management_execution_mode},
    )

    assert disabled_management_message_needs_no_client(
        session_factory,
        raw_message_id=raw_message_id,
    ) is False


def test_management_failure_does_not_block_same_message_entry(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "independent-failure.db")
    raw_message_id, management_id, entry_id = _persist_same_message_instruction_items(
        session_factory
    )
    calls = []

    def execute_one(*args, instruction_kind, candidate_id, **kwargs):
        calls.append((instruction_kind, candidate_id))
        if instruction_kind == "management":
            raise DeepcoinDefiniteRejection("close rejected")
        return {"status": "submitted", "order_id": "entry-1"}

    import telegram_kol_research.auto_trade_execution as auto_module

    monkeypatch.setattr(
        auto_module,
        "_auto_process_single_message_trade_signal",
        execute_one,
        raising=False,
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
    )

    assert calls == [("management", management_id), ("entry", entry_id)]
    assert [item["instruction_kind"] for item in result["items"]] == [
        "management",
        "entry",
    ]
    assert result["items"][0]["status"] == "failed"
    assert result["items"][0]["reason"] == "close rejected"
    assert result["items"][1]["status"] == "submitted"
    assert result["items"][1]["result"]["order_id"] == "entry-1"


def test_same_message_management_submission_precedes_entry_submission(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "ordered-submission.db")
    raw_message_id, _, _ = _persist_same_message_instruction_items(session_factory)
    call_log = []

    def execute_one(*args, instruction_kind, **kwargs):
        call_log.append(instruction_kind)
        return {"status": "submitted", "kind": instruction_kind}

    import telegram_kol_research.auto_trade_execution as auto_module

    monkeypatch.setattr(
        auto_module,
        "_auto_process_single_message_trade_signal",
        execute_one,
        raising=False,
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
    )

    assert call_log == ["management", "entry"]
    assert [item["status"] for item in result["items"]] == [
        "submitted",
        "submitted",
    ]


def test_unknown_management_submission_does_not_retry_or_block_same_message_entry(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "unknown-submission.db")
    raw_message_id, _, _ = _persist_same_message_instruction_items(session_factory)
    call_counts = {"management": 0, "entry": 0}

    def execute_one(*args, instruction_kind, **kwargs):
        call_counts[instruction_kind] += 1
        if instruction_kind == "management":
            raise DeepcoinRequestOutcomeUnknown("close submission timed out")
        return {"status": "submitted", "order_id": "entry-1"}

    import telegram_kol_research.auto_trade_execution as auto_module

    monkeypatch.setattr(
        auto_module,
        "_auto_process_single_message_trade_signal",
        execute_one,
        raising=False,
    )

    first = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
    )
    second = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
    )

    assert call_counts == {"management": 1, "entry": 1}
    assert [item["status"] for item in first["items"]] == ["unknown", "submitted"]
    assert second == first


def test_retired_instruction_set_never_falls_back_to_candidate_execution(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "retired-items.db")
    raw_message_id, _, _ = _persist_same_message_instruction_items(session_factory)
    with session_factory() as session:
        for item in session.query(MessageInstructionItem).all():
            item.retired_at = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        session.commit()

    import telegram_kol_research.auto_trade_execution as auto_module

    monkeypatch.setattr(
        auto_module,
        "_auto_process_single_message_trade_signal",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("retired durable items must block legacy fallback")
        ),
    )

    assert auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
    ) == {"status": "completed", "items": []}


def test_management_recovery_required_is_unknown_and_does_not_block_same_message_entry(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "recovery-required.db")
    raw_message_id, _, _ = _persist_same_message_instruction_items(session_factory)
    call_log = []

    def execute_one(*args, instruction_kind, **kwargs):
        call_log.append(instruction_kind)
        if instruction_kind == "management":
            return {
                "status": "recovery_required",
                "reason": "protection_recovery_required",
                "legs": [{"status": "recovery_required"}],
            }
        return {"status": "submitted", "order_id": "entry-1"}

    import telegram_kol_research.auto_trade_execution as auto_module

    monkeypatch.setattr(
        auto_module,
        "_auto_process_single_message_trade_signal",
        execute_one,
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
    )

    assert call_log == ["management", "entry"]
    assert [item["status"] for item in result["items"]] == ["unknown", "submitted"]
    assert result["items"][0]["reason"] == "protection_recovery_required"


def test_hold_update_is_informational_and_needs_no_deepcoin_client(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=902,
            text="继续持有",
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
                management_action="hold_update",
                recognition_generation="hold-generation",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        raw_id = raw.id
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
        },
    )

    assert disabled_management_message_needs_no_client(
        session_factory, raw_message_id=raw_id
    ) is True
    assert auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_id,
        group_config=GroupConfig(groups=[]),
        deepcoin_client=None,
    ) == {"status": "skipped", "reason": "management_intent_informational"}

    with session_factory() as session:
        event = session.query(ExecutionEvent).one()
        assert event.status == "skipped"
        assert event.reason == "management_intent_informational"
        assert session.query(StrategyManagementBatch).count() == 0


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


def _seed_verified_positions(session_factory, *, chat_id: int, count: int) -> None:
    for index in range(1, count + 1):
        binding_id = upsert_execution_binding(
            session_factory,
            ExecutionBindingRecord(
                kol_id=f"group:{chat_id}",
                chat_id=chat_id,
                message_id=1000 + index,
                symbol="BTC",
                side="long",
                venue="deepcoin",
            ),
        )
        _verify_bound_position(
            session_factory,
            binding_id=binding_id,
            pos_id=f"pos-{chat_id}-{index}",
        )
        with session_factory() as session:
            leg = session.query(ExecutionOrderLeg).filter_by(
                execution_binding_id=binding_id
            ).one()
            session.add(PositionProtectionLedger(
                venue="deepcoin", execution_binding_id=binding_id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=f"deepcoin:{chat_id}:{1000 + index}:BTC:long",
                pos_id=f"pos-{chat_id}-{index}", instrument_id="BTC-USDT-SWAP",
                side="long", order_id=f"stop-{chat_id}-{index}",
                purpose="stop_loss", trigger_price="67000", size_text="0",
                status="verified", evidence_source="test_fixture", evidence_json="{}",
                last_verified_at=datetime.now(UTC),
            ))
            session.commit()


def test_count_group_effective_positions_uses_distinct_verified_active_entry_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    def add_leg(
        *,
        chat_id: int,
        message_id: int,
        pos_id: str | None,
        venue: str = "deepcoin",
        purpose: str = "entry",
        status: str = "active",
        attribution_status: str = "verified",
    ) -> None:
        binding_id = upsert_execution_binding(
            session_factory,
            ExecutionBindingRecord(
                kol_id=f"group:{chat_id}",
                chat_id=chat_id,
                message_id=message_id,
                symbol="BTC",
                side="long",
                venue=venue,
            ),
        )
        upsert_execution_order_leg(
            session_factory,
            ExecutionOrderLegRecord(
                execution_binding_id=binding_id,
                leg_index=1,
                purpose=purpose,
                order_kind="market",
                pos_id=pos_id,
                venue=venue,
                status=status,
                attribution_status=attribution_status,
            ),
        )

    for message_id, pos_id in enumerate(
        ["pos-100-1", "pos-100-2", "pos-100-3", "pos-100-4"], start=1
    ):
        add_leg(chat_id=100, message_id=message_id, pos_id=pos_id)

    add_leg(
        chat_id=100,
        message_id=10,
        pos_id="pos-unassigned",
        attribution_status="unassigned",
    )
    add_leg(chat_id=100, message_id=11, pos_id="pos-terminal", status="closed")
    add_leg(chat_id=100, message_id=12, pos_id="pos-protection", purpose="protection")
    add_leg(chat_id=100, message_id=13, pos_id="")
    add_leg(chat_id=100, message_id=14, pos_id="pos-other-venue", venue="other")
    add_leg(chat_id=200, message_id=20, pos_id="pos-200-1")

    assert _count_group_effective_positions(session_factory, chat_id=100) == 4
    assert _count_group_effective_positions(session_factory, chat_id=200) == 1


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
    parse_source=None,
    chat_id=100,
    message_id=55,
):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
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
                parse_source=parse_source or ("mimo_direct" if with_media else "text_ai"),
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


def _persist_half_risk_preamble_before(session_factory, *, strategy_raw_message_id):
    from decimal import Decimal

    from telegram_kol_research.entry_preambles import persist_entry_preamble_in_session
    from telegram_kol_research.message_evidence import EntryPreambleEvidence

    with session_factory() as session:
        strategy = session.get(RawMessage, strategy_raw_message_id)
        preamble_message = RawMessage(
            chat_id=strategy.chat_id,
            message_id=strategy.message_id - 1,
            sender_id=strategy.sender_id,
            sender_name=strategy.sender_name,
            posted_at=strategy.posted_at - timedelta(minutes=1),
            text="BTC换手入场做空，半仓操作做个短线空单。",
            archived_target_group=True,
        )
        session.add(preamble_message)
        session.flush()
        evidence = MessageEvidenceVersion(
            raw_message_id=preamble_message.id,
            version=1,
            input_fingerprint=f"sha256:preamble:{strategy_raw_message_id}",
            model="mimo-v2.5",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=0.96,
            text_evidence_json="{}",
            image_evidence_json='{"images":[]}',
            normalized_evidence_json="{}",
        )
        session.add(evidence)
        session.flush()
        preamble = persist_entry_preamble_in_session(
            session,
            raw_message=preamble_message,
            evidence_version_id=evidence.id,
            recognition_generation="generation-half-risk",
            evidence=EntryPreambleEvidence(
                symbol="BTC",
                side="short",
                risk_multiplier=Decimal("0.5"),
                confidence=0.96,
                reason="半仓操作",
            ),
            now=datetime(2026, 6, 12, 7, 59, tzinfo=UTC),
        )
        session.commit()
        return preamble.id


@pytest.mark.parametrize("chat_id", [100, 200])
def test_live_entry_preamble_multiplies_usdt_risk_before_contract_sizing(tmp_path, chat_id):
    session_factory = create_session_factory(tmp_path / f"half-risk-live-{chat_id}.db")
    strategy_raw_id = _persist_candidate(
        session_factory,
        text="BTC short 63900-64200 SL 64900 TP 62800",
        entry_text="63900-64200",
        stop_loss_text="64900",
        take_profit_text="62800",
        symbol="BTC",
        side="short",
        chat_id=chat_id,
    )
    preamble_id = _persist_half_risk_preamble_before(
        session_factory, strategy_raw_message_id=strategy_raw_id
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
            "entry_preamble_mode": "live",
        },
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=strategy_raw_id,
        group_config=_group_config(chat_id=chat_id),
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 5, 12, 2, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert result["entry_preamble_assembly"]["configured_risk_budget_usdt"] == 20.0
    assert result["entry_preamble_assembly"]["risk_multiplier"] == "0.5"
    assert result["entry_preamble_assembly"]["effective_risk_budget_usdt"] == 10.0
    with session_factory() as session:
        decision = session.query(RecoveryDecisionRecord).one()
        binding = session.query(ExecutionBinding).one()
        preamble = session.get(EntryPreamble, preamble_id)
        assert decision.max_loss_usdt == 10.0
        assert preamble.status == "consumed"
        assert session.query(EntryStrategyAssembly).count() == 1
        binding_payload = json.loads(binding.payload_json)
    draft = binding_payload["draft"]
    assert draft["risk_budget_usdt"] == 10.0
    assert sum(
        leg["estimated_stop_loss_usdt"] for leg in draft["order_legs"]
    ) <= 10.0
    assert draft["entry_preamble_assembly"]["preamble_message_id"] == 54


def test_live_adjacent_admission_defers_before_exchange_or_trade_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "adjacent-defer.db")
    strategy_raw_id = _persist_candidate(
        session_factory,
        text="BTC short 63900-64200 SL 64900 TP 62800",
        entry_text="63900-64200",
        stop_loss_text="64900",
        take_profit_text="62800",
        symbol="BTC",
        side="short",
    )
    with session_factory() as session:
        strategy = session.get(RawMessage, strategy_raw_id)
        later = RawMessage(
            chat_id=strategy.chat_id,
            message_id=strategy.message_id + 1,
            posted_at=strategy.posted_at + timedelta(seconds=1),
            text="50%仓位",
        )
        session.add(later)
        session.flush()
        session.add(
            MessageEvidenceExtractionClaim(
                raw_message_id=later.id,
                input_fingerprint="later",
                claim_token="later-active",
                claimed_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
                lease_expires_at=datetime(2026, 8, 5, 12, 5, tzinfo=UTC),
            )
        )
        session.commit()
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "allowed_symbols": ["BTC"],
            "entry_message_assembly_v2_mode": "live",
        },
    )
    client = _TickerForbiddenDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=strategy_raw_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 5, 12, 2, tzinfo=UTC),
    )

    assert result == {
        "status": "deferred",
        "reason": "adjacent_entry_context_pending",
    }
    assert client.ticker_calls == 0
    with session_factory() as session:
        assert session.query(TradeSignal).count() == 0


def test_v2_shadow_proposal_preserves_legacy_blocking_decision():
    from telegram_kol_research.auto_trade_execution import (
        _overlay_v2_shadow_proposal,
    )
    from telegram_kol_research.entry_strategy_assembly import EntryAssemblyResult

    legacy = EntryAssemblyResult(
        status="blocked",
        reason_code="entry_preamble_ambiguous",
        mode="live",
        proposed_risk_multiplier=Decimal("1"),
        effective_risk_multiplier=Decimal("1"),
    )
    proposal = EntryAssemblyResult(
        status="proposed",
        reason_code=None,
        mode="shadow",
        proposed_risk_multiplier=Decimal("0.5"),
        effective_risk_multiplier=Decimal("1"),
        fragment_ids=(11,),
    )

    merged = _overlay_v2_shadow_proposal(
        legacy_assembly=legacy,
        v2_proposal=proposal,
    )

    assert merged.status == "blocked"
    assert merged.reason_code == "entry_preamble_ambiguous"
    assert merged.proposed_risk_multiplier == Decimal("0.5")
    assert merged.effective_risk_multiplier == Decimal("1")


def test_shadow_entry_preamble_reports_half_but_executes_configured_risk(tmp_path):
    session_factory = create_session_factory(tmp_path / "half-risk-shadow.db")
    strategy_raw_id = _persist_candidate(
        session_factory,
        text="BTC short 63900-64200 SL 64900 TP 62800",
        entry_text="63900-64200",
        stop_loss_text="64900",
        take_profit_text="62800",
        symbol="BTC",
        side="short",
    )
    preamble_id = _persist_half_risk_preamble_before(
        session_factory, strategy_raw_message_id=strategy_raw_id
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
            "entry_preamble_mode": "shadow",
        },
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=strategy_raw_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 5, 12, 2, tzinfo=UTC),
    )

    assert result["entry_preamble_assembly"]["risk_multiplier"] == "0.5"
    assert result["entry_preamble_assembly"]["effective_risk_budget_usdt"] == 20.0
    with session_factory() as session:
        assert session.query(RecoveryDecisionRecord).one().max_loss_usdt == 20.0
        assert session.get(EntryPreamble, preamble_id).status == "pending"


def test_live_v2_fragment_applies_half_budget_and_supplemental_leg_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "v2-risk-live.db")
    strategy_raw_id = _persist_candidate(
        session_factory,
        text="BTC short 63900-64200 SL 64900 TP 62800",
        entry_text="63900-64200",
        stop_loss_text="64900",
        take_profit_text="62800",
        symbol="BTC",
        side="short",
    )
    with session_factory() as session:
        strategy = session.get(RawMessage, strategy_raw_id)
        context = RawMessage(
            chat_id=strategy.chat_id,
            message_id=strategy.message_id + 1,
            posted_at=strategy.posted_at + timedelta(seconds=1),
            text="50%仓位，补仓63400",
        )
        session.add(context)
        session.flush()
        evidence = MessageEvidenceVersion(
            raw_message_id=context.id,
            version=1,
            input_fingerprint="v2-context",
            model="mimo",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=1,
            text_evidence_json="{}",
            image_evidence_json="{}",
            normalized_evidence_json="{}",
        )
        session.add(evidence)
        session.flush()
        session.add_all(
            [
                EntryStrategyFragment(
                    raw_message_id=context.id,
                    chat_id=context.chat_id,
                    message_id=context.message_id,
                    symbol="BTC",
                    side="short",
                    fragment_kind="risk_multiplier",
                    payload_json='{"risk_multiplier":"0.5"}',
                    evidence_version_id=evidence.id,
                    recognition_generation="v2-g",
                    source_relationship="unresolved",
                    status="pending",
                    reason="50%",
                    fingerprint="a" * 64,
                    created_at=context.posted_at,
                    updated_at=context.posted_at,
                ),
                EntryStrategyFragment(
                    raw_message_id=context.id,
                    chat_id=context.chat_id,
                    message_id=context.message_id,
                    symbol="BTC",
                    side="short",
                    fragment_kind="supplemental_entry",
                    payload_json='{"entry_price":"63400"}',
                    evidence_version_id=evidence.id,
                    recognition_generation="v2-g",
                    source_relationship="unresolved",
                    status="pending",
                    reason="补仓",
                    fingerprint="b" * 64,
                    created_at=context.posted_at,
                    updated_at=context.posted_at,
                ),
            ]
        )
        session.commit()
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
            "entry_message_assembly_v2_mode": "live",
        },
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=strategy_raw_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 5, 12, 2, tzinfo=UTC),
    )

    evidence_payload = result["entry_preamble_assembly"]
    assert evidence_payload["strategy_risk_multiplier"] == "0.5"
    assert evidence_payload["effective_risk_budget_usdt"] == 10.0
    with session_factory() as session:
        binding_payload = json.loads(session.query(ExecutionBinding).one().payload_json)
        assembly_evidence = json.loads(
            session.query(EntryStrategyAssembly).one().evidence_json
        )
    draft = binding_payload["draft"]
    assert [leg["price"] for leg in draft["order_legs"]] == [63810.0, 64110.0, 63400.0]
    assert sum(
        Decimal(str(leg["estimated_stop_loss_usdt"]))
        for leg in draft["order_legs"]
    ) <= Decimal("10")
    assert assembly_evidence["configured_risk_budget_usdt"] == "20.0"
    assert assembly_evidence["effective_risk_budget_usdt"] == "10.00"
    assert assembly_evidence["final_entry_leg_count"] == 3
    assert len(assembly_evidence["order_draft_snapshot"]["order_legs"]) == 3


def test_invalid_persisted_entry_preamble_multiplier_blocks_before_trade_signal(tmp_path):
    session_factory = create_session_factory(tmp_path / "invalid-risk.db")
    strategy_raw_id = _persist_candidate(
        session_factory,
        text="BTC short 63900-64200 SL 64900 TP 62800",
        entry_text="63900-64200",
        stop_loss_text="64900",
        take_profit_text="62800",
        symbol="BTC",
        side="short",
    )
    preamble_id = _persist_half_risk_preamble_before(
        session_factory, strategy_raw_message_id=strategy_raw_id
    )
    with session_factory() as session:
        session.get(EntryPreamble, preamble_id).risk_multiplier = "1.1"
        session.commit()
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
            "entry_preamble_mode": "live",
        },
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=strategy_raw_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 5, 12, 2, tzinfo=UTC),
    )

    assert result == {
        "status": "blocked",
        "reason": "entry_preamble_multiplier_invalid",
    }
    with session_factory() as session:
        assert session.query(TradeSignal).count() == 0
        assert session.query(RecoveryDecisionRecord).count() == 0


def test_auto_process_skips_symbol_price_scale_review_candidate_before_exchange_access(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="BTC 空单，进场 1840-1860，止损 1905，止盈 1780/1720",
        entry_text="1840-1860",
        stop_loss_text="1905",
        take_profit_text="1780/1720",
        symbol="ETH",
        side="short",
        confidence=0.69,
        parse_source="mimo_symbol_review",
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=None,
    )

    assert result == {
        "status": "skipped",
        "reason": "symbol_price_scale_conflict_review_required",
    }


def _group_config(*, chat_id=100):
    return GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="Auto Group",
                chat_id=chat_id,
                enabled=True,
                trading_mode="auto_trade",
                max_loss_usdt=20.0,
                symbol_whitelist=["BTC", "ETH"],
            )
        ]
    )


def test_group_position_limit_blocks_entry_before_exchange_access(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "max_concurrent_positions": 4,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    _seed_verified_positions(session_factory, chat_id=100, count=4)
    fake_client = _TickerForbiddenDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 16, 8, 1, tzinfo=UTC),
    )

    assert result == {
        "status": "skipped",
        "reason": "group_position_limit_reached",
        "current_position_count": 4,
        "max_concurrent_positions": 4,
    }
    assert fake_client.ticker_calls == 0
    assert fake_client.orders == []
    assert fake_client.trigger_orders == []
    with session_factory() as session:
        event = session.query(ExecutionEvent).one()
        assert event.reason == "group_position_limit_reached"
        payload = json.loads(event.request_json)
        assert payload["current_position_count"] == 4
        assert payload["max_concurrent_positions"] == 4


def test_new_entry_is_blocked_by_critical_unprotected_position_in_same_chat(tmp_path):
    session_factory = create_session_factory(tmp_path / "critical-entry-gate.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(session_factory, {
        "auto_trade_enabled": True, "allowed_symbols": ["BTC", "ETH"],
    })
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            strategy_instance_id="deepcoin:100:54:ETH:long", kol_id="group:100",
            chat_id=100, message_id=54, symbol="ETH", side="long",
            venue="deepcoin", status="active",
        ),
    )
    _verify_bound_position(session_factory, binding_id=binding_id, pos_id="pos-naked")
    client = _TickerForbiddenDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory, raw_message_id=raw_message_id, group_config=_group_config(),
        deepcoin_client=client, contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 3, 8, 1, tzinfo=UTC),
    )

    assert result == {
        "status": "blocked",
        "reason": "critical_unprotected_position_in_chat",
        "pos_ids": ["pos-naked"],
    }
    assert client.ticker_calls == 0


def test_critical_unprotected_position_does_not_block_other_chat_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "critical-entry-other-chat.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(session_factory, {
        "auto_trade_enabled": True,
        "allowed_symbols": ["BTC", "ETH"],
        "symbol_entry_thresholds": {"BTC": {
            "market_leg_threshold": "0", "first_limit_offset": "0", "second_limit_offset": "0",
        }},
    })
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            strategy_instance_id="deepcoin:200:54:ETH:long", kol_id="group:200",
            chat_id=200, message_id=54, symbol="ETH", side="long",
            venue="deepcoin", status="active",
        ),
    )
    _verify_bound_position(session_factory, binding_id=binding_id, pos_id="pos-other-chat-naked")
    client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory, raw_message_id=raw_message_id, group_config=_group_config(),
        deepcoin_client=client, contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 3, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert len(client.trigger_orders) == 2


def test_group_position_limit_isolated_by_chat_below_boundary_reaches_submission(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "max_concurrent_positions": 4,
            "allowed_symbols": ["BTC", "ETH"],
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "0",
                    "first_limit_offset": "0",
                    "second_limit_offset": "0",
                }
            },
        },
    )
    _seed_verified_positions(session_factory, chat_id=100, count=3)
    _seed_verified_positions(session_factory, chat_id=200, count=4)
    fake_client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 16, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert fake_client.orders == []
    assert len(fake_client.trigger_orders) == 2


def test_management_disabled_records_safe_skip_before_planning_or_exchange_access(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=901,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 7, 15, 8, 0, tzinfo=UTC),
            text="BTC all exit",
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
                recognition_generation="disabled-generation",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        raw_message_id = raw.id
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "disabled",
            "allowed_symbols": ["BTC"],
        },
    )
    import telegram_kol_research.auto_trade_execution as auto_module

    monkeypatch.setattr(
        auto_module,
        "plan_strategy_management_batch",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("disabled management must not plan")
        ),
    )
    client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
    )

    assert result == {"status": "skipped", "reason": "management_execution_disabled"}
    with session_factory() as session:
        event = session.query(ExecutionEvent).one()
        assert event.action == "management_auto_trade_skipped"
        assert event.status == "skipped"
        assert event.chat_id == 100
        assert event.message_id == 901
        assert event.reason == "management_execution_disabled"
        assert session.query(TradeSignal).count() == 0
    assert client.orders == []
    assert client.trigger_orders == []
    assert client.protections == []


@pytest.mark.parametrize(
    ("mode", "auto_trade_enabled", "expected_status", "expected_reason"),
    [
        ("shadow", False, "shadow_planned", None),
        ("live", True, "reconciling", "close_submissions_pending_reconciliation"),
    ],
)
def test_management_planning_shadows_or_executes_only_the_durable_batch(
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
        session.add(
            SignalCandidate(
                raw_message_id=management.id,
                symbol="ETH",
                side="long",
                event_type="entry_signal",
                parse_source="mimo_authoritative",
                confidence=1.0,
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
        from telegram_kol_research.strategy_management_batches import (
            load_management_batch,
        )
        from telegram_kol_research.strategy_management_worker import (
            run_strategy_management_worker_tick,
        )

        shadow_batch = load_management_batch(session_factory, result["batch_id"])
        assert shadow_batch.status == "blocked"
        assert shadow_batch.reason_code == "management_shadow_plan_only"
        assert shadow_batch.completed_at is not None

        save_trading_settings(
            session_factory,
            {
                "auto_trade_enabled": True,
                "management_execution_mode": "live",
                "allowed_symbols": ["BTC", "ETH"],
            },
        )
        background_writes = []
        tick = run_strategy_management_worker_tick(
            session_factory,
            deepcoin_client_factory=lambda: fake_client,
            executor=lambda *_args, **_kwargs: background_writes.append("execute"),
            processed_at=datetime(2026, 7, 15, 8, 2, tzinfo=UTC),
        )
        assert tick.discovered == 0
        assert background_writes == []
    elif mode == "live":
        assert result["reason"] == expected_reason
        from telegram_kol_research.models import StrategyManagementBatch

        with session_factory() as session:
            batch = session.get(StrategyManagementBatch, result["batch_id"])
            assert batch.status == "reconciling"
            assert batch.reason_code == "close_submissions_pending_reconciliation"
    assert isinstance(result["batch_id"], int)
    assert len(fake_client.orders) == (1 if mode == "live" else 0)
    if mode == "live":
        assert fake_client.orders[0]["closePosId"] == "pos-shadow"
        assert fake_client.orders[0]["ordType"] == "market"
        assert len(fake_client.orders[0]["clOrdId"]) <= 20
    assert fake_client.trigger_orders == []
    assert fake_client.protections == []
    assert fake_client.cancel_orders == []
    assert fake_client.cancel_trigger_orders == []
    with session_factory() as session:
        assert session.query(TradeSignal).count() == 0


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
            "entry_message_assembly_v2_mode": "live",
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "50",
                    "first_limit_offset": "90",
                    "second_limit_offset": "80",
                }
            },
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
    with session_factory() as session:
        assembly_evidence = json.loads(
            session.query(EntryStrategyAssembly).one().evidence_json
        )
    assert assembly_evidence["order_draft_snapshot"]["contract_spec"][
        "quantity_step"
    ] == 1.0
    assert [order["triggerPrice"] for order in fake_client.trigger_orders] == [
        "68290.0",
        "68080.0",
    ]
    assert all(not any(key.startswith("tp") for key in order) for order in fake_client.trigger_orders)
    assert fake_client.trigger_orders[0]["slTriggerPx"] == "67500.0"
    assert fake_client.protections == []
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        assert session.query(TriggerProtectionIntent).count() == 2
    assert binding.strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert binding.margin_mode == "cross"
    assert binding.position_mode == "split"


@pytest.mark.parametrize(
    "capability_reason",
    [
        "venue_instrument_unsupported",
        "venue_instrument_not_live",
        "contract_spec_missing",
        "contract_spec_invalid",
        "contract_spec_stale",
        "contract_spec_sync_unavailable",
    ],
)
def test_auto_entry_capability_rejection_precedes_every_durable_or_exchange_write(
    tmp_path, capability_reason
):
    session_factory = create_session_factory(tmp_path / f"{capability_reason}.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    client = _FakeDeepcoinClient()

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_CapabilityContractSpecProvider(capability_reason),
        processed_at=datetime(2026, 8, 8, 9, 0, tzinfo=UTC),
    )

    assert result["status"] in {"skipped", "blocked"}
    assert result["reason"] == capability_reason
    assert client.orders == []
    assert client.trigger_orders == []
    assert client.protections == []
    with session_factory() as session:
        assert session.query(TradeSignal).count() == 0
        assert session.query(ExecutionBinding).count() == 0


def test_auto_entry_global_allowlist_precedes_venue_capability(tmp_path):
    session_factory = create_session_factory(tmp_path / "global-first.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "allowed_symbols": ["ETH"]},
    )
    provider = _CapabilityContractSpecProvider("contract_spec_sync_unavailable")

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=provider,
        processed_at=datetime(2026, 8, 8, 9, 0, tzinfo=UTC),
    )

    assert result["reason"] == "symbol_not_allowed"


def test_auto_entry_embeds_exact_dynamic_sol_spec_and_snapshot_digest(tmp_path):
    session_factory = create_session_factory(tmp_path / "dynamic-sol.db")
    raw_message_id = _persist_candidate(
        session_factory,
        symbol="SOL",
        text="SOL long 150-152 SL 145 TP 160/170",
        entry_text="150-152",
        stop_loss_text="145",
        take_profit_text="160 / 170",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "allowed_symbols": ["BTC", "ETH", "SOL"],
        },
    )
    group_config = GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="Auto Group",
                chat_id=100,
                enabled=True,
                trading_mode="auto_trade",
                max_loss_usdt=20.0,
                symbol_whitelist=["BTC", "ETH", "SOL"],
            )
        ]
    )
    client = _FakeDeepcoinClient()
    client.ticker_prices["SOL-USDT-SWAP"] = 151.0

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=group_config,
        deepcoin_client=client,
        contract_spec_provider=_CapabilityContractSpecProvider("available"),
        processed_at=datetime(2026, 8, 8, 9, 0, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    with session_factory() as session:
        binding_payload = json.loads(session.query(ExecutionBinding).one().payload_json)
    draft = binding_payload["draft"]
    assert draft["contract_spec"] == {
        "instrument_id": "SOL-USDT-SWAP",
        "contract_value": 0.1,
        "quantity_step": 1.0,
        "min_quantity": 1.0,
        "price_tick": 0.001,
    }
    assert draft["contract_spec_snapshot"] == {
        "source_digest_sha256": "a" * 64,
        "fetched_at": "2026-08-08T08:00:00+00:00",
        "expires_at": "2026-08-09T08:00:00+00:00",
    }


def test_recovery_trigger_synchronizes_finalized_fingerprint_before_exchange_submission(
    tmp_path,
):
    session_factory = create_session_factory(
        tmp_path / "recovery-trigger-fingerprint.db"
    )
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
            "entry_message_assembly_v2_mode": "live",
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "50",
                    "first_limit_offset": "90",
                    "second_limit_offset": "80",
                }
            },
        },
    )

    class _FingerprintInspectingClient(_FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.inspected_first_submission = False

        def trigger_order(self, order_payload):
            if not self.inspected_first_submission:
                with session_factory() as session:
                    assembly = session.query(EntryStrategyAssembly).one()
                    signal_payload = json.loads(
                        session.query(TradeSignal).one().payload_json
                    )
                assert assembly.fingerprint == signal_payload[
                    "entry_preamble_assembly"
                ]["assembly_fingerprint"]
                assert assembly.fingerprint == signal_payload[
                    "deepcoin_order_draft"
                ]["entry_preamble_assembly"]["assembly_fingerprint"]
                self.inspected_first_submission = True
            return super().trigger_order(order_payload)

    client = _FingerprintInspectingClient()
    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 8, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert client.inspected_first_submission is True
    with session_factory() as session:
        assembly = session.query(EntryStrategyAssembly).one()
        binding_payload = json.loads(
            session.query(ExecutionBinding).one().payload_json
        )
    assert assembly.fingerprint == binding_payload["draft"][
        "entry_preamble_assembly"
    ]["assembly_fingerprint"]
    assert result["entry_preamble_assembly"]["assembly_fingerprint"] == (
        assembly.fingerprint
    )


def test_partial_v2_entry_submission_is_quarantined_and_never_reenqueued(
    tmp_path,
):
    from telegram_kol_research.recovery_live_submit import process_trade_signal_live
    from telegram_kol_research.trade_signals import load_or_create_trade_signal

    session_factory = create_session_factory(tmp_path / "partial-v2-entry.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
            "entry_message_assembly_v2_mode": "live",
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "50",
                    "first_limit_offset": "90",
                    "second_limit_offset": "80",
                }
            },
        },
    )

    class _SecondLegUnknownClient(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            self.trigger_orders.append(order_payload)
            if len(self.trigger_orders) >= 2:
                raise DeepcoinRequestOutcomeUnknown("second leg outcome unknown")
            return {"code": "0", "data": {"ordId": "trigger-1"}}

    client = _SecondLegUnknownClient()
    with pytest.raises(DeepcoinRequestOutcomeUnknown):
        auto_process_message_trade_signal(
            session_factory,
            raw_message_id=raw_message_id,
            group_config=_group_config(),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 8, 8, 1, tzinfo=UTC),
        )
    with session_factory() as session:
        signal = session.query(TradeSignal).one()
        original_payload_json = signal.payload_json
        assert signal.status == "partial_submission_failed"
        assert signal.last_error == "second leg outcome unknown"

    with pytest.raises(LookupError, match="recovery execution item not found"):
        auto_process_message_trade_signal(
            session_factory,
            raw_message_id=raw_message_id,
            group_config=_group_config(),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 8, 8, 2, tzinfo=UTC),
        )

    reused = load_or_create_trade_signal(
        session_factory,
        venue="deepcoin",
        source_type="recovery",
        kol_id="changed",
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        action="open_position",
        payload={"unsafe_retry_payload": True},
        strategy_instance_id="deepcoin:100:55:BTC:long",
    )
    assert reused.status == "partial_submission_failed"
    with pytest.raises(
        RecoveryLiveSubmitError,
        match="^trade_signal_claim_failed:partial_submission_failed$",
    ):
        process_trade_signal_live(
            session_factory,
            signal_id=reused.id,
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
        )

    assert len(client.trigger_orders) == 2
    with session_factory() as session:
        signal = session.query(TradeSignal).one()
        assert signal.status == "partial_submission_failed"
        assert signal.last_error == "second leg outcome unknown"
        assert signal.payload_json == original_payload_json


def test_auto_market_draft_uses_immutable_signal_enqueue(tmp_path, monkeypatch):
    import telegram_kol_research.auto_trade_execution as auto_module
    from telegram_kol_research.trade_signals import load_or_create_trade_signal

    session_factory = create_session_factory(tmp_path / "auto-market-immutable.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="BTC long market SL 67500 TP 69000/70000",
        entry_text="market",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
            "entry_message_assembly_v2_mode": "live",
        },
    )
    immutable_enqueue_calls = []

    def record_immutable_enqueue(*args, **kwargs):
        immutable_enqueue_calls.append(kwargs)
        return load_or_create_trade_signal(*args, **kwargs)

    monkeypatch.setattr(
        auto_module,
        "load_or_create_trade_signal",
        record_immutable_enqueue,
        raising=False,
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 8, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert len(immutable_enqueue_calls) == 1


def test_recovery_trigger_fingerprint_sync_failure_blocks_exchange_submission(
    tmp_path, monkeypatch
):
    from telegram_kol_research.trade_signals import (
        TradeSignalFingerprintSyncError,
    )

    session_factory = create_session_factory(
        tmp_path / "recovery-trigger-fingerprint-sync-failure.db"
    )
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
            "entry_message_assembly_v2_mode": "live",
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "50",
                    "first_limit_offset": "90",
                    "second_limit_offset": "80",
                }
            },
        },
    )
    client = _FakeDeepcoinClient()
    import telegram_kol_research.auto_trade_execution as auto_module

    import telegram_kol_research.trade_signals as trade_signals_module

    real_sync = trade_signals_module.synchronize_pending_entry_assembly_evidence
    sync_calls = []

    def fail_first_sync(*args, **kwargs):
        sync_calls.append(kwargs["signal_id"])
        if len(sync_calls) == 1:
            raise TradeSignalFingerprintSyncError(
                "entry_assembly_signal_cas_failed"
            )
        return real_sync(*args, **kwargs)

    monkeypatch.setattr(
        auto_module,
        "synchronize_pending_entry_assembly_evidence",
        fail_first_sync,
    )

    with pytest.raises(
        TradeSignalFingerprintSyncError,
        match="entry_assembly_signal_cas_failed",
    ):
        auto_process_message_trade_signal(
            session_factory,
            raw_message_id=raw_message_id,
            group_config=_group_config(),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 8, 8, 8, 1, tzinfo=UTC),
        )

    assert client.orders == []
    assert client.trigger_orders == []
    with session_factory() as session:
        assert session.query(TradeSignal).one().status == "pending"

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 8, 8, 8, 2, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert len(sync_calls) == 2
    assert client.orders == []
    assert len(client.trigger_orders) == 2


def test_trigger_limit_entry_persists_tpsl_intent_before_parent_submission(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "0",
                    "first_limit_offset": "0",
                    "second_limit_offset": "0",
                }
            },
        },
    )

    class _OrderedClient(_FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.call_order = []

        def list_trigger_orders_pending(self, *, inst_id):
            self.call_order.append(("snapshot", inst_id))
            return []

        def trigger_order(self, order_payload):
            self.call_order.append(("trigger", order_payload["instId"]))
            with session_factory() as session:
                intent = (
                    session.query(TriggerProtectionIntent)
                    .filter(
                        TriggerProtectionIntent.request_fingerprint
                        == _trigger_protection_request_fingerprint(order_payload)
                    )
                    .one()
                )
                assert intent.pre_submit_tpsl_baseline_json == "[]"
                assert intent.parent_trigger_order_id is None
            return super().trigger_order(order_payload)

    client = _OrderedClient()
    auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 20, 8, 1, tzinfo=UTC),
    )

    assert client.call_order[:4] == [
        ("snapshot", "BTC-USDT-SWAP"),
        ("trigger", "BTC-USDT-SWAP"),
        ("snapshot", "BTC-USDT-SWAP"),
        ("trigger", "BTC-USDT-SWAP"),
    ]
    with session_factory() as session:
        intents = session.query(TriggerProtectionIntent).order_by(TriggerProtectionIntent.id).all()
        legs = session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id).all()
        protection_legs = session.query(PositionProtectionLeg).order_by(
            PositionProtectionLeg.execution_order_leg_id,
            PositionProtectionLeg.role,
            PositionProtectionLeg.leg_index,
        ).all()
    assert [intent.execution_order_leg_id for intent in intents] == [leg.id for leg in legs]
    assert [json.loads(intent.pre_submit_tpsl_baseline_json) for intent in intents] == [[], []]
    assert all(len(intent.request_fingerprint) == 64 for intent in intents)
    assert [intent.parent_trigger_order_id for intent in intents] == ["trigger-1", "trigger-2"]
    assert {row.role for row in protection_legs} == {
        "primary_stop", "backup_stop", "take_profit"
    }
    assert {row.parent_entry_order_id for row in protection_legs} == {"trigger-1", "trigger-2"}
    assert all(row.pos_id is None and row.exchange_order_id is None for row in protection_legs)


def test_trigger_limit_entry_defers_when_tpsl_snapshot_is_malformed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "0",
                    "first_limit_offset": "0",
                    "second_limit_offset": "0",
                }
            },
        },
    )

    class _MalformedSnapshotClient(_FakeDeepcoinClient):
        def list_trigger_orders_pending(self, *, inst_id):
            return {"data": []}

    client = _MalformedSnapshotClient()
    with pytest.raises(RecoveryLiveSubmitError, match="trigger_protection_baseline_malformed"):
        auto_process_message_trade_signal(
            session_factory,
            raw_message_id=raw_message_id,
            group_config=_group_config(),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 7, 20, 8, 1, tzinfo=UTC),
        )

    assert client.trigger_orders == []
    with session_factory() as session:
        assert session.query(TriggerProtectionIntent).count() == 0
        assert session.query(TradeSignal).one().status == "failed"
        assert session.query(ExecutionBinding).count() == 0
        assert session.query(ExecutionOrderLeg).count() == 0


def test_trigger_limit_entry_rejects_alias_parent_id_without_persisting_it(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "default_max_loss_usdt": 20, "allowed_symbols": ["BTC"]},
    )

    class _AliasParentClient(_FakeDeepcoinClient):
        def trigger_order(self, order_payload):
            self.trigger_orders.append(order_payload)
            return {"code": "0", "data": {"orderId": "alias-parent"}}

    with pytest.raises(DeepcoinClientError, match="missing order id"):
        auto_process_message_trade_signal(
            session_factory,
            raw_message_id=raw_message_id,
            group_config=_group_config(),
            deepcoin_client=_AliasParentClient(),
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=datetime(2026, 7, 20, 8, 1, tzinfo=UTC),
        )

    with session_factory() as session:
        assert session.query(TriggerProtectionIntent).one().parent_trigger_order_id is None


def test_trigger_protection_lock_key_separates_distinct_account_identities():
    class _AccountClient:
        def __init__(self, api_key):
            self._credentials = DeepcoinCredentials(
                api_key=api_key,
                api_secret="secret",
                passphrase="passphrase",
            )

    first = _trigger_protection_lock_key(
        deepcoin_client=_AccountClient("account-a"),
        venue="deepcoin",
        inst_id="BTC-USDT-SWAP",
        side="long",
    )
    second = _trigger_protection_lock_key(
        deepcoin_client=_AccountClient("account-b"),
        venue="deepcoin",
        inst_id="BTC-USDT-SWAP",
        side="long",
    )

    assert first != second
    assert first[0] == second[0] == "deepcoin"
    assert first[2:] == second[2:] == ("BTC-USDT-SWAP", "long")


def test_trigger_protection_lock_key_shares_unknown_account_identity():
    class _UnknownAccountClient:
        pass

    first = _trigger_protection_lock_key(
        deepcoin_client=_UnknownAccountClient(),
        venue="deepcoin",
        inst_id="BTC-USDT-SWAP",
        side="long",
    )
    second = _trigger_protection_lock_key(
        deepcoin_client=_UnknownAccountClient(),
        venue="deepcoin",
        inst_id="BTC-USDT-SWAP",
        side="long",
    )

    assert first == second


def test_sl_only_trigger_entry_snapshots_and_persists_protection_intent(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="BTC long 68000-68200 SL 67500",
        take_profit_text=None,
    )
    save_trading_settings(
        session_factory,
        {"auto_trade_enabled": True, "default_max_loss_usdt": 20, "allowed_symbols": ["BTC"]},
    )

    class _OrderedClient(_FakeDeepcoinClient):
        def __init__(self):
            super().__init__()
            self.call_order = []

        def list_trigger_orders_pending(self, *, inst_id):
            self.call_order.append("snapshot")
            return []

        def trigger_order(self, order_payload):
            self.call_order.append("trigger")
            return super().trigger_order(order_payload)

    client = _OrderedClient()
    auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 20, 8, 1, tzinfo=UTC),
    )

    assert client.call_order[:2] == ["snapshot", "trigger"]
    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
    assert intent.parent_trigger_order_id == "trigger-1"
    assert len(intent.request_fingerprint) == 64
    assert _trigger_protection_request_fingerprint({"slTriggerPx": 67500}) == (
        _trigger_protection_request_fingerprint({"slTriggerPx": 67500, "tpTriggerPx": None})
    )


def test_auto_process_range_entry_uses_fixed_threshold_and_second_offset_when_near_edge(tmp_path):
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
            "symbol_entry_thresholds": {
                "ETH": {
                    "market_leg_threshold": "4",
                    "first_limit_offset": "2",
                    "second_limit_offset": "2",
                }
            },
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
    assert fake_client.orders[0]["sz"] == "3.2"
    assert fake_client.trigger_orders[0]["orderType"] == "limit"
    assert fake_client.trigger_orders[0]["triggerPrice"] == "1567.0"
    assert [order["sz"] for order in fake_client.trigger_orders] == ["3.2"]
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        events = session.query(ExecutionEvent).order_by(ExecutionEvent.id.asc()).all()
        assert session.query(TriggerProtectionIntent).count() == 1
    assert binding.symbol == "ETH"
    assert binding.order_id == "order-1,trigger-1"
    assert [event.action for event in events] == [
        "create_trigger_entry",
        "open_market_position",
        "set_position_tpsl",
    ]


def test_auto_process_short_range_uses_fixed_market_threshold_and_second_offset(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="ETH short 1585-1605 SL 1625 TP 1550",
        entry_text="1585-1605",
        stop_loss_text="1625",
        take_profit_text="1550",
        symbol="ETH",
        side="short",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "allowed_symbols": ["BTC", "ETH"],
            "symbol_entry_thresholds": {
                "ETH": {
                    "market_leg_threshold": "4",
                    "first_limit_offset": "2",
                    "second_limit_offset": "2",
                }
            },
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
    assert [order["ordType"] for order in fake_client.orders] == ["market"]
    assert [order["sz"] for order in fake_client.orders] == ["3.2"]
    assert [order["triggerPrice"] for order in fake_client.trigger_orders] == [
        "1603.0"
    ]
    assert [order["sz"] for order in fake_client.trigger_orders] == ["3.2"]


def test_auto_process_zero_fixed_market_threshold_keeps_two_limit_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(session_factory)
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "allowed_symbols": ["BTC", "ETH"],
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "0",
                    "first_limit_offset": "90",
                    "second_limit_offset": "80",
                }
            },
        },
    )
    fake_client = _FakeDeepcoinClient()
    fake_client.ticker_prices["BTC-USDT-SWAP"] = 68200.0

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 1, 8, 1, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    assert fake_client.orders == []
    assert [order["triggerPrice"] for order in fake_client.trigger_orders] == [
        "68290.0",
        "68080.0",
    ]


def test_strategy_revision_uses_binding_symbol_fixed_offsets_for_both_drafts(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:54:ETH:long",
            kol_id="group:100",
            chat_id=100,
            message_id=54,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            status="open",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=54,
            symbol="ETH",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 1, 7, 55, tzinfo=UTC),
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        raw = RawMessage(
            chat_id=100,
            message_id=55,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
            text="revise ETH entry to 1580-1590",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type="strategy_revision",
                target_lifecycle_id=lifecycle.id,
                entry_text="1580-1590",
                stop_loss_text="1550",
                take_profit_text="1650",
                recognition_generation="revision-generation",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        raw_message_id = raw.id
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "allowed_symbols": ["BTC", "ETH"],
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "200",
                    "first_limit_offset": "90",
                    "second_limit_offset": "80",
                },
                "ETH": {
                    "market_leg_threshold": "4",
                    "first_limit_offset": "2",
                    "second_limit_offset": "3",
                },
            },
        },
    )
    import telegram_kol_research.auto_trade_execution as auto_module

    original_builder = auto_module._build_revision_deepcoin_draft
    built_drafts = []

    def recording_builder(**kwargs):
        draft = original_builder(**kwargs)
        built_drafts.append(draft)
        return draft

    def execute_revision(_session_factory, *, replacement_writer, **_kwargs):
        return replacement_writer(batch_id=1, remaining_fraction=0.5)

    monkeypatch.setattr(auto_module, "_build_revision_deepcoin_draft", recording_builder)
    monkeypatch.setattr(auto_module, "execute_strategy_revision", execute_revision)
    monkeypatch.setattr(
        auto_module,
        "submit_strategy_revision_replacement_live",
        lambda _session_factory, *, draft, **_kwargs: draft,
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 1, 8, 1, tzinfo=UTC),
    )

    assert result["symbol"] == "ETH"
    assert len(built_drafts) == 2
    assert [
        [leg["price"] for leg in draft["order_legs"]]
        for draft in built_drafts
    ] == [[1592.0, 1583.0], [1592.0, 1583.0]]


def test_strategy_revision_rejects_disallowed_authoritative_binding_symbol(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:54:ETH:long",
            kol_id="group:100",
            chat_id=100,
            message_id=54,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            status="open",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=54,
            symbol="ETH",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 1, 7, 55, tzinfo=UTC),
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        raw = RawMessage(
            chat_id=100,
            message_id=55,
            sender_id=200,
            sender_name="Alice",
            posted_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
            text="revise ETH entry to 1580-1590",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type="strategy_revision",
                target_lifecycle_id=lifecycle.id,
                entry_text="1580-1590",
                stop_loss_text="1550",
                take_profit_text="1650",
                recognition_generation="revision-generation",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        raw_message_id = raw.id
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "allowed_symbols": ["BTC"],
        },
    )
    import telegram_kol_research.auto_trade_execution as auto_module

    monkeypatch.setattr(
        auto_module,
        "execute_strategy_revision",
        lambda *_args, **_kwargs: {"status": "unexpected_execution"},
    )

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=_FakeDeepcoinClient(),
        contract_spec_provider=_StaticContractSpecProvider(),
        revision_replacement_writer=lambda **_kwargs: {
            "status": "unexpected_replacement"
        },
        processed_at=datetime(2026, 7, 1, 8, 1, tzinfo=UTC),
    )

    assert result == {
        "status": "skipped",
        "reason": "symbol_not_allowed",
        "symbol": "ETH",
    }


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
            "avgPx": "68000",
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
    assert [payload.get("tpTriggerPx") for payload in fake_client.protections] == [None]
    assert [payload.get("sz") for payload in fake_client.protections] == [None]
    with session_factory() as session:
        events = session.query(ExecutionEvent).order_by(ExecutionEvent.id.asc()).all()
        convergences = session.query(TriggerTakeProfitConvergence).all()
    assert [event.action for event in events] == [
        "open_market_position",
        "set_position_tpsl",
    ]
    assert events[1].pos_id == "pos-1"
    assert '"stop_loss": "67500.0"' in (events[1].after_json or "")
    assert len(convergences) == 1
    assert convergences[0].status == "waiting_position"
    assert json.loads(convergences[0].desired_take_profits_json) == [
        {"allocation_pct": "50", "price": "69000"},
        {"allocation_pct": "50", "price": "70000"},
    ]


def test_auto_process_message_trade_signal_records_entry_protection_ledger(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="ETH 现价开多 SL 1788 TP 1955",
        entry_text="现价入场",
        stop_loss_text="1788",
        take_profit_text="1955",
        symbol="ETH",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _SequencedProtectionDeepcoinClient()
    fake_client.ticker_prices["ETH-USDT-SWAP"] = 1844.0
    fake_client.positions = [
        {
            "instId": "ETH-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "long",
            "pos": "2.6",
            "avgPx": "1844",
            "mrgPosition": "split",
            "mgnMode": "cross",
        }
    ]

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 18, 7, 47, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    with session_factory() as session:
        rows = (
            session.query(PositionProtectionLedger)
            .order_by(PositionProtectionLedger.order_id.asc())
            .all()
        )
        binding_ids = {binding.id for binding in session.query(ExecutionBinding).all()}
        leg_ids = {leg.id for leg in session.query(ExecutionOrderLeg).all()}
    assert [(row.order_id, row.pos_id, row.purpose, row.trigger_price) for row in rows] == [
        ("sltp-1", "pos-1", "stop_loss", "1788.0"),
    ]
    assert {row.execution_binding_id for row in rows} == binding_ids
    assert {row.execution_order_leg_id for row in rows} == leg_ids
    assert {row.evidence_source for row in rows} == {"entry_protection_response"}


def test_auto_process_message_trade_signal_records_response_anchored_primary_stop(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="ETH 现价开多 SL 1788 TP 1955",
        entry_text="现价入场",
        stop_loss_text="1788",
        take_profit_text="1955",
        symbol="ETH",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _SequencedProtectionDeepcoinClient()
    fake_client.ticker_prices["ETH-USDT-SWAP"] = 1844.0
    fake_client.positions = [
        {
            "instId": "ETH-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "long",
            "pos": "2.6",
            "avgPx": "1844",
            "mrgPosition": "split",
            "mgnMode": "cross",
        }
    ]
    original_set_position_sltp = fake_client.set_position_sltp

    def set_position_sltp_without_position_identity(payload):
        response = original_set_position_sltp(payload)
        for row in fake_client.trigger_pending:
            row.pop("posId", None)
            row.pop("closePosId", None)
            row["cTime"] = "2026-07-18T07:47:01Z"
            row["uTime"] = "2026-07-18T07:47:01Z"
        return response

    fake_client.set_position_sltp = set_position_sltp_without_position_identity

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 18, 7, 47, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    with session_factory() as session:
        rows = (
            session.query(PositionProtectionLedger)
            .order_by(PositionProtectionLedger.order_id.asc())
            .all()
        )
    assert [(row.order_id, row.pos_id, row.purpose, row.trigger_price) for row in rows] == [
        ("sltp-1", "pos-1", "stop_loss", "1788.0"),
    ]
    assert {json.loads(row.evidence_json)["match"] for row in rows} == {
        "exchange_returned_order_id_exact_readback",
    }


def test_auto_process_message_trade_signal_does_not_ledger_price_only_tpsl(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="ETH 现价开多 SL 1788 TP 1955",
        entry_text="现价入场",
        stop_loss_text="1788",
        take_profit_text="1955",
        symbol="ETH",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _SequencedProtectionDeepcoinClient()
    fake_client.ticker_prices["ETH-USDT-SWAP"] = 1844.0
    fake_client.positions = [
        {
            "instId": "ETH-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "long",
            "pos": "2.6",
            "avgPx": "1844",
            "mrgPosition": "split",
            "mgnMode": "cross",
        }
    ]
    original_set_position_sltp = fake_client.set_position_sltp

    def set_position_sltp_without_anchor(payload):
        response = original_set_position_sltp(payload)
        for row in fake_client.trigger_pending:
            row.pop("posId", None)
            row.pop("closePosId", None)
            row["cTime"] = "2026-07-18T07:47:01Z"
            row["uTime"] = "2026-07-18T07:47:01Z"
        response["data"].pop("ordId", None)
        return response

    fake_client.set_position_sltp = set_position_sltp_without_anchor

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 18, 7, 47, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    with session_factory() as session:
        rows = session.query(PositionProtectionLedger).all()
    assert rows == []


def test_auto_process_message_trade_signal_records_combined_entry_protection(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _persist_candidate(
        session_factory,
        text="ETH 现价开多 SL 1788 TP 1955",
        entry_text="现价入场",
        stop_loss_text="1788",
        take_profit_text="1955",
        symbol="ETH",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC", "ETH"],
        },
    )
    fake_client = _CombinedProtectionDeepcoinClient()
    fake_client.ticker_prices["ETH-USDT-SWAP"] = 1844.0
    fake_client.positions = [
        {
            "instId": "ETH-USDT-SWAP",
            "posId": "pos-1",
            "posSide": "long",
            "pos": "2.6",
            "avgPx": "1844",
            "mrgPosition": "split",
            "mgnMode": "cross",
        }
    ]

    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=fake_client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 7, 18, 7, 47, tzinfo=UTC),
    )

    assert result["status"] == "submitted"
    with session_factory() as session:
        rows = session.query(PositionProtectionLedger).all()
    assert [(row.order_id, row.purpose, row.trigger_price) for row in rows] == [
        ("combined-sltp-1", "stop_loss", "1788.0")
    ]


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
            "posId": "pos-1",
            "posSide": "long",
            "pos": "13",
            "avgPx": "59680",
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
            "symbol_entry_thresholds": {
                "BTC": {
                    "market_leg_threshold": "0",
                    "first_limit_offset": "0",
                    "second_limit_offset": "0",
                }
            },
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
        "58900.0",
    ]
    assert fake_client.protections == []
    assert fake_client.trigger_orders[0]["slTriggerPx"] == "57800.0"
    assert all(not any(key.startswith("tp") for key in order) for order in fake_client.trigger_orders)


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
