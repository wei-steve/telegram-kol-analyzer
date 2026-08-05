from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_bindings import _ReconcileSnapshot
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    TriggerProtectionIntent,
    TriggerProtectionStopRescue,
    PositionProtectionLedger,
)
from telegram_kol_research.position_management_liveness_recovery import (
    apply_position_management_liveness_recovery,
    build_position_management_liveness_recovery_plan,
)
from telegram_kol_research.trading_settings import save_trading_settings


NOW = datetime(2026, 8, 6, 11, 0, tzinfo=UTC)


def _enable_live_recovery(session_factory):
    save_trading_settings(session_factory, {
        "auto_trade_enabled": True,
        "management_execution_mode": "live",
        "position_management_liveness_v2_mode": "live",
    })


def _seed_exact_recovery_candidate(session_factory):
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:1:2:ETH:short",
            kol_id="kol", chat_id=1, message_id=2, symbol="ETH", side="short",
            venue="deepcoin", pos_id="pos-1", margin_mode="cross",
            position_mode="split", status="active",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1, purpose="entry", order_kind="trigger_limit",
            venue="deepcoin", pos_id="pos-1", status="active",
            attribution_status="verified",
            order_id="entry-order-1",
            attribution_evidence_json='{"policy_version":2}',
            request_json=(
                '{"instId":"ETH-USDT-SWAP","posSide":"short",'
                '"slTriggerPx":"1935","slOrdPx":"-1"}'
            ),
        )
        session.add(leg)
        session.flush()
        intent = TriggerProtectionIntent(
            venue="deepcoin", execution_binding_id=binding.id,
            execution_order_leg_id=leg.id, request_fingerprint="a" * 64,
            pre_submit_tpsl_baseline_json="[]", correlation_id="recover-pos-1",
            recovery_state="failed", recovery_disposition="exact_backup",
            parent_trigger_order_id="entry-order-1",
        )
        session.add(intent)
        session.add(ExecutionEvent(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            venue="deepcoin", action="create_trigger_entry", status="submitted",
            symbol="ETH", side="short", order_id="entry-order-1",
            request_json=(
                '{"instId":"ETH-USDT-SWAP","posSide":"short",'
                '"slTriggerPx":"1935","slOrdPx":"-1"}'
            ),
        ))
        session.commit()
        return int(binding.id), int(leg.id), int(intent.id)


def _snapshot(*, size="3.4", pending=None, errors=None):
    return _ReconcileSnapshot(
        positions=[{
            "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": size, "avgPx": "1900", "liqPx": "2050",
            "mrgPosition": "split", "mgnMode": "cross",
        }],
        pending_trigger_orders=list(pending or []),
        pending_tpsl_observations=[{
            "instrument_id": "ETH-USDT-SWAP", "complete": True,
        }],
        errors=dict(errors or {}),
    )


def test_dry_run_is_exact_bounded_and_never_mutates_exchange(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery.db")
    binding_id, leg_id, _ = _seed_exact_recovery_candidate(session_factory)
    client_calls = []
    client = object()

    plan = build_position_management_liveness_recovery_plan(
        session_factory,
        pos_id="pos-1",
        deepcoin_client=client,
        snapshot_loader=lambda *_args, **_kwargs: (
            client_calls.append("snapshot") or _snapshot()
        ),
        planned_at=NOW,
    )

    assert plan.action_kind == "create_exact_backup_stop"
    assert (plan.binding_id, plan.leg_id, plan.pos_id) == (
        binding_id, leg_id, "pos-1"
    )
    assert plan.exact_position["size"] == "3.4"
    assert plan.excluded_candidates == ()
    assert len(plan.fingerprint) == 64
    assert client_calls == ["snapshot"]


@pytest.mark.parametrize(
    "pos_id,snapshot,reason",
    [
        ("", _snapshot(), "exact_pos_id_required"),
        ("pos-1", _snapshot(size="0"), "exact_live_position_not_verified"),
        ("pos-1", _snapshot(errors={"positions": "unavailable"}), "snapshot_incomplete"),
    ],
)
def test_dry_run_returns_no_action_for_invalid_or_incomplete_state(
    tmp_path, pos_id, snapshot, reason
):
    session_factory = create_session_factory(tmp_path / f"{reason}.db")
    _seed_exact_recovery_candidate(session_factory)

    plan = build_position_management_liveness_recovery_plan(
        session_factory,
        pos_id=pos_id,
        deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: snapshot,
        planned_at=NOW,
    )

    assert plan.action_kind == "noop"
    assert plan.reason_code == reason


def test_changed_snapshot_invalidates_reviewed_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "stale-plan.db")
    _seed_exact_recovery_candidate(session_factory)
    _enable_live_recovery(session_factory)
    snapshots = [_snapshot(), _snapshot(size="3.5")]

    reviewed = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: snapshots.pop(0), planned_at=NOW,
    )

    with pytest.raises(ValueError, match="fingerprint changed"):
        apply_position_management_liveness_recovery(
            session_factory,
            pos_id="pos-1",
            expected_fingerprint=reviewed.fingerprint,
            deepcoin_client=object(),
            snapshot_loader=lambda *_args, **_kwargs: snapshots.pop(0),
            applied_at=NOW,
        )


