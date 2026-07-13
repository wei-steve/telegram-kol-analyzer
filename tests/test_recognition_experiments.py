import json
from datetime import UTC, datetime

import httpx

from telegram_kol_research.ai_recognition_config import AiModelConfig, AiRecognitionConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import AiPromptInvocation, MediaAsset, MessageRecognition, RawMessage, RecognitionExperiment, SignalCandidate, StrategyLifecycle
from telegram_kol_research.prompt_defaults import MIMO_VISION_PROMPT, SHARED_TRADING_PROMPT
from telegram_kol_research.recognition_experiments import (
    _build_mimo_payload,
    run_mimo_authoritative_for_message,
    run_mimo_direct_for_message,
    run_mimo_direct_experiment,
)


def test_run_mimo_authoritative_for_message_returns_unified_actionable_result(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=9, text="现价62800附近出局")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    payload = {
        "recognition_result": "非策略",
        "reason": "当前消息要求出局",
        "strategy": {},
        "lifecycle_event": {
            "event_type": "exit_position",
            "target_lifecycle_id": 439,
            "symbol": "BTC",
            "side": "short",
            "confidence": 0.95,
            "reason": "现价出局",
        },
        "input_reading": {"observed_text": "现价62800附近出局", "image_quality": "none"},
        "confidence": 0.95,
    }
    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return payload

    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments._call_mimo_direct_model",
        fake_call,
    )

    result = run_mimo_authoritative_for_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(
            ai_models=[
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo",
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="key",
                    model="mimo-v2.5",
                    supports_text=True,
                    supports_image=True,
                )
            ]
        ),
    )

    assert result.status == "非策略"
    assert result.input_kind == "text"
    assert result.model == "mimo-v2.5"
    assert result.payload["lifecycle_event"]["event_type"] == "exit_position"
    assert result.is_actionable is True
    assert result.prompt_versions.keys() == {
        SHARED_TRADING_PROMPT,
        MIMO_VISION_PROMPT,
    }
    assert "统一交易分析" not in captured["prompt"]
    assert "新开仓识别" in captured["prompt"]
    assert "图片与图文补充规则" in captured["prompt"]
    with session_factory() as session:
        invocation = session.query(AiPromptInvocation).one()
        assert invocation.feature == "message_recognition"
        assert json.loads(invocation.prompt_versions_json) == result.prompt_versions


def test_run_mimo_authoritative_for_message_contains_failure(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=10, text="BTC short")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    def fail(**kwargs):
        raise TimeoutError("mimo timeout")

    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments._call_mimo_direct_model",
        fail,
    )
    result = run_mimo_authoritative_for_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(
            ai_models=[
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo",
                    base_url="https://api.xiaomimimo.com/v1",
                    model="mimo-v2.5",
                )
            ]
        ),
    )

    assert result.status == "识别失败"
    assert result.is_actionable is False
    assert "mimo timeout" in (result.error_message or "")


def test_run_mimo_authoritative_includes_recent_context_and_active_strategies(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(RawMessage(chat_id=100, message_id=7, text="BTC short 63500"))
        current = RawMessage(chat_id=100, message_id=8, text="现价出局")
        session.add(current)
        session.flush()
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=7,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 13, 1, 0, tzinfo=UTC),
                entered_at=datetime(2026, 7, 13, 1, 1, tzinfo=UTC),
            )
        )
        session.commit()
        raw_id = current.id

    captured = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return {
            "recognition_result": "非策略",
            "reason": "exit",
            "strategy": {},
            "lifecycle_event": {"event_type": "none", "confidence": 0.0},
            "input_reading": {"observed_text": "现价出局", "image_quality": "none"},
            "confidence": 0.8,
        }

    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments._call_mimo_direct_model",
        fake_call,
    )
    run_mimo_authoritative_for_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(
            ai_models=[
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo",
                    base_url="https://api.xiaomimimo.com/v1",
                    model="mimo-v2.5",
                )
            ]
        ),
    )

    context_text = captured["context_text"]
    assert "Recent context" in context_text
    assert "BTC short 63500" in context_text
    assert "Active strategies" in context_text
    assert '"symbol": "BTC"' in context_text
    assert "api_key" not in context_text


