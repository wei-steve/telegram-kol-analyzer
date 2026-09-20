"""Static boundaries for the on-call watcher (phase 1 spec 1/3/7, phase 2 9.6/9.7).

Phase 1 is a detector with no authority: no exchange client, no write path, no
SQLAlchemy bootstrap against production. Phase 2 adds a diagnosis that can
only ever become a Telegram message: still no exchange client, no worker
request, no production write -- and a root-side runner whose entire import
closure is the standard library plus one dependency-free module.

These are cheap to assert and expensive to notice by hand, so they are
asserted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


#: Phase 1's four modules, which still run no command of any kind.
PHASE_ONE_MODULES = (
    "oncall_state.py",
    "oncall_detector.py",
    "oncall_alerts.py",
    "oncall_service.py",
)

#: Phase 2's three.
PHASE_TWO_MODULES = (
    "oncall_casefile.py",
    "oncall_codex.py",
    "oncall_codex_runner.py",
)

ONCALL_MODULES = PHASE_ONE_MODULES + PHASE_TWO_MODULES

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
    # Dependency-free on purpose: the root-side runner imports it, and its
    # closure is what decides what runs as root next to an OpenAI connection.
    "oncall_codex.py": frozenset(),
    "oncall_detector.py": frozenset({"oncall_state"}),
    "oncall_alerts.py": frozenset({"oncall_codex", "oncall_state"}),
    "oncall_casefile.py": frozenset({"oncall_codex", "oncall_detector", "oncall_state"}),
    "oncall_codex_runner.py": frozenset({"oncall_codex"}),
    "oncall_service.py": frozenset(
        {
            "oncall_alerts",
            "oncall_casefile",
            "oncall_codex",
            "oncall_detector",
            "oncall_state",
        }
    ),
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
def test_the_import_allowlist_matches_what_the_modules_actually_import():
    """A stale allowlist would make every test above vacuous."""

    for filename in ONCALL_MODULES:
        tree = ast.parse((SOURCE_ROOT / filename).read_text(encoding="utf-8"))
        actual = {
            imported.removeprefix("telegram_kol_research.").split(".", 1)[0]
            for imported in _package_imports(tree)
            if imported.startswith("telegram_kol_research")
        }
        assert actual <= ALLOWED_PACKAGE_IMPORTS[filename], filename


@pytest.mark.architecture
def test_the_production_database_is_opened_read_only_and_query_only():
    source = (SOURCE_ROOT / "oncall_detector.py").read_text(encoding="utf-8")

    assert "?mode=ro" in source
    assert "PRAGMA query_only=ON" in source
    assert "mode=rw" not in source
    assert "create_engine" not in source
    assert "create_session_factory" not in source


@pytest.mark.architecture
@pytest.mark.parametrize("filename", PHASE_ONE_MODULES)
def test_phase_one_runs_no_command_and_restarts_no_service(filename):
    code = code_without_prose(filename)

    assert "subprocess" not in code
    assert "os.system" not in code
    assert "systemctl" not in code
    # Phase 1 may now *name* the codex contracts (the wording of a verdict is
    # written here), but it still runs nothing: with no subprocess in the
    # module, there is no route from a name to an execution.
    assert "build_codex_command" not in code
    assert "run_codex_exec" not in code


@pytest.mark.architecture
@pytest.mark.parametrize("filename", PHASE_TWO_MODULES)
def test_phase_two_restarts_no_service_and_runs_only_what_it_declares(filename):
    """Phase 2 may run exactly two programs, and neither of them is a shell."""

    code = code_without_prose(filename)

    assert "os.system" not in code
    assert "systemctl" not in code
    assert "shell=True" not in code
    if filename == "oncall_codex.py":
        assert "subprocess" not in code, "the contracts module runs nothing"
    if filename == "oncall_casefile.py":
        # The journal excerpt is the only command the watcher side runs, and
        # its argv is a fixed list built in code. The watcher never runs codex
        # -- that is the whole reason the runner is a separate process.
        assert "build_codex_command" not in code
        assert "run_codex_exec" not in code
        assert code.count("subprocess.run") == 1
        assert "journalctl" in code


@pytest.mark.architecture
def test_the_diagnosis_can_only_ever_become_a_telegram_message():
    """Phase 2's hard limit: no remediation, no worker request, no exchange."""

    for filename in PHASE_TWO_MODULES:
        code = code_without_prose(filename).lower()
        for forbidden in (
            "worker_command",
            "position_mutation_gateway",
            "remediation",
            "apply_planned_action",
            "/fix",
            "requests.post",
            "urllib.request.urlopen",
        ):
            assert forbidden not in code, f"{filename} mentions {forbidden}"


@pytest.mark.architecture
def test_the_runners_import_closure_is_the_standard_library_plus_one_module():
    closure: set[str] = set()
    frontier = {"oncall_codex_runner"}
    while frontier:
        module = frontier.pop()
        if module in closure:
            continue
        closure.add(module)
        frontier |= set(ALLOWED_PACKAGE_IMPORTS[f"{module}.py"])

    assert closure == {"oncall_codex_runner", "oncall_codex"}


