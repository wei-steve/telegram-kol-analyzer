from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_kol_research.deepcoin_maintenance_evidence import (
    DeepcoinMaintenanceEvidence,
)
from telegram_kol_research.immutable_control_bootstrap import (
    BootstrapBlocked,
    BootstrapUnknown,
    apply_immutable_control_bootstrap_plan,
    build_immutable_control_bootstrap_plan,
)
from telegram_kol_research.reviewed_pending_entry_cancel import (
    REVIEWED_PENDING_ENTRY_TARGETS,
)
from telegram_kol_research.scoped_release_activation import ReleaseEvidence


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
CANDIDATE = "c" * 40
CONTROL = "d" * 40
REJECTED = "ffb06d19eabfd32dfdab2942b2152fd2809e3d17"
COMPONENTS = ("web", "ingest", "worker", "monitor")


def _release(commit: str, *, components=COMPONENTS) -> ReleaseEvidence:
    return ReleaseEvidence(
        commit=commit,
        manifest_sha256=("a" if commit == CANDIDATE else "b") * 64,
        content_sha256=("1" if commit == CANDIDATE else "2") * 64,
        action_manifest={"components": list(components)},
        release_path=SimpleNamespace(),
    )


def _evidence(*, age_seconds: int = 0, pending=()) -> DeepcoinMaintenanceEvidence:
    return DeepcoinMaintenanceEvidence(
        observed_at=NOW - timedelta(seconds=age_seconds),
        target_order_id="bootstrap-control",
        status="complete",
        reason_code=None,
        positions=(),
        regular_orders=(),
        pending_triggers=tuple(pending),
        trigger_history=(),
        fills=(),
        retry_count=0,
        fingerprint="e" * 64,
    )


def _plan(*, candidate=None, evidence=None):
    return build_immutable_control_bootstrap_plan(
        action_id="bootstrap-001",
        candidate=candidate or _release(CANDIDATE),
        control=_release(CONTROL),
        evidence=evidence or _evidence(),
        completed_order_ids=tuple(
            target.order_id for target in REVIEWED_PENDING_ENTRY_TARGETS
        ),
        expected_generation=7,
        now=NOW,
    )


class Authority:
    def __init__(self) -> None:
        self.state = "idle"
        self.generation = 7
        self.events: list[str] = []

    def acquire_bootstrap(self, plan):
        assert self.state == "idle"
        self.state = "held"
        self.generation += 1
        self.events.append("acquire")
        return SimpleNamespace(token="bootstrap-token", generation=self.generation)

    def release_bootstrap(self, *, token, generation, released_at):
        assert token == "bootstrap-token"
        assert generation == self.generation
        self.state = "idle"
        self.events.append("release")
        return True

    def block_bootstrap(self, *, token, generation, reason_code, blocked_at):
        self.state = "blocked"
        self.events.append(f"block:{reason_code}")


class Guard:
    def __init__(self) -> None:
        self.blocked_reason = None
        self.restored = False
        self.completed = False
        self.quiescent = False

    def enter(self, *, action_id):
        assert action_id == "bootstrap-001"
        self.quiescent = True
        return SimpleNamespace(fingerprint="f" * 64)

    def prove_quiescent(self):
        assert self.quiescent

    def mark_safe_to_restore(self, *, expected_fingerprint):
        return SimpleNamespace(fingerprint="9" * 64)

    def restore(self, *, expected_fingerprint):
        self.restored = True
        self.quiescent = False

    def block(self, *, reason_code):
        self.blocked_reason = reason_code

    def complete_candidate_takeover(self, *, expected_fingerprint):
        assert expected_fingerprint == "f" * 64
        self.completed = True
        self.quiescent = False


def _identity(role: str, *, capabilities=True, ticks=2000):
    worker_capabilities = {
        key: bool(capabilities)
        for key in (
            "global_exchange_authority",
            "management",
            "protection",
            "close",
            "tpsl",
            "rescue",
        )
    }
    return {
        "runtime_role": role,
        "release_commit": CANDIDATE,
        "manifest_sha256": "a" * 64,
        "pid": 200 + COMPONENTS.index(role),
        "process_start_ticks": ticks + COMPONENTS.index(role),
        "systemd_main_pid": 200 + COMPONENTS.index(role),
        "systemd_start_ticks": ticks + COMPONENTS.index(role),
        "entry_admission_frozen": True,
        "capabilities": worker_capabilities if role == "worker" else {},
        "authority_evidence": {
            "management_cycle": {"fresh": True, "successful": True},
            "protection_cycle": {"fresh": True, "successful": True},
            "close_cycle": {"fresh": True, "successful": True},
            "tpsl_cycle": {"fresh": True, "successful": True},
            "rescue_cycle": {"fresh": True, "successful": True},
        } if role == "worker" else {},
    }