def test_build_mimo_payload_uses_raw_image_without_ocr_text(tmp_path):
    media_root = tmp_path / "media"
    image_path = media_root / "group" / "1.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"fake-image")
    raw_message = RawMessage(
        id=1,
        chat_id=100,
        message_id=2,
        sender_name="Trader",
        text="caption",
    )
    media_asset = MediaAsset(
        raw_message_id=1,
        kind="photo",
        local_path="group/1.jpg",
        ocr_text="OLD OCR TEXT SHOULD NOT BE SENT",
    )

    payload = _build_mimo_payload(
        raw_message=raw_message,
        media_assets=[media_asset],
        model="mimo-v2.5",
        media_root=media_root,
    )

    user_content = payload["messages"][1]["content"]
    assert isinstance(user_content, list)
    assert user_content[0]["type"] == "text"
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "OLD OCR TEXT SHOULD NOT BE SENT" not in json.dumps(payload)


def test_authoritative_mimo_fails_closed_when_declared_image_is_missing(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=9, text="BTC short caption")
        session.add(raw)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw.id,
                kind="photo",
                local_path="missing/9.jpg",
            )
        )
        session.commit()
        raw_id = raw.id

    model_calls: list[dict] = []
    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments._call_mimo_direct_model",
        lambda **kwargs: model_calls.append(kwargs) or {},
    )
    result = run_mimo_authoritative_for_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(
            ai_models=[
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo",
                    base_url="https://api.xiaomimimo.com/v1",
                    model="mimo-v2.5",
                )
            ]
        ),
        media_root=tmp_path / "media",
    )

    assert result.input_kind == "text+image"
    assert result.status == "识别失败"
    assert "unavailable or unreadable" in result.error_message
    assert model_calls == []


def test_build_mimo_payload_skips_empty_images_and_uses_configured_prompt(tmp_path):
    media_root = tmp_path / "media"
    empty_path = media_root / "group" / "empty.jpg"
    valid_path = media_root / "group" / "valid.jpg"
    empty_path.parent.mkdir(parents=True)
    empty_path.write_bytes(b"")
    valid_path.write_bytes(b"fake-image")
    raw_message = RawMessage(
        id=1,
        chat_id=100,
        message_id=2,
        sender_name="Trader",
        text="caption",
    )

    payload = _build_mimo_payload(
        raw_message=raw_message,
        media_assets=[
            MediaAsset(raw_message_id=1, kind="photo", local_path="group/empty.jpg"),
            MediaAsset(raw_message_id=1, kind="photo", local_path="group/valid.jpg"),
        ],
        model="mimo-v2.5",
        prompt="custom mimo prompt",
        media_root=media_root,
    )

    assert payload["messages"][0]["content"] == "custom mimo prompt"
    user_content = payload["messages"][1]["content"]
    assert isinstance(user_content, list)
    assert len([part for part in user_content if part["type"] == "image_url"]) == 1


