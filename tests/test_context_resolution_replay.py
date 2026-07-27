import json
from pathlib import Path

from telegram_kol_research.ai_recognition_config import (
    AiProviderConfig,
    AiRecognitionConfig,
)
from telegram_kol_research.context_resolution import resolve_contextual_strategy
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RawMessage
from telegram_kol_research.trading_settings import trading_settings_from_payload


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "context_resolution"
    / "dpl_1460_1465.json"
)


def test_redacted_revision_cancel_replay_keeps_one_reply_thread(tmp_path):
    replay = json.loads(FIXTURE.read_text(encoding="utf-8"))
    session_factory = create_session_factory(tmp_path / "research.db")
    raw_ids = {}
    with session_factory() as session:
        for item in replay["messages"]:
            row = RawMessage(
                chat_id=replay["chat_id"],
                message_id=item["message_id"],
                text=item["text"],
                reply_to_message_id=item.get("reply_to_message_id"),
                archived_target_group=True,
            )
            session.add(row)
            session.flush()
            raw_ids[item["message_id"]] = row.id
        session.commit()

    responses = iter(
        [
            {
                "decision": "revise_thread",
                "target_thread_ids": [10],
                "management_action": "replace_entry",
                "supporting_message_ids": [1460, 1462],
                "opposing_message_ids": [],
                "conflict_types": [],
                "confidence": 0.96,
                "reason": "reply chain explicitly updates the original plan",
                "reanalysis_triggers": [],
                "risk_reducing_fanout_allowed": False,
            },
            {
                "decision": "cancel_thread",
                "target_thread_ids": [10],
                "management_action": "cancel_pending_entry",
                "supporting_message_ids": [1460, 1462, 1465],
                "opposing_message_ids": [],
                "conflict_types": [],
                "confidence": 0.98,
                "reason": "quoted continuation cancels the same plan",
                "reanalysis_triggers": [],
                "risk_reducing_fanout_allowed": False,
            },
        ]
    )

    def decide(message_id):
        return resolve_contextual_strategy(
            session_factory,
            raw_message_id=raw_ids[message_id],
            ai_recognition_config=AiRecognitionConfig(
                text_provider=AiProviderConfig(
                    base_url="https://api.deepseek.com",
                    api_key="redacted",
                    model="deepseek",
                )
            ),
            evidence={"text": replay["messages"][1 if message_id == 1462 else 2]["text"]},
            context_window={"messages": replay["messages"]},
            candidates=[{"thread_id": 10, "root_message_id": 1460}],
            first_pass_payload={"strategy_kind": "management"},
            exchange_state={},
            model_caller=lambda **_kwargs: next(responses),
        )

    revision = decide(1462)
    cancellation = decide(1465)
    repeated_revision = decide(1462)

    assert revision.decision == "revise_thread"
    assert cancellation.decision == "cancel_thread"
    assert revision.target_thread_ids == (10,)
    assert cancellation.target_thread_ids == (10,)
    assert repeated_revision == revision
    assert replay["expected"]["must_not_create_independent_thread_for"] == [1462, 1465]
    assert {
        item["after_cancel"]
        for item in replay["expected"]["entry_transitions"]
    } == {"cancelled"}
    assert replay["expected"]["late_fill"]["required_path"] == "exact_position_remediation"


def test_context_resolution_disabled_mode_is_a_no_call_gate():
    calls = []
    settings = trading_settings_from_payload(
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "context_resolution_enabled": False,
            "context_resolution_live_chat_ids": [-1009000000001],
        }
    )

    if settings.context_resolution_enabled_for_chat(-1009000000001):
        calls.append("resolver")

    assert calls == []
