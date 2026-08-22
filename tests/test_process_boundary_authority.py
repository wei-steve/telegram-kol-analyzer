"""Static guard for exchange authority crossing the Phase 6 Web boundary."""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "telegram_kol_research"
WEB_MODULE = "telegram_kol_research.web_app"
DEEPCOIN_WRITE_METHODS = {
    "cancel_order",
    "cancel_position_sltp",
    "cancel_trigger_order",
    "place_order",
    "replace_order_sltp",
    "set_position_sltp",
    "trigger_order",
}
DYNAMIC_AUTHORITY_CALLS = {
    "app.state.authoritative_processor",
    "app.state.auto_trade_executor",
}
@dataclass(frozen=True)
class FunctionRecord:
    key: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    module: str


def _module_name(path: Path) -> str:
    return f"telegram_kol_research.{path.stem}"


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    cursor = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if not isinstance(cursor, ast.Name):
        return None
    parts.append(cursor.id)
    return ".".join(reversed(parts))


def _direct_calls(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Call]:
    calls: list[ast.Call] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, child):
            if child is node:
                self.generic_visit(child)

        def visit_AsyncFunctionDef(self, child):
            if child is node:
                self.generic_visit(child)

        def visit_Lambda(self, child):
            return

        def visit_Call(self, child):
            calls.append(child)
            self.generic_visit(child)

    Visitor().visit(node)
    return calls


def _load_graph():
    functions: dict[str, FunctionRecord] = {}
    imports: dict[str, dict[str, str]] = {}
    routes: dict[str, str] = {}

    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases: dict[str, str] = {}
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module:
                for alias in statement.names:
                    aliases[alias.asname or alias.name] = (
                        f"{statement.module}.{alias.name}"
                    )
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    aliases[alias.asname or alias.name] = alias.name
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = f"{module}.{statement.name}"
                functions[key] = FunctionRecord(key, statement, module)

        if module == WEB_MODULE:
            for candidate in ast.walk(tree):
                if not isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for decorator in candidate.decorator_list:
                    if not isinstance(decorator, ast.Call):
                        continue
                    dotted = _dotted_name(decorator.func)
                    if dotted not in {
                        "app.post",
                        "app.put",
                        "app.patch",
                        "app.delete",
                    }:
                        continue
                    if not decorator.args or not isinstance(
                        decorator.args[0], ast.Constant
                    ):
                        continue
                    key = f"{module}.{candidate.name}"
                    functions[key] = FunctionRecord(key, candidate, module)
                    routes[key] = str(decorator.args[0].value)
        imports[module] = aliases

    edges: dict[str, set[str]] = {key: set() for key in functions}
    sinks: dict[str, str] = {}
    for key, record in functions.items():
        decorators = {
            _dotted_name(decorator.func)
            if isinstance(decorator, ast.Call)
            else _dotted_name(decorator)
            for decorator in record.node.decorator_list
        }
        if any(
            name and name.endswith("serialized_position_authority_mutation")
            for name in decorators
        ):
            sinks[key] = "position_authority_lock decorator"

        for call in _direct_calls(record.node):
            dotted = _dotted_name(call.func)
            if dotted is None:
                continue
            if dotted in DYNAMIC_AUTHORITY_CALLS:
                sinks[key] = f"dynamic authority call {dotted}"
            if dotted.endswith("position_authority_lock"):
                sinks[key] = "position_authority_lock context"
            if isinstance(call.func, ast.Attribute) and call.func.attr in DEEPCOIN_WRITE_METHODS:
                sinks[key] = f"Deepcoin write method {call.func.attr}"

            target = None
            if isinstance(call.func, ast.Name):
                target = imports[record.module].get(call.func.id)
                if target is None:
                    target = f"{record.module}.{call.func.id}"
            elif isinstance(call.func, ast.Attribute):
                owner = _dotted_name(call.func.value)
                imported_owner = imports[record.module].get(owner or "")
                if imported_owner:
                    target = f"{imported_owner}.{call.func.attr}"
            if target in functions:
                edges[key].add(target)

    return functions, routes, edges, sinks


def _authority_paths() -> dict[str, list[str]]:
    _functions, routes, edges, sinks = _load_graph()
    violations: dict[str, list[str]] = {}
    for route_key, route_path in routes.items():
        queue = deque([(route_key, [route_key])])
        visited = {route_key}
        while queue:
            current, path = queue.popleft()
            if current in sinks:
                violations[route_path] = [*path, sinks[current]]
                break
            for target in sorted(edges[current]):
                if target not in visited:
                    visited.add(target)
                    queue.append((target, [*path, target]))
    return violations


@pytest.mark.architecture
def test_web_routes_do_not_reach_exchange_mutation_authority():
    violations = _authority_paths()
    assert violations == {}, (
        "Web routes still reach recognition/exchange mutation authority. "
        "Phase 6 must move these operations behind worker-owned jobs before "
        f"the process split: {violations}"
    )
