"""Post-execution readback against production-shaped venue rows.

Venue TPSL rows from trigger-orders-pending carry ``slTriggerPrice`` and no
``posId`` (see ``deepcoin_trigger_rows`` module docstring); a position row's
``slTriggerPx`` is only the last write (ARCHITECTURE 4.8). The end-to-end
fixture's fake exchange emits rows *with* ``posId``, so these tests pin the
attribution path production actually takes.
"""

from __future__ import annotations

from types import SimpleNamespace

import telegram_kol_research.oncall_remediation as remediation
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.position_management_remediation import RemediationScope
from tests.oncall_remediation_fixtures import WritableRemediationClient

INST = "BTC-USDT-SWAP"


def _position(pos_id, side="long", size="1", misleading_sl="61000"):
    return {
        "instId": INST,
        "posId": pos_id,
        "posSide": side,
        "pos": size,
        "avgPx": "64000",
        "cTime": "1000",
        # deliberately wrong: the last write, not the effective stop
        "slTriggerPx": misleading_sl,
    }


def _tpsl_row(ord_id, price, side="long"):
    # production shape: no posId, slTriggerPrice
    return {"ordId": ord_id, "instId": INST, "posSide": side, "slTriggerPrice": price, "tpTriggerPrice": ""}


def _scope():
    return RemediationScope(
        raw_message_id=1, strategy_instance_ids=("s",), lifecycle_ids=(1,), symbols=("BTC",), instruments=(INST,)
    )


def _readback(session_factory, client, *, action_kind, expected_effect, pos_ids, pre=None):
    return remediation._perform_post_execution_readback(
        session_factory,
        deepcoin_client=client,
        action_kind=action_kind,
        expected_effect=expected_effect,
        pos_ids=tuple(pos_ids),
        pre_execution_positions=pre or [{"pos_id": pos_ids[0], "pos_side": "long", "size": "1", "avg_entry_price": "64000"}],
        scope=_scope(),
        strategy_instance_id="s",
    )


def test_adjust_stop_confirmed_from_rows_without_pos_id(tmp_path):
    sf = create_session_factory(tmp_path / "r.db")
    client = WritableRemediationClient([_position("p1")], pending=[_tpsl_row("o1", "63000")])
    result = _readback(sf, client, action_kind="adjust_stop_loss", expected_effect={"stop_loss": "63000"}, pos_ids=["p1"])
    assert result.outcome == "confirmed", result


def test_adjust_stop_mismatch_when_new_stop_is_missing(tmp_path):
    sf = create_session_factory(tmp_path / "r.db")
    client = WritableRemediationClient([_position("p1", misleading_sl="63000")], pending=[_tpsl_row("o1", "62000")])
    result = _readback(sf, client, action_kind="adjust_stop_loss", expected_effect={"stop_loss": "63000"}, pos_ids=["p1"])
    # the position row says 63000; the pending table (the truth) says 62000
    assert result.outcome == "mismatch", result


def test_adjust_stop_mismatch_when_a_tighter_old_stop_still_fires_first(tmp_path):
    sf = create_session_factory(tmp_path / "r.db")
    client = WritableRemediationClient(
        [_position("p1")], pending=[_tpsl_row("new", "63000"), _tpsl_row("old", "63500")]
    )
    result = _readback(sf, client, action_kind="adjust_stop_loss", expected_effect={"stop_loss": "63000"}, pos_ids=["p1"])
    assert result.outcome == "mismatch", result


def test_short_effective_stop_is_the_lowest(tmp_path):
    sf = create_session_factory(tmp_path / "r.db")
    client = WritableRemediationClient(
        [_position("p1", side="short")], pending=[_tpsl_row("a", "65000", side="short"), _tpsl_row("b", "66000", side="short")]
    )
    result = _readback(sf, client, action_kind="adjust_stop_loss", expected_effect={"stop_loss": "65000"}, pos_ids=["p1"])
    assert result.outcome == "confirmed", result


def test_price_text_is_compared_as_number(tmp_path):
    sf = create_session_factory(tmp_path / "r.db")
    client = WritableRemediationClient([_position("p1")], pending=[_tpsl_row("o1", "63000")])
    result = _readback(sf, client, action_kind="adjust_stop_loss", expected_effect={"stop_loss": "63000.0"}, pos_ids=["p1"])
    assert result.outcome == "confirmed", result


def test_two_positions_same_side_need_ledger_attribution(tmp_path, monkeypatch):
    sf = create_session_factory(tmp_path / "r.db")
    client = WritableRemediationClient(
        [_position("p1"), _position("p2")], pending=[_tpsl_row("o1", "63000"), _tpsl_row("o2", "60000")]
    )
    # unattributable: instrument+side is ambiguous and the ledger owns nothing
    result = _readback(sf, client, action_kind="adjust_stop_loss", expected_effect={"stop_loss": "63000"}, pos_ids=["p1"])
    assert result.outcome == "mismatch", result

    ownership = SimpleNamespace(orders_for_position=lambda pos_id: ("o1",) if pos_id == "p1" else ("o2",))
    monkeypatch.setattr(remediation, "load_account_protection_ownership", lambda *a, **k: ownership)
    result = _readback(sf, client, action_kind="adjust_stop_loss", expected_effect={"stop_loss": "63000"}, pos_ids=["p1"])
    assert result.outcome == "confirmed", result


def test_break_even_stop_placed_from_rows_without_pos_id(tmp_path):
    sf = create_session_factory(tmp_path / "r.db")
    client = WritableRemediationClient([_position("p1")], pending=[_tpsl_row("o1", "64000")])
    result = _readback(sf, client, action_kind="move_stop_to_break_even", expected_effect={}, pos_ids=["p1"])
    assert result.outcome == "confirmed", result
    assert result.branch == "stop_placed"


def test_break_even_market_closed_branch(tmp_path):
    sf = create_session_factory(tmp_path / "r.db")
    client = WritableRemediationClient([], pending=[])
    result = _readback(sf, client, action_kind="move_stop_to_break_even", expected_effect={}, pos_ids=["p1"])
    assert result.outcome == "confirmed", result
    assert result.branch == "market_closed"
