from __future__ import annotations

from pathlib import Path
import re
import subprocess

from telegram_kol_research.deployment_writer_surface import (
    WRITER_SURFACE_PATHS,
    classify_candidate_surface,
)


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_COMMIT = "2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa"
GATE_RUNTIME_PATHS = frozenset(
    {
        "src/telegram_kol_research/deployment_preflight.py",
        "src/telegram_kol_research/deployment_preflight_cli.py",
        "src/telegram_kol_research/deployment_work_evidence.py",
        "src/telegram_kol_research/deployment_writer_surface.py",
    }
)
UPDATER_PATHS = frozenset(
    {
        "deploy/telegram-kol-update",
        "scripts/bootstrap_server_updater.sh",
        "scripts/server_git_update.ps1",
        "scripts/server_git_update.sh",
    }
)
INTERFACE_PATHS = GATE_RUNTIME_PATHS | UPDATER_PATHS
APPROVED_INTERFACE_ENTRIES = {
    "deploy/telegram-kol-update": (
        "100755",
        "265d4e05e34844c70ab883fd0c8d81f0b2f18074",
    ),
    "scripts/bootstrap_server_updater.sh": (
        "100755",
        "00f79a2796876040db543dc3ddec231b248cbfba",
    ),
    "scripts/server_git_update.ps1": (
        "100644",
        "a31250b5f53bc83b3531c2383c3822fa1870bdcc",
    ),
    "scripts/server_git_update.sh": (
        "100755",
        "cf21709bfed7b6927b8765b7c3f460423661db02",
    ),
    "src/telegram_kol_research/deployment_preflight.py": (
        "100644",
        "76763b539ce5c9b35844b5dd0697eb3abe7a590e",
    ),
    "src/telegram_kol_research/deployment_preflight_cli.py": (
        "100644",
        "6e8a2bf6cf6a09aea8ec1ecc9990aa7eb864c673",
    ),
    "src/telegram_kol_research/deployment_work_evidence.py": (
        "100644",
        "c2ca7ff6297882e5a0e8f650a71d8e0dbdbcc734",
    ),
    "src/telegram_kol_research/deployment_writer_surface.py": (
        "100644",
        "dcec4ff5542fc6db90e4c1607b2da0381ed6c462",
    ),
}
RUNBOOK_PATHS = (
    "docs/server-deployment.md",
    "docs/runbook.md",
    "docs/migration-handoff.md",
)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _candidate_commit() -> str:
    return _git("rev-parse", "HEAD")


def _changed_paths() -> frozenset[str]:
    output = _git(
        "diff",
        "--name-only",
        "--no-renames",
        PRODUCTION_COMMIT,
        _candidate_commit(),
        "--",
    )
    return frozenset(output.splitlines())


def test_candidate_contains_only_gate_updater_tests_and_documentation() -> None:
    unexpected = {
        path
        for path in _changed_paths()
        if path not in GATE_RUNTIME_PATHS
        and path not in UPDATER_PATHS
        and not path.startswith("tests/")
        and not path.startswith("docs/")
    }

    assert unexpected == set()


def test_candidate_preserves_writer_mimo_and_schema_boundaries() -> None:
    candidate = _candidate_commit()
    changed = _changed_paths()
    surface = classify_candidate_surface(
        repository=ROOT,
        production_commit=PRODUCTION_COMMIT,
        candidate_commit=candidate,
    )

    assert surface.writer_changed is False
    assert (
        surface.production_writer_fingerprint
        == surface.candidate_writer_fingerprint
    )
    assert changed.isdisjoint(WRITER_SURFACE_PATHS)
    assert "src/telegram_kol_research/terminal_entry_cleanup.py" not in changed
    assert not any("mimo" in path.lower() for path in changed if path.startswith("src/"))
    assert not any(path.startswith("migrations/") for path in changed)
    assert "src/telegram_kol_research/db.py" not in changed
    assert "src/telegram_kol_research/models.py" not in changed


def test_reviewed_gate_and_updater_tree_entries_are_frozen() -> None:
    candidate = _candidate_commit()
    actual = {}
    for path in sorted(INTERFACE_PATHS):
        metadata, listed_path = _git("ls-tree", candidate, "--", path).split(
            "\t", 1
        )
        mode, object_type, blob_id = metadata.split()
        assert object_type == "blob"
        assert listed_path == path
        actual[path] = (mode, blob_id)

    assert actual == APPROVED_INTERFACE_ENTRIES


def test_gate_interface_has_no_manual_or_row_specific_escape_hatch() -> None:
    interface = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in sorted(INTERFACE_PATHS)
    )

    assert re.search(r"CHANGE_CLASS|ChangeClass", interface) is None
    assert re.search(r"(?i)batch\s*#?\s*\d+", interface) is None
    assert re.search(r"(?i)(age|heartbeat)[_-]?(cutoff|threshold)", interface) is None


def test_runbooks_describe_one_automatic_two_phase_contract() -> None:
    documents = {
        path: (ROOT / path).read_text(encoding="utf-8") for path in RUNBOOK_PATHS
    }
    combined = "\n".join(documents.values())

    assert re.search(r"CHANGE_CLASS|ChangeClass", combined) is None
    for required in (
        "writer fingerprint",
        "Phase A",
        "Phase B",
        "0=PASS",
        "2=WARN",
        "3=BLOCK",
        "4=invalid",
        "push approval",
        "shadow approval",
        "deployment approval",
        "MiMo v1",
        "MiMo v2",
    ):
        assert required in combined


def test_server_runbook_has_one_shadow_then_production_branch_path() -> None:
    deployment = (ROOT / RUNBOOK_PATHS[0]).read_text(encoding="utf-8")
    update_flow = deployment.split("## Update Flow", 1)[1].split(
        "## Backup-stop rollout gate", 1
    )[0]

    shadow_branch = "codex/deployment-gate-simplification"
    production_branch = "codex/deepcoin-auto-trading-v1"
    assert shadow_branch in update_flow
    assert production_branch in update_flow
    assert "fast-forward" in update_flow
    assert update_flow.index(shadow_branch) < update_flow.index(production_branch)
    assert "rollback failure" in update_flow
