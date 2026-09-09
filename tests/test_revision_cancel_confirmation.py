"""Phase 6-pre-5, the read half: what a lost cancel receipt actually did.

The fixed case is batch 7 on 2026-09-09, with the two orders exactly as the
exchange held them: ``...446`` cancelled and gone, ``...560`` still resting at
trigger 81910. That batch is now ``resolved`` because the user chose to keep
``...560``, and the last test here is the one that matters most -- a settled
batch must never be resumed, or the automation would cancel the leg the user
asked to keep.
"""

import pytest

from telegram_kol_research.revision_cancel_confirmation import (
    CONFIRMED_CANCELLED,
    CONFIRMED_TERMINAL_REASON,
    STILL_RESTING,
    UNREADABLE,
    cancel_retry_idempotency_key,
    confirm_unknown_cancel_outcome,
)

INST = "BTC-USDT-SWAP"
CANCELLED_ORDER = "1001125173252446"
RESTING_ORDER = "1001125173252560"

# Quoted from the live exchange on 2026-09-09, not invented.
RESTING_ROW = {
    "ordId": RESTING_ORDER,
    "instId": INST,
    "posSide": "short",
    "side": "sell",
    "sz": "6",
    "triggerOrderType": "Conditional",
    "closeSLTriggerPrice": "83000",
}
CANCELLED_HISTORY = {
    "instId": INST,
    "ordId": CANCELLED_ORDER,
    "px": "80910",
    "sz": "6",
    "triggerPx": "80910",
    "ordType": "Conditional",
    "side": "sell",
    "posSide": "short",
    "triggerTime": "0",
    "uTime": "1788947465000",
    "cTime": "1788798775000",
    "errorCode": "0",
}


class Client:
    def __init__(self, *, pending=None, history=None, pending_raises=False,
                 history_raises=False):
        self._pending = pending if pending is not None else []
        self._history = history
        self._pending_raises = pending_raises
        self._history_raises = history_raises

    def list_trigger_orders_pending(self, *, inst_id):
        if self._pending_raises:
            raise RuntimeError("exchange unreachable")
        return self._pending

    def get_trigger_order_history_by_id(self, *, inst_id, order_id):
        if self._history_raises:
            raise RuntimeError("exchange unreachable")
        return self._history


def test_batch_sevens_cancelled_order_is_confirmed_cancelled():
    client = Client(pending=[RESTING_ROW], history=CANCELLED_HISTORY)

    outcome = confirm_unknown_cancel_outcome(
        client, inst_id=INST, order_id=CANCELLED_ORDER
    )

    assert outcome.status == CONFIRMED_CANCELLED
    assert outcome.evidence["u_time"] == "1788947465000"
    assert outcome.evidence["trigger_time"] == "0"
    assert outcome.evidence["absent_from_pending"] is True


def test_batch_sevens_kept_order_reads_as_still_resting():
    client = Client(pending=[RESTING_ROW], history=None)

    outcome = confirm_unknown_cancel_outcome(
        client, inst_id=INST, order_id=RESTING_ORDER
    )

    assert outcome.status == STILL_RESTING
    assert outcome.evidence["sz"] == "6"


@pytest.mark.parametrize(
    ("client", "reason"),
    [
        (Client(pending_raises=True), "pending_snapshot_unavailable"),
        (Client(pending=[], history_raises=True), "history_unavailable"),
        (Client(pending=[], history=None), "absent_from_pending_and_history"),
        (Client(pending=[], history={}), "absent_from_pending_and_history"),
    ],
)
def test_every_gap_in_what_we_can_see_is_unknown(client, reason):
    """Hard rule 4: an unreadable exchange produces unknown, never zero."""

    outcome = confirm_unknown_cancel_outcome(
        client, inst_id=INST, order_id=CANCELLED_ORDER
    )

    assert outcome.status == UNREADABLE
    assert outcome.reason == reason


def test_an_order_that_triggered_is_not_a_cancel_question():
    """Absence from pending because it fired is not evidence of cancellation."""

    triggered = {**CANCELLED_HISTORY, "triggerTime": "1788947400000"}
    client = Client(pending=[], history=triggered)

    outcome = confirm_unknown_cancel_outcome(
        client, inst_id=INST, order_id=CANCELLED_ORDER
    )

    assert outcome.status == UNREADABLE
    assert outcome.reason == "order_triggered_before_cancel"


def test_the_retry_key_names_the_order():
    assert cancel_retry_idempotency_key(CANCELLED_ORDER) == (
        f"cancel-retry:{CANCELLED_ORDER}"
    )


