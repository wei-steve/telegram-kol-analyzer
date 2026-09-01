import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiProviderConfig,
    AiRecognitionConfig,
)
from telegram_kol_research.context_resolution import (
    ContextNetworkRetryPolicy,
    ContextProviderResult,
    ContextProviderCircuitRegistry,
    ContextResolutionError,
    parse_context_resolution_decision,
    resolve_contextual_strategy,
)
from telegram_kol_research.context_resolution_worker import (
    run_context_resolution_once,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.context_resolution_prompt import (
    CONTEXT_RESOLUTION_SYSTEM_PROMPT,
    build_context_resolution_request,
)
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    ExecutionEvent,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    TradeSignal,
)


def _valid_payload(**overrides):
    payload = {
        "decision": "revise_thread",
        "target_thread_ids": [12],
        "management_action": None,
        "confidence": 0.93,
        "supporting_message_ids": [1460, 1462],
        "opposing_message_ids": [],
        "conflict_types": [],
        "risk_reducing_fanout_allowed": False,
        "reanalysis_triggers": [],
        "reason": "1462 explicitly updates 1460",
    }
    payload.update(overrides)
    return payload


def test_parse_context_resolution_decision_accepts_closed_valid_contract():
    decision = parse_context_resolution_decision(
        _valid_payload(),
        allowed_thread_ids={12, 13},
        allowed_message_ids={1460, 1462, 1465},
    )

    assert decision.decision == "revise_thread"
    assert decision.target_thread_ids == (12,)
    assert decision.confidence == 0.93
    assert decision.supporting_message_ids == (1460, 1462)


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_thread_ids": [999]},
        {"decision": "invented"},
        {"management_action": "invented"},
        {"decision": "new_thread", "target_thread_ids": [], "management_action": "risk_update"},
        {"decision": "cancel_thread", "management_action": "replace_entry"},
        {"conflict_types": ["invented"]},
        {"target_thread_ids": []},
        {"target_thread_ids": [12, 13]},
        {"confidence": 1.2},
        {"supporting_message_ids": [9999]},
    ],
)
def test_parse_context_resolution_decision_rejects_unsafe_values(overrides):
    with pytest.raises(ContextResolutionError):
        parse_context_resolution_decision(
            _valid_payload(**overrides),
            allowed_thread_ids={12, 13},
            allowed_message_ids={1460, 1462, 1465},
        )


def test_risk_reducing_cancel_can_fan_out_to_known_threads():
    decision = parse_context_resolution_decision(
        _valid_payload(
            decision="cancel_thread",
            target_thread_ids=[12, 13],
            risk_reducing_fanout_allowed=True,
        ),
        allowed_thread_ids={12, 13},
        allowed_message_ids={1460, 1462},
    )

    assert decision.target_thread_ids == (12, 13)


def test_exact_multi_target_partial_take_profit_preserves_target_order():
    decision = parse_context_resolution_decision(
        _valid_payload(
            decision="manage_thread",
            target_thread_ids=[13, 12],
            management_action="partial_take_profit",
            risk_reducing_fanout_allowed=True,
        ),
        allowed_thread_ids={12, 13},
        allowed_message_ids={1460, 1462},
    )

    assert decision.target_thread_ids == (13, 12)


@pytest.mark.parametrize(
    ("decision_name", "management_action"),
    [
        ("cancel_thread", "cancel_pending_entry"),
        ("exit_thread", "exit_full"),
        ("exit_thread", "exit_partial"),
    ],
)
def test_context_multi_target_contract_uses_closed_risk_reduction_policy(
    decision_name,
    management_action,
):
    decision = parse_context_resolution_decision(
        _valid_payload(
            decision=decision_name,
            target_thread_ids=[12, 13],
            management_action=management_action,
            risk_reducing_fanout_allowed=True,
        ),
        allowed_thread_ids={12, 13},
        allowed_message_ids={1460, 1462},
    )

    assert decision.target_thread_ids == (12, 13)
    assert decision.management_action == management_action


@pytest.mark.parametrize("management_action", ["move_stop_to_protect", "risk_update"])
def test_multi_target_management_fanout_refuses_non_partial_actions(management_action):
    with pytest.raises(ContextResolutionError) as raised:
        parse_context_resolution_decision(
            _valid_payload(
                decision="manage_thread",
                target_thread_ids=[12, 13],
                management_action=management_action,
                risk_reducing_fanout_allowed=True,
            ),
            allowed_thread_ids={12, 13},
            allowed_message_ids={1460, 1462},
        )

    assert raised.value.code == "multi_target_action_not_allowed"


