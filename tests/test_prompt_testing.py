import json

import pytest

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
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
from telegram_kol_research.prompt_defaults import (
    DEFAULT_MIMO_VISION_PROMPT,
    MIMO_VISION_PROMPT,
    DEFAULT_SHARED_TRADING_ANALYSIS_PROMPT,
    SHARED_TRADING_PROMPT,
    seed_default_prompt_registry,
)
from telegram_kol_research.prompt_registry import save_prompt_draft
from telegram_kol_research.prompt_testing import run_prompt_draft_test


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
        model_kind="mimo",
        ai_recognition_config=AiRecognitionConfig(),
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
            model_kind="mimo",
            ai_recognition_config=AiRecognitionConfig(),
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
        model_kind="deepseek",
        ai_recognition_config=AiRecognitionConfig(),
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
        model_kind="mimo",
        ai_recognition_config=AiRecognitionConfig(),
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
        model_kind="mimo",
        ai_recognition_config=AiRecognitionConfig(),
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
            model_kind="mimo",
            ai_recognition_config=AiRecognitionConfig(),
            media_root=tmp_path,
            model_caller=lambda **_: _payload("none"),
        )
