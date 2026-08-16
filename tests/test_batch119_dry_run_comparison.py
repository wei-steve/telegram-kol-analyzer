from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "compare_batch119_dry_runs.py"
RUNBOOK = Path(__file__).parents[1] / "docs" / "runbook.md"


def _batch119_recovery_section() -> str:
    return RUNBOOK.read_text(encoding="utf-8").split(
        "## Batch 119 composite-management recovery", 1
    )[1].split("## 精确仓位管理活性 v2", 1)[0]


def _batch119_apply_isolation_block() -> str:
    section = _batch119_recovery_section()
    assert "# BEGIN BATCH119_APPLY_ISOLATION" in section
    assert "# END BATCH119_APPLY_ISOLATION" in section
    return section.split("# BEGIN BATCH119_APPLY_ISOLATION", 1)[1].split(
        "# END BATCH119_APPLY_ISOLATION", 1
    )[0]


def _confirmed_reservation_inspector_source() -> str:
    section = _batch119_recovery_section()
    assert "# BEGIN BATCH119_CONFIRMED_RESERVATION_INSPECTOR_PY" in section
    return section.split(
        "# BEGIN BATCH119_CONFIRMED_RESERVATION_INSPECTOR_PY", 1
    )[1].split("# END BATCH119_CONFIRMED_RESERVATION_INSPECTOR_PY", 1)[0]


def _valid_dry_run() -> dict[str, object]:
    source_fingerprint = "a" * 64
    exchange_fingerprint = "b" * 64
    position = {
        "disposition": "position_absent",
        "current_size": None,
        "close_delta": "0",
        "effective_remaining_size": "0",
    }
    evidence = {
        "schema_version": 1,
        "batch_id": 119,
        "decision": "repair_false_legacy_submission",
        "reason_code": "false_legacy_submission_proven",
        "source_fingerprint": source_fingerprint,
        "exchange_snapshot_fingerprint": exchange_fingerprint,
        "immutable_target": {
            "instrument_id": "BTC-USDT-SWAP",
            "side": "long",
            "trusted_start_size": "38",
            "target_remaining_size": "19",
            "quantity_step": "0.001",
            "min_quantity": "0.001",
        },
        "position": position,
        "durable": {
            "batch_status": "reconciling",
            "leg_status": "submitted",
            "batch_row_fingerprint": "1" * 64,
            "management_leg_row_fingerprint": "2" * 64,
            "batch_stable_authority_fingerprint": "3" * 64,
            "management_leg_stable_authority_fingerprint": "4" * 64,
            "component_statuses": ["recovery_required", "pending", "pending"],
            "component_attempt_counts": [0, 0, 0],
            "component_count": 3,
            "close_submission_evidence_count": 0,
            "instruction_population": {
                "schema_version": 1,
                "total_count": 1,
                "counts": {
                    "approved_historical_pending_frozen": 0,
                    "historical_unknown_frozen": 0,
                    "target_incident_frozen": 1,
                    "verified_terminal_mirror": 0,
                },
                "digest": "d" * 64,
            },
        },
        "exchange": {
            "snapshot_complete": True,
            "capture_authority": "write_generation",
            "exact_position_count": 0,
            "regular_close_evidence_count": 0,
            "owned_protection_count": 2,
        },
        "proposed_transition": {
            "batch_status": "resolved",
            "batch_reason_code": "composite_recovery_exact_position_absent",
            "leg_status": "failed",
            "component_statuses": [
                "safely_skipped",
                "safely_skipped",
                "safely_skipped",
            ],
            "exchange_call_possible": False,
        },
        "natural_stop": {
            "purpose": "stop_loss",
            "trigger_status": "successful_terminal",
            "position_status": "closed",
            "time_relation": "trigger_not_after_close",
            "trigger_count": 1,
            "closed_position_count": 1,
            "order_ref": "e" * 64,
            "position_ref": "f" * 64,
        },
    }
    evidence_fingerprint = sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "mode": "dry_run",
        "plan": {
            "batch_id": 119,
            "status": "ready",
            "reason_code": "false_legacy_submission_proven",
            "position": position,
            "source_fingerprint": source_fingerprint,
            "exchange_snapshot_fingerprint": exchange_fingerprint,
            "evidence_fingerprint": evidence_fingerprint,
            "evidence": evidence,
            "production_writes": 0,
            "exchange_calls": 0,
        },
    }


