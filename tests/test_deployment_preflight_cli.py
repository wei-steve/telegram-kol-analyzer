from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
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
