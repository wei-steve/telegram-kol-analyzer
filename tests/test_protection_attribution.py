from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import DeepcoinWsEvent
from telegram_kol_research.protection_attribution import (
    match_position_protection,
    normalize_protection_snapshot_rows,
    snapshot_protection_rows,
)
from telegram_kol_research.protection_authority import (
    pending_row_trade_unit_pos_ids,
    resting_entry_attached_stop_order_ids,
)


def _position(pos_id, *, size="1.5", created_at="1782788876000", **overrides):
    row = {
        "instId": "ETH-USDT-SWAP",
        "posId": pos_id,
        "posSide": "long",
        "pos": size,
        "cTime": created_at,
    }
    row.update(overrides)
    return row


def _tpsl(*, created_at="1782788877000", size="0", **overrides):
    row = {
        "instId": "ETH-USDT-SWAP",
        "posSide": "long",
        "triggerOrderType": "TPSL",
        "sz": size,
        "cTime": created_at,
    }
    row.update(overrides)
    return row


def test_one_second_timestamp_difference_never_establishes_ownership():
    result = match_position_protection(
        [_position("pos-smart-market")],
        [_tpsl(ordId="sl-1", slTriggerPrice="1820")],
    )

    protection = result.by_pos_id["pos-smart-market"]
    assert protection.status == "absent"
    assert protection.stop_loss is None
    assert protection.order_ids == []
    assert protection.can_mutate is False


def test_runtime_mode_never_authorizes_unscoped_time_size_candidate():
    result = match_position_protection(
        [_position("pos-runtime")],
        [_tpsl(ordId="sl-guess", slTriggerPrice="1820")],
    )

    protection = result.by_pos_id["pos-runtime"]
    assert protection.status == "absent"
    assert protection.order_ids == []
    assert protection.can_mutate is False


def test_zero_size_tpsl_without_ledger_owner_is_not_position_protection():
    result = match_position_protection(
        [_position("pos-zero", size="5.2")],
        [_tpsl(ordId="sl-zero", size="0", slTriggerPx="1555")],
    )

    assert result.by_pos_id["pos-zero"].stop_loss is None
    assert result.by_pos_id["pos-zero"].status == "absent"


def test_exact_managed_tpsl_evidence_uses_only_order_bound_rows():
    position = _position(
        "pos-managed",
        size="7",
        created_at="10000",
        tpTriggerPx="63100",
    )
    late_stop = _tpsl(
        created_at="24010000",
        size="0",
        ordId="managed-stop",
        slTriggerPrice="67200",
    )

    result = match_position_protection(
        [position],
        [late_stop],
        exact_order_position_ids={"managed-stop": "pos-managed"},
    )

    protection = result.by_pos_id["pos-managed"]
    assert protection.status == "verified"
    assert protection.stop_loss == 67200
    assert protection.take_profits == []
    assert protection.order_ids == ["managed-stop"]


def test_partial_take_profit_sizes_can_cover_one_position():
    result = match_position_protection(
        [_position("pos-split", size="1.5")],
        [
            _tpsl(ordId="sl-full", size="0", slTriggerPrice="1820"),
            _tpsl(ordId="tp-1", size="0.9", tpTriggerPrice="1900"),
            _tpsl(ordId="tp-2", size="0.6", tpTriggerPrice="2000"),
        ],
        exact_order_position_ids={
            "sl-full": "pos-split",
            "tp-1": "pos-split",
            "tp-2": "pos-split",
        },
    )

    protection = result.by_pos_id["pos-split"]
    assert protection.status == "verified"
    assert protection.stop_loss == 1820
    assert protection.take_profits == [1900, 2000]
    assert protection.order_ids == ["sl-full", "tp-1", "tp-2"]


def test_stop_and_partial_targets_created_at_nearby_times_form_one_evidence_group():
    result = match_position_protection(
        [_position("pos-staggered", size="1.5", created_at="10000")],
        [
            _tpsl(created_at="10000", ordId="sl", size="0", slTriggerPrice="1820"),
            _tpsl(created_at="11000", ordId="tp-1", size="0.9", tpTriggerPrice="1900"),
            _tpsl(created_at="12000", ordId="tp-2", size="0.6", tpTriggerPrice="2000"),
        ],
        exact_order_position_ids={
            "sl": "pos-staggered",
            "tp-1": "pos-staggered",
            "tp-2": "pos-staggered",
        },
    )

    protection = result.by_pos_id["pos-staggered"]
    assert protection.status == "verified"
    assert protection.stop_loss == 1820
    assert protection.take_profits == [1900, 2000]
    assert protection.order_ids == ["sl", "tp-1", "tp-2"]


