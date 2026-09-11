"""The naked-fill shadow needs fill evidence, and NULL is the case it is for.

Phase 6 follow-up. Two things these tests exist to pin, both of which would
otherwise produce a pass that means nothing:

* a leg with **no** fill evidence is not a candidate. Every historical match of
  the raw predicate -- submitted, sixty seconds on, still no position id -- was
  a resting order that got cancelled, and an order that never filled has no
  position to be naked. A shadow counting those would hand the release
  condition ("at least one sample whose verdict matches the design") a sample
  that is not the thing being studied;
* a leg whose ``attribution_status`` is ``"unassigned"`` must be examined --
  that is what a failed attribution actually lands as, since the column is
  ``NOT NULL DEFAULT 'unassigned'`` and the writer passes ``None``. The NULL
  arm of the filter is defensive rather than live, and the test below asserts
  the constraint that makes it so, because a comment claiming NULL "is the
  case this exists for" would be false and would stay false.
"""

from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import ExecutionEvent, PositionMutationIntent
from telegram_kol_research.naked_fill_shadow import (
    FILL_EVIDENCE_NONE,
    FILL_EVIDENCE_ORDER_HISTORY,
    SHADOW_EVENT_ACTION,
    run_naked_fill_shadow_pass,
)


NOW = datetime(2026, 9, 11, 16, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(minutes=30)
INST = "BTC-USDT-SWAP"
ORDER = "order-1"


class _Client:
    """Reads only. Every write method fails the test if reached."""

    def __init__(self, *, history=(), positions=(), pending=()):
        self._history = list(history)
        self._positions = list(positions)
        self._pending = list(pending)

    def list_order_history(self, *, inst_id=None):
        return [dict(row) for row in self._history]

    def list_positions(self, *, inst_id=None):
        return [dict(row) for row in self._positions]

    def list_trigger_orders_pending(self, *, inst_id):
        return [dict(row) for row in self._pending]

    def set_position_sltp(self, payload):  # pragma: no cover
        raise AssertionError("the shadow must not write to the exchange")

    def place_order(self, payload):  # pragma: no cover
        raise AssertionError("the shadow must not write to the exchange")

    def cancel_position_sltp(self, payload):  # pragma: no cover
        raise AssertionError("the shadow must not write to the exchange")


def _seed(tmp_path, *, attribution_status=None, status="active", order_kind="limit"):
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
            order_kind=order_kind, strategy_instance_id="deepcoin:1:1:BTC:long",
            venue="deepcoin", status=status, order_id=ORDER,
            attribution_status=attribution_status,
            request={"instId": INST, "posSide": "long", "sz": "5",
                     "slTriggerPx": "75000"},
        ),
    )
    with session_factory() as session:
        from telegram_kol_research.models import ExecutionOrderLeg

        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.updated_at = LONG_AGO
        leg.created_at = LONG_AGO
        session.commit()
    return session_factory


def _filled_history():
    return [{"ordId": ORDER, "instId": INST, "state": "filled", "accFillSz": "5"}]


def _run(session_factory, client):
    return run_naked_fill_shadow_pass(
        session_factory, deepcoin_client=client, now=NOW
    )


def test_a_leg_with_no_fill_evidence_is_not_a_candidate(tmp_path):
    """A resting order that never filled has no position and cannot be naked."""

    session_factory = _seed(tmp_path)
    client = _Client(history=[{"ordId": ORDER, "state": "live", "accFillSz": "0"}])

    result = _run(session_factory, client)

    assert result.legs_examined == 1
    assert result.candidates_with_fill_evidence == 0
    assert result.skipped_no_fill_evidence == 1
    assert result.rows == ()
    with session_factory() as session:
        assert (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == SHADOW_EVENT_ACTION)
            .count()
            == 0
        )


def test_attribution_status_cannot_be_null(tmp_path):
    """Pins why the filter's NULL arm is defensive rather than the live case.

    The first version of this test tried to write a NULL and was stopped by an
    IntegrityError. That is the answer, not an obstacle: the column is NOT NULL
    with a default, so a failed attribution lands as "unassigned". Asserting
    the constraint keeps the module's comment about it honest -- if the default
    is ever dropped, this turns red and the comment gets revisited with it.
    """

    import pytest
    from sqlalchemy.exc import IntegrityError

    from telegram_kol_research.models import ExecutionOrderLeg

    session_factory = _seed(tmp_path)
    with pytest.raises(IntegrityError):
        with session_factory() as session:
            leg = session.query(ExecutionOrderLeg).one()
            leg.attribution_status = None
            session.commit()


