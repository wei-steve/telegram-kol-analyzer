from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import re
import sys
from typing import Final, Sequence


class ManifestValidationError(ValueError):
    """Raised when an action manifest is incomplete or internally unsafe."""


class DeploymentAction(str, Enum):
    LOCAL = "local"
    PUSH = "push"
    STAGE = "stage"
    ACTIVATE = "activate"
    TRADING = "trading"


class RiskLevel(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class RuntimeComponent(str, Enum):
    WEB = "web"
    MONITOR = "monitor"
    INGEST = "ingest"
    WORKER = "worker"


class GateDisposition(str, Enum):
    REQUIRED = "required"
    PROHIBITED = "prohibited"


_REQUIRED_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "action",
        "risk_level",
        "components",
        "requires_restart",
        "schema_changed",
        "production_data_mutation",
        "exchange_write_semantics_changed",
        "authority_changed",
    }
)
_OPTIONAL_MANIFEST_FIELDS: Final[frozenset[str]] = frozenset({"rollback_releases"})
_MANIFEST_FIELDS: Final[frozenset[str]] = (
    _REQUIRED_MANIFEST_FIELDS | _OPTIONAL_MANIFEST_FIELDS
)
_SHA1_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
_COMPONENT_ORDER: Final[dict[RuntimeComponent, int]] = {
    component: index for index, component in enumerate(RuntimeComponent)
}
_AUTHORITY_COMPONENTS: Final[frozenset[RuntimeComponent]] = frozenset(
    {RuntimeComponent.INGEST, RuntimeComponent.WORKER}
)
_AUTHORITY_RUNTIME_SCOPE: Final[frozenset[RuntimeComponent]] = frozenset(
    RuntimeComponent
)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class RollbackReleaseTarget:
    component: RuntimeComponent
    commit: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class DeploymentManifest:
    action: DeploymentAction
    risk_level: RiskLevel
    components: tuple[RuntimeComponent, ...]
    requires_restart: bool
    schema_changed: bool
    production_data_mutation: bool
    exchange_write_semantics_changed: bool
    authority_changed: bool
    rollback_releases: tuple[RollbackReleaseTarget, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionGate:
    gate_id: str
    disposition: GateDisposition
    reason: str


@dataclass(frozen=True, slots=True)
class ActionPlan:
    action: DeploymentAction
    risk_level: RiskLevel
    components: tuple[RuntimeComponent, ...]
    gates: tuple[ActionGate, ...]
    rollback_releases: tuple[RollbackReleaseTarget, ...] = ()


def _parse_enum(
    value: object,
    enum_type: type[DeploymentAction] | type[RiskLevel] | type[RuntimeComponent],
    *,
    field_name: str,
) -> DeploymentAction | RiskLevel | RuntimeComponent:
    if not isinstance(value, str):
        raise ManifestValidationError(f"manifest field {field_name} must be a string")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ManifestValidationError(
            f"manifest field {field_name} has an unsupported value"
        ) from exc


def _parse_bool(data: Mapping[str, object], field_name: str) -> bool:
    value = data[field_name]
    if type(value) is not bool:
        raise ManifestValidationError(f"manifest field {field_name} must be boolean")
    return value


def _parse_components(value: object) -> tuple[RuntimeComponent, ...]:
    if not isinstance(value, list):
        raise ManifestValidationError("manifest field components must be a list")

    components: list[RuntimeComponent] = []
    for item in value:
        component = _parse_enum(
            item,
            RuntimeComponent,
            field_name="components",
        )
        assert isinstance(component, RuntimeComponent)
        components.append(component)

    if len(set(components)) != len(components):
        raise ManifestValidationError("manifest field components contains duplicates")

    return tuple(sorted(components, key=_COMPONENT_ORDER.__getitem__))


def _parse_rollback_releases(
    value: object,
    *,
    components: tuple[RuntimeComponent, ...],
) -> tuple[RollbackReleaseTarget, ...]:
    if not isinstance(value, Mapping):
        raise ManifestValidationError(
            "manifest field rollback_releases must be an object"
        )
    if not value:
        raise ManifestValidationError(
            "manifest field rollback_releases must not be empty"
        )
    expected = {component.value for component in components}
    if set(value) != expected:
        raise ManifestValidationError(
            "manifest field rollback_releases must exactly cover components"
        )
    targets: list[RollbackReleaseTarget] = []
    for component in components:
        raw_target = value.get(component.value)
        if not isinstance(raw_target, Mapping) or set(raw_target) != {
            "commit",
            "manifest_sha256",
        }:
            raise ManifestValidationError(
                "manifest rollback release has invalid fields"
            )
        commit = raw_target.get("commit")
        manifest_sha256 = raw_target.get("manifest_sha256")
        if not isinstance(commit, str) or _SHA1_RE.fullmatch(commit) is None:
            raise ManifestValidationError(
                "manifest rollback release has invalid commit"
            )
        if (
            not isinstance(manifest_sha256, str)
            or _SHA256_RE.fullmatch(manifest_sha256) is None
        ):
            raise ManifestValidationError(
                "manifest rollback release has invalid manifest digest"
            )
        targets.append(
            RollbackReleaseTarget(
                component=component,
                commit=commit,
                manifest_sha256=manifest_sha256,
            )
        )
    return tuple(targets)


def _validate_consistency(manifest: DeploymentManifest) -> None:
    high_risk_impact = (
        manifest.schema_changed
        or manifest.production_data_mutation
        or manifest.exchange_write_semantics_changed
    )
    if high_risk_impact and manifest.risk_level is not RiskLevel.L3:
        raise ManifestValidationError("high-risk impact requires risk level L3")

    if manifest.authority_changed and manifest.risk_level not in {
        RiskLevel.L2,
        RiskLevel.L3,
    }:
        raise ManifestValidationError("authority impact requires risk level L2 or L3")

    if manifest.authority_changed and not _AUTHORITY_COMPONENTS.intersection(
        manifest.components
    ):
        raise ManifestValidationError(
            "authority impact requires authority component scope"
        )

    if (
        manifest.rollback_releases
        and manifest.action is not DeploymentAction.ACTIVATE
    ):
        raise ManifestValidationError(
            "rollback releases are allowed only for activate actions"
        )

    if manifest.rollback_releases and {
        target.component for target in manifest.rollback_releases
    } != set(manifest.components):
        raise ManifestValidationError(
            "rollback releases must exactly cover activation components"
        )

    if (
        manifest.action is DeploymentAction.ACTIVATE
        and manifest.authority_changed
        and set(manifest.components) != _AUTHORITY_RUNTIME_SCOPE
    ):
        raise ManifestValidationError(
            "authority impact requires exact runtime scope"
        )

    if manifest.risk_level is RiskLevel.L0 and (
        manifest.components
        or manifest.requires_restart
        or high_risk_impact
        or manifest.authority_changed
    ):
        raise ManifestValidationError("risk level L0 cannot declare runtime impact")

    if manifest.requires_restart and not manifest.components:
        raise ManifestValidationError("restart impact requires component scope")

    if manifest.action is DeploymentAction.ACTIVATE:
        if not manifest.components:
            raise ManifestValidationError("activate action requires component scope")
        if not manifest.requires_restart:
            raise ManifestValidationError("activate action requires explicit restart impact")

    if (
        manifest.action is DeploymentAction.TRADING
        and manifest.risk_level is not RiskLevel.L3
    ):
        raise ManifestValidationError("trading action requires risk level L3")

    if manifest.action is DeploymentAction.TRADING and (
        manifest.components
        or manifest.requires_restart
        or high_risk_impact
        or manifest.authority_changed
    ):
        raise ManifestValidationError(
            "trading action cannot be combined with deployment impact"
        )


def _validate_typed_manifest(manifest: DeploymentManifest) -> None:
    if type(manifest.action) is not DeploymentAction:
        raise ManifestValidationError("manifest model has invalid action type")
    if type(manifest.risk_level) is not RiskLevel:
        raise ManifestValidationError("manifest model has invalid risk type")
    if type(manifest.components) is not tuple or any(
        type(component) is not RuntimeComponent for component in manifest.components
    ):
        raise ManifestValidationError("manifest model has invalid component types")
    if len(set(manifest.components)) != len(manifest.components):
        raise ManifestValidationError("manifest model has duplicate components")
    if tuple(sorted(manifest.components, key=_COMPONENT_ORDER.__getitem__)) != (
        manifest.components
    ):
        raise ManifestValidationError("manifest model has non-canonical component order")
    if type(manifest.rollback_releases) is not tuple or any(
        type(target) is not RollbackReleaseTarget
        for target in manifest.rollback_releases
    ):
        raise ManifestValidationError("manifest model has invalid rollback releases")
    if tuple(
        sorted(
            manifest.rollback_releases,
            key=lambda target: _COMPONENT_ORDER[target.component],
        )
    ) != manifest.rollback_releases:
        raise ManifestValidationError(
            "manifest model has non-canonical rollback release order"
        )
    if len({target.component for target in manifest.rollback_releases}) != len(
        manifest.rollback_releases
    ):
        raise ManifestValidationError(
            "manifest model has duplicate rollback release components"
        )
    for target in manifest.rollback_releases:
        if (
            type(target.component) is not RuntimeComponent
            or type(target.commit) is not str
            or _SHA1_RE.fullmatch(target.commit) is None
            or type(target.manifest_sha256) is not str
            or _SHA256_RE.fullmatch(target.manifest_sha256) is None
        ):
            raise ManifestValidationError("manifest model has invalid rollback target")

    boolean_fields = (
        manifest.requires_restart,
        manifest.schema_changed,
        manifest.production_data_mutation,
        manifest.exchange_write_semantics_changed,
        manifest.authority_changed,
    )
    if any(type(value) is not bool for value in boolean_fields):
        raise ManifestValidationError("manifest model has invalid boolean types")

    _validate_consistency(manifest)


def parse_manifest(data: Mapping[str, object]) -> DeploymentManifest:
    """Parse a complete action manifest without inferring omitted safety facts."""

    supplied_fields = frozenset(data)
    if not supplied_fields.issubset(_MANIFEST_FIELDS):
        raise ManifestValidationError("manifest contains unknown fields")

    missing_fields = _REQUIRED_MANIFEST_FIELDS - supplied_fields
    if missing_fields:
        missing = ", ".join(sorted(missing_fields))
        raise ManifestValidationError(f"manifest is missing required fields: {missing}")

    action = _parse_enum(data["action"], DeploymentAction, field_name="action")
    risk_level = _parse_enum(
        data["risk_level"],
        RiskLevel,
        field_name="risk_level",
    )
    assert isinstance(action, DeploymentAction)
    assert isinstance(risk_level, RiskLevel)

    components = _parse_components(data["components"])
    rollback_releases = (
        _parse_rollback_releases(
            data["rollback_releases"],
            components=components,
        )
        if "rollback_releases" in data
        else ()
    )
    manifest = DeploymentManifest(
        action=action,
        risk_level=risk_level,
        components=components,
        requires_restart=_parse_bool(data, "requires_restart"),
        schema_changed=_parse_bool(data, "schema_changed"),
        production_data_mutation=_parse_bool(data, "production_data_mutation"),
        exchange_write_semantics_changed=_parse_bool(
            data,
            "exchange_write_semantics_changed",
        ),
        authority_changed=_parse_bool(data, "authority_changed"),
        rollback_releases=rollback_releases,
    )
    _validate_consistency(manifest)
    return manifest


def _required(gate_id: str, reason: str) -> ActionGate:
    return ActionGate(gate_id, GateDisposition.REQUIRED, reason)


def _prohibited(gate_id: str, reason: str) -> ActionGate:
    return ActionGate(gate_id, GateDisposition.PROHIBITED, reason)


_NO_PRODUCTION_MUTATION: Final[tuple[ActionGate, ...]] = (
    _prohibited("exchange.write", "This action cannot write to the exchange."),
    _prohibited(
        "production.database_write",
        "This action cannot mutate the production database.",
    ),
    _prohibited(
        "production.settings_write",
        "This action cannot mutate production settings.",
    ),
    _prohibited(
        "runtime.service_control",
        "This action cannot stop, start, or restart runtime services.",
    ),
    _prohibited("telegram.send", "This action cannot send Telegram messages."),
)


def _local_gates() -> tuple[ActionGate, ...]:
    return (
        _required(
            "tests.risk_scoped",
            "Local verification must cover the declared change risk.",
        ),
        _required(
            "workspace.identity",
            "Local work must run in the intended repository and branch.",
        ),
        _prohibited("production.ssh", "Local work cannot require production SSH."),
        *_NO_PRODUCTION_MUTATION,
    )


def _push_gates() -> tuple[ActionGate, ...]:
    return (
        _required("git.clean_tree", "The pushed candidate must have no hidden edits."),
        _required(
            "git.exact_commit",
            "The reviewed commit identity must be the pushed identity.",
        ),
        _required(
            "git.remote_fast_forward",
            "Push must preserve the reviewed branch history.",
        ),
        _required(
            "git.reviewed_diff",
            "Only the reviewed explicit paths may be included.",
        ),
        *_NO_PRODUCTION_MUTATION,
    )


def _stage_gates() -> tuple[ActionGate, ...]:
    return (
        _required(
            "candidate.exact_commit",
            "The candidate artifact must resolve to the declared commit.",
        ),
        _required(
            "candidate.immutable_artifact",
            "Staging must create content-addressed immutable candidate files.",
        ),
        _required(
            "candidate.inactive_destination",
            "The candidate must be materialized outside the active runtime path.",
        ),
        _required(
            "candidate.receipt",
            "Staging must emit a non-secret receipt for later activation.",
        ),
        _prohibited(
            "runtime.active_checkout_mutation",
            "Staging cannot mutate the checkout used by running processes.",
        ),
        *_NO_PRODUCTION_MUTATION,
    )


def _activate_gates(manifest: DeploymentManifest) -> tuple[ActionGate, ...]:
    gates = [
        _required(
            "authorization.activate_explicit",
            "Runtime activation requires authorization separate from staging.",
        ),
        _required(
            "candidate.receipt_verified",
            "Activation must consume a verified immutable stage receipt.",
        ),
        _required(
            "rollback.runtime_ready",
            "The last verified runtime release must remain selectable.",
        ),
        _required(
            "runtime.affected_services_only",
            "Only explicitly declared services may be controlled.",
        ),
        _required(
            "runtime.identity_exact_artifact",
            "Process identity must prove the loaded artifact, not checkout HEAD alone.",
        ),
        _required(
            "runtime.scoped_health",
            "Success requires health evidence for each affected service.",
        ),
        _prohibited("exchange.write", "Activation cannot perform exchange actions."),
        _prohibited(
            "message.frozen_recovery",
            "Activation cannot recover messages received during a freeze.",
        ),
        _prohibited(
            "message.historical_replay",
            "Activation cannot replay historical messages.",
        ),
        _prohibited("order.bulk_action", "Activation cannot execute bulk order actions."),
        _prohibited(
            "production.settings_write",
            "Activation cannot silently change runtime authority settings.",
        ),
        _prohibited("telegram.send", "Activation cannot send Telegram messages."),
        _prohibited("trading.enable", "Activation cannot enable trading."),
    ]

    if _AUTHORITY_COMPONENTS.intersection(manifest.components):
        gates.extend(
            (
                _required(
                    "runtime.active_exchange_writes_zero",
                    "Authority cutover requires zero in-flight exchange submissions.",
                ),
                _required(
                    "runtime.authority_single_owner",
                    "Exactly one proven process may own active runtime authority.",
                ),
                _required(
                    "runtime.authority_unknown_absent",
                    "Unknown global authority state blocks activation.",
                ),
                _required(
                    "trading.protection_authority_proven",
                    "Protection authority must be observed directly before cutover.",
                ),
            )
        )

    if manifest.schema_changed or manifest.production_data_mutation:
        gates.extend(
            (
                _required(
                    "database.before_after_counts",
                    "L3 database scope requires bounded before and after counts.",
                ),
                _required(
                    "database.quick_check",
                    "L3 database scope requires integrity verification.",
                ),
                _required(
                    "database.rollback_ready",
                    "L3 database scope requires a tested rollback boundary.",
                ),
                _required(
                    "database.scoped_backup",
                    "L3 database scope requires a recoverable scoped backup.",
                ),
            )
        )
    else:
        gates.append(
            _prohibited(
                "production.database_write",
                "Non-L3 activation cannot mutate the production database.",
            )
        )

    return tuple(gates)


def _trading_gates() -> tuple[ActionGate, ...]:
    return (
        _required(
            "authorization.trading_explicit",
            "Each trading action requires separate explicit authorization.",
        ),
        _required(
            "confirmation.fresh",
            "The confirmation must be created from the latest single-target plan.",
        ),
        _required(
            "confirmation.single_use",
            "A confirmation token may authorize exactly one attempt.",
        ),
        _required(
            "evidence.exchange_fresh",
            "Exchange evidence must be refreshed immediately before the action.",
        ),
        _required(
            "evidence.runtime_fresh",
            "Runtime evidence must be refreshed immediately before the action.",
        ),
        _required(
            "runtime.authority_single_owner",
            "Exactly one proven process may own active runtime authority.",
        ),
        _required(
            "runtime.identity_exact_artifact",
            "The acting process must prove its loaded immutable artifact.",
        ),
        _required(
            "target.canonical",
            "The target must come from the repository canonical target set.",
        ),
        _required("target.one_only", "Each plan may contain exactly one target."),
        _required(
            "target.unknown_absent",
            "Any target-related unknown blocks the action permanently.",
        ),
        _required(
            "terminalization.complete",
            "Confirmed exchange completion must terminalize all local records and events.",
        ),
        _prohibited(
            "message.frozen_recovery",
            "Trading actions cannot recover messages received during a freeze.",
        ),
        _prohibited(
            "message.historical_replay",
            "Trading actions cannot replay historical messages.",
        ),
        _prohibited("order.bulk_action", "Bulk order actions are not authorized."),
        _prohibited(
            "unknown.automatic_retry",
            "An unknown exchange outcome must never be retried automatically.",
        ),
    )


def build_action_plan(manifest: DeploymentManifest) -> ActionPlan:
    """Return only the gates applicable to one declared action."""

    _validate_typed_manifest(manifest)
    gate_builders = {
        DeploymentAction.LOCAL: lambda: _local_gates(),
        DeploymentAction.PUSH: lambda: _push_gates(),
        DeploymentAction.STAGE: lambda: _stage_gates(),
        DeploymentAction.ACTIVATE: lambda: _activate_gates(manifest),
        DeploymentAction.TRADING: lambda: _trading_gates(),
    }
    gates = gate_builders[manifest.action]()
    unique_gates = {gate.gate_id: gate for gate in gates}
    if len(unique_gates) != len(gates):
        raise RuntimeError("action plan contains duplicate gate identifiers")

    return ActionPlan(
        action=manifest.action,
        risk_level=manifest.risk_level,
        components=manifest.components,
        gates=tuple(sorted(gates, key=lambda gate: gate.gate_id)),
        rollback_releases=manifest.rollback_releases,
    )


def _plan_payload(plan: ActionPlan) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "action": plan.action.value,
        "risk_level": plan.risk_level.value,
        "components": [component.value for component in plan.components],
        "gates": [
            {
                "id": gate.gate_id,
                "disposition": gate.disposition.value,
                "reason": gate.reason,
            }
            for gate in plan.gates
        ],
    }
    if plan.rollback_releases:
        payload["rollback_releases"] = {
            target.component.value: {
                "commit": target.commit,
                "manifest_sha256": target.manifest_sha256,
            }
            for target in plan.rollback_releases
        }
    return payload


