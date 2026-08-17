"""Versioned, bounded Git change-surface classification for deployment."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Mapping

from .deployment_work_evidence import (
    DEPLOYMENT_CHANGE_CLASSES,
    WORK_EVIDENCE_ADAPTERS,
)


CHANGE_SURFACE_REGISTRY_VERSION = 5
_CLASS_RANK = {
    "code": 0,
    "schema_compatible": 1,
    "execution_writer": 2,
    "live_promotion": 3,
}

DEPLOYMENT_GUARD_PATHS = frozenset(
    {
        "src/telegram_kol_research/deployment_preflight.py",
        "src/telegram_kol_research/deployment_work_evidence.py",
        "src/telegram_kol_research/deployment_change_surface.py",
        "src/telegram_kol_research/deployment_preflight_cli.py",
        "deploy/telegram-kol-update",
        "scripts/bootstrap_server_updater.sh",
        "scripts/server_git_update.sh",
        "scripts/server_git_update.ps1",
    }
)

SCHEMA_PATHS = frozenset(
    {
        "src/telegram_kol_research/db.py",
        "src/telegram_kol_research/models.py",
    }
)

MUTATION_AUTHORITY_PATHS = frozenset(
    {
        "src/telegram_kol_research/position_attribution.py",
        "src/telegram_kol_research/position_authority_lock.py",
        "src/telegram_kol_research/position_mutation_authority.py",
        "src/telegram_kol_research/position_mutation_gateway.py",
        "src/telegram_kol_research/protection_attribution.py",
        "src/telegram_kol_research/protection_ledger.py",
        "src/telegram_kol_research/recovery_live_submit_gate.py",
        "src/telegram_kol_research/source_message_deletion.py",
    }
)

OUTCOME_AUTHORITY_PATHS = frozenset(
    {
        "src/telegram_kol_research/instruction_execution_entry_adapter.py",
        "src/telegram_kol_research/instruction_execution_management_adapter.py",
        "src/telegram_kol_research/instruction_execution_outcomes.py",
        "src/telegram_kol_research/instruction_execution_projection.py",
        "src/telegram_kol_research/recovery_order_confirmation.py",
        "src/telegram_kol_research/strategy_management_contracts.py",
        "src/telegram_kol_research/strategy_management_market_decisions.py",
        "src/telegram_kol_research/strategy_management_market_policy.py",
    }
)

EXECUTION_WRITER_PATHS = frozenset(
    {
        "src/telegram_kol_research/auto_trade_execution.py",
        "src/telegram_kol_research/break_even_convergence_executor.py",
        "src/telegram_kol_research/break_even_convergence_planner.py",
        "src/telegram_kol_research/break_even_convergence_worker.py",
        "src/telegram_kol_research/cli.py",
        "src/telegram_kol_research/deepcoin_client.py",
        "src/telegram_kol_research/deepcoin_execution_actions.py",
        "src/telegram_kol_research/entry_revision_executor.py",
        "src/telegram_kol_research/execution_bindings.py",
        "src/telegram_kol_research/instruction_execution_reconciliation.py",
        "src/telegram_kol_research/instruction_execution_contracts.py",
        "src/telegram_kol_research/legacy_conditional_cancel.py",
        "src/telegram_kol_research/native_tpsl_migration.py",
        "src/telegram_kol_research/message_instruction_items.py",
        "src/telegram_kol_research/position_mutation_gateway.py",
        "src/telegram_kol_research/position_mutation_intents.py",
        "src/telegram_kol_research/position_protection_legs.py",
        "src/telegram_kol_research/position_take_profit_orders.py",
        "src/telegram_kol_research/recovery_live_submit.py",
        "src/telegram_kol_research/source_message_deletion_worker.py",
        "src/telegram_kol_research/source_message_deletion.py",
        "src/telegram_kol_research/strategy_management_batches.py",
        "src/telegram_kol_research/strategy_management_composite_executor.py",
        "src/telegram_kol_research/strategy_management_composite_reconciliation.py",
        "src/telegram_kol_research/strategy_management_components.py",
        "src/telegram_kol_research/strategy_management_executor.py",
        "src/telegram_kol_research/strategy_management_reconciliation.py",
        "src/telegram_kol_research/strategy_management_worker.py",
        "src/telegram_kol_research/strategy_revision_planner.py",
        "src/telegram_kol_research/system_operator_bot.py",
        "src/telegram_kol_research/terminal_entry_cleanup.py",
        "src/telegram_kol_research/trade_signals.py",
        "src/telegram_kol_research/trigger_backup_stop_executor.py",
        "src/telegram_kol_research/trigger_take_profit_convergence_executor.py",
        "src/telegram_kol_research/trigger_take_profit_convergence.py",
        "src/telegram_kol_research/trigger_protection_intents.py",
        "src/telegram_kol_research/trigger_protection_rescue_worker.py",
        "src/telegram_kol_research/web_app.py",
    }
) | MUTATION_AUTHORITY_PATHS | OUTCOME_AUTHORITY_PATHS

LIVE_PROMOTION_PATHS = frozenset(
    {
        "src/telegram_kol_research/authoritative_recognition.py",
        "src/telegram_kol_research/prompt_composition.py",
        "src/telegram_kol_research/prompt_defaults.py",
        "src/telegram_kol_research/prompt_registry.py",
        "src/telegram_kol_research/trading_settings.py",
    }
)

_RETIREMENT_PRODUCTION_COMMIT = "2274d90bd2b1a5bb7e7ed1c420c30e925d2bbdfa"
_RETIREMENT_REVIEW_COMMIT = "7813150b7b33cd8ce3d90a6145889c6fef192dc7"
_REVIEWED_RETIREMENT_RISK_PATHS = frozenset(
    LIVE_PROMOTION_PATHS | EXECUTION_WRITER_PATHS
)


class ChangeSurfaceError(ValueError):
    """The Git objects or requested change classification are malformed."""


@dataclass(frozen=True, slots=True)
class ChangeSurfaceFacts:
    registry_version: int
    effective_change_class: str
    underdeclared: bool
    changed_path_count: int
    change_surface_fingerprint: str
    restart_compatibility_changed: bool
    restart_handler_fingerprint: str
    blocking_reason_codes: tuple[str, ...]


def bind_phase_restart_surface_counts(
    preliminary_counts: Mapping[str, Mapping[str, int]],
    current_counts: Mapping[str, Mapping[str, int]],
) -> dict[str, dict[str, int]]:
    """Keep Phase A's restart-handler universe fixed through Phase B."""

    bound: dict[str, dict[str, int]] = {}
    for classification, source_counts in current_counts.items():
        if not isinstance(classification, str) or not isinstance(
            source_counts, Mapping
        ):
            raise ChangeSurfaceError("change_surface_malformed")
        bound[classification] = dict(source_counts)
    for classification in ("restart_safe_wait", "historical_residue"):
        source_counts = preliminary_counts.get(classification, {})
        if not isinstance(source_counts, Mapping):
            raise ChangeSurfaceError("change_surface_malformed")
        target = bound.setdefault(classification, {})
        for source, count in source_counts.items():
            if (
                not isinstance(source, str)
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
            ):
                raise ChangeSurfaceError("change_surface_malformed")
            if count > 0:
                current = target.get(source, 0)
                if not isinstance(current, int) or isinstance(current, bool):
                    raise ChangeSurfaceError("change_surface_malformed")
                target[source] = max(current, count)
    return bound


