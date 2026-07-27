import json
from types import SimpleNamespace

import pytest

from telegram_kol_research.ai_recognition_config import (
    AiProviderConfig,
    AiRecognitionConfig,
)
from telegram_kol_research.context_resolution import (
    ContextResolutionError,
    parse_context_resolution_decision,
    resolve_contextual_strategy,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import ContextResolutionAttempt, RawMessage


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
    assert "secret" not in attempt.request_summary_json


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
    assert attempt.status == "failed"
    assert attempt.error_class == "contract_error"
