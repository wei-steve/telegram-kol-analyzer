import copy
import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from telegram_kol_research.context_analysis_backfill import (
    ANALYST_MODEL,
    apply_context_analysis_manifest,
    export_context_analysis_incidents,
    rollback_context_analysis_backfill,
    validate_context_analysis_manifest,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageProcessingJob,
    RawMessage,
    StrategyThread,
)


OPERATIONAL_TABLES = (
    "message_processing_jobs",
    "message_recognitions",
    "recognition_decisions",
    "signal_candidates",
    "message_instruction_items",
    "message_operation_contracts",
    "message_operation_items",
    "strategy_message_links",
    "strategy_lifecycles",
    "strategy_management_batches",
    "worker_command_jobs",
    "execution_events",
    "trade_signals",
    "execution_bindings",
    "position_mutation_intents",
    "strategy_management_notifications",
    "message_operation_stage1_notifications",
)


def _canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _records_sha(records):
    return hashlib.sha256(_canonical_json(records).encode("utf-8")).hexdigest()


def _request(raw_id, message_id, *, thread_id):
    return {
        "current_message": {
            "raw_message_id": raw_id,
            "chat_id": 700,
            "message_id": message_id,
            "text": f"message-{message_id}",
        },
        "message_context": {
            "messages": [
                {"message_id": message_id - 1, "text": "earlier context"}
            ]
        },
        "candidate_strategy_threads": [
            {
                "thread_id": thread_id,
                "root_message_id": message_id - 1,
            }
        ],
        "first_pass_payload": {"recognition_result": "context"},
        "exchange_state": {"positions": []},
    }


def _add_attempt(
    session,
    *,
    raw_id,
    fingerprint,
    request,
    model="deepseek-v4-flash",
    error_class="network_error",
    state_fingerprint=None,
    last_error=None,
):
    row = ContextResolutionAttempt(
        raw_message_id=raw_id,
        context_fingerprint=fingerprint,
        state_fingerprint=state_fingerprint,
        model=model,
        prompt_versions_json=json.dumps(
            {"context_resolution": "context-resolution-v1"}
        ),
        request_summary_json=(
            request if isinstance(request, str) else _canonical_json(request)
        ),
        status="exhausted",
        error_class=error_class,
        attempts=2,
        last_error=last_error,
    )
    session.add(row)
    session.flush()
    return row.id


