from telegram_kol_research.position_attribution import (
    FillEvidence,
    LegEvidence,
    PositionEvidence,
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


def _fill(order_id, created_at_ms, *, source="regular_order", client_order_id=None):
    return FillEvidence(
        source=source,
        order_id=order_id,
        client_order_id=client_order_id,
        pos_id=None,
        symbol="ETH-USDT-SWAP",
        side="short",
        size=1.5,
        price=1770.0,
        created_at_ms=created_at_ms,
    )


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
        _fill("smart-market", market_time),
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

    result = match_entry_legs_to_positions(legs, positions, [_fill("order-1", 10_000)])

    assert result.assignments == {1: "exact"}


def test_one_second_beats_sixty_nine_seconds_distance():
    legs = [_leg(1, order_id="order-1")]
    positions = [_position("one-second", 11_000), _position("sixty-nine", 79_000)]

    result = match_entry_legs_to_positions(legs, positions, [_fill("order-1", 10_000)])

    assert result.assignments == {1: "one-second"}


def test_tied_direct_evidence_is_reported_as_conflict():
    legs = [_leg(1, order_id="order-1"), _leg(2, order_id="order-1")]
    positions = [_position("pos-1", 10_000)]
    evidence = [_fill("order-1", 10_000)]

    result = match_entry_legs_to_positions(legs, positions, evidence)

    assert result.assignments == {}
    assert len(result.conflicts) == 1
    assert result.conflicts[0]["position_ids"] == ["pos-1"]
    assert result.conflicts[0]["leg_ids"] == [1, 2]


def test_matching_is_independent_of_input_order():
    legs = [_leg(1, order_id="order-1"), _leg(2, order_id="order-2")]
    positions = [_position("pos-1", 10_000), _position("pos-2", 20_000)]
    evidence = [_fill("order-1", 10_000), _fill("order-2", 20_000)]

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
        legs, positions, [_fill("order-2", 10_000)]
    )

    assert result.assignments == {1: "pos-1"}
    assert 2 not in result.assignments


def test_exact_client_order_id_can_prove_candidate_link():
    leg = _leg(1, client_order_id="client-1")
    fill = _fill("exchange-order", 10_000, client_order_id="client-1")

    result = match_entry_legs_to_positions([leg], [_position("pos-1", 10_000)], [fill])

    assert result.assignments == {1: "pos-1"}
    assert result.evidence_by_leg[1]["evidence_type"] == "exact_client_order_id"
