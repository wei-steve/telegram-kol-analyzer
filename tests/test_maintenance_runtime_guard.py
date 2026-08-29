from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from telegram_kol_research.maintenance_runtime_guard import (
    MAINTENANCE_UNITS,
    GuardError,
    MaintenanceRuntimeGuard,
    SystemdMaintenanceRuntimeAdapter,
    UnitPreimage,
)


class FakeSystemdRuntime:
    def __init__(
        self,
        *,
        masked: bool = False,
        active: bool = True,
        main_pid: int = 0,
        cgroup_pids: tuple[int, ...] = (),
        process_scans: tuple[tuple[int, ...], ...] = ((), ()),
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.start_calls: list[str] = []
        self._states = {
            unit: UnitPreimage(
                unit=unit,
                enabled_state="enabled",
                active_state="active" if active else "inactive",
                masked=masked,
            )
            for unit in MAINTENANCE_UNITS
        }
        self._masked = {unit: masked for unit in MAINTENANCE_UNITS}
        self._active = {unit: active for unit in MAINTENANCE_UNITS}
        self._main_pid = main_pid
        self._cgroup_pids = cgroup_pids
        self._process_scans = list(process_scans)

    @classmethod
    def active_legacy(cls) -> "FakeSystemdRuntime":
        return cls()

    @classmethod
    def from_persisted_masks(
        cls,
        units: tuple[str, ...],
    ) -> "FakeSystemdRuntime":
        assert units == MAINTENANCE_UNITS
        return cls(masked=True, active=False)

    def inspect_unit(self, unit: str) -> UnitPreimage:
        return self._states[unit]

    def mask_unit(self, unit: str) -> None:
        self.calls.append(("mask", unit))
        self._masked[unit] = True

    def stop_unit(self, unit: str) -> None:
        self.calls.append(("stop", unit))
        self._active[unit] = False

    def unmask_unit(self, unit: str) -> None:
        self.calls.append(("unmask", unit))
        self._masked[unit] = False

    def start_unit(self, unit: str) -> None:
        self.calls.append(("start", unit))
        self.start_calls.append(unit)
        self._active[unit] = True

    def restore_enabled_state(self, unit: str, state: str) -> None:
        self.calls.append((f"enable:{state}", unit))

    def is_masked(self, unit: str) -> bool:
        return self._masked[unit]

    def main_pid(self, unit: str) -> int:
        return self._main_pid

    def cgroup_pids(self, unit: str) -> tuple[int, ...]:
        return self._cgroup_pids

    def matching_processes(self) -> tuple[int, ...]:
        if self._process_scans:
            return self._process_scans.pop(0)
        return ()


def _guard(
    tmp_path: Path,
    runtime: FakeSystemdRuntime,
    *,
    expected_uid: int | None = None,
) -> MaintenanceRuntimeGuard:
    return MaintenanceRuntimeGuard(
        runtime=runtime,
        receipt_path=tmp_path / "guard.json",
        lock_path=tmp_path / "guard.lock",
        expected_uid=os.getuid() if expected_uid is None else expected_uid,
    )


def test_enter_guard_persistently_masks_before_stopping_all_units(
    tmp_path: Path,
) -> None:
    runtime = FakeSystemdRuntime.active_legacy()
    guard = _guard(tmp_path, runtime)

    receipt = guard.enter(action_id="drain-001")

    boundary = len(MAINTENANCE_UNITS)
    assert runtime.calls[:boundary] == [
        ("mask", unit) for unit in MAINTENANCE_UNITS
    ]
    assert runtime.calls[boundary : boundary * 2] == [
        ("stop", unit) for unit in MAINTENANCE_UNITS
    ]
    assert all(runtime.is_masked(unit) for unit in MAINTENANCE_UNITS)
    assert receipt.safe_to_restore is False
    assert receipt.blocked_reason is None
    assert (tmp_path / "guard.json").stat().st_mode & 0o777 == 0o600


def test_enter_blocks_before_stop_when_a_mask_is_not_persistent(
    tmp_path: Path,
) -> None:
    runtime = FakeSystemdRuntime.active_legacy()
    original = runtime.mask_unit

    def drop_last_mask(unit: str) -> None:
        original(unit)
        if unit == MAINTENANCE_UNITS[-1]:
            runtime._masked[unit] = False

    runtime.mask_unit = drop_last_mask

    with pytest.raises(GuardError, match="maintenance_unit_mask_not_persistent"):
        _guard(tmp_path, runtime).enter(action_id="drain-001")

    assert not any(call[0] == "stop" for call in runtime.calls)
    receipt = json.loads((tmp_path / "guard.json").read_text())
    assert receipt["blocked_reason"] == "maintenance_guard_enter_failed"


def test_systemd_adapter_treats_absent_inactive_cgroup_as_empty(monkeypatch):
    adapter = SystemdMaintenanceRuntimeAdapter()
    monkeypatch.setattr(
        adapter,
        "_run",
        lambda *args, **kwargs: SimpleNamespace(stdout="\n"),
    )

    assert adapter.cgroup_pids("telegram-kol-worker.service") == ()


def test_systemd_adapter_uses_persistent_condition_dropin_with_local_unit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    unit = MAINTENANCE_UNITS[0]
    (tmp_path / unit).write_text("[Service]\nExecStart=/bin/true\n")
    adapter = SystemdMaintenanceRuntimeAdapter(
        dropin_root=tmp_path,
        expected_uid=os.getuid(),
    )
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(
            stdout="no\n" if "--property=NeedDaemonReload" in command else ""
        )

    monkeypatch.setattr(adapter, "_run", run)

    adapter.mask_unit(unit)
    assert adapter.is_masked(unit) is True
    assert not any(command[:2] == ["systemctl", "mask"] for command in calls)
    adapter.unmask_unit(unit)
    assert adapter.is_masked(unit) is False


def test_systemd_adapter_treats_unreadable_proc_cmdline_as_unknown(
    monkeypatch,
) -> None:
    adapter = SystemdMaintenanceRuntimeAdapter()
    original_iterdir = Path.iterdir
    original_read_bytes = Path.read_bytes

    def iterdir(path):
        if path == Path("/proc"):
            return iter((Path("/proc/123"),))
        return original_iterdir(path)

    def read_bytes(path):
        if path == Path("/proc/123/cmdline"):
            raise PermissionError("denied")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "read_bytes", read_bytes)

    with pytest.raises(GuardError, match="maintenance_process_scan_unknown"):
        adapter.matching_processes()


