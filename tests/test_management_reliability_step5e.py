"""A-5e: shrinking a stop is a replacement, not an addition.

``set-position-sltp`` adds a TPSL rather than editing one -- eighteen
production writes produced eighteen order ids -- so submitting the smaller
stop and stopping there leaves ten lots and five lots armed against the same
five lots. At the same trigger price the oversized one can fire first, which
is a larger exit than the position has. This path has never fired in
production; it had to be fixed before it did.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telegram_kol_research.models import (
    PositionProtectionIncident,
    PositionProtectionLedger,
)
from telegram_kol_research.stop_loss_size_convergence import (
    REPLACE_INCOMPLETE_INCIDENT_TYPE,
    execute_stop_loss_resize,
    plan_stop_loss_resizes,
)
from tests.test_management_reliability_step5 import (  # noqa: F401
    _ResizeClient,
    _binding_fixture,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC).replace(tzinfo=None)


def _plan(session_factory):
    return plan_stop_loss_resizes(
        session_factory, positions=[{"posId": "pos-222", "pos": "5"}]
    )[0]


def _incidents(session_factory, incident_type):
    with session_factory() as session:
        return (
            session.query(PositionProtectionIncident)
            .filter(PositionProtectionIncident.incident_type == incident_type)
            .all()
        )


def _ledger(session_factory, order_id):
    with session_factory() as session:
        return (
            session.query(PositionProtectionLedger)
            .filter_by(order_id=order_id)
            .one()
        )


def test_the_old_stop_is_cancelled_and_its_ledger_row_retired(tmp_path):
    session_factory = _binding_fixture(tmp_path)
    client = _ResizeClient()

    result = execute_stop_loss_resize(
        session_factory,
        plan=_plan(session_factory),
        deepcoin_client=client,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == "succeeded"
    assert [entry.get("ordId") for entry in client.cancels] == ["stop-1"]
    # The new stop carries the live size; the superseded row is retired rather
    # than left claiming a stop that no longer exists.
    assert _ledger(session_factory, "stop-2").size_text == "5"
    assert _ledger(session_factory, "stop-1").status == "cancelled"
    assert _incidents(session_factory, REPLACE_INCOMPLETE_INCIDENT_TYPE) == []


def test_a_failed_cancel_keeps_the_new_stop_and_raises_the_alarm(tmp_path):
    """Over-protected is the safe side; the new stop is never withdrawn."""

    session_factory = _binding_fixture(tmp_path)
    client = _ResizeClient(cancel_raises=True)

    result = execute_stop_loss_resize(
        session_factory,
        plan=_plan(session_factory),
        deepcoin_client=client,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == "incomplete"
    assert result.reason_code == "resize_old_stop_cancel_failed"
    # One submit, and nothing that would undo it.
    assert len(client.calls) == 1
    incidents = _incidents(session_factory, REPLACE_INCOMPLETE_INCIDENT_TYPE)
    assert len(incidents) == 1
    # The old row still describes a stop that is genuinely still armed.
    assert _ledger(session_factory, "stop-1").status == "verified"


def test_a_cancel_the_venue_accepted_but_did_not_apply_is_not_trusted(tmp_path):
    """The read-back is the proof, not the acknowledgement."""

    session_factory = _binding_fixture(tmp_path)
    client = _ResizeClient(old_stop_survives_cancel=True)

    result = execute_stop_loss_resize(
        session_factory,
        plan=_plan(session_factory),
        deepcoin_client=client,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status == "incomplete"
    assert result.reason_code == "resize_old_stop_still_pending"
    assert _ledger(session_factory, "stop-1").status == "verified"
    assert len(_incidents(session_factory, REPLACE_INCOMPLETE_INCIDENT_TYPE)) == 1


def test_a_failed_submit_never_reaches_the_cancel(tmp_path):
    """Nothing is retired while the position still has only the old stop."""

    session_factory = _binding_fixture(tmp_path)
    client = _ResizeClient(readback=False)

    result = execute_stop_loss_resize(
        session_factory,
        plan=_plan(session_factory),
        deepcoin_client=client,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )

    assert result.status != "succeeded"
    assert client.cancels == []
    assert _ledger(session_factory, "stop-1").status == "verified"
    assert _ledger(session_factory, "stop-1").size_text == "10"


def test_the_replacement_is_idempotent_and_never_sends_a_second_pair(tmp_path):
    session_factory = _binding_fixture(tmp_path)
    client = _ResizeClient()
    plan = _plan(session_factory)

    first = execute_stop_loss_resize(
        session_factory,
        plan=plan,
        deepcoin_client=client,
        executed_at=NOW,
        live_execution_gate=lambda: True,
    )
    second = execute_stop_loss_resize(
        session_factory,
        plan=plan,
        deepcoin_client=client,
        executed_at=NOW + timedelta(minutes=1),
        live_execution_gate=lambda: True,
    )

    assert first.status == "succeeded"
    # Both halves are keyed: one submit, one cancel, however many passes run.
    assert len(client.calls) == 1
    assert len(client.cancels) == 1
    assert second.status in {"succeeded", "skipped", "incomplete"}
    assert _ledger(session_factory, "stop-1").status == "cancelled"


def test_the_alarm_type_is_always_notified():
    from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES

    assert "stop_resize_replace_incomplete" in ALWAYS_NOTIFIED_INCIDENT_TYPES
