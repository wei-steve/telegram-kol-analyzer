import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

import telegram_kol_research.execution_bindings as execution_bindings_module
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    _leg_evidence,
    _leg_has_successful_fill_evidence,
    _entry_legs_by_binding_id,
    _successful_fill_leg_ids,
    build_position_evidence,
    _post_entry_protection_mutated_binding_ids,
    build_client_order_id,
    build_deepcoin_account_state,
    build_strategy_instance_id,
    bind_deepcoin_position_to_lifecycle,
    list_execution_order_legs,
    load_deepcoin_order_bindings,
    load_entry_binding_evidence,
    reconcile_deepcoin_execution_bindings,
    repair_execution_order_legs_from_binding_payloads,
    sync_manual_closed_deepcoin_positions,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PositionAttributionAudit,
    PositionProtectionLeg,
    PositionProtectionLedger,
    PositionReconciliationObservation,
    PositionProtectionRevision,
    PositionMutationIntent,
    PendingTpslSnapshotObservation,
    StrategyLifecycle,
    TriggerProtectionIntent,
)
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import StaticDeepcoinContractSpecProvider
from telegram_kol_research.models import PositionBackupStopOrder
from telegram_kol_research.models import PositionProtectionIncident
from telegram_kol_research.position_attribution import AttributionResult, FillEvidence
from telegram_kol_research.position_protection_legs import create_or_get_protection_leg
from telegram_kol_research.trigger_backup_stop_executor import (
    submit_verified_trigger_backup_stops,
)
from telegram_kol_research.trading_settings import save_trading_settings


def _binding(**overrides):
    values = {
        "kol_id": "alice",
        "chat_id": 100,
        "message_id": 55,
        "symbol": "BTC",
        "side": "long",
        "venue": "deepcoin",
        "order_id": "order-1",
        "client_order_id": "client-1",
        "pos_id": None,
        "margin_mode": "cross",
        "position_mode": "split",
        "status": "open",
    }
    values.update(overrides)
    return ExecutionBindingRecord(**values)


def test_read_only_reconcile_surfaces_incomplete_snapshot_after_local_apply(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    snapshot = execution_bindings_module._ReconcileSnapshot(
        errors={"positions": "Authorization: secret-value"}
    )
    applied = []
    monkeypatch.setattr(
        execution_bindings_module,
        "load_deepcoin_execution_reconciliation_snapshot",
        lambda *args, **kwargs: snapshot,
    )
    monkeypatch.setattr(
        execution_bindings_module,
        "_apply_reconcile_snapshot",
        lambda *args, **kwargs: applied.append(kwargs["snapshot"]),
    )

    with pytest.raises(
        execution_bindings_module.DeepcoinReconciliationSnapshotUnavailable
    ):
        execution_bindings_module.reconcile_deepcoin_execution_bindings_read_only(
            session_factory,
            client=object(),
            recovered_at=datetime(2026, 8, 7, 6, 30),
        )

    assert applied == [snapshot]


def test_live_reconcile_runs_due_stop_rescue_before_retry_rescheduling(
    tmp_path, monkeypatch
):
    import telegram_kol_research.strategy_management_reconciliation as management
    import telegram_kol_research.trigger_protection_rescue_worker as rescue_worker

    session_factory = create_session_factory(tmp_path / "rescue-order.db")
    snapshot = execution_bindings_module._ReconcileSnapshot()
    calls = []
    expected_result = object()
    monkeypatch.setattr(
        rescue_worker,
        "run_trigger_protection_rescue_tick",
        lambda *args, **kwargs: calls.append("rescue"),
    )
    monkeypatch.setattr(
        execution_bindings_module,
        "_apply_reconcile_snapshot",
        lambda *args, **kwargs: (calls.append("reconcile"), expected_result)[1],
    )
    monkeypatch.setattr(
        management,
        "reconcile_strategy_management_batches",
        lambda *args, **kwargs: calls.append("management"),
    )

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=object(),
        snapshot=snapshot,
        recovered_at=datetime(2026, 8, 7, 14, 0),
    )

    assert result is expected_result
    assert calls == ["rescue", "reconcile", "management"]


def _add_entry_leg(
    session_factory,
    binding_id,
    *,
    leg_index=1,
    order_id="order-1",
    client_order_id="client-1",
    pos_id=None,
    status="open",
    attribution_status=None,
    request=None,
):
    return upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=leg_index,
            order_id=order_id,
            client_order_id=client_order_id,
            pos_id=pos_id,
            status=status,
            attribution_status=attribution_status,
            attribution_evidence=(
                {
                    "policy_version": 2,
                    "evidence_type": "test_verified_entry",
                }
                if attribution_status == "verified"
                else None
            ),
            request=request,
        ),
    )


def test_entry_binding_evidence_requires_exact_order_and_client_ids(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-evidence.db")
    strategy_instance_id = "deepcoin:100:55:BTC:long"
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(strategy_instance_id=strategy_instance_id),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        order_id="order-1",
        client_order_id="client-1",
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        order_id=None,
        client_order_id="client-2",
    )

    evidence = load_entry_binding_evidence(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        strategy_instance_id=strategy_instance_id,
    )

    assert evidence.binding_id == binding_id
    assert evidence.leg_indices == (1,)
    assert evidence.client_order_ids == ("client-1",)
    assert evidence.exact is False


def _seed_trigger_protection_adoption(session_factory):
    request = {
        "clOrdId": "entry-client-1",
        "instId": "ETH-USDT-SWAP",
        "orderType": "limit",
        "posSide": "short",
        "price": "1883",
        "side": "sell",
        "slOrdPx": -1,
        "slTriggerPx": "1900",
        "sz": "4.4",
        "tdMode": "cross",
        "tpOrdPx": -1,
        "tpTriggerPx": "1860",
        "triggerPrice": "1883",
    }
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="entry-1",
            client_order_id="entry-client-1",
            status="open",
        ),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            order_kind="trigger_limit",
            strategy_instance_id="deepcoin:100:55:ETH:short",
            order_id="entry-1",
            client_order_id="entry-client-1",
            status="submitted",
            attribution_status="unassigned",
            request=request,
        ),
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        session.add(
            ExecutionEvent(
                execution_binding_id=binding_id,
                strategy_instance_id=binding.strategy_instance_id,
                venue="deepcoin",
                action="create_trigger_entry",
                status="submitted",
                symbol="ETH",
                side="short",
                order_id="entry-1",
                client_order_id="entry-client-1",
                reason="live_signal_auto_trade",
                request_json=json.dumps(request),
                response_json=json.dumps({"data": {"ordId": "entry-1"}}),
                created_at=datetime(2026, 7, 20, 8, 0),
            )
        )
        session.commit()
    return binding_id


def _save_trigger_protection_intent(session_factory, *, recovery_state="pending"):
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        request = json.loads(leg.request_json)
        create_or_get_protection_leg(
            session,
            venue="deepcoin",
            execution_order_leg_id=int(leg.id),
            role="primary_stop",
            leg_index=1,
            planned_trigger_price=str(request["slTriggerPx"]),
            planned_size=str(request["sz"]),
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
        fingerprint_request = dict(request)
        fingerprint_request["tpTriggerPx"] = request.get("tpTriggerPx")
        fingerprint_request["slTriggerPx"] = request.get("slTriggerPx")
        session.add(TriggerProtectionIntent(
            venue="deepcoin", execution_binding_id=leg.execution_binding_id,
            execution_order_leg_id=leg.id,
            request_fingerprint=hashlib.sha256(json.dumps(
                fingerprint_request,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()).hexdigest(),
            pre_submit_tpsl_baseline_json="[]", correlation_id="intent-1",
            parent_trigger_order_id="entry-1",
            recovery_state=recovery_state,
        ))
        session.commit()


class _ProtectionAdoptionReconciliationClient:
    def __init__(
        self,
        pending_rows=None,
        *,
        history_rows=None,
        pending_error=None,
        positions=None,
        order_history_rows=None,
    ):
        self.pending_rows = list(pending_rows or [])
        self.pending_error = pending_error
        self.history_rows = list(history_rows or [])
        self.pending_calls = 0
        self.positions = positions
        self.order_history_rows = order_history_rows

    def list_positions(self):
        default = [
            {
                "instId": "ETH-USDT-SWAP",
                "posId": "pos-1",
                "posSide": "short",
                "pos": "4.4",
                "avgPx": "1883",
                "mgnMode": "cross",
                "mrgPosition": "split",
                "cTime": "1784512860000",
            }
        ]
        return default if self.positions is None else list(self.positions)

    def list_open_orders(self):
        return []

    def list_trigger_orders_pending(self, *, inst_id):
        assert inst_id == "ETH-USDT-SWAP"
        self.pending_calls += 1
        if self.pending_error is not None:
            raise self.pending_error
        return self.pending_rows

    def list_order_history(self, *, inst_id=None):
        if self.order_history_rows is not None:
            return list(self.order_history_rows)
        return [
            {
                "instId": "ETH-USDT-SWAP",
                "ordId": "entry-1",
                "clOrdId": "entry-client-1",
                "posId": "pos-1",
                "state": "filled",
                "posSide": "short",
                "fillSz": "4.4",
                "avgPx": "1883",
                "fillTime": "1784512860000",
            }
        ]

    def list_trade_fills(self, *, inst_id=None):
        return []

    def list_trigger_order_history(self, *, inst_id=None):
        return self.history_rows


class _BackupStopSubmissionClient:
    def __init__(self, positions, *, responses=None):
        self.positions = [
            {
                "avgPx": "1883",
                "mgnMode": "cross",
                "posMode": "split",
                **row,
            }
            for row in positions
        ]
        self.responses = list(responses or [])
        self.sltp_payloads = []
        self.pending_rows = [{
            "ordId": f"primary-{row['posId']}", "instId": row["instId"],
            "posId": row["posId"], "posSide": row["posSide"],
            "triggerOrderType": "TPSL", "sz": "0", "slTriggerPx": "1900",
            "slOrdPx": "-1",
        } for row in self.positions]

    def list_positions(self, *, inst_id=None):
        if inst_id is None:
            return self.positions
        return [row for row in self.positions if row.get("instId") == inst_id]

    def set_position_sltp(self, payload):
        self.sltp_payloads.append(payload)
        response = self.responses.pop(0) if self.responses else {"code": "0", "data": "backup-1"}
        order_id = response.get("data") if isinstance(response, dict) else None
        if isinstance(order_id, str) and order_id:
            self.pending_rows.append({
                "ordId": order_id, "instId": payload["instId"],
                "posId": payload["posId"], "posSide": payload["posSide"],
                "triggerOrderType": "TPSL", "sz": "0",
                "slTriggerPx": payload["slTriggerPx"], "slOrdPx": payload["slOrdPx"],
            })
        return response

    def list_trigger_orders_pending(self, *, inst_id):
        return [row for row in self.pending_rows if row.get("instId") == inst_id]


def _backup_stop_provider():
    return StaticDeepcoinContractSpecProvider({
        "ETH-USDT-SWAP": DeepcoinContractSpec(
            instrument_id="ETH-USDT-SWAP", contract_value=0.1, quantity_step=0.1,
            min_quantity=0.1, price_tick=0.1,
        )
    })


def _enable_liveness_live(session_factory):
    save_trading_settings(session_factory, {
        "auto_trade_enabled": True,
        "management_execution_mode": "live",
        "position_management_liveness_v2_mode": "live",
    })


def _seed_exact_backup_candidate(
    session_factory,
    *,
    pos_id="pos-1",
    message_id=55,
    order_kind="market",
    with_primary=True,
):
    _enable_liveness_live(session_factory)
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            message_id=message_id,
            symbol="ETH",
            side="short",
            order_id=f"entry-{pos_id}",
            client_order_id=f"entry-client-{pos_id}",
            status="active",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        order_id=f"entry-{pos_id}",
        client_order_id=f"entry-client-{pos_id}",
        pos_id=pos_id,
        status="active",
        attribution_status="verified",
        request={"instId": "ETH-USDT-SWAP", "orderType": order_kind},
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == binding_id)
            .one()
        )
        leg.strategy_instance_id = binding.strategy_instance_id
        leg.order_kind = order_kind
        leg.attribution_evidence_json = json.dumps({"policy_version": 2})
        if with_primary:
            session.add(PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=leg.id,
                strategy_instance_id=binding.strategy_instance_id,
                pos_id=pos_id,
                instrument_id="ETH-USDT-SWAP",
                side="short",
                order_id=f"primary-{pos_id}",
                purpose="stop_loss",
                trigger_price="1900",
                status="verified",
                evidence_source="test",
                evidence_json="{}",
            ))
        session.commit()
    return binding_id


def test_reconcile_persists_raw_pending_tpsl_completeness_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "pending-tpsl-audit.db")
    _seed_trigger_protection_adoption(session_factory)

    class RawResponseClient(_ProtectionAdoptionReconciliationClient):
        def read_trigger_orders_pending(self, *, inst_id):
            assert inst_id == "ETH-USDT-SWAP"
            return {
                "code": "0",
                "data": [{"ordId": "tp-raw"}],
                "nextCursor": "unhandled-page",
            }

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=RawResponseClient(),
        recovered_at=datetime(2026, 7, 20, 9, 0),
    )

    with session_factory() as session:
        observation = session.query(PendingTpslSnapshotObservation).one()
    assert observation.instrument_id == "ETH-USDT-SWAP"
    assert observation.response_count == 1
    assert observation.order_ids_json == '["tp-raw"]'
    assert observation.complete is False
    assert observation.reason == "pagination_metadata_unsupported"


def _pending_combined_tpsl(order_id):
    return {
        "ordId": order_id,
        "instId": "ETH-USDT-SWAP",
        "posId": "pos-1",
        "posSide": "short",
        "triggerOrderType": "TPSL",
        "sz": "4.4",
        "tpTriggerPx": "1860",
        "slTriggerPx": "1900",
        "cTime": "1784512861000",
        "uTime": "1784512861000",
    }


