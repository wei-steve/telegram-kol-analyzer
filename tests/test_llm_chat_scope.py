from telegram_kol_research.llm_chat import (
    build_scope_context,
    extract_recent_message_limit,
)


def test_build_scope_context_includes_message_text_media_and_reply_context():
    messages = [
        {
            "raw_message_id": 10,
            "message_id": 100,
            "sender_name": "Alice",
            "text": "BTC long here",
            "reply_context": {"message_id": 99, "text": "Earlier context"},
            "media_assets": [{"kind": "photo", "ocr_text": "entry 68000"}],
        }
    ]

    context = build_scope_context(messages)

    assert "BTC long here" in context
    assert "Earlier context" in context
    assert "entry 68000" in context


def test_build_scope_context_includes_chronology_instruction_and_preserves_order():
    messages = [
        {
            "raw_message_id": 1,
            "message_id": 10,
            "sender_name": "Older",
            "text": "older message",
            "reply_context": None,
            "media_assets": [],
        },
        {
            "raw_message_id": 2,
            "message_id": 11,
            "sender_name": "Newer",
            "text": "newer message",
            "reply_context": None,
            "media_assets": [],
        },
    ]

    context = build_scope_context(messages)

    assert "Messages are ordered chronologically" in context
    assert context.index("older message") < context.index("newer message")


def test_extract_recent_message_limit_defaults_to_none_when_not_requested():
    assert extract_recent_message_limit("总结这个群最近在讨论什么") is None


def test_extract_recent_message_limit_reads_recent_count_patterns():
    assert extract_recent_message_limit("总结最近100条消息") == 100
    assert extract_recent_message_limit("分析最近 200 条里最重要的观点") == 200
    assert extract_recent_message_limit("summarize the recent 75 messages") == 75
