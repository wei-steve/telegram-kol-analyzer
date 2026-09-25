from pathlib import Path

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.prompt_defaults import build_prompt_seeds_from_legacy


SRC = Path(__file__).parents[1] / "src/telegram_kol_research"
#: Modules that send a business prompt to a model. Each one has to get that
#: prompt from the registry rather than carry its own copy.
AI_CALL_MODULES = {
    "message_recognition.py",
    "prompt_testing.py",
    "recognition_experiments.py",
    "strategy_alerts.py",
}

#: ``llm_chat`` stopped being one of those in phase 8: the Web group chat it
#: served is gone, and what is left is the runtime agent's transport, which is
#: handed its messages already built. It still must not grow a prompt of its
#: own, so the embedded-prompt scan still covers it.
NO_EMBEDDED_PROMPT_MODULES = AI_CALL_MODULES | {"llm_chat.py"}


def test_prompt_inventory_no_longer_seeds_the_retired_review_prompt():
    """``trading.disagreement.semantic_review`` is not seeded any more.

    The row an existing production database already holds is taken offline by a
    separate database operation, not by this code path; what this pins is that a
    fresh database never grows the definition back.
    """

    keys = {
        seed.prompt_key
        for seed in build_prompt_seeds_from_legacy(AiRecognitionConfig())
    }

    assert "trading.disagreement.semantic_review" not in keys
    assert keys == {
        "trading.analysis.shared",
        "trading.analysis.mimo_vision",
        "strategy.alert.classifier",
    }


def test_every_ai_call_site_uses_prompt_registry_without_embedded_business_prompts():
    forbidden_markers = (
        "You are an analyst for Telegram",
        "Classify one Telegram trading-group message",
        "MIMO_DIRECT_PROMPT =",
        "MiMo 对照实验要求",
    )
    for filename in AI_CALL_MODULES:
        source = (SRC / filename).read_text(encoding="utf-8")
        assert (
            "prompt_registry" in source or "prompt_composition" in source
        ), filename
    for filename in NO_EMBEDDED_PROMPT_MODULES:
        source = (SRC / filename).read_text(encoding="utf-8")
        for marker in forbidden_markers:
            assert marker not in source, f"{filename} embeds {marker}"