def _seed_identical_filled_stop_intents(
    session_factory,
    *,
    first_already_owned: bool,
):
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="first-entry",
            client_order_id="first-client",
            status="open",
        ),
    )
    leg_ids = []
    for leg_index, prefix, pos_id in (
        (1, "first", "first-pos"),
        (2, "second", "second-pos"),
    ):
        request = {
            "clOrdId": f"{prefix}-client",
            "instId": "ETH-USDT-SWAP",
            "orderType": "limit",
            "posSide": "short",
            "price": "1900",
            "side": "sell",
            "slOrdPx": -1,
            "slTriggerPx": "1935",
            "sz": "3.4",
            "tdMode": "cross",
            "triggerPrice": "1900",
        }
        leg_id = upsert_execution_order_leg(
            session_factory,
            ExecutionOrderLegRecord(
                execution_binding_id=binding_id,
                leg_index=leg_index,
                order_kind="trigger_limit",
                strategy_instance_id="deepcoin:100:55:ETH:short",
                order_id=f"{prefix}-entry",
                client_order_id=f"{prefix}-client",
                pos_id=pos_id,
                status="active",
                attribution_status="verified",
                attribution_evidence={"evidence_type": "test_exact_position"},
                request=request,
            ),
        )
        leg_ids.append(leg_id)
        with session_factory() as session:
            session.add(
                ExecutionEvent(
                    execution_binding_id=binding_id,
                    strategy_instance_id="deepcoin:100:55:ETH:short",
                    venue="deepcoin",
                    action="create_trigger_entry",
                    status="submitted",
                    symbol="ETH",
                    side="short",
                    order_id=f"{prefix}-entry",
                    client_order_id=f"{prefix}-client",
                    reason="live_signal_auto_trade",
                    request_json=json.dumps(request),
                    response_json=json.dumps(
                        {"data": {"ordId": f"{prefix}-entry"}}
                    ),
                    created_at=datetime(2026, 8, 5, 17, 30 + leg_index),
                )
            )
            fingerprint_request = dict(request)
            fingerprint_request["tpTriggerPx"] = None
            fingerprint_request["slTriggerPx"] = request["slTriggerPx"]
            session.add(
                TriggerProtectionIntent(
                    venue="deepcoin",
                    execution_binding_id=binding_id,
                    execution_order_leg_id=leg_id,
                    request_fingerprint=hashlib.sha256(
                        json.dumps(
                            fingerprint_request,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest(),
                    pre_submit_tpsl_baseline_json="[]",
                    correlation_id=f"{prefix}-intent",
                    parent_trigger_order_id=f"{prefix}-entry",
                    recovery_state=(
                        "adopted"
                        if prefix == "first" and first_already_owned
                        else "pending"
                    ),
                    adopted_order_id=(
                        "first-stop"
                        if prefix == "first" and first_already_owned
                        else None
                    ),
                )
            )
            create_or_get_protection_leg(
                session,
                venue="deepcoin",
                execution_order_leg_id=leg_id,
                role="primary_stop",
                leg_index=1,
                planned_trigger_price="1935",
                planned_size="3.4",
            )
            if prefix == "first" and first_already_owned:
                session.add(
                    PositionProtectionLedger(
                        venue="deepcoin",
                        execution_binding_id=binding_id,
                        execution_order_leg_id=leg_id,
                        strategy_instance_id="deepcoin:100:55:ETH:short",
                        pos_id="first-pos",
                        instrument_id="ETH-USDT-SWAP",
                        side="short",
                        order_id="first-stop",
                        purpose="stop_loss",
                        trigger_price="1935",
                        size_text="3.4",
                        status="verified",
                        evidence_source="test",
                    )
                )
            session.commit()
    return tuple(leg_ids)


def _anonymous_stop(order_id, created_at):
    return {
        "ordId": order_id,
        "instId": "ETH-USDT-SWAP",
        "posId": "",
        "posSide": "short",
        "triggerOrderType": "TPSL",
        "sz": "3.4",
        "slTriggerPx": "1935",
        "slOrdPx": "-1",
        "cTime": created_at,
        "uTime": created_at,
    }


def _filled_entry_history(prefix, pos_id, fill_time):
    return {
        "instId": "ETH-USDT-SWAP",
        "ordId": f"{prefix}-entry",
        "clOrdId": f"{prefix}-client",
        "posId": pos_id,
        "state": "filled",
        "posSide": "short",
        "fillSz": "3.4",
        "avgPx": "1900",
        "fillTime": fill_time,
    }


def test_reconcile_assigns_second_identical_split_stop_globally(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _enable_liveness_live(session_factory)
    _seed_identical_filled_stop_intents(
        session_factory,
        first_already_owned=True,
    )
    positions = [
        {
            "instId": "ETH-USDT-SWAP",
            "posId": "first-pos",
            "posSide": "short",
            "pos": "3.4",
            "cTime": "2026-08-05T17:40:00Z",
        },
        {
            "instId": "ETH-USDT-SWAP",
            "posId": "second-pos",
            "posSide": "short",
            "pos": "3.4",
            "cTime": "2026-08-05T18:00:00Z",
        },
    ]
    pending = [
        _anonymous_stop("second-stop", "2026-08-05T18:00:03Z"),
        _anonymous_stop("first-stop", "2026-08-05T17:40:25Z"),
    ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient(
            pending,
            positions=positions,
            order_history_rows=[
                _filled_entry_history(
                    "first", "first-pos", "2026-08-05T17:40:00Z"
                ),
                _filled_entry_history(
                    "second", "second-pos", "2026-08-05T18:00:00Z"
                ),
            ],
        ),
        recovered_at=datetime(2026, 8, 5, 18, 1),
    )

    with session_factory() as session:
        intents = session.query(TriggerProtectionIntent).order_by(
            TriggerProtectionIntent.execution_order_leg_id.asc()
        ).all()
        ledger_by_order = {
            row.order_id: row for row in session.query(PositionProtectionLedger).all()
        }
    assert intents[0].recovery_state == "adopted"
    assert intents[1].recovery_state == "adopted"
    assert intents[1].adopted_order_id == "second-stop"
    assert ledger_by_order["second-stop"].pos_id == "second-pos"
    assert result.protection_adopted == 1


def test_reconcile_keeps_true_anonymous_stop_ambiguity_recoverable(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _enable_liveness_live(session_factory)
    _seed_identical_filled_stop_intents(
        session_factory,
        first_already_owned=False,
    )
    positions = [
        {
            "instId": "ETH-USDT-SWAP",
            "posId": pos_id,
            "posSide": "short",
            "pos": "3.4",
            "cTime": "2026-08-05T17:40:00Z",
        }
        for pos_id in ("first-pos", "second-pos")
    ]
    pending = [
        _anonymous_stop("first-stop", "2026-08-05T17:40:25Z"),
        _anonymous_stop("second-stop", "2026-08-05T17:40:26Z"),
    ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient(
            pending,
            positions=positions,
            order_history_rows=[
                _filled_entry_history(
                    "first", "first-pos", "2026-08-05T17:40:00Z"
                ),
                _filled_entry_history(
                    "second", "second-pos", "2026-08-05T17:40:00Z"
                ),
            ],
        ),
        recovered_at=datetime(2026, 8, 5, 18, 1),
    )

    with session_factory() as session:
        intents = session.query(TriggerProtectionIntent).all()
        ledger_rows = session.query(PositionProtectionLedger).all()
    assert result.protection_adopted == 0
    assert ledger_rows == []
    assert {intent.recovery_state for intent in intents} == {"retrying"}
    assert {intent.recovery_disposition for intent in intents} == {"exact_backup"}


def test_shadow_global_assignment_is_evidence_only_and_not_authoritative(tmp_path):
    session_factory = create_session_factory(tmp_path / "assignment-shadow.db")
    save_trading_settings(session_factory, {
        "position_management_liveness_v2_mode": "shadow",
    })
    _seed_identical_filled_stop_intents(
        session_factory, first_already_owned=True,
    )
    positions = [
        {
            "instId": "ETH-USDT-SWAP", "posId": "first-pos",
            "posSide": "short", "pos": "3.4",
            "cTime": "2026-08-05T17:40:00Z",
        },
        {
            "instId": "ETH-USDT-SWAP", "posId": "second-pos",
            "posSide": "short", "pos": "3.4",
            "cTime": "2026-08-05T18:00:00Z",
        },
    ]
    pending = [
        _anonymous_stop("second-stop", "2026-08-05T18:00:03Z"),
        _anonymous_stop("first-stop", "2026-08-05T17:40:25Z"),
    ]

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient(
            pending,
            positions=positions,
            order_history_rows=[
                _filled_entry_history("first", "first-pos", "2026-08-05T17:40:00Z"),
                _filled_entry_history("second", "second-pos", "2026-08-05T18:00:00Z"),
            ],
        ),
        recovered_at=datetime(2026, 8, 5, 18, 1),
    )

    with session_factory() as session:
        ledger_orders = {
            row.order_id for row in session.query(PositionProtectionLedger).all()
        }
        incidents = session.query(PositionProtectionIncident).filter_by(
            incident_type="trigger_protection_assignment_shadow_plan"
        ).all()
    assert ledger_orders == {"first-stop"}
    assert incidents
    assert any(
        json.loads(row.evidence_json).get("proposed_order_id") == "second-stop"
        for row in incidents
    )


def test_reconcile_protection_adoption_records_unique_exact_trigger_entry_tpsl(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    client = _ProtectionAdoptionReconciliationClient(
        [_pending_combined_tpsl("tpsl-1")]
    )

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=client,
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        rows = session.query(PositionProtectionLedger).all()
    assert leg.attribution_status == "verified"
    assert leg.pos_id == "pos-1"
    assert [(row.pos_id, row.order_id, row.purpose, row.status) for row in rows] == [
        ("pos-1", "tpsl-1", "combined", "verified")
    ]
    assert result.protection_adopted == 1
    assert result.protection_adoption_refused == 0
    assert result.protection_snapshot_unavailable == 0
    assert client.pending_calls == 1


def test_reconcile_legacy_trigger_adoption_requires_current_live_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        leg.pos_id = "pos-1"
        leg.status = "active"
        leg.attribution_status = "verified"
        session.commit()

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient(
            [_pending_combined_tpsl("legacy-stale")],
            positions=[],
        ),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        assert session.query(PositionProtectionLedger).count() == 0
    assert result.protection_adopted == 0


def test_reconcile_legacy_trigger_adoption_rejects_prefill_candidate(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    candidate = _pending_combined_tpsl("legacy-prefill")
    candidate["cTime"] = "1784512859000"
    candidate["uTime"] = "1784512859000"

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([candidate]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        assert session.query(PositionProtectionLedger).count() == 0
    assert result.protection_adopted == 0
    assert result.protection_adoption_refused == 1


def test_saved_intent_persists_prefill_refusal_reason_for_terminal_recovery(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "saved-prefill.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)
    candidate = _pending_combined_tpsl("saved-prefill")
    candidate["cTime"] = "1784512859000"
    candidate["uTime"] = "1784512859000"

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([candidate]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
    assert result.protection_adoption_refused == 1
    assert intent.last_reason_code == "trigger_protection_candidate_predates_fill"
    assert json.loads(intent.last_evidence_json)["candidate_order_ids"] == [
        "saved-prefill"
    ]


def test_reconcile_submits_one_exact_backup_stop_when_explicitly_enabled(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _enable_liveness_live(session_factory)

    class BackupStopClient(_ProtectionAdoptionReconciliationClient):
        def __init__(self):
            super().__init__([_pending_combined_tpsl("tpsl-1")])
            self.sltp_payloads = []

        def list_positions(self, *, inst_id=None):
            rows = [{
                "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
                    "pos": "4.4", "avgPx": "1883", "liqPx": "2000",
                    "mgnMode": "cross", "mrgPosition": "split",
                    "cTime": "1784512860000",
                }]
            if inst_id is None:
                return rows
            return [row for row in rows if row["instId"] == inst_id]

        def set_position_sltp(self, payload):
            self.sltp_payloads.append(payload)
            self.pending_rows.append({
                "ordId": "backup-1", "instId": "ETH-USDT-SWAP", "posId": "pos-1",
                "posSide": "short", "triggerOrderType": "TPSL", "sz": "0",
                "slTriggerPx": payload["slTriggerPx"], "slOrdPx": payload["slOrdPx"],
            })
            return {"code": "0", "data": "backup-1"}

    client = BackupStopClient()
    provider = StaticDeepcoinContractSpecProvider({
        "ETH-USDT-SWAP": DeepcoinContractSpec(
            instrument_id="ETH-USDT-SWAP", contract_value=0.1, quantity_step=0.1,
            min_quantity=0.1, price_tick=0.1,
        )
    })

    reconcile_deepcoin_execution_bindings(
        session_factory, client=client, recovered_at=datetime(2026, 7, 20, 8, 5),
        contract_spec_provider=provider,
    )
    reconcile_deepcoin_execution_bindings(
        session_factory, client=client, recovered_at=datetime(2026, 7, 20, 8, 6),
        contract_spec_provider=provider,
    )

    assert len(client.sltp_payloads) == 1
    assert client.sltp_payloads[0]["posId"] == "pos-1"
    assert client.sltp_payloads[0]["posSide"] == "short"
    assert client.sltp_payloads[0]["slTriggerPx"] == "1903.8"
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
        assert (row.pos_id, row.order_id, row.status) == ("pos-1", "backup-1", "active")
        assert session.query(ExecutionEvent).filter(ExecutionEvent.action == "create_backup_stop").count() == 1


def test_reconcile_always_submits_missing_backup_stop_when_provider_is_available(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_backup_candidate(session_factory, order_kind="market")
    client = _BackupStopSubmissionClient([{
        "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
        "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
    }])

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=client,
        recovered_at=datetime(2026, 7, 20, 8, 5),
        contract_spec_provider=_backup_stop_provider(),
    )

    assert len(client.sltp_payloads) == 1
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
    assert (row.pos_id, row.order_id, row.status) == ("pos-1", "backup-1", "active")

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=client,
        recovered_at=datetime(2026, 7, 20, 8, 6),
        contract_spec_provider=_backup_stop_provider(),
    )

    assert len(client.sltp_payloads) == 1


def test_tp_convergence_readiness_accepts_exact_backup_without_native_primary(tmp_path):
    from telegram_kol_research.execution_bindings import (
        _ReconcileSnapshot,
        _ready_verified_trigger_take_profit_convergences,
    )
    from telegram_kol_research.models import TriggerTakeProfitConvergence
    from telegram_kol_research.trigger_take_profit_convergence import (
        create_or_get_trigger_take_profit_convergence,
    )

    session_factory = create_session_factory(tmp_path / "backup-only-tp-ready.db")
    binding_id = _seed_exact_backup_candidate(
        session_factory, order_kind="trigger_limit", with_primary=False
    )
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        binding.pos_id = "pos-1"
        leg = session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding_id
        ).one()
        convergence = create_or_get_trigger_take_profit_convergence(
            session,
            venue="deepcoin",
            execution_order_leg_id=int(leg.id),
            desired_take_profits=[
                {"price": "1890", "allocation_pct": "50"},
                {"price": "1860", "allocation_pct": "30"},
                {"price": "1825", "allocation_pct": "20"},
            ],
        )
        session.add(PositionBackupStopOrder(
            venue="deepcoin",
            execution_binding_id=binding_id,
            execution_order_leg_id=int(leg.id),
            pos_id="pos-1",
            instrument_id="ETH-USDT-SWAP",
            side="short",
            trigger_price="1903.8",
            order_id="backup-1",
            client_order_id="backup-client-1",
            status="active",
            request_json=json.dumps({
                "instId": "ETH-USDT-SWAP", "posId": "pos-1",
                "posSide": "short", "slTriggerPx": "1903.8", "slOrdPx": "-1",
            }),
        ))
        snapshot = _ReconcileSnapshot(
            positions=[{
                "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
                "pos": "3.4", "avgPx": "1883", "mgnMode": "cross",
                "mrgPosition": "split", "cTime": "1784512860000",
            }],
            pending_trigger_orders=[{
                "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
                "ordId": "backup-1", "triggerOrderType": "TPSL",
                "slTriggerPx": "1903.8", "slOrdPx": "-1", "sz": "0",
                "cTime": "1784512861000",
            }],
        )
        session.flush()
        _ready_verified_trigger_take_profit_convergences(
            session,
            legs=[leg],
            snapshot=snapshot,
            recovered_at=datetime(2026, 8, 6, 10, 0),
        )
        session.commit()
        convergence_id = int(convergence.id)

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        assert (convergence.status, convergence.reason_code) == ("ready", None)
        readiness = json.loads(convergence.request_json)["readiness"]
    assert (convergence.status, convergence.pos_id) == ("ready", "pos-1")
    assert readiness["pos_id"] == "pos-1"
    assert len(readiness["owned_stop_evidence_fingerprint"]) == 64

    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        convergence.pos_id = "stale-pos"
        _ready_verified_trigger_take_profit_convergences(
            session,
            legs=[leg],
            snapshot=snapshot,
            recovered_at=datetime(2026, 8, 6, 10, 1),
        )
        session.commit()
        conflict_state = (convergence.status, convergence.reason_code)
    assert conflict_state == (
        "conflicted", "convergence_exact_leg_not_verified"
    )

    snapshot.positions[0]["pos"] = "0"
    with session_factory() as session:
        convergence = session.get(TriggerTakeProfitConvergence, convergence_id)
        leg = session.get(ExecutionOrderLeg, convergence.execution_order_leg_id)
        convergence.pos_id = "pos-1"
        convergence.status = "waiting_backup_stop"
        _ready_verified_trigger_take_profit_convergences(
            session,
            legs=[leg],
            snapshot=snapshot,
            recovered_at=datetime(2026, 8, 6, 10, 2),
        )
        session.commit()
        zero_size_state = (convergence.status, convergence.reason_code)
    assert zero_size_state == (
        "waiting_backup_stop", "convergence_waiting_backup_stop"
    )


def test_backup_submission_creates_stop_for_verified_market_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_backup_candidate(session_factory, order_kind="market")
    client = _BackupStopSubmissionClient([
        {
            "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
        }
    ])

    submitted = submit_verified_trigger_backup_stops(
        session_factory,
        client=client,
        contract_spec_provider=_backup_stop_provider(),
        submitted_at=datetime(2026, 7, 20, 8, 5),
    )

    assert submitted == 1
    assert len(client.sltp_payloads) == 1
    assert client.sltp_payloads[0]["posId"] == "pos-1"
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
    assert (row.pos_id, row.order_id, row.status) == ("pos-1", "backup-1", "active")


def test_backup_submission_refuses_success_when_native_backup_replaces_primary_stop(tmp_path):
    """A second SL is only valid when the original native SL still exists."""

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_backup_candidate(session_factory, order_kind="market")

    class ReplacingSltpClient(_BackupStopSubmissionClient):
        def __init__(self):
            super().__init__([{
                "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
                "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
            }])
            self.pending_rows = [{
                "ordId": "primary-pos-1", "instId": "ETH-USDT-SWAP", "posId": "pos-1",
                "posSide": "short", "triggerOrderType": "TPSL", "sz": "0",
                "slTriggerPx": "1900", "slOrdPx": "-1",
            }]

        def set_position_sltp(self, payload):
            self.sltp_payloads.append(payload)
            # Reproduce the dangerous exchange behavior documented for this
            # endpoint: the new TPSL replaces the existing primary one.
            self.pending_rows = [{
                "ordId": "backup-1", "instId": payload["instId"], "posId": payload["posId"],
                "posSide": payload["posSide"], "triggerOrderType": "TPSL", "sz": "0",
                "slTriggerPx": payload["slTriggerPx"], "slOrdPx": payload["slOrdPx"],
            }]
            return {"code": "0", "data": "backup-1"}

    submitted = submit_verified_trigger_backup_stops(
        session_factory,
        client=ReplacingSltpClient(),
        contract_spec_provider=_backup_stop_provider(),
        submitted_at=datetime(2026, 7, 20, 8, 5),
    )

    assert submitted == 0
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
        incidents = session.query(PositionProtectionIncident).all()
    assert row.status == "pending_readback"
    assert [(incident.pos_id, incident.incident_type) for incident in incidents] == [
        ("pos-1", "backup_stop_pending_readback")
    ]


def test_backup_submission_does_not_write_without_current_primary_native_tpsl(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_backup_candidate(session_factory, order_kind="market")

    client = _BackupStopSubmissionClient([{
        "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
        "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
    }])
    client.pending_rows = []

    assert submit_verified_trigger_backup_stops(
        session_factory,
        client=client,
        contract_spec_provider=_backup_stop_provider(),
        submitted_at=datetime(2026, 7, 20, 8, 5),
    ) == 0
    assert client.sltp_payloads == []
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).count() == 0


def test_backup_submission_records_pending_readback_when_post_submit_read_fails(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_backup_candidate(session_factory, order_kind="market")

    class PostSubmitReadFailureClient(_BackupStopSubmissionClient):
        def __init__(self):
            super().__init__([{
                "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
                "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
            }])
            self.pending_reads = 0

        def list_trigger_orders_pending(self, *, inst_id):
            self.pending_reads += 1
            if self.pending_reads > 1:
                raise RuntimeError("pending endpoint unavailable")
            return super().list_trigger_orders_pending(inst_id=inst_id)

    client = PostSubmitReadFailureClient()
    assert submit_verified_trigger_backup_stops(
        session_factory,
        client=client,
        contract_spec_provider=_backup_stop_provider(),
        submitted_at=datetime(2026, 7, 20, 8, 5),
    ) == 0
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
    assert row.status == "pending_readback"


def test_backup_submission_excludes_manual_bound_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_backup_candidate(session_factory, order_kind="manual_bind")
    client = _BackupStopSubmissionClient([
        {
            "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
        }
    ])

    submitted = submit_verified_trigger_backup_stops(
        session_factory,
        client=client,
        contract_spec_provider=_backup_stop_provider(),
        submitted_at=datetime(2026, 7, 20, 8, 5),
    )

    assert submitted == 0
    assert client.sltp_payloads == []
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).count() == 0


def test_backup_submission_records_primary_stop_blocker_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_backup_candidate(session_factory, order_kind="market", with_primary=False)
    client = _BackupStopSubmissionClient([
        {
            "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
        }
    ])

    for minute in (5, 6):
        assert submit_verified_trigger_backup_stops(
            session_factory,
            client=client,
            contract_spec_provider=_backup_stop_provider(),
            submitted_at=datetime(2026, 7, 20, 8, minute),
        ) == 0

    assert client.sltp_payloads == []
    with session_factory() as session:
        incidents = session.query(PositionProtectionIncident).all()
    assert [(row.pos_id, row.incident_type, json.loads(row.evidence_json)) for row in incidents] == [
        ("pos-1", "backup_stop_blocked", {"reason_code": "primary_stop_not_verified"})
    ]


def test_backup_submission_stops_batch_after_unverified_native_tpsl_readback(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_backup_candidate(
        session_factory, pos_id="pos-1", message_id=55, order_kind="trigger_limit"
    )
    _seed_exact_backup_candidate(
        session_factory, pos_id="pos-2", message_id=56, order_kind="trigger_limit"
    )
    client = _BackupStopSubmissionClient(
        [
            {
                "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
                "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
            },
            {
                "instId": "ETH-USDT-SWAP", "posId": "pos-2", "posSide": "short",
                "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
            },
        ],
        responses=[{}],
    )

    submitted = submit_verified_trigger_backup_stops(
        session_factory,
        client=client,
        contract_spec_provider=_backup_stop_provider(),
        submitted_at=datetime(2026, 7, 20, 8, 5),
    )

    assert submitted == 0
    assert len(client.sltp_payloads) == 1
    with session_factory() as session:
        rows = session.query(PositionBackupStopOrder).order_by(PositionBackupStopOrder.pos_id).all()
        incidents = session.query(PositionProtectionIncident).all()
    assert [(row.pos_id, row.status) for row in rows] == [("pos-1", "pending_readback")]
    assert [(row.pos_id, row.incident_type) for row in incidents] == [
        ("pos-1", "backup_stop_pending_readback")
    ]


def test_backup_submission_marks_response_pending_until_exact_native_tpsl_readback(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_backup_candidate(session_factory, order_kind="market")

    class NoReadbackClient(_BackupStopSubmissionClient):
        def set_position_sltp(self, payload):
            self.sltp_payloads.append(payload)
            return {"code": "0", "data": "backup-1"}

    client = NoReadbackClient([{
        "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
        "pos": "4.4", "liqPx": "2000", "mrgPosition": "split",
    }])
    assert submit_verified_trigger_backup_stops(
        session_factory, client=client, contract_spec_provider=_backup_stop_provider(),
        submitted_at=datetime(2026, 7, 20, 8, 5),
    ) == 0
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
    assert (row.order_id, row.status) == ("backup-1", "pending_readback")


def test_backup_submission_accepts_exact_order_readback_when_deepcoin_omits_position_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_exact_backup_candidate(session_factory, order_kind="market")

    class OmittedPositionReadbackClient(_BackupStopSubmissionClient):
        def set_position_sltp(self, payload):
            self.sltp_payloads.append(payload)
            self.pending_rows.append({
                "ordId": "backup-1", "instId": payload["instId"],
                "posSide": payload["posSide"], "triggerOrderType": "TPSL", "sz": "0",
                "slTriggerPx": payload["slTriggerPx"], "slOrdPx": payload["slOrdPx"],
                "cTime": "1784512860000",
            })
            return {"code": "0", "data": "backup-1"}

    client = OmittedPositionReadbackClient([{
        "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
        "pos": "4.4", "liqPx": "2000", "mrgPosition": "split", "cTime": "1784512860000",
    }])
    assert submit_verified_trigger_backup_stops(
        session_factory, client=client, contract_spec_provider=_backup_stop_provider(),
        submitted_at=datetime(2026, 7, 20, 8, 5),
    ) == 1
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
    assert (row.order_id, row.status) == ("backup-1", "active")


def test_reconcile_marks_triggered_primary_stop_failure_and_deduplicates_exact_incident(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([_pending_combined_tpsl("tpsl-1")]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    failed_history = {
        **_pending_combined_tpsl("tpsl-1"),
        "triggerTime": "1784512900000",
        "errorCode": "203",
        "errorMsg": "NotEnoughMoneyToClose",
    }
    client = _ProtectionAdoptionReconciliationClient([], history_rows=[failed_history])
    reconcile_deepcoin_execution_bindings(
        session_factory, client=client, recovered_at=datetime(2026, 7, 20, 8, 6)
    )
    reconcile_deepcoin_execution_bindings(
        session_factory, client=client, recovered_at=datetime(2026, 7, 20, 8, 7)
    )

    with session_factory() as session:
        protection = session.query(PositionProtectionLedger).one()
        incidents = session.query(PositionProtectionIncident).all()
    assert protection.status == "stop_trigger_failed"
    assert [(row.pos_id, row.incident_type, row.delivery_status) for row in incidents] == [
        ("pos-1", "stop_trigger_failed", "pending")
    ]


def test_reconcile_keeps_owned_stop_verified_when_pending_row_omits_position_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([_pending_combined_tpsl("tpsl-1")]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    unscoped_pending = _pending_combined_tpsl("tpsl-1")
    unscoped_pending.pop("posId")
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([unscoped_pending]),
        recovered_at=datetime(2026, 7, 20, 8, 6),
    )

    with session_factory() as session:
        protection = session.query(PositionProtectionLedger).one()
    assert protection.status == "verified"


def test_reconcile_restores_missing_owned_stop_when_same_order_is_pending(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([_pending_combined_tpsl("tpsl-1")]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )
    with session_factory() as session:
        protection = session.query(PositionProtectionLedger).one()
        protection.status = "protection_missing"
        session.commit()

    unscoped_pending = _pending_combined_tpsl("tpsl-1")
    unscoped_pending.pop("posId")
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([unscoped_pending]),
        recovered_at=datetime(2026, 7, 20, 8, 6),
    )

    with session_factory() as session:
        protection = session.query(PositionProtectionLedger).one()
    assert protection.status == "verified"


def test_reconcile_refuses_owned_order_when_exchange_position_id_conflicts(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([_pending_combined_tpsl("tpsl-1")]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    conflict = _pending_combined_tpsl("tpsl-1")
    conflict["posId"] = "other-pos"
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([conflict]),
        recovered_at=datetime(2026, 7, 20, 8, 6),
    )

    with session_factory() as session:
        protection = session.query(PositionProtectionLedger).one()
        incidents = session.query(PositionProtectionIncident).all()
    assert protection.status == "protection_missing"
    assert [(row.pos_id, row.incident_type) for row in incidents] == [
        ("pos-1", "protection_position_conflict")
    ]


def test_reconcile_detects_failed_owned_stop_history_without_position_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([_pending_combined_tpsl("tpsl-1")]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    failed_history = _pending_combined_tpsl("tpsl-1")
    failed_history.pop("posId")
    failed_history.update(
        triggerTime="1784512900000",
        errorCode="203",
        errorMsg="NotEnoughMoneyToClose",
    )
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([], history_rows=[failed_history]),
        recovered_at=datetime(2026, 7, 20, 8, 6),
    )

    with session_factory() as session:
        protection = session.query(PositionProtectionLedger).one()
        incidents = session.query(PositionProtectionIncident).all()
    assert protection.status == "stop_trigger_failed"
    assert [(row.pos_id, row.incident_type) for row in incidents] == [
        ("pos-1", "stop_trigger_failed")
    ]


def test_primary_stop_failure_keeps_verified_backup_stop_active(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([_pending_combined_tpsl("tpsl-1")]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )
    with session_factory() as session:
        primary = session.query(PositionProtectionLedger).one()
        session.add(PositionBackupStopOrder(
            venue="deepcoin", execution_binding_id=primary.execution_binding_id,
            execution_order_leg_id=primary.execution_order_leg_id, pos_id="pos-1",
            instrument_id="ETH-USDT-SWAP", side="short", trigger_price="1903.8",
            order_id="backup-1", client_order_id="backup-client-1", status="active",
            request_json='{"closePosId":"pos-1","orderType":"market"}',
        ))
        session.commit()

    failed_history = _pending_combined_tpsl("tpsl-1")
    failed_history.pop("posId")
    failed_history.update(triggerTime="1784512900000", errorCode="203")
    backup_pending = {
        "instId": "ETH-USDT-SWAP", "ordId": "backup-1", "triggerPrice": "1903.8",
        "orderType": "market",
    }
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient(
            [backup_pending], history_rows=[failed_history]
        ),
        recovered_at=datetime(2026, 7, 20, 8, 6),
    )

    with session_factory() as session:
        primary = session.query(PositionProtectionLedger).one()
        backup = session.query(PositionBackupStopOrder).one()
    assert (primary.status, backup.status) == ("stop_trigger_failed", "active")


def test_reconcile_adopts_saved_trigger_protection_intent_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([_pending_combined_tpsl("tpsl-1")]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )
    duplicate = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([_pending_combined_tpsl("tpsl-1")]),
        recovered_at=datetime(2026, 7, 20, 8, 10),
    )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        rows = session.query(PositionProtectionLedger).all()
        revisions = session.query(PositionProtectionRevision).all()
    assert intent.recovery_state == "adopted"
    assert intent.adopted_order_id == "tpsl-1"
    assert len(rows) == 1
    assert len(revisions) == 1
    assert revisions[0].status == "active"
    assert json.loads(revisions[0].protection_json)["order_ids"] == ["tpsl-1"]
    assert rows[0].evidence_source == "reconciliation_trigger_protection_intent"
    assert result.protection_adopted == 1
    assert duplicate.protection_adopted == 0


def test_reconcile_saved_trigger_protection_requires_live_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        leg.pos_id = "pos-1"
        leg.status = "active"
        leg.attribution_status = "verified"
        session.commit()

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient(
            [_pending_combined_tpsl("tpsl-stale")],
            positions=[],
        ),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        primary = session.query(PositionProtectionLeg).filter(
            PositionProtectionLeg.role == "primary_stop"
        ).one()
        assert session.query(PositionProtectionLedger).count() == 0
        assert session.query(PositionProtectionRevision).count() == 0
    assert result.protection_adopted == 0
    assert intent.recovery_state != "adopted"
    assert primary.exchange_order_id is None


def test_reconcile_saved_trigger_protection_refuses_position_size_change(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        leg.pos_id = "pos-1"
        leg.status = "active"
        leg.attribution_status = "verified"
        session.commit()
    reduced_position = {
        "instId": "ETH-USDT-SWAP",
        "posId": "pos-1",
        "posSide": "short",
        "pos": "2.2",
        "avgPx": "1883",
        "mgnMode": "cross",
        "mrgPosition": "split",
        "cTime": "1784512860000",
    }

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient(
            [_pending_combined_tpsl("tpsl-old-size")],
            positions=[reduced_position],
        ),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        primary = session.query(PositionProtectionLeg).filter(
            PositionProtectionLeg.role == "primary_stop"
        ).one()
        assert session.query(PositionProtectionLedger).count() == 0
        assert session.query(PositionProtectionRevision).count() == 0
    assert result.protection_adopted == 0
    assert intent.recovery_state != "adopted"
    assert primary.exchange_order_id is None


def test_reconcile_anonymous_stop_ignores_same_signature_unfilled_sibling(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = _seed_trigger_protection_adoption(session_factory)
    with session_factory() as session:
        first = session.query(ExecutionOrderLeg).one()
        first_request = json.loads(first.request_json)
        first_request.pop("tpOrdPx", None)
        first_request.pop("tpTriggerPx", None)
        first.request_json = json.dumps(first_request)
        event = session.query(ExecutionEvent).one()
        event.request_json = json.dumps(first_request)
        session.commit()
    _save_trigger_protection_intent(session_factory)

    sibling_request = {
        **first_request,
        "clOrdId": "entry-client-2",
        "price": "1808",
        "triggerPrice": "1808",
    }
    sibling_leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=2,
            order_kind="trigger_limit",
            strategy_instance_id="deepcoin:100:55:ETH:short",
            order_id="entry-2",
            client_order_id="entry-client-2",
            status="pending",
            attribution_status="unassigned",
            request=sibling_request,
        ),
    )
    with session_factory() as session:
        sibling = session.get(ExecutionOrderLeg, sibling_leg_id)
        session.add(
            TriggerProtectionIntent(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=sibling_leg_id,
                request_fingerprint=hashlib.sha256(
                    json.dumps(
                        sibling_request,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                pre_submit_tpsl_baseline_json="[]",
                correlation_id="intent-2",
                parent_trigger_order_id="entry-2",
                recovery_state="pending",
            )
        )
        session.commit()

    anonymous_stop = _pending_combined_tpsl("tpsl-1")
    anonymous_stop["posId"] = ""
    anonymous_stop.pop("tpTriggerPx", None)
    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([anonymous_stop]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        intents = session.query(TriggerProtectionIntent).order_by(
            TriggerProtectionIntent.id.asc()
        ).all()
        ledger = session.query(PositionProtectionLedger).one()
        sibling = session.get(ExecutionOrderLeg, sibling_leg_id)
    assert result.protection_adopted == 1
    assert intents[0].recovery_state == "adopted"
    assert intents[0].adopted_order_id == "tpsl-1"
    assert intents[1].recovery_state == "pending"
    assert sibling.pos_id is None
    assert ledger.pos_id == "pos-1"
    assert ledger.order_id == "tpsl-1"


def test_reconcile_binds_verified_fill_to_planned_protection_before_child_adoption(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)
    with session_factory() as session:
        entry_leg = session.query(ExecutionOrderLeg).one()
        for role in ("primary_stop", "backup_stop", "take_profit"):
            create_or_get_protection_leg(
                session,
                venue="deepcoin",
                execution_order_leg_id=int(entry_leg.id),
                role=role,
                leg_index=1,
                planned_trigger_price="1900" if role != "backup_stop" else None,
                planned_size="4.4" if role != "backup_stop" else None,
            )
        session.commit()

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        rows = session.query(PositionProtectionLeg).order_by(
            PositionProtectionLeg.id.asc()
        ).all()
        assert {row.pos_id for row in rows} == {"pos-1"}
        assert all(row.exchange_order_id is None for row in rows)
        assert all(row.status == "protection_recovery_pending" for row in rows)


@pytest.mark.parametrize("recovery_state", ["failed", "submitting", "resolved"])
def test_reconcile_never_legacy_adopts_a_saved_terminal_or_inflight_intent(
    tmp_path, recovery_state
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory, recovery_state=recovery_state)

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([_pending_combined_tpsl("tpsl-1")]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        assert session.query(PositionProtectionLedger).count() == 0
    assert intent.recovery_state == recovery_state
    assert result.protection_adopted == 0


def test_reconcile_defers_saved_intent_once_and_backs_off_duplicate_delivery(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)
    client = _ProtectionAdoptionReconciliationClient([])
    now = datetime(2026, 7, 20, 8, 5)

    first = reconcile_deepcoin_execution_bindings(session_factory, client=client, recovered_at=now)
    second = reconcile_deepcoin_execution_bindings(session_factory, client=client, recovered_at=now)

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        audits = session.query(PositionAttributionAudit).filter(
            PositionAttributionAudit.event_type == "protection_adoption_refused"
        ).all()
    assert first.protection_adoption_deferred == 1
    assert second.protection_adoption_deferred == 0
    assert intent.recovery_state == "retrying"
    assert intent.retry_attempts == 1
    assert intent.recovery_disposition == "retry"
    assert intent.last_reason_code == "candidate_not_yet_observable"
    assert len(audits) == 1


def test_reconcile_saved_intent_counts_position_conflict(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)
    conflict = _pending_combined_tpsl("tpsl-conflict")
    conflict["posId"] = "other-pos"

    result = reconcile_deepcoin_execution_bindings(
        session_factory, client=_ProtectionAdoptionReconciliationClient([conflict]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        assert session.query(PositionProtectionLedger).count() == 0
    assert result.protection_adoption_conflicting == 1
    assert result.protection_adoption_refused == 1
    assert intent.recovery_state == "retrying"


def test_reconcile_saved_intent_adopts_history_only_proven_candidate(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)
    history = _pending_combined_tpsl("tpsl-history")
    history.update({"parentOrdId": "entry-1", "cTime": "2026-07-20T08:01:00Z"})

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([], history_rows=[history]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
    assert result.protection_adopted == 1
    assert intent.adopted_order_id == "tpsl-history"


def test_reconcile_saved_intent_records_unavailable_snapshot_and_retries(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)
    reconcile_deepcoin_execution_bindings(
        session_factory, client=_ProtectionAdoptionReconciliationClient([]),
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient(pending_error=RuntimeError("unavailable")),
        recovered_at=datetime(2026, 7, 20, 8, 10),
    )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        audit = session.query(PositionAttributionAudit).filter(
            PositionAttributionAudit.event_type == "protection_adoption_refused"
        ).order_by(PositionAttributionAudit.id.desc()).first()
    assert result.protection_snapshot_unavailable == 1
    assert intent.recovery_state == "retrying"
    assert intent.retry_attempts == 1
    assert intent.recovery_disposition == "wait"
    assert intent.last_reason_code == "snapshot_incomplete"
    assert json.loads(audit.evidence_json)["reason"] == "trigger_protection_snapshot_unavailable"


def test_reconcile_saved_intent_outage_does_not_exhaust_retry_budget(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)
    start = datetime(2026, 7, 20, 8, 5)
    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient([]),
        recovered_at=start,
    )
    unavailable = _ProtectionAdoptionReconciliationClient(
        pending_error=RuntimeError("unavailable")
    )

    for attempt in range(7):
        result = reconcile_deepcoin_execution_bindings(
            session_factory,
            client=unavailable,
            recovered_at=start + timedelta(minutes=5 * (attempt + 1)),
        )
        assert result.protection_snapshot_unavailable == 1

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        assert intent.recovery_state == "retrying"
        assert intent.retry_attempts == 1

    adopted = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=_ProtectionAdoptionReconciliationClient(
            [_pending_combined_tpsl("tpsl-after-outage")]
        ),
        recovered_at=start + timedelta(minutes=40),
    )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        assert intent.recovery_state == "adopted"
        assert intent.adopted_order_id == "tpsl-after-outage"
    assert adopted.protection_adopted == 1


def test_reconcile_saved_intent_not_yet_observable_has_bounded_retries(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    _save_trigger_protection_intent(session_factory)
    start = datetime(2026, 7, 20, 8, 5)

    for attempt in range(5):
        reconcile_deepcoin_execution_bindings(
            session_factory,
            client=_ProtectionAdoptionReconciliationClient([]),
            recovered_at=start + timedelta(hours=2 * attempt),
        )

    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        assert intent.recovery_state == "failed"
        assert intent.retry_attempts == 5


def test_reconcile_protection_adoption_refuses_duplicate_exact_candidates(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    client = _ProtectionAdoptionReconciliationClient(
        [_pending_combined_tpsl("tpsl-1"), _pending_combined_tpsl("tpsl-2")]
    )

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=client,
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        rows = session.query(PositionProtectionLedger).all()
        audit = (
            session.query(PositionAttributionAudit)
            .filter(PositionAttributionAudit.event_type == "protection_adoption_refused")
            .one()
        )
    assert rows == []
    assert result.protection_adopted == 0
    assert result.protection_adoption_refused == 1
    assert result.protection_snapshot_unavailable == 0
    assert json.loads(audit.evidence_json) == {
        "candidate_order_ids": ["tpsl-1", "tpsl-2"],
        "reason": "trigger_entry_tpsl_not_unique",
        "size_text": "4.4",
        "trigger_entry_order_id": "entry-1",
    }
    assert client.pending_calls == 1


def test_reconcile_protection_adoption_refuses_order_id_owned_by_other_venue_leg(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    other_binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            message_id=56,
            symbol="ETH",
            order_id="other-entry",
            client_order_id="other-entry-client",
        ),
    )
    other_leg_id = _add_entry_leg(
        session_factory,
        other_binding_id,
        order_id="other-entry",
        client_order_id="other-entry-client",
    )
    with session_factory() as session:
        other_binding = session.get(ExecutionBinding, other_binding_id)
        target_leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.client_order_id == "entry-client-1")
            .one()
        )
        target_leg_id = int(target_leg.id)
        session.add_all(
            [
                PositionProtectionLedger(
                    venue="deepcoin",
                    execution_binding_id=target_leg.execution_binding_id,
                    execution_order_leg_id=target_leg.id,
                    strategy_instance_id=target_leg.strategy_instance_id,
                    pos_id="pos-1",
                    instrument_id="ETH-USDT-SWAP",
                    side="short",
                    order_id="tpsl-1",
                    purpose="combined",
                    status="verified",
                    evidence_source="test",
                ),
                PositionProtectionLedger(
                    venue="other-venue",
                    execution_binding_id=other_binding_id,
                    execution_order_leg_id=other_leg_id,
                    strategy_instance_id=other_binding.strategy_instance_id,
                    pos_id="other-pos",
                    instrument_id="ETH-USDT-SWAP",
                    side="long",
                    order_id="tpsl-1",
                    purpose="combined",
                    status="verified",
                    evidence_source="test",
                ),
            ]
        )
        session.commit()
    client = _ProtectionAdoptionReconciliationClient(
        [_pending_combined_tpsl("tpsl-1")]
    )

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=client,
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    assert result.protection_adopted == 0
    assert result.protection_adoption_refused == 1
    with session_factory() as session:
        rows = session.query(PositionProtectionLedger).all()
        audit = (
            session.query(PositionAttributionAudit)
            .filter(PositionAttributionAudit.event_type == "protection_adoption_refused")
            .one()
        )
    assert sorted((row.venue, row.execution_order_leg_id) for row in rows) == [
        ("deepcoin", target_leg_id),
        ("other-venue", other_leg_id),
    ]
    assert json.loads(audit.evidence_json) == {
        "candidate_order_ids": ["tpsl-1"],
        "reason": "trigger_entry_tpsl_identity_conflict",
        "size_text": "4.4",
        "trigger_entry_order_id": "entry-1",
    }
    assert client.pending_calls == 1


def test_reconcile_protection_adoption_counts_unavailable_pending_snapshot(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    _seed_trigger_protection_adoption(session_factory)
    client = _ProtectionAdoptionReconciliationClient(
        pending_error=RuntimeError("pending TPSL unavailable")
    )

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=client,
        recovered_at=datetime(2026, 7, 20, 8, 5),
    )

    with session_factory() as session:
        assert session.query(PositionProtectionLedger).count() == 0
    assert result.protection_adopted == 0
    assert result.protection_adoption_refused == 0
    assert result.protection_snapshot_unavailable == 1
    assert client.pending_calls == 1


def test_database_bootstrap_creates_execution_bindings_table(tmp_path):
    database_path = tmp_path / "research.db"
    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(execution_bindings)").fetchall()
    }
    conn.close()

    assert "kol_id" in columns
    assert "message_id" in columns
    assert "order_id" in columns
    assert "client_order_id" in columns
    assert "pos_id" in columns
    assert "strategy_instance_id" in columns
    assert "margin_mode" in columns
    assert "position_mode" in columns
    assert "payload_json" in columns
    assert "recovered_at" in columns
    assert "status" in columns


def test_position_evidence_uses_only_direct_normalized_position_fields():
    evidence = build_position_evidence(
        {
            "posId": "pos-1",
            "instId": "ETH-USDT-SWAP",
            "posSide": "short",
            "pos": "1.5",
            "avgPx": "1770.0000001",
            "slTriggerPx": "1820",
            "tpTriggerPx": "1700",
            "mgnMode": "cross",
            "mrgPosition": "split",
            "draft": {"stop_loss": 9999, "take_profit": 1},
        }
    )

    assert evidence is not None
    assert evidence.entry_price == 1770.0000001
    assert evidence.stop_loss == 1820.0
    assert evidence.take_profits == (1700.0,)
    assert evidence.margin_mode == "cross"
    assert evidence.position_mode == "split"


def test_leg_evidence_normalizes_request_draft_binding_modes_and_fill_state():
    binding = ExecutionBinding(
        id=7,
        strategy_instance_id="strategy-7",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="ETH",
        side="short",
        venue="deepcoin",
        margin_mode="cross",
        position_mode="split",
        payload_json=json.dumps(
            {
                "draft": {
                    "order_legs": [
                        {"leg_index": 2, "price": 1770.0, "quantity": 1.5}
                    ],
                    "stop_loss": 1820.0,
                    "take_profit_legs": [{"price": 1700.0}, {"price": 1650.0}],
                }
            }
        ),
        status="open",
    )
    leg = ExecutionOrderLeg(
        id=9,
        execution_binding_id=7,
        strategy_instance_id=None,
        leg_index=2,
        purpose="entry",
        order_kind="trigger_limit",
        order_id="order-9",
        client_order_id="client-9",
        venue="deepcoin",
        status="open",
        request_json=json.dumps({"instId": "ETH-USDT-SWAP", "sz": "1.5"}),
    )

    evidence = _leg_evidence(
        leg,
        binding=binding,
        has_successful_entry_evidence=True,
        protection_mutated=False,
    )

    assert evidence.strategy_instance_id == "strategy-7"
    assert evidence.requested_size == 1.5
    assert evidence.entry_price == 1770.0
    assert evidence.stop_loss == 1820.0
    assert evidence.take_profits == (1700.0, 1650.0)
    assert evidence.margin_mode == "cross"
    assert evidence.position_mode == "split"
    assert evidence.order_kind == "trigger_limit"
    assert evidence.has_successful_entry_evidence is True
    assert evidence.protection_mutated is False


def test_successful_fill_does_not_cross_match_duplicate_client_id_when_order_differs():
    leg = ExecutionOrderLeg(
        id=2,
        execution_binding_id=7,
        leg_index=2,
        purpose="entry",
        order_kind="trigger_limit",
        order_id="order-2",
        client_order_id="duplicate-client",
        venue="deepcoin",
        status="open",
    )
    evidence = [
        FillEvidence(
            source="regular_order",
            order_id="order-1",
            client_order_id="duplicate-client",
            pos_id=None,
            symbol="ETH-USDT-SWAP",
            side="short",
            size=1.5,
            price=1770.0,
            created_at_ms=10_000,
        )
    ]

    assert _leg_has_successful_fill_evidence(leg, evidence, legs=[leg]) is False


def test_successful_fill_requires_at_least_one_shared_identifier():
    leg = ExecutionOrderLeg(
        id=2,
        execution_binding_id=7,
        leg_index=2,
        purpose="entry",
        order_kind="trigger_limit",
        order_id=None,
        client_order_id=None,
        venue="deepcoin",
        status="open",
    )
    evidence = [
        FillEvidence(
            source="regular_order",
            order_id="order-1",
            client_order_id="client-1",
            pos_id=None,
            symbol="ETH-USDT-SWAP",
            side="short",
            size=1.5,
            price=1770.0,
            created_at_ms=10_000,
        )
    ]

    assert _leg_has_successful_fill_evidence(leg, evidence, legs=[leg]) is False


@pytest.mark.parametrize("identifier_kind", ["client", "order"])
def test_successful_fill_rejects_identifier_shared_by_multiple_legs(identifier_kind):
    shared_order_id = "duplicate-order" if identifier_kind == "order" else None
    shared_client_id = "duplicate-client" if identifier_kind == "client" else None
    legs = [
        ExecutionOrderLeg(
            id=leg_id,
            execution_binding_id=7,
            leg_index=leg_id,
            purpose="entry",
            order_kind="trigger_limit",
            order_id=shared_order_id,
            client_order_id=shared_client_id,
            venue="deepcoin",
            status="open",
        )
        for leg_id in (1, 2)
    ]
    evidence = [
        FillEvidence(
            source="regular_order",
            order_id=shared_order_id,
            client_order_id=shared_client_id,
            pos_id=None,
            symbol="ETH-USDT-SWAP",
            side="short",
            size=1.5,
            price=1770.0,
            created_at_ms=10_000,
        )
    ]

    assert all(
        not _leg_has_successful_fill_evidence(leg, evidence, legs=legs)
        for leg in legs
    )


def test_successful_fill_accepts_unique_identifier_within_current_legs():
    unique_leg = ExecutionOrderLeg(
        id=1,
        execution_binding_id=7,
        leg_index=1,
        purpose="entry",
        order_kind="trigger_limit",
        order_id=None,
        client_order_id="unique-client",
        venue="deepcoin",
        status="open",
    )
    other_leg = ExecutionOrderLeg(
        id=2,
        execution_binding_id=7,
        leg_index=2,
        purpose="entry",
        order_kind="trigger_limit",
        order_id=None,
        client_order_id="other-client",
        venue="deepcoin",
        status="open",
    )
    evidence = [
        FillEvidence(
            source="regular_order",
            order_id=None,
            client_order_id="unique-client",
            pos_id=None,
            symbol="ETH-USDT-SWAP",
            side="short",
            size=1.5,
            price=1770.0,
            created_at_ms=10_000,
        )
    ]

    assert _leg_has_successful_fill_evidence(
        unique_leg, evidence, legs=[unique_leg, other_leg]
    )


def test_successful_fill_accepts_matching_order_and_client_pair():
    matching_leg = ExecutionOrderLeg(
        id=1,
        execution_binding_id=7,
        leg_index=1,
        purpose="entry",
        order_kind="trigger_limit",
        order_id="order-1",
        client_order_id="duplicate-client",
        venue="deepcoin",
        status="open",
    )
    other_leg = ExecutionOrderLeg(
        id=2,
        execution_binding_id=7,
        leg_index=2,
        purpose="entry",
        order_kind="trigger_limit",
        order_id="order-2",
        client_order_id="duplicate-client",
        venue="deepcoin",
        status="open",
    )
    evidence = [
        FillEvidence(
            source="regular_order",
            order_id="order-1",
            client_order_id="duplicate-client",
            pos_id=None,
            symbol="ETH-USDT-SWAP",
            side="short",
            size=1.5,
            price=1770.0,
            created_at_ms=10_000,
        )
    ]

    assert _leg_has_successful_fill_evidence(
        matching_leg, evidence, legs=[matching_leg, other_leg]
    )


def test_successful_fill_index_preserves_unique_matches_for_all_legs():
    legs = [
        ExecutionOrderLeg(
            id=leg_id,
            execution_binding_id=7,
            leg_index=leg_id,
            purpose="entry",
            order_kind="trigger_limit",
            order_id=f"order-{leg_id}",
            client_order_id=f"client-{leg_id}",
            venue="deepcoin",
            status="open",
        )
        for leg_id in range(1, 5)
    ]
    evidence = [
        FillEvidence(
            source="regular_order",
            order_id=f"order-{leg_id}",
            client_order_id=f"client-{leg_id}",
            pos_id=None,
            symbol="ETH-USDT-SWAP",
            side="short",
            size=1.5,
            price=1770.0,
            created_at_ms=10_000 + leg_id,
        )
        for leg_id in range(1, 5)
    ]

    assert _successful_fill_leg_ids(evidence, legs=legs) == {1, 2, 3, 4}


def test_entry_legs_index_groups_by_binding_without_reordering_legs():
    legs = [
        ExecutionOrderLeg(id=1, execution_binding_id=8, leg_index=1, purpose="entry", order_kind="market", venue="deepcoin", status="open"),
        ExecutionOrderLeg(id=2, execution_binding_id=7, leg_index=2, purpose="entry", order_kind="market", venue="deepcoin", status="open"),
        ExecutionOrderLeg(id=3, execution_binding_id=8, leg_index=3, purpose="entry", order_kind="market", venue="deepcoin", status="open"),
    ]

    grouped = _entry_legs_by_binding_id(legs)

    assert [leg.id for leg in grouped[8]] == [1, 3]
    assert [leg.id for leg in grouped[7]] == [2]


@pytest.mark.parametrize("invalid_list_value", [None, "not-a-list", {"bad": "shape"}])
def test_leg_evidence_safely_ignores_null_or_nonlist_draft_legs(invalid_list_value):
    binding = ExecutionBinding(
        id=7,
        strategy_instance_id="strategy-7",
        kol_id="alice",
        chat_id=100,
        message_id=55,
        symbol="ETH",
        side="short",
        venue="deepcoin",
        margin_mode="cross",
        position_mode="split",
        payload_json=json.dumps(
            {
                "draft": {
                    "order_legs": invalid_list_value,
                    "take_profit_legs": invalid_list_value,
                }
            }
        ),
        status="open",
    )
    leg = ExecutionOrderLeg(
        id=9,
        execution_binding_id=7,
        leg_index=2,
        purpose="entry",
        order_kind="trigger_limit",
        venue="deepcoin",
        status="open",
        request_json="{}",
    )

    evidence = _leg_evidence(leg, binding=binding)

    assert evidence.requested_size is None
    assert evidence.entry_price is None
    assert evidence.take_profits == ()


def test_recorded_post_entry_protection_mutation_is_loaded_but_initial_setup_is_not(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(session_factory, _binding(symbol="ETH"))
    initial_only_binding_id = upsert_execution_binding(
        session_factory, _binding(symbol="ETH", message_id=56)
    )
    with session_factory() as session:
        session.add_all(
            [
                ExecutionEvent(
                    execution_binding_id=initial_only_binding_id,
                    venue="deepcoin",
                    action="set_position_tpsl",
                    reason="entry_protection",
                    status="submitted",
                ),
                ExecutionEvent(
                    execution_binding_id=binding_id,
                    venue="deepcoin",
                    action="adjust_position_tpsl",
                    reason="adjust_stop_loss",
                    status="submitted",
                ),
            ]
        )
        session.commit()
        mutated = _post_entry_protection_mutated_binding_ids(
            session, binding_ids={binding_id, initial_only_binding_id}
        )

    assert mutated == {binding_id}


def test_legacy_duplicate_position_owners_do_not_block_repair_bootstrap(tmp_path):
    database_path = tmp_path / "legacy-duplicates.db"
    conn = sqlite3.connect(database_path)
    conn.executescript(
        """
        CREATE TABLE execution_order_legs (
            id INTEGER PRIMARY KEY,
            execution_binding_id INTEGER NOT NULL,
            strategy_instance_id VARCHAR(255),
            leg_index INTEGER NOT NULL,
            purpose VARCHAR(64) NOT NULL,
            order_kind VARCHAR(64) NOT NULL,
            order_id VARCHAR(255),
            client_order_id VARCHAR(255),
            pos_id VARCHAR(255),
            status VARCHAR(32) NOT NULL,
            request_json TEXT,
            response_json TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO execution_order_legs VALUES
          (1, 1, NULL, 1, 'entry', 'unknown', NULL, NULL, 'dup-pos', 'active', NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
          (2, 2, NULL, 1, 'entry', 'unknown', NULL, NULL, 'dup-pos', 'active', NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
        """
    )
    conn.commit()
    conn.close()

    create_session_factory(database_path)

    conn = sqlite3.connect(database_path)
    indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(execution_order_legs)").fetchall()
    }
    conn.close()
    assert "uq_execution_order_legs_venue_pos" not in indexes


def test_upsert_execution_binding_updates_existing_strategy_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    first = upsert_execution_binding(session_factory, _binding(order_id="order-1"))
    second = upsert_execution_binding(
        session_factory,
        _binding(order_id="order-2", pos_id="pos-1", status="active"),
    )

    assert first == second
    with session_factory() as session:
        stored = session.query(ExecutionBinding).one()

    assert stored.order_id == "order-2"
    assert stored.pos_id == "pos-1"
    assert stored.strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert stored.client_order_id == "client-1"
    assert stored.margin_mode == "cross"
    assert stored.position_mode == "split"
    assert stored.status == "active"


def test_upsert_execution_order_leg_tracks_deepcoin_ids_per_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(order_id="order-1,order-2", client_order_id="client-1,client-2"),
    )

    first_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:BTC:long",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="order-1",
            client_order_id="client-1",
            pos_id="pos-1",
            status="active",
            request={"instId": "BTC-USDT-SWAP", "sz": "5"},
            response={"data": {"ordId": "order-1"}},
        ),
    )
    second_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:BTC:long",
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="order-1b",
            client_order_id="client-1",
            pos_id="pos-1",
            status="active",
        ),
    )

    assert first_id == second_id
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert len(legs) == 1
    assert legs[0].order_id == "order-1b"
    assert legs[0].client_order_id == "client-1"
    assert legs[0].pos_id == "pos-1"
    with session_factory() as session:
        stored = session.query(ExecutionOrderLeg).one()
    assert stored.request_json == '{"instId":"BTC-USDT-SWAP","sz":"5"}'


def test_execution_order_leg_position_ownership_is_unique(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(session_factory, _binding())

    with session_factory() as session:
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=binding_id,
                    leg_index=1,
                    purpose="entry",
                    venue="deepcoin",
                    pos_id="pos-1",
                ),
                ExecutionOrderLeg(
                    execution_binding_id=binding_id,
                    leg_index=2,
                    purpose="entry",
                    venue="deepcoin",
                    pos_id="pos-1",
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_execution_order_leg_allows_multiple_unassigned_positions(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(session_factory, _binding())

    with session_factory() as session:
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=binding_id,
                    leg_index=1,
                    purpose="entry",
                    venue="deepcoin",
                    pos_id=None,
                ),
                ExecutionOrderLeg(
                    execution_binding_id=binding_id,
                    leg_index=2,
                    purpose="entry",
                    venue="deepcoin",
                    pos_id=None,
                ),
            ]
        )
        session.commit()

        assert session.query(ExecutionOrderLeg).count() == 2


@pytest.mark.parametrize("terminal_status", ["manually_cancelled", "exchange_cancelled"])
def test_reconcile_never_reopens_terminal_entry_leg_from_old_signal(
    tmp_path, terminal_status
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="old-trigger",
            client_order_id="old-client",
            status="closed",
        ),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            order_kind="trigger_limit",
            order_id="old-trigger",
            client_order_id="old-client",
            status=terminal_status,
            terminal_reason=terminal_status,
        ),
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "unrelated-live-position",
                    "posSide": "short",
                    "pos": "1.5",
                    "avgPx": "1770",
                }
            ]

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(session_factory, client=FakeClient())

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.query(ExecutionOrderLeg).one()
    assert binding.status == "closed"
    assert binding.pos_id is None
    assert leg.status == terminal_status
    assert leg.pos_id is None


def test_reconcile_marks_leg_exchange_cancelled_from_recorded_cancel_event(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(order_id="trigger-1", client_order_id="client-1", status="closed"),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            order_kind="trigger_limit",
            order_id="trigger-1",
            client_order_id="client-1",
            status="submitted",
        ),
    )
    with session_factory() as session:
        session.add(
            ExecutionEvent(
                execution_binding_id=binding_id,
                venue="deepcoin",
                action="cancel_trigger_entry",
                status="submitted",
                order_id="trigger-1",
                client_order_id="client-1",
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(session_factory, client=FakeClient())

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.query(ExecutionOrderLeg).one()
    assert binding.status == "closed"
    assert leg.status == "exchange_cancelled"
    assert leg.terminal_reason == "cancel_trigger_entry"


def test_reconcile_global_attribution_is_independent_of_binding_creation_order(tmp_path):
    def run_case(database_name, creation_order):
        session_factory = create_session_factory(tmp_path / database_name)
        definitions = {
            "market": ("market-order", "client-market"),
            "trigger": ("trigger-order", "client-trigger"),
        }
        binding_ids = {}
        for label in creation_order:
            order_id, client_order_id = definitions[label]
            binding_id = upsert_execution_binding(
                session_factory,
                _binding(
                    kol_id=f"smart-{label}",
                    chat_id=200 if label == "market" else 201,
                    message_id=300 if label == "market" else 301,
                    symbol="ETH",
                    side="short",
                    order_id=order_id,
                    client_order_id=client_order_id,
                    status="open",
                ),
            )
            binding_ids[label] = binding_id
            upsert_execution_order_leg(
                session_factory,
                ExecutionOrderLegRecord(
                    execution_binding_id=binding_id,
                    leg_index=1,
                    order_kind="market" if label == "market" else "trigger_limit",
                    order_id=order_id,
                    client_order_id=client_order_id,
                    status="open",
                    request={"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "1.5"},
                ),
            )

        class FakeClient:
            def list_positions(self):
                return [
                    {
                        "instId": "ETH-USDT-SWAP",
                        "posId": "market-order",
                        "posSide": "short",
                        "pos": "1.5",
                        "avgPx": "1770",
                        "cTime": "10000",
                    },
                    {
                        "instId": "ETH-USDT-SWAP",
                        "posId": "trigger-order",
                        "posSide": "short",
                        "pos": "1.5",
                        "avgPx": "1770",
                        "cTime": "79000",
                    },
                ]

            def list_open_orders(self):
                return []

            def list_order_history(self, *, inst_id=None):
                return [
                    {
                        "instId": "ETH-USDT-SWAP",
                        "ordId": "market-order",
                        "clOrdId": "client-market",
                        "state": "filled",
                        "posSide": "short",
                        "fillSz": "1.5",
                        "avgPx": "1770",
                        "fillTime": "10000",
                    },
                    {
                        "instId": "ETH-USDT-SWAP",
                        "ordId": "trigger-order",
                        "clOrdId": "client-trigger",
                        "state": "filled",
                        "posSide": "short",
                        "fillSz": "1.5",
                        "avgPx": "1770",
                        "fillTime": "79000",
                    },
                ]

            def list_trade_fills(self, *, inst_id=None):
                return []

            def list_trigger_order_history(self, *, inst_id=None):
                return []

        reconcile_deepcoin_execution_bindings(session_factory, client=FakeClient())
        with session_factory() as session:
            return {
                label: (
                    session.get(ExecutionBinding, binding_id).pos_id,
                    session.query(ExecutionOrderLeg)
                    .filter(ExecutionOrderLeg.execution_binding_id == binding_id)
                    .one()
                    .attribution_status,
                )
                for label, binding_id in binding_ids.items()
            }

    expected = {
        "market": ("market-order", "verified"),
        "trigger": ("trigger-order", "verified"),
    }
    assert run_case("forward.db", ["market", "trigger"]) == expected
    assert run_case("reverse.db", ["trigger", "market"]) == expected


def test_reconcile_does_not_claim_single_position_without_entry_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(symbol="ETH", side="short", order_id="missing-order", status="open"),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            order_id="missing-order",
            status="open",
            request={"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "1.5"},
        ),
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "manual-position",
                    "posSide": "short",
                    "pos": "1.5",
                    "avgPx": "1770",
                    "cTime": "10000",
                }
            ]

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(session_factory, client=FakeClient())

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.query(ExecutionOrderLeg).one()
    assert binding.pos_id is None
    assert binding.status == "stale"
    assert leg.pos_id is None
    assert leg.attribution_status == "unassigned"


def test_reconcile_api_failure_preserves_position_and_deduplicates_audit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(symbol="ETH", side="short", pos_id="pos-verified", status="active"),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            order_id="order-verified",
            pos_id="pos-verified",
            status="active",
            attribution_status="verified",
            attribution_evidence={"evidence_type": "exact_regular_order_id"},
        ),
    )

    class FailingClient:
        def list_positions(self):
            raise RuntimeError("positions temporarily unavailable")

        def list_open_orders(self):
            return []

    for _ in range(2):
        reconcile_deepcoin_execution_bindings(session_factory, client=FailingClient())

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.query(ExecutionOrderLeg).one()
        audits = session.query(PositionAttributionAudit).all()
    assert binding.pos_id == "pos-verified"
    assert binding.status == "active"
    assert leg.pos_id == "pos-verified"
    assert leg.attribution_status == "evidence_unavailable"
    assert len(audits) == 1
    assert "positions temporarily unavailable" in audits[0].evidence_json
    assert audits[0].notification_status == "pending"

    class DifferentFailingClient(FailingClient):
        def list_positions(self):
            raise RuntimeError("positions authorization rejected")

    reconcile_deepcoin_execution_bindings(
        session_factory, client=DifferentFailingClient()
    )
    with session_factory() as session:
        audits = session.query(PositionAttributionAudit).order_by(
            PositionAttributionAudit.id
        ).all()
    assert len(audits) == 2
    assert audits[0].fingerprint != audits[1].fingerprint
    assert all(row.notification_status == "pending" for row in audits)


def test_reconcile_restores_prior_authority_after_outage_and_position_close(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-verified", status="active"),
    )
    leg_id = _add_entry_leg(
        session_factory,
        binding_id,
        pos_id="pos-verified",
        status="active",
        attribution_status="verified",
    )
    with session_factory() as session:
        session.add(
            PositionAttributionAudit(
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                venue="deepcoin",
                pos_id="pos-verified",
                event_type="ownership_verified",
                prior_state="unassigned",
                new_state="verified",
                fingerprint="prior-outage-close-" + "a" * 45,
                evidence_json=json.dumps(
                    {
                        "policy_version": 2,
                        "evidence_type": "direct_order_position_id",
                    }
                ),
                created_at=datetime(2026, 8, 10, 1, 0),
            )
        )
        session.commit()

    class FailingClient:
        def list_positions(self):
            raise RuntimeError("positions temporarily unavailable")

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FailingClient(),
        recovered_at=datetime(2026, 8, 10, 1, 1),
    )
    with session_factory() as session:
        assert session.get(ExecutionOrderLeg, leg_id).attribution_status == (
            "evidence_unavailable"
        )

    class CompleteEmptyClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=CompleteEmptyClient(),
        recovered_at=datetime(2026, 8, 10, 1, 2),
    )

    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        conflict_count = (
            session.query(PositionAttributionAudit)
            .filter(PositionAttributionAudit.execution_order_leg_id == leg_id)
            .filter(PositionAttributionAudit.event_type == "attribution_conflict")
            .count()
        )
    assert leg.pos_id == "pos-verified"
    assert leg.attribution_status == "verified"
    assert conflict_count == 0


def test_reconcile_does_not_restore_prior_authority_over_current_identity_conflict(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-old", status="active"),
    )
    leg_id = _add_entry_leg(
        session_factory,
        binding_id,
        pos_id="pos-old",
        status="active",
        attribution_status="evidence_unavailable",
        request={
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "5",
            "px": "63000",
        },
    )
    with session_factory() as session:
        session.add(
            PositionAttributionAudit(
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                venue="deepcoin",
                pos_id="pos-old",
                event_type="ownership_verified",
                prior_state="unassigned",
                new_state="verified",
                fingerprint="prior-current-conflict-" + "b" * 42,
                evidence_json=json.dumps(
                    {
                        "policy_version": 2,
                        "evidence_type": "direct_order_position_id",
                    }
                ),
                created_at=datetime(2026, 8, 10, 2, 0),
            )
        )
        session.commit()

    monkeypatch.setattr(
        execution_bindings_module,
        "match_entry_legs_to_positions",
        lambda *args, **kwargs: AttributionResult(
            conflicts=[
                {
                    "leg_ids": [leg_id],
                    "position_ids": ["pos-new"],
                }
            ]
        ),
    )

    class ConflictingClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=ConflictingClient(),
        recovered_at=datetime(2026, 8, 10, 2, 1),
    )

    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        conflicts = (
            session.query(PositionAttributionAudit)
            .filter(PositionAttributionAudit.execution_order_leg_id == leg_id)
            .filter(PositionAttributionAudit.event_type == "attribution_conflict")
            .all()
        )
    assert leg.pos_id == "pos-old"
    assert leg.attribution_status == "attribution_conflict"
    assert len(conflicts) == 1
    assert "pos-new" in conflicts[0].evidence_json


