import pytest

from telegram_kol_research.position_attribution import (
    FillEvidence,
    LegEvidence,
    PositionEvidence,
    canonical_live_position_economics,
    _numeric_aware_identifier_key,
    classify_leg_exchange_state,
    classify_equivalent_attribution_components,
    filter_candidate_edges_by_entry_protection,
    is_fill_evidence,
    match_entry_legs_to_positions,
)


def _leg(leg_id, *, order_id=None, client_order_id=None, terminal=False, pos_id=None):
    return LegEvidence(
        leg_id=leg_id,
        binding_id=100 + leg_id,
        venue="deepcoin",
        symbol="ETH-USDT-SWAP",
        side="short",
        order_id=order_id,
        client_order_id=client_order_id,
        pos_id=pos_id,
        requested_size=1.5,
        terminal=terminal,
    )


def _position(pos_id, created_at_ms):
    return PositionEvidence(
        pos_id=pos_id,
        symbol="ETH-USDT-SWAP",
        side="short",
        size=1.5,
        average_price=1770.0,
        created_at_ms=created_at_ms,
    )


def _fill(
    order_id,
    created_at_ms,
    *,
    source="regular_order",
    client_order_id=None,
    pos_id=None,
):
    return FillEvidence(
        source=source,
        order_id=order_id,
        client_order_id=client_order_id,
        pos_id=pos_id,
        symbol="ETH-USDT-SWAP",
        side="short",
        size=1.5,
        price=1770.0,
        created_at_ms=created_at_ms,
    )


def _equivalent_leg(leg_id, *, binding_id=100, **overrides):
    values = {
        "leg_id": leg_id,
        "binding_id": binding_id,
        "venue": "deepcoin",
        "symbol": "ETH-USDT-SWAP",
        "side": "short",
        "order_id": f"order-{leg_id}",
        "client_order_id": f"client-{leg_id}",
        "pos_id": None,
        "requested_size": 1.5,
        "terminal": False,
        "strategy_instance_id": "strategy-1",
        "entry_price": 1770.0,
        "stop_loss": 1820.0,
        "take_profits": (1700.0, 1650.0),
        "margin_mode": "cross",
        "position_mode": "split",
        "order_kind": "trigger_limit",
        "has_successful_entry_evidence": True,
    }
    values.update(overrides)
    return LegEvidence(**values)


def _equivalent_position(pos_id, **overrides):
    values = {
        "pos_id": pos_id,
        "symbol": "ETH-USDT-SWAP",
        "side": "short",
        "size": 1.5,
        "average_price": 1770.0,
        "created_at_ms": 10_000,
        "entry_price": 1770.0,
        "stop_loss": 1820.0,
        "take_profits": (1700.0, 1650.0),
        "margin_mode": "cross",
        "position_mode": "split",
    }
    values.update(overrides)
    return PositionEvidence(**values)


def _closed_2x2_edges():
    return {(leg_id, pos_id) for leg_id in (1, 2) for pos_id in ("pos-1", "pos-2")}


def test_equivalent_closed_2x2_component_is_classified_without_assignments():
    legs = [_equivalent_leg(1), _equivalent_leg(2)]
    positions = [_equivalent_position("pos-1"), _equivalent_position("pos-2")]
    components = classify_equivalent_attribution_components(
        legs,
        positions,
        _closed_2x2_edges(),
    )

    assert [(item.leg_ids, item.position_ids) for item in components] == [
        ((1, 2), ("pos-1", "pos-2"))
    ]
    assert match_entry_legs_to_positions(legs, positions, []).assignments == {}


def test_authoritative_direct_pos_id_survives_partial_close_size_drift():
    leg = _equivalent_leg(
        1,
        order_id="pos-partial",
        pos_id="pos-partial",
        requested_size=9.0,
        side="long",
        symbol="BTC-USDT-SWAP",
        entry_price=63050.0,
        stop_loss=62000.0,
        take_profits=(64100.0,),
    )
    position = _equivalent_position(
        "pos-partial",
        symbol="BTC-USDT-SWAP",
        side="long",
        size=5.0,
        average_price=63050.0,
        entry_price=63050.0,
        stop_loss=62000.0,
        take_profits=(64100.0,),
    )

    result = match_entry_legs_to_positions([leg], [position], [])

    assert result.assignments == {1: "pos-partial"}
    assert result.evidence_by_leg[1]["evidence_type"] == "direct_pos_id"
    assert result.evidence_by_leg[1]["evidence_source"] == "persisted_leg"


