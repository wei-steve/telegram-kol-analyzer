"""First-pass classification, phase 3 batch 1: trigger criteria, parser fixes, ②.

Plan: ``docs/plans/2026-09-29-first-pass-phase3-implementation-plan.md`` (§1, §2,
§3.1, §4, §7). Every payload below is a literal copied from a production point
query (``recognition_decisions`` / ``context_resolution_attempts.request_summary_json
.mimo_first_pass`` / ``message_evidence_versions.normalized_evidence_json``) and
trimmed to the fields the code under test reads. Contact-footer lines are
stripped from the texts. The ``R<n>`` labels are the plan's §7 replay ids.
"""

from __future__ import annotations

import pytest

from telegram_kol_research.authoritative_recognition import (
    CONTEXT_TRIGGER_ORDER,
    _resolved_mimo_result,
    requires_context_resolution,
)
from telegram_kol_research.context_resolution import ContextResolutionDecision
from telegram_kol_research.context_resolution_shadow import (
    evaluate_context_resolution_shadow,
)
from telegram_kol_research.message_classification import (
    NON_FATAL_VIOLATIONS,
    MessageClassTarget,
    compare_message_classes,
    derive_message_classes,
    fatal_violations,
    message_class_identities,
    parse_message_classes,
)
from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult
from telegram_kol_research.recognition_failure_attribution import (
    classify_unapplied_lifecycle_event,
    execution_downgrade_note,
)
from telegram_kol_research.strategy_thread_candidates import StrategyThreadCandidate

NEUTRAL_EVIDENCE = {"conflicts": []}
_NONE_EVENT = {"event_type": "none", "target_lifecycle_id": None}


def _cand(lifecycle_id, status="entered", *, thread_id=None, reasons=()):
    return {
        "thread_id": thread_id if thread_id is not None else lifecycle_id + 1000,
        "lifecycle_id": lifecycle_id,
        "status": status,
        "reasons": tuple(reasons),
    }


def _trigger(payload, text, candidates, *, evidence=NEUTRAL_EVIDENCE):
    return requires_context_resolution(
        first_pass_payload=payload,
        evidence=evidence,
        context_window={"current": {"text": text}, "reply_chain": []},
        candidates=candidates,
    )


def _mgmt(class_name, resolution, lifecycle_id=None, symbol=None, side=None):
    return {
        "class": class_name,
        "target": {
            "resolution": resolution,
            "lifecycle_id": lifecycle_id,
            "symbol": symbol,
            "side": side,
        },
    }


# ---------------------------------------------------------------------------
# Trigger vocabulary
# ---------------------------------------------------------------------------


def test_trigger_order_drops_the_three_wording_triggers_and_adds_two():
    assert CONTEXT_TRIGGER_ORDER == (
        "management_without_exact_target",
        "exact_target_outside_candidates",
        "exact_target_not_manageable",
        "multiple_same_source_candidates",
        "reply_target_disagreement",
        "text_image_conflict",
        "apparent_entry_may_be_revision",
    )


@pytest.mark.parametrize(
    "text",
    [
        "更新一下 BTC",  # old revision word
        "先取消先观望",  # old cancellation word
        "继续持有，保本，已入场",  # old holder words
    ],
)
def test_wording_alone_no_longer_triggers(text):
    payload = {"recognition_result": "非策略", "lifecycle_event": _NONE_EVENT}
    assert _trigger(payload, text, [_cand(1319)]) == (False, ())


# ---------------------------------------------------------------------------
# R2: old-field (v8) first passes with an exact live target do not trigger
# ---------------------------------------------------------------------------

R2_FIRST_PASSES = {
    # 18893, chat 米娅: entry_confirm -> 1319
    18893: (
        "这笔策略我们入场即浮盈，目前浮盈1000点，继续持有！",
        {
            "recognition_result": "非策略",
            "strategy": {"entry": None, "side": None, "stop_loss": None, "symbol": None},
            "lifecycle_event": {
                "event_type": "entry_confirm",
                "management_action": None,
                "symbol": "BTC",
                "side": "short",
                "target_lifecycle_id": 1319,
            },
        },
        1319,
    ),
    # 18895: position_update -> 1319
    18895: (
        "BTC现价83400附近，止盈50%，剩余仓位止损位下移至84500，做无风险持仓！",
        {
            "recognition_result": "非策略",
            "strategy": {},
            "lifecycle_event": {
                "event_type": "position_update",
                "management_action": "partial_take_profit, move_stop_to_protect",
                "symbol": "BTC",
                "side": "short",
                "target_lifecycle_id": 1319,
            },
        },
        1319,
    ),
    # 18555: position_update -> 1271 (继续持有)
    18555: (
        "比特币空单目前浮盈1200点，继续持有中原计划不变。",
        {
            "recognition_result": "非策略",
            "strategy": {},
            "lifecycle_event": {
                "event_type": "position_update",
                "management_action": "continue_holding_original_plan",
                "symbol": "BTC",
                "side": "short",
                "target_lifecycle_id": 1271,
            },
        },
        1271,
    ),
    # 18375: position_update -> 1272 (保本)
    18375: (
        "大镖客·Andy\n插到了85400，保本是没问题的了",
        {
            "recognition_result": "非策略",
            "strategy": {},
            "lifecycle_event": {
                "event_type": "position_update",
                "management_action": "move_stop_to_protect",
                "symbol": "BTC",
                "side": "short",
                "target_lifecycle_id": 1272,
            },
        },
        1272,
    ),
}