def test_resolver_retries_malformed_json_once_and_persists_safe_attempt(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1462, text="更新上面的 BTC 多单")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    calls: list[dict] = []

    def model_caller(*, provider, system_prompt, request_payload):
        calls.append(request_payload)
        if len(calls) == 1:
            return "not-json"
        return json.dumps(_valid_payload(), ensure_ascii=False)

    decision = resolve_contextual_strategy(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=AiProviderConfig(
                base_url="https://api.deepseek.com",
                api_key="secret",
                model="deepseek-v4-flash",
            )
        ),
        evidence={
            "text": {"observed_text": "更新上面的 BTC 多单"},
            "images": [{"asset_id": 7, "fields": {"entry": "65100"}}],
        },
        context_window={
            "current": {"message_id": 1462},
            "messages": [{"message_id": 1460, "text": "BTC 多单"}],
            "reply_chain": [],
        },
        candidates=[
            {
                "thread_id": 12,
                "lifecycle_id": 22,
                "root_message_id": 1460,
                "symbol": "BTC",
                "side": "long",
            }
        ],
        first_pass_payload={"recognition_result": "是策略"},
        exchange_state={"positions": [{"pos_id_hash": "abc", "symbol": "BTC"}]},
        model_caller=model_caller,
    )

    assert decision.target_thread_ids == (12,)
    assert len(calls) == 2
    assert "secret" not in json.dumps(calls, ensure_ascii=False)
    assert "image_url" not in json.dumps(calls, ensure_ascii=False)
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
    assert attempt.status == "completed"
    assert attempt.attempts == 2
    assert json.loads(attempt.decision_json)["decision"] == "revise_thread"
    assert attempt.rejected_response_diagnostic_json is None
    assert "secret" not in attempt.request_summary_json


def test_resolver_persists_ordered_observability_and_raw_provider_usage(tmp_path):
    session_factory = create_session_factory(tmp_path / "observability.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1468, text="更新 BTC 多单")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    usage = {
        "prompt_tokens": 321,
        "completion_tokens": 45,
        "total_tokens": 366,
        "provider_extension": {"cached_tokens": 12},
    }

    decision = resolve_contextual_strategy(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        evidence={"conflicts": []},
        context_window={
            "current": {"message_id": 1468},
            "messages": [{"message_id": 1460, "text": "BTC 多单"}],
            "reply_chain": [{"message_id": 1460}],
            "active_strategies": [{"lifecycle_id": 22}],
        },
        candidates=[{"thread_id": 12, "root_message_id": 1460}],
        first_pass_payload={"recognition_result": "是策略"},
        exchange_state={},
        invocation_triggers=(
            "revision_language",
            "apparent_entry_may_be_revision",
        ),
        attempt_phase="reanalysis",
        model_caller=lambda **_: ContextProviderResult(
            content=json.dumps(
                _valid_payload(supporting_message_ids=[1460, 1468]),
                ensure_ascii=False,
            ),
            usage=usage,
        ),
    )

    assert decision.decision == "revise_thread"
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        request = json.loads(attempt.request_summary_json)
        refs = json.loads(attempt.context_message_refs_json)
        thread_ids = json.loads(attempt.candidate_thread_ids_json)
        component_hashes = json.loads(attempt.request_component_sha256_json)
        components = json.loads(attempt.request_component_bytes_json)
        provider_usage = json.loads(attempt.provider_usage_json)
    assert json.loads(attempt.invocation_triggers_json) == [
        "revision_language",
        "apparent_entry_may_be_revision",
    ]
    assert attempt.attempt_phase == "reanalysis"
    assert attempt.provider_request_count == 1
    assert request != {}
    assert refs == {
        "chat_id": 88,
        "current": [raw_id, 1468, None],
        "messages": [[None, 1460, None]],
        "reply_chain": [[None, 1460, None]],
    }
    assert thread_ids == [12]
    assert len(attempt.rendered_prompt_sha256) == 64
    assert set(component_hashes) == {
        "current_message",
        "saved_evidence",
        "message_context",
        "candidate_strategy_threads",
        "redacted_exchange_state",
        "mimo_first_pass",
    }
    assert provider_usage == [
        {"available": True, "request_number": 1, "usage": usage}
    ]
    assert attempt.shadow_would_trigger is True
    assert json.loads(attempt.shadow_conditions_json) == {
        "conditions": [
            "authoritative:revision_language",
            "authoritative:apparent_entry_may_be_revision",
        ],
        "contract": "context-resolution-shadow-v1",
        "matched_action_patterns": [],
    }
    assert attempt.shadow_agrees_with_authoritative is True
    assert attempt.shadow_disagreement_direction is None
    assert attempt.shadow_evaluation_error is None
    assert set(components) == {
        "encoding",
        "request_total_bytes",
        "message_context_bytes",
        "reply_chain_bytes",
        "active_strategies_bytes",
        "current_message_bytes",
        "remainder_bytes",
    }
    canonical = lambda value: json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert components["encoding"] == "utf-8-canonical-json-v1"
    assert components["request_total_bytes"] == len(canonical(request))
    assert components["message_context_bytes"] == len(
        canonical(request["message_context"])
    )
    assert components["reply_chain_bytes"] == len(
        canonical(request["message_context"]["reply_chain"])
    )
    assert components["active_strategies_bytes"] == len(
        canonical(request["message_context"]["active_strategies"])
    )
    assert components["current_message_bytes"] == len(
        canonical(request["current_message"])
    )
    assert components["remainder_bytes"] > 0


