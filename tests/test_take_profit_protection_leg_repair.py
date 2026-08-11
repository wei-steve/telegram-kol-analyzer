from __future__ import annotations

from datetime import UTC, datetime

import pytest


NOW = datetime(2026, 8, 11, 20, 30, tzinfo=UTC)


class _ReadOnlyClient:
    def __init__(self):
        self.positions = [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "pos": "3.4",
                "mrgPosition": "split",
            }
        ]
        self.pending = [
            {
                "triggerOrderType": "TPSL",
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "long",
                "ordId": "tp-1",
                "tpTriggerPx": "65100",
                "tpOrdPx": "-1",
                "sz": "3.4",
            }
        ]
        self.position_reads = 0
        self.pending_reads = 0
        self.write_calls = 0

    def list_positions(self, *, inst_id=None):
        self.position_reads += 1
        return list(self.positions)

    def list_trigger_orders_pending(self, *, inst_id):
        self.pending_reads += 1
        return list(self.pending)

    def set_position_sltp(self, payload):
        self.write_calls += 1
        raise AssertionError("repair must never write to the exchange")


def _seed_split_truth(tmp_path):
    from telegram_kol_research.db import create_session_factory
    from telegram_kol_research.execution_bindings import (
        ExecutionBindingRecord,
        ExecutionOrderLegRecord,
        upsert_execution_binding,
        upsert_execution_order_leg,
    )
    from telegram_kol_research.models import ExecutionOrderLeg
    from telegram_kol_research.position_protection_legs import (
        bind_filled_position,
        create_or_get_protection_leg,
    )
    from telegram_kol_research.position_take_profit_orders import (
        record_take_profit_order,
    )
    from telegram_kol_research.protection_ledger import upsert_protection_ledger_row

    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol",
            chat_id=1,
            message_id=1,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            pos_id="pos-1",
            status="active",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            purpose="entry",
            strategy_instance_id="deepcoin:1:1:BTC:long",
            order_kind="trigger_limit",
            venue="deepcoin",
            pos_id="pos-1",
            status="active",
        ),
    )
    with session_factory() as session:
        entry_leg = session.get(ExecutionOrderLeg, leg_id)
        entry_leg.attribution_status = "verified"
        entry_leg.attribution_evidence_json = '{"policy_version":2}'
        logical_leg = create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=leg_id,
            role="take_profit",
            leg_index=1,
            planned_trigger_price="65100.0",
            planned_size="5",
        )
        bind_filled_position(session, logical_leg, pos_id="pos-1")
        logical_leg.status = "protection_recovery_pending"
        record_take_profit_order(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            pos_id="pos-1",
            order_id="tp-1",
            trigger_price="65100",
            size_text="3.4",
            created_at=NOW,
            evidence={
                "source": "native_tpsl_pending_readback",
                "native_tpsl": {
                    "triggerOrderType": "TPSL",
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-1",
                    "posSide": "long",
                    "ordId": "tp-1",
                    "tpTriggerPx": "65100",
                    "tpOrdPx": "-1",
                    "sz": "3.4",
                },
            },
        )
        upsert_protection_ledger_row(
            session,
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            strategy_instance_id=entry_leg.strategy_instance_id,
            pos_id="pos-1",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="tp-1",
            purpose="take_profit",
            trigger_price="65100.00",
            size_text="3.4",
            status="verified",
            evidence_source="trigger_take_profit_pending_readback",
            evidence={},
            seen_at=NOW,
        )
        session.commit()
        return session_factory, int(logical_leg.id)


