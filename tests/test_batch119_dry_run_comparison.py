from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "compare_batch119_dry_runs.py"


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
            "component_statuses": ["pending", "pending", "pending"],
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
            "exact_position_count": 0,
            "regular_close_evidence_count": 0,
            "owned_protection_count": 3,
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
        paths.append(path)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(path) for path in paths)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_batch119_dry_run_comparator_accepts_only_identical_safe_ready_plans(
    tmp_path,
):
    document = _valid_dry_run()

    result = _run_compare(tmp_path, document, deepcopy(document))

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"status": "stable"}
    assert result.stderr == ""


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
