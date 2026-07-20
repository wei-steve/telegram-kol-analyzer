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


def test_entry_protection_repair_matches_filled_trigger_entry_protection(tmp_path):
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
        "trigger_entry_unique_size_time_tpsl"
    }


def test_entry_protection_repair_refuses_ambiguous_trigger_entry_tpsl(tmp_path):
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