@pytest.mark.architecture
def test_the_runner_never_reaches_for_a_more_permissive_sandbox():
    source = (SOURCE_ROOT / "oncall_codex.py").read_text(encoding="utf-8")

    assert "read-only" in source
    assert "danger-full-access" not in source
    assert "workspace-write" not in source


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


CODEX_UNIT = (
    PROJECT_ROOT / "deploy" / "systemd" / "telegram-kol-oncall-codex.service"
)


@pytest.mark.architecture
def test_the_codex_unit_gives_root_an_allowlist_and_no_capabilities():
    unit = CODEX_UNIT.read_text(encoding="utf-8")

    for required in (
        "User=root",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "NoNewPrivileges=true",
        "ProtectSystem=strict",
        "ProtectHome=tmpfs",
        "PrivateTmp=true",
        "PrivateDevices=true",
        "ProtectKernelTunables=true",
        "ProtectKernelModules=true",
        "ProtectKernelLogs=true",
        "ProtectControlGroups=true",
        "UMask=0077",
        "Restart=always",
        "RestartSec=15",
        "ConditionPathExists=/etc/telegram-kol-oncall.env",
        "python -B -m telegram_kol_research.oncall_codex_runner",
    ):
        assert required in unit, required

    for tree in ("/etc", "/opt", "/var", "/srv", "/data", "/mnt", "/media"):
        assert f"TemporaryFileSystem={tree}:ro" in unit, tree

    for path in (
        "/etc/ssl",
        "/etc/pki",
        "/etc/ca-certificates",
        "/etc/resolv.conf",
        "/etc/hosts",
        "/etc/nsswitch.conf",
        "/etc/passwd",
        "/etc/group",
        "/etc/localtime",
        "/opt/telegram-kol-analyzer/src",
        "/opt/telegram-kol-analyzer/.venv",
    ):
        assert f"BindReadOnlyPaths=-{path}\n" in unit or f"BindReadOnlyPaths={path}\n" in unit, path

    assert "BindPaths=/root/.codex" in unit
    assert "BindPaths=/var/lib/telegram-kol-oncall/codex-spool" in unit
    # The spool is the only place the two units meet, and the group is what
    # lets the unprivileged watcher read what root wrote there.
    assert "chown root:telegram-kol-oncall" in unit
    assert "chmod 2770" in unit


@pytest.mark.architecture
def test_the_codex_unit_does_not_add_the_options_that_would_break_the_sandbox():
    """This kernel has no landlock, so codex falls back to userns + seccomp."""

    unit = CODEX_UNIT.read_text(encoding="utf-8")
    executable = "\n".join(
        line
        for line in unit.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )

    for forbidden in (
        "RestrictNamespaces",
        "SystemCallFilter",
        "PrivateUsers",
        "MemoryDenyWriteExecute",
    ):
        assert forbidden not in executable, forbidden


@pytest.mark.architecture
def test_the_codex_unit_mounts_nothing_that_holds_a_secret():
    unit = CODEX_UNIT.read_text(encoding="utf-8")

    assert "research.db" not in unit
    for line in unit.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        # These two name the settings file by design; every other line must
        # not mention an environment file or a settings directory at all.
        if stripped.startswith(("EnvironmentFile=", "ConditionPathExists=")):
            continue
        assert ".env" not in stripped, stripped
        assert "config" not in stripped.lower(), stripped
    assert "telegram-kol-worker.env" not in unit


@pytest.mark.architecture
def test_the_sandbox_probe_reuses_the_units_own_properties():
    """A hand-copied second list is how a probe drifts from what it checks."""

    import importlib.util

    path = PROJECT_ROOT / "scripts" / "oncall_codex_sandbox_probe.py"
    spec = importlib.util.spec_from_file_location("oncall_codex_sandbox_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    unit = CODEX_UNIT.read_text(encoding="utf-8")
    properties = module.parse_sandbox_properties(unit)

    assert "CapabilityBoundingSet=" in properties
    assert "TemporaryFileSystem=/etc:ro" in properties
    assert "BindPaths=/root/.codex" in properties
    assert not any(prop.startswith(("ExecStart", "Restart", "Description")) for prop in properties)
    # Every Bind*/TemporaryFileSystem line in the unit is carried across.
    for line in unit.splitlines():
        stripped = line.strip()
        if stripped.startswith(("TemporaryFileSystem=", "BindReadOnlyPaths=", "BindPaths=")):
            assert stripped in properties, stripped

    command = module.build_systemd_run_command(properties, script="echo hi")
    assert command[0] == "systemd-run"
    assert "--property=BindPaths=/root/.codex" in command
    assert command[-2:] == ["-c", "echo hi"]


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
