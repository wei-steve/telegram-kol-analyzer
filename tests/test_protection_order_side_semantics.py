from decimal import Decimal

import pytest

from telegram_kol_research import trigger_backup_stop_executor as backup
from telegram_kol_research import trigger_take_profit_convergence_executor as tp
from telegram_kol_research import entry_protection_ledger_repair as repair
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.native_tpsl import native_tpsl_order_id_is_unique


def _leg583_evidence():
    # Whitelisted verbatim fields from the 2026-09-05 exact-readiness diagnostic.
    position = dict(instType="SWAP", mgnMode="cross", instId="BTC-USDT-SWAP",
        posId="1001125135694798", posSide="long", pos="6", avgPx="79519",
        mrgPosition="split", slTriggerPx="77500", cTime="1788577607000")
    order = dict(instType="SWAP", instId="BTC-USDT-SWAP", ordId="1001125135694875",
        triggerPx="0", ordPx="0", sz="0", ordType="", side="sell", posSide="long",
        tdMode="cross", triggerOrderType="TPSL", triggerPxType="last", lever="125",
        slPrice="0", slTriggerPrice="77500", tpPrice="0", tpTriggerPrice="0",
        closeSLPrice="0", closeSLTriggerPrice="77500", closeTPPrice="0",
        closeTPTriggerPrice="0", cTime="1788577608000", uTime="1788577608000")
    ledger = PositionProtectionLedger(id=659, execution_binding_id=339,
        execution_order_leg_id=583, pos_id=position["posId"], order_id=order["ordId"],
        instrument_id=position["instId"], side="long", purpose="stop_loss",
        trigger_price="77500", status="verified", evidence_source="entry_protection_response")
    return position, order, ledger


@pytest.mark.parametrize("executor", [tp, backup])
def test_leg583_real_close_long_row_is_not_an_alias_conflict(executor):
    _, order, _ = _leg583_evidence()
    assert executor._native_tpsl_aliases_consistent(order)


def test_leg583_sz_zero_primary_returns_exact_stop_fingerprint(tmp_path):
    position, order, ledger = _leg583_evidence()
    sf = create_session_factory(tmp_path / "evidence.db")
    assert tp._verified_native_primary_stop_row(stop_rows=[ledger], position=position,
        open_positions=[position], pending=[order], position_size=Decimal("6")) is ledger
    with sf() as session:
        assert tp.exact_owned_stop_evidence_fingerprint(session, binding_id=339, leg_id=583,
            pos_id=position["posId"], inst_id=position["instId"], side="long",
            stop_rows=[ledger], pending=[order], position=position, open_positions=[position],
            position_size=Decimal("6")) is not None


def test_leg583_backup_primary_readback_recognizes_close_long():
    position, order, ledger = _leg583_evidence()
    assert backup._pending_matches_primary([order], order_id=ledger.order_id,
        trigger_price="77500", position=position, open_positions=(position,))


@pytest.mark.parametrize("executor", [tp, backup])
@pytest.mark.parametrize("position_side,order_side", [("long", "buy"), ("short", "sell")])
def test_real_close_side_contradictions_are_rejected(executor, position_side, order_side):
    _, order, _ = _leg583_evidence()
    order.update(posSide=position_side, side=order_side)
    assert not executor._native_tpsl_aliases_consistent(order)


@pytest.mark.parametrize("executor", [tp, backup])
@pytest.mark.parametrize("position_side,order_side", [("long", "sell"), ("short", "buy"),
    ("LONG", "SELL"), ("SHORT", "BUY"), ("long", None), ("short", "")])
def test_protective_direction_normal_combinations(executor, position_side, order_side):
    _, order, _ = _leg583_evidence()
    order.update(posSide=position_side, side=order_side)
    assert executor._native_tpsl_aliases_consistent(order)