def test_an_unassigned_attribution_status_is_examined(tmp_path):
    """And this is what production actually holds.

    ``upsert_execution_order_leg`` turns a None into "unassigned", so the
    failed-attribution write from recovery_live_submit lands as that rather
    than as NULL -- which is exactly what every one of the nineteen historical
    matches carries. Both are covered because the code has to survive either.
    """

    session_factory = _seed(tmp_path, attribution_status="unassigned")

    result = _run(session_factory, _Client(history=_filled_history()))

    assert result.legs_examined == 1
    assert result.rows[0].attribution_status == "unassigned"


def test_a_terminal_leg_is_not_examined(tmp_path):
    """Cancelled is the state all nineteen historical matches were actually in."""

    session_factory = _seed(tmp_path, status="cancelled")
    client = _Client(history=_filled_history())

    result = _run(session_factory, client)

    assert result.legs_examined == 0
    assert result.rows == ()


def test_a_verified_leg_is_not_examined(tmp_path):
    session_factory = _seed(tmp_path, attribution_status="verified")

    result = _run(session_factory, _Client(history=_filled_history()))

    assert result.legs_examined == 0


def test_a_limit_entry_is_examined_which_the_live_net_would_skip(tmp_path):
    """The widening this shadow exists to measure.

    Every historical match was a limit entry, and the live net admits only
    market ones -- so this is the case that has always been invisible.
    """

    session_factory = _seed(tmp_path, order_kind="limit")

    result = _run(session_factory, _Client(history=_filled_history()))

    assert result.legs_examined == 1
    assert result.rows[0].order_kind == "limit"
    # The assertion that matters is on the *decision*, not on the row existing.
    # `legs_examined` counts what this module's own query returned, so it stays
    # 1 even if the widening is removed; only the reason shows whether
    # precondition (a) actually admitted the leg. Mutation-checked: narrowing
    # entry_order_kinds back to {"market"}, or restoring
    # require_unverified_attribution, turns this red and the two above did not.
    assert result.rows[0].reason != "not_an_unverified_market_entry"


def test_the_widening_is_what_admits_it_not_the_query(tmp_path):
    """The same leg, judged by the live net's own defaults, is skipped.

    Pins the difference this shadow exists to measure, by asking the decision
    function both ways on one leg.
    """

    from telegram_kol_research.naked_fill_stop_net import (
        LIVE_ENTRY_ORDER_KINDS,
        evaluate_naked_fill,
    )

    session_factory = _seed(tmp_path, order_kind="limit")
    with session_factory() as session:
        from telegram_kol_research.models import ExecutionOrderLeg

        leg_id = session.query(ExecutionOrderLeg).one().id

    live = evaluate_naked_fill(
        session_factory, leg_id=leg_id, live_positions=[], now=NOW
    )
    widened = evaluate_naked_fill(
        session_factory,
        leg_id=leg_id,
        live_positions=[],
        now=NOW,
        entry_order_kinds=frozenset({"market", "limit"}),
        require_unverified_attribution=False,
    )

    assert live.reason == "not_an_unverified_market_entry"
    assert widened.reason != "not_an_unverified_market_entry"
    # And the live default has not moved. Releasing the net is a separate,
    # approved step; it must not happen as a side effect of building a shadow.
    assert LIVE_ENTRY_ORDER_KINDS == frozenset({"market"})


def test_the_record_says_where_the_fill_evidence_came_from(tmp_path):
    """Without it a reader cannot tell a real candidate from a resting order."""

    import json

    session_factory = _seed(tmp_path)

    _run(session_factory, _Client(history=_filled_history()))

    with session_factory() as session:
        event = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == SHADOW_EVENT_ACTION)
            .one()
        )
    payload = json.loads(event.after_json)
    assert payload["fill_evidence"] == FILL_EVIDENCE_ORDER_HISTORY
    assert "state=filled" in (payload["fill_evidence_detail"] or "")
    assert payload["order_kind"] == "limit"
    assert payload["shadow_only"] is True
    assert "preconditions" in payload


def test_the_shadow_writes_nothing_to_the_exchange_or_the_intents(tmp_path):
    session_factory = _seed(tmp_path)
    client = _Client(
        history=_filled_history(),
        positions=[{"posId": "pos-1", "instId": INST, "posSide": "long", "pos": "5"}],
        pending=[],
    )

    result = _run(session_factory, client)

    assert result.candidates_with_fill_evidence == 1
    with session_factory() as session:
        assert session.query(PositionMutationIntent).count() == 0


def test_an_unreadable_position_snapshot_is_not_a_verdict(tmp_path):
    """Hard rule 4, inherited from the decision function this calls."""

    class _NoPositions(_Client):
        def list_positions(self, *, inst_id=None):
            raise RuntimeError("read failed")

    session_factory = _seed(tmp_path)
    client = _NoPositions(history=_filled_history())

    result = _run(session_factory, client)

    assert result.read_failures
    assert result.rows[0].action != "attach"
