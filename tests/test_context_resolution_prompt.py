from telegram_kol_research.context_resolution_prompt import (
    CONTEXT_RESOLUTION_PROMPT_VERSION,
    CONTEXT_RESOLUTION_SYSTEM_PROMPT,
    build_context_resolution_request,
)
from telegram_kol_research.strategy_thread_candidates import StrategyThreadCandidate


def test_context_request_includes_bounded_candidate_risk_summary():
    candidate = StrategyThreadCandidate(
        thread_id=63,
        lifecycle_id=693,
        root_message_id=4167,
        symbol="BTC",
        side="long",
        status="entered",
        score=320,
        reasons=("same_chat", "same_symbol", "same_side"),
        lifecycle_summary={"id": 693},
        binding_summary={"id": 241, "payload_json": "must-not-be-present"},
        verified_leg_summaries=(),
        risk_state="current_risk",
        live_verified_pos_ids=("pos-current",),
        pending_entry_leg_ids=(431,),
        uncertain_entry_leg_ids=(),
    )

    request = build_context_resolution_request(
        current_message={"message_id": 4168},
        evidence={},
        context_window=[],
        candidates=(candidate,),
        exchange_state={},
        first_pass_payload={},
    )

    saved = request["candidate_strategy_threads"][0]
    assert saved["risk_state"] == "current_risk"
    assert saved["live_verified_pos_ids"] == ["pos-current"]
    assert saved["pending_entry_leg_ids"] == [431]
    assert saved["uncertain_entry_leg_ids"] == []
    assert "payload_json" not in saved["binding_summary"]


def test_context_prompt_limits_management_fanout_to_explicit_partial_profit():
    assert "partial_take_profit" in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    assert "明确点名" in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    assert "增加风险" in CONTEXT_RESOLUTION_SYSTEM_PROMPT


def test_context_prompt_v2_states_target_cardinality_and_commentary_example():
    assert CONTEXT_RESOLUTION_PROMPT_VERSION == "context-resolution-v2"
    assert (
        "new_thread、hold、unresolved 的 target_thread_ids 必须是 []"
        in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    )
    assert (
        "revise_thread、manage_thread、cancel_thread、exit_thread"
        in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    )
    assert "仅讨论已有策略不等于产生可执行目标" in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    assert '"decision":"hold"' in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    assert '"target_thread_ids":[]' in CONTEXT_RESOLUTION_SYSTEM_PROMPT
    assert "9758" not in CONTEXT_RESOLUTION_SYSTEM_PROMPT
