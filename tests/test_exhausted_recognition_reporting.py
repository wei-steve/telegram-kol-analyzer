"""2026-09-28: the exhausted rewrite must not take a message off any report.

The worker rewrites ``mimo_authoritative_failed`` to
``mimo_authoritative_failed_exhausted`` when a job spends its last retry. Every
consumer that counted the first value as "nothing was read" has to count the
second one too, or the rewrite would quietly clear an on-call case, drop the
message from the Web card's "未安全接纳" verdict, and hide it from the outage
replay.
"""

from __future__ import annotations

from types import SimpleNamespace

from telegram_kol_research import recognition_failure_attribution as attribution
from telegram_kol_research.oncall_alerts import reason_label
from telegram_kol_research.oncall_detector import lossy_recognition_reason
from telegram_kol_research.web_queries import _serialize_system_acceptance


EXHAUSTED = attribution.MIMO_AUTHORITATIVE_FAILED_EXHAUSTED


def test_d3_still_reads_the_rewritten_row_as_lost():
    # Whatever the agreement status says, the reason alone keeps the case.
    assert lossy_recognition_reason("agree", EXHAUSTED) == EXHAUSTED
    assert lossy_recognition_reason("authoritative_failed", EXHAUSTED) == EXHAUSTED


def test_the_on_call_alert_names_it_in_chinese():
    label = reason_label(EXHAUSTED)
    assert "重试已耗尽" in label
    assert "未收录原因" not in label


def test_the_web_card_still_says_not_safely_accepted():
    decision = SimpleNamespace(
        automation_status="skipped",
        automation_reason=EXHAUSTED,
        authoritative_status="completed",
    )

    acceptance = _serialize_system_acceptance(
        recognition=None,
        decision=decision,
        candidates=[],
        mimo_analysis=None,
    )

    assert acceptance["status"] == "failed"
    assert acceptance["status_label"] == "系统未安全接纳"


def test_the_outage_replay_and_the_alert_gate_both_see_it():
    # ``provider_outage_replay.select_replay_candidates`` selects by
    # ``AUTHORITY_NOT_PRODUCED_REASONS``; ``_alert_recognition_not_applied``
    # gates on ``ALERTED_REASONS``.
    assert EXHAUSTED in attribution.AUTHORITY_NOT_PRODUCED_REASONS
    assert EXHAUSTED in attribution.ALERTED_REASONS
