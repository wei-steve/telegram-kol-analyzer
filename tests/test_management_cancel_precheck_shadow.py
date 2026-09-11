"""The management cancel shadow records a verdict and changes nothing.

Phase 6g. Fixtures are venue-shaped: a TPSL row from ``trigger-orders-pending``
carries ``ordId``, ``instId``, ``posSide``, ``slTriggerPrice`` and ``sz``, and
no ``posId``. Inventing one here would let the reading under test pass for the
wrong reason -- that is how the take-profit defect survived three months.
"""

from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.management_cancel_precheck_shadow import (
    PATH_AFTER_REPLACEMENT,
    SHADOW_EVENT_ACTION,
    VERDICT_UNCHANGED,
    observe_cancel_precheck,
)
from telegram_kol_research.models import (
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionMutationIntent,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"
POS = "pos-1"
STOP = "stop-1"


def _tpsl_row(order_id=STOP, price="75700", sz="15"):
    return {
        "ordId": order_id, "instId": INST, "instType": "SWAP", "posSide": "long",
        "side": "sell", "sz": sz, "slTriggerPrice": price, "slPrice": "0",
        "tpTriggerPrice": "0", "triggerOrderType": "TPSL",
        "triggerPx": "0", "triggerPxType": "last", "tdMode": "cross",
    }


class _Client:
    """Reads only; every write method fails the test if reached."""

    def __init__(self, pending, *, raises=False):
        self._pending = pending
        self.raises = raises

    def list_trigger_orders_pending(self, *, inst_id):
        if self.raises:
            raise RuntimeError("read failed")
        return [dict(row) for row in self._pending]

    def set_position_sltp(self, payload):  # pragma: no cover
        raise AssertionError("the shadow must not write to the exchange")

    def cancel_position_sltp(self, payload):  # pragma: no cover
        raise AssertionError("the shadow must not write to the exchange")

    def place_order(self, payload):  # pragma: no cover
        raise AssertionError("the shadow must not write to the exchange")


def _seed(tmp_path, *, price="75700", size="15"):
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
            order_kind="market", strategy_instance_id="deepcoin:1:1:BTC:long",
            venue="deepcoin", pos_id=POS, status="active",
            attribution_status="verified",
        ),
    )
    with session_factory() as session:
        # The chain only treats an entry leg as verified when its attribution
        # evidence is present; without this the authority refuses and every
        # verdict below would be "target not resolved" -- a refusal for a
        # fixture's reason, which reads exactly like a refusal for a real one.
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_evidence_json = '{"policy_version":2}'
        upsert_protection_ledger_row(
            session, venue="deepcoin", execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            strategy_instance_id="deepcoin:1:1:BTC:long", pos_id=POS,
            instrument_id=INST, side="long", order_id=STOP,
            purpose="stop_loss", trigger_price=price, size_text=size,
            status="verified", evidence_source="test", evidence={},
            seen_at=NOW,
        )
        session.commit()
    return session_factory


def _observe(session_factory, client, order_id=STOP):
    return observe_cancel_precheck(
        session_factory,
        deepcoin_client=client,
        pos_id=POS,
        instrument_id=INST,
        side="long",
        order_id=order_id,
        path=PATH_AFTER_REPLACEMENT,
        batch_id=163,
        leg_id=141,
        now=NOW,
    )


def _events(session_factory):
    with session_factory() as session:
        return (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == SHADOW_EVENT_ACTION)
            .all()
        )


def test_an_unchanged_order_is_recorded_as_unchanged(tmp_path):
    session_factory = _seed(tmp_path)

    observation = _observe(session_factory, _Client([_tpsl_row()]))

    assert observation is not None
    assert observation.verdict == VERDICT_UNCHANGED
    assert observation.recorded is True
    events = _events(session_factory)
    assert len(events) == 1
    assert events[0].order_id == STOP


def test_a_resized_order_is_not_unchanged(tmp_path):
    """The defect this shadow exists to measure: it is no longer that order."""

    session_factory = _seed(tmp_path, size="15")

    observation = _observe(session_factory, _Client([_tpsl_row(sz="7")]))

    assert observation.verdict != VERDICT_UNCHANGED