@pytest.mark.parametrize("raw_id", sorted(R2_FIRST_PASSES))
def test_r2_exact_target_in_a_single_live_candidate_does_not_trigger(raw_id):
    text, payload, lifecycle_id = R2_FIRST_PASSES[raw_id]
    # These are v8 payloads: no ``message_classes``, so the old-field fallback
    # decides, and it sees a named target.
    assert "message_classes" not in payload
    assert _trigger(payload, text, [_cand(lifecycle_id, "entered")]) == (False, ())


@pytest.mark.parametrize("raw_id", sorted(R2_FIRST_PASSES))
def test_r2_two_candidates_still_fire_multiple_same_source(raw_id):
    """Unchanged behaviour, asserted on purpose: the structural signal stays."""

    text, payload, lifecycle_id = R2_FIRST_PASSES[raw_id]
    required, reasons = _trigger(
        payload,
        text,
        [_cand(lifecycle_id, "entered"), _cand(lifecycle_id + 500, "entered")],
    )
    assert required is True
    assert reasons == ("multiple_same_source_candidates",)


# ---------------------------------------------------------------------------
# R3: 19481 and apparent_entry_may_be_revision
# ---------------------------------------------------------------------------

R3_TEXT = "陈哥合约交易策略\nBTC，83000-83300附近，做多\n止损预计：81500\n止盈预计：85800-87000"
R3_FIRST_PASS = {
    "recognition_result": "是策略",
    "message_classes": [{"class": "新策略", "target": None}],
    "strategy": {
        "entry": "83000-83300附近",
        "order_type": "limit",
        "side": "long",
        "stop_loss": "81500",
        "symbol": "BTC",
        "take_profit": "85800-87000",
    },
    "lifecycle_event": _NONE_EVENT,
}


def test_r3_expired_overlapping_candidate_does_not_trigger_revision():
    assert _trigger(
        R3_FIRST_PASS, R3_TEXT, [_cand(1327, "expired", reasons=("overlapping_entry",))]
    ) == (False, ())


def test_r3_pending_entry_overlapping_candidate_triggers_revision():
    assert _trigger(
        R3_FIRST_PASS,
        R3_TEXT,
        [_cand(1327, "pending_entry", reasons=("overlapping_entry",))],
    ) == (True, ("apparent_entry_may_be_revision",))


def test_r3_revision_word_with_a_pending_candidate_triggers_revision():
    required, reasons = _trigger(
        R3_FIRST_PASS,
        R3_TEXT + "\n更新",
        [_cand(1327, "pending_entry")],
    )
    assert (required, reasons) == (True, ("apparent_entry_may_be_revision",))


def test_revision_word_is_a_local_condition_of_the_revision_trigger():
    """Kept as a private word list: it still fires there, on an *expired-free*
    candidate set, exactly as ``"revision_language" in reasons`` used to."""

    required, reasons = _trigger(
        R3_FIRST_PASS, R3_TEXT + "\n修改", [_cand(1327, "expired")]
    )
    assert (required, reasons) == (True, ("apparent_entry_may_be_revision",))


# ---------------------------------------------------------------------------
# R7 / F1: duplicate detection keys on symbol/side for unknown
# ---------------------------------------------------------------------------

R7_19309 = {
    "recognition_result": "非策略",
    "message_classes": [
        _mgmt("仓位管理", "unknown", None, "TST", "long"),
        _mgmt("仓位管理", "unknown", None, "PUMP", "long"),
    ],
    "strategy": {"entry": None, "side": None, "stop_loss": None, "symbol": None},
}
R7_19780 = {
    "recognition_result": "非策略",
    "message_classes": [
        _mgmt("仓位管理", "unknown", None, symbol, "long")
        for symbol in ("BTC", "ETH", "SOL", "ZEC")
    ],
    "strategy": {},
}


@pytest.mark.parametrize("payload", [R7_19309, R7_19780], ids=["19309", "19780"])
def test_r7_different_symbols_are_not_duplicates(payload):
    parsed = parse_message_classes(payload)
    assert parsed.violations == ()
    assert not parsed.fatal
    assert len(parsed.elements) == len(payload["message_classes"])


