"""R5-i: a write-before-log guard for the management executor's own audit.

Design: ``docs/plans/2026-09-29-management-preflight-refusal-uncertain-design.md``
section 4, requirement 2. Status:
``docs/management-preflight-refusal-status.md``.

The no-exchange-contact proof in ``execution_boundary.
management_batches_prove_no_exchange_contact`` only holds because every
Deepcoin write call site in ``strategy_management_executor`` commits a ledger
row (a leg transitioned to ``reserved`` with its ``client_order_id`` /
``request_json``, or a "reserved" ``execution_events`` row, or a rescue
transitioned to ``reserved``) *before* the corresponding exchange call. These
tests drive a fake client that raises the instant one of those methods is
called, and assert the ledger is already in its post-reservation state at
that point -- so a future change that reorders "call the exchange" ahead of
"commit the reservation" fails here first.

Covers the three paths the design explicitly names: the close leg, the TPSL
replace, and the rescue. It does not cover every one of the audit table's
call sites (``_cancel_old_protection_after_replacement``,
``_restore_precancelled_protection_for_rejected_close``, the risk-reduction
precancel) -- see the status document for why (fixture cost) and confirmation
that each of those also writes an ``execution_events`` "reserved" row before
its own call, by direct code reading rather than by an exercised test here.

The one path this file also documents, but does *not* claim: the deferred
entry cancel path (``_cancel_deferred_entry_legs``) is the one exception the
audit found -- see the comment on ``management_batches_prove_no_exchange_
contact`` in ``execution_boundary.py``.
"""

from __future__ import annotations

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import StrategyManagementLeg
from telegram_kol_research.strategy_management_batches import load_management_batch
from telegram_kol_research.strategy_management_executor import (
    execute_management_batch,
    execute_trigger_protection_stop_rescue,
)

from test_strategy_management_executor import (
    NOW,
    _FakeClient,
    _persist_close_batch,
    _persist_protection_batch,
    _ProtectionClient,
)
from test_trigger_protection_stop_rescue import (
    _Client as _RescueClient,
    _saved_deferred_intent,
)


class _SimulatedWriteFailure(RuntimeError):
    """A stand-in for any transport failure at the exchange-write call site."""


@pytest.fixture(autouse=True)
def _fixed_stop_gate_check_clock(monkeypatch):
    """Freeze the stop-price gate's freshness clock to ``NOW`` (2026-07-15).

    ``test_strategy_management_executor.py`` declares this same fixture
    ``autouse``, but that only reaches tests collected *in that module* --
    this file needs its own copy. Without it, ``management_stop_price_gate``
    compares each fixture's fixed 2026-07-15 quote timestamp against the real
    wall clock and refuses every batch with ``management_stop_reference_
    unavailable`` before the write path under test is ever reached.
    """

    from telegram_kol_research import management_stop_price_gate as gate

    monkeypatch.setattr(gate, "_stop_check_now", lambda: NOW)


def test_close_leg_is_reserved_before_the_exchange_call_raises(tmp_path):
    """Close path: ``place_order`` raises; the leg must already be ``reserved``."""

    session_factory = create_session_factory(tmp_path / "research.db")
    batch = _persist_close_batch(session_factory)
    client = _FakeClient(
        session_factory,
        outcomes=[_SimulatedWriteFailure("simulated close failure")],
    )

    # The gateway wraps whatever the client raises into its own outcome
    # classification -- an exception, or a "submit unknown" return value --
    # which exact shape comes back is not this test's concern; what matters is
    # that the ledger was already updated before the client method was ever
    # called.
    try:
        execute_management_batch(
            session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
        )
    except Exception:
        pass

    # ``place_order`` itself already proved this: it looked the leg's status up
    # by ``client_order_id`` *before* raising, and recorded what it saw. Both
    # legs reach the call (the first's failure does not stop the loop from
    # reserving and calling for the second), and both were already ``reserved``
    # at that moment.
    assert len(client.calls) == 2
    assert [status for _payload, status in client.calls] == ["reserved", "reserved"]

    stored = load_management_batch(session_factory, batch.id)
    reserved_or_later = {"reserved", "submit_unknown", "recovery_required", "failed"}
    assert stored.legs[0].status in reserved_or_later
    assert stored.legs[0].status != "planned"
    assert stored.legs[0].client_order_id is not None
    assert stored.legs[0].request is not None


