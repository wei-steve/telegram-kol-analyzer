import copy
import hashlib
import json
import subprocess
import sys

import pytest

from telegram_kol_research.context_analysis_backfill import (
    ANALYST_MODEL,
    export_context_analysis_incidents,
    validate_context_analysis_manifest,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    MessageProcessingJob,
    RawMessage,
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
