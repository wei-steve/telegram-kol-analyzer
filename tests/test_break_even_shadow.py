"""The shadow reads a TPSL row as a TPSL row, and never writes anything.

Phase 6h. Every fixture here is shaped like the venue's actual response, which
is the point: a TPSL row from ``trigger-orders-pending`` carries ``ordId``,
``instId``, ``posSide``, ``slTriggerPrice`` and ``sz``, and **no ``posId`` at
all**; a position row carries ``posId``, ``avgPx``, ``lastPx`` and an
``slTriggerPx`` that reflects only the most recent write. Fixtures that invent
a ``posId`` on a TPSL row would let the defect under test pass.
"""

from datetime import UTC, datetime

import pytest

from telegram_kol_research.break_even_shadow import run_break_even_shadow_pass
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import PositionBackupStopOrder
from telegram_kol_research.position_protection_legs import (
    bind_filled_position,
    bind_verified_exchange_order,
    create_or_get_protection_leg,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 9, 10, 21, 30, tzinfo=UTC)
INST = "BTC-USDT-SWAP"
POS = "1001125216121996"


def _position(*, avg="77000", last="77156.6", sl="75548.6", pos="15"):
    """A position row exactly as the venue returns it.

    ``tpTriggerPx`` is present and empty on a stop-only position, and
    ``slTriggerPx`` here is the *backup* stop -- the most recent write -- while
    the primary at 75700 is still resting. Both are real, measured 2026-09-10.
    """

    return {
        "instId": INST, "instType": "SWAP", "posId": POS, "posSide": "long",
        "pos": pos, "avgPx": avg, "lastPx": last, "liqPx": "68116.6",
        "slTriggerPx": sl, "tpTriggerPx": "", "mgnMode": "cross",
        "mrgPosition": "split", "lever": "125",
    }


def _tpsl_row(order_id, price, *, sz="15"):
    """A TPSL row exactly as the venue returns it -- note: no ``posId`` key."""

    return {
        "ordId": order_id, "instId": INST, "instType": "SWAP", "posSide": "long",
        "side": "sell", "sz": sz, "slTriggerPrice": price, "slPrice": "0",
        "tpTriggerPrice": "0", "tpPrice": "0", "triggerOrderType": "TPSL",
        "triggerPx": "0", "triggerPxType": "last", "ordPx": "0", "ordType": "",
        "tdMode": "cross", "lever": "125",
        "closeSLPrice": "", "closeSLTriggerPrice": "",
        "closeTPPrice": "", "closeTPTriggerPrice": "",
    }


class _Client:
    """Reads only. Every write method raises."""

    def __init__(self, positions, pending, *, pending_raises=False):
        self._positions = positions
        self._pending = pending
        self.pending_raises = pending_raises

    def list_positions(self, *, inst_id=None):
        return [dict(row) for row in self._positions]

    def list_trigger_orders_pending(self, *, inst_id):
        if self.pending_raises:
            raise RuntimeError("read failed")
        return [dict(row) for row in self._pending]

    def set_position_sltp(self, payload):  # pragma: no cover - must never run
        raise AssertionError("the shadow must not write to the exchange")

    def cancel_position_sltp(self, payload):  # pragma: no cover - must never run
        raise AssertionError("the shadow must not write to the exchange")

    def place_order(self, payload):  # pragma: no cover - must never run
        raise AssertionError("the shadow must not write to the exchange")