def test_a_repriced_order_is_not_unchanged(tmp_path):
    session_factory = _seed(tmp_path, price="75700")

    observation = _observe(session_factory, _Client([_tpsl_row(price="76000")]))

    assert observation.verdict != VERDICT_UNCHANGED


def test_the_same_price_spelled_differently_is_still_unchanged(tmp_path):
    """6f-1 one layer on: 75700 and 75700.0 are the same stop, not a change."""

    session_factory = _seed(tmp_path, price="75700.0")

    observation = _observe(session_factory, _Client([_tpsl_row(price="75700")]))

    assert observation.verdict == VERDICT_UNCHANGED


def test_an_order_the_exchange_no_longer_lists_is_not_unchanged(tmp_path):
    session_factory = _seed(tmp_path)

    observation = _observe(session_factory, _Client([_tpsl_row("someone-else")]))

    assert observation.verdict != VERDICT_UNCHANGED


def test_an_unreadable_pending_list_is_not_permission(tmp_path):
    """Hard rule 4. A failed read must not read as "still the same order"."""

    session_factory = _seed(tmp_path)

    observation = _observe(session_factory, _Client([], raises=True))

    assert observation is not None
    assert observation.verdict != VERDICT_UNCHANGED


def test_the_observation_carries_a_real_clock_not_a_batch_stamp(tmp_path):
    """The whole point of the wall clock field, asserted by name.

    Every intent a management batch writes carries one ``executed_at``, so the
    interval between stripping protection and replacing it reads as zero
    across every batch on record. This field is what makes it answerable.
    """

    import json

    session_factory = _seed(tmp_path)

    _observe(session_factory, _Client([_tpsl_row()]))

    payload = json.loads(_events(session_factory)[0].after_json)
    assert payload["observed_at_wall"] == NOW.isoformat()
    assert payload["path"] == PATH_AFTER_REPLACEMENT
    assert payload["batch_id"] == 163
    assert payload["shadow_only"] is True


def test_the_shadow_creates_no_mutation_intent(tmp_path):
    """It observes a write about to happen; it must not be one."""

    session_factory = _seed(tmp_path)

    _observe(session_factory, _Client([_tpsl_row()]))

    with session_factory() as session:
        assert session.query(PositionMutationIntent).count() == 0


def test_a_broken_shadow_never_reaches_the_caller(tmp_path):
    """A shadow that breaks a cancel is worse than the defect it measures."""

    session_factory = _seed(tmp_path)

    class _Exploding:
        def list_trigger_orders_pending(self, *, inst_id):
            raise RuntimeError("boom")

    # No exception escapes, and the caller gets an answer it can ignore.
    observation = _observe(session_factory, _Exploding())

    assert observation is None or observation.verdict != VERDICT_UNCHANGED


def test_the_first_observation_raises_an_incident(tmp_path, monkeypatch):
    """Its cadence is the reason, not its severity.

    A management protection replacement happens about once every four days, so
    no window contains one and nothing prompts a person to look. The first
    observation has to say so by itself.
    """

    captured = []
    import telegram_kol_research.management_cancel_precheck_shadow as shadow

    def _fake(session_factory, *, observation, first):
        captured.append((observation.verdict, first))

    monkeypatch.setattr(shadow, "_capture_incident", _fake)
    session_factory = _seed(tmp_path)
    client = _Client([_tpsl_row()])

    _observe(session_factory, client)
    _observe(session_factory, client)

    # First observation alerts; the second, also unchanged, does not.
    assert captured == [(VERDICT_UNCHANGED, True)]


def test_every_mismatch_raises_an_incident_even_when_not_first(
    tmp_path, monkeypatch
):
    captured = []
    import telegram_kol_research.management_cancel_precheck_shadow as shadow

    monkeypatch.setattr(
        shadow,
        "_capture_incident",
        lambda session_factory, *, observation, first: captured.append(
            (observation.verdict, first)
        ),
    )
    session_factory = _seed(tmp_path)

    _observe(session_factory, _Client([_tpsl_row()]))          # first, unchanged
    _observe(session_factory, _Client([_tpsl_row(sz="7")]))    # later, resized

    assert len(captured) == 2
    assert captured[0][1] is True
    assert captured[1][0] != VERDICT_UNCHANGED
    assert captured[1][1] is False
