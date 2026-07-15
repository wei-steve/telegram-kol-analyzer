from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_kol_research.historical_attribution_cleanup import (
    HistoricalCleanupDecision,
    plan_historical_attribution_cleanup,
)
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    StrategyLifecycle,
)


NOW = datetime(2026, 7, 15, tzinfo=UTC)


def _binding(*, row_id: int, status: str = "unknown") -> ExecutionBinding:
    return ExecutionBinding(
        id=row_id,
        strategy_instance_id=f"deepcoin:1:{row_id}:BTC:long",
        kol_id=f"group:{row_id}",
        chat_id=row_id,
        message_id=row_id,
        symbol="BTC",
        side="long",
        venue="deepcoin",
        status=status,
    )


def _leg(
    *,
    row_id: int,
    binding_id: int,
    pos_id: str | None,
    order_id: str | None,
    status: str = "manually_closed",
    attribution_status: str = "attribution_conflict",
) -> ExecutionOrderLeg:
    return ExecutionOrderLeg(
        id=row_id,
        execution_binding_id=binding_id,
        strategy_instance_id=f"deepcoin:1:{binding_id}:BTC:long",
        leg_index=row_id,
        purpose="entry",
        order_kind="market",
        order_id=order_id,
        client_order_id=f"client-{row_id}",
        pos_id=pos_id,
        venue="deepcoin",
        status=status,
        attribution_status=attribution_status,
    )


def _lifecycle(
    *,
    row_id: int,
    binding_id: int | None,
    status: str,
    exit_reason: str | None = None,
) -> StrategyLifecycle:
    return StrategyLifecycle(
        id=row_id,
        chat_id=binding_id or row_id,
        message_id=binding_id or row_id,
        symbol="BTC",
        side="long",
        lifecycle_status=status,
        exit_reason=exit_reason,
        signal_at=NOW,
        entered_at=NOW,
        exited_at=NOW if status == "exited" else None,
        execution_binding_id=binding_id,
    )


def _snapshot(**overrides):
    values = {
        "positions": [],
        "position_history": [],
        "open_orders": [],
        "pending_trigger_orders": [],
        "order_history": [],
        "trade_fills": [],
        "trigger_history": [],
        "errors": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _position_history_row(**overrides):
    values = {
        "instId": "BTC-USDT-SWAP",
        "posId": "position-1",
        "posSide": "long",
        "mrgPosition": "split",
        "pos": "4",
        "closePos": "4.0",
        "avgPx": "62500",
        "closeAvgPx": "62790.1",
        "pnl": "1.1604",
        "cTime": "1784073600000",
        "uTime": "1784077200000",
    }
    values.update(overrides)
    return values


def _decision(
    *,
    bindings,
    legs,
    lifecycles=(),
    events=(),
    reservations=(),
    snapshot=None,
):
    return plan_historical_attribution_cleanup(
        bindings=list(bindings),
        legs=list(legs),
        lifecycles=list(lifecycles),
        events=list(events),
        reservations=list(reservations),
        snapshot=snapshot or _snapshot(),
    )


def test_exact_fully_closed_split_position_history_is_terminal_evidence():
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[
            _leg(
                row_id=1,
                binding_id=10,
                pos_id="position-1",
                order_id="position-1",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=100, binding_id=10, status="entered")],
        snapshot=_snapshot(position_history=[_position_history_row()]),
    )

    terminalize = next(
        action
        for action in decision.actions
        if action.action == "terminalize_historical_entry_leg"
    )
    assert terminalize.new_state == "closed"
    assert terminalize.evidence["terminal_evidence"] == {
        "source": "exchange_position_history",
        "pos_id": "position-1",
        "pos": "4",
        "close_pos": "4.0",
        "avg_px": "62500",
        "close_avg_px": "62790.1",
        "pnl": "1.1604",
        "created_at": "1784073600000",
        "updated_at": "1784077200000",
    }
    assert decision.conflicts == ()


