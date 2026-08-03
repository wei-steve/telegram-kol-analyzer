from datetime import UTC, datetime
from concurrent.futures import ThreadPoolExecutor

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RuntimeIncidentObservation
from telegram_kol_research.runtime_incident_observations import record_observation
from telegram_kol_research.runtime_incident_scanner import InvariantObservation


NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _observation(outcome="abnormal", evidence_fingerprint="e" * 64):
    return InvariantObservation(
        rule_id="terminal_lifecycle_exchange_exposure_v1",
        rule_version="1",
        object_kind="lifecycle",
        object_id="42",
        severity="critical",
        outcome=outcome,
        evidence_references=("lifecycle:42", "exchange-position:7"),
        evidence_fingerprint=evidence_fingerprint,
        summary={"lifecycle_terminal": True, "exchange_exposure": True},
    )


def test_first_and_repeated_observation_are_deduplicated(tmp_path):
    sf = create_session_factory(tmp_path / "observations.db")
    first = record_observation(sf, observation=_observation(), observed_at=NOW)
    second = record_observation(sf, observation=_observation(), observed_at=NOW)
    assert first.state == "observing"
    assert second.state == "shadow_confirmed"
    assert second.consecutive_count == 2
    with sf() as session:
        assert session.query(RuntimeIncidentObservation).count() == 1


def test_normal_observation_resolves_without_incident(tmp_path):
    sf = create_session_factory(tmp_path / "observations.db")
    record_observation(sf, observation=_observation(), observed_at=NOW)
    row = record_observation(
        sf, observation=_observation(outcome="normal"), observed_at=NOW
    )
    assert row.state == "resolved_without_incident"
    assert row.recovered_at == NOW.replace(tzinfo=None)


def test_material_evidence_change_restarts_confirmation_count(tmp_path):
    sf = create_session_factory(tmp_path / "observations.db")
    record_observation(sf, observation=_observation(), observed_at=NOW)
    row = record_observation(
        sf, observation=_observation(evidence_fingerprint="f" * 64), observed_at=NOW
    )
    assert row.consecutive_count == 1
    assert row.evidence_fingerprint == "f" * 64


def test_concurrent_scanners_keep_one_row_and_count_every_observation(tmp_path):
    sf = create_session_factory(tmp_path / "observations.db")
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(
            lambda _: record_observation(sf, observation=_observation(), observed_at=NOW),
            range(4),
        ))
    assert {row.id for row in rows} == {rows[0].id}
    with sf() as session:
        row = session.query(RuntimeIncidentObservation).one()
        assert row.consecutive_count == 4


def test_stale_observation_cannot_overwrite_newer_state(tmp_path):
    sf = create_session_factory(tmp_path / "observations.db")
    newer = NOW.replace(hour=2)
    older = NOW.replace(hour=1)
    record_observation(sf, observation=_observation(), observed_at=newer)
    row = record_observation(
        sf, observation=_observation(outcome="normal"), observed_at=older
    )
    assert row.state == "observing"
    assert row.last_observed_at == newer.replace(tzinfo=None)
