"""Static boundaries for the on-call watcher (spec sections 1, 3 and 7).

Phase 1 is a detector with no authority: no exchange client, no write path,
no SQLAlchemy bootstrap against production, and -- until phase 2 is separately
approved -- no ``codex exec``. These are cheap to assert and expensive to
notice by hand, so they are asserted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


ONCALL_MODULES = (
    "oncall_state.py",
    "oncall_detector.py",
    "oncall_alerts.py",
    "oncall_service.py",
)

#: Spec section 1. None of these may appear in the watcher's import closure.
FORBIDDEN_MODULE_FRAGMENTS = (
    "deepcoin_client",
    "deepcoin_execution_actions",
    "position_mutation_gateway",
    "strategy_management_executor",
    "worker_command_jobs",
    "worker_command_executor",
    "position_authority_lock",
    "auto_trade_execution",
    "sqlalchemy",
)

#: Package modules each watcher module may import. Everything else is stdlib.
ALLOWED_PACKAGE_IMPORTS = {
    "oncall_state.py": frozenset(),
    "oncall_detector.py": frozenset({"oncall_state"}),
    "oncall_alerts.py": frozenset({"oncall_state"}),
    "oncall_service.py": frozenset({"oncall_alerts", "oncall_detector", "oncall_state"}),
}

SOURCE_ROOT = Path(__file__).parents[1] / "src" / "telegram_kol_research"
PROJECT_ROOT = Path(__file__).parents[1]


def code_without_prose(filename: str) -> str:
    """The module's executable code only.

    Comments and docstrings are where this phase *describes* what it must not
    do ("no ``codex exec``", "no ``parse_mode``"), so a plain substring search
    over the file would fail on its own explanation. Round-tripping through
    the AST with docstrings removed leaves exactly the code.
    """

    tree = ast.parse((SOURCE_ROOT / filename).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(ast.fix_missing_locations(tree))


def _package_imports(tree: ast.AST) -> list[str]:
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                imported.append(node.module or "")
            else:
                imported.append(node.module or "")
    return imported


@pytest.mark.architecture
@pytest.mark.parametrize("filename", ONCALL_MODULES)
def test_the_watcher_imports_no_execution_or_exchange_module(filename):
    path = SOURCE_ROOT / filename
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for imported in _package_imports(tree):
        lowered = imported.lower()
        assert not any(
            fragment in lowered for fragment in FORBIDDEN_MODULE_FRAGMENTS
        ), f"{filename} imports a forbidden module: {imported}"
        if not lowered.startswith("telegram_kol_research"):
            continue
        module = imported.removeprefix("telegram_kol_research.").split(".", 1)[0]
        assert module in ALLOWED_PACKAGE_IMPORTS[filename], (
            f"{filename} imports non-allowlisted application module: {module}"
        )


@pytest.mark.architecture
def test_the_watchers_import_closure_is_only_itself():
    """The allowlist above is transitively closed, so no indirect route exists."""

    closure = set()
    frontier = {name.removesuffix(".py") for name in ONCALL_MODULES}
    while frontier:
        module = frontier.pop()
        if module in closure:
            continue
        closure.add(module)
        frontier |= set(ALLOWED_PACKAGE_IMPORTS[f"{module}.py"])

    assert closure == {name.removesuffix(".py") for name in ONCALL_MODULES}


@pytest.mark.architecture
def test_the_production_database_is_opened_read_only_and_query_only():
    source = (SOURCE_ROOT / "oncall_detector.py").read_text(encoding="utf-8")

    assert "?mode=ro" in source
    assert "PRAGMA query_only=ON" in source
    assert "mode=rw" not in source
    assert "create_engine" not in source
    assert "create_session_factory" not in source


@pytest.mark.architecture
@pytest.mark.parametrize("filename", ONCALL_MODULES)
def test_phase_one_runs_no_command_and_restarts_no_service(filename):
    code = code_without_prose(filename)

    assert "subprocess" not in code
    assert "os.system" not in code
    assert "systemctl" not in code
    assert "codex" not in code.lower()


@pytest.mark.architecture
def test_the_watcher_only_ever_sends_to_telegram_and_the_local_health_endpoint():
    alerts = code_without_prose("oncall_alerts.py")
    service = code_without_prose("oncall_service.py")

    assert "https://api.telegram.org/bot" in alerts
    # Untrusted KOL text is sent as plain text: no parse_mode is ever set.
    assert "parse_mode" not in alerts
    assert "deepcoin" not in (alerts + service).lower()


@pytest.mark.architecture
def test_the_systemd_unit_is_a_watchdogged_read_only_sandbox():
    unit = (
        PROJECT_ROOT / "deploy" / "systemd" / "telegram-kol-oncall.service"
    ).read_text(encoding="utf-8")

    for required in (
        "Type=notify",
        "NotifyAccess=main",
        "WatchdogSec=300",
        "Restart=always",
        "RestartSec=10",
        # Without this, mode=off would restart-loop every ten seconds.
        "RestartPreventExitStatus=0",
        "User=telegram-kol-oncall",
        "StateDirectory=telegram-kol-oncall",
        "EnvironmentFile=/etc/telegram-kol-oncall.env",
        "ConditionPathExists=/etc/telegram-kol-oncall.env",
        "TemporaryFileSystem=/opt/telegram-kol-analyzer:ro",
        "BindReadOnlyPaths=/opt/telegram-kol-analyzer/data/research.db",
        "ReadWritePaths=/var/lib/telegram-kol-oncall",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "oncall-watch",
    ):
        assert required in unit, required

    # The worker's own environment file must never be readable from here.
    assert "telegram-kol-worker.env" not in unit
    assert "ReadWritePaths=/opt/telegram-kol-analyzer" not in unit


@pytest.mark.architecture
def test_the_environment_example_has_placeholders_and_no_secret():
    example = (PROJECT_ROOT / "config" / "oncall.env.example").read_text(encoding="utf-8")

    assert "TELEGRAM_KOL_ONCALL_MODE=off" in example
    assert "TELEGRAM_KOL_ONCALL_BOT_TOKEN=" in example
    assert "TELEGRAM_KOL_ONCALL_CHAT_ID=" in example
    assert "TELEGRAM_KOL_ONCALL_DAILY_ALERT_CAP=30" in example
    assert (
        "TELEGRAM_KOL_ONCALL_WORKER_HEALTH_URL="
        "http://127.0.0.1:8002/api/runtime/loop-health" in example
    )
    for line in example.splitlines():
        if line.startswith("TELEGRAM_KOL_ONCALL_BOT_TOKEN="):
            assert line.strip() == "TELEGRAM_KOL_ONCALL_BOT_TOKEN="