def test_shadow_evaluation_failure_is_recorded_without_blocking_provider(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "shadow-failure.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1471, text="更新 BTC 多单")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    monkeypatch.setattr(
        "telegram_kol_research.context_resolution.evaluate_context_resolution_shadow",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("shadow broke")),
    )
    decision = resolve_contextual_strategy(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        evidence={},
        context_window={"current": {"message_id": 1471}},
        candidates=[{"thread_id": 12, "root_message_id": 1470}],
        first_pass_payload={"recognition_result": "是策略"},
        exchange_state={},
        invocation_triggers=("multiple_same_source_candidates",),
        model_caller=lambda **_: json.dumps(
            _valid_payload(supporting_message_ids=[1470, 1471]),
            ensure_ascii=False,
        ),
    )

    assert decision.decision == "revise_thread"
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        assert attempt.status == "completed"
        assert attempt.shadow_would_trigger is None
        assert attempt.shadow_conditions_json is None
        assert attempt.shadow_agrees_with_authoritative is None
        assert attempt.shadow_disagreement_direction is None
        assert attempt.shadow_evaluation_error == "ValueError"


def test_shadow_serialization_failure_is_recorded_without_blocking_provider(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "shadow-json-failure.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1473, text="更新 BTC 多单")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    monkeypatch.setattr(
        "telegram_kol_research.context_resolution.evaluate_context_resolution_shadow",
        lambda **_kwargs: SimpleNamespace(
            would_trigger=True,
            conditions=(object(),),
            matched_action_patterns=(),
            agrees_with_authoritative=True,
            disagreement_direction=None,
        ),
    )
    decision = resolve_contextual_strategy(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        evidence={},
        context_window={"current": {"message_id": 1473}},
        candidates=[{"thread_id": 12, "root_message_id": 1470}],
        first_pass_payload={"recognition_result": "是策略"},
        exchange_state={},
        invocation_triggers=("multiple_same_source_candidates",),
        model_caller=lambda **_: json.dumps(
            _valid_payload(supporting_message_ids=[1470, 1473]),
            ensure_ascii=False,
        ),
    )

    assert decision.decision == "revise_thread"
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        assert attempt.status == "completed"
        assert attempt.shadow_would_trigger is None
        assert attempt.shadow_conditions_json is None
        assert attempt.shadow_agrees_with_authoritative is None
        assert attempt.shadow_disagreement_direction is None
        assert attempt.shadow_evaluation_error == "TypeError"


def test_shadow_result_projection_failure_does_not_block_provider(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "shadow-projection.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1474, text="更新 BTC 多单")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    monkeypatch.setattr(
        "telegram_kol_research.context_resolution.evaluate_context_resolution_shadow",
        lambda **_kwargs: SimpleNamespace(
            conditions=(),
            matched_action_patterns=(),
        ),
    )
    decision = resolve_contextual_strategy(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        evidence={},
        context_window={"current": {"message_id": 1474}},
        candidates=[{"thread_id": 12, "root_message_id": 1470}],
        first_pass_payload={"recognition_result": "是策略"},
        exchange_state={},
        invocation_triggers=("multiple_same_source_candidates",),
        model_caller=lambda **_: json.dumps(
            _valid_payload(supporting_message_ids=[1470, 1474]),
            ensure_ascii=False,
        ),
    )

    assert decision.decision == "revise_thread"
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        assert attempt.status == "completed"
        assert attempt.shadow_would_trigger is None
        assert attempt.shadow_conditions_json is None
        assert attempt.shadow_agrees_with_authoritative is None
        assert attempt.shadow_disagreement_direction is None
        assert attempt.shadow_evaluation_error == "AttributeError"


