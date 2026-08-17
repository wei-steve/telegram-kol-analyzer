from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from telegram_kol_research.deployment_change_surface import (
    ChangeSurfaceError,
    classify_change_surface,
)


def _git(repository, *args):
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repository, path, body, message):
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repository, "add", str(path))
    _git(
        repository,
        "-c",
        "user.name=Codex Test",
        "-c",
        "user.email=codex@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD")


def _repository(tmp_path):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    base = _commit(repository, "README.md", "base\n", "base")
    return repository, base


def test_requested_class_is_a_lower_bound_and_writer_change_upgrades(tmp_path):
    repository, base = _repository(tmp_path)
    guard = _commit(
        repository,
        "src/telegram_kol_research/deployment_preflight.py",
        "guard\n",
        "guard",
    )
    safe = classify_change_surface(
        repository=repository,
        production_commit=base,
        candidate_commit=guard,
        requested_change_class="schema_compatible",
    )

    assert safe.effective_change_class == "schema_compatible"
    assert safe.underdeclared is False
    assert safe.changed_path_count == 1

    writer = _commit(
        repository,
        "src/telegram_kol_research/strategy_management_reconciliation.py",
        "changed writer semantics\n",
        "writer",
    )
    unsafe = classify_change_surface(
        repository=repository,
        production_commit=base,
        candidate_commit=writer,
        requested_change_class="schema_compatible",
    )

    assert unsafe.effective_change_class == "execution_writer"
    assert unsafe.underdeclared is True
    assert len(unsafe.change_surface_fingerprint) == 64


def test_restart_residue_requires_unchanged_reviewed_handler(tmp_path):
    repository, base = _repository(tmp_path)
    guard = _commit(
        repository,
        "src/telegram_kol_research/deployment_work_evidence.py",
        "guard\n",
        "guard",
    )
    counts = {"historical_residue": {"management_batches": 1}}

    safe = classify_change_surface(
        repository=repository,
        production_commit=base,
        candidate_commit=guard,
        requested_change_class="code",
        work_classification_counts=counts,
    )
    assert safe.restart_compatibility_changed is False

    changed = _commit(
        repository,
        "src/telegram_kol_research/strategy_management_reconciliation.py",
        "changed\n",
        "reconciler",
    )
    unsafe = classify_change_surface(
        repository=repository,
        production_commit=base,
        candidate_commit=changed,
        requested_change_class="code",
        work_classification_counts=counts,
    )
    assert unsafe.restart_compatibility_changed is True
    assert "restart_compatibility_changed" in unsafe.blocking_reason_codes


def test_unknown_git_object_fails_closed(tmp_path):
    repository, base = _repository(tmp_path)

    with pytest.raises(ChangeSurfaceError, match="change_surface_malformed"):
        classify_change_surface(
            repository=repository,
            production_commit=base,
            candidate_commit="f" * 40,
            requested_change_class="code",
        )


def test_exact_reviewed_retirement_diff_does_not_promote_live_authority():
    repository = Path(__file__).resolve().parents[1]

    facts = classify_change_surface(
        repository=repository,
        production_commit="2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa",
        candidate_commit="7813150b7b33cd8ce3d90a6145889c6fef192dc7",
        requested_change_class="schema_compatible",
    )

    assert facts.effective_change_class == "schema_compatible"
    assert facts.underdeclared is False
