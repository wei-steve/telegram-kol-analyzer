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
    ExecutionOrderLeg,
    PositionProtectionLedger,
)
from telegram_kol_research.protection_authority import (
    ADOPTION_EVIDENCE_SOURCE,
    FREEZE_COMBINED_TPSL_ORDER,
    FREEZE_ORDER_UNATTRIBUTABLE,
    FREEZE_PENDING_READ_INCOMPLETE,
    FREEZE_POSITION_NOT_VERIFIED,
    GROUP_STOP,
    GROUP_TAKE_PROFIT,
    adopt_protection_orders,
    resolve_protection_authority,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
INST = "ETH-USDT-SWAP"


def _seed(tmp_path, *, attribution_status="verified"):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol",
            chat_id=1,
            message_id=1,
            symbol="ETH",
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
            order_kind="market",
            strategy_instance_id="deepcoin:1:1:ETH:long",
            venue="deepcoin",
            pos_id="pos-1",
            status="active",
            attribution_status=attribution_status,
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_evidence_json = '{"policy_version":2}'
        session.commit()
    return session_factory, binding_id, leg_id


def _ledger(session_factory, *, binding_id, leg_id, order_id, purpose, price, size="4"):
    with session_factory() as session:
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            strategy_instance_id=None,
            pos_id="pos-1",
            instrument_id=INST,
            side="long",
            order_id=order_id,
            purpose=purpose,
            trigger_price=price,
            size_text=size,
            status="verified",
            evidence_source="test",
            evidence={},
            seen_at=NOW,
        )
        session.commit()


def _stop_row(order_id, price="2500", pos_side="long", size="4"):
    return {
        "ordId": order_id,
        "instId": INST,
        "posSide": pos_side,
        "triggerOrderType": "TPSL",
        "slTriggerPrice": price,
        "sz": size,
    }


def _take_profit_row(order_id, price="3000", pos_side="long", size="2"):
    return {
        "ordId": order_id,
        "instId": INST,
        "posSide": pos_side,
        "triggerOrderType": "TPSL",
        "tpTriggerPrice": price,
        "sz": size,
    }


def _ws_trigger_frame(order_id, trade_unit_id):
    return DeepcoinWsEvent(
        venue="deepcoin",
        channel="TriggerOrder",
        action="push",
        order_sys_id=order_id,
        trade_unit_id=trade_unit_id,
        received_at=NOW,
        received_ms=1,
        raw_payload="{}",
        payload_hash="hash-" + order_id,
    )


def _resolve(session_factory, pending_rows):
    with session_factory() as session:
        return resolve_protection_authority(
            session,
            venue="deepcoin",
            pos_id="pos-1",
            instrument_id=INST,
            side="long",
            pending_rows=pending_rows,
        )