def test_shadow_skip_is_audited_but_does_not_skip_the_authoritative_call(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "shadow-skip.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1472, text="只是普通评论")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    provider_calls = []

    def model_caller(**kwargs):
        provider_calls.append(kwargs)
        return json.dumps(
            _valid_payload(supporting_message_ids=[1470, 1472]),
            ensure_ascii=False,
        )

    decision = resolve_contextual_strategy(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        evidence={},
        context_window={"current": {"message_id": 1472}},
        candidates=[
            {"thread_id": 12, "root_message_id": 1470},
            {"thread_id": 13, "root_message_id": 1471},
        ],
        first_pass_payload={
            "recognition_result": "非策略",
            "lifecycle_event": {"event_type": "none"},
            "input_reading": {"observed_text": ""},
        },
        exchange_state={},
        invocation_triggers=("multiple_same_source_candidates",),
        model_caller=model_caller,
    )

    assert decision.decision == "revise_thread"
    assert len(provider_calls) == 1
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        assert attempt.shadow_would_trigger is False
        assert attempt.shadow_agrees_with_authoritative is False
        assert (
            attempt.shadow_disagreement_direction
            == "shadow_would_skip"
        )
        assert attempt.shadow_evaluation_error is None


def test_resolver_marks_provider_usage_unavailable_and_null_metadata_is_ignored(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "unavailable-usage.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1469, text="更新 BTC 多单")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    kwargs = {
        "raw_message_id": raw_id,
        "ai_recognition_config": AiRecognitionConfig(),
        "evidence": {},
        "context_window": {
            "current": {"message_id": 1469},
            "messages": [{"message_id": 1460}],
        },
        "candidates": [{"thread_id": 12, "root_message_id": 1460}],
        "first_pass_payload": {"recognition_result": "是策略"},
        "exchange_state": {},
    }
    expected = resolve_contextual_strategy(
        session_factory,
        **kwargs,
        model_caller=lambda **_: json.dumps(
            _valid_payload(supporting_message_ids=[1460, 1469]),
            ensure_ascii=False,
        ),
    )
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        assert json.loads(attempt.provider_usage_json) == [
            {
                "available": False,
                "reason": "provider_usage_not_returned",
                "request_number": 1,
            }
        ]
        attempt.invocation_triggers_json = None
        attempt.attempt_phase = None
        attempt.provider_request_count = None
        attempt.provider_usage_json = None
        attempt.request_component_bytes_json = None
        session.commit()

    repeated = resolve_contextual_strategy(
        session_factory,
        **kwargs,
        model_caller=lambda **_: (_ for _ in ()).throw(
            AssertionError("cached decision must not call provider")
        ),
    )
    assert repeated == expected


def test_network_error_schedules_durable_retry_without_immediate_second_request(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "durable-network-retry.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1470, text="更新 BTC 多单")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    calls = []
    now = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)
    policy = ContextNetworkRetryPolicy(
        base_delay_seconds=5,
        max_delay_seconds=60,
        failure_threshold=3,
        open_seconds=120,
    )

    with pytest.raises(ContextResolutionError) as raised:
        resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            evidence={},
            context_window={"current": {"message_id": 1470}, "messages": []},
            candidates=[],
            first_pass_payload={},
            exchange_state={},
            model_caller=lambda **kwargs: calls.append(kwargs) or (_ for _ in ()).throw(
                OSError("network unavailable")
            ),
            network_retry_policy=policy,
            circuit_registry=ContextProviderCircuitRegistry(),
            now_provider=lambda: now,
        )

    assert raised.value.code == "network_error"
    assert len(calls) == 1
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        assert attempt.status == "retry_pending"
        assert attempt.attempts == 1
        assert attempt.provider_request_count == 1
        assert attempt.next_attempt_at == (
            now + timedelta(seconds=5)
        ).replace(tzinfo=None)
        assert attempt.decision_json is None


def test_isolated_network_error_retries_in_window_and_keeps_same_decision(
    tmp_path,
):
    retry_factory = create_session_factory(tmp_path / "isolated-retry.db")
    baseline_factory = create_session_factory(tmp_path / "immediate-success.db")
    for factory in (retry_factory, baseline_factory):
        with factory() as session:
            raw = RawMessage(chat_id=88, message_id=1471, text="更新 BTC 多单")
            session.add(raw)
            session.commit()
    policy = ContextNetworkRetryPolicy(
        base_delay_seconds=5,
        max_delay_seconds=60,
        failure_threshold=3,
        open_seconds=120,
    )
    circuit = ContextProviderCircuitRegistry()
    start = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)
    success_payload = _valid_payload(
        supporting_message_ids=[1471],
        target_thread_ids=[],
        decision="hold",
    )
    common = {
        "ai_recognition_config": AiRecognitionConfig(),
        "evidence": {},
        "context_window": {"current": {"message_id": 1471}, "messages": []},
        "candidates": [],
        "first_pass_payload": {},
        "exchange_state": {},
    }
    baseline = resolve_contextual_strategy(
        baseline_factory,
        raw_message_id=1,
        **common,
        model_caller=lambda **_: success_payload,
    )
    calls = []

    def first_call(**kwargs):
        calls.append(("first", start))
        raise OSError("isolated reset")

    with pytest.raises(ContextResolutionError):
        resolve_contextual_strategy(
            retry_factory,
            raw_message_id=1,
            **common,
            model_caller=first_call,
            network_retry_policy=policy,
            circuit_registry=circuit,
            now_provider=lambda: start,
        )

    second_at = start + timedelta(seconds=65)

    def delayed_success(**kwargs):
        calls.append(("second", second_at))
        return success_payload

    result_holder = {}

    def reanalyze(raw_message_id, _fingerprint):
        result_holder["decision"] = resolve_contextual_strategy(
            retry_factory,
            raw_message_id=raw_message_id,
            **common,
            model_caller=delayed_success,
            network_retry_policy=policy,
            circuit_registry=circuit,
            now_provider=lambda: second_at,
        )
        return {"status": "completed"}

    worker_result = run_context_resolution_once(
        retry_factory,
        context_fingerprint_factory=lambda _: "sha256:current",
        reanalyze=reanalyze,
        now=second_at,
    )

    assert worker_result["status"] == "completed"
    assert result_holder["decision"] == baseline
    assert calls == [("first", start), ("second", second_at)]
    assert second_at - start < timedelta(minutes=15)
    with retry_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        assert attempt.status == "completed"
        assert attempt.attempts == 2
        assert attempt.provider_request_count == 2


