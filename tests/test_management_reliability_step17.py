"""A-17: closing a binding retires its protection rows in the same session.

2026-09-11: the user closed two BTC longs by hand. The manual-close sweep marked
both bindings closed within ninety seconds and left every protection row as it
was -- six exchange orders voided with the positions, six ledger rows still
``verified``. Measured afterwards: 553 such ledger rows and 919 non-terminal
legs under 147 closed bindings. This step is forward-only by ruling; the history
is A-17b.

Every place that sets a binding's status to ``closed`` must retire. The ruling
named three places and the first count found four; walking the source found
ten assignments in nine functions (management full and selected closes, the
source-deletion exit, the web manual close and a capability-deferred successor
had all been missed). Each gets its own behavioural test below, and an AST walk
fails the build when a new close site appears without the call -- because a
rule that names three places and is wired into three of ten is exactly the
shape this programme has spent two days removing.
"""

from __future__ import annotations

import ast
import json
import pathlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest


NOW = datetime(2026, 9, 11, 14, 43, 43, tzinfo=UTC)


def _seed_binding_with_protection(session_factory, *, binding_status="active"):
    """One binding, one entry leg, two ledger rows, four protection legs."""

    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        PositionProtectionLeg,
        PositionProtectionLedger,
    )

    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="kol", chat_id=-1002337721508, message_id=10434, symbol="BTC",
            side="long", venue="deepcoin", margin_mode="cross",
            position_mode="split", status=binding_status,
            pos_id="1001125216121996",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id, leg_index=1, purpose="entry",
            venue="deepcoin", order_kind="limit", status="active",
            strategy_instance_id="deepcoin:x", pos_id="1001125216121996",
        )
        session.add(leg)
        session.flush()
        for order_id, purpose, price in (
            ("1001125216121995", "stop_loss", "75700"),
            ("1001125226308043", "take_profit", "79800"),
        ):
            session.add(PositionProtectionLedger(
                venue="deepcoin", execution_binding_id=binding.id,
                execution_order_leg_id=leg.id, pos_id="1001125216121996",
                instrument_id="BTC-USDT-SWAP", side="long", order_id=order_id,
                purpose=purpose, trigger_price=price, status="verified",
                evidence_source="test",
                evidence_json=json.dumps({"ordId": order_id, "proof": "original"}),
            ))
        for index, (role, status) in enumerate((
            ("primary_stop", "verified"),
            ("take_profit", "verified"),
            ("take_profit", "planned"),
            ("take_profit", "filled"),
        ), start=1):
            session.add(PositionProtectionLeg(
                venue="deepcoin", execution_binding_id=binding.id,
                execution_order_leg_id=leg.id, role=role, leg_index=index,
                pos_id="1001125216121996", status=status,
            ))
        session.commit()
        return int(binding.id)


def _states(session_factory, binding_id):
    from telegram_kol_research.models import PositionProtectionLeg, PositionProtectionLedger

    # Ordered by a column, never by the tuple: two rows with the same status
    # would make sorted() fall through to comparing the evidence dicts, which
    # raises. The first version did exactly that, and every test died in this
    # helper before the function under test was reached.
    with session_factory() as session:
        ledger = [
            (row.status, json.loads(row.evidence_json))
            for row in session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.execution_binding_id == binding_id)
            .order_by(PositionProtectionLedger.id)
        ]
        legs = [
            (row.leg_index, row.status, json.loads(row.readback_evidence_json or "{}"))
            for row in session.query(PositionProtectionLeg)
            .filter(PositionProtectionLeg.execution_binding_id == binding_id)
            .order_by(PositionProtectionLeg.leg_index)
        ]
    return ledger, legs


