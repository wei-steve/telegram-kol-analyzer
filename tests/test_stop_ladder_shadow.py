"""A shadow round decides everything and does nothing.

Phase 1 is L1: no mode writes to the exchange, no ``PositionMutationIntent``
row is ever created, and the whole output is one ``execution_events`` row per
position per change.  The rows are the venue's own shapes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from deepcoin_production_rows import (
    pending_stop_row,
    pending_take_profit_row,
    position_row,
)

from telegram_kol_research.break_even_shadow import run_break_even_shadow_pass
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    ExecutionEvent,
    PositionMutationIntent,
    StrategyLifecycle,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row
from telegram_kol_research.protection_health import record_take_profit_ledger_fill
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"
POS = "1001125216121996"
STOP_ORDER = "sl-82300"
TPS = (("tp-79800", "79800"), ("tp-79100", "79100"), ("tp-78400", "78400"))


class _Client:
    """Reads only.  Every write method fails the test."""

    def __init__(self, positions, pending):
        self._positions = positions
        self._pending = pending

    def list_positions(self, *, inst_id=None):
        return [dict(row) for row in self._positions]

    def list_trigger_orders_pending(self, *, inst_id):
        return [dict(row) for row in self._pending]

    def set_position_sltp(self, payload):  # pragma: no cover - must never run
        raise AssertionError("the shadow must not write to the exchange")

    def cancel_position_sltp(self, payload):  # pragma: no cover - must never run
        raise AssertionError("the shadow must not write to the exchange")

    def close_position(self, payload):  # pragma: no cover - must never run
        raise AssertionError("the shadow must not write to the exchange")

    def place_order(self, payload):  # pragma: no cover - must never run
        raise AssertionError("the shadow must not write to the exchange")


def _seed(tmp_path, *, mode="shadow", filled=(), stop_price="82300"):
    session_factory = create_session_factory(tmp_path / "research.db")
    if mode is not None:
        save_trading_settings(session_factory, {"stop_ladder_mode": mode})
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol",
            chat_id=1,
            message_id=1,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
            pos_id=POS,
            strategy_instance_id="deepcoin:1:1:BTC:short",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            purpose="entry",
            order_kind="limit",
            strategy_instance_id="deepcoin:1:1:BTC:short",
            venue="deepcoin",
            pos_id=POS,
            status="active",
            attribution_status="verified",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                execution_binding_id=binding_id,
                chat_id=1,
                message_id=1,
                symbol="BTC",
                side="short",
                entry_range_low=80500.0,
                entry_range_high=81600.0,
                lifecycle_status="entered",
                signal_at=NOW - timedelta(hours=1),
            )
        )
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            strategy_instance_id="deepcoin:1:1:BTC:short",
            pos_id=POS,
            instrument_id=INST,
            side="short",
            order_id=STOP_ORDER,
            purpose="stop_loss",
            trigger_price=stop_price,
            size_text="7",
            status="verified",
            evidence_source="test",
            evidence={},
            seen_at=NOW - timedelta(minutes=10),
        )
        for order_id, price in TPS:
            upsert_protection_ledger_row(
                session,
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                strategy_instance_id="deepcoin:1:1:BTC:short",
                pos_id=POS,
                instrument_id=INST,
                side="short",
                order_id=order_id,
                purpose="take_profit",
                trigger_price=price,
                size_text="3",
                status="verified",
                evidence_source="test",
                evidence={},
                seen_at=NOW - timedelta(minutes=10),
            )
        session.commit()
    _mark_filled(session_factory, filled)
    return session_factory, binding_id


def _mark_filled(session_factory, order_ids):
    from telegram_kol_research.models import PositionProtectionLedger

    if not order_ids:
        return
    with session_factory() as session:
        for level, order_id in enumerate(order_ids, start=1):
            row = (
                session.query(PositionProtectionLedger)
                .filter_by(order_id=order_id)
                .one()
            )
            record_take_profit_ledger_fill(
                row,
                order_id=order_id,
                evidence={"level": level, "evidence_form": "trigger_history"},
                observed_at=NOW - timedelta(minutes=5),
            )
        session.commit()


def _client(*, market="79790", size="7", stop_price="82300"):
    position = position_row(
        pos_id=POS, inst_id=INST, pos_side="short", size=size, avg_price="80436"
    )
    position["lastPx"] = market
    pending = [
        pending_stop_row(
            ord_id=STOP_ORDER,
            inst_id=INST,
            pos_side="short",
            trigger_price=stop_price,
            size=size,
        ),
        *[
            pending_take_profit_row(
                ord_id=order_id,
                inst_id=INST,
                pos_side="short",
                trigger_price=price,
                size="3",
            )
            for order_id, price in TPS
        ],
    ]
    return _Client([position], pending)


def _run(session_factory, client, *, now=NOW):
    return run_break_even_shadow_pass(
        session_factory, deepcoin_client=client, now=now
    )


def _events(session_factory):
    with session_factory() as session:
        return (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action.like("stop_ladder%"))
            .order_by(ExecutionEvent.id.asc())
            .all()
        )


def test_shadow_records_what_the_first_rung_would_do(tmp_path):
    session_factory, binding_id = _seed(tmp_path, filled=["tp-79800"])

    result = _run(session_factory, _client())

    rows = result.summary()["stop_ladder"]["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["filled_level"] == 1
    assert row["target_price"] == "80500"
    assert row["target_source"] == "strategy_first_leg"
    assert row["would_action"] == "replace_stop"
    assert row["existing_stops"] == ["82300"]

    events = _events(session_factory)
    assert [event.action for event in events] == ["stop_ladder_would_replace"]
    assert events[0].status == "shadow"
    assert events[0].pos_id == POS
    assert events[0].execution_binding_id == binding_id
    detail = json.loads(events[0].after_json)
    assert detail["filled_level"] == 1
    assert detail["target_price"] == "80500"
    assert detail["rungs"][0]["trigger_price"] == "79800"


def test_shadow_records_the_second_rung_against_the_first_take_profit(tmp_path):
    session_factory, _binding_id = _seed(
        tmp_path, filled=["tp-79800", "tp-79100"]
    )

    result = _run(session_factory, _client(market="79090"))

    row = result.summary()["stop_ladder"]["rows"][0]
    assert (row["filled_level"], row["target_price"]) == (2, "79800")
    assert row["would_action"] == "replace_stop"


def test_shadow_records_a_market_close_when_the_target_has_been_passed(tmp_path):
    session_factory, _binding_id = _seed(
        tmp_path, filled=["tp-79800", "tp-79100"]
    )

    _run(session_factory, _client(market="79850"))

    assert [event.action for event in _events(session_factory)] == [
        "stop_ladder_would_close"
    ]


def test_shadow_records_no_change_when_the_resting_stop_is_tighter(tmp_path):
    session_factory, _binding_id = _seed(
        tmp_path, filled=["tp-79800", "tp-79100"], stop_price="79500"
    )

    _run(session_factory, _client(market="79200", stop_price="79500"))

    events = _events(session_factory)
    assert [event.action for event in events] == ["stop_ladder_no_change"]
    assert json.loads(events[0].after_json)["effective_stop_price"] == "79500"


def test_the_same_answer_twice_is_recorded_once(tmp_path):
    session_factory, _binding_id = _seed(tmp_path, filled=["tp-79800"])
    client = _client()

    _run(session_factory, client)
    _run(session_factory, client, now=NOW + timedelta(minutes=1))

    assert len(_events(session_factory)) == 1


def test_a_changed_level_is_recorded_again(tmp_path):
    session_factory, _binding_id = _seed(tmp_path, filled=["tp-79800"])
    client = _client()
    _run(session_factory, client)

    _mark_filled(session_factory, ["tp-79800", "tp-79100"])
    _run(session_factory, _client(market="79090"), now=NOW + timedelta(minutes=2))

    events = _events(session_factory)
    assert [json.loads(event.after_json)["filled_level"] for event in events] == [1, 2]


def test_disabled_writes_nothing_at_all(tmp_path):
    session_factory, _binding_id = _seed(
        tmp_path, mode="disabled", filled=["tp-79800"]
    )

    result = _run(session_factory, _client())

    assert result.summary()["stop_ladder"]["mode"] == "disabled"
    assert result.summary()["stop_ladder"]["rows"] == []
    assert _events(session_factory) == []


def test_the_default_setting_is_disabled(tmp_path):
    session_factory, _binding_id = _seed(tmp_path, mode=None, filled=["tp-79800"])

    _run(session_factory, _client())

    assert _events(session_factory) == []


def test_live_behaves_as_shadow_and_says_so(tmp_path, caplog):
    import logging

    session_factory, _binding_id = _seed(tmp_path, mode="live", filled=["tp-79800"])
    logger = logging.getLogger("telegram_kol_research")
    handler_records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            handler_records.append(record)

    handler = _Capture()
    logger.addHandler(handler)
    try:
        result = _run(session_factory, _client())
    finally:
        logger.removeHandler(handler)

    assert result.summary()["stop_ladder"]["mode"] == "shadow"
    assert result.summary()["stop_ladder"]["configured_mode"] == "live"
    assert any(
        "stop_ladder_mode=live is not released" in record.getMessage()
        for record in handler_records
    )
    assert [event.action for event in _events(session_factory)] == [
        "stop_ladder_would_replace"
    ]


def test_a_shadow_round_creates_no_position_mutation_intent(tmp_path):
    session_factory, _binding_id = _seed(
        tmp_path, filled=["tp-79800", "tp-79100"]
    )

    _run(session_factory, _client(market="79850"))

    with session_factory() as session:
        assert session.query(PositionMutationIntent).count() == 0


def test_a_position_with_no_filled_rung_records_no_change_only_once(tmp_path):
    session_factory, _binding_id = _seed(tmp_path)

    _run(session_factory, _client())
    _run(session_factory, _client(), now=NOW + timedelta(minutes=1))

    events = _events(session_factory)
    assert [event.action for event in events] == ["stop_ladder_no_change"]
    assert json.loads(events[0].after_json)["filled_level"] == 0