def test_protection_snapshot_preserves_every_ordered_row_and_execution_semantics():
    rows = [
        _tpsl(
            ordId="tp-1",
            size="0.9",
            tpTriggerPrice="1900",
            tpTriggerPxType="mark",
            tpOrdPx="1899",
        ),
        _tpsl(
            ordId="tp-2",
            size="0.6",
            tpTriggerPrice="2000",
            tpTriggerPxType="index",
            tpOrdPx="-1",
        ),
        _tpsl(
            ordId="sl-full",
            size="0",
            slTriggerPrice="1820",
            slTriggerPxType="last",
            slOrdPx="-1",
        ),
    ]
    assert snapshot_protection_rows(rows) == [
        {
            "order_id": "tp-1",
            "purpose": "take_profit",
            "trigger_price": "1900",
            "size": "0.9",
            "full_position": False,
            "trigger_type": "mark",
            "order_price": "1899",
        },
        {
            "order_id": "tp-2",
            "purpose": "take_profit",
            "trigger_price": "2000",
            "size": "0.6",
            "full_position": False,
            "trigger_type": "index",
            "order_price": "-1",
        },
        {
            "order_id": "sl-full",
            "purpose": "stop_loss",
            "trigger_price": "1820",
            "size": "0",
            "full_position": True,
            "trigger_type": "last",
            "order_price": "-1",
        },
    ]


def test_protection_snapshot_does_not_classify_zero_stop_side_as_combined():
    assert snapshot_protection_rows([
        _tpsl(
            ordId="tp-only",
            size="1",
            tpTriggerPx="1900",
            slTriggerPx="0",
            tpTriggerPxType="mark",
            tpOrdPx="-1",
        )
    ]) == [{
        "order_id": "tp-only",
        "purpose": "take_profit",
        "trigger_price": "1900",
        "size": "1",
        "full_position": False,
        "trigger_type": "mark",
        "order_price": "-1",
    }]


def test_normalize_protection_snapshots_converts_legacy_zero_combined_sides():
    assert normalize_protection_snapshot_rows([
        {
            "order_id": "tp-only",
            "purpose": "combined",
            "take_profit": {
                "trigger_price": "1900",
                "trigger_type": "mark",
                "order_price": "-1",
            },
            "stop_loss": {
                "trigger_price": "0.0",
                "trigger_type": "last",
                "order_price": "-1",
            },
            "size": "1",
            "full_position": False,
        },
        {
            "order_id": "sl-only",
            "purpose": "combined",
            "take_profit": {
                "trigger_price": "0",
                "trigger_type": "last",
                "order_price": "-1",
            },
            "stop_loss": {
                "trigger_price": "1820",
                "trigger_type": "index",
                "order_price": "-1",
            },
            "size": "0",
            "full_position": True,
        },
    ]) == [
        {
            "order_id": "tp-only",
            "purpose": "take_profit",
            "trigger_price": "1900",
            "size": "1",
            "full_position": False,
            "trigger_type": "mark",
            "order_price": "-1",
        },
        {
            "order_id": "sl-only",
            "purpose": "stop_loss",
            "trigger_price": "1820",
            "size": "0",
            "full_position": True,
            "trigger_type": "index",
            "order_price": "-1",
        },
    ]

def test_ledger_keeps_nearby_positions_orders_separate():
    result = match_position_protection(
        [
            _position("pos-a", size="1", created_at="10000"),
            _position("pos-b", size="2", created_at="14000"),
        ],
        [
            _tpsl(created_at="10000", ordId="sl-a", size="0", slTriggerPrice="1800"),
            _tpsl(created_at="14000", ordId="sl-b", size="0", slTriggerPrice="1700"),
            _tpsl(created_at="14000", ordId="tp-b", size="2", tpTriggerPrice="2000"),
            _tpsl(created_at="10000", ordId="tp-a", size="1", tpTriggerPrice="1900"),
        ],
        exact_order_position_ids={
            "sl-a": "pos-a",
            "tp-a": "pos-a",
            "sl-b": "pos-b",
            "tp-b": "pos-b",
        },
    )

    assert result.by_pos_id["pos-a"].order_ids == ["sl-a", "tp-a"]
    assert result.by_pos_id["pos-a"].stop_loss == 1800
    assert result.by_pos_id["pos-b"].order_ids == ["sl-b", "tp-b"]
    assert result.by_pos_id["pos-b"].stop_loss == 1700


