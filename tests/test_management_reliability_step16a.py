"""A-16a: a management instruction that resolves to nothing must not be silent.

On 2026-09-11 a KOL sent three messages managing two live BTC positions. The
resolver read each of them correctly -- it recorded "part take-profit 50%, move
the stop to cost" as the instruction -- and refused each one because the entry
price it named matched two entered threads instead of one. The refusal is
defensible. The silence was not: no instruction item, no batch, no
notification, and the user closed both positions by hand.

These tests assert the incident row, not that the adapter was called. A-10b was
built with the opposite habit and its alarm produced no row at all for two days.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime


NOW = datetime(2026, 9, 11, 5, 3, 12, tzinfo=UTC)
RAW_MESSAGE_ID = 16078
INSTRUCTION = "先拿1000点利润50%，剩余做成本保护继续持有"
REASON = (
    "消息明确要求对BTC仓位执行部分止盈并移动止损至成本保护，"
    "但未通过引用或明确命名关联到候选策略线程520或521，无法唯一确定目标。"
)


def _permissive_incident_config():
    from telegram_kol_research.config import (
        ALWAYS_NOTIFIED_INCIDENT_TYPES,
        RuntimeIncidentConfig,
    )

    return RuntimeIncidentConfig(
        capture_types=frozenset(ALWAYS_NOTIFIED_INCIDENT_TYPES)
    )


def _capture(session_factory, **overrides):
    from telegram_kol_research.runtime_incident_adapters import (
        capture_management_recognition_unresolved,
    )

    payload = {
        "config": _permissive_incident_config(),
        "raw_message_id": RAW_MESSAGE_ID,
        "chat_id": -1002337721508,
        "decision": "unresolved",
        "conflict_types": ("target_ambiguous", "multiple_candidates"),
        "candidate_thread_ids": (520, 521),
        "instruction_text": INSTRUCTION,
        "resolution_reason": REASON,
        "occurred_at": NOW,
    }
    payload.update(overrides)
    return capture_management_recognition_unresolved(session_factory, **payload)


def _incident_rows(session_factory):
    from telegram_kol_research.models import RuntimeIncident

    with session_factory() as session:
        return (
            session.query(RuntimeIncident)
            .filter(
                RuntimeIncident.incident_type
                == "management_recognition_unresolved"
            )
            .all()
        )


def test_an_unresolved_management_instruction_produces_an_incident_row(tmp_path):
    """The row exists, and it names the threads that were in the running."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _capture(session_factory)

    rows = _incident_rows(session_factory)
    assert len(rows) == 1, "the summary was refused and nobody was told"
    summary = json.loads(rows[0].redacted_summary)
    assert summary["candidate_thread_ids"] == "520_521"
    assert summary["conflict_types"] == "target_ambiguous_multiple_candidates"
    assert summary["raw_message_id"] == RAW_MESSAGE_ID
    assert summary["impact"] == "management_instruction_not_executed"
    assert summary["reason_code"] == "unresolved"