def test_reconcile_does_not_restore_prior_authority_over_local_position_owner_conflict(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-shared", status="active"),
    )
    authoritative_leg_id = _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        order_id="order-authoritative",
        client_order_id="client-authoritative",
        pos_id="pos-shared",
        status="active",
        attribution_status="evidence_unavailable",
    )
    competing_leg_id = _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        order_id="order-competing",
        client_order_id="client-competing",
        pos_id="pos-shared,legacy-other",
        status="active",
        attribution_status="attribution_conflict",
    )
    with session_factory() as session:
        session.add(
            PositionAttributionAudit(
                execution_binding_id=binding_id,
                execution_order_leg_id=authoritative_leg_id,
                venue="deepcoin",
                pos_id="pos-shared",
                event_type="ownership_verified",
                prior_state="unassigned",
                new_state="verified",
                fingerprint="prior-local-conflict-" + "c" * 43,
                evidence_json=json.dumps(
                    {
                        "policy_version": 2,
                        "evidence_type": "direct_order_position_id",
                    }
                ),
                created_at=datetime(2026, 8, 10, 3, 0),
            )
        )
        session.commit()

    class CompleteEmptyClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=CompleteEmptyClient(),
        recovered_at=datetime(2026, 8, 10, 3, 1),
    )

    with session_factory() as session:
        authoritative_leg = session.get(
            ExecutionOrderLeg,
            authoritative_leg_id,
        )
        competing_leg = session.get(ExecutionOrderLeg, competing_leg_id)
        conflicts = (
            session.query(PositionAttributionAudit)
            .filter(PositionAttributionAudit.event_type == "attribution_conflict")
            .all()
        )
    assert authoritative_leg.attribution_status == "attribution_conflict"
    assert competing_leg.attribution_status == "attribution_conflict"
    assert any(
        str(authoritative_leg_id) in row.evidence_json
        and str(competing_leg_id) in row.evidence_json
        for row in conflicts
    )


