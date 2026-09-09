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
    # Phase 6-pre-2. The only writer that does not prove ownership first, and
    # the only one that could not: it exists for the position whose ownership
    # cannot be proved at all. It is on this list rather than behind the
    # gateway because the gateway refuses an unverified position three times
    # over -- in build_position_mutation_authority, in exact_position_write_gate
    # and in _load_verified_binding -- and weakening any of those to admit the
    # net would have cost far more than the net is worth.
    #
    # What earns it the exemption, all of it enforced in
    # tests/test_naked_fill_stop_net.py: it sends a stop and nothing else (no
    # take profit, no close, no cancel); it acts once per order, latched by a
    # compare-and-set; it requires all four preconditions (an unverified market
    # entry leg, sixty seconds elapsed, exactly one unclaimed active position of
    # exactly the filled size, a readable snapshot); it reserves a durable
    # position_mutation_intents row before the request, revalidates immediately
    # before it, never resends an unknown outcome, and confirms by exact
    # readback; it crosses DeepcoinTpslWriteLimiter like every other write; and
    # it never claims ownership -- the marker it leaves is deliberately not
    # "verified", so every ownership check keeps refusing the position.
    "src/telegram_kol_research/naked_fill_stop_net.py",
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
