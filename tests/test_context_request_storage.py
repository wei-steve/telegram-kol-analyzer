import hashlib
import json

import pytest

from telegram_kol_research.context_request_storage import (
    ContextRequestStorageError,
    build_context_message_refs,
    build_request_component_sha256,
    collect_candidate_thread_ids,
    parse_context_request_storage,
    rendered_prompt_sha256,
)
from telegram_kol_research.context_resolution_prompt import (
    CONTEXT_RESOLUTION_SYSTEM_PROMPT,
    build_context_provider_messages,
)


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _request():
    return {
        "current_message": {
            "raw_message_id": 30,
            "chat_id": -10088,
            "message_id": 1030,
            "text": "更新策略",
        },
        "saved_evidence": {"version": 4},
        "message_context": {
            "current": {
                "raw_message_id": 30,
                "message_id": 1030,
                "evidence_version_id": 44,
            },
            "messages": [
                {
                    "raw_message_id": 28,
                    "message_id": 1028,
                    "evidence_version_id": 42,
                    "strategy_links": [{"strategy_thread_id": 17}],
                },
                {
                    "raw_message_id": 29,
                    "message_id": 1029,
                    "evidence_version_id": 43,
                },
            ],
            "reply_chain": [
                {
                    "raw_message_id": 27,
                    "message_id": 1027,
                    "evidence_version_id": 41,
                    "thread_id": 16,
                }
            ],
            "active_strategies": [{"strategy_thread_id": 18}],
        },
        "candidate_strategy_threads": [{"thread_id": 19}],
        "redacted_exchange_state": {},
        "mimo_first_pass": {"recognition_result": "是策略"},
    }


def test_request_storage_parser_distinguishes_all_three_states_strictly():
    legacy = parse_context_request_storage(_canonical(_request()))
    reference = parse_context_request_storage(
        '{"contract":"context-resolution-request-storage-v1",'
        '"storage":"reference_only"}'
    )
    archived = parse_context_request_storage(
        '{"archive_artifact_sha256":"' + "a" * 64 + '",'
        '"contract":"context-resolution-request-storage-v1",'
        '"record_sha256":"' + "b" * 64 + '","storage":"archive"}'
    )

    assert legacy.storage == "legacy-full"
    assert legacy.request_payload == _request()
    assert reference.storage == "reference-only"
    assert reference.request_payload is None
    assert archived.storage == "archived"
    assert archived.request_payload is None

    with pytest.raises(ContextRequestStorageError, match="archived"):
        archived.require_legacy_full()
    with pytest.raises(ContextRequestStorageError, match="exact fields"):
        parse_context_request_storage(
            '{"contract":"context-resolution-request-storage-v1",'
            '"storage":"reference_only","request":{}}'
        )
    with pytest.raises(ContextRequestStorageError, match="unknown storage"):
        parse_context_request_storage(
            '{"contract":"context-resolution-request-storage-v1",'
            '"storage":"future"}'
        )


def test_r1_reference_and_fingerprint_projection_is_exact_and_compact():
    request = _request()

    assert build_context_message_refs(request) == {
        "chat_id": -10088,
        "current": [30, 1030, 44],
        "messages": [[28, 1028, 42], [29, 1029, 43]],
        "reply_chain": [[27, 1027, 41]],
    }
    assert collect_candidate_thread_ids(request) == [16, 17, 18, 19]

    component_hashes = build_request_component_sha256(request)
    assert set(component_hashes) == {
        "current_message",
        "saved_evidence",
        "message_context",
        "candidate_strategy_threads",
        "redacted_exchange_state",
        "mimo_first_pass",
    }
    assert component_hashes["current_message"] == hashlib.sha256(
        _canonical(request["current_message"]).encode("utf-8")
    ).hexdigest()

    messages = build_context_provider_messages(
        CONTEXT_RESOLUTION_SYSTEM_PROMPT,
        request,
    )
    assert rendered_prompt_sha256(
        system_prompt=CONTEXT_RESOLUTION_SYSTEM_PROMPT,
        request_payload=request,
    ) == hashlib.sha256(_canonical(messages).encode("utf-8")).hexdigest()
