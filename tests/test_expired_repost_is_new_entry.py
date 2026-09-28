"""2026-09-28 chen-btc-repost design (docs/plans/2026-09-28-chen-btc-expired-
repost-and-queue-block-design.md §2.3, 2a + 2b).

Raw 19073 (msg 10758, 2026-09-25 14:07:58 UTC) posted "BTC，83000-83300附近，
做多 止损预计：81500 止盈预计：85800-87000" in chat -1002337721508. Lifecycle
1327 (strategy_thread 696) never got an exchange leg and expired at
09-26 01:32. On 2026-09-28 02:58:35 raw 19481 (msg 10789) reposted the exact
same text. The first pass read it as 是策略; the candidate generator flagged
`overlapping_entry` against 696 purely because the price range matched, which
fired `apparent_entry_may_be_revision`; the context model resolved it to
`manage_thread` against 696 with `management_action=null`; and the
unconditional downgrade in `_resolved_mimo_result` turned it into 非策略 with
no alert -- a genuine re-entry signal silently dropped.

2a narrows the trigger to only count `overlapping_entry` when the candidate is
still `pending_entry` (revisable). 2b is the result-layer backstop: even if
something else lands the message in the context resolver, a `manage_thread` /
`revise_thread` decision with no management action, targeting only `expired`
threads with no exchange exposure, cannot be a management instruction at all,
so the first pass is kept and the override is recorded for audit.
"""

from dataclasses import replace

from telegram_kol_research.authoritative_recognition import (
    _resolved_mimo_result,
    requires_context_resolution,
)
from telegram_kol_research.context_resolution import ContextResolutionDecision
from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult
from telegram_kol_research.strategy_thread_candidates import StrategyThreadCandidate


RAW_19481_TEXT = "BTC，83000-83300附近，做多 止损预计：81500 止盈预计：85800-87000"


def _requires_context_resolution_for_repost(*, status: str):
    return requires_context_resolution(
        first_pass_payload={
            "recognition_result": "是策略",
            "lifecycle_event": {"event_type": "none"},
        },
        evidence={"conflicts": []},
        context_window={"current": {"text": RAW_19481_TEXT}, "reply_chain": []},
        candidates=[
            {
                "thread_id": 696,
                "lifecycle_id": 1327,
                "status": status,
                "reasons": ("overlapping_entry",),
            }
        ],
    )


def test_expired_candidate_overlap_no_longer_fires_the_revision_trigger():
    """2a: an ``expired`` lifecycle 696 cannot be "revised" by 19481."""

    required, reasons = _requires_context_resolution_for_repost(status="expired")

    assert (required, reasons) == (False, ())


def test_pending_entry_candidate_overlap_still_fires_the_revision_trigger():
    """Negative for 2a: a still-live, un-entered candidate is unchanged."""

    required, reasons = _requires_context_resolution_for_repost(status="pending_entry")

    assert required is True
    assert reasons == ("apparent_entry_may_be_revision",)


def _repost_candidate(
    *,
    status: str,
    binding_summary=None,
    risk_state: str = "no_current_risk",
    live_verified_pos_ids: tuple[str, ...] = (),
    pending_entry_leg_ids: tuple[int, ...] = (),
    uncertain_entry_leg_ids: tuple[int, ...] = (),
    execution_binding_id=None,
) -> StrategyThreadCandidate:
    lifecycle_summary = {"id": 1327}
    if execution_binding_id is not None:
        lifecycle_summary["execution_binding_id"] = execution_binding_id
    return StrategyThreadCandidate(
        thread_id=696,
        lifecycle_id=1327,
        root_message_id=10758,
        symbol="BTC",
        side="long",
        status=status,
        score=60,
        reasons=("same_chat", "same_symbol", "same_side", "overlapping_entry"),
        lifecycle_summary=lifecycle_summary,
        binding_summary=binding_summary,
        verified_leg_summaries=(),
        risk_state=risk_state,
        live_verified_pos_ids=live_verified_pos_ids,
        pending_entry_leg_ids=pending_entry_leg_ids,
        uncertain_entry_leg_ids=uncertain_entry_leg_ids,
    )


def _repost_mimo(*, recognition_result: str = "是策略") -> MimoAuthoritativeResult:
    return MimoAuthoritativeResult(
        raw_message_id=19481,
        payload={
            "recognition_result": recognition_result,
            "strategy": {
                "symbol": "BTC",
                "side": "long",
                "entry_range": [83000, 83300],
                "stop_loss": 81500,
                "take_profit": [85800, 87000],
            },
            "lifecycle_event": {"event_type": "none"},
            "evidence": {"images": []},
        },
        input_kind="text",
        model="mimo-v2.5",
        status=recognition_result,
    )


