from pathlib import Path


SRC = Path(__file__).parents[1] / "src/telegram_kol_research"
AI_CALL_MODULES = {
    "message_recognition.py",
    "prompt_testing.py",
    "recognition_experiments.py",
    "llm_chat.py",
    "strategy_alerts.py",
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
            "prompt_registry" in source
            or "prompt_composition" in source
            or (filename == "llm_chat.py" and "system_prompt: str" in source)
        ), filename
        for marker in forbidden_markers:
            assert marker not in source, f"{filename} embeds {marker}"


def test_chat_call_site_requires_explicit_registered_system_prompt():
    source = (SRC / "llm_chat.py").read_text(encoding="utf-8")

    assert "system_prompt: str" in source
    assert '"content": system_prompt.strip()' in source