def _build_incident_database(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    expected = {}
    with session_factory() as session:
        raw_one = RawMessage(
            chat_id=700,
            message_id=2101,
            text="active failed",
            source_status="active",
            raw_payload='{"api_key":"outside-secret"}',
        )
        raw_two = RawMessage(
            chat_id=700,
            message_id=2201,
            text="deleted failed",
            source_status="deleted",
        )
        raw_three = RawMessage(
            chat_id=700,
            message_id=2301,
            text="active expired",
            source_status="active",
        )
        unrelated_provider = RawMessage(
            chat_id=700,
            message_id=2401,
            text="unrelated provider",
            source_status="active",
        )
        unrelated_error = RawMessage(
            chat_id=700,
            message_id=2501,
            text="unrelated error",
            source_status="active",
        )
        session.add_all(
            [
                raw_one,
                raw_two,
                raw_three,
                unrelated_provider,
                unrelated_error,
            ]
        )
        session.flush()
        session.add_all(
            StrategyThread(
                id=thread_id,
                chat_id=700,
                root_message_id=root_message_id,
                symbol="BTCUSDT",
                side="long",
                status="active",
            )
            for thread_id, root_message_id in (
                (3101, 2100),
                (3201, 2200),
                (3300, 2299),
                (3301, 2300),
            )
        )
        jobs = [
            (raw_one, "failed"),
            (raw_two, "failed"),
            (raw_three, "expired"),
            (unrelated_provider, "failed"),
            (unrelated_error, "failed"),
        ]
        session.add_all(
            MessageProcessingJob(
                raw_message_id=raw.id,
                chat_id=raw.chat_id,
                status=status,
                attempt_count=1,
                shadow=False,
            )
            for raw, status in jobs
        )
        valid_one = _request(raw_one.id, raw_one.message_id, thread_id=3101)
        expected[raw_one.id] = {
            "source_attempt_id": _add_attempt(
                session,
                raw_id=raw_one.id,
                fingerprint="sha256:one-valid",
                request=valid_one,
                state_fingerprint="sha256:state-one",
                last_error="Authorization: Bearer outside-attempt-secret",
            ),
            "request": valid_one,
        }
        _add_attempt(
            session,
            raw_id=raw_one.id,
            fingerprint="sha256:one-newest-malformed",
            request="{not-json",
        )
        valid_two = _request(raw_two.id, raw_two.message_id, thread_id=3201)
        expected[raw_two.id] = {
            "source_attempt_id": _add_attempt(
                session,
                raw_id=raw_two.id,
                fingerprint="sha256:two-valid",
                request=valid_two,
            ),
            "request": valid_two,
        }
        older_three = _request(raw_three.id, raw_three.message_id, thread_id=3300)
        _add_attempt(
            session,
            raw_id=raw_three.id,
            fingerprint="sha256:three-older",
            request=older_three,
        )
        valid_three = _request(raw_three.id, raw_three.message_id, thread_id=3301)
        expected[raw_three.id] = {
            "source_attempt_id": _add_attempt(
                session,
                raw_id=raw_three.id,
                fingerprint="sha256:three-newest",
                request=valid_three,
            ),
            "request": valid_three,
        }
        _add_attempt(
            session,
            raw_id=unrelated_provider.id,
            fingerprint="sha256:provider",
            request=_request(
                unrelated_provider.id,
                unrelated_provider.message_id,
                thread_id=3401,
            ),
            model="mimo-v2.5",
        )
        _add_attempt(
            session,
            raw_id=unrelated_error.id,
            fingerprint="sha256:error",
            request=_request(
                unrelated_error.id,
                unrelated_error.message_id,
                thread_id=3501,
            ),
            error_class="malformed_json",
        )
        session.commit()
    return database_path, expected


def _decision_for(record):
    return {
        "decision": "revise_thread",
        "target_thread_ids": [record["allowed_target_thread_ids"][0]],
        "management_action": None,
        "confidence": 0.91,
        "supporting_message_ids": record["allowed_message_ids"],
        "opposing_message_ids": [],
        "conflict_types": [],
        "risk_reducing_fanout_allowed": False,
        "reanalysis_triggers": [],
        "reason": "closed historical analysis",
    }


def _finalize(exported):
    manifest = copy.deepcopy(exported)
    for record in manifest["records"]:
        if record["source_status"] == "deleted":
            assert record["status"] == "skipped_deleted"
            continue
        record["status"] = "analysis_only_completed"
        record["decision"] = _decision_for(record)
        record["skip_reason"] = None
    manifest["records_sha256"] = _records_sha(manifest["records"])
    return manifest


def _write_manifest(path, manifest):
    path.write_text(_canonical_json(manifest) + "\n", encoding="utf-8")


def _database_files(database_path):
    return {
        path.name: path.read_bytes()
        for path in (database_path, database_path.with_name(f"{database_path.name}-wal"))
        if path.exists()
    }


def _table_snapshot(database_path, tables=OPERATIONAL_TABLES):
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        snapshot = {}
        for table in tables:
            rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY id')]
            snapshot[table] = {
                "count": len(rows),
                "sha256": hashlib.sha256(
                    _canonical_json(rows).encode("utf-8")
                ).hexdigest(),
            }
        return snapshot
    finally:
        connection.close()


def _prepared_manifest(tmp_path, *, run_id="phase6c-apply-run"):
    database_path, _ = _build_incident_database(tmp_path)
    exported = export_context_analysis_incidents(
        database_path,
        run_id=run_id,
        output_path=tmp_path / f"{run_id}-export.json",
    )
    manifest = _finalize(exported)
    manifest_path = tmp_path / f"{run_id}-manifest.json"
    _write_manifest(manifest_path, manifest)
    return database_path, manifest, manifest_path


def _apply(tmp_path, database_path, manifest, manifest_path, *, receipt_name="apply.json"):
    return apply_context_analysis_manifest(
        database_path,
        manifest_path=manifest_path,
        output_path=tmp_path / receipt_name,
        effects="analysis-only",
        apply=True,
        expected_database_identity=manifest["database_identity"],
        expected_records_sha256=manifest["records_sha256"],
        expected_record_count=manifest["record_count"],
    )


def _refresh_manifest_database_identity(database_path, manifest, manifest_path):
    import telegram_kol_research.context_analysis_backfill as backfill

    manifest["database_identity"] = backfill._database_identity(database_path)
    _write_manifest(manifest_path, manifest)


def test_export_selects_newest_valid_source_and_is_canonical_and_secret_free(
    tmp_path,
):
    database_path, expected = _build_incident_database(tmp_path)
    first_path = tmp_path / "export-one.json"
    second_path = tmp_path / "export-two.json"
    before_database = _database_files(database_path)

    first = export_context_analysis_incidents(
        database_path,
        run_id="phase6c-test-run",
        output_path=first_path,
    )
    second = export_context_analysis_incidents(
        database_path,
        run_id="phase6c-test-run",
        output_path=second_path,
    )

    assert first == second
    assert first_path.read_bytes() == second_path.read_bytes()
    assert first_path.read_text(encoding="utf-8") == _canonical_json(first) + "\n"
    assert first["schema_version"] == "context-analysis-backfill-v1"
    assert first["run_id"] == "phase6c-test-run"
    assert first["database_identity"].startswith("sha256:")
    assert first["incident_filter"] == {
        "error_class": "network_error",
        "job_statuses": ["expired", "failed"],
        "provider_model": "deepseek-v4-flash",
        "source_attempt_status": "exhausted",
    }
    assert first["record_count"] == 3
    assert first["records_sha256"] == _records_sha(first["records"])
    assert [record["raw_message_id"] for record in first["records"]] == sorted(
        expected
    )
    records = {record["raw_message_id"]: record for record in first["records"]}
    for raw_id, expected_record in expected.items():
        record = records[raw_id]
        assert record["source_attempt_id"] == expected_record["source_attempt_id"]
        assert record["request"] == expected_record["request"]
        expected_thread = expected_record["request"]["candidate_strategy_threads"][0][
            "thread_id"
        ]
        assert record["allowed_target_thread_ids"] == [expected_thread]
        assert record["allowed_message_ids"] == sorted(
            {
                expected_record["request"]["current_message"]["message_id"],
                expected_record["request"]["message_context"]["messages"][0][
                    "message_id"
                ],
            }
        )
        assert record["analyst_model"] == ANALYST_MODEL
    deleted = next(
        record for record in first["records"] if record["source_status"] == "deleted"
    )
    assert deleted["status"] == "skipped_deleted"
    assert deleted["decision"] is None
    assert deleted["skip_reason"] == "source_deleted"
    assert {record["job_status"] for record in first["records"]} == {
        "failed",
        "expired",
    }
    serialized = first_path.read_text(encoding="utf-8")
    assert "outside-secret" not in serialized
    assert "outside-attempt-secret" not in serialized
    assert _database_files(database_path) == before_database


def test_validate_accepts_closed_final_manifest_and_writes_bounded_receipt(tmp_path):
    database_path, _ = _build_incident_database(tmp_path)
    export_path = tmp_path / "export.json"
    exported = export_context_analysis_incidents(
        database_path,
        run_id="phase6c-validate-run",
        output_path=export_path,
    )
    manifest = _finalize(exported)
    manifest_path = tmp_path / "analysis.json"
    receipt_path = tmp_path / "validation.json"
    _write_manifest(manifest_path, manifest)
    before_database = _database_files(database_path)

    receipt = validate_context_analysis_manifest(
        database_path,
        manifest_path=manifest_path,
        output_path=receipt_path,
    )

    assert receipt == {
        "schema_version": "context-analysis-backfill-validation-v1",
        "run_id": "phase6c-validate-run",
        "database_identity": manifest["database_identity"],
        "record_count": 3,
        "records_sha256": manifest["records_sha256"],
        "valid": True,
    }
    assert receipt_path.read_text(encoding="utf-8") == _canonical_json(receipt) + "\n"
    assert "secret" not in receipt_path.read_text(encoding="utf-8")
    assert _database_files(database_path) == before_database


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("unknown_top_field", "unknown manifest fields"),
        ("unknown_record_field", "unknown record fields"),
        ("record_count", "record_count"),
        ("records_hash", "records_sha256"),
        ("duplicate_raw_message", "duplicate raw_message_id"),
        ("missing_source_attempt", "source attempt"),
        ("analyst_model", "analyst_model"),
        ("target_outside_request", "target_outside_candidate_set"),
        ("evidence_outside_request", "message_evidence_outside_context"),
    ],
)
def test_validate_rejects_manifest_drift_and_out_of_contract_decisions(
    tmp_path, mutation, error
):
    database_path, _ = _build_incident_database(tmp_path)
    exported = export_context_analysis_incidents(
        database_path,
        run_id="phase6c-invalid-run",
        output_path=tmp_path / "export.json",
    )
    manifest = _finalize(exported)
    if mutation == "unknown_top_field":
        manifest["unexpected"] = True
    elif mutation == "unknown_record_field":
        manifest["records"][0]["unexpected"] = True
        manifest["records_sha256"] = _records_sha(manifest["records"])
    elif mutation == "record_count":
        manifest["record_count"] += 1
    elif mutation == "records_hash":
        manifest["records"][0]["source_status"] = "tampered"
    elif mutation == "duplicate_raw_message":
        manifest["records"].append(copy.deepcopy(manifest["records"][0]))
        manifest["record_count"] = len(manifest["records"])
        manifest["records_sha256"] = _records_sha(manifest["records"])
    elif mutation == "missing_source_attempt":
        manifest["records"][0]["source_attempt_id"] = 999999
        manifest["records_sha256"] = _records_sha(manifest["records"])
    elif mutation == "analyst_model":
        manifest["records"][0]["analyst_model"] = "another-model"
        manifest["records_sha256"] = _records_sha(manifest["records"])
    elif mutation == "target_outside_request":
        manifest["records"][0]["decision"]["target_thread_ids"] = [999999]
        manifest["records_sha256"] = _records_sha(manifest["records"])
    elif mutation == "evidence_outside_request":
        manifest["records"][0]["decision"]["supporting_message_ids"] = [999999]
        manifest["records_sha256"] = _records_sha(manifest["records"])
    manifest_path = tmp_path / f"{mutation}.json"
    _write_manifest(manifest_path, manifest)

    with pytest.raises(ValueError, match=error):
        validate_context_analysis_manifest(
            database_path,
            manifest_path=manifest_path,
            output_path=tmp_path / f"{mutation}-receipt.json",
        )


