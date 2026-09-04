from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import telegram_kol_research.scoped_release_activation as scoped_activation

from telegram_kol_research.scoped_release_activation import (
    ActivationError,
    ActivationPaths,
    action_plan_sha256,
    activate_release,
    exclusive_runtime_control_lock,
    render_release_dropin,
    SystemRuntimeAdapter,
)
from telegram_kol_research.deployment_action_plan import parse_manifest


CANDIDATE = "2" * 40
ROLLBACK = "1" * 40
OTHER = "3" * 40
CONTROLLER = "4" * 40
MONITOR_ROLLBACK = "5" * 40


def _monitor_diagnostic_payload(*, release_commit=CANDIDATE, **overrides):
    payload = {
        "adapter_failures": [],
        "audit_ran": False,
        "checked_at": "2026-08-31T01:00:00+00:00",
        "contract": "monitor-deployment-diagnostic-v1",
        "details": {
            "entry_preamble_invariant_codes": ["stale_entry_preamble_unresolved"]
        },
        "healthy": False,
        "loaded_artifact_verified": True,
        "manifest_sha256": "a" * 64,
        "monitor_error": None,
        "notification_status": "disabled",
        "reason_codes": ["stale_entry_preamble_unresolved"],
        "release_commit": release_commit,
        "result_complete": True,
        "schema_version": 1,
        "sources_complete": True,
    }
    payload.update(overrides)
    return payload


def test_runtime_control_lock_is_shared_and_nonblocking(tmp_path) -> None:
    lock_path = tmp_path / "runtime-control.lock"

    with exclusive_runtime_control_lock(
        lock_path=lock_path,
        expected_uid=lock_path.parent.stat().st_uid,
    ):
        with pytest.raises(ActivationError, match="runtime control is locked"):
            with exclusive_runtime_control_lock(
                lock_path=lock_path,
                expected_uid=lock_path.parent.stat().st_uid,
            ):
                raise AssertionError("contended lock must not be entered")


