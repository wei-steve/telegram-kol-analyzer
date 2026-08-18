"""Census of synchronous ticks called directly from async loops.

Phase 0 of the runtime serialization remediation only measures. This test
freezes the current set of blocking calls so that a new one cannot be added
silently, and so that each remediation phase can shrink the allowlist.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"

# Every entry must name the phase that removes it.
KNOWN_BLOCKING_CALLS = frozenset(
    {
        # Discovered by the Phase 0 census, beyond the two already identified.
        # Recorded in docs/runtime-serialization-remediation-status.md; not yet
        # assigned to a remediation phase.
        "system_operator_bot.run_runtime_incident_notification_loop"
        " -> run_operator_maintenance_tick",
    }
)


def _module_level_sync_functions(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _blocking_calls_in_async_loop(
    async_def: ast.AsyncFunctionDef,
    *,
    sync_functions: set[str],
) -> set[str]:
    awaited: set[int] = set()
    for node in ast.walk(async_def):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            awaited.add(id(node.value))

    found: set[str] = set()
    for node in ast.walk(async_def):
        if not isinstance(node, ast.While):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if not isinstance(func, ast.Name):
                continue
            if not (func.id.endswith("_tick") or func.id.endswith("_once")):
                continue
            if func.id not in sync_functions:
                continue
            if id(inner) in awaited:
                continue
            found.add(f"{async_def.name} -> {func.id}")
    return found


def discover_blocking_calls() -> set[str]:
    """Report async while-loops that call a same-module sync tick directly.

    Calls wrapped in ``await``, ``asyncio.to_thread`` or ``run_in_executor``
    are not reported: the first is awaited, and the latter two receive the
    function as a bare name rather than calling it.
    """

    discovered: set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sync_functions = _module_level_sync_functions(tree)
        module = path.relative_to(SOURCE_ROOT).with_suffix("").as_posix().replace("/", ".")
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for entry in _blocking_calls_in_async_loop(
                node, sync_functions=sync_functions
            ):
                discovered.add(f"{module}.{entry}")
    return discovered


def test_blocking_call_census_matches_the_explicit_allowlist():
    discovered = discover_blocking_calls()

    assert discovered == set(KNOWN_BLOCKING_CALLS), {
        "unexpected": sorted(discovered - KNOWN_BLOCKING_CALLS),
        "resolved": sorted(KNOWN_BLOCKING_CALLS - discovered),
    }


def test_census_detects_a_newly_introduced_blocking_call(tmp_path):
    module = tmp_path / "sample_worker.py"
    module.write_text(
        "import asyncio\n"
        "\n"
        "def run_sample_tick():\n"
        "    return None\n"
        "\n"
        "async def run_sample_loop():\n"
        "    while True:\n"
        "        run_sample_tick()\n"
        "        await asyncio.sleep(1)\n",
        encoding="utf-8",
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    async_def = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
    )

    assert _blocking_calls_in_async_loop(
        async_def, sync_functions=_module_level_sync_functions(tree)
    ) == {"run_sample_loop -> run_sample_tick"}


def test_census_ignores_ticks_moved_off_the_event_loop(tmp_path):
    module = tmp_path / "sample_worker.py"
    module.write_text(
        "import asyncio\n"
        "\n"
        "def run_sample_tick():\n"
        "    return None\n"
        "\n"
        "async def run_sample_loop():\n"
        "    while True:\n"
        "        await asyncio.to_thread(run_sample_tick)\n"
        "        await asyncio.sleep(1)\n",
        encoding="utf-8",
    )
    tree = ast.parse(module.read_text(encoding="utf-8"))
    async_def = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
    )

    assert (
        _blocking_calls_in_async_loop(
            async_def, sync_functions=_module_level_sync_functions(tree)
        )
        == set()
    )
