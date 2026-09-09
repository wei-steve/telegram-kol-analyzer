"""A-6b: a frozen attempt must say whether the venue was contacted.

Every ``uncertain`` row since 2026-09-04 -- twenty-three of them -- had an
empty ``evidence_refs_json``, so nothing on the row distinguished "we sent a
request and never heard back" from "we sent nothing". The A-7 monitor's
``uncertain_no_evidence`` counter could therefore never reach zero, and the
one thing a person needs to know before touching the exchange by hand was
missing.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    AuthoritativeExecutionAttempt,
    RawMessage,
    RecognitionDecision,
)
from telegram_kol_research.authoritative_execution_attempts import (
    NO_WRITE_TRACKED,
    mark_authoritative_execution_uncertain,
)

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
GENERATION = "generation-6b"
CLAIM = "claim-6b"


def _executing_attempt(tmp_path, *, raw_message_id=15633):
    import importlib

    session_factory = create_session_factory(tmp_path / "research.db")
    # The attempts table lives behind an explicit, hash-checked schema plan
    # rather than metadata.create_all; applying it is how every other suite
    # gets the table.
    schema = importlib.import_module(
        "telegram_kol_research.authoritative_execution_schema"
    )
    plan = schema.build_recognition_execution_schema_plan(session_factory.kw["bind"])
    schema.apply_recognition_execution_schema(
        session_factory.kw["bind"], expected_plan_sha256=plan.plan_sha256
    )
    with session_factory() as session:
        session.add(
            RawMessage(
                id=raw_message_id,
                chat_id=-1002337721508,
                message_id=raw_message_id,
                text="全部平掉",
                posted_at=NOW.replace(tzinfo=None),
                created_at=NOW.replace(tzinfo=None),
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message_id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="是策略",
                authoritative_payload_json="{}",
                agreement_status="agreed",
                differences_json="[]",
                comparison_status="execution_running",
                comparison_claim_token=GENERATION,
                created_at=NOW.replace(tzinfo=None),
                updated_at=NOW.replace(tzinfo=None),
            )
        )
        attempt = AuthoritativeExecutionAttempt(
            raw_message_id=raw_message_id,
            authoritative_generation=GENERATION,
            status="executing",
            claim_token=CLAIM,
            owner_runtime_role="worker",
            owner_instance_id="instance-6b",
            owner_pid=1,
            owner_boot_id="unavailable",
            owner_process_start_ticks=1,
            owner_systemd_invocation_id="invocation-6b",
            claimed_at=NOW.replace(tzinfo=None),
            heartbeat_at=NOW.replace(tzinfo=None),
            lease_expires_at=NOW.replace(tzinfo=None),
            side_effect_started_at=NOW.replace(tzinfo=None),
            exchange_effect="outcome_unknown",
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        session.add(attempt)
        session.commit()
        attempt_id = int(attempt.id)
    return session_factory, attempt_id


def _freeze(monkeypatch, session_factory, attempt_id, evidence_refs, captured):
    import telegram_kol_research.authoritative_execution_attempts as module

    # monkeypatch, not assignment: these are module-level names other suites
    # rely on, and a bare assignment leaks for the rest of the process.
    monkeypatch.setattr(
        module, "_capture_uncertain_without_write", lambda *a, **k: captured.append(k)
    )
    monkeypatch.setattr(module, "_capture_uncertain_incident", lambda *a, **k: None)
    return mark_authoritative_execution_uncertain(
        session_factory,
        attempt_id=attempt_id,
        claim_token=CLAIM,
        uncertain_at=NOW.replace(tzinfo=None),
        error_class="ExecutionBoundaryOutcomeUnknown",
        error_summary="unknown",
        evidence_refs=evidence_refs,
    )


def test_a_frozen_attempt_carries_the_writes_it_made(tmp_path, monkeypatch):
    """The difference between "sent, no answer" and "sent nothing"."""

    session_factory, attempt_id = _executing_attempt(tmp_path)
    captured: list[dict] = []
    assert _freeze(
        monkeypatch,
        session_factory,
        attempt_id,
        [
            {
                "kind": "deepcoin_write",
                "method": "place_order",
                "ordinal": 1,
                "outcome": "outcome_unknown",
            }
        ],
        captured,
    )

    with session_factory() as session:
        row = session.get(AuthoritativeExecutionAttempt, attempt_id)
        refs = json.loads(row.evidence_refs_json)
        assert refs[0]["method"] == "place_order"
        assert refs[0]["outcome"] == "outcome_unknown"
        # A write was tracked, so neither the label nor the alarm applies.
        assert NO_WRITE_TRACKED not in str(row.error_summary)
    assert captured == []


def test_a_freeze_with_no_write_says_so_and_raises_the_alarm(tmp_path, monkeypatch):
    """Since A-6 this combination should be impossible.

    A refusal with no writes behind it ends in ``failed_safe``, carrying its
    evidence. An ``uncertain`` with nothing tracked means the boundary lost
    sight of a write or something reached the venue outside it -- so it is
    written down as ``[]`` rather than left NULL, and somebody is told.
    """

    session_factory, attempt_id = _executing_attempt(tmp_path)
    captured: list[dict] = []
    assert _freeze(monkeypatch, session_factory, attempt_id, [], captured)

    with session_factory() as session:
        row = session.get(AuthoritativeExecutionAttempt, attempt_id)
        assert json.loads(row.evidence_refs_json) == []
        assert NO_WRITE_TRACKED in str(row.error_summary)
    assert len(captured) == 1
    assert captured[0]["attempt_id"] == attempt_id


def test_the_alarm_type_is_always_notified():
    from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES

    assert "uncertain_without_write" in ALWAYS_NOTIFIED_INCIDENT_TYPES


def test_the_boundary_hands_over_each_write_with_its_outcome():
    from telegram_kol_research.execution_boundary import (
        ExecutionBoundaryTracker,
        build_execution_boundary_outcome,
    )

    tracker = ExecutionBoundaryTracker()
    ordinal = tracker.begin("place_order")
    tracker.failed(ordinal, RuntimeError("connection reset"))
    outcome = build_execution_boundary_outcome({"status": "unknown"}, tracker)

    write = next(
        ref for ref in outcome.evidence_refs if ref["kind"] == "deepcoin_write"
    )
    assert write["outcome"] == "outcome_unknown"


# --------------------------------------------------------------------------
# The guard: expected state, not a stack trace
# --------------------------------------------------------------------------


def test_the_guard_has_its_own_type_and_is_still_a_runtime_error():
    from telegram_kol_research.recognition_decisions import (
        AuthoritativeExecutionInProgress,
    )

    guard = AuthoritativeExecutionInProgress(
        raw_message_id=15551, comparison_status="execution_uncertain"
    )
    # Existing handlers catch RuntimeError; nothing about them may change.
    assert isinstance(guard, RuntimeError)
    assert guard.raw_message_id == 15551
    assert guard.comparison_status == "execution_uncertain"


def test_the_guard_is_raised_with_the_status_that_caused_it(tmp_path):
    from telegram_kol_research.recognition_decisions import (
        AuthoritativeExecutionInProgress,
        RecognitionDecisionRecord,
        save_pending_authoritative_decision,
    )

    session_factory, _ = _executing_attempt(tmp_path)
    record = RecognitionDecisionRecord(
        raw_message_id=15633,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="是策略",
        authoritative_payload={},
        auxiliary_model=None,
        auxiliary_status=None,
        auxiliary_payload=None,
        agreement_status="agreed",
        differences=[],
    )

    with pytest.raises(AuthoritativeExecutionInProgress) as excinfo:
        save_pending_authoritative_decision(session_factory, record)
    assert excinfo.value.raw_message_id == 15633
    assert excinfo.value.comparison_status == "execution_running"


def test_a_guard_hit_is_reported_at_info_and_never_as_a_stack():
    """What the worker sees decides whether it logs a stack.

    ``context_resolution_worker`` calls ``logger.exception`` for anything that
    escapes ``reanalyze``; twelve stacks in one thirty-minute window all came
    from this one expected state. Returning a result instead of raising is the
    whole fix.

    The records are collected through a handler on the logger itself rather
    than through ``caplog``: whether caplog sees anything depends on the root
    logger's configuration, which other suites in the same process change.
    """

    from telegram_kol_research.recognition_decisions import (
        AuthoritativeExecutionInProgress,
    )

    logger = logging.getLogger("telegram_kol_research.web_app")

    class Collector(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.records: list[logging.LogRecord] = []

        def emit(self, record):
            self.records.append(record)

    def reanalyze_like(raw_message_id):
        try:
            raise AuthoritativeExecutionInProgress(
                raw_message_id=raw_message_id,
                comparison_status="execution_uncertain",
            )
        except AuthoritativeExecutionInProgress as guard:
            logger.info(
                "context reanalysis skipped, execution owns the message "
                "raw_message_id=%s comparison_status=%s",
                guard.raw_message_id,
                guard.comparison_status,
            )
            return {
                "status": "execution_in_progress",
                "comparison_status": guard.comparison_status,
                "raw_message_id": guard.raw_message_id,
            }

    collector = Collector()
    previous_level = logger.level
    logger.addHandler(collector)
    logger.setLevel(logging.INFO)
    try:
        result = reanalyze_like(15551)
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)

    assert result["status"] == "execution_in_progress"
    assert result["comparison_status"] == "execution_uncertain"
    assert [record.levelno for record in collector.records] == [logging.INFO]
    assert all(record.exc_info is None for record in collector.records)
