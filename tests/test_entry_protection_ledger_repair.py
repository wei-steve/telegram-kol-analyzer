import json
import hashlib
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_protection_ledger_repair import (
    EntryProtectionLedgerRepairAction,
    EntryProtectionLedgerRepairPlan,
    TriggerProtectionOwnerState,
    apply_entry_protection_ledger_repair_plan,
    build_entry_protection_ledger_repair_plan,
    plan_trigger_protection_intent_adoption,
    plan_verified_trigger_entry_protection_adoption,
    upsert_entry_protection_ledger_action,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    PositionProtectionLeg,
    PositionProtectionRevision,
    TriggerProtectionIntent,
)
from telegram_kol_research.position_protection_legs import create_or_get_protection_leg


class FakeDeepcoinClient:
    def __init__(self, rows):
        self.rows = rows
        self.pending_calls = []

    def list_trigger_orders_pending(self, *, inst_id):
        self.pending_calls.append(inst_id)
        return [
            row
            for row in self.rows
            if str(row.get("instId") or "").upper() == inst_id
        ]


def test_entry_repair_apply_accepts_only_one_response_anchored_action(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    leg_ids = []
    _seed_entry_protection_event(
        session_factory,
        binding_id=149,
        leg_id_holder=leg_ids,
        pos_id="1001124189941220",
        returned_order_id="1001124189941227",
    )
    action = EntryProtectionLedgerRepairAction(
        event_id=1,
        binding_id=149,
        leg_id=leg_ids[0],
        strategy_instance_id="deepcoin:1",
        pos_id="1001124189941220",
        instrument_id="ETH-USDT-SWAP",
        side="long",
        order_id="1001124189941227",
        purpose="take_profit",
        trigger_price="1955",
        size_text="4.4",
        evidence={"match": "response_anchored_order"},
    )
    plan = EntryProtectionLedgerRepairPlan(
        created_at=datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
        actions=(action,),
        refusals=(),
        fingerprint="f" * 64,
    )

    result = apply_entry_protection_ledger_repair_plan(
        session_factory,
        plan,
        action_id=action.action_id,
        pos_id=action.pos_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="entry-exact-confirm",
    )

    assert result.applied == 1


def test_entry_protection_repair_anchors_returned_order_and_sibling_tpsl(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_entry_protection_event(
        session_factory,
        binding_id=149,
        leg_id_holder=[],
        pos_id="1001124189941220",
        returned_order_id="1001124189941227",
    )
    client = FakeDeepcoinClient(
        [
            _pending_tpsl_row(
                "1001124189941227",
                purpose="take_profit",
                price="1955",
                ctime="2026-07-18T07:47:43Z",
            ),
            _pending_tpsl_row(
                "1001124189941228",
                purpose="stop_loss",
                price="1788",
                ctime="2026-07-18T07:47:43Z",
            ),
        ]
    )

    plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
    )

    assert plan.actions == ()
    assert plan.refusals[0].reason == (
        "response_order_id_missing_for_purpose"
    )
    assert plan.refusals[0].evidence == {
        "missing_purposes": ["stop_loss"],
        "anchor_order_ids": ["1001124189941227"],
    }
    with session_factory() as session:
        assert session.query(PositionProtectionLedger).count() == 0


def test_entry_protection_repair_refuses_when_returned_order_missing(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_entry_protection_event(
        session_factory,
        binding_id=149,
        leg_id_holder=[],
        pos_id="1001124189941220",
        returned_order_id="1001124189941227",
    )
    client = FakeDeepcoinClient(
        [
            _pending_tpsl_row(
                "1001124189941228",
                purpose="stop_loss",
                price="1788",
                ctime="2026-07-18T07:47:43Z",
            )
        ]
    )

    plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
    )

    assert plan.actions == ()
    assert plan.refusals[0].reason == "returned_order_not_pending"


def test_entry_protection_repair_reads_pending_orders_once_per_instrument(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_entry_protection_event(
        session_factory,
        binding_id=149,
        leg_id_holder=[],
        pos_id="pos-1",
        returned_order_id="tpsl-1",
    )
    _seed_entry_protection_event(
        session_factory,
        binding_id=150,
        leg_id_holder=[],
        pos_id="pos-2",
        returned_order_id="tpsl-2",
        message_id=1845,
    )
    client = FakeDeepcoinClient([])

    build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
    )

    assert client.pending_calls == ["ETH-USDT-SWAP"]


def test_entry_protection_repair_refuses_ambiguous_sibling(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_entry_protection_event(
        session_factory,
        binding_id=149,
        leg_id_holder=[],
        pos_id="1001124189941220",
        returned_order_id="1001124189941227",
    )
    client = FakeDeepcoinClient(
        [
            _pending_tpsl_row(
                "1001124189941227",
                purpose="take_profit",
                price="1955",
                ctime="2026-07-18T07:47:43Z",
            ),
            _pending_tpsl_row(
                "1001124189941228",
                purpose="stop_loss",
                price="1788",
                ctime="2026-07-18T07:47:43Z",
            ),
            _pending_tpsl_row(
                "1001124189941229",
                purpose="stop_loss",
                price="1788",
                ctime="2026-07-18T07:47:44Z",
            ),
        ]
    )

    plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
    )

    assert plan.actions == ()
    assert plan.refusals[0].reason == (
        "response_order_id_missing_for_purpose"
    )


def test_entry_protection_repair_refuses_unverified_entry_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_entry_protection_event(
        session_factory,
        binding_id=149,
        leg_id_holder=[],
        pos_id="1001124189941220",
        returned_order_id="1001124189941227",
        attribution_status="unassigned",
    )
    client = FakeDeepcoinClient(
        [
            _pending_tpsl_row(
                "1001124189941227",
                purpose="take_profit",
                price="1955",
                ctime="2026-07-18T07:47:43Z",
            ),
            _pending_tpsl_row(
                "1001124189941228",
                purpose="stop_loss",
                price="1788",
                ctime="2026-07-18T07:47:43Z",
            ),
        ]
    )

    plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 18, 8, 0, tzinfo=UTC),
    )

    assert plan.actions == ()
    assert plan.refusals[0].reason == "verified_entry_leg_missing"