def test_plan_requires_exact_durable_and_exchange_evidence(tmp_path):
    from telegram_kol_research.take_profit_protection_leg_repair import (
        build_take_profit_protection_leg_repair_plan,
    )

    session_factory, logical_leg_id = _seed_split_truth(tmp_path)
    client = _ReadOnlyClient()

    plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )

    assert len(plan.actions) == 1
    assert plan.refusals == ()
    action = plan.actions[0]
    assert action.logical_leg_id == logical_leg_id
    assert action.planned_trigger_price == "65100.0"
    assert action.submitted_trigger_price == "65100"
    assert action.submitted_size == "3.4"
    assert len(action.action_id) == 64
    assert len(plan.fingerprint) == 64
    assert len(plan.confirmation_token) >= 16
    assert client.position_reads == 1
    assert client.pending_reads == 1
    assert client.write_calls == 0


def test_plan_accepts_ledger_owned_exchange_order_without_pos_id(tmp_path):
    from telegram_kol_research.take_profit_protection_leg_repair import (
        build_take_profit_protection_leg_repair_plan,
    )

    session_factory, logical_leg_id = _seed_split_truth(tmp_path)
    client = _ReadOnlyClient()
    client.pending[0].pop("posId")

    plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )

    assert [row.logical_leg_id for row in plan.actions] == [logical_leg_id]
    assert plan.refusals == ()
    assert client.write_calls == 0


@pytest.mark.parametrize(
    "pending_patch",
    [
        {"closePosId": "pos-other"},
        {"PositionID": "pos-1", "posId": "pos-other"},
    ],
)
def test_plan_refuses_any_explicit_exchange_position_id_conflict(
    tmp_path, pending_patch
):
    from telegram_kol_research.take_profit_protection_leg_repair import (
        build_take_profit_protection_leg_repair_plan,
    )

    session_factory, _ = _seed_split_truth(tmp_path)
    client = _ReadOnlyClient()
    client.pending[0].pop("posId")
    client.pending[0].update(pending_patch)

    plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )

    assert plan.actions == ()
    assert [row.reason for row in plan.refusals] == [
        "exchange_take_profit_mismatch"
    ]
    assert client.write_calls == 0


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda session: setattr(
                session.query(__import__(
                    "telegram_kol_research.models", fromlist=["ExecutionOrderLeg"]
                ).ExecutionOrderLeg).one(),
                "status",
                "closed",
            ),
            "entry_leg_not_active_verified",
        ),
        (
            lambda session: setattr(
                session.query(__import__(
                    "telegram_kol_research.models", fromlist=["PositionProtectionLedger"]
                ).PositionProtectionLedger).one(),
                "pos_id",
                "pos-other",
            ),
            "verified_ledger_owner_mismatch",
        ),
    ],
)
def test_plan_refuses_changed_durable_ownership(tmp_path, mutate, reason):
    from telegram_kol_research.take_profit_protection_leg_repair import (
        build_take_profit_protection_leg_repair_plan,
    )

    session_factory, _ = _seed_split_truth(tmp_path)
    with session_factory() as session:
        mutate(session)
        session.commit()

    plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        observed_at=NOW,
    )

    assert plan.actions == ()
    assert [row.reason for row in plan.refusals] == [reason]


@pytest.mark.parametrize(
    ("pending_patch", "reason"),
    [
        ({"posId": "pos-other"}, "exchange_take_profit_mismatch"),
        ({"tpTriggerPx": "65101"}, "exchange_take_profit_mismatch"),
        ({"sz": "3.5"}, "exchange_take_profit_mismatch"),
        ({"tpOrdPx": "65100"}, "exchange_take_profit_not_market"),
    ],
)
def test_plan_refuses_stale_or_mismatched_exchange_order(
    tmp_path, pending_patch, reason
):
    from telegram_kol_research.take_profit_protection_leg_repair import (
        build_take_profit_protection_leg_repair_plan,
    )

    session_factory, _ = _seed_split_truth(tmp_path)
    client = _ReadOnlyClient()
    client.pending[0].update(pending_patch)

    plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )

    assert plan.actions == ()
    assert [row.reason for row in plan.refusals] == [reason]
    assert client.write_calls == 0


