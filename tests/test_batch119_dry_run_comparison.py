from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "compare_batch119_dry_runs.py"


def _valid_dry_run() -> dict[str, object]:
    return {
        "mode": "dry_run",
        "plan": {
            "batch_id": 119,
            "status": "ready",
            "reason_code": "false_legacy_submission_proven",
            "position": {
                "disposition": "position_absent",
                "current_size": "0",
                "close_delta": "0",
                "effective_remaining_size": "0",
            },
            "source_fingerprint": "a" * 64,
            "exchange_snapshot_fingerprint": "b" * 64,
            "evidence_fingerprint": "c" * 64,
            "evidence": {
                "exact_scope_fingerprint": "d" * 64,
                "natural_stop_fingerprint": "e" * 64,
            },
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

