import json
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    MediaAsset,
    MessageEvidenceVersion,
    MessageRecognition,
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    RawMessage,
    RecognitionDecision,
)
from telegram_kol_research.web_queries import load_group_messages


NOW = datetime(2026, 8, 11, 20, 0)


def _v2_payload(*, asset_id: int | None = None) -> dict[str, object]:
    images = []
    refs = ["text:stop_loss"]
    if asset_id is not None:
        images = [
            {
                "asset_id": asset_id,
                "image_type": "position_screenshot",
                "quality": "clear",
                "observed_text": "ETHUSDT 永续，空，止损1940",
                "summary": "ETHUSDT空仓持仓截图",
                "fields": {
                    "symbol": {
                        "value": "ETH",
                        "source": "image",
                        "confidence": 0.99,
                    }
                },
                "confidence": 0.97,
            }
        ]
        refs.append(f"image:{asset_id}:symbol")
    return {
        "contract_version": "mimo-authoritative-v2",
        "summary": "管理已有 ETH 空单并移动止损",
        "confidence": 0.94,
        "intents": [
            {
                "intent_type": "position_management",
                "action": {
                    "kind": "move_stop_to_protect",
                    "target": {"lifecycle_id": 790, "thread_id": 52},
                    "strategy": None,
                    "parameters": {"stop_loss": "1940"},
                },
                "reason": "消息明确要求移动止损到1940",
                "confidence": 0.95,
                "evidence_refs": refs,
            },
            {
                "intent_type": "market_commentary",
                "action": None,
                "reason": "同时提到市场波动",
                "confidence": 0.8,
                "evidence_refs": [],
            },
        ],
        "evidence": {
            "text": {"observed_text": "移动止损到1940", "fields": {}},
            "images": images,
            "conflicts": [],
        },
    }


def _add_v2_evidence(session, raw, payload, run):
    evidence = payload["evidence"]
    session.add(
        MessageEvidenceVersion(
            raw_message_id=raw.id,
            mimo_recognition_run_id=run.id,
            version=1,
            input_fingerprint="sha256:input",
            model=run.model,
            prompt_versions_json='{"trading.analysis.mimo_v2_authoritative":1}',
            extraction_status="completed",
            confidence=payload["confidence"],
            text_evidence_json=json.dumps(evidence["text"], ensure_ascii=False),
            image_evidence_json=json.dumps(
                {
                    "images": evidence["images"],
                    "conflicts": evidence["conflicts"],
                },
                ensure_ascii=False,
            ),
            normalized_evidence_json=json.dumps(
                {
                    key: payload[key]
                    for key in (
                        "contract_version",
                        "summary",
                        "confidence",
                        "intents",
                    )
                },
                ensure_ascii=False,
            ),
        )
    )


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


def test_web_projection_separates_v2_intents_image_evidence_and_application_failure(
    tmp_path,
):
    factory = create_session_factory(tmp_path / "projection.db")
    with factory() as session:
        raw = RawMessage(chat_id=88, message_id=31, text="移动止损到1940")
        session.add(raw)
        session.flush()
        media = MediaAsset(
            raw_message_id=raw.id,
            kind="photo",
            mime_type="image/jpeg",
            local_path="data/media/88/31.jpg",
        )
        session.add(media)
        session.flush()
        run = MimoRecognitionRun(
            raw_message_id=raw.id,
            run_kind="v2_authoritative",
            contract_version="mimo-authoritative-v2",
            model="mimo-v2.5",
            input_kind="text+image",
            input_fingerprint="sha256:input",
            prompt_versions_json='{"trading.analysis.mimo_v2_authoritative":1}',
            status="completed",
            attempt_count=1,
            selected_attempt_ordinal=1,
            became_authoritative=True,
            started_at=NOW,
            completed_at=NOW + timedelta(milliseconds=425),
        )
        session.add(run)
        session.flush()
        session.add(
            MimoRecognitionAttempt(
                run_id=run.id,
                ordinal=1,
                status="completed",
                response_fingerprint="a" * 64,
                started_at=NOW,
                completed_at=NOW + timedelta(milliseconds=425),
                duration_ms=425,
            )
        )
        payload = _v2_payload(asset_id=media.id)
        _add_v2_evidence(session, raw, payload, run)
        _add_decision(session, raw)
        session.commit()

    message = load_group_messages(factory, chat_id=88, limit=10)[0]

    analysis = message["mimo_analysis"]
    assert analysis["format"] == "v2"
    assert analysis["runtime"]["status"] == "completed"
    assert analysis["runtime"]["duration_ms"] == 425
    assert analysis["runtime"]["attempts"][0]["selected"] is True
    assert [intent["intent_label"] for intent in analysis["intents"]] == [
        "仓位管理",
        "市场评论",
    ]
    assert analysis["intents"][0]["action"]["action_label"] == "移动止损保护"
    assert analysis["evidence"]["images"][0]["summary"] == "ETHUSDT空仓持仓截图"
    assert analysis["evidence"]["images"][0]["image_type_label"] == "持仓截图"
    assert analysis["evidence"]["images"][0]["quality_label"] == "清晰"
    assert analysis["evidence"]["images"][0]["media"]["local_path"].endswith(
        "31.jpg"
    )
    assert message["system_acceptance"]["status"] == "failed"
    assert message["system_acceptance"]["reason_code"] == "target_unresolved"


