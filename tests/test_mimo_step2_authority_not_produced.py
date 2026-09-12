"""step-18: "no authoritative decision was produced" can never be un-alerted.

On 2026-09-12 MiMo answered 402 for fourteen hours. The alert type existed and
was ALWAYS_NOTIFIED; it never fired, because its first gate was
``reason not in ALERTED_REASONS`` and the two reasons recorded that day --
``mimo_authoritative_failed`` and ``authoritative_gap_recovery_expired`` --
were not in the set. A narrowing that reads as reasonable silently excluded
the one case that mattered most.

Two kinds of guard, because each misses what the other catches (ARCHITECTURE
section 6, "我这一项在集合里" vs "每一项都接上了线"):

* named: these two reasons are alerted;
* traversal: every writer of a terminal ``authoritative_failed`` decision is
  found in the source, must be registered with its reason, and every
  registered reason must reach an incident row -- with sentinels, because a
  traversal over nothing passes.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_kol_research import message_processing_worker as worker_module
from telegram_kol_research import web_app as web_app_module
from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
from telegram_kol_research.authoritative_recognition import (
    AuthoritativeAssessment,
    process_authoritative_message,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_processing_worker import (
    run_message_processing_worker_loop,
    run_message_processing_worker_tick,
)
from telegram_kol_research.models import RawMessage, RuntimeIncident
from telegram_kol_research.recognition_experiments import MimoAuthoritativeResult
from telegram_kol_research.recognition_failure_attribution import (
    ALERTED_REASONS,
    AUTHORITY_NOT_PRODUCED_REASONS,
    AUTHORITY_NOT_PRODUCED_WRITERS,
    GAP_RECOVERY_EXPIRED,
    MIMO_AUTHORITATIVE_FAILED,
)
from telegram_kol_research.telegram_live_listener import _enqueue_processing_jobs


SRC = Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"
_WRITER_FUNCTIONS = {
    "save_terminal_authoritative_decision",
    "_save_terminal_authoritative_decision_in_session",
}


# --------------------------------------------------------------------------
# Named
# --------------------------------------------------------------------------


def test_the_two_step18_reasons_are_alerted():
    assert MIMO_AUTHORITATIVE_FAILED == "mimo_authoritative_failed"
    assert GAP_RECOVERY_EXPIRED == "authoritative_gap_recovery_expired"
    assert MIMO_AUTHORITATIVE_FAILED in ALERTED_REASONS
    assert GAP_RECOVERY_EXPIRED in ALERTED_REASONS


# --------------------------------------------------------------------------
# Traversal
# --------------------------------------------------------------------------


class _WriterScan(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.stack: list[str] = []
        self.call_sites: list[str] = []
        self.literal_sites: list[str] = []

    def _enclosing(self) -> str:
        return f"{self.module}.{self.stack[-1] if self.stack else '<module>'}"

    def _visit_function(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name in _WRITER_FUNCTIONS and not (
            self.stack and self.stack[-1] in _WRITER_FUNCTIONS
        ):
            self.call_sites.append(self._enclosing())
        for keyword in node.keywords:
            if (
                keyword.arg == "agreement_status"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "authoritative_failed"
            ):
                self.literal_sites.append(self._enclosing())
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        names = {
            element.id
            for target in node.targets
            for element in ast.walk(target)
            if isinstance(element, ast.Name)
        }
        if "agreement_status" in names and any(
            isinstance(element, ast.Constant)
            and element.value == "authoritative_failed"
            for element in ast.walk(node.value)
        ):
            self.literal_sites.append(self._enclosing())
        self.generic_visit(node)


def _scan_source():
    call_sites: list[str] = []
    literal_sites: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        scan = _WriterScan(path.stem)
        scan.visit(ast.parse(path.read_text(encoding="utf-8")))
        call_sites.extend(scan.call_sites)
        literal_sites.extend(scan.literal_sites)
    return call_sites, literal_sites


def test_every_writer_of_a_missing_decision_is_registered_and_alerted():
    call_sites, literal_sites = _scan_source()

    # Sentinels: a scan that parsed nothing would otherwise pass everything.
    assert len(call_sites) >= 2, call_sites
    assert len(literal_sites) >= 2, literal_sites
    assert len(AUTHORITY_NOT_PRODUCED_WRITERS) >= 2
    assert len(AUTHORITY_NOT_PRODUCED_REASONS) >= 2

    # A new writer that nobody registered turns this red: its reason would not
    # be known to be alerted.
    assert set(call_sites) == set(AUTHORITY_NOT_PRODUCED_WRITERS)
    # A writer that sets the status without going through the save helpers is
    # still caught.
    assert set(literal_sites) <= set(AUTHORITY_NOT_PRODUCED_WRITERS)

    assert set(AUTHORITY_NOT_PRODUCED_WRITERS.values()) == AUTHORITY_NOT_PRODUCED_REASONS
    assert AUTHORITY_NOT_PRODUCED_REASONS <= ALERTED_REASONS

    # The registry names a reason; the writer's module must actually record it.
    for writer, reason in AUTHORITY_NOT_PRODUCED_WRITERS.items():
        module = writer.split(".", 1)[0]
        assert f'"{reason}"' in (SRC / f"{module}.py").read_text(encoding="utf-8"), (
            writer
        )


# --------------------------------------------------------------------------
# Each registered reason, through its real caller, reaches an incident row
# --------------------------------------------------------------------------


def _worker_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_ROLE", "worker")
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES", "management_partial_failed"
    )
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES", "management_partial_failed"
    )
    monkeypatch.setenv("TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_ENABLED", "true")


def _recognition_incidents(session_factory, reason):
    with session_factory() as session:
        return [
            row
            for row in session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == "authoritative_recognition_failed")
            .all()
            if f'"reason_code":"{reason}"' in (row.redacted_summary or "")
        ]


def _add_raw(session_factory, *, chat_id, posted_at=None):
    with session_factory() as session:
        raw = RawMessage(chat_id=chat_id, message_id=4, text="BTC short", posted_at=posted_at)
        session.add(raw)
        session.commit()
        return raw.id


@pytest.mark.parametrize("mode,expected", [("auto_trade", 1), ("notify_only", 0)])
def test_a_failed_authoritative_call_alerts_in_a_trading_group_only(
    tmp_path, monkeypatch, mode, expected
):
    """One message, one difference (the group's mode), two outcomes."""

    _worker_env(monkeypatch)
    session_factory = create_session_factory(tmp_path / "failed.db")
    raw_id = _add_raw(session_factory, chat_id=1)
    assessment = AuthoritativeAssessment(
        raw_message_id=raw_id,
        mimo=MimoAuthoritativeResult(
            raw_message_id=raw_id,
            payload={},
            input_kind="text",
            model="mimo-v2.5",
            status="识别失败",
            error_message="MiMo failed after 2 attempts: 402 Payment Required",
        ),
        deepseek_payload=None,
        agreement_status="authoritative_failed",
        differences=[],
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.assess_message_authoritatively",
        lambda *args, **kwargs: assessment,
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.apply_authoritative_assessment",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        "telegram_kol_research.authoritative_recognition.update_recognition_execution_outcome",
        lambda *args, **kwargs: None,
    )

    result = process_authoritative_message(
        session_factory,
        raw_message_id=raw_id,
        ai_recognition_config=AiRecognitionConfig(),
        media_root=tmp_path,
        auto_trade_executor=lambda raw_message_id: pytest.fail("must not execute"),
        group_trading_mode_provider=lambda chat: mode,
    )

    assert result.automation["reason"] == MIMO_AUTHORITATIVE_FAILED
    assert len(_recognition_incidents(session_factory, MIMO_AUTHORITATIVE_FAILED)) == (
        expected
    )


def _expired_job(session_factory, *, chat_id, now):
    raw_id = _add_raw(
        session_factory, chat_id=chat_id, posted_at=(now - timedelta(minutes=20)).replace(tzinfo=None)
    )
    _enqueue_processing_jobs(
        session_factory, raw_message_ids=[raw_id], last_reason="queue_enqueued"
    )
    return raw_id


@pytest.mark.parametrize("mode,expected", [("auto_trade", 1), ("notify_only", 0)])
def test_an_expired_message_alerts_in_a_trading_group_only(
    tmp_path, monkeypatch, mode, expected
):
    _worker_env(monkeypatch)
    session_factory = create_session_factory(tmp_path / "expired.db")
    now = datetime.now(UTC)
    _expired_job(session_factory, chat_id=1, now=now)

    result = asyncio.run(
        run_message_processing_worker_tick(
            session_factory,
            now=now,
            job_processor=lambda *args, **kwargs: pytest.fail("must not execute"),
            loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
            group_trading_mode_provider=lambda chat: mode,
        )
    )

    assert result.expired == 1
    assert len(_recognition_incidents(session_factory, GAP_RECOVERY_EXPIRED)) == expected


# --------------------------------------------------------------------------
# Wiring: the provider really reaches the expiry path in production
# --------------------------------------------------------------------------


def test_the_expiry_provider_is_accepted_forwarded_and_passed_by_web_app():
    assert "group_trading_mode_provider" in inspect.signature(
        run_message_processing_worker_tick
    ).parameters
    loop_parameters = inspect.signature(run_message_processing_worker_loop).parameters
    assert any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in loop_parameters.values()
    )
    assert "**tick_kwargs" in inspect.getsource(run_message_processing_worker_loop)

    source = inspect.getsource(web_app_module)
    start = source.index("app.state.message_processing_worker_runner(")
    end = source.index("activity=app.state.message_processing_activity", start)
    call = source[start : source.index(")\n        )", end)]
    assert "group_trading_mode_provider=" in call
    # The step-1 health tick must not be reachable only through a keyword that
    # a later edit could drop: the worker launch is the only place it is set.
    assert worker_module._alert_expired_without_decision is not None


def test_the_loop_forwards_the_provider_to_an_expired_job(tmp_path, monkeypatch):
    _worker_env(monkeypatch)
    session_factory = create_session_factory(tmp_path / "loop.db")
    now = datetime.now(UTC)
    _expired_job(session_factory, chat_id=1, now=now)

    async def run_briefly():
        task = asyncio.create_task(
            run_message_processing_worker_loop(
                session_factory,
                interval_seconds=0.01,
                job_processor=lambda *args, **kwargs: pytest.fail("must not execute"),
                loop_lag_snapshot_provider=lambda: {"last_stall_at": None},
                group_trading_mode_provider=lambda chat: "auto_trade",
            )
        )
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_briefly())

    assert len(_recognition_incidents(session_factory, GAP_RECOVERY_EXPIRED)) == 1