def _seed(
    tmp_path,
    stops=(("1001125216121995", "75700"),),
    *,
    backup_order_id=None,
    backup_leg_order_id=None,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol", chat_id=1, message_id=1, symbol="BTC", side="long",
            venue="deepcoin", margin_mode="cross", position_mode="split",
            status="active",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, leg_index=1, purpose="entry",
            order_kind="limit", strategy_instance_id="deepcoin:1:1:BTC:long",
            venue="deepcoin", pos_id=POS, status="active",
            attribution_status="verified",
        ),
    )
    with session_factory() as session:
        for order_id, price in stops:
            upsert_protection_ledger_row(
                session, venue="deepcoin", execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                strategy_instance_id="deepcoin:1:1:BTC:long", pos_id=POS,
                instrument_id=INST, side="long", order_id=order_id,
                purpose="stop_loss", trigger_price=price, size_text="15",
                status="verified", evidence_source="test", evidence={},
                seen_at=NOW,
            )
        if backup_order_id is not None:
            session.add(
                PositionBackupStopOrder(
                    venue="deepcoin", execution_binding_id=binding_id,
                    execution_order_leg_id=leg_id, pos_id=POS,
                    instrument_id=INST, side="long", trigger_price="75548.6",
                    client_order_id="TK-test-backup", order_id=backup_order_id,
                    status="active", request_json="{}",
                    created_at=NOW, updated_at=NOW,
                )
            )
        if backup_leg_order_id is not None:
            leg = create_or_get_protection_leg(
                session, venue="deepcoin", execution_order_leg_id=leg_id,
                role="backup_stop", leg_index=1,
                planned_trigger_price="75548.6", planned_size="0",
            )
            bind_filled_position(session, leg, pos_id=POS)
            bind_verified_exchange_order(
                session, leg, exchange_order_id=backup_leg_order_id,
                readback_evidence={"ordId": backup_leg_order_id, "posId": POS},
            )
        session.commit()
    return session_factory


def _run(session_factory, client):
    return run_break_even_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )


def test_a_tpsl_row_resolves_even_though_it_carries_no_position_id(tmp_path):
    """The whole defect, stated as a passing test.

    The executor's predicate requires ``row["posId"] == pos_id`` on a row that
    has no ``posId``. The shadow attributes by order id, so the same row
    resolves -- and ``legacy_would_refuse`` records that the old predicate
    would have thrown it out.
    """

    session_factory = _seed(tmp_path)
    client = _Client([_position()], [_tpsl_row("1001125216121995", "75700")])

    result = _run(session_factory, client)

    assert result.positions_seen == 1
    row = result.rows[0]
    assert (row.stops_examined, row.stops_resolved) == (1, 1)
    assert row.legacy_would_refuse == 1
    assert row.reason_code is None
    assert row.action is not None


def test_the_price_is_read_by_the_venues_own_spelling(tmp_path):
    """``slTriggerPrice`` on a TPSL row, not ``slTriggerPx``.

    Deliberately paired with the position row above, which spells the same
    idea ``slTriggerPx`` and holds a *different* number (75548.6, the backup).
    A reader that reached for the position row's spelling finds nothing here;
    one that reached for the position row itself finds the wrong price.
    """

    session_factory = _seed(tmp_path)
    client = _Client([_position()], [_tpsl_row("1001125216121995", "75700")])

    row = _run(session_factory, client).rows[0]

    assert row.current_stop_prices == ("75700",)
    assert "75548.6" not in row.current_stop_prices


def test_a_price_the_venue_spells_differently_still_compares_equal(tmp_path):
    """6f-1, one layer down: 75700 and 75700.0 are the same stop."""

    session_factory = _seed(tmp_path, stops=(("1001125216121995", "75700.0"),))
    client = _Client([_position()], [_tpsl_row("1001125216121995", "75700")])

    row = _run(session_factory, client).rows[0]

    assert row.stops_resolved == 1
    assert row.reason_code is None


