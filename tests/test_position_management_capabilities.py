import pytest

from telegram_kol_research.position_management_capabilities import (
    evaluate_position_management_capabilities,
)


def _evaluate(**updates):
    values = {
        "exact_position_verified": True,
        "native_stop_owned": False,
        "exact_owned_stop": False,
        "conflicting_unknown_take_profit": False,
        "retained_take_profit_safe": True,
        "snapshot_complete": True,
        "active_or_unknown_mutation": False,
    }
    values.update(updates)
    return evaluate_position_management_capabilities(**values)


def test_unknown_native_stop_does_not_block_exact_backup_or_full_close():
    caps = _evaluate()

    assert caps.may_cancel_owned_protection is False
    assert caps.may_replace_owned_protection is False
    assert caps.may_add_exact_backup_stop is True
    assert caps.may_add_exact_take_profit is False
    assert caps.may_close_exact_position is True


def test_unknown_take_profit_blocks_add_tp_and_partial_not_full_close():
    caps = _evaluate(
        exact_owned_stop=True,
        conflicting_unknown_take_profit=True,
    )

    assert caps.may_add_exact_take_profit is False
    assert caps.may_reduce_exact_position is False
    assert caps.may_close_exact_position is True
    assert "conflicting_unknown_take_profit" in caps.reason_codes


@pytest.mark.parametrize(
    "updates",
    [
        {"exact_position_verified": False},
        {"snapshot_complete": False},
        {"active_or_unknown_mutation": True},
    ],
)
def test_missing_authority_or_freshness_disables_every_write(updates):
    caps = _evaluate(native_stop_owned=True, exact_owned_stop=True, **updates)

    assert caps.may_cancel_owned_protection is False
    assert caps.may_replace_owned_protection is False
    assert caps.may_add_exact_backup_stop is False
    assert caps.may_add_exact_take_profit is False
    assert caps.may_reduce_exact_position is False
    assert caps.may_close_exact_position is False


def test_retained_take_profit_overflow_blocks_partial_but_not_full_close():
    caps = _evaluate(
        exact_owned_stop=True,
        retained_take_profit_safe=False,
    )

    assert caps.may_add_exact_take_profit is False
    assert caps.may_reduce_exact_position is False
    assert caps.may_close_exact_position is True
    assert "retained_take_profit_overflow" in caps.reason_codes


def test_exact_owned_stop_allows_take_profit_and_suppresses_duplicate_backup():
    caps = _evaluate(exact_owned_stop=True)

    assert caps.may_add_exact_backup_stop is False
    assert caps.may_add_exact_take_profit is True
    assert caps.may_reduce_exact_position is True
    assert caps.may_close_exact_position is True


def test_owned_native_stop_can_be_cancelled_and_replaced():
    caps = _evaluate(native_stop_owned=True, exact_owned_stop=True)

    assert caps.may_cancel_owned_protection is True
    assert caps.may_replace_owned_protection is True