def test_export_and_validate_cli_are_read_only_and_write_requested_outputs(tmp_path):
    database_path, _ = _build_incident_database(tmp_path)
    export_path = tmp_path / "cli-export.json"
    before_export = _database_files(database_path)
    exported = subprocess.run(
        [
            sys.executable,
            "-m",
            "telegram_kol_research.context_analysis_backfill",
            "export",
            str(database_path),
            "--run-id",
            "phase6c-cli-run",
            "--output",
            str(export_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert exported.returncode == 0, exported.stderr
    assert _database_files(database_path) == before_export
    manifest = _finalize(json.loads(export_path.read_text(encoding="utf-8")))
    manifest_path = tmp_path / "cli-analysis.json"
    receipt_path = tmp_path / "cli-validation.json"
    _write_manifest(manifest_path, manifest)
    before_validate = _database_files(database_path)

    validated = subprocess.run(
        [
            sys.executable,
            "-m",
            "telegram_kol_research.context_analysis_backfill",
            "validate",
            str(database_path),
            "--manifest",
            str(manifest_path),
            "--output",
            str(receipt_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert validated.returncode == 0, validated.stderr
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["valid"] is True
    assert _database_files(database_path) == before_validate
    assert "outside-secret" not in exported.stdout + exported.stderr
    assert "outside-secret" not in validated.stdout + validated.stderr


def test_apply_defaults_to_dry_run_and_preserves_all_database_tables(tmp_path):
    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)
    before_files = _database_files(database_path)
    before_operational = _table_snapshot(database_path)

    receipt = apply_context_analysis_manifest(
        database_path,
        manifest_path=manifest_path,
        output_path=tmp_path / "dry-run.json",
    )

    assert receipt["status"] == "dry_run"
    assert receipt["effects"] == "analysis-only"
    assert receipt["record_count"] == manifest["record_count"]
    assert receipt["receipt_sha256"] == hashlib.sha256(
        _canonical_json(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        ).encode("utf-8")
    ).hexdigest()
    assert _database_files(database_path) == before_files
    assert _table_snapshot(database_path) == before_operational


def test_apply_requires_explicit_analysis_only_effects(tmp_path):
    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)

    with pytest.raises(ValueError, match="effects must be analysis-only"):
        apply_context_analysis_manifest(
            database_path,
            manifest_path=manifest_path,
            output_path=tmp_path / "apply.json",
            apply=True,
            expected_database_identity=manifest["database_identity"],
            expected_records_sha256=manifest["records_sha256"],
            expected_record_count=manifest["record_count"],
        )


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"expected_database_identity": "sha256:wrong"}, "database_identity"),
        ({"expected_records_sha256": "wrong"}, "records_sha256"),
        ({"expected_record_count": 999}, "record_count"),
    ],
)
def test_apply_rejects_expected_identity_hash_or_count_mismatch(
    tmp_path, override, error
):
    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)
    arguments = {
        "expected_database_identity": manifest["database_identity"],
        "expected_records_sha256": manifest["records_sha256"],
        "expected_record_count": manifest["record_count"],
    }
    arguments.update(override)

    with pytest.raises(ValueError, match=error):
        apply_context_analysis_manifest(
            database_path,
            manifest_path=manifest_path,
            output_path=tmp_path / "apply.json",
            effects="analysis-only",
            apply=True,
            **arguments,
        )