def test_trigger_entry_repair_matches_unique_expected_protection_shape(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_entry_fill(
        session_factory,
        binding_id=152,
        legs=[
            {
                "leg_id": 289,
                "leg_index": 1,
                "order_id": "1001124198560580",
                "client_order_id": "TKSQ3347E1",
                "pos_id": "1001124219349221",
                "size": "4.4",
                "entry_price": "1883.0",
            },
            {
                "leg_id": 290,
                "leg_index": 2,
                "order_id": "1001124198560598",
                "client_order_id": "TKSQ3347E2",
                "pos_id": "1001124219426042",
                "size": "6.2",
                "entry_price": "1888.0",
            },
        ],
    )
    client = FakeDeepcoinClient(
        [
            _pending_tpsl_row(
                "1001124219349220",
                purpose="combined",
                price="1860",
                stop_price="1900",
                ctime="2026-07-20T00:11:13Z",
                inst_id="ETH-USDT-SWAP",
                side="short",
                size="4.4",
            ),
            _pending_tpsl_row(
                "1001124219426041",
                purpose="combined",
                price="1860",
                stop_price="1900",
                ctime="2026-07-20T00:13:14Z",
                inst_id="ETH-USDT-SWAP",
                side="short",
                size="6.2",
            ),
        ]
    )

    plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 20, 2, 0, tzinfo=UTC),
        binding_id=152,
        include_trigger_entries=True,
    )

    assert plan.refusals == ()
    assert [
        (row.leg_id, row.order_id, row.pos_id, row.purpose, row.trigger_price, row.size_text)
        for row in plan.actions
    ] == [
        (289, "1001124219349220", "1001124219349221", "combined", None, "4.4"),
        (290, "1001124219426041", "1001124219426042", "combined", None, "6.2"),
    ]
    assert {row.evidence["match"] for row in plan.actions} == {
        "trigger_entry_unique_expected_protection_shape"
    }
    assert all("exchange_order_created_at" not in row.evidence for row in plan.actions)


def test_failed_trigger_intent_repair_uses_verified_filled_leg_ownership(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    filled_leg_id, sibling_leg_id = _seed_failed_trigger_intent_repair(session_factory)
    client = FakeDeepcoinClient([_anonymous_stop_child("stop-child-1")])

    plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 8, 2, 1, 0, tzinfo=UTC),
        binding_id=152,
        pos_id="pos-1",
        include_trigger_entries=True,
    )

    assert len(plan.actions) == 1
    action = plan.actions[0]
    assert action.leg_id == filled_leg_id
    assert action.pos_id == "pos-1"
    assert action.order_id == "stop-child-1"
    assert action.evidence["match"] == "verified_filled_leg_unique_child"
    assert action.evidence["sibling_states"] == [
        {
            "execution_order_leg_id": sibling_leg_id,
            "status": "pending",
            "attribution_status": "unassigned",
            "has_pos_id": False,
        }
    ]


def test_failed_trigger_intent_repair_apply_finalizes_all_identity_records(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    filled_leg_id, _ = _seed_failed_trigger_intent_repair(session_factory)
    plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=FakeDeepcoinClient([_anonymous_stop_child("stop-child-1")]),
        binding_id=152,
        pos_id="pos-1",
        include_trigger_entries=True,
    )
    action = plan.actions[0]

    result = apply_entry_protection_ledger_repair_plan(
        session_factory,
        plan,
        action_id=action.action_id,
        pos_id="pos-1",
        expected_fingerprint=plan.fingerprint,
        confirmation_token="failed-trigger-confirm",
        seen_at=datetime(2026, 8, 2, 1, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).filter_by(
            execution_order_leg_id=filled_leg_id
        ).one()
        primary = session.query(PositionProtectionLeg).filter_by(
            execution_order_leg_id=filled_leg_id,
            role="primary_stop",
            leg_index=1,
        ).one()
        ledger = session.query(PositionProtectionLedger).one()
        revision = session.query(PositionProtectionRevision).one()
    assert result.applied == 1
    assert (intent.recovery_state, intent.adopted_order_id) == (
        "adopted",
        "stop-child-1",
    )
    assert (primary.pos_id, primary.exchange_order_id, primary.status) == (
        "pos-1",
        "stop-child-1",
        "verified",
    )
    assert (ledger.pos_id, ledger.order_id, ledger.execution_order_leg_id) == (
        "pos-1",
        "stop-child-1",
        filled_leg_id,
    )
    assert revision.status == "active"


def test_failed_trigger_intent_repair_sibling_change_invalidates_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _, sibling_leg_id = _seed_failed_trigger_intent_repair(session_factory)
    client = FakeDeepcoinClient([_anonymous_stop_child("stop-child-1")])
    reviewed = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        binding_id=152,
        pos_id="pos-1",
        include_trigger_entries=True,
    )
    with session_factory() as session:
        sibling = session.get(ExecutionOrderLeg, sibling_leg_id)
        sibling.status = "active"
        sibling.attribution_status = "verified"
        sibling.pos_id = "pos-2"
        session.commit()
    current = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        binding_id=152,
        pos_id="pos-1",
        include_trigger_entries=True,
    )

    assert current.fingerprint != reviewed.fingerprint
    with pytest.raises(ValueError, match="fingerprint mismatch"):
        apply_entry_protection_ledger_repair_plan(
            session_factory,
            current,
            action_id=reviewed.actions[0].action_id,
            pos_id="pos-1",
            expected_fingerprint=reviewed.fingerprint,
            confirmation_token="stale-trigger-confirm",
        )


