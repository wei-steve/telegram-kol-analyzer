import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_recognition_labels import (
    load_message_recognition_label,
    save_message_recognition_label,
)
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageRecognitionLabel,
    MimoRecognitionRun,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
)
from telegram_kol_research.web_queries import load_group_messages
from telegram_kol_research.web_app import create_web_app


def _add_message(session, *, message_id=101):
    raw = RawMessage(chat_id=77, message_id=message_id, text="BTC long")
    session.add(raw)
    session.flush()
    return raw


def _add_decision(
    session,
    raw_message_id,
    *,
    recognition_result="是策略",
    event_type="entry_signal",
    confidence=0.91,
    model="mimo-v2.5",
    prompt_versions_json='{"decision":"v1"}',
):
    payload = {}
    if recognition_result is not None:
        payload["recognition_result"] = recognition_result
    if event_type is not None:
        payload["lifecycle_event"] = {"event_type": event_type}
    if confidence is not None:
        payload["confidence"] = confidence
    decision = RecognitionDecision(
        raw_message_id=raw_message_id,
        input_kind="text",
        authoritative_model=model,
        authoritative_status="completed",
        authoritative_payload_json=json.dumps(payload, ensure_ascii=False),
        agreement_status="authoritative_only",
        differences_json="[]",
        prompt_versions_json=prompt_versions_json,
    )
    session.add(decision)
    return decision