def _run_compare(tmp_path: Path, left: object, right: object):
    paths = []
    for index, value in enumerate((left, right), start=1):
        path = tmp_path / f"dry-run-{index}.json"
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            path.write_text(
                json.dumps(value, sort_keys=True),
                encoding="utf-8",
            )
        path.chmod(0o600)
        paths.append(path)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(path) for path in paths)],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_fingerprint_projection(tmp_path: Path, document: object):
    path = tmp_path / "fresh-review.json"
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--project-fingerprint", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_batch119_fingerprint_projector_uses_full_closed_validator(tmp_path):
    document = _valid_dry_run()
    accepted = _run_fingerprint_projection(tmp_path, document)
    document["plan"]["reason_code"] = "future_reason"
    refused = _run_fingerprint_projection(tmp_path, document)

    assert accepted.returncode == 0
    assert accepted.stdout == document["plan"]["evidence_fingerprint"] + "\n"
    assert accepted.stderr == ""
    assert refused.returncode == 2
    assert refused.stdout == ""
    assert refused.stderr == ""


def test_batch119_fingerprint_projector_refuses_symlink(tmp_path):
    document = _valid_dry_run()
    target = tmp_path / "target.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "review.json"
    link.symlink_to(target)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--project-fingerprint", str(link)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr == ""