def test_run_mimo_direct_experiment_persists_side_channel_only(tmp_path, monkeypatch):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=100,
                message_id=1,
                sender_name="Trader",
                posted_at=datetime(2026, 6, 1, tzinfo=UTC),
                text="BTC long 68000 SL 67000 TP 70000",
            )
        )
        session.commit()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "recognition_result": "是策略",
                                    "input_reading": {
                                        "observed_text": "BTC long 68000 SL 67000 TP 70000",
                                        "image_quality": "none",
                                    },
                                    "reason": "明确给出新开仓策略",
                                    "strategy": {
                                        "symbol": "BTC",
                                        "side": "long",
                                        "entry": "68000",
                                        "stop_loss": "67000",
                                        "take_profit": "70000",
                                        "leverage": None,
                                        "order_type": None,
                                    },
                                    "confidence": 0.9,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            assert url == "https://api.xiaomimimo.com/v1/chat/completions"
            assert headers["Authorization"] == "Bearer mimo-key"
            assert json["model"] == "mimo-v2.5"
            system_prompt = json["messages"][0]["content"]
            assert "Use MiMo prompt from config." not in system_prompt
            assert "图片与图文补充规则" in system_prompt
            return FakeResponse()

    monkeypatch.setattr("telegram_kol_research.recognition_experiments.httpx.Client", FakeClient)

    stats = run_mimo_direct_experiment(
        session_factory,
        ai_recognition_config=AiRecognitionConfig(
            mimo_direct_prompt="Use MiMo prompt from config.",
            ai_models=[
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo V2.5",
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="mimo-key",
                    model="mimo-v2.5",
                    supports_text=True,
                    supports_image=True,
                )
            ],
        ),
        limit=10,
    )

    assert stats.succeeded == 1
    with session_factory() as session:
        experiment = session.query(RecognitionExperiment).one()
        assert experiment.experiment_name == "mimo_direct_v1"
        assert experiment.model == "mimo-v2.5"
        assert experiment.status == "是策略"
        assert experiment.observed_text == "BTC long 68000 SL 67000 TP 70000"
        assert experiment.confidence == 0.9
        assert session.query(MessageRecognition).count() == 0
        assert session.query(SignalCandidate).count() == 0
        invocation = session.query(AiPromptInvocation).one()
        assert invocation.feature == "recognition_experiment"
        assert set(json.loads(invocation.prompt_versions_json)) == {
            SHARED_TRADING_PROMPT,
            MIMO_VISION_PROMPT,
        }


def test_run_mimo_direct_for_message_persists_text_side_channel(tmp_path, monkeypatch):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=8,
            sender_name="Trader",
            posted_at=datetime(2026, 6, 1, tzinfo=UTC),
            text="SOL short 73 SL 75 TP 70",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "recognition_result": "是策略",
                                    "input_reading": {
                                        "observed_text": "SOL short 73 SL 75 TP 70",
                                        "image_quality": "none",
                                    },
                                    "reason": "MiMo text comparison detected a strategy.",
                                    "strategy": {
                                        "symbol": "SOL",
                                        "side": "short",
                                        "entry": "73",
                                        "stop_loss": "75",
                                        "take_profit": "70",
                                    },
                                    "confidence": 0.88,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            user_content = json["messages"][1]["content"]
            assert isinstance(user_content, str)
            assert "SOL short 73 SL 75 TP 70" in user_content
            return FakeResponse()

    monkeypatch.setattr("telegram_kol_research.recognition_experiments.httpx.Client", FakeClient)

    result = run_mimo_direct_for_message(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            recognition_prompt="Use DeepSeek text rules.",
            mimo_direct_prompt="Use MiMo prompt from config.",
            ai_models=[
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo V2.5",
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="mimo-key",
                    model="mimo-v2.5",
                    supports_text=True,
                    supports_image=True,
                )
            ],
        ),
    )

    assert result is not None
    with session_factory() as session:
        experiment = session.query(RecognitionExperiment).one()
        assert experiment.experiment_name == "mimo_direct_v1"
        assert experiment.input_kind == "text"
        assert experiment.model == "mimo-v2.5"
        assert experiment.status == "是策略"
        assert experiment.observed_text == "SOL short 73 SL 75 TP 70"
        assert session.query(MessageRecognition).count() == 0
        assert session.query(SignalCandidate).count() == 0
        assert session.query(AiPromptInvocation).one().feature == "recognition_experiment"


def test_run_mimo_direct_for_message_omits_empty_strategy_json(tmp_path, monkeypatch):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=9,
            sender_name="Trader",
            posted_at=datetime(2026, 6, 1, tzinfo=UTC),
            text="Join the VIP channel.",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "recognition_result": "\u975e\u7b56\u7565",
                                    "input_reading": {"observed_text": "Join the VIP channel."},
                                    "reason": "Advertisement.",
                                    "strategy": {
                                        "symbol": None,
                                        "side": None,
                                        "entry": None,
                                        "stop_loss": None,
                                        "take_profit": None,
                                    },
                                    "confidence": 0.1,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            return FakeResponse()

    monkeypatch.setattr("telegram_kol_research.recognition_experiments.httpx.Client", FakeClient)

    run_mimo_direct_for_message(
        session_factory,
        raw_message_id=raw_message_id,
        media_root=tmp_path,
        ai_recognition_config=AiRecognitionConfig(
            ai_models=[
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo V2.5",
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="mimo-key",
                    model="mimo-v2.5",
                    supports_text=True,
                    supports_image=True,
                )
            ],
        ),
    )

    with session_factory() as session:
        experiment = session.query(RecognitionExperiment).one()
        assert experiment.status == "\u975e\u7b56\u7565"
        assert experiment.strategy_json is None


