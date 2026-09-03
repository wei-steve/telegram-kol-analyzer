from __future__ import annotations

import json
from pathlib import Path

import pytest

from telegram_kol_research.deployment_action_plan import (
    ActionPlan,
    DeploymentAction,
    DeploymentManifest,
    GateDisposition,
    ManifestValidationError,
    RiskLevel,
    RuntimeComponent,
    build_action_plan,
    main,
    parse_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def _manifest(**overrides: object) -> dict[str, object]:
    manifest: dict[str, object] = {
        "action": "local",
        "risk_level": "L0",
        "components": [],
        "requires_restart": False,
        "schema_changed": False,
        "production_data_mutation": False,
        "exchange_write_semantics_changed": False,
        "authority_changed": False,
    }
    manifest.update(overrides)
    return manifest


def test_closed_action_risk_and_component_names() -> None:
    assert {item.value for item in DeploymentAction} == {
        "local",
        "push",
        "stage",
        "activate",
        "trading",
    }
    assert {item.value for item in RiskLevel} == {"L0", "L1", "L2", "L3"}
    assert {item.value for item in RuntimeComponent} == {
        "web",
        "monitor",
        "ingest",
        "worker",
    }


def test_parse_manifest_builds_immutable_closed_model() -> None:
    manifest = parse_manifest(
        _manifest(
            action="stage",
            risk_level="L3",
            components=["web", "monitor", "worker", "ingest"],
            requires_restart=True,
            schema_changed=True,
            authority_changed=True,
        )
    )

    assert manifest.action is DeploymentAction.STAGE
    assert manifest.risk_level is RiskLevel.L3
    assert manifest.components == (
        RuntimeComponent.WEB,
        RuntimeComponent.MONITOR,
        RuntimeComponent.INGEST,
        RuntimeComponent.WORKER,
    )
    assert manifest.requires_restart is True
    with pytest.raises(AttributeError):
        manifest.action = DeploymentAction.LOCAL  # type: ignore[misc]


def test_activation_manifest_accepts_exact_per_component_rollback_releases() -> None:
    rollback_releases = {
        component: {"commit": str(index) * 40, "manifest_sha256": str(index) * 64}
        for index, component in enumerate(
            ("web", "monitor", "ingest", "worker"),
            start=1,
        )
    }

    manifest = parse_manifest(
        _manifest(
            action="activate",
            risk_level="L2",
            components=["web", "monitor", "ingest", "worker"],
            requires_restart=True,
            authority_changed=True,
            rollback_releases=rollback_releases,
        )
    )

    assert {
        target.component.value: {
            "commit": target.commit,
            "manifest_sha256": target.manifest_sha256,
        }
        for target in manifest.rollback_releases
    } == rollback_releases


@pytest.mark.parametrize(
    "action,components,rollback_releases",
    (
        (
            "stage",
            ["web"],
            {"web": {"commit": "1" * 40, "manifest_sha256": "a" * 64}},
        ),
        (
            "activate",
            ["web", "monitor"],
            {"web": {"commit": "1" * 40, "manifest_sha256": "a" * 64}},
        ),
        (
            "activate",
            ["web"],
            {
                "web": {"commit": "1" * 40, "manifest_sha256": "a" * 64},
                "monitor": {"commit": "2" * 40, "manifest_sha256": "b" * 64},
            },
        ),
        (
            "activate",
            ["web"],
            {"web": {"commit": "A" * 40, "manifest_sha256": "a" * 64}},
        ),
        (
            "activate",
            ["web"],
            {"web": {"commit": "1" * 40, "manifest_sha256": "a" * 63}},
        ),
        (
            "activate",
            ["web"],
            {
                "web": {
                    "commit": "1" * 40,
                    "manifest_sha256": "a" * 64,
                    "extra": "rejected",
                }
            },
        ),
    ),
    ids=(
        "activation-only",
        "missing-component",
        "extra-component",
        "uppercase-commit",
        "short-manifest-digest",
        "unknown-target-field",
    ),
)
def test_rollback_release_manifest_contract_is_closed(
    action: str,
    components: list[str],
    rollback_releases: object,
) -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(
            _manifest(
                action=action,
                risk_level="L2",
                components=components,
                requires_restart=True,
                rollback_releases=rollback_releases,
            )
        )


def test_manifest_json_rejects_duplicate_rollback_component_keys(
    tmp_path: Path,
    capsys,
) -> None:
    manifest = tmp_path / "duplicate.json"
    manifest.write_text(
        '{"action":"activate","risk_level":"L1","components":["web"],'
        '"requires_restart":true,"schema_changed":false,'
        '"production_data_mutation":false,'
        '"exchange_write_semantics_changed":false,"authority_changed":false,'
        '"rollback_releases":{"web":{"commit":"' + "1" * 40
        + '","manifest_sha256":"' + "a" * 64
        + '"},"web":{"commit":"' + "2" * 40
        + '","manifest_sha256":"' + "b" * 64 + '"}}}',
        encoding="utf-8",
    )

    assert main(["--manifest", str(manifest), "--format", "json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"] == "invalid_json"


@pytest.mark.parametrize(
    "components",
    (
        ["worker"],
        ["web", "ingest", "worker"],
        ["monitor", "ingest", "worker"],
    ),
)
def test_authority_change_requires_exact_four_component_scope(components) -> None:
    with pytest.raises(ManifestValidationError, match="exact runtime scope"):
        parse_manifest(
            _manifest(
                action="activate",
                risk_level="L2",
                components=components,
                requires_restart=True,
                authority_changed=True,
            )
        )
@pytest.mark.parametrize(
    "manifest",
    (
        _manifest(secret_override="must-not-be-echoed"),
        {key: value for key, value in _manifest().items() if key != "risk_level"},
        _manifest(action="invented"),
        _manifest(risk_level="L9"),
        _manifest(components=["database"]),
        _manifest(components=["worker", "worker"]),
        _manifest(requires_restart=1),
    ),
)
def test_parse_manifest_rejects_unknown_incomplete_or_invalid_input(
    manifest: dict[str, object],
) -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(manifest)


def test_error_does_not_echo_unknown_field_or_value() -> None:
    with pytest.raises(ManifestValidationError) as exc_info:
        parse_manifest(_manifest(api_secret="sensitive-value"))

    message = str(exc_info.value)
    assert "api_secret" not in message
    assert "sensitive-value" not in message


@pytest.mark.parametrize(
    "manifest",
    (
        _manifest(action="activate", risk_level="L1"),
        _manifest(
            action="activate",
            risk_level="L1",
            components=["web"],
            requires_restart=False,
        ),
        _manifest(risk_level="L0", components=["web"]),
        _manifest(risk_level="L0", requires_restart=True),
        _manifest(risk_level="L1", authority_changed=True),
        _manifest(risk_level="L2", schema_changed=True),
        _manifest(risk_level="L2", production_data_mutation=True),
        _manifest(risk_level="L2", exchange_write_semantics_changed=True),
        _manifest(action="trading", risk_level="L2"),
    ),
)
def test_parse_manifest_rejects_inconsistent_or_underclassified_impact(
    manifest: dict[str, object],
) -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(manifest)


def test_l3_accepts_every_explicit_high_risk_impact() -> None:
    manifest = parse_manifest(
        _manifest(
            action="activate",
            risk_level="L3",
            components=["web", "monitor", "ingest", "worker"],
            requires_restart=True,
            schema_changed=True,
            production_data_mutation=True,
            exchange_write_semantics_changed=True,
            authority_changed=True,
        )
    )

    assert manifest.risk_level is RiskLevel.L3


def _gate_ids(
    plan: ActionPlan,
    disposition: GateDisposition,
) -> set[str]:
    return {
        gate.gate_id
        for gate in plan.gates
        if gate.disposition is disposition
    }


def test_local_plan_has_no_production_runtime_database_or_exchange_gate() -> None:
    plan = build_action_plan(parse_manifest(_manifest()))

    assert _gate_ids(plan, GateDisposition.REQUIRED) == {
        "tests.risk_scoped",
        "workspace.identity",
    }
    assert {
        "exchange.write",
        "production.database_write",
        "production.settings_write",
        "production.ssh",
        "runtime.service_control",
        "telegram.send",
    }.issubset(_gate_ids(plan, GateDisposition.PROHIBITED))


def test_push_plan_is_git_only_and_does_not_advance_to_stage() -> None:
    plan = build_action_plan(
        parse_manifest(_manifest(action="push", risk_level="L0"))
    )

    assert _gate_ids(plan, GateDisposition.REQUIRED) == {
        "git.clean_tree",
        "git.exact_commit",
        "git.remote_fast_forward",
        "git.reviewed_diff",
    }
    assert "candidate.immutable_artifact" not in _gate_ids(
        plan,
        GateDisposition.REQUIRED,
    )
    assert "runtime.service_control" in _gate_ids(
        plan,
        GateDisposition.PROHIBITED,
    )


def test_stage_l3_worker_plan_needs_no_live_runtime_or_database_evidence() -> None:
    plan = build_action_plan(
        parse_manifest(
            _manifest(
                action="stage",
                risk_level="L3",
                components=["worker"],
                requires_restart=True,
                schema_changed=True,
                authority_changed=True,
            )
        )
    )

    required = _gate_ids(plan, GateDisposition.REQUIRED)
    assert required == {
        "candidate.exact_commit",
        "candidate.immutable_artifact",
        "candidate.inactive_destination",
        "candidate.receipt",
    }
    assert "runtime.identity_exact_artifact" not in required
    assert "runtime.active_exchange_writes_zero" not in required
    assert "database.scoped_backup" not in required
    assert {
        "exchange.write",
        "production.database_write",
        "production.settings_write",
        "runtime.active_checkout_mutation",
        "runtime.service_control",
        "telegram.send",
    }.issubset(_gate_ids(plan, GateDisposition.PROHIBITED))


@pytest.mark.parametrize("component", ("web", "monitor"))
def test_observer_activation_excludes_trading_authority_gates(component: str) -> None:
    plan = build_action_plan(
        parse_manifest(
            _manifest(
                action="activate",
                risk_level="L1",
                components=[component],
                requires_restart=True,
            )
        )
    )

    required = _gate_ids(plan, GateDisposition.REQUIRED)
    assert {
        "authorization.activate_explicit",
        "candidate.receipt_verified",
        "rollback.runtime_ready",
        "runtime.affected_services_only",
        "runtime.identity_exact_artifact",
        "runtime.scoped_health",
    }.issubset(required)
    assert "runtime.active_exchange_writes_zero" not in required
    assert "runtime.authority_single_owner" not in required
    assert "trading.protection_authority_proven" not in required


def test_authority_activation_keeps_write_and_protection_gates() -> None:
    plan = build_action_plan(
        parse_manifest(
            _manifest(
                action="activate",
                risk_level="L2",
                components=["web", "monitor", "ingest", "worker"],
                requires_restart=True,
                authority_changed=True,
            )
        )
    )

    required = _gate_ids(plan, GateDisposition.REQUIRED)
    assert {
        "runtime.active_exchange_writes_zero",
        "runtime.authority_single_owner",
        "runtime.authority_unknown_absent",
        "trading.protection_authority_proven",
    }.issubset(required)


def test_l3_activation_adds_scoped_database_gates() -> None:
    plan = build_action_plan(
        parse_manifest(
            _manifest(
                action="activate",
                risk_level="L3",
                components=["worker"],
                requires_restart=True,
                schema_changed=True,
                production_data_mutation=True,
            )
        )
    )

    assert {
        "database.before_after_counts",
        "database.quick_check",
        "database.rollback_ready",
        "database.scoped_backup",
    }.issubset(_gate_ids(plan, GateDisposition.REQUIRED))


def test_exchange_semantics_only_activation_does_not_inherit_database_gates() -> None:
    plan = build_action_plan(
        parse_manifest(
            _manifest(
                action="activate",
                risk_level="L3",
                components=["worker"],
                requires_restart=True,
                exchange_write_semantics_changed=True,
            )
        )
    )

    required = _gate_ids(plan, GateDisposition.REQUIRED)
    assert "database.before_after_counts" not in required
    assert "database.quick_check" not in required
    assert "database.rollback_ready" not in required
    assert "database.scoped_backup" not in required
    assert "production.database_write" in _gate_ids(
        plan,
        GateDisposition.PROHIBITED,
    )


def test_authority_change_requires_authority_component_scope() -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(
            _manifest(
                action="stage",
                risk_level="L2",
                components=["web"],
                requires_restart=True,
                authority_changed=True,
            )
        )


def test_direct_model_construction_cannot_bypass_validation() -> None:
    underclassified = DeploymentManifest(
        action=DeploymentAction.STAGE,
        risk_level=RiskLevel.L2,
        components=(RuntimeComponent.WORKER,),
        requires_restart=True,
        schema_changed=True,
        production_data_mutation=False,
        exchange_write_semantics_changed=False,
        authority_changed=True,
    )
    mistyped = DeploymentManifest(
        action="stage",  # type: ignore[arg-type]
        risk_level=RiskLevel.L3,
        components=(RuntimeComponent.WORKER,),
        requires_restart=True,
        schema_changed=True,
        production_data_mutation=False,
        exchange_write_semantics_changed=False,
        authority_changed=True,
    )

    with pytest.raises(ManifestValidationError):
        build_action_plan(underclassified)
    with pytest.raises(ManifestValidationError):
        build_action_plan(mistyped)


def test_trading_action_cannot_be_combined_with_deployment_impact() -> None:
    with pytest.raises(ManifestValidationError):
        parse_manifest(
            _manifest(
                action="trading",
                risk_level="L3",
                components=["worker"],
                requires_restart=True,
            )
        )


def test_trading_plan_is_single_target_fail_closed_and_separately_authorized() -> None:
    plan = build_action_plan(
        parse_manifest(_manifest(action="trading", risk_level="L3"))
    )

    required = _gate_ids(plan, GateDisposition.REQUIRED)
    assert {
        "authorization.trading_explicit",
        "confirmation.fresh",
        "confirmation.single_use",
        "evidence.exchange_fresh",
        "evidence.runtime_fresh",
        "runtime.authority_single_owner",
        "runtime.identity_exact_artifact",
        "target.canonical",
        "target.one_only",
        "target.unknown_absent",
        "terminalization.complete",
    }.issubset(required)
    assert {
        "message.frozen_recovery",
        "message.historical_replay",
        "order.bulk_action",
        "unknown.automatic_retry",
    }.issubset(_gate_ids(plan, GateDisposition.PROHIBITED))


def test_gate_output_is_deterministic_and_has_nonempty_reasons() -> None:
    manifest = parse_manifest(
        _manifest(
            action="activate",
            risk_level="L2",
            components=["web", "monitor", "ingest", "worker"],
            requires_restart=True,
            authority_changed=True,
        )
    )

    first = build_action_plan(manifest)
    second = build_action_plan(manifest)

    assert first == second
    assert [gate.gate_id for gate in first.gates] == sorted(
        gate.gate_id for gate in first.gates
    )
    assert all(gate.reason.strip() for gate in first.gates)


def test_cli_emits_deterministic_json_plan(tmp_path, capsys) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            _manifest(
                action="stage",
                risk_level="L3",
                components=["worker"],
                requires_restart=True,
                schema_changed=True,
            )
        ),
        encoding="utf-8",
    )

    first_exit = main(
        ["--manifest", str(manifest_path), "--format", "json"]
    )
    first_output = capsys.readouterr()
    second_exit = main(
        ["--manifest", str(manifest_path), "--format", "json"]
    )
    second_output = capsys.readouterr()

    assert first_exit == second_exit == 0
    assert first_output.err == second_output.err == ""
    assert first_output.out == second_output.out
    payload = json.loads(first_output.out)
    assert payload["schema_version"] == 1
    assert payload["action"] == "stage"
    assert payload["risk_level"] == "L3"
    assert payload["components"] == ["worker"]
    assert [gate["id"] for gate in payload["gates"]] == sorted(
        gate["id"] for gate in payload["gates"]
    )


