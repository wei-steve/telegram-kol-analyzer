import json
from datetime import UTC, datetime

import httpx
import pytest

from telegram_kol_research.ai_recognition_config import AiModelConfig, AiRecognitionConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.mimo_recognition_runs import load_mimo_attempts
from telegram_kol_research.models import (
    AiPromptInvocation,
    MediaAsset,
    MessageRecognition,
    MimoRecognitionRun,
    RawMessage,
    RecognitionExperiment,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.prompt_defaults import (
    MIMO_V2_AUTHORITATIVE_PROMPT,
    MIMO_VISION_PROMPT,
    SHARED_TRADING_PROMPT,
)
from telegram_kol_research.recognition_experiments import (
    _build_mimo_payload,
    infer_mimo_authoritative_v2,
    run_mimo_authoritative_for_message,
    run_mimo_direct_for_message,
    run_mimo_direct_experiment,
)


def _v2_config() -> AiRecognitionConfig:
    return AiRecognitionConfig(
        ai_models=[
            AiModelConfig(
                id="mimo-v2.5",
                label="MiMo",
                base_url="https://api.xiaomimimo.com/v1",
                api_key="test-key",
                model="mimo-v2.5",
                supports_text=True,
                supports_image=True,
            )
        ]
    )


def _v2_payload(observed_text: str = "BTC 偏多观点") -> dict:
    return {
        "contract_version": "mimo-authoritative-v2",
        "summary": "普通市场观点",
        "confidence": 0.82,
        "intents": [
            {
                "intent_type": "market_commentary",
                "action": None,
                "reason": "没有完整交易动作",
                "confidence": 0.82,
                "evidence_refs": ["text:observed_text"],
            }
        ],
        "evidence": {
            "text": {"observed_text": observed_text, "fields": {}},
            "images": [],
            "conflicts": [],
        },
    }


def _v2_message(factory, *, text: str = "BTC 偏多观点") -> int:
    with factory() as session:
        row = RawMessage(chat_id=900, message_id=17, text=text)
        session.add(row)
        session.commit()
        return int(row.id)


def _sequence_requester(*outcomes):
    remaining = list(outcomes)

    def request(**kwargs):
        if not remaining:
            raise AssertionError("requester called too many times")
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    request.remaining = remaining
    return request


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


def test_run_mimo_authoritative_retries_once_after_transient_failure(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=100, message_id=11, text="移动保本损 剩余30%挂65000全部止盈")
        session.add(raw)
        session.commit()
        raw_id = raw.id

    calls: list[str] = []
    payload = {
        "recognition_result": "非策略",
        "reason": "当前消息是持仓管理",
        "strategy": {},
        "lifecycle_event": {
            "event_type": "position_update",
            "target_lifecycle_id": 88,
            "symbol": "BTC",
            "side": "long",
            "take_profit": "65000",
            "management_action": "move_stop_to_protect",
            "confidence": 0.93,
            "reason": "移动保本损并设置剩余止盈",
        },
        "input_reading": {
            "observed_text": "移动保本损 剩余30%挂65000全部止盈",
            "image_quality": "none",
        },
        "confidence": 0.93,
    }

    def flaky_call(**kwargs):
        calls.append(kwargs["raw_message"].text)
        if len(calls) == 1:
            raise TimeoutError("mimo timeout")
        return payload

    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments._call_mimo_direct_model",
        flaky_call,
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

    assert len(calls) == 2
    assert result.status == "非策略"
    assert result.error_message is None
    assert result.payload["lifecycle_event"]["event_type"] == "position_update"
    with session_factory() as session:
        experiment = session.query(RecognitionExperiment).one()
        invocation = session.query(AiPromptInvocation).one()
        assert experiment.status == "非策略"
        assert experiment.error_message is None
        assert invocation.status == "completed"


def test_run_mimo_authoritative_includes_recent_context_and_active_strategies(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=100,
                message_id=7,
                posted_at=datetime(2026, 7, 13, 1, 0, tzinfo=UTC),
                text="BTC short 63500",
            )
        )
        current = RawMessage(
            chat_id=100,
            message_id=8,
            posted_at=datetime(2026, 7, 13, 2, 0, tzinfo=UTC),
            text="现价出局",
            reply_to_message_id=7,
        )
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
    assert "Reply context" in context_text
    assert '"reply_to_message_id": 7' in context_text
    assert "2026-07-13T01:00:00" in context_text
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


