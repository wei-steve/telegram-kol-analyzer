from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.deepcoin_contract_specs import StaticDeepcoinContractSpecProvider
from telegram_kol_research.execution_bindings import (
    ExecutionBindingRecord,
    ExecutionOrderLegRecord,
    upsert_execution_binding,
    upsert_execution_order_leg,
)
from telegram_kol_research.models import ExecutionOrderLeg, PositionBackupStopOrder
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.protection_ledger import upsert_protection_ledger_row


NOW = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)


class _Client:
    def __init__(self, *, verify_after_submit=False):
        self.set_position_sltp_payloads = []
        self.verify_after_submit = verify_after_submit
        self.pending_rows = [{
            "ordId": "primary-1", "instId": "ETH-USDT-SWAP", "posId": "pos-1",
            "posSide": "short", "triggerOrderType": "TPSL", "slTriggerPrice": "1900",
            "sz": "4.4",
        }]

    def list_positions(self, *, inst_id=None):
        return [{
            "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": "4.4", "avgPx": "1800", "liqPx": "2000",
            "mgnMode": "cross", "mrgPosition": "split",
        }]

    def list_trigger_orders_pending(self, *, inst_id):
        return [row for row in self.pending_rows if row["instId"] == inst_id]

    def set_position_sltp(self, payload):
        self.set_position_sltp_payloads.append(dict(payload))
        if self.verify_after_submit:
            self.pending_rows.append({
                "ordId": "backup-1", "instId": payload["instId"],
                "posId": payload["posId"], "posSide": payload["posSide"],
                "triggerOrderType": "TPSL", "slTriggerPrice": payload["slTriggerPx"],
                "sz": "0",
            })
        return {"code": "0", "data": "backup-1"}


def _provider():
    return StaticDeepcoinContractSpecProvider({
        "ETH-USDT-SWAP": DeepcoinContractSpec(
            instrument_id="ETH-USDT-SWAP", contract_value=0.1, quantity_step=0.1,
            min_quantity=0.1, price_tick=0.1,
        )
    })


def _seed(session_factory):
    binding_id = upsert_execution_binding(
        session_factory,
        ExecutionBindingRecord(
            kol_id="kol", chat_id=1, message_id=1, symbol="ETH", side="short",
            venue="deepcoin", margin_mode="cross", position_mode="split", status="active",
        ),
    )
    leg_id = upsert_execution_order_leg(
        session_factory,
        ExecutionOrderLegRecord(
            execution_binding_id=binding_id, leg_index=1, purpose="entry", order_kind="market",
            strategy_instance_id="deepcoin:1:1:ETH:short",
            venue="deepcoin", pos_id="pos-1", status="active", attribution_status="verified",
        ),
    )
    with session_factory() as session:
        leg = session.get(ExecutionOrderLeg, leg_id)
        leg.attribution_evidence_json = '{"policy_version":2}'
        upsert_protection_ledger_row(
            session, venue="deepcoin", execution_binding_id=binding_id,
            execution_order_leg_id=leg_id, strategy_instance_id=None, pos_id="pos-1",
            instrument_id="ETH-USDT-SWAP", side="short", order_id="primary-1",
            purpose="stop_loss", trigger_price="1900", size_text="4.4", status="verified",
            evidence_source="test", evidence={}, seen_at=NOW,
        )
        session.commit()


def test_repair_plan_is_read_only_fingerprinted_and_uses_twenty_bps(tmp_path):
    from telegram_kol_research.backup_stop_repair import build_backup_stop_repair_plan

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()

    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    assert client.set_position_sltp_payloads == []
    assert plan.conflicts == ()
    assert plan.fingerprint
    assert [(action.pos_id, action.primary_order_id, action.backup_stop) for action in plan.actions] == [
        ("pos-1", "primary-1", "1903.8")
    ]


def test_repair_plan_uses_persisted_backup_stop_buffer_setting(tmp_path):
    from telegram_kol_research.backup_stop_repair import build_backup_stop_repair_plan

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    save_trading_settings(session_factory, {"trigger_backup_stop_buffer_bps": 25})
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=_Client(), contract_spec_provider=_provider(), now=NOW
    )
    assert plan.actions[0].backup_stop == "1904.8"