def test_r7_same_symbol_unknown_twice_is_flagged_but_non_fatal_and_deduped():
    payload = {
        "recognition_result": "非策略",
        "message_classes": [
            _mgmt("仓位管理", "unknown", None, "btc", "LONG"),
            _mgmt("仓位管理", "unknown", None, "BTC", "long"),
            _mgmt("仓位管理", "unknown", None, "ETH", "long"),
        ],
        "strategy": {},
    }
    parsed = parse_message_classes(payload)
    assert parsed.violations == ("duplicate_class_target",)
    assert fatal_violations(parsed) == ()
    assert not parsed.fatal
    assert [e.target.symbol for e in parsed.elements] == ["btc", "ETH"]
    assert len(parsed.to_payload()) == 2


def test_two_symbol_less_unknowns_are_duplicates_and_exact_uses_the_id_only():
    unknowns = parse_message_classes(
        {"message_classes": [_mgmt("策略管理", "unknown"), _mgmt("策略管理", "unknown")]}
    )
    assert unknowns.violations == ("duplicate_class_target",)
    exact_same = parse_message_classes(
        {
            "message_classes": [
                _mgmt("仓位管理", "exact", 5, "BTC", "long"),
                _mgmt("仓位管理", "exact", 5, "ETH", "short"),
            ]
        },
    )
    assert exact_same.violations == ("duplicate_class_target",)


def test_comparison_identity_still_ignores_symbol_and_side():
    a = [_mgmt("仓位管理", "unknown", None, "TST", "long")]
    b = [_mgmt("仓位管理", "unknown", None, "PUMP", "short")]
    assert compare_message_classes(a, b)["agrees"] is True
    assert message_class_identities(a) == message_class_identities(b)
    assert MessageClassTarget("unknown", None, "A", "long").identity() == (
        MessageClassTarget("unknown", None, "B", "short").identity()
    )


def test_class_order_is_repaired_and_non_fatal():
    payload = {
        "recognition_result": "是策略",
        "message_classes": [
            {"class": "新策略", "target": None},
            _mgmt("策略管理", "unknown"),
        ],
        "strategy": {"symbol": "BTC", "side": "long", "entry": "1", "stop_loss": "0.5"},
    }
    parsed = parse_message_classes(payload)
    assert parsed.violations == ("class_order_violation",)
    assert not parsed.fatal
    assert [e.message_class for e in parsed.elements] == ["策略管理", "新策略"]


def test_non_fatal_set_is_exactly_the_documented_one():
    assert NON_FATAL_VIOLATIONS == {
        "duplicate_class_target",
        "class_order_violation",
        "strategy_not_allowed",
        "lifecycle_id_outside_candidate_set",
    }
    parsed = parse_message_classes({"message_classes": []})
    assert parsed.fatal and fatal_violations(parsed) == ("classes_empty",)


def test_strategy_not_allowed_is_non_fatal():
    """A 是策略 with no 新策略 element (only take profits, no stop loss) is the
    deliberate 'two criteria' shape (design §11 R10)."""

    parsed = parse_message_classes(
        {
            "recognition_result": "是策略",
            "message_classes": [{"class": "闲话", "target": None}],
            "strategy": {"symbol": "BTC", "take_profit": "90000"},
        }
    )
    assert parsed.violations == ("strategy_not_allowed",)
    assert not parsed.fatal


# ---------------------------------------------------------------------------
# R8 / F2: violations are judged on the first pass, not on the context rewrite
# ---------------------------------------------------------------------------

R8_19351_FIRST_PASS = {
    "recognition_result": "是策略",
    "message_classes": [_mgmt("策略管理", "unknown"), {"class": "新策略", "target": None}],
    "strategy": {
        "entry": "86888/88000",
        "order_type": "limit",
        "side": "short",
        "stop_loss": "89000",
        "symbol": "BTC",
        "take_profit": "85288/84388/83188",
    },
    "lifecycle_event": {"event_type": "cancel_entry", "target_lifecycle_id": None},
}


@pytest.mark.parametrize(
    "first_pass", [R8_19351_FIRST_PASS, R3_FIRST_PASS], ids=["19351", "19481"]
)
def test_r8_first_pass_evidence_has_no_strategy_required(first_pass):
    parsed = parse_message_classes(first_pass)
    assert "strategy_required" not in parsed.violations
    assert parsed.violations == ()


def test_r8_reparsing_the_context_rewritten_payload_is_the_bug_readers_must_avoid():
    """Documents *why* readers use the stored first-pass violations: the final
    payload has ``strategy: {}`` and would report ``strategy_required``."""

    final = dict(R3_FIRST_PASS, strategy={})
    assert "strategy_required" in parse_message_classes(final).violations


