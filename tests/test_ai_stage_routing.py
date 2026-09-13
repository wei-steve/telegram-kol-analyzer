"""Every remaining stage picks its model from its own chain (design §6).

One pair of questions per stage: does a backup answer when the primary does
not, and is a single-model chain still exactly what it was? The second half is
the one that matters for deployment -- nobody has a fallback configured yet.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from telegram_kol_research.ai_model_router import (
    MIN_REMAINING_SECONDS,
    async_run_with_fallback,
    run_with_fallback,
)
from telegram_kol_research.ai_recognition_config import (
    AiModel,
    AiModelConfig,
    AiProvider,
    AiProviderConfig,
    AiRecognitionConfig,
    stage_head_provider,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage


PRIMARY = "primary-text"
BACKUP = "backup-text"


def _model(model_id: str, *, image: bool = False) -> AiModelConfig:
    return AiModelConfig(
        id=model_id,
        label=model_id,
        base_url=f"https://{model_id}.example.com/v1",
        api_key=f"{model_id}-key",
        model=model_id,
        supports_text=True,
        supports_image=image,
    )


def _chain_config(stage: str, *model_ids: str, image: bool = False) -> AiRecognitionConfig:
    providers = [
        AiProvider(
            id=model_id,
            base_url=f"https://{model_id}.example.com/v1",
            api_key=f"{model_id}-key",
        )
        for model_id in (PRIMARY, BACKUP)
    ]
    by_id = {provider.id: provider for provider in providers}
    return AiRecognitionConfig(
        providers=providers,
        models=[
            AiModel(
                id=model_id,
                provider_id=model_id,
                model=model_id,
                supports_text=True,
                supports_image=image,
                provider=by_id[model_id],
            )
            for model_id in (PRIMARY, BACKUP)
        ],
        stages={stage: list(model_ids)},
    )


# ---------------------------------------------------------------------------
# The async router matches the synchronous one
# ---------------------------------------------------------------------------


def _outcomes(model, deadline_seconds=None):
    if model.id == BACKUP:
        return "ok"
    raise RuntimeError("402 Payment Required")


async def _async_outcomes(model, deadline_seconds=None):
    return _outcomes(model, deadline_seconds)


def test_the_async_router_decides_exactly_what_the_sync_one_decides():
    chain = [_model(PRIMARY), _model(BACKUP)]

    sync = run_with_fallback(chain, _outcomes)
    api = asyncio.run(async_run_with_fallback(chain, _async_outcomes))

    assert (api.succeeded, api.value) == (sync.succeeded, sync.value)
    assert api.model.id == sync.model.id
    assert api.fallback_from == sync.fallback_from
    assert [item.model_id for item in api.failures] == [
        item.model_id for item in sync.failures
    ]


def test_the_async_router_honours_the_same_budget_rule():
    clock = iter([0.0, 0.0, 240.0 - MIN_REMAINING_SECONDS + 1.0])

    async def always_fails(model, deadline_seconds=None):
        raise RuntimeError("boom")

    result = asyncio.run(
        async_run_with_fallback(
            [_model(PRIMARY), _model(BACKUP)],
            always_fails,
            budget_seconds=240.0,
            monotonic=lambda: next(clock),
        )
    )

    assert result.skipped_for_budget == (BACKUP,)


def test_the_async_router_does_not_change_model_when_classify_refuses():
    tried: list[str] = []

    async def attempt(model, deadline_seconds=None):
        tried.append(model.id)
        raise RuntimeError("local failure")

    result = asyncio.run(
        async_run_with_fallback(
            [_model(PRIMARY), _model(BACKUP)],
            attempt,
            classify=lambda exc: False,
        )
    )

    assert tried == [PRIMARY]
    assert result.succeeded is False


# ---------------------------------------------------------------------------
# context_resolution
# ---------------------------------------------------------------------------


def _context_inputs(factory):
    from telegram_kol_research.context_resolution import (
        ContextProviderResult,  # noqa: F401  (imported for the test's vocabulary)
    )

    with factory() as session:
        row = RawMessage(chat_id=7, message_id=11, text="BTC 现价出局")
        session.add(row)
        session.commit()
        return int(row.id)


def _context_decision_json() -> str:
    return json.dumps(
        {
            "decision": "hold",
            "target_thread_ids": [],
            "management_action": None,
            "confidence": 0.4,
            "supporting_message_ids": [],
            "opposing_message_ids": [],
            "conflict_types": [],
            "risk_reducing_fanout_allowed": False,
            "reanalysis_triggers": [],
            "reason": "上下文不足",
        },
        ensure_ascii=False,
    )


def _resolve_context(factory, raw_id, config, caller):
    from telegram_kol_research.context_resolution import resolve_contextual_strategy

    return resolve_contextual_strategy(
        factory,
        raw_message_id=raw_id,
        ai_recognition_config=config,
        evidence={},
        context_window={},
        candidates=[],
        first_pass_payload={},
        exchange_state={},
    ) if caller is None else resolve_contextual_strategy(
        factory,
        raw_message_id=raw_id,
        ai_recognition_config=config,
        evidence={},
        context_window={},
        candidates=[],
        first_pass_payload={},
        exchange_state={},
        model_caller=caller,
    )


def test_context_resolution_chain_is_the_stage_binding():
    from telegram_kol_research.context_resolution import resolve_context_model_chain

    chain = resolve_context_model_chain(
        _chain_config("context_resolution", PRIMARY, BACKUP)
    )

    assert [item.id for item in chain] == [PRIMARY, BACKUP]


def test_context_resolution_without_a_v2_table_keeps_the_old_rule():
    from telegram_kol_research.context_resolution import (
        _select_provider,
        resolve_context_model_chain,
    )

    config = AiRecognitionConfig(
        text_provider=AiProviderConfig(
            base_url="https://api.deepseek.example", model="deepseek-review"
        ),
        ai_models=[
            AiModelConfig(
                id="other",
                label="Other",
                base_url="https://other.example",
                model="other-model",
                supports_text=True,
            )
        ],
        context_resolution_model_id="other",
    )

    assert [item.model for item in resolve_context_model_chain(config)] == [
        "other-model"
    ]
    assert _select_provider(config).model == "other-model"

    plain = AiRecognitionConfig(
        text_provider=AiProviderConfig(
            base_url="https://api.deepseek.example", model="deepseek-review"
        )
    )
    assert _select_provider(plain).model == "deepseek-review"


def test_context_resolution_falls_back_and_records_the_answering_model(tmp_path):
    from telegram_kol_research.context_resolution import ContextProviderResult
    from telegram_kol_research.models import ContextResolutionAttempt

    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _context_inputs(factory)
    seen: list[str] = []

    def caller(*, provider, system_prompt, request_payload):
        seen.append(provider.model)
        if provider.model == PRIMARY:
            raise httpx.ConnectError("primary is unreachable")
        return ContextProviderResult(content=_context_decision_json(), usage=None)

    decision = _resolve_context(
        factory,
        raw_id,
        _chain_config("context_resolution", PRIMARY, BACKUP),
        caller,
    )

    assert decision.decision == "hold"
    assert seen == [PRIMARY, BACKUP]
    with factory() as session:
        rows = session.query(ContextResolutionAttempt).all()
    assert [row.model for row in rows] == [BACKUP]
    assert [row.status for row in rows] == ["completed"]


def test_context_resolution_single_model_chain_is_unchanged(tmp_path):
    from telegram_kol_research.context_resolution import ContextProviderResult
    from telegram_kol_research.models import ContextResolutionAttempt

    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _context_inputs(factory)

    decision = _resolve_context(
        factory,
        raw_id,
        _chain_config("context_resolution", PRIMARY),
        lambda **kwargs: ContextProviderResult(
            content=_context_decision_json(), usage=None
        ),
    )

    assert decision.decision == "hold"
    with factory() as session:
        rows = session.query(ContextResolutionAttempt).all()
    assert [(row.model, row.status) for row in rows] == [(PRIMARY, "completed")]


def test_context_resolution_changes_model_for_a_body_that_will_not_decode(tmp_path):
    from telegram_kol_research.context_resolution import ContextProviderResult

    factory = create_session_factory(tmp_path / "research.db")
    raw_id = _context_inputs(factory)
    seen: list[str] = []

    def caller(*, provider, system_prompt, request_payload):
        seen.append(provider.model)
        if provider.model == PRIMARY:
            return ContextProviderResult(content="not json at all", usage=None)
        return ContextProviderResult(content=_context_decision_json(), usage=None)

    decision = _resolve_context(
        factory,
        raw_id,
        _chain_config("context_resolution", PRIMARY, BACKUP),
        caller,
    )

    assert decision.decision == "hold"
    assert seen == [PRIMARY, BACKUP]


# ---------------------------------------------------------------------------
# semantic_review
# ---------------------------------------------------------------------------


def _review_payload() -> dict:
    return {
        "independent_action": {
            "action_type": "none",
            "target_lifecycle_id": None,
            "symbol": None,
            "side": None,
            "stop_loss": None,
            "take_profit": None,
            "management_action": None,
        },
        "evidence": [],
        "conflict_types": [],
        "material_disagreement": False,
        "suggested_severity": "none",
        "confidence": 0.9,
        "reason": "reviewer reason",
    }


def _seed_review_inputs(factory):
    from telegram_kol_research.models import RecognitionDecision

    payload = {
        "recognition_result": "非策略",
        "reason": "只是观点",
        "strategy": {},
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "input_reading": {"observed_text": "BTC short", "image_quality": "none"},
        "confidence": 0.4,
    }
    with factory() as session:
        raw = RawMessage(chat_id=1, message_id=2, text="BTC short")
        session.add(raw)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="mimo",
                authoritative_status="非策略",
                authoritative_payload_json=json.dumps(payload),
                agreement_status="pending",
                differences_json="[]",
                comparison_status="running",
            )
        )
        session.commit()
        return int(raw.id)


def test_semantic_review_falls_back_and_records_the_answering_model(tmp_path):
    from telegram_kol_research.prompt_defaults import seed_default_prompt_registry
    from telegram_kol_research.semantic_disagreement_review import (
        run_deepseek_semantic_review,
    )

    factory = create_session_factory(tmp_path / "research.db")
    config = _chain_config("semantic_review", PRIMARY, BACKUP)
    seed_default_prompt_registry(factory, config)
    raw_id = _seed_review_inputs(factory)
    seen: list[str] = []

    def requester(*, url, json, headers, timeout):
        seen.append(json["model"])
        if json["model"] == PRIMARY:
            raise httpx.ConnectError("primary is unreachable")
        return {
            "choices": [
                {"message": {"content": __import__("json").dumps(_review_payload())}}
            ]
        }

    run = run_deepseek_semantic_review(
        factory,
        raw_message_id=raw_id,
        config=config,
        requester=requester,
    )

    assert seen == [PRIMARY, BACKUP]
    assert run.model == BACKUP


def test_semantic_review_single_model_chain_is_unchanged(tmp_path):
    from telegram_kol_research.prompt_defaults import seed_default_prompt_registry
    from telegram_kol_research.semantic_disagreement_review import (
        run_deepseek_semantic_review,
    )

    factory = create_session_factory(tmp_path / "research.db")
    config = _chain_config("semantic_review", PRIMARY)
    seed_default_prompt_registry(factory, config)
    raw_id = _seed_review_inputs(factory)

    run = run_deepseek_semantic_review(
        factory,
        raw_message_id=raw_id,
        config=config,
        requester=lambda **kwargs: {
            "choices": [{"message": {"content": json.dumps(_review_payload())}}]
        },
    )

    assert run.model == PRIMARY


def test_semantic_review_without_a_v2_table_keeps_the_text_provider(tmp_path):
    from telegram_kol_research.semantic_disagreement_review import (
        resolve_semantic_review_chain,
    )

    config = AiRecognitionConfig(
        text_provider=AiProviderConfig(
            base_url="https://api.deepseek.example", model="deepseek-review"
        )
    )

    assert [item.model for item in resolve_semantic_review_chain(config)] == [
        "deepseek-review"
    ]
    assert resolve_semantic_review_chain(AiRecognitionConfig()) == []


# ---------------------------------------------------------------------------
# strategy_alert
# ---------------------------------------------------------------------------


def _alert_config():
    from telegram_kol_research.strategy_alerts import StrategyAlertConfig

    return StrategyAlertConfig(
        llm_base_url="http://127.0.0.1:8317",
        llm_api_key="env-key",
        llm_model="env-model",
        timeout_seconds=30.0,
        bot_token="token",
        alert_chat_id="42",
    )


def test_an_unbound_strategy_alert_keeps_the_environment_model():
    from telegram_kol_research.strategy_alerts import resolve_strategy_alert_chain

    config = _alert_config()

    chain = resolve_strategy_alert_chain(config, AiRecognitionConfig())

    assert len(chain) == 1
    assert chain[0].alert_config is config
    assert chain[0].model == "env-model"


def test_a_bound_strategy_alert_uses_the_chain_and_keeps_the_bot_settings():
    from telegram_kol_research.strategy_alerts import resolve_strategy_alert_chain

    chain = resolve_strategy_alert_chain(
        _alert_config(), _chain_config("strategy_alert", PRIMARY, BACKUP)
    )

    assert [item.model for item in chain] == [PRIMARY, BACKUP]
    assert chain[0].alert_config.llm_base_url == f"https://{PRIMARY}.example.com/v1"
    assert chain[0].alert_config.llm_api_key == f"{PRIMARY}-key"
    # The destination and thresholds have nothing to do with which model answers.
    assert chain[1].alert_config.bot_token == "token"
    assert chain[1].alert_config.alert_chat_id == "42"


def test_an_unreadable_ai_config_does_not_lose_the_alert():
    from telegram_kol_research.strategy_alerts import _load_ai_config_for_alerts

    def broken():
        raise OSError("unreadable")

    assert _load_ai_config_for_alerts(broken) is None


# ---------------------------------------------------------------------------
# research_chat
# ---------------------------------------------------------------------------


def _proxy_config():
    from telegram_kol_research.llm_chat import LLMProxyConfig

    return LLMProxyConfig(
        base_url="http://127.0.0.1:8317",
        api_key="env-key",
        model="env-model",
        timeout_seconds=60.0,
        egress_socket_path="/run/egress.sock",
    )


def test_an_unbound_research_chat_keeps_the_environment_proxy():
    from telegram_kol_research.llm_chat import resolve_research_chat_chain

    config = _proxy_config()

    chain = resolve_research_chat_chain(config, AiRecognitionConfig())

    assert len(chain) == 1
    assert chain[0].proxy_config is config


def test_a_bound_research_chat_uses_the_chain_and_keeps_the_egress_socket():
    from telegram_kol_research.llm_chat import resolve_research_chat_chain

    chain = resolve_research_chat_chain(
        _proxy_config(), _chain_config("research_chat", PRIMARY, BACKUP)
    )

    assert [item.model for item in chain] == [PRIMARY, BACKUP]
    assert chain[0].proxy_config.base_url == f"https://{PRIMARY}.example.com/v1"
    assert chain[1].proxy_config.egress_socket_path == "/run/egress.sock"


# ---------------------------------------------------------------------------
# batch_* and the prompt centre
# ---------------------------------------------------------------------------


def test_the_batch_stages_read_their_own_chain_heads():
    from telegram_kol_research.message_recognition import (
        _batch_image_provider,
        _batch_text_provider,
    )

    text = _chain_config("batch_text_recognition", BACKUP, PRIMARY)
    image = _chain_config("batch_image_recognition", BACKUP, image=True)

    assert _batch_text_provider(text).model == BACKUP
    assert _batch_image_provider(image).model == BACKUP


def test_a_config_without_a_v2_table_keeps_the_v1_providers():
    from telegram_kol_research.message_recognition import (
        _batch_image_provider,
        _batch_text_provider,
    )

    config = AiRecognitionConfig(
        text_provider=AiProviderConfig(
            base_url="https://text.example", model="text-model"
        ),
        image_provider=AiProviderConfig(
            base_url="https://image.example", model="glm-ocr"
        ),
    )

    assert _batch_text_provider(config).model == "text-model"
    assert _batch_image_provider(config).model == "glm-ocr"


def test_glm_ocr_is_still_decided_by_the_chain_heads_model_name():
    from telegram_kol_research.message_recognition import (
        _batch_image_provider,
        _is_glm_ocr_model,
    )

    ocr = AiRecognitionConfig(
        providers=[AiProvider(id="zhipu", base_url="https://open.bigmodel.cn/api/paas/v4")],
        models=[
            AiModel(
                id="glm-ocr",
                provider_id="zhipu",
                model="glm-ocr",
                supports_text=False,
                supports_image=True,
                provider=AiProvider(
                    id="zhipu", base_url="https://open.bigmodel.cn/api/paas/v4"
                ),
            )
        ],
        stages={"batch_image_recognition": ["glm-ocr"]},
    )

    assert _is_glm_ocr_model(_batch_image_provider(ocr).model) is True


def test_the_prompt_centre_deepseek_test_follows_the_batch_text_chain():
    from telegram_kol_research.prompt_testing import _model_name

    config = _chain_config("batch_text_recognition", BACKUP, PRIMARY)

    assert _model_name(config, "deepseek") == BACKUP


def test_stage_head_provider_falls_back_to_the_v1_field():
    legacy = AiProviderConfig(base_url="https://legacy.example", model="legacy")

    assert (
        stage_head_provider(
            AiRecognitionConfig(), "batch_text_recognition", legacy=legacy
        )
        is legacy
    )
    assert (
        stage_head_provider(
            AiRecognitionConfig(), "batch_text_recognition"
        ).is_configured
        is False
    )


# ---------------------------------------------------------------------------
# The health tick reads the chain head once per file version
# ---------------------------------------------------------------------------


def test_the_chain_head_is_not_reparsed_on_every_tick(tmp_path, monkeypatch):
    from telegram_kol_research import mimo_provider_health as health

    path = tmp_path / "ai_recognition.yaml"
    path.write_text(
        Path("config/ai_recognition.example.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    health._CHAIN_HEAD_CACHE.clear()
    loads: list[int] = []
    original = health.resolve_chain_head_model

    import telegram_kol_research.ai_recognition_config as config_module

    real_load = config_module.load_ai_recognition_config

    def counted(*args, **kwargs):
        loads.append(1)
        return real_load(*args, **kwargs)

    monkeypatch.setattr(config_module, "load_ai_recognition_config", counted)

    with pytest.warns(DeprecationWarning):
        first = original(ai_recognition_config_path=path)
    second = original(ai_recognition_config_path=path)

    assert first == second == "mimo-v2.5"
    assert len(loads) == 1

    # A save changes the file, and the next tick sees the new head.
    path.write_text(
        path.read_text(encoding="utf-8") + "\n# saved again\n", encoding="utf-8"
    )
    with pytest.warns(DeprecationWarning):
        original(ai_recognition_config_path=path)
    assert len(loads) == 2
    health._CHAIN_HEAD_CACHE.clear()
