"""Standalone command line interface for the simplified deployment gate."""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence

from .deployment_preflight import (
    DeploymentPreflightInputError,
    build_final_deployment_preflight_artifact,
    build_preliminary_deployment_preflight_artifact,
    read_deployment_preflight_artifact,
    verify_deployment_preflight_artifact,
    write_deployment_preflight_artifact,
)
from .deployment_work_evidence import collect_deployment_evidence
from .deployment_writer_surface import (
    WriterSurfaceError,
    classify_candidate_surface,
)


MAX_INPUT_JSON_BYTES = 65_536
_EXIT_CODES = {"PASS": 0, "WARN": 2, "BLOCK": 3}


class _CliInputError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _CliInputError("cli_arguments_invalid")


def _add_surface_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--production-commit", required=True)
    parser.add_argument("--candidate-commit", required=True)


def _add_fact_arguments(parser: argparse.ArgumentParser) -> None:
    _add_surface_arguments(parser)
    parser.add_argument("--database-path", required=True)
    parser.add_argument("--snapshot-status", required=True)
    parser.add_argument("--schema-verification", required=True)
    parser.add_argument("--database-watermark", required=True)
    parser.add_argument("--now")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="deployment-preflight-v2", add_help=True)
    subcommands = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_ArgumentParser,
    )

    surface = subcommands.add_parser("surface")
    _add_surface_arguments(surface)

    collect = subcommands.add_parser("collect")
    _add_fact_arguments(collect)
    collect.add_argument("--phase", required=True, choices=("preliminary", "final"))
    collect.add_argument("--output", required=True)
    collect.add_argument("--preliminary-artifact")
    collect.add_argument("--preliminary-fingerprint")

    verify = subcommands.add_parser("verify")
    _add_fact_arguments(verify)
    verify.add_argument(
        "--expected-phase",
        required=True,
        choices=("preliminary", "final"),
    )
    verify.add_argument("--input", required=True)
    verify.add_argument("--preliminary-artifact")
    verify.add_argument("--preliminary-fingerprint")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        if arguments.command == "surface":
            return _surface_command(arguments)
        if arguments.command == "collect":
            return _collect_command(arguments)
        if arguments.command == "verify":
            return _verify_command(arguments)
        raise _CliInputError("cli_command_invalid")
    except (
        _CliInputError,
        DeploymentPreflightInputError,
        WriterSurfaceError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ):
        print("ERROR invalid_input", file=sys.stderr)
        return 4
    except Exception:
        print("ERROR internal_validation_failed", file=sys.stderr)
        return 4


def _surface_command(arguments: argparse.Namespace) -> int:
    surface = _surface(arguments)
    print(json.dumps(asdict(surface), sort_keys=True, separators=(",", ":")))
    return 0


def _collect_command(arguments: argparse.Namespace) -> int:
    surface, evidence, snapshot, schema, watermark, now = _facts(arguments)
    phase = str(arguments.phase)
    if phase == "preliminary":
        if (
            arguments.preliminary_artifact is not None
            or arguments.preliminary_fingerprint is not None
        ):
            raise _CliInputError("cli_parent_invalid")
        artifact = build_preliminary_deployment_preflight_artifact(
            production_commit=arguments.production_commit,
            candidate_commit=arguments.candidate_commit,
            surface=surface,
            evidence=evidence,
            snapshot_status=snapshot,
            schema_verification=schema,
            database_watermark=watermark,
            now=now,
        )
    else:
        if (
            arguments.preliminary_artifact is None
            or arguments.preliminary_fingerprint is None
        ):
            raise _CliInputError("cli_parent_missing")
        artifact = build_final_deployment_preflight_artifact(
            production_commit=arguments.production_commit,
            candidate_commit=arguments.candidate_commit,
            surface=surface,
            evidence=evidence,
            snapshot_status=snapshot,
            schema_verification=schema,
            database_watermark=watermark,
            preliminary_artifact=read_deployment_preflight_artifact(
                arguments.preliminary_artifact
            ),
            preliminary_fingerprint=arguments.preliminary_fingerprint,
            now=now,
        )
    write_deployment_preflight_artifact(arguments.output, artifact)
    return _report(artifact)


def _verify_command(arguments: argparse.Namespace) -> int:
    surface, evidence, snapshot, schema, watermark, now = _facts(arguments)
    phase = str(arguments.expected_phase)
    preliminary = None
    if phase == "final":
        if (
            arguments.preliminary_artifact is None
            or arguments.preliminary_fingerprint is None
        ):
            raise _CliInputError("cli_parent_missing")
        preliminary = read_deployment_preflight_artifact(
            arguments.preliminary_artifact
        )
    elif (
        arguments.preliminary_artifact is not None
        or arguments.preliminary_fingerprint is not None
    ):
        raise _CliInputError("cli_parent_invalid")
    artifact = read_deployment_preflight_artifact(arguments.input)
    decision = verify_deployment_preflight_artifact(
        artifact,
        expected_phase=phase,
        production_commit=arguments.production_commit,
        candidate_commit=arguments.candidate_commit,
        surface=surface,
        evidence=evidence,
        snapshot_status=snapshot,
        schema_verification=schema,
        database_watermark=watermark,
        preliminary_artifact=preliminary,
        preliminary_fingerprint=arguments.preliminary_fingerprint,
        now=now,
    )
    return _report({"phase": phase, "decision": decision, "reason_codes": artifact["reason_codes"]})


def _surface(arguments: argparse.Namespace):
    return classify_candidate_surface(
        repository=arguments.repository,
        production_commit=arguments.production_commit,
        candidate_commit=arguments.candidate_commit,
    )


def _facts(arguments: argparse.Namespace):
    return (
        _surface(arguments),
        collect_deployment_evidence(arguments.database_path),
        _json_mapping(arguments.snapshot_status),
        _json_mapping(arguments.schema_verification),
        _json_mapping(arguments.database_watermark),
        _now(arguments.now),
    )


def _json_mapping(path: str | Path) -> Mapping[str, object]:
    source = Path(path)
    with source.open("rb") as handle:
        payload = handle.read(MAX_INPUT_JSON_BYTES + 1)
    if len(payload) > MAX_INPUT_JSON_BYTES:
        raise _CliInputError("cli_json_too_large")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise _CliInputError("cli_json_invalid")
    return value


def _now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _CliInputError("cli_time_invalid") from exc
    if parsed.tzinfo is None:
        raise _CliInputError("cli_time_invalid")
    return parsed.astimezone(UTC)


def _report(artifact: Mapping[str, object]) -> int:
    decision = str(artifact.get("decision"))
    if decision not in _EXIT_CODES:
        raise _CliInputError("cli_decision_invalid")
    reasons = artifact.get("reason_codes")
    if not isinstance(reasons, list):
        raise _CliInputError("cli_reasons_invalid")
    rendered_reasons = ",".join(str(reason) for reason in reasons) or "none"
    print(
        f"phase={artifact.get('phase')} decision={decision} reasons={rendered_reasons}"
    )
    return _EXIT_CODES[decision]


if __name__ == "__main__":
    raise SystemExit(main())