def test_entry_protection_repair_skips_already_repaired_trigger_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_entry_fill(
        session_factory,
        binding_id=152,
        legs=[
            {
                "leg_id": 289,
                "leg_index": 1,
                "order_id": "1001124198560580",
                "client_order_id": "TKSQ3347E1",
                "pos_id": "1001124219349221",
                "size": "4.4",
                "entry_price": "1883.0",
            }
        ],
    )
    client = FakeDeepcoinClient(
        [
            _pending_tpsl_row(
                "1001124219349220",
                purpose="combined",
                price="1860",
                stop_price="1900",
                ctime="2026-07-20T00:11:13Z",
                inst_id="ETH-USDT-SWAP",
                side="short",
                size="4.4",
            )
        ]
    )
    initial_plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 20, 2, 0, tzinfo=UTC),
        binding_id=152,
        include_trigger_entries=True,
    )
    with session_factory() as session:
        upsert_entry_protection_ledger_action(
            session,
            initial_plan.actions[0],
            evidence_source="test_existing_trigger_protection",
            seen_at=datetime(2026, 7, 20, 2, 0, tzinfo=UTC),
        )
        session.commit()

    followup_plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 20, 2, 1, tzinfo=UTC),
        binding_id=152,
        include_trigger_entries=True,
    )

    assert followup_plan.actions == ()
    assert followup_plan.refusals == ()


