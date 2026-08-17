"""Standalone schema-v2 deployment preflight command line interface."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence

from .deployment_change_surface import ChangeSurfaceError, classify_change_surface
from .deployment_preflight import (
    DeploymentPreflightInputError,
    build_final_deployment_preflight_artifact,
    build_preliminary_deployment_preflight_artifact,
    collect_deployment_preflight_facts,
    read_deployment_preflight_artifact,
    verify_phase_bound_deployment_preflight_artifact,
    write_deployment_preflight_artifact,
)


_EXIT_CODES = {"PASS": 0, "WARN": 2, "BLOCK": 3}


class _PreflightArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise DeploymentPreflightInputError(
            "preflight_cli_arguments_invalid"
        )


def _parser() -> argparse.ArgumentParser:
    parser = _PreflightArgumentParser(prog="deployment-preflight")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser("collect")
    _add_identity_arguments(collect)
    collect.add_argument("--database", required=True)
    collect.add_argument("--output", required=True)
    collect.add_argument("--live-snapshot-path")
    collect.add_argument("--previous-live-snapshot-path")
    collect.add_argument("--schema-backup-path")
    collect.add_argument("--schema-migration-dry-run-path")
    collect.add_argument("--reviewed-shadow-evidence-path")
    collect.add_argument("--authorize-live-promotion", action="store_true")
    collect.add_argument("--preliminary-artifact")

    verify = subparsers.add_parser("verify")
    _add_identity_arguments(verify)
    verify.add_argument("--input", required=True)
    verify.add_argument("--preliminary-artifact")
    return parser


def _add_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--production-commit", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--requested-change-class", required=True)
    parser.add_argument(
        "--phase", required=True, choices=("preliminary", "final")
    )
    parser.add_argument("--now")


def _now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DeploymentPreflightInputError("preflight_time_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeploymentPreflightInputError("preflight_time_invalid")
    return parsed.astimezone(UTC)


def _surface(args: argparse.Namespace, counts: Mapping[str, Mapping[str, int]]):
    return classify_change_surface(
        repository=args.repository,
        production_commit=args.production_commit,
        candidate_commit=args.candidate_commit,
        requested_change_class=args.requested_change_class,
        work_classification_counts=counts,
    )


def _collect(args: argparse.Namespace) -> int:
    checked_at = _now(args.now)
    initial_surface = _surface(args, {})
    facts = collect_deployment_preflight_facts(
        database_path=args.database,
        change_class=initial_surface.effective_change_class,
        now=checked_at,
        live_snapshot_path=args.live_snapshot_path,
        previous_live_snapshot_path=args.previous_live_snapshot_path,
        schema_backup_path=args.schema_backup_path,
        schema_migration_dry_run_path=args.schema_migration_dry_run_path,
        reviewed_shadow_evidence_path=args.reviewed_shadow_evidence_path,
        expected_commit=args.candidate_commit,
        explicit_live_authorization=args.authorize_live_promotion,
    )
    change_surface = _surface(args, facts.work_classification_counts)
    if change_surface.effective_change_class != initial_surface.effective_change_class:
        facts = collect_deployment_preflight_facts(
            database_path=args.database,
            change_class=change_surface.effective_change_class,
            now=checked_at,
            live_snapshot_path=args.live_snapshot_path,
            previous_live_snapshot_path=args.previous_live_snapshot_path,
            schema_backup_path=args.schema_backup_path,
            schema_migration_dry_run_path=args.schema_migration_dry_run_path,
            reviewed_shadow_evidence_path=args.reviewed_shadow_evidence_path,
            expected_commit=args.candidate_commit,
            explicit_live_authorization=args.authorize_live_promotion,
        )
        change_surface = _surface(args, facts.work_classification_counts)

    if args.phase == "preliminary":
        if args.preliminary_artifact is not None:
            raise DeploymentPreflightInputError(
                "preliminary_artifact_unexpected"
            )
        artifact = build_preliminary_deployment_preflight_artifact(
            production_commit=args.production_commit,
            candidate_commit=args.candidate_commit,
            requested_change_class=args.requested_change_class,
            change_surface=change_surface,
            facts=facts,
            now=checked_at,
        )
    else:
        if args.preliminary_artifact is None:
            raise DeploymentPreflightInputError(
                "preliminary_artifact_required"
            )
        preliminary = read_deployment_preflight_artifact(
            args.preliminary_artifact
        )
        artifact = build_final_deployment_preflight_artifact(
            preliminary_artifact=preliminary,
            production_commit=args.production_commit,
            candidate_commit=args.candidate_commit,
            requested_change_class=args.requested_change_class,
            change_surface=change_surface,
            facts=facts,
            now=checked_at,
        )
    write_deployment_preflight_artifact(args.output, artifact)
    return _emit_success(artifact)


def _verify(args: argparse.Namespace) -> int:
    checked_at = _now(args.now)
    artifact = read_deployment_preflight_artifact(args.input)
    checked_facts = artifact.get("checked_facts")
    if not isinstance(checked_facts, Mapping):
        raise DeploymentPreflightInputError("preflight_artifact_facts_invalid")
    counts = checked_facts.get("work_classification_counts")
    if not isinstance(counts, Mapping):
        raise DeploymentPreflightInputError("preflight_artifact_facts_invalid")
    change_surface = _surface(args, counts)
    preliminary = None
    if args.phase == "final":
        if args.preliminary_artifact is None:
            raise DeploymentPreflightInputError(
                "preliminary_artifact_required"
            )
        preliminary = read_deployment_preflight_artifact(
            args.preliminary_artifact
        )
    elif args.preliminary_artifact is not None:
        raise DeploymentPreflightInputError("preliminary_artifact_unexpected")
    decision = verify_phase_bound_deployment_preflight_artifact(
        artifact,
        phase=args.phase,
        production_commit=args.production_commit,
        candidate_commit=args.candidate_commit,
        requested_change_class=args.requested_change_class,
        change_surface=change_surface,
        now=checked_at,
        preliminary_artifact=preliminary,
    )
    return _emit_success(artifact, decision=decision)


def _emit_success(
    artifact: Mapping[str, object], *, decision: str | None = None
) -> int:
    normalized_decision = decision or str(artifact["decision"])
    print(
        json.dumps(
            {
                "decision": normalized_decision,
                "fingerprint": artifact["fingerprint"],
                "phase": artifact["phase"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return _EXIT_CODES[normalized_decision]


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "collect":
            return _collect(args)
        return _verify(args)
    except (DeploymentPreflightInputError, ChangeSurfaceError) as exc:
        reason = getattr(exc, "reason_code", str(exc))
        print(str(reason), file=sys.stderr)
        return 4
    except (OSError, TypeError, ValueError, KeyError):
        print("preflight_input_malformed", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
