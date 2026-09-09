"""Phase 6-pre-6: a blocked lease must not outlive the process that caused it.

On 2026-09-09 batch 7's holder never returned the lease. The next applicant
found it expired, turned the document to ``blocked``, and ``blocked`` had no
reset path at all -- so every entry after it was refused until a person
intervened. One real entry was lost that way. These tests pin each of the four
things that had to change, and the one that matters most: after the reset, an
acquire actually succeeds.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_revision_exchange_authority import (
    BLOCKED_RESET_GRACE,
    BLOCKED_RESET_INCIDENT_TYPE,
    RESET_AUDIT_ACTION,
    acquire_entry_revision_exchange_authority,
    entry_revision_authority_owner_is_alive,
    reset_blocked_entry_revision_authority,
    seed_entry_revision_exchange_authority,
)
from telegram_kol_research.entry_revision_exchange_authority_contract import (
    ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY,
)
from telegram_kol_research.models import ExecutionEvent, RuntimeIncident, TradingSetting

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _factory(tmp_path, name="authority.db"):
    session_factory = create_session_factory(tmp_path / name)
    seeded = seed_entry_revision_exchange_authority(
        session_factory, seeded_at=NOW - timedelta(hours=1)
    )
    assert seeded.seeded or seeded.reason_code == (
        "entry_revision_exchange_authority_already_exists"
    )
    return session_factory


def _write_blocked(
    session_factory,
    *,
    generation=7,
    blocked_at=NOW,
    owner_pid=4242,
    owner_start_ticks=99999,
    legacy=False,
):
    document = {
        "action_id": "batch:7",
        "blocked_at": blocked_at.isoformat(),
        "generation": generation,
        "prior_owner_kind": "entry_revision_worker",
        "reason_code": "authority_lease_expired",
        "schema_version": 2,
        "state": "blocked",
        "token_sha256": "a" * 64,
        "write_boundary_reached": True,
    }
    if not legacy:
        document["owner_pid"] = owner_pid
        document["owner_start_ticks"] = owner_start_ticks
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
            .one()
        )
        row.value_json = json.dumps(document, sort_keys=True, separators=(",", ":"))
        session.commit()


def _document(session_factory):
    with session_factory() as session:
        row = (
            session.query(TradingSetting)
            .filter(TradingSetting.key == ENTRY_REVISION_EXCHANGE_AUTHORITY_KEY)
            .one()
        )
        return json.loads(row.value_json)


def _dead(**_kwargs):
    return False


def _alive(**_kwargs):
    return True


def _unknown(**_kwargs):
    return None


# --- (3) the blocked document has to say who was holding it ---------------


def test_a_blocked_document_carries_the_owner_identity(tmp_path):
    """Without this nothing downstream can tell a crash from a live write."""

    session_factory = _factory(tmp_path)
    acquired = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:7",
        acquired_at=NOW - timedelta(minutes=20),
    )
    assert acquired.acquired

    # The next applicant finds the lease expired and blocks it.
    refused = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:8",
        acquired_at=NOW,
    )
    assert refused.reason_code == (
        "entry_revision_exchange_authority_expired_blocked"
    )
    document = _document(session_factory)
    assert document["state"] == "blocked"
    assert document["owner_pid"] > 1
    assert document["owner_start_ticks"] > 0


def test_a_legacy_blocked_document_is_still_legal(tmp_path):
    """A document written by the previous release is mid-flight during deploy.

    Rejecting it would turn a recoverable block into an unparseable one, which
    is worse than the bug being fixed.
    """

    session_factory = _factory(tmp_path)
    _write_blocked(session_factory, legacy=True)

    result = reset_blocked_entry_revision_authority(
        session_factory, now=NOW + timedelta(seconds=30), liveness_probe=_unknown
    )
    # Unknown owner, block still fresh: hold.
    assert result.reset is False
    assert result.reason_code == "entry_revision_exchange_authority_block_recent"

    # Once the grace has passed there is nothing left to wait for.
    late = reset_blocked_entry_revision_authority(
        session_factory,
        now=NOW + BLOCKED_RESET_GRACE + timedelta(seconds=1),
        liveness_probe=_unknown,
    )
    assert late.reset is True
    assert late.reason_code == "block_grace_elapsed"


# --- (2)/(3) liveness decides, and "unknown" is not "dead" ----------------


def test_a_dead_owner_is_reset_immediately(tmp_path):
    session_factory = _factory(tmp_path)
    _write_blocked(session_factory, generation=7)

    result = reset_blocked_entry_revision_authority(
        session_factory, now=NOW + timedelta(seconds=5), liveness_probe=_dead
    )

    assert result.reset is True
    assert result.reason_code == "owner_process_gone"
    document = _document(session_factory)
    assert document["state"] == "idle"
    assert document["generation"] == 8
    # (idle key set is fixed and validated exactly -- the reason cannot live here)
    assert set(document) == {"schema_version", "state", "generation", "released_at"}


def test_a_live_owner_is_never_reset(tmp_path):
    """The dangerous direction: resetting under a process still mid-write."""

    session_factory = _factory(tmp_path)
    _write_blocked(session_factory)

    result = reset_blocked_entry_revision_authority(
        session_factory,
        now=NOW + BLOCKED_RESET_GRACE + timedelta(hours=1),
        liveness_probe=_alive,
    )

    assert result.reset is False
    assert result.reason_code == "entry_revision_exchange_authority_owner_alive"
    assert _document(session_factory)["state"] == "blocked"


def test_an_unprovable_owner_waits_for_the_grace_period(tmp_path):
    session_factory = _factory(tmp_path)
    _write_blocked(session_factory)

    early = reset_blocked_entry_revision_authority(
        session_factory, now=NOW + timedelta(minutes=9), liveness_probe=_unknown
    )
    assert early.reset is False

    late = reset_blocked_entry_revision_authority(
        session_factory,
        now=NOW + BLOCKED_RESET_GRACE + timedelta(seconds=1),
        liveness_probe=_unknown,
    )
    assert late.reset is True


def test_identity_is_the_pair_not_the_pid_alone():
    """A reused pid reads as alive while the real owner is long gone."""

    import os

    me = os.getpid()
    assert entry_revision_authority_owner_is_alive(
        owner_pid=me, owner_start_ticks=1
    ) in (False, None)
    assert entry_revision_authority_owner_is_alive(
        owner_pid=None, owner_start_ticks=None
    ) is None


# --- the reset leaves both traces ----------------------------------------


def test_a_reset_leaves_an_audit_row_and_an_alert(tmp_path):
    session_factory = _factory(tmp_path)
    _write_blocked(session_factory, generation=11)

    reset_blocked_entry_revision_authority(
        session_factory, now=NOW + timedelta(seconds=5), liveness_probe=_dead
    )

    with session_factory() as session:
        audit = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == RESET_AUDIT_ACTION)
            .one()
        )
        before = json.loads(audit.before_json)
        assert before["prior_generation"] == 11
        assert before["prior_owner_kind"] == "entry_revision_worker"
        assert before["blocked_reason_code"] == "authority_lease_expired"
        assert before["write_boundary_reached"] is True
        assert json.loads(audit.after_json)["generation"] == 12

        incident = (
            session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == BLOCKED_RESET_INCIDENT_TYPE)
            .one()
        )
        assert incident.severity == "high"
        assert json.loads(incident.redacted_summary)["reason_code"] == (
            "owner_process_gone"
        )


def test_the_reset_alert_is_always_notified():
    from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES

    assert BLOCKED_RESET_INCIDENT_TYPE in ALWAYS_NOTIFIED_INCIDENT_TYPES


# --- the whole point: after the reset, an acquire succeeds ----------------


def test_after_a_reset_an_acquire_succeeds(tmp_path):
    """The deadlock is only fixed if the next entry can actually proceed."""

    session_factory = _factory(tmp_path)
    _write_blocked(session_factory, generation=7)

    reset_blocked_entry_revision_authority(
        session_factory, now=NOW + timedelta(seconds=5), liveness_probe=_dead
    )
    acquired = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:9",
        acquired_at=NOW + timedelta(seconds=6),
    )

    assert acquired.acquired is True
    assert acquired.generation == 9
    assert _document(session_factory)["state"] == "held"


def test_acquire_itself_clears_a_block_whose_owner_is_gone(tmp_path):
    """The fast half: recovery must not need a separate maintenance tick.

    The first acquire reports the reset rather than taking the lease inside a
    transaction that already read a stale document; the next one takes it.
    """

    session_factory = _factory(tmp_path)
    # A pid that cannot be running: pid 2 with an impossible start time.
    _write_blocked(session_factory, generation=3, owner_pid=2, owner_start_ticks=1)

    first = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:10",
        acquired_at=NOW + BLOCKED_RESET_GRACE + timedelta(seconds=1),
    )
    assert first.acquired is False
    assert first.reason_code == "entry_revision_exchange_authority_blocked_reset"

    second = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:10",
        acquired_at=NOW + BLOCKED_RESET_GRACE + timedelta(seconds=2),
    )
    assert second.acquired is True


def test_a_live_owner_still_blocks_acquire(tmp_path, monkeypatch):
    """The fast path must not become a way to steal a live lease."""

    import telegram_kol_research.entry_revision_exchange_authority as module

    session_factory = _factory(tmp_path)
    _write_blocked(session_factory, generation=5)
    monkeypatch.setattr(module, "entry_revision_authority_owner_is_alive", _alive)

    refused = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id="batch:11",
        acquired_at=NOW + timedelta(hours=2),
    )

    assert refused.acquired is False
    assert refused.reason_code == "entry_revision_exchange_authority_blocked"
    assert _document(session_factory)["state"] == "blocked"


def test_reset_is_a_no_op_on_an_idle_document(tmp_path):
    session_factory = _factory(tmp_path)

    result = reset_blocked_entry_revision_authority(
        session_factory, now=NOW, liveness_probe=_dead
    )

    assert result.reset is False
    assert result.reason_code == "entry_revision_exchange_authority_not_blocked"


# --- (1) a lease held for a batch that has finished ----------------------


def _terminal_batch(session_factory, *, status="recovery_required", reason="x"):
    """The smallest batch row the release guard will accept."""

    from telegram_kol_research.models import (
        ExecutionBinding,
        RawMessage,
        StrategyLifecycle,
        StrategyRevisionBatch,
        StrategyThread,
    )

    with session_factory() as session:
        raw = RawMessage(chat_id=-1001, message_id=9, posted_at=NOW, text="x")
        session.add(raw)
        session.flush()
        thread = StrategyThread(
            chat_id=-1001, root_message_id=9, symbol="BTC", side="short",
            status="open",
        )
        session.add(thread)
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="s", kol_id="k", chat_id=-1001, message_id=9,
            symbol="BTC", side="short", venue="deepcoin", margin_mode="cross",
            position_mode="split", status="open",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=-1001, message_id=9, symbol="BTC", side="short",
            lifecycle_status="entered", signal_at=NOW, filled_tp_index=0,
        )
        session.add(lifecycle)
        session.flush()
        batch = StrategyRevisionBatch(
            idempotency_fingerprint="f" * 64,
            raw_message_id=raw.id,
            strategy_thread_id=thread.id,
            target_lifecycle_id=lifecycle.id,
            execution_binding_id=binding.id,
            status=status,
            replacement_json="{}",
            reason_code=reason,
            planned_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(batch)
        session.commit()
        return batch.id


def _hold_for_batch(session_factory, batch_id):
    acquired = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="entry_revision_worker",
        owner_id=f"batch:{batch_id}",
        acquired_at=NOW,
    )
    assert acquired.acquired
    return acquired.generation


def test_a_lease_held_for_a_finished_batch_is_returned(tmp_path):
    """The 2026-09-09 shape: the token is gone and the batch is terminal."""

    from telegram_kol_research.entry_revision_exchange_authority import (
        release_authority_for_finished_batches,
    )

    session_factory = _factory(tmp_path, name="terminal-release.db")
    batch_id = _terminal_batch(session_factory)
    generation = _hold_for_batch(session_factory, batch_id)

    result = release_authority_for_finished_batches(
        session_factory, now=NOW + timedelta(seconds=5)
    )

    assert result.released is True
    assert result.generation == generation + 1
    assert _document(session_factory)["state"] == "idle"

    with session_factory() as session:
        audit = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action == "entry_revision_authority_terminal_release"
            )
            .one()
        )
        before = json.loads(audit.before_json)
        assert before["batch_id"] == batch_id
        assert before["generation"] == generation
        assert before["batch_status"] == "recovery_required"


def test_a_lease_held_for_a_running_batch_is_left_alone(tmp_path):
    """The isolation the terminal branches rely on must survive this sweep."""

    from telegram_kol_research.entry_revision_exchange_authority import (
        release_authority_for_finished_batches,
    )

    session_factory = _factory(tmp_path, name="running-batch.db")
    batch_id = _terminal_batch(session_factory, status="planned", reason="")
    _hold_for_batch(session_factory, batch_id)

    result = release_authority_for_finished_batches(
        session_factory, now=NOW + timedelta(seconds=5)
    )

    assert result.released is False
    assert result.reason_code == "entry_revision_batch_not_terminal"
    assert _document(session_factory)["state"] == "held"


def test_the_terminal_release_refuses_a_generation_someone_else_now_holds(tmp_path):
    """Returning a lease that has moved on would be theft, not cleanup."""

    from telegram_kol_research.entry_revision_exchange_authority import (
        release_entry_revision_authority_for_terminal_batch,
    )

    session_factory = _factory(tmp_path, name="stale-generation.db")
    batch_id = _terminal_batch(session_factory)
    generation = _hold_for_batch(session_factory, batch_id)

    result = release_entry_revision_authority_for_terminal_batch(
        session_factory,
        batch_id=batch_id,
        generation=generation - 1,
        owner_kind="entry_revision_worker",
        now=NOW + timedelta(seconds=5),
    )

    assert result.released is False
    assert result.reason_code == "entry_revision_exchange_authority_owner_mismatch"
    assert _document(session_factory)["state"] == "held"


def test_a_signal_holder_is_never_swept(tmp_path):
    """The new-entry path has no batch row to prove terminality against."""

    from telegram_kol_research.entry_revision_exchange_authority import (
        release_authority_for_finished_batches,
    )

    session_factory = _factory(tmp_path, name="signal-holder.db")
    acquired = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="new_entry_worker",
        owner_id="signal:41",
        acquired_at=NOW,
    )
    assert acquired.acquired

    result = release_authority_for_finished_batches(
        session_factory, now=NOW + timedelta(seconds=5)
    )

    assert result.released is False
    assert result.reason_code == "entry_revision_authority_holder_not_a_batch"
    assert _document(session_factory)["state"] == "held"


# --- the token rule has one hole; keep it that size ----------------------


def test_only_the_authority_module_may_release_without_a_token():
    """A token-free release is a deliberate hole. It must stay one hole.

    ``release_entry_revision_authority_for_terminal_batch`` returns a lease
    without proving possession of its token. That is justified only by the fact
    that it re-reads the batch and refuses unless it is terminal. A second
    caller with a different justification would not be covered by that
    reasoning, so a new name here has to be a decision somebody makes in front
    of this assertion.
    """

    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"
    guarded = "release_entry_revision_authority_for_terminal_batch"
    callers = set()
    for path in sorted(src.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == guarded for alias in node.names
            ):
                callers.add(path.name)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == guarded
            ):
                callers.add(path.name)

    assert callers == {
        # Defines it, and calls it from the finished-batch sweep.
        "entry_revision_exchange_authority.py",
    }, sorted(callers)


def test_the_ordinary_release_still_requires_its_token(tmp_path):
    """The hole must not have widened the normal path."""

    from telegram_kol_research.entry_revision_exchange_authority import (
        release_entry_revision_exchange_authority,
    )

    session_factory = _factory(tmp_path, name="token-still-required.db")
    batch_id = _terminal_batch(session_factory)
    generation = _hold_for_batch(session_factory, batch_id)

    released = release_entry_revision_exchange_authority(
        session_factory,
        token="not-the-real-token",
        owner_kind="entry_revision_worker",
        expected_generation=generation,
        released_at=NOW + timedelta(seconds=1),
    )

    assert released.released is False
    assert _document(session_factory)["state"] == "held"


# --- 6-pre-7: the same hole where the holder is a trade signal ------------


def _terminal_signal(session_factory, *, status="failed"):
    from telegram_kol_research.models import TradeSignal

    with session_factory() as session:
        signal = TradeSignal(
            signal_uid=f"uid-{status}",
            strategy_instance_id="deepcoin:-1001:9:BTC:short",
            source_type="recovery",
            venue="deepcoin",
            kol_id="k",
            chat_id=-1001,
            message_id=9,
            symbol="BTC",
            side="short",
            action="open_position",
            status=status,
            payload_json="{}",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(signal)
        session.commit()
        return signal.id


def _hold_for_signal(session_factory, signal_id):
    acquired = acquire_entry_revision_exchange_authority(
        session_factory,
        owner_kind="new_entry_worker",
        owner_id=f"signal:{signal_id}",
        acquired_at=NOW,
    )
    assert acquired.acquired
    return acquired.generation


def test_a_lease_held_for_a_finished_signal_is_returned(tmp_path):
    """The hole 6-pre-6 deliberately left open, now closed."""

    from telegram_kol_research.entry_revision_exchange_authority import (
        release_authority_for_finished_trade_signals,
    )

    session_factory = _factory(tmp_path, name="signal-release.db")
    signal_id = _terminal_signal(session_factory, status="failed")
    generation = _hold_for_signal(session_factory, signal_id)

    result = release_authority_for_finished_trade_signals(
        session_factory, now=NOW + timedelta(seconds=5)
    )

    assert result.released is True
    assert result.generation == generation + 1
    assert _document(session_factory)["state"] == "idle"
    with session_factory() as session:
        audit = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action == "entry_revision_authority_signal_release"
            )
            .one()
        )
        before = json.loads(audit.before_json)
        assert before["trade_signal_id"] == signal_id
        assert before["signal_status"] == "failed"
        assert before["generation"] == generation


@pytest.mark.parametrize("status", ["pending", "processing"])
def test_a_lease_held_for_a_running_signal_is_left_alone(tmp_path, status):
    """A signal still working must keep its exclusion."""

    from telegram_kol_research.entry_revision_exchange_authority import (
        release_authority_for_finished_trade_signals,
    )

    session_factory = _factory(tmp_path, name=f"signal-running-{status}.db")
    signal_id = _terminal_signal(session_factory, status=status)
    _hold_for_signal(session_factory, signal_id)

    result = release_authority_for_finished_trade_signals(
        session_factory, now=NOW + timedelta(seconds=5)
    )

    assert result.released is False
    assert result.reason_code == "trade_signal_not_terminal"
    assert _document(session_factory)["state"] == "held"


def test_an_unfamiliar_signal_state_leaves_the_lease_alone(tmp_path):
    """The state test is a whitelist, so a state nobody anticipated is safe."""

    from telegram_kol_research.entry_revision_exchange_authority import (
        release_authority_for_finished_trade_signals,
    )

    session_factory = _factory(tmp_path, name="signal-unknown-state.db")
    signal_id = _terminal_signal(session_factory, status="some_future_state")
    _hold_for_signal(session_factory, signal_id)

    result = release_authority_for_finished_trade_signals(
        session_factory, now=NOW + timedelta(seconds=5)
    )

    assert result.released is False
    assert result.reason_code == "trade_signal_not_terminal"
    assert _document(session_factory)["state"] == "held"


def test_the_signal_release_refuses_a_generation_someone_else_now_holds(tmp_path):
    from telegram_kol_research.entry_revision_exchange_authority import (
        release_entry_revision_authority_for_terminal_signal,
    )

    session_factory = _factory(tmp_path, name="signal-stale-gen.db")
    signal_id = _terminal_signal(session_factory)
    generation = _hold_for_signal(session_factory, signal_id)

    result = release_entry_revision_authority_for_terminal_signal(
        session_factory,
        trade_signal_id=signal_id,
        generation=generation - 1,
        owner_kind="new_entry_worker",
        now=NOW + timedelta(seconds=5),
    )

    assert result.released is False
    assert result.reason_code == "entry_revision_exchange_authority_owner_mismatch"
    assert _document(session_factory)["state"] == "held"


def test_the_two_sweeps_do_not_touch_each_others_holders(tmp_path):
    """A batch sweep must not release a signal's lease, and vice versa."""

    from telegram_kol_research.entry_revision_exchange_authority import (
        release_authority_for_finished_batches,
        release_authority_for_finished_trade_signals,
    )

    session_factory = _factory(tmp_path, name="cross-holder.db")
    signal_id = _terminal_signal(session_factory)
    _hold_for_signal(session_factory, signal_id)

    batch_sweep = release_authority_for_finished_batches(
        session_factory, now=NOW + timedelta(seconds=5)
    )
    assert batch_sweep.released is False
    assert batch_sweep.reason_code == "entry_revision_authority_holder_not_a_batch"
    assert _document(session_factory)["state"] == "held"

    signal_sweep = release_authority_for_finished_trade_signals(
        session_factory, now=NOW + timedelta(seconds=6)
    )
    assert signal_sweep.released is True


