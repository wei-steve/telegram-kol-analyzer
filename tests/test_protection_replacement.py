from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    PositionProtectionIncident,
    PositionProtectionLedger,
)
from telegram_kol_research import protection_replacement as pr
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
INST = "ETH-USDT-SWAP"


class _Exchange:
    """Records every call in order, so the sequence itself can be asserted."""

    def __init__(self, *, pending_after_cancel=(), pending_raises=False):
        self.calls: list[tuple[str, str]] = []
        self.pending_after_cancel = list(pending_after_cancel)
        self.pending_raises = pending_raises
        self.cancel_fails_for: set[str] = set()
        self.place_fails = False

    def list_trigger_orders_pending(self, *, inst_id):
        self.calls.append(("read_pending", inst_id))
        if self.pending_raises:
            raise RuntimeError("read failed")
        return [{"ordId": order_id, "instId": inst_id} for order_id in self.pending_after_cancel]


def _seed(tmp_path):
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
            attribution_status="verified",
        ),
    )
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
            order_id="old-stop",
            purpose="stop_loss",
            trigger_price="2500",
            size_text="4",
            status="verified",
            evidence_source="test",
            evidence={},
            seen_at=NOW,
        )
        session.commit()
    return session_factory, binding_id, leg_id


def _plan(binding_id, leg_id, *, group, new_orders, old_order_ids):
    return pr.ProtectionReplacementPlan(
        venue="deepcoin",
        pos_id="pos-1",
        instrument_id=INST,
        execution_binding_id=binding_id,
        execution_order_leg_id=leg_id,
        group=group,
        new_orders=new_orders,
        old_order_ids=old_order_ids,
        idempotency_prefix="test:1",
    )


@pytest.fixture
def recorded(monkeypatch):
    """Replace the two gateway adapters with recorders. Sequence is the subject."""

    calls: list[tuple[str, str]] = []
    state = {"place_fails": False, "cancel_fails_for": set(), "next_order_id": "new-stop"}

    def fake_submit(**kwargs):
        calls.append(("place", kwargs["idempotency_key"]))
        if state["place_fails"]:
            raise RuntimeError("place failed")
        return {"data": {"ordId": state["next_order_id"]}}

    def fake_cancel(**kwargs):
        calls.append(("cancel", str(kwargs["order_id"])))
        if str(kwargs["order_id"]) in state["cancel_fails_for"]:
            raise RuntimeError("cancel failed")
        return {"data": {"ordId": kwargs["order_id"]}}

    monkeypatch.setattr(pr, "submit_exact_position_sltp", fake_submit)
    monkeypatch.setattr(pr, "cancel_exact_position_sltp", fake_cancel)
    return calls, state


def _ledger_status(session_factory, order_id):
    with session_factory() as session:
        row = (
            session.query(PositionProtectionLedger)
            .filter_by(venue="deepcoin", order_id=order_id)
            .one_or_none()
        )
        return None if row is None else row.status


def _incidents(session_factory):
    with session_factory() as session:
        return [
            (row.incident_type, row.pos_id)
            for row in session.query(PositionProtectionIncident).all()
        ]