def test_legacy_retry_row_keeps_request_numbering_when_usage_columns_are_null(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "legacy-retry.db")
    with session_factory() as session:
        session.add(RawMessage(chat_id=88, message_id=1473, text="更新 BTC 多单"))
        session.commit()
    start = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)
    common = {
        "raw_message_id": 1,
        "ai_recognition_config": AiRecognitionConfig(),
        "evidence": {},
        "context_window": {"current": {"message_id": 1473}, "messages": []},
        "candidates": [],
        "first_pass_payload": {},
        "exchange_state": {},
        "network_retry_policy": ContextNetworkRetryPolicy(),
        "circuit_registry": ContextProviderCircuitRegistry(),
    }
    with pytest.raises(ContextResolutionError):
        resolve_contextual_strategy(
            session_factory,
            **common,
            model_caller=lambda **_: (_ for _ in ()).throw(OSError("down")),
            now_provider=lambda: start,
        )
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        attempt.provider_request_count = None
        attempt.provider_usage_json = None
        session.commit()

    decision = resolve_contextual_strategy(
        session_factory,
        **common,
        model_caller=lambda **_: _valid_payload(
            supporting_message_ids=[1473],
            target_thread_ids=[],
            decision="hold",
        ),
        now_provider=lambda: start + timedelta(seconds=5),
    )

    assert decision.decision == "hold"
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        assert attempt.provider_request_count == 2
        assert json.loads(attempt.provider_usage_json) == [
            {
                "available": False,
                "reason": "legacy_provider_usage_unavailable",
                "request_number": 1,
            },
            {
                "available": False,
                "reason": "provider_usage_not_returned",
                "request_number": 2,
            },
        ]


def test_consecutive_network_errors_open_circuit_and_admit_one_half_open_probe():
    policy = ContextNetworkRetryPolicy(
        base_delay_seconds=5,
        max_delay_seconds=60,
        failure_threshold=2,
        open_seconds=120,
    )
    registry = ContextProviderCircuitRegistry()
    provider_key = "https://provider.example|model"
    now = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)

    assert registry.record_network_failure(provider_key, now, policy) == (
        now + timedelta(seconds=5)
    )
    open_until = registry.record_network_failure(
        provider_key,
        now + timedelta(seconds=1),
        policy,
    )
    assert open_until == now + timedelta(seconds=121)
    assert registry.reserve_retry(provider_key, now + timedelta(seconds=60)) == (
        False,
        open_until,
    )
    assert registry.reserve_retry(provider_key, open_until) == (True, None)
    assert registry.reserve_retry(provider_key, open_until) == (
        False,
        open_until + timedelta(seconds=120),
    )
    registry.record_success(provider_key)
    assert registry.reserve_retry(provider_key, open_until) == (True, None)


def test_network_retry_policy_is_environment_configurable_with_bounded_defaults():
    configured = ContextNetworkRetryPolicy.from_environ(
        {
            "TELEGRAM_KOL_CONTEXT_NETWORK_BASE_DELAY_SECONDS": "7",
            "TELEGRAM_KOL_CONTEXT_NETWORK_MAX_DELAY_SECONDS": "45",
            "TELEGRAM_KOL_CONTEXT_NETWORK_FAILURE_THRESHOLD": "4",
            "TELEGRAM_KOL_CONTEXT_NETWORK_OPEN_SECONDS": "180",
        }
    )
    conservative = ContextNetworkRetryPolicy.from_environ(
        {
            "TELEGRAM_KOL_CONTEXT_NETWORK_BASE_DELAY_SECONDS": "invalid",
            "TELEGRAM_KOL_CONTEXT_NETWORK_MAX_DELAY_SECONDS": "0",
            "TELEGRAM_KOL_CONTEXT_NETWORK_FAILURE_THRESHOLD": "1",
            "TELEGRAM_KOL_CONTEXT_NETWORK_OPEN_SECONDS": "99999",
        }
    )

    assert configured == ContextNetworkRetryPolicy(7, 45, 4, 180)
    assert conservative == ContextNetworkRetryPolicy()