def test_full_close_predicate_rejects_conflicting_position_identity_aliases():
    assert not execution_bindings_module.position_history_row_proves_full_close(
        {
            "instId": "BTC-USDT-SWAP",
            "PositionID": "pos-target",
            "positionId": "pos-other",
            "posSide": "short",
            "pos": "5",
            "closePos": "5",
        },
        instrument_id="BTC-USDT-SWAP",
        position_side="short",
        pos_id="pos-target",
    )


@pytest.mark.parametrize(
    "size_evidence",
    [
        {"pos": "5", "positionSize": "1", "closePos": "5"},
        {"pos": "5", "size": "0", "closePos": "5"},
        {"pos": "5", "size": "NaN", "closePos": "5"},
        {"pos": "5", "closePos": "5", "closedSize": "4"},
    ],
)
def test_full_close_predicate_rejects_conflicting_or_nonfinite_size_aliases(
    size_evidence,
):
    assert not execution_bindings_module.position_history_row_proves_full_close(
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-target",
            "posSide": "short",
            **size_evidence,
        },
        instrument_id="BTC-USDT-SWAP",
        position_side="short",
        pos_id="pos-target",
    )


@pytest.mark.parametrize(
    "identity_evidence",
    [
        {"instId": "BTC-USDT-SWAP", "symbol": "ETH"},
        {"posSide": "short", "side": "long"},
    ],
)
def test_full_close_predicate_rejects_conflicting_instrument_or_side_aliases(
    identity_evidence,
):
    assert not execution_bindings_module.position_history_row_proves_full_close(
        {
            "instId": "BTC-USDT-SWAP",
            "posId": "pos-target",
            "posSide": "short",
            "pos": "5",
            "closePos": "5",
            **identity_evidence,
        },
        instrument_id="BTC-USDT-SWAP",
        position_side="short",
        pos_id="pos-target",
    )