def test_a_stop_is_placed_before_the_old_one_is_cancelled(tmp_path, recorded):
    calls, _ = recorded
    session_factory, binding_id, leg_id = _seed(tmp_path)
    exchange = _Exchange(pending_after_cancel=())
    plan = _plan(
        binding_id,
        leg_id,
        group=pr.GROUP_STOP,
        new_orders=(pr.NewProtectionOrder(purpose="stop_loss", payload={"instId": INST, "posId": "pos-1", "slTriggerPx": "2535.06", "sz": "4"}),),
        old_order_ids=("old-stop",),
    )

    result = pr.replace_stop_group(
        session_factory,
        plan=plan,
        deepcoin_client=exchange,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == pr.STATUS_SUCCEEDED
    assert [name for name, _ in calls] == ["place", "cancel"]
    assert result.new_order_ids == ("new-stop",)
    assert result.cancelled_order_ids == ("old-stop",)
    assert _ledger_status(session_factory, "old-stop") == "cancelled"
    assert _incidents(session_factory) == []


def test_a_take_profit_is_cancelled_before_the_new_one_is_placed(tmp_path, recorded):
    """The opposite order, for the opposite reason: two take profits over-reduce."""

    calls, state = recorded
    state["next_order_id"] = "new-tp"
    session_factory, binding_id, leg_id = _seed(tmp_path)
    exchange = _Exchange(pending_after_cancel=())
    plan = _plan(
        binding_id,
        leg_id,
        group=pr.GROUP_TAKE_PROFIT,
        new_orders=(pr.NewProtectionOrder(purpose="take_profit", payload={"instId": INST, "posId": "pos-1", "tpTriggerPx": "3000", "sz": "2"}),),
        old_order_ids=("old-tp",),
    )

    result = pr.replace_take_profit_group(
        session_factory,
        plan=plan,
        deepcoin_client=exchange,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == pr.STATUS_SUCCEEDED
    assert [name for name, _ in calls] == ["cancel", "place"]


def test_a_failed_cancel_keeps_the_new_stop_and_freezes(tmp_path, recorded):
    calls, state = recorded
    state["cancel_fails_for"] = {"old-stop"}
    session_factory, binding_id, leg_id = _seed(tmp_path)
    exchange = _Exchange(pending_after_cancel=("old-stop",))
    plan = _plan(
        binding_id,
        leg_id,
        group=pr.GROUP_STOP,
        new_orders=(pr.NewProtectionOrder(purpose="stop_loss", payload={"instId": INST, "posId": "pos-1", "slTriggerPx": "2535.06", "sz": "4"}),),
        old_order_ids=("old-stop",),
    )

    result = pr.replace_stop_group(
        session_factory,
        plan=plan,
        deepcoin_client=exchange,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == pr.STATUS_INCOMPLETE
    assert result.reason_code == "protection_old_order_cancel_failed"
    # The new stop is never undone: cancelling it is the one write that could
    # leave the position naked.
    assert [name for name, _ in calls] == ["place", "cancel"]
    assert result.new_order_ids == ("new-stop",)
    assert _ledger_status(session_factory, "old-stop") == "verified"
    assert _incidents(session_factory) == [(pr.REPLACE_INCOMPLETE_INCIDENT_TYPE, "pos-1")]


def test_an_old_stop_still_listed_after_its_cancel_is_not_retired(tmp_path, recorded):
    _, _ = recorded
    session_factory, binding_id, leg_id = _seed(tmp_path)
    exchange = _Exchange(pending_after_cancel=("old-stop",))
    plan = _plan(
        binding_id,
        leg_id,
        group=pr.GROUP_STOP,
        new_orders=(pr.NewProtectionOrder(purpose="stop_loss", payload={"instId": INST, "posId": "pos-1", "slTriggerPx": "2535.06"}),),
        old_order_ids=("old-stop",),
    )

    result = pr.replace_stop_group(
        session_factory,
        plan=plan,
        deepcoin_client=exchange,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == pr.STATUS_INCOMPLETE
    assert result.reason_code == "protection_old_order_still_pending"
    assert _ledger_status(session_factory, "old-stop") == "verified"


def test_an_unreadable_pending_list_is_never_proof_the_old_stop_is_gone(tmp_path, recorded):
    _, _ = recorded
    session_factory, binding_id, leg_id = _seed(tmp_path)
    exchange = _Exchange(pending_raises=True)
    plan = _plan(
        binding_id,
        leg_id,
        group=pr.GROUP_STOP,
        new_orders=(pr.NewProtectionOrder(purpose="stop_loss", payload={"instId": INST, "posId": "pos-1", "slTriggerPx": "2535.06"}),),
        old_order_ids=("old-stop",),
    )

    result = pr.replace_stop_group(
        session_factory,
        plan=plan,
        deepcoin_client=exchange,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == pr.STATUS_INCOMPLETE
    assert result.reason_code == "protection_old_order_absence_unproven"
    assert _ledger_status(session_factory, "old-stop") == "verified"


def test_a_rejected_stop_placement_leaves_the_old_stop_armed(tmp_path, recorded):
    calls, state = recorded
    state["place_fails"] = True
    session_factory, binding_id, leg_id = _seed(tmp_path)
    exchange = _Exchange()
    plan = _plan(
        binding_id,
        leg_id,
        group=pr.GROUP_STOP,
        new_orders=(pr.NewProtectionOrder(purpose="stop_loss", payload={"instId": INST, "posId": "pos-1", "slTriggerPx": "2535.06"}),),
        old_order_ids=("old-stop",),
    )

    result = pr.replace_stop_group(
        session_factory,
        plan=plan,
        deepcoin_client=exchange,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == pr.STATUS_UNKNOWN
    assert [name for name, _ in calls] == ["place"]
    assert _ledger_status(session_factory, "old-stop") == "verified"


def test_a_failed_take_profit_cancel_places_nothing(tmp_path, recorded):
    calls, state = recorded
    state["cancel_fails_for"] = {"old-tp"}
    session_factory, binding_id, leg_id = _seed(tmp_path)
    exchange = _Exchange(pending_after_cancel=("old-tp",))
    plan = _plan(
        binding_id,
        leg_id,
        group=pr.GROUP_TAKE_PROFIT,
        new_orders=(pr.NewProtectionOrder(purpose="take_profit", payload={"instId": INST, "posId": "pos-1", "tpTriggerPx": "3000"}),),
        old_order_ids=("old-tp",),
    )

    result = pr.replace_take_profit_group(
        session_factory,
        plan=plan,
        deepcoin_client=exchange,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == pr.STATUS_INCOMPLETE
    assert [name for name, _ in calls] == ["cancel"]
    assert _incidents(session_factory) == [
        (pr.TAKE_PROFIT_REPLACE_INCOMPLETE_INCIDENT_TYPE, "pos-1")
    ]


def test_a_pre_cancel_mismatch_aborts_before_any_cancel(tmp_path, recorded):
    calls, _ = recorded
    session_factory, binding_id, leg_id = _seed(tmp_path)
    exchange = _Exchange(pending_after_cancel=("old-stop",))
    plan = _plan(
        binding_id,
        leg_id,
        group=pr.GROUP_STOP,
        new_orders=(pr.NewProtectionOrder(purpose="stop_loss", payload={"instId": INST, "posId": "pos-1", "slTriggerPx": "2535.06"}),),
        old_order_ids=("old-stop",),
    )

    result = pr.replace_stop_group(
        session_factory,
        plan=plan,
        deepcoin_client=exchange,
        executed_at=NOW,
        live_execution_gate=lambda: True,
        pre_cancel_check=lambda rows, order_id: "protection_cancel_target_changed",
    )

    assert result.status == pr.STATUS_INCOMPLETE
    assert result.reason_code == "protection_cancel_target_changed"
    assert [name for name, _ in calls] == ["place"]
    assert _ledger_status(session_factory, "old-stop") == "verified"


def test_a_group_with_nothing_new_to_place_is_not_a_cancel_all(tmp_path, recorded):
    """A stop replacement with no new stop must never become a bare cancel."""

    calls, _ = recorded
    session_factory, binding_id, leg_id = _seed(tmp_path)
    exchange = _Exchange()
    plan = _plan(
        binding_id,
        leg_id,
        group=pr.GROUP_STOP,
        new_orders=(),
        old_order_ids=("old-stop",),
    )

    result = pr.replace_stop_group(
        session_factory,
        plan=plan,
        deepcoin_client=exchange,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == pr.STATUS_SKIPPED
    assert calls == []
    assert _ledger_status(session_factory, "old-stop") == "verified"