def test_open_circuit_reschedules_durable_retry_without_counting_or_dropping_it(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "open-circuit-durable.db")
    with session_factory() as session:
        session.add(RawMessage(chat_id=88, message_id=1472, text="更新 BTC 多单"))
        session.commit()
    start = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)
    policy = ContextNetworkRetryPolicy(5, 60, 2, 120)
    circuit = ContextProviderCircuitRegistry()
    common = {
        "raw_message_id": 1,
        "ai_recognition_config": AiRecognitionConfig(),
        "evidence": {},
        "context_window": {"current": {"message_id": 1472}, "messages": []},
        "candidates": [],
        "first_pass_payload": {},
        "exchange_state": {},
        "network_retry_policy": policy,
        "circuit_registry": circuit,
    }
    with pytest.raises(ContextResolutionError):
        resolve_contextual_strategy(
            session_factory,
            **common,
            model_caller=lambda **_: (_ for _ in ()).throw(OSError("down")),
            now_provider=lambda: start,
        )
    open_until = circuit.record_network_failure("|", start + timedelta(seconds=1), policy)
    calls = []
    with pytest.raises(ContextResolutionError) as raised:
        resolve_contextual_strategy(
            session_factory,
            **common,
            model_caller=lambda **kwargs: calls.append(kwargs),
            now_provider=lambda: start + timedelta(seconds=5),
        )

    assert raised.value.code == "network_error"
    assert calls == []
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        assert attempt.status == "retry_pending"
        assert attempt.attempts == 1
        assert attempt.provider_request_count == 1
        assert attempt.next_attempt_at == open_until.replace(tzinfo=None)
        assert attempt.decision_json is None


def test_resolver_retries_closed_contract_error_once_then_completes(tmp_path):
    session_factory = create_session_factory(tmp_path / "contract-retry.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1465, text="BTC空单止盈一部分")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    responses = [
        _valid_payload(
            decision="manage_thread",
            target_thread_ids=[12, 13],
            management_action="move_stop_to_protect",
            risk_reducing_fanout_allowed=True,
            supporting_message_ids=[1465],
        ),
        _valid_payload(
            decision="manage_thread",
            target_thread_ids=[12],
            management_action="partial_take_profit",
            supporting_message_ids=[1465],
        ),
    ]

    decision = resolve_contextual_strategy(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        evidence={},
        context_window={"current": {"message_id": 1465}, "messages": []},
        candidates=[{"thread_id": 12}, {"thread_id": 13}],
        first_pass_payload={},
        exchange_state={},
        model_caller=lambda **kwargs: responses.pop(0),
    )

    assert decision.target_thread_ids == (12,)
    assert responses == []
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
    assert attempt.status == "completed"
    assert attempt.attempts == 2


def test_target_not_allowed_retry_adds_correction_and_keeps_bounded_diagnostic(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "target-correction.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1465, text="回顾已有策略")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    responses = [
        _valid_payload(
            decision="hold",
            target_thread_ids=[12],
            management_action=None,
            supporting_message_ids=[1465],
        ),
        _valid_payload(
            decision="hold",
            target_thread_ids=[],
            management_action=None,
            supporting_message_ids=[1465],
        ),
    ]
    calls = []

    def model_caller(**kwargs):
        calls.append(kwargs["system_prompt"])
        return responses.pop(0)

    decision = resolve_contextual_strategy(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        evidence={},
        context_window={"current": {"message_id": 1465}, "messages": []},
        candidates=[{"thread_id": 12}],
        first_pass_payload={},
        exchange_state={},
        model_caller=model_caller,
    )

    assert decision.decision == "hold"
    assert decision.target_thread_ids == ()
    assert len(calls) == 2
    assert "上一次响应违反 target_not_allowed" not in calls[0]
    assert "上一次响应违反 target_not_allowed" in calls[1]
    assert "不要修改 decision 来绕过校验" in calls[1]
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
    assert json.loads(attempt.rejected_response_diagnostic_json) == {
        "decision": "hold",
        "error_class": "target_not_allowed",
        "target_thread_count": 1,
    }
    assert "12" not in attempt.rejected_response_diagnostic_json
    assert attempt.status == "completed"
    assert attempt.attempts == 2


def test_target_not_allowed_retry_remains_exhausted_after_two_invalid_responses(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "target-exhausted.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=1465, text="回顾已有策略")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    calls = []
    invalid = _valid_payload(
        decision="hold",
        target_thread_ids=[12],
        management_action=None,
        supporting_message_ids=[1465],
    )

    def model_caller(**kwargs):
        calls.append(kwargs["system_prompt"])
        return invalid

    with pytest.raises(ContextResolutionError) as raised:
        resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            evidence={},
            context_window={"current": {"message_id": 1465}, "messages": []},
            candidates=[{"thread_id": 12}],
            first_pass_payload={},
            exchange_state={},
            model_caller=model_caller,
        )

    assert raised.value.code == "target_not_allowed"
    assert len(calls) == 2
    assert "上一次响应违反 target_not_allowed" in calls[1]
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
    assert attempt.status == "exhausted"
    assert attempt.attempts == 2
    assert attempt.error_class == "target_not_allowed"