def test_repair_execution_order_legs_from_binding_payloads_backfills_legacy_rows(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            order_id="trigger-1,trigger-2",
            client_order_id="client-1,client-2",
            status="open",
            payload={
                "submitted_orders": [
                    {
                        "leg_index": 1,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-1",
                        "client_order_id": "client-1",
                        "request": {"instId": "BTC-USDT-SWAP", "sz": "5"},
                        "response": {"data": {"ordId": "trigger-1"}},
                    },
                    {
                        "leg_index": 2,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-2",
                        "client_order_id": "client-2",
                        "pos_id": "pos-2",
                    },
                ]
            },
        ),
    )

    repaired = repair_execution_order_legs_from_binding_payloads(session_factory)

    assert repaired == 2
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.order_id, leg.client_order_id, leg.pos_id, leg.status) for leg in legs] == [
        (1, "trigger-1", "client-1", None, "open"),
        (2, "trigger-2", "client-2", "pos-2", "active"),
    ]


def test_load_deepcoin_order_bindings_returns_open_and_active_records(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(session_factory, _binding(order_id="order-1", status="open"))
    upsert_execution_binding(
        session_factory,
        _binding(
            kol_id="bob",
            chat_id=101,
            message_id=66,
            symbol="ETH",
            side="short",
            order_id="order-2",
            pos_id="pos-2",
            status="closed",
        ),
    )
    upsert_execution_binding(
        session_factory,
        _binding(
            kol_id="carol",
            chat_id=102,
            message_id=77,
            symbol="BTC",
            side="short",
            order_id=None,
            pos_id="pos-3",
            status="active",
        ),
    )

    bindings = load_deepcoin_order_bindings(session_factory)

    assert [(binding.kol_id, binding.order_id, binding.pos_id) for binding in bindings] == [
        ("alice", "order-1", None),
        ("carol", None, "pos-3"),
    ]
    assert bindings[0].client_order_id == "client-1"


def test_build_deepcoin_account_state_uses_persisted_bindings(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id="order-1", pos_id="pos-1", status="active"),
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-1",
                    "posSide": "long",
                    "pos": "1",
                }
            ]

        def list_open_orders(self):
            return []

    account_state = build_deepcoin_account_state(
        session_factory,
        client=FakeClient(),
    )

    positions = account_state.load_active_positions()
    assert len(positions) == 1
    assert positions[0].kol_id == "alice"


def test_build_stable_strategy_and_client_order_ids():
    strategy_id = build_strategy_instance_id(
        venue="deepcoin",
        chat_id=100,
        message_id=55,
        symbol="btc",
        side="LONG",
    )

    assert strategy_id == "deepcoin:100:55:BTC:long"
    client_order_id = build_client_order_id(strategy_instance_id=strategy_id, leg_index=2)
    assert client_order_id == "TK729D11F4739D2A2"
    assert client_order_id.isalnum()
    assert len(client_order_id) <= 20


def test_build_client_order_id_can_include_kol_code_and_message_id():
    strategy_id = build_strategy_instance_id(
        venue="deepcoin",
        chat_id=-1002409877375,
        message_id=8248,
        symbol="btc",
        side="short",
    )

    client_order_id = build_client_order_id(
        strategy_instance_id=strategy_id,
        leg_index=1,
        kol_code="FG",
        message_id=8248,
    )

    assert client_order_id == "TKFG8248E1"
    assert client_order_id.isalnum()
    assert len(client_order_id) <= 20


def test_reconcile_deepcoin_execution_bindings_marks_restart_state(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id=None, client_order_id="client-open", status="unknown"),
    )
    upsert_execution_binding(
        session_factory,
        _binding(
            kol_id="bob",
            chat_id=101,
            message_id=66,
            symbol="ETH",
            side="short",
            order_id="order-stale",
            client_order_id="client-stale",
            status="open",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=101,
                message_id=66,
                symbol="ETH",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "clOrdId": "client-open",
                    "posSide": "long",
                    "state": "live",
                }
            ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
    )

    assert result.open == 1
    assert result.stale == 1
    with session_factory() as session:
        rows = session.query(ExecutionBinding).order_by(ExecutionBinding.chat_id.asc()).all()
    assert rows[0].status == "open"
    assert rows[0].last_exchange_status == "entry_order_pending"
    assert rows[0].strategy_instance_id == "deepcoin:100:55:BTC:long"
    assert rows[1].status == "stale"
    assert rows[1].last_exchange_status == "position_ownership_unassigned"
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).filter_by(chat_id=101).one()
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None


def test_reconcile_deepcoin_execution_bindings_keeps_trigger_pending_order_open(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id="trigger-open", client_order_id="client-trigger", status="open"),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 30, 9, 0),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "trigger-open",
                    "clOrdId": "client-trigger",
                    "state": "live",
                }
            ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
    )

    assert result.open == 1
    assert result.stale == 0
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()
    assert binding.status == "open"
    assert binding.last_exchange_status == "entry_order_pending"
    assert lifecycle.lifecycle_status == "pending_entry"


def test_reconcile_does_not_recover_position_from_uniqueness_alone(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id="order-filled", client_order_id="client-filled", status="open"),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 30, 9, 0),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-filled",
                    "posSide": "long",
                    "pos": "9",
                }
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.active == 0
    assert result.stale == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "stale"
    assert binding.pos_id is None
    assert binding.last_exchange_status == "position_ownership_unassigned"
    assert lifecycle.lifecycle_status == "pending_entry"


def test_reconcile_keeps_bound_live_position_active_even_when_signal_is_old(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-late", status="active"),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        pos_id="pos-late",
        status="active",
        attribution_status="verified",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 30, 2, 51),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-late",
                    "posSide": "long",
                    "pos": "9",
                }
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 3, 3, 44),
    )

    assert result.active == 1
    assert result.stale == 0
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert binding.last_exchange_status == "position_ownership_verified"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None
    assert lifecycle.execution_binding_id == binding_id


def test_reconcile_revives_exited_lifecycle_when_bound_position_is_active(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            pos_id="pos-live",
            status="active",
            payload={
                "draft": {
                    "stop_loss": 62440.0,
                    "take_profit_legs": [{"price": 59588.0}],
                }
            },
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        pos_id="pos-live",
        status="active",
        attribution_status="verified",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                exit_reason="take_profit",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
                exited_at=datetime(2026, 6, 30, 10, 0),
                stop_loss=2,
                take_profit=None,
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-live",
                    "posSide": "long",
                    "pos": "9",
                }
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 6, 30, 10, 5),
    )

    assert result.active == 1
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None
    assert lifecycle.stop_loss == 62440
    assert lifecycle.take_profit == "59588"


