import json
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_protection_ledger_repair import (
    apply_entry_protection_ledger_repair_plan,
    build_entry_protection_ledger_repair_plan,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLedger,
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


def _pending_tpsl_row(order_id, *, purpose, price, ctime):
    row = {
        "ordId": order_id,
        "instId": "ETH-USDT-SWAP",
        "posSide": "long",
        "triggerOrderType": "TPSL",
        "sz": "0",
        "cTime": ctime,
        "uTime": ctime,
    }
    if purpose == "take_profit":
        row["tpTriggerPx"] = price
    else:
        row["slTriggerPx"] = price
    return row
