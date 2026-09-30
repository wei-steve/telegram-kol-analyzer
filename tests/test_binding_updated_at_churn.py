"""Reconcile must move ``execution_bindings.updated_at`` only on real change.

``recovered_at`` is the last reconcile check and is refreshed every round;
``updated_at`` is the time the binding's content really changed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from types import SimpleNamespace

import pytest
from sqlalchemy import event

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    _derive_binding_from_entry_legs,
    _ReconcileSnapshot,
    _apply_reconcile_snapshot,
)
from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg

T1 = datetime(2026, 9, 30, 8, 0, 0)
T2 = T1 + timedelta(minutes=1)
T3 = T1 + timedelta(minutes=2)
SEEDED = datetime(2026, 9, 30, 7, 0, 0)
LIVE_POS = "POS-LIVE-1"


def _add(sf, *, message_id, symbol, status, leg_status, attribution="", pos_id=None,
         leg_pos_id=None, last_exchange_status=None):
    with sf() as session:
        b = ExecutionBinding(
            kol_id="group:1", chat_id=1, message_id=message_id, symbol=symbol,
            side="long", venue="deepcoin", status=status, pos_id=pos_id,
            last_exchange_status=last_exchange_status,
            order_id=f"o{message_id}", client_order_id=f"c{message_id}",
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


def _snapshot(*, live=True, errors=None):
    positions = []
    if live:
        positions = [{"instId": "BTC-USDT-SWAP", "posId": LIVE_POS,
                      "posSide": "long", "pos": "1", "avgPx": "60000"}]
    return _ReconcileSnapshot(positions=positions, errors=dict(errors or {}))


def _times(sf):
    with sf() as session:
        return {
            r.id: (r.status, r.last_exchange_status, r.updated_at, r.recovered_at)
            for r in session.query(ExecutionBinding).all()
        }


def _round(sf, at, **kw):
    _apply_reconcile_snapshot(sf, snapshot=_snapshot(**kw), recovered_at=at)


def _seed_mixed(sf):
    """closed / active (live verified) / stale / open (pending order on exchange)."""

    return {
        "closed": _add(sf, message_id=1, symbol="ETH", status="closed", leg_status="cancelled",
                       last_exchange_status="entry_legs_terminal"),
        "active": _add(sf, message_id=2, symbol="BTC", status="active", leg_status="active",
                       attribution="verified", pos_id=LIVE_POS, leg_pos_id=LIVE_POS,
                       last_exchange_status="position_ownership_verified"),
        "stale": _add(sf, message_id=3, symbol="SOL", status="stale", leg_status="filled",
                      last_exchange_status="position_ownership_unassigned"),
        "open": _add(sf, message_id=4, symbol="XRP", status="open", leg_status="open",
                     last_exchange_status="entry_order_pending"),
    }


def _pending_order_snapshot(*, live=True, with_order=True):
    snap = _snapshot(live=live)
    if with_order:
        snap.open_orders = [{
            "instId": "XRP-USDT-SWAP", "ordId": "o4", "clOrdId": "c4",
            "state": "live", "side": "buy", "posSide": "long", "px": "1", "sz": "1",
        }]
    return snap


def _round_mixed(sf, at, **kw):
    _apply_reconcile_snapshot(sf, snapshot=_pending_order_snapshot(**kw), recovered_at=at)


def test_unchanged_content_keeps_updated_at_but_refreshes_recovered_at(tmp_path):
    sf = create_session_factory(tmp_path / "a.db")
    ids = _seed_mixed(sf)
    _round_mixed(sf, T1)  # settle any first-round normalisation
    first = _times(sf)
    assert {v[0] for v in first.values()} == {"closed", "active", "open", "stale"}
    _round_mixed(sf, T2)
    second = _times(sf)
    for binding_id in ids.values():
        assert second[binding_id][:3] == first[binding_id][:3]
        assert second[binding_id][3] == T2
        assert first[binding_id][3] == T1


def test_only_the_row_whose_inputs_changed_advances_updated_at(tmp_path):
    sf = create_session_factory(tmp_path / "b.db")
    ids = _seed_mixed(sf)
    _round_mixed(sf, T1)
    first = _times(sf)
    # The live position disappears from the exchange snapshot: only the active
    # binding changes (verified position missing -> stale).
    _round_mixed(sf, T2, live=False)
    second = _times(sf)
    for name, binding_id in ids.items():
        if name == "active":
            assert second[binding_id][0] == "stale"
            assert second[binding_id][2] == T2
        else:
            assert second[binding_id][2] == first[binding_id][2]
        assert second[binding_id][3] == T2


def test_pending_leg_turning_terminal_advances_only_that_row(tmp_path):
    sf = create_session_factory(tmp_path / "b2.db")
    ids = _seed_mixed(sf)
    _round_mixed(sf, T1)
    first = _times(sf)
    with sf() as session:  # the pending entry leg reaches a terminal state
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=ids["open"]).one()
        leg.status = "cancelled"
        session.commit()
    _round_mixed(sf, T2, with_order=False)
    second = _times(sf)
    changed = [n for n, i in ids.items() if second[i][2] != first[i][2]]
    assert changed == ["open"]
    assert second[ids["open"]][2] == T2
    assert second[ids["open"]][0] != "open"


def test_update_statements_leave_updated_at_out_for_unchanged_rows(tmp_path):
    sf = create_session_factory(tmp_path / "c.db")
    ids = _seed_mixed(sf)
    _round_mixed(sf, T1)
    engine = sf.kw["bind"]
    captured: list[tuple[str, object]] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE EXECUTION_BINDINGS"):
            captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", listener)
    try:
        _round_mixed(sf, T2)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    # executemany batches identical statements: count parameter rows.
    assert sum(len(p) if isinstance(p, list) else 1 for _, p in captured) == len(ids)
    for statement, _ in captured:
        set_clause = statement.split("SET", 1)[1].split("WHERE", 1)[0]
        assert "recovered_at" in set_clause
        assert "updated_at" not in set_clause

    # And a real change puts updated_at back into that row's SET clause.
    captured.clear()
    event.listen(engine, "before_cursor_execute", listener)
    try:
        _round_mixed(sf, T3, live=False)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert sum(
        len(p) if isinstance(p, list) else 1
        for st, p in captured if "updated_at" in st.split("SET", 1)[1].split("WHERE", 1)[0]
    ) == 1


# --- per-branch coverage of _derive_binding_from_entry_legs ----------------

def _leg(status, attribution="", pos_id=None, index=1):
    return SimpleNamespace(
        leg_index=index, status=status, attribution_status=attribution or None,
        pos_id=pos_id,
    )


def _derive(sf, binding_id, legs, *, live=(), at=T2):
    with sf() as session:
        binding = session.get(ExecutionBinding, binding_id)
        _derive_binding_from_entry_legs(
            session, binding=binding, legs=legs,
            live_position_ids=set(live), recovered_at=at,
        )
        session.commit()
    with sf() as session:
        row = session.get(ExecutionBinding, binding_id)
        return row.status, row.last_exchange_status, row.updated_at, row.recovered_at


BRANCHES = {
    # name: (initial status, initial last_exchange_status, initial pos_id, legs, live, expected status)
    "active": ("stale", "position_ownership_unassigned", None,
               [_leg("active", "verified", LIVE_POS)], [LIVE_POS], "active"),
    "all_terminal": ("open", "entry_order_pending", None,
                     [_leg("cancelled")], [], "closed"),
    "verified_missing": ("active", "position_ownership_verified", LIVE_POS,
                         [_leg("active", "verified", LIVE_POS)], [], "stale"),
    "has_unavailable": ("open", "entry_order_pending", None,
                        [_leg("filled", "evidence_unavailable")], [], "open"),
    "has_conflict": ("open", "entry_order_pending", None,
                     [_leg("filled", "attribution_conflict")], [], "unknown"),
    "has_pending": ("stale", "position_ownership_unassigned", None,
                    [_leg("open")], [], "open"),
    "fallback_stale": ("open", "entry_order_pending", None,
                       [_leg("filled")], [], "stale"),
    "pos_id_only": ("active", "position_ownership_verified", "OLD-POS",
                    [_leg("active", "verified", LIVE_POS)], [LIVE_POS], "active"),
}


@pytest.mark.parametrize("name", sorted(BRANCHES))
def test_each_changing_branch_advances_updated_at_then_settles(tmp_path, name):
    status, last, pos_id, legs, live, expected = BRANCHES[name]
    sf = create_session_factory(tmp_path / "d.db")
    binding_id = _add(sf, message_id=9, symbol="BTC", status=status, leg_status="open",
                      pos_id=pos_id, last_exchange_status=last)
    got = _derive(sf, binding_id, legs, live=live, at=T2)
    assert got[0] == expected
    assert got[2] == T2 and got[3] == T2
    # Same inputs again: nothing changed, only the check time moves.
    again = _derive(sf, binding_id, legs, live=live, at=T3)
    assert again[:2] == got[:2]
    assert again[2] == T2
    assert again[3] == T3


def test_lifecycle_expiry_flip_inside_attach_is_not_reachable_from_derive():
    # _attach_binding_to_lifecycle can set status="stale" only when the row is
    # not active, but _derive_binding_from_entry_legs calls it right after
    # setting status="active", so that flip is unreachable through derive.
    # The snapshot compare would still count it (it reads final column values).
    import inspect

    src = inspect.getsource(_derive_binding_from_entry_legs)
    assert src.index('binding.status = "active"') < src.index("_attach_binding_to_lifecycle(")


# --- snapshot-errors branch -------------------------------------------------

def test_snapshot_errors_branch_moves_updated_at_only_when_status_changes(tmp_path):
    sf = create_session_factory(tmp_path / "e.db")
    ids = _seed_mixed(sf)
    _round_mixed(sf, T1)
    before = _times(sf)
    errors = {"positions": "boom"}
    _apply_reconcile_snapshot(sf, snapshot=_snapshot(errors=errors), recovered_at=T2)
    mid = _times(sf)
    _apply_reconcile_snapshot(sf, snapshot=_snapshot(errors=errors), recovered_at=T3)
    last = _times(sf)
    for binding_id in ids.values():
        assert mid[binding_id][1] == "position_attribution_evidence_unavailable"
        assert mid[binding_id][2] == T2  # first errors round: content changed
        assert mid[binding_id][3] == T2
        assert last[binding_id][2] == T2  # repeated errors round: no change
        assert last[binding_id][3] == T3
    assert before  # silence linters


def test_verified_position_ids_survive_two_rounds(tmp_path):
    from telegram_kol_research.management_target_verification import (
        load_verified_position_ids,
    )

    sf = create_session_factory(tmp_path / "f.db")
    _seed_mixed(sf)
    _round_mixed(sf, T1)
    _round_mixed(sf, T2)
    with sf() as session:
        assert load_verified_position_ids(session, now=T2) == frozenset({LIVE_POS})
