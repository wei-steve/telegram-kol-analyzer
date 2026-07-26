from __future__ import annotations

from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionBackupStopOrder,
    PositionMutationIntent,
)
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 7, 27, 5, 0, tzinfo=UTC)


class _Client:
    def __init__(
        self,
        *,
        cancel_response=None,
        mutate_other_after_cancel=False,
        after_cancel=None,
    ):
        self.positions = [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-owned",
                "posSide": "long",
                "pos": "4",
            },
            {
                "instId": "BTC-USDT-SWAP",
                "posId": "pos-orphan",
                "posSide": "long",
                "pos": "3",
            },
        ]
        self.pending = [
            {
                "ordId": "legacy-owned",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "side": "sell",
                "sz": "4",
                "triggerPx": "61676.4",
                "triggerOrderType": "Conditional",
            },
            {
                "ordId": "legacy-orphan",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "side": "sell",
                "sz": "3",
                "triggerPx": "61491",
                "triggerOrderType": "Conditional",
            },
            {
                "ordId": "native-owned",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "side": "sell",
                "sz": "0",
                "slTriggerPrice": "61800",
                "triggerOrderType": "TPSL",
            },
            {
                "ordId": "native-orphan",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "side": "sell",
                "sz": "3",
                "slTriggerPrice": "61800",
                "triggerOrderType": "TPSL",
            },
        ]
        self.cancel_response = cancel_response
        self.mutate_other_after_cancel = mutate_other_after_cancel
        self.after_cancel = after_cancel
        self.cancel_payloads = []

    def list_positions(self, *, inst_id=None):
        return list(self.positions)

    def list_trigger_orders_pending(self, *, inst_id):
        return list(self.pending)

    def cancel_trigger_order(self, payload):
        self.cancel_payloads.append(dict(payload))
        order_id = payload["ordId"]
        if self.cancel_response is None:
            self.pending = [row for row in self.pending if row["ordId"] != order_id]
            if self.mutate_other_after_cancel:
                next(
                    row
                    for row in self.pending
                    if row["ordId"] == "legacy-orphan"
                )["sz"] = "30"
            if self.after_cancel is not None:
                self.after_cancel()
            return {"code": "0", "data": order_id}
        return self.cancel_response


def _targets():
    from telegram_kol_research.legacy_conditional_cancel import (
        ReviewedLegacyConditionalTarget,
    )

    return (
        ReviewedLegacyConditionalTarget(
            order_id="legacy-owned",
            pos_id="pos-owned",
            trigger_price="61676.4",
            size="4",
            native_stop_order_id="native-owned",
            native_stop_price="61800",
        ),
        ReviewedLegacyConditionalTarget(
            order_id="legacy-orphan",
            pos_id="pos-orphan",
            trigger_price="61491",
            size="3",
            native_stop_order_id="native-orphan",
            native_stop_price="61800",
            orphan=True,
        ),
    )


def _seed(session_factory):
    rows = (
        ("pos-owned", "legacy-owned", "61676.4", "native-owned", "4"),
        ("pos-orphan", None, None, "native-orphan", "3"),
    )
    for index, (pos_id, legacy_id, legacy_price, native_id, size) in enumerate(rows, 1):
        binding_id = upsert_execution_binding(
            session_factory,
            ExecutionBindingRecord(
                kol_id=f"kol-{index}",
                chat_id=index,
                message_id=index,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                margin_mode="cross",
                position_mode="split",
                status="active",
            ),
        )
        leg_id = upsert_execution_order_leg(
            session_factory,
            ExecutionOrderLegRecord(
                execution_binding_id=binding_id,
                leg_index=1,
                purpose="entry",
                order_kind="market",
                strategy_instance_id=f"deepcoin:{index}:{index}:BTC:long",
                venue="deepcoin",
                pos_id=pos_id,
                status="active",
                attribution_status="verified",
            ),
        )
        with session_factory() as session:
            leg = session.get(ExecutionOrderLeg, leg_id)
            assert leg is not None
            leg.attribution_evidence_json = '{"policy_version":2}'
            upsert_protection_ledger_row(
                session,
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                strategy_instance_id=None,
                pos_id=pos_id,
                instrument_id="BTC-USDT-SWAP",
                side="long",
                order_id=native_id,
                purpose="stop_loss",
                trigger_price="61800",
                size_text=size,
                status="verified",
                evidence_source="test",
                evidence={},
                seen_at=NOW,
            )
            if legacy_id is not None:
                session.add(
                    PositionBackupStopOrder(
                        venue="deepcoin",
                        execution_binding_id=binding_id,
                        execution_order_leg_id=leg_id,
                        pos_id=pos_id,
                        instrument_id="BTC-USDT-SWAP",
                        side="long",
                        trigger_price=legacy_price,
                        order_id=legacy_id,
                        client_order_id="legacy-client",
                        status="active",
                        request_json=(
                            '{"closePosId":"pos-owned","instId":"BTC-USDT-SWAP",'
                            '"mrgPosition":"split","orderType":"market",'
                            '"posSide":"long","side":"sell","sz":"4",'
                            '"triggerPrice":"61676.4"}'
                        ),
                    )
                )
            session.commit()


