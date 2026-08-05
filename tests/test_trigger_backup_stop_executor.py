import json
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionProtectionLedger,
    TriggerProtectionIntent,
)
from telegram_kol_research.position_protection_legs import (
    create_or_get_protection_leg,
)
from telegram_kol_research.strategy_management_executor import (
    execute_trigger_protection_stop_rescue,
)
from telegram_kol_research.strategy_management_planner import (
    plan_trigger_protection_stop_rescue,
)


NOW = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)


class _ExactFallbackClient:
    def __init__(self, *, include_liquidation=True):
        self.submissions = []
        self.cancel_calls = []
        self.include_liquidation = include_liquidation
        self.pending = [
            {
                "ordId": "anonymous-native-stop",
                "instId": "ETH-USDT-SWAP",
                "posSide": "short",
                "triggerOrderType": "TPSL",
                "sz": "3.4",
                "slTriggerPx": "1935",
                "slOrdPx": "-1",
            }
        ]

    def list_positions(self, *, inst_id=None):
        position = {
                "instId": "ETH-USDT-SWAP",
                "posId": "second-pos",
                "posSide": "short",
                "pos": "3.4",
                "avgPx": "1900",
                "mgnMode": "cross",
                "posMode": "split",
            }
        if self.include_liquidation:
            position["liqPx"] = "2050"
        rows = [position]
        return rows if inst_id is None else [row for row in rows if row["instId"] == inst_id]

    def list_trigger_orders_pending(self, *, inst_id):
        return [row for row in self.pending if row["instId"] == inst_id]

    def set_position_sltp(self, payload):
        self.submissions.append(dict(payload))
        self.pending.append(
            {
                "ordId": "exact-fallback-stop",
                "instId": payload["instId"],
                "posId": payload["posId"],
                "posSide": payload["posSide"],
                "triggerOrderType": "TPSL",
                "sz": "0",
                "slTriggerPx": payload["slTriggerPx"],
                "slOrdPx": payload["slOrdPx"],
            }
        )
        return {"code": "0", "data": {"ordId": "exact-fallback-stop"}}

    def cancel_trigger_order(self, payload):
        self.cancel_calls.append(dict(payload))
        raise AssertionError("exact fallback must never cancel an anonymous native stop")


def _seed_exact_fallback(session_factory):
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:1:1:ETH:short",
            kol_id="shuqin",
            chat_id=1,
            message_id=1,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            margin_mode="cross",
            position_mode="split",
            pos_id="second-pos",
            status="active",
            last_exchange_status="positions_verified",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=2,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="second-entry",
            pos_id="second-pos",
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json='{"policy_version":2}',
            status="active",
            request_json=json.dumps(
                {
                    "instId": "ETH-USDT-SWAP",
                    "posSide": "short",
                    "sz": "3.4",
                    "slTriggerPx": "1935",
                }
            ),
        )
        session.add(leg)
        session.flush()
        intent = TriggerProtectionIntent(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            request_fingerprint="a" * 64,
            pre_submit_tpsl_baseline_json="[]",
            correlation_id="second-intent",
            parent_trigger_order_id="second-entry",
            recovery_state="failed",
            recovery_disposition="exact_backup",
            last_reason_code="protection_assignment_not_mutual_unique",
        )
        session.add(intent)
        session.add(
            ExecutionEvent(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                venue="deepcoin",
                action="create_trigger_entry",
                status="submitted",
                symbol="ETH",
                side="short",
                order_id="second-entry",
                pos_id="second-pos",
                request_json=json.dumps(
                    {
                        "instId": "ETH-USDT-SWAP",
                        "posSide": "short",
                        "sz": "3.4",
                        "slTriggerPx": "1935",
                        "slTriggerPxType": "last",
                        "slOrdPx": "-1",
                    }
                ),
                created_at=NOW,
            )
        )
        create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=leg.id,
            role="primary_stop",
            leg_index=1,
            planned_trigger_price="1935",
            planned_size="3.4",
        )
        session.commit()
        return intent.id


def test_exact_backup_disposition_plans_without_cancelling_anonymous_stop(tmp_path):
    session_factory = create_session_factory(tmp_path / "fallback.db")
    intent_id = _seed_exact_fallback(session_factory)
    client = _ExactFallbackClient()

    plan = plan_trigger_protection_stop_rescue(
        session_factory,
        intent_id=intent_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert plan.status == "ready"
    assert plan.payload["posId"] == "second-pos"
    assert plan.payload["slTriggerPx"] == "1935"
    assert plan.cancel_order_ids == ()
    assert client.cancel_calls == []


def test_exact_backup_execution_persists_exact_ledger_and_is_idempotent(tmp_path):
    session_factory = create_session_factory(tmp_path / "fallback.db")
    intent_id = _seed_exact_fallback(session_factory)
    client = _ExactFallbackClient()
    plan = plan_trigger_protection_stop_rescue(
        session_factory,
        intent_id=intent_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    first = execute_trigger_protection_stop_rescue(
        session_factory,
        rescue_id=plan.rescue_id,
        deepcoin_client=client,
        executed_at=NOW,
    )
    second = execute_trigger_protection_stop_rescue(
        session_factory,
        rescue_id=plan.rescue_id,
        deepcoin_client=client,
        executed_at=NOW,
    )

    assert first["status"] == second["status"] == "verified"
    assert len(client.submissions) == 1
    assert client.cancel_calls == []
    assert {row["ordId"] for row in client.pending} == {
        "anonymous-native-stop",
        "exact-fallback-stop",
    }
    with session_factory() as session:
        ledger = session.query(PositionProtectionLedger).one()
        intent = session.get(TriggerProtectionIntent, intent_id)
    assert (ledger.order_id, ledger.pos_id) == (
        "exact-fallback-stop",
        "second-pos",
    )
    assert (intent.recovery_state, intent.adopted_order_id) == (
        "adopted",
        "exact-fallback-stop",
    )


def test_exact_backup_requires_liquidation_safety_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "fallback.db")
    intent_id = _seed_exact_fallback(session_factory)
    client = _ExactFallbackClient(include_liquidation=False)

    plan = plan_trigger_protection_stop_rescue(
        session_factory,
        intent_id=intent_id,
        deepcoin_client=client,
        planned_at=NOW,
    )

    assert plan.status == "blocked"
    assert plan.reason_code == "rescue_liquidation_price_unavailable"
    assert client.submissions == []
