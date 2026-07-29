from __future__ import annotations

import ast
from pathlib import Path

import pytest


RUNTIME_AGENT_MODULES = (
    "runtime_incidents.py",
    "runtime_incident_adapters.py",
    "runtime_agent_contracts.py",
    "runtime_agent_tools.py",
    "runtime_agent_prompt.py",
    "runtime_agent_worker.py",
    "runtime_agent_evaluation.py",
    "runtime_incident_handoff.py",
    "runtime_agent_playbooks.py",
    "runtime_agent_policy.py",
    "runtime_agent_executor.py",
)
FORBIDDEN_SYMBOL_FRAGMENTS = (
    "recognize",
    "resolve_context",
    "select_strategy",
    "execute_management",
    "place_order",
    "cancel_order",
    "close_position",
)
ALLOWED_PACKAGE_IMPORTS_BY_MODULE = {
    "runtime_incidents.py": frozenset({"models", "runtime_agent_playbooks"}),
    "runtime_incident_adapters.py": frozenset({"config", "runtime_incidents"}),
    "runtime_agent_contracts.py": frozenset(),
    "runtime_agent_tools.py": frozenset({"runtime_agent_contracts"}),
    "runtime_agent_prompt.py": frozenset(
        {"runtime_agent_contracts", "runtime_agent_playbooks"}
    ),
    "runtime_agent_worker.py": frozenset(
        {
            "llm_chat",
            "runtime_agent_contracts",
            "runtime_agent_executor",
            "runtime_agent_playbooks",
            "runtime_agent_policy",
            "runtime_agent_tools",
            "runtime_agent_prompt",
            "runtime_incident_handoff",
            "runtime_incidents",
        }
    ),
    "runtime_agent_playbooks.py": frozenset(),
    "runtime_agent_policy.py": frozenset({"runtime_agent_playbooks"}),
    "runtime_agent_executor.py": frozenset(
        {"models", "runtime_agent_policy", "runtime_agent_tools"}
    ),
    "runtime_agent_evaluation.py": frozenset(
        {"runtime_agent_policy"}
    ),
    "runtime_incident_handoff.py": frozenset(
        {"runtime_agent_contracts", "runtime_incidents"}
    ),
}


@pytest.mark.architecture
def test_runtime_agent_modules_do_not_import_business_resolution_or_write_paths():
    source_root = Path(__file__).parents[1] / "src" / "telegram_kol_research"

    for filename in RUNTIME_AGENT_MODULES:
        path = source_root / filename
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                package_imports = [
                    alias.name.removeprefix("telegram_kol_research.")
                    for alias in node.names
                    if alias.name == "telegram_kol_research"
                    or alias.name.startswith("telegram_kol_research.")
                ]
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    package_imports = [node.module or ""]
                elif (node.module or "") == "telegram_kol_research":
                    package_imports = [alias.name for alias in node.names]
                elif (node.module or "").startswith("telegram_kol_research."):
                    package_imports = [
                        (node.module or "").removeprefix("telegram_kol_research.")
                    ]
                else:
                    package_imports = []
                imported = [node.module or "", *(alias.name for alias in node.names)]
            else:
                continue
            lowered = " ".join(imported).lower()
            assert not any(fragment in lowered for fragment in FORBIDDEN_SYMBOL_FRAGMENTS)
            allowed = ALLOWED_PACKAGE_IMPORTS_BY_MODULE.get(filename, frozenset())
            assert all(
                imported_module.split(".", 1)[0] in allowed
                for imported_module in package_imports
            ), f"{filename} imports non-allowlisted application module: {package_imports}"