def test_many_same_side_positions_match_only_ledger_owned_orders():
    result = match_position_protection(
        [
            _position("pos-a", size="8", created_at="10000"),
            _position("pos-b", size="19", created_at="15000"),
            _position("pos-c", size="5", created_at="21000"),
        ],
        [
            _tpsl(created_at="10000", ordId="a", size="8", slTriggerPrice="61500"),
            _tpsl(created_at="15000", ordId="b", size="19", slTriggerPrice="62070"),
            _tpsl(created_at="21000", ordId="c", size="5", slTriggerPrice="61000"),
        ],
        exact_order_position_ids={"a": "pos-a", "b": "pos-b", "c": "pos-c"},
    )

    assert result.by_pos_id["pos-a"].status == "verified"
    assert result.by_pos_id["pos-a"].order_ids == ["a"]
    assert result.by_pos_id["pos-b"].status == "verified"
    assert result.by_pos_id["pos-b"].order_ids == ["b"]
    assert result.by_pos_id["pos-c"].status == "verified"
    assert result.by_pos_id["pos-c"].order_ids == ["c"]


def test_inline_position_prices_are_not_cancellable_order_rows():
    result = match_position_protection(
        [
            _position(
                "pos-a",
                size="19",
                created_at="15000",
                slTriggerPx="62070",
                tpTriggerPx="64880",
            )
        ],
        [
            _tpsl(
                created_at="15000",
                ordId="position-tpsl",
                size="19",
                slTriggerPrice="62070",
                tpTriggerPrice="64880",
            )
        ],
        exact_order_position_ids={"position-tpsl": "pos-a"},
    )

    protection = result.by_pos_id["pos-a"]
    assert protection.status == "verified"
    assert protection.order_ids == ["position-tpsl"]
    assert [row.get("ordId") for row in protection.rows] == ["position-tpsl"]


def test_price_and_size_candidates_without_ledger_owner_remain_absent():
    result = match_position_protection(
        [
            _position("pos-a", size="1", created_at="10000"),
            _position("pos-b", size="2", created_at="14000"),
        ],
        [
            _tpsl(created_at="10000", ordId="sl-a", size="0", slTriggerPrice="1800"),
            _tpsl(created_at="14000", ordId="sl-b", size="0", slTriggerPrice="1700"),
            _tpsl(created_at="11000", ordId="tp-b", size="2", tpTriggerPrice="2000"),
        ],
    )

    assert result.by_pos_id["pos-a"].status == "absent"
    assert result.by_pos_id["pos-a"].order_ids == []
    assert result.by_pos_id["pos-b"].status == "absent"
    assert result.by_pos_id["pos-b"].order_ids == []


def test_equidistant_target_does_not_establish_any_owner():
    result = match_position_protection(
        [
            _position("pos-a", size="1", created_at="10000"),
            _position("pos-b", size="1", created_at="14000"),
        ],
        [
            _tpsl(created_at="10000", ordId="sl-a", size="0", slTriggerPrice="1800"),
            _tpsl(created_at="14000", ordId="sl-b", size="0", slTriggerPrice="1700"),
            _tpsl(created_at="12000", ordId="tp-unknown", size="1", tpTriggerPrice="1900"),
        ],
    )

    assert result.by_pos_id["pos-a"].status == "absent"
    assert result.by_pos_id["pos-b"].status == "absent"


def test_exchange_position_id_without_ledger_is_not_authority():
    result = match_position_protection(
        [_position("pos-a"), _position("pos-b")],
        [_tpsl(posId="pos-b", ordId="sl-b", slTriggerPrice="1820")],
    )

    assert result.by_pos_id["pos-a"].status == "absent"
    assert result.by_pos_id["pos-b"].status == "absent"
    assert result.by_pos_id["pos-b"].stop_loss is None


def test_exchange_close_pos_id_without_ledger_is_not_authority():
    result = match_position_protection(
        [_position("pos-a"), _position("pos-b")],
        [_tpsl(closePosId="pos-b", ordId="sl-b", slTriggerPrice="1820")],
    )

    assert result.by_pos_id["pos-a"].status == "absent"
    assert result.by_pos_id["pos-b"].status == "absent"
    assert result.by_pos_id["pos-b"].stop_loss is None