@pytest.mark.parametrize(
    ("gate", "expected_error"),
    [
        ("active_write", "active exchange write"),
        ("management", "active management batch"),
        ("message_job", "claimed message job"),
        ("worker_command", "active worker command"),
    ],
)
def test_apply_fails_closed_when_runtime_gate_is_active(
    tmp_path, gate, expected_error
):
    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)
    connection = sqlite3.connect(database_path)
    try:
        if gate == "active_write":
            connection.execute(
                """
                INSERT INTO trade_signals (
                    signal_uid, source_type, venue, kol_id, chat_id, message_id,
                    symbol, side, action, status, payload_json, attempts,
                    created_at, updated_at
                ) VALUES (
                    'gate-signal', 'recovery', 'deepcoin', 'gate', 1, 1,
                    'BTCUSDT', 'long', 'open_position', 'submitting', '{}', 0,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        elif gate == "management":
            connection.execute(
                """
                INSERT INTO strategy_management_batches (
                    idempotency_fingerprint, raw_message_id,
                    recognition_decision_id, recognition_generation,
                    target_lifecycle_id, strategy_instance_id,
                    execution_binding_id, intent, effective_action,
                    execution_mode, partial_round_before, status,
                    target_fingerprint, target_snapshot_json, planned_at,
                    created_at, updated_at
                ) VALUES (
                    'gate-management', 1, 1, 'gate', 1, 'gate', 1,
                    'manage', 'hold', 'disabled', 0, 'ready',
                    'gate-target', '{}', CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        elif gate == "message_job":
            connection.execute(
                "UPDATE message_processing_jobs SET status = 'claimed' "
                "WHERE raw_message_id = (SELECT MAX(id) FROM raw_messages)"
            )
        else:
            connection.execute(
                """
                INSERT INTO worker_command_jobs (
                    command_id, command_type, request_json,
                    request_fingerprint, status, attempt_count,
                    result_schema_version, created_at
                ) VALUES (
                    'gate-command', 'sync_deepcoin_execution', '{}',
                    'gate-fingerprint', 'claimed', 0, 1, CURRENT_TIMESTAMP
                )
                """
            )
        connection.commit()
    finally:
        connection.close()
    _refresh_manifest_database_identity(database_path, manifest, manifest_path)

    with pytest.raises(ValueError, match=expected_error):
        _apply(tmp_path, database_path, manifest, manifest_path)


def test_apply_rejects_stale_source_evidence(tmp_path):
    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)
    source_attempt_id = manifest["records"][0]["source_attempt_id"]
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE context_resolution_attempts SET state_fingerprint = ? WHERE id = ?",
            ("sha256:changed-state", source_attempt_id),
        )
        connection.commit()
    finally:
        connection.close()
    _refresh_manifest_database_identity(database_path, manifest, manifest_path)

    with pytest.raises(ValueError, match="stale source evidence"):
        _apply(tmp_path, database_path, manifest, manifest_path)