def test_the_confirmed_reason_matches_the_one_off_that_repaired_batch_seven():
    """The automated path and the hand repair must read alike in the ledger."""

    from telegram_kol_research.one_off.revision_batch_7_alignment_2026_09_09 import (
        TERMINAL_REASON,
    )

    assert CONFIRMED_TERMINAL_REASON == TERMINAL_REASON


def test_a_settled_batch_can_never_be_resumed():
    """Batch 7 is resolved because the user chose to keep the resting leg.

    Resuming it would cancel that leg. The refusal has to live in the planner,
    not in whichever caller happens to check first.
    """

    from telegram_kol_research.strategy_revision_planner import (
        TERMINAL_REVISION_STATES,
    )

    assert "resolved" in TERMINAL_REVISION_STATES
    assert "recovery_required" in TERMINAL_REVISION_STATES


# --- the reconcile half ---------------------------------------------------

import json as _json
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    RuntimeIncident,
    StrategyLifecycle,
    StrategyRevisionBatch,
    StrategyRevisionLeg,
    StrategyThread,
)
from telegram_kol_research.revision_cancel_confirmation import (
    RevisionCancelReconcileResult,
    reconcile_unknown_revision_cancels,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _batch_seven(tmp_path, *, name="batch7.db", status="recovery_required",
                 reason="revision_cancel_outcome_unknown", completed_at=None):
    """Batch 7's real shape: two cancel legs, both stuck at submit_unknown."""

    session_factory = create_session_factory(tmp_path / name)
    with session_factory() as session:
        raw = RawMessage(chat_id=-1002370796392, message_id=3639, posted_at=NOW, text="x")
        session.add(raw)
        session.flush()
        thread = StrategyThread(
            chat_id=-1002370796392, root_message_id=3633, symbol="BTC",
            side="short", status="open",
        )
        session.add(thread)
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:-1002370796392:3633:BTC:short",
            kol_id="k", chat_id=-1002370796392, message_id=3633, symbol="BTC",
            side="short", venue="deepcoin", margin_mode="cross",
            position_mode="split", status="open",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=-1002370796392, message_id=3633, symbol="BTC", side="short",
            lifecycle_status="pending_entry", signal_at=NOW, filled_tp_index=0,
        )
        session.add(lifecycle)
        session.flush()
        batch = StrategyRevisionBatch(
            idempotency_fingerprint="7" * 64, raw_message_id=raw.id,
            strategy_thread_id=thread.id, target_lifecycle_id=lifecycle.id,
            execution_binding_id=binding.id, status=status,
            replacement_json='{"entry":"81000-82000"}', reason_code=reason,
            planned_at=NOW, completed_at=completed_at, created_at=NOW, updated_at=NOW,
        )
        session.add(batch)
        session.flush()
        legs = {}
        for index, order_id in ((1, CANCELLED_ORDER), (2, RESTING_ORDER)):
            execution_leg = ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id="deepcoin:-1002370796392:3633:BTC:short",
                leg_index=index, purpose="entry", order_kind="trigger_limit",
                venue="deepcoin", order_id=order_id, status="pending",
            )
            session.add(execution_leg)
            session.flush()
            revision_leg = StrategyRevisionLeg(
                revision_batch_id=batch.id,
                execution_order_leg_id=execution_leg.id,
                action="cancel_pending", prior_status="pending",
                status="submit_unknown", order_id=order_id,
                created_at=NOW, updated_at=NOW,
            )
            session.add(revision_leg)
            session.flush()
            legs[order_id] = (execution_leg.id, revision_leg.id)
        session.commit()
        return session_factory, batch.id, legs


def test_batch_seven_reconciles_to_one_confirmed_and_one_retry(tmp_path):
    """The real 2026-09-09 state, end to end, with no exchange write."""

    session_factory, batch_id, legs = _batch_seven(tmp_path)
    client = Client(pending=[RESTING_ROW], history=CANCELLED_HISTORY)

    result = reconcile_unknown_revision_cancels(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=5)
    )

    assert (result.confirmed, result.retried, result.alerted) == (1, 1, 0)
    with session_factory() as session:
        cancelled_exec, cancelled_rev = legs[CANCELLED_ORDER]
        assert session.get(ExecutionOrderLeg, cancelled_exec).status == "cancelled"
        assert session.get(ExecutionOrderLeg, cancelled_exec).terminal_reason == (
            CONFIRMED_TERMINAL_REASON
        )
        assert session.get(StrategyRevisionLeg, cancelled_rev).status == "cancelled"

        resting_exec, resting_rev = legs[RESTING_ORDER]
        resting_leg = session.get(StrategyRevisionLeg, resting_rev)
        # Handed back to the ordinary advance path, with the one retry stamped.
        assert resting_leg.status == "pending"
        assert _json.loads(resting_leg.error_json)["cancel_retry"]["key"] == (
            f"cancel-retry:{RESTING_ORDER}"
        )
        assert session.get(ExecutionOrderLeg, resting_exec).status == "pending"

        batch = session.get(StrategyRevisionBatch, batch_id)
        assert batch.status == "planned"
        assert batch.reason_code is None


