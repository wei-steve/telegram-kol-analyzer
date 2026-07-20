import json
import hashlib
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_protection_ledger_repair import (
    apply_entry_protection_ledger_repair_plan,
    build_entry_protection_ledger_repair_plan,
    plan_trigger_protection_intent_adoption,
    plan_verified_trigger_entry_protection_adoption,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    TriggerProtectionIntent,
)


class FakeDeepcoinClient:
    def __init__(self, rows):
        self.rows = rows

    def list_trigger_orders_pending(self, *, inst_id):
        return [
            row for row in self.rows if str(row.get("instId") or "").upper() == inst_id
        ]


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

    assert plan.refusals == ()
    assert [(row.order_id, row.purpose, row.trigger_price) for row in plan.actions] == [
        ("1001124189941227", "take_profit", "1955"),
        ("1001124189941228", "stop_loss", "1788"),
    ]
    with session_factory() as session:
        assert session.query(PositionProtectionLedger).count() == 0

    result = apply_entry_protection_ledger_repair_plan(
        session_factory,
        plan,
        expected_fingerprint=plan.fingerprint,
    )

    assert result.applied == 2
    with session_factory() as session:
        rows = (
            session.query(PositionProtectionLedger)
            .order_by(PositionProtectionLedger.order_id.asc())
            .all()
        )
    assert [(row.order_id, row.purpose, row.pos_id) for row in rows] == [
        ("1001124189941227", "take_profit", "1001124189941220"),
        ("1001124189941228", "stop_loss", "1001124189941220"),
    ]
    assert {row.evidence_source for row in rows} == {
        "entry_protection_event_repair"
    }
    assert {json.loads(row.evidence_json)["match"] for row in rows} == {
        "response_anchored_order",
        "response_anchored_sibling_tpsl",
    }


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
    assert plan.refusals[0].reason == "sibling_tpsl_not_unique"


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
    apply_entry_protection_ledger_repair_plan(
        session_factory,
        initial_plan,
        expected_fingerprint=initial_plan.fingerprint,
    )

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
    result = _plan_intent_adoption(tmp_path, baseline='[{"ord_id":"tpsl-new"}]')

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


def _plan_intent_adoption(
    tmp_path, *, baseline="[]", pending_rows=None, history_rows=None, pending_update=None,
    existing_ledger_rows=None, history_time_range_start=None, history_time_range_end=None,
    planner_session=None, existing_intents=None, request_update=None,
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
        payload["tpTriggerPx"] = request.get("tpTriggerPx")
        payload["slTriggerPx"] = request.get("slTriggerPx")
        intent = TriggerProtectionIntent(
            venue="deepcoin", execution_binding_id=152, execution_order_leg_id=289,
            request_fingerprint=hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            pre_submit_tpsl_baseline_json=baseline, correlation_id="intent-289", parent_trigger_order_id="entry-1",
        )
        return plan_trigger_protection_intent_adoption(
            session if planner_session is None else planner_session,
            entry_leg=leg, intent=intent, parent_event=event,
            pending_tpsl_rows=[row] if pending_rows is None else pending_rows,
            history_tpsl_rows=history_rows or [], existing_ledger_rows=existing_ledger_rows or [],
            existing_intents=[intent] if existing_intents is None else [intent, *existing_intents],
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
):
    with session_factory() as session:
        binding = ExecutionBinding(
            id=binding_id,
            strategy_instance_id="deepcoin:-1003825498321:1844:ETH:long",
            kol_id="group:-1003825498321",
            chat_id=-1003825498321,
            message_id=1844,
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