def test_the_alert_carries_the_instruction_and_the_model_s_own_reason(tmp_path):
    """Without these an operator cannot tell a real miss from noise."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _capture(session_factory)

    summary = json.loads(_incident_rows(session_factory)[0].redacted_summary)
    assert "instruction_excerpt" in summary
    assert "50" in summary["instruction_excerpt"]
    assert "error_summary" in summary
    assert "520" in summary["error_summary"]


def test_this_incident_type_is_always_notified(tmp_path):
    """A row nobody is told about is the silence this step exists to end."""

    from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES

    assert "management_recognition_unresolved" in ALWAYS_NOTIFIED_INCIDENT_TYPES


def test_every_summary_field_is_inside_the_closed_vocabulary(tmp_path):
    """The A-10b failure, asserted directly rather than hoped for.

    ``runtime_incidents`` refuses a summary carrying an unknown key and the
    refusal is logged, not raised, so the only way to notice is to look for the
    row. The detailed summary must survive on its own -- landing on the minimal
    fallback would silently drop the instruction text.
    """

    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.runtime_incidents import _SUMMARY_FIELDS

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _capture(session_factory)

    summary = json.loads(_incident_rows(session_factory)[0].redacted_summary)
    assert set(summary) <= set(_SUMMARY_FIELDS)
    assert "instruction_excerpt" in summary, "fell back to the minimal summary"


def _decision(**overrides):
    from telegram_kol_research.context_resolution import ContextResolutionDecision

    payload = {
        "decision": "unresolved",
        "target_thread_ids": (),
        "management_action": None,
        "confidence": 0.6,
        "supporting_message_ids": (),
        "opposing_message_ids": (),
        "conflict_types": ("target_ambiguous",),
        "risk_reducing_fanout_allowed": False,
        "reanalysis_triggers": (),
        "reason": REASON,
    }
    payload.update(overrides)
    return ContextResolutionDecision(**payload)


class _Candidate:
    def __init__(self, thread_id):
        self.thread_id = thread_id


def _payload(*, instruction=INSTRUCTION):
    fields = {"symbol": {"value": "BTC", "confidence": 1.0, "source": "text"}}
    if instruction is not None:
        fields["management_instruction"] = {
            "value": instruction,
            "confidence": 1.0,
            "source": "text",
        }
    return {"evidence": {"text": {"fields": fields}}}


def _run_gate(session_factory, *, decision, payload, monkeypatch=None):
    from telegram_kol_research import config as config_module
    from telegram_kol_research.authoritative_recognition import (
        _alert_unresolved_management_instruction,
    )

    # Incident capture is dormant unless an operator selector is set, and the
    # test process has none. Production does set one -- read from the worker's
    # own environ on 2026-09-11 -- so the always-notified baseline folds this
    # type in there; see the config test below, which pins that mechanism
    # rather than trusting it.
    if monkeypatch is not None:
        monkeypatch.setattr(
            config_module,
            "load_runtime_incident_config",
            lambda *a, **k: _permissive_incident_config(),
        )
    _alert_unresolved_management_instruction(
        session_factory,
        raw_message_id=RAW_MESSAGE_ID,
        payload=payload,
        decision=decision,
        candidates=(_Candidate(520), _Candidate(521)),
    )


def test_the_gate_alerts_when_an_unresolved_message_carried_an_instruction(tmp_path, monkeypatch):
    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _run_gate(session_factory, monkeypatch=monkeypatch, decision=_decision(), payload=_payload())

    assert len(_incident_rows(session_factory)) == 1


def test_the_gate_stays_quiet_when_no_management_instruction_was_read(tmp_path, monkeypatch):
    """The measured narrowing.

    Alerting on every unresolved context resolution would be 100-200 a day
    (3065 rows since 2026-07-27); alerting only when the resolver itself
    recorded a management instruction is 75 in 46 days. The narrowing field is
    the resolver's own evidence, not a keyword list, so this test pins the
    difference between "we refused to act" and "we refused to act on an
    instruction".
    """

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _run_gate(session_factory, monkeypatch=monkeypatch, decision=_decision(), payload=_payload(instruction=None))

    assert _incident_rows(session_factory) == []


def test_the_gate_stays_quiet_when_the_resolver_did_produce_an_action(tmp_path, monkeypatch):
    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _run_gate(
        session_factory,
        monkeypatch=monkeypatch,
        decision=_decision(decision="manage_thread", confidence=0.9,
                           target_thread_ids=(520,)),
        payload=_payload(),
    )

    assert _incident_rows(session_factory) == []


def test_a_low_confidence_downgrade_alerts_too(tmp_path, monkeypatch):
    """``_resolved_mimo_result`` turns these into 非策略 as well.

    An instruction dropped for low confidence is just as unexecuted as one
    dropped for an ambiguous target, and it was just as silent.
    """

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _run_gate(
        session_factory,
        monkeypatch=monkeypatch,
        decision=_decision(decision="manage_thread", confidence=0.5,
                           target_thread_ids=(520,)),
        payload=_payload(),
    )

    assert len(_incident_rows(session_factory)) == 1


def test_the_gate_never_raises_into_the_recognition_path(tmp_path, monkeypatch):
    """Losing the alert must not also lose the message."""

    from telegram_kol_research.db import create_session_factory

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _run_gate(session_factory, monkeypatch=monkeypatch, decision=_decision(), payload={"evidence": "not-a-mapping"})
    _run_gate(session_factory, monkeypatch=monkeypatch, decision=_decision(), payload=None)

    assert _incident_rows(session_factory) == []


def test_a_production_shaped_selector_captures_and_notifies_this_type():
    """The mechanism that makes this alert reach anyone, pinned.

    Capture is dormant by default and an operator maintains both selectors by
    hand. Neither list names this type, and neither should have to: a non-empty
    selector folds in the always-notified baseline precisely so that forgetting
    one cannot turn a critical alert into silence. Both selectors were read
    from the production worker's own environ on 2026-09-11 and both are
    non-empty, which is what makes this test the relevant one.
    """

    from telegram_kol_research.config import load_runtime_incident_config

    config = load_runtime_incident_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES": "management_partial_failed",
            "TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES": "management_partial_failed",
        },
        env_file_paths=[],
    )

    assert config.captures("management_recognition_unresolved")
    assert "management_recognition_unresolved" in (
        config.telegram_notification_types or frozenset()
    )
