import json
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    MediaAsset,
    MessageEvidenceVersion,
    MessageInstructionItem,
    MessageRecognition,
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
)
from telegram_kol_research.web_queries import load_group_messages


NOW = datetime(2026, 8, 11, 20, 0)


def _add_decision(session, raw, *, status="failed", reason="target_unresolved"):
    session.add(
        RecognitionDecision(
            raw_message_id=raw.id,
            input_kind="text+image",
            authoritative_model="mimo-v2.5",
            authoritative_status="非策略",
            authoritative_payload_json="{}",
            agreement_status="authoritative_only",
            differences_json="[]",
            automation_status=status,
            automation_reason=reason,
        )
    )


def test_web_projection_labels_history_without_inventing_v2_intents(tmp_path):
    factory = create_session_factory(tmp_path / "history.db")
    with factory() as session:
        raw = RawMessage(chat_id=88, message_id=35, text="ETH long")
        session.add(raw)
        session.flush()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=raw.id,
                version=1,
                input_fingerprint="sha256:legacy",
                model="mimo-v2.5",
                extraction_status="completed",
                confidence=0.83,
                text_evidence_json='{"observed_text":"ETH long"}',
                image_evidence_json='{"fields":{"symbol":"ETH"}}',
                normalized_evidence_json=(
                    '{"recognition_result":"是策略","summary":"ETH多单",'
                    '"confidence":0.83,"strategy":{"symbol":"ETH","side":"long"}}'
                ),
            )
        )
        _add_decision(session, raw, status="skipped", reason="auto_trade_disabled")
        session.commit()

    analysis = load_group_messages(factory, chat_id=88, limit=10)[0]["mimo_analysis"]

    assert analysis["format"] == "historical_v1"
    assert analysis["history_label"] == "MiMo 历史结果 · v1格式"
    assert analysis["summary"] == "ETH多单"
    assert analysis["intents"] == []
    assert analysis["detail_flags"] == {
        "attempts_recorded": False,
        "per_image_evidence_recorded": False,
    }
    assert analysis["legacy_image_evidence"] == {"fields": {"symbol": "ETH"}}


def test_web_projection_keeps_pre_evidence_mimo_history_visible(tmp_path):
    factory = create_session_factory(tmp_path / "pre-evidence-history.db")
    with factory() as session:
        raw = RawMessage(chat_id=88, message_id=36, text="ETH long")
        session.add(raw)
        session.flush()
        session.add(
            MessageRecognition(
                raw_message_id=raw.id,
                status="非策略",
                reason="这是已有仓位的更新。",
                summary="ETH 多单持仓更新",
                engine="mimo-v2.5",
            )
        )
        _add_decision(session, raw, status="skipped", reason="not_actionable")
        session.commit()

    analysis = load_group_messages(factory, chat_id=88, limit=10)[0]["mimo_analysis"]

    assert analysis["format"] == "historical_v1"
    assert analysis["history_label"] == "MiMo 历史结果 · v1格式"
    assert analysis["summary"] == "ETH 多单持仓更新"
    assert analysis["intents"] == []
    assert analysis["legacy_result"] == {
        "status": "非策略",
        "reason": "这是已有仓位的更新。",
        "model": "mimo-v2.5",
    }
    assert analysis["detail_flags"] == {
        "attempts_recorded": False,
        "per_image_evidence_recorded": False,
    }


def test_web_projection_does_not_label_current_v1_run_as_history(tmp_path):
    factory = create_session_factory(tmp_path / "current-v1.db")
    with factory() as session:
        raw = RawMessage(chat_id=88, message_id=40, text="普通消息")
        session.add(raw)
        session.flush()
        session.add(
            MimoRecognitionRun(
                raw_message_id=raw.id,
                run_kind="v1_authoritative",
                contract_version="v1",
                model="mimo-v2.5",
                input_kind="text",
                input_fingerprint="sha256:current-v1",
                prompt_versions_json="{}",
                status="completed",
                attempt_count=1,
                selected_attempt_ordinal=1,
                became_authoritative=True,
                started_at=NOW,
                completed_at=NOW + timedelta(milliseconds=50),
            )
        )
        session.commit()

    analysis = load_group_messages(factory, chat_id=88, limit=10)[0]["mimo_analysis"]

    assert analysis["format"] == "v1"
    assert analysis["version_label"] == "权威识别结果"
    assert analysis["history_label"] is None
    assert analysis["projection"] == {"status": "v1", "reason_code": None}


def test_web_projection_does_not_accept_stale_candidate_after_no_action_reanalysis(
    tmp_path,
):
    factory = create_session_factory(tmp_path / "stale-candidate.db")
    with factory() as session:
        raw = RawMessage(chat_id=88, message_id=38, text="普通聊天")
        session.add(raw)
        session.flush()
        session.add(
            MessageRecognition(
                raw_message_id=raw.id,
                status="非策略",
                reason="当前消息没有交易动作。",
                engine="mimo-v2.5",
            )
        )
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                parse_source="mimo_authoritative",
                confidence=0.9,
            )
        )
        _add_decision(session, raw, status="skipped", reason="mimo_no_action")
        session.commit()

    acceptance = load_group_messages(factory, chat_id=88, limit=10)[0][
        "system_acceptance"
    ]

    assert acceptance["status"] == "not_accepted"
    assert acceptance["accepted_candidate_count"] == 0


def test_web_projection_does_not_relabel_non_mimo_history(tmp_path):
    factory = create_session_factory(tmp_path / "deepseek-history.db")
    with factory() as session:
        raw = RawMessage(chat_id=88, message_id=42, text="legacy")
        session.add(raw)
        session.flush()
        session.add(
            MessageRecognition(
                raw_message_id=raw.id,
                status="非策略",
                reason="legacy deepseek result",
                engine="deepseek-v4-flash",
            )
        )
        session.commit()

    message = load_group_messages(factory, chat_id=88, limit=10)[0]

    assert message["mimo_analysis"] is None


def test_web_projection_excludes_retired_instruction_candidate_from_acceptance(tmp_path):
    factory = create_session_factory(tmp_path / "retired-candidate.db")
    with factory() as session:
        raw = RawMessage(chat_id=88, message_id=44, text="move stop")
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="ETH",
            side="short",
            event_type="position_update",
            target_lifecycle_id=790,
            management_action="move_stop_to_protect",
            parse_source="mimo_authoritative",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        session.add(
            MessageInstructionItem(
                raw_message_id=raw.id,
                signal_candidate_id=candidate.id,
                sequence=0,
                instruction_kind="management",
                idempotency_key="r" * 64,
                status="succeeded",
                retired_at=NOW,
            )
        )
        _add_decision(session, raw, status="skipped", reason="mimo_no_action")
        session.commit()

    acceptance = load_group_messages(factory, chat_id=88, limit=10)[0][
        "system_acceptance"
    ]

    assert acceptance["accepted_candidate_count"] == 0
    assert acceptance["status"] == "not_accepted"

