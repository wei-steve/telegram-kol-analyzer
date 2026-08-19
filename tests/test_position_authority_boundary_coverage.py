"""Architecture test: Phase 2 Task 1, the position-authority boundary check.

Per-chat message lock sharding (docs/plans/2026-08-18-runtime-serialization-
remediation/phase-2-per-chat-lock-sharding.md) is only safe if every path
reachable from `auto_trade_executor` that submits a Deepcoin order or mutates
position/protection state is already serialized by `position_authority_lock()`
/ `@serialized_position_authority_mutation`, independent of the message lock.
Today it is not: two live call chains write exchange/position state under a
different lock, or under no lock at all. This is the phase file's Task 1
decision gate, and its answer is why `message_lock_mode` defaults to, and
must stay at, "global" - see docs/runtime-serialization-remediation-status.md
for the full trace.

This test encodes that finding as a regression guard, mirroring how
tests/test_runtime_event_loop_blocking_census.py holds an explicit allowlist:
if a covered leaf loses its lock, or a known-uncovered leaf becomes silently
"fixed" without anyone updating this file and the decision gate, both are
signals worth catching rather than a green suite silently drifting out of
sync with the documented finding.
"""

from __future__ import annotations

import inspect

import pytest

from telegram_kol_research import deepcoin_execution_actions
from telegram_kol_research import execution_bindings
from telegram_kol_research import recovery_live_submit
from telegram_kol_research import strategy_management_composite_executor
from telegram_kol_research import strategy_management_executor


def _source_mentions_position_authority_lock(func) -> bool:
    # Matches both the direct `with position_authority_lock():` form and the
    # `@serialized_position_authority_mutation` decorator, which share this
    # prefix but diverge after it ("_lock" vs "_mutation").
    return "position_authority" in inspect.getsource(func)


# Leaf functions verified to hold position_authority_lock (directly or via
# @serialized_position_authority_mutation) across their exchange-mutating work.
COVERED_LEAVES = [
    deepcoin_execution_actions.cancel_revision_entry_leg,
    strategy_management_executor.execute_management_batch,
    execution_bindings.reconcile_deepcoin_execution_bindings,
]

# Leaf functions verified NOT to hold position_authority_lock, reachable from
# auto_trade_executor. Each entry is the concrete reason per-chat sharding is
# unsafe today.
KNOWN_UNCOVERED_LEAVES = [
    (
        recovery_live_submit._submit_recovery_signal_direct,
        "entry-signal order submission and strategy-revision-replacement "
        "submission are serialized only by _source_execution_lock "
        "(source_message_deletion.py), a distinct RLock, not "
        "position_authority_lock",
    ),
    (
        strategy_management_composite_executor.execute_composite_management_batch,
        "composite management batch close/SLTP writes go through "
        "position_mutation_gateway.py, which provides idempotency via a DB "
        "state machine but no mutual exclusion of its own, and this module "
        "never imports position_authority_lock",
    ),
]


@pytest.mark.architecture
@pytest.mark.parametrize(
    "func", COVERED_LEAVES, ids=[f.__qualname__ for f in COVERED_LEAVES]
)
def test_known_covered_exchange_mutation_leaves_stay_covered(func):
    assert _source_mentions_position_authority_lock(func), (
        f"{func.__qualname__} no longer references position_authority_lock. "
        "If this coverage was intentionally removed, the Phase 2 boundary "
        "finding in docs/runtime-serialization-remediation-status.md is now "
        "wrong in the other direction and must be corrected."
    )


@pytest.mark.architecture
@pytest.mark.parametrize(
    "func",
    [func for func, _reason in KNOWN_UNCOVERED_LEAVES],
    ids=[func.__qualname__ for func, _reason in KNOWN_UNCOVERED_LEAVES],
)
def test_known_gap_leaves_remain_uncovered(func):
    """If this starts failing, a Phase 2 prerequisite gap has closed.

    Do not enable message_lock_mode=per_chat on the strength of this test
    flipping alone - update the decision-gate note in
    docs/runtime-serialization-remediation-status.md and re-verify the other
    gap first.
    """

    assert not _source_mentions_position_authority_lock(func), (
        f"{func.__qualname__} now references position_authority_lock. This "
        "may mean the gap is closed, but confirm and update the Phase 2 "
        "status file before treating per-chat sharding as safe."
    )


def test_per_chat_sharding_decision_gate_is_not_yet_met():
    """Phase 2 Task 1's explicit decision gate: coverage is incomplete.

    Per the phase file: "if any exchange mutation path reachable from two
    different chats concurrently is not covered by position_authority_lock,
    do not enable per-chat sharding in this phase... record the gap, and
    stop." KNOWN_UNCOVERED_LEAVES is that record. message_lock_mode must stay
    "global" in production until both entries are resolved.
    """

    assert len(KNOWN_UNCOVERED_LEAVES) > 0, (
        "All previously known gaps are closed. Re-run the full Task 1 trace "
        "(not just these two functions) before enabling per_chat sharding, "
        "then replace this test with real coverage assertions."
    )