def test_mimo_v2_first_attempt_success_records_selected_attempt_and_prompt(
    tmp_path,
):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)
    captured: dict = {}

    def requester(**kwargs):
        captured.update(kwargs)
        return _v2_payload()

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        context_text="Recent context: no active strategy",
        requester=requester,
        retry_delay_seconds=0,
    )

    assert result.error_message is None
    assert result.error_code is None
    assert result.parsed_result is not None
    assert result.parsed_result.contract_version == "mimo-authoritative-v2"
    assert result.input_kind == "text"
    assert result.prompt_versions.keys() == {MIMO_V2_AUTHORITATIVE_PROMPT}
    assert "mimo-authoritative-v2" in captured["prompt"]
    assert captured["context_text"] == "Recent context: no active strategy"
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == ["completed"]
    assert attempts[0].selected is True
    with factory() as session:
        run = session.get(MimoRecognitionRun, result.run_id)
        assert run.status == "completed"
        assert run.run_kind == "v2_authoritative"
        assert run.selected_attempt_ordinal == 1
        assert run.became_authoritative is True
        assert run.input_fingerprint == result.analysis_input_fingerprint
        assert result.adapted_result is not None
        assert run.canonical_payload_fingerprint == (
            result.adapted_result.canonical_v2_fingerprint
        )
        assert run.projection_fingerprint == (
            result.adapted_result.projection_fingerprint
        )
        assert json.loads(run.prompt_versions_json) == result.prompt_versions
        invocation = session.query(AiPromptInvocation).one()
        assert invocation.status == "completed"
        assert json.loads(invocation.prompt_versions_json) == result.prompt_versions
        assert session.query(RecognitionExperiment).count() == 0
        assert session.query(MessageRecognition).count() == 0
        assert session.query(SignalCandidate).count() == 0
        assert session.query(StrategyLifecycle).count() == 0


def test_mimo_v2_timeout_then_success_records_two_attempts(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)
    requester = _sequence_requester(TimeoutError("slow"), _v2_payload())

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        requester=requester,
        retry_delay_seconds=0,
    )

    assert result.error_message is None
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == ["timeout", "completed"]
    assert [row.error_code for row in attempts] == ["provider_timeout", None]
    assert [row.retry_of_ordinal for row in attempts] == [None, 1]
    assert [row.selected for row in attempts] == [False, True]


def test_mimo_v2_exhausted_timeout_fails_run_without_selection(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        requester=_sequence_requester(
            TimeoutError("first slow"),
            TimeoutError("Authorization: Bearer private-token second slow"),
        ),
        retry_delay_seconds=0,
    )

    assert result.parsed_result is None
    assert result.error_code == "provider_timeout"
    assert "private-token" not in (result.error_message or "")
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == ["timeout", "timeout"]
    assert not any(row.selected for row in attempts)
    with factory() as session:
        run = session.get(MimoRecognitionRun, result.run_id)
        assert run.status == "failed"
        assert run.selected_attempt_ordinal is None
        assert run.final_error_code == "provider_timeout"


def test_mimo_v2_http_failure_retries_and_sanitizes_error(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)
    request = httpx.Request("POST", "https://api.xiaomimimo.com/v1/chat/completions")
    response = httpx.Response(503, request=request)
    failures = (
        httpx.HTTPStatusError(
            "api_key=private-key unavailable",
            request=request,
            response=response,
        ),
        httpx.HTTPStatusError(
            "api_key=private-key still unavailable",
            request=request,
            response=response,
        ),
    )

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        requester=_sequence_requester(*failures),
        retry_delay_seconds=0,
    )

    assert result.error_code == "provider_http_error"
    assert "private-key" not in (result.error_message or "")
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == ["http_error", "http_error"]


def test_mimo_v2_unexpected_provider_error_does_not_leave_running_run(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)

    class UnexpectedProviderError(Exception):
        pass

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        requester=_sequence_requester(
            UnexpectedProviderError("provider SDK failed unexpectedly"),
            UnexpectedProviderError("provider SDK failed again"),
        ),
        retry_delay_seconds=0,
    )

    assert result.error_code == "provider_http_error"
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == ["http_error", "http_error"]
    with factory() as session:
        assert session.get(MimoRecognitionRun, result.run_id).status == "failed"