class Runtime:
    def __init__(self, authority: Authority) -> None:
        self.authority = authority
        self.events: list[str] = []
        self.capabilities = True
        self.fail_static = False
        self.fail_rollback = False
        self.unknown_start = False
        self.legacy_worker = (101, 1001)

    def legacy_identities(self):
        return {"worker": {"pid": 101, "process_start_ticks": 1001}}

    def capture_dropin_preimages(self):
        self.events.append("capture-preimages")
        return {"dropins": "legacy"}

    def publish_candidate_configuration(self, plan, *, entry_frozen):
        assert entry_frozen is True
        self.events.append("publish-candidate")

    def verify_candidate_configuration(self, plan):
        self.events.append("verify-static")
        if self.fail_static:
            raise RuntimeError("systemd_verify_failed")

    def open_candidate_start_boundary(self, plan):
        assert self.authority.state == "held"
        self.events.append("open-candidate-boundary")
        if self.unknown_start:
            raise BootstrapUnknown("candidate_start_unknown")

    def candidate_identities(self):
        self.events.append("candidate-identities")
        return {
            role: _identity(role, capabilities=self.capabilities)
            for role in COMPONENTS
        }

    def no_exchange_write_authority_round_trip(self, *, expected_generation):
        assert self.authority.state == "idle"
        assert expected_generation == self.authority.generation
        self.events.append("authority-self-test")
        self.authority.generation += 1
        return self.authority.generation

    def verify_monitor(self, plan):
        self.events.append("verify-monitor")

    def reinhibit_and_stop_candidate(self):
        self.events.append("reinhibit-candidate")

    def restore_control_configuration(self, preimages):
        self.events.append("restore-control")
        if self.fail_rollback:
            raise RuntimeError("rollback_unknown")


def _apply(plan=None, *, runtime=None, authority=None, guard=None):
    authority = authority or Authority()
    runtime = runtime or Runtime(authority)
    guard = guard or Guard()
    result = apply_immutable_control_bootstrap_plan(
        plan or _plan(),
        guard=guard,
        authority=authority,
        runtime=runtime,
        now=NOW,
    )
    return result, runtime, authority, guard


def test_bootstrap_requires_web_ingest_worker_and_monitor_exactly():
    with pytest.raises(BootstrapBlocked, match="bootstrap_scope_invalid"):
        _plan(candidate=_release(CANDIDATE, components=("web", "worker")))


def test_bootstrap_rejects_ffb_release_or_same_candidate_and_control_sha():
    with pytest.raises(BootstrapBlocked, match="rejected_candidate_release"):
        _plan(candidate=_release(REJECTED))
    with pytest.raises(BootstrapBlocked, match="candidate_equals_control"):
        build_immutable_control_bootstrap_plan(
            action_id="bootstrap-001",
            candidate=_release(CONTROL),
            control=_release(CONTROL),
            evidence=_evidence(),
            completed_order_ids=tuple(
                target.order_id for target in REVIEWED_PENDING_ENTRY_TARGETS
            ),
            expected_generation=7,
            now=NOW,
        )


def test_candidate_pid_start_tuple_must_differ_from_legacy():
    authority = Authority()
    runtime = Runtime(authority)
    original = runtime.candidate_identities

    def same_worker():
        identities = original()
        identities["worker"].update(
            pid=101,
            process_start_ticks=1001,
            systemd_main_pid=101,
            systemd_start_ticks=1001,
        )
        return identities

    runtime.candidate_identities = same_worker
    with pytest.raises(BootstrapBlocked, match="candidate_identity_not_distinct"):
        _apply(runtime=runtime, authority=authority)
    assert "reinhibit-candidate" in runtime.events


def test_candidate_starts_while_bootstrap_authority_is_held():
    result, runtime, authority, guard = _apply()
    assert result.status == "bootstrapped_entry_frozen"
    assert authority.events == ["acquire", "release"]
    assert guard.completed is True


def test_candidate_entry_is_frozen_but_independent_capabilities_are_proven():
    result, runtime, _, _ = _apply()
    assert result.entry_admission_frozen is True
    assert "candidate-identities" in runtime.events


def test_all_disabled_capabilities_reject_bootstrap():
    authority = Authority()
    runtime = Runtime(authority)
    runtime.capabilities = False
    with pytest.raises(BootstrapBlocked, match="candidate_capability_unproven"):
        _apply(runtime=runtime, authority=authority)
    assert "reinhibit-candidate" in runtime.events


def test_candidate_completes_no_exchange_write_authority_round_trip():
    result, runtime, authority, _ = _apply()
    assert "authority-self-test" in runtime.events
    assert result.generation == authority.generation == 9


def test_partial_dropin_or_systemd_verify_failure_rolls_back_before_open():
    authority = Authority()
    runtime = Runtime(authority)
    runtime.fail_static = True
    guard = Guard()
    with pytest.raises(BootstrapBlocked, match="bootstrap_rolled_back"):
        _apply(runtime=runtime, authority=authority, guard=guard)
    assert "open-candidate-boundary" not in runtime.events
    assert runtime.events[-1] == "restore-control"
    assert guard.restored is True


def test_unknown_write_or_rollback_uncertainty_retains_inhibit_and_blocks():
    authority = Authority()
    runtime = Runtime(authority)
    runtime.unknown_start = True
    guard = Guard()
    with pytest.raises(BootstrapBlocked, match="candidate_start_unknown"):
        _apply(runtime=runtime, authority=authority, guard=guard)
    assert authority.state == "blocked"
    assert guard.blocked_reason == "candidate_start_unknown"
    assert guard.restored is False

    authority = Authority()
    runtime = Runtime(authority)
    runtime.fail_static = True
    runtime.fail_rollback = True
    guard = Guard()
    with pytest.raises(BootstrapBlocked, match="bootstrap_rollback_unknown"):
        _apply(runtime=runtime, authority=authority, guard=guard)
    assert authority.state == "blocked"
    assert guard.restored is False