# ---------------------------------------------------------------------------
# R9 / F3: derive reads ``lifecycle_id`` inside ``targets[]``
# ---------------------------------------------------------------------------


def test_r9_19741_derivation_reads_targets_lifecycle_id():
    payload = {
        "recognition_result": "非策略",
        "lifecycle_event": {
            "event_type": "position_update",
            "management_action": "move_stop_to_protect",
            "target_lifecycle_id": None,
            "targets": [
                {"lifecycle_id": 1361, "side": "short", "symbol": "BTC"},
                {"lifecycle_id": 1365, "side": "short", "symbol": "BTC"},
            ],
        },
    }
    derived = derive_message_classes(payload)
    assert message_class_identities(derived) == (
        ("仓位管理", "exact", 1361),
        ("仓位管理", "exact", 1365),
    )
    explicit = [
        _mgmt("策略管理", "exact", 1365, "BTC", "short"),
        _mgmt("仓位管理", "exact", 1361, "BTC", "short"),
    ]
    # The explicit list classifies 1365 as 策略管理 while derivation can only say
    # 仓位管理 for both; the ids agree, which is what F3 fixes.
    assert {i[2] for i in message_class_identities(explicit)} == {1361, 1365}
    assert all(i[1] == "exact" for i in message_class_identities(derived))


def test_derive_still_reads_target_lifecycle_id():
    derived = derive_message_classes(
        {
            "recognition_result": "非策略",
            "lifecycle_event": {"event_type": "cancel_entry", "target_lifecycle_id": 9},
        }
    )
    assert message_class_identities(derived) == (("策略管理", "exact", 9),)


# ---------------------------------------------------------------------------
# R10: 18970, an all-null target on 闲话
# ---------------------------------------------------------------------------


def test_r10_18970_all_null_target_on_small_talk_is_normalised():
    payload = {
        "recognition_result": "非策略",
        "message_classes": [
            {
                "class": "闲话",
                "target": {
                    "lifecycle_id": None,
                    "resolution": None,
                    "side": None,
                    "symbol": None,
                },
            }
        ],
        "strategy": {"entry": None, "side": None, "stop_loss": None, "symbol": None},
    }
    parsed = parse_message_classes(payload)
    assert parsed.violations == ()
    assert parsed.valid
    assert parsed.to_payload() == [{"class": "闲话", "target": None}]


def test_a_non_null_target_on_small_talk_is_still_a_violation():
    parsed = parse_message_classes(
        {"message_classes": [_mgmt("闲话", "unknown", None, "BTC")]}
    )
    assert "target_not_allowed" in parsed.violations
    assert parsed.fatal


def test_all_null_target_on_a_management_class_is_still_fatal():
    parsed = parse_message_classes(
        {"message_classes": [_mgmt("策略管理", None)]}
    )
    assert "resolution_missing" in parsed.violations
    assert parsed.fatal


# ---------------------------------------------------------------------------
# R11 / R12: the two deterministic triggers
# ---------------------------------------------------------------------------

R11_19308 = {
    "recognition_result": "非策略",
    "message_classes": [
        _mgmt("仓位管理", "exact", 1207, "BTC", "long"),
        _mgmt("策略管理", "exact", 909, "ETH", "long"),
    ],
    "strategy": {},
    "lifecycle_event": {
        "event_type": "position_update",
        "management_action": "partial_take_profit; continue_hold",
        "target_lifecycle_id": 1207,
    },
}
R11_TEXT = "1200点，做第一止盈吧，剩下看851\n其他周一再议。\nETH 止损依旧是2660。"


def test_r11_exact_target_outside_the_candidate_set_triggers():
    required, reasons = _trigger(R11_19308, R11_TEXT, [_cand(1207, "entered")])
    assert required is True
    assert reasons == ("exact_target_outside_candidates",)


def test_r11_both_targets_inside_the_candidates_does_not_fire_it():
    required, reasons = _trigger(
        R11_19308, R11_TEXT, [_cand(1207, "entered"), _cand(909, "pending_entry")]
    )
    # 909 is only pending_entry (manageable for 策略管理); two candidates fire
    # the unchanged structural signal, which is not what is asserted here.
    assert "exact_target_outside_candidates" not in reasons
    assert "exact_target_not_manageable" not in reasons


def _exact_payload(class_name, lifecycle_id):
    return {
        "recognition_result": "非策略",
        "message_classes": [_mgmt(class_name, "exact", lifecycle_id, "BTC", "long")],
        "strategy": {},
        "lifecycle_event": {"event_type": "position_update", "target_lifecycle_id": lifecycle_id},
    }


def test_r12_exact_target_in_a_terminal_status_is_not_manageable():
    # 18978 -> 1314 (expired)
    required, reasons = _trigger(
        _exact_payload("仓位管理", 1314), "BTC多单剩余仓位也全部止盈出局", [_cand(1314, "expired")]
    )
    assert (required, reasons) == (True, ("exact_target_not_manageable",))