def test_protection_replace_leg_is_reserved_before_the_exchange_call_raises(tmp_path):
    """TPSL replace path: ``set_position_sltp`` raises; the leg must be ``reserved``."""

    session_factory = create_session_factory(tmp_path / "research.db")
    batch, rows_by_pos = _persist_protection_batch(session_factory)
    client = _ProtectionClient(
        session_factory,
        rows_by_pos,
        set_outcomes=[_SimulatedWriteFailure("simulated protection failure")],
    )

    # ``_execute_protection_batch``'s replacement loop catches every exception
    # from the per-row submit itself (to compensate and restore, rather than
    # letting one row's failure surface as a bare exception), so this does not
    # propagate -- the assertion that matters is on the ledger, not on how the
    # call returns.
    execute_management_batch(
        session_factory, batch_id=batch.id, deepcoin_client=client, executed_at=NOW
    )

    assert len(client.set_calls) == 2
    with session_factory() as session:
        legs = (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id == batch.id)
            .order_by(StrategyManagementLeg.leg_index)
            .all()
        )
    # Both legs were already ``reserved`` (with their cancel/replacement plan
    # committed as ``request_json``) before their respective ``set_position_
    # sltp`` call -- the loop's own exception handler then moves the failing
    # one on to ``recovery_required``, but neither was ever ``planned`` at the
    # moment of the raise.
    assert all(leg.status != "planned" for leg in legs)
    assert all(leg.request_json is not None for leg in legs)
    assert {leg.status for leg in legs} <= {"reserved", "recovery_required", "succeeded"}


def test_rescue_is_reserved_before_the_exchange_call_raises(tmp_path):
    """Rescue path: ``set_position_sltp`` raises; the rescue must be ``reserved``.

    Unlike the two paths above, ``execute_trigger_protection_stop_rescue``
    deliberately swallows the exception into ``_complete_trigger_protection_
    rescue_failure`` rather than propagating it (a durable reservation cannot
    distinguish a crash before the call from one after, so it is never retried
    automatically) -- so this test reads the ledger from the *return value*,
    not from a caught exception, and separately confirms the request was
    already durable before the client was ever invoked (the executor commits
    ``status="reserved"`` and ``request_json`` and releases the session before
    entering the ``try`` that calls the client at all).
    """

    from telegram_kol_research.models import TriggerProtectionIntent, TriggerProtectionStopRescue
    from telegram_kol_research.position_protection_legs import create_or_get_protection_leg
    from telegram_kol_research.strategy_management_planner import (
        plan_trigger_protection_stop_rescue,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    intent_id = _saved_deferred_intent(session_factory)
    with session_factory() as session:
        intent = session.get(TriggerProtectionIntent, intent_id)
        create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=int(intent.execution_order_leg_id),
            role="primary_stop",
            leg_index=1,
            planned_trigger_price="65000",
            planned_size=None,
        )
        session.commit()

    planning_client = _RescueClient()
    planned = plan_trigger_protection_stop_rescue(
        session_factory, intent_id=intent_id, deepcoin_client=planning_client, planned_at=NOW
    )
    assert planned.status == "ready"

    class _RaisingRescueClient(_RescueClient):
        def set_position_sltp(self, payload):
            # By the time this is called, the executor has already committed
            # ``reserved`` + ``request_json`` and released that session (see
            # ``execute_trigger_protection_stop_rescue``, which commits and
            # exits its ``with session_factory()`` block before the ``try``
            # that calls this method). Read it back here to prove that, not
            # just assert it from the outside afterward.
            with session_factory() as session:
                rescue = session.get(TriggerProtectionStopRescue, planned.rescue_id)
                assert rescue.status == "reserved"
                assert rescue.request_json is not None
            raise _SimulatedWriteFailure("simulated rescue failure")

    client = _RaisingRescueClient()
    result = execute_trigger_protection_stop_rescue(
        session_factory, rescue_id=planned.rescue_id, deepcoin_client=client, executed_at=NOW
    )

    # The executor caught it and terminalized the rescue rather than leaving it
    # ``reserved`` forever -- but it was ``reserved`` (with a durable request)
    # for the whole duration of the exchange call, which is what this test
    # exists to pin down.
    assert result["status"] in {"blocked", "unresolved", "submit_unknown"}
    with session_factory() as session:
        rescue = session.get(TriggerProtectionStopRescue, planned.rescue_id)
        assert rescue.status != "ready"
        assert rescue.request_json is not None