def test_break_even_replaces_the_primary_and_leaves_the_backup_alone(tmp_path):
    """The replacement set is the primary stop only. Ruled 2026-09-10.

    Since phase 6f a protected position carries a primary and a backup, and
    both sit below the entry price on a long, so neither qualifies as
    break-even and the decision is ``set_break_even``. The executor collects
    *every* ledger stop row, so left alone it would cancel both and leave one
    stop at the entry price -- the position losing its backup as a side effect
    of tightening its primary.

    The backup is not re-priced here. ``trigger_backup_stop_executor``
    recomputes it from the new primary on a later round and replaces the old
    one in the A-5e order, which is the only path that has ever placed one.
    """

    session_factory = _seed(
        tmp_path,
        stops=(("1001125216121995", "75700"), ("1001125219289222", "75548.6")),
        backup_order_id="1001125219289222",
    )
    client = _Client(
        [_position()],
        [
            _tpsl_row("1001125216121995", "75700"),
            _tpsl_row("1001125219289222", "75548.6", sz="0"),
        ],
    )

    row = _run(session_factory, client).rows[0]

    assert row.action == "set_break_even"
    assert row.target_stop_price == "77000"
    assert row.would_cancel_order_ids == ("1001125216121995",)
    assert "1001125219289222" not in row.would_cancel_order_ids
    assert row.current_stop_prices == ("75700", "75548.6")
    assert row.legacy_would_refuse == 2


def test_a_backup_recorded_only_as_a_protection_leg_is_still_spared(tmp_path):
    """Either record is enough to mark an order a backup; the union is the point."""

    session_factory = _seed(
        tmp_path,
        stops=(("1001125216121995", "75700"), ("1001125219289222", "75548.6")),
        backup_leg_order_id="1001125219289222",
    )
    client = _Client(
        [_position()],
        [
            _tpsl_row("1001125216121995", "75700"),
            _tpsl_row("1001125219289222", "75548.6", sz="0"),
        ],
    )

    row = _run(session_factory, client).rows[0]

    assert row.would_cancel_order_ids == ("1001125216121995",)


def test_a_primary_that_cannot_be_named_exactly_refuses(tmp_path):
    """Unknown is not a subset.

    Two stops and neither recorded as a backup: the shadow cannot say which
    one a break-even should replace, so it names none. Cancelling "the ones we
    could identify" would be a guess with a live position behind it.
    """

    session_factory = _seed(
        tmp_path,
        stops=(("1001125216121995", "75700"), ("1001125219289222", "75548.6")),
    )
    client = _Client(
        [_position()],
        [
            _tpsl_row("1001125216121995", "75700"),
            _tpsl_row("1001125219289222", "75548.6", sz="0"),
        ],
    )

    row = _run(session_factory, client).rows[0]

    assert row.action == "set_break_even"
    assert row.would_cancel_order_ids == ()
    assert row.reason_code == "primary_stop_not_exactly_one:2"


def test_a_stop_already_at_break_even_is_kept_and_cancels_nothing(tmp_path):
    """The negative half: not every resolved position becomes a replacement."""

    session_factory = _seed(tmp_path, stops=(("1001125216121995", "77500"),))
    client = _Client([_position()], [_tpsl_row("1001125216121995", "77500")])

    row = _run(session_factory, client).rows[0]

    assert row.action == "keep_tighter_stop"
    assert row.would_cancel_order_ids == ()


def test_an_unreadable_pending_list_is_unknown_not_unprotected(tmp_path):
    """Hard rule 4. A failed read must not read as "this position has no stop"."""

    session_factory = _seed(tmp_path)
    client = _Client([_position()], [], pending_raises=True)

    result = _run(session_factory, client)

    assert result.read_failures == (INST,)
    assert result.rows[0].reason_code == "pending_unreadable"
    assert result.rows[0].action is None
    assert result.would_cancel_total == 0


def test_a_stop_the_exchange_does_not_have_is_refused(tmp_path):
    session_factory = _seed(tmp_path)
    client = _Client([_position()], [_tpsl_row("some-other-order", "75700")])

    row = _run(session_factory, client).rows[0]

    assert row.reason_code == "ledger_stop_absent_from_exchange"
    assert row.would_cancel_order_ids == ()


def test_a_row_claiming_another_position_is_refused(tmp_path):
    """A position id may contradict; it may never be required.

    TPSL rows carry none, so this only fires on rows that do carry one -- and
    then it means the order id matched something belonging elsewhere, which is
    a genuine conflict rather than the absence the executor mistook for one.
    """

    session_factory = _seed(tmp_path)
    claimed = _tpsl_row("1001125216121995", "75700") | {"posId": "9999999999"}
    client = _Client([_position()], [claimed])

    row = _run(session_factory, client).rows[0]

    assert row.reason_code == "stop_order_claimed_by_another_position"
    assert row.would_cancel_order_ids == ()


