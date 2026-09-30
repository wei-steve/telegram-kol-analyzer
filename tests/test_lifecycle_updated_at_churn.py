"""Reconcile must move ``strategy_lifecycles.updated_at`` only on real change.

Companion of ``test_binding_updated_at_churn.py``: the binding keeps
``recovered_at`` as its per-round check time; the lifecycle has no such column,
so an unchanged lifecycle must get no UPDATE at all.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event

from telegram_kol_research.context_resolution_worker import (
    build_context_state_fingerprint,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    _ReconcileSnapshot,
    _apply_reconcile_snapshot,
    _attach_binding_to_lifecycle,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    StrategyLifecycle,
    StrategyThread,
)
from telegram_kol_research.web_live_state import strategies_base_fingerprint

T1 = datetime(2026, 9, 30, 8, 0, 0)
T2 = T1 + timedelta(minutes=1)
T3 = T1 + timedelta(minutes=2)
SEEDED = datetime(2026, 9, 30, 7, 0, 0)
SIGNAL_AT = datetime(2026, 9, 30, 6, 30, 0)
LIVE_POS = "POS-LIVE-1"


def _add_binding(sf, *, message_id, symbol, status, leg_status, attribution="",
                 pos_id=None, leg_pos_id=None, last_exchange_status=None,
                 payload=None):
    with sf() as session:
        b = ExecutionBinding(
            kol_id="group:1", chat_id=1, message_id=message_id, symbol=symbol,
            side="long", venue="deepcoin", status=status, pos_id=pos_id,
            last_exchange_status=last_exchange_status,
            order_id=f"o{message_id}", client_order_id=f"c{message_id}",
            payload_json=json.dumps(payload) if payload is not None else None,
            created_at=SEEDED, updated_at=SEEDED, recovered_at=SEEDED,
        )
        session.add(b)
        session.flush()
        session.add(ExecutionOrderLeg(
            execution_binding_id=b.id, leg_index=1, purpose="entry",
            venue="deepcoin", order_kind="limit", order_id=f"o{message_id}",
            client_order_id=f"c{message_id}", status=leg_status,
            attribution_status=attribution or None, pos_id=leg_pos_id,
            attribution_evidence_json=(
                json.dumps({"policy_version": 2, "evidence_type": "test_verified_entry"})
                if attribution == "verified" else None
            ),
            created_at=SEEDED, updated_at=SEEDED,
        ))
        session.commit()
        return int(b.id)


def _add_lifecycle(sf, *, message_id, symbol, status, binding_id=None, **kw):
    with sf() as session:
        lc = StrategyLifecycle(
            chat_id=1, message_id=message_id, symbol=symbol, side="long",
            lifecycle_status=status, signal_at=kw.pop("signal_at", SIGNAL_AT),
            execution_binding_id=binding_id,
            created_at=SEEDED, updated_at=SEEDED, **kw,
        )
        session.add(lc)
        session.commit()
        return int(lc.id)


def _active(sf, message_id=2, payload=None):
    return _add_binding(
        sf, message_id=message_id, symbol="BTC", status="active", leg_status="active",
        attribution="verified", pos_id=LIVE_POS, leg_pos_id=LIVE_POS,
        last_exchange_status="position_ownership_verified", payload=payload,
    )


def _open(sf, message_id=4):
    return _add_binding(
        sf, message_id=message_id, symbol="XRP", status="open", leg_status="open",
        last_exchange_status="entry_order_pending",
    )


def _snapshot(*, with_order=False):
    snap = _ReconcileSnapshot(
        positions=[{"instId": "BTC-USDT-SWAP", "posId": LIVE_POS, "posSide": "long",
                    "pos": "1", "avgPx": "60000"}],
        errors={},
    )
    if with_order:
        snap.open_orders = [{
            "instId": "XRP-USDT-SWAP", "ordId": "o4", "clOrdId": "c4",
            "state": "live", "side": "buy", "posSide": "long", "px": "1", "sz": "1",
        }]
    return snap


def _round(sf, at, *, with_order=False):
    _apply_reconcile_snapshot(sf, snapshot=_snapshot(with_order=with_order),
                              recovered_at=at)


def _lc(sf, lifecycle_id):
    with sf() as session:
        lc = session.get(StrategyLifecycle, lifecycle_id)
        session.expunge(lc)
        return lc


def _count_lifecycle_updates(sf, fn):
    engine = sf.kw["bind"]
    captured: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE STRATEGY_LIFECYCLES"):
            captured.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    return len(captured)


def _settled_then_quiet(sf, lifecycle_id, *, with_order=False):
    _round(sf, T1, with_order=with_order)
    first = _lc(sf, lifecycle_id).updated_at
    writes = _count_lifecycle_updates(
        sf, lambda: _round(sf, T2, with_order=with_order))
    return first, _lc(sf, lifecycle_id).updated_at, writes


# --- unchanged content: no write ------------------------------------------

def test_entered_lifecycle_on_active_binding_gets_no_write_on_second_round(tmp_path):
    sf = create_session_factory(tmp_path / "a.db")
    bid = _active(sf)
    lid = _add_lifecycle(sf, message_id=2, symbol="BTC", status="entered",
                         binding_id=bid, entered_at=SEEDED)
    first, second, writes = _settled_then_quiet(sf, lid)
    assert second == first
    assert writes == 0


def test_pending_entry_lifecycle_on_open_binding_gets_no_write_on_second_round(tmp_path):
    sf = create_session_factory(tmp_path / "b.db")
    bid = _open(sf)
    lid = _add_lifecycle(sf, message_id=4, symbol="XRP", status="pending_entry",
                         binding_id=bid)
    first, second, writes = _settled_then_quiet(sf, lid, with_order=True)
    assert _lc(sf, lid).lifecycle_status == "pending_entry"
    assert second == first
    assert writes == 0


def test_first_round_into_pending_entry_advances_then_settles(tmp_path):
    sf = create_session_factory(tmp_path / "b2.db")
    bid = _open(sf)
    lid = _add_lifecycle(sf, message_id=4, symbol="XRP", status="expired",
                         exit_reason="expired", exited_at=SEEDED, binding_id=None)
    _round(sf, T1, with_order=True)
    assert _lc(sf, lid).lifecycle_status == "pending_entry"
    assert _lc(sf, lid).updated_at == T1
    writes = _count_lifecycle_updates(sf, lambda: _round(sf, T2, with_order=True))
    assert writes == 0 and _lc(sf, lid).updated_at == T1
    assert bid


def test_kol_signal_exit_reopened_once_then_quiet(tmp_path):
    sf = create_session_factory(tmp_path / "c.db")
    bid = _active(sf)
    lid = _add_lifecycle(sf, message_id=2, symbol="BTC", status="exited",
                         exit_reason="kol_signal", exited_at=SEEDED, entered_at=SEEDED,
                         binding_id=bid)
    _round(sf, T1)
    assert _lc(sf, lid).lifecycle_status == "entered"
    assert _lc(sf, lid).updated_at == T1
    writes = _count_lifecycle_updates(sf, lambda: _round(sf, T2))
    assert writes == 0 and _lc(sf, lid).updated_at == T1


def test_terminal_exited_branch_is_quiet_on_repeat(tmp_path):
    # The early-return branch for a terminal exited lifecycle only runs for a
    # non-active binding, which derive never passes, so call it directly.
    sf = create_session_factory(tmp_path / "c2.db")
    bid = _active(sf)
    lid = _add_lifecycle(sf, message_id=2, symbol="BTC", status="exited",
                         exit_reason="manual", exited_at=SEEDED, binding_id=None)
    with sf() as session:
        row = session.get(ExecutionBinding, bid)
        row.status = "closed"
        session.commit()
    _attach(sf, bid, T1)  # links the binding id: a real change
    assert _lc(sf, lid).execution_binding_id == bid
    assert _lc(sf, lid).updated_at == T1
    writes = _count_lifecycle_updates(sf, lambda: _attach(sf, bid, T2))
    assert writes == 0 and _lc(sf, lid).updated_at == T1
    assert _lc(sf, lid).lifecycle_status == "exited"


# --- real changes always advance ------------------------------------------

def _attach(sf, binding_id, at, **kw):
    with sf() as session:
        row = session.get(ExecutionBinding, binding_id)
        result = _attach_binding_to_lifecycle(session, row, at, **kw)
        session.commit()
        return result


def test_pending_entry_to_entered_advances(tmp_path):
    sf = create_session_factory(tmp_path / "d1.db")
    bid = _active(sf)
    lid = _add_lifecycle(sf, message_id=2, symbol="BTC", status="pending_entry",
                         binding_id=bid)
    _round(sf, T1)
    lc = _lc(sf, lid)
    assert lc.lifecycle_status == "entered" and lc.entered_at == T1
    assert lc.updated_at == T1


def test_kol_signal_exit_reopened_when_binding_active_advances(tmp_path):
    sf = create_session_factory(tmp_path / "d2.db")
    bid = _active(sf)
    lid = _add_lifecycle(sf, message_id=2, symbol="BTC", status="exited",
                         exit_reason="kol_signal", exited_at=SEEDED,
                         entered_at=SEEDED, exit_signal_message_id=77, binding_id=bid)
    _round(sf, T1)
    lc = _lc(sf, lid)
    assert lc.lifecycle_status == "entered" and lc.exit_reason is None
    assert lc.management_action == "exit_requested"
    assert lc.updated_at == T1


def test_stop_loss_and_take_profit_filled_from_binding_draft_advance(tmp_path):
    sf = create_session_factory(tmp_path / "d3.db")
    payload = {"draft": {"stop_loss": 59000.0,
                         "take_profit_legs": [{"price": 62000.0}]}}
    bid = _active(sf, payload=payload)
    lid = _add_lifecycle(sf, message_id=2, symbol="BTC", status="entered",
                         binding_id=bid, entered_at=SEEDED, entry_price_actual=60000.0)
    _round(sf, T1)
    lc = _lc(sf, lid)
    assert lc.stop_loss == 59000.0
    assert lc.take_profit
    assert lc.updated_at == T1
    writes = _count_lifecycle_updates(sf, lambda: _round(sf, T2))
    assert writes == 0 and _lc(sf, lid).updated_at == T1


def test_expiry_review_cleared_advances(tmp_path):
    sf = create_session_factory(tmp_path / "d4.db")
    bid = _active(sf)
    lid = _add_lifecycle(sf, message_id=2, symbol="BTC", status="entered",
                         binding_id=bid, entered_at=SEEDED,
                         management_action="expiry_review_requested")
    _round(sf, T1)
    lc = _lc(sf, lid)
    assert lc.management_action is None
    assert lc.updated_at == T1
    writes = _count_lifecycle_updates(sf, lambda: _round(sf, T2))
    assert writes == 0 and _lc(sf, lid).updated_at == T1


def test_expired_unentered_branch_advances_via_direct_call(tmp_path):
    # Unreachable from reconcile (derive sets the binding active first), so the
    # branch is exercised directly.
    sf = create_session_factory(tmp_path / "d5.db")
    bid = _open(sf)
    lid = _add_lifecycle(sf, message_id=4, symbol="XRP", status="pending_entry",
                         binding_id=bid, signal_at=T1 - timedelta(hours=10))
    assert _attach(sf, bid, T1) is False
    lc = _lc(sf, lid)
    assert lc.lifecycle_status == "expired" and lc.execution_binding_id is None
    assert lc.updated_at == T1


# --- readers that motivated the fix ---------------------------------------

def _fingerprint_fixture(sf):
    bid = _active(sf)
    lid = _add_lifecycle(sf, message_id=2, symbol="BTC", status="entered",
                         binding_id=bid, entered_at=SEEDED)
    with sf() as session:
        raw = RawMessage(chat_id=1, message_id=900, text="hold", posted_at=T1)
        thread = StrategyThread(
            chat_id=1, root_message_id=2, symbol="BTC", side="long",
            current_lifecycle_id=lid, created_at=SEEDED, updated_at=SEEDED,
        )
        session.add_all([raw, thread])
        session.commit()
        return bid, lid, int(raw.id), int(thread.id)


def test_context_fingerprint_stable_across_rounds_and_moves_on_real_change(tmp_path):
    sf = create_session_factory(tmp_path / "e.db")
    bid, lid, raw_id, thread_id = _fingerprint_fixture(sf)

    def fp():
        return build_context_state_fingerprint(
            sf, raw_id, candidate_thread_ids={thread_id})

    _round(sf, T1)
    before = fp()
    _round(sf, T2)
    assert fp() == before
    with sf() as session:  # a real change (stop-loss appears in the binding draft)
        row = session.get(ExecutionBinding, bid)
        row.payload_json = json.dumps({"draft": {"stop_loss": 59000.0}})
        session.commit()
    _round(sf, T3)
    assert fp() != before


def test_web_strategies_version_stable_across_rounds(tmp_path):
    sf = create_session_factory(tmp_path / "f.db")
    bid = _active(sf)
    _add_lifecycle(sf, message_id=2, symbol="BTC", status="entered",
                   binding_id=bid, entered_at=SEEDED)
    _round(sf, T1)
    with sf() as session:
        before = strategies_base_fingerprint(session)
    _round(sf, T2)
    with sf() as session:
        assert strategies_base_fingerprint(session) == before


def test_binding_side_still_refreshes_recovered_at_and_verifies_position(tmp_path):
    from telegram_kol_research.management_target_verification import (
        load_verified_position_ids,
    )

    sf = create_session_factory(tmp_path / "g.db")
    bid = _active(sf)
    _add_lifecycle(sf, message_id=2, symbol="BTC", status="entered",
                   binding_id=bid, entered_at=SEEDED)
    _round(sf, T1)
    with sf() as session:
        assert session.get(ExecutionBinding, bid).recovered_at == T1
    _round(sf, T2)
    with sf() as session:
        row = session.get(ExecutionBinding, bid)
        assert row.recovered_at == T2 and row.status == "active"
        assert load_verified_position_ids(session, now=T2) == frozenset({LIVE_POS})
