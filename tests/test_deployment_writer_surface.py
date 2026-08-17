from __future__ import annotations

import ast
import inspect
from pathlib import Path
import subprocess

import pytest

from telegram_kol_research.deployment_writer_surface import (
    MUTATION_AUTHORITY_PATHS,
    OUTCOME_AUTHORITY_PATHS,
    WORKER_CLAIM_PATHS,
    WRITER_SURFACE_PATHS,
    WriterSurfaceError,
    classify_candidate_surface,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src/telegram_kol_research"
CLIENT_PATH = SOURCE_ROOT / "deepcoin_client.py"


def _git(repository: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
    )
    return result.stdout.strip()


def _init_repository(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Gate Test")
    _git(path, "config", "user.email", "gate@example.invalid")


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755 if executable else 0o644)


def _commit(repository: Path, message: str) -> str:
    _git(repository, "add", "-A")
    _git(
        repository,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        message,
    )
    return _git(repository, "rev-parse", "HEAD")


def _baseline(repository: Path) -> str:
    _write(
        repository / "src/telegram_kol_research/deepcoin_client.py",
        "def writer():\n    return 'v1'\n",
    )
    _write(repository / "README.md", "baseline\n")
    return _commit(repository, "baseline")


def test_identical_writer_blobs_have_identical_fingerprints(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init_repository(repository)
    production = _baseline(repository)
    _write(repository / "docs/gate.md", "gate docs\n")
    candidate = _commit(repository, "docs only")

    surface = classify_candidate_surface(
        repository=repository,
        production_commit=production,
        candidate_commit=candidate,
    )

    assert surface.writer_changed is False
    assert (
        surface.production_writer_fingerprint
        == surface.candidate_writer_fingerprint
    )
    assert surface.changed_path_count == 1


def test_writer_blob_change_flips_fingerprint(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init_repository(repository)
    production = _baseline(repository)
    _write(
        repository / "src/telegram_kol_research/deepcoin_client.py",
        "def writer():\n    return 'v2'\n",
    )
    candidate = _commit(repository, "writer change")

    surface = classify_candidate_surface(
        repository=repository,
        production_commit=production,
        candidate_commit=candidate,
    )

    assert surface.writer_changed is True
    assert (
        surface.production_writer_fingerprint
        != surface.candidate_writer_fingerprint
    )


@pytest.mark.parametrize(
    "path",
    [
        "src/telegram_kol_research/deployment_preflight.py",
        "deploy/telegram-kol-update",
        "tests/test_gate.py",
        "docs/gate.md",
    ],
)
def test_gate_updater_test_and_docs_do_not_change_writer_fingerprint(
    tmp_path: Path,
    path: str,
) -> None:
    repository = tmp_path / "repo"
    _init_repository(repository)
    production = _baseline(repository)
    _write(repository / path, "candidate-only\n")
    candidate = _commit(repository, path)

    surface = classify_candidate_surface(
        repository=repository,
        production_commit=production,
        candidate_commit=candidate,
    )

    assert surface.writer_changed is False


@pytest.mark.parametrize(
    "path",
    [
        "src/telegram_kol_research/db.py",
        "src/telegram_kol_research/models.py",
        "migrations/versions/001_change.py",
    ],
)
def test_schema_paths_are_detected_automatically(tmp_path: Path, path: str) -> None:
    repository = tmp_path / "repo"
    _init_repository(repository)
    production = _baseline(repository)
    _write(repository / path, "schema change\n")
    candidate = _commit(repository, path)

    surface = classify_candidate_surface(
        repository=repository,
        production_commit=production,
        candidate_commit=candidate,
    )

    assert surface.schema_changed is True
    assert "change_class" not in inspect.signature(
        classify_candidate_surface
    ).parameters


def test_writer_mode_change_is_part_of_fingerprint(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init_repository(repository)
    production = _baseline(repository)
    writer = repository / "src/telegram_kol_research/deepcoin_client.py"
    writer.chmod(0o755)
    candidate = _commit(repository, "mode change")

    surface = classify_candidate_surface(
        repository=repository,
        production_commit=production,
        candidate_commit=candidate,
    )

    assert surface.writer_changed is True


def test_writer_delete_and_rename_are_deterministic(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init_repository(repository)
    production = _baseline(repository)
    old_path = repository / "src/telegram_kol_research/deepcoin_client.py"
    new_path = repository / "src/telegram_kol_research/not_writer.py"
    old_path.rename(new_path)
    candidate = _commit(repository, "rename writer away")

    first = classify_candidate_surface(
        repository=repository,
        production_commit=production,
        candidate_commit=candidate,
    )
    second = classify_candidate_surface(
        repository=repository,
        production_commit=production,
        candidate_commit=candidate,
    )

    assert first == second
    assert first.writer_changed is True
    assert first.changed_path_count == 2


def test_manifest_hash_is_independent_of_file_creation_order(tmp_path: Path) -> None:
    fingerprints: list[str] = []
    for index, paths in enumerate(
        [
            ["deepcoin_client.py", "trade_signals.py"],
            ["trade_signals.py", "deepcoin_client.py"],
        ]
    ):
        repository = tmp_path / f"repo-{index}"
        _init_repository(repository)
        for name in paths:
            _write(repository / "src/telegram_kol_research" / name, name)
        commit = _commit(repository, "same tree")
        surface = classify_candidate_surface(
            repository=repository,
            production_commit=commit,
            candidate_commit=commit,
        )
        fingerprints.append(surface.production_writer_fingerprint)

    assert fingerprints[0] == fingerprints[1]


@pytest.mark.parametrize("commit", ["bad", "f" * 40])
def test_malformed_or_missing_commit_fails_closed(
    tmp_path: Path,
    commit: str,
) -> None:
    repository = tmp_path / "repo"
    _init_repository(repository)
    production = _baseline(repository)

    with pytest.raises(WriterSurfaceError, match="git_commit_invalid"):
        classify_candidate_surface(
            repository=repository,
            production_commit=production,
            candidate_commit=commit,
        )


def test_non_commit_object_fails_closed(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init_repository(repository)
    production = _baseline(repository)
    blob = _git(repository, "hash-object", "-w", "--stdin", input_text="blob")

    with pytest.raises(WriterSurfaceError, match="git_commit_invalid"):
        classify_candidate_surface(
            repository=repository,
            production_commit=production,
            candidate_commit=blob,
        )


def test_repository_must_be_the_git_root(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    _init_repository(repository)
    production = _baseline(repository)
    child = repository / "src"

    with pytest.raises(WriterSurfaceError, match="git_repository_invalid"):
        classify_candidate_surface(
            repository=child,
            production_commit=production,
            candidate_commit=production,
        )


def _posting_methods(client_path: Path) -> set[str]:
    tree = ast.parse(client_path.read_text(encoding="utf-8"))
    client_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "DeepcoinRestClient"
    )
    methods = {
        node.name: node
        for node in client_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    calls: dict[str, set[str]] = {name: set() for name in methods}
    posting: set[str] = set()
    for name, method in methods.items():
        for node in ast.walk(method):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if not isinstance(node.func.value, ast.Name) or node.func.value.id != "self":
                continue
            calls[name].add(node.func.attr)
            if (
                node.func.attr == "_request"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and str(node.args[0].value).upper() == "POST"
            ):
                posting.add(name)
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            if name not in posting and posting & callees:
                posting.add(name)
                changed = True
    return posting


def _post_call_site_paths(source_root: Path, methods: set[str]) -> set[str]:
    paths: set[str] = set()
    for source in source_root.glob("*.py"):
        if source.name == "deepcoin_client.py":
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"))
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in methods
            for node in ast.walk(tree)
        ) or any(
            isinstance(node, ast.Constant)
            and node.value in methods
            and str(node.value).startswith("_")
            for node in ast.walk(tree)
        ):
            paths.add(f"src/telegram_kol_research/{source.name}")
    return paths


def test_every_deepcoin_post_call_site_is_writer_sensitive() -> None:
    methods = _posting_methods(CLIENT_PATH)
    call_sites = _post_call_site_paths(SOURCE_ROOT, methods)

    assert methods
    assert call_sites
    assert call_sites <= WRITER_SURFACE_PATHS


def test_synthetic_post_call_site_would_be_detected_outside_manifest(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "src/telegram_kol_research"
    _write(source_root / "new_unregistered_writer.py", "client.place_order({})\n")

    call_sites = _post_call_site_paths(source_root, {"place_order"})

    assert call_sites - WRITER_SURFACE_PATHS == {
        "src/telegram_kol_research/new_unregistered_writer.py"
    }


def test_indirect_authority_and_worker_primitives_remain_in_manifest() -> None:
    assert MUTATION_AUTHORITY_PATHS <= WRITER_SURFACE_PATHS
    assert OUTCOME_AUTHORITY_PATHS <= WRITER_SURFACE_PATHS
    assert WORKER_CLAIM_PATHS <= WRITER_SURFACE_PATHS
    assert {
        "src/telegram_kol_research/position_authority_lock.py",
        "src/telegram_kol_research/instruction_execution_outcomes.py",
        "src/telegram_kol_research/instruction_execution_projection.py",
        "src/telegram_kol_research/strategy_management_market_decisions.py",
    } <= WRITER_SURFACE_PATHS