def test_unknown_close_pos_id_is_never_borrowed_by_a_live_position():
    result = match_position_protection(
        [_position("pos-live")],
        [_tpsl(closePosId="pos-closed", ordId="sl-old", slTriggerPrice="1820")],
    )

    assert result.by_pos_id["pos-live"].status == "absent"
    assert result.by_pos_id["pos-live"].order_ids == []


def test_indistinguishable_positions_do_not_adopt_unscoped_protection():
    result = match_position_protection(
        [_position("pos-a"), _position("pos-b")],
        [_tpsl(ordId="sl-unknown", slTriggerPrice="1820")],
    )

    for pos_id in ("pos-a", "pos-b"):
        protection = result.by_pos_id[pos_id]
        assert protection.status == "absent"
        assert protection.stop_loss is None
        assert protection.order_ids == []
        assert protection.can_mutate is False


def test_unowned_extra_order_freezes_ledger_owned_position_without_borrowing():
    result = match_position_protection(
        [_position("pos-a"), _position("pos-b")],
        [
            _tpsl(posId="pos-a", ordId="sl-a", slTriggerPrice="1810"),
            _tpsl(ordId="sl-unknown", slTriggerPrice="1820"),
        ],
        exact_order_position_ids={"sl-a": "pos-a"},
    )

    assert result.by_pos_id["pos-a"].status == "present_but_ambiguous"
    assert result.by_pos_id["pos-a"].order_ids == []
    assert result.by_pos_id["pos-a"].can_mutate is False


def test_unowned_pending_order_freezes_other_ledger_owned_protection():
    result = match_position_protection(
        [
            _position(
                "pos-a",
                size="5",
                created_at="10000",
                slTriggerPx="66500",
                tpTriggerPx="63300",
            )
        ],
        [
            _tpsl(
                created_at="10000",
                size="5",
                ordId="position-tpsl",
                slTriggerPrice="66500",
                tpTriggerPrice="63300",
            ),
            _tpsl(
                created_at="20000",
                size="7",
                ordId="other-tpsl",
                slTriggerPrice="66500",
            ),
        ],
        exact_order_position_ids={"position-tpsl": "pos-a"},
    )

    protection = result.by_pos_id["pos-a"]
    assert protection.status == "present_but_ambiguous"
    assert protection.stop_loss is None
    assert protection.take_profits == []
    assert protection.order_ids == []
    assert protection.can_mutate is False


def test_missing_tpsl_evidence_is_not_reported_as_absent():
    result = match_position_protection(
        [_position("pos-api-error")],
        [],
        evidence_available=False,
    )

    protection = result.by_pos_id["pos-api-error"]
    assert protection.status == "evidence_unavailable"
    assert protection.can_mutate is False


def test_unscoped_tpsl_without_timestamps_never_authorizes_mutation():
    position = _position("pos-no-time")
    position.pop("cTime")
    order = _tpsl(ordId="sl-no-time", slTriggerPrice="1820")
    order.pop("cTime")

    result = match_position_protection([position], [order])

    protection = result.by_pos_id["pos-no-time"]
    assert protection.status == "absent"
    assert protection.order_ids == []
    assert protection.can_mutate is False


def test_two_unscoped_groups_never_become_position_owned():
    result = match_position_protection(
        [_position("pos-one", created_at="10000")],
        [
            _tpsl(created_at="10000", ordId="sl-one", slTriggerPrice="1820"),
            _tpsl(created_at="11000", ordId="sl-two", slTriggerPrice="1810"),
        ],
    )

    protection = result.by_pos_id["pos-one"]
    assert protection.status == "absent"
    assert protection.order_ids == []
    assert protection.can_mutate is False


def test_incompatible_size_does_not_authorize_unscoped_tpsl():
    result = match_position_protection(
        [_position("pos-size", size="1.5", created_at="10000")],
        [_tpsl(created_at="10000", size="9", ordId="sl-wrong", slTriggerPrice="1820")],
    )

    protection = result.by_pos_id["pos-size"]
    assert protection.status == "absent"
    assert protection.can_mutate is False