def test_resolver_exhausts_repeated_closed_contract_error(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "contract-exhausted.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=3465, text="Btc eth空单可以止盈一部分")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    captures = []
    monkeypatch.setattr(
        "telegram_kol_research.context_resolution.capture_runtime_incident_best_effort",
        lambda *args, **kwargs: captures.append(kwargs),
        raising=False,
    )
    calls = 0
    invalid = _valid_payload(
        decision="manage_thread",
        target_thread_ids=[12, 13],
        management_action="move_stop_to_protect",
        risk_reducing_fanout_allowed=True,
        supporting_message_ids=[3465],
    )

    def invalid_caller(**kwargs):
        nonlocal calls
        calls += 1
        return invalid

    with pytest.raises(ContextResolutionError) as raised:
        resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            evidence={},
            context_window={"current": {"message_id": 3465}, "messages": []},
            candidates=[{"thread_id": 12}, {"thread_id": 13}],
            first_pass_payload={},
            exchange_state={},
            model_caller=invalid_caller,
        )

    assert raised.value.code == "multi_target_action_not_allowed"
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
    assert attempt.status == "exhausted"
    assert attempt.attempts == 2
    assert attempt.error_class == "multi_target_action_not_allowed"
    assert len(captures) == 1
    assert captures[0]["status"] == "exhausted"

    with pytest.raises(ContextResolutionError) as replayed:
        resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(),
            evidence={},
            context_window={"current": {"message_id": 3465}, "messages": []},
            candidates=[{"thread_id": 12}, {"thread_id": 13}],
            first_pass_payload={},
            exchange_state={},
            model_caller=invalid_caller,
        )
    assert replayed.value.code == "multi_target_action_not_allowed"
    assert calls == 2
    assert len(captures) == 1


def test_resolver_rejects_supporting_message_outside_context_and_persists_failure(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=89, message_id=1465, text="策略先取消")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    with pytest.raises(ContextResolutionError):
        resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(
                text_provider=AiProviderConfig(
                    base_url="https://api.deepseek.com",
                    model="deepseek-v4-flash",
                )
            ),
            evidence={},
            context_window={
                "current": {"message_id": 1465},
                "messages": [{"message_id": 1462}],
                "reply_chain": [{"message_id": 1460}],
            },
            candidates=[{"thread_id": 12, "root_message_id": 1460}],
            first_pass_payload={},
            exchange_state={},
            model_caller=lambda **kwargs: _valid_payload(
                decision="cancel_thread",
                supporting_message_ids=[9999],
            ),
        )

    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
    assert attempt.status == "exhausted"
    assert attempt.attempts == 2
    assert attempt.error_class == "message_evidence_outside_context"


def test_completed_context_fingerprint_is_reused_without_recalling_model(tmp_path):
    session_factory = create_session_factory(tmp_path / "idempotent.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=91, message_id=1500, text="更新 BTC 多单")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    calls = 0

    def model_caller(**kwargs):
        nonlocal calls
        calls += 1
        return _valid_payload(
            supporting_message_ids=[1499, 1500],
        )

    kwargs = {
        "raw_message_id": raw_id,
        "ai_recognition_config": AiRecognitionConfig(
            text_provider=AiProviderConfig(
                base_url="https://api.deepseek.com",
                model="deepseek-v4-flash",
            )
        ),
        "evidence": {},
        "context_window": {
            "current": {"message_id": 1500},
            "messages": [{"message_id": 1499}],
            "reply_chain": [],
        },
        "candidates": [{"thread_id": 12, "root_message_id": 1499}],
        "first_pass_payload": {},
        "exchange_state": {},
        "model_caller": model_caller,
    }

    first = resolve_contextual_strategy(session_factory, **kwargs)
    repeated = resolve_contextual_strategy(session_factory, **kwargs)

    assert repeated == first
    assert calls == 1
    with session_factory() as session:
        assert session.query(ContextResolutionAttempt).count() == 1


def test_resolver_selects_configured_mimo_provider_without_changing_contract(tmp_path):
    session_factory = create_session_factory(tmp_path / "mimo-selection.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=92, message_id=1600, text="更新 BTC 多单")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    calls = []
    evidence = {"text": {"observed_text": "更新 BTC 多单"}}
    context_window = {
        "current": {"message_id": 1600},
        "messages": [{"message_id": 1599, "text": "BTC 多单"}],
        "reply_chain": [],
    }
    candidates = [{"thread_id": 12, "root_message_id": 1599}]
    first_pass_payload = {"recognition_result": "是策略"}
    exchange_state = {"positions": []}

    def model_caller(**kwargs):
        calls.append(kwargs)
        return _valid_payload(supporting_message_ids=[1599, 1600])

    resolve_contextual_strategy(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=AiProviderConfig(
                base_url="https://api.deepseek.com",
                api_key="deepseek-secret",
                model="deepseek-v4-flash",
            ),
            ai_models=[
                AiModelConfig(
                    id="deepseek-v4-flash",
                    label="DeepSeek",
                    base_url="https://api.deepseek.com",
                    api_key="deepseek-secret",
                    model="deepseek-v4-flash",
                    supports_text=True,
                ),
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo",
                    base_url="https://api.xiaomimimo.com/v1/",
                    api_key="mimo-secret",
                    model="mimo-v2.5",
                    timeout_seconds=17.5,
                    supports_text=True,
                ),
            ],
            context_resolution_model_id="mimo-v2.5",
        ),
        evidence=evidence,
        context_window=context_window,
        candidates=candidates,
        first_pass_payload=first_pass_payload,
        exchange_state=exchange_state,
        model_caller=model_caller,
    )

    assert len(calls) == 1
    provider = calls[0]["provider"]
    assert provider.base_url == "https://api.xiaomimimo.com/v1/"
    assert provider.api_key == "mimo-secret"
    assert provider.model == "mimo-v2.5"
    assert provider.timeout_seconds == 17.5
    assert calls[0]["system_prompt"] == CONTEXT_RESOLUTION_SYSTEM_PROMPT
    assert calls[0]["request_payload"] == build_context_resolution_request(
        current_message={
            "raw_message_id": raw_id,
            "chat_id": 92,
            "message_id": 1600,
            "posted_at": None,
            "text": "更新 BTC 多单",
            "reply_to_message_id": None,
        },
        evidence=evidence,
        context_window=context_window,
        candidates=candidates,
        exchange_state=exchange_state,
        first_pass_payload=first_pass_payload,
    )


