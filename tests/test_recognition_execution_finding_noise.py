"""The scanner's reporting throttle and per-action severity.

Design section 5 of ``docs/plans/2026-09-26-uncertain-attempt-closeout-design.md``:
closing the 37 frozen rows removes today's noise, but the next ``uncertain`` row
would be re-reported every two minutes exactly as they were. These tests pin the
throttle, the levels, and the fact that the incident ledger still sees every
finding.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research import web_app
from telegram_kol_research.recognition_execution_scanner import (
    FINDING_REPORT_MIN_INTERVAL,
    OBSERVE_ONLY_FINDING_ACTIONS,
    RecognitionExecutionFinding,
    _finding,
    finding_log_level,
    reset_finding_report_throttle,
    should_report_finding,
)
from telegram_kol_research.source_deletion_exit_timeout import (
    STUCK_EXIT_CAPTURE_MIN_INTERVAL,
)


NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_throttle():
    reset_finding_report_throttle()
    yield
    reset_finding_report_throttle()


def _uncertain_finding(row_id=1, action="observe_uncertain", phase="uncertain"):
    return _finding("active_authoritative_attempt", row_id, 9001, phase, action)


# --- throttle ---------------------------------------------------------------


def test_same_row_is_reported_once_inside_the_interval():
    finding = _uncertain_finding()

    assert should_report_finding(finding, moment=NOW) is True
    assert should_report_finding(finding, moment=NOW + timedelta(minutes=2)) is False
    assert (
        should_report_finding(
            finding, moment=NOW + FINDING_REPORT_MIN_INTERVAL - timedelta(seconds=1)
        )
        is False
    )


def test_the_interval_expiring_lets_it_speak_again():
    finding = _uncertain_finding()

    assert should_report_finding(finding, moment=NOW) is True
    assert (
        should_report_finding(finding, moment=NOW + FINDING_REPORT_MIN_INTERVAL)
        is True
    )


def test_an_action_change_is_reported_immediately():
    assert should_report_finding(_uncertain_finding(), moment=NOW) is True
    changed = _uncertain_finding(action="finalize_raised")

    assert should_report_finding(changed, moment=NOW + timedelta(seconds=30)) is True


def test_a_phase_change_is_reported_immediately():
    assert should_report_finding(_uncertain_finding(), moment=NOW) is True
    changed = _uncertain_finding(phase="outcome_recorded")

    assert should_report_finding(changed, moment=NOW + timedelta(seconds=30)) is True


def test_a_different_row_is_never_silenced_by_its_neighbour():
    assert should_report_finding(_uncertain_finding(row_id=1), moment=NOW) is True
    assert should_report_finding(_uncertain_finding(row_id=2), moment=NOW) is True


def test_the_same_row_id_in_another_family_is_its_own_key():
    first = _finding("active_authoritative_attempt", 7, 1, "uncertain", "observe_uncertain")
    second = _finding("active_wakeup_execution", 7, 1, "uncertain", "observe_uncertain")

    assert should_report_finding(first, moment=NOW) is True
    assert should_report_finding(second, moment=NOW) is True


def test_a_naive_moment_is_treated_as_utc():
    finding = _uncertain_finding()

    assert should_report_finding(finding, moment=NOW) is True
    assert (
        should_report_finding(finding, moment=datetime(2026, 9, 26, 12, 2))
        is False
    )


def test_the_interval_matches_the_deletion_exit_throttle():
    assert FINDING_REPORT_MIN_INTERVAL == STUCK_EXIT_CAPTURE_MIN_INTERVAL


# --- severity ---------------------------------------------------------------


@pytest.mark.parametrize("action", sorted(OBSERVE_ONLY_FINDING_ACTIONS))
def test_observation_actions_are_warnings(action):
    assert finding_log_level(_uncertain_finding(action=action)) == logging.WARNING


@pytest.mark.parametrize(
    "action",
    [
        "family_scan_raised",
        "inspection_raised",
        "finalize_raised",
        "finalize_cas_failed",
        "terminalize_cas_failed",
        "expired_owner_still_alive",
        "expired_owner_liveness_unknown",
        "owner_not_alive_lease_active",
        "finalized_locally",
        "failed_safe",
        "marked_uncertain",
        "an_action_invented_next_year",
    ],
)
def test_everything_else_stays_an_error(action):
    assert finding_log_level(_uncertain_finding(action=action)) == logging.ERROR


def test_observe_only_allowlist_does_not_swallow_the_raised_actions():
    assert not any(
        action.endswith("_raised") for action in OBSERVE_ONLY_FINDING_ACTIONS
    )


# --- the call site ----------------------------------------------------------


def _app(session_factory=object()):
    return SimpleNamespace(
        state=SimpleNamespace(
            session_factory=session_factory,
            runtime_role="worker",
            runtime_incident_config_loader=lambda: None,
        )
    )


class _Recorder(logging.Handler):
    """Collect records straight off ``web_app.logger``.

    Deliberately not ``caplog``: ``app_logging.configure_application_logging``
    sets ``logging.getLogger("telegram_kol_research").propagate = False`` for the
    whole process, so once any earlier test in the run has called it, records
    from this logger never reach pytest's root handler and ``caplog.records``
    comes back empty while the line is plainly on stderr. That is exactly how
    these two tests passed alone and failed in the full suite.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _finding_lines(recorder: _Recorder) -> list[logging.LogRecord]:
    return [
        record
        for record in recorder.records
        if record.getMessage().startswith("recognition execution finding")
    ]


def _drive_cycle(monkeypatch, findings, *, moments, recorder=None):
    captured: list[RecognitionExecutionFinding] = []
    monkeypatch.setattr(
        web_app,
        "scan_recognition_execution_cycle",
        lambda *args, **kwargs: tuple(findings),
    )
    monkeypatch.setattr(
        web_app,
        "capture_runtime_incident_best_effort",
        lambda *args, **kwargs: captured.append(kwargs.get("row_id")),
    )
    app = _app()
    previous_level = web_app.logger.level
    if recorder is not None:
        web_app.logger.addHandler(recorder)
        web_app.logger.setLevel(logging.DEBUG)
    try:
        for moment in moments:
            web_app._run_recognition_execution_scanner_cycle(app, observed_at=moment)
    finally:
        if recorder is not None:
            web_app.logger.removeHandler(recorder)
            web_app.logger.setLevel(previous_level)
    return captured


def test_the_cycle_logs_a_frozen_row_once_but_captures_it_every_pass(monkeypatch):
    finding = _uncertain_finding(row_id=42)
    moments = [NOW + timedelta(minutes=2 * index) for index in range(5)]
    recorder = _Recorder()

    captured = _drive_cycle(
        monkeypatch, [finding], moments=moments, recorder=recorder
    )

    lines = _finding_lines(recorder)
    assert len(lines) == 1
    assert lines[0].levelno == logging.WARNING
    # The ledger is not throttled: five passes, five captures, which is what
    # ``runtime_incidents`` coalesces into one row with repeat_count 5.
    assert captured == [42, 42, 42, 42, 42]


def test_the_cycle_keeps_a_real_exception_at_error(monkeypatch):
    finding = _finding("active_authoritative_attempt", 43, 9002, "scan", "finalize_raised")
    recorder = _Recorder()

    _drive_cycle(monkeypatch, [finding], moments=[NOW], recorder=recorder)

    assert [record.levelno for record in _finding_lines(recorder)] == [logging.ERROR]
