"""Adoption writes one ledger row and nothing else -- never an exchange order."""

from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    DeepcoinWsEvent,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLedger,
)
from telegram_kol_research.protection_adoption import (
    ADOPTION_EVENT_ACTION,
    run_protection_adoption_pass,
)


NOW = datetime(2026, 9, 10, 19, 0, tzinfo=UTC)
INST = "BTC-USDT-SWAP"


class _Client:
    """Every write method raises: adoption must never reach the exchange."""

    def __init__(self, pending, *, pending_raises=False):
        self.pending = pending
        self.pending_raises = pending_raises

    def list_positions(self, *, inst_id=None):
        return [
            {"instId": INST, "posId": "pos-1", "posSide": "long", "pos": "15"}
        ]

    def list_trigger_orders_pending(self, *, inst_id):
        if self.pending_raises:
            raise RuntimeError("read failed")
        return list(self.pending)

    def set_position_sltp(self, payload):  # pragma: no cover - must never run
        raise AssertionError("adoption must not write to the exchange")

    def cancel_position_sltp(self, payload):  # pragma: no cover - must never run
        raise AssertionError("adoption must not write to the exchange")

    def place_order(self, payload):  # pragma: no cover - must never run
        raise AssertionError("adoption must not write to the exchange")


def _stop_row(order_id, *, price="75700", size="15", pos_side="long"):
    return {
        "ordId": order_id,
        "instId": INST,
        "posSide": pos_side,
        "triggerOrderType": "TPSL",
        "slTriggerPrice": price,
        "sz": size,
    }


def _seed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol",
            chat_id=1,
            message_id=1,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            purpose="entry",
            order_kind="limit",
            strategy_instance_id="deepcoin:1:1:BTC:long",
            venue="deepcoin",
            pos_id="pos-1",
            status="active",
            attribution_status="verified",
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_evidence_json = '{"policy_version":2}'
        session.add(
            DeepcoinWsEvent(
                venue="deepcoin",
                channel="TriggerOrder",
                action="push",
                order_sys_id="entry-stop",
                trade_unit_id="pos-1",
                received_at=NOW,
                received_ms=1,
                raw_payload="{}",
                payload_hash="hash-entry-stop",
            )
        )
        session.commit()
    return session_factory, binding_id, leg_id


def _ledger(session_factory):
    with session_factory() as session:
        return [
            (row.order_id, row.purpose, row.status, row.evidence_source)
            for row in session.query(PositionProtectionLedger).all()
        ]


def test_a_stop_only_the_exchange_knows_is_written_into_the_ledger(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    client = _Client([_stop_row("entry-stop")])

    result = run_protection_adoption_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert (result.positions_seen, result.adopted_rows) == (1, 1)
    assert _ledger(session_factory) == [
        ("entry-stop", "stop_loss", "verified", "exchange_adopted_by_tu")
    ]
    with session_factory() as session:
        events = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == ADOPTION_EVENT_ACTION)
            .all()
        )
    assert len(events) == 1
    assert events[0].pos_id == "pos-1"
    assert "entry-stop" in (events[0].after_json or "")


def test_a_second_pass_adopts_nothing(tmp_path):
    """Idempotent by construction: a row the ledger names is not a candidate."""

    session_factory, _, _ = _seed(tmp_path)
    client = _Client([_stop_row("entry-stop")])

    run_protection_adoption_pass(session_factory, deepcoin_client=client, now=NOW)
    second = run_protection_adoption_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert second.adopted_rows == 0
    assert len(_ledger(session_factory)) == 1
    with session_factory() as session:
        assert (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == ADOPTION_EVENT_ACTION)
            .count()
            == 1
        )


def test_a_resting_entrys_own_stop_is_never_adopted(tmp_path):
    """It belongs to an order that has not filled; it protects nothing yet."""

    session_factory, binding_id, _ = _seed(tmp_path)
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=9,
            purpose="entry",
            order_kind="limit",
            strategy_instance_id="deepcoin:1:1:BTC:long",
            venue="deepcoin",
            status="pending",
            attribution_status="unassigned",
            order_id="resting-entry",
            request={
                "instId": INST,
                "posSide": "long",
                "ordType": "limit",
                "px": "80000",
                "sz": "6",
                "slTriggerPx": "81000",
                "tdMode": "cross",
            },
        ),
    )
    with session_factory() as session:
        session.add(
            DeepcoinWsEvent(
                venue="deepcoin",
                channel="TriggerOrder",
                action="push",
                order_sys_id="resting-entry-stop",
                trade_unit_id="default",
                received_at=NOW,
                received_ms=2,
                raw_payload="{}",
                payload_hash="hash-resting",
            )
        )
        session.commit()
    resting = _stop_row("resting-entry-stop", price="81000", size="6")
    client = _Client([_stop_row("entry-stop"), resting])

    result = run_protection_adoption_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result.adopted_rows == 1
    assert [row[0] for row in _ledger(session_factory)] == ["entry-stop"]


def test_an_unreadable_pending_list_adopts_nothing(tmp_path):
    """Unknown is not "the ledger may be filled in from what we did not read".

    Note what this test can and cannot pin. Substituting ``[]`` for ``None`` on
    a failed read does **not** turn it red, because an empty pending list also
    yields nothing to adopt -- at this layer the two are indistinguishable by
    outcome. The distinction is enforced one level down, in
    ``protection_authority.resolve_protection_authority``, where ``None``
    freezes and ``[]`` resolves-with-nothing, and it is mutation-checked there
    (``test_unreadable_pending_list_is_unknown_not_unprotected``). What this
    test pins is the pass-level consequence: nothing adopted, and the failed
    read surfaced rather than swallowed.
    """

    session_factory, _, _ = _seed(tmp_path)
    client = _Client([], pending_raises=True)

    result = run_protection_adoption_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result.adopted_rows == 0
    assert result.read_failures == (INST,)
    assert _ledger(session_factory) == []


def test_an_order_no_frame_can_place_is_refused_and_recorded(tmp_path):
    session_factory, _, _ = _seed(tmp_path)
    client = _Client([_stop_row("entry-stop"), _stop_row("stranger")])

    result = run_protection_adoption_pass(
        session_factory, deepcoin_client=client, now=NOW
    )

    assert result.adopted_rows == 0
    assert result.refused_positions == 1
    assert _ledger(session_factory) == []
    with session_factory() as session:
        refusals = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "protection_adoption_refused")
            .all()
        )
    assert len(refusals) == 1
    assert "stranger" in (refusals[0].after_json or "")
