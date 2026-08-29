"""One-time, entry-frozen bootstrap of the first immutable control release."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import Any, Callable, Iterable, Mapping, Protocol

from telegram_kol_research.deepcoin_maintenance_evidence import (
    DeepcoinMaintenanceEvidence,
    DeepcoinMaintenanceEvidenceRefused,
    require_fresh_deepcoin_maintenance_evidence,
    require_fresh_deepcoin_maintenance_observed_at,
)
from telegram_kol_research.reviewed_pending_entry_cancel import (
    REVIEWED_PENDING_ENTRY_TARGETS,
)
from telegram_kol_research.runtime_deployment_identity import (
    validate_runtime_authority_scope,
    validate_runtime_identity_health,
)
from telegram_kol_research.maintenance_runtime_guard import (
    MAINTENANCE_UNITS,
    SystemdMaintenanceRuntimeAdapter,
)
from telegram_kol_research.scoped_release_activation import (
    ActivationPaths,
    ReleaseEvidence,
    SystemRuntimeAdapter,
    publish_release_dropins,
    render_release_dropin,
    validate_release,
)


BOOTSTRAP_COMPONENTS = ("web", "monitor", "ingest", "worker")
BOOTSTRAP_LIVE_RUNTIME_ROLES = ("web", "ingest", "worker")
BOOTSTRAP_TARGET = "telegram-kol-runtime.target"
BOOTSTRAP_AUTOSTART_UNITS = (
    "telegram-kol-worker.service",
    "telegram-kol-web.service",
    "telegram-kol-ingest.service",
    "telegram-kol-monitor.timer",
)
REJECTED_LEGACY_BRIDGE_RELEASE = (
    "ffb06d19eabfd32dfdab2942b2152fd2809e3d17"
)


class BootstrapBlocked(RuntimeError):
    """The bootstrap reached a proven refusal or fail-closed terminal state."""


class BootstrapUnknown(RuntimeError):
    """The bootstrap crossed a boundary whose outcome cannot be classified."""


@dataclass(frozen=True, slots=True)
class BootstrapPlan:
    action_id: str
    candidate: ReleaseEvidence
    control: ReleaseEvidence
    components: tuple[str, ...]
    evidence_sha256: str
    evidence_observed_at: datetime
    expected_generation: int
    fingerprint: str


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    status: str
    commit: str
    generation: int
    entry_admission_frozen: bool


@dataclass(frozen=True, slots=True)
class DropinPreimage:
    path: Path
    content: bytes | None


class BootstrapGuard(Protocol):
    def enter(self, *, action_id: str) -> Any: ...
    def prove_quiescent(self) -> None: ...
    def mark_safe_to_restore(self, *, expected_fingerprint: str) -> Any: ...
    def restore(self, *, expected_fingerprint: str) -> None: ...
    def block(self, *, reason_code: str) -> Any: ...
    def complete_candidate_takeover(
        self,
        *,
        expected_fingerprint: str,
    ) -> None: ...


class BootstrapAuthorityAdapter(Protocol):
    def acquire_bootstrap(self, plan: BootstrapPlan) -> Any: ...
    def release_bootstrap(
        self,
        *,
        token: str,
        generation: int,
        released_at: datetime,
    ) -> bool: ...
    def block_bootstrap(
        self,
        *,
        token: str,
        generation: int,
        reason_code: str,
        blocked_at: datetime,
    ) -> None: ...
    def no_exchange_write_round_trip(
        self,
        plan: BootstrapPlan,
        *,
        expected_generation: int,
    ) -> int: ...


class BootstrapRuntimeAdapter(Protocol):
    def legacy_identities(self) -> Mapping[str, Mapping[str, Any]]: ...
    def capture_dropin_preimages(self) -> Any: ...
    def publish_candidate_configuration(
        self,
        plan: BootstrapPlan,
        *,
        entry_frozen: bool,
    ) -> None: ...
    def verify_candidate_configuration(self, plan: BootstrapPlan) -> None: ...
    def open_candidate_start_boundary(self, plan: BootstrapPlan) -> None: ...
    def candidate_identities(self) -> Mapping[str, Mapping[str, Any]]: ...
    def verify_monitor(self, plan: BootstrapPlan) -> None: ...
    def complete_candidate_start(
        self,
        plan: BootstrapPlan,
    ) -> Mapping[str, Mapping[str, Any]]: ...
    def reinhibit_and_stop_candidate(self) -> None: ...
    def restore_control_configuration(self, preimages: Any) -> None: ...


@dataclass(slots=True)
class SystemdImmutableControlBootstrapRuntimeAdapter:
    """Concrete root-only systemd adapter for the one-time bootstrap."""

    paths: ActivationPaths
    scoped_runtime: SystemRuntimeAdapter
    control_runtime: SystemdMaintenanceRuntimeAdapter
    expected_uid: int = 0

    def legacy_identities(self) -> Mapping[str, Mapping[str, Any]]:
        return {"worker": self.scoped_runtime.runtime_identity("worker")}

    def capture_dropin_preimages(self) -> tuple[DropinPreimage, ...]:
        rows = []
        paths = [
            self.paths.dropin_root / unit
            for unit in _bootstrap_unit_files()
        ]
        for component, units in _component_units().items():
            _ = component
            for unit in units:
                paths.append(
                    self.paths.dropin_root
                    / f"{unit}.d/10-telegram-kol-release.conf"
                )
        for path in paths:
            try:
                metadata = path.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_size > 65_536
                ):
                    raise BootstrapBlocked("bootstrap_dropin_preimage_unsafe")
                content = path.read_bytes()
            except FileNotFoundError:
                content = None
            rows.append(DropinPreimage(path=path, content=content))
        return tuple(rows)

    def publish_candidate_configuration(
        self,
        plan: BootstrapPlan,
        *,
        entry_frozen: bool,
    ) -> None:
        for unit in _bootstrap_unit_files():
            _atomic_publish_configuration(
                self.paths.dropin_root / unit,
                (plan.candidate.release_path / "deploy/systemd" / unit).read_bytes(),
            )
        publish_release_dropins(
            self.paths,
            plan.candidate,
            list(plan.components),
            entry_frozen=entry_frozen,
        )
        self.scoped_runtime.daemon_reload()
        # Old releases enabled each role independently.  Disable every direct
        # boot edge and the new aggregate target before candidate processes are
        # opened.  The target becomes the one final reboot fence.
        for unit in (*BOOTSTRAP_AUTOSTART_UNITS, BOOTSTRAP_TARGET):
            self.control_runtime.restore_enabled_state(unit, "disabled")

    def verify_candidate_configuration(self, plan: BootstrapPlan) -> None:
        release = validate_release(
            plan.candidate.release_path.parent,
            plan.candidate.commit,
            expected_uid=self.expected_uid,
        )
        if (
            release.release_path != plan.candidate.release_path
            or release.manifest_sha256 != plan.candidate.manifest_sha256
        ):
            raise BootstrapBlocked("bootstrap_candidate_release_invalid")
        for unit in _bootstrap_unit_files():
            if (
                (self.paths.dropin_root / unit).read_bytes()
                != (plan.candidate.release_path / "deploy/systemd" / unit).read_bytes()
            ):
                raise BootstrapBlocked("bootstrap_candidate_unit_invalid")
        for component, units in _component_units().items():
            expected = render_release_dropin(
                plan.candidate,
                component=component,
                entry_frozen=True,
            ).encode("utf-8")
            for unit in units:
                actual = (
                    self.paths.dropin_root
                    / f"{unit}.d/10-telegram-kol-release.conf"
                ).read_bytes()
                if actual != expected:
                    raise BootstrapBlocked("bootstrap_candidate_dropin_invalid")
        _run_systemd(
            [
                "systemd-analyze",
                "verify",
                *(
                    str(self.paths.dropin_root / unit)
                    for unit in _bootstrap_unit_files()
                ),
            ]
        )
        if any(
            self.control_runtime.inspect_unit(unit).enabled_state != "disabled"
            for unit in (*BOOTSTRAP_AUTOSTART_UNITS, BOOTSTRAP_TARGET)
        ):
            raise BootstrapBlocked("bootstrap_autostart_fence_invalid")

    def open_candidate_start_boundary(self, plan: BootstrapPlan) -> None:
        _ = plan
        runtime_units = (
            "telegram-kol-worker.service",
            "telegram-kol-web.service",
            "telegram-kol-ingest.service",
        )
        try:
            for unit in runtime_units:
                self.control_runtime.unmask_unit(unit)
            if any(
                self.control_runtime.is_masked(unit)
                for unit in runtime_units
            ):
                raise RuntimeError("candidate_unmask_unproven")
            for unit in runtime_units:
                self.control_runtime.start_unit(unit)
            # Keep the candidate running, but re-install the persistent
            # inhibitors before releasing bootstrap authority. A host crash
            # during identity/self-test cannot restart any candidate process.
            for unit in runtime_units:
                self.control_runtime.mask_unit(unit)
            if any(
                not self.control_runtime.is_masked(unit)
                for unit in MAINTENANCE_UNITS
            ):
                raise RuntimeError("candidate_reinhibit_unproven")
        except Exception as exc:
            raise BootstrapUnknown("candidate_start_unknown") from exc

    def candidate_identities(self) -> Mapping[str, Mapping[str, Any]]:
        return {
            role: self.scoped_runtime.runtime_identity(role)
            for role in BOOTSTRAP_LIVE_RUNTIME_ROLES
        }

    def verify_monitor(self, plan: BootstrapPlan) -> None:
        diagnostic = "telegram-kol-monitor-diagnostic.service"
        self.control_runtime.unmask_unit(diagnostic)
        try:
            self.scoped_runtime.verify_monitor_release(plan.candidate)
        finally:
            self.control_runtime.mask_unit(diagnostic)

    def complete_candidate_start(
        self,
        plan: BootstrapPlan,
    ) -> Mapping[str, Mapping[str, Any]]:
        _ = plan
        for unit in MAINTENANCE_UNITS:
            self.control_runtime.unmask_unit(unit)
        if any(
            self.control_runtime.is_masked(unit)
            for unit in MAINTENANCE_UNITS
        ):
            raise BootstrapUnknown("candidate_final_unmask_unproven")
        # One target enable is the persistent takeover fence.  A reboot before
        # this point starts no individual role; after it, systemd requests the
        # complete candidate scope rather than a partially unmasked subset.
        self.control_runtime.restore_enabled_state(BOOTSTRAP_TARGET, "enabled")
        if (
            self.control_runtime.inspect_unit(BOOTSTRAP_TARGET).enabled_state
            != "enabled"
            or any(
                self.control_runtime.inspect_unit(unit).enabled_state
                != "disabled"
                for unit in BOOTSTRAP_AUTOSTART_UNITS
            )
        ):
            raise BootstrapUnknown("candidate_takeover_fence_unproven")
        self.control_runtime.start_unit(BOOTSTRAP_TARGET)
        if (
            self.control_runtime.inspect_unit(BOOTSTRAP_TARGET).active_state
            != "active"
            or any(
                self.control_runtime.inspect_unit(unit).active_state != "active"
                for unit in BOOTSTRAP_AUTOSTART_UNITS
            )
        ):
            raise BootstrapUnknown("candidate_takeover_scope_unproven")
        return self.candidate_identities()

    def reinhibit_and_stop_candidate(self) -> None:
        failed = False
        try:
            self.control_runtime.restore_enabled_state(
                BOOTSTRAP_TARGET,
                "disabled",
            )
        except Exception:
            failed = True
        try:
            self.control_runtime.stop_unit(BOOTSTRAP_TARGET)
        except Exception:
            failed = True
        for unit in MAINTENANCE_UNITS:
            try:
                self.control_runtime.mask_unit(unit)
            except Exception:
                failed = True
        for unit in MAINTENANCE_UNITS:
            try:
                self.control_runtime.stop_unit(unit)
            except Exception:
                failed = True
        for unit in MAINTENANCE_UNITS:
            try:
                if (
                    not self.control_runtime.is_masked(unit)
                    or self.control_runtime.main_pid(unit) != 0
                    or self.control_runtime.cgroup_pids(unit)
                ):
                    failed = True
            except Exception:
                failed = True
        for _ in range(2):
            try:
                if self.control_runtime.matching_processes():
                    failed = True
            except Exception:
                failed = True
        if failed:
            raise BootstrapUnknown("candidate_reinhibit_unproven")

    def restore_control_configuration(
        self,
        preimages: tuple[DropinPreimage, ...],
    ) -> None:
        for preimage in preimages:
            _restore_dropin_preimage(preimage)
        self.scoped_runtime.daemon_reload()


def build_immutable_control_bootstrap_plan(
    *,
    action_id: str,
    candidate: ReleaseEvidence,
    control: ReleaseEvidence,
    evidence: DeepcoinMaintenanceEvidence,
    completed_order_ids: Iterable[str],
    expected_generation: int,
    now: datetime,
) -> BootstrapPlan:
    clean_action = str(action_id or "").strip()
    if not clean_action or len(clean_action) > 128:
        raise BootstrapBlocked("bootstrap_action_invalid")
    declared_components = tuple(candidate.action_manifest.get("components", ()))
    if (
        len(declared_components) != len(BOOTSTRAP_COMPONENTS)
        or set(declared_components) != set(BOOTSTRAP_COMPONENTS)
    ):
        raise BootstrapBlocked("bootstrap_scope_invalid")
    components = BOOTSTRAP_COMPONENTS
    if candidate.commit == REJECTED_LEGACY_BRIDGE_RELEASE:
        raise BootstrapBlocked("rejected_candidate_release")
    if candidate.commit == control.commit:
        raise BootstrapBlocked("candidate_equals_control")
    try:
        require_fresh_deepcoin_maintenance_evidence(evidence, now=now)
    except DeepcoinMaintenanceEvidenceRefused as exc:
        raise BootstrapBlocked("bootstrap_evidence_unavailable") from exc
    if evidence.positions or evidence.regular_orders or evidence.pending_triggers:
        raise BootstrapBlocked("bootstrap_exchange_not_empty")
    canonical = {target.order_id for target in REVIEWED_PENDING_ENTRY_TARGETS}
    completed = {str(value) for value in completed_order_ids}
    if completed != canonical:
        raise BootstrapBlocked("bootstrap_local_terminalization_incomplete")
    if isinstance(expected_generation, bool) or int(expected_generation) < 0:
        raise BootstrapBlocked("bootstrap_generation_invalid")
    material = {
        "action_id": clean_action,
        "candidate_commit": candidate.commit,
        "candidate_manifest_sha256": candidate.manifest_sha256,
        "components": list(components),
        "control_commit": control.commit,
        "control_manifest_sha256": control.manifest_sha256,
        "evidence_sha256": evidence.fingerprint,
        "expected_generation": int(expected_generation),
    }
    return BootstrapPlan(
        action_id=clean_action,
        candidate=candidate,
        control=control,
        components=components,
        evidence_sha256=evidence.fingerprint,
        evidence_observed_at=evidence.observed_at,
        expected_generation=int(expected_generation),
        fingerprint=_fingerprint(material),
    )


def bootstrap_unit_manifest_sha256(release: ReleaseEvidence) -> str:
    """Hash the exact base units and rendered immutable activation drop-ins."""

    material: list[tuple[str, str, str]] = []
    for component, units in _component_units().items():
        dropin = render_release_dropin(
            release,
            component=component,
            entry_frozen=True,
        ).encode("utf-8")
        for unit in units:
            base = (release.release_path / "deploy/systemd" / unit).read_bytes()
            material.append(
                (
                    unit,
                    hashlib.sha256(base).hexdigest(),
                    hashlib.sha256(dropin).hexdigest(),
                )
            )
    timer = release.release_path / "deploy/systemd/telegram-kol-monitor.timer"
    material.append(
        (
            "telegram-kol-monitor.timer",
            hashlib.sha256(timer.read_bytes()).hexdigest(),
            "none",
        )
    )
    target = release.release_path / f"deploy/systemd/{BOOTSTRAP_TARGET}"
    material.append(
        (
            BOOTSTRAP_TARGET,
            hashlib.sha256(target.read_bytes()).hexdigest(),
            "none",
        )
    )
    return _fingerprint({"unit_scope": material})


def apply_immutable_control_bootstrap_plan(
    plan: BootstrapPlan,
    *,
    guard: BootstrapGuard,
    authority: BootstrapAuthorityAdapter,
    runtime: BootstrapRuntimeAdapter,
    authorization_expires_at: datetime | None = None,
    now: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
) -> BootstrapResult:
    """Apply one bootstrap; success deliberately leaves entry admission frozen."""

    if now is not None and clock is not None:
        raise ValueError("bootstrap clock is ambiguous")
    observed_now = (
        (lambda: now)
        if now is not None
        else (clock or (lambda: datetime.now(UTC)))
    )

    def boundary_time() -> datetime:
        value = observed_now()
        if value is None or value.tzinfo is None:
            raise BootstrapBlocked("bootstrap_clock_invalid")
        return value.astimezone(UTC)

    def require_live_authorization() -> None:
        if authorization_expires_at is None:
            return
        if (
            authorization_expires_at.tzinfo is None
            or authorization_expires_at.utcoffset() is None
        ):
            raise BootstrapBlocked("bootstrap_authorization_invalid")
        if boundary_time() >= authorization_expires_at.astimezone(UTC):
            raise BootstrapBlocked("bootstrap_authorization_expired")

    try:
        require_fresh_deepcoin_maintenance_observed_at(
            plan.evidence_observed_at,
            now=boundary_time(),
        )
    except DeepcoinMaintenanceEvidenceRefused as exc:
        raise BootstrapBlocked("bootstrap_evidence_unavailable") from exc
    legacy = runtime.legacy_identities()
    legacy_worker = legacy.get("worker")
    if not isinstance(legacy_worker, Mapping):
        raise BootstrapBlocked("legacy_worker_identity_unavailable")
    legacy_tuple = _process_tuple(legacy_worker)
    # Planning may include bounded database and exchange reads.  Recheck the
    # signed deadline at the last read-only boundary before any inhibitor,
    # authority, file, or systemd mutation.
    require_live_authorization()
    receipt = guard.enter(action_id=plan.action_id)
    guard.prove_quiescent()
    preimages = runtime.capture_dropin_preimages()
    lease = None
    released = False
    candidate_boundary_open = False
    try:
        lease = authority.acquire_bootstrap(plan)
        token = str(getattr(lease, "token", ""))
        generation = int(getattr(lease, "generation", -1))
        if not token or generation != plan.expected_generation + 1:
            raise BootstrapBlocked("bootstrap_authority_unavailable")
        runtime.publish_candidate_configuration(plan, entry_frozen=True)
        runtime.verify_candidate_configuration(plan)
        try:
            require_fresh_deepcoin_maintenance_observed_at(
                plan.evidence_observed_at,
                now=boundary_time(),
            )
        except DeepcoinMaintenanceEvidenceRefused as exc:
            raise BootstrapBlocked("bootstrap_evidence_unavailable") from exc
        require_live_authorization()

        # Revised, explicit boundary: static files are proven first.  The
        # official units are then opened only while the bootstrap lease is held
        # and the persistent candidate drop-ins keep entry admission frozen.
        candidate_boundary_open = True
        runtime.open_candidate_start_boundary(plan)
        identities = runtime.candidate_identities()
        _validate_candidate_identities(
            identities,
            plan=plan,
            legacy_worker_tuple=legacy_tuple,
            checked_at=boundary_time(),
        )
        # The diagnostic process is the fourth runtime role.  Prove it while
        # bootstrap authority is still held; a post-release monitor check would
        # leave a role gap at the handoff boundary.
        runtime.verify_monitor(plan)
        if not authority.release_bootstrap(
            token=token,
            generation=generation,
            released_at=boundary_time(),
        ):
            raise BootstrapUnknown("bootstrap_authority_release_unknown")
        released = True
        try:
            self_test_generation = authority.no_exchange_write_round_trip(
                plan,
                expected_generation=generation,
            )
        except Exception as exc:
            raise BootstrapUnknown("authority_self_test_unknown") from exc
        if self_test_generation != generation + 1:
            raise BootstrapUnknown("authority_self_test_unknown")
        final_identities = runtime.complete_candidate_start(plan)
        _validate_candidate_identities(
            final_identities,
            plan=plan,
            legacy_worker_tuple=legacy_tuple,
            checked_at=boundary_time(),
        )
        if any(
            _process_tuple(final_identities[role])
            != _process_tuple(identities[role])
            for role in BOOTSTRAP_LIVE_RUNTIME_ROLES
        ):
            raise BootstrapUnknown("candidate_takeover_process_changed")
        guard.complete_candidate_takeover(
            expected_fingerprint=receipt.fingerprint,
        )
        return BootstrapResult(
            status="bootstrapped_entry_frozen",
            commit=plan.candidate.commit,
            generation=self_test_generation,
            entry_admission_frozen=True,
        )
    except BootstrapUnknown as exc:
        compensation_failed = False
        if candidate_boundary_open:
            try:
                runtime.reinhibit_and_stop_candidate()
            except Exception:
                compensation_failed = True
        if lease is not None and not released:
            try:
                authority.block_bootstrap(
                    token=str(lease.token),
                    generation=int(lease.generation),
                    reason_code=_reason_code(str(exc)),
                    blocked_at=boundary_time(),
                )
            except Exception:
                compensation_failed = True
        reason = (
            "bootstrap_compensation_failed"
            if compensation_failed
            else _reason_code(str(exc))
        )
        try:
            guard.block(reason_code=reason)
        except Exception as block_exc:
            raise BootstrapBlocked("bootstrap_compensation_failed") from block_exc
        if compensation_failed:
            raise BootstrapBlocked("bootstrap_compensation_failed") from exc
        raise BootstrapBlocked(str(exc)) from exc
    except Exception as exc:
        try:
            if candidate_boundary_open:
                runtime.reinhibit_and_stop_candidate()
            runtime.restore_control_configuration(preimages)
            if lease is not None and not released:
                if not authority.release_bootstrap(
                    token=str(lease.token),
                    generation=int(lease.generation),
                    released_at=boundary_time(),
                ):
                    raise BootstrapUnknown("bootstrap_authority_release_unknown")
            safe = guard.mark_safe_to_restore(
                expected_fingerprint=receipt.fingerprint,
            )
            guard.restore(expected_fingerprint=safe.fingerprint)
        except Exception as rollback_exc:
            if lease is not None and not released:
                try:
                    authority.block_bootstrap(
                        token=str(lease.token),
                        generation=int(lease.generation),
                        reason_code="bootstrap_rollback_unknown",
                        blocked_at=boundary_time(),
                    )
                except Exception:
                    pass
            guard.block(reason_code="bootstrap_rollback_unknown")
            raise BootstrapBlocked("bootstrap_rollback_unknown") from rollback_exc
        if isinstance(exc, BootstrapBlocked):
            raise BootstrapBlocked(str(exc)) from exc
        raise BootstrapBlocked("bootstrap_rolled_back") from exc


def _validate_candidate_identities(
    identities: Mapping[str, Mapping[str, Any]],
    *,
    plan: BootstrapPlan,
    legacy_worker_tuple: tuple[int, int],
    checked_at: datetime,
) -> None:
    if tuple(identities) != BOOTSTRAP_LIVE_RUNTIME_ROLES:
        raise BootstrapBlocked("candidate_identity_scope_invalid")
    process_tuples: set[tuple[int, int]] = set()
    for role in BOOTSTRAP_LIVE_RUNTIME_ROLES:
        identity = identities.get(role)
        if not isinstance(identity, Mapping):
            raise BootstrapBlocked("candidate_identity_unproven")
        if (
            identity.get("runtime_role") != role
            or identity.get("command_role") != role
            or identity.get("loaded_artifact_verified") is not True
            or str(identity.get("loaded_cwd") or "")
            != "/opt/telegram-kol-analyzer"
            or identity.get("release_commit") != plan.candidate.commit
            or identity.get("manifest_sha256")
            != plan.candidate.manifest_sha256
            or identity.get("systemd_main_pid") != identity.get("pid")
            or identity.get("systemd_start_ticks")
            != identity.get("process_start_ticks")
        ):
            raise BootstrapBlocked("candidate_identity_unproven")
        try:
            validate_runtime_identity_health(
                identity,
                role=role,
                checked_at=checked_at,
                require_entry_frozen=True,
            )
        except ValueError as exc:
            raise BootstrapBlocked("candidate_identity_unproven") from exc
        process_tuple = _process_tuple(identity)
        if process_tuple in process_tuples:
            raise BootstrapBlocked("candidate_identity_unproven")
        process_tuples.add(process_tuple)
        if role != "monitor" and identity.get("entry_admission_frozen") is not True:
            raise BootstrapBlocked("candidate_entry_not_frozen")
    if _process_tuple(identities["worker"]) == legacy_worker_tuple:
        raise BootstrapBlocked("candidate_identity_not_distinct")
    try:
        validate_runtime_authority_scope(identities)
    except ValueError as exc:
        raise BootstrapBlocked("candidate_capability_unproven") from exc


def _process_tuple(identity: Mapping[str, Any]) -> tuple[int, int]:
    pid = identity.get("pid")
    ticks = identity.get("process_start_ticks")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 1
        or isinstance(ticks, bool)
        or not isinstance(ticks, int)
        or ticks <= 0
    ):
        raise BootstrapBlocked("candidate_identity_unproven")
    return pid, ticks


def _component_units() -> Mapping[str, tuple[str, ...]]:
    return {
        "web": ("telegram-kol-web.service",),
        "ingest": ("telegram-kol-ingest.service",),
        "worker": ("telegram-kol-worker.service",),
        "monitor": (
            "telegram-kol-monitor.service",
            "telegram-kol-monitor-diagnostic.service",
            "telegram-kol-monitor-test-notification.service",
        ),
    }


def _bootstrap_unit_files() -> tuple[str, ...]:
    return tuple(
        unit for units in _component_units().values() for unit in units
    ) + ("telegram-kol-monitor.timer", BOOTSTRAP_TARGET)


def _run_systemd(command: list[str]) -> None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapBlocked("bootstrap_systemd_verify_failed") from exc
    if completed.returncode != 0:
        raise BootstrapBlocked("bootstrap_systemd_verify_failed")


def _restore_dropin_preimage(preimage: DropinPreimage) -> None:
    path = preimage.path
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    if preimage.content is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    else:
        _atomic_publish_configuration(path, preimage.content)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_publish_configuration(path: Path, content: bytes) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o755)
    temporary = directory / f".{path.name}.bootstrap-publish"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, 0o644)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _reason_code(value: str) -> str:
    clean = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in str(value).lower()
    ).strip("_")
    return (clean or "bootstrap_unknown")[:64]
