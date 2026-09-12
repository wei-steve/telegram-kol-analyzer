"""A-16e: keep what the downgrade overwrites.

A-16c's replay of the three messages that went silent on 2026-09-11 could
assert the routing decision and not the management action, because the
first-pass ``lifecycle_event`` is not persisted anywhere: recognition_decisions
stores the payload after the rewrite, and the run and attempt tables keep only
fingerprints. "Which action did the model read" was already gone by the time
anybody asked.
"""

from __future__ import annotations


def _decision(**overrides):
    from telegram_kol_research.context_resolution import ContextResolutionDecision

    payload = {
        "decision": "unresolved",
        "target_thread_ids": (),
        "management_action": None,
        "confidence": 0.6,
        "supporting_message_ids": (),
        "opposing_message_ids": (),
        "conflict_types": ("target_ambiguous",),
        "risk_reducing_fanout_allowed": False,
        "reanalysis_triggers": (),
        "reason": "无法唯一确定目标",
    }
    payload.update(overrides)
    return ContextResolutionDecision(**payload)


def _mimo(payload):
    from telegram_kol_research.authoritative_recognition import (
        MimoAuthoritativeResult,
    )

    return MimoAuthoritativeResult(
        raw_message_id=16078,
        payload=payload,
        input_kind="text",
        model="mimo-v2.5",
        status="管理",
        prompt_versions={},
    )


FIRST_PASS = {
    "recognition_result": "管理",
    "lifecycle_event": {"event_type": "position_update", "confidence": 0.9},
    "strategy": {"symbol": "BTC", "side": "long"},
    "confidence": 0.9,
    "evidence": {"text": {"fields": {}}},
}


def _resolve(decision):
    from telegram_kol_research.authoritative_recognition import (
        _resolved_mimo_result,
    )

    return _resolved_mimo_result(
        _mimo(dict(FIRST_PASS)),
        decision,
        (),
        current_message_id=10442,
        exact_risk_reduction_authorized=False,
    )


def test_the_downgrade_keeps_what_it_overwrites():
    """The four fields it replaces are recoverable afterwards."""

    result = _resolve(_decision())

    assert result.payload["recognition_result"] == "非策略"
    assert result.payload["lifecycle_event"]["event_type"] == "none"
    first_pass = result.payload["_context_resolution"]["first_pass"]
    assert first_pass["recognition_result"] == "管理"
    assert first_pass["lifecycle_event"] == {
        "event_type": "position_update", "confidence": 0.9
    }
    assert first_pass["strategy"] == {"symbol": "BTC", "side": "long"}
    assert first_pass["confidence"] == 0.9


def test_the_kept_copy_is_not_an_alias_of_the_live_payload():
    """Mutating the payload afterwards must not rewrite the record.

    Written on the ``new_thread`` path on purpose. On the downgrade path the
    assertion holds either way -- the downgrade *rebinds* ``lifecycle_event``
    to a fresh dict instead of mutating it, so storing a reference would still
    pass, and the first version of this test passed under the mutation that
    removed the ``dict()`` copy. Here the payload still points at the very
    object that was kept, so an alias is observable.
    """

    result = _resolve(_decision(decision="new_thread", confidence=0.9))
    result.payload["lifecycle_event"]["event_type"] = "tampered"

    kept = result.payload["_context_resolution"]["first_pass"]["lifecycle_event"]
    assert kept["event_type"] == "position_update"


def test_a_resolved_decision_also_carries_the_first_pass():
    """Recorded on every resolution, not only on the downgrade.

    A resolution that produced an action is exactly as worth reconstructing
    afterwards as one that produced none, and 2026-09-11 showed that nobody
    asks the question until days later.
    """

    result = _resolve(_decision(decision="new_thread", confidence=0.9))

    first_pass = result.payload["_context_resolution"]["first_pass"]
    assert first_pass["recognition_result"] == "管理"