# --- Per-position narrowing (2026-09-28 audit fixes design, section 1) ------
#
# The 2026-09-28 13:44Z production case: 陈哥's two long BTC positions were
# frozen by two short TPSL rows that were actually 大漂亮's two resting limit
# entry legs' own attached stops -- unrelated instrument side, unrelated
# strategy, unrelated group. R1-a (executor level, with the real order ids)
# lives in tests/test_strategy_management_executor.py; these are the matcher
# level cases (R1-b..f), each computing ``excluded_order_ids`` and
# ``order_trade_unit_pos_ids`` from a real sqlite test DB the same way
# production callers now do (``protection_authority.
# resting_entry_attached_stop_order_ids`` / ``pending_row_trade_unit_pos_ids``).

_NOW = datetime(2026, 9, 28, 13, 44, tzinfo=UTC)
_INST = "BTC-USDT-SWAP"


def _btc_tpsl(**overrides):
    """``_tpsl`` defaults to ``instId="ETH-USDT-SWAP"``; this scenario is BTC,
    and the resting-entry signature match requires the row's ``instId`` to
    equal the entry leg's, so every row below needs it set explicitly."""

    overrides.setdefault("instId", _INST)
    return _tpsl(**overrides)


def _narrowing_session(tmp_path):
    return create_session_factory(tmp_path / "research.db")


def _seed_binding(session_factory, *, side="long"):
    return upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol",
            chat_id=1,
            message_id=1,
            symbol="BTC",
            side=side,
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            status="active",
        ),
    )


def _pending_entry_leg_row(
    session_factory,
    *,
    binding_id,
    order_id,
    pos_side,
    sz,
    stop,
    index,
):
    """A resting (unfilled) limit entry leg whose own ``slTriggerPx`` the
    exchange already holds as a pending TPSL row -- the shape the 大漂亮
    binding's two entry legs (662/663) actually had."""

    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=index,
            purpose="entry",
            order_kind="limit",
            strategy_instance_id=f"deepcoin:1:1:BTC:{pos_side}",
            venue="deepcoin",
            status="pending",
            attribution_status="unassigned",
            order_id=order_id,
            request={
                "instId": _INST,
                "posSide": pos_side,
                "ordType": "limit",
                "px": "83810.0",
                "sz": sz,
                "slTriggerPx": stop,
                "tdMode": "cross",
                "mrgPosition": "split",
                "side": "sell" if pos_side == "short" else "buy",
            },
        ),
    )


def _ws_default_frame(order_id, trade_unit_id="default"):
    return DeepcoinWsEvent(
        venue="deepcoin",
        channel="TriggerOrder",
        action="push",
        order_sys_id=order_id,
        trade_unit_id=trade_unit_id,
        received_at=_NOW,
        received_ms=1,
        raw_payload="{}",
        payload_hash="hash-" + order_id,
    )


def test_r1b_resting_entry_leg_2_no_longer_freezes_its_own_filled_sibling(
    tmp_path,
):
    """R1-b: 大漂亮 now -- filled short position, its sibling leg still resting."""

    session_factory = _narrowing_session(tmp_path)
    binding_id = _seed_binding(session_factory, side="short")
    # Leg 663 (…523253, 85810 x 7) is still resting and carries its own stop
    # (…523252) at 86700. Leg 662 already filled into the position, so it is
    # not registered as a pending entry leg here.
    _pending_entry_leg_row(
        session_factory,
        binding_id=binding_id,
        order_id="1001125407523253",
        pos_side="short",
        sz="7.0",
        stop="86700.0",
        index=663,
    )
    with session_factory() as session:
        session.add(_ws_default_frame("1001125407523252"))
        session.commit()

    positions = [
        {
            "posId": "1001125407523145",
            "instId": _INST,
            "posSide": "short",
            "pos": "4",
        }
    ]
    pending = [
        _btc_tpsl(
            ordId="1001125407523144",
            posSide="short",
            slTriggerPrice="86700",
            size="4",
        ),
        _btc_tpsl(
            ordId="1001125409290803",
            posSide="short",
            slTriggerPrice="86873.4",
            size="4",
        ),
        _btc_tpsl(
            ordId="1001125409292132",
            posSide="short",
            tpTriggerPrice="81800",
            size="2",
        ),
        _btc_tpsl(
            ordId="1001125409292297",
            posSide="short",
            tpTriggerPrice="80200",
            size="2",
        ),
        # The still-resting sibling leg's own attached stop.
        _btc_tpsl(
            ordId="1001125407523252",
            posSide="short",
            slTriggerPrice="86700",
            size="7",
        ),
    ]
    exact_order_position_ids = {
        "1001125407523144": "1001125407523145",
        "1001125409290803": "1001125407523145",
        "1001125409292132": "1001125407523145",
        "1001125409292297": "1001125407523145",
    }

    with session_factory() as session:
        excluded_order_ids = resting_entry_attached_stop_order_ids(
            session, rows=pending
        )
        order_trade_unit_pos_ids = pending_row_trade_unit_pos_ids(
            session, rows=pending
        )

    assert excluded_order_ids == frozenset({"1001125407523252"})

    result = match_position_protection(
        positions,
        pending,
        exact_order_position_ids=exact_order_position_ids,
        excluded_order_ids=excluded_order_ids,
        order_trade_unit_pos_ids=order_trade_unit_pos_ids,
    )

    protection = result.by_pos_id["1001125407523145"]
    assert protection.status == "verified"
    assert protection.can_mutate is True
    assert set(protection.order_ids) == {
        "1001125407523144",
        "1001125409290803",
        "1001125409292132",
        "1001125409292297",
    }