def test_canonical_live_position_economics_accepts_deepcoin_mrg_position_key():
    economics = canonical_live_position_economics(
        [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "pos": "9",
                "avgPx": "63050",
                "mgnMode": "cross",
                "mrgPosition": "split",
            }
        ],
        target_pos_ids=["pos-1"],
        instrument_id="BTC-USDT-SWAP",
        side="long",
    )

    assert economics == (
        {
            "pos_id": "pos-1",
            "instrument_id": "BTC-USDT-SWAP",
            "side": "long",
            "size": "9",
            "avg_entry_price": "63050",
            "margin_mode": "cross",
            "position_mode": "split",
        },
    )


def test_equivalent_permutation_assignment_is_explicit_stable_and_evidenced():
    legs = [_equivalent_leg(244), _equivalent_leg(245)]
    positions = [
        _equivalent_position("1001124099803507"),
        _equivalent_position("1001124099803509"),
    ]
    fills = [
        _fill("order-244", 10_000, source="trigger_fill"),
        _fill("order-245", 10_000, source="trigger_fill"),
    ]

    default_result = match_entry_legs_to_positions(legs, positions, fills)
    result = match_entry_legs_to_positions(
        legs,
        positions,
        fills,
        allow_equivalent_permutation=True,
    )
    reversed_result = match_entry_legs_to_positions(
        reversed(legs),
        reversed(positions),
        reversed(fills),
        allow_equivalent_permutation=True,
    )

    expected_assignments = {
        244: "1001124099803507",
        245: "1001124099803509",
    }
    assert default_result.assignments == {}
    assert default_result.conflicts == [
        {
            "leg_ids": [244, 245],
            "position_ids": ["1001124099803507", "1001124099803509"],
        }
    ]
    assert result.assignments == expected_assignments
    assert reversed_result.assignments == expected_assignments
    assert reversed_result.evidence_by_leg == result.evidence_by_leg
    evidence = result.evidence_by_leg[244]
    assert evidence["policy_version"] == 2
    assert evidence["evidence_type"] == "equivalent_permutation_assignment"
    assert evidence["component_leg_ids"] == [244, 245]
    assert evidence["component_position_ids"] == [
        "1001124099803507",
        "1001124099803509",
    ]
    expected_common_signature = {
        "binding_id": 100,
        "entry_price": 1770.0,
        "margin_mode": "cross",
        "order_kind": "trigger_limit",
        "position_mode": "split",
        "protection_mutated": False,
        "requested_size": 1.5,
        "side": "short",
        "stop_loss": 1820.0,
        "strategy_instance_id": "strategy-1",
        "symbol": "ETH-USDT-SWAP",
        "take_profits": [1650.0, 1700.0],
        "venue": "deepcoin",
    }
    assert {
        key: evidence["equivalence_signature"][key]
        for key in expected_common_signature
    } == expected_common_signature
    assert evidence["mapping_basis"] == "stable_sorted_canonicalization"
    assert evidence["ownership_statement"] == (
        "binding owner proven; parent-child mapping canonicalized"
    )
    assert evidence["equivalence_signature"]["leg_population"] == [
        {
            "binding_id": 100,
            "entry_price": 1770.0,
            "leg_id": 244,
            "margin_mode": "cross",
            "order_kind": "trigger_limit",
            "position_mode": "split",
            "protection_mutated": False,
            "requested_size": 1.5,
            "side": "short",
            "stop_loss": 1820.0,
            "strategy_instance_id": "strategy-1",
            "symbol": "ETH-USDT-SWAP",
            "take_profits": [1650.0, 1700.0],
            "venue": "deepcoin",
        },
        {
            "binding_id": 100,
            "entry_price": 1770.0,
            "leg_id": 245,
            "margin_mode": "cross",
            "order_kind": "trigger_limit",
            "position_mode": "split",
            "protection_mutated": False,
            "requested_size": 1.5,
            "side": "short",
            "stop_loss": 1820.0,
            "strategy_instance_id": "strategy-1",
            "symbol": "ETH-USDT-SWAP",
            "take_profits": [1650.0, 1700.0],
            "venue": "deepcoin",
        },
    ]
    assert evidence["equivalence_signature"]["position_population"] == [
        {
            "entry_price": 1770.0,
            "margin_mode": "cross",
            "position_id": "1001124099803507",
            "position_mode": "split",
            "side": "short",
            "size": 1.5,
            "stop_loss": 1820.0,
            "symbol": "ETH-USDT-SWAP",
            "take_profits": [1650.0, 1700.0],
        },
        {
            "entry_price": 1770.0,
            "margin_mode": "cross",
            "position_id": "1001124099803509",
            "position_mode": "split",
            "side": "short",
            "size": 1.5,
            "stop_loss": 1820.0,
            "symbol": "ETH-USDT-SWAP",
            "take_profits": [1650.0, 1700.0],
        },
    ]