def test_mimo_provider_failure_exhausts_without_deepseek_fallback_or_operations(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "mimo-failure.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=93, message_id=1700, text="看看已有策略")
        session.add(raw)
        session.commit()
        raw_id = raw.id
    calls = {"mimo": 0, "deepseek": 0}
    policy = ContextNetworkRetryPolicy()
    circuit = ContextProviderCircuitRegistry()
    first_at = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)

    def failing_caller(*, provider, **_kwargs):
        if provider.model == "mimo-v2.5":
            calls["mimo"] += 1
        else:
            calls["deepseek"] += 1
        raise RuntimeError("provider unavailable")

    with pytest.raises(ContextResolutionError) as raised:
        resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(
                text_provider=AiProviderConfig(
                    base_url="https://api.deepseek.com",
                    api_key="deepseek-secret",
                    model="deepseek-v4-flash",
                ),
                ai_models=[
                    AiModelConfig(
                        id="deepseek-v4-flash",
                        label="DeepSeek",
                        base_url="https://api.deepseek.com",
                        api_key="deepseek-secret",
                        model="deepseek-v4-flash",
                    ),
                    AiModelConfig(
                        id="mimo-v2.5",
                        label="MiMo",
                        base_url="https://api.xiaomimimo.com/v1",
                        api_key="mimo-secret",
                        model="mimo-v2.5",
                    ),
                ],
                context_resolution_model_id="mimo-v2.5",
            ),
            evidence={},
            context_window={"current": {"message_id": 1700}, "messages": []},
            candidates=[],
            first_pass_payload={},
            exchange_state={},
            model_caller=failing_caller,
            network_retry_policy=policy,
            circuit_registry=circuit,
            now_provider=lambda: first_at,
        )

    assert raised.value.code == "network_error"
    assert calls == {"mimo": 1, "deepseek": 0}
    with pytest.raises(ContextResolutionError) as exhausted:
        resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_id,
            ai_recognition_config=AiRecognitionConfig(
                ai_models=[
                    AiModelConfig(
                        id="mimo-v2.5",
                        label="MiMo",
                        base_url="https://api.xiaomimimo.com/v1",
                        api_key="mimo-secret",
                        model="mimo-v2.5",
                    )
                ],
                context_resolution_model_id="mimo-v2.5",
            ),
            evidence={},
            context_window={"current": {"message_id": 1700}, "messages": []},
            candidates=[],
            first_pass_payload={},
            exchange_state={},
            model_caller=failing_caller,
            network_retry_policy=policy,
            circuit_registry=circuit,
            now_provider=lambda: first_at + timedelta(seconds=5),
        )
    assert exhausted.value.code == "network_error"
    assert calls == {"mimo": 2, "deepseek": 0}
    with session_factory() as session:
        attempt = session.query(ContextResolutionAttempt).one()
        assert attempt.model == "mimo-v2.5"
        assert attempt.status == "exhausted"
        assert attempt.error_class == "network_error"
        assert attempt.attempts == 2
        for model in (
            RecognitionDecision,
            SignalCandidate,
            StrategyLifecycle,
            TradeSignal,
            ExecutionEvent,
        ):
            assert session.query(model).count() == 0