def test_ledger_rows_are_grouped_by_purpose(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _ledger(
        session_factory,
        binding_id=binding_id,
        leg_id=leg_id,
        order_id="stop-1",
        purpose="stop_loss",
        price="2500",
    )
    _ledger(
        session_factory,
        binding_id=binding_id,
        leg_id=leg_id,
        order_id="tp-1",
        purpose="take_profit",
        price="3000",
    )

    authority = _resolve(session_factory, [_stop_row("stop-1"), _take_profit_row("tp-1")])

    assert authority.resolved
    assert [item.order_id for item in authority.group(GROUP_STOP)] == ["stop-1"]
    assert [item.order_id for item in authority.group(GROUP_TAKE_PROFIT)] == ["tp-1"]
    assert authority.adoptions == ()


def test_trade_unit_frame_adopts_an_order_the_ledger_does_not_know(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _ledger(
        session_factory,
        binding_id=binding_id,
        leg_id=leg_id,
        order_id="stop-1",
        purpose="stop_loss",
        price="2500",
    )
    with session_factory() as session:
        session.add(_ws_trigger_frame("stop-2", "pos-1"))
        session.commit()

    authority = _resolve(
        session_factory, [_stop_row("stop-1"), _stop_row("stop-2", price="2535.06")]
    )

    assert authority.resolved
    assert sorted(item.order_id for item in authority.group(GROUP_STOP)) == [
        "stop-1",
        "stop-2",
    ]
    assert [item.order_id for item in authority.adoptions] == ["stop-2"]
    assert authority.adoptions[0].trigger_price == "2535.06"


def test_trade_unit_naming_another_position_is_not_ours(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    with session_factory() as session:
        session.add(_ws_trigger_frame("stop-9", "pos-other"))
        session.commit()

    authority = _resolve(session_factory, [_stop_row("stop-9")])

    assert authority.resolved
    assert authority.order_ids == ()
    assert authority.adoptions == ()


def test_unattributable_protection_row_on_our_side_freezes(tmp_path):
    session_factory, _, _ = _seed(tmp_path)

    authority = _resolve(session_factory, [_stop_row("stop-unknown")])

    assert authority.status == "frozen"
    assert authority.reason_code == FREEZE_ORDER_UNATTRIBUTABLE
    assert authority.unattributable_order_ids == ("stop-unknown",)


def test_unattributable_protection_row_on_the_other_side_is_excluded(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _ledger(
        session_factory,
        binding_id=binding_id,
        leg_id=leg_id,
        order_id="stop-1",
        purpose="stop_loss",
        price="2500",
    )

    authority = _resolve(
        session_factory,
        [_stop_row("stop-1"), _stop_row("stop-unknown", pos_side="short")],
    )

    assert authority.resolved
    assert authority.order_ids == ("stop-1",)


def test_conditional_entry_rows_are_never_protection(tmp_path):
    session_factory, _, _ = _seed(tmp_path)
    entry_row = {
        "ordId": "entry-1",
        "instId": INST,
        "posSide": "long",
        "side": "buy",
        "triggerOrderType": "Conditional",
        "closeSLTriggerPrice": "2400",
    }

    authority = _resolve(session_factory, [entry_row])

    assert authority.resolved
    assert authority.order_ids == ()
    assert authority.unattributable_order_ids == ()


def test_one_order_carrying_both_triggers_freezes(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    _ledger(
        session_factory,
        binding_id=binding_id,
        leg_id=leg_id,
        order_id="both-1",
        purpose="stop_loss",
        price="2500",
    )
    row = _stop_row("both-1")
    row["tpTriggerPrice"] = "3000"

    authority = _resolve(session_factory, [row])

    assert authority.status == "frozen"
    assert authority.reason_code == FREEZE_COMBINED_TPSL_ORDER


def test_two_disagreeing_trade_units_are_unknown_not_newest_wins(tmp_path):
    session_factory, _, _ = _seed(tmp_path)
    with session_factory() as session:
        session.add(_ws_trigger_frame("stop-3", "pos-1"))
        session.add(_ws_trigger_frame("stop-3", "pos-other"))
        session.commit()

    authority = _resolve(session_factory, [_stop_row("stop-3")])

    assert authority.status == "frozen"
    assert authority.reason_code == FREEZE_ORDER_UNATTRIBUTABLE


def test_unverified_attribution_refuses_before_any_row_is_read(tmp_path):
    session_factory, _, _ = _seed(tmp_path, attribution_status="unverified")

    authority = _resolve(session_factory, [_stop_row("stop-1")])

    assert authority.status == "frozen"
    assert authority.reason_code == FREEZE_POSITION_NOT_VERIFIED


def test_unreadable_pending_list_is_unknown_not_unprotected(tmp_path):
    session_factory, _, _ = _seed(tmp_path)

    authority = _resolve(session_factory, None)

    assert authority.status == "frozen"
    assert authority.reason_code == FREEZE_PENDING_READ_INCOMPLETE
    assert authority.order_ids == ()


def test_adoption_writes_a_ledger_row_naming_how_it_was_proven(tmp_path):
    session_factory, binding_id, leg_id = _seed(tmp_path)
    with session_factory() as session:
        session.add(_ws_trigger_frame("stop-2", "pos-1"))
        session.commit()
    authority = _resolve(session_factory, [_stop_row("stop-2", price="2535.06")])

    with session_factory() as session:
        written = adopt_protection_orders(
            session, authority=authority, venue="deepcoin", adopted_at=NOW
        )
        session.commit()

    assert written == 1
    with session_factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.order_id == "stop-2")
            .one()
        )
        assert row.pos_id == "pos-1"
        assert row.purpose == "stop_loss"
        assert row.status == "verified"
        assert row.evidence_source == ADOPTION_EVIDENCE_SOURCE
        assert row.execution_binding_id == binding_id
        assert row.execution_order_leg_id == leg_id


def _pending_entry_leg(session_factory, *, binding_id, order_id, size, stop, index=9):
    """One resting limit entry whose ``slTriggerPx`` the exchange already holds."""

    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=index,
            purpose="entry",
            order_kind="limit",
            strategy_instance_id="deepcoin:1:1:ETH:long",
            venue="deepcoin",
            status="pending",
            attribution_status="unassigned",
            order_id=order_id,
            request={
                "instId": INST,
                "posSide": "long",
                "ordType": "limit",
                "px": "2600.0",
                "sz": size,
                "slTriggerPx": stop,
                "tdMode": "cross",
                "mrgPosition": "split",
                "side": "buy",
            },
        ),
    )
    return leg_id


def test_a_resting_entrys_own_stop_is_excluded_not_frozen(tmp_path):
    """The production shape from 2026-09-10: two resting entries froze a position.

    A migrated limit entry carries ``slTriggerPx`` on the order, so the
    exchange arms that stop while the entry rests. In
    ``trigger-orders-pending`` it is a ``TPSL`` row with no position id and
    ``TU == "default"`` -- the same shape as an ownerless stop on an open
    position. Excluding it is not claiming it: it is left exactly alone.
    """

    session_factory, binding_id, leg_id = _seed(tmp_path)
    _ledger(
        session_factory,
        binding_id=binding_id,
        leg_id=leg_id,
        order_id="stop-1",
        purpose="stop_loss",
        price="2500",
    )
    _pending_entry_leg(
        session_factory, binding_id=binding_id, order_id="entry-99", size="6.0", stop="2400.0"
    )
    with session_factory() as session:
        session.add(_ws_trigger_frame("entry-stop-98", "default"))
        session.commit()

    authority = _resolve(
        session_factory,
        [_stop_row("stop-1"), _stop_row("entry-stop-98", price="2400", size="6")],
    )

    assert authority.resolved
    assert authority.order_ids == ("stop-1",)
    assert authority.excluded_pending_entry_order_ids == ("entry-stop-98",)
    assert authority.unattributable_order_ids == ()


def test_a_default_trade_unit_alone_does_not_exclude(tmp_path):
    """Condition (b): without a matching resting entry it is still unattributable."""

    session_factory, _, _ = _seed(tmp_path)
    with session_factory() as session:
        session.add(_ws_trigger_frame("orphan-1", "default"))
        session.commit()

    authority = _resolve(session_factory, [_stop_row("orphan-1", price="2400", size="6")])

    assert authority.status == "frozen"
    assert authority.reason_code == FREEZE_ORDER_UNATTRIBUTABLE
    assert authority.excluded_pending_entry_order_ids == ()


def test_a_matching_entry_without_a_default_frame_does_not_exclude(tmp_path):
    """Condition (a): an order no frame ever placed is unknown, not excluded.

    "The position does not exist yet" and "no frame ever arrived" are different
    facts. Only the first one may exclude.
    """

    session_factory, binding_id, _ = _seed(tmp_path)
    _pending_entry_leg(
        session_factory, binding_id=binding_id, order_id="entry-99", size="6.0", stop="2400.0"
    )

    authority = _resolve(session_factory, [_stop_row("silent-1", price="2400", size="6")])

    assert authority.status == "frozen"
    assert authority.reason_code == FREEZE_ORDER_UNATTRIBUTABLE
    assert authority.excluded_pending_entry_order_ids == ()


def test_once_the_trade_unit_flips_the_stop_is_owned_again(tmp_path):
    """After the entry fills, ``TU`` names the position and exclusion stops.

    The flip leaves both values behind (``default`` then the posId), so the
    exclusion must key on "``default`` and nothing else" rather than on
    "``default`` appears somewhere".
    """

    session_factory, binding_id, leg_id = _seed(tmp_path)
    _pending_entry_leg(
        session_factory, binding_id=binding_id, order_id="entry-99", size="6.0", stop="2400.0"
    )
    with session_factory() as session:
        session.add(_ws_trigger_frame("entry-stop-98", "default"))
        session.add(_ws_trigger_frame("entry-stop-98", "pos-1"))
        session.commit()

    authority = _resolve(
        session_factory, [_stop_row("entry-stop-98", price="2400", size="6")]
    )

    assert authority.resolved
    assert authority.excluded_pending_entry_order_ids == ()
    assert authority.order_ids == ("entry-stop-98",)
    assert [item.order_id for item in authority.adoptions] == ["entry-stop-98"]


def test_the_cancel_precheck_passes_only_when_all_four_fields_still_agree(tmp_path):
    """instrument, posSide, trigger price and size -- by exact order id."""

    session_factory, binding_id, leg_id = _seed(tmp_path)
    _ledger(
        session_factory,
        binding_id=binding_id,
        leg_id=leg_id,
        order_id="stop-1",
        purpose="stop_loss",
        price="2500",
        size="4",
    )
    rows = [_stop_row("stop-1", price="2500", size="4")]
    authority = _resolve(session_factory, rows)

    from telegram_kol_research.protection_authority import (
        CANCEL_TARGET_ABSENT,
        CANCEL_TARGET_NOT_RESOLVED,
        CANCEL_TARGET_SIDE_CHANGED,
        CANCEL_TARGET_SIZE_CHANGED,
        CANCEL_TARGET_TRIGGER_CHANGED,
        evaluate_cancel_precheck,
    )

    assert evaluate_cancel_precheck(authority, rows, "stop-1") is None

    moved = [_stop_row("stop-1", price="2535.06", size="4")]
    assert (
        evaluate_cancel_precheck(authority, moved, "stop-1")
        == CANCEL_TARGET_TRIGGER_CHANGED
    )
    resized = [_stop_row("stop-1", price="2500", size="2")]
    assert (
        evaluate_cancel_precheck(authority, resized, "stop-1")
        == CANCEL_TARGET_SIZE_CHANGED
    )
    flipped = [_stop_row("stop-1", price="2500", size="4", pos_side="short")]
    assert (
        evaluate_cancel_precheck(authority, flipped, "stop-1")
        == CANCEL_TARGET_SIDE_CHANGED
    )
    assert evaluate_cancel_precheck(authority, [], "stop-1") == CANCEL_TARGET_ABSENT
    assert (
        evaluate_cancel_precheck(authority, rows, "never-resolved")
        == CANCEL_TARGET_NOT_RESOLVED
    )


def test_an_unreadable_pending_list_is_never_permission_to_cancel(tmp_path):
    """Unknown is not "go ahead" -- hard rule 4, on the way out this time."""

    session_factory, binding_id, leg_id = _seed(tmp_path)
    _ledger(
        session_factory,
        binding_id=binding_id,
        leg_id=leg_id,
        order_id="stop-1",
        purpose="stop_loss",
        price="2500",
        size="4",
    )
    authority = _resolve(session_factory, [_stop_row("stop-1", size="4")])

    from telegram_kol_research.protection_authority import (
        FREEZE_PENDING_READ_INCOMPLETE,
        evaluate_cancel_precheck,
    )

    assert (
        evaluate_cancel_precheck(authority, None, "stop-1")
        == FREEZE_PENDING_READ_INCOMPLETE
    )
