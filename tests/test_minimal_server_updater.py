from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _run_git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def stage_harness(tmp_path: Path):
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    release_root = tmp_path / "releases"
    action_manifest = tmp_path / "stage-action.json"
    origin.mkdir()
    source.mkdir()
    release_root.mkdir(mode=0o755)
    subprocess.run(["git", "init", "--bare", str(origin)], check=True)
    subprocess.run(["git", "init", str(source)], check=True)
    _run_git(source, "config", "user.name", "Stage Test")
    _run_git(source, "config", "user.email", "stage@example.invalid")
    _run_git(source, "checkout", "-b", "codex/stage-test")
    (source / "app.py").write_text("VALUE = 'candidate'\n", encoding="utf-8")
    executable = source / "deploy-tool"
    executable.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    _run_git(source, "add", "app.py", "deploy-tool")
    _run_git(source, "commit", "-m", "candidate")
    _run_git(source, "remote", "add", "origin", str(origin))
    _run_git(source, "push", "-u", "origin", "codex/stage-test")
    candidate = _run_git(source, "rev-parse", "HEAD")
    action_manifest.write_text(
        json.dumps(
            {
                "action": "stage",
                "risk_level": "L2",
                "components": ["worker"],
                "requires_restart": True,
                "schema_changed": False,
                "production_data_mutation": False,
                "exchange_write_semantics_changed": False,
                "authority_changed": True,
            }
        ),
        encoding="utf-8",
    )
    source_head = _run_git(source, "rev-parse", "HEAD")

    def run(**overrides: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(ROOT / "src"),
                "SOURCE_REPO": str(source),
                "RELEASE_ROOT": str(release_root),
                "EXPECTED_COMMIT": candidate,
                "BRANCH": "codex/stage-test",
                "ACTION_MANIFEST": str(action_manifest),
                "STAGER_LOCK_PATH": str(tmp_path / "stage.lock"),
                "STAGER_TEST_MODE": "1",
                "DEEPCOIN_API_SECRET": "must-not-be-printed",
            }
        )
        environment.update(overrides)
        return subprocess.run(
            [str(ROOT / ".venv/bin/python"), str(ROOT / "deploy/telegram-kol-stage")],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=20,
        )

    return {
        "run": run,
        "candidate": candidate,
        "source": source,
        "source_head": source_head,
        "release_root": release_root,
        "action_manifest": action_manifest,
    }


def test_stage_only_materializes_exact_immutable_release_without_source_mutation(
    stage_harness,
) -> None:
    result = stage_harness["run"]()

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    candidate = stage_harness["candidate"]
    release = stage_harness["release_root"] / candidate
    assert payload["status"] == "staged"
    assert payload["commit"] == candidate
    assert "must-not-be-printed" not in result.stdout + result.stderr
    assert (release / "app.py").read_text(encoding="utf-8") == "VALUE = 'candidate'\n"
    assert not (release / ".git").exists()
    assert _run_git(stage_harness["source"], "rev-parse", "HEAD") == stage_harness[
        "source_head"
    ]
    assert _run_git(stage_harness["source"], "status", "--porcelain") == ""
    assert not (stage_harness["source"] / ".git/FETCH_HEAD").exists()

    manifest = json.loads(
        (release / ".telegram-kol-release.json").read_text(encoding="utf-8")
    )
    receipt = json.loads(
        (release / ".telegram-kol-stage-receipt.json").read_text(encoding="utf-8")
    )
    assert manifest["commit"] == receipt["commit"] == candidate
    assert manifest["action_manifest"]["action"] == "stage"
    assert receipt["manifest_sha256"]
    assert "created_at" not in receipt
    assert "release_path" not in receipt

    for path in release.rglob("*"):
        mode = stat.S_IMODE(path.lstat().st_mode)
        if path.is_dir():
            assert mode == 0o555
        elif path.name == "deploy-tool":
            assert mode == 0o555
        else:
            assert mode == 0o444


def test_stage_only_same_sha_and_manifest_is_idempotent(stage_harness) -> None:
    first = stage_harness["run"]()
    release = stage_harness["release_root"] / stage_harness["candidate"]
    receipt_path = release / ".telegram-kol-stage-receipt.json"
    inode = release.stat().st_ino
    receipt_bytes = receipt_path.read_bytes()

    second = stage_harness["run"]()

    assert first.returncode == second.returncode == 0
    assert json.loads(second.stdout)["status"] == "already_staged"
    assert release.stat().st_ino == inode
    assert receipt_path.read_bytes() == receipt_bytes


def test_stage_only_refuses_corrupted_existing_release(stage_harness) -> None:
    assert stage_harness["run"]().returncode == 0
    release = stage_harness["release_root"] / stage_harness["candidate"]
    candidate_file = release / "app.py"
    candidate_file.chmod(0o644)
    candidate_file.write_text("VALUE = 'corrupted'\n", encoding="utf-8")
    candidate_file.chmod(0o444)

    result = stage_harness["run"]()

    assert result.returncode != 0
    assert "release validation failed" in result.stderr.lower()
    assert candidate_file.read_text(encoding="utf-8") == "VALUE = 'corrupted'\n"


def test_stage_only_refuses_same_sha_with_different_action_manifest(
    stage_harness,
) -> None:
    assert stage_harness["run"]().returncode == 0
    action_manifest = stage_harness["action_manifest"]
    changed = json.loads(action_manifest.read_text(encoding="utf-8"))
    changed["authority_changed"] = False
    action_manifest.write_text(json.dumps(changed), encoding="utf-8")

    result = stage_harness["run"]()

    assert result.returncode != 0
    assert "release validation failed" in result.stderr.lower()


def test_stage_only_refuses_remote_head_mismatch_without_partial_release(
    stage_harness,
) -> None:
    wrong_commit = "f" * 40

    result = stage_harness["run"](EXPECTED_COMMIT=wrong_commit)

    assert result.returncode != 0
    assert "remote branch head does not match" in result.stderr.lower()
    assert not (stage_harness["release_root"] / wrong_commit).exists()
    assert not list(stage_harness["release_root"].glob(".telegram-kol-stage.*"))


def test_stage_only_requires_a_stage_action_manifest(stage_harness) -> None:
    action_manifest = stage_harness["action_manifest"]
    invalid = json.loads(action_manifest.read_text(encoding="utf-8"))
    invalid["action"] = "activate"
    invalid["components"] = ["web", "monitor", "ingest", "worker"]
    action_manifest.write_text(json.dumps(invalid), encoding="utf-8")

    result = stage_harness["run"]()

    assert result.returncode != 0
    assert "action manifest must declare stage" in result.stderr.lower()
    assert not (
        stage_harness["release_root"] / stage_harness["candidate"]
    ).exists()


def test_stage_only_rejects_origin_url_with_embedded_credentials(
    stage_harness,
) -> None:
    _run_git(
        stage_harness["source"],
        "remote",
        "set-url",
        "origin",
        "https://secret-token@example.invalid/repository.git",
    )

    result = stage_harness["run"]()

    assert result.returncode != 0
    assert "origin remote must not contain credentials" in result.stderr.lower()
    assert "secret-token" not in result.stdout + result.stderr


def test_stage_only_does_not_inherit_active_source_ownership_or_mode_gate(
    stage_harness,
) -> None:
    stage_harness["source"].chmod(0o777)

    result = stage_harness["run"]()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "staged"
