from datetime import UTC, datetime, timedelta

from telegram_kol_research.trigger_protection_assignment import (
    ProtectionOrderCandidate,
    ProtectionOwner,
    assign_trigger_protection_orders,
)


BASE_TIME = datetime(2026, 8, 5, 17, 40, tzinfo=UTC)


def _owner(
    leg_id: int,
    pos_id: str,
    *,
    created_at: datetime | None = None,
) -> ProtectionOwner:
    return ProtectionOwner(
        leg_id=leg_id,
        binding_id=80 + leg_id,
        pos_id=pos_id,
        instrument_id="ETH-USDT-SWAP",
        side="short",
        size_text="3.4",
        stop_price="1935",
        position_created_at=created_at or BASE_TIME,
    )


def _candidate(
    order_id: str,
    *,
    created_at: datetime | None = None,
    explicit_pos_ids: tuple[str, ...] = (),
) -> ProtectionOrderCandidate:
    return ProtectionOrderCandidate(
        order_id=order_id,
        instrument_id="ETH-USDT-SWAP",
        side="short",
        size_text="3.4",
        stop_price="1935",
        created_at=created_at or BASE_TIME + timedelta(seconds=1),
        explicit_pos_ids=explicit_pos_ids,
    )


def test_assigns_identical_split_stops_after_excluding_existing_owner():
    first_owner = _owner(433, "first-pos")
    second_owner = _owner(
        434,
        "second-pos",
        created_at=BASE_TIME + timedelta(minutes=20),
    )
    first_stop = _candidate("first-stop")
    second_stop = _candidate(
        "second-stop",
        created_at=BASE_TIME + timedelta(minutes=20, seconds=1),
    )

    result = assign_trigger_protection_orders(
        owners=(first_owner, second_owner),
        candidates=(first_stop, second_stop),
        existing_order_owners={first_stop.order_id: first_owner.pos_id},
        snapshot_complete=True,
    )

    assert result.assignments == {second_owner.leg_id: second_stop.order_id}
    assert result.conflicts == ()


def test_assignment_is_independent_of_candidate_and_owner_order():
    first_owner = _owner(433, "first-pos")
    second_owner = _owner(
        434,
        "second-pos",
        created_at=BASE_TIME + timedelta(minutes=20),
    )
    first_stop = _candidate("first-stop")
    second_stop = _candidate(
        "second-stop",
        created_at=BASE_TIME + timedelta(minutes=20, seconds=1),
    )
    kwargs = {
        "existing_order_owners": {first_stop.order_id: first_owner.pos_id},
        "snapshot_complete": True,
    }

    forward = assign_trigger_protection_orders(
        owners=(first_owner, second_owner),
        candidates=(first_stop, second_stop),
        **kwargs,
    )
    reverse = assign_trigger_protection_orders(
        owners=(second_owner, first_owner),
        candidates=(second_stop, first_stop),
        **kwargs,
    )

    assert forward == reverse


def test_prefill_candidate_is_excluded_without_blocking_newer_unique_candidate():
    owner = _owner(
        434,
        "second-pos",
        created_at=BASE_TIME + timedelta(minutes=20),
    )
    old_stop = _candidate("old-stop")
    new_stop = _candidate(
        "new-stop",
        created_at=BASE_TIME + timedelta(minutes=20, seconds=1),
    )

    result = assign_trigger_protection_orders(
        owners=(owner,),
        candidates=(old_stop, new_stop),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {owner.leg_id: new_stop.order_id}
    assert result.exclusions[old_stop.order_id] == "candidate_predates_fill"


def test_true_many_to_many_shape_remains_unassigned():
    owners = (_owner(433, "first-pos"), _owner(434, "second-pos"))
    candidates = (_candidate("first-stop"), _candidate("second-stop"))

    result = assign_trigger_protection_orders(
        owners=owners,
        candidates=candidates,
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert {conflict.reason_code for conflict in result.conflicts} == {
        "protection_assignment_not_mutual_unique"
    }


def test_explicit_position_identity_resolves_otherwise_identical_candidates():
    owners = (_owner(433, "first-pos"), _owner(434, "second-pos"))
    candidates = (
        _candidate("first-stop", explicit_pos_ids=("first-pos",)),
        _candidate("second-stop", explicit_pos_ids=("second-pos",)),
    )

    result = assign_trigger_protection_orders(
        owners=owners,
        candidates=candidates,
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {433: "first-stop", 434: "second-stop"}
    assert result.conflicts == ()


def test_conflicting_position_aliases_never_create_an_assignment():
    candidate = _candidate(
        "conflicted-stop",
        explicit_pos_ids=("first-pos", "second-pos"),
    )

    result = assign_trigger_protection_orders(
        owners=(_owner(433, "first-pos"),),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_pos_id_conflict"
    assert result.conflicts[0].reason_code == "candidate_pos_id_conflict"


def test_incomplete_snapshot_fails_closed_with_a_stable_fingerprint():
    kwargs = {
        "owners": (_owner(433, "first-pos"),),
        "candidates": (_candidate("first-stop"),),
        "existing_order_owners": {},
        "snapshot_complete": False,
    }

    result = assign_trigger_protection_orders(**kwargs)
    repeated = assign_trigger_protection_orders(**kwargs)

    assert result.assignments == {}
    assert result.conflicts[0].reason_code == "snapshot_incomplete"
    assert result.snapshot_fingerprint == repeated.snapshot_fingerprint


def test_missing_timestamp_is_excluded_instead_of_guessed():
    candidate = _candidate("undated-stop")
    object.__setattr__(candidate, "created_at", None)

    result = assign_trigger_protection_orders(
        owners=(_owner(433, "first-pos"),),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_time_unavailable"


def test_immutable_owner_conflict_fails_closed():
    candidate = _candidate("first-stop", explicit_pos_ids=("first-pos",))

    result = assign_trigger_protection_orders(
        owners=(_owner(433, "first-pos"),),
        candidates=(candidate,),
        existing_order_owners={candidate.order_id: "other-pos"},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert result.conflicts[0].reason_code == "immutable_owner_conflict"


def test_order_owned_by_another_live_position_is_not_reused():
    candidate = _candidate("other-stop")

    result = assign_trigger_protection_orders(
        owners=(_owner(433, "first-pos"),),
        candidates=(candidate,),
        existing_order_owners={candidate.order_id: "other-pos"},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_owned_by_other_position"