@pytest.mark.parametrize("status", ["exited", "cancelled", "invalidated"])
def test_r12_every_terminal_status_is_not_manageable(status):
    required, reasons = _trigger(
        _exact_payload("策略管理", 1346), "触发成本价附近直接出局", [_cand(1346, status)]
    )
    assert reasons == ("exact_target_not_manageable",)


def test_r12_position_management_on_pending_entry_is_not_manageable():
    required, reasons = _trigger(
        _exact_payload("仓位管理", 1346), "推保护价：83500", [_cand(1346, "pending_entry")]
    )
    assert (required, reasons) == (True, ("exact_target_not_manageable",))


def test_r12_strategy_management_on_pending_entry_does_not_fire():
    assert _trigger(
        _exact_payload("策略管理", 1346), "先取消", [_cand(1346, "pending_entry")]
    ) == (False, ())


def test_forthcoming_never_triggers():
    payload = {
        "recognition_result": "是策略",
        "message_classes": [
            _mgmt("策略管理", "forthcoming", None, "BTC", "long"),
            {"class": "新策略", "target": None},
        ],
        "entry_context": {"kind": "x"},
        "strategy": {"symbol": "BTC", "side": "long", "entry": "1", "stop_loss": "0.5"},
        "lifecycle_event": _NONE_EVENT,
    }
    assert _trigger(payload, "撤掉旧的，新的马上发", [_cand(1, "entered")]) == (False, ())


# ---------------------------------------------------------------------------
# management_without_exact_target: the rewritten criterion
# ---------------------------------------------------------------------------

R6_19692 = {
    "recognition_result": "非策略",
    "message_classes": [_mgmt("策略管理", "unknown", None, "JTO")],
    "strategy": {},
    "lifecycle_event": {
        "event_type": "none",
        "management_action": "adjust_stop_loss",
        "stop_loss": "0.51",
        "symbol": "JTO",
        "target_lifecycle_id": None,
    },
}


def test_unknown_management_element_triggers_even_with_old_event_none():
    required, reasons = _trigger(R6_19692, "JTO设个止损0.51", [])
    assert (required, reasons) == (True, ("management_without_exact_target",))


def test_old_field_rule_still_applies_next_to_a_usable_classification():
    payload = _exact_payload("仓位管理", 1207)
    payload["lifecycle_event"]["target_lifecycle_id"] = None
    required, reasons = _trigger(payload, "x", [_cand(1207, "entered")])
    assert reasons == ("management_without_exact_target",)


# R16: no message_classes -> old-field logic only, zero violations
def test_r16_payload_without_message_classes_uses_old_fields_only():
    payload = {
        "recognition_result": "非策略",
        "lifecycle_event": {"event_type": "position_update", "target_lifecycle_id": None},
    }
    parsed = parse_message_classes(payload)
    assert parsed.present is False
    assert parsed.violations == ()
    assert not parsed.fatal
    assert _trigger(payload, "x", [_cand(1)]) == (True, ("management_without_exact_target",))
    named = {
        "recognition_result": "非策略",
        "lifecycle_event": {"event_type": "position_update", "target_lifecycle_id": 1},
    }
    assert _trigger(named, "x", [_cand(1, "expired")]) == (False, ())


def test_fatal_violation_falls_back_to_old_fields_only():
    payload = {
        "recognition_result": "非策略",
        "message_classes": [_mgmt("仓位管理", "exact", None)],  # lifecycle_id_required
        "lifecycle_event": {"event_type": "none"},
    }
    assert parse_message_classes(payload).fatal
    # The unknown-looking element is ignored while the list is fatal.
    assert _trigger(payload, "x", [_cand(1, "expired")]) == (False, ())


def test_outside_and_not_manageable_are_ignored_when_the_list_is_fatal():
    payload = _exact_payload("仓位管理", 1314)
    payload["message_classes"].append({"class": "闲话", "target": None})  # not alone
    assert parse_message_classes(payload).fatal
    assert _trigger(payload, "x", [_cand(1314, "expired")]) == (False, ())


# ---------------------------------------------------------------------------
# context_resolution_shadow keeps working with the new trigger names
# ---------------------------------------------------------------------------