def test_adoption_plans_one_exact_trigger_entry_protection_without_session_write(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_entry_fill(
        session_factory,
        binding_id=152,
        legs=[
            {
                "leg_id": 289,
                "leg_index": 1,
                "order_id": "entry-1",
                "client_order_id": "entry-client-1",
                "pos_id": "pos-1",
                "size": "4.4",
                "entry_price": "1883.0",
            }
        ],
    )
    pending_rows = [
        _pending_tpsl_row(
            "tpsl-1",
            purpose="combined",
            price="1860",
            stop_price="1900",
            ctime="2026-07-20T00:11:13Z",
            inst_id="ETH-USDT-SWAP",
            side="short",
            size="4.4",
        )
    ]
    pending_rows[0]["posId"] = "pos-1"

    with session_factory() as session:
        entry_leg = session.get(ExecutionOrderLeg, 289)
        entry_event = session.query(ExecutionEvent).one()
        result = plan_verified_trigger_entry_protection_adoption(
            session,
            entry_leg=entry_leg,
            event=entry_event,
            pending_tpsl_rows=pending_rows,
            existing_order_ids=set(),
            existing_order_associations=set(),
        )
        assert result.action is not None
        assert result.action.pos_id == "pos-1"
        assert result.action.order_id == "tpsl-1"
        assert result.refusal is None
        assert session.query(PositionProtectionLedger).count() == 0


def test_intent_adoption_plans_one_new_exact_trigger_entry_protection(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_entry_fill(
        session_factory,
        binding_id=152,
        legs=[{
            "leg_id": 289, "leg_index": 1, "order_id": "entry-1",
            "client_order_id": "entry-client-1", "pos_id": "pos-1",
            "size": "4.4", "entry_price": "1883.0",
        }],
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, 289)
        event = session.query(ExecutionEvent).one()
        request = json.loads(leg.request_json)
        fingerprint_payload = dict(request)
        fingerprint_payload["tpTriggerPx"] = request.get("tpTriggerPx")
        fingerprint_payload["slTriggerPx"] = request.get("slTriggerPx")
        intent = TriggerProtectionIntent(
            venue="deepcoin", execution_binding_id=152, execution_order_leg_id=289,
            request_fingerprint=hashlib.sha256(json.dumps(
                fingerprint_payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest(),
            pre_submit_tpsl_baseline_json="[]", correlation_id="intent-289",
            parent_trigger_order_id="entry-1",
        )
        result = plan_trigger_protection_intent_adoption(
            session,
            entry_leg=leg,
            intent=intent,
            parent_event=event,
            pending_tpsl_rows=[{
                **_pending_tpsl_row(
                    "tpsl-new", purpose="combined", price="1860", stop_price="1900",
                    ctime="2026-07-20T00:11:13Z", inst_id="ETH-USDT-SWAP",
                    side="short", size="4.4",
                ),
                "posId": "pos-1",
            }],
            history_tpsl_rows=[],
            existing_ledger_rows=[],
            existing_intents=[intent],
        )

    assert result.action is not None
    assert result.action.order_id == "tpsl-new"
    assert result.deferred is None
    assert result.refusal is None


def test_intent_adoption_refuses_candidate_present_in_pre_submit_baseline(tmp_path):
    result = _plan_intent_adoption(tmp_path, baseline=json.dumps([{
        "ord_id": "tpsl-new", "instrument": "ETH-USDT-SWAP", "side": "short",
        "trigger_order_type": "TPSL", "size": "4.4",
        "take_profit_trigger_price": "1860", "stop_loss_trigger_price": "1900",
        "exchange_created_at": "2026-07-20T00:11:13Z", "exchange_updated_at": "2026-07-20T00:11:13Z",
    }], sort_keys=True, separators=(",", ":")))

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_candidate_in_baseline"


def test_intent_adoption_refuses_duplicate_candidates_across_pending_and_history(tmp_path):
    result = _plan_intent_adoption(
        tmp_path,
        history_rows=[{
            **_pending_tpsl_row(
                "tpsl-history", purpose="combined", price="1860", stop_price="1900",
                ctime="2026-07-20T00:11:13Z", inst_id="ETH-USDT-SWAP", side="short", size="4.4",
            ),
            "posId": "pos-1",
        }],
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_candidate_not_unique"


def test_intent_adoption_refuses_conflicting_returned_position_id(tmp_path):
    result = _plan_intent_adoption(tmp_path, pending_update={"posId": "other-pos"})

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_candidate_position_conflict"


def test_intent_adoption_requires_one_explicit_returned_position_id(tmp_path):
    result = _plan_intent_adoption(tmp_path, pending_update={"posId": ""})

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_candidate_position_invalid"


def test_intent_adoption_accepts_unique_stop_only_candidate_without_position_id(tmp_path):
    result = _plan_intent_adoption(
        tmp_path,
        request_update={"tpTriggerPx": None},
        pending_update={"posId": "", "tpTriggerPx": None, "slTriggerPx": "1900"},
    )

    assert result.refusal is None
    assert result.action is not None
    assert result.action.purpose == "stop_loss"
    assert result.action.order_id == "tpsl-new"
    assert result.action.evidence["match"] == (
        "verified_filled_leg_unique_child"
    )


def test_intent_adoption_refuses_conflicting_position_aliases_for_anonymous_stop(tmp_path):
    result = _plan_intent_adoption(
        tmp_path,
        request_update={"tpTriggerPx": None},
        pending_update={
            "posId": "other-pos",
            "closePosId": "pos-1",
            "tpTriggerPx": None,
            "slTriggerPx": "1900",
        },
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_candidate_position_invalid"


def test_intent_adoption_refuses_anonymous_stop_with_same_pending_intent_fingerprint(
    tmp_path,
):
    other_intent = TriggerProtectionIntent(
        id=777,
        venue="deepcoin",
        execution_binding_id=999,
        execution_order_leg_id=998,
        request_fingerprint="different-fingerprint",
        pre_submit_tpsl_baseline_json="[]",
        correlation_id="other",
        recovery_state="pending",
    )
    result = _plan_intent_adoption(
        tmp_path,
        request_update={"tpTriggerPx": None},
        pending_update={"posId": "", "tpTriggerPx": None, "slTriggerPx": "1900"},
        existing_intents=[other_intent],
        existing_intent_requests="same_as_current",
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_candidate_not_unique"


def test_intent_adoption_ignores_same_signature_unfilled_sibling(tmp_path):
    other_intent = TriggerProtectionIntent(
        id=777,
        venue="deepcoin",
        execution_binding_id=152,
        execution_order_leg_id=290,
        request_fingerprint="different-fingerprint",
        pre_submit_tpsl_baseline_json="[]",
        correlation_id="other",
        recovery_state="pending",
    )

    result = _plan_intent_adoption(
        tmp_path,
        request_update={
            "price": "1828",
            "sz": "0.6",
            "tpTriggerPx": None,
            "slTriggerPx": "1695",
        },
        pending_update={
            "posId": "",
            "sz": "0.6",
            "tpTriggerPx": None,
            "slTriggerPx": "1695",
        },
        existing_intents=[other_intent],
        existing_intent_requests="same_as_current",
        existing_intent_owner_states={
            777: TriggerProtectionOwnerState(
                execution_order_leg_id=290,
                status="pending",
                attribution_status="unassigned",
                pos_id=None,
                parent_order_id="entry-2",
            )
        },
    )

    assert result.refusal is None
    assert result.action is not None
    assert result.action.order_id == "tpsl-new"
    assert result.action.pos_id == "pos-1"


def test_intent_adoption_refuses_same_signature_filled_sibling(tmp_path):
    other_intent = TriggerProtectionIntent(
        id=777,
        venue="deepcoin",
        execution_binding_id=152,
        execution_order_leg_id=290,
        request_fingerprint="different-fingerprint",
        pre_submit_tpsl_baseline_json="[]",
        correlation_id="other",
        recovery_state="pending",
    )

    result = _plan_intent_adoption(
        tmp_path,
        request_update={"tpTriggerPx": None},
        pending_update={"posId": "", "tpTriggerPx": None, "slTriggerPx": "1900"},
        existing_intents=[other_intent],
        existing_intent_requests="same_as_current",
        existing_intent_owner_states={
            777: TriggerProtectionOwnerState(
                execution_order_leg_id=290,
                status="active",
                attribution_status="verified",
                pos_id="pos-2",
                parent_order_id="entry-2",
            )
        },
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_candidate_not_unique"


def test_intent_adoption_refuses_filled_sibling_even_when_its_recovery_failed(
    tmp_path,
):
    other_intent = TriggerProtectionIntent(
        id=777,
        venue="deepcoin",
        execution_binding_id=152,
        execution_order_leg_id=290,
        request_fingerprint="different-fingerprint",
        pre_submit_tpsl_baseline_json="[]",
        correlation_id="other",
        recovery_state="failed",
    )

    result = _plan_intent_adoption(
        tmp_path,
        request_update={"tpTriggerPx": None},
        pending_update={"posId": "", "tpTriggerPx": None, "slTriggerPx": "1900"},
        existing_intents=[other_intent],
        existing_intent_requests="same_as_current",
        existing_intent_owner_states={
            777: TriggerProtectionOwnerState(
                execution_order_leg_id=290,
                status="active",
                attribution_status="verified",
                pos_id="pos-2",
                parent_order_id="entry-2",
            )
        },
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_candidate_not_unique"


def test_intent_adoption_requires_absent_unrequested_protection_side(tmp_path):
    result = _plan_intent_adoption(
        tmp_path,
        request_update={"slTriggerPx": None},
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_candidate_protection_conflict"


def test_intent_adoption_refuses_same_leg_ledger_without_exact_association(tmp_path):
    ledger = PositionProtectionLedger(
        venue="deepcoin", execution_binding_id=152, execution_order_leg_id=289,
        pos_id="other-pos", instrument_id="ETH-USDT-SWAP", side="short",
        order_id="tpsl-new", purpose="combined", status="verified", evidence_source="test",
    )
    result = _plan_intent_adoption(tmp_path, existing_ledger_rows=[ledger])

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_order_owned"


def test_intent_adoption_checks_other_intent_collision_before_exact_ledger_defer(tmp_path):
    ledger = PositionProtectionLedger(
        venue="deepcoin", execution_binding_id=152, execution_order_leg_id=289,
        pos_id="pos-1", instrument_id="ETH-USDT-SWAP", side="short",
        order_id="tpsl-new", purpose="combined", status="verified", evidence_source="test",
    )
    other_intent = TriggerProtectionIntent(
        id=777, venue="deepcoin", execution_binding_id=999, execution_order_leg_id=998,
        request_fingerprint="a" * 64, pre_submit_tpsl_baseline_json="[]",
        correlation_id="other", adopted_order_id="tpsl-new",
    )
    result = _plan_intent_adoption(
        tmp_path, adopted_order_id="tpsl-new", existing_ledger_rows=[ledger],
        existing_intents=[other_intent],
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_order_owned"


def test_intent_adoption_refuses_candidate_different_from_immutable_adopted_order(tmp_path):
    result = _plan_intent_adoption(tmp_path, adopted_order_id="already-adopted")

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_adopted_order_conflict"


def test_intent_adoption_defers_when_current_intent_already_adopted_without_ledger(tmp_path):
    result = _plan_intent_adoption(tmp_path, adopted_order_id="tpsl-new")

    assert result.action is None
    assert result.deferred is not None
    assert result.deferred.reason == "trigger_protection_already_adopted"


def test_intent_adoption_normalizes_aware_history_range_to_utc_naive(tmp_path):
    history = _pending_tpsl_row(
        "tpsl-history", purpose="combined", price="1860", stop_price="1900",
        ctime="2026-07-20T00:11:13Z", inst_id="ETH-USDT-SWAP", side="short", size="4.4",
    )
    history.update({"posId": "pos-1", "parentOrdId": "entry-1"})
    result = _plan_intent_adoption(
        tmp_path, pending_rows=[], history_rows=[history],
        history_time_range_start=datetime(2026, 7, 20, 0, 11, tzinfo=UTC),
        history_time_range_end=datetime(2026, 7, 20, 0, 12, tzinfo=UTC),
    )

    assert result.action is not None


def test_intent_adoption_refuses_invalid_history_range_without_raising(tmp_path):
    history = _pending_tpsl_row(
        "tpsl-history", purpose="combined", price="1860", stop_price="1900",
        ctime="2026-07-20T00:11:13Z", inst_id="ETH-USDT-SWAP", side="short", size="4.4",
    )
    history.update({"posId": "pos-1", "parentOrdId": "entry-1"})
    result = _plan_intent_adoption(
        tmp_path, pending_rows=[], history_rows=[history],
        history_time_range_start="invalid", history_time_range_end=datetime(2026, 7, 20, 0, 12),
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_history_unproven"


@pytest.mark.parametrize("baseline", ["[123]", "{}", '{"orders":["bad"]}'])
def test_intent_adoption_refuses_malformed_baseline_schema(tmp_path, baseline):
    result = _plan_intent_adoption(tmp_path, baseline=baseline)

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_baseline_invalid"


def test_intent_adoption_fingerprint_ignores_persisted_internal_metadata(tmp_path):
    result = _plan_intent_adoption(
        tmp_path,
        request_update={"merged_from_leg_indices": [0, 1]},
        fingerprint_without_internal_metadata=True,
    )

    assert result.action is not None


def test_intent_adoption_dedupes_same_order_from_pending_and_history(tmp_path):
    history = _pending_tpsl_row(
        "tpsl-new", purpose="combined", price="1860", stop_price="1900",
        ctime="2026-07-20T00:11:13Z", inst_id="ETH-USDT-SWAP", side="short", size="4.4",
    )
    history["posId"] = "pos-1"
    result = _plan_intent_adoption(tmp_path, history_rows=[history])

    assert result.action is not None
    assert result.action.evidence["source"] == "pending"


def test_intent_adoption_accepts_history_only_with_parent_proof_and_explicit_range(tmp_path):
    row = _pending_tpsl_row(
        "tpsl-history", purpose="combined", price="1860", stop_price="1900",
        ctime="2026-07-20T00:11:13Z", inst_id="ETH-USDT-SWAP", side="short", size="4.4",
    )
    row["parentOrdId"] = "entry-1"
    row["posId"] = "pos-1"
    result = _plan_intent_adoption(
        tmp_path, pending_rows=[], history_rows=[row],
        history_time_range_start=datetime(2026, 7, 20, 0, 11),
        history_time_range_end=datetime(2026, 7, 20, 0, 12),
    )

    assert result.action is not None
    assert result.action.order_id == "tpsl-history"


def test_intent_adoption_refuses_history_only_without_explicit_proof_or_range(tmp_path):
    result = _plan_intent_adoption(
        tmp_path, pending_rows=[], history_rows=[{
            **_pending_tpsl_row(
                "tpsl-history", purpose="combined", price="1860", stop_price="1900",
                ctime="2026-07-20T00:11:13Z", inst_id="ETH-USDT-SWAP", side="short", size="4.4",
            ),
            "posId": "pos-1",
        }],
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_history_unproven"


def test_intent_adoption_refuses_existing_ledger_or_other_intent_ownership(tmp_path):
    ledger = PositionProtectionLedger(
        venue="deepcoin", execution_binding_id=999, execution_order_leg_id=998,
        pos_id="other-pos", instrument_id="ETH-USDT-SWAP", side="short",
        order_id="tpsl-new", purpose="combined", status="verified", evidence_source="test",
    )
    result = _plan_intent_adoption(tmp_path, existing_ledger_rows=[ledger])

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_order_owned"

    other_intent = TriggerProtectionIntent(
        id=777, venue="deepcoin", execution_binding_id=999, execution_order_leg_id=998,
        request_fingerprint="a" * 64, pre_submit_tpsl_baseline_json="[]",
        correlation_id="other", adopted_order_id="tpsl-new",
    )
    other_tmp_path = tmp_path / "other"
    other_tmp_path.mkdir()
    result = _plan_intent_adoption(other_tmp_path, existing_intents=[other_intent])

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_order_owned"


def test_intent_adoption_planner_cannot_access_session_for_client_or_writes(tmp_path):
    class NoSessionAccess:
        def __getattr__(self, name):
            raise AssertionError(f"planner accessed session.{name}")

    result = _plan_intent_adoption(tmp_path, planner_session=NoSessionAccess())

    assert result.action is not None


def test_intent_adoption_refuses_one_sided_attached_protection(tmp_path):
    result = _plan_intent_adoption(
        tmp_path,
        request_update={"slTriggerPx": "0"},
        pending_update={"slTriggerPx": "0"},
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_protection_candidate_protection_conflict"


def _plan_intent_adoption(
    tmp_path, *, baseline="[]", pending_rows=None, history_rows=None, pending_update=None,
    existing_ledger_rows=None, history_time_range_start=None, history_time_range_end=None,
    planner_session=None, existing_intents=None, request_update=None, adopted_order_id=None,
    fingerprint_without_internal_metadata=False, match_existing_intent_fingerprint=False,
    existing_intent_requests=None, existing_intent_owner_states=None,
):
    session_factory = create_session_factory(tmp_path / "intent-adoption.db")
    _seed_trigger_entry_fill(session_factory, binding_id=152, legs=[{
        "leg_id": 289, "leg_index": 1, "order_id": "entry-1",
        "client_order_id": "entry-client-1", "pos_id": "pos-1", "size": "4.4", "entry_price": "1883.0",
    }])
    row = _pending_tpsl_row("tpsl-new", purpose="combined", price="1860", stop_price="1900", ctime="2026-07-20T00:11:13Z", inst_id="ETH-USDT-SWAP", side="short", size="4.4")
    row["posId"] = "pos-1"
    row.update(pending_update or {})
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, 289)
        event = session.query(ExecutionEvent).one()
        request = json.loads(leg.request_json)
        request.update(request_update or {})
        leg.request_json = json.dumps(request)
        event.request_json = json.dumps(request)
        payload = dict(request)
        if fingerprint_without_internal_metadata:
            payload.pop("merged_from_leg_indices", None)
        payload["tpTriggerPx"] = request.get("tpTriggerPx")
        payload["slTriggerPx"] = request.get("slTriggerPx")
        intent = TriggerProtectionIntent(
            venue="deepcoin", execution_binding_id=152, execution_order_leg_id=289,
            request_fingerprint=hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            pre_submit_tpsl_baseline_json=baseline, correlation_id="intent-289", parent_trigger_order_id="entry-1",
            adopted_order_id=adopted_order_id,
        )
        if match_existing_intent_fingerprint:
            for other_intent in existing_intents or []:
                other_intent.request_fingerprint = intent.request_fingerprint
        if existing_intent_requests == "same_as_current":
            existing_intent_requests = {
                int(other_intent.id): dict(request)
                for other_intent in existing_intents or []
            }
        return plan_trigger_protection_intent_adoption(
            session if planner_session is None else planner_session,
            entry_leg=leg, intent=intent, parent_event=event,
            pending_tpsl_rows=[row] if pending_rows is None else pending_rows,
            history_tpsl_rows=history_rows or [], existing_ledger_rows=existing_ledger_rows or [],
            existing_intents=[intent] if existing_intents is None else [intent, *existing_intents],
            existing_intent_requests=existing_intent_requests,
            existing_intent_owner_states=existing_intent_owner_states,
            history_time_range_start=history_time_range_start,
            history_time_range_end=history_time_range_end,
        )


def test_adoption_refuses_pending_row_without_order_id(tmp_path):
    result = _plan_trigger_entry_adoption(tmp_path, pending_row={"ordId": ""})

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_entry_tpsl_missing"


def test_adoption_refuses_pending_row_with_contradictory_position_id(tmp_path):
    result = _plan_trigger_entry_adoption(tmp_path, pending_row={"posId": "other-pos"})

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_entry_tpsl_missing"


def test_adoption_refuses_generic_trigger_price_when_tp_and_sl_prices_are_equal(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_entry_fill(
        session_factory,
        binding_id=152,
        legs=[
            {
                "leg_id": 289,
                "leg_index": 1,
                "order_id": "entry-1",
                "client_order_id": "entry-client-1",
                "pos_id": "pos-1",
                "size": "4.4",
                "entry_price": "1883.0",
            }
        ],
    )
    pending_row = _pending_tpsl_row(
        "tpsl-1",
        purpose="combined",
        price="1860",
        stop_price="1860",
        ctime="2026-07-20T00:11:13Z",
        inst_id="ETH-USDT-SWAP",
        side="short",
        size="4.4",
    )
    pending_row.pop("tpTriggerPx")
    pending_row.pop("slTriggerPx")
    pending_row["triggerPx"] = "1860"

    with session_factory() as session:
        entry_leg = session.get(ExecutionOrderLeg, 289)
        entry_event = session.query(ExecutionEvent).one()
        request = json.loads(entry_event.request_json)
        request["tpTriggerPx"] = 1860.0
        request["slTriggerPx"] = 1860.0
        entry_event.request_json = json.dumps(request)
        result = plan_verified_trigger_entry_protection_adoption(
            session,
            entry_leg=entry_leg,
            event=entry_event,
            pending_tpsl_rows=[pending_row],
            existing_order_ids=set(),
            existing_order_associations=set(),
        )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_entry_tpsl_missing"


def test_adoption_refuses_partial_size_pending_row(tmp_path):
    result = _plan_trigger_entry_adoption(tmp_path, pending_row={"sz": "2.2"})

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_entry_tpsl_missing"


def test_adoption_is_noop_for_already_verified_ledger_order(tmp_path):
    result = _plan_trigger_entry_adoption(
        tmp_path,
        pending_row={},
        existing_order_ids={"tpsl-1"},
        existing_order_associations={
            ("tpsl-1", "deepcoin", 152, 289, "pos-1", "verified")
        },
    )

    assert result.action is None
    assert result.refusal is None


def test_trigger_entry_repair_is_noop_when_leg_already_has_verified_protection(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_entry_fill(
        session_factory,
        binding_id=152,
        legs=[
            {
                "leg_id": 289,
                "leg_index": 1,
                "order_id": "entry-1",
                "client_order_id": "entry-client-1",
                "pos_id": "pos-1",
                "size": "4.4",
                "entry_price": "1883.0",
            }
        ],
    )
    with session_factory() as session:
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=152,
                execution_order_leg_id=289,
                strategy_instance_id="deepcoin:-1002370796392:3347:ETH:short",
                pos_id="pos-1",
                instrument_id="ETH-USDT-SWAP",
                side="short",
                order_id="previous-tpsl-id",
                purpose="combined",
                status="verified",
                evidence_source="entry_protection_event_repair",
            )
        )
        session.commit()

    plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=FakeDeepcoinClient(
            [
                _pending_tpsl_row(
                    "new-candidate-tpsl-id",
                    purpose="combined",
                    price="1860",
                    stop_price="1900",
                    ctime="2026-07-20T00:11:13Z",
                    inst_id="ETH-USDT-SWAP",
                    side="short",
                    size="4.4",
                )
            ]
        ),
        include_trigger_entries=True,
    )

    assert plan.actions == ()
    assert plan.refusals == ()


def test_adoption_refuses_when_any_populated_position_alias_contradicts_verified_leg(tmp_path):
    result = _plan_trigger_entry_adoption(
        tmp_path,
        pending_row={"closePosId": "pos-1", "positionId": "other-pos"},
    )

    assert result.action is None
    assert result.refusal is not None
    assert result.refusal.reason == "trigger_entry_tpsl_missing"


def test_trigger_entry_repair_refuses_duplicate_expected_protection_shape(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_entry_fill(
        session_factory,
        binding_id=152,
        legs=[
            {
                "leg_id": 289,
                "leg_index": 1,
                "order_id": "1001124198560580",
                "client_order_id": "TKSQ3347E1",
                "pos_id": "1001124219349221",
                "size": "4.4",
                "entry_price": "1883.0",
            }
        ],
    )
    client = FakeDeepcoinClient(
        [
            _pending_tpsl_row(
                "tpsl-a",
                purpose="combined",
                price="1860",
                stop_price="1900",
                ctime="2026-07-20T00:11:13Z",
                inst_id="ETH-USDT-SWAP",
                side="short",
                size="4.4",
            ),
            _pending_tpsl_row(
                "tpsl-b",
                purpose="combined",
                price="1860",
                stop_price="1900",
                ctime="2026-07-20T00:11:13Z",
                inst_id="ETH-USDT-SWAP",
                side="short",
                size="4.4",
            ),
        ]
    )

    plan = build_entry_protection_ledger_repair_plan(
        session_factory,
        deepcoin_client=client,
        now=datetime(2026, 7, 20, 2, 0, tzinfo=UTC),
        binding_id=152,
        include_trigger_entries=True,
    )

    assert plan.actions == ()
    assert plan.refusals[0].reason == "trigger_entry_tpsl_not_unique"


def _seed_entry_protection_event(
    session_factory,
    *,
    binding_id,
    leg_id_holder,
    pos_id,
    returned_order_id,
    attribution_status="verified",
    message_id=1844,
):
    with session_factory() as session:
        binding = ExecutionBinding(
            id=binding_id,
            strategy_instance_id="deepcoin:-1003825498321:1844:ETH:long",
            kol_id="group:-1003825498321",
            chat_id=-1003825498321,
            message_id=message_id,
            symbol="ETH",
            side="long",
            venue="deepcoin",
            pos_id=pos_id,
            status="active",
        )
        leg = ExecutionOrderLeg(
            execution_binding_id=binding_id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="1001124189941220",
            pos_id=pos_id,
            venue="deepcoin",
            attribution_status=attribution_status,
            status="active",
        )
        event = ExecutionEvent(
            execution_binding_id=binding_id,
            strategy_instance_id=binding.strategy_instance_id,
            venue="deepcoin",
            action="set_position_tpsl",
            status="submitted",
            symbol="ETH",
            side="long",
            pos_id=pos_id,
            reason="entry_protection",
            request_json=json.dumps(
                {
                    "instId": "ETH-USDT-SWAP",
                    "posSide": "long",
                    "posId": pos_id,
                    "tpTriggerPx": "1955",
                    "slTriggerPx": "1788",
                    "sz": "0",
                }
            ),
            response_json=json.dumps({"data": [{"ordId": returned_order_id}]}),
            created_at=datetime(2026, 7, 18, 7, 47, 38),
        )
        session.add_all([binding, leg, event])
        session.commit()
        leg_id_holder.append(leg.id)


def _seed_trigger_entry_fill(session_factory, *, binding_id, legs):
    with session_factory() as session:
        binding = ExecutionBinding(
            id=binding_id,
            strategy_instance_id="deepcoin:-1002370796392:3347:ETH:short",
            kol_id="group:-1002370796392",
            chat_id=-1002370796392,
            message_id=3347,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            pos_id=",".join(str(leg["pos_id"]) for leg in legs),
            status="active",
        )
        session.add(binding)
        for leg in legs:
            request = {
                "clOrdId": leg["client_order_id"],
                "instId": "ETH-USDT-SWAP",
                "orderType": "limit",
                "posSide": "short",
                "price": leg["entry_price"],
                "side": "sell",
                "slOrdPx": -1,
                "slTriggerPx": 1900.0,
                "sz": leg["size"],
                "tdMode": "cross",
                "tpOrdPx": -1,
                "tpTriggerPx": 1860.0,
                "triggerPrice": leg["entry_price"],
            }
            session.add(
                ExecutionOrderLeg(
                    id=leg["leg_id"],
                    execution_binding_id=binding_id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=leg["leg_index"],
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id=leg["order_id"],
                    client_order_id=leg["client_order_id"],
                    pos_id=leg["pos_id"],
                    venue="deepcoin",
                    attribution_status="verified",
                    status="active",
                    request_json=json.dumps(request),
                )
            )
            session.add(
                ExecutionEvent(
                    execution_binding_id=binding_id,
                    strategy_instance_id=binding.strategy_instance_id,
                    venue="deepcoin",
                    action="create_trigger_entry",
                    status="submitted",
                    symbol="ETH",
                    side="short",
                    order_id=leg["order_id"],
                    client_order_id=leg["client_order_id"],
                    reason="live_signal_auto_trade",
                    request_json=json.dumps(request),
                    response_json=json.dumps({"data": {"ordId": leg["order_id"]}}),
                    after_json=json.dumps({"stop_loss": 1900.0, "take_profit": 1860.0}),
                    created_at=datetime(2026, 7, 18, 20, 16, 21),
                )
            )
        session.commit()


def _seed_failed_trigger_intent_repair(session_factory):
    filled_leg_id = 289
    sibling_leg_id = 290
    _seed_trigger_entry_fill(
        session_factory,
        binding_id=152,
        legs=[
            {
                "leg_id": filled_leg_id,
                "leg_index": 1,
                "order_id": "entry-filled",
                "client_order_id": "entry-client-filled",
                "pos_id": "pos-1",
                "size": "0.6",
                "entry_price": "1828",
            },
            {
                "leg_id": sibling_leg_id,
                "leg_index": 2,
                "order_id": "entry-pending",
                "client_order_id": "entry-client-pending",
                "pos_id": "temporary-pos",
                "size": "0.6",
                "entry_price": "1808",
            },
        ],
    )
    with session_factory() as session:
        legs = {
            int(row.id): row
            for row in session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.id).all()
        }
        events = {
            str(row.order_id): row
            for row in session.query(ExecutionEvent).order_by(ExecutionEvent.id).all()
        }
        for leg in legs.values():
            request = json.loads(leg.request_json)
            request["tpTriggerPx"] = None
            request["slTriggerPx"] = "1695"
            leg.request_json = json.dumps(request)
            events[str(leg.order_id)].request_json = json.dumps(request)
            fingerprint = hashlib.sha256(
                json.dumps(
                    request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            session.add(
                TriggerProtectionIntent(
                    venue="deepcoin",
                    execution_binding_id=152,
                    execution_order_leg_id=int(leg.id),
                    request_fingerprint=fingerprint,
                    pre_submit_tpsl_baseline_json="[]",
                    correlation_id=f"intent-{leg.id}",
                    parent_trigger_order_id=str(leg.order_id),
                    recovery_state=(
                        "failed" if int(leg.id) == filled_leg_id else "pending"
                    ),
                    retry_attempts=5 if int(leg.id) == filled_leg_id else 0,
                )
            )
            create_or_get_protection_leg(
                session,
                venue="deepcoin",
                execution_order_leg_id=int(leg.id),
                role="primary_stop",
                leg_index=1,
                planned_trigger_price="1695",
                planned_size="0.6",
            )
            create_or_get_protection_leg(
                session,
                venue="deepcoin",
                execution_order_leg_id=int(leg.id),
                role="backup_stop",
                leg_index=1,
                planned_trigger_price=None,
                planned_size=None,
            )
        sibling = legs[sibling_leg_id]
        sibling.status = "pending"
        sibling.attribution_status = "unassigned"
        sibling.pos_id = None
        session.commit()
    return filled_leg_id, sibling_leg_id


def _anonymous_stop_child(order_id):
    row = _pending_tpsl_row(
        order_id,
        purpose="stop_loss",
        price="1695",
        ctime="2026-08-02T00:05:10Z",
        inst_id="ETH-USDT-SWAP",
        side="short",
        size="0.6",
    )
    row["posId"] = ""
    return row


def _plan_trigger_entry_adoption(
    tmp_path,
    *,
    pending_row,
    existing_order_ids=None,
    existing_order_associations=None,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_entry_fill(
        session_factory,
        binding_id=152,
        legs=[
            {
                "leg_id": 289,
                "leg_index": 1,
                "order_id": "entry-1",
                "client_order_id": "entry-client-1",
                "pos_id": "pos-1",
                "size": "4.4",
                "entry_price": "1883.0",
            }
        ],
    )
    row = _pending_tpsl_row(
        "tpsl-1",
        purpose="combined",
        price="1860",
        stop_price="1900",
        ctime="2026-07-20T00:11:13Z",
        inst_id="ETH-USDT-SWAP",
        side="short",
        size="4.4",
    )
    row.update(pending_row)
    with session_factory() as session:
        return plan_verified_trigger_entry_protection_adoption(
            session,
            entry_leg=session.get(ExecutionOrderLeg, 289),
            event=session.query(ExecutionEvent).one(),
            pending_tpsl_rows=[row],
            existing_order_ids=existing_order_ids or set(),
            existing_order_associations=existing_order_associations or set(),
        )


def _pending_tpsl_row(
    order_id,
    *,
    purpose,
    price,
    ctime,
    stop_price=None,
    inst_id="ETH-USDT-SWAP",
    side="long",
    size="0",
):
    row = {
        "ordId": order_id,
        "instId": inst_id,
        "posSide": side,
        "triggerOrderType": "TPSL",
        "sz": size,
        "cTime": ctime,
        "uTime": ctime,
    }
    if purpose == "take_profit":
        row["tpTriggerPx"] = price
    elif purpose == "stop_loss":
        row["slTriggerPx"] = price
    else:
        row["tpTriggerPx"] = price
        row["slTriggerPx"] = stop_price
    return row
