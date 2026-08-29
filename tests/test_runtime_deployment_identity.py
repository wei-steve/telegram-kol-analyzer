from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from telegram_kol_research.runtime_deployment_identity import (
    RuntimeAuthorityStatus,
    build_runtime_deployment_identity,
)


class RunningTask:
    def done(self) -> bool:
        return False

    def cancelled(self) -> bool:
        return False


def test_worker_identity_proves_loaded_release_and_direct_authority_tasks(
    tmp_path: Path,
) -> None:
    commit = "a" * 40
    release = tmp_path / commit
    module = release / "src/telegram_kol_research/web_app.py"
    module.parent.mkdir(parents=True)
    module.write_text("# loaded module\n", encoding="utf-8")
    manifest = {
        "commit": commit,
        "contract": "immutable-release-v1",
        "schema_version": 1,
    }
    manifest_bytes = (
        json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    (release / ".telegram-kol-release.json").write_bytes(manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    task = RunningTask()

    payload = build_runtime_deployment_identity(
        runtime_role="worker",
        module_path=module,
        expected_commit=commit,
        expected_manifest_sha256=manifest_sha,
        tasks={
            "strategy_management_worker": task,
            "break_even_convergence_worker": task,
            "deepcoin_reconcile": task,
            "lifecycle_monitor": task,
            "worker_command_worker": task,
        },
        authority_snapshot={
            "management": True,
            "protection": True,
            "close": True,
            "tpsl": True,
            "rescue": True,
        },
        process_start_ticks=1234,
        entry_admission_frozen=True,
        authority_evidence={
            "management_cycle": {"fresh": True, "age_seconds": 0.0},
        },
        now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
    )

    assert payload["loaded_artifact_verified"] is True
    assert payload["release_commit"] == commit
    assert payload["manifest_sha256"] == manifest_sha
    assert payload["process_start_ticks"] == 1234
    assert payload["entry_admission_frozen"] is True
    assert payload["health"]["message_processing"] is False
    assert payload["authority_evidence"] == {
        "management_cycle": {"fresh": True, "age_seconds": 0.0},
    }
    assert payload["capabilities"] == {
        "global_exchange_authority": True,
        "management": True,
        "protection": True,
        "close": True,
        "tpsl": True,
        "rescue": True,
    }


def test_identity_fails_closed_when_env_claim_does_not_match_loaded_module(
    tmp_path: Path,
) -> None:
    module = tmp_path / "checkout/src/telegram_kol_research/web_app.py"
    module.parent.mkdir(parents=True)
    module.write_text("# checkout module\n", encoding="utf-8")

    payload = build_runtime_deployment_identity(
        runtime_role="worker",
        module_path=module,
        expected_commit="a" * 40,
        expected_manifest_sha256="b" * 64,
        tasks={},
        process_start_ticks=1234,
        now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
    )

    assert payload["loaded_artifact_verified"] is False
    assert not any(payload["capabilities"].values())


def test_authority_status_requires_fresh_successful_cycles_and_live_modes() -> None:
    status = RuntimeAuthorityStatus(max_age_seconds=90)
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

    status.record_management_cycle(
        management_enabled=True,
        rescue_enabled=True,
        observed_at=now,
    )
    status.record_break_even_cycle(observed_at=now)
    status.record_reconcile_cycle(observed_at=now)

    assert status.snapshot(now=now) == {
        "management": True,
        "protection": True,
        "close": True,
        "tpsl": True,
        "rescue": True,
    }
    status.record_reconcile_failure(observed_at=now)
    assert status.snapshot(now=now)["protection"] is False
    assert status.snapshot(now=now)["rescue"] is False
    assert not any(
        status.snapshot(now=datetime(2026, 8, 28, 12, 2, tzinfo=UTC)).values()
    )
    assert status.evidence(now=now)["reconcile_cycle"] == {
        "age_seconds": 0.0,
        "fresh": True,
        "successful": False,
    }
    assert status.evidence(
        now=datetime(2026, 8, 28, 12, 2, tzinfo=UTC)
    )["management_cycle"]["fresh"] is False


def test_disabled_effective_management_never_claims_authority() -> None:
    status = RuntimeAuthorityStatus(max_age_seconds=90)
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

    status.record_management_cycle(
        management_enabled=False,
        rescue_enabled=False,
        observed_at=now,
    )
    status.record_break_even_cycle(observed_at=now)
    status.record_reconcile_cycle(observed_at=now)

    assert not any(status.snapshot(now=now).values())


def test_identity_reports_loaded_cwd_command_role_and_independent_entry_gate(
    tmp_path: Path,
) -> None:
    commit = "c" * 40
    release = tmp_path / commit
    module = release / "src/telegram_kol_research/web_app.py"
    module.parent.mkdir(parents=True)
    module.write_text("# immutable module\n", encoding="utf-8")
    manifest = {
        "commit": commit,
        "contract": "immutable-release-v1",
        "schema_version": 1,
    }
    encoded = (
        json.dumps(manifest, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()
    (release / ".telegram-kol-release.json").write_bytes(encoded)
    manifest_sha = hashlib.sha256(encoded).hexdigest()

    payload = build_runtime_deployment_identity(
        runtime_role="worker",
        command_role="worker",
        loaded_cwd="/opt/telegram-kol-analyzer",
        module_path=module,
        expected_commit=commit,
        expected_manifest_sha256=manifest_sha,
        tasks={},
        authority_snapshot={
            "management": True,
            "protection": True,
            "close": True,
            "tpsl": True,
            "rescue": True,
        },
        process_start_ticks=4321,
        entry_admission_frozen=True,
        now=datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
    )

    assert payload["loaded_cwd"] == "/opt/telegram-kol-analyzer"
    assert payload["command_role"] == "worker"
    assert payload["entry_admission_frozen"] is True
    assert not any(payload["capabilities"].values())
