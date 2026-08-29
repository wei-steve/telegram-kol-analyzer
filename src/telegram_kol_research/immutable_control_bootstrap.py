"""One-time, entry-frozen bootstrap of the first immutable control release."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Iterable, Mapping, Protocol

from telegram_kol_research.deepcoin_maintenance_evidence import (
    DeepcoinMaintenanceEvidence,
    DeepcoinMaintenanceEvidenceRefused,
    require_fresh_deepcoin_maintenance_evidence,
)
from telegram_kol_research.reviewed_pending_entry_cancel import (
    REVIEWED_PENDING_ENTRY_TARGETS,
)
from telegram_kol_research.scoped_release_activation import ReleaseEvidence


BOOTSTRAP_COMPONENTS = ("web", "ingest", "worker", "monitor")
REJECTED_LEGACY_BRIDGE_RELEASE = (
    "ffb06d19eabfd32dfdab2942b2152fd2809e3d17"
)
_REQUIRED_CAPABILITIES = (
    "global_exchange_authority",
    "management",
    "protection",
    "close",
    "tpsl",
    "rescue",
)
_REQUIRED_CYCLES = (
    "management_cycle",
    "protection_cycle",
    "close_cycle",
    "tpsl_cycle",
    "rescue_cycle",
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
    def no_exchange_write_authority_round_trip(
        self,
        *,
        expected_generation: int,
    ) -> int: ...
    def verify_monitor(self, plan: BootstrapPlan) -> None: ...
    def reinhibit_and_stop_candidate(self) -> None: ...
    def restore_control_configuration(self, preimages: Any) -> None: ...


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
    components = tuple(candidate.action_manifest.get("components", ()))
    if components != BOOTSTRAP_COMPONENTS:
        raise BootstrapBlocked("bootstrap_scope_invalid")
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


def apply_immutable_control_bootstrap_plan(
    plan: BootstrapPlan,
    *,
    guard: BootstrapGuard,
    authority: BootstrapAuthorityAdapter,
    runtime: BootstrapRuntimeAdapter,
    now: datetime | None = None,
) -> BootstrapResult:
    """Apply one bootstrap; success deliberately leaves entry admission frozen."""

    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    legacy = runtime.legacy_identities()
    legacy_worker = legacy.get("worker")
    if not isinstance(legacy_worker, Mapping):
        raise BootstrapBlocked("legacy_worker_identity_unavailable")
    legacy_tuple = _process_tuple(legacy_worker)
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
        )
        if not authority.release_bootstrap(
            token=token,
            generation=generation,
            released_at=observed_at,
        ):
            raise BootstrapUnknown("bootstrap_authority_release_unknown")
        released = True
        self_test_generation = runtime.no_exchange_write_authority_round_trip(
            expected_generation=generation,
        )
        if self_test_generation != generation + 1:
            raise BootstrapUnknown("authority_self_test_unknown")
        runtime.verify_monitor(plan)
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
        if candidate_boundary_open:
            try:
                runtime.reinhibit_and_stop_candidate()
            except Exception:
                pass
        if lease is not None and not released:
            try:
                authority.block_bootstrap(
                    token=str(lease.token),
                    generation=int(lease.generation),
                    reason_code=_reason_code(str(exc)),
                    blocked_at=observed_at,
                )
            except Exception:
                pass
        guard.block(reason_code=_reason_code(str(exc)))
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
                    released_at=observed_at,
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
                        blocked_at=observed_at,
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
) -> None:
    if tuple(identities) != BOOTSTRAP_COMPONENTS:
        raise BootstrapBlocked("candidate_identity_scope_invalid")
    for role in BOOTSTRAP_COMPONENTS:
        identity = identities.get(role)
        if not isinstance(identity, Mapping):
            raise BootstrapBlocked("candidate_identity_unproven")
        if (
            identity.get("runtime_role") != role
            or identity.get("release_commit") != plan.candidate.commit
            or identity.get("manifest_sha256")
            != plan.candidate.manifest_sha256
            or identity.get("systemd_main_pid") != identity.get("pid")
            or identity.get("systemd_start_ticks")
            != identity.get("process_start_ticks")
        ):
            raise BootstrapBlocked("candidate_identity_unproven")
        _process_tuple(identity)
        if role != "monitor" and identity.get("entry_admission_frozen") is not True:
            raise BootstrapBlocked("candidate_entry_not_frozen")
    if _process_tuple(identities["worker"]) == legacy_worker_tuple:
        raise BootstrapBlocked("candidate_identity_not_distinct")
    worker = identities["worker"]
    capabilities = worker.get("capabilities")
    if not isinstance(capabilities, Mapping) or any(
        capabilities.get(name) is not True for name in _REQUIRED_CAPABILITIES
    ):
        raise BootstrapBlocked("candidate_capability_unproven")
    evidence = worker.get("authority_evidence")
    if not isinstance(evidence, Mapping):
        raise BootstrapBlocked("candidate_capability_unproven")
    for name in _REQUIRED_CYCLES:
        cycle = evidence.get(name)
        if (
            not isinstance(cycle, Mapping)
            or cycle.get("fresh") is not True
            or cycle.get("successful") is not True
        ):
            raise BootstrapBlocked("candidate_capability_unproven")


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