def test_the_helper_retires_active_rows_and_keeps_their_evidence(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.protection_retirement import (
        retire_protection_for_closed_binding,
    )

    session_factory = create_session_factory(tmp_path / "a17.db")
    binding_id = _seed_binding_with_protection(session_factory)
    with session_factory() as session:
        counts = retire_protection_for_closed_binding(
            session, execution_binding_id=binding_id,
            reason="binding_closed", retired_at=NOW,
        )
        session.commit()

    assert counts == (2, 3)
    ledger, legs = _states(session_factory, binding_id)
    assert {status for status, _ in ledger} == {"retired"}
    for _, evidence in ledger:
        # Merged, never replaced: the original proof survives the retirement.
        assert evidence["proof"] == "original"
        assert evidence["retired_reason"] == "binding_closed"
        assert evidence["retired_from_status"] == "verified"
    assert [status for _, status, _ in legs] == ["retired", "retired", "retired", "filled"]


def test_filled_and_cancelled_legs_are_left_as_they_are(tmp_path):
    """``filled`` already says a take profit traded; overwriting it erases that."""

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.protection_retirement import (
        retire_protection_for_closed_binding,
    )

    session_factory = create_session_factory(tmp_path / "a17.db")
    binding_id = _seed_binding_with_protection(session_factory)
    with session_factory() as session:
        retire_protection_for_closed_binding(
            session, execution_binding_id=binding_id,
            reason="binding_closed", retired_at=NOW,
        )
        session.commit()

    _, legs = _states(session_factory, binding_id)
    filled = [evidence for index, status, evidence in legs if status == "filled"]
    assert filled == [{}], "a filled leg must not be touched at all"


def test_unparseable_evidence_is_kept_not_dropped(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionProtectionLedger
    from telegram_kol_research.protection_retirement import (
        retire_protection_for_closed_binding,
    )

    session_factory = create_session_factory(tmp_path / "a17.db")
    binding_id = _seed_binding_with_protection(session_factory)
    with session_factory() as session:
        row = session.query(PositionProtectionLedger).first()
        row.evidence_json = "not json at all"
        session.commit()
    with session_factory() as session:
        retire_protection_for_closed_binding(
            session, execution_binding_id=binding_id,
            reason="binding_closed", retired_at=NOW,
        )
        session.commit()

    ledger, _ = _states(session_factory, binding_id)
    assert any(ev.get("unparsed_evidence") == "not json at all" for _, ev in ledger)


def test_the_helper_does_not_commit(tmp_path):
    """Binding and protection change in one transaction, or not at all."""

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.protection_retirement import (
        retire_protection_for_closed_binding,
    )

    session_factory = create_session_factory(tmp_path / "a17.db")
    binding_id = _seed_binding_with_protection(session_factory)
    with session_factory() as session:
        retire_protection_for_closed_binding(
            session, execution_binding_id=binding_id,
            reason="binding_closed", retired_at=NOW,
        )
        session.rollback()

    ledger, _ = _states(session_factory, binding_id)
    assert {status for status, _ in ledger} == {"verified"}


# ---------------------------------------------------------------------------
# Per-site tests. Each closes a binding through its real code path and checks
# that the protection rows carry the retirement with that site's ``retired_by``.
# ---------------------------------------------------------------------------


def _add_protection(session_factory, binding_id, *, pos_id, entry_leg_id=None):
    """Two verified ledger rows and two live stop legs on an existing binding.

    Stops only: a take-profit leg would pull the close sweep into its
    take-profit-close proof, which is not what these tests are about.
    """

    from telegram_kol_research.models import (
        ExecutionOrderLeg,
        PositionProtectionLeg,
        PositionProtectionLedger,
    )

    with session_factory() as session:
        if entry_leg_id is None:
            existing = (
                session.query(ExecutionOrderLeg)
                .filter_by(execution_binding_id=binding_id, purpose="entry")
                .order_by(ExecutionOrderLeg.id)
                .first()
            )
            if existing is None:
                existing = ExecutionOrderLeg(
                    execution_binding_id=binding_id, leg_index=90,
                    purpose="entry", venue="deepcoin", order_kind="market",
                    status="active", pos_id=pos_id,
                    attribution_status="verified",
                )
                session.add(existing)
                session.flush()
            entry_leg_id = int(existing.id)
        for purpose in ("stop_loss", "backup_stop"):
            session.add(PositionProtectionLedger(
                venue="deepcoin", execution_binding_id=binding_id,
                execution_order_leg_id=entry_leg_id, pos_id=pos_id,
                instrument_id="BTC-USDT-SWAP", side="short",
                order_id=f"a17-{binding_id}-{purpose}", purpose=purpose,
                trigger_price="61000", status="verified",
                evidence_source="test", evidence_json='{"proof": "original"}',
            ))
        for index, (role, status) in enumerate(
            (("primary_stop", "verified"), ("backup_stop", "planned")), start=1
        ):
            session.add(PositionProtectionLeg(
                venue="deepcoin", execution_binding_id=binding_id,
                execution_order_leg_id=entry_leg_id, role=role,
                leg_index=index, pos_id=pos_id, status=status,
            ))
        session.commit()
    return entry_leg_id


def _protection_summary(session_factory, binding_id):
    from telegram_kol_research.models import PositionProtectionLeg, PositionProtectionLedger

    with session_factory() as session:
        ledger = (
            session.query(PositionProtectionLedger)
            .filter_by(execution_binding_id=binding_id)
            .order_by(PositionProtectionLedger.id)
            .all()
        )
        legs = (
            session.query(PositionProtectionLeg)
            .filter_by(execution_binding_id=binding_id)
            .order_by(PositionProtectionLeg.leg_index)
            .all()
        )
        evidence = [json.loads(row.evidence_json) for row in ledger] + [
            json.loads(row.readback_evidence_json or "{}") for row in legs
        ]
        return {
            "ledger": [row.status for row in ledger],
            "legs": [row.status for row in legs],
            "retired_by": {item["retired_by"] for item in evidence if "retired_by" in item},
        }


def _assert_retired_by(session_factory, binding_id, closed_by):
    summary = _protection_summary(session_factory, binding_id)
    assert summary["ledger"] and set(summary["ledger"]) == {"retired"}, summary
    assert set(summary["legs"]) - {"filled"} == {"retired"}, summary
    assert summary["retired_by"] == {closed_by}, summary


def _assert_untouched(session_factory, binding_id):
    summary = _protection_summary(session_factory, binding_id)
    assert "retired" not in summary["ledger"] + summary["legs"], summary
    assert summary["retired_by"] == set(), summary


def test_site_entry_legs_all_terminal(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.execution_bindings import _derive_binding_from_entry_legs
    from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg

    session_factory = create_session_factory(tmp_path / "a17.db")
    binding_id = _seed_binding_with_protection(session_factory)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        legs = session.query(ExecutionOrderLeg).filter_by(execution_binding_id=binding_id).all()
        for leg in legs:
            leg.status = "cancelled"
        _derive_binding_from_entry_legs(
            session, binding=binding, legs=legs, live_position_ids=set(), recovered_at=NOW
        )
        session.commit()
        assert session.get(ExecutionBinding, binding_id).status == "closed"

    _assert_retired_by(session_factory, binding_id, "entry_legs_terminal")


def test_site_manual_close_sweep(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.execution_bindings import sync_manual_closed_deepcoin_positions
    from telegram_kol_research.models import (
        ExecutionBinding,
        ExecutionOrderLeg,
        StrategyLifecycle,
    )

    session_factory = create_session_factory(tmp_path / "a17.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:100", chat_id=100, message_id=55, symbol="BTC",
            side="short", venue="deepcoin", status="active", pos_id="pos-closed",
        )
        session.add(binding)
        session.add(StrategyLifecycle(
            chat_id=100, message_id=55, symbol="BTC", side="short",
            lifecycle_status="entered", signal_at=datetime(2026, 9, 11, 9, 0),
            entered_at=datetime(2026, 9, 11, 9, 1),
        ))
        session.commit()
        binding_id = int(binding.id)
        # The rows hang off a non-entry leg: a verified *entry* leg sends the
        # sweep to ask for close history first, which is another path.
        anchor = ExecutionOrderLeg(
            execution_binding_id=binding_id, leg_index=90, purpose="stop_loss",
            venue="deepcoin", order_kind="market", status="active",
            pos_id="pos-closed", attribution_status="verified",
        )
        session.add(anchor)
        session.commit()
        anchor_id = int(anchor.id)
    _add_protection(session_factory, binding_id, pos_id="pos-closed", entry_leg_id=anchor_id)

    class FakeClient:
        def list_positions(self):
            return [{"instId": "SOL-USDT-SWAP", "posId": "unrelated-pos", "posSide": "long",
                     "pos": "1", "avgPx": "100", "mgnMode": "cross", "mrgPosition": "split"}]

    closed_at = datetime(2026, 9, 11, 14, 43)
    sync_manual_closed_deepcoin_positions(
        session_factory, client=FakeClient(), synced_at=closed_at - timedelta(seconds=61)
    )
    # One absent snapshot is only an observation; nothing may be retired yet.
    _assert_untouched(session_factory, binding_id)
    result = sync_manual_closed_deepcoin_positions(
        session_factory, client=FakeClient(), synced_at=closed_at
    )

    assert result.manually_closed == 1
    _assert_retired_by(session_factory, binding_id, "manual_close_sweep")


@pytest.mark.parametrize(
    ("binding_status", "last_exchange_status", "leg_status", "closed_by"),
    [
        ("stale", "manual_closed_or_not_found_on_exchange", "active", "attribution_repair_terminal"),
        ("active", "position_active", "cancelled", "entry_legs_terminal_by_repair"),
        ("active", "position_active", "open", None),
    ],
    ids=["terminal-binding", "all-entry-legs-terminal", "open-leg-not-closed"],
)
def test_site_attribution_repair_branches(
    tmp_path, binding_status, last_exchange_status, leg_status, closed_by
):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import ExecutionBinding, ExecutionOrderLeg
    from telegram_kol_research.position_attribution_repair import _derive_repaired_bindings

    session_factory = create_session_factory(tmp_path / "a17.db")
    binding_id = _seed_binding_with_protection(session_factory, binding_status=binding_status)
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.last_exchange_status = last_exchange_status
        legs = session.query(ExecutionOrderLeg).filter_by(execution_binding_id=binding_id).all()
        for leg in legs:
            leg.status = leg_status
        _derive_repaired_bindings(
            session, [binding], legs, NOW,
            affected_binding_ids={binding_id}, live_position_ids=set(),
        )
        session.commit()
        status = session.get(ExecutionBinding, binding_id).status

    if closed_by is None:
        assert status == "open"
        assert _protection_summary(session_factory, binding_id)["retired_by"] == set()
    else:
        assert status == "closed"
        _assert_retired_by(session_factory, binding_id, closed_by)


def test_site_historical_cleanup_close(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.historical_attribution_cleanup import HistoricalCleanupAction
    from telegram_kol_research.models import ExecutionBinding
    from telegram_kol_research.position_attribution_repair import _apply_historical_cleanup_action

    session_factory = create_session_factory(tmp_path / "a17.db")
    binding_id = _seed_binding_with_protection(session_factory, binding_status="stale")
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        _apply_historical_cleanup_action(
            session,
            action=HistoricalCleanupAction(
                action="close_historical_binding", binding_id=binding_id,
                leg_id=None, lifecycle_id=None, venue="deepcoin",
                old_pos_id=binding.pos_id, new_pos_id=None,
                old_state="stale", new_state="closed",
            ),
            plan=SimpleNamespace(created_at=NOW),
            legs_by_id={}, bindings_by_id={binding_id: binding},
            lifecycles_by_id={}, planned_clears_by_leg={},
        )
        session.commit()

    _assert_retired_by(session_factory, binding_id, "historical_cleanup_terminal")


@pytest.mark.parametrize("close_every_leg", [True, False], ids=["nothing-left", "one-left"])
def test_site_management_selected_close(tmp_path, close_every_leg):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import ExecutionBinding
    from telegram_kol_research.strategy_management_reconciliation import (
        _terminalize_selected_market_close_legs,
    )
    from tests.test_strategy_management_reconciliation import _persist_batch

    session_factory = create_session_factory(tmp_path / "a17.db")
    batch = _persist_batch(session_factory, action="full_close", sizes=("2", "2"), preflight=("2", "2"))
    _add_protection(
        session_factory, batch.execution_binding_id, pos_id="pos-1",
        entry_leg_id=batch.legs[0].execution_order_leg_id,
    )
    close_legs = batch.legs if close_every_leg else batch.legs[:1]
    with session_factory() as session:
        _terminalize_selected_market_close_legs(
            session, batch=batch, close_legs=close_legs, now=NOW
        )
        session.commit()
        status = session.get(ExecutionBinding, batch.execution_binding_id).status

    if close_every_leg:
        assert status == "closed"
        _assert_retired_by(session_factory, batch.execution_binding_id, "management_selected_close")
    else:
        assert status == "active"
        _assert_untouched(session_factory, batch.execution_binding_id)


def test_site_management_full_close(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from tests.test_strategy_management_reconciliation import (
        _Client,
        _persist_batch,
        _position,
        _reconcile,
    )

    session_factory = create_session_factory(tmp_path / "a17.db")
    batch = _persist_batch(session_factory, action="full_close", sizes=("2", "2"), preflight=("2", "2"))
    _add_protection(
        session_factory, batch.execution_binding_id, pos_id="pos-1",
        entry_leg_id=batch.legs[0].execution_order_leg_id,
    )

    _reconcile(session_factory, _Client(positions=[_position("pos-2", "2")]))
    _assert_untouched(session_factory, batch.execution_binding_id)
    _reconcile(session_factory, _Client(positions=[]))

    _assert_retired_by(session_factory, batch.execution_binding_id, "management_full_close")


def test_site_source_message_deletion_exit(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import (
        ExecutionBinding,
        SourceMessageDeletionExit,
        StrategyManagementBatch,
    )
    from telegram_kol_research.source_message_deletion import record_source_message_deleted
    from telegram_kol_research.source_message_deletion_worker import (
        finalize_source_message_deletion_exit,
        run_source_message_deletion_worker_tick,
    )
    from tests.test_source_message_deletion_worker import (
        NOW as DELETION_NOW,
        _confirm_management_batch,
        _ContractSpecs,
        _PositionClient,
        _seed_filled_strategy,
    )

    session_factory = create_session_factory(tmp_path / "a17.db")
    _seed_filled_strategy(session_factory)
    deletion = record_source_message_deleted(
        session_factory, chat_id=20, message_id=200, deleted_at=DELETION_NOW
    )
    with session_factory() as session:
        session.get(SourceMessageDeletionExit, deletion.exit_id).state = "closing_positions"
        session.commit()
    run_source_message_deletion_worker_tick(
        session_factory, deepcoin_client_factory=_PositionClient,
        contract_spec_provider=_ContractSpecs(), processed_at=DELETION_NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "reconciling"
        batch_id = deletion_exit.management_batch_id
        binding_id = int(session.query(ExecutionBinding).one().id)
        session.get(StrategyManagementBatch, batch_id).status = "succeeded"
        session.commit()
    _add_protection(session_factory, binding_id, pos_id="pos-filled")
    _confirm_management_batch(session_factory, batch_id)

    assert finalize_source_message_deletion_exit(
        session_factory, deletion_exit_id=deletion.exit_id,
        snapshot=SimpleNamespace(errors={}, positions=[], open_orders=[], pending_trigger_orders=[]),
        finalized_at=DELETION_NOW,
    ) == "succeeded"
    _assert_retired_by(session_factory, binding_id, "source_message_deletion_exit")


def test_site_web_manual_close(tmp_path):
    from fastapi.testclient import TestClient

    from telegram_kol_research.models import ExecutionBinding, StrategyLifecycle
    from telegram_kol_research.web_app import create_web_app

    app = create_web_app(database_path=tmp_path / "research.db")
    session_factory = app.state.session_factory
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88, message_id=10, symbol="BTC", side="short",
            lifecycle_status="entered", signal_at=datetime(2026, 9, 11, 9, 0),
            entered_at=datetime(2026, 9, 11, 9, 1),
        )
        binding = ExecutionBinding(
            kol_id="group:88", chat_id=88, message_id=10, symbol="BTC",
            side="short", status="active", pos_id="pos-1", order_id="order-1",
            last_exchange_status="position_active",
        )
        session.add_all([lifecycle, binding])
        session.commit()
        lifecycle_id, binding_id = int(lifecycle.id), int(binding.id)
    _add_protection(session_factory, binding_id, pos_id="pos-1")

    response = TestClient(app).post(
        f"/api/strategy-lifecycles/{lifecycle_id}/manual-close",
        json={"exit_price": 59500, "note": "a17"},
    )

    assert response.status_code == 200
    _assert_retired_by(session_factory, binding_id, "web_manual_close")


def test_a_tp1_fill_proven_after_retirement_is_still_recorded(tmp_path):
    """Without this the reconcile snapshot raises every round after such a close."""

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.models import PositionProtectionLeg
    from telegram_kol_research.position_protection_legs import record_verified_take_profit_fill
    from telegram_kol_research.protection_retirement import retire_protection_for_closed_binding

    session_factory = create_session_factory(tmp_path / "a17.db")
    binding_id = _seed_binding_with_protection(session_factory)
    with session_factory() as session:
        for leg in session.query(PositionProtectionLeg).filter_by(execution_binding_id=binding_id):
            leg.exchange_order_id = f"tp-{leg.leg_index}"
        retire_protection_for_closed_binding(
            session, execution_binding_id=binding_id, reason="binding_closed",
            retired_at=NOW, closed_by="manual_close_sweep",
        )
        session.commit()
    with session_factory() as session:
        retired = session.query(PositionProtectionLeg).filter_by(
            execution_binding_id=binding_id, leg_index=2
        ).one()
        assert retired.status == "retired"
        record_verified_take_profit_fill(
            session, retired, evidence={"filled_size": "7"}, completed_at=NOW
        )
        session.commit()
        evidence = json.loads(retired.readback_evidence_json)
        assert retired.status == "filled"
        assert evidence["tp1_fill"] == {"filled_size": "7"}
        assert evidence["retired_reason"] == "binding_closed"

        planned = session.query(PositionProtectionLeg).filter_by(
            execution_binding_id=binding_id, leg_index=3
        ).one()
        planned.status = "planned"
        with pytest.raises(ValueError, match="protection_leg_take_profit_fill_invalid"):
            record_verified_take_profit_fill(
                session, planned, evidence={"filled_size": "8"}, completed_at=NOW
            )


# ---------------------------------------------------------------------------
# Traversal guard.
# ---------------------------------------------------------------------------

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"
_CONSTANT_CLOSE_NAMES = {"binding", "row", "execution_binding"}
_DYNAMIC_BINDING_NAMES = {"binding", "execution_binding"}

#: Functions that assign a computed value to ``binding.status`` without
#: retiring, and why that value is never ``closed``. Compared exactly, so a new
#: computed assignment fails here until someone decides which list it joins.
_DYNAMIC_STATUS_NOT_CLOSED = {
    ("execution_bindings.py", "upsert_execution_binding"):
        "record.status; the only callers (recovery_live_submit) pass open or active",
    ("instruction_execution_reconciliation.py", "_persist_binding_from_readback"):
        "_terminal_binding_status returns rejected/cancelled/expired/failed, never closed",
}


def _own_nodes(function):
    stack = list(function.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                stack.append(child)


def _binding_status_writers():
    closing, dynamic, retiring = set(), set(), set()
    for path in sorted(_SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            key = (path.name, function.name)
            for node in _own_nodes(function):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == (
                    "retire_protection_for_closed_binding"
                ):
                    retiring.add(key)
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    if not (
                        isinstance(target, ast.Attribute)
                        and target.attr == "status"
                        and isinstance(target.value, ast.Name)
                    ):
                        continue
                    if isinstance(node.value, ast.Constant):
                        if node.value.value == "closed" and target.value.id in _CONSTANT_CLOSE_NAMES:
                            closing.add(key)
                    elif target.value.id in _DYNAMIC_BINDING_NAMES:
                        dynamic.add(key)
    return closing, dynamic, retiring


def test_every_function_that_closes_a_binding_retires_its_protection():
    closing, dynamic, retiring = _binding_status_writers()

    # Sentinel: a walk that parsed nothing would pass everything below.
    assert len(closing) >= 8, sorted(closing)
    assert closing - retiring == set(), sorted(closing - retiring)
    assert dynamic - retiring == set(_DYNAMIC_STATUS_NOT_CLOSED), sorted(dynamic - retiring)
