"""Standalone analysis-only context backfill export, validation, and apply."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .context_resolution import (
    ContextResolutionError,
    parse_context_resolution_decision,
)
from .context_resolution_worker import build_context_state_fingerprint


SCHEMA_VERSION = "context-analysis-backfill-v1"
VALIDATION_SCHEMA_VERSION = "context-analysis-backfill-validation-v1"
APPLY_RECEIPT_SCHEMA_VERSION = "context-analysis-backfill-apply-v1"
ROLLBACK_RECEIPT_SCHEMA_VERSION = "context-analysis-backfill-rollback-v1"
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
LEDGER_COLUMNS = (
    "run_id",
    "raw_message_id",
    "source_attempt_id",
    "source_request_sha256",
    "source_state_fingerprint",
    "prompt_version",
    "analyst_model",
    "decision_json",
    "status",
    "skip_reason",
)
ACTIVE_WRITE_QUERIES = (
    "SELECT COUNT(*) FROM position_backup_stop_orders WHERE status = 'submitting'",
    "SELECT COUNT(*) FROM execution_order_legs WHERE status IN ('submitting', 'cancel_submitting')",
    "SELECT COUNT(*) FROM instruction_execution_contracts WHERE state = 'submitting'",
    "SELECT COUNT(*) FROM strategy_management_components WHERE status IN ('submitting', 'cancel_submitting')",
    "SELECT COUNT(*) FROM strategy_management_batches WHERE status = 'executing'",
    "SELECT COUNT(*) FROM strategy_revision_batches WHERE status = 'submitting_replacements'",
    """
    SELECT COUNT(*) FROM strategy_revision_legs AS child
    JOIN strategy_revision_batches AS batch ON batch.id = child.revision_batch_id
    WHERE child.status = 'cancel_submitting'
      AND typeof(batch.advance_claim_token) = 'text'
      AND length(batch.advance_claim_token) > 0
      AND batch.advance_claimed_at IS NOT NULL
    """,
    """
    SELECT COUNT(*) FROM entry_revision_replacements AS child
    JOIN strategy_revision_batches AS batch ON batch.id = child.revision_batch_id
    WHERE child.status = 'submit_reserved'
      AND typeof(batch.advance_claim_token) = 'text'
      AND length(batch.advance_claim_token) > 0
      AND batch.advance_claimed_at IS NOT NULL
    """,
    "SELECT COUNT(*) FROM trigger_protection_intents WHERE recovery_state IN ('submitting', 'cancel_submitting')",
    "SELECT COUNT(*) FROM position_mutation_intents WHERE status IN ('submitting', 'cancel_submitting')",
    "SELECT COUNT(*) FROM trade_signals WHERE status IN ('processing', 'submitting', 'cancel_submitting')",
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
    identity, records_sha = _validate_loaded_manifest(database, manifest)
    records = manifest["records"]
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


def apply_context_analysis_manifest(
    database_path: str | Path,
    *,
    manifest_path: str | Path,
    output_path: str | Path,
    effects: str | None = None,
    apply: bool = False,
    expected_database_identity: str | None = None,
    expected_records_sha256: str | None = None,
    expected_record_count: int | None = None,
) -> dict[str, Any]:
    """Dry-run or insert a closed manifest into the audit-only ledger."""

    database = Path(database_path).resolve(strict=True)
    manifest = _load_json_object(manifest_path, label="manifest")
    _validate_manifest_shape(manifest)
    if apply:
        if effects != "analysis-only":
            raise ValueError("effects must be analysis-only")
        _require_expected_apply_values(
            manifest,
            expected_database_identity=expected_database_identity,
            expected_records_sha256=expected_records_sha256,
            expected_record_count=expected_record_count,
        )

    existing = _load_existing_run(database, run_id=manifest["run_id"])
    if existing:
        _require_existing_rows_match(manifest["records"], existing)
        receipt = _build_apply_receipt(
            manifest,
            status="already_applied",
            inserted_count=0,
            rows=existing,
            database_identity_before=_database_identity(database),
            database_identity_after=_database_identity(database),
        )
        _write_canonical_json(output_path, receipt)
        return receipt

    identity, _ = _validate_loaded_manifest(database, manifest)
    with _read_only_connection(database) as connection:
        _check_runtime_gates(connection)
        _check_target_threads(connection, manifest["records"])
    if not apply:
        receipt = _build_apply_receipt(
            manifest,
            status="dry_run",
            inserted_count=0,
            rows=[],
            database_identity_before=identity,
            database_identity_after=identity,
        )
        _write_canonical_json(output_path, receipt)
        return receipt

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.set_authorizer(_make_write_authorizer("apply"))
    try:
        connection.execute("BEGIN IMMEDIATE")
        if _database_identity(database) != identity:
            raise ValueError("database_identity changed before apply lock")
        _check_current_state_fingerprints(database, manifest["records"])
        _check_runtime_gates(connection)
        for record in manifest["records"]:
            _validate_record(connection, record)
        _check_target_threads(connection, manifest["records"])
        for record in manifest["records"]:
            values = _ledger_values(manifest["run_id"], record)
            connection.execute(
                """
                INSERT INTO context_analysis_backfills (
                    run_id, raw_message_id, source_attempt_id,
                    source_request_sha256, source_state_fingerprint,
                    prompt_version, analyst_model, decision_json,
                    status, skip_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                tuple(values[column] for column in LEDGER_COLUMNS),
            )
        rows = _load_run_rows(connection, run_id=manifest["run_id"])
        _require_existing_rows_match(manifest["records"], rows)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    receipt = _build_apply_receipt(
        manifest,
        status="applied",
        inserted_count=len(rows),
        rows=rows,
        database_identity_before=identity,
        database_identity_after=_database_identity(database),
    )
    _write_canonical_json(output_path, receipt)
    return receipt