def _manage_thread_decision(
    *,
    management_action=None,
    target_thread_ids=(696,),
    confidence: float = 0.9,
) -> ContextResolutionDecision:
    return ContextResolutionDecision(
        decision="manage_thread",
        target_thread_ids=tuple(target_thread_ids),
        management_action=management_action,
        confidence=confidence,
        supporting_message_ids=(10789,),
        opposing_message_ids=(),
        conflict_types=(),
        risk_reducing_fanout_allowed=False,
        reanalysis_triggers=(),
        reason="虽然 thread_id 696 已过期，当前消息可视为对该已过期策略的重新发布或确认",
    )


def test_terminal_target_guard_keeps_the_first_pass_strategy():
    """2b: 19481 replayed -- manage_thread/null at an expired, exposure-free 696."""

    mimo = _repost_mimo()
    decision = _manage_thread_decision()
    candidate = _repost_candidate(status="expired")

    result = _resolved_mimo_result(
        mimo,
        decision,
        (candidate,),
        current_message_id=10789,
        exact_risk_reduction_authorized=False,
    )

    assert result.payload["recognition_result"] == "是策略"
    assert result.payload["strategy"] == mimo.payload["strategy"]
    assert result.payload["lifecycle_event"] == {"event_type": "none"}
    assert result.payload["_context_resolution"]["override_rejected"] == "terminal_target"
    # Exactly the ``new_thread`` shape: status untouched, not forced to 非策略.
    assert result.status == "是策略"


def test_terminal_target_guard_does_not_fire_with_an_explicit_management_action():
    """Negative: a non-null management_action on an expired thread is unchanged."""

    mimo = _repost_mimo()
    decision = _manage_thread_decision(management_action="cancel_pending_entry")
    candidate = _repost_candidate(status="expired")

    result = _resolved_mimo_result(
        mimo,
        decision,
        (candidate,),
        current_message_id=10789,
        exact_risk_reduction_authorized=False,
    )

    assert result.payload["recognition_result"] == "非策略"
    assert result.payload["lifecycle_event"]["management_action"] == "cancel_pending_entry"
    assert "override_rejected" not in result.payload["_context_resolution"]


def test_terminal_target_guard_does_not_fire_for_a_pending_entry_target():
    """Negative: target still ``pending_entry`` (revisable) -- unchanged."""

    mimo = _repost_mimo()
    decision = _manage_thread_decision()
    candidate = _repost_candidate(status="pending_entry")

    result = _resolved_mimo_result(
        mimo,
        decision,
        (candidate,),
        current_message_id=10789,
        exact_risk_reduction_authorized=False,
    )

    assert result.payload["recognition_result"] == "非策略"
    assert "override_rejected" not in result.payload["_context_resolution"]


def test_terminal_target_guard_does_not_fire_for_an_entered_target():
    """Negative: target already ``entered`` (live position) -- unchanged."""

    mimo = _repost_mimo()
    decision = _manage_thread_decision()
    candidate = _repost_candidate(status="entered")

    result = _resolved_mimo_result(
        mimo,
        decision,
        (candidate,),
        current_message_id=10789,
        exact_risk_reduction_authorized=False,
    )

    assert result.payload["recognition_result"] == "非策略"
    assert "override_rejected" not in result.payload["_context_resolution"]


def test_terminal_target_guard_does_not_fire_with_an_unsettled_exchange_leg():
    """Negative: expired but still holding something -- unchanged (A1 condition 3)."""

    mimo = _repost_mimo()
    decision = _manage_thread_decision()
    candidate = _repost_candidate(
        status="expired",
        binding_summary={"id": 55},
        risk_state="current_risk",
        live_verified_pos_ids=("pos-55",),
        execution_binding_id=55,
    )

    result = _resolved_mimo_result(
        mimo,
        decision,
        (candidate,),
        current_message_id=10789,
        exact_risk_reduction_authorized=False,
    )

    assert result.payload["recognition_result"] == "非策略"
    assert "override_rejected" not in result.payload["_context_resolution"]


def test_terminal_target_guard_does_not_fire_when_first_pass_was_not_a_strategy():
    """Negative: first pass already 非策略 -- guard is scoped to 是策略 only."""

    mimo = _repost_mimo(recognition_result="非策略")
    decision = _manage_thread_decision()
    candidate = _repost_candidate(status="expired")

    result = _resolved_mimo_result(
        mimo,
        decision,
        (candidate,),
        current_message_id=10789,
        exact_risk_reduction_authorized=False,
    )

    assert result.payload["recognition_result"] == "非策略"
    assert "override_rejected" not in result.payload["_context_resolution"]


def test_terminal_target_guard_applies_to_revise_thread_too():
    """revise_thread/null at an expired, exposure-free target is also kept."""

    mimo = _repost_mimo()
    decision = replace(
        _manage_thread_decision(),
        decision="revise_thread",
    )
    candidate = _repost_candidate(status="expired")

    result = _resolved_mimo_result(
        mimo,
        decision,
        (candidate,),
        current_message_id=10789,
        exact_risk_reduction_authorized=False,
    )

    assert result.payload["recognition_result"] == "是策略"
    assert result.payload["_context_resolution"]["override_rejected"] == "terminal_target"
    assert result.status == "是策略"
