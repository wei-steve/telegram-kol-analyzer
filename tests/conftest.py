import os

import pytest

from telegram_kol_research.models import MediaAsset, RawMessage, StrategyLifecycle
from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult
from telegram_kol_research.runtime_incident_adapters import STRICT_CAPTURE_ENV_VAR

# A-10e. Incident capture fails open in production on purpose: an alert that
# cannot be written must not take the work down with it. The cost is that a
# summary key outside the closed vocabulary produces a permanently silent
# alarm whose only symptom is a log line -- A-8c lost one that way, and A-10b
# lost another, discovered only when three real write-offs went unannounced.
# Under the suite, the same condition raises, so the next one is a red test.
os.environ.setdefault(STRICT_CAPTURE_ENV_VAR, "1")


@pytest.fixture
def allow_incident_capture_to_fail_open(monkeypatch):
    """For the cases that assert the production fail-open behaviour itself."""

    monkeypatch.delenv(STRICT_CAPTURE_ENV_VAR, raising=False)


@pytest.fixture
def stub_mimo_authoritative_model(monkeypatch):
    """Keep the real authority/apply pipeline while replacing only MiMo I/O."""

    def fake_mimo_result(session_factory, *, raw_message_id, **kwargs):
        with session_factory() as session:
            raw_message = session.get(RawMessage, raw_message_id)
            has_image = (
                session.query(MediaAsset.id)
                .filter(MediaAsset.raw_message_id == raw_message_id)
                .first()
                is not None
            )
            lifecycle = (
                session.query(StrategyLifecycle)
                .filter(StrategyLifecycle.chat_id == raw_message.chat_id)
                .order_by(StrategyLifecycle.id.desc())
                .first()
            )

        message_text = raw_message.text or ""
        if "now entered" in message_text:
            payload = {
                "recognition_result": "非策略",
                "reason": "MiMo recognized an entry confirmation",
                "strategy": {},
                "lifecycle_event": {
                    "event_type": "entry_confirm",
                    "target_lifecycle_id": lifecycle.id if lifecycle else None,
                    "symbol": "BTC",
                    "side": "long",
                    "entry_price": 68100,
                    "confidence": 0.95,
                    "reason": "The pending strategy is now entered",
                },
                "input_reading": {
                    "observed_text": message_text,
                    "image_quality": "none",
                },
                "confidence": 0.95,
            }
            status = "非策略"
        elif "SL moved" in message_text:
            payload = {
                "recognition_result": "非策略",
                "reason": "MiMo recognized a position update",
                "strategy": {},
                "lifecycle_event": {
                    "event_type": "position_update",
                    "target_lifecycle_id": lifecycle.id if lifecycle else None,
                    "symbol": "BTC",
                    "side": "long",
                    "stop_loss": 68050,
                    "management_action": "risk_update",
                    "confidence": 0.95,
                    "reason": "Stop loss moved to 68050",
                },
                "input_reading": {
                    "observed_text": message_text,
                    "image_quality": "none",
                },
                "confidence": 0.95,
            }
            status = "非策略"
        else:
            payload = {
                "recognition_result": "是策略",
                "reason": "MiMo recognized an actionable entry",
                "strategy": {
                    "symbol": "BTC",
                    "side": "long",
                    "entry": "68000-68200",
                    "stop_loss": "67500",
                    "take_profit": "69000/70000",
                    "leverage": None,
                    "order_type": "limit",
                },
                "lifecycle_event": {"event_type": "none", "confidence": 0.0},
                "input_reading": {
                    "observed_text": message_text,
                    "image_quality": "clear" if has_image else "none",
                },
                "confidence": 0.95,
            }
            status = "是策略"

        return MimoAuthoritativeResult(
            raw_message_id=raw_message_id,
            payload=payload,
            input_kind="image" if has_image else "text",
            model="mimo-v2.5",
            status=status,
            prompt_versions={
                "trading.analysis.shared": 1,
                "trading.analysis.mimo_vision": 2,
            },
        )

    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.run_mimo_authoritative_for_message",
        fake_mimo_result,
    )
    return fake_mimo_result