def test_mimo_v2_invalid_json_fails_without_retry(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)
    requester = _sequence_requester("not-json", _v2_payload())

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        requester=requester,
        retry_delay_seconds=0,
    )

    assert result.error_code == "invalid_json"
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == ["invalid_json"]
    assert requester.remaining == [_v2_payload()]
    assert attempts[0].response_fingerprint is not None


def test_mimo_v2_contract_failure_records_response_and_stops(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)
    invalid = _v2_payload()
    invalid["contract_version"] = "mimo-authoritative-v3"

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        requester=_sequence_requester(invalid),
        retry_delay_seconds=0,
    )

    assert result.error_code == "contract_validation_failed"
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == ["contract_failure"]
    assert attempts[0].response_fingerprint is not None


def test_mimo_v2_adapter_rejection_is_audited_as_adapter_failure(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory, text="ETH 减仓一半，剩余仓位继续持有")
    payload = _v2_payload("ETH 减仓一半，剩余仓位继续持有")
    payload["intents"] = [
        {
            "intent_type": "position_management",
            "action": {
                "kind": "partial_take_profit",
                "target": {"lifecycle_id": 790, "thread_id": 52},
                "strategy": None,
                "parameters": {"management_fraction": 0.5},
            },
            "reason": "明确要求减仓一半",
            "confidence": 0.95,
            "evidence_refs": ["text:observed_text"],
        },
        {
            "intent_type": "position_management",
            "action": {
                "kind": "hold_update",
                "target": {"lifecycle_id": 790, "thread_id": 52},
                "strategy": None,
                "parameters": {"take_profit": "1750"},
            },
            "reason": "剩余仓位继续持有",
            "confidence": 0.91,
            "evidence_refs": ["text:observed_text"],
        },
    ]

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        requester=lambda **kwargs: payload,
        retry_delay_seconds=0,
    )

    assert result.error_code == "adapter_failure"
    assert "unsupported_multiple_lifecycle_actions" in result.error_message
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == ["contract_failure"]


@pytest.mark.parametrize(
    ("response", "attempt_status"),
    (
        ("not-json", "invalid_json"),
        (
            {
                **_v2_payload(),
                "contract_version": "mimo-authoritative-v3",
            },
            "contract_failure",
        ),
    ),
)
def test_mimo_v2_input_change_takes_precedence_over_invalid_response(
    tmp_path,
    response,
    attempt_status,
):
    factory = create_session_factory(tmp_path / f"{attempt_status}.db")
    raw_message_id = _v2_message(factory)

    def mutate_input(**kwargs):
        with factory() as session:
            raw = session.get(RawMessage, raw_message_id)
            raw.text = "edited while invalid response was returning"
            session.commit()
        return response

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        requester=mutate_input,
        retry_delay_seconds=0,
    )

    assert result.error_code == "input_changed_during_analysis"
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == [attempt_status]


def test_mimo_v2_unreadable_declared_image_fails_before_provider(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory, text="ETH 持仓截图")
    with factory() as session:
        session.add(
            MediaAsset(
                raw_message_id=raw_message_id,
                kind="photo",
                mime_type="image/jpeg",
                local_path="missing/position.jpg",
            )
        )
        session.commit()

    def should_not_call(**kwargs):
        raise AssertionError("provider must not be called")

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        media_root=tmp_path / "media",
        requester=should_not_call,
        retry_delay_seconds=0,
    )

    assert result.error_code == "image_unavailable"
    assert result.input_kind == "text+image"
    assert load_mimo_attempts(factory, run_id=result.run_id) == []
    with factory() as session:
        run = session.get(MimoRecognitionRun, result.run_id)
        assert run.status == "failed"
        assert run.attempt_count == 0


@pytest.mark.parametrize("media_error", (OSError("read failed"), RuntimeError("symlink loop")))
def test_mimo_v2_image_read_error_does_not_leave_running_run(
    tmp_path,
    monkeypatch,
    media_error,
):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory, text="ETH 持仓截图")
    image_path = tmp_path / "position.jpg"
    image_path.write_bytes(b"image")
    with factory() as session:
        session.add(
            MediaAsset(
                raw_message_id=raw_message_id,
                kind="photo",
                mime_type="image/jpeg",
                local_path=str(image_path),
            )
        )
        session.commit()
    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments._media_asset_to_data_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(media_error),
    )

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        media_root=tmp_path,
        requester=lambda **kwargs: _v2_payload(),
        retry_delay_seconds=0,
    )

    assert result.error_code == "image_unavailable"
    assert load_mimo_attempts(factory, run_id=result.run_id) == []
    with factory() as session:
        assert session.get(MimoRecognitionRun, result.run_id).status == "failed"