def _chen_and_dapiaoliang_positions_and_ledger():
    positions = [
        {"posId": "1001125406857038", "instId": _INST, "posSide": "long", "pos": "5"},
        {"posId": "1001125406857169", "instId": _INST, "posSide": "long", "pos": "5"},
        {"posId": "1001125407523145", "instId": _INST, "posSide": "short", "pos": "4"},
    ]
    pending = [
        _btc_tpsl(ordId="chen1-sl", posSide="long", slTriggerPrice="60000", size="5"),
        _btc_tpsl(ordId="chen2-sl", posSide="long", slTriggerPrice="60100", size="5"),
        _btc_tpsl(
            ordId="1001125407523144",
            posSide="short",
            slTriggerPrice="86700",
            size="4",
        ),
    ]
    exact_order_position_ids = {
        "chen1-sl": "1001125406857038",
        "chen2-sl": "1001125406857169",
        "1001125407523144": "1001125407523145",
    }
    return positions, pending, exact_order_position_ids


def test_r1c_unknown_same_side_row_freezes_only_that_side(tmp_path):
    """R1-c (fail-closed kept): an unknown short row with no TU and no
    signature match still freezes the short position; 陈哥's long positions
    are unaffected."""

    session_factory = _narrowing_session(tmp_path)
    positions, pending, exact_order_position_ids = (
        _chen_and_dapiaoliang_positions_and_ledger()
    )
    # No resting entry leg is registered for this order and it never sent a
    # TriggerOrder frame at all, so it is neither excluded nor TU-narrowed.
    unknown = _btc_tpsl(
        ordId="unknown-short", posSide="short", slTriggerPrice="90000", size="9"
    )
    pending = [*pending, unknown]

    with session_factory() as session:
        excluded_order_ids = resting_entry_attached_stop_order_ids(
            session, rows=pending
        )
        order_trade_unit_pos_ids = pending_row_trade_unit_pos_ids(
            session, rows=pending
        )

    assert excluded_order_ids == frozenset()
    assert order_trade_unit_pos_ids == {}

    result = match_position_protection(
        positions,
        pending,
        exact_order_position_ids=exact_order_position_ids,
        excluded_order_ids=excluded_order_ids,
        order_trade_unit_pos_ids=order_trade_unit_pos_ids,
    )

    assert result.by_pos_id["1001125406857038"].status == "verified"
    assert result.by_pos_id["1001125406857169"].status == "verified"
    short = result.by_pos_id["1001125407523145"]
    assert short.status == "present_but_ambiguous"
    assert short.evidence["match"] == "unowned_order_same_side_present"


def test_r1d_unknown_row_without_pos_side_freezes_every_position(tmp_path):
    """R1-d (fail-closed kept): an unknown row with no readable ``posSide``
    still freezes every ledger-owned position, exactly as before this fix."""

    session_factory = _narrowing_session(tmp_path)
    positions, pending, exact_order_position_ids = (
        _chen_and_dapiaoliang_positions_and_ledger()
    )
    unknown = _btc_tpsl(ordId="unknown-no-side", slTriggerPrice="90000", size="9")
    unknown.pop("posSide", None)
    pending = [*pending, unknown]

    with session_factory() as session:
        excluded_order_ids = resting_entry_attached_stop_order_ids(
            session, rows=pending
        )
        order_trade_unit_pos_ids = pending_row_trade_unit_pos_ids(
            session, rows=pending
        )

    result = match_position_protection(
        positions,
        pending,
        exact_order_position_ids=exact_order_position_ids,
        excluded_order_ids=excluded_order_ids,
        order_trade_unit_pos_ids=order_trade_unit_pos_ids,
    )

    for pos_id in (
        "1001125406857038",
        "1001125406857169",
        "1001125407523145",
    ):
        protection = result.by_pos_id[pos_id]
        assert protection.status == "present_but_ambiguous"
        assert protection.evidence["match"] == "global_unowned_order_present"