def test_the_signal_sweep_refuses_a_batch_holder(tmp_path):
    """The dangerous direction, and the one the ids make easy to get wrong.

    A ``batch:7`` holder parses as the integer 7 just as happily as a
    ``signal:7`` one. Without the prefix check the signal sweep would look up
    trade signal 7 -- an unrelated row that may well be terminal -- and release
    a lease held for a batch that is still running.
    """

    from telegram_kol_research.entry_revision_exchange_authority import (
        release_authority_for_finished_trade_signals,
    )
    from telegram_kol_research.models import StrategyRevisionBatch

    session_factory = _factory(tmp_path, name="signal-sweep-batch-holder.db")
    # A running batch holds the lease...
    batch_id = _terminal_batch(session_factory, status="planned", reason="")
    _hold_for_batch(session_factory, batch_id)
    # ...and a *terminal* trade signal happens to carry the same id.
    with session_factory() as session:
        assert session.get(StrategyRevisionBatch, batch_id) is not None
    from telegram_kol_research.models import TradeSignal

    with session_factory() as session:
        session.add(
            TradeSignal(
                id=batch_id,
                signal_uid="collision",
                strategy_instance_id="s",
                source_type="recovery",
                venue="deepcoin",
                kol_id="k",
                chat_id=-1001,
                message_id=9,
                symbol="BTC",
                side="short",
                action="open_position",
                status="failed",
                payload_json="{}",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    result = release_authority_for_finished_trade_signals(
        session_factory, now=NOW + timedelta(seconds=5)
    )

    assert result.released is False
    assert result.reason_code == "entry_revision_authority_holder_not_a_signal"
    # The running batch keeps its exclusion.
    assert _document(session_factory)["state"] == "held"


def test_only_the_authority_module_may_release_a_signal_without_a_token():
    """Second token-free release, same guard as the first."""

    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"
    guarded = "release_entry_revision_authority_for_terminal_signal"
    callers = set()
    for path in sorted(src.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(
                alias.name == guarded for alias in node.names
            ):
                callers.add(path.name)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == guarded
            ):
                callers.add(path.name)
    assert callers == {"entry_revision_exchange_authority.py"}, sorted(callers)