def test_equivalent_assignment_numeric_position_sort_has_a_stable_tie_breaker():
    assert _numeric_aware_identifier_key("01") != _numeric_aware_identifier_key("1")
    assert sorted(["1", "01"], key=_numeric_aware_identifier_key) == sorted(
        ["01", "1"], key=_numeric_aware_identifier_key
    )

    legs = [_equivalent_leg(1), _equivalent_leg(2)]
    positions = [_equivalent_position("1"), _equivalent_position("01")]
    fills = [
        _fill("order-1", 10_000, source="trigger_fill"),
        _fill("order-2", 10_000, source="trigger_fill"),
    ]

    forward = match_entry_legs_to_positions(
        legs, positions, fills, allow_equivalent_permutation=True
    )
    reversed_result = match_entry_legs_to_positions(
        reversed(legs),
        reversed(positions),
        reversed(fills),
        allow_equivalent_permutation=True,
    )

    assert forward.assignments == reversed_result.assignments
    assert forward.evidence_by_leg == reversed_result.evidence_by_leg


def test_equivalent_component_rejects_cross_binding_graph():
    components = classify_equivalent_attribution_components(
        [_equivalent_leg(1), _equivalent_leg(2, binding_id=101)],
        [_equivalent_position("pos-1"), _equivalent_position("pos-2")],
        _closed_2x2_edges(),
    )

    assert components == ()


@pytest.mark.parametrize(
    ("leg_overrides", "position_overrides"),
    [
        ({"requested_size": 2.0}, {}),
        ({"entry_price": 1771.0}, {}),
        ({"take_profits": (1690.0,)}, {}),
        ({"stop_loss": 1830.0}, {}),
        ({"symbol": "BTC-USDT-SWAP"}, {}),
        ({"side": "long"}, {}),
        ({"margin_mode": "isolated"}, {}),
        ({"position_mode": "net"}, {}),
        ({"order_kind": "market"}, {}),
    ],
)
def test_equivalent_component_rejects_unequal_economic_signatures(
    leg_overrides, position_overrides
):
    components = classify_equivalent_attribution_components(
        [_equivalent_leg(1), _equivalent_leg(2, **leg_overrides)],
        [
            _equivalent_position("pos-1"),
            _equivalent_position("pos-2", **position_overrides),
        ],
        _closed_2x2_edges(),
    )

    assert components == ()


def test_equivalent_component_rejects_terminal_leg():
    components = classify_equivalent_attribution_components(
        [_equivalent_leg(1), _equivalent_leg(2, terminal=True)],
        [_equivalent_position("pos-1"), _equivalent_position("pos-2")],
        _closed_2x2_edges(),
    )

    assert components == ()


def test_equivalent_component_rejects_candidate_edge_outside_evidence_population():
    components = classify_equivalent_attribution_components(
        [_equivalent_leg(1), _equivalent_leg(2)],
        [_equivalent_position("pos-1"), _equivalent_position("pos-2")],
        {*_closed_2x2_edges(), (1, "outside-position")},
    )

    assert components == ()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"evidence_available": False},
        {},
    ],
)
def test_equivalent_component_rejects_api_failure_or_missing_fill_evidence(kwargs):
    legs = [_equivalent_leg(1), _equivalent_leg(2)]
    if not kwargs:
        legs[1] = _equivalent_leg(2, has_successful_entry_evidence=False)

    components = classify_equivalent_attribution_components(
        legs,
        [_equivalent_position("pos-1"), _equivalent_position("pos-2")],
        _closed_2x2_edges(),
        **kwargs,
    )

    assert components == ()