def test_restore_failure_stops_and_reinhibits_every_unit(tmp_path: Path) -> None:
    runtime = FakeSystemdRuntime.active_legacy()
    guard = _guard(tmp_path, runtime)
    receipt = guard.enter(action_id="drain-001")
    safe = guard.mark_safe_to_restore(
        expected_fingerprint=receipt.fingerprint
    )
    original_start = runtime.start_unit

    def fail_late(unit: str) -> None:
        if unit == MAINTENANCE_UNITS[-1]:
            raise RuntimeError("late start failure")
        original_start(unit)

    runtime.start_unit = fail_late

    with pytest.raises(GuardError, match="maintenance_restore_failed"):
        guard.restore(expected_fingerprint=safe.fingerprint)

    assert all(runtime.is_masked(unit) for unit in MAINTENANCE_UNITS)
    assert all(runtime._active[unit] is False for unit in MAINTENANCE_UNITS)
    stored = json.loads((tmp_path / "guard.json").read_text())
    assert stored["safe_to_restore"] is False
    assert stored["blocked_reason"] == "maintenance_restore_failed"


def test_restore_compensation_requires_proven_process_quiescence(
    tmp_path: Path,
) -> None:
    runtime = FakeSystemdRuntime(
        process_scans=((), (), (4242,)),
    )
    guard = _guard(tmp_path, runtime)
    receipt = guard.enter(action_id="drain-001")
    safe = guard.mark_safe_to_restore(
        expected_fingerprint=receipt.fingerprint
    )
    original_start = runtime.start_unit

    def fail_late(unit: str) -> None:
        if unit == MAINTENANCE_UNITS[-1]:
            raise RuntimeError("late start failure")
        original_start(unit)

    runtime.start_unit = fail_late

    with pytest.raises(
        GuardError,
        match="maintenance_restore_compensation_failed",
    ):
        guard.restore(expected_fingerprint=safe.fingerprint)

    stored = json.loads((tmp_path / "guard.json").read_text())
    assert stored["safe_to_restore"] is False
    assert stored["blocked_reason"] == (
        "maintenance_restore_compensation_failed"
    )