def test_plan_refuses_ambiguous_numeric_logical_leg(tmp_path):
    from telegram_kol_research.models import PositionProtectionLeg
    from telegram_kol_research.take_profit_protection_leg_repair import (
        build_take_profit_protection_leg_repair_plan,
    )

    session_factory, logical_leg_id = _seed_split_truth(tmp_path)
    with session_factory() as session:
        original = session.get(PositionProtectionLeg, logical_leg_id)
        session.add(
            PositionProtectionLeg(
                venue="deepcoin",
                execution_binding_id=original.execution_binding_id,
                execution_order_leg_id=original.execution_order_leg_id,
                role="take_profit",
                leg_index=2,
                planned_trigger_price="65100.00",
                planned_size="5",
                pos_id="pos-1",
                exchange_order_id="historical-other-tp",
                status="verified",
            )
        )
        session.commit()

    plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=_ReadOnlyClient(),
        observed_at=NOW,
    )

    assert plan.actions == ()
    assert {row.reason for row in plan.refusals} == {
        "logical_take_profit_price_ambiguous"
    }


def test_apply_binds_existing_order_without_exchange_write_and_is_idempotent(tmp_path):
    from telegram_kol_research.models import PositionProtectionLeg
    from telegram_kol_research.take_profit_protection_leg_repair import (
        apply_take_profit_protection_leg_repair_plan,
        build_take_profit_protection_leg_repair_plan,
    )

    session_factory, logical_leg_id = _seed_split_truth(tmp_path)
    client = _ReadOnlyClient()
    plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )
    action = plan.actions[0]

    result = apply_take_profit_protection_leg_repair_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        action_id=action.action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token=plan.confirmation_token,
        applied_at=NOW,
    )

    assert result.applied == 1
    assert client.write_calls == 0
    with session_factory() as session:
        row = session.get(PositionProtectionLeg, logical_leg_id)
        assert row.status == "verified"
        assert row.exchange_order_id == "tp-1"
        assert "supervised_take_profit_protection_leg_repair" in row.readback_evidence_json
    retry_plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )
    assert retry_plan.actions == ()
    assert retry_plan.refusals == ()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("action_id", "wrong", "exactly one reviewed repair action"),
        ("expected_fingerprint", "0" * 64, "repair plan fingerprint mismatch"),
        ("confirmation_token", "wrong-token", "confirmation token mismatch"),
    ],
)
def test_apply_rejects_wrong_review_gate(tmp_path, field, value, message):
    from telegram_kol_research.take_profit_protection_leg_repair import (
        apply_take_profit_protection_leg_repair_plan,
        build_take_profit_protection_leg_repair_plan,
    )

    session_factory, _ = _seed_split_truth(tmp_path)
    client = _ReadOnlyClient()
    plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )
    values = {
        "action_id": plan.actions[0].action_id,
        "expected_fingerprint": plan.fingerprint,
        "confirmation_token": plan.confirmation_token,
    }
    values[field] = value

    with pytest.raises(ValueError, match=message):
        apply_take_profit_protection_leg_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            applied_at=NOW,
            **values,
        )
    assert client.write_calls == 0


def test_apply_rebuilds_live_plan_and_refuses_exchange_drift(tmp_path):
    from telegram_kol_research.take_profit_protection_leg_repair import (
        apply_take_profit_protection_leg_repair_plan,
        build_take_profit_protection_leg_repair_plan,
    )

    session_factory, _ = _seed_split_truth(tmp_path)
    client = _ReadOnlyClient()
    plan = build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )
    client.pending[0]["sz"] = "3.5"

    with pytest.raises(ValueError, match="repair plan fingerprint changed"):
        apply_take_profit_protection_leg_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            action_id=plan.actions[0].action_id,
            expected_fingerprint=plan.fingerprint,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )
    assert client.write_calls == 0