def test_equivalent_component_rejects_authoritative_outside_owner():
    components = classify_equivalent_attribution_components(
        [_equivalent_leg(1), _equivalent_leg(2)],
        [_equivalent_position("pos-1"), _equivalent_position("pos-2")],
        _closed_2x2_edges(),
        authoritative_owner_by_position={"pos-1": 99},
    )

    assert components == ()


def test_distinct_direct_protection_filters_edges_to_mutual_unique_components():
    legs = [
        _equivalent_leg(1, stop_loss=1820.0, take_profits=(1700.0,)),
        _equivalent_leg(2, stop_loss=1830.0, take_profits=(1690.0,)),
    ]
    positions = [
        _equivalent_position("pos-1", stop_loss=1820.0, take_profits=(1700.0,)),
        _equivalent_position("pos-2", stop_loss=1830.0, take_profits=(1690.0,)),
    ]

    filtered = filter_candidate_edges_by_entry_protection(
        legs, positions, _closed_2x2_edges()
    )
    components = classify_equivalent_attribution_components(
        legs, positions, _closed_2x2_edges()
    )

    assert filtered == frozenset({(1, "pos-1"), (2, "pos-2")})
    assert [(item.leg_ids, item.position_ids) for item in components] == [
        ((1,), ("pos-1",)),
        ((2,), ("pos-2",)),
    ]


@pytest.mark.parametrize(
    ("leg_overrides", "position_overrides"),
    [
        ({}, {}),
        ({"stop_loss": None, "take_profits": ()}, {}),
        ({}, {"stop_loss": None, "take_profits": ()}),
    ],
)
def test_equal_or_missing_protection_does_not_break_candidate_tie(
    leg_overrides, position_overrides
):
    legs = [
        _equivalent_leg(1, **leg_overrides),
        _equivalent_leg(2, **leg_overrides),
    ]
    positions = [
        _equivalent_position("pos-1", **position_overrides),
        _equivalent_position("pos-2", **position_overrides),
    ]

    assert filter_candidate_edges_by_entry_protection(
        legs, positions, _closed_2x2_edges()
    ) == frozenset(_closed_2x2_edges())
    if leg_overrides or position_overrides:
        assert (
            classify_equivalent_attribution_components(
                legs, positions, _closed_2x2_edges()
            )
            == ()
        )


def test_post_entry_protection_mutation_disables_protection_identity():
    legs = [
        _equivalent_leg(
            1,
            stop_loss=1820.0,
            take_profits=(1700.0,),
            protection_mutated=True,
        ),
        _equivalent_leg(
            2,
            stop_loss=1820.0,
            take_profits=(1700.0,),
            protection_mutated=True,
        ),
    ]
    positions = [
        _equivalent_position("pos-1", stop_loss=1830.0, take_profits=(1690.0,)),
        _equivalent_position("pos-2", stop_loss=1840.0, take_profits=(1680.0,)),
    ]

    assert filter_candidate_edges_by_entry_protection(
        legs, positions, _closed_2x2_edges()
    ) == frozenset(_closed_2x2_edges())
    components = classify_equivalent_attribution_components(
        legs, positions, _closed_2x2_edges()
    )

    assert [(item.leg_ids, item.position_ids) for item in components] == [
        ((1, 2), ("pos-1", "pos-2"))
    ]


def test_incident_assigns_both_smart_legs_and_excludes_cancelled_horse_leg():
    horse_cancelled = _leg(1, order_id="horse-trigger", terminal=True)
    smart_market = _leg(2, order_id="smart-market")
    smart_trigger = _leg(3, order_id="smart-trigger")
    market_time = 1_720_000_000_000
    trigger_time = market_time + 69_000
    positions = [
        _position("1001124083084014", market_time),
        _position("1001124083099498", trigger_time),
    ]
    evidence = [
        _fill("smart-market", market_time, pos_id="1001124083084014"),
        _fill("smart-trigger", trigger_time, source="trigger_fill"),
        _fill("horse-trigger", trigger_time, source="trigger_fill"),
    ]

    result = match_entry_legs_to_positions(
        [horse_cancelled, smart_market, smart_trigger], positions, evidence
    )

    assert result.assignments == {
        smart_market.leg_id: "1001124083084014",
        smart_trigger.leg_id: "1001124083099498",
    }
    assert horse_cancelled.leg_id not in result.assignments
    assert result.conflicts == []