def test_mimo_v2_input_change_after_response_fails_closed(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)

    def mutate_input(**kwargs):
        with factory() as session:
            raw = session.get(RawMessage, raw_message_id)
            raw.text = "edited while MiMo was running"
            session.commit()
        return _v2_payload()

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        media_root=tmp_path / "media",
        requester=mutate_input,
        retry_delay_seconds=0,
    )

    assert result.parsed_result is None
    assert result.error_code == "input_changed_during_analysis"
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == ["completed"]
    assert attempts[0].selected is False
    with factory() as session:
        run = session.get(MimoRecognitionRun, result.run_id)
        assert run.status == "failed"
        assert run.canonical_payload_fingerprint is None
        assert run.projection_fingerprint is None


def test_mimo_v2_dynamic_context_change_after_response_fails_closed(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    with factory() as session:
        prior = RawMessage(
            chat_id=900,
            message_id=16,
            posted_at=datetime(2026, 8, 11, 1, 0, tzinfo=UTC),
            text="BTC long strategy remains active",
        )
        current = RawMessage(
            chat_id=900,
            message_id=17,
            posted_at=datetime(2026, 8, 11, 2, 0, tzinfo=UTC),
            text="BTC 偏多观点",
        )
        session.add_all([prior, current])
        session.commit()
        prior_id = int(prior.id)
        current_id = int(current.id)
    captured: dict = {}

    def mutate_context(**kwargs):
        captured.update(kwargs)
        with factory() as session:
            prior = session.get(RawMessage, prior_id)
            prior.text = "BTC strategy was cancelled"
            session.commit()
        return _v2_payload()

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=current_id,
        config=_v2_config(),
        requester=mutate_context,
        retry_delay_seconds=0,
    )

    assert "BTC long strategy remains active" in captured["context_text"]
    assert result.error_code == "input_changed_during_analysis"
    attempts = load_mimo_attempts(factory, run_id=result.run_id)
    assert [row.status for row in attempts] == ["completed"]
    assert attempts[0].selected is False


def test_mimo_v2_invalid_retry_settings_do_not_create_running_run(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)

    with pytest.raises(ValueError, match="max_attempts"):
        infer_mimo_authoritative_v2(
            factory,
            raw_message_id=raw_message_id,
            config=_v2_config(),
            requester=lambda **kwargs: _v2_payload(),
            max_attempts="invalid",
            retry_delay_seconds=0,
        )

    with factory() as session:
        assert session.query(MimoRecognitionRun).count() == 0


@pytest.mark.parametrize(
    "kwargs",
    (
        {"max_attempts": 4},
        {"retry_delay_seconds": float("nan")},
        {"retry_delay_seconds": float("inf")},
        {"retry_delay_seconds": float("-inf")},
    ),
)
def test_mimo_v2_unbounded_retry_settings_do_not_create_running_run(
    tmp_path,
    kwargs,
):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)

    with pytest.raises(ValueError):
        infer_mimo_authoritative_v2(
            factory,
            raw_message_id=raw_message_id,
            config=_v2_config(),
            requester=lambda **request_kwargs: _v2_payload(),
            **kwargs,
        )

    with factory() as session:
        assert session.query(MimoRecognitionRun).count() == 0


def test_mimo_v2_prompt_invocation_failure_does_not_reverse_success(
    tmp_path,
    monkeypatch,
):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _v2_message(factory)
    monkeypatch.setattr(
        "telegram_kol_research.recognition_experiments.record_prompt_invocation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("prompt audit unavailable")
        ),
    )

    result = infer_mimo_authoritative_v2(
        factory,
        raw_message_id=raw_message_id,
        config=_v2_config(),
        requester=lambda **kwargs: _v2_payload(),
        retry_delay_seconds=0,
    )

    assert result.succeeded is True
    with factory() as session:
        run = session.get(MimoRecognitionRun, result.run_id)
        assert run.status == "completed"
        assert run.became_authoritative is True
