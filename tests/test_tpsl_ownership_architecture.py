from __future__ import annotations

import inspect

from telegram_kol_research import (
    deepcoin_execution_actions,
    native_tpsl,
    protection_attribution,
    protection_snapshot,
    strategy_management_executor,
    strategy_management_planner,
    trigger_take_profit_convergence_executor,
)
from telegram_kol_research.native_tpsl import (
    NativeTpslExpectation,
    match_native_tpsl_order,
)
from telegram_kol_research.protection_attribution import match_position_protection


def _position(pos_id: str = "pos-1") -> dict[str, str]:
    return {
        "instId": "BTC-USDT-SWAP",
        "posId": pos_id,
        "posSide": "long",
        "pos": "2",
        "cTime": "1000",
    }


def _unscoped_stop(order_id: str = "sl-unowned") -> dict[str, str]:
    return {
        "instId": "BTC-USDT-SWAP",
        "posSide": "long",
        "triggerOrderType": "TPSL",
        "ordId": order_id,
        "slTriggerPx": "62000",
        "sz": "0",
        "cTime": "1001",
    }


def test_unscoped_order_never_becomes_position_protection_without_ledger_owner():
    protection = match_position_protection(
        [_position()],
        [_unscoped_stop()],
    ).by_pos_id["pos-1"]

    assert protection.status == "absent"
    assert protection.order_ids == []
    assert protection.can_mutate is False


def test_native_tpsl_without_ledger_order_id_never_establishes_ownership():
    match = match_native_tpsl_order(
        _position(),
        [_unscoped_stop()],
        NativeTpslExpectation(
            purpose="stop_loss",
            trigger_price="62000",
            size="0",
        ),
    )

    assert match.status == "not_found"
    assert match.order is None


def test_exchange_position_id_conflicting_with_ledger_owner_fails_closed():
    protection = match_position_protection(
        [_position("pos-ledger"), _position("pos-exchange")],
        [{**_unscoped_stop("sl-conflict"), "posId": "pos-exchange"}],
        exact_order_position_ids={"sl-conflict": "pos-ledger"},
    ).by_pos_id["pos-ledger"]

    assert protection.status == "present_but_ambiguous"
    assert protection.order_ids == []
    assert protection.can_mutate is False


def test_runtime_mutation_paths_load_the_account_wide_ledger():
    for module in (
        strategy_management_planner,
        strategy_management_executor,
        deepcoin_execution_actions,
    ):
        assert "list_verified_account_ledger_rows" in inspect.getsource(module)


def test_runtime_ownership_modules_contain_no_guessing_paths():
    forbidden_by_module = {
        protection_attribution: (
            "mutual_unique_instrument_side_time_size",
            "_size_penalty",
            "time_tolerance_ms",
            "allow_heuristic_attribution",
        ),
        native_tpsl: (
            "_time_matches_position(order.created_time",
            "order.size in {position_size",
        ),
        protection_snapshot: (
            "native_tpsl_belongs_to_position",
        ),
        trigger_take_profit_convergence_executor: (
            "_native_tpsl_is_scoped_to_position",
            "_time_is_within_position_window",
        ),
    }

    for module, forbidden_fragments in forbidden_by_module.items():
        source = inspect.getsource(module)
        for fragment in forbidden_fragments:
            assert fragment not in source, (
                f"{module.__name__} reintroduced guessed TPSL ownership: "
                f"{fragment}"
            )
