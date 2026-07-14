from telegram_kol_research.protection_attribution import match_position_protection


def _position(pos_id, *, size="1.5", created_at="1782788876000", **overrides):
    row = {
        "instId": "ETH-USDT-SWAP",
        "posId": pos_id,
        "posSide": "long",
        "pos": size,
        "cTime": created_at,
    }
    row.update(overrides)
    return row


def _tpsl(*, created_at="1782788877000", size="0", **overrides):
    row = {
        "instId": "ETH-USDT-SWAP",
        "posSide": "long",
        "triggerOrderType": "TPSL",
        "sz": size,
        "cTime": created_at,
    }
    row.update(overrides)
    return row


def test_one_second_timestamp_difference_matches_full_position_stop():
    result = match_position_protection(
        [_position("pos-smart-market")],
        [_tpsl(ordId="sl-1", slTriggerPrice="1820")],
    )

    protection = result.by_pos_id["pos-smart-market"]
    assert protection.status == "verified"
    assert protection.stop_loss == 1820
    assert protection.order_ids == ["sl-1"]
    assert protection.can_mutate is True


def test_zero_size_tpsl_is_full_position_protection():
    result = match_position_protection(
        [_position("pos-zero", size="5.2")],
        [_tpsl(ordId="sl-zero", size="0", slTriggerPx="1555")],
    )

    assert result.by_pos_id["pos-zero"].stop_loss == 1555
    assert result.by_pos_id["pos-zero"].status == "verified"


def test_partial_take_profit_sizes_can_cover_one_position():
    result = match_position_protection(
        [_position("pos-split", size="1.5")],
        [
            _tpsl(ordId="sl-full", size="0", slTriggerPrice="1820"),
            _tpsl(ordId="tp-1", size="0.9", tpTriggerPrice="1900"),
            _tpsl(ordId="tp-2", size="0.6", tpTriggerPrice="2000"),
        ],
    )

    protection = result.by_pos_id["pos-split"]
    assert protection.status == "verified"
    assert protection.stop_loss == 1820
    assert protection.take_profits == [1900, 2000]
    assert protection.order_ids == ["sl-full", "tp-1", "tp-2"]


def test_stop_and_partial_targets_created_at_nearby_times_form_one_evidence_group():
    result = match_position_protection(
        [_position("pos-staggered", size="1.5", created_at="10000")],
        [
            _tpsl(created_at="10000", ordId="sl", size="0", slTriggerPrice="1820"),
            _tpsl(created_at="11000", ordId="tp-1", size="0.9", tpTriggerPrice="1900"),
            _tpsl(created_at="12000", ordId="tp-2", size="0.6", tpTriggerPrice="2000"),
        ],
    )

    protection = result.by_pos_id["pos-staggered"]
    assert protection.status == "verified"
    assert protection.stop_loss == 1820
    assert protection.take_profits == [1900, 2000]
    assert protection.order_ids == ["sl", "tp-1", "tp-2"]


def test_nearby_positions_keep_each_target_with_its_nearest_stop_group():
    result = match_position_protection(
        [
            _position("pos-a", size="1", created_at="10000"),
            _position("pos-b", size="2", created_at="14000"),
        ],
        [
            _tpsl(created_at="10000", ordId="sl-a", size="0", slTriggerPrice="1800"),
            _tpsl(created_at="14000", ordId="sl-b", size="0", slTriggerPrice="1700"),
            _tpsl(created_at="14000", ordId="tp-b", size="2", tpTriggerPrice="2000"),
            _tpsl(created_at="10000", ordId="tp-a", size="1", tpTriggerPrice="1900"),
        ],
    )

    assert result.by_pos_id["pos-a"].order_ids == ["sl-a", "tp-a"]
    assert result.by_pos_id["pos-a"].stop_loss == 1800
    assert result.by_pos_id["pos-b"].order_ids == ["sl-b", "tp-b"]
    assert result.by_pos_id["pos-b"].stop_loss == 1700


def test_full_stop_does_not_hide_partial_target_size_mismatch():
    result = match_position_protection(
        [
            _position("pos-a", size="1", created_at="10000"),
            _position("pos-b", size="2", created_at="14000"),
        ],
        [
            _tpsl(created_at="10000", ordId="sl-a", size="0", slTriggerPrice="1800"),
            _tpsl(created_at="14000", ordId="sl-b", size="0", slTriggerPrice="1700"),
            _tpsl(created_at="11000", ordId="tp-b", size="2", tpTriggerPrice="2000"),
        ],
    )

    assert result.by_pos_id["pos-a"].status == "present_but_ambiguous"
    assert result.by_pos_id["pos-a"].order_ids == []
    assert result.by_pos_id["pos-b"].status == "present_but_ambiguous"
    assert result.by_pos_id["pos-b"].order_ids == []


