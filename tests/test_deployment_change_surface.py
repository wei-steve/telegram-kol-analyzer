from __future__ import annotations

import subprocess
from pathlib import Path
import re

import pytest

from telegram_kol_research.deployment_change_surface import (
    ChangeSurfaceError,
    EXECUTION_WRITER_PATHS,
    bind_phase_restart_surface_counts,
    classify_change_surface,
)
from telegram_kol_research.deployment_work_evidence import WORK_EVIDENCE_ADAPTERS


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


def test_phase_b_preserves_phase_a_restart_handler_universe(tmp_path):
    repository, base = _repository(tmp_path)
    candidate = _commit(repository, "README.md", "candidate\n", "candidate")
    preliminary_counts = {
        "restart_safe_wait": {"management_batches": 1}
    }
    final_counts = {"terminal": {"management_batches": 1}}

    preliminary_surface = classify_change_surface(
        repository=repository,
        production_commit=base,
        candidate_commit=candidate,
        requested_change_class="code",
        work_classification_counts=preliminary_counts,
    )
    final_surface = classify_change_surface(
        repository=repository,
        production_commit=base,
        candidate_commit=candidate,
        requested_change_class="code",
        work_classification_counts=bind_phase_restart_surface_counts(
            preliminary_counts,
            final_counts,
        ),
    )

    assert (
        final_surface.restart_handler_fingerprint
        == preliminary_surface.restart_handler_fingerprint
    )


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


@pytest.mark.parametrize(
    "writer_path",
    [
        "src/telegram_kol_research/legacy_conditional_cancel.py",
        "src/telegram_kol_research/native_tpsl_migration.py",
        "src/telegram_kol_research/trigger_backup_stop_executor.py",
    ],
)
def test_each_reviewed_direct_exchange_writer_upgrades_change_class(
    tmp_path,
    writer_path,
):
    repository, base = _repository(tmp_path)
    candidate = _commit(repository, writer_path, "writer\n", "writer")

    facts = classify_change_surface(
        repository=repository,
        production_commit=base,
        candidate_commit=candidate,
        requested_change_class="code",
    )

    assert facts.effective_change_class == "execution_writer"
    assert facts.underdeclared is True
    assert facts.registry_version == 2


def test_trade_signal_retry_and_state_transitions_are_writer_sensitive(tmp_path):
    repository, base = _repository(tmp_path)
    candidate = _commit(
        repository,
        "src/telegram_kol_research/trade_signals.py",
        "changed retry semantics\n",
        "trade signal transition",
    )

    facts = classify_change_surface(
        repository=repository,
        production_commit=base,
        candidate_commit=candidate,
        requested_change_class="code",
        work_classification_counts={
            "historical_residue": {"trade_signals": 1}
        },
    )

    assert facts.effective_change_class == "execution_writer"
    assert facts.underdeclared is True


def test_every_restart_handler_is_registered_as_writer_sensitive():
    restart_handlers = {
        path
        for adapter in WORK_EVIDENCE_ADAPTERS
        for path in adapter.restart_surface_files
    }

    assert restart_handlers <= EXECUTION_WRITER_PATHS


def test_registered_writer_paths_exist_in_review_repository():
    repository = Path(__file__).resolve().parents[1]

    missing = sorted(
        path
        for path in EXECUTION_WRITER_PATHS
        if not (repository / path).is_file()
    )

    assert missing == []


def test_every_direct_deepcoin_write_caller_is_registered():
    repository = Path(__file__).resolve().parents[1]
    pattern = re.compile(
        r"\.(?:place_order|trigger_order|set_position_sltp|replace_order_sltp|"
        r"cancel_position_sltp|cancel_order|cancel_trigger_order)\("
    )
    direct_callers = {
        path.relative_to(repository).as_posix()
        for path in (repository / "src/telegram_kol_research").glob("*.py")
        if pattern.search(path.read_text(encoding="utf-8"))
    }

    assert direct_callers <= EXECUTION_WRITER_PATHS


def test_reviewed_retirement_exception_breaks_on_risk_path_change(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    worktree = tmp_path / "candidate"
    _git(
        repository,
        "worktree",
        "add",
        "--detach",
        str(worktree),
        "7813150b7b33cd8ce3d90a6145889c6fef192dc7",
    )
    try:
        changed = _commit(
            worktree,
            "src/telegram_kol_research/trading_settings.py",
            "changed authority\n",
            "authority drift",
        )
        facts = classify_change_surface(
            repository=repository,
            production_commit="2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa",
            candidate_commit=changed,
            requested_change_class="schema_compatible",
        )
    finally:
        _git(repository, "worktree", "remove", "--force", str(worktree))

    assert facts.effective_change_class == "live_promotion"
    assert facts.underdeclared is True


def test_post_retirement_writer_fix_keeps_retired_live_authority_exempt():
    repository = Path(__file__).resolve().parents[1]
    candidate = _git(repository, "rev-parse", "HEAD")

    facts = classify_change_surface(
        repository=repository,
        production_commit="2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa",
        candidate_commit=candidate,
        requested_change_class="schema_compatible",
    )

    assert facts.effective_change_class == "execution_writer"
    assert facts.underdeclared is True