def test_web_projection_exposes_failed_v2_run_before_authoritative_v1_fallback(
    tmp_path,
):
    factory = create_session_factory(tmp_path / "fallback.db")
    with factory() as session:
        raw = RawMessage(chat_id=88, message_id=32, text="BTC long")
        session.add(raw)
        session.flush()
        failed = MimoRecognitionRun(
            raw_message_id=raw.id,
            run_kind="v2_authoritative",
            contract_version="mimo-authoritative-v2",
            model="mimo-v2.5",
            input_kind="text",
            input_fingerprint="sha256:fallback",
            prompt_versions_json="{}",
            status="failed",
            attempt_count=1,
            final_error_code="contract_invalid",
            final_error_message="schema rejected",
            became_authoritative=False,
            started_at=NOW,
            completed_at=NOW + timedelta(milliseconds=300),
        )
        session.add(failed)
        session.flush()
        fallback = MimoRecognitionRun(
            raw_message_id=raw.id,
            run_kind="v1_fallback",
            contract_version="v1",
            model="mimo-v2.5",
            input_kind="text",
            input_fingerprint="sha256:fallback",
            prompt_versions_json="{}",
            status="completed",
            attempt_count=1,
            retry_of_run_id=failed.id,
            selected_attempt_ordinal=1,
            became_authoritative=True,
            started_at=NOW + timedelta(milliseconds=301),
            completed_at=NOW + timedelta(milliseconds=701),
        )
        session.add(fallback)
        session.flush()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=raw.id,
                mimo_recognition_run_id=fallback.id,
                version=1,
                input_fingerprint="sha256:fallback",
                model="mimo-v2.5",
                extraction_status="completed",
                confidence=0.91,
                text_evidence_json='{"observed_text":"BTC long"}',
                image_evidence_json="{}",
                normalized_evidence_json=(
                    '{"recognition_result":"是策略","summary":"BTC多单",'
                    '"confidence":0.91,"strategy":{"symbol":"BTC","side":"long"}}'
                ),
            )
        )
        _add_decision(session, raw, status="skipped", reason="auto_trade_disabled")
        session.commit()

    analysis = load_group_messages(factory, chat_id=88, limit=10)[0]["mimo_analysis"]

    assert analysis["format"] == "historical_v1"
    assert analysis["runtime"]["status"] == "fallback"
    assert analysis["runtime"]["retry_count"] == 1
    assert analysis["runtime"]["fallback_source"] == {
        "contract_version": "mimo-authoritative-v2",
        "error_code": "contract_invalid",
        "error_message": "schema rejected",
    }
    assert analysis["history_label"] == "MiMo 历史结果 · v1格式"
    assert analysis["intents"] == []


def test_web_projection_keeps_exhausted_v2_failure_distinct_from_projection_failure(
    tmp_path,
):
    factory = create_session_factory(tmp_path / "failed.db")
    with factory() as session:
        raw = RawMessage(chat_id=88, message_id=33, text="exit")
        session.add(raw)
        session.flush()
        run = MimoRecognitionRun(
            raw_message_id=raw.id,
            run_kind="v2_authoritative",
            contract_version="mimo-authoritative-v2",
            model="mimo-v2.5",
            input_kind="text",
            input_fingerprint="sha256:failed",
            prompt_versions_json="{}",
            status="failed",
            attempt_count=2,
            final_error_code="provider_timeout",
            final_error_message="request timed out",
            became_authoritative=False,
            started_at=NOW,
            completed_at=NOW + timedelta(seconds=2),
        )
        session.add(run)
        _add_decision(session, raw, status="failed", reason="mimo_authoritative_failed")
        session.commit()

    analysis = load_group_messages(factory, chat_id=88, limit=10)[0]["mimo_analysis"]

    assert analysis["format"] == "v2"
    assert analysis["runtime"]["status"] == "failed"
    assert analysis["runtime"]["error_code"] == "provider_timeout"
    assert analysis["projection"]["status"] == "not_available"
    assert analysis["projection"]["reason_code"] == "canonical_result_not_persisted"


def test_web_projection_refuses_malformed_stored_v2_without_relabelling_provider_run(
    tmp_path,
):
    factory = create_session_factory(tmp_path / "invalid.db")
    with factory() as session:
        raw = RawMessage(chat_id=88, message_id=34, text="hold")
        session.add(raw)
        session.flush()
        run = MimoRecognitionRun(
            raw_message_id=raw.id,
            run_kind="v2_authoritative",
            contract_version="mimo-authoritative-v2",
            model="mimo-v2.5",
            input_kind="text",
            input_fingerprint="sha256:invalid",
            prompt_versions_json="{}",
            status="completed",
            attempt_count=1,
            selected_attempt_ordinal=1,
            became_authoritative=True,
            started_at=NOW,
            completed_at=NOW + timedelta(milliseconds=50),
        )
        session.add(run)
        session.flush()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=raw.id,
                mimo_recognition_run_id=run.id,
                version=1,
                input_fingerprint="sha256:invalid",
                model="mimo-v2.5",
                extraction_status="completed",
                confidence=0.9,
                text_evidence_json='{"observed_text":"hold","fields":{}}',
                image_evidence_json='{"images":[],"conflicts":[]}',
                normalized_evidence_json=(
                    '{"contract_version":"mimo-authoritative-v2",'
                    '"summary":"bad","confidence":0.9,"intents":[]}'
                ),
            )
        )
        _add_decision(session, raw, status="skipped", reason="not_actionable")
        session.commit()

    analysis = load_group_messages(factory, chat_id=88, limit=10)[0]["mimo_analysis"]

    assert analysis["runtime"]["status"] == "completed"
    assert analysis["projection"]["status"] == "failed"
    assert analysis["projection"]["reason_code"] == "stored_v2_contract_invalid"
    assert analysis["intents"] == []


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