def test_exact_time_beats_one_second_distance():
    legs = [_leg(1, order_id="order-1")]
    positions = [_position("exact", 10_000), _position("one-second", 11_000)]

    result = match_entry_legs_to_positions(
        legs, positions, [_fill("order-1", 10_000, source="trigger_fill")]
    )

    assert result.assignments == {1: "exact"}


def test_one_second_beats_sixty_nine_seconds_distance():
    legs = [_leg(1, order_id="order-1")]
    positions = [_position("one-second", 11_000), _position("sixty-nine", 79_000)]

    result = match_entry_legs_to_positions(
        legs, positions, [_fill("order-1", 10_000, source="trigger_fill")]
    )

    assert result.assignments == {1: "one-second"}


def test_tied_direct_evidence_is_reported_as_conflict():
    legs = [_leg(1, order_id="order-1"), _leg(2, order_id="order-1")]
    positions = [_position("pos-1", 10_000)]
    evidence = [_fill("order-1", 10_000, source="trigger_fill")]

    result = match_entry_legs_to_positions(legs, positions, evidence)

    assert result.assignments == {}
    assert len(result.conflicts) == 1
    assert result.conflicts[0]["position_ids"] == ["pos-1"]
    assert result.conflicts[0]["leg_ids"] == [1, 2]


def test_matching_is_independent_of_input_order():
    legs = [_leg(1, order_id="order-1"), _leg(2, order_id="order-2")]
    positions = [_position("pos-1", 10_000), _position("pos-2", 20_000)]
    evidence = [
        _fill("order-1", 10_000, source="trigger_fill"),
        _fill("order-2", 20_000, source="trigger_fill"),
    ]

    forward = match_entry_legs_to_positions(legs, positions, evidence)
    reverse = match_entry_legs_to_positions(
        list(reversed(legs)), list(reversed(positions)), list(reversed(evidence))
    )

    assert forward.assignments == reverse.assignments == {1: "pos-1", 2: "pos-2"}
    assert forward.conflicts == reverse.conflicts == []


def test_symbol_side_size_and_time_without_order_evidence_do_not_assign():
    result = match_entry_legs_to_positions(
        [_leg(1)], [_position("pos-1", 10_000)], []
    )

    assert result.assignments == {}
    assert result.conflicts == []
    assert result.unassigned_position_ids == {"pos-1"}


def test_terminal_leg_is_never_a_candidate():
    result = match_entry_legs_to_positions(
        [_leg(1, order_id="order-1", terminal=True)],
        [_position("pos-1", 10_000)],
        [_fill("order-1", 10_000)],
    )

    assert result.assignments == {}
    assert result.unassigned_position_ids == {"pos-1"}


def test_one_position_is_never_assigned_to_two_legs():
    legs = [_leg(1, pos_id="pos-1"), _leg(2, order_id="order-2")]
    positions = [_position("pos-1", 10_000)]

    result = match_entry_legs_to_positions(
        legs, positions, [_fill("order-2", 10_000, source="trigger_fill")]
    )

    assert result.assignments == {1: "pos-1"}
    assert 2 not in result.assignments


def test_exact_client_order_id_can_prove_candidate_link():
    leg = _leg(1, client_order_id="client-1")
    fill = _fill("pos-1", 10_000, client_order_id="client-1")

    result = match_entry_legs_to_positions([leg], [_position("pos-1", 10_000)], [fill])

    assert result.assignments == {1: "pos-1"}
    assert result.evidence_by_leg[1]["evidence_type"] == "direct_order_position_id"


def test_exact_order_fill_without_position_link_fields_does_not_assign():
    fill = FillEvidence(
        source="regular_order",
        order_id="order-1",
        client_order_id=None,
        pos_id=None,
        symbol="ETH-USDT-SWAP",
        side="short",
        size=None,
        price=None,
        created_at_ms=None,
    )

    result = match_entry_legs_to_positions(
        [_leg(1, order_id="order-1")], [_position("pos-1", 10_000)], [fill]
    )

    assert result.assignments == {}
    assert result.unassigned_position_ids == {"pos-1"}


def test_exact_order_fill_without_timestamp_cannot_attach_to_later_position():
    fill = FillEvidence(
        source="trade_fill",
        order_id="order-1",
        client_order_id=None,
        pos_id=None,
        symbol="ETH-USDT-SWAP",
        side="short",
        size=1.5,
        price=1770.0,
        created_at_ms=None,
    )

    result = match_entry_legs_to_positions(
        [_leg(1, order_id="order-1")], [_position("later-pos", 10_000)], [fill]
    )

    assert result.assignments == {}
    assert result.unassigned_position_ids == {"later-pos"}


