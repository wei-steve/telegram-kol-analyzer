import ast
import inspect
import textwrap
from decimal import Decimal

import pytest


def _sizing():
    from telegram_kol_research import strategy_management_sizing

    return strategy_management_sizing


def test_entry_revision_risk_reduction_uses_exact_management_delta():
    assert _sizing().entry_revision_risk_reduction_delta(
        current_size="0.012",
        target_size="0.010",
        quantity_step="0.001",
        min_quantity="0.001",
    ) == "0.002"


@pytest.mark.parametrize(
    ("round_before", "fraction", "expected"),
    [
        (0, None, ("partial_close", 0.5)),
        (0, 0.3, ("partial_close", 0.3)),
        (1, None, ("full_close", 1.0)),
        (1, 0.3, ("full_close", 1.0)),
    ],
)
def test_effective_action_implements_two_round_policy(
    round_before, fraction, expected
):
    assert _sizing().effective_action(
        round_before=round_before, fraction=fraction
    ) == expected


@pytest.mark.parametrize(
    ("sizes", "fraction", "step", "minimum", "expected"),
    [
        (("6", "4"), Decimal("0.5"), "1", "1", ("3", "2")),
        (("0.02", "0.02"), Decimal("0.5"), "0.01", "0.01", ("0.01", "0.01")),
        (("5", "3", "2"), Decimal("0.6"), "1", "1", ("3", "2", "1")),
        (("5", "3"), Decimal("0.5"), "1", "1", ("3", "1")),
    ],
)
def test_allocate_close_sizes_uses_aggregate_target_and_stable_remainders(
    sizes, fraction, step, minimum, expected
):
    assert _sizing().allocate_close_sizes(
        sizes,
        fraction=fraction,
        quantity_step=step,
        min_quantity=minimum,
    ) == expected


def test_allocate_close_sizes_for_chen_retain_40_percent_case_is_step_aligned():
    planned = _sizing().allocate_close_sizes(
        ("6", "5"),
        fraction=Decimal("0.6"),
        quantity_step="1",
        min_quantity="1",
    )

    assert planned == ("3", "3")
    assert all(Decimal(size) % Decimal("1") == 0 for size in planned)
    assert sum(map(Decimal, planned)) == Decimal("6")


def test_allocate_close_sizes_rejects_target_below_every_leg_minimum():
    sizing = _sizing()

    with pytest.raises(sizing.ManagementSizingError):
        sizing.allocate_close_sizes(
            ("0.01", "0.01"),
            fraction=Decimal("0.25"),
            quantity_step="0.01",
            min_quantity="0.01",
        )


def test_allocate_close_sizes_never_over_closes_a_position():
    sizing = _sizing()

    planned = sizing.allocate_close_sizes(
        ("1", "9"),
        fraction=Decimal("0.9"),
        quantity_step="1",
        min_quantity="1",
    )

    assert planned == ("1", "8")
    assert all(Decimal(close) <= Decimal(size) for close, size in zip(planned, ("1", "9")))


def test_allocate_close_sizes_reserves_minimum_for_every_participating_position():
    assert _sizing().allocate_close_sizes(
        ("1", "1", "8"),
        fraction=Decimal("0.5"),
        quantity_step="1",
        min_quantity="1",
    ) == ("1", "1", "3")


def test_allocate_close_sizes_fails_when_one_position_cannot_meet_its_minimum():
    sizing = _sizing()

    with pytest.raises(sizing.ManagementSizingError):
        sizing.allocate_close_sizes(
            ("0.5", "9.5"),
            fraction=Decimal("0.5"),
            quantity_step="0.5",
            min_quantity="1",
        )


def test_allocate_close_sizes_has_no_step_count_while_loop():
    sizing = _sizing()
    sources = (
        inspect.getsource(sizing.allocate_close_sizes),
        inspect.getsource(sizing._bulk_extra_step_quotas),
    )

    for source in sources:
        tree = ast.parse(textwrap.dedent(source))
        assert not any(isinstance(node, ast.While) for node in ast.walk(tree))
        for node in ast.walk(tree):
            if not isinstance(node, ast.For) or not isinstance(node.iter, ast.Call):
                continue
            if not isinstance(node.iter.func, ast.Name) or node.iter.func.id != "range":
                continue
            range_names = {
                child.id
                for argument in node.iter.args
                for child in ast.walk(argument)
                if isinstance(child, ast.Name)
            }
            assert not range_names.intersection({"target", "remaining", "remaining_steps"})


@pytest.mark.parametrize(
    ("current", "expected"),
    [("16", "8"), ("12", "4"), ("8", "0")],
)
def test_target_remaining_close_delta_converges_only_the_unresolved_amount(
    current, expected
):
    assert _sizing().target_remaining_close_delta(
        trusted_start_size="16",
        target_remaining_size="8",
        current_size=current,
        quantity_step="1",
        min_quantity="1",
    ) == expected


def test_target_remaining_close_delta_refuses_below_target():
    sizing = _sizing()

    with pytest.raises(
        sizing.ManagementSizingError, match="position_below_target_remaining"
    ):
        sizing.target_remaining_close_delta(
            trusted_start_size="16",
            target_remaining_size="8",
            current_size="7",
            quantity_step="1",
            min_quantity="1",
        )


def test_target_remaining_close_delta_refuses_size_increase():
    sizing = _sizing()

    with pytest.raises(
        sizing.ManagementSizingError, match="position_size_increased_after_snapshot"
    ):
        sizing.target_remaining_close_delta(
            trusted_start_size="16",
            target_remaining_size="8",
            current_size="17",
            quantity_step="1",
            min_quantity="1",
        )