def test_reconcile_uses_order_history_to_pick_position_when_symbol_side_ambiguous(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id="order-filled", client_order_id="client-filled", status="open"),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 30, 9, 0),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "order-filled",
                    "posSide": "long",
                    "pos": "9",
                    "avgPx": "68100",
                    "cTime": "100000",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-other",
                    "posSide": "long",
                    "pos": "9",
                    "avgPx": "69000",
                    "cTime": "100100",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-filled",
                    "clOrdId": "",
                    "state": "filled",
                    "avgPx": "68100",
                    "fillSz": "9",
                    "fillTime": "100000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.active == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()

    assert binding.status == "active"
    assert binding.pos_id == "order-filled"
    assert binding.last_exchange_status == "position_ownership_verified"


def test_reconcile_updates_matching_order_leg_with_recovered_position_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(order_id="order-filled", client_order_id="client-filled", status="open"),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            strategy_instance_id="deepcoin:100:55:BTC:long",
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="order-filled",
            client_order_id="client-filled",
            status="open",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 6, 30, 9, 0),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "order-filled",
                    "posSide": "long",
                    "pos": "9",
                    "avgPx": "68100",
                    "cTime": "100000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-filled",
                    "clOrdId": "client-filled",
                    "state": "filled",
                    "avgPx": "68100",
                    "fillSz": "9",
                    "fillTime": "100000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 6, 30, 10, 0),
    )

    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.order_id, leg.client_order_id, leg.pos_id, leg.status) for leg in legs] == [
        ("order-filled", "client-filled", "order-filled", "active")
    ]


def test_reconcile_does_not_grandfather_legacy_weak_verified_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(order_id="old-order", pos_id="later-position", status="active"),
    )
    upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id,
            leg_index=1,
            order_id="old-order",
            pos_id="later-position",
            status="active",
            attribution_status="verified",
            attribution_evidence={"evidence_type": "exact_regular_order_id"},
            request={"instId": "BTC-USDT-SWAP", "posSide": "long", "sz": "7"},
        ),
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "later-position",
                    "posSide": "long",
                    "pos": "7",
                    "avgPx": "62900",
                    "cTime": "1784043117000",
                }
            ]

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(session_factory, client=FakeClient())

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.query(ExecutionOrderLeg).one()

    assert binding.pos_id is None
    assert binding.status == "unknown"
    assert leg.pos_id == "later-position"
    assert leg.attribution_status == "attribution_conflict"


def test_reconcile_preserves_verified_position_after_partial_close_size_drift(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            order_id="pos-partial",
            pos_id="pos-partial",
            status="active",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        order_id="pos-partial",
        pos_id="pos-partial",
        status="active",
        attribution_status="verified",
        request={
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "9",
            "px": "63050",
        },
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-partial",
                    "posSide": "long",
                    "pos": "5",
                    "avgPx": "63050",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
                    "cTime": "1784266812000",
                }
            ]

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 17, 11, 30),
    )

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.query(ExecutionOrderLeg).one()

    assert binding.status == "active"
    assert binding.pos_id == "pos-partial"
    assert binding.last_exchange_status == "position_ownership_verified"
    assert leg.status == "active"
    assert leg.pos_id == "pos-partial"
    assert leg.attribution_status == "verified"


def test_reconcile_recovers_prior_verified_position_after_old_conflict(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            order_id="trigger-partial",
            client_order_id="client-partial",
            pos_id=None,
            status="unknown",
        ),
    )
    leg_id = _add_entry_leg(
        session_factory,
        binding_id,
        order_id="trigger-partial",
        client_order_id="client-partial",
        pos_id="pos-partial",
        status="active",
        attribution_status="attribution_conflict",
        request={
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "sz": "9",
            "px": "63050",
        },
    )
    with session_factory() as session:
        session.add(
            PositionAttributionAudit(
                execution_binding_id=binding_id,
                execution_order_leg_id=leg_id,
                venue="deepcoin",
                pos_id="pos-partial",
                event_type="ownership_verified",
                prior_state="unassigned",
                new_state="verified",
                fingerprint="prior-verified-" + "a" * 49,
                evidence_json=json.dumps(
                    {"policy_version": 2, "evidence_type": "unique_trigger_fill"}
                ),
                created_at=datetime(2026, 7, 17, 5, 40),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-partial",
                    "posSide": "long",
                    "pos": "5",
                    "avgPx": "63050",
                    "mgnMode": "cross",
                    "mrgPosition": "split",
                    "cTime": "1784266812000",
                }
            ]

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 17, 11, 30),
    )

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.get(ExecutionOrderLeg, leg_id)

    assert binding.status == "active"
    assert binding.pos_id == "pos-partial"
    assert binding.last_exchange_status == "position_ownership_verified"
    assert leg.status == "active"
    assert leg.pos_id == "pos-partial"
    assert leg.attribution_status == "verified"


def test_reconcile_maps_multiple_current_policy_positions_back_to_matching_order_legs(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            order_id="trigger-1,trigger-2",
            client_order_id="client-1,client-2",
            pos_id="pos-1,pos-2",
            status="stale",
            payload={
                "submitted_orders": [
                    {
                        "leg_index": 1,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-1",
                        "client_order_id": "client-1",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "long",
                            "sz": "7",
                            "triggerPrice": "62900",
                        },
                    },
                    {
                        "leg_index": 2,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-2",
                        "client_order_id": "client-2",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "long",
                            "sz": "8",
                            "triggerPrice": "63050",
                        },
                    },
                ]
            },
        ),
    )
    repair_execution_order_legs_from_binding_payloads(session_factory)
    with session_factory() as session:
        for leg, pos_id in zip(
            session.query(ExecutionOrderLeg).order_by(ExecutionOrderLeg.leg_index).all(),
            ["pos-1", "pos-2"],
            strict=True,
        ):
            leg.pos_id = pos_id
            leg.attribution_status = "verified"
            leg.attribution_evidence_json = (
                '{"policy_version":2,"evidence_type":"verified_by_current_policy"}'
            )
            leg.status = "active"
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-1",
                    "posSide": "long",
                    "pos": "7",
                    "avgPx": "62900",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-2",
                    "posSide": "long",
                    "pos": "8",
                    "avgPx": "63050",
                },
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 5, 12, 0),
    )

    assert result.active == 1
    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert [(leg.leg_index, leg.pos_id, leg.status) for leg in legs] == [
        (1, "pos-1", "active"),
        (2, "pos-2", "active"),
    ]


def test_reconcile_uses_trigger_history_to_pick_position_after_trigger_entry_fills(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            order_id="trigger-entry",
            client_order_id="client-trigger",
            side="short",
            status="open",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="short",
                lifecycle_status="pending_entry",
                signal_at=datetime(2026, 7, 3, 9, 0),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "filled-entry-order",
                    "posSide": "short",
                    "pos": "10",
                    "avgPx": "61351",
                    "cTime": "1782995766000",
                    "uTime": "1782995766000",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "other-short",
                    "posSide": "short",
                    "pos": "10",
                    "avgPx": "61688",
                    "cTime": "1782995900000",
                    "uTime": "1782995900000",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "filled-entry-order",
                    "state": "filled",
                    "side": "sell",
                    "posSide": "short",
                    "avgPx": "61351",
                    "fillSz": "10",
                    "fillTime": "1782995766000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                    {
                        "instId": "BTC-USDT-SWAP",
                        "ordId": "trigger-entry",
                        "side": "sell",
                    "posSide": "short",
                    "sz": "10",
                    "px": "61351",
                    # Deepcoin trigger history may return seconds while cTime/uTime
                    # and position timestamps use milliseconds.
                    "triggerTime": "1782995766",
                    "uTime": "1782995766000",
                    "errorCode": "0",
                }
            ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 3, 9, 5),
    )

    assert result.active == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert binding.pos_id == "filled-entry-order"
    assert binding.last_exchange_status == "position_ownership_verified"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.execution_binding_id == binding.id


def test_reconcile_links_delayed_live_position_through_trigger_child_order_history(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="1001124198560580",
            client_order_id="TKSQ3347E1",
            status="open",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        order_id="1001124198560580",
        client_order_id="TKSQ3347E1",
        status="pending",
        request={
            "instId": "ETH-USDT-SWAP",
            "posSide": "short",
            "side": "sell",
            "price": "1883.0",
            "sz": "4.4",
        },
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=-1002370796392,
                message_id=3347,
                symbol="ETH",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 18, 20, 15, 52),
                entered_at=datetime(2026, 7, 20, 0, 10),
                execution_binding_id=binding_id,
                management_action="expiry_review_requested",
                management_note="上次人工选择继续等待后又超过 3 小时",
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "1001124219349221",
                    "posSide": "short",
                    "pos": "4.4",
                    "avgPx": "1883",
                    # Deepcoin live position can be created after the child
                    # order-history fill time.
                    "cTime": "1784506273000",
                    "uTime": "1784506273000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            assert inst_id == "ETH-USDT-SWAP"
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "1001124219349221",
                    "clOrdId": "",
                    "state": "filled",
                    "side": "sell",
                    "posSide": "short",
                    "avgPx": "1883",
                    "fillPx": "1883",
                    "fillSz": "4.4",
                    "sz": "4.4",
                    "px": "1883",
                    "cTime": "1784506226000",
                    "uTime": "1784506273000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            assert inst_id == "ETH-USDT-SWAP"
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "1001124198560580",
                    "side": "sell",
                    "posSide": "short",
                    "sz": "4.4",
                    "px": "1883",
                    "triggerPx": "1883",
                    # Deepcoin returns this field in seconds for trigger history.
                    "triggerTime": "1784506226",
                    "uTime": "1784506226000",
                    "errorCode": "0",
                }
            ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 20, 0, 20),
    )

    assert result.active == 1
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.query(ExecutionOrderLeg).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.pos_id == "1001124219349221"
    assert binding.last_exchange_status == "position_ownership_verified"
    assert leg.pos_id == "1001124219349221"
    assert leg.status == "active"
    assert leg.attribution_status == "verified"
    assert lifecycle.management_action is None
    assert lifecycle.management_note is None


def test_reconcile_does_not_link_trigger_child_when_order_history_is_ambiguous(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="parent-trigger",
            client_order_id="parent-client",
            status="open",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        order_id="parent-trigger",
        client_order_id="parent-client",
        status="pending",
        request={
            "instId": "ETH-USDT-SWAP",
            "posSide": "short",
            "side": "sell",
            "price": "1883",
            "sz": "4.4",
        },
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "child-a",
                    "posSide": "short",
                    "pos": "4.4",
                    "avgPx": "1883",
                    "cTime": "1784506273000",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "child-b",
                    "posSide": "short",
                    "pos": "4.4",
                    "avgPx": "1883",
                    "cTime": "1784506274000",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "child-a",
                    "state": "filled",
                    "side": "sell",
                    "posSide": "short",
                    "avgPx": "1883",
                    "fillSz": "4.4",
                    "cTime": "1784506226000",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "child-b",
                    "state": "filled",
                    "side": "sell",
                    "posSide": "short",
                    "avgPx": "1883",
                    "fillSz": "4.4",
                    "cTime": "1784506226000",
                },
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "parent-trigger",
                    "side": "sell",
                    "posSide": "short",
                    "sz": "4.4",
                    "px": "1883",
                    "triggerTime": "1784506226",
                    "uTime": "1784506226000",
                    "errorCode": "0",
                }
            ]

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 20, 0, 20),
    )

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.query(ExecutionOrderLeg).one()

    assert binding.pos_id is None
    assert leg.pos_id is None
    assert leg.attribution_status == "unassigned"


def test_reconcile_appends_filled_second_leg_position_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="order-market,order-limit",
            client_order_id="client-market,client-limit",
            pos_id="pos-market",
            status="active",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        order_id="order-market",
        client_order_id="client-market",
        pos_id="pos-market",
        status="active",
        attribution_status="verified",
        request={"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "4.3"},
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        order_id="order-limit",
        client_order_id="client-limit",
        status="open",
        request={"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "6.4"},
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="ETH",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 2, 10, 0),
                entered_at=datetime(2026, 7, 2, 10, 1),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-market",
                    "posSide": "short",
                    "pos": "4.3",
                    "avgPx": "1616.8",
                    "cTime": "100000",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "order-limit",
                    "posSide": "short",
                    "pos": "6.4",
                    "avgPx": "1624.5",
                    "cTime": "160000",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            assert inst_id == "ETH-USDT-SWAP"
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "order-limit",
                    "clOrdId": "client-limit",
                    "state": "filled",
                    "avgPx": "1624.5",
                    "fillSz": "6.4",
                    "fillTime": "160000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 2, 10, 5),
    )

    assert result.active == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()

    assert binding.pos_id == "pos-market,order-limit"
    assert binding.last_exchange_status == "position_ownership_verified"


def test_reconcile_recovers_each_trigger_leg_despite_fill_slippage(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="trigger-37,trigger-42",
            client_order_id="client-37,client-42",
            pos_id="pos-42",
            status="active",
            payload={
                "submitted_orders": [
                    {
                        "leg_index": 1,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-37",
                        "client_order_id": "client-37",
                        "request": {
                            "instId": "ETH-USDT-SWAP",
                            "posSide": "short",
                            "triggerPx": "1765",
                            "sz": "3.7",
                        },
                    },
                    {
                        "leg_index": 2,
                        "execution_type": "trigger_limit",
                        "order_id": "trigger-42",
                        "client_order_id": "client-42",
                        "request": {
                            "instId": "ETH-USDT-SWAP",
                            "posSide": "short",
                            "triggerPx": "1767.5",
                            "sz": "4.2",
                        },
                    },
                ]
            },
        ),
    )
    repair_execution_order_legs_from_binding_payloads(session_factory)

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-37",
                    "posSide": "short",
                    "pos": "3.7",
                    "avgPx": "1767.18",
                    "uTime": "1783648112000",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-42",
                    "posSide": "short",
                    "pos": "4.2",
                    "avgPx": "1769.13",
                    "uTime": "1783648113000",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            assert inst_id == "ETH-USDT-SWAP"
            return [
                    {
                        "instId": "ETH-USDT-SWAP",
                        "ordId": "trigger-37",
                        "state": "filled",
                        "posSide": "short",
                    "side": "sell",
                    "sz": "3.7",
                    "px": "1765",
                        "triggerTime": "1783648112000",
                        "errorCode": "0",
                },
                    {
                        "instId": "ETH-USDT-SWAP",
                        "ordId": "trigger-42",
                        "state": "filled",
                        "posSide": "short",
                    "side": "sell",
                    "sz": "4.2",
                    "px": "1767.5",
                        "triggerTime": "1783648113000",
                        "errorCode": "0",
                },
            ]

    reconcile_deepcoin_execution_bindings(session_factory, client=FakeClient())

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)

    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert binding.pos_id == "pos-37,pos-42"
    assert [(leg.order_id, leg.pos_id) for leg in legs] == [
        ("trigger-37", "pos-37"),
        ("trigger-42", "pos-42"),
    ]


def test_reconcile_marks_conflict_instead_of_reassigning_existing_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    wrong_binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            chat_id=99,
            message_id=1,
            symbol="ETH",
            side="short",
            order_id="old-trigger",
            client_order_id="old-client",
            pos_id="pos-37",
            status="active",
            payload={
                "submitted_orders": [
                    {
                        "leg_index": 1,
                        "order_id": "old-trigger",
                        "client_order_id": "old-client",
                    }
                ]
            },
        ),
    )
    correct_binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            chat_id=100,
            message_id=2,
            symbol="ETH",
            side="short",
            order_id="current-trigger",
            client_order_id="current-client",
            pos_id=None,
            status="open",
            payload={
                "submitted_orders": [
                    {
                        "leg_index": 1,
                        "order_id": "current-trigger",
                        "client_order_id": "current-client",
                    }
                ]
            },
        ),
    )
    repair_execution_order_legs_from_binding_payloads(session_factory)
    with session_factory() as session:
        wrong_leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == wrong_binding_id)
            .one()
        )
        wrong_leg.pos_id = "pos-37"
        wrong_leg.attribution_status = "unassigned"
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-37",
                    "posSide": "short",
                    "pos": "3.7",
                    "avgPx": "1767.18",
                    "uTime": "1783648112000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            assert inst_id == "ETH-USDT-SWAP"
            return [
                    {
                        "instId": "ETH-USDT-SWAP",
                        "ordId": "old-trigger",
                        "state": "filled",
                        "posSide": "short",
                    "side": "sell",
                    "sz": "3.7",
                    "px": "1790",
                        "triggerTime": "1783354072000",
                        "errorCode": "0",
                },
                    {
                        "instId": "ETH-USDT-SWAP",
                        "ordId": "current-trigger",
                        "state": "filled",
                        "posSide": "short",
                    "side": "sell",
                    "sz": "3.7",
                    "px": "1765",
                        "triggerTime": "1783648112000",
                        "errorCode": "0",
                },
            ]

    reconcile_deepcoin_execution_bindings(session_factory, client=FakeClient())

    with session_factory() as session:
        wrong_binding = session.get(ExecutionBinding, wrong_binding_id)
        correct_binding = session.get(ExecutionBinding, correct_binding_id)

    assert wrong_binding.pos_id is None
    assert correct_binding.pos_id is None
    wrong_legs = list_execution_order_legs(session_factory, execution_binding_id=wrong_binding_id)
    correct_legs = list_execution_order_legs(session_factory, execution_binding_id=correct_binding_id)
    assert wrong_legs[0].pos_id == "pos-37"
    assert correct_legs[0].pos_id is None
    assert wrong_legs[0].attribution_status == "attribution_conflict"
    assert correct_legs[0].attribution_status == "attribution_conflict"
    with session_factory() as session:
        incidents = (
            session.query(PositionAttributionAudit)
            .filter(PositionAttributionAudit.new_state == "attribution_conflict")
            .all()
        )
    assert len(incidents) == 1
    assert incidents[0].notification_status == "pending"