def test_r1e_trade_unit_narrows_ambiguity_to_the_named_position(tmp_path):
    """R1-e: an unknown row whose ``TU`` frames agree on one posId freezes
    only that position, even though it shares 陈哥's ``long`` side."""

    session_factory = _narrowing_session(tmp_path)
    positions, pending, exact_order_position_ids = (
        _chen_and_dapiaoliang_positions_and_ledger()
    )
    # An unknown long stop whose TriggerOrder frames name pos 857169
    # specifically (e.g. a backup stop adopted moments after this read, still
    # unowned by the ledger snapshot this call used).
    unknown = _btc_tpsl(
        ordId="unknown-tu", posSide="long", slTriggerPrice="59500", size="5"
    )
    pending = [*pending, unknown]
    with session_factory() as session:
        session.add(_ws_default_frame("unknown-tu", "1001125406857169"))
        session.commit()

        excluded_order_ids = resting_entry_attached_stop_order_ids(
            session, rows=pending
        )
        order_trade_unit_pos_ids = pending_row_trade_unit_pos_ids(
            session, rows=pending
        )

    assert order_trade_unit_pos_ids == {"unknown-tu": "1001125406857169"}

    result = match_position_protection(
        positions,
        pending,
        exact_order_position_ids=exact_order_position_ids,
        excluded_order_ids=excluded_order_ids,
        order_trade_unit_pos_ids=order_trade_unit_pos_ids,
    )

    assert result.by_pos_id["1001125406857038"].status == "verified"
    assert result.by_pos_id["1001125407523145"].status == "verified"
    frozen = result.by_pos_id["1001125406857169"]
    assert frozen.status == "present_but_ambiguous"
    assert frozen.evidence == {
        "match": "unowned_order_trade_unit_points_here",
        "pos_id": "1001125406857169",
    }


def test_r1f_resting_shaped_row_without_any_frame_is_not_excluded(tmp_path):
    """R1-f: a row that matches a resting entry leg's signature but never sent
    a single ``TriggerOrder`` frame is not excluded -- "no position yet" and
    "we never heard from the exchange about it" are different facts, and only
    the first may exclude a row."""

    session_factory = _narrowing_session(tmp_path)
    binding_id = _seed_binding(session_factory, side="short")
    _pending_entry_leg_row(
        session_factory,
        binding_id=binding_id,
        order_id="1001125407523253",
        pos_side="short",
        sz="7.0",
        stop="86700.0",
        index=663,
    )
    # No DeepcoinWsEvent registered for "1001125407523252" at all.

    positions, pending, exact_order_position_ids = (
        _chen_and_dapiaoliang_positions_and_ledger()
    )
    resting_shaped = _btc_tpsl(
        ordId="1001125407523252", posSide="short", slTriggerPrice="86700", size="7"
    )
    pending = [*pending, resting_shaped]

    with session_factory() as session:
        excluded_order_ids = resting_entry_attached_stop_order_ids(
            session, rows=pending
        )
        order_trade_unit_pos_ids = pending_row_trade_unit_pos_ids(
            session, rows=pending
        )

    assert excluded_order_ids == frozenset()
    assert order_trade_unit_pos_ids == {}

    result = match_position_protection(
        positions,
        pending,
        exact_order_position_ids=exact_order_position_ids,
        excluded_order_ids=excluded_order_ids,
        order_trade_unit_pos_ids=order_trade_unit_pos_ids,
    )

    assert result.by_pos_id["1001125406857038"].status == "verified"
    assert result.by_pos_id["1001125406857169"].status == "verified"
    short = result.by_pos_id["1001125407523145"]
    assert short.status == "present_but_ambiguous"
    assert short.evidence["match"] == "unowned_order_same_side_present"