def classify_change_surface(
    *,
    repository: str | Path,
    production_commit: str,
    candidate_commit: str,
    requested_change_class: str,
    work_classification_counts: Mapping[str, Mapping[str, int]] | None = None,
) -> ChangeSurfaceFacts:
    """Classify exact Git objects; the requested class is only a lower bound."""

    requested = str(requested_change_class).strip().lower()
    if requested not in DEPLOYMENT_CHANGE_CLASSES:
        raise ChangeSurfaceError("change_surface_malformed")
    root = Path(repository).resolve()
    production = _commit(root, production_commit)
    candidate = _commit(root, candidate_commit)
    changes = _changed_paths(root, production, candidate)
    surface_fingerprint = _surface_fingerprint(
        root,
        production,
        candidate,
        changes,
    )
    observed = "code"
    for path in changes:
        path_class = "code"
        if path in SCHEMA_PATHS or path.startswith("migrations/"):
            path_class = "schema_compatible"
        elif _is_unchanged_reviewed_retirement_path(
            root,
            production,
            candidate,
            path,
        ):
            path_class = "code"
        elif path in LIVE_PROMOTION_PATHS:
            path_class = "live_promotion"
        elif path in EXECUTION_WRITER_PATHS:
            path_class = "execution_writer"
        elif path in DEPLOYMENT_GUARD_PATHS:
            path_class = "code"
        elif path.startswith("src/telegram_kol_research/"):
            # Runtime helpers are writer-sensitive by default. A new indirect
            # authority, projector, payload builder, or retry primitive must
            # never silently inherit the ordinary-code deployment policy.
            path_class = "execution_writer"
        if _CLASS_RANK[path_class] > _CLASS_RANK[observed]:
            observed = path_class
    effective = max((requested, observed), key=_CLASS_RANK.__getitem__)
    underdeclared = _CLASS_RANK[observed] > _CLASS_RANK[requested]

    restart_paths = _restart_paths_for_present_work(
        work_classification_counts or {}
    )
    restart_changed, restart_fingerprint = _restart_compatibility(
        root,
        production,
        candidate,
        restart_paths,
    )
    blocking: set[str] = set()
    if underdeclared:
        blocking.add("change_class_underdeclared")
    if restart_changed:
        blocking.add("restart_compatibility_changed")
    return ChangeSurfaceFacts(
        registry_version=CHANGE_SURFACE_REGISTRY_VERSION,
        effective_change_class=effective,
        underdeclared=underdeclared,
        changed_path_count=len(changes),
        change_surface_fingerprint=surface_fingerprint,
        restart_compatibility_changed=restart_changed,
        restart_handler_fingerprint=restart_fingerprint,
        blocking_reason_codes=tuple(sorted(blocking)),
    )