def _canonical(payload: dict) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def test_bootstrap_can_render_canonical_entry_frozen_release_dropin() -> None:
    release = type(
        "Release",
        (),
        {
            "release_path": Path("/opt/telegram-kol-releases/candidate"),
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()
    rendered = render_release_dropin(
        release,
        component="worker",
        entry_frozen=True,
    )

    assert f'TELEGRAM_KOL_RELEASE_COMMIT={CANDIDATE}' in rendered
    assert 'TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN=1' in rendered
    assert 'Environment="PYTHONDONTWRITEBYTECODE=1"' in rendered

    monitor = render_release_dropin(
        release,
        component="monitor",
        entry_frozen=True,
    )
    assert "TELEGRAM_KOL_MONITOR_RELEASE_PATH=" not in monitor
    assert "TELEGRAM_KOL_MONITOR_RELEASE_COMMIT=" not in monitor
    assert "TELEGRAM_KOL_MONITOR_RELEASE_MANIFEST_SHA256=" not in monitor
    assert (
        'Environment="PYTHONPATH=/opt/telegram-kol-releases/candidate/src"'
        in monitor
    )


def test_system_runtime_allows_first_authority_cycles_to_finish_before_retry() -> None:
    assert SystemRuntimeAdapter.identity_retry_delay_seconds == 60


def test_runtime_support_digest_allows_only_canonical_monitor_execstart_migration(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy"
    canonical = tmp_path / "canonical"
    changed = tmp_path / "changed"
    comment_legacy = tmp_path / "comment-legacy"
    comment_canonical = tmp_path / "comment-canonical"
    for root in (legacy, canonical, changed, comment_legacy, comment_canonical):
        unit_dir = root / "deploy/systemd"
        unit_dir.mkdir(parents=True)
        (root / "config").mkdir()
        (root / "config/groups.yaml").write_text("groups: []\n", encoding="utf-8")
    legacy_command = (
        "ExecStart=/usr/bin/env "
        "PYTHONPATH=${TELEGRAM_KOL_MONITOR_RELEASE_PATH}/src "
        "/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research "
        "monitor-production-safety --notify\n"
    )
    canonical_command = (
        "ExecStart=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research "
        "monitor-production-safety --notify\n"
    )
    (legacy / "deploy/systemd/telegram-kol-monitor.service").write_text(
        "[Service]\n" + legacy_command + "NoNewPrivileges=true\n",
        encoding="utf-8",
    )
    (canonical / "deploy/systemd/telegram-kol-monitor.service").write_text(
        "[Service]\n" + canonical_command + "NoNewPrivileges=true\n",
        encoding="utf-8",
    )
    (changed / "deploy/systemd/telegram-kol-monitor.service").write_text(
        "[Service]\n" + canonical_command + "NoNewPrivileges=false\n",
        encoding="utf-8",
    )
    (comment_legacy / "deploy/systemd/telegram-kol-monitor.service").write_text(
        "[Service]\n# " + legacy_command,
        encoding="utf-8",
    )
    (comment_canonical / "deploy/systemd/telegram-kol-monitor.service").write_text(
        "[Service]\n# " + canonical_command,
        encoding="utf-8",
    )

    assert scoped_activation._runtime_support_digest(
        legacy
    ) == scoped_activation._runtime_support_digest(canonical)
    assert scoped_activation._runtime_support_digest(
        canonical
    ) != scoped_activation._runtime_support_digest(changed)
    assert scoped_activation._runtime_support_digest(
        comment_legacy
    ) != scoped_activation._runtime_support_digest(comment_canonical)


@pytest.mark.parametrize(
    "diagnostic_offset_ns,manifest_sha256,expected_pass",
    (
        (1_000_000_000, "a" * 64, True),
        (0, "a" * 64, False),
        (-1, "a" * 64, False),
        (1_000_000_000, "b" * 64, False),
    ),
    ids=("fresh", "equal-mtime", "stale", "manifest-mismatch"),
)
def test_monitor_rollback_identity_requires_matching_diagnostic_newer_than_config(
    tmp_path: Path,
    monkeypatch,
    diagnostic_offset_ns: int,
    manifest_sha256: str,
    expected_pass: bool,
) -> None:
    runtime = SystemRuntimeAdapter(
        python=Path("/venv/python"),
        expected_uid=tmp_path.stat().st_uid,
    )
    release_path = tmp_path / CANDIDATE
    release_path.mkdir()
    release = type(
        "Release",
        (),
        {
            "release_path": release_path,
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()
    environment_file = tmp_path / "monitor.env"
    environment_file.write_text("MONITOR_POLICY=live\n", encoding="utf-8")
    environment_file.chmod(0o600)
    config_mtime_ns = 1_800_000_000_000_000_000
    unit_files: dict[str, tuple[Path, Path]] = {}
    for index, unit in enumerate(
        (*scoped_activation._UNITS["monitor"], "telegram-kol-monitor.timer")
    ):
        fragment = tmp_path / f"fragment-{index}.service"
        dropin = tmp_path / f"dropin-{index}.conf"
        fragment.write_text("[Unit]\n", encoding="utf-8")
        dropin.write_text("[Service]\n", encoding="utf-8")
        os.utime(fragment, ns=(config_mtime_ns, config_mtime_ns))
        os.utime(dropin, ns=(config_mtime_ns, config_mtime_ns))
        unit_files[unit] = (fragment, dropin)
    checked_at = datetime.fromtimestamp(
        (config_mtime_ns + diagnostic_offset_ns) / 1_000_000_000,
        tz=UTC,
    ).isoformat()
    payload = _monitor_diagnostic_payload(
        checked_at=checked_at,
        manifest_sha256=manifest_sha256,
    )

    def run(command, **kwargs):
        stdout = ""
        if command[0:2] == ["systemctl", "show"] and "--property=Environment" in command:
            unit = command[2]
            fragment, dropin = unit_files[unit]
            if unit in scoped_activation._UNITS["monitor"]:
                environment = " ".join(
                    (
                        f"PYTHONPATH={release_path}/src",
                        f"TELEGRAM_KOL_RELEASE_COMMIT={CANDIDATE}",
                        f"TELEGRAM_KOL_RELEASE_MANIFEST_SHA256={'a' * 64}",
                    )
                )
                stdout = (
                    f"Environment={environment}\n"
                    f"EnvironmentFiles={environment_file} (ignore_errors=no)\n"
                    "ExecStart={ path=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research ; "
                    "argv[]=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research "
                    "monitor-production-safety ; }\n"
                )
            stdout += f"FragmentPath={fragment}\nDropInPaths={dropin}\n"
        elif command == [
            "systemctl",
            "show",
            "telegram-kol-monitor-diagnostic.service",
            "--property=Result",
            "--property=ExecMainStatus",
        ]:
            stdout = "Result=success\nExecMainStatus=0\n"
        elif command[0] == "journalctl":
            stdout = json.dumps(payload) + "\n"
        return scoped_activation.subprocess.CompletedProcess(
            command, 0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)

    if expected_pass:
        evidence = runtime.prove_monitor_rollback_release(release)
        assert evidence["release_commit"] == CANDIDATE
        assert evidence["latest_configuration_mtime_ns"] == config_mtime_ns
    else:
        with pytest.raises(ActivationError, match="monitor rollback identity proof failed"):
            runtime.prove_monitor_rollback_release(release)


@pytest.mark.parametrize(
    "missing_environment_unit,missing_fragment_unit,timer_environment,expected_pass",
    (
        (scoped_activation._UNITS["monitor"][0], None, False, False),
        (None, scoped_activation._UNITS["monitor"][0], False, False),
        (None, scoped_activation._UNITS["monitor"][1], False, False),
        (None, scoped_activation._UNITS["monitor"][2], False, False),
        (None, "telegram-kol-monitor.timer", False, False),
        (None, None, True, True),
    ),
    ids=(
        "service-missing-environment",
        "monitor-service-missing-fragment",
        "diagnostic-service-missing-fragment",
        "test-notification-service-missing-fragment",
        "timer-missing-fragment",
        "timer-extra-environment-accepted",
    ),
)
def test_monitor_rollback_identity_requires_unit_type_specific_systemd_properties(
    tmp_path: Path,
    monkeypatch,
    missing_environment_unit: str | None,
    missing_fragment_unit: str | None,
    timer_environment: bool,
    expected_pass: bool,
) -> None:
    runtime = SystemRuntimeAdapter(
        python=Path("/venv/python"),
        expected_uid=tmp_path.stat().st_uid,
    )
    release_path = tmp_path / CANDIDATE
    release_path.mkdir()
    release = type(
        "Release",
        (),
        {
            "release_path": release_path,
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()
    environment_file = tmp_path / "monitor-properties.env"
    environment_file.write_text("MONITOR_POLICY=live\n", encoding="utf-8")
    environment_file.chmod(0o600)
    config_mtime_ns = 1_800_000_000_000_000_000
    unit_files: dict[str, tuple[Path, Path]] = {}
    units = (*scoped_activation._UNITS["monitor"], "telegram-kol-monitor.timer")
    for index, unit in enumerate(units):
        fragment = tmp_path / f"property-fragment-{index}"
        dropin = tmp_path / f"property-dropin-{index}.conf"
        fragment.write_text("[Unit]\n", encoding="utf-8")
        dropin.write_text("[Service]\n", encoding="utf-8")
        os.utime(fragment, ns=(config_mtime_ns, config_mtime_ns))
        os.utime(dropin, ns=(config_mtime_ns, config_mtime_ns))
        unit_files[unit] = (fragment, dropin)
    payload = _monitor_diagnostic_payload(
        checked_at=datetime.fromtimestamp(
            (config_mtime_ns + 1_000_000_000) / 1_000_000_000,
            tz=UTC,
        ).isoformat()
    )

    def run(command, **kwargs):
        stdout = ""
        if command[0:2] == ["systemctl", "show"] and "--property=Environment" in command:
            unit = command[2]
            fragment, dropin = unit_files[unit]
            lines = []
            if unit in scoped_activation._UNITS["monitor"]:
                if unit != missing_environment_unit:
                    lines.append(
                        "Environment="
                        + " ".join(
                            (
                                f"TELEGRAM_KOL_RELEASE_COMMIT={CANDIDATE}",
                                f"TELEGRAM_KOL_RELEASE_MANIFEST_SHA256={'a' * 64}",
                                f"PYTHONPATH={release_path}/src",
                            )
                        )
                    )
                    lines.append(
                        f"EnvironmentFiles={environment_file} (ignore_errors=no)"
                    )
                    lines.append(
                        "ExecStart={ path=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research ; "
                        "argv[]=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research "
                        "monitor-production-safety ; }"
                    )
            elif timer_environment:
                lines.append("Environment=IGNORED_FOR_PROCESSLESS_TIMER=1")
            if unit != missing_fragment_unit:
                lines.append(f"FragmentPath={fragment}")
            lines.append(f"DropInPaths={dropin}")
            stdout = "\n".join(lines) + "\n"
        elif command == [
            "systemctl",
            "show",
            "telegram-kol-monitor-diagnostic.service",
            "--property=Result",
            "--property=ExecMainStatus",
        ]:
            stdout = "Result=success\nExecMainStatus=0\n"
        elif command[0] == "journalctl":
            stdout = json.dumps(payload) + "\n"
        return scoped_activation.subprocess.CompletedProcess(
            command, 0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)

    if expected_pass:
        assert runtime.prove_monitor_rollback_release(release)["release_commit"] == CANDIDATE
    else:
        with pytest.raises(ActivationError, match="monitor rollback identity proof failed"):
            runtime.prove_monitor_rollback_release(release)


@pytest.mark.parametrize(
    "journal_payload,unsafe_config",
    (
        (None, False),
        (
            _monitor_diagnostic_payload(
                checked_at="2027-01-01T00:00:00+00:00",
                result_complete=False,
            ),
            False,
        ),
        (
            _monitor_diagnostic_payload(
                checked_at="2027-01-01T00:00:00+00:00",
            ),
            True,
        ),
    ),
    ids=("missing-diagnostic", "incomplete-diagnostic", "unsafe-config-path"),
)
def test_monitor_rollback_identity_fails_closed_on_incomplete_evidence(
    tmp_path: Path,
    monkeypatch,
    journal_payload: dict | None,
    unsafe_config: bool,
) -> None:
    runtime = SystemRuntimeAdapter(
        python=Path("/venv/python"),
        expected_uid=tmp_path.stat().st_uid,
    )
    release_path = tmp_path / CANDIDATE
    release_path.mkdir()
    release = type(
        "Release",
        (),
        {
            "release_path": release_path,
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()
    environment_file = tmp_path / "monitor-incomplete.env"
    environment_file.write_text("MONITOR_POLICY=live\n", encoding="utf-8")
    environment_file.chmod(0o600)
    unit_files: dict[str, tuple[Path, Path]] = {}
    for index, unit in enumerate(
        (*scoped_activation._UNITS["monitor"], "telegram-kol-monitor.timer")
    ):
        fragment = tmp_path / f"fragment-incomplete-{index}.service"
        dropin = tmp_path / f"dropin-incomplete-{index}.conf"
        fragment.write_text("[Unit]\n", encoding="utf-8")
        dropin.write_text("[Service]\n", encoding="utf-8")
        unit_files[unit] = (fragment, dropin)
    if unsafe_config:
        fragment, dropin = unit_files[scoped_activation._UNITS["monitor"][0]]
        fragment.unlink()
        fragment.symlink_to(dropin)

    def run(command, **kwargs):
        stdout = ""
        if command[0:2] == ["systemctl", "show"] and "--property=Environment" in command:
            unit = command[2]
            fragment, dropin = unit_files[unit]
            environment = ""
            if unit in scoped_activation._UNITS["monitor"]:
                environment = " ".join(
                    (
                        f"PYTHONPATH={release_path}/src",
                        f"TELEGRAM_KOL_RELEASE_COMMIT={CANDIDATE}",
                        f"TELEGRAM_KOL_RELEASE_MANIFEST_SHA256={'a' * 64}",
                    )
                )
            stdout = (
                f"Environment={environment}\n"
                + (
                    f"EnvironmentFiles={environment_file} (ignore_errors=no)\n"
                    "ExecStart={ path=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research ; "
                    "argv[]=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research "
                    "monitor-production-safety ; }\n"
                    if unit in scoped_activation._UNITS["monitor"]
                    else ""
                )
                +
                f"FragmentPath={fragment}\n"
                f"DropInPaths={dropin}\n"
            )
        elif command[0:2] == ["systemctl", "show"]:
            stdout = "Result=success\nExecMainStatus=0\n"
        elif command[0] == "journalctl" and journal_payload is not None:
            stdout = json.dumps(journal_payload) + "\n"
        return scoped_activation.subprocess.CompletedProcess(
            command, 0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)

    with pytest.raises(ActivationError, match="monitor rollback identity proof failed"):
        runtime.prove_monitor_rollback_release(release)


@pytest.mark.parametrize(
    "case,expected_pass",
    (
        ("clean", True),
        ("generic-current-matches-candidate", True),
        ("retired-env-path", False),
        ("retired-env-commit", False),
        ("retired-env-manifest", False),
        ("missing-environment", False),
        ("missing-environment-files", False),
        ("missing-environment-file", False),
        ("missing-pythonpath", False),
        ("missing-release-commit", False),
        ("missing-manifest-sha256", False),
        ("legacy-execstart", False),
    ),
)
def test_monitor_candidate_main_import_proof_uses_effective_systemd_sources(
    tmp_path: Path,
    monkeypatch,
    case: str,
    expected_pass: bool,
) -> None:
    runtime = SystemRuntimeAdapter(
        python=Path("/venv/python"),
        expected_uid=tmp_path.stat().st_uid,
    )
    release_path = tmp_path / CANDIDATE
    release_path.mkdir()
    release = type(
        "Release",
        (),
        {
            "release_path": release_path,
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()
    current_matches_candidate = case == "generic-current-matches-candidate"
    current_path = release_path if current_matches_candidate else tmp_path / ROLLBACK
    if current_path != release_path:
        current_path.mkdir()
    environment_file = tmp_path / "telegram-kol-monitor.env"
    environment_file.write_text("MONITOR_POLICY=live\n", encoding="utf-8")
    environment_file.chmod(0o600)
    retired_cases = {
        "retired-env-path": (
            "TELEGRAM_KOL_MONITOR_RELEASE_PATH",
            str(current_path),
            "PYTHONPATH",
            f"{release_path}/src",
        ),
        "retired-env-commit": (
            "TELEGRAM_KOL_MONITOR_RELEASE_COMMIT",
            ROLLBACK,
            "TELEGRAM_KOL_RELEASE_COMMIT",
            CANDIDATE,
        ),
        "retired-env-manifest": (
            "TELEGRAM_KOL_MONITOR_RELEASE_MANIFEST_SHA256",
            "b" * 64,
            "TELEGRAM_KOL_RELEASE_MANIFEST_SHA256",
            "a" * 64,
        ),
    }
    if case in retired_cases:
        retired_key, retired_value, _, _ = retired_cases[case]
        environment_file.write_text(
            f"{retired_key}={retired_value}\n",
            encoding="utf-8",
        )
    if case == "missing-environment-file":
        environment_file.unlink()

    def run(command, **kwargs):
        if command[0:2] != ["systemctl", "show"]:
            raise AssertionError(command)
        environment = " ".join(
            (
                f"PYTHONPATH={current_path}/src",
                "TELEGRAM_KOL_RELEASE_COMMIT="
                f"{CANDIDATE if current_matches_candidate else ROLLBACK}",
                "TELEGRAM_KOL_RELEASE_MANIFEST_SHA256="
                f"{'a' * 64 if current_matches_candidate else 'b' * 64}",
            )
        )
        if case == "missing-pythonpath":
            environment = " ".join(
                item for item in environment.split() if not item.startswith("PYTHONPATH=")
            )
        if case == "missing-release-commit":
            environment = " ".join(
                item
                for item in environment.split()
                if not item.startswith("TELEGRAM_KOL_RELEASE_COMMIT=")
            )
        if case == "missing-manifest-sha256":
            environment = " ".join(
                item
                for item in environment.split()
                if not item.startswith("TELEGRAM_KOL_RELEASE_MANIFEST_SHA256=")
            )
        if case in retired_cases:
            retired_key, retired_value, _, _ = retired_cases[case]
            environment += f" {retired_key}={retired_value}"
        exec_start = (
            "{ path=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research ; "
            "argv[]=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research "
            "monitor-production-safety --deployment-diagnostic ; }"
        )
        if case == "legacy-execstart":
            exec_start = (
                "{ path=/usr/bin/env ; argv[]=/usr/bin/env "
                "PYTHONPATH=${TELEGRAM_KOL_MONITOR_RELEASE_PATH}/src "
                "/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research "
                "monitor-production-safety ; }"
            )
        lines = [
            f"Environment={environment}",
            f"EnvironmentFiles={environment_file} (ignore_errors=no)",
            f"ExecStart={exec_start}",
            f"FragmentPath={tmp_path / command[2]}",
            "DropInPaths=",
        ]
        if case == "missing-environment":
            lines.pop(0)
        if case == "missing-environment-files":
            lines.pop(1)
        return scoped_activation.subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(lines) + "\n",
            stderr="",
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)

    if expected_pass:
        evidence = runtime.prove_monitor_candidate_release(release)
        assert evidence["release_commit"] == CANDIDATE
        assert evidence["main_pythonpath"] == f"{release_path}/src"
    else:
        with pytest.raises(ActivationError, match="monitor candidate main import") as error:
            runtime.prove_monitor_candidate_release(release)
        if case in retired_cases:
            retired_key, retired_value, prospective_key, prospective_value = (
                retired_cases[case]
            )
            assert (
                "source=EnvironmentFile "
                f"key={retired_key} observed={retired_value}"
            ) in str(error.value)
            assert (
                "source=prospective_dropin "
                f"key={prospective_key} observed={prospective_value}"
                in str(error.value)
            )


def test_monitor_release_proof_allows_success_when_journal_evidence_is_unavailable(
    monkeypatch,
) -> None:
    runtime = SystemRuntimeAdapter(python=Path("/venv/python"))

    def run(command, **kwargs):
        if command == [
            "systemctl",
            "show",
            "telegram-kol-monitor-diagnostic.service",
            "--property=Result",
            "--property=ExecMainStatus",
        ]:
            return scoped_activation.subprocess.CompletedProcess(
                command,
                0,
                stdout="Result=success\nExecMainStatus=0\n",
                stderr="",
            )
        if command == [
            "journalctl",
            "-u",
            "telegram-kol-monitor-diagnostic.service",
            "--lines=100",
            "--output=cat",
            "--no-pager",
        ]:
            return scoped_activation.subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="journal unavailable",
            )
        return scoped_activation.subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)
    release = type(
        "Release",
        (),
        {
            "release_path": Path("/opt/telegram-kol-releases/candidate"),
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()

    evidence = runtime.verify_monitor_release(release)

    assert evidence["decision"] == {
        "basis": "systemctl_start_and_systemd_result_exec_main_status",
        "exec_main_status": "0",
        "passed": True,
        "start_returncode": 0,
        "systemd_result": "success",
    }
    assert evidence["journal_evidence"] == {
        "reason": "journal_command_nonzero",
        "returncode": 1,
        "status": "unavailable",
    }


def test_monitor_release_proof_allows_success_when_journal_decoding_fails(
    monkeypatch,
) -> None:
    runtime = SystemRuntimeAdapter(python=Path("/venv/python"))

    def run(command, **kwargs):
        if command == [
            "systemctl",
            "show",
            "telegram-kol-monitor-diagnostic.service",
            "--property=Result",
            "--property=ExecMainStatus",
        ]:
            return scoped_activation.subprocess.CompletedProcess(
                command,
                0,
                stdout="Result=success\nExecMainStatus=0\n",
                stderr="",
            )
        if command[0] == "journalctl":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        return scoped_activation.subprocess.CompletedProcess(
            command, 0, stdout="", stderr=""
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)
    release = type(
        "Release",
        (),
        {
            "release_path": Path("/opt/telegram-kol-releases/candidate"),
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()

    evidence = runtime.verify_monitor_release(release)

    assert evidence["decision"]["passed"] is True
    assert evidence["journal_evidence"] == {
        "reason": "journal_evidence_exception",
        "status": "unavailable",
    }


def test_monitor_release_proof_records_identity_precheck_failure(
    monkeypatch, capsys
) -> None:
    runtime = SystemRuntimeAdapter(python=Path("/venv/python"))

    def run(command, **kwargs):
        return scoped_activation.subprocess.CompletedProcess(
            command,
            1 if command[-1] == "telegram_kol_research.runtime_deployment_identity" else 0,
            stdout="",
            stderr="failed",
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)
    release = type(
        "Release",
        (),
        {
            "release_path": Path("/opt/telegram-kol-releases/candidate"),
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()

    with pytest.raises(ActivationError, match="runtime command failed"):
        runtime.verify_monitor_release(release)

    evidence = json.loads(capsys.readouterr().err.strip().partition("=")[2])
    assert evidence["decision"] == {
        "basis": "runtime_identity_precheck",
        "exec_main_status": None,
        "passed": False,
        "reason": "runtime_identity_precheck_failed",
        "start_returncode": None,
        "systemd_result": None,
    }


def test_monitor_release_proof_records_latest_payload_without_correlation_gate(
    monkeypatch, capsys
) -> None:
    runtime = SystemRuntimeAdapter(python=Path("/venv/python"))
    calls = []
    earlier = _monitor_diagnostic_payload(release_commit=OTHER)
    latest = _monitor_diagnostic_payload()

    def run(command, **kwargs):
        calls.append(command)
        stdout = ""
        if command == [
            "systemctl",
            "show",
            "telegram-kol-monitor-diagnostic.service",
            "--property=Result",
            "--property=ExecMainStatus",
        ]:
            stdout = "Result=success\nExecMainStatus=0\n"
        elif command[0] == "journalctl":
            stdout = json.dumps(earlier) + "\n" + json.dumps(latest) + "\n"
        return scoped_activation.subprocess.CompletedProcess(
            command, 0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)
    release = type(
        "Release",
        (),
        {
            "release_path": Path("/opt/telegram-kol-releases/candidate"),
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()

    evidence = runtime.verify_monitor_release(release)

    assert evidence["decision"]["passed"] is True
    assert evidence["journal_evidence"] == {
        "association": "best_effort_latest_unit_payload",
        "observed_payload_count": 2,
        "payload": latest,
        "status": "available",
    }
    assert not any("--show-transaction" in command for command in calls)
    assert not any("--show-cursor" in command for command in calls)
    assert not any(
        any(part.startswith("--after-cursor=") for part in command)
        for command in calls
    )
    emitted = capsys.readouterr().err.strip()
    assert emitted.startswith("MONITOR_ACTIVATION_GATE_EVIDENCE=")
    assert json.loads(emitted.partition("=")[2]) == evidence


@pytest.mark.parametrize(
    "journal_output,expected_status",
    [
        ("", "unavailable"),
        ("not-json\n", "unavailable"),
        (
            json.dumps(_monitor_diagnostic_payload(release_commit=OTHER))
            + "\n"
            + json.dumps({"contract": "monitor-deployment-diagnostic-v1"})
            + "\n",
            "available",
        ),
    ],
    ids=("missing", "malformed", "duplicate_or_wrong"),
)
def test_monitor_release_proof_never_uses_journal_payload_as_a_gate(
    monkeypatch, journal_output, expected_status
) -> None:
    runtime = SystemRuntimeAdapter(python=Path("/venv/python"))

    def run(command, **kwargs):
        stdout = ""
        if command == [
            "systemctl",
            "show",
            "telegram-kol-monitor-diagnostic.service",
            "--property=Result",
            "--property=ExecMainStatus",
        ]:
            stdout = "Result=success\nExecMainStatus=0\n"
        elif command[0] == "journalctl":
            stdout = journal_output
        return scoped_activation.subprocess.CompletedProcess(
            command, 0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)
    release = type(
        "Release",
        (),
        {
            "release_path": Path("/opt/telegram-kol-releases/candidate"),
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()

    evidence = runtime.verify_monitor_release(release)

    assert evidence["decision"]["passed"] is True
    assert evidence["journal_evidence"]["status"] == expected_status


@pytest.mark.parametrize(
    "systemd_result,exec_main_status",
    [("exit-code", "1"), ("signal", "15")],
    ids=("nonzero", "signal"),
)
def test_monitor_release_proof_rejects_systemd_exit_status_and_records_actuals(
    monkeypatch, capsys, systemd_result, exec_main_status
) -> None:
    runtime = SystemRuntimeAdapter(python=Path("/venv/python"))

    def run(command, **kwargs):
        stdout = ""
        if command == [
            "systemctl",
            "show",
            "telegram-kol-monitor-diagnostic.service",
            "--property=Result",
            "--property=ExecMainStatus",
        ]:
            stdout = (
                f"Result={systemd_result}\n"
                f"ExecMainStatus={exec_main_status}\n"
            )
        return scoped_activation.subprocess.CompletedProcess(
            command, 0, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)
    release = type(
        "Release",
        (),
        {
            "release_path": Path("/opt/telegram-kol-releases/candidate"),
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()

    with pytest.raises(ActivationError, match="monitor runtime proof failed"):
        runtime.verify_monitor_release(release)

    emitted = capsys.readouterr().err.strip()
    evidence = json.loads(emitted.partition("=")[2])
    assert evidence["decision"] == {
        "basis": "systemctl_start_and_systemd_result_exec_main_status",
        "exec_main_status": exec_main_status,
        "passed": False,
        "reason": "diagnostic_exit_failed",
        "start_returncode": 0,
        "systemd_result": systemd_result,
    }


@pytest.mark.parametrize(
    "failure_mode", ["timeout", "decode_error", "nonzero", "signal"]
)
def test_monitor_release_proof_rejects_real_runner_failure_modes(
    monkeypatch, capsys, failure_mode
) -> None:
    runtime = SystemRuntimeAdapter(python=Path("/venv/python"))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command == [
            "systemctl",
            "start",
            "telegram-kol-monitor-diagnostic.service",
        ]:
            if failure_mode == "timeout":
                raise scoped_activation.subprocess.TimeoutExpired(
                    command,
                    kwargs["timeout"],
                )
            if failure_mode == "decode_error":
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
            returncode = 1 if failure_mode == "nonzero" else -15
            return scoped_activation.subprocess.CompletedProcess(
                command,
                returncode,
                stdout="",
                stderr="failed",
            )
        if command == [
            "systemctl",
            "show",
            "telegram-kol-monitor-diagnostic.service",
            "--property=Result",
            "--property=ExecMainStatus",
        ]:
            systemd_result = "signal" if failure_mode == "signal" else "exit-code"
            exec_main_status = "15" if failure_mode == "signal" else "1"
            return scoped_activation.subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    f"Result={systemd_result}\n"
                    f"ExecMainStatus={exec_main_status}\n"
                ),
                stderr="",
            )
        return scoped_activation.subprocess.CompletedProcess(
            command, 0, stdout="", stderr=""
        )

    monkeypatch.setattr(scoped_activation.subprocess, "run", run)
    release = type(
        "Release",
        (),
        {
            "release_path": Path("/opt/telegram-kol-releases/candidate"),
            "commit": CANDIDATE,
            "manifest_sha256": "a" * 64,
        },
    )()

    with pytest.raises(ActivationError, match="runtime command failed"):
        runtime.verify_monitor_release(release)
    start_calls = [
        kwargs
        for command, kwargs in calls
        if command
        == [
            "systemctl",
            "start",
            "telegram-kol-monitor-diagnostic.service",
        ]
    ]
    assert len(start_calls) == 1
    assert start_calls[0]["timeout"] == 45
    evidence = json.loads(capsys.readouterr().err.strip().partition("=")[2])
    assert evidence["decision"]["reason"] == "diagnostic_start_failed"
    assert evidence["decision"]["passed"] is False
    assert evidence["decision"]["systemd_status_observation"] == "available"
    if failure_mode == "signal":
        assert evidence["decision"]["start_observation"] == "nonzero_or_signal"
        assert evidence["decision"]["start_returncode"] == -15
        assert evidence["decision"]["systemd_result"] == "signal"
        assert evidence["decision"]["exec_main_status"] == "15"
    elif failure_mode == "nonzero":
        assert evidence["decision"]["start_observation"] == "nonzero_or_signal"
        assert evidence["decision"]["start_returncode"] == 1
        assert evidence["decision"]["systemd_result"] == "exit-code"
        assert evidence["decision"]["exec_main_status"] == "1"
    elif failure_mode == "timeout":
        assert evidence["decision"]["start_observation"] == "timeout"
        assert evidence["decision"]["start_returncode"] is None
    else:
        assert evidence["decision"]["start_observation"] == "unavailable"
        assert evidence["decision"]["start_returncode"] is None


def _content_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if path.is_dir() or relative in {
            ".telegram-kol-release.json",
            ".telegram-kol-stage-receipt.json",
        }:
            continue
        encoded = relative.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(b"x" if metadata.st_mode & 0o111 else b"-")
        digest.update(metadata.st_size.to_bytes(8, "big"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _write_release(
    root: Path,
    commit: str,
    *,
    component: str = "web",
    components: list[str] | None = None,
    extra_files: dict[str, str] | None = None,
) -> Path:
    release = root / commit
    source = release / "src/telegram_kol_research"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text(f"COMMIT = {commit!r}\n", encoding="utf-8")
    for relative, content in (extra_files or {}).items():
        destination = release / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    declared_components = components or [component]
    action_manifest = {
        "action": "stage",
        "authority_changed": bool(
            set(declared_components) & {"ingest", "worker"}
        ),
        "components": declared_components,
        "exchange_write_semantics_changed": False,
        "production_data_mutation": False,
        "requires_restart": True,
        "risk_level": "L2",
        "schema_changed": False,
    }
    content_sha = _content_digest(release)
    stage_plan_sha = action_plan_sha256(parse_manifest(action_manifest))
    manifest = {
        "action_manifest": action_manifest,
        "action_plan_sha256": stage_plan_sha,
        "branch": "codex/test",
        "commit": commit,
        "content_sha256": content_sha,
        "contract": "immutable-release-v1",
        "schema_version": 1,
        "tree": "b" * 40,
    }
    manifest_bytes = _canonical(manifest)
    receipt = {
        "action_plan_sha256": stage_plan_sha,
        "branch": "codex/test",
        "commit": commit,
        "content_sha256": content_sha,
        "contract": "immutable-release-v1",
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "release_name": commit,
        "schema_version": 1,
        "status": "staged",
        "tree": "b" * 40,
    }
    (release / ".telegram-kol-release.json").write_bytes(manifest_bytes)
    (release / ".telegram-kol-stage-receipt.json").write_bytes(_canonical(receipt))
    for path in sorted(release.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    release.chmod(0o555)
    return release


class FakeRuntime:
    def __init__(self, *, current_commit: str, authority_ok: bool = True):
        self.current_commit_by_role = {
            role: current_commit for role in ("web", "ingest", "worker")
        }
        self.authority_ok = authority_ok
        self.events: list[str] = []
        self.active_write_results = [0, 0]
        self.manifest_by_commit: dict[str, str] = {}
        self.start_ticks_by_role = {
            "web": 9000,
            "ingest": 9001,
            "worker": 9002,
        }
        self.entry_frozen_by_role = {
            role: False for role in ("web", "ingest", "worker")
        }
        self.maintenance_state_by_unit = {
            unit: ("active", "enabled")
            for unit in (
                "telegram-kol-web.service",
                "telegram-kol-ingest.service",
                "telegram-kol-worker.service",
                "telegram-kol-monitor.timer",
                "telegram-kol-monitor.service",
                "telegram-kol-monitor-diagnostic.service",
                "telegram-kol-monitor-test-notification.service",
                "telegram-kol.service",
            )
        }
        self.main_pid_by_unit = {
            unit: 0 for unit in self.maintenance_state_by_unit
        }
        self.cgroup_pids_by_unit = {
            unit: () for unit in self.maintenance_state_by_unit
        }
        self.matching_runtime_pids: tuple[int, ...] = ()
        self.stop_fail_units: set[str] = set()
        self.mask_fail_units: set[str] = set()
        self.database_write_count = 0
        self.exchange_write_count = 0

    def active_write_count(self, database_path: Path) -> int:
        self.events.append("active-write")
        return self.active_write_results.pop(0)

    def runtime_identity(self, role: str) -> dict:
        current_commit = self.current_commit_by_role[role]
        self.events.append(f"identity:{role}:{current_commit}")
        capabilities = {
            "global_exchange_authority": role == "worker" and self.authority_ok,
            "management": role == "worker" and self.authority_ok,
            "protection": role == "worker" and self.authority_ok,
            "close": role == "worker" and self.authority_ok,
            "tpsl": role == "worker" and self.authority_ok,
            "rescue": role == "worker" and self.authority_ok,
        }
        return {
            "contract": "runtime-deployment-identity-v1",
            "loaded_artifact_verified": True,
            "manifest_sha256": self.manifest_by_commit[current_commit],
            "pid": 100 + {"web": 0, "ingest": 1, "worker": 2}[role],
            "process_start_ticks": self.start_ticks_by_role[role],
            "systemd_main_pid": 100 + {"web": 0, "ingest": 1, "worker": 2}[role],
            "systemd_start_ticks": self.start_ticks_by_role[role],
            "release_commit": current_commit,
            "runtime_role": role,
            "observed_at": datetime.now(UTC).isoformat(),
            "health": {
                "event_loop": True,
                "ingest_live_listener": role == "ingest",
                "ingest_reconcile": role == "ingest",
                "worker_command": role == "worker",
                "message_processing": (
                    role == "worker" and not self.entry_frozen_by_role[role]
                ),
            },
            "entry_admission_frozen": self.entry_frozen_by_role[role],
            "authority_evidence": {
                "max_age_seconds": 90.0,
                "management_cycle": {
                    "age_seconds": 0.0,
                    "effective_management_enabled": self.authority_ok,
                    "effective_rescue_enabled": self.authority_ok,
                    "fresh": self.authority_ok,
                    "successful": self.authority_ok,
                },
                "break_even_cycle": {
                    "age_seconds": 0.0,
                    "fresh": self.authority_ok,
                    "successful": self.authority_ok,
                },
                "reconcile_cycle": {
                    "age_seconds": 0.0,
                    "fresh": self.authority_ok,
                    "successful": self.authority_ok,
                },
            },
            "capabilities": capabilities,
        }

    def stop_unit(self, unit: str) -> None:
        self.events.append(f"stop:{unit}")
        if unit in self.stop_fail_units:
            raise ActivationError(f"stop failed: {unit}")
        _, enabled_state = self.maintenance_state_by_unit[unit]
        self.maintenance_state_by_unit[unit] = ("inactive", enabled_state)
        self.main_pid_by_unit[unit] = 0
        self.cgroup_pids_by_unit[unit] = ()

    def start_unit(self, unit: str) -> None:
        self.events.append(f"start:{unit}")
        _, enabled_state = self.maintenance_state_by_unit[unit]
        self.maintenance_state_by_unit[unit] = ("active", enabled_state)
        role = unit.removeprefix("telegram-kol-").removesuffix(".service")
        if role not in self.current_commit_by_role:
            return
        pid = 100 + {"web": 0, "ingest": 1, "worker": 2}[role]
        self.main_pid_by_unit[unit] = pid
        self.cgroup_pids_by_unit[unit] = (pid,)
        self.start_ticks_by_role[role] += 10
        dropin = Path(self.dropin_root) / f"{unit}.d/10-telegram-kol-release.conf"
        text = dropin.read_text(encoding="utf-8")
        self.entry_frozen_by_role[role] = (
            'Environment="TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN=1"' in text
        )
        for commit in self.manifest_by_commit:
            if commit in text:
                self.current_commit_by_role[role] = commit

    def daemon_reload(self) -> None:
        self.events.append("daemon-reload")

    def maintenance_unit_state(self, unit: str) -> tuple[str, str]:
        self.events.append(f"maintenance-state:{unit}")
        return self.maintenance_state_by_unit[unit]

    def unmask_unit(self, unit: str) -> None:
        self.events.append(f"unmask:{unit}")
        active_state, _ = self.maintenance_state_by_unit[unit]
        self.maintenance_state_by_unit[unit] = (active_state, "enabled")

    def mask_unit(self, unit: str) -> None:
        self.events.append(f"mask:{unit}")
        if unit in self.mask_fail_units:
            raise ActivationError(f"mask failed: {unit}")
        self.maintenance_state_by_unit[unit] = ("inactive", "inhibited")

    def main_pid(self, unit: str) -> int:
        self.events.append(f"main-pid:{unit}")
        return self.main_pid_by_unit[unit]

    def cgroup_pids(self, unit: str) -> tuple[int, ...]:
        self.events.append(f"cgroup-pids:{unit}")
        return self.cgroup_pids_by_unit[unit]

    def matching_processes(self) -> tuple[int, ...]:
        self.events.append("matching-processes")
        return self.matching_runtime_pids

    def monitor_timer_active(self) -> bool:
        return True

    def prove_monitor_rollback_release(self, release):
        self.events.append(f"monitor-rollback-identity:{release.commit}")
        return {
            "contract": "monitor-rollback-identity-v1",
            "release_commit": release.commit,
            "manifest_sha256": release.manifest_sha256,
        }

    def prove_monitor_candidate_release(self, release):
        self.events.append(f"monitor-candidate-identity:{release.commit}")
        return {
            "contract": "monitor-candidate-main-identity-v1",
            "release_commit": release.commit,
            "manifest_sha256": release.manifest_sha256,
        }

    def verify_monitor_release(self, release):
        self.events.append(f"monitor-identity:{release.commit}")
        return {
            "contract": "monitor-activation-gate-evidence-v1",
            "decision": {
                "basis": "systemctl_start_and_systemd_result_exec_main_status",
                "exec_main_status": "0",
                "passed": True,
                "systemd_result": "success",
            },
            "journal_evidence": {
                "association": "best_effort_latest_unit_payload",
                "observed_payload_count": 1,
                "payload": _monitor_diagnostic_payload(
                    release_commit=release.commit
                ),
                "status": "available",
            },
            "schema_version": 1,
        }


@pytest.fixture
def activation_harness(tmp_path: Path):
    release_root = tmp_path / "releases"
    release_root.mkdir()
    _write_release(release_root, ROLLBACK)
    _write_release(release_root, CANDIDATE)
    manifest_path = tmp_path / "activate.json"
    manifest = {
        "action": "activate",
        "risk_level": "L2",
        "components": ["web"],
        "requires_restart": True,
        "schema_changed": False,
        "production_data_mutation": False,
        "exchange_write_semantics_changed": False,
        "authority_changed": False,
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    authorization = tmp_path / "authorization.json"
    parsed = parse_manifest(manifest)
    now = datetime.now(UTC)
    authorization.write_bytes(
        _canonical(
            {
                "contract": "scoped-activation-authorization-v2",
                "schema_version": 2,
                "commit": CANDIDATE,
                "components": ["web"],
                "source_mode": "immutable",
                "action_plan_sha256": action_plan_sha256(parsed),
                "nonce": "c" * 64,
                "issued_at": (now - timedelta(seconds=1)).isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
            }
        )
    )
    authorization.chmod(0o400)
    paths = ActivationPaths(
        release_root=release_root,
        action_manifest=manifest_path,
        authorization=authorization,
        authorization_consumed=tmp_path / "authorization.used.json",
        dropin_root=tmp_path / "systemd",
        database_path=tmp_path / "research.db",
    )
    paths.database_path.write_bytes(b"not-used-for-web")
    runtime = FakeRuntime(current_commit=ROLLBACK)
    runtime.dropin_root = paths.dropin_root
    runtime.manifest_by_commit = {
        commit: json.loads(
            (release_root / commit / ".telegram-kol-stage-receipt.json").read_text(
                encoding="utf-8"
            )
        )["manifest_sha256"]
        for commit in (ROLLBACK, CANDIDATE)
    }
    return paths, runtime, manifest


def _set_authorization_source_mode(paths: ActivationPaths, source_mode: str) -> None:
    payload = json.loads(paths.authorization.read_text(encoding="utf-8"))
    payload["source_mode"] = source_mode
    paths.authorization.chmod(0o600)
    paths.authorization.write_bytes(_canonical(payload))
    paths.authorization.chmod(0o400)


def _split_runtime_activation_harness(
    tmp_path: Path,
) -> tuple[ActivationPaths, FakeRuntime, dict[str, dict[str, str]]]:
    release_root = tmp_path / "releases"
    release_root.mkdir()
    for commit in (ROLLBACK, OTHER, MONITOR_ROLLBACK, CANDIDATE):
        _write_release(
            release_root,
            commit,
            components=["web", "monitor", "ingest", "worker"],
        )
    manifests = {
        commit: json.loads(
            (release_root / commit / ".telegram-kol-stage-receipt.json").read_text(
                encoding="utf-8"
            )
        )["manifest_sha256"]
        for commit in (ROLLBACK, OTHER, MONITOR_ROLLBACK, CANDIDATE)
    }
    rollback_releases = {
        "web": {"commit": OTHER, "manifest_sha256": manifests[OTHER]},
        "monitor": {
            "commit": MONITOR_ROLLBACK,
            "manifest_sha256": manifests[MONITOR_ROLLBACK],
        },
        "ingest": {"commit": ROLLBACK, "manifest_sha256": manifests[ROLLBACK]},
        "worker": {"commit": ROLLBACK, "manifest_sha256": manifests[ROLLBACK]},
    }
    manifest = {
        "action": "activate",
        "authority_changed": True,
        "components": ["web", "monitor", "ingest", "worker"],
        "exchange_write_semantics_changed": False,
        "production_data_mutation": False,
        "requires_restart": True,
        "risk_level": "L2",
        "rollback_releases": rollback_releases,
        "schema_changed": False,
    }
    manifest_path = tmp_path / "activate.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    parsed = parse_manifest(manifest)
    now = datetime.now(UTC)
    authorization = tmp_path / "authorization.json"
    authorization.write_bytes(
        _canonical(
            {
                "action_plan_sha256": action_plan_sha256(parsed),
                "commit": CANDIDATE,
                "components": ["web", "monitor", "ingest", "worker"],
                "contract": "scoped-activation-authorization-v3",
                "controller_bundle_sha256": "d" * 64,
                "controller_commit": CONTROLLER,
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
                "issued_at": (now - timedelta(seconds=1)).isoformat(),
                "nonce": "c" * 64,
                "rollback_releases": rollback_releases,
                "schema_version": 3,
                "source_mode": "immutable",
            }
        )
    )
    authorization.chmod(0o400)
    paths = ActivationPaths(
        release_root=release_root,
        action_manifest=manifest_path,
        authorization=authorization,
        authorization_consumed=tmp_path / "authorization.used.json",
        dropin_root=tmp_path / "systemd",
        database_path=tmp_path / "research.db",
    )
    paths.database_path.write_bytes(b"not-used")
    runtime = FakeRuntime(current_commit=ROLLBACK)
    runtime.current_commit_by_role["web"] = OTHER
    runtime.dropin_root = paths.dropin_root
    runtime.manifest_by_commit = manifests
    return paths, runtime, rollback_releases


def test_legacy_single_rollback_rejects_split_runtime_authority_state(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    runtime.current_commit_by_role["web"] = OTHER
    runtime.manifest_by_commit[OTHER] = "e" * 64

    with pytest.raises(ActivationError, match="runtime identity proof failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("stop:") for event in runtime.events)


def test_split_runtime_authority_activation_accepts_bound_per_role_rollbacks(
    tmp_path: Path,
) -> None:
    paths, runtime, rollback_releases = _split_runtime_activation_harness(tmp_path)

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit="",
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
        controller_commit=CONTROLLER,
        controller_bundle_sha256="d" * 64,
    )

    assert result["status"] == "activated"
    assert result["rollback_releases"] == rollback_releases


def test_legacy_single_rollback_result_contract_does_not_gain_v3_fields(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit=ROLLBACK,
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
    )

    assert "rollback_releases" not in result
    assert result["rollback_commit"] == ROLLBACK


@pytest.mark.parametrize(
    "field,value",
    (
        ("controller_commit", ROLLBACK),
        ("controller_bundle_sha256", "e" * 64),
        (
            "rollback_releases",
            {
                "web": {"commit": ROLLBACK, "manifest_sha256": "f" * 64},
                "monitor": {"commit": OTHER, "manifest_sha256": "f" * 64},
                "ingest": {"commit": ROLLBACK, "manifest_sha256": "f" * 64},
                "worker": {"commit": ROLLBACK, "manifest_sha256": "f" * 64},
            },
        ),
    ),
)
def test_per_role_authorization_binds_map_and_controller_identity(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    paths, runtime, _ = _split_runtime_activation_harness(tmp_path)
    authorization = json.loads(paths.authorization.read_text(encoding="utf-8"))
    authorization[field] = value
    paths.authorization.chmod(0o600)
    paths.authorization.write_bytes(_canonical(authorization))
    paths.authorization.chmod(0o400)

    with pytest.raises(ActivationError, match="authorization is invalid"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit="",
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            controller_commit=CONTROLLER,
            controller_bundle_sha256="d" * 64,
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("stop:") for event in runtime.events)


def test_per_role_manifest_digest_mismatch_fails_before_authorization(
    tmp_path: Path,
) -> None:
    paths, runtime, rollback_releases = _split_runtime_activation_harness(tmp_path)
    declared = json.loads(paths.action_manifest.read_text(encoding="utf-8"))
    rollback_releases["web"]["manifest_sha256"] = "f" * 64
    declared["rollback_releases"] = rollback_releases
    paths.action_manifest.write_text(json.dumps(declared), encoding="utf-8")
    authorization = json.loads(paths.authorization.read_text(encoding="utf-8"))
    authorization["rollback_releases"] = rollback_releases
    authorization["action_plan_sha256"] = action_plan_sha256(parse_manifest(declared))
    paths.authorization.chmod(0o600)
    paths.authorization.write_bytes(_canonical(authorization))
    paths.authorization.chmod(0o400)

    with pytest.raises(ActivationError, match="manifest does not match declaration"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit="",
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            controller_commit=CONTROLLER,
            controller_bundle_sha256="d" * 64,
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("stop:") for event in runtime.events)


def test_arbitrary_valid_per_role_release_cannot_replace_observed_runtime(
    tmp_path: Path,
) -> None:
    paths, runtime, _ = _split_runtime_activation_harness(tmp_path)
    runtime.current_commit_by_role["web"] = ROLLBACK

    with pytest.raises(ActivationError, match="runtime identity proof failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit="",
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            controller_commit=CONTROLLER,
            controller_bundle_sha256="d" * 64,
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("stop:") for event in runtime.events)


def test_monitor_observed_release_mismatch_fails_before_service_control(
    tmp_path: Path,
) -> None:
    paths, runtime, _ = _split_runtime_activation_harness(tmp_path)

    def reject_monitor_identity(release) -> dict:
        raise ActivationError("monitor rollback identity proof failed")

    runtime.prove_monitor_rollback_release = reject_monitor_identity

    with pytest.raises(ActivationError, match="monitor rollback identity proof failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit="",
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            controller_commit=CONTROLLER,
            controller_bundle_sha256="d" * 64,
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("stop:") for event in runtime.events)


def test_per_role_dry_run_executes_authority_and_monitor_gates_without_mutation(
    tmp_path: Path,
) -> None:
    paths, runtime, rollback_releases = _split_runtime_activation_harness(tmp_path)

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit="",
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
        controller_commit=CONTROLLER,
        controller_bundle_sha256="d" * 64,
        dry_run=True,
    )

    assert result["status"] == "validated"
    assert result["rollback_releases"] == rollback_releases
    assert runtime.events.count("active-write") == 1
    assert f"monitor-rollback-identity:{MONITOR_ROLLBACK}" in runtime.events
    assert f"monitor-candidate-identity:{CANDIDATE}" in runtime.events
    assert all(
        any(event.startswith(f"identity:{role}:") for event in runtime.events)
        for role in ("web", "ingest", "worker")
    )
    assert not any(
        event.startswith(("stop:", "start:", "monitor-identity:"))
        or event == "daemon-reload"
        for event in runtime.events
    )
    assert paths.authorization.exists()
    assert not paths.authorization_consumed.exists()


def test_per_role_dry_run_rejects_monitor_main_import_from_stale_environment_file(
    tmp_path: Path,
) -> None:
    paths, runtime, _ = _split_runtime_activation_harness(tmp_path)

    def reject_split_main_identity(release) -> dict:
        raise ActivationError(
            "monitor candidate main import conflict: "
            f"dropin={release.commit} environment_file={MONITOR_ROLLBACK}"
        )

    runtime.prove_monitor_candidate_release = reject_split_main_identity

    with pytest.raises(
        ActivationError,
        match=(
            "monitor candidate main import conflict: "
            f"dropin={CANDIDATE} environment_file={MONITOR_ROLLBACK}"
        ),
    ):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit="",
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            controller_commit=CONTROLLER,
            controller_bundle_sha256="d" * 64,
            dry_run=True,
        )

    assert paths.authorization.exists()
    assert not paths.authorization_consumed.exists()
    assert "active-write" not in runtime.events
    assert not any(
        event.startswith(("stop:", "start:")) or event == "daemon-reload"
        for event in runtime.events
    )


def test_per_role_rollback_restores_each_component_to_its_bound_release(
    tmp_path: Path,
) -> None:
    paths, runtime, _ = _split_runtime_activation_harness(tmp_path)
    original_identity = runtime.runtime_identity

    def fail_candidate_worker_identity(role: str) -> dict:
        payload = original_identity(role)
        if role == "worker" and payload["release_commit"] == CANDIDATE:
            payload["loaded_artifact_verified"] = False
        return payload

    runtime.runtime_identity = fail_candidate_worker_identity

    with pytest.raises(ActivationError, match="rollback_complete"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit="",
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            controller_commit=CONTROLLER,
            controller_bundle_sha256="d" * 64,
        )

    assert runtime.current_commit_by_role == {
        "web": OTHER,
        "ingest": ROLLBACK,
        "worker": ROLLBACK,
    }
    monitor_dropin = (
        paths.dropin_root
        / "telegram-kol-monitor.service.d/10-telegram-kol-release.conf"
    ).read_text(encoding="utf-8")
    assert MONITOR_ROLLBACK in monitor_dropin
    assert CANDIDATE not in monitor_dropin


def test_release_tree_change_after_preflight_stops_without_starting_any_unit(
    tmp_path: Path,
) -> None:
    paths, runtime, _ = _split_runtime_activation_harness(tmp_path)
    original_reload = runtime.daemon_reload

    def corrupt_rollback_after_publication() -> None:
        original_reload()
        release = paths.release_root / OTHER
        release.chmod(0o755)
        source = release / "src/telegram_kol_research/__init__.py"
        source.chmod(0o644)
        source.write_text("corrupt\n", encoding="utf-8")

    runtime.daemon_reload = corrupt_rollback_after_publication

    with pytest.raises(ActivationError, match="rollback_failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit="",
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            controller_commit=CONTROLLER,
            controller_bundle_sha256="d" * 64,
        )

    assert not any(event.startswith("start:") for event in runtime.events)
    assert all(
        runtime.maintenance_state_by_unit[unit][0] == "inactive"
        for unit in scoped_activation._controlled_units(
            ["web", "monitor", "ingest", "worker"]
        )
    )


def test_web_activation_consumes_verified_receipt_and_restarts_only_web(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit=ROLLBACK,
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
    )

    assert result["status"] == "activated"
    assert result["components"] == ["web"]
    assert not paths.authorization.exists()
    assert paths.authorization_consumed.exists()
    assert "active-write" not in runtime.events
    assert [event for event in runtime.events if event.startswith(("stop:", "start:"))] == [
        "stop:telegram-kol-web.service",
        "start:telegram-kol-web.service",
    ]


def test_dry_run_validates_without_consuming_authorization_or_controlling_services(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit=ROLLBACK,
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
        dry_run=True,
    )

    assert result["status"] == "validated"
    assert result["authorization_consumed"] is False
    assert paths.authorization.exists()
    assert not paths.authorization_consumed.exists()
    assert not paths.dropin_root.exists()
    assert not any(
        event.startswith(("stop:", "start:")) or event == "daemon-reload"
        for event in runtime.events
    )


def test_web_only_activation_preserves_existing_entry_freeze(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    runtime.entry_frozen_by_role["web"] = True

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit=ROLLBACK,
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
    )

    assert result["status"] == "activated"
    assert runtime.entry_frozen_by_role["web"] is True
    dropin = (
        paths.dropin_root
        / "telegram-kol-web.service.d/10-telegram-kol-release.conf"
    ).read_text(encoding="utf-8")
    assert 'Environment="TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN=1"' in dropin


def test_corrupt_release_fails_before_authorization_or_service_control(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    candidate_file = paths.release_root / CANDIDATE / "src/telegram_kol_research/__init__.py"
    candidate_file.chmod(0o644)
    candidate_file.write_text("corrupt\n", encoding="utf-8")
    candidate_file.chmod(0o444)

    with pytest.raises(ActivationError, match="release validation failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert runtime.events == []


def test_non_source_revision_change_is_rejected_before_authorization(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    candidate = paths.release_root / CANDIDATE
    candidate.chmod(0o755)
    for child in candidate.rglob("*"):
        child.chmod(0o755 if child.is_dir() else 0o644)
    for child in sorted(candidate.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    candidate.rmdir()
    _write_release(
        paths.release_root,
        CANDIDATE,
        extra_files={"config/groups.yaml": "changed: true\n"},
    )

    with pytest.raises(ActivationError, match="release scope validation failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert runtime.events == []


def test_test_only_revision_change_is_outside_runtime_support_scope(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    candidate = paths.release_root / CANDIDATE
    candidate.chmod(0o755)
    for child in candidate.rglob("*"):
        child.chmod(0o755 if child.is_dir() else 0o644)
    for child in sorted(candidate.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if child.is_file():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    candidate.rmdir()
    _write_release(
        paths.release_root,
        CANDIDATE,
        extra_files={"tests/test_candidate_only.py": "def test_candidate(): pass\n"},
    )
    runtime.manifest_by_commit[CANDIDATE] = json.loads(
        (candidate / ".telegram-kol-stage-receipt.json").read_text(encoding="utf-8")
    )["manifest_sha256"]

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit=ROLLBACK,
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
    )

    assert result["status"] == "activated"


def test_systemd_pid_start_identity_mismatch_fails_before_authorization(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    original_identity = runtime.runtime_identity

    def mismatched_identity(role: str) -> dict:
        payload = original_identity(role)
        payload["systemd_start_ticks"] += 1
        return payload

    runtime.runtime_identity = mismatched_identity

    with pytest.raises(ActivationError, match="runtime identity proof failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("stop:") for event in runtime.events)


def test_stale_runtime_identity_fails_closed_without_retrying_service_control(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    original_identity = runtime.runtime_identity

    def stale_identity(role: str) -> dict:
        payload = original_identity(role)
        payload["observed_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
        return payload

    runtime.runtime_identity = stale_identity

    with pytest.raises(ActivationError, match="runtime identity proof failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("stop:") for event in runtime.events)


def test_noncanonical_authorization_fails_before_service_control(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    payload = json.loads(paths.authorization.read_text(encoding="utf-8"))
    paths.authorization.chmod(0o600)
    paths.authorization.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    paths.authorization.chmod(0o400)

    with pytest.raises(ActivationError, match="authorization is invalid"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("stop:") for event in runtime.events)


def _configure_worker_harness(paths, runtime, manifest) -> None:
    for commit in (ROLLBACK, CANDIDATE):
        release = paths.release_root / commit
        release.chmod(0o755)
        for child in release.rglob("*"):
            child.chmod(0o755 if child.is_dir() else 0o644)
        for child in sorted(release.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if child.is_file():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        release.rmdir()
        _write_release(
            paths.release_root,
            commit,
            components=["web", "monitor", "ingest", "worker"],
        )
    manifest.update(
        {
            "components": ["web", "monitor", "ingest", "worker"],
            "authority_changed": True,
        }
    )
    paths.action_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    authorization = json.loads(paths.authorization.read_text(encoding="utf-8"))
    authorization["components"] = ["web", "monitor", "ingest", "worker"]
    authorization["action_plan_sha256"] = action_plan_sha256(parse_manifest(manifest))
    paths.authorization.chmod(0o600)
    paths.authorization.write_bytes(_canonical(authorization))
    paths.authorization.chmod(0o400)
    runtime.manifest_by_commit = {
        commit: json.loads(
            (paths.release_root / commit / ".telegram-kol-stage-receipt.json").read_text(
                encoding="utf-8"
            )
        )["manifest_sha256"]
        for commit in (ROLLBACK, CANDIDATE)
    }


def test_authority_activation_freezes_all_entry_runtimes_and_checks_quiescence(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit=ROLLBACK,
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
    )

    assert f"monitor-rollback-identity:{ROLLBACK}" in runtime.events
    assert f"monitor-candidate-identity:{CANDIDATE}" in runtime.events

    assert result["status"] == "activated"
    monitor_verification = result["monitor_verification"]
    assert monitor_verification["decision"]["passed"] is True
    monitor_payload = monitor_verification["journal_evidence"]["payload"]
    assert monitor_payload["healthy"] is False
    assert monitor_payload["reason_codes"] == [
        "stale_entry_preamble_unresolved"
    ]
    assert monitor_payload["details"] == {
        "entry_preamble_invariant_codes": ["stale_entry_preamble_unresolved"]
    }
    assert runtime.current_commit_by_role == {
        "web": CANDIDATE,
        "ingest": CANDIDATE,
        "worker": CANDIDATE,
    }
    assert f"monitor-identity:{CANDIDATE}" in runtime.events
    assert runtime.events.count("active-write") == 2
    assert all(runtime.entry_frozen_by_role.values())
    for role in ("web", "ingest", "worker"):
        dropin = (
            paths.dropin_root
            / f"telegram-kol-{role}.service.d/10-telegram-kol-release.conf"
        ).read_text(encoding="utf-8")
        assert (
            'Environment="TELEGRAM_KOL_DEPLOYMENT_ENTRY_FROZEN=1"'
            in dropin
        )
    assert [event for event in runtime.events if event.startswith(("stop:", "start:"))] == [
        "stop:telegram-kol-ingest.service",
        "stop:telegram-kol-web.service",
        "stop:telegram-kol-worker.service",
        "stop:telegram-kol-monitor.timer",
        "stop:telegram-kol-monitor.service",
        "stop:telegram-kol-monitor-diagnostic.service",
        "stop:telegram-kol-monitor-test-notification.service",
        "start:telegram-kol-worker.service",
        "start:telegram-kol-web.service",
        "start:telegram-kol-ingest.service",
        "start:telegram-kol-monitor.timer",
    ]


def test_stopped_legacy_activation_does_not_require_legacy_runtime_identity(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    _set_authorization_source_mode(paths, "stopped_legacy")
    runtime.maintenance_state_by_unit = {
        unit: ("inactive", "inhibited")
        for unit in runtime.maintenance_state_by_unit
    }

    immutable_identity = runtime.runtime_identity

    def legacy_identity_is_unavailable(role: str) -> dict:
        if runtime.current_commit_by_role[role] == ROLLBACK:
            raise ActivationError(f"legacy {role} has no identity endpoint")
        return immutable_identity(role)

    runtime.runtime_identity = legacy_identity_is_unavailable

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit=ROLLBACK,
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
        source_mode="stopped_legacy",
    )

    assert result["status"] == "activated"
    assert result["source_mode"] == "stopped_legacy"
    assert {
        event for event in runtime.events if event.startswith("unmask:")
    } == {
        f"unmask:{unit}"
        for unit in runtime.maintenance_state_by_unit
        if unit != "telegram-kol.service"
    }
    assert "maintenance-state:telegram-kol.service" in runtime.events
    assert "main-pid:telegram-kol.service" in runtime.events
    assert "cgroup-pids:telegram-kol.service" in runtime.events
    assert "unmask:telegram-kol.service" not in runtime.events
    assert "start:telegram-kol.service" not in runtime.events
    assert all(runtime.entry_frozen_by_role.values())
    assert runtime.maintenance_state_by_unit["telegram-kol-monitor.timer"][0] == "active"


def test_stopped_legacy_dry_run_rejects_monitor_candidate_main_identity_conflict(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    _set_authorization_source_mode(paths, "stopped_legacy")
    runtime.maintenance_state_by_unit = {
        unit: ("inactive", "inhibited")
        for unit in runtime.maintenance_state_by_unit
    }

    def reject_candidate_identity(release) -> dict:
        raise ActivationError(
            f"monitor candidate main import conflict: candidate={release.commit}"
        )

    runtime.prove_monitor_candidate_release = reject_candidate_identity

    with pytest.raises(
        ActivationError,
        match=f"monitor candidate main import conflict: candidate={CANDIDATE}",
    ):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            source_mode="stopped_legacy",
            dry_run=True,
        )

    assert paths.authorization.exists()
    assert not paths.authorization_consumed.exists()
    assert "active-write" not in runtime.events
    assert not any(
        event.startswith(("unmask:", "start:", "stop:"))
        or event == "daemon-reload"
        for event in runtime.events
    )


def test_stopped_legacy_candidate_protection_failure_ends_maintenance_stopped(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    _set_authorization_source_mode(paths, "stopped_legacy")
    runtime.maintenance_state_by_unit = {
        unit: ("inactive", "inhibited")
        for unit in runtime.maintenance_state_by_unit
    }
    immutable_identity = runtime.runtime_identity

    def candidate_worker_protection_fails(role: str) -> dict:
        payload = immutable_identity(role)
        if role == "worker" and runtime.current_commit_by_role[role] == CANDIDATE:
            payload["capabilities"]["protection"] = False
        return payload

    runtime.runtime_identity = candidate_worker_protection_fails

    with pytest.raises(ActivationError, match="maintenance_stopped"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            source_mode="stopped_legacy",
        )

    assert set(runtime.maintenance_state_by_unit.values()) == {
        ("inactive", "inhibited")
    }
    assert set(runtime.main_pid_by_unit.values()) == {0}
    assert set(runtime.cgroup_pids_by_unit.values()) == {()}
    assert "start:telegram-kol.service" not in runtime.events
    assert runtime.current_commit_by_role == {
        "web": CANDIDATE,
        "ingest": CANDIDATE,
        "worker": CANDIDATE,
    }
    assert runtime.database_write_count == 0
    assert runtime.exchange_write_count == 0
    assert paths.authorization_consumed.exists()


def test_stopped_legacy_candidate_identity_unknown_is_retried_once(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    _set_authorization_source_mode(paths, "stopped_legacy")
    runtime.maintenance_state_by_unit = {
        unit: ("inactive", "inhibited")
        for unit in runtime.maintenance_state_by_unit
    }
    immutable_identity = runtime.runtime_identity
    candidate_worker_attempts = 0

    def first_candidate_worker_identity_is_unknown(role: str) -> dict:
        nonlocal candidate_worker_attempts
        if role == "worker" and runtime.current_commit_by_role[role] == CANDIDATE:
            candidate_worker_attempts += 1
            if candidate_worker_attempts == 1:
                raise ActivationError("candidate worker identity unknown")
        return immutable_identity(role)

    runtime.runtime_identity = first_candidate_worker_identity_is_unknown

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit="",
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
        source_mode="stopped_legacy",
    )

    assert result["status"] == "activated"
    assert candidate_worker_attempts == 2
    assert runtime.database_write_count == 0
    assert runtime.exchange_write_count == 0


def test_stopped_legacy_activation_does_not_require_rollback_release(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    _set_authorization_source_mode(paths, "stopped_legacy")
    runtime.maintenance_state_by_unit = {
        unit: ("inactive", "inhibited")
        for unit in runtime.maintenance_state_by_unit
    }

    result = activate_release(
        expected_commit=CANDIDATE,
        rollback_commit="",
        paths=paths,
        runtime=runtime,
        expected_uid=paths.release_root.stat().st_uid,
        source_mode="stopped_legacy",
    )

    assert result["status"] == "activated"
    assert result["rollback_commit"] is None


def test_stopped_legacy_candidate_start_failure_returns_to_maintenance_stopped(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    _set_authorization_source_mode(paths, "stopped_legacy")
    runtime.maintenance_state_by_unit = {
        unit: ("inactive", "inhibited")
        for unit in runtime.maintenance_state_by_unit
    }
    original_start = runtime.start_unit

    def no_worker_can_start(unit: str) -> None:
        if unit == "telegram-kol-worker.service":
            raise ActivationError("worker start failed")
        original_start(unit)

    runtime.start_unit = no_worker_can_start

    with pytest.raises(ActivationError, match="maintenance_stopped"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            source_mode="stopped_legacy",
        )

    assert set(runtime.maintenance_state_by_unit.values()) == {("inactive", "inhibited")}


def test_stopped_legacy_failed_stop_proof_attempts_every_inhibit_and_stop(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    _set_authorization_source_mode(paths, "stopped_legacy")
    runtime.maintenance_state_by_unit = {
        unit: ("inactive", "inhibited")
        for unit in runtime.maintenance_state_by_unit
    }
    runtime.mask_fail_units = {"telegram-kol-ingest.service"}
    runtime.stop_fail_units = {"telegram-kol-worker.service"}
    original_start = runtime.start_unit

    def no_worker_can_start(unit: str) -> None:
        if unit == "telegram-kol-worker.service":
            raise ActivationError("worker start failed")
        original_start(unit)

    runtime.start_unit = no_worker_can_start

    with pytest.raises(ActivationError, match="maintenance_stop_failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            source_mode="stopped_legacy",
        )

    expected_units = set(runtime.maintenance_state_by_unit)
    assert {
        event.removeprefix("mask:")
        for event in runtime.events
        if event.startswith("mask:")
    } == expected_units
    assert {
        event.removeprefix("stop:")
        for event in runtime.events
        if event.startswith("stop:")
    } == expected_units


def test_system_runtime_adapter_uses_persistent_inhibit_dropin(tmp_path, monkeypatch):
    runtime = SystemRuntimeAdapter(
        dropin_root=tmp_path,
        expected_uid=tmp_path.stat().st_uid,
    )
    commands = []

    def run(command, *, environment=None):
        commands.append(command)
        if "--property=ActiveState" in command:
            return type("Result", (), {"stdout": "inactive\n"})()
        return type("Result", (), {"stdout": "no\n"})()

    monkeypatch.setattr(runtime, "_run", run)

    runtime.mask_unit("telegram-kol.service")

    inhibit = (
        tmp_path
        / "telegram-kol.service.d/00-telegram-kol-maintenance-inhibit.conf"
    )
    assert inhibit.read_bytes() == (
        b"[Unit]\nConditionPathExists=/dev/null/telegram-kol-maintenance-never\n"
    )
    assert inhibit.stat().st_mode & 0o777 == 0o444
    assert ["systemctl", "mask", "telegram-kol.service"] not in commands
    assert ["systemctl", "daemon-reload"] in commands
    assert runtime.maintenance_unit_state("telegram-kol.service") == (
        "inactive",
        "inhibited",
    )

    runtime.unmask_unit("telegram-kol.service")

    assert not inhibit.exists()


def test_system_runtime_adapter_accepts_empty_main_pid_for_monitor_timer(
    monkeypatch,
) -> None:
    runtime = SystemRuntimeAdapter()

    monkeypatch.setattr(
        runtime,
        "_run",
        lambda command, *, environment=None: type(
            "Result", (), {"stdout": "\n"}
        )(),
    )

    assert runtime.main_pid("telegram-kol-monitor.timer") == 0


def test_system_runtime_adapter_rejects_empty_main_pid_for_service(
    monkeypatch,
) -> None:
    runtime = SystemRuntimeAdapter()

    monkeypatch.setattr(
        runtime,
        "_run",
        lambda command, *, environment=None: type(
            "Result", (), {"stdout": "\n"}
        )(),
    )

    with pytest.raises(ActivationError, match="runtime command failed"):
        runtime.main_pid("telegram-kol-web.service")


def test_activation_source_mode_is_explicit_and_closed(monkeypatch) -> None:
    monkeypatch.delenv("ACTIVATION_SOURCE_MODE", raising=False)
    assert scoped_activation._activation_source_mode() == "immutable"
    monkeypatch.setenv("ACTIVATION_SOURCE_MODE", "stopped_legacy")
    assert scoped_activation._activation_source_mode() == "stopped_legacy"
    monkeypatch.setenv("ACTIVATION_SOURCE_MODE", "legacy-ish")
    with pytest.raises(ActivationError, match="activation source mode is invalid"):
        scoped_activation._activation_source_mode()


def test_activation_dry_run_mode_is_explicit_and_closed(monkeypatch) -> None:
    monkeypatch.delenv("ACTIVATION_DRY_RUN", raising=False)
    assert scoped_activation._activation_dry_run() is False
    monkeypatch.setenv("ACTIVATION_DRY_RUN", "1")
    assert scoped_activation._activation_dry_run() is True
    monkeypatch.setenv("ACTIVATION_DRY_RUN", "yes")
    with pytest.raises(ActivationError, match="dry-run mode is invalid"):
        scoped_activation._activation_dry_run()


def test_stopped_legacy_source_mode_rejects_ordinary_immutable_authorization(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    runtime.maintenance_state_by_unit = {
        unit: ("inactive", "inhibited")
        for unit in runtime.maintenance_state_by_unit
    }

    with pytest.raises(ActivationError, match="authorization is invalid"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            source_mode="stopped_legacy",
        )

    assert paths.authorization.exists()
    assert runtime.events == []


@pytest.mark.parametrize(
    ("unit", "state", "reason"),
    [
        ("telegram-kol-worker.service", ("active", "inhibited"), "not persistently stopped"),
        ("telegram-kol-ingest.service", ("inactive", "enabled"), "not persistently stopped"),
        ("telegram-kol-worker.service", ("inactive", "masked"), "not persistently stopped"),
    ],
)
def test_stopped_legacy_activation_refuses_active_or_unmasked_unit_before_authorization(
    activation_harness,
    unit,
    state,
    reason,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    _set_authorization_source_mode(paths, "stopped_legacy")
    runtime.maintenance_state_by_unit = {
        name: ("inactive", "inhibited")
        for name in runtime.maintenance_state_by_unit
    }
    runtime.maintenance_state_by_unit[unit] = state

    with pytest.raises(ActivationError, match=reason):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            source_mode="stopped_legacy",
        )

    assert paths.authorization.exists()
    assert not any(
        event.startswith(("unmask:", "start:")) for event in runtime.events
    )


def test_stopped_legacy_activation_refuses_partial_runtime_scope(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    _set_authorization_source_mode(paths, "stopped_legacy")
    runtime.maintenance_state_by_unit = {
        unit: ("inactive", "inhibited")
        for unit in runtime.maintenance_state_by_unit
    }

    with pytest.raises(ActivationError, match="requires full runtime scope"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            source_mode="stopped_legacy",
        )

    assert paths.authorization.exists()


def test_stopped_legacy_activation_refuses_unknown_unit_state(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    _set_authorization_source_mode(paths, "stopped_legacy")

    def unavailable_state(unit: str) -> tuple[str, str]:
        raise OSError(f"cannot read {unit}")

    runtime.maintenance_unit_state = unavailable_state

    with pytest.raises(ActivationError, match="runtime state is unknown"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            source_mode="stopped_legacy",
        )

    assert paths.authorization.exists()
    assert not any(
        event.startswith(("unmask:", "start:")) for event in runtime.events
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda runtime: runtime.main_pid_by_unit.__setitem__("telegram-kol.service", 411), "not persistently stopped"),
        (lambda runtime: runtime.cgroup_pids_by_unit.__setitem__("telegram-kol.service", (411,)), "not persistently stopped"),
        (lambda runtime: setattr(runtime, "matching_runtime_pids", (411,)), "runtime process remains"),
    ],
)
def test_stopped_legacy_activation_refuses_legacy_pid_cgroup_or_process(
    activation_harness,
    mutation,
    reason,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    _set_authorization_source_mode(paths, "stopped_legacy")
    runtime.maintenance_state_by_unit = {
        unit: ("inactive", "inhibited")
        for unit in runtime.maintenance_state_by_unit
    }
    mutation(runtime)

    with pytest.raises(ActivationError, match=reason):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
            source_mode="stopped_legacy",
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("unmask:") for event in runtime.events)


def test_partial_worker_authority_activation_is_rejected(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    manifest["components"] = ["worker"]
    paths.action_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ActivationError, match="action manifest is invalid"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert runtime.events == []


def test_worker_activation_requires_direct_authority_and_post_stop_quiescence(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    runtime.authority_ok = False

    with pytest.raises(ActivationError, match="authority proof failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("stop:") for event in runtime.events)


def test_worker_authority_requires_fresh_successful_cycle_evidence(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    original_identity = runtime.runtime_identity

    def stale_authority_identity(role: str) -> dict:
        payload = original_identity(role)
        if role == "worker":
            payload["authority_evidence"]["reconcile_cycle"].update(
                {"age_seconds": 91.0, "fresh": False}
            )
        return payload

    runtime.runtime_identity = stale_authority_identity

    with pytest.raises(ActivationError, match="authority proof failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert not any(event.startswith("stop:") for event in runtime.events)


def test_post_start_identity_mismatch_rolls_back_to_verified_release(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    original_start = runtime.start_unit

    def start_without_loading_candidate(unit: str) -> None:
        original_start(unit)
        if runtime.current_commit_by_role["web"] == CANDIDATE:
            runtime.current_commit_by_role["web"] = "3" * 40
            runtime.manifest_by_commit["3" * 40] = "4" * 64

    runtime.start_unit = start_without_loading_candidate

    with pytest.raises(ActivationError, match="rollback_complete"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert runtime.current_commit_by_role["web"] == ROLLBACK
    assert runtime.events.count("daemon-reload") == 2
    assert paths.authorization_consumed.exists()


def test_partial_rollback_failure_best_effort_stops_all_declared_units(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    original_start = runtime.start_unit
    start_count = 0

    def fail_candidate_and_rollback_start(unit: str) -> None:
        nonlocal start_count
        start_count += 1
        original_start(unit)
        raise ActivationError(f"start failed {start_count}")

    runtime.start_unit = fail_candidate_and_rollback_start

    with pytest.raises(ActivationError, match="rollback_failed"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert runtime.maintenance_state_by_unit["telegram-kol-web.service"][0] == "inactive"
    assert runtime.events.count("stop:telegram-kol-web.service") >= 3


def test_worker_activation_rolls_back_when_candidate_does_not_prove_entry_freeze(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    original_identity = runtime.runtime_identity

    def identity_without_freeze(role: str) -> dict:
        payload = original_identity(role)
        if role == "worker" and payload["release_commit"] == CANDIDATE:
            payload["entry_admission_frozen"] = False
        return payload

    runtime.runtime_identity = identity_without_freeze

    with pytest.raises(ActivationError, match="rollback_complete"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert runtime.current_commit_by_role["worker"] == ROLLBACK
    assert runtime.entry_frozen_by_role["worker"] is True


def test_same_pid_and_start_ticks_cannot_claim_a_restart(
    activation_harness,
) -> None:
    paths, runtime, _ = activation_harness
    original_start = runtime.start_unit

    def start_without_new_process(unit: str) -> None:
        original_start(unit)
        if unit == "telegram-kol-web.service" and runtime.current_commit_by_role["web"] == CANDIDATE:
            runtime.start_ticks_by_role["web"] -= 10

    runtime.start_unit = start_without_new_process

    with pytest.raises(ActivationError, match="rollback_complete"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert runtime.current_commit_by_role["web"] == ROLLBACK


def test_partial_ingest_worker_authority_scope_is_rejected(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    _configure_worker_harness(paths, runtime, manifest)
    manifest["components"] = ["ingest", "worker"]
    paths.action_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ActivationError, match="action manifest is invalid"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert runtime.events == []


def test_schema_or_data_activation_remains_fail_closed_without_l3_executor(
    activation_harness,
) -> None:
    paths, runtime, manifest = activation_harness
    manifest.update({"risk_level": "L3", "schema_changed": True})
    paths.action_manifest.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ActivationError, match="L3 database activation"):
        activate_release(
            expected_commit=CANDIDATE,
            rollback_commit=ROLLBACK,
            paths=paths,
            runtime=runtime,
            expected_uid=paths.release_root.stat().st_uid,
        )

    assert paths.authorization.exists()
    assert runtime.events == []
