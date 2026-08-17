"""Automatic, flat Git fingerprint for exchange-writer compatibility."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re
import subprocess


WRITER_MANIFEST_VERSION = 1
_FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
_SOURCE_PREFIX = "src/telegram_kol_research/"


def _source_paths(*names: str) -> frozenset[str]:
    return frozenset(f"{_SOURCE_PREFIX}{name}" for name in names)


MUTATION_AUTHORITY_PATHS = _source_paths(
    "position_attribution.py",
    "position_authority_lock.py",
    "position_mutation_authority.py",
    "position_mutation_gateway.py",
    "protection_attribution.py",
    "protection_ledger.py",
    "recovery_live_submit_gate.py",
    "source_message_deletion.py",
)

OUTCOME_AUTHORITY_PATHS = _source_paths(
    "instruction_execution_entry_adapter.py",
    "instruction_execution_management_adapter.py",
    "instruction_execution_outcomes.py",
    "instruction_execution_projection.py",
    "recovery_order_confirmation.py",
    "strategy_management_contracts.py",
    "strategy_management_market_decisions.py",
    "strategy_management_market_policy.py",
)

WORKER_CLAIM_PATHS = _source_paths(
    "auto_trade_execution.py",
    "break_even_convergence_worker.py",
    "message_instruction_items.py",
    "source_message_deletion_worker.py",
    "strategy_management_worker.py",
    "trigger_protection_rescue_worker.py",
)

WRITER_SURFACE_PATHS = frozenset(
    _source_paths(
        "auto_trade_execution.py",
        "break_even_convergence_executor.py",
        "break_even_convergence_planner.py",
        "break_even_convergence_worker.py",
        "cli.py",
        "deepcoin_client.py",
        "deepcoin_execution_actions.py",
        "entry_revision_executor.py",
        "execution_bindings.py",
        "instruction_execution_contracts.py",
        "instruction_execution_reconciliation.py",
        "legacy_conditional_cancel.py",
        "message_instruction_items.py",
        "native_tpsl_migration.py",
        "position_mutation_intents.py",
        "position_protection_legs.py",
        "position_take_profit_orders.py",
        "recovery_live_submit.py",
        "source_message_deletion_worker.py",
        "strategy_management_batches.py",
        "strategy_management_composite_executor.py",
        "strategy_management_composite_reconciliation.py",
        "strategy_management_components.py",
        "strategy_management_executor.py",
        "strategy_management_reconciliation.py",
        "strategy_management_worker.py",
        "strategy_revision_planner.py",
        "system_operator_bot.py",
        "terminal_entry_cleanup.py",
        "trade_signals.py",
        "trigger_backup_stop_executor.py",
        "trigger_take_profit_convergence.py",
        "trigger_take_profit_convergence_executor.py",
        "trigger_protection_intents.py",
        "trigger_protection_rescue_worker.py",
        "web_app.py",
    )
    | MUTATION_AUTHORITY_PATHS
    | OUTCOME_AUTHORITY_PATHS
    | WORKER_CLAIM_PATHS
)

SCHEMA_PATHS = frozenset(
    {
        "src/telegram_kol_research/db.py",
        "src/telegram_kol_research/models.py",
    }
)


class WriterSurfaceError(ValueError):
    """The repository or exact Git objects cannot be classified safely."""


@dataclass(frozen=True, slots=True)
class CandidateSurface:
    manifest_version: int
    production_writer_fingerprint: str
    candidate_writer_fingerprint: str
    writer_changed: bool
    schema_changed: bool
    changed_path_count: int


def classify_candidate_surface(
    *,
    repository: str | Path,
    production_commit: str,
    candidate_commit: str,
) -> CandidateSurface:
    """Classify exact commits without an operator-provided change class."""

    root = _repository_root(repository)
    production = _exact_commit(root, production_commit)
    candidate = _exact_commit(root, candidate_commit)
    changed_paths = _changed_paths(root, production, candidate)
    production_fingerprint = _writer_fingerprint(root, production)
    candidate_fingerprint = _writer_fingerprint(root, candidate)
    schema_changed = any(
        path in SCHEMA_PATHS or path.startswith("migrations/")
        for path in changed_paths
    )
    return CandidateSurface(
        manifest_version=WRITER_MANIFEST_VERSION,
        production_writer_fingerprint=production_fingerprint,
        candidate_writer_fingerprint=candidate_fingerprint,
        writer_changed=production_fingerprint != candidate_fingerprint,
        schema_changed=schema_changed,
        changed_path_count=len(changed_paths),
    )


def _repository_root(repository: str | Path) -> Path:
    path = Path(repository).resolve()
    if not path.is_dir():
        raise WriterSurfaceError("git_repository_invalid")
    result = _git(path, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise WriterSurfaceError("git_repository_invalid")
    try:
        root = Path(result.stdout.decode("utf-8").strip()).resolve()
    except UnicodeDecodeError as exc:
        raise WriterSurfaceError("git_repository_invalid") from exc
    if root != path:
        raise WriterSurfaceError("git_repository_invalid")
    return root


def _exact_commit(root: Path, value: str) -> str:
    rendered = str(value).strip().lower()
    if _FULL_SHA_RE.fullmatch(rendered) is None:
        raise WriterSurfaceError("git_commit_invalid")
    result = _git(root, "cat-file", "-t", rendered)
    if result.returncode != 0 or result.stdout.strip() != b"commit":
        raise WriterSurfaceError("git_commit_invalid")
    return rendered


def _changed_paths(root: Path, production: str, candidate: str) -> frozenset[str]:
    result = _git(
        root,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        production,
        candidate,
        "--",
    )
    if result.returncode != 0:
        raise WriterSurfaceError("git_diff_invalid")
    try:
        paths = frozenset(
            item.decode("utf-8") for item in result.stdout.split(b"\0") if item
        )
    except UnicodeDecodeError as exc:
        raise WriterSurfaceError("git_diff_invalid") from exc
    if len(paths) > 100_000 or any(
        path.startswith("/") or ".." in Path(path).parts for path in paths
    ):
        raise WriterSurfaceError("git_diff_invalid")
    return paths


def _writer_fingerprint(root: Path, commit: str) -> str:
    result = _git(root, "ls-tree", "-rz", commit, "--")
    if result.returncode != 0:
        raise WriterSurfaceError("git_tree_invalid")
    entries: dict[str, tuple[str, str, str]] = {}
    try:
        for raw_entry in result.stdout.split(b"\0"):
            if not raw_entry:
                continue
            metadata, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
            path = raw_path.decode("utf-8")
            if path in WRITER_SURFACE_PATHS:
                entries[path] = (mode, object_type, object_id)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WriterSurfaceError("git_tree_invalid") from exc

    digest = sha256()
    digest.update(f"writer-manifest-v{WRITER_MANIFEST_VERSION}\0".encode("ascii"))
    for path in sorted(WRITER_SURFACE_PATHS):
        entry = entries.get(path)
        if entry is None:
            record = f"{path}\0missing\0"
        else:
            mode, object_type, object_id = entry
            record = f"{path}\0{mode}\0{object_type}\0{object_id}\0"
        digest.update(record.encode("utf-8"))
    return digest.hexdigest()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise WriterSurfaceError("git_unavailable") from exc