def test_label_upsert_captures_server_snapshot_and_prompt_run_provenance(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = _add_message(session)
        _add_decision(session, raw.id)
        session.add_all(
            [
                SignalCandidate(
                    raw_message_id=raw.id,
                    event_type="entry_signal",
                    parse_source="text",
                ),
                SignalCandidate(
                    raw_message_id=raw.id,
                    event_type="entry_signal",
                    parse_source="text",
                ),
                ContextResolutionAttempt(
                    raw_message_id=raw.id,
                    context_fingerprint="context:101",
                    model="context-model",
                    prompt_versions_json="{}",
                    request_summary_json="{}",
                    status="completed",
                ),
                MimoRecognitionRun(
                    raw_message_id=raw.id,
                    run_kind="v1_authoritative",
                    contract_version="mimo-authoritative-v1",
                    model="run-model",
                    input_kind="text",
                    input_fingerprint="input:101",
                    prompt_versions_json='{"run":"v2"}',
                    status="completed",
                    became_authoritative=True,
                ),
            ]
        )
        session.commit()
        raw_id = raw.id

    created_at = datetime(2026, 9, 2, 8, 0, tzinfo=UTC)
    saved = save_message_recognition_label(
        session_factory,
        raw_message_id=raw_id,
        payload={"verdict": "incorrect", "error_kind": "wrong_event_type", "note": "应为仓位管理"},
        now=created_at,
    )

    assert saved["verdict"] == "incorrect"
    assert saved["error_kind"] == "wrong_event_type"
    assert saved["note"] == "应为仓位管理"
    assert saved["labeled_recognition_result"] == "是策略"
    assert saved["labeled_event_type"] == "entry_signal"
    assert saved["labeled_confidence"] == 0.91
    assert saved["labeled_model"] == "run-model"
    assert saved["labeled_prompt_versions_json"] == '{"run":"v2"}'
    assert saved["labeled_prompt_versions_source"] == "mimo_run"
    assert saved["labeled_signal_candidate_count"] == 2
    assert saved["labeled_accepted_candidate_count"] == 0
    assert saved["labeled_context_attempt_status"] == "completed"
    assert saved["created_at"] == created_at
    assert saved["updated_at"] == created_at

    updated_at = created_at + timedelta(minutes=5)
    with session_factory() as session:
        decision = session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one()
        decision.authoritative_payload_json = json.dumps(
            {
                "recognition_result": "非策略",
                "lifecycle_event": {"event_type": "none"},
                "confidence": 0.72,
            },
            ensure_ascii=False,
        )
        session.commit()

    updated = save_message_recognition_label(
        session_factory,
        raw_message_id=raw_id,
        payload={"verdict": "correct", "error_kind": None, "note": None},
        now=updated_at,
    )
    assert updated["id"] == saved["id"]
    assert updated["created_at"] == created_at
    assert updated["updated_at"] == updated_at
    assert updated["labeled_recognition_result"] == "非策略"
    assert updated["labeled_event_type"] == "none"
    assert updated["labeled_confidence"] == 0.72
    with session_factory() as session:
        assert session.query(MessageRecognitionLabel).count() == 1


def test_label_snapshot_falls_back_to_decision_prompt_provenance(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = _add_message(session)
        _add_decision(
            session,
            raw.id,
            model="decision-only-model",
            prompt_versions_json='{"decision":"fallback"}',
        )
        session.commit()
        raw_id = raw.id

    saved = save_message_recognition_label(
        session_factory,
        raw_message_id=raw_id,
        payload={"verdict": "uncertain"},
    )

    assert saved["labeled_model"] == "decision-only-model"
    assert saved["labeled_prompt_versions_json"] == '{"decision":"fallback"}'
    assert saved["labeled_prompt_versions_source"] == "recognition_decision"


def test_label_snapshot_preserves_unavailable_facts_as_none(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = _add_message(session)
        session.commit()
        raw_id = raw.id

    saved = save_message_recognition_label(
        session_factory,
        raw_message_id=raw_id,
        payload={"verdict": "uncertain", "note": ""},
    )

    assert saved["note"] is None
    assert saved["labeled_recognition_result"] is None
    assert saved["labeled_event_type"] is None
    assert saved["labeled_confidence"] is None
    assert saved["labeled_model"] is None
    assert saved["labeled_prompt_versions_json"] is None
    assert saved["labeled_prompt_versions_source"] is None
    assert saved["labeled_signal_candidate_count"] == 0
    assert saved["labeled_accepted_candidate_count"] is None
    assert saved["labeled_context_attempt_status"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"verdict": "wrong"},
        {"verdict": "correct", "error_kind": "wrong_target"},
        {"verdict": "incorrect", "error_kind": "not_an_error_kind"},
        {"verdict": "correct", "note": "x" * 2001},
        {"verdict": "correct", "labeled_confidence": 1.0},
        {"verdict": "correct", "extra": "not accepted"},
    ],
)
def test_label_payload_validation_is_strict(tmp_path, payload):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = _add_message(session)
        session.commit()
        raw_id = raw.id

    with pytest.raises(ValueError):
        save_message_recognition_label(
            session_factory,
            raw_message_id=raw_id,
            payload=payload,
        )
    assert load_message_recognition_label(
        session_factory, raw_message_id=raw_id
    ) is None


def test_label_lookup_distinguishes_missing_message_from_unlabeled(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = _add_message(session)
        session.commit()
        raw_id = raw.id

    assert load_message_recognition_label(
        session_factory, raw_message_id=raw_id
    ) is None
    with pytest.raises(LookupError):
        load_message_recognition_label(
            session_factory, raw_message_id=raw_id + 999
        )


def test_label_note_boundary_accepts_exactly_2000_characters(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = _add_message(session)
        session.commit()
        raw_id = raw.id

    saved = save_message_recognition_label(
        session_factory,
        raw_message_id=raw_id,
        payload={"verdict": "incorrect", "note": "x" * 2000},
    )
    assert len(saved["note"]) == 2000


def test_message_projection_bulk_loads_label_and_computes_render_only_drift(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = _add_message(session)
        _add_decision(session, raw.id)
        session.commit()
        raw_id = raw.id

    save_message_recognition_label(
        session_factory,
        raw_message_id=raw_id,
        payload={"verdict": "incorrect", "error_kind": "wrong_parameters"},
    )
    unchanged = load_group_messages(
        session_factory, chat_id=77, limit=10, include_recognition_labels=True
    )[0]
    assert unchanged["recognition_label"]["verdict"] == "incorrect"
    assert unchanged["recognition_label_has_drift"] is False

    with session_factory() as session:
        decision = session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one()
        decision.authoritative_payload_json = json.dumps(
            {
                "recognition_result": "是策略",
                "lifecycle_event": {"event_type": "position_update"},
                "confidence": 0.91,
            },
            ensure_ascii=False,
        )
        session.commit()

    changed = load_group_messages(
        session_factory, chat_id=77, limit=10, include_recognition_labels=True
    )[0]
    assert changed["recognition_label"]["labeled_event_type"] == "entry_signal"
    assert changed["lifecycle_event_type"] == "position_update"
    assert changed["recognition_label_has_drift"] is True
    with session_factory() as session:
        persisted = session.query(MessageRecognitionLabel).one()
        assert persisted.labeled_event_type == "entry_signal"


@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("recognition_result", "非策略"),
        ("event_type", "position_update"),
        ("confidence", 0.73),
    ],
)
def test_label_drift_compares_each_current_recognition_fact(
    tmp_path, changed_field, changed_value
):
    session_factory = create_session_factory(tmp_path / f"{changed_field}.db")
    with session_factory() as session:
        raw = _add_message(session)
        _add_decision(session, raw.id)
        session.commit()
        raw_id = raw.id
    save_message_recognition_label(
        session_factory,
        raw_message_id=raw_id,
        payload={"verdict": "incorrect"},
    )

    changed_payload = {
        "recognition_result": "是策略",
        "lifecycle_event": {"event_type": "entry_signal"},
        "confidence": 0.91,
    }
    if changed_field == "event_type":
        changed_payload["lifecycle_event"]["event_type"] = changed_value
    else:
        changed_payload[changed_field] = changed_value
    with session_factory() as session:
        decision = session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one()
        decision.authoritative_payload_json = json.dumps(
            changed_payload, ensure_ascii=False
        )
        session.commit()

    projected = load_group_messages(
        session_factory, chat_id=77, limit=10, include_recognition_labels=True
    )[0]
    assert projected["recognition_label_has_drift"] is True
    with session_factory() as session:
        label = session.query(MessageRecognitionLabel).one()
        assert label.labeled_recognition_result == "是策略"
        assert label.labeled_event_type == "entry_signal"
        assert label.labeled_confidence == 0.91


@pytest.mark.parametrize(
    ("current_confidence", "expected_drift"),
    [
        (0.9100000005, False),
        (0.910000002, True),
    ],
)
def test_label_drift_compares_confidence_with_tolerance(
    tmp_path, current_confidence, expected_drift
):
    session_factory = create_session_factory(
        tmp_path / f"confidence-tolerance-{expected_drift}.db"
    )
    with session_factory() as session:
        raw = _add_message(session)
        _add_decision(session, raw.id, confidence=0.91)
        session.commit()
        raw_id = raw.id
    save_message_recognition_label(
        session_factory,
        raw_message_id=raw_id,
        payload={"verdict": "incorrect"},
    )

    with session_factory() as session:
        decision = session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_id
        ).one()
        decision.authoritative_payload_json = json.dumps(
            {
                "recognition_result": "是策略",
                "lifecycle_event": {"event_type": "entry_signal"},
                "confidence": current_confidence,
            },
            ensure_ascii=False,
        )
        session.commit()

    projected = load_group_messages(
        session_factory, chat_id=77, limit=10, include_recognition_labels=True
    )[0]
    assert projected["recognition_label_has_drift"] is expected_drift


def test_non_ui_message_projection_does_not_read_human_labels(tmp_path):
    session_factory = create_session_factory(tmp_path / "projection-boundary.db")
    with session_factory() as session:
        raw = _add_message(session)
        session.commit()
        raw_id = raw.id
    save_message_recognition_label(
        session_factory,
        raw_message_id=raw_id,
        payload={"verdict": "correct"},
    )
    statements = []
    engine = session_factory.kw["bind"]

    def capture_statement(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture_statement)
    try:
        without_labels = load_group_messages(
            session_factory, chat_id=77, limit=10
        )[0]
        default_statements = list(statements)
        statements.clear()
        with_labels = load_group_messages(
            session_factory,
            chat_id=77,
            limit=10,
            include_recognition_labels=True,
        )[0]
        ui_statements = list(statements)
    finally:
        event.remove(engine, "before_cursor_execute", capture_statement)

    assert without_labels["recognition_label"] is None
    assert not any("message_recognition_labels" in sql for sql in default_statements)
    assert with_labels["recognition_label"]["verdict"] == "correct"
    assert any("message_recognition_labels" in sql for sql in ui_statements)


@pytest.mark.parametrize("owned_role", ["web", "all"])
def test_recognition_label_api_create_update_get_and_validation(tmp_path, owned_role):
    database_path = tmp_path / f"{owned_role}.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw = _add_message(session)
        _add_decision(session, raw.id)
        session.commit()
        raw_id = raw.id
    client = TestClient(
        create_web_app(database_path=database_path, runtime_role=owned_role)
    )

    empty = client.get(f"/api/messages/{raw_id}/recognition-label")
    assert empty.status_code == 200
    assert empty.json() == {"raw_message_id": raw_id, "label": None}

    created = client.post(
        f"/api/messages/{raw_id}/recognition-label",
        json={
            "verdict": "incorrect",
            "error_kind": "wrong_target",
            "note": "目标错了",
        },
    )
    assert created.status_code == 200
    assert created.json()["label"]["verdict"] == "incorrect"
    assert created.json()["label"]["labeled_recognition_result"] == "是策略"

    updated = client.post(
        f"/api/messages/{raw_id}/recognition-label",
        json={"verdict": "correct", "error_kind": None, "note": None},
    )
    assert updated.status_code == 200
    assert updated.json()["label"]["id"] == created.json()["label"]["id"]
    assert client.get(
        f"/api/messages/{raw_id}/recognition-label"
    ).json()["label"]["verdict"] == "correct"

    for invalid_payload in (
        {"verdict": "correct", "note": "x" * 2001},
        {"verdict": "incorrect", "error_kind": "wrong_target", "labeled_confidence": 1},
        {"verdict": "correct", "error_kind": "wrong_target"},
    ):
        response = client.post(
            f"/api/messages/{raw_id}/recognition-label", json=invalid_payload
        )
        assert response.status_code == 422
    assert client.get(
        f"/api/messages/{raw_id}/recognition-label"
    ).json()["label"]["verdict"] == "correct"

    assert client.get(
        f"/api/messages/{raw_id + 999}/recognition-label"
    ).status_code == 404
    assert client.post(
        f"/api/messages/{raw_id + 999}/recognition-label",
        json={"verdict": "correct"},
    ).status_code == 404


@pytest.mark.parametrize("runtime_role", ["worker", "ingest"])
def test_recognition_label_api_is_not_owned_by_worker_or_ingest(tmp_path, runtime_role):
    database_path = tmp_path / f"{runtime_role}.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw = _add_message(session)
        session.commit()
        raw_id = raw.id
    client = TestClient(
        create_web_app(database_path=database_path, runtime_role=runtime_role)
    )

    for response in (
        client.get(f"/api/messages/{raw_id}/recognition-label"),
        client.post(
            f"/api/messages/{raw_id}/recognition-label",
            json={"verdict": "correct"},
        ),
    ):
        assert response.status_code == 503
        assert response.json()["detail"] == {
            "code": "label_not_owned_by_runtime_role"
        }
    with session_factory() as session:
        assert session.query(MessageRecognitionLabel).count() == 0


def _card_fragment(body, raw_message_id):
    marker = f'id="message-{raw_message_id}"'
    marker_index = body.index(marker)
    start = body.rfind("<article", 0, marker_index)
    next_card = body.find('\n      <article\n        class="message-card', marker_index)
    return body[start : next_card if next_card >= 0 else len(body)]


def test_message_page_renders_human_labels_form_and_read_only_drift_hint(tmp_path):
    database_path = tmp_path / "labels-ui.db"
    session_factory = create_session_factory(database_path)
    raw_ids = {}
    with session_factory() as session:
        for index, name in enumerate(
            ("correct", "incorrect", "uncertain", "unlabeled"), start=1
        ):
            raw = _add_message(session, message_id=700 + index)
            raw.text = name
            _add_decision(
                session,
                raw.id,
                recognition_result="非策略",
                event_type="none",
                confidence=0.9,
            )
            raw_ids[name] = raw.id
        session.commit()

    save_message_recognition_label(
        session_factory,
        raw_message_id=raw_ids["correct"],
        payload={"verdict": "correct", "note": "人工确认"},
    )
    save_message_recognition_label(
        session_factory,
        raw_message_id=raw_ids["incorrect"],
        payload={
            "verdict": "incorrect",
            "error_kind": "should_be_strategy",
            "note": "后续识别可能已修正",
        },
    )
    save_message_recognition_label(
        session_factory,
        raw_message_id=raw_ids["uncertain"],
        payload={"verdict": "uncertain"},
    )
    with session_factory() as session:
        decision = session.query(RecognitionDecision).filter_by(
            raw_message_id=raw_ids["incorrect"]
        ).one()
        decision.authoritative_payload_json = json.dumps(
            {
                "recognition_result": "是策略",
                "lifecycle_event": {"event_type": "none"},
                "confidence": 0.9,
            },
            ensure_ascii=False,
        )
        session.commit()

    body = TestClient(
        create_web_app(database_path=database_path, runtime_role="web")
    ).get("/groups/77/messages").text
    correct = _card_fragment(body, raw_ids["correct"])
    incorrect = _card_fragment(body, raw_ids["incorrect"])
    uncertain = _card_fragment(body, raw_ids["uncertain"])
    unlabeled = _card_fragment(body, raw_ids["unlabeled"])

    assert 'data-message-labeled="true"' in correct
    assert 'message-ai-chip is-human-correct' in correct
    assert "人工标注：正确" in correct
    assert 'name="verdict" value="correct" checked' in correct
    assert 'data-recognition-label-error-fields hidden' in correct
    assert "人工确认" in correct

    assert 'message-ai-chip is-human-incorrect' in incorrect
    assert "人工标注：错了" in incorrect
    assert 'name="verdict" value="incorrect" checked' in incorrect
    assert '<option value="should_be_strategy" selected>' in incorrect
    drift_tag = next(
        tag for tag in incorrect.split("<") if "识别已变更" in tag
    )
    assert "is-label-drift" in drift_tag
    assert "is-danger" not in drift_tag
    assert "is-warning" not in drift_tag
    assert "该标注针对的是标注当时的识别结果" in incorrect
    assert 'data-message-needs-attention="false"' in incorrect

    assert 'message-ai-chip is-human-uncertain' in uncertain
    assert "人工标注：不确定" in uncertain
    assert "识别已变更" not in uncertain

    assert 'data-message-labeled="false"' in unlabeled
    assert "人工标注：" not in unlabeled
    assert 'data-recognition-label-toggle' in unlabeled
    assert re.search(r'<form[^>]*data-recognition-label-form[^>]*hidden', unlabeled)
    assert 'name="note" maxlength="2000"' in unlabeled