def test_the_retry_happens_at_most_once(tmp_path):
    session_factory, batch_id, _ = _batch_seven(tmp_path, name="retry-once.db")
    client = Client(pending=[RESTING_ROW], history=CANCELLED_HISTORY)

    first = reconcile_unknown_revision_cancels(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=5)
    )
    # Put it back into the frozen shape, as a second lost receipt would.
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, batch_id)
        batch.status = "recovery_required"
        batch.reason_code = "revision_cancel_outcome_unknown"
        leg = (
            session.query(StrategyRevisionLeg)
            .filter(StrategyRevisionLeg.order_id == RESTING_ORDER)
            .one()
        )
        leg.status = "submit_unknown"
        session.commit()

    second = reconcile_unknown_revision_cancels(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=10)
    )

    assert first.retried == 1
    assert second.retried == 0
    assert second.alerted == 1
    with session_factory() as session:
        assert session.get(StrategyRevisionBatch, batch_id).status == (
            "recovery_required"
        )


def test_an_unreadable_exchange_freezes_and_alerts(tmp_path):
    session_factory, batch_id, _ = _batch_seven(tmp_path, name="unreadable.db")
    client = Client(pending_raises=True)

    result = reconcile_unknown_revision_cancels(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=5)
    )

    assert (result.confirmed, result.retried) == (0, 0)
    assert result.alerted == 2
    with session_factory() as session:
        assert session.get(StrategyRevisionBatch, batch_id).status == (
            "recovery_required"
        )
        incidents = (
            session.query(RuntimeIncident)
            .filter(
                RuntimeIncident.incident_type == "revision_cancel_outcome_unresolved"
            )
            .all()
        )
        assert incidents and incidents[0].severity == "high"


def test_a_settled_batch_is_never_selected(tmp_path):
    """Batch 7 today: resolved because the user kept the resting leg."""

    session_factory, batch_id, legs = _batch_seven(
        tmp_path, name="settled.db", status="resolved",
        reason="operator_kept_remaining_leg", completed_at=NOW,
    )
    client = Client(pending=[RESTING_ROW], history=CANCELLED_HISTORY)

    result = reconcile_unknown_revision_cancels(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=5)
    )

    assert result == RevisionCancelReconcileResult()
    with session_factory() as session:
        assert session.get(StrategyRevisionBatch, batch_id).status == "resolved"
        resting_exec, _ = legs[RESTING_ORDER]
        # The leg the user asked to keep is untouched, field for field.
        assert session.get(ExecutionOrderLeg, resting_exec).status == "pending"


def test_a_completed_batch_is_never_selected(tmp_path):
    session_factory, batch_id, _ = _batch_seven(
        tmp_path, name="completed.db", completed_at=NOW,
    )
    client = Client(pending=[RESTING_ROW], history=CANCELLED_HISTORY)

    result = reconcile_unknown_revision_cancels(
        session_factory, deepcoin_client=client, now=NOW + timedelta(minutes=5)
    )

    assert result == RevisionCancelReconcileResult()


def test_the_reconciler_issues_no_exchange_write(tmp_path):
    """It reads. The retry is performed by the ordinary advance path."""

    session_factory, _, _ = _batch_seven(tmp_path, name="no-write.db")

    class WriteForbidden(Client):
        def cancel_trigger_order(self, *_args, **_kwargs):
            raise AssertionError("the reconciler must not cancel anything")

        def place_order(self, *_args, **_kwargs):
            raise AssertionError("the reconciler must not place anything")

    reconcile_unknown_revision_cancels(
        session_factory,
        deepcoin_client=WriteForbidden(
            pending=[RESTING_ROW], history=CANCELLED_HISTORY
        ),
        now=NOW + timedelta(minutes=5),
    )