def test_apply_is_blocked_until_liveness_gate_is_effectively_live(tmp_path):
    session_factory = create_session_factory(tmp_path / "recovery-gate.db")
    _seed_exact_recovery_candidate(session_factory)
    reviewed = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: _snapshot(), planned_at=NOW,
    )

    with pytest.raises(ValueError, match="not live"):
        apply_position_management_liveness_recovery(
            session_factory,
            pos_id="pos-1",
            expected_fingerprint=reviewed.fingerprint,
            deepcoin_client=object(),
            snapshot_loader=lambda *_args, **_kwargs: _snapshot(),
            applied_at=NOW,
        )


def test_manual_review_disposition_blocks_native_stop_adoption(tmp_path):
    session_factory = create_session_factory(tmp_path / "manual-review.db")
    _seed_exact_recovery_candidate(session_factory)
    with session_factory() as session:
        intent = session.query(TriggerProtectionIntent).one()
        intent.recovery_disposition = "manual_review"
        session.commit()
    pending = [{
        "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
        "ordId": "native-stop-1", "slTriggerPx": "1935", "slOrdPx": "-1",
    }]

    plan = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: _snapshot(pending=pending),
        planned_at=NOW,
    )

    assert plan.action_kind == "noop"
    assert plan.reason_code == "protection_recovery_requires_manual_review"


def test_leg_request_payload_change_invalidates_recovery_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "request-fingerprint.db")
    _seed_exact_recovery_candidate(session_factory)
    before = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: _snapshot(), planned_at=NOW,
    )
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        leg.request_json = leg.request_json.replace("1935", "1940")
        session.commit()
    after = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: _snapshot(), planned_at=NOW,
    )

    assert before.fingerprint != after.fingerprint


def test_account_wide_ledger_owner_blocks_cross_position_adoption(tmp_path):
    session_factory = create_session_factory(tmp_path / "foreign-owner.db")
    _seed_exact_recovery_candidate(session_factory)
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:9:9:ETH:short",
            kol_id="other", chat_id=9, message_id=9, symbol="ETH", side="short",
            venue="deepcoin", pos_id="pos-other", margin_mode="cross",
            position_mode="split", status="active",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1, purpose="entry", order_kind="trigger_limit",
            venue="deepcoin", pos_id="pos-other", status="active",
            attribution_status="verified",
        )
        session.add(leg)
        session.flush()
        session.add(PositionProtectionLedger(
            venue="deepcoin", execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=binding.strategy_instance_id,
            pos_id="pos-other", instrument_id="ETH-USDT-SWAP", side="short",
            order_id="native-stop-1", purpose="stop_loss",
            trigger_price="1935", status="verified",
            evidence_source="test", evidence_json="{}",
        ))
        session.commit()
    pending = [{
        "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
        "ordId": "native-stop-1", "slTriggerPx": "1935", "slOrdPx": "-1",
    }]

    plan = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: _snapshot(pending=pending),
        planned_at=NOW,
    )

    assert plan.action_kind == "noop"
    assert plan.reason_code == "native_stop_owned_by_another_position"