def test_reconcile_does_not_reopen_manually_exited_strategy_when_old_leg_fills(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="filled-trigger,pending-trigger",
            client_order_id="filled-client,pending-client",
            pos_id="old-pos",
            status="closed",
            payload={
                "submitted_orders": [
                    {
                        "leg_index": 1,
                        "execution_type": "trigger_limit",
                        "order_id": "filled-trigger",
                        "client_order_id": "filled-client",
                        "request": {
                            "instId": "ETH-USDT-SWAP",
                            "posSide": "short",
                            "triggerPx": "1760",
                            "sz": "2.5",
                        },
                    },
                    {
                        "leg_index": 2,
                        "execution_type": "trigger_limit",
                        "order_id": "pending-trigger",
                        "client_order_id": "pending-client",
                        "request": {
                            "instId": "ETH-USDT-SWAP",
                            "posSide": "short",
                            "triggerPx": "1766",
                            "sz": "3.9",
                        },
                    },
                ]
            },
        ),
    )
    repair_execution_order_legs_from_binding_payloads(session_factory)
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="ETH",
                side="short",
                lifecycle_status="exited",
                exit_reason="manual",
                signal_at=datetime(2026, 7, 8, 3, 44),
                exited_at=datetime(2026, 7, 8, 7, 6),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "new-pos",
                    "posSide": "short",
                    "pos": "3.9",
                    "avgPx": "1767.03",
                    "uTime": "1783648112000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            assert inst_id == "ETH-USDT-SWAP"
            return [
                    {
                        "instId": "ETH-USDT-SWAP",
                        "ordId": "pending-trigger",
                        "state": "filled",
                        "posSide": "short",
                    "side": "sell",
                    "sz": "3.9",
                    "px": "1766",
                        "triggerTime": "1783648112000",
                        "errorCode": "0",
                }
            ]

    reconcile_deepcoin_execution_bindings(session_factory, client=FakeClient())

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.query(StrategyLifecycle).one()

    legs = list_execution_order_legs(session_factory, execution_binding_id=binding_id)
    assert binding.status == "closed"
    assert binding.pos_id == "old-pos"
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "manual"
    assert [(leg.order_id, leg.pos_id) for leg in legs] == [
        ("filled-trigger", None),
        ("pending-trigger", None),
    ]
    assert [leg.status for leg in legs] == ["manually_closed", "manually_closed"]
    assert [leg.terminal_reason for leg in legs] == [
        "manual_lifecycle_terminal",
        "manual_lifecycle_terminal",
    ]
    assert [leg.attribution_status for leg in legs] == ["unassigned", "unassigned"]


def test_reconcile_manual_lifecycle_terminalizes_legacy_backfilled_closed_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="legacy-order",
            client_order_id="legacy-client",
            status="closed",
            last_exchange_status="manual_closed_by_user",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="ETH",
                side="short",
                lifecycle_status="exited",
                exit_reason="manual",
                signal_at=datetime(2026, 7, 8, 3, 44),
                exited_at=datetime(2026, 7, 8, 7, 6),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class EmptySnapshotClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=EmptySnapshotClient(),
        recovered_at=datetime(2026, 7, 15, 10, 30),
    )

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.query(ExecutionOrderLeg).filter_by(execution_binding_id=binding_id).one()
        lifecycle = session.query(StrategyLifecycle).one()
        assert (binding.status, binding.last_exchange_status) == (
            "closed",
            "manual_closed_by_user",
        )
        assert (lifecycle.lifecycle_status, lifecycle.exit_reason) == ("exited", "manual")
        assert (leg.status, leg.terminal_reason) == (
            "manually_closed",
            "manual_lifecycle_terminal",
        )
        assert leg.pos_id is None
        assert leg.attribution_status == "unassigned"


def test_reconcile_recovers_second_leg_when_first_leg_is_no_longer_active(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="ETH",
            side="short",
            order_id="order-market,order-limit",
            client_order_id="client-market,client-limit",
            pos_id="pos-market",
            status="active",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        order_id="order-market",
        client_order_id="client-market",
        status="manually_closed",
        request={"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "4.3"},
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        order_id="order-limit",
        client_order_id="client-limit",
        status="open",
        request={"instId": "ETH-USDT-SWAP", "posSide": "short", "sz": "6.4"},
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="ETH",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 2, 10, 0),
                entered_at=datetime(2026, 7, 2, 10, 1),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "order-limit",
                    "posSide": "short",
                    "pos": "6.4",
                    "avgPx": "1624.5",
                    "cTime": "160000",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            assert inst_id == "ETH-USDT-SWAP"
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "order-limit",
                    "clOrdId": "client-limit",
                    "state": "filled",
                    "avgPx": "1624.5",
                    "fillSz": "6.4",
                    "fillTime": "160000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 2, 10, 5),
    )

    assert result.active == 1
    assert result.stale == 0
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert binding.pos_id == "order-limit"
    assert lifecycle.lifecycle_status == "entered"


def test_reconcile_does_not_revive_cancelled_legs_when_similar_positions_appear(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="BTC",
            side="short",
            order_id="order-a,order-b",
            client_order_id="client-a,client-b",
            pos_id=None,
            status="stale",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        order_id="order-a",
        client_order_id="client-a",
        status="manually_cancelled",
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        order_id="order-b",
        client_order_id="client-b",
        status="manually_cancelled",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="short",
                lifecycle_status="exited",
                exit_reason="cancelled",
                signal_at=datetime(2026, 7, 2, 10, 0),
                exited_at=datetime(2026, 7, 2, 10, 5),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-a",
                    "posSide": "short",
                    "pos": "25",
                    "avgPx": "60950",
                    "cTime": "200000",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-b",
                    "posSide": "short",
                    "pos": "25",
                    "avgPx": "60950",
                    "cTime": "200000",
                },
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-a",
                    "clOrdId": "client-a",
                    "state": "filled",
                    "avgPx": "60950",
                    "fillSz": "25",
                    "fillTime": "200000",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-b",
                    "clOrdId": "client-b",
                    "state": "filled",
                    "avgPx": "60950",
                    "fillSz": "25",
                    "fillTime": "200000",
                },
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 2, 10, 10),
    )

    assert result.active == 0
    assert result.stale == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "closed"
    assert binding.pos_id is None
    assert binding.last_exchange_status == "entry_legs_terminal"
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "cancelled"


def test_reconcile_reopens_legacy_kol_exit_while_bound_position_is_still_active(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            symbol="BTC",
            side="long",
            order_id="order-a",
            client_order_id="client-a",
            pos_id="pos-a",
            status="active",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        order_id="order-a",
        client_order_id="client-a",
        pos_id="pos-a",
        status="active",
        attribution_status="verified",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                exit_reason="kol_signal",
                signal_at=datetime(2026, 7, 7, 4, 38),
                entered_at=datetime(2026, 7, 7, 14, 36),
                exited_at=datetime(2026, 7, 7, 22, 49),
                exit_signal_message_id=3870,
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-a",
                    "posSide": "long",
                    "pos": "7",
                    "avgPx": "62600",
                    "cTime": "200000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 9, 12, 30),
    )

    assert result.active == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert lifecycle.execution_binding_id == binding.id
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None
    assert lifecycle.exit_signal_message_id == 3870
    assert lifecycle.management_signal_message_id == 3870
    assert lifecycle.management_action == "exit_requested"


def test_reconcile_revives_expired_keep_order_when_position_fills_later(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            symbol="BTC",
            side="short",
            order_id="order-keep",
            client_order_id="client-keep",
            pos_id=None,
            status="open",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="short",
                lifecycle_status="expired",
                exit_reason="expired",
                signal_at=datetime(2026, 7, 2, 10, 0),
                exited_at=datetime(2026, 7, 2, 16, 0),
                management_action="expiry_expired_keep_order",
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "order-keep",
                    "posSide": "short",
                    "pos": "25",
                    "avgPx": "60950",
                    "cTime": "200000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "order-keep",
                    "clOrdId": "client-keep",
                    "state": "filled",
                    "avgPx": "60950",
                    "fillSz": "25",
                    "fillTime": "200000",
                }
            ]

        def list_trade_fills(self, *, inst_id=None):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 3, 10, 10),
    )

    assert result.active == 1
    assert result.stale == 0
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "active"
    assert binding.pos_id == "order-keep"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None
    assert lifecycle.execution_binding_id == binding.id


def test_reconcile_does_not_guess_position_id_when_ambiguous(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(order_id="order-1", client_order_id="client-1", status="open"),
    )
    upsert_execution_binding(
        session_factory,
        _binding(
            chat_id=101,
            message_id=56,
            order_id="order-2",
            client_order_id="client-2",
            status="open",
        ),
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-a",
                    "posSide": "long",
                    "pos": "9",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-b",
                    "posSide": "long",
                    "pos": "9",
                },
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
    )

    assert result.stale == 2
    with session_factory() as session:
        rows = session.query(ExecutionBinding).order_by(ExecutionBinding.chat_id).all()

    assert [row.pos_id for row in rows] == [None, None]


def test_reconcile_does_not_use_submitted_order_payload_as_ownership_proof(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            kol_id="group:-1002370796392",
            chat_id=-1002370796392,
            message_id=3240,
            symbol="BTC",
            side="short",
            order_id="1001123853022859,1001123853022867",
            client_order_id="TKSQ3240E1,TKSQ3240E2",
            status="open",
            payload={
                "submitted_orders": [
                    {
                        "client_order_id": "TKSQ3240E1",
                        "execution_type": "trigger_limit",
                        "leg_index": 1,
                        "order_id": "1001123853022859",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "short",
                            "price": "62300.0",
                            "triggerPrice": "62300.0",
                            "sz": "12.0",
                        },
                    },
                    {
                        "client_order_id": "TKSQ3240E2",
                        "execution_type": "trigger_limit",
                        "leg_index": 2,
                        "order_id": "1001123853022867",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "short",
                            "price": "62500.0",
                            "triggerPrice": "62500.0",
                            "sz": "16.0",
                        },
                    },
                ]
            },
        ),
    )
    upsert_execution_binding(
        session_factory,
        _binding(
            chat_id=-1003825498321,
            message_id=442,
            symbol="BTC",
            side="short",
            order_id="other-order",
            client_order_id="other-client",
            status="open",
        ),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=-1002370796392,
                message_id=3240,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 2, 13, 20, 5),
                entered_at=datetime(2026, 7, 3, 15, 51, 47),
                entry_range_low=62300,
                entry_range_high=62700,
                stop_loss=63100,
                take_profit="61500/60800/60000",
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "1001123877920316",
                    "posSide": "short",
                    "pos": "12",
                    "avgPx": "62300.0",
                }
            ]

        def list_open_orders(self):
            return []

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 3, 16, 0),
    )

    assert result.active == 0
    assert result.stale == 2
    with session_factory() as session:
        recovered = (
            session.query(ExecutionBinding)
            .filter_by(chat_id=-1002370796392, message_id=3240)
            .one()
        )
        other = session.query(ExecutionBinding).filter_by(message_id=442).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert recovered.status == "stale"
    assert recovered.pos_id is None
    assert recovered.last_exchange_status == "position_ownership_unassigned"
    assert lifecycle.execution_binding_id is None
    assert other.pos_id is None


def test_reconcile_keeps_pending_leg_open_without_fill_evidence_for_live_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(
            chat_id=-1002370796392,
            message_id=3240,
            symbol="BTC",
            side="short",
            order_id="filled-trigger,open-trigger",
            client_order_id="filled-client,open-client",
            status="open",
            payload={
                "submitted_orders": [
                    {
                        "order_id": "filled-trigger",
                        "client_order_id": "filled-client",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "short",
                            "price": "62300",
                            "triggerPrice": "62300",
                            "sz": "12",
                        },
                    },
                    {
                        "order_id": "open-trigger",
                        "client_order_id": "open-client",
                        "request": {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "short",
                            "price": "62500",
                            "triggerPrice": "62500",
                            "sz": "16",
                        },
                    },
                ]
            },
        ),
    )

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "filled-pos",
                    "posSide": "short",
                    "pos": "12",
                    "avgPx": "62300",
                }
            ]

        def list_open_orders(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "ordId": "open-trigger",
                    "clOrdId": "open-client",
                    "state": "live",
                }
            ]

    result = reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 7, 3, 16, 5),
    )

    assert result.active == 0
    assert result.open == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()

    assert binding.status == "open"
    assert binding.pos_id is None
    assert binding.last_exchange_status == "entry_order_pending"


@pytest.mark.parametrize("binding_status", ["active", "stale"])
def test_sync_manual_closed_positions_closes_missing_bound_position(tmp_path, binding_status):
    session_factory = create_session_factory(tmp_path / "research.db")
    upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-closed", status=binding_status),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.checked == 1
    assert result.manually_closed == 1
    with session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "closed"
    assert binding.last_exchange_status == "manual_closed_or_not_found_on_exchange"
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "manual"


@pytest.mark.parametrize(
    "live_position",
    [
        {"PositionID": "pos-live-alias", "pos": "1"},
        {
            "PositionID": "pos-live-alias",
            "positionId": "different-position",
            "pos": "1",
        },
        {"PositionID": "pos-live-alias", "sz": "1"},
        {
            "PositionID": "pos-live-alias",
            "pos": "0",
            "positionSize": "1",
        },
        {"PositionID": "pos-live-alias", "pos": "not-a-number"},
        {"PositionID": "pos-live-alias"},
    ],
)
def test_sync_manual_closed_positions_keeps_supported_live_position_aliases(
    tmp_path,
    live_position,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-live-alias", status="active"),
    )
    leg_id = _add_entry_leg(
        session_factory,
        binding_id,
        pos_id="pos-live-alias",
        status="active",
        attribution_status="verified",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 8, 10, 4, 0),
                entered_at=datetime(2026, 8, 10, 4, 1),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [live_position]

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=datetime(2026, 8, 10, 4, 2),
    )

    assert result.manually_closed == 0
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.get(ExecutionOrderLeg, leg_id)
        lifecycle = session.query(StrategyLifecycle).one()
    assert binding.status == "active"
    assert leg.status == "active"
    assert lifecycle.lifecycle_status == "entered"


@pytest.mark.parametrize("evidence_case", ["proven", "ambiguous", "unproven"])
def test_sync_missing_position_attributes_verified_take_profit_close(
    tmp_path, evidence_case
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-tp-closed", status="active", side="short"),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        pos_id="pos-tp-closed",
        status="active",
        attribution_status="verified",
    )
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        leg.response_json = json.dumps({"posId": "pos-tp-closed"})
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding_id,
                execution_order_leg_id=leg.id,
                strategy_instance_id="deepcoin:100:55:BTC:short",
                pos_id="pos-tp-closed",
                instrument_id="BTC-USDT-SWAP",
                side="short",
                order_id="tp-owned",
                purpose="take_profit",
                trigger_price="63800",
                size_text="4",
                status="verified",
                evidence_source="test_exact_owner",
                evidence_json="{}",
                last_verified_at=datetime(2026, 7, 31, 8, 39),
            )
        )
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 31, 3, 20),
                entered_at=datetime(2026, 7, 31, 3, 23),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

        def list_position_history(self, *, inst_id, pos_id=None):
            assert inst_id == "BTC-USDT-SWAP"
            assert pos_id == "pos-tp-closed"
            return [
                {
                    "posId": "pos-tp-closed",
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "short",
                    "pos": "8",
                    "closePos": "8",
                    "uTime": "1785487255000",
                }
            ]

        def list_trigger_order_history(self, *, inst_id):
            return [
                {
                    "ordId": "tp-owned",
                    "instId": inst_id,
                    "posSide": "short",
                    "px": "0",
                    "tpTriggerPrice": "63800",
                    "triggerTime": (
                        "1785487255000" if evidence_case != "unproven" else "0"
                    ),
                    "errorCode": "0",
                    "uTime": "1785487255000",
                }
            ]

        def list_order_history(self, *, inst_id=None):
            row = {
                "ordId": "trigger-child-close",
                "instId": inst_id,
                "posSide": "short",
                "side": "buy",
                "reduceOnly": "true",
                "state": "filled",
                "fillPx": "63800",
                "fillSz": "4",
                "uTime": "1785487255000",
            }
            return [
                row,
                *(
                    [{**row, "ordId": "ambiguous-second-child"}]
                    if evidence_case == "ambiguous"
                    else []
                ),
            ]

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=datetime(2026, 7, 31, 8, 41),
    )

    assert result.manually_closed == 1
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.query(StrategyLifecycle).one()
        leg = session.query(ExecutionOrderLeg).one()
    assert binding.status == "closed"
    assert binding.last_exchange_status == (
        "take_profit_closed_on_exchange"
        if evidence_case == "proven"
        else "manual_closed_or_not_found_on_exchange"
    )
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == (
        "take_profit" if evidence_case == "proven" else "manual"
    )
    assert leg.status == (
        "closed" if evidence_case == "proven" else "manually_closed"
    )
    assert leg.terminal_reason == (
        "take_profit_position_closed"
        if evidence_case == "proven"
        else "manual_position_missing"
    )


def test_reconcile_then_sync_closes_a_previously_verified_missing_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-manually-closed", status="active"),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        pos_id="pos-manually-closed",
        status="active",
        attribution_status="verified",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

        def list_open_orders(self):
            return []

    client = FakeClient()
    reconcile_deepcoin_execution_bindings(
        session_factory, client=client, recovered_at=datetime(2026, 6, 30, 9, 59)
    )
    result = sync_manual_closed_deepcoin_positions(
        session_factory, client=client, synced_at=datetime(2026, 6, 30, 10, 0)
    )

    assert result.manually_closed == 1
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.query(ExecutionOrderLeg).filter_by(execution_binding_id=binding_id).one()
        lifecycle = session.query(StrategyLifecycle).one()
    assert binding.status == "closed"
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "manual"
    assert leg.status == "manually_closed"
    assert leg.terminal_reason == "manual_position_missing"


def test_confirmed_close_mutation_converges_reservation_without_closing_live_sibling(tmp_path):
    session_factory = create_session_factory(tmp_path / "close-reservation.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            strategy_instance_id="deepcoin:100:55:BTC:long",
            pos_id="pos-closed,pos-live", status="active",
        ),
    )
    _add_entry_leg(
        session_factory, binding_id, leg_index=1, pos_id="pos-closed",
        status="active", attribution_status="verified",
    )
    _add_entry_leg(
        session_factory, binding_id, leg_index=2, order_id="order-2",
        client_order_id="client-2", pos_id="pos-live", status="active",
        attribution_status="verified",
    )
    now = datetime(2026, 8, 3, 8, 0)
    with session_factory() as session:
        closed_leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-closed").one()
        closed_leg.response_json = json.dumps({"posId": "pos-closed"})
        session.add(BoundPositionCloseReservation(
            pos_id="pos-closed", execution_binding_id=binding_id, status="submitted",
        ))
        session.add(PositionMutationIntent(
            idempotency_key="close-pos-closed", venue="deepcoin",
            operation="close_position", strategy_instance_id="deepcoin:100:55:BTC:long",
            execution_binding_id=binding_id, execution_order_leg_id=closed_leg.id,
            pos_id="pos-closed", authority_fingerprint="a" * 64,
            request_fingerprint="b" * 64, status="submitted", request_json="{}",
            reserved_at=now, submitted_at=now,
        ))
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [{"posId": "pos-live", "pos": "2"}]

        def list_position_history(self, *, inst_id, pos_id=None):
            assert pos_id == "pos-closed"
            return []

    sync_manual_closed_deepcoin_positions(
        session_factory, client=FakeClient(), synced_at=now,
    )
    with session_factory() as session:
        closed_leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-closed").one()
        reservation = session.query(BoundPositionCloseReservation).one()
        mutation = session.query(PositionMutationIntent).one()
        assert closed_leg.attribution_status == "evidence_unavailable"
        assert reservation.status == "submitted"
        mutation.status = "confirmed"
        mutation.confirmed_at = now + timedelta(minutes=1)
        session.commit()
    sync_manual_closed_deepcoin_positions(
        session_factory, client=FakeClient(), synced_at=now + timedelta(minutes=1),
    )

    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        closed_leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-closed").one()
        live_leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-live").one()
        reservation = session.query(BoundPositionCloseReservation).one()
    assert (closed_leg.status, closed_leg.terminal_reason) == (
        "manually_closed", "manual_position_missing",
    )
    assert reservation.status == "confirmed"
    assert binding.status == "active"
    assert live_leg.status == "active"