def test_repair_plan_flags_local_active_backup_missing_from_exchange(tmp_path):
    from telegram_kol_research.backup_stop_repair import build_backup_stop_repair_plan

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        session.add(PositionBackupStopOrder(
            venue="deepcoin", execution_binding_id=leg.execution_binding_id,
            execution_order_leg_id=leg.id, pos_id="pos-1", instrument_id="ETH-USDT-SWAP",
            side="short", order_id="backup-local", trigger_price="1903.8",
            client_order_id="backup-client", status="active", request_json="{}",
        ))
        session.commit()

    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=_Client(), contract_spec_provider=_provider(), now=NOW
    )
    assert plan.actions == ()
    assert plan.conflicts == ({"pos_id": "pos-1", "reason": "backup_stop_missing_on_exchange"},)


def test_targeted_backup_repair_gate_ignores_unrelated_conflicts():
    from telegram_kol_research.backup_stop_repair import BackupStopRepairPlan
    from telegram_kol_research.cli import _backup_stop_conflicts_for_target

    plan = BackupStopRepairPlan(
        created_at=NOW,
        actions=(),
        conflicts=(
            {"pos_id": "blocked-pos", "reason": "primary_stop_not_verified"},
            {"pos_id": "target-pos", "reason": "backup_exchange_outcome_unknown"},
        ),
        database_fingerprint="database",
        exchange_fingerprint="exchange",
        fingerprint="plan",
    )

    assert _backup_stop_conflicts_for_target(plan, pos_id="safe-pos") == ()
    assert _backup_stop_conflicts_for_target(plan, pos_id="target-pos") == (
        {"pos_id": "target-pos", "reason": "backup_exchange_outcome_unknown"},
    )