def test_shadow_handles_the_new_trigger_names_and_never_sees_the_deleted_ones():
    request = {
        "current_message": {"text": "普通评论"},
        "mimo_first_pass": {
            "recognition_result": "非策略",
            "lifecycle_event": {"event_type": "none"},
            "input_reading": {"observed_text": ""},
        },
        "saved_evidence": {"images": []},
    }
    result = evaluate_context_resolution_shadow(
        request_payload=request,
        authoritative_triggers=(
            "exact_target_not_manageable",
            "multiple_same_source_candidates",
        ),
        authoritative_would_trigger=True,
    )
    assert result.would_trigger is True
    assert result.conditions == ("authoritative:exact_target_not_manageable",)
    assert result.agrees_with_authoritative is True
    for deleted in ("revision_language", "cancellation_language", "entered_holder_language"):
        assert deleted not in CONTEXT_TRIGGER_ORDER
        assert not any(deleted in c for c in result.conditions)


# ---------------------------------------------------------------------------
# ②: first-pass classification survives a context downgrade
# ---------------------------------------------------------------------------

CLASSES_POSITION_UNKNOWN = [_mgmt("仓位管理", "unknown", None, "BTC", "long")]


def _candidate(status="entered"):
    return StrategyThreadCandidate(
        thread_id=696,
        lifecycle_id=1327,
        root_message_id=10758,
        symbol="BTC",
        side="long",
        status=status,
        score=60,
        reasons=("same_chat",),
        lifecycle_summary={"id": 1327},
        binding_summary=None,
        verified_leg_summaries=(),
        risk_state="no_current_risk",
        live_verified_pos_ids=(),
        pending_entry_leg_ids=(),
        uncertain_entry_leg_ids=(),
    )


def _mimo(*, recognition_result="非策略", classes=CLASSES_POSITION_UNKNOWN, event=None):
    payload = {
        "recognition_result": recognition_result,
        "strategy": {},
        "lifecycle_event": event or {"event_type": "position_update", "target_lifecycle_id": None},
        "confidence": 0.9,
        "evidence": {"images": []},
    }
    if classes is not None:
        payload["message_classes"] = classes
    return MimoAuthoritativeResult(
        raw_message_id=1,
        payload=payload,
        input_kind="text",
        model="m",
        status=recognition_result,
    )


def _decision(decision, *, confidence=0.9, management_action=None, targets=(696,)):
    return ContextResolutionDecision(
        decision=decision,
        target_thread_ids=tuple(targets),
        management_action=management_action,
        confidence=confidence,
        supporting_message_ids=(1,),
        opposing_message_ids=(),
        conflict_types=(),
        risk_reducing_fanout_allowed=False,
        reanalysis_triggers=(),
        reason="r",
    )


def _resolve(mimo, decision, candidate=None):
    return _resolved_mimo_result(
        mimo,
        decision,
        (candidate or _candidate(),),
        current_message_id=1,
        exact_risk_reduction_authorized=False,
    )


@pytest.mark.parametrize(
    ("decision", "expected_reason", "targets"),
    [
        (_decision("hold", targets=()), "hold", ()),
        (_decision("unresolved", targets=()), "unresolved", ()),
        (_decision("manage_thread", confidence=0.3), "low_confidence", (696,)),
        (_decision("revise_thread"), "revise_planner", (696,)),
        (_decision("manage_thread", management_action="exit_partial"), "retargeted", (696,)),
        (_decision("cancel_thread", management_action="cancel_pending_entry"), "retargeted", (696,)),
        (_decision("exit_thread", management_action="exit_full"), "retargeted", (696,)),
    ],
)
def test_second_pass_downgrade_keeps_classes_and_records_the_reason(
    decision, expected_reason, targets
):
    mimo = _mimo()
    result = _resolve(mimo, decision)

    context = result.payload["_context_resolution"]
    assert result.payload["message_classes"] == CLASSES_POSITION_UNKNOWN
    assert context["first_pass"]["message_classes"] == CLASSES_POSITION_UNKNOWN
    assert context["execution_downgrade"] == {
        "from": ["仓位管理"],
        "reason": expected_reason,
    }
    # The four legacy first-pass keys are untouched.
    assert set(context["first_pass"]) >= {
        "recognition_result", "lifecycle_event", "strategy", "confidence",
    }


def test_new_thread_records_no_downgrade_but_still_snapshots_classes():
    result = _resolve(_mimo(), _decision("new_thread", targets=()))
    context = result.payload["_context_resolution"]
    assert "execution_downgrade" not in context
    assert context["first_pass"]["message_classes"] == CLASSES_POSITION_UNKNOWN


def test_first_pass_without_classes_records_from_none_and_no_snapshot_key():
    result = _resolve(_mimo(classes=None), _decision("hold", targets=()))
    context = result.payload["_context_resolution"]
    assert "message_classes" not in result.payload
    assert "message_classes" not in context["first_pass"]
    assert context["execution_downgrade"] == {"from": None, "reason": "hold"}


