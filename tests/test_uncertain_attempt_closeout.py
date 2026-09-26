"""Closeout of frozen ``uncertain`` attempts, and the freeze it must preserve.

Design: ``docs/plans/2026-09-26-uncertain-attempt-closeout-design.md``.
Status: ``docs/uncertain-attempt-closeout-status.md``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from telegram_kol_research.authoritative_execution_attempts import (
    CLOSED_NO_WRITE,
    CLOSED_SETTLED_BINDING,
    CLOSEOUT_STATUSES,
)
from telegram_kol_research.authoritative_execution_schema import (
    apply_recognition_execution_schema,
    build_recognition_execution_schema_plan,
    validate_recognition_execution_schema,
)
from telegram_kol_research.db import (
    AUTHORITATIVE_EXECUTION_ATTEMPT_STATUSES,
    create_existing_session_factory,
    create_session_factory,
)
from telegram_kol_research.models import (
    AuthoritativeExecutionAttempt,
    Base,
    ExecutionBinding,
    ExecutionEvent,
    RawMessage,
    RecognitionDecision,
)
from telegram_kol_research.recognition_decisions import (
    AuthoritativeExecutionInProgress,
    RecognitionDecisionRecord,
    save_pending_authoritative_decision,
    save_terminal_authoritative_decision,
)
from telegram_kol_research.recognition_execution_scanner import (
    scan_recognition_execution_cycle,
)
from telegram_kol_research.uncertain_attempt_closeout import (
    BUCKET_EXCHANGE_WRITE,
    BUCKET_NON_WRITING_EVENTS_ONLY,
    BUCKET_NO_EXECUTION_EVENT,
    REFUSAL_BINDING_NOT_TERMINAL,
    REFUSAL_NO_BINDING_TO_VERIFY,
    TERMINAL_BINDING_STATES,
    UncertainAttemptCloseoutRefused,
    apply_uncertain_attempt_closeout,
    build_uncertain_attempt_closeout_plan,
)


NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
CLOSED_AT = datetime(2026, 9, 26, 13, tzinfo=UTC)


def _prepared(tmp_path, name="closeout.db"):
    session_factory = create_session_factory(tmp_path / name)
    engine = session_factory.kw["bind"]
    plan = build_recognition_execution_schema_plan(engine)
    apply_recognition_execution_schema(engine, expected_plan_sha256=plan.plan_sha256)
    return session_factory


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _uncertain_message(
    session_factory,
    *,
    message_id: int,
    events: tuple[tuple[str, dict], ...] = (),
    bindings: tuple[tuple[int, str], ...] = (),
) -> tuple[int, int]:
    """Create one frozen message: decision + uncertain attempt + its ledger.

    ``bindings`` is ``(binding_id, status)``; ``events`` is
    ``(action, extra column values)``.
    """

    with session_factory() as session:
        raw = RawMessage(
            chat_id=-100,
            message_id=message_id,
            sender_name="峰哥",
            posted_at=_naive(NOW) - timedelta(days=20),
            text="strategy",
        )
        session.add(raw)
        session.flush()
        raw_message_id = int(raw.id)
        generation = f"generation-{message_id}"
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message_id,
                input_kind="text",
                authoritative_model="recognition",
                authoritative_status="入场策略",
                authoritative_payload_json='{"recognition_result":"入场策略"}',
                agreement_status="pending",
                differences_json="[]",
                prompt_versions_json="{}",
                comparison_status="execution_uncertain",
                comparison_claim_token=generation,
                automation_status="uncertain",
                automation_reason="authoritative_execution_outcome_unknown",
                created_at=_naive(NOW),
                updated_at=_naive(NOW),
            )
        )
        for binding_id, status in bindings:
            session.add(
                ExecutionBinding(
                    id=binding_id,
                    kol_id="kol",
                    chat_id=-100,
                    message_id=message_id,
                    symbol="ETH-USDT-SWAP",
                    side="long",
                    venue="deepcoin",
                    status=status,
                )
            )
        session.flush()
        for action, extra in events:
            session.add(
                ExecutionEvent(
                    action=action,
                    venue="deepcoin",
                    status=extra.pop("status", "submitted"),
                    chat_id=-100,
                    message_id=message_id,
                    created_at=_naive(NOW),
                    **extra,
                )
            )
        attempt = AuthoritativeExecutionAttempt(
            raw_message_id=raw_message_id,
            authoritative_generation=generation,
            status="uncertain",
            claim_token=f"token-{message_id}",
            owner_runtime_role="worker",
            owner_instance_id="instance",
            owner_pid=1,
            owner_boot_id="boot",
            owner_process_start_ticks="1",
            claimed_at=_naive(NOW),
            heartbeat_at=_naive(NOW),
            lease_expires_at=_naive(NOW) + timedelta(minutes=5),
            side_effect_started_at=_naive(NOW),
            exchange_effect="outcome_unknown",
            error_class="ExpiredOwner",
            error_summary="expired owner identity is no longer alive",
            uncertain_at=_naive(NOW),
            completed_at=_naive(NOW),
            created_at=_naive(NOW),
            updated_at=_naive(NOW),
        )
        session.add(attempt)
        session.commit()
        return raw_message_id, int(attempt.id)


def _decision_snapshot(session_factory, raw_message_id: int) -> dict:
    with session_factory() as session:
        row = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id == raw_message_id)
            .one()
        )
        return {
            column.name: repr(getattr(row, column.name))
            for column in row.__table__.columns
        }


# --- bucketing -------------------------------------------------------------


def test_no_execution_event_bucket_closes_as_no_write(tmp_path):
    session_factory = _prepared(tmp_path)
    _, attempt_id = _uncertain_message(session_factory, message_id=1)

    plan = build_uncertain_attempt_closeout_plan(session_factory)

    assert len(plan.rows) == 1
    row = plan.rows[0]
    assert row.attempt_id == attempt_id
    assert row.bucket == BUCKET_NO_EXECUTION_EVENT
    assert row.closeout_status == CLOSED_NO_WRITE
    assert row.refusal_reason is None
    assert row.chat_id == -100
    assert row.sender_name == "峰哥"


def test_notification_only_events_still_close_as_no_write(tmp_path):
    session_factory = _prepared(tmp_path)
    _uncertain_message(
        session_factory,
        message_id=2,
        events=(
            ("management_target_confirmation_reminder", {"status": "manual_review"}),
            ("management_target_confirmation_timeout", {"status": "skipped"}),
        ),
    )

    plan = build_uncertain_attempt_closeout_plan(session_factory)

    row = plan.rows[0]
    assert row.bucket == BUCKET_NON_WRITING_EVENTS_ONLY
    assert row.event_types == (
        "management_target_confirmation_reminder",
        "management_target_confirmation_timeout",
    )
    assert row.writing_event_types == ()
    assert row.closeout_status == CLOSED_NO_WRITE


def test_real_write_with_settled_binding_closes_as_settled_binding(tmp_path):
    session_factory = _prepared(tmp_path)
    _uncertain_message(
        session_factory,
        message_id=3,
        bindings=((340, "closed"),),
        events=(
            (
                "open_market_position",
                {"order_id": "o-1", "execution_binding_id": 340},
            ),
            (
                "set_position_tpsl",
                {"order_id": "o-2", "execution_binding_id": 340},
            ),
        ),
    )

    plan = build_uncertain_attempt_closeout_plan(session_factory)

    row = plan.rows[0]
    assert row.bucket == BUCKET_EXCHANGE_WRITE
    assert row.writing_event_types == ("open_market_position", "set_position_tpsl")
    assert row.binding_ids == (340,)
    assert row.live_binding_ids == ()
    assert row.closeout_status == CLOSED_SETTLED_BINDING


def test_live_binding_refuses_closeout_and_says_which_binding(tmp_path):
    session_factory = _prepared(tmp_path)
    _uncertain_message(
        session_factory,
        message_id=4,
        bindings=((341, "open"),),
        events=(
            (
                "open_market_position",
                {"order_id": "o-3", "execution_binding_id": 341},
            ),
        ),
    )

    plan = build_uncertain_attempt_closeout_plan(session_factory)

    row = plan.rows[0]
    assert row.bucket == BUCKET_EXCHANGE_WRITE
    assert row.closeout_status is None
    assert row.refusal_reason == REFUSAL_BINDING_NOT_TERMINAL
    assert row.live_binding_ids == (341,)
    assert plan.closeable_rows == ()
    assert len(plan.refused_rows) == 1


def test_write_without_any_binding_is_refused_not_quietly_closed(tmp_path):
    session_factory = _prepared(tmp_path)
    _uncertain_message(
        session_factory,
        message_id=5,
        events=(("open_market_position", {"order_id": "o-4"}),),
    )

    plan = build_uncertain_attempt_closeout_plan(session_factory)

    row = plan.rows[0]
    assert row.closeout_status is None
    assert row.refusal_reason == REFUSAL_NO_BINDING_TO_VERIFY


def test_unlisted_action_without_identity_still_counts_as_a_write(tmp_path):
    """Fail-closed: an action nobody classified is treated as an exchange write."""

    session_factory = _prepared(tmp_path)
    _uncertain_message(
        session_factory,
        message_id=6,
        events=(("some_action_invented_later", {}),),
    )

    plan = build_uncertain_attempt_closeout_plan(session_factory)

    assert plan.rows[0].bucket == BUCKET_EXCHANGE_WRITE
    assert plan.rows[0].refusal_reason == REFUSAL_NO_BINDING_TO_VERIFY


def test_a_no_write_bucket_reports_a_live_binding_without_refusing(tmp_path):
    """The design's rule for the no-write buckets is the absence of a write.

    A binding under a message that never wrote is a ledger artefact, not
    exposure this attempt created, and nothing that watches live positions reads
    attempt status. So it is printed for the operator rather than turned into a
    refusal the design did not ask for -- recorded here so the choice is visible
    if it ever needs revisiting.
    """

    session_factory = _prepared(tmp_path)
    _uncertain_message(
        session_factory,
        message_id=21,
        bindings=((345, "open"),),
    )

    row = build_uncertain_attempt_closeout_plan(session_factory).rows[0]

    assert row.bucket == BUCKET_NO_EXECUTION_EVENT
    assert row.binding_ids == (345,)
    assert row.live_binding_ids == (345,)
    assert row.closeout_status == CLOSED_NO_WRITE


def test_terminal_binding_states_match_the_other_two_copies():
    from telegram_kol_research import historical_state_repair, position_take_profit_orders

    assert TERMINAL_BINDING_STATES == position_take_profit_orders._TERMINAL_BINDING_STATES
    assert TERMINAL_BINDING_STATES == historical_state_repair._TERMINAL_BINDING_STATES


# --- apply guards ----------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path):
    session_factory = _prepared(tmp_path)
    raw_message_id, attempt_id = _uncertain_message(session_factory, message_id=7)
    before = _decision_snapshot(session_factory, raw_message_id)

    plan = build_uncertain_attempt_closeout_plan(session_factory)

    assert plan.to_dict()["closeable_count"] == 1
    assert plan.to_dict()["exchange_write_count"] == 0
    with session_factory() as session:
        attempt = session.get(AuthoritativeExecutionAttempt, attempt_id)
        assert attempt.status == "uncertain"
        assert attempt.error_summary == "expired owner identity is no longer alive"
    assert _decision_snapshot(session_factory, raw_message_id) == before


def test_expected_count_mismatch_refuses_and_writes_nothing(tmp_path):
    session_factory = _prepared(tmp_path)
    _, attempt_id = _uncertain_message(session_factory, message_id=8)
    _uncertain_message(session_factory, message_id=9)

    with pytest.raises(UncertainAttemptCloseoutRefused) as excinfo:
        apply_uncertain_attempt_closeout(
            session_factory, expected_count=1, closed_at=CLOSED_AT
        )

    assert "expected_count_mismatch" in str(excinfo.value)
    with session_factory() as session:
        statuses = {
            int(row.id): str(row.status)
            for row in session.query(AuthoritativeExecutionAttempt).all()
        }
    assert set(statuses.values()) == {"uncertain"}


def test_apply_closes_both_buckets_and_stamps_the_reason(tmp_path):
    session_factory = _prepared(tmp_path)
    _, no_write_attempt = _uncertain_message(session_factory, message_id=10)
    _, settled_attempt = _uncertain_message(
        session_factory,
        message_id=11,
        bindings=((342, "cancelled"),),
        events=(
            (
                "open_market_position",
                {"order_id": "o-5", "execution_binding_id": 342},
            ),
        ),
    )

    result = apply_uncertain_attempt_closeout(
        session_factory, expected_count=2, closed_at=CLOSED_AT
    )

    assert result.changed_count == 2
    assert result.to_dict()["exchange_write_count"] == 0
    with session_factory() as session:
        first = session.get(AuthoritativeExecutionAttempt, no_write_attempt)
        second = session.get(AuthoritativeExecutionAttempt, settled_attempt)
        assert first.status == CLOSED_NO_WRITE
        assert second.status == CLOSED_SETTLED_BINDING
        assert first.error_summary.endswith("closed_out=closed_no_write@2026-09-26")
        assert second.error_summary.endswith(
            "closed_out=closed_settled_binding@2026-09-26"
        )
        # The freeze's own facts are left exactly as the execution wrote them:
        # what the venue did with that request is still unknown.
        assert first.exchange_effect == "outcome_unknown"
        assert first.uncertain_at == _naive(NOW)
        assert first.completed_at == _naive(NOW)
        assert first.updated_at == _naive(CLOSED_AT)


def test_apply_leaves_a_live_binding_row_frozen(tmp_path):
    session_factory = _prepared(tmp_path)
    _, closeable = _uncertain_message(session_factory, message_id=12)
    _, refused = _uncertain_message(
        session_factory,
        message_id=13,
        bindings=((343, "open"),),
        events=(
            (
                "open_market_position",
                {"order_id": "o-6", "execution_binding_id": 343},
            ),
        ),
    )

    result = apply_uncertain_attempt_closeout(
        session_factory, expected_count=1, closed_at=CLOSED_AT
    )

    assert result.changed_count == 1
    with session_factory() as session:
        assert session.get(
            AuthoritativeExecutionAttempt, closeable
        ).status == CLOSED_NO_WRITE
        assert session.get(AuthoritativeExecutionAttempt, refused).status == "uncertain"


# --- the safety constraint: the message stays frozen -----------------------


def test_closed_out_message_still_cannot_be_re_recognised(tmp_path):
    """The acceptance core of the whole change (design section 2).

    The closeout must not unfreeze the message. This drives the decision-row
    guard *directly* rather than through ``process_authoritative_message``,
    because that path is also blocked by the attempt-status retry guard -- and a
    test that cannot tell the two apart would stay green with the decision row
    wiped, which is precisely the failure this pins.
    """

    session_factory = _prepared(tmp_path)
    raw_message_id, _ = _uncertain_message(session_factory, message_id=14)
    before = _decision_snapshot(session_factory, raw_message_id)

    apply_uncertain_attempt_closeout(
        session_factory, expected_count=1, closed_at=CLOSED_AT
    )

    record = RecognitionDecisionRecord(
        raw_message_id=raw_message_id,
        input_kind="text",
        authoritative_model="recognition",
        authoritative_status="入场策略",
        authoritative_payload={"recognition_result": "入场策略"},
        auxiliary_model=None,
        auxiliary_status=None,
        auxiliary_payload=None,
        agreement_status="pending",
        differences=[],
    )
    with pytest.raises(AuthoritativeExecutionInProgress) as pending_guard:
        save_pending_authoritative_decision(session_factory, record)
    assert pending_guard.value.comparison_status == "execution_uncertain"

    with pytest.raises(AuthoritativeExecutionInProgress):
        save_terminal_authoritative_decision(session_factory, record)

    assert _decision_snapshot(session_factory, raw_message_id) == before


def test_closeout_leaves_every_decision_column_byte_identical(tmp_path):
    session_factory = _prepared(tmp_path)
    raw_message_id, _ = _uncertain_message(session_factory, message_id=15)
    before = _decision_snapshot(session_factory, raw_message_id)

    apply_uncertain_attempt_closeout(
        session_factory, expected_count=1, closed_at=CLOSED_AT
    )

    assert _decision_snapshot(session_factory, raw_message_id) == before


def test_retry_guard_still_blocks_a_closed_out_message(tmp_path):
    """The second lock on the freeze is kept, not released. See
    ``authoritative_recognition.RETRY_BLOCKING_ATTEMPT_STATUSES``."""

    from telegram_kol_research.authoritative_recognition import (
        RETRY_BLOCKING_ATTEMPT_STATUSES,
    )

    assert CLOSEOUT_STATUSES <= RETRY_BLOCKING_ATTEMPT_STATUSES


# --- downstream consumers --------------------------------------------------


def test_closed_out_rows_are_no_longer_scanned(tmp_path):
    session_factory = _prepared(tmp_path)
    _uncertain_message(session_factory, message_id=16)

    before = scan_recognition_execution_cycle(
        session_factory, runtime_role="worker", now=NOW
    )
    assert [finding.action for finding in before] == ["observe_uncertain"]

    apply_uncertain_attempt_closeout(
        session_factory, expected_count=1, closed_at=CLOSED_AT
    )

    # Two cycles: the cursor wraps to zero when a family runs dry, so the
    # second pass is the one that would re-report the row if it were still
    # eligible. That wrap is why one frozen row produced 27000 lines a day.
    first = scan_recognition_execution_cycle(
        session_factory, runtime_role="worker", now=NOW
    )
    second = scan_recognition_execution_cycle(
        session_factory, runtime_role="worker", now=NOW
    )
    assert [f for f in first if f.family == "active_authoritative_attempt"] == []
    assert [f for f in second if f.family == "active_authoritative_attempt"] == []


def test_backlog_expiry_no_longer_counts_a_closed_out_attempt_as_active(tmp_path):
    """A deliberate behaviour change, named in design section 3.

    ``message_processing_backlog_expiry`` treats
    ``('executing','uncertain','outcome_recorded')`` attempts as "still active"
    and refuses while one exists. A closed-out row is no longer in that set, so
    it stops blocking -- which is the point: a row with no exchange exposure
    left should not hold a queue. The *decision-row* guard in the same function
    is untouched and still refuses, so this change alone cannot let a frozen
    message's backlog expire.
    """

    from telegram_kol_research.message_processing_backlog_expiry import (
        AuthoritativeExecutionAttempt as _Attempt,
    )

    session_factory = _prepared(tmp_path)
    _, attempt_id = _uncertain_message(session_factory, message_id=17)
    active_statuses = ("executing", "uncertain", "outcome_recorded")

    def active_count():
        with session_factory() as session:
            return (
                session.query(_Attempt)
                .filter(_Attempt.status.in_(active_statuses))
                .count()
            )

    assert active_count() == 1
    apply_uncertain_attempt_closeout(
        session_factory, expected_count=1, closed_at=CLOSED_AT
    )
    assert active_count() == 0
    with session_factory() as session:
        assert (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.comparison_status == "execution_uncertain")
            .count()
            == 1
        )
        assert session.get(_Attempt, attempt_id).status == CLOSED_NO_WRITE


# --- schema -----------------------------------------------------------------


def test_declared_statuses_match_the_model_check_constraint():
    constraint = next(
        item
        for item in Base.metadata.tables[
            "authoritative_execution_attempts"
        ].constraints
        if getattr(item, "name", None)
        == "ck_authoritative_execution_attempts_status"
    )
    sqltext = str(constraint.sqltext)
    for status in AUTHORITATIVE_EXECUTION_ATTEMPT_STATUSES:
        assert f"'{status}'" in sqltext
    assert CLOSEOUT_STATUSES <= set(AUTHORITATIVE_EXECUTION_ATTEMPT_STATUSES)


def test_legacy_narrow_check_is_widened_at_startup(tmp_path):
    """The one database that matters has the pre-closeout CHECK.

    Without the ``init_db`` rebuild, widening the model's constraint would make
    ``validate_recognition_execution_schema`` report
    ``check_signature:authoritative_execution_attempts`` on production and fail
    every authoritative execution call closed.
    """

    database_path = tmp_path / "legacy.db"
    session_factory = _prepared(tmp_path, name="legacy.db")
    raw_message_id, attempt_id = _uncertain_message(session_factory, message_id=18)

    # Rewrite the table with the narrow, pre-closeout CHECK.
    legacy = create_existing_session_factory(database_path)
    with legacy() as session:
        connection = session.connection()
        definition = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='authoritative_execution_attempts'"
        ).scalar_one()
        narrow = definition.replace(
            "'uncertain','closed_no_write','closed_settled_binding'", "'uncertain'"
        ).replace("authoritative_execution_attempts", "legacy_rebuild", 1)
        assert "closed_no_write" not in narrow
        connection.exec_driver_sql(narrow)
        connection.exec_driver_sql(
            "INSERT INTO legacy_rebuild SELECT * FROM "
            "authoritative_execution_attempts"
        )
        connection.exec_driver_sql("DROP TABLE authoritative_execution_attempts")
        connection.exec_driver_sql(
            "ALTER TABLE legacy_rebuild RENAME TO authoritative_execution_attempts"
        )
        session.commit()

    stale = create_existing_session_factory(database_path)
    assert not validate_recognition_execution_schema(stale).valid

    # What every production process does at startup.
    reopened = create_session_factory(database_path)
    validation = validate_recognition_execution_schema(reopened)
    assert validation.valid, validation.errors
    with reopened() as session:
        attempt = session.get(AuthoritativeExecutionAttempt, attempt_id)
        assert attempt.status == "uncertain"
        assert int(attempt.raw_message_id) == raw_message_id
        indexes = {
            str(row[0])
            for row in session.connection()
            .exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='authoritative_execution_attempts'"
            )
            .all()
        }
    assert "ix_authoritative_execution_attempts_status_lease" in indexes
    assert "ix_authoritative_execution_attempts_raw_message_id" in indexes

    plan = build_uncertain_attempt_closeout_plan(reopened)
    assert len(plan.closeable_rows) == 1
    result = apply_uncertain_attempt_closeout(
        reopened, expected_count=1, closed_at=CLOSED_AT
    )
    assert result.changed_count == 1


def test_widening_is_idempotent(tmp_path):
    database_path = tmp_path / "idempotent.db"
    _prepared(tmp_path, name="idempotent.db")
    for _ in range(3):
        factory = create_session_factory(database_path)
        assert validate_recognition_execution_schema(factory).valid


# --- CLI --------------------------------------------------------------------


def test_cli_dry_run_then_apply(tmp_path):
    from typer.testing import CliRunner

    from telegram_kol_research.cli import app

    database_path = tmp_path / "cli.db"
    session_factory = _prepared(tmp_path, name="cli.db")
    _uncertain_message(session_factory, message_id=19)
    _uncertain_message(
        session_factory,
        message_id=20,
        bindings=((344, "open"),),
        events=(
            (
                "open_market_position",
                {"order_id": "o-7", "execution_binding_id": 344},
            ),
        ),
    )
    runner = CliRunner()

    dry = runner.invoke(
        app,
        [
            "close-out-uncertain-attempts",
            "--database-path",
            str(database_path),
        ],
    )
    assert dry.exit_code == 0, dry.output
    payload = json.loads(dry.stdout.strip().splitlines()[-1])
    assert payload["mode"] == "dry_run"
    assert payload["scanned_count"] == 2
    assert payload["closeable_count"] == 1
    assert payload["refused_count"] == 1
    assert payload["changed_count"] == 0
    assert payload["exchange_write_count"] == 0
    refused = [row for row in payload["rows"] if row["closeout_status"] is None]
    assert refused[0]["refusal_reason"] == REFUSAL_BINDING_NOT_TERMINAL

    mismatched = runner.invoke(
        app,
        [
            "close-out-uncertain-attempts",
            "--database-path",
            str(database_path),
            "--apply",
            "--expected-count",
            "2",
        ],
    )
    assert mismatched.exit_code == 2
    with session_factory() as session:
        assert (
            session.query(AuthoritativeExecutionAttempt)
            .filter(AuthoritativeExecutionAttempt.status == "uncertain")
            .count()
            == 2
        )

    missing_guard = runner.invoke(
        app,
        [
            "close-out-uncertain-attempts",
            "--database-path",
            str(database_path),
            "--apply",
        ],
    )
    assert missing_guard.exit_code == 2

    applied = runner.invoke(
        app,
        [
            "close-out-uncertain-attempts",
            "--database-path",
            str(database_path),
            "--apply",
            "--expected-count",
            "1",
        ],
    )
    assert applied.exit_code == 0, applied.output
    applied_payload = json.loads(applied.stdout.strip().splitlines()[-1])
    assert applied_payload["mode"] == "apply"
    assert applied_payload["changed_count"] == 1
    assert applied_payload["exchange_write_count"] == 0


def test_the_module_has_no_way_to_reach_the_exchange():
    """``exchange_write_count: 0`` is a claim about the code, not just output.

    Static, because the honest version of "this command never writes to the
    venue" is that it holds no client and no path to one.
    """

    from pathlib import Path

    from telegram_kol_research import uncertain_attempt_closeout

    source = Path(uncertain_attempt_closeout.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "import deepcoin",
        "deepcoin_client",
        "DeepcoinRestClient",
        "build_deepcoin_client_from_env",
        "httpx",
        "requests",
    ):
        assert forbidden not in source, forbidden


def test_cli_refuses_a_missing_database(tmp_path):
    from typer.testing import CliRunner

    from telegram_kol_research.cli import app

    result = CliRunner().invoke(
        app,
        [
            "close-out-uncertain-attempts",
            "--database-path",
            str(tmp_path / "absent.db"),
        ],
    )
    assert result.exit_code == 2


def test_plan_requires_the_execution_fence_schema(tmp_path):
    session_factory = create_session_factory(tmp_path / "bare.db")
    with session_factory() as session:
        session.execute(text("SELECT 1"))
    with pytest.raises(RuntimeError) as excinfo:
        build_uncertain_attempt_closeout_plan(session_factory)
    assert "recognition_execution_schema_invalid" in str(excinfo.value)
