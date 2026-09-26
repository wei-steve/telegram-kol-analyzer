"""The stuck deletion exit: say why, stop shouting, and close by itself.

Production, 2026-09-15 to 09-26 (docs/2026-09-26-silent-stall-case-note.md):
陈哥's group deleted two BTC-long messages, both exits landed in
``recovery_required`` with no ``execution_binding_id``, and the deferral
barrier sealed that group's BTC-long lane for eleven days. Eleven later
messages -- four of them entry strategies -- were held until they expired. The
system did alert: 356933 times, notified once, and the one field carrying the
answer ("why was the lane not released") was refused by the incident summary
vocabulary on every single pass.

Three defects, three sections here:

* 甲 ``release_reason`` has to survive the summary contract.
* 乙 the same unchanged exit must be captured once per interval, not every
  five seconds -- but a changed one must still be captured at once.
* 丙 an exit with no execution credentials, over a lane where every live
  position and resting order belongs to somebody else, must be allowed to
  close. And nothing that already expired may be brought back.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory


NOW = datetime(2026, 9, 26, 9, 46, tzinfo=UTC)


# --------------------------------------------------------------------------
# 甲: the vocabulary that swallowed the only useful field
# --------------------------------------------------------------------------


def _permissive_incident_config():
    from telegram_kol_research.config import (
        ALWAYS_NOTIFIED_INCIDENT_TYPES,
        RuntimeIncidentConfig,
    )

    return RuntimeIncidentConfig(
        capture_types=frozenset(ALWAYS_NOTIFIED_INCIDENT_TYPES)
    )


def _capture_stuck_incident(session_factory, **overrides):
    from telegram_kol_research.runtime_incident_adapters import (
        capture_source_deletion_exit_stuck,
    )

    payload = {
        "config": _permissive_incident_config(),
        "deletion_exit_id": 310,
        "state": "recovery_required",
        "reason_code": "exact_lifecycle_missing",
        "timeout_minutes": 120,
        "lane_released": False,
        "release_reason": "exit_has_no_known_position",
        "occurred_at": NOW,
    }
    payload.update(overrides)
    return capture_source_deletion_exit_stuck(session_factory, **payload)


def _incident_rows(session_factory):
    from telegram_kol_research.models import RuntimeIncident

    with session_factory() as session:
        return (
            session.query(RuntimeIncident)
            .filter(
                RuntimeIncident.incident_type == "source_deletion_exit_stuck"
            )
            .all()
        )


def test_the_stuck_exit_alert_lands_with_impact_and_release_reason(tmp_path):
    """The detailed summary, not the minimal fallback it used to land on."""

    session_factory = create_session_factory(tmp_path / "incidents.db")

    _capture_stuck_incident(session_factory)

    rows = _incident_rows(session_factory)
    assert len(rows) == 1
    summary = json.loads(rows[0].redacted_summary)
    assert summary["release_reason"] == "exit_has_no_known_position"
    assert summary["impact"] == "lane_still_held_after_timeout"
    assert summary["timeout_minutes"] == 120
    assert summary["operation"] == "deletion_exit_310"
    assert summary["reason_code"] == "exact_lifecycle_missing"


def test_the_notification_a_person_reads_names_the_release_verdict(tmp_path):
    """The row is not the last mile -- the Telegram message is."""

    from telegram_kol_research.system_operator_bot import (
        format_runtime_incident_notification,
    )

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _capture_stuck_incident(session_factory)

    rendered = format_runtime_incident_notification(_incident_rows(session_factory)[0])
    assert "释放判定: exit_has_no_known_position" in rendered


def test_every_stuck_exit_summary_field_is_inside_the_closed_vocabulary(tmp_path):
    """The A-8c / A-10b failure asserted, since this was its third repeat.

    ``runtime_incidents`` refuses a summary carrying an unknown key and logs
    the refusal instead of raising it, so a missing word costs an alarm and
    nothing turns red. Under the suite the same condition raises, so this test
    fails loudly if the word disappears again.
    """

    from telegram_kol_research.runtime_incidents import _SUMMARY_FIELDS

    session_factory = create_session_factory(tmp_path / "incidents.db")
    _capture_stuck_incident(session_factory, lane_released=True,
                            release_reason="position_gone_confirmed")

    summary = json.loads(_incident_rows(session_factory)[0].redacted_summary)
    assert set(summary) <= set(_SUMMARY_FIELDS)
    assert summary["release_reason"] == "position_gone_confirmed"
    assert summary["impact"] == "lane_released_after_timeout"
