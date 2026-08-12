from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import importlib
import threading

import pytest

from telegram_kol_research.db import create_session_factory


def _circuit_module():
    return importlib.import_module(
        "telegram_kol_research.mimo_contract_circuit"
    )


def test_contract_failure_opens_circuit_immediately_and_survives_restart(
    tmp_path,
):
    module = _circuit_module()
    database_path = tmp_path / "mimo-circuit.db"
    factory = create_session_factory(database_path)
    observed_at = datetime(2026, 8, 11, 18, tzinfo=UTC)

    state = module.record_mimo_v2_outcome(
        factory,
        outcome="contract_validation_failed",
        observed_at=observed_at,
    )

    assert state.is_open is True
    assert state.opened_reason == "contract_validation_failed"
    assert state.opened_at == observed_at
    restarted_factory = create_session_factory(database_path)
    assert module.load_mimo_contract_circuit(restarted_factory) == state


def test_adapter_failure_opens_circuit_immediately(tmp_path):
    module = _circuit_module()
    factory = create_session_factory(tmp_path / "adapter-circuit.db")

    state = module.record_mimo_v2_outcome(factory, outcome="adapter_failure")

    assert state.is_open is True
    assert state.opened_reason == "adapter_failure"


@pytest.mark.parametrize(
    "outcome",
    ["provider_timeout", "provider_http_error", "invalid_json"],
)
def test_third_consecutive_transport_failure_opens_circuit(tmp_path, outcome):
    module = _circuit_module()
    factory = create_session_factory(tmp_path / f"{outcome}.db")

    first = module.record_mimo_v2_outcome(factory, outcome=outcome)
    second = module.record_mimo_v2_outcome(factory, outcome=outcome)
    third = module.record_mimo_v2_outcome(factory, outcome=outcome)

    assert first.is_open is False
    assert first.consecutive_transport_failures == 1
    assert second.is_open is False
    assert second.consecutive_transport_failures == 2
    assert third.is_open is True
    assert third.consecutive_transport_failures == 3
    assert third.opened_reason == "consecutive_transport_failures"


def test_success_resets_count_without_silently_closing_open_circuit(tmp_path):
    module = _circuit_module()
    factory = create_session_factory(tmp_path / "success-reset.db")
    module.record_mimo_v2_outcome(factory, outcome="provider_timeout")
    observed_at = datetime(2026, 8, 11, 18, 30, tzinfo=UTC)

    state = module.record_mimo_v2_outcome(
        factory,
        outcome="success",
        observed_at=observed_at,
    )

    assert state.is_open is False
    assert state.consecutive_transport_failures == 0
    assert state.last_success_at == observed_at

    module.record_mimo_v2_outcome(
        factory,
        outcome="contract_validation_failed",
    )
    still_open = module.record_mimo_v2_outcome(factory, outcome="success")
    assert still_open.is_open is True
    assert still_open.consecutive_transport_failures == 0


@pytest.mark.parametrize("outcome", ["business_outcome", "safety_refusal"])
def test_business_and_safety_outcomes_do_not_change_failure_count(
    tmp_path,
    outcome,
):
    module = _circuit_module()
    factory = create_session_factory(tmp_path / f"{outcome}.db")
    module.record_mimo_v2_outcome(factory, outcome="provider_timeout")

    state = module.record_mimo_v2_outcome(factory, outcome=outcome)

    assert state.is_open is False
    assert state.consecutive_transport_failures == 1


def test_unknown_circuit_outcome_is_rejected(tmp_path):
    module = _circuit_module()
    factory = create_session_factory(tmp_path / "unknown.db")

    with pytest.raises(ValueError, match="mimo_v2_outcome_invalid"):
        module.record_mimo_v2_outcome(factory, outcome="unknown")


def test_three_concurrent_transport_failures_open_circuit(tmp_path):
    module = _circuit_module()
    factory = create_session_factory(tmp_path / "concurrent-circuit.db")
    barrier = threading.Barrier(3)

    def fail_once(_index):
        barrier.wait()
        return module.record_mimo_v2_outcome(
            factory,
            outcome="provider_timeout",
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fail_once, range(3)))

    state = module.load_mimo_contract_circuit(factory)
    assert state.consecutive_transport_failures == 3
    assert state.is_open is True