@pytest.mark.parametrize(
    ("thread_change", "error"),
    [("delete", "target thread is missing"), ("cross_chat", "target thread chat")],
)
def test_apply_rejects_missing_or_cross_chat_target_thread(
    tmp_path, thread_change, error
):
    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)
    target_id = manifest["records"][0]["decision"]["target_thread_ids"][0]
    connection = sqlite3.connect(database_path)
    try:
        if thread_change == "delete":
            connection.execute("DELETE FROM strategy_threads WHERE id = ?", (target_id,))
        else:
            connection.execute(
                "UPDATE strategy_threads SET chat_id = 999 WHERE id = ?", (target_id,)
            )
        connection.commit()
    finally:
        connection.close()
    _refresh_manifest_database_identity(database_path, manifest, manifest_path)

    with pytest.raises(ValueError, match=error):
        _apply(tmp_path, database_path, manifest, manifest_path)


def test_apply_rejects_deleted_source_completed_classification(tmp_path):
    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)
    deleted = next(row for row in manifest["records"] if row["source_status"] == "deleted")
    deleted["status"] = "analysis_only_completed"
    deleted["decision"] = _decision_for(deleted)
    deleted["skip_reason"] = None
    manifest["records_sha256"] = _records_sha(manifest["records"])
    _write_manifest(manifest_path, manifest)

    with pytest.raises(ValueError, match="deleted source"):
        _apply(tmp_path, database_path, manifest, manifest_path)


