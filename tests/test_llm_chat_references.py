from telegram_kol_research.llm_chat import build_source_reference_map


def test_build_source_reference_map_indexes_messages_for_citation_rendering():
    references = build_source_reference_map(
        [
            {
                "raw_message_id": 5,
                "message_id": 50,
                "sender_name": "Bob",
                "text": "ETH short",
            }
        ]
    )

    assert references[0]["raw_message_id"] == 5
    assert references[0]["label"].startswith("[1]")
