"""Option A of the 2026-09-15 context-trigger tightening analysis.

`multiple_same_source_candidates` is a structural signal about the *source*
(it only asks whether >= 2 candidate threads are live), not about the message
being judged. These cases pin the gate that keeps it from firing on messages
the first pass already read as neither a strategy nor a lifecycle event, while
leaving the seven other signals exactly as they were.
"""

from telegram_kol_research.authoritative_recognition import (
    requires_context_resolution,
)


NEUTRAL_EVIDENCE = {"conflicts": []}
TWO_CANDIDATES = (
    {"thread_id": 41, "lifecycle_id": 51},
    {"thread_id": 42, "lifecycle_id": 52},
)


def _evaluate(
    *,
    recognition_result,
    lifecycle_event,
    text,
    candidates=TWO_CANDIDATES,
    evidence=NEUTRAL_EVIDENCE,
):
    return requires_context_resolution(
        first_pass_payload={
            "recognition_result": recognition_result,
            "lifecycle_event": lifecycle_event,
        },
        evidence=evidence,
        context_window={"current": {"text": text}, "reply_chain": []},
        candidates=list(candidates),
    )


def test_non_actionable_message_ignores_multiple_candidates():
    required, reasons = _evaluate(
        recognition_result="非策略",
        lifecycle_event={"event_type": "none"},
        text="今天行情有点意思",
    )

    assert (required, reasons) == (False, ())


def test_lifecycle_event_without_target_keeps_multiple_candidates():
    required, reasons = _evaluate(
        recognition_result="非策略",
        lifecycle_event={
            "event_type": "position_update",
            "target_lifecycle_id": None,
        },
        text="往上推一推",
    )

    assert required is True
    assert "management_without_exact_target" in reasons
    assert "multiple_same_source_candidates" in reasons


def test_strategy_message_keeps_multiple_candidates():
    required, reasons = _evaluate(
        recognition_result="是策略",
        lifecycle_event={"event_type": "none"},
        text="btc 市价多，止损 6 万",
    )

    assert required is True
    assert reasons == ("multiple_same_source_candidates",)


def test_wording_signal_still_triggers_without_the_structural_one():
    required, reasons = _evaluate(
        recognition_result="非策略",
        lifecycle_event={"event_type": "none"},
        text="这个持仓先放着",
    )

    assert required is True
    assert "entered_holder_language" in reasons
    assert "multiple_same_source_candidates" not in reasons


def test_apparent_entry_revision_is_unchanged_for_non_strategy_messages():
    required, reasons = _evaluate(
        recognition_result="非策略",
        lifecycle_event={"event_type": "none"},
        text="修改一下之前那个",
        candidates=(
            {
                "thread_id": 41,
                "lifecycle_id": 51,
                "reasons": ("overlapping_entry",),
            },
            {
                "thread_id": 42,
                "lifecycle_id": 52,
                "reasons": ("overlapping_entry",),
            },
        ),
    )

    # `apparent_entry_may_be_revision` has always required
    # `recognition_result == "是策略"`, so it must stay absent here; the gate
    # only removes the structural signal and leaves `revision_language`.
    assert required is True
    assert reasons == ("revision_language",)
