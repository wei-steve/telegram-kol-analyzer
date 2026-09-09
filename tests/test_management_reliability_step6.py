"""A-6: a refusal is not an unknown, and getting out is not a partial.

Two production facts drive this file. Twenty-one authoritative attempts sat
frozen as ``uncertain`` with ``evidence_refs_json`` NULL -- twelve because every
item said ``kol_or_group_auto_trade_disabled / skipped``, four because every
item said ``blocked`` with a pre-submit refusal, five because the work had been
handed to an asynchronous batch and had not finished yet. Not one of them had a
single exchange write behind it. Separately, an unresolved partial-close batch
blocked every later management instruction for its lifecycle, ``full_exit``
included.
"""

from telegram_kol_research.execution_boundary import (
    ExecutionBoundaryTracker,
    build_execution_boundary_outcome,
)


def _outcome(result, *, writes=()):
    tracker = ExecutionBoundaryTracker()
    for method, response in writes:
        ordinal = tracker.begin(method)
        tracker.applied(ordinal, response)
    return build_execution_boundary_outcome(result, tracker)


def _item(item_id, status, payload, kind="management"):
    key = "error" if status in {"failed", "unknown"} else "result"
    return {
        "item_id": item_id,
        "sequence": 0,
        "instruction_kind": kind,
        "status": status,
        key: payload,
        "reason": payload.get("reason"),
    }


# --------------------------------------------------------------------------
# Task 1: deterministic refusal versus unknown outcome
# --------------------------------------------------------------------------


def test_every_item_skipped_is_a_completed_no_op_not_an_unknown():
    """The twelve-attempt family: auto-trade was off for the group."""

    outcome = _outcome(
        {
            "status": "completed",
            "items": [
                _item(
                    1,
                    "succeeded",
                    {"reason": "kol_or_group_auto_trade_disabled", "status": "skipped"},
                )
            ],
        }
    )

    assert (outcome.status, outcome.exchange_effect) == ("completed", "not_started")
    assert outcome.evidence_refs[0]["kind"] == "instruction_item_no_exchange_contact"
    assert outcome.evidence_refs[0]["reason"] == "kol_or_group_auto_trade_disabled"


def test_partial_failed_with_blocked_items_is_failed_safe_and_carries_the_reasons():
    """The four-attempt family: a pre-submit refusal, one reason per item."""

    outcome = _outcome(
        {
            "status": "partial_failed",
            "items": [
                _item(
                    1,
                    "failed",
                    {
                        "batch_id": 160,
                        "execution_mode": "live",
                        "reason": "management_stop_action_conflict",
                        "status": "blocked",
                    },
                )
            ],
        }
    )

    assert (outcome.status, outcome.exchange_effect) == ("failed_safe", "not_started")
    assert outcome.evidence_refs[0]["reason"] == "management_stop_action_conflict"
    assert outcome.evidence_refs[0]["payload_status"] == "blocked"


def test_blocked_with_deterministic_items_is_failed_safe():
    outcome = _outcome(
        {
            "status": "blocked",
            "items": [
                _item(1, "failed", {"reason": "prior_partial_batch_unresolved",
                                    "status": "blocked"})
            ],
        }
    )

    assert outcome.status == "failed_safe"


def test_in_progress_with_unfinished_items_is_handed_off_not_frozen():
    """The five-attempt family: the batch or entry queue owns it now."""

    outcome = _outcome(
        {
            "status": "in_progress",
            "items": [_item(1, "pending", {"status": "pending"}, kind="entry")],
        }
    )

    assert (outcome.status, outcome.exchange_effect) == ("completed", "not_started")
    assert outcome.public_result["reason"] == "handed_off_to_batch"
    assert outcome.public_result["status"] == "completed"
    assert outcome.public_result["handed_off_from_status"] == "in_progress"


def test_a_tracked_write_still_freezes_everything():
    """The rule this step must not touch: any write, and nothing is assumed."""

    outcome = _outcome(
        {
            "status": "partial_failed",
            "items": [
                _item(1, "failed", {"reason": "management_stop_action_conflict",
                                    "status": "blocked"})
            ],
        },
        writes=[("place_order", {"ordId": "1"})],
    )

    assert (outcome.status, outcome.exchange_effect) == (
        "outcome_unknown",
        "outcome_unknown",
    )


def test_one_item_without_a_payload_keeps_the_whole_message_unknown():
    outcome = _outcome(
        {
            "status": "partial_failed",
            "items": [
                _item(1, "failed", {"reason": "management_stop_action_conflict",
                                    "status": "blocked"}),
                {"item_id": 2, "status": "failed", "reason": "something"},
            ],
        }
    )

    assert outcome.status == "outcome_unknown"


def test_an_item_claiming_an_exchange_effect_keeps_the_message_unknown():
    """``submitted`` is not a refusal, whatever the rolled-up word says."""

    outcome = _outcome(
        {
            "status": "completed",
            "items": [_item(1, "submitted", {"status": "submitted"}, kind="entry")],
        }
    )

    assert outcome.status == "outcome_unknown"


def test_in_progress_with_a_finished_item_is_not_treated_as_handed_off():
    outcome = _outcome(
        {
            "status": "in_progress",
            "items": [
                _item(1, "pending", {"status": "pending"}, kind="entry"),
                _item(2, "submitted", {"status": "submitted"}, kind="entry"),
            ],
        }
    )

    assert outcome.status == "outcome_unknown"


def test_a_message_with_no_items_is_unchanged():
    """The single-candidate path has no items; its old classification stands."""

    assert _outcome({"status": "partial_failed"}).status == "outcome_unknown"
    assert _outcome({"status": "blocked"}).status == "completed"
    assert _outcome({"status": "failed"}).status == "failed_safe"


def test_a_visibility_deferred_item_is_a_hand_off_not_a_finished_no_op():
    """The shape B-line phase 6-pre-1 produces when the WS stream has a gap.

    The item is deferred for visibility, so its status stays ``pending`` while
    its payload already reads ``deferred``. The payload alone would look like a
    finished statement that nothing was sent; it is not one -- the
    visibility-retry timer still owns the item and will try again. Judging on
    the item's own status keeps that distinction, and the message comes out as
    a hand-off rather than a completed no-op.
    """

    outcome = _outcome(
        {
            "status": "in_progress",
            "items": [
                {
                    "item_id": 1,
                    "sequence": 0,
                    "instruction_kind": "entry",
                    "status": "pending",
                    "result": {
                        "status": "deferred",
                        "reason": "ws_observation_pending",
                    },
                }
            ],
        }
    )

    assert (outcome.status, outcome.exchange_effect) == ("completed", "not_started")
    assert outcome.public_result["reason"] == "handed_off_to_batch"
    assert outcome.public_result["handed_off_from_status"] == "in_progress"


def test_a_finished_item_whose_payload_deferred_is_still_proof_of_no_contact():
    """A defer that was *not* retried terminalises the item; that is a no-op."""

    outcome = _outcome(
        {
            "status": "partial_failed",
            "items": [
                _item(1, "failed", {"status": "deferred", "reason": "entry_deferred"})
            ],
        }
    )

    assert outcome.status == "failed_safe"
    assert outcome.evidence_refs[0]["payload_status"] == "deferred"


def test_a_submitted_item_can_never_be_part_of_a_no_contact_proof():
    outcome = _outcome(
        {
            "status": "completed",
            "items": [_item(1, "submitted", {"status": "skipped"}, kind="entry")],
        }
    )

    assert outcome.status == "outcome_unknown"
