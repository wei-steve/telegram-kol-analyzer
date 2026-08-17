from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import subprocess

from telegram_kol_research.db import create_session_factory


NOW = datetime(2026, 8, 17, 4, 0, tzinfo=UTC)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repository(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    (repository / "README.md").write_text("production\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c",
        "user.name=Codex Test",
        "-c",
        "user.email=codex@example.invalid",
        "commit",
        "-m",
        "production",
    )
    production = _git(repository, "rev-parse", "HEAD")
    (repository / "README.md").write_text("candidate\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(
        repository,
        "-c",
        "user.name=Codex Test",
        "-c",
        "user.email=codex@example.invalid",
        "commit",
        "-m",
        "candidate",
    )
    return repository, production, _git(repository, "rev-parse", "HEAD")


def _run(*args):
    return subprocess.run(
        [
            ".venv/bin/python",
            "-m",
            "telegram_kol_research.deployment_preflight_cli",
            *map(str, args),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_standalone_cli_collects_and_verifies_preliminary_artifact(tmp_path):
    repository, production, candidate = _repository(tmp_path)
    database = tmp_path / "research.db"
    output = tmp_path / "preliminary.json"
    create_session_factory(database)

    collected = _run(
        "collect",
        "--repository",
        repository,
        "--production-commit",
        production,
        "--candidate-commit",
        candidate,
        "--requested-change-class",
        "code",
        "--phase",
        "preliminary",
        "--database",
        database,
        "--output",
        output,
        "--now",
        NOW.isoformat(),
    )

    assert collected.returncode == 2, collected.stderr
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 2
    assert artifact["phase"] == "preliminary"

    verified = _run(
        "verify",
        "--repository",
        repository,
        "--production-commit",
        production,
        "--candidate-commit",
        candidate,
        "--requested-change-class",
        "code",
        "--phase",
        "preliminary",
        "--input",
        output,
        "--now",
        NOW.isoformat(),
    )
    assert verified.returncode == 2, verified.stderr
    assert json.loads(verified.stdout)["decision"] == "WARN"


def test_standalone_cli_rejects_final_without_preliminary(tmp_path):
    repository, production, candidate = _repository(tmp_path)
    database = tmp_path / "research.db"
    create_session_factory(database)

    result = _run(
        "collect",
        "--repository",
        repository,
        "--production-commit",
        production,
        "--candidate-commit",
        candidate,
        "--requested-change-class",
        "code",
        "--phase",
        "final",
        "--database",
        database,
        "--output",
        tmp_path / "final.json",
        "--now",
        NOW.isoformat(),
    )

    assert result.returncode == 4
    assert "preliminary_artifact_required" in result.stderr


def test_final_cli_allows_restart_safe_work_to_become_terminal(tmp_path):
    repository, production, candidate = _repository(tmp_path)
    database = tmp_path / "research.db"
    preliminary_output = tmp_path / "preliminary.json"
    final_output = tmp_path / "final.json"
    create_session_factory(database)
    database_now = NOW.replace(tzinfo=None).isoformat(sep=" ")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO strategy_management_batches (
                idempotency_fingerprint, raw_message_id,
                recognition_decision_id, recognition_generation,
                target_lifecycle_id, strategy_instance_id,
                execution_binding_id, intent, effective_action,
                execution_mode, partial_round_before, status,
                target_fingerprint, target_snapshot_json,
                planned_at, created_at, updated_at
            ) VALUES (?, 1, 1, 'generation', 1, 'strategy', 1,
                      'cancel_entry', 'cancel_entry', 'disabled', 0,
                      'submitted', ?, '{}', ?, ?, ?)
            """,
            ("a" * 64, "b" * 64, database_now, database_now, database_now),
        )

    preliminary = _run(
        "collect",
        "--repository", repository,
        "--production-commit", production,
        "--candidate-commit", candidate,
        "--requested-change-class", "code",
        "--phase", "preliminary",
        "--database", database,
        "--output", preliminary_output,
        "--now", NOW.isoformat(),
    )
    assert preliminary.returncode == 2, preliminary_output.read_text(encoding="utf-8")

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE strategy_management_batches SET status = 'succeeded'"
        )

    final = _run(
        "collect",
        "--repository", repository,
        "--production-commit", production,
        "--candidate-commit", candidate,
        "--requested-change-class", "code",
        "--phase", "final",
        "--database", database,
        "--output", final_output,
        "--preliminary-artifact", preliminary_output,
        "--now", (NOW.replace(minute=1)).isoformat(),
    )

    assert final.returncode == 2, final.stderr
    assert json.loads(final_output.read_text(encoding="utf-8"))["phase"] == "final"


def test_standalone_cli_maps_parser_errors_to_malformed_exit_code():
    result = _run("verify", "--phase", "bogus")

    assert result.returncode == 4
    assert "preflight_cli_arguments_invalid" in result.stderr
