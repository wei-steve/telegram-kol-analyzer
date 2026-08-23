"""Read-only export and validation for analysis-only context backfills."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .context_resolution import (
    ContextResolutionError,
    parse_context_resolution_decision,
)


SCHEMA_VERSION = "context-analysis-backfill-v1"
VALIDATION_SCHEMA_VERSION = "context-analysis-backfill-validation-v1"
ANALYST_MODEL = "codex-manual-context-v1"
INCIDENT_FILTER = {
    "error_class": "network_error",
    "job_statuses": ["expired", "failed"],
    "provider_model": "deepseek-v4-flash",
    "source_attempt_status": "exhausted",
}
TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "database_identity",
        "incident_filter",
        "record_count",
        "records_sha256",
        "records",
    }
)
RECORD_FIELDS = frozenset(
    {
        "raw_message_id",
        "source_attempt_id",
        "source_request_sha256",
        "source_state_fingerprint",
        "prompt_version",
        "source_status",
        "job_status",
        "request",
        "allowed_target_thread_ids",
        "allowed_message_ids",
        "analyst_model",
        "decision",
        "status",
        "skip_reason",
    }
)
DECISION_FIELDS = frozenset(
    {
        "decision",
        "target_thread_ids",
        "management_action",
        "confidence",
        "supporting_message_ids",
        "opposing_message_ids",
        "conflict_types",
        "risk_reducing_fanout_allowed",
        "reanalysis_triggers",
        "reason",
    }
)


def export_context_analysis_incidents(
    database_path: str | Path,
    *,
    run_id: str,
    output_path: str | Path,
) -> dict[str, Any]:
    database = Path(database_path).resolve(strict=True)
    normalized_run_id = str(run_id).strip()
    if not normalized_run_id:
        raise ValueError("run_id is required")
    identity = _database_identity(database)
    with _read_only_connection(database) as connection:
        rows = connection.execute(
            """
            SELECT
                cra.id AS source_attempt_id,
                cra.raw_message_id,
                cra.state_fingerprint AS source_state_fingerprint,
                cra.prompt_versions_json,
                cra.request_summary_json,
                rm.source_status,
                mpj.status AS job_status
            FROM context_resolution_attempts AS cra
            JOIN raw_messages AS rm ON rm.id = cra.raw_message_id
            JOIN message_processing_jobs AS mpj
              ON mpj.raw_message_id = cra.raw_message_id
            WHERE cra.model = ?
              AND cra.error_class = ?
              AND cra.status = ?
              AND mpj.status IN (?, ?)
            ORDER BY cra.raw_message_id ASC, cra.id DESC
            """,
            (
                INCIDENT_FILTER["provider_model"],
                INCIDENT_FILTER["error_class"],
                INCIDENT_FILTER["source_attempt_status"],
                *INCIDENT_FILTER["job_statuses"],
            ),
        ).fetchall()
    records = _select_export_records(rows)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": normalized_run_id,
        "database_identity": identity,
        "incident_filter": INCIDENT_FILTER,
        "record_count": len(records),
        "records_sha256": _sha256_text(_canonical_json(records)),
        "records": records,
    }
    _write_canonical_json(output_path, manifest)
    return manifest


def validate_context_analysis_manifest(
    database_path: str | Path,
    *,
    manifest_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    database = Path(database_path).resolve(strict=True)
    manifest = _load_json_object(manifest_path, label="manifest")
    _require_exact_fields(manifest, TOP_LEVEL_FIELDS, label="manifest")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("schema_version mismatch")
    if not isinstance(manifest["run_id"], str) or not manifest["run_id"].strip():
        raise ValueError("run_id is required")
    identity = _database_identity(database)
    if manifest["database_identity"] != identity:
        raise ValueError("database_identity mismatch")
    if manifest["incident_filter"] != INCIDENT_FILTER:
        raise ValueError("incident_filter mismatch")
    records = manifest["records"]
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    if manifest["record_count"] != len(records):
        raise ValueError("record_count mismatch")
    records_sha = _sha256_text(_canonical_json(records))
    if manifest["records_sha256"] != records_sha:
        raise ValueError("records_sha256 mismatch")
    raw_message_ids = [
        _strict_int(record.get("raw_message_id"), field="raw_message_id")
        if isinstance(record, Mapping)
        else -1
        for record in records
    ]
    if len(raw_message_ids) != len(set(raw_message_ids)):
        raise ValueError("duplicate raw_message_id")

    with _read_only_connection(database) as connection:
        for record in records:
            _validate_record(connection, record)
    receipt = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "run_id": manifest["run_id"],
        "database_identity": identity,
        "record_count": len(records),
        "records_sha256": records_sha,
        "valid": True,
    }
    _write_canonical_json(output_path, receipt)
    return receipt


def _select_export_records(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    by_message: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_message.setdefault(int(row["raw_message_id"]), []).append(row)
    records: list[dict[str, Any]] = []
    for raw_message_id in sorted(by_message):
        selected: tuple[sqlite3.Row, dict[str, Any], str] | None = None
        for row in by_message[raw_message_id]:
            try:
                request = json.loads(str(row["request_summary_json"]))
                prompt_versions = json.loads(str(row["prompt_versions_json"]))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(request, dict) or not isinstance(prompt_versions, dict):
                continue
            prompt_version = str(
                prompt_versions.get("context_resolution") or ""
            ).strip()
            if not prompt_version:
                continue
            selected = (row, request, prompt_version)
            break
        if selected is None:
            raise ValueError(
                f"no valid source request for raw_message_id={raw_message_id}"
            )
        row, request, prompt_version = selected
        allowed_threads, allowed_messages = _allowed_ids(request)
        deleted = str(row["source_status"]) == "deleted"
        records.append(
            {
                "raw_message_id": raw_message_id,
                "source_attempt_id": int(row["source_attempt_id"]),
                "source_request_sha256": _sha256_text(_canonical_json(request)),
                "source_state_fingerprint": row["source_state_fingerprint"],
                "prompt_version": prompt_version,
                "source_status": str(row["source_status"]),
                "job_status": str(row["job_status"]),
                "request": request,
                "allowed_target_thread_ids": sorted(allowed_threads),
                "allowed_message_ids": sorted(allowed_messages),
                "analyst_model": ANALYST_MODEL,
                "decision": None,
                "status": "skipped_deleted" if deleted else "pending_analysis",
                "skip_reason": "source_deleted" if deleted else None,
            }
        )
    return records


def _validate_record(connection: sqlite3.Connection, record: Any) -> None:
    if not isinstance(record, Mapping):
        raise ValueError("record must be an object")
    _require_exact_fields(record, RECORD_FIELDS, label="record")
    raw_message_id = _strict_int(record["raw_message_id"], field="raw_message_id")
    source_attempt_id = _strict_int(
        record["source_attempt_id"], field="source_attempt_id"
    )
    if record["analyst_model"] != ANALYST_MODEL:
        raise ValueError("analyst_model mismatch")
    source = connection.execute(
        """
        SELECT
            cra.raw_message_id,
            cra.state_fingerprint AS source_state_fingerprint,
            cra.prompt_versions_json,
            cra.request_summary_json,
            cra.model,
            cra.error_class,
            cra.status AS source_attempt_status,
            rm.source_status,
            mpj.status AS job_status
        FROM context_resolution_attempts AS cra
        JOIN raw_messages AS rm ON rm.id = cra.raw_message_id
        JOIN message_processing_jobs AS mpj
          ON mpj.raw_message_id = cra.raw_message_id
        WHERE cra.id = ?
        """,
        (source_attempt_id,),
    ).fetchone()
    if source is None:
        raise ValueError("source attempt is missing")
    if int(source["raw_message_id"]) != raw_message_id:
        raise ValueError("source attempt raw_message_id mismatch")
    if (
        source["model"] != INCIDENT_FILTER["provider_model"]
        or source["error_class"] != INCIDENT_FILTER["error_class"]
        or source["source_attempt_status"]
        != INCIDENT_FILTER["source_attempt_status"]
        or source["job_status"] not in INCIDENT_FILTER["job_statuses"]
    ):
        raise ValueError("source attempt is outside incident filter")
    try:
        request = json.loads(str(source["request_summary_json"]))
        prompt_versions = json.loads(str(source["prompt_versions_json"]))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("source attempt request is malformed") from exc
    if not isinstance(request, dict) or not isinstance(prompt_versions, dict):
        raise ValueError("source attempt request is malformed")
    source_request_sha = _sha256_text(_canonical_json(request))
    if record["source_request_sha256"] != source_request_sha:
        raise ValueError("source_request_sha256 mismatch")
    if record["request"] != request:
        raise ValueError("source request mismatch")
    if record["source_state_fingerprint"] != source["source_state_fingerprint"]:
        raise ValueError("source_state_fingerprint mismatch")
    prompt_version = str(prompt_versions.get("context_resolution") or "").strip()
    if record["prompt_version"] != prompt_version:
        raise ValueError("prompt_version mismatch")
    if record["source_status"] != source["source_status"]:
        raise ValueError("source_status mismatch")
    if record["job_status"] != source["job_status"]:
        raise ValueError("job_status mismatch")
    allowed_threads, allowed_messages = _allowed_ids(request)
    if record["allowed_target_thread_ids"] != sorted(allowed_threads):
        raise ValueError("allowed_target_thread_ids mismatch")
    if record["allowed_message_ids"] != sorted(allowed_messages):
        raise ValueError("allowed_message_ids mismatch")
    _validate_analysis_result(
        record,
        source_status=str(source["source_status"]),
        allowed_threads=allowed_threads,
        allowed_messages=allowed_messages,
    )


def _validate_analysis_result(
    record: Mapping[str, Any],
    *,
    source_status: str,
    allowed_threads: set[int],
    allowed_messages: set[int],
) -> None:
    status = record["status"]
    decision = record["decision"]
    skip_reason = record["skip_reason"]
    if source_status == "deleted":
        if status != "skipped_deleted" or decision is not None or not skip_reason:
            raise ValueError("deleted source must be skipped_deleted")
        return
    if status == "skipped_stale":
        if decision is not None or not skip_reason:
            raise ValueError("skipped_stale requires a skip_reason and no decision")
        return
    if status != "analysis_only_completed":
        raise ValueError("non-deleted source must be analysis_only_completed")
    if not isinstance(decision, Mapping):
        raise ValueError("analysis_only_completed requires a decision")
    _require_exact_fields(decision, DECISION_FIELDS, label="decision")
    try:
        parsed = parse_context_resolution_decision(
            decision,
            allowed_thread_ids=allowed_threads,
            allowed_message_ids=allowed_messages,
        )
    except ContextResolutionError:
        raise
    if parsed.to_dict() != decision:
        raise ValueError("decision normalization mismatch")
    if skip_reason is not None:
        raise ValueError("analysis_only_completed cannot have skip_reason")


def _allowed_ids(request: Mapping[str, Any]) -> tuple[set[int], set[int]]:
    allowed_threads = _collect_ids(
        request.get("candidate_strategy_threads"),
        {"thread_id", "strategy_thread_id"},
    )
    allowed_messages = _collect_ids(
        {
            "current": request.get("current_message"),
            "context": request.get("message_context"),
            "candidates": request.get("candidate_strategy_threads"),
        },
        {"message_id", "source_message_id", "root_message_id"},
    )
    return allowed_threads, allowed_messages


def _collect_ids(value: Any, key_names: set[str]) -> set[int]:
    found: set[int] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in key_names and item is not None:
                try:
                    found.add(int(item))
                except (TypeError, ValueError):
                    pass
            found.update(_collect_ids(item, key_names))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_collect_ids(item, key_names))
    return found


@contextmanager
def _read_only_connection(database: Path) -> Iterator[sqlite3.Connection]:
    uri = f"{database.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=1")
        before = connection.total_changes
        yield connection
        if connection.total_changes != before:
            raise RuntimeError("read-only command changed the database")
    finally:
        connection.close()


def _database_identity(database: Path) -> str:
    digest = hashlib.sha256()
    for suffix in ("", "-wal"):
        path = Path(f"{database}{suffix}")
        if not path.exists():
            continue
        content = path.read_bytes()
        digest.update(suffix.encode("ascii"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"


def _load_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], *, label: str
) -> None:
    actual = set(value)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise ValueError(f"unknown {label} fields: {','.join(unknown)}")
    if missing:
        raise ValueError(f"missing {label} fields: {','.join(missing)}")


def _strict_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_canonical_json(path: str | Path, value: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_canonical_json(value) + "\n", encoding="utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export or validate analysis-only context backfills."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export_parser = commands.add_parser("export")
    export_parser.add_argument("database_path")
    export_parser.add_argument("--run-id", required=True)
    export_parser.add_argument("--output", required=True)
    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("database_path")
    validate_parser.add_argument("--manifest", required=True)
    validate_parser.add_argument("--output", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_context_analysis_incidents(
                args.database_path,
                run_id=args.run_id,
                output_path=args.output,
            )
        else:
            result = validate_context_analysis_manifest(
                args.database_path,
                manifest_path=args.manifest,
                output_path=args.output,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(
        _canonical_json(
            {
                "database_identity": result["database_identity"],
                "record_count": result["record_count"],
                "run_id": result["run_id"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