def test_apply_inserts_only_audit_rows_and_exact_repeat_is_idempotent(
    tmp_path, monkeypatch
):
    import telegram_kol_research.context_analysis_backfill as backfill

    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)
    before = _table_snapshot(database_path)
    write_actions = []
    original_factory = backfill._make_write_authorizer

    def recording_factory(operation):
        authorize = original_factory(operation)

        def recording_authorizer(action, arg1, arg2, database_name, trigger_name):
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                write_actions.append((operation, action, arg1))
            return authorize(action, arg1, arg2, database_name, trigger_name)

        return recording_authorizer

    monkeypatch.setattr(backfill, "_make_write_authorizer", recording_factory)
    receipt = _apply(tmp_path, database_path, manifest, manifest_path)
    repeat = _apply(
        tmp_path,
        database_path,
        manifest,
        manifest_path,
        receipt_name="repeat.json",
    )

    assert receipt["status"] == "applied"
    assert receipt["inserted_count"] == manifest["record_count"]
    assert len(receipt["rows"]) == manifest["record_count"]
    assert repeat["status"] == "already_applied"
    assert repeat["inserted_count"] == 0
    assert _table_snapshot(database_path) == before
    assert write_actions
    assert {
        (action, table) for _, action, table in write_actions
    } == {(sqlite3.SQLITE_INSERT, "context_analysis_backfills")}


