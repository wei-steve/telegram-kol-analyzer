import json

import pytest

from telegram_kol_research.ai_recognition_config import (
    AiModelConfig,
    AiProviderConfig,
    AiRecognitionConfig,
)
from telegram_kol_research.context_resolution import (
    ContextResolutionError,
    parse_context_resolution_decision,
    resolve_contextual_strategy,
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
        )

    assert raised.value.code == "network_error"
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