def _render_json(plan: ActionPlan) -> str:
    return json.dumps(
        _plan_payload(plan),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _render_text(plan: ActionPlan) -> str:
    components = ",".join(component.value for component in plan.components) or "-"
    lines = [
        f"action={plan.action.value}",
        f"risk_level={plan.risk_level.value}",
        f"components={components}",
    ]
    lines.extend(
        f"{gate.disposition.value} {gate.gate_id} {gate.reason}"
        for gate in plan.gates
    )
    return "\n".join(lines)


def _write_error(error: str, message: str | None = None) -> None:
    payload = {"error": error}
    if message:
        payload["message"] = message
    sys.stderr.write(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate one deployment action manifest and print its gates.",
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)

    try:
        raw_manifest = json.loads(
            args.manifest.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except OSError:
        _write_error("manifest_unavailable")
        return 2
    except (json.JSONDecodeError, ValueError):
        _write_error("invalid_json")
        return 2

    if not isinstance(raw_manifest, dict):
        _write_error("invalid_manifest", "manifest root must be an object")
        return 2

    try:
        manifest = parse_manifest(raw_manifest)
    except ManifestValidationError as exc:
        _write_error("invalid_manifest", str(exc))
        return 2

    plan = build_action_plan(manifest)
    rendered = _render_json(plan) if args.format == "json" else _render_text(plan)
    sys.stdout.write(rendered + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
