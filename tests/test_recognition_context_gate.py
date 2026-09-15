"""The contextual second pass records why it did or did not run.

Both gates deciding the second pass used to live only in memory, so a message
with no ``context_resolution_attempts`` row was indistinguishable from one the
resolver was never asked about. ``recognition_decisions.context_resolution_gate_json``
is the witness; these tests pin what it holds and how the Web card reads it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.authoritative_recognition import (
    process_authoritative_message,
)
from telegram_kol_research.context_resolution import ContextResolutionDecision
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    RawMessage,
    RecognitionDecision,
)
from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult
from telegram_kol_research.recognition_decisions import (
    RecognitionDecisionRecord,
    save_pending_authoritative_decision,
    save_terminal_authoritative_decision,
)
from telegram_kol_research.web_queries import _serialize_context_resolution


NOW = datetime(2026, 9, 15, 3, 0, tzinfo=UTC).replace(tzinfo=None)


def _record(raw_message_id: int, gate: dict | None) -> RecognitionDecisionRecord:
    return RecognitionDecisionRecord(
        raw_message_id=raw_message_id,
        input_kind="text",
        authoritative_model="gpt-5.6-luna",
        authoritative_status="是策略",
        authoritative_payload={"recognition_result": "是策略"},
        auxiliary_model=None,
        auxiliary_status=None,
        auxiliary_payload=None,
        agreement_status="pending",
        differences=[],
        context_resolution_gate=gate,
    )


def _raw_message(session_factory, *, message_id: int) -> int:
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=message_id, text="测试消息")
        session.add(raw)
        session.commit()
        return int(raw.id)


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome, triggers",
    [
        ("invoked", ["revision_language", "multiple_same_source_candidates"]),
        ("not_needed", []),
        ("resolver_disabled", ["cancellation_language"]),
        ("recognition_failed", []),
    ],
)
def test_recognition_decision_round_trips_every_gate_outcome(
    tmp_path, outcome, triggers
):
    session_factory = create_session_factory(tmp_path / f"gate-{outcome}.db")
    raw_id = _raw_message(session_factory, message_id=100)

    save_pending_authoritative_decision(
        session_factory,
        _record(raw_id, {"outcome": outcome, "triggers": triggers}),
    )

    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_id)
            .one()
        )
        assert json.loads(row.context_resolution_gate_json) == {
            "outcome": outcome,
            "triggers": triggers,
        }


def test_recognition_decision_gate_survives_the_update_path(tmp_path):
    """A re-recognition rewrites the gate rather than keeping the stale one."""

    session_factory = create_session_factory(tmp_path / "gate-update.db")
    raw_id = _raw_message(session_factory, message_id=101)

    save_pending_authoritative_decision(
        session_factory,
        _record(raw_id, {"outcome": "not_needed", "triggers": []}),
    )
    save_pending_authoritative_decision(
        session_factory,
        _record(raw_id, {"outcome": "invoked", "triggers": ["text_image_conflict"]}),
    )

    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_id)
            .one()
        )
        assert json.loads(row.context_resolution_gate_json) == {
            "outcome": "invoked",
            "triggers": ["text_image_conflict"],
        }


def test_recovery_guard_without_a_gate_does_not_erase_the_recorded_one(tmp_path):
    """The recovery guard never evaluates the gates, so it must not clear them.

    ``telegram_live_listener`` writes a terminal decision for a message whose
    recognition never finished. It passes no gate; overwriting the column with
    NULL would destroy the only record of why the second pass ran.
    """

    session_factory = create_session_factory(tmp_path / "gate-preserve.db")
    raw_id = _raw_message(session_factory, message_id=102)

    save_pending_authoritative_decision(
        session_factory,
        _record(raw_id, {"outcome": "invoked", "triggers": ["revision_language"]}),
    )
    save_terminal_authoritative_decision(session_factory, _record(raw_id, None))

    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_id)
            .one()
        )
        assert json.loads(row.context_resolution_gate_json) == {
            "outcome": "invoked",
            "triggers": ["revision_language"],
        }


def test_sqlite_compat_adds_the_gate_column_to_an_existing_database(tmp_path):
    """An old database gains the column without a migration step."""

    database_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE recognition_decisions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "raw_message_id INTEGER NOT NULL, "
        "input_kind VARCHAR(32) NOT NULL, "
        "authoritative_model VARCHAR(128) NOT NULL, "
        "authoritative_status VARCHAR(32) NOT NULL, "
        "authoritative_payload_json TEXT NOT NULL, "
        "agreement_status VARCHAR(32) NOT NULL, "
        "differences_json TEXT NOT NULL DEFAULT '[]', "
        "created_at DATETIME, updated_at DATETIME)"
    )
    connection.commit()
    connection.close()

    session_factory = create_session_factory(database_path)

    with session_factory() as session:
        columns = {
            row[1]
            for row in session.execute(
                text("PRAGMA table_info(recognition_decisions)")
            ).all()
        }
    assert "context_resolution_gate_json" in columns


# --------------------------------------------------------------------------
# process_authoritative_message: which outcome each path records
# --------------------------------------------------------------------------


def _install_first_pass(monkeypatch, *, raw_id: int, status: str = "是策略"):
    payload = (
        {}
        if status == "识别失败"
        else {
            "recognition_result": "是策略",
            "strategy": {
                "symbol": "SOL",
                "side": "long",
                "entry": "市价进",
                "stop_loss": "180",
                "take_profit": "200",
            },
            "lifecycle_event": {"event_type": "none", "confidence": 0.0},
            "confidence": 0.95,
        }
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition"
        ".run_mimo_authoritative_for_message",
        lambda *args, **kwargs: MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload=payload,
            input_kind="text",
            model="gpt-5.6-luna",
            status=status,
            error_message=("provider unreachable" if status == "识别失败" else None),
        ),
    )


def _stored_gate(session_factory, raw_id: int) -> dict:
    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_id)
            .one()
        )
        return json.loads(row.context_resolution_gate_json or "null")


def test_gate_records_not_needed_when_no_signal_fires(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "not-needed.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=92,
            message_id=1700,
            text="SOL 新多单，市价进，止损 180，止盈 200",
        )
        session.add(raw)
        session.commit()
        raw_id = int(raw.id)
    _install_first_pass(monkeypatch, raw_id=raw_id)

    process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        context_resolver=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("no signal fired; the resolver must not be called")
        ),
    )

    assert _stored_gate(session_factory, raw_id) == {
        "outcome": "not_needed",
        "triggers": [],
    }


def test_gate_records_resolver_disabled_when_the_group_gate_is_shut(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "resolver-disabled.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=92,
            message_id=1701,
            text="SOL 多单，止损改为 180，取消原来的挂单",
        )
        session.add(raw)
        session.commit()
        raw_id = int(raw.id)
    _install_first_pass(monkeypatch, raw_id=raw_id)

    # context_resolver=None is exactly what the web and CLI entry points pass
    # when TradingSettings has the group's contextual pass switched off.
    process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        context_resolver=None,
    )

    gate = _stored_gate(session_factory, raw_id)
    assert gate["outcome"] == "resolver_disabled"
    assert "revision_language" in gate["triggers"]
    assert "cancellation_language" in gate["triggers"]


def test_gate_records_recognition_failed_before_any_gate_is_evaluated(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "recognition-failed.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=92,
            message_id=1702,
            text="SOL 多单，止损改为 180",
        )
        session.add(raw)
        session.commit()
        raw_id = int(raw.id)
    _install_first_pass(monkeypatch, raw_id=raw_id, status="识别失败")

    process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        context_resolver=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("a failed first pass must not reach the resolver")
        ),
    )

    assert _stored_gate(session_factory, raw_id) == {
        "outcome": "recognition_failed",
        "triggers": [],
    }


def test_gate_records_invoked_when_the_resolver_runs(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "invoked.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=92,
            message_id=1703,
            text="SOL 多单，止损改为 180",
        )
        session.add(raw)
        session.commit()
        raw_id = int(raw.id)
    _install_first_pass(monkeypatch, raw_id=raw_id)
    calls: list[tuple[str, ...]] = []

    def _resolver(**kwargs):
        calls.append(tuple(kwargs["invocation_triggers"]))
        return ContextResolutionDecision(
            raw_message_id=raw_id,
            decision="unresolved",
            target_thread_ids=[],
            confidence=0.2,
            reason="no prior thread",
            supporting_message_ids=[],
            opposing_message_ids=[],
            next_triggers=[],
            model="gpt-5.6-luna",
        )

    process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        context_resolver=_resolver,
    )

    assert calls and "revision_language" in calls[0]
    gate = _stored_gate(session_factory, raw_id)
    assert gate["outcome"] == "invoked"
    assert "revision_language" in gate["triggers"]


def test_gate_still_records_invoked_when_the_resolver_raises(tmp_path, monkeypatch):
    """A resolver that blows up rewrites ``mimo`` into a recognition failure.

    Deciding the outcome after that rewrite would file the most alarming case
    -- the second pass ran and crashed -- as "recognition_failed", hiding it
    from exactly the reader who needs it.
    """

    session_factory = create_session_factory(tmp_path / "invoked-raised.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=92,
            message_id=1704,
            text="SOL 多单，止损改为 180",
        )
        session.add(raw)
        session.commit()
        raw_id = int(raw.id)
    _install_first_pass(monkeypatch, raw_id=raw_id)

    def _resolver(**kwargs):
        raise RuntimeError("context provider exploded")

    process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        context_resolver=_resolver,
    )

    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_id)
            .one()
        )
        # The failure is still recorded as a failure ...
        assert row.authoritative_status == "识别失败"
        # ... but the gate says the second pass was reached.
        assert json.loads(row.context_resolution_gate_json)["outcome"] == "invoked"


# --------------------------------------------------------------------------
# _serialize_context_resolution: the card's closed state enum
# --------------------------------------------------------------------------


def _attempt(status: str, *, triggers: list[str] | None = None, model="ctx-model"):
    return ContextResolutionAttempt(
        raw_message_id=1,
        context_fingerprint="sha256:ctx",
        model=model,
        status=status,
        invocation_triggers_json=(
            json.dumps(triggers) if triggers is not None else None
        ),
        decision_json=json.dumps({"decision": "same_strategy", "confidence": 0.9}),
    )


def _serialize(*, attempt=None, decision=None, model_labels=None):
    return _serialize_context_resolution(
        raw_message=RawMessage(chat_id=88, message_id=7, text="消息"),
        attempt=attempt,
        decision=decision,
        evidence=None,
        links=[],
        thread_history_by_thread_id={},
        model_labels=model_labels,
    )


def _decision_with_gate(outcome: str | None, triggers: list[str] | None = None):
    return RecognitionDecision(
        raw_message_id=1,
        input_kind="text",
        authoritative_model="gpt-5.6-luna",
        authoritative_status="是策略",
        authoritative_payload_json="{}",
        agreement_status="pending",
        differences_json="[]",
        context_resolution_gate_json=(
            None
            if outcome is None
            else json.dumps({"outcome": outcome, "triggers": triggers or []})
        ),
    )


@pytest.mark.parametrize(
    "status, expected",
    [
        ("pending", "in_progress"),
        ("running", "in_progress"),
        ("retry_pending", "in_progress"),
        ("pending_reanalysis", "in_progress"),
        ("exhausted", "exhausted"),
        ("blocked_disabled", "blocked_disabled"),
        ("blocked_execution_terminal", "blocked_terminal"),
        ("superseded", "superseded"),
        ("completed", "completed"),
    ],
)
def test_execution_state_from_attempt_status(status, expected):
    context = _serialize(attempt=_attempt(status))

    assert context["execution_state"] == expected


@pytest.mark.parametrize(
    "outcome, expected",
    [
        ("not_needed", "not_needed"),
        ("resolver_disabled", "disabled"),
        ("recognition_failed", "not_evaluated"),
        (None, "unknown"),
    ],
)
def test_execution_state_without_an_attempt_comes_from_the_gate(outcome, expected):
    context = _serialize(decision=_decision_with_gate(outcome))

    assert context["execution_state"] == expected
    assert context["gate_outcome"] == outcome


def test_recognised_message_without_an_attempt_still_gets_a_card():
    """Every recognised message answers "did the second pass run?"."""

    context = _serialize(decision=_decision_with_gate("not_needed"))

    assert context is not None
    assert context["attempt_status"] is None
    assert context["execution_state"] == "not_needed"


def test_message_never_recognised_gets_no_card():
    assert _serialize() is None


def test_gate_triggers_fall_back_to_the_attempt_for_historical_rows():
    """Rows written before the gate column still show why they were resolved."""

    context = _serialize(
        attempt=_attempt("completed", triggers=["multiple_same_source_candidates"]),
        decision=_decision_with_gate(None),
    )

    assert context["gate_outcome"] is None
    assert context["gate_triggers"] == ["multiple_same_source_candidates"]


def test_gate_column_triggers_win_over_the_attempt():
    context = _serialize(
        attempt=_attempt("completed", triggers=["text_image_conflict"]),
        decision=_decision_with_gate("invoked", ["revision_language"]),
    )

    assert context["gate_triggers"] == ["revision_language"]


def test_context_model_label_falls_back_to_the_raw_id_for_unknown_models():
    known = _serialize(
        attempt=_attempt("completed", model="mimo-v2.5"),
        model_labels={"mimo-v2.5": "MiMo V2.5"},
    )
    unknown = _serialize(
        attempt=_attempt("completed", model="retired-model-7"),
        model_labels={"mimo-v2.5": "MiMo V2.5"},
    )

    assert known["model"] == "mimo-v2.5"
    assert known["model_label"] == "MiMo V2.5"
    assert unknown["model_label"] == "retired-model-7"
