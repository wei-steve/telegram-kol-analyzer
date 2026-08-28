from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_kol_research.deepcoin_maintenance_actions import (
    MaintenanceBlocked,
    SingleOrderDrainRequest,
    run_entry_authority_seed,
    run_single_order_drain,
)
from telegram_kol_research.entry_authority_seed import SeedResult


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


@dataclass(frozen=True, slots=True)
class FakeReceipt:
    fingerprint: str


class FakeSeedActionGuard:
    def __init__(self) -> None:
        self.masked = False
        self.block_reason: str | None = None
        self.restore_fingerprints: list[str] = []

    def enter(self, *, action_id: str) -> FakeReceipt:
        assert action_id == "seed-001"
        self.masked = True
        return FakeReceipt(fingerprint="a" * 64)

    def prove_quiescent(self) -> None:
        assert self.masked is True

    def block(self, *, reason_code: str) -> FakeReceipt:
        self.block_reason = reason_code
        return FakeReceipt(fingerprint="b" * 64)

    def mark_safe_to_restore(self, *, expected_fingerprint: str) -> FakeReceipt:
        assert expected_fingerprint == "a" * 64
        return FakeReceipt(fingerprint="c" * 64)

    def restore(self, *, expected_fingerprint: str) -> None:
        self.restore_fingerprints.append(expected_fingerprint)
        self.masked = False


def test_seed_action_restores_exact_preimage_only_after_seed_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import telegram_kol_research.deepcoin_maintenance_actions as actions

    database_path = tmp_path / "production.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    guard = FakeSeedActionGuard()
    monkeypatch.setattr(
        actions,
        "apply_entry_authority_seed_plan",
        lambda *args, **kwargs: SeedResult(
            status="seeded",
            plan_fingerprint="d" * 64,
            backup_path=backup_path,
        ),
    )

    result = run_entry_authority_seed(
        action_id="seed-001",
        database_path=database_path,
        backup_path=backup_path,
        expected_fingerprint="d" * 64,
        guard=guard,
        now=datetime(2026, 8, 28, tzinfo=UTC),
    )

    assert result.status == "seeded"
    assert guard.restore_fingerprints == ["c" * 64]
    assert guard.masked is False


def test_seed_action_blocked_result_never_restores_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import telegram_kol_research.deepcoin_maintenance_actions as actions

    database_path = tmp_path / "production.sqlite3"
    backup_path = tmp_path / "backup.sqlite3"
    guard = FakeSeedActionGuard()
    monkeypatch.setattr(
        actions,
        "apply_entry_authority_seed_plan",
        lambda *args, **kwargs: SeedResult(
            status="blocked",
            plan_fingerprint="d" * 64,
            backup_path=backup_path,
            reason_code="entry_authority_seed_restore_unknown",
        ),
    )

    with pytest.raises(MaintenanceBlocked, match="entry_authority_seed_restore_unknown"):
        run_entry_authority_seed(
            action_id="seed-001",
            database_path=database_path,
            backup_path=backup_path,
            expected_fingerprint="d" * 64,
            guard=guard,
            now=datetime(2026, 8, 28, tzinfo=UTC),
        )

    assert guard.restore_fingerprints == []
    assert guard.masked is True
