"""Phases 1 and 2 of the entry-confirm sizing and lifecycle integrity design.

``docs/plans/2026-09-23-entry-confirm-sizing-and-lifecycle-integrity-design.md``

Phase 1: a message that only *confirms* an existing pending entry never opens a
position of its own, and never inherits the target's stop loss to get past the
"a pure market entry must carry a stop" gate. The one exception is a message
that names a market entry **and** carries its own stop: that is a new strategy
keyed on itself.

Phase 2: the sizing word such a message carries ("半仓") is persisted as an
entry preamble and consumed by the next adjacent strategy, which means the
confirmation candidate must stop acting as a hard adjacency boundary.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from telegram_kol_research.auto_trade_execution import (
    auto_process_message_trade_signal,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_revision_exchange_authority import (
    seed_entry_revision_exchange_authority,
)
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.message_recognition import (
    apply_authoritative_mimo_payload,
)
from telegram_kol_research.models import (
    EntryPreamble,
    ExecutionBinding,
    ExecutionEvent,
    MessageEvidenceVersion,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.trading_settings import save_trading_settings

from tests.test_auto_trade_execution import (
    _FakeDeepcoinClient,
    _StaticContractSpecProvider,
)


CHAT_ID = 100
_BASE_CREATE_SESSION_FACTORY = create_session_factory


def _session_factory(path):
    session_factory = _BASE_CREATE_SESSION_FACTORY(path)
    seed_entry_revision_exchange_authority(
        session_factory,
        seeded_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    return session_factory


def _group_config(*, chat_id=CHAT_ID):
    return GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="Chen",
                chat_id=chat_id,
                enabled=True,
                trading_mode="auto_trade",
                max_loss_usdt=20.0,
                symbol_whitelist=["BTC", "ETH", "SUSHI"],
            )
        ]
    )


def _add_raw_message(session, *, message_id, posted_at, text, chat_id=CHAT_ID):
    raw = RawMessage(
        chat_id=chat_id,
        message_id=message_id,
        sender_id=7,
        sender_name="Chen",
        posted_at=posted_at,
        text=text,
        archived_target_group=True,
    )
    session.add(raw)
    session.flush()
    return raw


def _add_completed_evidence(session, raw, *, normalized="{}"):
    evidence = MessageEvidenceVersion(
        raw_message_id=raw.id,
        version=1,
        input_fingerprint=f"fingerprint-{raw.id}",
        model="mimo-v2.5",
        prompt_versions_json="{}",
        extraction_status="completed",
        confidence=0.95,
        text_evidence_json="{}",
        image_evidence_json='{"images":[]}',
        normalized_evidence_json=normalized,
    )
    session.add(evidence)
    session.flush()
    return evidence


def _add_pending_lifecycle(
    session,
    *,
    message_id,
    signal_at,
    symbol="BTC",
    side="short",
    entry_low=85700.0,
    entry_high=86000.0,
    stop_loss=87200.0,
    take_profit="84000-82000",
    chat_id=CHAT_ID,
):
    lifecycle = StrategyLifecycle(
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        lifecycle_status="pending_entry",
        signal_at=signal_at,
        entry_range_low=entry_low,
        entry_range_high=entry_high,
        stop_loss=stop_loss,
        take_profit=take_profit,
    )
    session.add(lifecycle)
    session.flush()
    return lifecycle


def _confirm_payload(lifecycle_id, *, symbol, side, entry_price=None, stop_loss=None):
    lifecycle_event = {
        "event_type": "entry_confirm",
        "target_lifecycle_id": lifecycle_id,
        "symbol": symbol,
        "side": side,
        "entry_price": entry_price,
        "confidence": 0.93,
        "reason": "可唯一对应已有 pending_entry 策略",
    }
    if stop_loss is not None:
        lifecycle_event["stop_loss"] = stop_loss
    return {"recognition_result": "非策略", "lifecycle_event": lifecycle_event}


def _entry_result(result):
    """Unwrap the instruction-item envelope when the message has one."""

    if result.get("status") == "completed" and result.get("items"):
        entries = [
            item
            for item in result["items"]
            if item.get("instruction_kind") == "entry"
        ]
        if entries:
            return entries[0]["result"]
    return result


def _entry_candidate(session, raw_message_id):
    return (
        session.query(SignalCandidate)
        .filter(SignalCandidate.raw_message_id == raw_message_id)
        .filter(SignalCandidate.event_type == "entry_signal")
        .one()
    )


# --------------------------------------------------------------------------
# Phase 1 -- test 1: the confirmation candidate carries its own marker
# --------------------------------------------------------------------------


def test_authoritative_entry_confirm_marks_the_candidate_as_entry_confirm(tmp_path):
    session_factory = _session_factory(tmp_path / "confirm-marker.db")
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(
            session,
            message_id=10672,
            signal_at=datetime(2026, 9, 22, 1, 17, 30, tzinfo=UTC),
        )
        raw = _add_raw_message(
            session,
            message_id=10696,
            posted_at=datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC),
            text="比特币市价86500附近，半仓入场做个短线空单",
        )
        session.commit()
        raw_message_id, lifecycle_id = raw.id, lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=_confirm_payload(
            lifecycle_id, symbol="BTC", side="short", entry_price=86500
        ),
        model="mimo-v2.5",
        authoritative_generation="generation-10696",
    )

    with session_factory() as session:
        candidate = _entry_candidate(session, raw_message_id)
        assert candidate.management_action == "entry_confirm"
        assert candidate.event_type == "entry_signal"
        assert candidate.target_lifecycle_id is None
        assert candidate.parse_source == "mimo_authoritative"
        item = session.query(MessageInstructionItem).one()
        assert item.instruction_kind == "entry"


# --------------------------------------------------------------------------
# Phase 1 -- test 2: the marked candidate is refused with zero exchange writes
# --------------------------------------------------------------------------


def test_marked_confirmation_candidate_is_refused_without_any_exchange_write(tmp_path):
    session_factory = _session_factory(tmp_path / "confirm-refused.db")
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(
            session,
            message_id=10672,
            signal_at=datetime(2026, 9, 22, 1, 17, 30, tzinfo=UTC),
        )
        raw = _add_raw_message(
            session,
            message_id=10696,
            posted_at=datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC),
            text="比特币市价86500附近，半仓入场做个短线空单",
        )
        session.commit()
        raw_message_id, lifecycle_id = raw.id, lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=_confirm_payload(
            lifecycle_id, symbol="BTC", side="short", entry_price=86500
        ),
        model="mimo-v2.5",
        authoritative_generation="generation-10696",
    )
    with session_factory() as session:
        candidate = _entry_candidate(session, raw_message_id)
        assert candidate.entry_text == "86500"
        assert candidate.parse_source == "mimo_authoritative"

    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
        },
    )
    client = _FakeDeepcoinClient(session_factory)

    auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=datetime(2026, 9, 23, 6, 4, 2, tzinfo=UTC),
    )

    assert client.orders == []
    assert client.trigger_orders == []
    assert client.protections == []
    with session_factory() as session:
        assert session.query(ExecutionBinding).count() == 0
        reasons = {
            row.reason
            for row in session.query(ExecutionEvent).all()
            if row.action == "auto_trade_skipped"
        }
        assert "lifecycle_event_not_new_entry" in reasons


# --------------------------------------------------------------------------
# Phase 1 -- test 3: shape replay, exactly one binding per strategy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "strategy_message_id",
        "confirm_message_id",
        "next_message_id",
        "confirm_text",
        "next_entry_text",
        "next_stop",
        "next_take_profit",
        "target_stop",
    ),
    [
        (
            10672,
            10696,
            10697,
            "比特币市价86500附近，半仓入场做个短线空单",
            "86500-86700",
            "88300",
            "84200-82700",
            87200.0,
        ),
        (
            10594,
            10595,
            10596,
            "比特币81000附近进场",
            "80910-81210",
            "82100",
            "79000-78000",
            82000.0,
        ),
    ],
)
def test_confirmation_message_never_opens_its_own_position_in_shape_replay(
    tmp_path,
    strategy_message_id,
    confirm_message_id,
    next_message_id,
    confirm_text,
    next_entry_text,
    next_stop,
    next_take_profit,
    target_stop,
):
    session_factory = _session_factory(tmp_path / f"replay-{confirm_message_id}.db")
    confirm_at = datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(
            session,
            message_id=strategy_message_id,
            signal_at=confirm_at - timedelta(hours=28),
            stop_loss=target_stop,
        )
        confirm_raw = _add_raw_message(
            session,
            message_id=confirm_message_id,
            posted_at=confirm_at,
            text=confirm_text,
        )
        _add_completed_evidence(session, confirm_raw)
        next_raw = _add_raw_message(
            session,
            message_id=next_message_id,
            posted_at=confirm_at + timedelta(seconds=65),
            text=f"BTC {next_entry_text} 做空，止损 {next_stop}",
        )
        session.add(
            SignalCandidate(
                raw_message_id=next_raw.id,
                symbol="BTC",
                side="short",
                event_type="entry_signal",
                entry_text=next_entry_text,
                stop_loss_text=next_stop,
                take_profit_text=next_take_profit,
                parse_source="mimo_authoritative",
                confidence=0.95,
            )
        )
        session.commit()
        confirm_raw_id = confirm_raw.id
        next_raw_id = next_raw.id
        lifecycle_id = lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=confirm_raw_id,
        payload=_confirm_payload(
            lifecycle_id, symbol="BTC", side="short", entry_price=86500
        ),
        model="mimo-v2.5",
        authoritative_generation=f"generation-{confirm_message_id}",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
        },
    )
    client = _FakeDeepcoinClient(session_factory)
    auto_process_message_trade_signal(
        session_factory,
        raw_message_id=confirm_raw_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=confirm_at + timedelta(seconds=22),
    )
    auto_process_message_trade_signal(
        session_factory,
        raw_message_id=next_raw_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=confirm_at + timedelta(seconds=85),
    )

    with session_factory() as session:
        bindings = session.query(ExecutionBinding).all()
        assert len(bindings) == 1
        assert bindings[0].message_id == next_message_id
        ordering_actions = {
            str(row.action or "")
            for row in session.query(ExecutionEvent).all()
            if row.message_id == confirm_message_id
        }
        # The acceptance criterion is the execution_events row, not a candidate
        # or attempt status field: these two actions are the only ways an entry
        # reaches the venue.
        assert ordering_actions & {
            "open_position",
            "eligible_for_recovery_limit_order",
        } == set()
        assert "auto_trade_skipped" in ordering_actions


# --------------------------------------------------------------------------
# Phase 1 -- test 4: the market + own stop exception (1.3)
# --------------------------------------------------------------------------


def test_market_confirmation_without_its_own_stop_never_opens(tmp_path):
    """Nick 1509 shape: 「现价入场」 and nothing else."""

    session_factory = _session_factory(tmp_path / "market-no-stop.db")
    posted_at = datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(
            session,
            message_id=1508,
            signal_at=posted_at - timedelta(hours=3),
        )
        raw = _add_raw_message(
            session, message_id=1509, posted_at=posted_at, text="现价入场"
        )
        session.commit()
        raw_message_id, lifecycle_id = raw.id, lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=_confirm_payload(lifecycle_id, symbol="BTC", side="short"),
        model="mimo-v2.5",
        authoritative_generation="generation-1509",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
        },
    )
    client = _FakeDeepcoinClient(session_factory)
    auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=posted_at + timedelta(seconds=20),
    )

    assert client.orders == []
    with session_factory() as session:
        candidate = _entry_candidate(session, raw_message_id)
        assert candidate.management_action == "entry_confirm"
        assert session.query(ExecutionBinding).count() == 0
        assert session.query(StrategyLifecycle).count() == 1


def test_confirmation_never_inherits_the_target_stop_to_qualify_as_a_strategy(
    tmp_path,
):
    """The message names a market entry but carries no stop of its own."""

    session_factory = _session_factory(tmp_path / "inherited-stop.db")
    posted_at = datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(
            session,
            message_id=10672,
            signal_at=posted_at - timedelta(hours=28),
            stop_loss=87200.0,
        )
        raw = _add_raw_message(
            session,
            message_id=10696,
            posted_at=posted_at,
            text="比特币市价86500附近，半仓入场做个短线空单",
        )
        session.commit()
        raw_message_id, lifecycle_id = raw.id, lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=_confirm_payload(
            lifecycle_id, symbol="BTC", side="short", entry_price=86500
        ),
        model="mimo-v2.5",
        authoritative_generation="generation-10696",
    )

    with session_factory() as session:
        candidate = _entry_candidate(session, raw_message_id)
        assert candidate.management_action == "entry_confirm"
        assert candidate.stop_loss_text == "87200"
        # Exactly one lifecycle: the confirmation did not mint one of its own.
        assert session.query(StrategyLifecycle).count() == 1


def test_market_entry_with_its_own_stop_is_a_new_strategy_keyed_on_this_message(
    tmp_path,
):
    """SUSHI shape: 「现价开个多…止损 0.152」 stands on its own."""

    session_factory = _session_factory(tmp_path / "market-own-stop.db")
    posted_at = datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC)
    with session_factory() as session:
        target = _add_pending_lifecycle(
            session,
            message_id=880,
            signal_at=posted_at - timedelta(hours=5),
            symbol="SUSHI",
            side="long",
            entry_low=0.148,
            entry_high=0.150,
            stop_loss=0.140,
            take_profit="0.170",
        )
        raw = _add_raw_message(
            session,
            message_id=881,
            posted_at=posted_at,
            text="SUSHI 现价开个多，止损 0.152",
        )
        session.commit()
        raw_message_id, target_id = raw.id, target.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=_confirm_payload(
            target_id, symbol="SUSHI", side="long", stop_loss="0.152"
        ),
        model="mimo-v2.5",
        authoritative_generation="generation-881",
    )

    with session_factory() as session:
        candidate = _entry_candidate(session, raw_message_id)
        assert candidate.management_action is None
        assert candidate.stop_loss_text == "0.152"
        assert candidate.symbol == "SUSHI"
        assert candidate.side == "long"
        own = (
            session.query(StrategyLifecycle)
            .filter(StrategyLifecycle.message_id == 881)
            .one()
        )
        assert own.stop_loss == pytest.approx(0.152)
        assert own.lifecycle_status == "pending_entry"
        target = session.get(StrategyLifecycle, target_id)
        assert target.lifecycle_status == "pending_entry"
        assert target.entered_at is None


def test_market_entry_with_its_own_stop_opens_against_the_live_price(tmp_path):
    session_factory = _session_factory(tmp_path / "market-own-stop-order.db")
    posted_at = datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC)
    with session_factory() as session:
        target = _add_pending_lifecycle(
            session,
            message_id=10672,
            signal_at=posted_at - timedelta(hours=28),
            stop_loss=87200.0,
        )
        raw = _add_raw_message(
            session,
            message_id=10696,
            posted_at=posted_at,
            text="比特币市价入场做个短线空单，止损 87000",
        )
        session.commit()
        raw_message_id, target_id = raw.id, target.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=_confirm_payload(
            target_id, symbol="BTC", side="short", stop_loss="87000"
        ),
        model="mimo-v2.5",
        authoritative_generation="generation-10696",
    )
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["BTC"],
        },
    )
    client = _FakeDeepcoinClient(session_factory)
    client.ticker_prices["BTC-USDT-SWAP"] = 86500.0
    result = _entry_result(
        auto_process_message_trade_signal(
            session_factory,
            raw_message_id=raw_message_id,
            group_config=_group_config(),
            deepcoin_client=client,
            contract_spec_provider=_StaticContractSpecProvider(),
            processed_at=posted_at + timedelta(seconds=22),
        )
    )

    assert result["status"] == "submitted", result
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        assert binding.message_id == 10696
        target = session.get(StrategyLifecycle, target_id)
        assert target.lifecycle_status == "pending_entry"


# --------------------------------------------------------------------------
# Phase 2 -- test 7: the confirmation message produces a pending preamble
# --------------------------------------------------------------------------


def test_confirmation_message_with_a_sizing_word_writes_a_pending_preamble(tmp_path):
    session_factory = _session_factory(tmp_path / "confirm-preamble.db")
    posted_at = datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(
            session,
            message_id=10672,
            signal_at=posted_at - timedelta(hours=28),
        )
        raw = _add_raw_message(
            session,
            message_id=10696,
            posted_at=posted_at,
            text="比特币市价86500附近，半仓入场做个短线空单",
        )
        _add_completed_evidence(session, raw)
        session.commit()
        raw_message_id, lifecycle_id = raw.id, lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=_confirm_payload(
            lifecycle_id, symbol="BTC", side="short", entry_price=86500
        ),
        model="mimo-v2.5",
        authoritative_generation="generation-10696",
    )

    with session_factory() as session:
        preamble = session.query(EntryPreamble).one()
        assert preamble.raw_message_id == raw_message_id
        assert preamble.message_id == 10696
        assert preamble.symbol == "BTC"
        assert preamble.side == "short"
        assert preamble.risk_multiplier == "0.5"
        assert preamble.status == "pending"
        assert preamble.recognition_generation == "generation-10696"


def test_confirmation_message_without_a_sizing_word_writes_no_preamble(tmp_path):
    session_factory = _session_factory(tmp_path / "confirm-no-preamble.db")
    posted_at = datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(
            session,
            message_id=10672,
            signal_at=posted_at - timedelta(hours=28),
        )
        raw = _add_raw_message(
            session,
            message_id=10696,
            posted_at=posted_at,
            text="比特币市价86500附近，正常仓位入场做个短线空单",
        )
        _add_completed_evidence(session, raw)
        session.commit()
        raw_message_id, lifecycle_id = raw.id, lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=_confirm_payload(
            lifecycle_id, symbol="BTC", side="short", entry_price=86500
        ),
        model="mimo-v2.5",
        authoritative_generation="generation-10696",
    )

    with session_factory() as session:
        assert session.query(EntryPreamble).count() == 0


# --------------------------------------------------------------------------
# Phase 2 -- test 8: end to end, 10696 -> 10697, 65 seconds apart
# --------------------------------------------------------------------------


def _replay_10696_then_10697(session_factory, *, confirm_text):
    confirm_at = datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC)
    with session_factory() as session:
        lifecycle = _add_pending_lifecycle(
            session,
            message_id=10672,
            signal_at=confirm_at - timedelta(hours=28),
        )
        confirm_raw = _add_raw_message(
            session,
            message_id=10696,
            posted_at=confirm_at,
            text=confirm_text,
        )
        _add_completed_evidence(session, confirm_raw)
        next_raw = _add_raw_message(
            session,
            message_id=10697,
            posted_at=confirm_at + timedelta(seconds=65),
            text="BTC 86500-86700 做空，止损 88300，止盈 84200-82700",
        )
        session.add(
            SignalCandidate(
                raw_message_id=next_raw.id,
                symbol="BTC",
                side="short",
                event_type="entry_signal",
                entry_text="86500-86700",
                stop_loss_text="88300",
                take_profit_text="84200-82700",
                parse_source="mimo_authoritative",
                confidence=0.95,
            )
        )
        session.commit()
        confirm_raw_id = confirm_raw.id
        next_raw_id = next_raw.id
        lifecycle_id = lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=confirm_raw_id,
        payload=_confirm_payload(
            lifecycle_id, symbol="BTC", side="short", entry_price=86500
        ),
        model="mimo-v2.5",
        authoritative_generation="generation-10696",
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
    client = _FakeDeepcoinClient(session_factory)
    client.ticker_prices["BTC-USDT-SWAP"] = 86520.0
    auto_process_message_trade_signal(
        session_factory,
        raw_message_id=confirm_raw_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=confirm_at + timedelta(seconds=22),
    )
    result = auto_process_message_trade_signal(
        session_factory,
        raw_message_id=next_raw_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=confirm_at + timedelta(seconds=85),
    )
    return result, client


def _submitted_contracts(client):
    return sum(float(order.get("sz") or 0) for order in client.orders)


def test_half_size_from_the_confirmation_message_halves_the_next_strategy(tmp_path):
    session_factory = _session_factory(tmp_path / "e2e-half.db")
    result, client = _replay_10696_then_10697(
        session_factory,
        confirm_text="比特币市价86500附近，半仓入场做个短线空单",
    )

    assert result["status"] == "submitted", result
    assembly = result["entry_preamble_assembly"]
    assert assembly["applied_risk_multiplier"] == "0.5"
    assert assembly["effective_risk_budget_usdt"] == 10.0

    control_factory = _session_factory(tmp_path / "e2e-full.db")
    control_result, control_client = _replay_10696_then_10697(
        control_factory,
        confirm_text="比特币市价86500附近，正常仓位入场做个短线空单",
    )
    assert control_result["status"] == "submitted", control_result
    assert control_result["entry_preamble_assembly"]["applied_risk_multiplier"] == "1"

    half = _submitted_contracts(client)
    full = _submitted_contracts(control_client)
    assert full > 0
    assert half == pytest.approx(full / 2, abs=1.0)
    assert half < full

    with session_factory() as session:
        assert session.query(EntryPreamble).one().status == "consumed"
        binding = session.query(ExecutionBinding).one()
        assert binding.message_id == 10697
        draft = json.loads(binding.payload_json)["draft"]
        assert draft["risk_budget_usdt"] == 10.0


# --------------------------------------------------------------------------
# Phase 2 -- test 9: a real adjacent entry is still a hard boundary
# --------------------------------------------------------------------------


def test_a_real_adjacent_entry_candidate_is_still_a_hard_boundary(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )
    from telegram_kol_research.entry_preambles import (
        persist_entry_preamble_in_session,
    )
    from telegram_kol_research.message_evidence import EntryPreambleEvidence

    session_factory = _session_factory(tmp_path / "hard-boundary.db")
    base = datetime(2026, 9, 23, 6, 0, tzinfo=UTC)
    with session_factory() as session:
        earlier = _add_raw_message(
            session, message_id=900, posted_at=base, text="BTC 空单半仓"
        )
        evidence = _add_completed_evidence(session, earlier)
        persist_entry_preamble_in_session(
            session,
            raw_message=earlier,
            evidence_version_id=evidence.id,
            recognition_generation="generation-900",
            evidence=EntryPreambleEvidence(
                symbol="BTC",
                side="short",
                risk_multiplier=Decimal("0.5"),
                confidence=0.95,
                reason="半仓",
            ),
            now=base,
        )
        # A genuine complete entry sits between the preamble and the strategy.
        boundary = _add_raw_message(
            session,
            message_id=901,
            posted_at=base + timedelta(seconds=30),
            text="BTC 86000-86200 做空，止损 87000",
        )
        _add_completed_evidence(session, boundary)
        session.add(
            SignalCandidate(
                raw_message_id=boundary.id,
                symbol="BTC",
                side="short",
                event_type="entry_signal",
                entry_text="86000-86200",
                stop_loss_text="87000",
                parse_source="mimo_authoritative",
                confidence=0.95,
            )
        )
        strategy = _add_raw_message(
            session,
            message_id=902,
            posted_at=base + timedelta(seconds=60),
            text="BTC 86500-86700 做空，止损 88300",
        )
        _add_completed_evidence(session, strategy)
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            entry_text="86500-86700",
            stop_loss_text="88300",
            parse_source="mimo_authoritative",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        session.commit()
        strategy_id, candidate_id = strategy.id, candidate.id

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=base + timedelta(seconds=90),
    )

    assert decision.status == "ready"
    assert decision.selection.risk_multiplier == 1
    assert decision.selection.legacy_preamble_ids == ()


def test_a_confirmation_candidate_does_not_bound_the_preamble_on_its_own_message(
    tmp_path,
):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )
    from telegram_kol_research.entry_preambles import (
        persist_entry_preamble_in_session,
    )
    from telegram_kol_research.message_evidence import EntryPreambleEvidence

    session_factory = _session_factory(tmp_path / "confirm-not-boundary.db")
    base = datetime(2026, 9, 23, 6, 3, 40, tzinfo=UTC)
    with session_factory() as session:
        confirm = _add_raw_message(
            session,
            message_id=10696,
            posted_at=base,
            text="比特币市价86500附近，半仓入场做个短线空单",
        )
        evidence = _add_completed_evidence(session, confirm)
        persist_entry_preamble_in_session(
            session,
            raw_message=confirm,
            evidence_version_id=evidence.id,
            recognition_generation="generation-10696",
            evidence=EntryPreambleEvidence(
                symbol="BTC",
                side="short",
                risk_multiplier=Decimal("0.5"),
                confidence=0.93,
                reason="半仓（来自入场确认消息）",
            ),
            now=base,
        )
        session.add(
            SignalCandidate(
                raw_message_id=confirm.id,
                symbol="BTC",
                side="short",
                event_type="entry_signal",
                entry_text="86500",
                stop_loss_text="87200",
                take_profit_text="84000-82000",
                management_action="entry_confirm",
                parse_source="mimo_authoritative",
                confidence=0.93,
            )
        )
        strategy = _add_raw_message(
            session,
            message_id=10697,
            posted_at=base + timedelta(seconds=65),
            text="BTC 86500-86700 做空，止损 88300",
        )
        _add_completed_evidence(session, strategy)
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            entry_text="86500-86700",
            stop_loss_text="88300",
            parse_source="mimo_authoritative",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        session.commit()
        strategy_id, candidate_id = strategy.id, candidate.id

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=base + timedelta(seconds=90),
    )

    assert decision.status == "ready"
    assert decision.selection.risk_multiplier == Decimal("0.5")
    assert decision.selection.boundary_evidence == ()