def rollback_context_analysis_backfill(
    database_path: str | Path,
    *,
    receipt_path: str | Path,
    output_path: str | Path,
    effects: str | None = None,
    apply: bool = False,
    expected_receipt_sha256: str | None = None,
) -> dict[str, Any]:
    """Delete only exact ledger rows named and hashed by an apply receipt."""

    database = Path(database_path).resolve(strict=True)
    receipt = _load_json_object(receipt_path, label="receipt")
    if receipt.get("schema_version") != APPLY_RECEIPT_SCHEMA_VERSION:
        raise ValueError("apply receipt schema_version mismatch")
    actual_receipt_sha = _receipt_sha256(receipt)
    if receipt.get("receipt_sha256") != actual_receipt_sha:
        raise ValueError("receipt_sha256 mismatch")
    if expected_receipt_sha256 != actual_receipt_sha:
        raise ValueError("expected receipt_sha256 mismatch")
    if receipt.get("status") != "applied":
        raise ValueError("rollback requires an applied receipt")
    if not apply:
        raise ValueError("rollback requires --apply")
    if effects != "analysis-only":
        raise ValueError("effects must be analysis-only")
    rows = receipt.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("apply receipt rows are required")

    before = _database_identity(database)
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.set_authorizer(_make_write_authorizer("rollback"))
    try:
        connection.execute("BEGIN IMMEDIATE")
        for expected in rows:
            if not isinstance(expected, Mapping):
                raise ValueError("apply receipt row is malformed")
            row = connection.execute(
                "SELECT * FROM context_analysis_backfills WHERE id = ?",
                (_strict_int(expected.get("id"), field="receipt row id"),),
            ).fetchone()
            if row is None or _ledger_row_sha(row) != expected.get("row_sha256"):
                raise ValueError("rollback row drift")
            if str(row["run_id"]) != receipt["run_id"]:
                raise ValueError("rollback row drift")
        for expected in rows:
            connection.execute(
                "DELETE FROM context_analysis_backfills WHERE id = ?",
                (expected["id"],),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    result = {
        "schema_version": ROLLBACK_RECEIPT_SCHEMA_VERSION,
        "status": "rolled_back",
        "effects": "analysis-only",
        "run_id": receipt["run_id"],
        "source_receipt_sha256": actual_receipt_sha,
        "deleted_count": len(rows),
        "database_identity_before": before,
        "database_identity_after": _database_identity(database),
    }
    result["receipt_sha256"] = _receipt_sha256(result)
    _write_canonical_json(output_path, result)
    return result


def _validate_manifest_shape(manifest: Mapping[str, Any]) -> tuple[list[Any], str]:
    _require_exact_fields(manifest, TOP_LEVEL_FIELDS, label="manifest")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("schema_version mismatch")
    if not isinstance(manifest["run_id"], str) or not manifest["run_id"].strip():
        raise ValueError("run_id is required")
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
    return records, records_sha


def _validate_loaded_manifest(
    database: Path, manifest: Mapping[str, Any]
) -> tuple[str, str]:
    records, records_sha = _validate_manifest_shape(manifest)
    identity = _database_identity(database)
    if manifest["database_identity"] != identity:
        raise ValueError("database_identity mismatch")
    with _read_only_connection(database) as connection:
        for record in records:
            _validate_record(connection, record)
        _check_target_threads(connection, records)
    _check_current_state_fingerprints(database, records)
    return identity, records_sha


def _check_current_state_fingerprints(
    database: Path, records: list[Any]
) -> None:
    engine = create_engine(
        f"sqlite+pysqlite:///file:{database}?mode=ro&uri=true"
    )
    readonly_session_factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    try:
        for record in records:
            if (
                not isinstance(record, Mapping)
                or record.get("status") != "analysis_only_completed"
            ):
                continue
            try:
                current = build_context_state_fingerprint(
                    readonly_session_factory,
                    _strict_int(
                        record["raw_message_id"], field="raw_message_id"
                    ),
                    candidate_thread_ids={
                        _strict_int(value, field="allowed_target_thread_id")
                        for value in record["allowed_target_thread_ids"]
                    },
                )
            except LookupError as exc:
                raise ValueError("stale current context state") from exc
            if current != record["source_state_fingerprint"]:
                raise ValueError("stale current context state")
    finally:
        engine.dispose()


def _require_expected_apply_values(
    manifest: Mapping[str, Any],
    *,
    expected_database_identity: str | None,
    expected_records_sha256: str | None,
    expected_record_count: int | None,
) -> None:
    if expected_database_identity != manifest["database_identity"]:
        raise ValueError("expected database_identity mismatch")
    if expected_records_sha256 != manifest["records_sha256"]:
        raise ValueError("expected records_sha256 mismatch")
    if expected_record_count != manifest["record_count"]:
        raise ValueError("expected record_count mismatch")


def _ledger_values(run_id: str, record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "raw_message_id": record["raw_message_id"],
        "source_attempt_id": record["source_attempt_id"],
        "source_request_sha256": record["source_request_sha256"],
        "source_state_fingerprint": record["source_state_fingerprint"],
        "prompt_version": record["prompt_version"],
        "analyst_model": record["analyst_model"],
        "decision_json": (
            _canonical_json(record["decision"])
            if record["decision"] is not None
            else None
        ),
        "status": record["status"],
        "skip_reason": record["skip_reason"],
    }


def _ledger_row_payload(row: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    return {column: row[column] for column in LEDGER_COLUMNS}


def _ledger_row_sha(row: Mapping[str, Any] | sqlite3.Row) -> str:
    return _sha256_text(_canonical_json(_ledger_row_payload(row)))


def _load_run_rows(
    connection: sqlite3.Connection, *, run_id: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM context_analysis_backfills WHERE run_id = ? ORDER BY raw_message_id",
        (run_id,),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "raw_message_id": int(row["raw_message_id"]),
            "row_sha256": _ledger_row_sha(row),
            "payload": _ledger_row_payload(row),
        }
        for row in rows
    ]


def _load_existing_run(database: Path, *, run_id: str) -> list[dict[str, Any]]:
    with _read_only_connection(database) as connection:
        return _load_run_rows(connection, run_id=run_id)


def _require_existing_rows_match(
    records: list[Any], existing: list[Mapping[str, Any]]
) -> None:
    if len(existing) != len(records):
        raise ValueError("existing backfill row count mismatch")
    expected_by_raw = {
        int(record["raw_message_id"]): _ledger_values("", record)
        for record in records
        if isinstance(record, Mapping)
    }
    for row in existing:
        raw_message_id = _strict_int(row.get("raw_message_id"), field="raw_message_id")
        expected = expected_by_raw.get(raw_message_id)
        payload = row.get("payload")
        if expected is None or not isinstance(payload, Mapping):
            raise ValueError("existing backfill row mismatch")
        expected["run_id"] = payload.get("run_id")
        expected_sha = _sha256_text(_canonical_json(expected))
        if row.get("row_sha256") != expected_sha:
            raise ValueError("existing backfill row hash mismatch")


def _build_apply_receipt(
    manifest: Mapping[str, Any],
    *,
    status: str,
    inserted_count: int,
    rows: list[Mapping[str, Any]],
    database_identity_before: str,
    database_identity_after: str,
) -> dict[str, Any]:
    receipt = {
        "schema_version": APPLY_RECEIPT_SCHEMA_VERSION,
        "status": status,
        "effects": "analysis-only",
        "run_id": manifest["run_id"],
        "record_count": manifest["record_count"],
        "records_sha256": manifest["records_sha256"],
        "inserted_count": inserted_count,
        "database_identity_before": database_identity_before,
        "database_identity_after": database_identity_after,
        "rows": [dict(row) for row in rows],
    }
    receipt["receipt_sha256"] = _receipt_sha256(receipt)
    return receipt


def _receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return _sha256_text(
        _canonical_json(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    )


def _check_runtime_gates(connection: sqlite3.Connection) -> None:
    active_writes = sum(
        int(connection.execute(query).fetchone()[0])
        for query in ACTIVE_WRITE_QUERIES
    )
    if active_writes:
        raise ValueError("active exchange write gate failed")
    active_management = int(
        connection.execute(
            """
            SELECT COUNT(*) FROM strategy_management_batches
            WHERE status NOT IN ('succeeded', 'blocked', 'resolved')
            """
        ).fetchone()[0]
    )
    if active_management:
        raise ValueError("active management batch gate failed")
    claimed_jobs = int(
        connection.execute(
            "SELECT COUNT(*) FROM message_processing_jobs WHERE status = 'claimed'"
        ).fetchone()[0]
    )
    if claimed_jobs:
        raise ValueError("claimed message job gate failed")
    active_commands = int(
        connection.execute(
            "SELECT COUNT(*) FROM worker_command_jobs WHERE status IN ('claimed', 'executing')"
        ).fetchone()[0]
    )
    if active_commands:
        raise ValueError("active worker command gate failed")


def _check_target_threads(
    connection: sqlite3.Connection, records: list[Any]
) -> None:
    for record in records:
        if not isinstance(record, Mapping) or record.get("status") != "analysis_only_completed":
            continue
        raw = connection.execute(
            "SELECT chat_id FROM raw_messages WHERE id = ?",
            (record["raw_message_id"],),
        ).fetchone()
        if raw is None:
            raise ValueError("source raw message is missing")
        for thread_id in record["decision"]["target_thread_ids"]:
            thread = connection.execute(
                "SELECT chat_id FROM strategy_threads WHERE id = ?", (thread_id,)
            ).fetchone()
            if thread is None:
                raise ValueError("target thread is missing")
            if int(thread["chat_id"]) != int(raw["chat_id"]):
                raise ValueError("target thread chat mismatch")


def _make_write_authorizer(operation: str):
    allowed_action = (
        sqlite3.SQLITE_INSERT if operation == "apply" else sqlite3.SQLITE_DELETE
    )

    def authorize(
        action: int,
        arg1: str | None,
        arg2: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        del arg2, database_name, trigger_name
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            if action == allowed_action and arg1 == "context_analysis_backfills":
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in {
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_REINDEX,
        }:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


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
        raise ValueError("stale source evidence: source_state_fingerprint mismatch")
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
    with _read_only_connection(database) as connection:
        serialized = connection.serialize()
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


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
        description="Export, validate, apply, or roll back analysis-only context backfills."
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
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("database_path")
    apply_parser.add_argument("--manifest", required=True)
    apply_parser.add_argument("--output", required=True)
    apply_parser.add_argument("--effects")
    apply_parser.add_argument("--apply", action="store_true")
    apply_parser.add_argument("--expected-database-identity")
    apply_parser.add_argument("--expected-records-sha256")
    apply_parser.add_argument("--expected-record-count", type=int)
    rollback_parser = commands.add_parser("rollback")
    rollback_parser.add_argument("database_path")
    rollback_parser.add_argument("--receipt", required=True)
    rollback_parser.add_argument("--output", required=True)
    rollback_parser.add_argument("--effects")
    rollback_parser.add_argument("--apply", action="store_true")
    rollback_parser.add_argument("--expected-receipt-sha256")
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
        elif args.command == "validate":
            result = validate_context_analysis_manifest(
                args.database_path,
                manifest_path=args.manifest,
                output_path=args.output,
            )
        elif args.command == "apply":
            result = apply_context_analysis_manifest(
                args.database_path,
                manifest_path=args.manifest,
                output_path=args.output,
                effects=args.effects,
                apply=args.apply,
                expected_database_identity=args.expected_database_identity,
                expected_records_sha256=args.expected_records_sha256,
                expected_record_count=args.expected_record_count,
            )
        else:
            result = rollback_context_analysis_backfill(
                args.database_path,
                receipt_path=args.receipt,
                output_path=args.output,
                effects=args.effects,
                apply=args.apply,
                expected_receipt_sha256=args.expected_receipt_sha256,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(
        _canonical_json(
            {
                "database_identity": result.get(
                    "database_identity", result.get("database_identity_after")
                ),
                "record_count": result.get("record_count", result.get("deleted_count")),
                "run_id": result["run_id"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