# --- Fail-closed hardening: unrecognized posSide values (review follow-up) --
#
# ``protection_order_position_sides`` only translates ``buy``/``sell`` order
# aliases into ``long``/``short``; a one-way-mode ``net``, a venue's ``both``,
# an empty string, or a typo all pass through unchanged. Side-based narrowing
# must never treat "unrecognized value" as "provably the other side" -- that
# would be fail-*open*: an account in one-way mode (posSide ``net`` on every
# row) would never be frozen by an otherwise-unowned same-instrument row, and
# a stray non-``long``/``short`` value on the *unowned row itself* would wrongly
# narrow instead of falling back to freezing everyone.


def test_position_with_unrecognized_pos_side_is_frozen_by_unowned_same_side_row(
    tmp_path,
):
    """A position whose own ``posSide`` is not ``long``/``short`` (here,
    one-way-mode ``net``) can never be proven to be the *other* side, so an
    otherwise-unowned row still freezes it -- even though a naive ``!=``
    string comparison would wrongly read that as "different side, not
    ambiguous"."""

    session_factory = _narrowing_session(tmp_path)
    positions, pending, exact_order_position_ids = (
        _chen_and_dapiaoliang_positions_and_ledger()
    )
    # A one-way-mode position: the exchange reports "net", not long/short.
    net_position = {
        "posId": "net-pos-1",
        "instId": _INST,
        "posSide": "net",
        "pos": "3",
    }
    positions = [*positions, net_position]
    pending = [
        *pending,
        _btc_tpsl(
            ordId="net-pos-sl", posSide="net", slTriggerPrice="61000", size="3"
        ),
    ]
    exact_order_position_ids = {
        **exact_order_position_ids,
        "net-pos-sl": "net-pos-1",
    }
    # The same unrelated, unattributable short row as R1-c/f.
    unknown_short = _btc_tpsl(
        ordId="unknown-short", posSide="short", slTriggerPrice="90000", size="9"
    )
    pending = [*pending, unknown_short]

    with session_factory() as session:
        excluded_order_ids = resting_entry_attached_stop_order_ids(
            session, rows=pending
        )
        order_trade_unit_pos_ids = pending_row_trade_unit_pos_ids(
            session, rows=pending
        )

    result = match_position_protection(
        positions,
        pending,
        exact_order_position_ids=exact_order_position_ids,
        excluded_order_ids=excluded_order_ids,
        order_trade_unit_pos_ids=order_trade_unit_pos_ids,
    )

    # Known, provably different sides are still unaffected.
    assert result.by_pos_id["1001125406857038"].status == "verified"
    assert result.by_pos_id["1001125406857169"].status == "verified"
    # Known same side: still ambiguous, as before.
    assert result.by_pos_id["1001125407523145"].status == "present_but_ambiguous"
    # Unrecognized side: frozen too, fail-closed -- it cannot be ruled out.
    net_protection = result.by_pos_id["net-pos-1"]
    assert net_protection.status == "present_but_ambiguous"
    assert net_protection.evidence["match"] == "unowned_order_same_side_present"


def test_unowned_row_with_unrecognized_pos_side_freezes_every_position(tmp_path):
    """An unowned row whose own ``posSide`` is not a single recognized
    ``long``/``short`` value (here, ``net``) cannot be narrowed by side at
    all, so it falls back to freezing every ledger-owned position --
    including ones with a perfectly ordinary, known ``long``/``short`` side."""

    session_factory = _narrowing_session(tmp_path)
    positions, pending, exact_order_position_ids = (
        _chen_and_dapiaoliang_positions_and_ledger()
    )
    unknown_net_side = _btc_tpsl(
        ordId="unknown-net", posSide="net", slTriggerPrice="61500", size="3"
    )
    pending = [*pending, unknown_net_side]

    with session_factory() as session:
        excluded_order_ids = resting_entry_attached_stop_order_ids(
            session, rows=pending
        )
        order_trade_unit_pos_ids = pending_row_trade_unit_pos_ids(
            session, rows=pending
        )

    assert excluded_order_ids == frozenset()
    assert order_trade_unit_pos_ids == {}

    result = match_position_protection(
        positions,
        pending,
        exact_order_position_ids=exact_order_position_ids,
        excluded_order_ids=excluded_order_ids,
        order_trade_unit_pos_ids=order_trade_unit_pos_ids,
    )

    for pos_id in (
        "1001125406857038",
        "1001125406857169",
        "1001125407523145",
    ):
        protection = result.by_pos_id[pos_id]
        assert protection.status == "present_but_ambiguous"
        assert protection.evidence["match"] == "global_unowned_order_present"
