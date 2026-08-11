import json

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.mimo_recognition_runs import (
    MimoRecognitionRunConflict,
    MimoRecognitionRunValidationError,
    canonical_json_fingerprint,
    complete_mimo_run,
    load_mimo_attempts,
    record_mimo_attempt,
    start_mimo_run,
)
from telegram_kol_research.models import (
    MimoRecognitionAttempt,
    MimoRecognitionRun,
    RawMessage,
)


def _message(factory, *, chat_id: int = 7, message_id: int = 11) -> int:
    with factory() as session:
        row = RawMessage(chat_id=chat_id, message_id=message_id, text="test")
        session.add(row)
        session.commit()
        return row.id


def _start(factory, raw_message_id: int, **overrides):
    values = {
        "raw_message_id": raw_message_id,
        "run_kind": "v2_authoritative",
        "contract_version": "mimo-authoritative-v2",
        "model": "mimo-v2.5",
        "input_kind": "text+image",
        "input_fingerprint": "sha256:input",
        "prompt_versions": {"trading.analysis.mimo_v2_authoritative": 12},
    }
    values.update(overrides)
    return start_mimo_run(factory, **values)


def test_run_records_ordered_attempts_and_terminal_selection(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _message(factory)
    run = _start(factory, raw_message_id)

    first = record_mimo_attempt(
        factory,
        run_id=run.id,
        ordinal=1,
        status="timeout",
        error_code="provider_timeout",
        error_message=(
            "Authorization: Bearer private-token api_key=private-key request timed out"
        ),
        response_payload="timeout-body",
        duration_ms=1500,
    )
    second = record_mimo_attempt(
        factory,
        run_id=run.id,
        ordinal=2,
        retry_of_ordinal=1,
        status="completed",
        response_payload={"b": 2, "a": 1},
        duration_ms=230,
    )
    completed = complete_mimo_run(
        factory,
        run_id=run.id,
        status="completed",
        selected_ordinal=2,
        canonical_payload={"contract_version": "mimo-authoritative-v2", "a": 1},
        projection_payload={"recognition_result": "非策略", "a": 1},
        became_authoritative=True,
    )

    assert run.status == "running"
    assert run.attempt_count == 0
    assert completed.status == "completed"
    assert completed.attempt_count == 2
    assert completed.selected_attempt_ordinal == 2
    assert completed.became_authoritative is True
    assert completed.completed_at is not None
    assert len(completed.canonical_payload_fingerprint) == 64
    assert len(completed.projection_fingerprint) == 64
    assert first.error_message is not None
    assert "private-token" not in first.error_message
    assert "private-key" not in first.error_message
    assert "[redacted]" in first.error_message
    assert len(first.response_fingerprint) == 64
    assert len(second.response_fingerprint) == 64
    assert canonical_json_fingerprint({"b": 2, "a": 1}) == (
        canonical_json_fingerprint({"a": 1, "b": 2})
    )
    attempts = load_mimo_attempts(factory, run_id=run.id)
    assert [row.ordinal for row in attempts] == [1, 2]
    assert [row.retry_of_ordinal for row in attempts] == [None, 1]
    assert [row.selected for row in attempts] == [False, True]

    with factory() as session:
        stored = session.get(MimoRecognitionRun, run.id)
        assert json.loads(stored.prompt_versions_json) == {
            "trading.analysis.mimo_v2_authoritative": 12
        }
        assert session.query(MimoRecognitionAttempt).count() == 2


def test_attempts_are_append_only_and_terminal_run_is_guarded(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _message(factory)
    run = _start(factory, raw_message_id)

    with pytest.raises(MimoRecognitionRunConflict, match="next ordinal"):
        record_mimo_attempt(
            factory,
            run_id=run.id,
            ordinal=2,
            status="timeout",
        )
    record_mimo_attempt(
        factory,
        run_id=run.id,
        ordinal=1,
        status="completed",
        response_payload={"ok": True},
    )
    complete_mimo_run(
        factory,
        run_id=run.id,
        status="completed",
        selected_ordinal=1,
        canonical_payload={"ok": True},
        projection_payload={"ok": True},
        became_authoritative=False,
    )

    with pytest.raises(MimoRecognitionRunConflict, match="terminal"):
        record_mimo_attempt(
            factory,
            run_id=run.id,
            ordinal=2,
            retry_of_ordinal=1,
            status="completed",
        )
    with pytest.raises(MimoRecognitionRunConflict, match="terminal"):
        complete_mimo_run(
            factory,
            run_id=run.id,
            status="failed",
            selected_ordinal=None,
            final_error_code="contract_validation_failed",
            final_error_message="failed again",
        )


def test_failed_run_has_sanitized_terminal_error_and_no_selected_attempt(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _message(factory)
    run = _start(factory, raw_message_id)
    record_mimo_attempt(
        factory,
        run_id=run.id,
        ordinal=1,
        status="contract_failure",
        error_code="contract_validation_failed",
        error_message='password="hunter 2" malformed payload',
        response_payload={"unexpected": "json"},
    )

    failed = complete_mimo_run(
        factory,
        run_id=run.id,
        status="failed",
        selected_ordinal=None,
        final_error_code="contract_validation_failed",
        final_error_message='password="hunter 2" malformed payload',
    )

    assert failed.attempt_count == 1
    assert failed.selected_attempt_ordinal is None
    assert failed.became_authoritative is False
    assert failed.canonical_payload_fingerprint is None
    assert failed.projection_fingerprint is None
    assert "hunter" not in failed.final_error_message
    assert '2"' not in failed.final_error_message


def test_run_kinds_and_whole_run_retry_are_closed_and_linked(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    raw_message_id = _message(factory)
    v2 = _start(factory, raw_message_id)
    complete_mimo_run(
        factory,
        run_id=v2.id,
        status="failed",
        selected_ordinal=None,
        final_error_code="provider_timeout",
        final_error_message="timeout",
    )
    fallback = _start(
        factory,
        raw_message_id,
        run_kind="v1_fallback",
        contract_version="v1",
        prompt_versions={"trading.analysis.shared": 5},
        retry_of_run_id=v2.id,
    )
    direct_v1 = _start(
        factory,
        raw_message_id,
        run_kind="v1_authoritative",
        contract_version="v1",
        prompt_versions={"trading.analysis.shared": 5},
    )

    assert fallback.retry_of_run_id == v2.id
    assert direct_v1.run_kind == "v1_authoritative"
    with pytest.raises(MimoRecognitionRunValidationError, match="run kind"):
        _start(factory, raw_message_id, run_kind="shadow")


def test_retry_link_requires_terminal_run_for_same_message(tmp_path):
    factory = create_session_factory(tmp_path / "research.db")
    first_message_id = _message(factory, chat_id=1, message_id=1)
    second_message_id = _message(factory, chat_id=1, message_id=2)
    running = _start(factory, first_message_id)

    with pytest.raises(MimoRecognitionRunConflict, match="terminal"):
        _start(factory, first_message_id, retry_of_run_id=running.id)
    complete_mimo_run(
        factory,
        run_id=running.id,
        status="failed",
        selected_ordinal=None,
        final_error_code="provider_timeout",
        final_error_message="timeout",
    )
    with pytest.raises(MimoRecognitionRunConflict, match="same message"):
        _start(factory, second_message_id, retry_of_run_id=running.id)


@pytest.mark.parametrize(
    ("status", "selected_ordinal"),
    (("completed", None), ("failed", 1)),
)
def test_terminal_selection_matches_status(tmp_path, status, selected_ordinal):
    factory = create_session_factory(tmp_path / f"{status}.db")
    raw_message_id = _message(factory)
    run = _start(factory, raw_message_id)
    record_mimo_attempt(
        factory,
        run_id=run.id,
        ordinal=1,
        status="completed",
        response_payload={"ok": True},
    )

    with pytest.raises(MimoRecognitionRunValidationError, match="selected"):
        complete_mimo_run(
            factory,
            run_id=run.id,
            status=status,
            selected_ordinal=selected_ordinal,
            canonical_payload={"ok": True},
            projection_payload={"ok": True},
        )
