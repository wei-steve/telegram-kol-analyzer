import json

import pytest

from telegram_kol_research.ai_recognition_config import (
    AiModel,
    AiProvider,
    AiRecognitionConfig,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    AiPromptTestRun,
    ExecutionBinding,
    MediaAsset,
    RawMessage,
    SignalCandidate,
    StrategyAlert,
    StrategyLifecycle,
)
from telegram_kol_research.prompt_registry import save_prompt_draft
from telegram_kol_research.prompt_testing import (
    PROMPT_TEST_STAGE_BY_PROMPT_KEY,
    PromptTestModelError,
    prompt_test_models,
    prompt_test_stage_key,
    resolve_prompt_test_model,
    run_prompt_draft_test,
)
from telegram_kol_research.prompt_defaults import (
    DEFAULT_MIMO_VISION_PROMPT,
    DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
    MIMO_VISION_PROMPT,
    SHARED_TRADING_PROMPT,
    seed_default_prompt_registry,
)


def _model(model_id: str, *, supports_image: bool = True) -> AiModel:
    return AiModel(
        id=model_id,
        provider_id="house",
        model=model_id,
        label=model_id,
        supports_text=True,
        supports_image=supports_image,
    )


def _config(*model_ids: str, models: list[AiModel] | None = None) -> AiRecognitionConfig:
    """A config whose authoritative stage is bound to the given model ids."""

    entries = models if models is not None else [_model(item) for item in model_ids]
    return AiRecognitionConfig(
        providers=[AiProvider(id="house", base_url="https://house.example/v1")],
        models=entries,
        stages={"authoritative_recognition": list(model_ids)},
    )


def _payload(event_type: str) -> dict:
    return {
        "recognition_result": "非策略",
        "strategy": {},
        "lifecycle_event": {"event_type": event_type, "confidence": 0.9},
        "input_reading": {"observed_text": "出局", "image_quality": "none"},
        "confidence": 0.9,
    }


