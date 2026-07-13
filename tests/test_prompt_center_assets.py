from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_prompt_center_javascript_uses_registry_lifecycle_and_stale_request_guard():
    source = (ROOT / "src/telegram_kol_research/static/app.js").read_text(encoding="utf-8")

    assert "/api/ai-prompts" in source
    assert "/draft" in source
    assert "/validate" in source
    assert "/test" in source
    assert "/publish" in source
    assert "/rollback" in source
    assert "promptCenterRequestId" in source
    assert "window.confirm" in source
    assert "telegram-workbench:prompt:" in source
    assert "导入为草稿" in source


def test_chat_request_no_longer_sends_unversioned_group_prompt():
    source = (ROOT / "src/telegram_kol_research/static/app.js").read_text(encoding="utf-8")
    submit_section = source[source.index("async function submitAiQuestion"):]

    assert "group_prompt:" not in submit_section.split("function ", 1)[0]
