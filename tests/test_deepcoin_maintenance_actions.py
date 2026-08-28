from __future__ import annotations

from dataclasses import dataclass

import pytest

from telegram_kol_research.deepcoin_maintenance_actions import (
    MaintenanceBlocked,
    SingleOrderDrainRequest,
    run_single_order_drain,
)


class ExchangeThatAcceptedThenTimedOut:
    def __init__(self) -> None:
        self.cancel_calls = 0

    def cancel_trigger_order(self, *, order_id: str) -> None:
        self.cancel_calls += 1
        raise TimeoutError(f"response lost for {order_id}")


class FakeAuthority:
    def __init__(self, *, state: str, generation: int) -> None:
        self.state = state
        self.generation = generation
        self.write_boundary_reached = False
        self.block_reason: str | None = None

    def acquire(self, request: SingleOrderDrainRequest) -> None:
        assert self.state == "idle"
        self.state = "held"
        self.generation += 1

    def mark_write_boundary(self) -> None:
        assert self.state == "held"
        self.write_boundary_reached = True

    def block(self, *, reason_code: str) -> None:
        assert self.state == "held"
        self.state = "blocked"
        self.block_reason = reason_code


@dataclass(frozen=True, slots=True)
class RebootState:
    worker_start_allowed: bool


class FakePersistentGuard:
    def __init__(self) -> None:
        self.masked = False
        self.block_reason: str | None = None
        self.restore_calls = 0

    def enter(self, *, action_id: str) -> None:
        assert action_id
        self.masked = True

    def prove_quiescent(self) -> None:
        assert self.masked

    def block(self, *, reason_code: str) -> None:
        self.block_reason = reason_code

    def restore(self) -> None:
        self.restore_calls += 1
        self.masked = False

    def simulate_host_reboot(self) -> RebootState:
        return RebootState(worker_start_allowed=not self.masked)


class FailIfCalled:
    def terminalize(self) -> None:
        raise AssertionError("terminalization must not run after unknown")


def _request() -> SingleOrderDrainRequest:
    return SingleOrderDrainRequest(
        action_id="drain-001",
        order_id="canonical-order",
        plan_sha256="a" * 64,
        evidence_sha256="b" * 64,
        confirmation_token="fresh-token",
    )


def test_cancel_timeout_then_crash_and_reboot_never_retries_or_restores() -> None:
    exchange = ExchangeThatAcceptedThenTimedOut()
    authority = FakeAuthority(state="idle", generation=7)
    guard = FakePersistentGuard()

    with pytest.raises(MaintenanceBlocked) as exc_info:
        run_single_order_drain(
            request=_request(),
            authority=authority,
            exchange=exchange,
            guard=guard,
            terminalizer=FailIfCalled(),
        )

    rebooted = guard.simulate_host_reboot()
    assert str(exc_info.value) == "exchange_outcome_unknown"
    assert exchange.cancel_calls == 1
    assert authority.state in {"held", "blocked"}
    assert authority.write_boundary_reached is True
    assert authority.block_reason == "exchange_outcome_unknown"
    assert guard.block_reason == "exchange_outcome_unknown"
    assert rebooted.worker_start_allowed is False
    assert guard.restore_calls == 0