def test_exact_order_fill_outside_position_link_window_does_not_assign():
    order_time = 10_000
    twenty_hours_later = order_time + 20 * 60 * 60 * 1000

    result = match_entry_legs_to_positions(
        [_leg(1, order_id="order-1")],
        [_position("later-pos", twenty_hours_later)],
        [_fill("order-1", order_time)],
    )

    assert result.assignments == {}
    assert result.unassigned_position_ids == {"later-pos"}


def test_regular_fill_near_in_time_without_direct_position_link_does_not_assign():
    order_time = 1_000_000

    result = match_entry_legs_to_positions(
        [_leg(89, order_id="old-order")],
        [_position("new-unrelated-position", order_time + 1_000)],
        [_fill("old-order", order_time)],
    )

    assert result.assignments == {}
    assert result.unassigned_position_ids == {"new-unrelated-position"}


def test_successful_current_trigger_beats_old_regular_order_for_reopened_position():
    position_time = 1_780_434_230_000
    old_leg = _leg(120, order_id="old-market-order")
    current_trigger_leg = _leg(124, order_id="current-trigger")
    position = _position("current-position", position_time)
    evidence = [
        _fill("old-market-order", position_time - 20 * 60 * 60 * 1000),
        _fill("current-trigger", position_time - 1_000, source="trigger_fill"),
    ]

    result = match_entry_legs_to_positions(
        [old_leg, current_trigger_leg], [position], evidence
    )

    assert result.assignments == {124: "current-position"}
    assert 120 not in result.assignments


def test_cancelled_trigger_history_is_terminal_but_not_fill_evidence():
    cancelled = {
        "_evidence_source": "trigger_history",
        "state": "cancelled",
        "ordId": "trigger-1",
        "sz": "1.5",
        "px": "1770",
        "triggerTime": "10000",
    }

    assert classify_leg_exchange_state(cancelled) == "manually_cancelled"
    assert is_fill_evidence(cancelled) is False


def test_successful_trigger_history_is_explicit_fill_evidence():
    successful = {
        "_evidence_source": "trigger_fill",
        "ordId": "trigger-1",
        "triggerTime": "1784034229",
        "errorCode": "0",
    }
    failed = {**successful, "errorCode": "4"}
    not_triggered = {**successful, "triggerTime": "0"}
    misleading_filled_failure = {
        **successful,
        "state": "filled",
        "triggerTime": "0",
        "errorCode": "4",
    }

    assert is_fill_evidence(successful) is True
    assert is_fill_evidence(failed) is False
    assert is_fill_evidence(not_triggered) is False
    assert is_fill_evidence(misleading_filled_failure) is False


def test_successful_trigger_without_size_cannot_authorize_position():
    fill = FillEvidence(
        source="trigger_fill",
        order_id="trigger-1",
        client_order_id=None,
        pos_id=None,
        symbol="ETH-USDT-SWAP",
        side="short",
        size=None,
        price=1770.0,
        created_at_ms=10_000,
    )

    result = match_entry_legs_to_positions(
        [_leg(1, order_id="trigger-1")], [_position("position-1", 10_000)], [fill]
    )

    assert result.assignments == {}
    assert result.unassigned_position_ids == {"position-1"}


def test_only_explicit_order_fill_or_fills_endpoint_row_is_fill_evidence():
    explicit_fill = {
        "_evidence_source": "order_history",
        "state": "filled",
        "ordId": "order-1",
    }
    trade_fill = {
        "_evidence_source": "trade_fill",
        "ordId": "order-2",
        "fillSz": "1.5",
    }
    numeric_only_history = {
        "_evidence_source": "trigger_history",
        "ordId": "order-3",
        "sz": "1.5",
        "px": "1770",
    }

    assert is_fill_evidence(explicit_fill) is True
    assert is_fill_evidence(trade_fill) is True
    assert is_fill_evidence(numeric_only_history) is False


def test_recorded_cancel_event_classifies_as_exchange_cancelled():
    assert (
        classify_leg_exchange_state(
            {"state": "cancelled"}, cancel_event_action="cancel_trigger_entry"
        )
        == "exchange_cancelled"
    )