def test_batch119_fingerprint_projector_refuses_path_replacement_after_read(
    tmp_path,
    monkeypatch,
):
    spec = importlib.util.spec_from_file_location(
        "_batch119_comparator_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    path = tmp_path / "review.json"
    replacement = tmp_path / "replacement.json"
    for target in (path, replacement):
        target.write_text(json.dumps(_valid_dry_run()), encoding="utf-8")
        target.chmod(0o600)
    real_read = module._read_private_file

    def replace_after_read(candidate):
        result = real_read(candidate)
        replacement.replace(path)
        return result

    monkeypatch.setattr(module, "_read_private_file", replace_after_read)

    assert module.main(["--project-fingerprint", str(path)]) == 2


def _resign_evidence(document: dict[str, object]) -> None:
    evidence = document["plan"]["evidence"]
    document["plan"]["evidence_fingerprint"] = sha256(
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@pytest.mark.parametrize(
    "mutation",
    ["durable_statuses", "owned_protection_count"],
)
def test_batch119_dry_run_comparator_rejects_identical_resigned_false_semantics(
    tmp_path,
    mutation,
):
    document = _valid_dry_run()
    if mutation == "durable_statuses":
        durable = document["plan"]["evidence"]["durable"]
        durable["batch_status"] = "ready"
        durable["leg_status"] = "planned"
        durable["component_statuses"] = ["pending", "pending", "pending"]
    else:
        document["plan"]["evidence"]["exchange"][
            "owned_protection_count"
        ] = 99
    _resign_evidence(document)

    result = _run_compare(tmp_path, document, deepcopy(document))

    assert result.returncode != 0
    assert json.loads(result.stdout) == {
        "reason_code": "dry_run_comparison_refused",
        "status": "refused",
    }


def test_batch119_dry_run_comparator_accepts_only_identical_safe_ready_plans(
    tmp_path,
):
    document = _valid_dry_run()

    result = _run_compare(tmp_path, document, deepcopy(document))

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "stable"}
    assert result.stderr == ""


def test_joint_runbook_keeps_batch119_captures_fresh_sequential_and_read_only():
    runbook = RUNBOOK.read_text(encoding="utf-8")
    section = runbook.split(
        "## Bound position close reservation convergence", 1
    )[1].split("## Batch 119 composite-management recovery", 1)[0]
    block = section.split("```bash", 1)[1].split("```", 1)[0]
    stopped = block.split("run_joint_stopped_phase() {", 1)[1].split(
        "\n}\n", 1
    )[0]

    assert stopped.index("run_joint_batch119_capture 1") < stopped.index(
        "run_joint_bound_close_capture 1"
    )
    assert stopped.index("run_joint_bound_close_capture 1") < stopped.index(
        "run_joint_batch119_capture 2"
    )
    assert stopped.index("run_joint_batch119_capture 2") < stopped.index(
        "run_joint_bound_close_capture 2"
    )
    assert 'research-copy-${ATTEMPT}.db' in block
    assert block.count("recover-composite-management-batch") == 1
    assert 'run_joint_batch119_capture 1' in stopped
    assert 'run_joint_batch119_capture 2' in stopped
    assert "--batch-id 119" in block
    assert "compare_batch119_dry_runs.py" in stopped
    assert "production_writes" not in stopped
    assert "--apply" not in block


def test_joint_batch119_hard_limit_covers_copy_bootstrap_and_capture():
    runbook = RUNBOOK.read_text(encoding="utf-8")
    section = runbook.split(
        "## Bound position close reservation convergence", 1
    )[1].split("## Batch 119 composite-management recovery", 1)[0]
    block = section.split("```bash", 1)[1].split("```", 1)[0]
    batch = block.split("run_joint_batch119_capture() {", 1)[1].split(
        "\n}\n", 1
    )[0]

    assert "JOINT_CAPTURE_DEADLINE_EPOCH" in batch
    assert batch.count("run_joint_capture_step_before_deadline") == 4
    assert 'sqlite3 -readonly "$PRODUCTION_DB" ".backup' in batch
    assert batch.index("run_joint_capture_step_before_deadline") < batch.index(
        'sqlite3 -readonly "$PRODUCTION_DB"'
    )
    assert batch.rindex("run_joint_capture_step_before_deadline") < batch.index(
        "recover-composite-management-batch"
    )


def test_batch119_runbook_requires_all_units_inactive_and_cleanup_failure_nonzero():
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert 'active|inactive) ;;' in runbook
    assert 'sudo systemctl stop "${QUIESCE_UNITS[@]}"' in runbook
    assert 'test "$(systemctl is-active "$UNIT" || true)" = inactive' in runbook
    assert "trap - EXIT" in runbook
    assert 'if [ "$cleanup_status" -ne 0 ]; then' in runbook
    assert 'exit "$cleanup_status"' in runbook
    assert runbook.count(
        '"${ORIGINAL_UNIT_STATE[$UNIT]}"'
    ) >= 4

    result = subprocess.run(
        [
            "bash",
            "-c",
            "cleanup() { return 7; }; "
            "finish() { local original=$?; local cleaned=0; trap - EXIT; "
            "cleanup || cleaned=$?; "
            "if [ \"$cleaned\" -ne 0 ]; then exit \"$cleaned\"; fi; "
            "exit \"$original\"; }; trap finish EXIT; true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 7


def test_batch119_runbook_handles_prior_schema_and_fetches_reviewed_branch():
    runbook = RUNBOOK.read_text(encoding="utf-8")
    diagnostic = runbook.split(
        "# 任何 durable active/unknown writer 事实都使诊断拒绝。",
        1,
    )[1].split("for ATTEMPT in 1 2; do", 1)[0]

    assert "sqlite_master" in diagnostic
    assert "deepcoin_execution_operations" in diagnostic
    assert "entry_confirmed" not in diagnostic
    assert "state IS NULL" in diagnostic
    assert "pre_submit_deferred" in diagnostic
    assert "submission_failed_no_exposure" in diagnostic
    assert (
        "git -C \"$PRODUCTION_ROOT\" fetch --no-tags origin "
        "codex/deployment-gate-batch-recovery-plan"
    ) in runbook
    assert (
        "APPROVED_REF='refs/remotes/origin/"
        "codex/deployment-gate-batch-recovery-plan'"
    ) in runbook


def test_batch119_apply_is_second_fresh_and_isolated_from_joint_artifacts():
    block = _batch119_apply_isolation_block()
    pre = block.index(
        'capture_confirmed_bound_close_population "$BATCH119_RESERVATIONS_PRE"'
    )
    first_capture = block.index("recover-composite-management-batch", pre)
    reviewed_input = block.index(
        "IFS= read -r BATCH119_EXPECTED_FINGERPRINT", first_capture
    )
    backup = block.index('sqlite3 -readonly "$PRODUCTION_DB" ".backup')
    final_pre = block.index(
        'capture_confirmed_bound_close_population '
        '"$BATCH119_RESERVATIONS_PRE_FINAL"'
    )
    pre_compare = block.index(
        'cmp -s "$BATCH119_RESERVATIONS_PRE" '
        '"$BATCH119_RESERVATIONS_PRE_FINAL"'
    )
    apply = block.index("--apply", first_capture)
    post = block.index(
        'capture_confirmed_bound_close_population "$BATCH119_RESERVATIONS_POST"'
    )
    compare = block.index(
        'cmp -s "$BATCH119_RESERVATIONS_PRE" "$BATCH119_RESERVATIONS_POST"'
    )

    assert (
        pre
        < first_capture
        < reviewed_input
        < backup
        < final_pre
        < pre_compare
        < apply
        < post
        < compare
    )
    assert block.count("recover-composite-management-batch") == 2
    assert block.count('--database-path "$PRODUCTION_DB"') == 2
    assert block.count('--generation-database-path "$PRODUCTION_DB"') == 2
    assert "I_AUTHORIZE_BATCH_119_TO_REMAINING_19" in block
    assert "I_APPROVE_BATCH119_ALL_DB_UNITS_STOPPED_APPLY_CAPTURE" in block
    assert '--expected-fingerprint "$BATCH119_EXPECTED_FINGERPRINT"' in block
    assert "--project-fingerprint \"$BATCH119_FRESH_REVIEW\"" in block
    assert 'test ! -e "$BATCH119_BACKUP_PATH"' in block
    assert 'test ! -L "$BATCH119_BACKUP_PATH"' in block
    assert 'chmod 0600 "$BATCH119_BACKUP_PATH"' in block
    assert "PRAGMA quick_check;" in block
    assert "BOUND_APPLY" not in block
    assert "joint-diagnostic" not in block
    assert "dry-run-1.json" not in block
    assert "dry-run-2.json" not in block


def _reservation_ref(row_id: int) -> str:
    encoded = json.dumps(
        {"kind": "reservation", "value": row_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _confirmed_population_database(path: Path, *, unconfirmed_index: int | None = None):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE bound_position_close_reservations (
              id INTEGER PRIMARY KEY,
              pos_id TEXT NOT NULL,
              execution_binding_id INTEGER NOT NULL,
              status TEXT NOT NULL,
              last_error TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE execution_events (
              id INTEGER PRIMARY KEY,
              action TEXT NOT NULL,
              status TEXT NOT NULL,
              order_id TEXT,
              client_order_id TEXT,
              pos_id TEXT,
              related_order_id TEXT,
              before_json TEXT,
              after_json TEXT,
              request_json TEXT,
              response_json TEXT,
              notification_fingerprint TEXT,
              notification_attempts INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        reservations = []
        for index in range(29):
            row_id = 900 + index
            status = "submitted" if index == unconfirmed_index else "confirmed"
            connection.execute(
                "INSERT INTO bound_position_close_reservations "
                "(id, pos_id, execution_binding_id, status, last_error, "
                "created_at, updated_at) VALUES (?, ?, 77, ?, NULL, ?, ?)",
                (
                    row_id,
                    f"private-position-{index}",
                    status,
                    "2026-08-16 10:00:00.000000",
                    "2026-08-16 10:05:00.000000",
                ),
            )
            reservations.append(
                {
                    "durable_invariant_fingerprint": f"{index + 1:064x}",
                    "reservation_ref": _reservation_ref(row_id),
                    "status": "confirmed",
                }
            )
        after = {
            "action_count": 29,
            "evidence_fingerprint": "e" * 64,
            "reservations": reservations,
            "status": "confirmed",
        }
        connection.execute(
            "INSERT INTO execution_events "
            "(id, action, status, order_id, client_order_id, pos_id, "
            "related_order_id, before_json, after_json, request_json, "
            "response_json, notification_fingerprint, notification_attempts, "
            "created_at) VALUES "
            "(1, 'bound_close_reservation_history_converged', 'succeeded', "
            "NULL, NULL, NULL, NULL, '{}', ?, NULL, NULL, ?, 0, ?)",
            (
                json.dumps(after, separators=(",", ":"), sort_keys=True),
                "e" * 64,
                "2026-08-16 10:05:00.000000",
            ),
        )


def _inspect_confirmed_population(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-", str(path)],
        input=_confirmed_reservation_inspector_source(),
        capture_output=True,
        text=True,
        check=False,
    )


def test_confirmed_population_inspector_is_query_only_stable_and_drift_sensitive(
    tmp_path,
):
    database = tmp_path / "confirmed.db"
    _confirmed_population_database(database)

    first = _inspect_confirmed_population(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE bound_position_close_reservations "
            "SET created_at = '2026-08-16 09:59:59.000000' WHERE id = 900"
        )
    second = _inspect_confirmed_population(database)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert set(first_payload) == {
        "material_fingerprint",
        "reservation_count",
        "schema_version",
        "status",
    }
    assert first_payload["schema_version"] == 1
    assert first_payload["status"] == "confirmed"
    assert first_payload["reservation_count"] == 29
    assert first_payload["material_fingerprint"] != second_payload[
        "material_fingerprint"
    ]


def test_confirmed_population_inspector_refuses_any_unconfirmed_target(tmp_path):
    database = tmp_path / "unconfirmed.db"
    _confirmed_population_database(database, unconfirmed_index=7)

    result = _inspect_confirmed_population(database)

    assert result.returncode != 0
    assert result.stdout == ""
    assert "private-position" not in result.stderr


def test_confirmed_population_fingerprint_binds_bound_close_audit_row(tmp_path):
    database = tmp_path / "audit-drift.db"
    _confirmed_population_database(database)
    first = _inspect_confirmed_population(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE execution_events SET before_json = '{\"drift\":true}' "
            "WHERE action = 'bound_close_reservation_history_converged'"
        )
    second = _inspect_confirmed_population(database)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout)["material_fingerprint"] != json.loads(
        second.stdout
    )["material_fingerprint"]


def test_batch119_dry_run_comparator_rejects_identical_nonabsent_empty_evidence(
    tmp_path,
):
    document = _valid_dry_run()
    document["plan"]["position"] = {
        "disposition": "protection_only_at_target",
        "current_size": "19",
        "close_delta": "0",
        "effective_remaining_size": "19",
    }
    document["plan"]["evidence"] = {}
    document["plan"]["evidence_fingerprint"] = sha256(b"{}").hexdigest()

    result = _run_compare(tmp_path, document, deepcopy(document))

    assert result.returncode != 0
    assert json.loads(result.stdout)["status"] == "refused"


@pytest.mark.parametrize(
    "mutation",
    [
        "outer_key",
        "plan_key",
        "status",
        "disposition",
        "writes",
        "calls",
        "fingerprint",
        "semantic_drift",
        "duplicate_key",
        "deep",
        "oversize",
    ],
)
def test_batch119_dry_run_comparator_fails_closed_without_echoing_payload(
    tmp_path,
    mutation,
):
    hostile = "sensitive-provider-identity-123456789"
    left = _valid_dry_run()
    right: object = deepcopy(left)
    if mutation == "outer_key":
        right["unsafe"] = hostile
    elif mutation == "plan_key":
        right["plan"]["unsafe"] = hostile
    elif mutation == "status":
        right["plan"]["status"] = "refused"
    elif mutation == "disposition":
        right["plan"]["position"]["disposition"] = "unknown"
    elif mutation == "writes":
        right["plan"]["production_writes"] = 1
    elif mutation == "calls":
        right["plan"]["exchange_calls"] = 1
    elif mutation == "fingerprint":
        right["plan"]["source_fingerprint"] = hostile
    elif mutation == "semantic_drift":
        right["plan"]["evidence_fingerprint"] = "f" * 64
    elif mutation == "duplicate_key":
        right = (
            '{"mode":"dry_run","mode":"dry_run","unsafe":"'
            + hostile
            + '"}'
        )
    elif mutation == "deep":
        nested: object = hostile
        for _ in range(70):
            nested = [nested]
        right["plan"]["evidence"] = nested
    elif mutation == "oversize":
        right["plan"]["evidence"] = {"unsafe": hostile * 40_000}
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(mutation)

    result = _run_compare(tmp_path, left, right)

    assert result.returncode != 0
    assert json.loads(result.stdout) == {
        "reason_code": "dry_run_comparison_refused",
        "status": "refused",
    }
    assert result.stderr == ""
    assert hostile not in result.stdout
