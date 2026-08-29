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
from telegram_kol_research.entry_authority_seed import SeedPlanRefused, SeedResult
from telegram_kol_research.reviewed_pending_entry_cancel import (
    ReviewedPendingEntryCancelAction,
    ReviewedPendingEntryCancelPlan,
    ReviewedPendingEntryCancelResult,
)


def _request() -> SingleOrderDrainRequest:
    return SingleOrderDrainRequest(
        action_id="drain-001",
        order_id="canonical-order",
        plan_sha256="a" * 64,
        evidence_sha256="b" * 64,
        confirmation_token="fresh-token",
    )


def _drain_plan() -> ReviewedPendingEntryCancelPlan:
    action = ReviewedPendingEntryCancelAction(
        order_id="canonical-order",
        instrument_id="ETH-USDT-SWAP",
        lifecycle_id=1,
        execution_binding_id=2,
        execution_order_leg_id=3,
        strategy_instance_id="strategy",
        trigger_price="1",
        size="1",
        embedded_stop_price="0.9",
        request_fingerprint="c" * 64,
        request_json_fingerprint="d" * 64,
        exchange_row_fingerprint="e" * 64,
        action_id="drain-001",
    )
    return ReviewedPendingEntryCancelPlan(
        created_at=datetime(2026, 8, 28, tzinfo=UTC),
        actions=(action,),
        conflicts=(),
        completed_order_ids=(),
        expected_generation=7,
        evidence_fingerprint="b" * 64,
        pending_fingerprints=(("canonical-order", "e" * 64),),
        fingerprint="a" * 64,
    )


def test_single_order_action_dispatches_exactly_once(monkeypatch) -> None:
    import telegram_kol_research.deepcoin_maintenance_actions as actions

    captured = []
    expected = ReviewedPendingEntryCancelResult(
        status="cancelled",
        order_id="canonical-order",
    )
    monkeypatch.setattr(
        actions,
        "apply_reviewed_pending_entry_cancel_plan",
        lambda *args, **kwargs: captured.append((args, kwargs)) or expected,
    )

    result = run_single_order_drain(
        session_factory=object(),
        plan=_drain_plan(),
        request=_request(),
        deepcoin_client=object(),
        targets=(object(),),
        guard=object(),
        authorization_expires_at=datetime(2026, 8, 28, 0, 15, tzinfo=UTC),
        now=datetime(2026, 8, 28, tzinfo=UTC),
    )

    assert result is expected
    assert len(captured) == 1
    assert captured[0][1]["order_id"] == "canonical-order"
    assert captured[0][1]["confirmation_token"] == "fresh-token"
    assert captured[0][1]["authorization_expires_at"] == datetime(
        2026, 8, 28, 0, 15, tzinfo=UTC
    )


def test_single_order_action_rejects_hash_or_action_mismatch(monkeypatch) -> None:
    import telegram_kol_research.deepcoin_maintenance_actions as actions

    monkeypatch.setattr(
        actions,
        "apply_reviewed_pending_entry_cancel_plan",
        lambda *args, **kwargs: pytest.fail("must not dispatch"),
    )
    bad = SingleOrderDrainRequest(
        action_id="other-action",
        order_id="canonical-order",
        plan_sha256="a" * 64,
        evidence_sha256="b" * 64,
        confirmation_token="fresh-token",
    )

    with pytest.raises(ValueError, match="exactly one"):
        run_single_order_drain(
            session_factory=object(),
            plan=_drain_plan(),
            request=bad,
            deepcoin_client=object(),
            targets=(object(),),
            guard=object(),
            authorization_expires_at=datetime(
                2026, 8, 28, 0, 15, tzinfo=UTC
            ),
            now=datetime(2026, 8, 28, tzinfo=UTC),
        )


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


class CompensatedRestoreFailureGuard(FakeSeedActionGuard):
    def restore(self, *, expected_fingerprint: str) -> None:
        self.restore_fingerprints.append(expected_fingerprint)
        self.block_reason = "maintenance_restore_compensation_failed"
        raise RuntimeError("maintenance_restore_compensation_failed")


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


def test_seed_action_preserves_specific_restore_compensation_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import telegram_kol_research.deepcoin_maintenance_actions as actions

    guard = CompensatedRestoreFailureGuard()
    monkeypatch.setattr(
        actions,
        "apply_entry_authority_seed_plan",
        lambda *args, **kwargs: SeedResult(
            status="seeded",
            plan_fingerprint="d" * 64,
            backup_path=tmp_path / "backup.sqlite3",
        ),
    )

    with pytest.raises(
        MaintenanceBlocked,
        match="maintenance_restore_compensation_failed",
    ):
        run_entry_authority_seed(
            action_id="seed-001",
            database_path=tmp_path / "production.sqlite3",
            backup_path=tmp_path / "backup.sqlite3",
            expected_fingerprint="d" * 64,
            guard=guard,
            now=datetime(2026, 8, 28, tzinfo=UTC),
        )

    assert guard.block_reason == "maintenance_restore_compensation_failed"


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


def test_seed_authorization_expired_after_quiescence_restores_without_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import telegram_kol_research.deepcoin_maintenance_actions as actions

    guard = FakeSeedActionGuard()
    monkeypatch.setattr(
        actions,
        "apply_entry_authority_seed_plan",
        lambda *args, **kwargs: pytest.fail("seed write must stay unreachable"),
    )
    deadline = datetime(2026, 8, 28, 0, 10, tzinfo=UTC)

    with pytest.raises(SeedPlanRefused, match="maintenance_authorization_expired"):
        run_entry_authority_seed(
            action_id="seed-001",
            database_path=tmp_path / "production.sqlite3",
            backup_path=tmp_path / "backup.sqlite3",
            expected_fingerprint="d" * 64,
            guard=guard,
            now=datetime(2026, 8, 28, tzinfo=UTC),
            authorization_expires_at=deadline,
            clock=lambda: deadline,
        )

    assert guard.restore_fingerprints == ["c" * 64]
    assert guard.masked is False