def test_run_mimo_direct_for_message_accepts_position_management_status(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    image_path = tmp_path / "eth-position.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=10,
            sender_name="Trader",
            posted_at=datetime(2026, 6, 1, tzinfo=UTC),
            text="\u7b2c\u4e00\u6b62\u76c8\u70b9\u6765\u4e86",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="messagemediaphoto",
                local_path=str(image_path),
                mime_type="image/jpeg",
            )
        )
        session.commit()
        raw_message_id = raw_message.id

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "recognition_result": "\u4ed3\u4f4d\u7ba1\u7406",
                                    "input_reading": {
                                        "observed_text": "\u7b2c\u4e00\u6b62\u76c8\u70b9\u6765\u4e86\uff1bETHUSDT \u591a\u5355\u6301\u4ed3\u6536\u76ca\u622a\u56fe",
                                        "image_quality": "clear",
                                    },
                                    "reason": "\u5df2\u6709 ETH \u591a\u5355\u8fbe\u5230\u7b2c\u4e00\u6b62\u76c8\uff0c\u5c5e\u4e8e\u90e8\u5206\u6b62\u76c8\u7ba1\u7406\u3002",
                                    "strategy": {
                                        "symbol": "ETH",
                                        "side": "long",
                                        "take_profit": "\u7b2c\u4e00\u6b62\u76c8",
                                    },
                                    "confidence": 0.86,
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            system_prompt = json["messages"][0]["content"]
            assert "\u7b2c\u4e00\u6b62\u76c8\u70b9\u6765\u4e86" in system_prompt
            assert "\u4ed3\u4f4d\u7ba1\u7406" in system_prompt
            return FakeResponse()

    monkeypatch.setattr("telegram_kol_research.recognition_experiments.httpx.Client", FakeClient)

    run_mimo_direct_for_message(
        session_factory,
        raw_message_id=raw_message_id,
        media_root=tmp_path,
        ai_recognition_config=AiRecognitionConfig(
            ai_models=[
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo V2.5",
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="mimo-key",
                    model="mimo-v2.5",
                    supports_text=True,
                    supports_image=True,
                )
            ],
        ),
    )

    with session_factory() as session:
        experiment = session.query(RecognitionExperiment).one()
        assert experiment.status == "\u4ed3\u4f4d\u7ba1\u7406"
        assert experiment.input_kind == "text+image"
        assert experiment.strategy_json is not None


def test_run_mimo_direct_experiment_persists_http_error_response_body(tmp_path, monkeypatch):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=100,
                message_id=1,
                sender_name="Trader",
                posted_at=datetime(2026, 6, 1, tzinfo=UTC),
                text="BTC long 68000 SL 67000 TP 70000",
            )
        )
        session.commit()

    class FakeResponse:
        text = '{"error":"bad image"}'

        def raise_for_status(self):
            request = httpx.Request("POST", "https://api.xiaomimimo.com/v1/chat/completions")
            response = httpx.Response(400, request=request, text=self.text)
            raise httpx.HTTPStatusError("bad request", request=request, response=response)

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            return FakeResponse()

    monkeypatch.setattr("telegram_kol_research.recognition_experiments.httpx.Client", FakeClient)

    stats = run_mimo_direct_experiment(
        session_factory,
        ai_recognition_config=AiRecognitionConfig(
            ai_models=[
                AiModelConfig(
                    id="mimo-v2.5",
                    label="MiMo V2.5",
                    base_url="https://api.xiaomimimo.com/v1",
                    api_key="mimo-key",
                    model="mimo-v2.5",
                    supports_text=True,
                    supports_image=True,
                )
            ],
        ),
        limit=10,
    )

    assert stats.failed == 1
    with session_factory() as session:
        experiment = session.query(RecognitionExperiment).one()
        assert "response_body=" in experiment.error_message
        assert "bad image" in experiment.error_message