def test_draft_recognition_test_has_no_production_side_effects(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_default_prompt_registry(factory, AiRecognitionConfig())
    with factory() as session:
        message = RawMessage(chat_id=88, message_id=7, text="现价出局")
        session.add(message)
        session.commit()
        raw_id = message.id
    detail = save_prompt_draft(
        factory,
        SHARED_TRADING_PROMPT,
        content=DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT + "\nDRAFT_MARKER",
        change_note="test exit wording",
    )

    def caller(**kwargs):
        return _payload("exit_position" if "DRAFT_MARKER" in kwargs["system_prompt"] else "none")

    production_models = (SignalCandidate, StrategyLifecycle, ExecutionBinding, StrategyAlert)
    with factory() as session:
        before = [session.query(model).count() for model in production_models]

    result = run_prompt_draft_test(
        factory,
        prompt_key=SHARED_TRADING_PROMPT,
        draft_version_id=detail.draft_version.id,
        raw_message_id=raw_id,
        ai_recognition_config=_config("mimo-v2.5"),
        media_root=tmp_path,
        model_caller=caller,
    )

    with factory() as session:
        after = [session.query(model).count() for model in production_models]
        stored = session.query(AiPromptTestRun).one()
    assert after == before
    assert result.differences == ["lifecycle_event.event_type"]
    assert json.loads(stored.active_result_json)["lifecycle_event"]["event_type"] == "none"
    assert json.loads(stored.draft_result_json)["lifecycle_event"]["event_type"] == "exit_position"
    assert stored.duration_ms >= 0


def test_draft_test_rejects_stale_draft_id(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_default_prompt_registry(factory, AiRecognitionConfig())
    with factory() as session:
        message = RawMessage(chat_id=1, message_id=1, text="hello")
        session.add(message)
        session.commit()
        raw_id = message.id

    with pytest.raises(ValueError, match="draft version changed"):
        run_prompt_draft_test(
            factory,
            prompt_key=SHARED_TRADING_PROMPT,
            draft_version_id=999,
            raw_message_id=raw_id,
            ai_recognition_config=_config("mimo-v2.5"),
            media_root=tmp_path,
            model_caller=lambda **_: _payload("none"),
        )


def test_draft_test_stores_model_failure_without_production_writes(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_default_prompt_registry(factory, AiRecognitionConfig())
    with factory() as session:
        message = RawMessage(chat_id=1, message_id=1, text="hello")
        session.add(message)
        session.commit()
        raw_id = message.id
    detail = save_prompt_draft(
        factory,
        SHARED_TRADING_PROMPT,
        content=DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT + "\nDRAFT",
        change_note="network failure test",
    )

    result = run_prompt_draft_test(
        factory,
        prompt_key=SHARED_TRADING_PROMPT,
        draft_version_id=detail.draft_version.id,
        raw_message_id=raw_id,
        ai_recognition_config=_config("mimo-v2.5"),
        media_root=tmp_path,
        model_caller=lambda **_: (_ for _ in ()).throw(RuntimeError("proxy offline")),
    )

    assert result.error_message == "proxy offline"
    with factory() as session:
        assert session.query(AiPromptTestRun).one().status == "failed"


def test_draft_test_rejects_parseable_but_invalid_model_payload(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_default_prompt_registry(factory, AiRecognitionConfig())
    with factory() as session:
        message = RawMessage(chat_id=1, message_id=2, text="hello")
        session.add(message)
        session.commit()
        raw_id = message.id
    detail = save_prompt_draft(
        factory,
        SHARED_TRADING_PROMPT,
        content=DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT + "\nDRAFT",
        change_note="invalid payload test",
    )

    result = run_prompt_draft_test(
        factory,
        prompt_key=SHARED_TRADING_PROMPT,
        draft_version_id=detail.draft_version.id,
        raw_message_id=raw_id,
        ai_recognition_config=_config("mimo-v2.5"),
        media_root=tmp_path,
        model_caller=lambda **_: {"unexpected": "json"},
    )

    assert "invalid recognition_result" in result.error_message
    with factory() as session:
        assert session.query(AiPromptTestRun).one().status == "failed"


def test_mimo_vision_draft_test_forwards_readable_image_assets(tmp_path):
    media_root = tmp_path / "media"
    image_path = media_root / "group" / "chart.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image-bytes")
    factory = create_session_factory(tmp_path / "research.db")
    seed_default_prompt_registry(factory, AiRecognitionConfig())
    with factory() as session:
        message = RawMessage(chat_id=1, message_id=1, text="chart")
        session.add(message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=message.id,
                kind="photo",
                mime_type="image/jpeg",
                local_path="group/chart.jpg",
            )
        )
        session.commit()
        raw_id = message.id
    detail = save_prompt_draft(
        factory,
        MIMO_VISION_PROMPT,
        content=DEFAULT_MIMO_VISION_PROMPT + "\nDRAFT_IMAGE_MARKER",
        change_note="image reading regression",
    )
    captured = []

    def caller(**kwargs):
        captured.append(
            {
                "system_prompt": kwargs["system_prompt"],
                "media_paths": [asset.local_path for asset in kwargs["media_assets"]],
            }
        )
        return _payload("none")

    run_prompt_draft_test(
        factory,
        prompt_key=MIMO_VISION_PROMPT,
        draft_version_id=detail.draft_version.id,
        raw_message_id=raw_id,
        ai_recognition_config=_config("mimo-v2.5"),
        media_root=media_root,
        model_caller=caller,
    )

    assert len(captured) == 2
    assert captured[0]["media_paths"] == ["group/chart.jpg"]
    assert "DRAFT_IMAGE_MARKER" not in captured[0]["system_prompt"]
    assert "DRAFT_IMAGE_MARKER" in captured[1]["system_prompt"]


def test_mimo_vision_draft_test_rejects_unreadable_image(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    seed_default_prompt_registry(factory, AiRecognitionConfig())
    with factory() as session:
        message = RawMessage(chat_id=1, message_id=1, text=None)
        session.add(message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=message.id,
                kind="photo",
                local_path="missing.jpg",
            )
        )
        session.commit()
        raw_id = message.id
    detail = save_prompt_draft(
        factory,
        MIMO_VISION_PROMPT,
        content=DEFAULT_MIMO_VISION_PROMPT + "\nDRAFT",
        change_note="unreadable image",
    )

    with pytest.raises(ValueError, match="unavailable or unreadable"):
        run_prompt_draft_test(
            factory,
            prompt_key=MIMO_VISION_PROMPT,
            draft_version_id=detail.draft_version.id,
            raw_message_id=raw_id,
            ai_recognition_config=_config("mimo-v2.5"),
            media_root=tmp_path,
            model_caller=lambda **_: _payload("none"),
        )


def _seeded_factory(tmp_path, *, text="现价出局"):
    factory = create_session_factory(tmp_path / "research.db")
    seed_default_prompt_registry(factory, AiRecognitionConfig())
    with factory() as session:
        message = RawMessage(chat_id=5, message_id=5, text=text)
        session.add(message)
        session.commit()
        return factory, message.id


def _shared_draft(factory):
    return save_prompt_draft(
        factory,
        SHARED_TRADING_PROMPT,
        content=DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT + "\nDRAFT",
        change_note="stage-bound model test",
    ).draft_version.id


def test_both_testable_prompts_follow_the_authoritative_stage():
    """The vision prompt is a supplement to the shared one, not its own stage.

    ``recognition_experiments`` composes both into the one system prompt it
    sends down ``authoritative_recognition``; nothing else sends either of
    them to a model on the production path.
    """

    assert PROMPT_TEST_STAGE_BY_PROMPT_KEY == {
        SHARED_TRADING_PROMPT: "authoritative_recognition",
        MIMO_VISION_PROMPT: "authoritative_recognition",
    }
    assert prompt_test_stage_key(SHARED_TRADING_PROMPT) == "authoritative_recognition"
    assert prompt_test_stage_key(MIMO_VISION_PROMPT) == "authoritative_recognition"


def test_unknown_prompt_key_has_no_test_stage():
    with pytest.raises(ValueError, match="trading prompts only"):
        prompt_test_stage_key("research.chat.group")


def test_default_test_model_is_the_stage_chain_head():
    config = _config("head-model", "second-model")
    chosen = resolve_prompt_test_model(config, prompt_key=SHARED_TRADING_PROMPT)
    assert chosen.id == "head-model"
    assert [model.id for model in prompt_test_models(config, SHARED_TRADING_PROMPT)] == [
        "head-model",
        "second-model",
    ]


def test_a_named_member_of_the_chain_may_be_tested_instead_of_the_head():
    config = _config("head-model", "second-model")
    chosen = resolve_prompt_test_model(
        config, prompt_key=SHARED_TRADING_PROMPT, model_id="second-model"
    )
    assert chosen.id == "second-model"


def test_a_model_outside_the_chain_is_refused():
    config = _config("head-model")
    with pytest.raises(PromptTestModelError, match="not a usable member"):
        resolve_prompt_test_model(
            config, prompt_key=SHARED_TRADING_PROMPT, model_id="some-other-model"
        )


def test_an_empty_stage_chain_refuses_before_anything_is_recorded(tmp_path):
    """No binding means there is nothing to test, and nothing is stored.

    The old code fell back to the literal string ``mimo-v2.5`` here and wrote
    a failed run naming a model nobody had configured.
    """

    factory, raw_id = _seeded_factory(tmp_path)
    draft_id = _shared_draft(factory)
    empty = AiRecognitionConfig(
        providers=[AiProvider(id="house", base_url="https://house.example/v1")],
        models=[_model("head-model")],
        stages={"authoritative_recognition": []},
    )

    with pytest.raises(PromptTestModelError, match="has no usable model"):
        run_prompt_draft_test(
            factory,
            prompt_key=SHARED_TRADING_PROMPT,
            draft_version_id=draft_id,
            raw_message_id=raw_id,
            ai_recognition_config=empty,
            media_root=tmp_path,
            model_caller=lambda **_: _payload("none"),
        )

    assert prompt_test_models(empty, SHARED_TRADING_PROMPT) == []
    with factory() as session:
        assert session.query(AiPromptTestRun).count() == 0


def test_a_disabled_provider_empties_the_chain_without_losing_the_binding():
    config = AiRecognitionConfig(
        providers=[
            AiProvider(id="house", base_url="https://house.example/v1", enabled=False)
        ],
        models=[_model("head-model")],
        stages={"authoritative_recognition": ["head-model"]},
    )
    assert prompt_test_models(config, SHARED_TRADING_PROMPT) == []
    with pytest.raises(PromptTestModelError, match="has no usable model"):
        resolve_prompt_test_model(config, prompt_key=SHARED_TRADING_PROMPT)


def test_the_image_prompt_accepts_an_image_model_and_refuses_a_text_only_one():
    """One config, one difference: whether the model can read an image.

    Both models are bound to the same stage, so the refusal cannot be blamed
    on the binding -- it is the capability, which is what replaced "it has to
    be MiMo".
    """

    config = AiRecognitionConfig(
        providers=[AiProvider(id="house", base_url="https://house.example/v1")],
        models=[
            _model("sees-images", supports_image=True),
            _model("text-only", supports_image=False),
        ],
        stages={"authoritative_recognition": ["sees-images", "text-only"]},
    )

    chosen = resolve_prompt_test_model(
        config, prompt_key=MIMO_VISION_PROMPT, model_id="sees-images"
    )
    assert chosen.id == "sees-images"

    with pytest.raises(PromptTestModelError, match="cannot read images"):
        resolve_prompt_test_model(
            config, prompt_key=MIMO_VISION_PROMPT, model_id="text-only"
        )

    # The page never offers what the resolver would then refuse.
    assert [model.id for model in prompt_test_models(config, MIMO_VISION_PROMPT)] == [
        "sees-images"
    ]


def test_rebinding_the_stage_moves_the_test_to_the_new_head(tmp_path):
    """The point of the change: no name in the code decides which model runs."""

    factory, raw_id = _seeded_factory(tmp_path)
    draft_id = _shared_draft(factory)
    seen: list[str] = []

    def caller(**kwargs):
        seen.append(kwargs["model"].id)
        return _payload("none")

    def run(config):
        return run_prompt_draft_test(
            factory,
            prompt_key=SHARED_TRADING_PROMPT,
            draft_version_id=draft_id,
            raw_message_id=raw_id,
            ai_recognition_config=config,
            media_root=tmp_path,
            model_caller=caller,
        )

    before = run(_config("old-head", "new-head"))
    after = run(_config("new-head", "old-head"))

    assert before.model_id == "old-head"
    assert after.model_id == "new-head"
    assert seen == ["old-head", "old-head", "new-head", "new-head"]
    with factory() as session:
        rows = session.query(AiPromptTestRun).order_by(AiPromptTestRun.id.asc()).all()
        assert [row.model for row in rows] == ["old-head", "new-head"]


def test_the_run_row_records_the_stage_binding_and_the_model_that_ran(tmp_path):
    """``model_kind`` holds the stage key, ``model`` the model that answered.

    Old rows held ``mimo`` / ``deepseek``, which already meant "the head of
    one stage's chain"; the stage key is that same fact stated precisely, and
    it stays true when the bound model changes.
    """

    factory, raw_id = _seeded_factory(tmp_path)
    draft_id = _shared_draft(factory)

    result = run_prompt_draft_test(
        factory,
        prompt_key=SHARED_TRADING_PROMPT,
        draft_version_id=draft_id,
        raw_message_id=raw_id,
        model_id="second-model",
        ai_recognition_config=_config("head-model", "second-model"),
        media_root=tmp_path,
        model_caller=lambda **_: _payload("none"),
    )

    assert result.stage_key == "authoritative_recognition"
    assert result.model_id == "second-model"
    with factory() as session:
        row = session.query(AiPromptTestRun).one()
    assert row.model_kind == "authoritative_recognition"
    assert row.model == "second-model"


def test_the_shared_prompt_test_sends_the_same_two_parts_production_sends(tmp_path):
    """Composition is A + B, which is what the authoritative stage composes."""

    factory, raw_id = _seeded_factory(tmp_path)
    draft_id = _shared_draft(factory)
    prompts: list[str] = []

    run_prompt_draft_test(
        factory,
        prompt_key=SHARED_TRADING_PROMPT,
        draft_version_id=draft_id,
        raw_message_id=raw_id,
        ai_recognition_config=_config("head-model"),
        media_root=tmp_path,
        model_caller=lambda **kwargs: (
            prompts.append(kwargs["system_prompt"]) or _payload("none")
        ),
    )

    assert len(prompts) == 2
    for text in prompts:
        assert DEFAULT_MIMO_VISION_PROMPT.strip()[:24] in text
    assert "DRAFT" not in prompts[0]
    assert "DRAFT" in prompts[1]