def test_apply_revalidates_durable_order_inside_final_transaction(
    tmp_path, monkeypatch
):
    from telegram_kol_research.models import PositionTakeProfitOrder
    import telegram_kol_research.take_profit_protection_leg_repair as repair

    session_factory, _ = _seed_split_truth(tmp_path)
    client = _ReadOnlyClient()
    plan = repair.build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )
    original_build = repair.build_take_profit_protection_leg_repair_plan

    def build_then_cancel(*args, **kwargs):
        fresh = original_build(*args, **kwargs)
        with session_factory() as session:
            order = session.query(PositionTakeProfitOrder).one()
            order.status = "cancelled"
            session.commit()
        return fresh

    monkeypatch.setattr(
        repair,
        "build_take_profit_protection_leg_repair_plan",
        build_then_cancel,
    )

    with pytest.raises(ValueError, match="durable repair evidence changed"):
        repair.apply_take_profit_protection_leg_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            action_id=plan.actions[0].action_id,
            expected_fingerprint=plan.fingerprint,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )
    assert client.write_calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("role", "primary_stop"), ("venue", "other"), ("leg_index", 2)],
)
def test_apply_revalidates_complete_logical_identity_inside_final_transaction(
    tmp_path, monkeypatch, field, value
):
    from telegram_kol_research.models import PositionProtectionLeg
    import telegram_kol_research.take_profit_protection_leg_repair as repair

    session_factory, logical_leg_id = _seed_split_truth(tmp_path)
    client = _ReadOnlyClient()
    plan = repair.build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )
    original_build = repair.build_take_profit_protection_leg_repair_plan

    def build_then_change_logical_identity(*args, **kwargs):
        fresh = original_build(*args, **kwargs)
        with session_factory() as session:
            logical_leg = session.get(PositionProtectionLeg, logical_leg_id)
            setattr(logical_leg, field, value)
            session.commit()
        return fresh

    monkeypatch.setattr(
        repair,
        "build_take_profit_protection_leg_repair_plan",
        build_then_change_logical_identity,
    )

    with pytest.raises(ValueError, match="logical protection leg changed"):
        repair.apply_take_profit_protection_leg_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            action_id=plan.actions[0].action_id,
            expected_fingerprint=plan.fingerprint,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )
    assert client.write_calls == 0


def test_apply_revalidates_decimal_price_uniqueness_inside_final_transaction(
    tmp_path, monkeypatch
):
    from telegram_kol_research.models import PositionProtectionLeg
    import telegram_kol_research.take_profit_protection_leg_repair as repair

    session_factory, logical_leg_id = _seed_split_truth(tmp_path)
    client = _ReadOnlyClient()
    plan = repair.build_take_profit_protection_leg_repair_plan(
        session_factory,
        deepcoin_client=client,
        observed_at=NOW,
    )
    original_build = repair.build_take_profit_protection_leg_repair_plan

    def build_then_add_same_price_sibling(*args, **kwargs):
        fresh = original_build(*args, **kwargs)
        with session_factory() as session:
            original = session.get(PositionProtectionLeg, logical_leg_id)
            session.add(
                PositionProtectionLeg(
                    venue="deepcoin",
                    execution_binding_id=original.execution_binding_id,
                    execution_order_leg_id=original.execution_order_leg_id,
                    role="take_profit",
                    leg_index=2,
                    planned_trigger_price="65100.00",
                    planned_size="1",
                    pos_id="pos-1",
                    exchange_order_id="historical-other-tp",
                    status="verified",
                )
            )
            session.commit()
        return fresh

    monkeypatch.setattr(
        repair,
        "build_take_profit_protection_leg_repair_plan",
        build_then_add_same_price_sibling,
    )

    with pytest.raises(ValueError, match="logical protection leg changed"):
        repair.apply_take_profit_protection_leg_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            action_id=plan.actions[0].action_id,
            expected_fingerprint=plan.fingerprint,
            confirmation_token=plan.confirmation_token,
            applied_at=NOW,
        )
    assert client.write_calls == 0
