"""A-8: the label split, its alarm, and the ghost-target hand-off.

The inventory these tests encode: 181 auto_trade messages recorded as
``识别失败`` and not one of them a recognition failure. Four different things
were wearing that one label, and only some of them are worth waking a person
for.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from telegram_kol_research.recognition_failure_attribution import (
    APPLY_FAILED,
    ALERTED_REASONS,
    CONTRACT_INVALID,
    NO_ACTIONABLE_INTENT,
    NO_TARGET_NAMED,
    TARGET_NOT_VERIFIABLE,
    classify_unapplied_lifecycle_event,
    reason_code_from_recognition_reason,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# The four outcomes the one label was hiding
# --------------------------------------------------------------------------


def test_a_message_asking_for_nothing_is_not_a_failure():
    """29 of the 181: "继续拿着不变", "带好止盈止损"."""

    verdict = classify_unapplied_lifecycle_event(
        intent="none", target_lifecycle_id=1096, target_verified=False
    )
    assert verdict.reason_code == NO_ACTIONABLE_INTENT


def test_asking_for_nothing_beats_a_ghost_target():
    """Ordering, not an accident.

    raw 15170 and 15316 are both: hold what you have, aimed at a lifecycle
    with no execution binding. Nothing was lost, so nothing should page. If
    the target were asked about first these would alert, and that noise is
    what buried the ten real losses.
    """

    verdict = classify_unapplied_lifecycle_event(
        intent="none",
        target_lifecycle_id=1096,
        target_verified=False,
        target_detail="no_execution_binding",
    )
    assert verdict.reason_code not in ALERTED_REASONS


def test_a_real_instruction_with_no_target_named():
    """55 of the 181, all of them before 2026-07-27."""

    verdict = classify_unapplied_lifecycle_event(
        intent="partial_take_profit", target_lifecycle_id=None, target_verified=None
    )
    assert verdict.reason_code == NO_TARGET_NAMED


def test_a_real_instruction_aimed_at_a_ghost_is_alerted():
    """raw 14500's shape: 将加仓的部分止盈出局, at a lifecycle with no binding."""

    verdict = classify_unapplied_lifecycle_event(
        intent="partial_take_profit",
        target_lifecycle_id=1043,
        target_verified=False,
        target_detail="no_execution_binding",
    )
    assert verdict.reason_code == TARGET_NOT_VERIFIABLE
    assert verdict.reason_code in ALERTED_REASONS


def test_a_verified_target_that_still_did_not_apply_is_the_real_failure():
    """The inventory found zero of these, which is exactly why it needs an alarm."""

    verdict = classify_unapplied_lifecycle_event(
        intent="adjust_stop_loss", target_lifecycle_id=1097, target_verified=True
    )
    assert verdict.reason_code == APPLY_FAILED
    assert verdict.reason_code in ALERTED_REASONS


def test_the_benign_two_are_never_alerted():
    assert NO_ACTIONABLE_INTENT not in ALERTED_REASONS
    assert NO_TARGET_NAMED not in ALERTED_REASONS


# --------------------------------------------------------------------------
# Carrying the verdict on the recognition row
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("authoritative_lifecycle_not_applied:no_actionable_intent", NO_ACTIONABLE_INTENT),
        ("authoritative_lifecycle_not_applied:target_not_verifiable", TARGET_NOT_VERIFIABLE),
        (
            "authoritative_instruction_contract_invalid:instruction_0_strategy_incomplete",
            CONTRACT_INVALID,
        ),
        ("MiMo lifecycle event could not be applied safely", None),
        (None, None),
        ("", None),
    ],
)
def test_the_verdict_reads_back_off_the_recognition_row(reason, expected):
    assert reason_code_from_recognition_reason(reason) == expected


def test_rows_written_before_a8_keep_their_old_meaning():
    """A historical row is not retro-labelled; it maps to the old reason.

    Retro-labelling would put a confident new verdict on evidence that never
    supported it -- the old rows do not record which of the four they were.
    """

    from telegram_kol_research.authoritative_recognition import (
        _lifecycle_not_applied_reason,
    )

    class Row:
        status = "识别失败"
        reason = "MiMo lifecycle event could not be applied safely"

    assert _lifecycle_not_applied_reason(Row()) == "mimo_authoritative_not_safely_applied"


def test_a_recognition_that_applied_is_left_alone():
    from telegram_kol_research.authoritative_recognition import (
        _lifecycle_not_applied_reason,
    )

    class Row:
        status = "是策略"
        reason = None

    assert _lifecycle_not_applied_reason(Row()) is None


# --------------------------------------------------------------------------
# The alarm: who gets told
# --------------------------------------------------------------------------


def _alert(reason, mode, captured, awaiting=False):
    from telegram_kol_research.authoritative_recognition import (
        _alert_recognition_not_applied,
    )

    class Raw:
        chat_id = -1002337721508

    class Query:
        def filter(self, *args):
            return self

        def first(self):
            return object() if awaiting else None

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, model, ident):
            return Raw()

        def query(self, model):
            return Query()

    return _alert_recognition_not_applied(
        lambda: Session(),
        raw_message_id=15578,
        automation={"status": "skipped", "reason": reason},
        group_trading_mode_provider=lambda chat_id: mode,
        capture=lambda **kwargs: captured.append(kwargs),
    )


def test_a_contract_rejection_in_an_auto_trade_group_alerts():
    captured: list[dict] = []
    assert _alert(CONTRACT_INVALID, "auto_trade", captured) == CONTRACT_INVALID
    assert captured[0]["reason_code"] == CONTRACT_INVALID
    assert captured[0]["chat_id"] == -1002337721508


def test_the_same_rejection_in_a_notify_only_group_is_recorded_but_silent():
    """A group that executes nothing loses nothing when an instruction drops."""

    captured: list[dict] = []
    assert _alert(CONTRACT_INVALID, "notify_only", captured) is None
    assert captured == []


def test_benign_outcomes_do_not_alert_even_in_auto_trade_groups():
    for reason in (NO_ACTIONABLE_INTENT, NO_TARGET_NAMED):
        captured: list[dict] = []
        assert _alert(reason, "auto_trade", captured) is None
        assert captured == []


def test_an_unreadable_group_mode_errs_towards_telling_somebody():
    def explode(chat_id):
        raise RuntimeError("groups.yaml unreadable")

    from telegram_kol_research.authoritative_recognition import (
        _alert_recognition_not_applied,
    )

    class Raw:
        chat_id = -1002337721508

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, model, ident):
            return Raw()

    captured: list[dict] = []
    result = _alert_recognition_not_applied(
        lambda: Session(),
        raw_message_id=15578,
        automation={"status": "skipped", "reason": CONTRACT_INVALID},
        group_trading_mode_provider=explode,
        capture=lambda **kwargs: captured.append(kwargs),
    )
    assert result == CONTRACT_INVALID
    assert len(captured) == 1


def test_the_incident_type_is_always_notified():
    from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES

    assert "authoritative_recognition_failed" in ALWAYS_NOTIFIED_INCIDENT_TYPES


def test_one_message_asks_one_question_when_both_gates_fire():
    """A-7 parks and notifies during assessment; A-8 sees the same message
    afterwards. Two notifications for one instruction would be worse than one.
    """

    captured: list[dict] = []
    result = _alert(TARGET_NOT_VERIFIABLE, "auto_trade", captured, awaiting=True)
    assert result is None
    assert captured == []