def test_exact_rollback_restores_preimage_and_is_scoped_to_receipt(
    tmp_path, monkeypatch
):
    import telegram_kol_research.context_analysis_backfill as backfill

    database_path, manifest, manifest_path = _prepared_manifest(tmp_path, run_id="run-one")
    before = _table_snapshot(database_path)
    first_receipt_path = tmp_path / "run-one-receipt.json"
    first = apply_context_analysis_manifest(
        database_path,
        manifest_path=manifest_path,
        output_path=first_receipt_path,
        effects="analysis-only",
        apply=True,
        expected_database_identity=manifest["database_identity"],
        expected_records_sha256=manifest["records_sha256"],
        expected_record_count=manifest["record_count"],
    )
    second_export = export_context_analysis_incidents(
        database_path,
        run_id="run-two",
        output_path=tmp_path / "run-two-export.json",
    )
    second_manifest = _finalize(second_export)
    second_manifest_path = tmp_path / "run-two-manifest.json"
    _write_manifest(second_manifest_path, second_manifest)
    second = _apply(
        tmp_path,
        database_path,
        second_manifest,
        second_manifest_path,
        receipt_name="run-two-receipt.json",
    )
    write_actions = []
    original_factory = backfill._make_write_authorizer

    def recording_factory(operation):
        authorize = original_factory(operation)

        def recording_authorizer(action, arg1, arg2, database_name, trigger_name):
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
                write_actions.append((operation, action, arg1))
            return authorize(action, arg1, arg2, database_name, trigger_name)

        return recording_authorizer

    monkeypatch.setattr(backfill, "_make_write_authorizer", recording_factory)
    rollback = rollback_context_analysis_backfill(
        database_path,
        receipt_path=first_receipt_path,
        output_path=tmp_path / "rollback.json",
        effects="analysis-only",
        apply=True,
        expected_receipt_sha256=first["receipt_sha256"],
    )

    assert rollback["status"] == "rolled_back"
    assert rollback["deleted_count"] == manifest["record_count"]
    assert _table_snapshot(database_path) == before
    assert {
        (action, table) for _, action, table in write_actions
    } == {(sqlite3.SQLITE_DELETE, "context_analysis_backfills")}
    connection = sqlite3.connect(database_path)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM context_analysis_backfills WHERE run_id = 'run-one'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM context_analysis_backfills WHERE run_id = 'run-two'"
        ).fetchone()[0] == second_manifest["record_count"]
    finally:
        connection.close()
    assert second["status"] == "applied"


def test_rollback_rejects_receipt_hash_and_row_drift(tmp_path):
    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)
    receipt_path = tmp_path / "apply.json"
    receipt = _apply(
        tmp_path,
        database_path,
        manifest,
        manifest_path,
        receipt_name=receipt_path.name,
    )

    with pytest.raises(ValueError, match="receipt_sha256"):
        rollback_context_analysis_backfill(
            database_path,
            receipt_path=receipt_path,
            output_path=tmp_path / "bad-hash.json",
            effects="analysis-only",
            apply=True,
            expected_receipt_sha256="wrong",
        )

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE context_analysis_backfills SET skip_reason = 'drift' WHERE id = ?",
            (receipt["rows"][0]["id"],),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(ValueError, match="rollback row drift"):
        rollback_context_analysis_backfill(
            database_path,
            receipt_path=receipt_path,
            output_path=tmp_path / "drift.json",
            effects="analysis-only",
            apply=True,
            expected_receipt_sha256=receipt["receipt_sha256"],
        )


def test_apply_cli_is_dry_run_by_default_and_requires_effects_for_write(tmp_path):
    database_path, manifest, manifest_path = _prepared_manifest(tmp_path)
    dry_path = tmp_path / "cli-dry.json"
    dry = subprocess.run(
        [
            sys.executable,
            "-m",
            "telegram_kol_research.context_analysis_backfill",
            "apply",
            str(database_path),
            "--manifest",
            str(manifest_path),
            "--output",
            str(dry_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert dry.returncode == 0, dry.stderr
    assert json.loads(dry_path.read_text(encoding="utf-8"))["status"] == "dry_run"

    missing_effects = subprocess.run(
        [
            sys.executable,
            "-m",
            "telegram_kol_research.context_analysis_backfill",
            "apply",
            str(database_path),
            "--manifest",
            str(manifest_path),
            "--output",
            str(tmp_path / "cli-apply.json"),
            "--apply",
            "--expected-database-identity",
            manifest["database_identity"],
            "--expected-records-sha256",
            manifest["records_sha256"],
            "--expected-record-count",
            str(manifest["record_count"]),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_effects.returncode != 0
    assert "effects must be analysis-only" in missing_effects.stderr