def test_the_shadow_writes_nothing(tmp_path):
    """Every write method on the client raises; reaching one fails the test."""

    session_factory = _seed(
        tmp_path,
        stops=(("1001125216121995", "75700"), ("1001125219289222", "75548.6")),
        backup_order_id="1001125219289222",
    )
    client = _Client(
        [_position()],
        [
            _tpsl_row("1001125216121995", "75700"),
            _tpsl_row("1001125219289222", "75548.6", sz="0"),
        ],
    )

    result = _run(session_factory, client)

    # It decided to cancel the primary, and still cancelled nothing. A run
    # that decided nothing would satisfy "wrote nothing" for the wrong reason.
    assert result.would_cancel_total == 1
    with session_factory() as session:
        from telegram_kol_research.models import PositionMutationIntent

        assert session.query(PositionMutationIntent).count() == 0


def test_the_summary_carries_every_field_the_round_log_needs(tmp_path):
    session_factory = _seed(tmp_path)
    client = _Client([_position()], [_tpsl_row("1001125216121995", "75700")])

    summary = _run(session_factory, client).summary()

    assert set(summary) == {
        "positions_seen", "counts_by_action", "stops_examined", "stops_resolved",
        "legacy_would_refuse", "would_cancel_total", "would_close_positions",
        "read_failures", "rows",
    }
    assert summary["rows"][0]["pos_id"] == POS
    assert summary["legacy_would_refuse"] == 1


@pytest.mark.parametrize(
    ("bad_price_key", "bad_price"),
    [("slTriggerPx", "75700"), ("stopLossPrice", "75700")],
)
def test_the_reader_accepts_the_other_spellings_too(
    tmp_path, bad_price_key, bad_price
):
    """Rows recorded before phase 5 spell it differently; the reader knows all three."""

    session_factory = _seed(tmp_path)
    row = _tpsl_row("1001125216121995", "75700")
    del row["slTriggerPrice"]
    row[bad_price_key] = bad_price
    client = _Client([_position()], [row])

    assert _run(session_factory, client).rows[0].stops_resolved == 1


def test_a_full_exit_row_says_what_the_branch_would_actually_do(tmp_path):
    """``full_exit`` is a market close, and the row has to say so.

    Recording only the action name tells a reviewer which branch runs and
    nothing about what it does -- the unreviewable record phase 6f already had
    to fix once. ``cancels_stops_first`` is asserted explicitly because it is
    the one fact not visible from the payload: this branch issues no cancel,
    so the stops resting on the position are untouched by it.
    """

    session_factory = _seed(tmp_path)
    # Below the entry price on a long: moving the stop to entry would trigger
    # at once, so the policy answers full_exit instead.
    client = _Client(
        [_position(last="76500")], [_tpsl_row("1001125216121995", "75700")]
    )

    result = _run(session_factory, client)
    row = result.rows[0]

    assert row.action == "full_exit"
    assert row.would_close_size == "15"
    assert row.would_close_endpoint == "close_position"
    assert row.would_close_ord_type == "market"
    assert row.would_close_cancels_stops_first is False
    assert row.would_cancel_order_ids == ()
    assert result.would_close_positions == 1
    assert result.summary()["rows"][0]["would_close_size"] == "15"


def test_a_replacement_row_carries_no_close_fields(tmp_path):
    """The negative half: the close fields appear only on the close branch."""

    session_factory = _seed(tmp_path)
    client = _Client([_position()], [_tpsl_row("1001125216121995", "75700")])

    row = _run(session_factory, client).rows[0]

    assert row.action == "set_break_even"
    assert row.would_close_size is None
    assert row.would_close_endpoint is None
    assert row.would_close_cancels_stops_first is None