def test_cli_text_output_is_stable_and_contains_only_plan_fields(
    tmp_path,
    capsys,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(action="push")),
        encoding="utf-8",
    )

    assert main(["--manifest", str(manifest_path), "--format", "text"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("action=push\nrisk_level=L0\ncomponents=-\n")
    assert "required git.clean_tree" in captured.out
    assert "prohibited runtime.service_control" in captured.out


def test_cli_rejects_unknown_input_without_echoing_secret(tmp_path, capsys) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest(api_secret="sensitive-value")),
        encoding="utf-8",
    )

    assert main(["--manifest", str(manifest_path), "--format", "json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid_manifest" in captured.err
    assert "api_secret" not in captured.err
    assert "sensitive-value" not in captured.err


@pytest.mark.parametrize(
    "contents",
    (
        "not-json sensitive-value",
        '["sensitive-value"]',
    ),
)
def test_cli_rejects_unreadable_shape_without_echoing_contents(
    tmp_path,
    capsys,
    contents: str,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(contents, encoding="utf-8")

    assert main(["--manifest", str(manifest_path), "--format", "json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "sensitive-value" not in captured.err


def test_cli_missing_manifest_is_a_bounded_error(tmp_path, capsys) -> None:
    missing_path = tmp_path / "missing.json"

    assert main(["--manifest", str(missing_path), "--format", "json"]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == '{"error":"manifest_unavailable"}\n'


def test_action_gate_policy_documents_all_actions_and_authority_boundary() -> None:
    policy = (ROOT / "docs" / "deployment-action-gates.md").read_text(
        encoding="utf-8"
    )

    for action in ("local", "push", "stage", "activate", "trading"):
        assert f"`{action}`" in policy
    assert "A generated plan is not authorization." in policy
    assert "Immutable staging and scoped activation are the only deployment path" in policy
    assert "legacy one-command updater has been removed" in policy
    assert "Stage must not inspect live runtime or database state" in policy
    assert "Trading enablement is never implied by activation" in policy