def test_2b_terminal_target_override_is_unchanged_and_not_a_downgrade():
    mimo = _mimo(
        recognition_result="是策略",
        classes=[{"class": "新策略", "target": None}],
        event={"event_type": "none"},
    )
    result = _resolve(mimo, _decision("manage_thread"), _candidate("expired"))
    context = result.payload["_context_resolution"]
    assert context["override_rejected"] == "terminal_target"
    assert "execution_downgrade" not in context
    assert result.payload["recognition_result"] == "是策略"
    assert result.payload["message_classes"] == [{"class": "新策略", "target": None}]
    assert context["first_pass"]["message_classes"] == [{"class": "新策略", "target": None}]


# ---------------------------------------------------------------------------
# recognition_failure_attribution surfaces the downgrade, read-only
# ---------------------------------------------------------------------------


def test_attribution_note_reads_the_downgrade_and_annotates_the_detail_only():
    payload = {
        "_context_resolution": {
            "execution_downgrade": {"from": ["仓位管理"], "reason": "hold"}
        }
    }
    note = execution_downgrade_note(payload)
    assert note == "首轮：仓位管理 → 上下文降级（hold）"
    verdict = classify_unapplied_lifecycle_event(
        intent="none",
        target_lifecycle_id=None,
        target_verified=None,
        execution_downgrade=note,
    )
    assert verdict.reason_code == "no_actionable_intent"
    assert note in verdict.detail
    plain = classify_unapplied_lifecycle_event(
        intent="none", target_lifecycle_id=None, target_verified=None
    )
    assert plain.detail == "none"
    assert execution_downgrade_note({}) is None
    assert execution_downgrade_note(None) is None


# ---------------------------------------------------------------------------
# Web projection (§4 F4, §6.2 E2, §1.3)
# ---------------------------------------------------------------------------

import json  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from jinja2 import Environment, FileSystemLoader  # noqa: E402

import telegram_kol_research  # noqa: E402
from telegram_kol_research.db import create_session_factory  # noqa: E402
from telegram_kol_research.models import RawMessage, RecognitionDecision  # noqa: E402
from telegram_kol_research.web_queries import load_group_messages  # noqa: E402


def _projected_row(tmp_path, payload, *, text="x"):
    session_factory = create_session_factory(tmp_path / "phase3-projection.db")
    with session_factory() as session:
        message = RawMessage(
            chat_id=77,
            message_id=5001,
            posted_at=datetime(2026, 9, 29, 11, 0, tzinfo=UTC),
            text=text,
        )
        session.add(message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=message.id,
                input_kind="text",
                authoritative_model="m",
                authoritative_status=str(payload.get("recognition_result")),
                authoritative_payload_json=json.dumps(payload, ensure_ascii=False),
                agreement_status="pending",
                differences_json="[]",
                comparison_status="completed",
            )
        )
        session.commit()
    return load_group_messages(session_factory, chat_id=77, limit=10)[0]


def _render_card(row):
    environment = Environment(
        loader=FileSystemLoader(
            str(Path(telegram_kol_research.__file__).parent / "templates")
        ),
        autoescape=True,
    )
    return environment.get_template("_messages.html").render(
        messages=[row],
        has_more=False,
        message_page_size=50,
        selected_chat_id=77,
        selected_group=None,
        search_text="",
        sender_name="",
        before_message_id=None,
        live_listener_enabled=False,
        monitor_status={"state": "idle"},
        live_listener_status_reason=None,
        live_listener_delegated=False,
        database_latest_message_at=None,
        database_stale_hours=None,
        refresh_mode_label="仅本地快照",
    )


def _rewritten_19481_payload():
    """19481 as stored: first pass in ``_context_resolution.first_pass``, final
    payload rewritten by context (strategy cleared, non-strategy)."""

    return {
        "recognition_result": "非策略",
        "strategy": {},
        "lifecycle_event": {"event_type": "position_update", "target_lifecycle_id": 696},
        "message_classes": [{"class": "新策略", "target": None}],
        "message_classes_violations": None,
        "_context_resolution": {
            "decision": "manage_thread",
            "first_pass": {
                "recognition_result": "是策略",
                "strategy": dict(R3_FIRST_PASS["strategy"]),
                "lifecycle_event": dict(_NONE_EVENT),
                "confidence": 0.99,
                "message_classes": [{"class": "新策略", "target": None}],
            },
            "execution_downgrade": {"from": ["新策略"], "reason": "retargeted"},
        },
    }


def test_f4_agreement_is_derived_from_the_first_pass_old_fields(tmp_path):
    row = _projected_row(tmp_path, _rewritten_19481_payload())
    # Derived from the *final* fields this would be [仓位管理] and disagree.
    assert [c["class"] for c in row["message_classes_derived"]] == ["新策略"]
    assert row["message_classes_agrees"] is True
    assert row["message_classes_context_rewrote"] is True
    assert row["message_classes_execution_downgrade"] == {
        "from": ["新策略"],
        "reason": "retargeted",
    }