def test_equidistant_target_does_not_merge_into_first_stop_group():
    result = match_position_protection(
        [
            _position("pos-a", size="1", created_at="10000"),
            _position("pos-b", size="1", created_at="14000"),
        ],
        [
            _tpsl(created_at="10000", ordId="sl-a", size="0", slTriggerPrice="1800"),
            _tpsl(created_at="14000", ordId="sl-b", size="0", slTriggerPrice="1700"),
            _tpsl(created_at="12000", ordId="tp-unknown", size="1", tpTriggerPrice="1900"),
        ],
    )

    assert result.by_pos_id["pos-a"].status == "present_but_ambiguous"
    assert result.by_pos_id["pos-b"].status == "present_but_ambiguous"


def test_exact_position_id_has_priority_over_indistinguishable_positions():
    result = match_position_protection(
        [_position("pos-a"), _position("pos-b")],
        [_tpsl(posId="pos-b", ordId="sl-b", slTriggerPrice="1820")],
    )

    assert result.by_pos_id["pos-a"].status == "absent"
    assert result.by_pos_id["pos-b"].status == "verified"
    assert result.by_pos_id["pos-b"].stop_loss == 1820


def test_indistinguishable_positions_make_unscoped_protection_ambiguous():
    result = match_position_protection(
        [_position("pos-a"), _position("pos-b")],
        [_tpsl(ordId="sl-unknown", slTriggerPrice="1820")],
    )

    for pos_id in ("pos-a", "pos-b"):
        protection = result.by_pos_id[pos_id]
        assert protection.status == "present_but_ambiguous"
        assert protection.stop_loss is None
        assert protection.order_ids == []
        assert protection.can_mutate is False


def test_ambiguous_extra_order_blocks_mutation_even_with_one_exact_order():
    result = match_position_protection(
        [_position("pos-a"), _position("pos-b")],
        [
            _tpsl(posId="pos-a", ordId="sl-a", slTriggerPrice="1810"),
            _tpsl(ordId="sl-unknown", slTriggerPrice="1820"),
        ],
    )

    assert result.by_pos_id["pos-a"].status == "present_but_ambiguous"
    assert result.by_pos_id["pos-a"].order_ids == []
    assert result.by_pos_id["pos-a"].can_mutate is False


def test_missing_tpsl_evidence_is_not_reported_as_absent():
    result = match_position_protection(
        [_position("pos-api-error")],
        [],
        evidence_available=False,
    )

    protection = result.by_pos_id["pos-api-error"]
    assert protection.status == "evidence_unavailable"
    assert protection.can_mutate is False


def test_unscoped_tpsl_without_timestamps_never_authorizes_mutation():
    position = _position("pos-no-time")
    position.pop("cTime")
    order = _tpsl(ordId="sl-no-time", slTriggerPrice="1820")
    order.pop("cTime")

    result = match_position_protection([position], [order])

    protection = result.by_pos_id["pos-no-time"]
    assert protection.status == "present_but_ambiguous"
    assert protection.order_ids == []
    assert protection.can_mutate is False


def test_two_unscoped_groups_competing_for_one_position_are_ambiguous():
    result = match_position_protection(
        [_position("pos-one", created_at="10000")],
        [
            _tpsl(created_at="10000", ordId="sl-one", slTriggerPrice="1820"),
            _tpsl(created_at="11000", ordId="sl-two", slTriggerPrice="1810"),
        ],
    )

    protection = result.by_pos_id["pos-one"]
    assert protection.status == "present_but_ambiguous"
    assert protection.order_ids == []
    assert protection.can_mutate is False


def test_incompatible_size_does_not_authorize_unscoped_tpsl():
    result = match_position_protection(
        [_position("pos-size", size="1.5", created_at="10000")],
        [_tpsl(created_at="10000", size="9", ordId="sl-wrong", slTriggerPrice="1820")],
    )

    protection = result.by_pos_id["pos-size"]
    assert protection.status == "present_but_ambiguous"
    assert protection.can_mutate is False