@pytest.mark.parametrize("executor", [tp, backup])
@pytest.mark.parametrize("mutation", [
    {"pos_side": "short"}, {"posSide": ""}, {"posSide": "net"},
    {"side": "unknown"}, {"side": "long"}, {"side": 0}, {"side": False},
    {"orderId": "other"}, {"instrument_id": "ETH-USDT-SWAP"},
    {"posId": "one", "pos_id": "other"}, {"size": "6"},
    {"slTriggerPx": "77000"}, {"trigger_order_type": "Conditional"},
])
def test_other_alias_conflicts_and_unknown_supplied_direction_still_rejected(executor, mutation):
    _, order, _ = _leg583_evidence()
    order.update(mutation)
    assert not executor._native_tpsl_aliases_consistent(order)


@pytest.mark.parametrize("mutation", [{"side": "buy"}, {"posSide": "short"},
    {"instId": "ETH-USDT-SWAP"}, {"ordId": "unowned"}, {"sz": "5"},
    {"posId": "other-position"}, {"slTriggerPrice": "76000", "closeSLTriggerPrice": "76000"}])
def test_leg583_other_primary_match_gates_still_reject(mutation):
    position, order, ledger = _leg583_evidence()
    order.update(mutation)
    assert tp._verified_native_primary_stop_row(stop_rows=[ledger], position=position,
        open_positions=[position], pending=[order], position_size=Decimal("6")) is None


def test_duplicate_exact_order_id_still_rejected():
    position, order, ledger = _leg583_evidence()
    assert tp._verified_native_primary_stop_row(stop_rows=[ledger], position=position,
        open_positions=[position], pending=[order, dict(order)], position_size=Decimal("6")) is None


def test_unowned_take_profit_scopes_by_position_direction_after_order_side_check():
    position, order, _ = _leg583_evidence()
    order.update(tpTriggerPx="80200", tpTriggerPrice="80200", closeTPTriggerPrice="80200")
    kwargs = dict(pending=[order], inst_id=position["instId"], pos_id=position["posId"],
        owned_order_ids=set(), known_order_position_ids={})
    assert tp._unowned_pending_take_profit_present(side="long", **kwargs)
    assert not tp._unowned_pending_take_profit_present(side="short", **kwargs)
    order["side"] = "buy"
    assert tp._unowned_pending_take_profit_present(side="long", **kwargs)


def test_abnormal_same_id_duplicate_cannot_disappear_before_primary_uniqueness():
    position, order, _ = _leg583_evidence()
    _, _, ledger = _leg583_evidence()
    order.pop("side")
    invalid = {**order, "side": "buy"}
    pending = [order, invalid]
    assert tp._verified_native_primary_stop_row(stop_rows=[ledger], position=position,
        open_positions=[position], pending=pending, position_size=Decimal("6")) is None
    assert not backup._pending_matches_primary(pending, order_id=ledger.order_id,
        trigger_price="77500", position=position, open_positions=(position,))


def test_abnormal_same_id_duplicate_cannot_disappear_before_take_profit_readback():
    position, order, _ = _leg583_evidence()
    order.pop("side")
    order.update(tpTriggerPx="80200", tpTriggerPrice="80200", closeTPTriggerPrice="80200",
                 tpOrdPx="-1", sz="3")
    invalid = {**order, "side": "buy"}
    assert tp._verified_native_take_profit(position=position, open_positions=[position],
        pending=[order, invalid], order_id=order["ordId"],
        payload={"tpTriggerPx":"80200", "sz":"3"}) is None


@pytest.mark.parametrize("alias", ["OrderSysID", "ordId", "orderId", "order_id", "algoId", "triggerOrderId", "id"])
def test_raw_identity_uniqueness_counts_rows_not_equivalent_alias_fields(alias):
    assert native_tpsl_order_id_is_unique([{"ordId": "owned", alias: "owned"}], "owned")
    assert not native_tpsl_order_id_is_unique([{"ordId": "owned"}, {alias: "owned"}], "owned")
    assert not native_tpsl_order_id_is_unique([{alias: "other"}], "owned")