def test_plan_requires_exact_legacy_rows_positions_and_verified_native_stops(tmp_path):
    from telegram_kol_research.legacy_conditional_cancel import (
        build_reviewed_legacy_conditional_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=_Client(),
        targets=_targets(),
        now=NOW,
    )

    assert [action.order_id for action in plan.actions] == [
        "legacy-orphan",
        "legacy-owned",
    ]
    assert plan.conflicts == ()
    assert len(plan.fingerprint) == 64


def test_plan_refuses_a_changed_legacy_payload(tmp_path):
    from telegram_kol_research.legacy_conditional_cancel import (
        build_reviewed_legacy_conditional_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    client.pending[0]["sz"] = "40"

    plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )

    assert [action.order_id for action in plan.actions] == ["legacy-orphan"]
    assert {"order_id": "legacy-owned", "reason": "legacy_payload_mismatch"} in plan.conflicts
    assert client.cancel_payloads == []


def test_apply_cancels_one_exact_order_after_fresh_plan_and_readback(tmp_path):
    from telegram_kol_research.legacy_conditional_cancel import (
        apply_reviewed_legacy_conditional_cancel_plan,
        build_reviewed_legacy_conditional_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )
    action = next(item for item in plan.actions if item.order_id == "legacy-owned")

    result = apply_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        targets=_targets(),
        pos_id=action.pos_id,
        action_id=action.action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="cancel-owned-confirmation",
        now=NOW,
    )

    assert result.status == "cancelled"
    assert client.cancel_payloads == [
        {"instId": "BTC-USDT-SWAP", "ordId": "legacy-owned"}
    ]
    assert {row["ordId"] for row in client.pending} >= {
        "native-owned",
        "legacy-orphan",
        "native-orphan",
    }
    with session_factory() as session:
        backup = (
            session.query(PositionBackupStopOrder)
            .filter(PositionBackupStopOrder.order_id == "legacy-owned")
            .one()
        )
        assert backup.status == "cancelled"
        event = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.order_id == "legacy-owned")
            .one()
        )
        assert (event.action, event.status, event.reason) == (
            "cancel_reviewed_legacy_conditional",
            "confirmed",
            "reviewed_legacy_conditional_cancelled",
        )
        intent = session.query(PositionMutationIntent).one()
        assert (intent.operation, intent.status, intent.order_id) == (
            "cancel_trigger_order",
            "confirmed",
            "legacy-owned",
        )

    next_plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )
    assert [item.order_id for item in next_plan.actions] == ["legacy-orphan"]
    assert next_plan.conflicts == ()

    client.positions[0]["pos"] = "2"
    drifted_plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )
    assert {
        "order_id": "legacy-owned",
        "reason": "completed_target_state_changed",
    } in drifted_plan.conflicts


def test_apply_refuses_when_native_stop_disappears_before_write(tmp_path):
    from telegram_kol_research.legacy_conditional_cancel import (
        apply_reviewed_legacy_conditional_cancel_plan,
        build_reviewed_legacy_conditional_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )
    action = next(item for item in plan.actions if item.order_id == "legacy-owned")
    client.pending = [row for row in client.pending if row["ordId"] != "native-owned"]

    try:
        apply_reviewed_legacy_conditional_cancel_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            targets=_targets(),
            pos_id=action.pos_id,
            action_id=action.action_id,
            expected_fingerprint=plan.fingerprint,
            confirmation_token="cancel-missing-native",
            now=NOW,
        )
    except ValueError as exc:
        assert "plan fingerprint changed" in str(exc)
    else:
        raise AssertionError("missing native stop must fail closed")
    assert client.cancel_payloads == []


