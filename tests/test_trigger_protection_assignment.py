from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

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
        client_order_id=f"client-{leg_id}",
    )


def _candidate(
    order_id: str,
    *,
    created_at: datetime | None = None,
    explicit_pos_ids: tuple[str, ...] = (),
    explicit_client_order_ids: tuple[str, ...] = (),
    order_id_aliases: tuple[str, ...] = (),
) -> ProtectionOrderCandidate:
    exact_time = created_at or BASE_TIME + timedelta(seconds=1)
    return ProtectionOrderCandidate(
        order_id=order_id,
        instrument_id="ETH-USDT-SWAP",
        side="short",
        size_text="3.4",
        stop_price="1935",
        created_at=exact_time,
        explicit_pos_ids=explicit_pos_ids,
        explicit_client_order_ids=explicit_client_order_ids,
        order_id_aliases=order_id_aliases,
        created_at_raw=exact_time.isoformat(),
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


@pytest.mark.parametrize(
    "client_aliases",
    [
        ("wrong-client",),
        ("client-433", "wrong-client"),
    ],
)
def test_candidate_client_identity_must_uniquely_match_owner(client_aliases):
    candidate = _candidate(
        "first-stop",
        explicit_pos_ids=("first-pos",),
        explicit_client_order_ids=client_aliases,
    )

    result = assign_trigger_protection_orders(
        owners=(_owner(433, "first-pos"),),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}


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


def test_explicit_position_candidate_missing_timestamp_is_still_excluded():
    candidate = _candidate(
        "undated-direct-stop", explicit_pos_ids=("first-pos",)
    )
    object.__setattr__(candidate, "created_at", None)
    object.__setattr__(candidate, "created_at_raw", None)

    result = assign_trigger_protection_orders(
        owners=(_owner(433, "first-pos"),),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_time_unavailable"


@pytest.mark.parametrize(
    ("created_at", "reason"),
    [
        (
            BASE_TIME - timedelta(microseconds=1),
            "candidate_predates_submission_intent",
        ),
        (BASE_TIME + timedelta(seconds=11), "candidate_after_snapshot"),
    ],
)
def test_explicit_position_candidate_obeys_lineage_sequence_bounds(
    created_at, reason
):
    owner = replace(
        _owner(433, "first-pos"),
        intent_created_at=BASE_TIME,
        snapshot_observed_at=BASE_TIME + timedelta(seconds=10),
        lineage_evidence_required=True,
        direct_identity_permitted=True,
    )
    candidate = _candidate(
        "direct-stop",
        explicit_pos_ids=("first-pos",),
        created_at=created_at,
    )

    result = assign_trigger_protection_orders(
        owners=(owner,),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == reason


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


def test_candidate_order_alias_conflict_fails_closed():
    candidate = _candidate(
        "first-stop",
        order_id_aliases=("first-stop", "other-stop"),
    )

    result = assign_trigger_protection_orders(
        owners=(_owner(433, "first-pos"),),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert result.conflicts[0].reason_code == "candidate_order_id_alias_conflict"


def test_conflicting_alias_pollutes_that_order_id_across_the_account_snapshot():
    conflicted = _candidate(
        "first-stop", order_id_aliases=("first-stop", "shared-stop")
    )
    apparently_exact = _candidate(
        "shared-stop", order_id_aliases=("shared-stop",)
    )

    result = assign_trigger_protection_orders(
        owners=(_owner(433, "first-pos"),),
        candidates=(conflicted, apparently_exact),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert any(
        conflict.reason_code == "candidate_order_id_not_unique"
        and conflict.candidate_order_ids == ("shared-stop",)
        for conflict in result.conflicts
    )


def test_duplicate_owner_pos_id_fails_closed_for_every_owner():
    owners = (_owner(433, "same-pos"), _owner(434, "same-pos"))
    result = assign_trigger_protection_orders(
        owners=owners,
        candidates=(_candidate("second-stop", explicit_pos_ids=("same-pos",)),),
        existing_order_owners={"first-stop": "same-pos"},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    conflict = next(
        row for row in result.conflicts if row.reason_code == "owner_pos_id_not_unique"
    )
    assert conflict.owner_leg_ids == (433, 434)


def test_duplicate_owner_pos_id_blocks_unrelated_anonymous_lineage_assignment():
    duplicate_a = replace(
        _lineage_owner(433, "same-pos"),
        lineage_evidence_required=True,
    )
    duplicate_b = replace(
        _lineage_owner(434, "same-pos"),
        lineage_evidence_required=True,
    )
    otherwise_complete = replace(
        _lineage_owner(435, "target-pos"),
        lineage_evidence_required=True,
    )

    result = assign_trigger_protection_orders(
        owners=(duplicate_a, duplicate_b, otherwise_complete),
        candidates=(_candidate("shared-stop"),),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}


def test_unchanged_ownership_evidence_has_stable_fingerprint_across_poll_times():
    owner = _owner(433, "first-pos")
    first = assign_trigger_protection_orders(
        owners=(replace(owner, snapshot_observed_at=BASE_TIME + timedelta(seconds=1)),),
        candidates=(_candidate("stop-1"),),
        existing_order_owners={},
        snapshot_complete=True,
    )
    second = assign_trigger_protection_orders(
        owners=(replace(owner, snapshot_observed_at=BASE_TIME + timedelta(minutes=1)),),
        candidates=(_candidate("stop-1"),),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert first.snapshot_fingerprint == second.snapshot_fingerprint


def test_incomplete_same_shape_owner_blocks_anonymous_candidate_for_other_owner():
    incomplete = replace(
        _lineage_owner(433, "first-pos"),
        lineage_evidence_required=True,
        lineage_attestation_fingerprint=None,
        direct_identity_permitted=False,
    )
    complete = replace(
        _lineage_owner(434, "second-pos"),
        lineage_evidence_required=True,
    )
    candidate = _candidate("anonymous-stop")

    result = assign_trigger_protection_orders(
        owners=(incomplete, complete),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert any(
        conflict.reason_code == "protection_assignment_not_mutual_unique"
        for conflict in result.conflicts
    )


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


def _lineage_regression_cases():
    fixture_path = Path(__file__).parent / "fixtures" / "trigger_protection_lineage_cases.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))["cases"]


def _fixture_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_real_attached_stop_lineage_cases_are_not_rejected_for_predating_position():
    """RED: the stop and child share cTime, but the position projection is later."""

    for case in _lineage_regression_cases():
        assert case["candidate_created_at"] == case["child_exchange_created_at"]
        assert case["pre_submit_tpsl_baseline"] == []
        assert case["protection_response"]["data"]["attached_on_trigger_order"] is True
        owner = ProtectionOwner(
            leg_id=case["leg_id"],
            binding_id=case["binding_id"],
            pos_id=case["pos_id"],
            instrument_id=case["instrument_id"],
            side=case["side"],
            size_text=case["size_text"],
            stop_price=case["stop_price"],
            position_created_at=_fixture_time(case["live_position_created_at"]),
            intent_id=case["intent_id"],
            intent_created_at=_fixture_time(case["intent_created_at"]),
            snapshot_observed_at=_fixture_time(case["snapshot_observed_at"]),
            child_regular_order_id=case["child_regular_order_id"],
            child_exchange_created_at=_fixture_time(
                case["child_exchange_created_at"]
            ),
            child_exchange_created_at_raw=case["child_exchange_created_at"],
            parent_trigger_order_id=case["parent_trigger_order_id"],
            request_fingerprint="a" * 64,
            owner_baseline_order_ids=(),
            owner_baseline_fingerprint="b" * 64,
            lineage_attestation_fingerprint="c" * 64,
        )
        candidate = ProtectionOrderCandidate(
            order_id=case["candidate_order_id"],
            instrument_id=case["instrument_id"],
            side=case["side"],
            size_text=case["size_text"],
            stop_price=case["stop_price"],
            created_at=_fixture_time(case["candidate_created_at"]),
            created_at_raw=case["candidate_created_at"],
        )

        result = assign_trigger_protection_orders(
            owners=(owner,),
            candidates=(candidate,),
            existing_order_owners={},
            snapshot_complete=True,
        )

        assert result.assignments == {case["leg_id"]: case["candidate_order_id"]}
        assert result.evidence_by_leg[case["leg_id"]]["match_kind"] == (
            "lineage_attested_attached_stop"
        )


def _lineage_owner(
    leg_id: int,
    pos_id: str,
    *,
    child_time: datetime = BASE_TIME + timedelta(seconds=1),
    baseline: tuple[str, ...] = (),
) -> ProtectionOwner:
    return ProtectionOwner(
        leg_id=leg_id,
        binding_id=80 + leg_id,
        pos_id=pos_id,
        instrument_id="ETH-USDT-SWAP",
        side="short",
        size_text="3.4",
        stop_price="1935",
        position_created_at=BASE_TIME + timedelta(seconds=10),
        intent_id=leg_id + 1000,
        intent_created_at=BASE_TIME,
        snapshot_observed_at=BASE_TIME + timedelta(seconds=20),
        child_regular_order_id=pos_id,
        child_exchange_created_at=child_time,
        child_exchange_created_at_raw=child_time.isoformat(),
        parent_trigger_order_id=f"parent-{leg_id}",
        request_fingerprint="a" * 64,
        owner_baseline_order_ids=baseline,
        owner_baseline_fingerprint="b" * 64,
        lineage_attestation_fingerprint=f"{leg_id:064d}",
    )


def test_lineage_owner_uses_its_own_baseline_only():
    earlier = _lineage_owner(433, "first-pos")
    later = _lineage_owner(434, "second-pos", baseline=("first-stop",))
    candidate = _candidate("first-stop")

    result = assign_trigger_protection_orders(
        owners=(earlier, later),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {earlier.leg_id: candidate.order_id}


def test_blocking_only_owner_without_intent_time_prevents_anonymous_adoption():
    complete_owner = _lineage_owner(434, "second-pos")
    blocking_owner = replace(
        _owner(433, "first-pos"),
        intent_created_at=None,
        snapshot_observed_at=None,
        lineage_evidence_required=True,
        direct_identity_permitted=False,
    )
    candidate = _candidate("shared-stop")

    result = assign_trigger_protection_orders(
        owners=(blocking_owner, complete_owner),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert any(
        conflict.reason_code == "protection_assignment_not_mutual_unique"
        and conflict.owner_leg_ids == (433, 434)
        for conflict in result.conflicts
    )


def test_legacy_owner_keeps_the_old_global_baseline_exclusion():
    owner = _owner(433, "first-pos")
    owner = replace(owner, owner_baseline_order_ids=("first-stop",))
    candidate = _candidate("first-stop")

    result = assign_trigger_protection_orders(
        owners=(owner,), candidates=(candidate,), existing_order_owners={}, snapshot_complete=True
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_in_global_baseline"


def test_lineage_candidate_in_owners_own_baseline_is_rejected():
    owner = _lineage_owner(433, "first-pos", baseline=("first-stop",))
    candidate = _candidate("first-stop")

    result = assign_trigger_protection_orders(
        owners=(owner,), candidates=(candidate,), existing_order_owners={}, snapshot_complete=True
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_in_owner_baseline"


@pytest.mark.parametrize(
    ("candidate_time", "reason"),
    [
        (BASE_TIME + timedelta(seconds=2), "candidate_child_time_mismatch"),
        (BASE_TIME - timedelta(microseconds=1), "candidate_child_time_mismatch"),
        (BASE_TIME + timedelta(seconds=21), "candidate_child_time_mismatch"),
    ],
)
def test_lineage_candidate_requires_exact_child_time(candidate_time, reason):
    owner = _lineage_owner(433, "first-pos")
    candidate = _candidate("first-stop", created_at=candidate_time)

    result = assign_trigger_protection_orders(
        owners=(owner,), candidates=(candidate,), existing_order_owners={}, snapshot_complete=True
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == reason


def test_lineage_candidate_cannot_predate_persisted_intent():
    candidate_time = BASE_TIME - timedelta(microseconds=1)
    owner = _lineage_owner(433, "first-pos", child_time=candidate_time)
    candidate = _candidate("first-stop", created_at=candidate_time)

    result = assign_trigger_protection_orders(
        owners=(owner,), candidates=(candidate,), existing_order_owners={}, snapshot_complete=True
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_predates_submission_intent"


def test_lineage_candidate_cannot_follow_its_snapshot():
    candidate_time = BASE_TIME + timedelta(seconds=21)
    owner = _lineage_owner(433, "first-pos", child_time=candidate_time)
    candidate = _candidate("first-stop", created_at=candidate_time)

    result = assign_trigger_protection_orders(
        owners=(owner,), candidates=(candidate,), existing_order_owners={}, snapshot_complete=True
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_after_snapshot"


def test_same_shape_lineage_competitors_remain_unassigned():
    owner = _lineage_owner(433, "first-pos")
    candidates = (_candidate("first-stop"), _candidate("second-stop"))

    result = assign_trigger_protection_orders(
        owners=(owner,), candidates=candidates, existing_order_owners={}, snapshot_complete=True
    )

    assert result.assignments == {}
    assert result.conflicts[0].reason_code == "protection_assignment_not_mutual_unique"


def test_same_child_time_for_two_lineage_owners_remains_unassigned():
    owners = (
        _lineage_owner(433, "first-pos"),
        _lineage_owner(434, "second-pos"),
    )
    candidates = (_candidate("first-stop"), _candidate("second-stop"))

    result = assign_trigger_protection_orders(
        owners=owners, candidates=candidates, existing_order_owners={}, snapshot_complete=True
    )

    assert result.assignments == {}
    assert {item.reason_code for item in result.conflicts} == {
        "protection_assignment_not_mutual_unique"
    }


def test_duplicate_candidate_order_id_is_never_assigned():
    owner = _lineage_owner(433, "first-pos")
    duplicate = _candidate("same-stop")

    result = assign_trigger_protection_orders(
        owners=(owner,),
        candidates=(duplicate, duplicate),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert any(
        item.reason_code == "candidate_order_id_not_unique"
        for item in result.conflicts
    )


def test_same_shape_incomplete_candidate_blocks_complete_candidate_adoption():
    owner = _lineage_owner(433, "first-pos")
    complete = _candidate("complete-stop")
    incomplete = replace(
        _candidate("unknown-stop"),
        evidence_complete=False,
        order_type_aliases=(),
    )

    result = assign_trigger_protection_orders(
        owners=(owner,),
        candidates=(complete, incomplete),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert any(
        conflict.reason_code == "protection_assignment_not_mutual_unique"
        for conflict in result.conflicts
    )


def test_unbounded_incomplete_candidate_closes_lineage_authority():
    owner = _lineage_owner(433, "first-pos")
    complete = _candidate("complete-stop")
    unbounded = replace(
        _candidate("unbounded-stop"),
        size_text="",
        size_aliases=(),
        evidence_complete=False,
    )

    result = assign_trigger_protection_orders(
        owners=(owner,),
        candidates=(complete, unbounded),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert any(
        conflict.reason_code == "candidate_evidence_unbounded"
        for conflict in result.conflicts
    )


def test_explicit_non_tpsl_pending_order_does_not_block_lineage_candidate():
    owner = _lineage_owner(433, "first-pos")
    complete = _candidate("complete-stop")
    trigger_entry = replace(
        _candidate("pending-entry"),
        evidence_complete=False,
        order_type_aliases=("TRIGGER",),
        stop_price="",
        stop_price_aliases=(),
    )

    result = assign_trigger_protection_orders(
        owners=(owner,),
        candidates=(complete, trigger_entry),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {owner.leg_id: complete.order_id}


def test_explicit_non_tpsl_row_still_occupies_duplicate_order_id():
    owner = _lineage_owner(433, "first-pos")
    complete = _candidate("shared-order-id")
    trigger_entry = replace(
        _candidate("shared-order-id"),
        evidence_complete=False,
        order_type_aliases=("TRIGGER",),
        stop_price="",
        stop_price_aliases=(),
    )

    result = assign_trigger_protection_orders(
        owners=(owner,),
        candidates=(complete, trigger_entry),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert any(
        conflict.reason_code == "candidate_order_id_not_unique"
        for conflict in result.conflicts
    )


def test_explicit_pos_id_still_rejects_conflicting_parent_trigger_alias():
    owner = _lineage_owner(433, "first-pos")
    candidate = ProtectionOrderCandidate(
        order_id="first-stop",
        instrument_id="ETH-USDT-SWAP",
        side="short",
        size_text="3.4",
        stop_price="1935",
        created_at=BASE_TIME + timedelta(seconds=1),
        explicit_pos_ids=("first-pos",),
        explicit_parent_order_ids=("other-parent",),
    )

    result = assign_trigger_protection_orders(
        owners=(owner,),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_parent_id_conflict"


def test_anonymous_candidate_rejects_wrong_parent_trigger_alias():
    owner = _lineage_owner(433, "first-pos")
    candidate = replace(
        _candidate("first-stop"),
        explicit_parent_order_ids=("other-parent",),
    )

    result = assign_trigger_protection_orders(
        owners=(owner,),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_parent_id_conflict"


def test_parent_alias_directs_anonymous_candidate_to_exact_owner_only():
    first = _lineage_owner(433, "first-pos")
    second = _lineage_owner(434, "second-pos")
    candidate = replace(
        _candidate("first-stop"),
        explicit_parent_order_ids=(first.parent_trigger_order_id,),
    )

    result = assign_trigger_protection_orders(
        owners=(first, second),
        candidates=(candidate,),
        existing_order_owners={},
        snapshot_complete=True,
    )

    assert result.assignments == {first.leg_id: candidate.order_id}


def test_lineage_rejects_same_instant_encoded_at_different_exchange_precision():
    owner = replace(
        _lineage_owner(433, "first-pos"),
        child_exchange_created_at_raw="1784512860000",
    )
    candidate = replace(
        _candidate("first-stop"),
        created_at_raw="1784512860",
    )

    result = assign_trigger_protection_orders(
        owners=(owner,), candidates=(candidate,), existing_order_owners={}, snapshot_complete=True
    )

    assert result.assignments == {}
    assert result.exclusions[candidate.order_id] == "candidate_child_time_mismatch"
