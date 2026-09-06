"""Census of synchronous ticks called directly from async loops.

Phase 0 of the runtime serialization remediation only measures. This test
freezes the current set of blocking calls so that a new one cannot be added
silently, and so that each remediation phase can shrink the allowlist.
"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"

# Every entry must name the phase that removes it, or why it may stay.
#
# Phase 1d widened the matcher (imported callees, any name), so this list is no
# longer the empty set it reached under the narrow rule. Emptiness there was
# false assurance: the call causing every production stall was invisible to it.
#
# The three Bot database calls formerly listed here are owned by the per-chat
# activation event-loop optimization and now run off the event loop. The
# remaining entries are reviewed pure helpers retained because this static
# matcher cannot prove purity.
KNOWN_BLOCKING_CALLS = frozenset(
    {
        # ── pure helpers: no I/O, microseconds, safe on the loop ──
        # Kept because the matcher cannot prove purity statically. Reviewed by
        # hand in Phase 1d; none touches a session, a client, or the network.
        # Exponential backoff with jitter: arithmetic plus one ``random()``
        # draw, no session, no client, no network. Reviewed for the phase 2
        # REST+WebSocket work.
        "deepcoin_private_ws.run_forever -> compute_backoff_delay",
        "lifecycle_monitor._fetch_candles_full -> _candle_from_payload",
        "lifecycle_monitor._scan_contract -> _utc_naive",
        "semantic_disagreement_review.run_semantic_review_loop -> utc_now",
        "telegram_bot_commands.run_system_operator_bot_command_loop"
        " -> _callback_operator_name",
        "telegram_bot_commands.run_system_operator_bot_command_loop"
        " -> _command_name",
        "telegram_bot_commands.run_system_operator_bot_command_loop"
        " -> _expiry_callback_needs_deepcoin_client",
        "telegram_bot_commands.run_system_operator_bot_command_loop"
        " -> _log_system_operator_callback_processed",
        "telegram_bot_commands.run_system_operator_bot_command_loop"
        " -> _message_is_from_alert_chat",
        "telegram_bot_commands.run_telegram_bot_command_loop -> _is_pending_command",
        "telegram_bot_commands.run_telegram_bot_command_loop -> _is_positions_command",
        "telegram_bot_commands.run_telegram_bot_command_loop"
        " -> _message_is_from_alert_chat",
        "telegram_bot_commands.run_telegram_bot_command_loop -> split_telegram_message",
        "web_app._supervise_semantic_review_runner -> _build_semantic_review_notifier",
        "web_app.run_deepcoin_execution_reconcile_loop -> system_operator_bot_enabled",
    }
)


def _module_level_sync_functions(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _module_level_async_functions(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _imported_names(tree: ast.Module) -> set[str]:
    """Names bound by `from x import y` at module level.

    Phase 1d widened the census to include these. The call it missed for three
    phases, ``reconcile_deepcoin_execution_bindings``, was imported rather than
    defined locally, which the same-module-only rule silently exempted.
    """

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


# Callables that are safe to invoke on the loop: they do not block.
_NON_BLOCKING_BUILTINS = frozenset({
    "bool", "dict", "float", "getattr", "hasattr", "int", "isinstance", "len",
    "list", "max", "min", "next", "range", "repr", "round", "set", "sorted",
    "str", "tuple", "type", "zip", "enumerate", "any", "all", "abs", "iter",
    "print", "id", "callable", "format", "sum",
})


def _blocking_calls_in_async_loop(
    async_def: ast.AsyncFunctionDef,
    *,
    sync_functions: set[str],
    async_functions: set[str] | None = None,
    imported_names: set[str] | None = None,
) -> set[str]:
    """Report calls made directly inside an async while-loop that block it.

    Widened in Phase 1d. It previously required the callee to be BOTH named
    ``*_tick``/``*_once`` AND defined in the same module. The call that caused
    every stall in production satisfied neither, so the census reported zero
    offenders while the loop stalled once every thirty-seven seconds.

    Now any call to a module-level synchronous function counts, whether it is
    defined locally or imported, whatever it is named. Still exempt, because
    they do not run the callee on the loop: awaited calls, and functions passed
    by bare name to ``asyncio.to_thread`` or ``run_in_executor``.
    """

    async_functions = async_functions or set()
    imported_names = imported_names or set()

    awaited: set[int] = set()
    for node in ast.walk(async_def):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            awaited.add(id(node.value))

    candidates = (sync_functions | imported_names) - async_functions

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
            if func.id in _NON_BLOCKING_BUILTINS:
                continue
            if func.id not in candidates:
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
        async_functions = _module_level_async_functions(tree)
        imported_names = _imported_names(tree)
        module = path.relative_to(SOURCE_ROOT).with_suffix("").as_posix().replace("/", ".")
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for entry in _blocking_calls_in_async_loop(
                node,
                sync_functions=sync_functions,
                async_functions=async_functions,
                imported_names=imported_names,
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