def test_partial_position_history_is_not_terminal_evidence():
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[
            _leg(
                row_id=1,
                binding_id=10,
                pos_id="position-1",
                order_id="position-1",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=100, binding_id=10, status="entered")],
        snapshot=_snapshot(
            position_history=[_position_history_row(closePos="3")]
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_terminal_evidence_missing"


def test_persisted_position_partial_history_blocks_order_id_fallback():
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[
            _leg(
                row_id=1,
                binding_id=10,
                pos_id="persisted-position",
                order_id="different-order-position",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=100, binding_id=10, status="entered")],
        snapshot=_snapshot(
            position_history=[
                _position_history_row(
                    posId="persisted-position",
                    closePos="3",
                ),
                _position_history_row(posId="different-order-position"),
            ]
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_terminal_evidence_missing"


@pytest.mark.parametrize("original", ["0", "0.0", "not-a-number", None])
def test_zero_or_malformed_original_position_is_not_terminal_evidence(original):
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[
            _leg(
                row_id=1,
                binding_id=10,
                pos_id="position-1",
                order_id="position-1",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=100, binding_id=10, status="entered")],
        snapshot=_snapshot(
            position_history=[_position_history_row(pos=original)]
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_terminal_evidence_missing"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("posId", "other-position"),
        ("instId", "ETH-USDT-SWAP"),
        ("posSide", "short"),
        ("mrgPosition", "merge"),
    ],
)
def test_mismatched_position_history_identity_is_not_terminal_evidence(field, value):
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[
            _leg(
                row_id=1,
                binding_id=10,
                pos_id="position-1",
                order_id="position-1",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=100, binding_id=10, status="entered")],
        snapshot=_snapshot(
            position_history=[_position_history_row(**{field: value})]
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_terminal_evidence_missing"


def test_conflicting_exact_position_history_rows_are_unresolved():
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[
            _leg(
                row_id=1,
                binding_id=10,
                pos_id="position-1",
                order_id="position-1",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=100, binding_id=10, status="entered")],
        snapshot=_snapshot(
            position_history=[
                _position_history_row(),
                _position_history_row(closeAvgPx="62800"),
            ]
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_position_history_conflict"


@pytest.mark.parametrize(
    "conflicting_row",
    [
        _position_history_row(closePos="3"),
        _position_history_row(pos="malformed"),
        _position_history_row(closeAvgPx="62800"),
        _position_history_row(instId="ETH-USDT-SWAP"),
        _position_history_row(posSide="short"),
        _position_history_row(mrgPosition="merge"),
    ],
    ids=[
        "partial",
        "malformed",
        "different_closed_row",
        "instrument_mismatch",
        "side_mismatch",
        "position_mode_mismatch",
    ],
)
def test_fully_closed_row_mixed_with_same_candidate_invalid_row_is_unresolved(
    conflicting_row,
):
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[
            _leg(
                row_id=1,
                binding_id=10,
                pos_id="position-1",
                order_id="position-1",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=100, binding_id=10, status="entered")],
        snapshot=_snapshot(
            position_history=[_position_history_row(), conflicting_row]
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_position_history_conflict"


@pytest.mark.parametrize(
    ("snapshot_field", "row"),
    [
        ("positions", {"posId": "position-1", "pos": "4"}),
        ("open_orders", {"ordId": "position-1", "state": "live"}),
        (
            "pending_trigger_orders",
            {"ordId": "position-1", "state": "live", "triggerOrderType": "NORMAL"},
        ),
    ],
)
def test_live_or_pending_identity_blocks_fully_closed_position_history(
    snapshot_field, row
):
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[
            _leg(
                row_id=1,
                binding_id=10,
                pos_id="position-1",
                order_id="position-1",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=100, binding_id=10, status="entered")],
        snapshot=_snapshot(
            position_history=[_position_history_row()],
            **{snapshot_field: [row]},
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason in {
        "historical_position_still_exchange_active",
        "historical_pending_order_active",
    }


def test_stale_shared_position_uses_exact_fully_closed_evidence_per_leg():
    decision = _decision(
        bindings=[_binding(row_id=9), _binding(row_id=11)],
        legs=[
            _leg(
                row_id=10,
                binding_id=9,
                pos_id="shared",
                order_id="shared",
                status="active",
            ),
            _leg(
                row_id=13,
                binding_id=11,
                pos_id="shared",
                order_id="actual-1",
                status="active",
            ),
            _leg(
                row_id=14,
                binding_id=11,
                pos_id="shared",
                order_id="actual-2",
                status="active",
            ),
        ],
        lifecycles=[
            _lifecycle(row_id=90, binding_id=9, status="entered"),
            _lifecycle(row_id=110, binding_id=11, status="entered"),
        ],
        snapshot=_snapshot(
            position_history=[
                _position_history_row(posId="shared"),
                _position_history_row(posId="actual-1"),
                _position_history_row(posId="actual-2"),
            ]
        ),
    )

    clear_actions = {
        action.leg_id: action
        for action in decision.actions
        if action.action == "clear_redundant_historical_position"
    }
    terminalize_actions = {
        action.leg_id: action
        for action in decision.actions
        if action.action == "terminalize_historical_entry_leg"
    }
    assert sorted(clear_actions) == [13, 14]
    assert clear_actions[13].old_pos_id == "shared"
    assert clear_actions[14].old_pos_id == "shared"
    assert terminalize_actions[10].new_pos_id == "shared"
    assert terminalize_actions[10].evidence["terminal_evidence"]["pos_id"] == "shared"
    assert terminalize_actions[13].evidence["terminal_evidence"]["pos_id"] == "actual-1"
    assert terminalize_actions[14].evidence["terminal_evidence"]["pos_id"] == "actual-2"
    assert not any("assign" in action.action for action in decision.actions)
    assert decision.conflicts == ()


def test_redundant_competitor_requires_own_order_derived_closed_history():
    reservation = BoundPositionCloseReservation(
        id=1,
        pos_id="actual-1",
        execution_binding_id=11,
        status="completed",
        created_at=NOW,
        updated_at=NOW,
    )
    event = ExecutionEvent(
        id=1,
        execution_binding_id=11,
        strategy_instance_id="deepcoin:1:11:BTC:long",
        venue="deepcoin",
        action="close_position_market",
        status="completed",
        pos_id="actual-1",
        created_at=NOW,
    )
    decision = _decision(
        bindings=[_binding(row_id=9), _binding(row_id=11)],
        legs=[
            _leg(
                row_id=10,
                binding_id=9,
                pos_id="shared",
                order_id="shared",
                status="active",
            ),
            _leg(
                row_id=13,
                binding_id=11,
                pos_id="shared",
                order_id="actual-1",
                status="active",
            ),
        ],
        lifecycles=[
            _lifecycle(row_id=90, binding_id=9, status="entered"),
            _lifecycle(
                row_id=110,
                binding_id=11,
                status="exited",
                exit_reason="manual",
            ),
        ],
        events=[event],
        reservations=[reservation],
        snapshot=_snapshot(
            position_history=[_position_history_row(posId="shared")]
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_terminal_evidence_missing"


@pytest.mark.parametrize(
    ("snapshot_field", "row"),
    [
        ("positions", {"posId": "actual-1", "pos": "4"}),
        ("open_orders", {"ordId": "actual-1", "state": "live"}),
        (
            "pending_trigger_orders",
            {"ordId": "actual-2", "state": "live", "triggerOrderType": "NORMAL"},
        ),
        (
            "pending_trigger_orders",
            {"posId": "actual-2", "state": "live", "triggerOrderType": "TPSL"},
        ),
    ],
)
def test_shared_owner_component_blocks_live_or_pending_order_derived_identity(
    snapshot_field, row
):
    decision = _decision(
        bindings=[_binding(row_id=9), _binding(row_id=11)],
        legs=[
            _leg(
                row_id=10,
                binding_id=9,
                pos_id="shared",
                order_id="shared",
                status="active",
            ),
            _leg(
                row_id=13,
                binding_id=11,
                pos_id="shared",
                order_id="actual-1",
                status="active",
            ),
            _leg(
                row_id=14,
                binding_id=11,
                pos_id="shared",
                order_id="actual-2",
                status="active",
            ),
        ],
        lifecycles=[
            _lifecycle(row_id=90, binding_id=9, status="entered"),
            _lifecycle(row_id=110, binding_id=11, status="entered"),
        ],
        snapshot=_snapshot(
            position_history=[
                _position_history_row(posId="shared"),
                _position_history_row(posId="actual-1"),
                _position_history_row(posId="actual-2"),
            ],
            **{snapshot_field: [row]},
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason in {
        "historical_position_still_exchange_active",
        "historical_pending_order_active",
    }


def test_same_binding_duplicate_retains_only_exact_direct_owner():
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[
            _leg(row_id=1, binding_id=10, pos_id="p1", order_id="p1"),
            _leg(row_id=2, binding_id=10, pos_id="p1", order_id="child-2"),
        ],
        lifecycles=[
            _lifecycle(row_id=100, binding_id=10, status="exited", exit_reason="manual")
        ],
        snapshot=_snapshot(
            position_history=[
                _position_history_row(posId="p1"),
                _position_history_row(posId="child-2"),
            ]
        ),
    )

    clear_actions = [
        action
        for action in decision.actions
        if action.action == "clear_redundant_historical_position"
    ]
    assert [action.leg_id for action in clear_actions] == [2]
    assert clear_actions[0].old_pos_id == "p1"
    assert decision.conflicts == ()


def test_cross_binding_duplicate_without_unique_authority_is_unresolved():
    decision = _decision(
        bindings=[_binding(row_id=10), _binding(row_id=11)],
        legs=[
            _leg(row_id=1, binding_id=10, pos_id="p1", order_id="a"),
            _leg(row_id=2, binding_id=11, pos_id="p1", order_id="b"),
        ],
        lifecycles=[
            _lifecycle(row_id=100, binding_id=10, status="exited", exit_reason="manual"),
            _lifecycle(row_id=101, binding_id=11, status="exited", exit_reason="manual"),
        ],
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_owner_ambiguous"
    assert decision.conflicts[0].pos_ids == ("p1",)


@pytest.mark.parametrize(
    ("snapshot_field", "row"),
    [
        ("positions", {"posId": "p1", "pos": "1"}),
        ("open_orders", {"posId": "p1", "state": "live"}),
        (
            "pending_trigger_orders",
            {"posId": "p1", "state": "live", "triggerOrderType": "NORMAL"},
        ),
        (
            "pending_trigger_orders",
            {"posId": "p1", "state": "live", "triggerOrderType": "TPSL"},
        ),
    ],
)
def test_live_or_pending_exchange_identity_blocks_historical_cleanup(
    snapshot_field, row
):
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[_leg(row_id=1, binding_id=10, pos_id="p1", order_id="p1")],
        lifecycles=[
            _lifecycle(row_id=100, binding_id=10, status="exited", exit_reason="manual")
        ],
        snapshot=_snapshot(**{snapshot_field: [row]}),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_position_still_exchange_active"


@pytest.mark.parametrize("snapshot_field", ["open_orders", "pending_trigger_orders"])
def test_pending_order_identity_without_pos_id_blocks_historical_cleanup(
    snapshot_field,
):
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[_leg(row_id=1, binding_id=10, pos_id="p1", order_id="entry-1")],
        lifecycles=[
            _lifecycle(row_id=100, binding_id=10, status="exited", exit_reason="manual")
        ],
        snapshot=_snapshot(
            **{
                snapshot_field: [
                    {
                        "ordId": "entry-1",
                        "state": "live",
                        "triggerOrderType": "NORMAL",
                    }
                ]
            }
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_pending_order_active"


def test_entered_lifecycle_without_exact_terminal_evidence_is_unresolved():
    decision = _decision(
        bindings=[_binding(row_id=96)],
        legs=[
            _leg(
                row_id=188,
                binding_id=96,
                pos_id="old",
                order_id="old",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=420, binding_id=96, status="entered")],
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_terminal_evidence_missing"


def test_completed_close_reservation_is_exact_terminal_evidence():
    reservation = BoundPositionCloseReservation(
        id=1,
        pos_id="old",
        execution_binding_id=96,
        status="completed",
        created_at=NOW,
        updated_at=NOW,
    )
    decision = _decision(
        bindings=[_binding(row_id=96)],
        legs=[
            _leg(
                row_id=188,
                binding_id=96,
                pos_id="old",
                order_id="old",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=420, binding_id=96, status="entered")],
        reservations=[reservation],
    )

    assert [action.action for action in decision.actions] == [
        "terminalize_historical_entry_leg",
        "close_historical_binding",
        "exit_historical_lifecycle",
    ]
    assert decision.conflicts == ()


def test_exact_close_event_is_terminal_evidence():
    event = ExecutionEvent(
        id=1,
        execution_binding_id=96,
        strategy_instance_id="deepcoin:1:96:BTC:long",
        venue="deepcoin",
        action="close_position_market",
        status="completed",
        pos_id="old",
        created_at=NOW,
    )
    decision = _decision(
        bindings=[_binding(row_id=96)],
        legs=[
            _leg(
                row_id=188,
                binding_id=96,
                pos_id="old",
                order_id="old",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=420, binding_id=96, status="entered")],
        events=[event],
    )

    assert any(
        action.action == "exit_historical_lifecycle" for action in decision.actions
    )
    assert decision.conflicts == ()


def test_research_only_lifecycle_is_not_a_cleanup_candidate():
    decision = _decision(
        bindings=[],
        legs=[],
        lifecycles=[_lifecycle(row_id=120, binding_id=None, status="entered")],
    )

    assert decision == HistoricalCleanupDecision(actions=(), conflicts=())


def test_api_error_blocks_all_historical_cleanup_actions():
    decision = _decision(
        bindings=[_binding(row_id=10)],
        legs=[_leg(row_id=1, binding_id=10, pos_id="p1", order_id="p1")],
        lifecycles=[
            _lifecycle(row_id=100, binding_id=10, status="exited", exit_reason="manual")
        ],
        snapshot=_snapshot(errors={"positions": "timeout"}),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_evidence_unavailable"


def test_unrelated_cancel_history_cannot_terminalize_binding_without_order_identity():
    binding = _binding(row_id=96)
    binding.order_id = None
    binding.client_order_id = None
    decision = _decision(
        bindings=[binding],
        legs=[
            _leg(
                row_id=188,
                binding_id=96,
                pos_id="old",
                order_id="old",
                status="active",
            )
        ],
        lifecycles=[_lifecycle(row_id=420, binding_id=96, status="entered")],
        snapshot=_snapshot(
            order_history=[{"ordId": "unrelated", "state": "cancelled"}]
        ),
    )

    assert decision.actions == ()
    assert decision.conflicts[0].reason == "historical_terminal_evidence_missing"


def test_action_order_is_independent_of_input_order():
    bindings = [_binding(row_id=10), _binding(row_id=20)]
    legs = [
        _leg(row_id=1, binding_id=10, pos_id="p1", order_id="p1"),
        _leg(row_id=2, binding_id=10, pos_id="p1", order_id="child"),
        _leg(row_id=3, binding_id=20, pos_id="p2", order_id="p2"),
    ]
    lifecycles = [
        _lifecycle(row_id=100, binding_id=10, status="exited", exit_reason="manual"),
        _lifecycle(row_id=200, binding_id=20, status="exited", exit_reason="manual"),
    ]

    first = _decision(bindings=bindings, legs=legs, lifecycles=lifecycles)
    second = _decision(
        bindings=reversed(bindings),
        legs=reversed(legs),
        lifecycles=reversed(lifecycles),
    )

    assert first == second