def test_repair_apply_requires_one_position_and_exact_fingerprint(tmp_path):
    from telegram_kol_research.backup_stop_repair import (
        apply_backup_stop_repair_plan,
        build_backup_stop_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    with pytest.raises(ValueError, match="pos_id"):
        apply_backup_stop_repair_plan(
            session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
            pos_id="", action_id=plan.actions[0].action_id,
            expected_fingerprint=plan.fingerprint,
            confirmation_token="confirm-test-1", now=NOW,
        )
    with pytest.raises(ValueError, match="fingerprint"):
        apply_backup_stop_repair_plan(
            session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
            pos_id="pos-1", action_id=plan.actions[0].action_id,
            expected_fingerprint="wrong",
            confirmation_token="confirm-test-2", now=NOW,
        )
    assert client.set_position_sltp_payloads == []


def test_repair_apply_marks_submitted_native_tpsl_pending_readback_without_retry(tmp_path):
    from telegram_kol_research.backup_stop_repair import (
        apply_backup_stop_repair_plan,
        build_backup_stop_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    result = apply_backup_stop_repair_plan(
        session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
        pos_id="pos-1", action_id=plan.actions[0].action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="confirm-test-3", now=NOW,
    )

    assert result.status == "pending_readback"
    assert len(client.set_position_sltp_payloads) == 1
    with pytest.raises(ValueError, match="confirmation_token already consumed"):
        apply_backup_stop_repair_plan(
            session_factory,
            plan,
            deepcoin_client=client,
            contract_spec_provider=_provider(),
            pos_id="pos-1",
            action_id=plan.actions[0].action_id,
            expected_fingerprint=plan.fingerprint,
            confirmation_token="confirm-test-3",
            now=NOW,
        )
    assert len(client.set_position_sltp_payloads) == 1
    assert client.set_position_sltp_payloads[0]["slTriggerPx"] == "1903.8"
    assert "triggerPrice" not in client.set_position_sltp_payloads[0]
    assert "closePosId" not in client.set_position_sltp_payloads[0]
    with session_factory() as session:
        row = session.query(PositionBackupStopOrder).one()
    assert row.status == "pending_readback"


def test_repair_apply_activates_only_after_native_tpsl_readback_matches(tmp_path):
    from telegram_kol_research.backup_stop_repair import (
        apply_backup_stop_repair_plan,
        build_backup_stop_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client(verify_after_submit=True)
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    result = apply_backup_stop_repair_plan(
        session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
        pos_id="pos-1", action_id=plan.actions[0].action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="confirm-test-4", now=NOW,
    )

    assert result.status == "active"
    assert result.order_id == "backup-1"
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "active"


def test_pending_native_readback_freezes_future_backup_repair_without_rebuild(tmp_path):
    from telegram_kol_research.backup_stop_repair import build_backup_stop_repair_plan

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        session.add(PositionBackupStopOrder(
            venue="deepcoin", execution_binding_id=leg.execution_binding_id,
            execution_order_leg_id=leg.id, pos_id="pos-1", instrument_id="ETH-USDT-SWAP",
            side="short", trigger_price="1903.8", client_order_id="backup-client",
            status="pending_readback", request_json="{}",
        ))
        session.commit()

    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=_Client(), contract_spec_provider=_provider(), now=NOW
    )

    assert plan.actions == ()
    assert plan.conflicts == ({"pos_id": "pos-1", "reason": "backup_stop_pending_readback"},)


def test_unscoped_native_manual_full_position_stop_freezes_backup_repair(tmp_path):
    from telegram_kol_research.backup_stop_repair import build_backup_stop_repair_plan

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()
    client.pending_rows.append({
        "ordId": "manual-stop", "instId": "ETH-USDT-SWAP", "posSide": "short",
        "triggerOrderType": "TPSL", "slTriggerPrice": "1903.8", "sz": "0",
    })

    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    assert plan.actions == ()
    assert plan.conflicts == ({"pos_id": "pos-1", "reason": "backup_similar_unscoped_order"},)


def test_unrelated_unscoped_full_position_stops_do_not_block_backup_repair(tmp_path):
    from telegram_kol_research.backup_stop_repair import build_backup_stop_repair_plan

    class _TimestampedClient(_Client):
        def list_positions(self, *, inst_id=None):
            return [{
                **super().list_positions(inst_id=inst_id)[0],
                "cTime": "1000",
            }]

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _TimestampedClient()
    client.pending_rows.extend([
        {
            "ordId": "manual-stop-1", "instId": "ETH-USDT-SWAP", "posSide": "short",
            "triggerOrderType": "TPSL", "slTriggerPrice": "1904", "sz": "0", "cTime": "1000",
        },
        {
            "ordId": "manual-stop-2", "instId": "ETH-USDT-SWAP", "posSide": "short",
            "triggerOrderType": "TPSL", "slTriggerPrice": "1905", "sz": "0", "cTime": "1000",
        },
    ])

    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    assert plan.conflicts == ()
    assert [action.pos_id for action in plan.actions] == ["pos-1"]


def test_background_submission_uses_native_tpsl_and_waits_for_readback(tmp_path):
    from telegram_kol_research.trigger_backup_stop_executor import (
        submit_verified_trigger_backup_stops,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _Client()

    submitted = submit_verified_trigger_backup_stops(
        session_factory,
        client=client,
        contract_spec_provider=_provider(),
        submitted_at=NOW,
    )

    assert submitted == 0
    assert client.set_position_sltp_payloads == [{
        "instType": "SWAP", "instId": "ETH-USDT-SWAP", "posSide": "short",
        "mrgPosition": "split", "tdMode": "cross", "posId": "pos-1",
        "slTriggerPx": "1903.8", "slTriggerPxType": "last", "slOrdPx": "-1",
    }]
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "pending_readback"


class _TwoSplitPositionsClient(_Client):
    def __init__(self, *, reveal_second_after_submit=False, response_has_order_id=True):
        super().__init__()
        self.reveal_second_after_submit = reveal_second_after_submit
        self.response_has_order_id = response_has_order_id
        self.second_revealed = not reveal_second_after_submit
        self.submitted = False
        self.position_read_calls = []

    def list_positions(self, *, inst_id=None):
        self.position_read_calls.append((self.submitted, inst_id))
        positions = [{
            "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": "4.4", "avgPx": "1800", "liqPx": "2000",
            "mgnMode": "cross", "mrgPosition": "split", "cTime": "1000",
        }]
        if self.second_revealed:
            positions.append({
                "instId": "ETH-USDT-SWAP", "posId": "pos-2", "posSide": "short",
                "pos": "4.4", "avgPx": "1800", "liqPx": "2000",
                "mgnMode": "cross", "mrgPosition": "split", "cTime": "1000",
            })
        return positions

    def set_position_sltp(self, payload):
        self.set_position_sltp_payloads.append(dict(payload))
        self.submitted = True
        self.second_revealed = True
        self.pending_rows.append({
            "ordId": "backup-1", "instId": payload["instId"], "posSide": payload["posSide"],
            "triggerOrderType": "TPSL", "slTriggerPrice": payload["slTriggerPx"],
            "sz": "0", "cTime": "1000",
        })
        return {"code": "0", "data": "backup-1" if self.response_has_order_id else {}}


class _SingleSplitNativeReadbackClient(_Client):
    def list_positions(self, *, inst_id=None):
        return [{
            "instId": "ETH-USDT-SWAP", "posId": "pos-1", "posSide": "short",
            "pos": "4.4", "avgPx": "1800", "liqPx": "2000",
            "mgnMode": "cross", "mrgPosition": "split", "cTime": "1000",
        }]

    def set_position_sltp(self, payload):
        self.set_position_sltp_payloads.append(dict(payload))
        self.pending_rows.append({
            "ordId": "backup-1", "instId": payload["instId"], "posSide": payload["posSide"],
            "triggerOrderType": "TPSL", "slTriggerPrice": payload["slTriggerPx"],
            "sz": "0", "cTime": "1000",
        })
        return {"code": "0", "data": {}}


class _PositionReadbackUnavailableClient(_Client):
    def __init__(self):
        super().__init__()
        self.submitted = False

    def list_positions(self, *, inst_id=None):
        if self.submitted:
            raise RuntimeError("positions unavailable after submit")
        return super().list_positions(inst_id=inst_id)

    def set_position_sltp(self, payload):
        self.submitted = True
        return super().set_position_sltp(payload)


def test_background_submission_verifies_returned_unscoped_native_tpsl_with_two_split_positions(tmp_path):
    from telegram_kol_research.trigger_backup_stop_executor import (
        submit_verified_trigger_backup_stops,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _TwoSplitPositionsClient()

    submitted = submit_verified_trigger_backup_stops(
        session_factory,
        client=client,
        contract_spec_provider=_provider(),
        submitted_at=NOW,
    )

    assert submitted == 1
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "active"


def test_background_submission_does_not_activate_fallback_without_response_order_id(tmp_path):
    from telegram_kol_research.trigger_backup_stop_executor import (
        submit_verified_trigger_backup_stops,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _SingleSplitNativeReadbackClient()

    submitted = submit_verified_trigger_backup_stops(
        session_factory,
        client=client,
        contract_spec_provider=_provider(),
        submitted_at=NOW,
    )

    assert submitted == 0
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "pending_readback"


def test_background_submission_rereads_full_positions_after_submit_race_without_order_id(tmp_path):
    from telegram_kol_research.trigger_backup_stop_executor import (
        submit_verified_trigger_backup_stops,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _TwoSplitPositionsClient(
        reveal_second_after_submit=True,
        response_has_order_id=False,
    )

    submitted = submit_verified_trigger_backup_stops(
        session_factory,
        client=client,
        contract_spec_provider=_provider(),
        submitted_at=NOW,
    )

    assert submitted == 0
    assert (True, None) in client.position_read_calls
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "pending_readback"


def test_repair_apply_verifies_returned_unscoped_native_tpsl_with_two_split_positions(tmp_path):
    from telegram_kol_research.backup_stop_repair import (
        apply_backup_stop_repair_plan,
        build_backup_stop_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _TwoSplitPositionsClient()
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    result = apply_backup_stop_repair_plan(
        session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
        pos_id="pos-1", action_id=plan.actions[0].action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="confirm-test-5", now=NOW,
    )

    assert result.status == "active"
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "active"


def test_repair_apply_does_not_activate_native_tpsl_when_response_has_no_order_id(tmp_path):
    from telegram_kol_research.backup_stop_repair import (
        apply_backup_stop_repair_plan,
        build_backup_stop_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _SingleSplitNativeReadbackClient()
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    result = apply_backup_stop_repair_plan(
        session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
        pos_id="pos-1", action_id=plan.actions[0].action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="confirm-test-6", now=NOW,
    )

    assert result.status == "pending_readback"
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "pending_readback"


def test_repair_rereads_full_positions_after_submit_race_without_order_id(tmp_path):
    from telegram_kol_research.backup_stop_repair import (
        apply_backup_stop_repair_plan,
        build_backup_stop_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _TwoSplitPositionsClient(
        reveal_second_after_submit=True,
        response_has_order_id=False,
    )
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    result = apply_backup_stop_repair_plan(
        session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
        pos_id="pos-1", action_id=plan.actions[0].action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="confirm-test-7", now=NOW,
    )

    assert result.status == "pending_readback"
    assert (True, None) in client.position_read_calls


def test_repair_apply_freezes_when_live_position_readback_is_unavailable(tmp_path):
    from telegram_kol_research.backup_stop_repair import (
        apply_backup_stop_repair_plan,
        build_backup_stop_repair_plan,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    _seed(session_factory)
    client = _PositionReadbackUnavailableClient()
    plan = build_backup_stop_repair_plan(
        session_factory, deepcoin_client=client, contract_spec_provider=_provider(), now=NOW
    )

    result = apply_backup_stop_repair_plan(
        session_factory, plan, deepcoin_client=client, contract_spec_provider=_provider(),
        pos_id="pos-1", action_id=plan.actions[0].action_id,
        expected_fingerprint=plan.fingerprint,
        confirmation_token="confirm-test-8", now=NOW,
    )

    assert result.status == "pending_readback"
    with session_factory() as session:
        assert session.query(PositionBackupStopOrder).one().status == "pending_readback"
