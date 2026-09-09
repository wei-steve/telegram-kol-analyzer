"""The bypass must stay a bypass: one module, one purpose, one caller.

Phase 6-pre-2 opens the only path in this repository that writes to the
exchange without ``require_verified_position_ownership`` behind it. The value
of that path being safe rests entirely on it being *narrow*, and narrowness is
not something a reviewer can keep re-checking by hand. These tests fail the
moment it stops being narrow.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "telegram_kol_research"
NET_MODULE = "naked_fill_stop_net"
BYPASS_NAMES = {
    "build_naked_fill_stop_authority",
    "NakedFillStopAuthority",
    "submit_naked_fill_stop",
}


def _module_sources():
    for path in sorted(SRC.glob("*.py")):
        if path.stem == NET_MODULE:
            continue
        yield path, path.read_text(encoding="utf-8")


def test_no_other_production_module_imports_the_bypass():
    offenders = []
    for path, source in _module_sources():
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and node.module.endswith(NET_MODULE):
                    imported = {alias.name for alias in node.names}
                    if imported & BYPASS_NAMES:
                        offenders.append((path.name, sorted(imported & BYPASS_NAMES)))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.endswith(NET_MODULE):
                        # Importing the module wholesale would put the bypass
                        # one attribute access away from any caller.
                        offenders.append((path.name, [alias.name]))
    assert offenders == [], (
        "the naked-fill bypass may only be reached from its own module; "
        f"these import it: {offenders}"
    )


def test_only_the_reconciler_entry_point_is_meant_to_be_called_elsewhere():
    """The operator tick calls one function. Everything else stays internal."""

    from telegram_kol_research import naked_fill_stop_net

    public_entry = "reconcile_naked_market_fills"
    assert hasattr(naked_fill_stop_net, public_entry)
    for path, source in _module_sources():
        if public_entry in source:
            continue
        for name in BYPASS_NAMES:
            assert name not in source, f"{path.name} references {name}"


def test_the_bypass_authority_cannot_authorize_anything_but_a_stop():
    from telegram_kol_research.naked_fill_stop_net import (
        NakedFillDecision,
        NakedFillStopAuthority,
        submit_naked_fill_stop,
    )
    from telegram_kol_research.position_mutation_authority import (
        PositionMutationAuthority,
        PositionMutationAuthorityError,
    )

    ordinary = PositionMutationAuthority(
        venue="deepcoin",
        strategy_instance_id="s",
        execution_binding_id=1,
        execution_order_leg_id=1,
        pos_id="p",
        instrument_id="ETH-USDT-SWAP",
        side="long",
        position_fingerprint="f",
    )
    decision = NakedFillDecision(
        "attach", "unique_unclaimed_candidate", leg_id=1, order_id="o",
        pos_id="p", stop_loss="2500",
    )
    # An ordinary authority must not be able to travel this path at all.
    with pytest.raises(PositionMutationAuthorityError):
        submit_naked_fill_stop(
            None,
            deepcoin_client=None,
            authority=ordinary,
            decision=decision,
            now=None,
            revalidate=lambda: True,
        )
    # And the bypass authority must refuse a decision that is not actionable.
    bypass = NakedFillStopAuthority(
        venue="deepcoin",
        strategy_instance_id="s",
        execution_binding_id=1,
        execution_order_leg_id=1,
        pos_id="p",
        instrument_id="ETH-USDT-SWAP",
        side="long",
        position_fingerprint="f",
    )
    with pytest.raises(PositionMutationAuthorityError):
        submit_naked_fill_stop(
            None,
            deepcoin_client=None,
            authority=bypass,
            decision=NakedFillDecision("alert_only", "no_unclaimed_candidate"),
            now=None,
            revalidate=lambda: True,
        )


def test_the_existing_ownership_guards_are_untouched_by_this_phase():
    """The three checks the ordinary path relies on must still be exactly there."""

    from telegram_kol_research import position_mutation_authority, position_attribution
    from telegram_kol_research import position_mutation_gateway

    authority_source = pathlib.Path(
        position_mutation_authority.__file__
    ).read_text(encoding="utf-8")
    gateway_source = pathlib.Path(
        position_mutation_gateway.__file__
    ).read_text(encoding="utf-8")
    attribution_source = pathlib.Path(
        position_attribution.__file__
    ).read_text(encoding="utf-8")

    assert "require_verified_position_ownership" in authority_source
    assert "require_verified_position_ownership" in gateway_source
    assert 'if state != "verified":' in attribution_source
    # The marker this phase writes is not "verified", so every one of those
    # checks keeps refusing a rescued position.
    from telegram_kol_research.naked_fill_stop_net import (
        NAKED_FILL_STOP_ATTRIBUTION,
    )

    assert NAKED_FILL_STOP_ATTRIBUTION != "verified"


def _imports_or_calls(source, path, name):
    """Real imports and calls only -- a name in a docstring is not a caller."""

    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name == name for alias in node.names
        ):
            return True
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        ):
            return True
    return False


def test_the_set_of_ownership_proving_writer_callers_did_not_grow():
    """The ordinary write path keeps exactly the callers it had.

    Phase 6-pre-2 adds a second way to reach the exchange. The guarantee that
    it did not also *widen* the first one is this list:
    ``submit_exact_position_sltp`` proves ownership three times over, and every
    module below depends on that. A new name here means somebody routed a new
    write through it -- which may be right, but must be a deliberate review
    rather than a side effect of this phase.
    """

    callers = {
        path.name
        for path, source in _module_sources()
        if _imports_or_calls(source, path, "submit_exact_position_sltp")
    }
    assert callers == {
        "backup_stop_repair.py",
        "break_even_convergence_executor.py",
        "deepcoin_execution_actions.py",
        "native_tpsl_migration.py",
        "recovery_live_submit.py",
        "stop_loss_size_convergence.py",
        "strategy_management_composite_executor.py",
        "strategy_management_executor.py",
        "trigger_backup_stop_executor.py",
        "trigger_take_profit_convergence_executor.py",
    }, sorted(callers)


def test_the_safety_net_never_reaches_the_ownership_proving_writer():
    """The bypass and the ordinary path must not be reachable from each other."""

    from telegram_kol_research import naked_fill_stop_net

    path = pathlib.Path(naked_fill_stop_net.__file__)
    source = path.read_text(encoding="utf-8")
    assert not _imports_or_calls(source, path, "submit_exact_position_sltp"), (
        "the safety net must use its own minimal writer, not the ownership path"
    )
    # It may borrow the gateway's read-only helpers -- parsing a response shape
    # or matching a readback row is not authority -- but nothing that writes.
    assert "PositionMutationGateway(" not in source