def test_receipt_removal_failure_reinhibits_and_proves_quiescence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = FakeSystemdRuntime.active_legacy()
    guard = _guard(tmp_path, runtime)
    receipt = guard.enter(action_id="drain-001")
    safe = guard.mark_safe_to_restore(
        expected_fingerprint=receipt.fingerprint
    )

    monkeypatch.setattr(
        guard,
        "_remove_receipt",
        lambda: (_ for _ in ()).throw(
            GuardError("maintenance_receipt_remove_failed")
        ),
    )

    with pytest.raises(GuardError, match="maintenance_restore_failed"):
        guard.restore(expected_fingerprint=safe.fingerprint)

    assert all(runtime.is_masked(unit) for unit in MAINTENANCE_UNITS)
    assert all(runtime._active[unit] is False for unit in MAINTENANCE_UNITS)
    stored = json.loads((tmp_path / "guard.json").read_text())
    assert stored["safe_to_restore"] is False
    assert stored["blocked_reason"] == "maintenance_restore_failed"


def test_complete_candidate_takeover_requires_all_inhibitors_removed(
    tmp_path: Path,
) -> None:
    runtime = FakeSystemdRuntime.active_legacy()
    guard = _guard(tmp_path, runtime)
    receipt = guard.enter(action_id="bootstrap-001")

    with pytest.raises(GuardError, match="candidate_takeover_inhibited"):
        guard.complete_candidate_takeover(
            expected_fingerprint=receipt.fingerprint,
        )

    for unit in MAINTENANCE_UNITS:
        runtime.unmask_unit(unit)
    guard.complete_candidate_takeover(
        expected_fingerprint=receipt.fingerprint,
    )

    assert not (tmp_path / "guard.json").exists()


def test_reconcile_after_process_crash_and_reboot_keeps_units_masked(
    tmp_path: Path,
) -> None:
    original_runtime = FakeSystemdRuntime.active_legacy()
    original = _guard(tmp_path, original_runtime)
    original.enter(action_id="drain-001")
    original.close()
    rebooted_runtime = FakeSystemdRuntime.from_persisted_masks(
        MAINTENANCE_UNITS
    )
    guard = _guard(tmp_path, rebooted_runtime)

    result = guard.reconcile_after_restart()

    assert result.safe_to_restore is False
    assert result.blocked_reason == "maintenance_recovery_required"
    assert rebooted_runtime.start_calls == []
    assert all(
        rebooted_runtime.is_masked(unit) for unit in MAINTENANCE_UNITS
    )


def test_second_guard_refuses_busy_host_lock(tmp_path: Path) -> None:
    first = _guard(tmp_path, FakeSystemdRuntime.active_legacy())
    second = _guard(tmp_path, FakeSystemdRuntime.active_legacy())
    first.enter(action_id="drain-001")

    with pytest.raises(GuardError, match="maintenance_host_lock_busy"):
        second.enter(action_id="drain-002")


