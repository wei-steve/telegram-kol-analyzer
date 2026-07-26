import ast
from pathlib import Path

import pytest


FORBIDDEN_POSITION_WRITE_ATTRIBUTES = {
    "set_position_sltp",
    "cancel_position_sltp",
    "_set_position_sltp_unchecked",
    "_cancel_position_sltp_unchecked",
    "_place_position_close_unchecked",
}
ALLOWED_WRITER_PATHS = {
    "src/telegram_kol_research/position_mutation_gateway.py",
    "src/telegram_kol_research/deepcoin_client.py",
}
POSITION_SLTP_PATH_FRAGMENTS = {
    "/deepcoin/trade/set-position-sltp",
    "/deepcoin/trade/cancel-position-sltp",
}


@pytest.mark.architecture
def test_all_position_writes_cross_the_exact_gateway():
    root = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    for base in (root / "src" / "telegram_kol_research", root / "scripts"):
        for path in sorted(base.rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if relative in ALLOWED_WRITER_PATHS:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr in FORBIDDEN_POSITION_WRITE_ATTRIBUTES
                ):
                    violations.append(f"{relative}:{node.lineno}:{node.attr}")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in {"getattr", "hasattr"}
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in FORBIDDEN_POSITION_WRITE_ATTRIBUTES
                ):
                    violations.append(
                        f"{relative}:{node.lineno}:dynamic_{node.args[1].value}"
                    )
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "submit"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value in FORBIDDEN_POSITION_WRITE_ATTRIBUTES
                ):
                    violations.append(
                        f"{relative}:{node.lineno}:dynamic_{node.args[1].value}"
                    )
                if (
                    relative.startswith("scripts/")
                    and isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and any(
                        fragment in node.value
                        for fragment in POSITION_SLTP_PATH_FRAGMENTS
                    )
                ):
                    violations.append(
                        f"{relative}:{node.lineno}:raw_position_sltp_endpoint"
                    )
    assert violations == [], (
        "position writes must use PositionMutationGateway:\n"
        + "\n".join(violations)
    )