def _commit(repository: Path, value: str) -> str:
    candidate = str(value).strip().lower()
    if len(candidate) != 40 or any(
        char not in "0123456789abcdef" for char in candidate
    ):
        raise ChangeSurfaceError("change_surface_malformed")
    try:
        resolved = _git(repository, "rev-parse", "--verify", f"{candidate}^{{commit}}")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ChangeSurfaceError("change_surface_malformed") from exc
    if resolved != candidate:
        raise ChangeSurfaceError("change_surface_malformed")
    return resolved


def _changed_paths(
    repository: Path,
    production: str,
    candidate: str,
) -> tuple[str, ...]:
    try:
        output = _git_bytes(
            repository,
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            production,
            candidate,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ChangeSurfaceError("change_surface_malformed") from exc
    paths = tuple(sorted(item.decode("utf-8") for item in output.split(b"\0") if item))
    if any(path.startswith("/") or ".." in Path(path).parts for path in paths):
        raise ChangeSurfaceError("change_surface_malformed")
    return paths


def _surface_fingerprint(
    repository: Path,
    production: str,
    candidate: str,
    paths: tuple[str, ...],
) -> str:
    facts = [
        {
            "path": path,
            "production_blob": _blob(repository, production, path),
            "candidate_blob": _blob(repository, candidate, path),
        }
        for path in paths
    ]
    return _fingerprint(facts)


def _is_unchanged_reviewed_retirement_path(
    repository: Path,
    production: str,
    candidate: str,
    path: str,
) -> bool:
    if (
        production != _RETIREMENT_PRODUCTION_COMMIT
        or (
            path not in _REVIEWED_RETIREMENT_RISK_PATHS
            and not path.startswith("src/telegram_kol_research/")
        )
    ):
        return False
    ancestry = subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "merge-base",
            "--is-ancestor",
            _RETIREMENT_REVIEW_COMMIT,
            candidate,
        ],
        check=False,
        capture_output=True,
    )
    return ancestry.returncode == 0 and (
        _blob(repository, candidate, path)
        == _blob(repository, _RETIREMENT_REVIEW_COMMIT, path)
    )


def _restart_paths_for_present_work(
    counts: Mapping[str, Mapping[str, int]],
) -> frozenset[str]:
    present_sources = {
        source
        for classification in ("restart_safe_wait", "historical_residue")
        for source, count in counts.get(classification, {}).items()
        if isinstance(count, int) and not isinstance(count, bool) and count > 0
    }
    return frozenset(
        path
        for adapter in WORK_EVIDENCE_ADAPTERS
        if adapter.output_name in present_sources
        for path in adapter.restart_surface_files
    )


def _restart_compatibility(
    repository: Path,
    production: str,
    candidate: str,
    paths: frozenset[str],
) -> tuple[bool, str]:
    facts = [
        {
            "path": path,
            "production_blob": _blob(repository, production, path),
            "candidate_blob": _blob(repository, candidate, path),
        }
        for path in sorted(paths)
    ]
    changed = any(
        fact["production_blob"] != fact["candidate_blob"] for fact in facts
    )
    return changed, _fingerprint(facts)


def _blob(repository: Path, commit: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", f"{commit}:{path}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip().lower()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ChangeSurfaceError("change_surface_malformed")
    return value


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_bytes(repository: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
    ).stdout


def _fingerprint(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return sha256(body.encode("utf-8")).hexdigest()