def test_reconcile_records_complete_owned_position_observation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            pos_id="pos-observed",
            status="active",
            strategy_instance_id="deepcoin:100:55:BTC:long",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        pos_id="pos-observed",
        status="active",
        attribution_status="verified",
    )

    class FakeClient:
        def list_positions(self):
            return [{
                "posId": "pos-observed",
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "pos": "7",
                "avgPx": "64000",
            }]

        def list_open_orders(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return [{
                "ordId": "tp-observed",
                "instId": inst_id,
                "posId": "pos-observed",
                "posSide": "long",
                "side": "sell",
                "sz": "3",
                "triggerPx": "65000",
            }]

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            return []

    reconcile_deepcoin_execution_bindings(
        session_factory,
        client=FakeClient(),
        recovered_at=datetime(2026, 8, 2, 8, 0),
    )

    with session_factory() as session:
        row = session.query(PositionReconciliationObservation).one()
        assert row.pos_id == "pos-observed"
        assert row.size_text == "7"
        assert row.avg_entry_price == "64000"
        assert row.snapshot_complete is True
        assert [
            item["order_id"] for item in json.loads(row.pending_tpsl_json)
        ] == ["tp-observed"]


def test_sync_missing_position_cleans_pending_entry_before_lifecycle_exit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            pos_id="pos-manually-closed",
            status="active",
            order_id="entry-1,entry-2",
            client_order_id="client-1,client-2",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        order_id="entry-1",
        client_order_id="client-1",
        pos_id="pos-manually-closed",
        status="active",
        attribution_status="verified",
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        order_id="entry-2",
        client_order_id="client-2",
        pos_id=None,
        status="pending",
        attribution_status="unassigned",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 30, 1, 0),
                entered_at=datetime(2026, 7, 30, 1, 1),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def __init__(self):
            self.trigger_orders = [
                {
                    "ordId": "entry-2",
                    "clOrdId": "client-2",
                    "instId": "BTC-USDT-SWAP",
                }
            ]
            self.cancelled = []
            self.trigger_history = []

        def list_positions(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return list(self.trigger_orders)

        def list_open_orders(self, *, inst_id=None):
            return []

        def cancel_trigger_order(self, payload):
            self.cancelled.append(payload["ordId"])
            self.trigger_orders = []
            self.trigger_history.append(
                {"ordId": payload["ordId"], "state": "canceled"}
            )
            return {"code": "0"}

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return list(self.trigger_history)

        def list_trade_fills(self, *, inst_id=None):
            return []

    client = FakeClient()
    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=client,
        synced_at=datetime(2026, 7, 30, 2, 0),
    )

    assert client.cancelled == ["entry-2"]
    assert result.manually_closed == 1
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.query(StrategyLifecycle).one()
        legs = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id)
            .order_by(ExecutionOrderLeg.leg_index)
            .all()
        )
        assert binding.status == "closed"
        assert lifecycle.lifecycle_status == "exited"
        assert [(leg.status, leg.terminal_reason) for leg in legs] == [
            ("manually_closed", "manual_position_missing"),
            ("cancelled", "terminal_entry_cleanup_confirmed"),
        ]


def test_sync_manual_closed_positions_disables_exchange_mutations_explicitly(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            pos_id="pos-reconcile-only",
            status="active",
            order_id="entry-filled,entry-pending",
            client_order_id="client-filled,client-pending",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        order_id="entry-filled",
        client_order_id="client-filled",
        pos_id="pos-reconcile-only",
        status="active",
        attribution_status="verified",
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        order_id="entry-pending",
        client_order_id="client-pending",
        pos_id=None,
        status="pending",
        attribution_status="unassigned",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 30, 1, 0),
                entered_at=datetime(2026, 7, 30, 1, 1),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class MutationTrapClient:
        def __init__(self):
            self.cancel_calls = 0

        def list_positions(self, *, inst_id=None):
            return []

        def cancel_trigger_order(self, _payload):
            self.cancel_calls += 1
            raise AssertionError("exchange cancellation must be unreachable")

        def cancel_order(self, _payload):
            self.cancel_calls += 1
            raise AssertionError("exchange cancellation must be unreachable")

    client = MutationTrapClient()
    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=client,
        synced_at=datetime(2026, 7, 30, 2, 0),
        allow_exchange_mutations=False,
    )

    assert client.cancel_calls == 0
    assert result.checked == 1
    assert result.manually_closed == 0
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.query(StrategyLifecycle).one()
        legs = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id)
            .order_by(ExecutionOrderLeg.leg_index)
            .all()
        )
        assert binding.status == "open"
        assert binding.last_exchange_status == "entry_legs_pending_after_position_closed"
        assert lifecycle.lifecycle_status == "entered"
        assert [(leg.status, leg.terminal_reason) for leg in legs] == [
            ("active", None),
            ("pending", None),
        ]


def test_missing_position_history_uncertainty_does_not_skip_pending_order_cancel(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            pos_id="pos-history-unknown",
            status="active",
            order_id="entry-filled,entry-pending",
            client_order_id="client-filled,client-pending",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        order_id="entry-filled",
        client_order_id="client-filled",
        pos_id="pos-history-unknown",
        status="active",
        attribution_status="attribution_conflict",
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        order_id="entry-pending",
        client_order_id="client-pending",
        pos_id=None,
        status="pending",
        attribution_status="unassigned",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 30, 1, 0),
                entered_at=datetime(2026, 7, 30, 1, 1),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class Client:
        def __init__(self):
            self.pending = [
                {
                    "ordId": "entry-pending",
                    "clOrdId": "client-pending",
                }
            ]
            self.history = []
            self.cancel_calls = 0

        def list_positions(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return list(self.pending)

        def list_open_orders(self, *, inst_id=None):
            return []

        def cancel_trigger_order(self, payload):
            self.cancel_calls += 1
            self.pending = []
            self.history = [{"ordId": "entry-pending", "state": "canceled"}]
            return {"code": "0"}

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return list(self.history)

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_position_history(self, *, inst_id, pos_id):
            return []

    client = Client()
    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=client,
        synced_at=datetime(2026, 7, 30, 2, 0),
    )

    assert client.cancel_calls == 1
    assert result.manually_closed == 0
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).one()
        pending_leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-pending")
            .one()
        )
        assert pending_leg.status == "cancelled"
        assert lifecycle.lifecycle_status == "entered"


def test_sync_manual_closed_positions_terminalizes_only_missing_verified_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-live,pos-closed", status="active", symbol="ETH"),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        pos_id="pos-live",
        status="active",
        attribution_status="verified",
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        pos_id="pos-closed",
        status="active",
        attribution_status="verified",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="ETH",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-live",
                    "posSide": "long",
                    "pos": "4.2",
                }
            ]

        def list_position_history(self, *, inst_id, pos_id):
            assert (inst_id, pos_id) == ("ETH-USDT-SWAP", "pos-closed")
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-closed",
                    "posSide": "long",
                    "pos": "3.7",
                    "closePos": "3.7",
                }
            ]

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.checked == 1
    assert result.manually_closed == 0
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.query(StrategyLifecycle).one()
        legs = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id)
            .order_by(ExecutionOrderLeg.leg_index.asc())
            .all()
        )

    assert binding.status == "active"
    assert binding.last_exchange_status == "position_ownership_verified"
    assert lifecycle.lifecycle_status == "entered"
    assert [(leg.pos_id, leg.status, leg.terminal_reason) for leg in legs] == [
        ("pos-live", "active", None),
        ("pos-closed", "manually_closed", "manual_position_missing"),
    ]


def test_sync_manual_closed_positions_skips_weak_verified_missing_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-live,pos-closed", status="active", symbol="ETH"),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        pos_id="pos-live",
        status="active",
        attribution_status="verified",
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        pos_id="pos-closed",
        status="active",
        attribution_status="verified",
    )
    with session_factory() as session:
        weak_leg = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id, pos_id="pos-closed")
            .one()
        )
        weak_leg.attribution_evidence_json = json.dumps(
            {"evidence_type": "exact_regular_order_id"}
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-live",
                    "posSide": "long",
                    "pos": "4.2",
                }
            ]

        def list_position_history(self, *, inst_id, pos_id):
            assert (inst_id, pos_id) == ("ETH-USDT-SWAP", "pos-closed")
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-closed",
                    "posSide": "long",
                    "pos": "3.7",
                    "closePos": "3.7",
                }
            ]

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.checked == 1
    assert result.partial_legs_closed == 0
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        weak_leg = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id, pos_id="pos-closed")
            .one()
        )
    assert binding.status == "active"
    assert binding.pos_id == "pos-live,pos-closed"
    assert weak_leg.status == "active"
    assert weak_leg.terminal_reason is None


def test_sync_manual_closed_positions_terminalizes_exited_conflict_legs_with_exact_history(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            pos_id=None,
            status="unknown",
            order_id="order-1,order-2",
            client_order_id="client-1,client-2",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        order_id="order-1",
        client_order_id="client-1",
        pos_id="pos-closed-1",
        status="active",
        attribution_status="attribution_conflict",
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        order_id="order-2",
        client_order_id="client-2",
        pos_id="pos-closed-2",
        status="active",
        attribution_status="attribution_conflict",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                exit_reason="kol_signal",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
                exited_at=datetime(2026, 6, 30, 9, 30),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

        def list_position_history(self, *, inst_id, pos_id):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": pos_id,
                    "posSide": "long",
                    "pos": "7",
                    "closePos": "7",
                }
            ]

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.checked == 1
    assert result.manually_closed == 1
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.query(StrategyLifecycle).one()
        legs = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id)
            .order_by(ExecutionOrderLeg.leg_index.asc())
            .all()
        )

    assert binding.status == "closed"
    assert binding.pos_id is None
    assert binding.last_exchange_status == "entry_legs_terminal"
    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert [(leg.status, leg.terminal_reason) for leg in legs] == [
        ("manually_closed", "manual_position_missing"),
        ("manually_closed", "manual_position_missing"),
    ]
    assert [leg.attribution_status for leg in legs] == [
        "attribution_conflict",
        "attribution_conflict",
    ]


def test_sync_manual_closed_positions_keeps_unknown_legacy_binding_without_entry_legs(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="legacy-pos", status="unknown"),
    )

    class FakeClient:
        def list_positions(self):
            return []

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.checked == 1
    assert result.manually_closed == 0
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)

    assert binding.status == "unknown"
    assert binding.pos_id == "legacy-pos"


def test_sync_repairs_terminal_lifecycle_with_pending_entry_leg_exactly_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            pos_id=None,
            status="unknown",
            order_id="entry-1,entry-2",
            client_order_id="client-1,client-2",
        ),
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=1,
        order_id="entry-1",
        client_order_id="client-1",
        pos_id="closed-pos",
        status="filled",
        attribution_status="attribution_conflict",
    )
    _add_entry_leg(
        session_factory,
        binding_id,
        leg_index=2,
        order_id="entry-2",
        client_order_id="client-2",
        pos_id=None,
        status="pending",
        attribution_status="unassigned",
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                exit_reason="take_profit",
                signal_at=datetime(2026, 7, 27, 0, 17),
                entered_at=datetime(2026, 7, 27, 0, 18),
                exited_at=datetime(2026, 7, 27, 14, 37),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def __init__(self):
            self.trigger_orders = [
                {
                    "ordId": "entry-2",
                    "clOrdId": "client-2",
                    "instId": "BTC-USDT-SWAP",
                }
            ]
            self.cancel_calls = 0
            self.trigger_history = []

        def list_positions(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return list(self.trigger_orders)

        def list_open_orders(self, *, inst_id=None):
            return []

        def cancel_trigger_order(self, payload):
            self.cancel_calls += 1
            self.trigger_orders = []
            self.trigger_history.append(
                {"ordId": payload["ordId"], "state": "canceled"}
            )
            return {"code": "0"}

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return list(self.trigger_history)

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_position_history(self, *, inst_id, pos_id):
            return []

    client = FakeClient()
    first = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=client,
        synced_at=datetime(2026, 7, 30, 2, 0),
    )
    second = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=client,
        synced_at=datetime(2026, 7, 30, 2, 1),
    )

    assert client.cancel_calls == 1
    assert first.checked == 1
    assert second.checked == 1
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).one()
        pending_leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "entry-2")
            .one()
        )
        assert lifecycle.lifecycle_status == "exited"
        assert lifecycle.exit_reason == "take_profit"
        assert pending_leg.status == "cancelled"
        assert pending_leg.terminal_reason == "terminal_entry_cleanup_confirmed"


def test_terminal_cleanup_query_does_not_starve_anomaly_after_clean_history(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        for index in range(101):
            binding = ExecutionBinding(
                kol_id=f"group:{index}",
                chat_id=10_000 + index,
                message_id=index + 1,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                status="closed",
                strategy_instance_id=f"deepcoin:{index}:BTC:long",
            )
            session.add(binding)
            session.flush()
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="limit",
                    order_id=f"clean-{index}",
                    status="cancelled",
                    terminal_reason="test_terminal",
                )
            )
            session.add(
                StrategyLifecycle(
                    chat_id=binding.chat_id,
                    message_id=binding.message_id,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="exited",
                    signal_at=datetime(2026, 7, 1),
                    execution_binding_id=binding.id,
                )
            )
        anomaly_binding = ExecutionBinding(
            kol_id="group:anomaly",
            chat_id=99_999,
            message_id=999,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="unknown",
            strategy_instance_id="deepcoin:anomaly:BTC:long",
        )
        session.add(anomaly_binding)
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=anomaly_binding.id,
                strategy_instance_id=anomaly_binding.strategy_instance_id,
                leg_index=1,
                purpose="entry",
                order_kind="trigger_limit",
                order_id="anomaly-entry",
                client_order_id="anomaly-client",
                status="pending",
            )
        )
        session.add(
            StrategyLifecycle(
                chat_id=anomaly_binding.chat_id,
                message_id=anomaly_binding.message_id,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                signal_at=datetime(2026, 7, 1),
                execution_binding_id=anomaly_binding.id,
            )
        )
        session.commit()

    class Client:
        def __init__(self):
            self.pending = [
                {
                    "ordId": "anomaly-entry",
                    "clOrdId": "anomaly-client",
                }
            ]
            self.history = []
            self.cancel_calls = 0

        def list_positions(self, *, inst_id=None):
            return []

        def list_trigger_orders_pending(self, *, inst_id):
            return list(self.pending)

        def list_open_orders(self, *, inst_id=None):
            return []

        def cancel_trigger_order(self, payload):
            self.cancel_calls += 1
            self.pending = []
            self.history = [{"ordId": "anomaly-entry", "state": "canceled"}]
            return {"code": "0"}

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id):
            return list(self.history)

        def list_trade_fills(self, *, inst_id=None):
            return []

    client = Client()
    sync_manual_closed_deepcoin_positions(
        session_factory,
        client=client,
        synced_at=datetime(2026, 7, 30, 2, 0),
    )

    assert client.cancel_calls == 1
    with session_factory() as session:
        leg = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.order_id == "anomaly-entry")
            .one()
        )
        assert leg.status == "cancelled"


def test_sync_closed_position_finalizes_pending_kol_exit_exactly_once(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(pos_id="pos-closed", status="active"),
    )
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
                execution_binding_id=binding_id,
                exit_signal_message_id=8401,
                management_signal_message_id=8401,
                management_action="exit_requested",
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

    closed_at = datetime(2026, 6, 30, 10, 0)
    first = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=closed_at,
    )
    second = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=datetime(2026, 6, 30, 10, 1),
    )

    assert first.manually_closed == 1
    assert second.manually_closed == 0
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).one()
        assert lifecycle.lifecycle_status == "exited"
        assert lifecycle.exit_reason == "kol_signal"
        assert lifecycle.exited_at == closed_at
        assert lifecycle.exit_signal_message_id == 8401


def test_sync_manual_closed_positions_keeps_binding_open_for_unfilled_entry_leg(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    binding_id = upsert_execution_binding(
        session_factory,
        _binding(
            order_id="filled-trigger,pending-trigger",
            client_order_id="filled-client,pending-client",
            pos_id="closed-pos",
            status="active",
            payload={
                "submitted_orders": [
                    {
                        "leg_index": 1,
                        "order_id": "filled-trigger",
                        "client_order_id": "filled-client",
                    },
                    {
                        "leg_index": 2,
                        "order_id": "pending-trigger",
                        "client_order_id": "pending-client",
                    },
                ]
            },
        ),
    )
    repair_execution_order_legs_from_binding_payloads(session_factory)
    with session_factory() as session:
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=55,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
                execution_binding_id=binding_id,
            )
        )
        session.commit()

    class FakeClient:
        def list_positions(self):
            return []

    result = sync_manual_closed_deepcoin_positions(
        session_factory,
        client=FakeClient(),
        synced_at=datetime(2026, 6, 30, 10, 0),
    )

    assert result.checked == 1
    assert result.manually_closed == 0
    with session_factory() as session:
        binding = session.get(ExecutionBinding, binding_id)
        lifecycle = session.query(StrategyLifecycle).one()

    assert binding.status == "open"
    assert binding.last_exchange_status == "terminal_entry_cleanup_unknown"
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_action == "terminal_cleanup_required"


def test_manual_bind_rolls_back_binding_and_lifecycle_when_leg_owner_conflicts(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    owner_binding_id = upsert_execution_binding(
        session_factory, _binding(chat_id=999, message_id=999, status="active")
    )
    _add_entry_leg(
        session_factory,
        owner_binding_id,
        pos_id="pos-already-owned",
        status="active",
        attribution_status="verified",
    )
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=100,
            message_id=55,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 30, 9, 0),
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = int(lifecycle.id)

    with pytest.raises(ValueError, match="already has a verified owner"):
        bind_deepcoin_position_to_lifecycle(
            session_factory,
            lifecycle_id=lifecycle_id,
            pos_id="pos-already-owned",
        )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate_binding = (
            session.query(ExecutionBinding)
            .filter_by(chat_id=100, message_id=55, symbol="BTC", side="long")
            .one_or_none()
        )
    assert lifecycle.execution_binding_id is None
    assert lifecycle.lifecycle_status == "pending_entry"
    assert candidate_binding is None