def test_apply_refuses_position_size_drift_before_write(tmp_path):
    from telegram_kol_research.legacy_conditional_cancel import (
        apply_reviewed_legacy_conditional_cancel_plan,
        build_reviewed_legacy_conditional_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )
    action = next(item for item in plan.actions if item.order_id == "legacy-owned")
    client.positions[0]["pos"] = "2"

    try:
        apply_reviewed_legacy_conditional_cancel_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            targets=_targets(),
            pos_id=action.pos_id,
            action_id=action.action_id,
            expected_fingerprint=plan.fingerprint,
            confirmation_token="cancel-size-drift",
            now=NOW,
        )
    except ValueError as exc:
        assert "plan fingerprint changed" in str(exc)
    else:
        raise AssertionError("position-size drift must fail closed")
    assert client.cancel_payloads == []


def test_apply_refuses_native_stop_payload_drift_before_write(tmp_path):
    from telegram_kol_research.legacy_conditional_cancel import (
        apply_reviewed_legacy_conditional_cancel_plan,
        build_reviewed_legacy_conditional_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )
    action = next(item for item in plan.actions if item.order_id == "legacy-owned")
    next(
        row for row in client.pending if row["ordId"] == "native-owned"
    )["slTriggerPrice"] = "61700"

    try:
        apply_reviewed_legacy_conditional_cancel_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            targets=_targets(),
            pos_id=action.pos_id,
            action_id=action.action_id,
            expected_fingerprint=plan.fingerprint,
            confirmation_token="cancel-native-drift",
            now=NOW,
        )
    except ValueError as exc:
        assert "plan fingerprint changed" in str(exc)
    else:
        raise AssertionError("native-stop drift must fail closed")
    assert client.cancel_payloads == []


def test_apply_stops_when_another_reviewed_target_changes_after_write(tmp_path):
    from telegram_kol_research.legacy_conditional_cancel import (
        apply_reviewed_legacy_conditional_cancel_plan,
        build_reviewed_legacy_conditional_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client(mutate_other_after_cancel=True)
    plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )
    action = next(item for item in plan.actions if item.order_id == "legacy-owned")

    result = apply_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        targets=_targets(),
        pos_id=action.pos_id,
        action_id=action.action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="cancel-other-drift",
        now=NOW,
    )

    assert result.status == "cancel_confirmed_readback_changed"
    with session_factory() as session:
        assert session.query(PositionMutationIntent).one().status == "confirmed"
        event = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.order_id == "legacy-owned")
            .one()
        )
        assert event.reason == "post_cancel_state_changed"

    blocked_plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )
    assert blocked_plan.conflicts


def test_confirmed_cancel_is_audited_if_backup_row_changes_after_write(tmp_path):
    from telegram_kol_research.legacy_conditional_cancel import (
        apply_reviewed_legacy_conditional_cancel_plan,
        build_reviewed_legacy_conditional_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)

    def mutate_database():
        with session_factory() as session:
            row = (
                session.query(PositionBackupStopOrder)
                .filter(PositionBackupStopOrder.order_id == "legacy-owned")
                .one()
            )
            row.status = "operator_changed"
            session.commit()

    client = _Client(after_cancel=mutate_database)
    plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )
    action = next(item for item in plan.actions if item.order_id == "legacy-owned")

    result = apply_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        targets=_targets(),
        pos_id=action.pos_id,
        action_id=action.action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="cancel-db-drift",
        now=NOW,
    )

    assert result.status == "cancelled_audit_state_changed"
    with session_factory() as session:
        assert session.query(PositionMutationIntent).one().status == "confirmed"
        event = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.order_id == "legacy-owned")
            .one()
        )
        assert event.reason == "confirmed_cancel_database_state_changed"


def test_apply_does_not_mark_cancelled_on_unconfirmed_response(tmp_path):
    from telegram_kol_research.legacy_conditional_cancel import (
        apply_reviewed_legacy_conditional_cancel_plan,
        build_reviewed_legacy_conditional_cancel_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client(cancel_response={"code": "0", "data": "another-order"})
    plan = build_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        deepcoin_client=client,
        targets=_targets(),
        now=NOW,
    )
    action = next(item for item in plan.actions if item.order_id == "legacy-owned")

    result = apply_reviewed_legacy_conditional_cancel_plan(
        session_factory,
        plan,
        deepcoin_client=client,
        targets=_targets(),
        pos_id=action.pos_id,
        action_id=action.action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="cancel-unconfirmed",
        now=NOW,
    )

    assert result.status == "cancel_unconfirmed"
    with session_factory() as session:
        backup = (
            session.query(PositionBackupStopOrder)
            .filter(PositionBackupStopOrder.order_id == "legacy-owned")
            .one()
        )
        assert backup.status == "active"
