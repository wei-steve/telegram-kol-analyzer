"""Architecture test: the `one_off` sub-package must stay off every online path.

`src/telegram_kol_research/one_off/` (cleanup step 5,
docs/plans/2026-09-06-post-migration-cleanup/step-5-inventory.md) holds
historical data-repair tools whose target rows were already dealt with and
whose plans the docs mark as evidence-only. They are kept for traceability,
not to be run again, so nothing that a running role process can reach may
import them.

The check is static rather than import-based on purpose: an online module that
imports a one-off tool only inside a rarely taken branch would still pass a
runtime probe, and that is exactly the drift this guard is for. `cli.py` is the
one allowed importer - the CLI is where an operator would invoke such a tool by
hand, and no lifespan task, singleton loop, or worker reaches it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"
ONE_OFF_PACKAGE = "telegram_kol_research.one_off"

# `cli.py` may import one-off tools: it is operator-invoked, never a role process.
ALLOWED_IMPORTERS = frozenset({"cli.py"})


def _imported_module_names(tree: ast.AST, module_path: Path) -> set[str]:
    """Absolute names of every `telegram_kol_research.*` module the file imports."""

    package_depth = len(module_path.relative_to(PACKAGE_ROOT).parts) - 1
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from . import x` / `from ..pkg import x` inside the package.
                ancestor = ["telegram_kol_research"]
                kept = package_depth - (node.level - 1)
                ancestor.extend(module_path.relative_to(PACKAGE_ROOT).parts[:kept])
                base = ".".join(ancestor + ([node.module] if node.module else []))
            elif node.module:
                base = node.module
            else:
                continue
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _online_module_paths() -> list[Path]:
    return sorted(
        path
        for path in PACKAGE_ROOT.rglob("*.py")
        if "one_off" not in path.relative_to(PACKAGE_ROOT).parts
        and path.name not in ALLOWED_IMPORTERS
    )


@pytest.mark.architecture
def test_one_off_package_is_not_imported_outside_itself() -> None:
    offenders: list[str] = []
    for path in _online_module_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imported_module_names(tree, path):
            if name == ONE_OFF_PACKAGE or name.startswith(f"{ONE_OFF_PACKAGE}."):
                offenders.append(f"{path.relative_to(PACKAGE_ROOT)} -> {name}")

    assert not offenders, (
        "one-off repair modules must stay off every online path; "
        "only cli.py may import them: " + ", ".join(sorted(offenders))
    )


@pytest.mark.architecture
def test_one_off_package_exists_and_is_discovered() -> None:
    """Guard the guard: a renamed or emptied package must not silently pass."""

    one_off_root = PACKAGE_ROOT / "one_off"
    assert (one_off_root / "__init__.py").is_file()
    modules = sorted(
        path.name for path in one_off_root.glob("*.py") if path.name != "__init__.py"
    )
    assert modules, "one_off/ holds no modules; the isolation guard would be vacuous"
    assert _online_module_paths(), "no online modules were scanned"
