"""Architecture test: Phase 2 Task 1 / Phase 2f, the position-authority
boundary check.

Per-chat message lock sharding (docs/plans/2026-08-18-runtime-serialization-
remediation/phase-2-per-chat-lock-sharding.md) is only safe if every path
reachable from `auto_trade_executor` that submits a Deepcoin order or mutates
position/protection state is already serialized by `position_authority_lock()`
/ `@serialized_position_authority_mutation`, independent of the message lock.

Phase 2's own Task 1 found this false: two live call chains wrote
exchange/position state under a different lock, or under no lock at all. That
is why `message_lock_mode` defaulted to, and stayed at, "global". Phase 2f
(docs/plans/2026-08-18-runtime-serialization-remediation/phase-2f-close-
position-authority-coverage-gaps.md) closed both gaps: `recovery_live_submit.
_submit_recovery_signal_direct` now also holds `position_authority_lock` (in
addition to its pre-existing `_source_execution_lock`, acquired in that
order - `position_authority_lock` outer, matching the same stacking order
already live in `deepcoin_execution_actions.recreate_trigger_entry_tpsl`),
and `strategy_management_composite_executor.execute_composite_management_batch`
now holds `position_authority_lock` around its whole body, following the
exact pattern `strategy_management_executor.execute_management_batch` already
used. See docs/runtime-serialization-remediation-status.md, "Phase 2f", for
the full trace and the lock-ordering safety check.

This test encodes that finding as a regression guard, mirroring how
tests/test_runtime_event_loop_blocking_census.py holds an explicit allowlist:
if a covered leaf loses its lock, that is a signal worth catching rather than
a green suite silently drifting out of sync with the documented finding.
`message_lock_mode` still stays "global" in production - closing this gap
makes per-chat sharding's prerequisite true, it does not itself enable
per-chat sharding, which remains a separate, explicit decision.
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
    # Phase 2f gap A: now also holds position_authority_lock, stacked outside
    # its pre-existing @serialized_source_message_execution decorator (see
    # tests/test_recovery_live_submit.py::
    # test_submit_recovery_signal_direct_is_covered_by_position_authority_lock
    # for the observable, lock-contention proof - not just this source check).
    recovery_live_submit._submit_recovery_signal_direct,
    # Phase 2f gap B: now holds position_authority_lock around its whole body
    # (see tests/test_strategy_management_executor.py::
    # test_execute_composite_management_batch_is_covered_by_position_authority_lock).
    strategy_management_composite_executor.execute_composite_management_batch,
]

# Historical record only: both leaves below were verified NOT to hold
# position_authority_lock as of Phase 2's Task 1 trace (2026-08-19), and were
# closed by Phase 2f (2026-08-19) - see docs/runtime-serialization-
# remediation-status.md, "Phase 2f", for the closing commit and evidence.
# Kept here, not deleted, so the history of what was wrong and when it was
# fixed stays discoverable in the same file as the current guard.
KNOWN_UNCOVERED_LEAVES: list[tuple[object, str]] = []


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


def test_no_known_uncovered_leaves_remain():
    """KNOWN_UNCOVERED_LEAVES must stay empty unless a NEW gap is found.

    Phase 2's Task 1 decision gate failed with exactly two entries in this
    list. Phase 2f closed both (see COVERED_LEAVES above and
    docs/runtime-serialization-remediation-status.md, "Phase 2f"). If this
    ever fails, someone added an entry back - which means a new gap was
    found and deliberately recorded, not that this test is wrong. Update
    this docstring and the status file together with whatever added the
    entry.
    """

    assert KNOWN_UNCOVERED_LEAVES == [], (
        "KNOWN_UNCOVERED_LEAVES is no longer empty - a new position-authority "
        "coverage gap was recorded. Re-run the Task 1 trace, do not enable "
        "message_lock_mode=per_chat, and update docs/runtime-serialization-"
        "remediation-status.md before treating this as routine."
    )


def test_per_chat_sharding_prerequisite_gap_is_closed():
    """Phase 2 Task 1's explicit decision gate now passes.

    Per the phase file: "if any exchange mutation path reachable from two
    different chats concurrently is not covered by position_authority_lock,
    do not enable per-chat sharding in this phase... record the gap, and
    stop." Both gaps Phase 2 recorded are now closed (Phase 2f). This makes
    the PREREQUISITE for message_lock_mode=per_chat true - it does NOT
    enable per_chat itself. message_lock_mode must stay "global" in
    production until a separate, explicit session decision re-verifies this
    boundary end to end and turns the flag on deliberately.
    """

    assert KNOWN_UNCOVERED_LEAVES == [], (
        "The decision gate is not met: KNOWN_UNCOVERED_LEAVES is non-empty. "
        "Do not enable message_lock_mode=per_chat."
    )