def test_f4_payload_without_a_context_snapshot_is_unchanged(tmp_path):
    payload = {
        "recognition_result": "是策略",
        "strategy": dict(R3_FIRST_PASS["strategy"]),
        "lifecycle_event": dict(_NONE_EVENT),
        "message_classes": [{"class": "新策略", "target": None}],
    }
    row = _projected_row(tmp_path, payload)
    assert row["message_classes_agrees"] is True
    assert row["message_classes_context_rewrote"] is False
    assert row["message_classes_execution_downgrade"] is None


def test_f2_violations_come_from_stored_first_pass_evidence_not_a_reparse(tmp_path):
    """19481 final payload has ``strategy: {}``: re-parsing it would say
    ``strategy_required``. The projection reports only what evidence stored."""

    row = _projected_row(tmp_path, _rewritten_19481_payload())
    assert row["message_classes_violations"] == []
    assert row["message_classes_fatal_violations"] == []

    stored = _rewritten_19481_payload()
    stored["message_classes_violations"] = ["duplicate_class_target", "resolution_missing"]
    row = _projected_row(tmp_path, stored)
    assert row["message_classes_violations"] == [
        "duplicate_class_target",
        "resolution_missing",
    ]
    assert row["message_classes_fatal_violations"] == ["resolution_missing"]


def test_e2_card_primary_label_reads_message_classes_and_can_show_several(tmp_path):
    payload = {
        "recognition_result": "是策略",
        "strategy": dict(R8_19351_FIRST_PASS["strategy"]),
        "lifecycle_event": dict(R8_19351_FIRST_PASS["lifecycle_event"]),
        "message_classes": R8_19351_FIRST_PASS["message_classes"],
    }
    rendered = _render_card(_projected_row(tmp_path, payload))
    assert rendered.count("data-message-primary-class") == 2
    assert "策略管理</span>" in rendered and "新策略</span>" in rendered
    assert "开仓信号" not in rendered  # the legacy single label is replaced


def test_e2_card_marks_context_rewrite_and_downgrade(tmp_path):
    rendered = _render_card(_projected_row(tmp_path, _rewritten_19481_payload()))
    assert "data-message-context-rewrote" in rendered
    assert "data-message-execution-downgrade" in rendered
    assert "上下文降级：retargeted" in rendered


def test_e2_card_falls_back_to_the_old_label_when_classes_are_absent_or_fatal(tmp_path):
    absent = {
        "recognition_result": "非策略",
        "strategy": {},
        "lifecycle_event": {"event_type": "none"},
    }
    rendered = _render_card(_projected_row(tmp_path, absent))
    assert "data-message-primary-class" not in rendered
    assert "闲聊无关" in rendered

    fatal = dict(absent)
    fatal["message_classes"] = [{"class": "闲话", "target": None}]
    fatal["message_classes_violations"] = ["classes_empty"]
    rendered = _render_card(_projected_row(tmp_path, fatal))
    assert "data-message-primary-class" not in rendered
    assert "闲聊无关" in rendered


def test_new_trigger_names_have_chinese_labels_and_old_ones_are_kept():
    root = Path(telegram_kol_research.__file__).parent
    template = (root / "templates" / "_messages.html").read_text(encoding="utf-8")
    script = (root / "static" / "app.js").read_text(encoding="utf-8")
    for name in CONTEXT_TRIGGER_ORDER:
        assert f"'{name}':" in template
        assert f"{name}:" in script
    for historical in ("revision_language", "cancellation_language", "entered_holder_language"):
        assert f"'{historical}':" in template
        assert f"{historical}:" in script


def test_old_field_multi_target_event_with_exact_ids_is_not_targetless():
    """Review fix: raw 19741's old field names both lifecycles in ``targets[]``."""

    from telegram_kol_research.authoritative_recognition import (
        requires_context_resolution,
    )

    payload = {
        "recognition_result": "非策略",
        "lifecycle_event": {
            "event_type": "position_update",
            "target_lifecycle_id": None,
            "targets": [{"lifecycle_id": 1365}, {"target_lifecycle_id": 1361}],
        },
    }
    candidates = [
        {"thread_id": 1, "lifecycle_id": 1365, "status": "pending_entry", "reasons": ()},
    ]
    _, reasons = requires_context_resolution(
        first_pass_payload=payload,
        evidence={},
        context_window={"current": {"text": "两笔空单统一上调止损位到84600"}},
        candidates=candidates,
    )
    assert "management_without_exact_target" not in reasons

    payload["lifecycle_event"]["targets"].append({"symbol": "BTC"})
    _, reasons = requires_context_resolution(
        first_pass_payload=payload,
        evidence={},
        context_window={"current": {"text": "两笔空单统一上调止损位到84600"}},
        candidates=candidates,
    )
    assert "management_without_exact_target" in reasons