def test_receipt_mutation_without_held_host_lock_is_rejected(
    tmp_path: Path,
) -> None:
    runtime = FakeSystemdRuntime.active_legacy()
    original = _guard(tmp_path, runtime)
    original.enter(action_id="drain-001")
    original.close()

    with pytest.raises(GuardError, match="maintenance_host_lock_required"):
        _guard(tmp_path, runtime).block(reason_code="exchange_outcome_unknown")


@pytest.mark.parametrize(
    ("runtime", "reason"),
    (
        (
            FakeSystemdRuntime(main_pid=99),
            "maintenance_unit_main_pid_present",
        ),
        (
            FakeSystemdRuntime(cgroup_pids=(99,)),
            "maintenance_unit_cgroup_not_empty",
        ),
        (
            FakeSystemdRuntime(process_scans=((99,), (99,))),
            "maintenance_matching_process_present",
        ),
    ),
)
def test_enter_refuses_surviving_runtime_processes(
    tmp_path: Path,
    runtime: FakeSystemdRuntime,
    reason: str,
) -> None:
    guard = _guard(tmp_path, runtime)

    with pytest.raises(GuardError, match=reason):
        guard.enter(action_id="drain-001")

    stored = json.loads((tmp_path / "guard.json").read_text())
    assert stored["blocked_reason"] == "maintenance_guard_enter_failed"
    assert all(runtime.is_masked(unit) for unit in MAINTENANCE_UNITS)


def test_guard_rejects_unsafe_receipt_mode(tmp_path: Path) -> None:
    runtime = FakeSystemdRuntime.active_legacy()
    guard = _guard(tmp_path, runtime)
    guard.enter(action_id="drain-001")
    guard.close()
    (tmp_path / "guard.json").chmod(0o644)

    with pytest.raises(GuardError, match="maintenance_receipt_unsafe"):
        _guard(tmp_path, runtime).reconcile_after_restart()


def test_guard_rejects_maintenance_artifact_owner_mismatch(
    tmp_path: Path,
) -> None:
    runtime = FakeSystemdRuntime.active_legacy()
    guard = _guard(tmp_path, runtime)
    guard.enter(action_id="drain-001")
    guard.close()

    with pytest.raises(GuardError, match="maintenance_host_lock_unsafe"):
        _guard(
            tmp_path,
            runtime,
            expected_uid=os.getuid() + 1,
        ).reconcile_after_restart()


def test_guard_rejects_receipt_fingerprint_drift(tmp_path: Path) -> None:
    runtime = FakeSystemdRuntime.active_legacy()
    guard = _guard(tmp_path, runtime)
    guard.enter(action_id="drain-001")
    guard.close()
    path = tmp_path / "guard.json"
    payload = json.loads(path.read_text())
    payload["action_id"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(GuardError, match="maintenance_receipt_invalid"):
        _guard(tmp_path, runtime).reconcile_after_restart()


def test_restore_requires_exact_safe_receipt(tmp_path: Path) -> None:
    runtime = FakeSystemdRuntime.active_legacy()
    guard = _guard(tmp_path, runtime)
    entered = guard.enter(action_id="drain-001")

    with pytest.raises(GuardError, match="maintenance_restore_not_safe"):
        guard.restore(expected_fingerprint=entered.fingerprint)

    safe = guard.mark_safe_to_restore(
        expected_fingerprint=entered.fingerprint
    )
    guard.restore(expected_fingerprint=safe.fingerprint)

    assert runtime.start_calls == list(MAINTENANCE_UNITS)
    assert all(not runtime.is_masked(unit) for unit in MAINTENANCE_UNITS)


def test_unit_preimage_is_immutable() -> None:
    row = UnitPreimage(
        unit="telegram-kol-worker.service",
        enabled_state="enabled",
        active_state="active",
        masked=False,
    )

    with pytest.raises(AttributeError):
        replace(row, unit="changed").unit = "again"
