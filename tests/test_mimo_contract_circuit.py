from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.mimo_contract_circuit import (
    load_mimo_contract_circuit,
    record_mimo_v2_outcome,
)


def test_contract_failure_opens_breaker_immediately_and_survives_restart(tmp_path):
    database_path = tmp_path / "mimo-circuit.db"
    factory = create_session_factory(database_path)
    observed_at = datetime(2026, 8, 11, 18, tzinfo=UTC)

    state = record_mimo_v2_outcome(
        factory,
        outcome="contract_validation_failed",
        observed_at=observed_at,
    )

    assert state.is_open is True
    assert state.opened_reason == "contract_validation_failed"
    assert state.opened_at == observed_at
    restarted_factory = create_session_factory(database_path)
    assert load_mimo_contract_circuit(restarted_factory) == state


def test_adapter_failure_opens_breaker_immediately(tmp_path):
    factory = create_session_factory(tmp_path / "adapter-circuit.db")

    state = record_mimo_v2_outcome(factory, outcome="adapter_failure")

    assert state.is_open is True
    assert state.opened_reason == "adapter_failure"


@pytest.mark.parametrize(
    "outcome",
    ["provider_timeout", "provider_http_error", "invalid_json"],
)
def test_three_consecutive_transport_failures_open_breaker(tmp_path, outcome):
    factory = create_session_factory(tmp_path / f"{outcome}.db")

    first = record_mimo_v2_outcome(factory, outcome=outcome)
    second = record_mimo_v2_outcome(factory, outcome=outcome)
    third = record_mimo_v2_outcome(factory, outcome=outcome)

    assert first.is_open is False
    assert first.consecutive_transport_failures == 1
    assert second.is_open is False
    assert second.consecutive_transport_failures == 2
    assert third.is_open is True
    assert third.consecutive_transport_failures == 3
    assert third.opened_reason == "consecutive_transport_failures"


def test_success_clears_transport_count_but_does_not_silently_close_open_breaker(
    tmp_path,
):
    factory = create_session_factory(tmp_path / "success-reset.db")
    record_mimo_v2_outcome(factory, outcome="provider_timeout")
    observed_at = datetime(2026, 8, 11, 18, 30, tzinfo=UTC)

    state = record_mimo_v2_outcome(
        factory,
        outcome="success",
        observed_at=observed_at,
    )

    assert state.is_open is False
    assert state.consecutive_transport_failures == 0
    assert state.last_success_at == observed_at

    record_mimo_v2_outcome(factory, outcome="contract_validation_failed")
    still_open = record_mimo_v2_outcome(factory, outcome="success")
    assert still_open.is_open is True
    assert still_open.consecutive_transport_failures == 0


@pytest.mark.parametrize("outcome", ["business_outcome", "safety_refusal"])
def test_business_and_safety_outcomes_do_not_count_as_failures(tmp_path, outcome):
    factory = create_session_factory(tmp_path / f"{outcome}.db")
    record_mimo_v2_outcome(factory, outcome="provider_timeout")

    state = record_mimo_v2_outcome(factory, outcome=outcome)

    assert state.is_open is False
    assert state.consecutive_transport_failures == 1


def test_unknown_circuit_outcome_is_rejected(tmp_path):
    factory = create_session_factory(tmp_path / "unknown.db")

    with pytest.raises(ValueError, match="mimo_v2_outcome_invalid"):
        record_mimo_v2_outcome(factory, outcome="unknown")