def test_missing_parent_event_makes_backup_recovery_a_noop(tmp_path):
    session_factory = create_session_factory(tmp_path / "missing-parent-event.db")
    _seed_exact_recovery_candidate(session_factory)
    with session_factory() as session:
        session.query(ExecutionEvent).delete()
        session.commit()

    plan = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: _snapshot(), planned_at=NOW,
    )

    assert plan.action_kind == "noop"


def test_unknown_existing_rescue_blocks_recovery_restart(tmp_path):
    session_factory = create_session_factory(tmp_path / "unknown-rescue.db")
    binding_id, leg_id, intent_id = _seed_exact_recovery_candidate(session_factory)
    with session_factory() as session:
        session.add(TriggerProtectionStopRescue(
            trigger_protection_intent_id=intent_id,
            execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            pos_id="pos-1", status="submit_unknown",
        ))
        session.commit()

    plan = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: _snapshot(), planned_at=NOW,
    )

    assert plan.action_kind == "noop"
    assert plan.reason_code == "stop_rescue_exchange_outcome_unknown"


def test_pending_ledger_and_intent_changes_all_change_fingerprint(tmp_path):
    session_factory = create_session_factory(tmp_path / "fingerprint-inputs.db")
    binding_id, leg_id, intent_id = _seed_exact_recovery_candidate(session_factory)
    base_snapshot = _snapshot()
    reviewed = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: base_snapshot, planned_at=NOW,
    )
    changed_pending = _snapshot(pending=[{
        "instId": "ETH-USDT-SWAP", "posId": "other-pos", "posSide": "short",
        "ordId": "other-stop", "slTriggerPx": "2000", "slOrdPx": "-1",
    }])
    pending_plan = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: changed_pending, planned_at=NOW,
    )
    with session_factory() as session:
        session.add(PositionProtectionLedger(
            venue="deepcoin", execution_binding_id=binding_id,
            execution_order_leg_id=leg_id,
            strategy_instance_id="deepcoin:1:2:ETH:short", pos_id="pos-1",
            instrument_id="ETH-USDT-SWAP", side="short", order_id="owned-stop",
            purpose="stop_loss", trigger_price="1935", status="verified",
            evidence_source="test", evidence_json="{}",
        ))
        session.commit()
    ledger_plan = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: base_snapshot, planned_at=NOW,
    )
    with session_factory() as session:
        intent = session.get(TriggerProtectionIntent, intent_id)
        intent.recovery_state = "adopted"
        intent.recovery_disposition = None
        session.commit()
    intent_plan = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: base_snapshot, planned_at=NOW,
    )

    assert len({
        reviewed.fingerprint, pending_plan.fingerprint,
        ledger_plan.fingerprint, intent_plan.fingerprint,
    }) == 4


def test_adoption_apply_is_database_only_and_restart_becomes_noop(tmp_path):
    session_factory = create_session_factory(tmp_path / "adoption-restart.db")
    _seed_exact_recovery_candidate(session_factory)
    _enable_live_recovery(session_factory)
    exact_pending = [{
        "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
        "ordId": "native-stop-1", "slTriggerPx": "1935", "slOrdPx": "-1",
        "sz": "3.4",
    }]
    coherent = _snapshot(pending=exact_pending)
    reviewed = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: coherent, planned_at=NOW,
    )
    assert reviewed.action_kind == "adopt_unique_native_stop"

    result = apply_position_management_liveness_recovery(
        session_factory,
        pos_id="pos-1",
        expected_fingerprint=reviewed.fingerprint,
        deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: coherent,
        applied_at=NOW,
    )
    restarted = build_position_management_liveness_recovery_plan(
        session_factory, pos_id="pos-1", deepcoin_client=object(),
        snapshot_loader=lambda *_args, **_kwargs: coherent, planned_at=NOW,
    )

    assert result.status == "applied"
    assert restarted.action_kind == "noop"
    with session_factory() as session:
        ledger = session.query(PositionProtectionLedger).one()
        intent = session.query(TriggerProtectionIntent).one()
    assert (ledger.pos_id, ledger.order_id, ledger.status) == (
        "pos-1", "native-stop-1", "verified"
    )
    assert (intent.recovery_state, intent.adopted_order_id) == (
        "adopted", "native-stop-1"
    )
