from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    PendingTpslSnapshotObservation,
    PositionBackupStopOrder,
    PositionProtectionLedger,
    PositionProtectionRevision,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TriggerProtectionIntent,
    TriggerProtectionStopRescue,
)
from telegram_kol_research.web_app import create_web_app


NOW = datetime(2026, 7, 17, 8, 30, tzinfo=UTC)


class EmptyDeepcoinClient:
    def list_positions(self):
        return []

    def list_open_orders(self):
        return []

    def list_order_history(self):
        return []


def _seed_strategy_records(database_path):
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=77,
            message_id=701,
            posted_at=NOW,
            sender_name="<script>alert(1)</script>",
            text="<script>unsafeEvidence()</script> BTC 做多",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTCUSDT",
            side="long",
            event_type="entry_signal",
            created_at=NOW,
        )
        session.add(candidate)
        session.flush()
        decision = RecognitionDecision(
            raw_message_id=raw_message.id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="accepted",
            authoritative_payload_json='{"token":"must-not-render","symbol":"BTCUSDT"}',
            agreement_status="agreed",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(decision)
        session.flush()
        binding = ExecutionBinding(
            strategy_instance_id="strategy-web-record",
            kol_id="77",
            chat_id=77,
            message_id=701,
            symbol="BTCUSDT",
            side="long",
            venue="deepcoin",
            pos_id="pos-web-record",
            status="open",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            execution_binding_id=binding.id,
            chat_id=77,
            message_id=701,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            stop_loss=None,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.flush()
        order_leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id="strategy-web-record",
            leg_index=0,
            purpose="entry",
            order_kind="market",
            order_id="exchange-order-web-1",
            client_order_id="client-order-web-1",
            pos_id="pos-web-record",
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json=(
                '{"evidence_type":"direct_fill","policy_version":2}'
            ),
            status="filled",
            request_json='{"token":"must-not-render","size":"1"}',
            response_json='{"code":"0","order":"exchange-order-web-1"}',
            last_verified_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(order_leg)
        session.flush()
        event = ExecutionEvent(
            execution_binding_id=binding.id,
            strategy_instance_id="strategy-web-record",
            action="fill",
            status="succeeded",
            chat_id=77,
            message_id=701,
            source_message_id=701,
            symbol="BTCUSDT",
            side="long",
            order_id="exchange-order-web-1",
            client_order_id="client-order-web-1",
            pos_id="pos-web-record",
            reason="exchange-reconciled",
            request_json='{"authorization":"must-not-render"}',
            response_json='{"fill":"confirmed"}',
            exchange_event_time=NOW,
            created_at=NOW,
        )
        batch = StrategyManagementBatch(
            idempotency_fingerprint="web-detail-management-batch",
            raw_message_id=raw_message.id,
            recognition_decision_id=decision.id,
            recognition_generation="mimo_only_v2",
            target_lifecycle_id=lifecycle.id,
            strategy_instance_id="strategy-web-record",
            execution_binding_id=binding.id,
            intent="risk_update",
            effective_action="move_stop",
            execution_mode="live",
            status="succeeded",
            reason_code="exchange_reconciled",
            target_fingerprint="web-detail-target",
            target_snapshot_json='{"stop":"breakeven"}',
            planned_at=NOW,
            reconciled_at=NOW,
            completed_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add_all([event, batch])
        session.flush()
        session.add(
            StrategyManagementLeg(
                management_batch_id=batch.id,
                execution_order_leg_id=order_leg.id,
                pos_id="pos-web-record",
                leg_index=0,
                status="succeeded",
                preflight_size="1",
                planned_close_size="0.5",
                client_order_id="management-client-web-1",
                exchange_order_id="management-order-web-1",
                request_json='{"passphrase":"must-not-render","size":"0.5"}',
                response_json='{"code":"0"}',
                last_exchange_snapshot_json='{"posId":"pos-web-record","size":"0.5"}',
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
        return lifecycle.id


def _client(tmp_path, *, client_factory=EmptyDeepcoinClient):
    database_path = tmp_path / "research.db"
    lifecycle_id = _seed_strategy_records(database_path)
    group_config = GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="web-record-group",
                chat_id=77,
                custom_group_label="测试群组",
            )
        ]
    )
    app = create_web_app(
        database_path=database_path,
        deepcoin_client_factory=client_factory,
        group_config=group_config,
        now_provider=lambda: NOW,
    )
    return TestClient(app), lifecycle_id


def test_strategy_record_list_is_mobile_read_only_and_exposes_attention(tmp_path):
    client, _ = _client(tmp_path)

    response = client.get(
        "/strategy-records",
        params={"filter": "needs_attention", "chat_id": "", "limit": 50},
    )

    assert response.status_code == 200
    assert "data-strategy-record-list" in response.text
    assert "data-strategy-record-card" in response.text
    assert 'data-attention-code="missing_stop"' in response.text
    assert 'data-service-health="telegram"' in response.text
    assert 'data-service-health="database"' in response.text
    assert 'data-service-health="deepcoin"' in response.text
    assert "data-last-success-at" in response.text
    assert "测试群组" in response.text
    assert "data-live-action" not in response.text
    assert "市价平仓" not in response.text


def test_strategy_record_list_cards_do_not_expose_live_action_controls(tmp_path):
    client, _ = _client(tmp_path)

    response = client.get("/strategy-records")

    assert response.status_code == 200
    card_start = response.text.index('data-strategy-record-card')
    card_end = response.text.index("</a>", card_start)
    card = response.text[card_start:card_end]
    for forbidden in (
        "data-close-bound-position",
        "data-bind",
        "data-tpsl",
        "type=\"submit\"",
        "data-live-action",
    ):
        assert forbidden not in card


def test_strategy_record_list_exposes_mobile_controller_hooks(tmp_path):
    client, _ = _client(tmp_path)

    response = client.get("/strategy-records")

    assert response.status_code == 200
    for hook in (
        "data-strategy-record-filter",
        "data-strategy-group-filter",
        "data-strategy-record-scroll",
        "data-strategy-new-changes",
        "data-strategy-record-retry",
    ):
        assert hook in response.text
    assert "data-strategy-record-refresh" in response.text
    assert "data-strategy-record-last-success" in response.text


def test_strategy_record_standalone_pages_link_back_to_full_workbench(tmp_path):
    client, lifecycle_id = _client(tmp_path)

    list_response = client.get("/strategy-records")
    detail_response = client.get(f"/strategy-records/{lifecycle_id}")

    assert list_response.status_code == 200
    assert detail_response.status_code == 200
    for response in (list_response, detail_response):
        assert 'class="strategy-record-workbench-shell"' in response.text
        assert "strategy-record-workbench-rail" in response.text
        assert 'aria-label="完整工作台导航"' in response.text
        for view, label in (
            ("strategies", "策略"),
            ("positions", "持仓"),
            ("activity", "动态"),
            ("groups", "群组"),
            ("more", "更多"),
        ):
            assert f'href="/?view={view}"' in response.text
            assert f">{label}</a>" in response.text


def test_strategy_record_detail_is_semantic_read_only_and_escapes_evidence(tmp_path):
    client, lifecycle_id = _client(tmp_path)

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert "data-strategy-record-detail" in response.text
    assert 'data-strategy-detail-section="overview"' in response.text
    assert 'data-strategy-detail-section="timeline"' in response.text
    assert 'data-strategy-detail-section="execution"' in response.text
    assert 'data-strategy-protection-states' in response.text
    assert 'data-strategy-detail-section="evidence"' in response.text
    assert "&lt;script&gt;unsafeEvidence()&lt;/script&gt;" in response.text
    assert "<script>unsafeEvidence()</script>" not in response.text
    assert "data-live-action" not in response.text
    assert "市价平仓" not in response.text
    for evidence in (
        "strategy-web-record",
        "exchange-order-web-1",
        "client-order-web-1",
        "exchange-reconciled",
        "management-client-web-1",
        "management-order-web-1",
        "pos-web-record",
        '"fill": "confirmed"',
        '"size": "0.5"',
    ):
        assert evidence in response.text
    assert "must-not-render" not in response.text
    assert "[REDACTED]" in response.text


def test_strategy_record_detail_renders_recoverable_stop_without_owner_false_alarm(
    tmp_path,
):
    client, lifecycle_id = _client(tmp_path)
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        leg = session.query(ExecutionOrderLeg).filter_by(purpose="entry").one()
        session.add(TriggerProtectionIntent(
            venue="deepcoin",
            execution_binding_id=leg.execution_binding_id,
            execution_order_leg_id=leg.id,
            request_fingerprint="d" * 64,
            pre_submit_tpsl_baseline_json="[]",
            correlation_id="web-recoverable-stop",
            recovery_state="failed",
            recovery_disposition="exact_backup",
        ))
        session.add(PositionBackupStopOrder(
            venue="deepcoin",
            execution_binding_id=leg.execution_binding_id,
            execution_order_leg_id=leg.id,
            pos_id="pos-web-record",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            trigger_price="59500",
            order_id="web-backup-exact",
            client_order_id="web-backup-exact-client",
            status="active",
            request_json="{}",
        ))
        session.add(PendingTpslSnapshotObservation(
            venue="deepcoin", instrument_id="BTC-USDT-SWAP",
            response_count=1, order_ids_json='["web-backup-exact"]',
            complete=True, observed_at=NOW,
        ))
        session.commit()

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert "原生止损归属待恢复；精确仓位可继续风险降低操作" in response.text
    assert "持仓归属</dt><dd>已验证" in response.text


def test_strategy_record_detail_renders_safe_trigger_recovery_audit_fields(tmp_path):
    client, lifecycle_id = _client(tmp_path)
    with client.app.state.session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        leg = session.query(ExecutionOrderLeg).one()
        intent = TriggerProtectionIntent(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            request_fingerprint="c" * 64,
            pre_submit_tpsl_baseline_json='{"raw":"must-not-render"}',
            correlation_id="web-recovery-1",
            parent_trigger_order_id="parent-trigger-web-1",
            recovery_state="retry_scheduled",
            retry_attempts=4,
            adopted_order_id="adopted-tpsl-web-1",
        )
        session.add(intent)
        session.flush()
        session.add(
            TriggerProtectionStopRescue(
                trigger_protection_intent_id=intent.id,
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                pos_id="pos-web-record",
                status="blocked",
                reason_code="rescue_opaque_take_profit_present",
                exchange_order_id="rescue-order-must-not-render",
                request_json='{"raw":"must-not-render"}',
                response_json='{"raw":"must-not-render"}',
                error_json='{"raw":"must-not-render"}',
            )
        )
        session.commit()

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert 'data-trigger-protection-recovery-id="1"' in response.text
    for visible_value in (
        "parent-trigger-web-1",
        "pos-web-record",
        "retry_scheduled",
        "4",
        "adopted-tpsl-web-1",
        "rescue_opaque_take_profit_present",
        "blocked",
    ):
        assert visible_value in response.text
    assert "must-not-render" not in response.text
    assert "rescue-order-must-not-render" not in response.text


def test_strategy_record_detail_renders_protection_revision_history(tmp_path):
    client, lifecycle_id = _client(tmp_path)
    with client.app.state.session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        leg = session.query(ExecutionOrderLeg).one()
        session.add_all(
            [
                PositionProtectionRevision(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    pos_id="pos-web-record",
                    source="entry_submit",
                    status="superseded",
                    protection_json='{"take_profit_order_ids":["tp-initial"],"stop_loss_order_ids":["sl-initial"]}',
                    created_at=NOW,
                    updated_at=NOW,
                ),
                PositionProtectionRevision(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    pos_id="pos-web-record",
                    previous_revision_id=1,
                    source="management_replace",
                    status="active",
                    protection_json='{"take_profit_order_ids":["tp-current"],"stop_loss_order_ids":["sl-current"]}',
                    created_at=NOW + timedelta(minutes=1),
                    updated_at=NOW + timedelta(minutes=1),
                ),
            ]
        )
        session.commit()

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert 'data-protection-revision-id="1"' in response.text
    assert 'data-protection-revision-id="2"' in response.text
    for visible_value in (
        "entry_submit",
        "management_replace",
        "superseded",
        "active",
        "tp-initial",
        "sl-initial",
        "tp-current",
        "sl-current",
    ):
        assert visible_value in response.text


def test_strategy_record_detail_management_batch_exposes_authoritative_ids_and_leg_statuses(tmp_path):
    client, lifecycle_id = _client(tmp_path)

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert f'data-management-lifecycle-id="{lifecycle_id}"' in response.text
    assert 'data-management-binding-id="1"' in response.text
    assert 'data-management-position-id="pos-web-record"' in response.text
    assert 'data-management-leg-status="succeeded"' in response.text


def test_strategy_record_templates_wrap_long_mobile_evidence_into_semantic_containers(tmp_path):
    client, lifecycle_id = _client(tmp_path)
    long_message = (
        "这是一个需要在手机浏览器中完整换行显示的超长中文策略消息，"
        "包含入场条件、风险说明、仓位管理要求和后续确认信息。" * 5
    )
    take_profits = '["68000", "69000", "70000", "72000"]'
    with client.app.state.session_factory() as session:
        session.query(RawMessage).one().text = long_message
        lifecycle = session.query(StrategyLifecycle).one()
        lifecycle.take_profit = take_profits
        binding = session.query(ExecutionBinding).one()
        for index in range(3):
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id="strategy-web-record",
                    leg_index=index,
                    purpose="take_profit",
                    order_kind="limit",
                    order_id=f"exchange-order-with-a-very-long-identifier-{index}",
                    client_order_id=f"client-order-with-a-very-long-identifier-{index}",
                    pos_id=f"position-with-a-very-long-identifier-{index}",
                    venue="deepcoin",
                    attribution_status="verified",
                    status="submitted",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        session.commit()

    list_response = client.get("/strategy-records")
    detail_response = client.get(f"/strategy-records/{lifecycle_id}")

    assert list_response.status_code == 200
    assert 'class="strategy-record-card' in list_response.text
    assert 'data-strategy-state-label="attention"' in list_response.text
    assert detail_response.status_code == 200
    assert 'class="home-dashboard strategy-record-detail"' in detail_response.text
    assert 'class="strategy-value-list" data-strategy-take-profits' in detail_response.text
    for take_profit in ("68000", "69000", "70000", "72000"):
        assert f'<span class="strategy-value-chip">{take_profit}</span>' in detail_response.text
    assert 'class="strategy-evidence-text" data-strategy-message-evidence' in detail_response.text
    assert long_message in detail_response.text
    assert detail_response.text.count('class="strategy-identifier"') >= 12
    assert "exchange-order-with-a-very-long-identifier-2" in detail_response.text
    assert "position-with-a-very-long-identifier-2" in detail_response.text
    assert f"<code>{long_message}</code>" not in detail_response.text
    assert "data-live-action" not in detail_response.text
    assert "市价平仓" not in detail_response.text


def test_strategy_record_detail_rejects_reused_pos_id_owned_by_other_strategy(tmp_path):
    class MatchingPositionClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-web-record",
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": "67000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    client, lifecycle_id = _client(tmp_path, client_factory=MatchingPositionClient)
    with client.app.state.session_factory() as session:
        leg = session.query(ExecutionOrderLeg).one()
        leg.strategy_instance_id = "other-strategy-owner"
        session.commit()

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert 'data-exchange-state="conflict"' in response.text
    assert "策略归属身份不一致" in response.text
    assert "已确认" not in response.text


def test_strategy_record_detail_confirms_and_renders_every_verified_entry_leg(tmp_path):
    class MultiLegPositionClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": pos_id,
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": avg_px,
                }
                for pos_id, avg_px in (("pos-leg-a", "67000"), ("pos-leg-b", "66500"))
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    client, lifecycle_id = _client(tmp_path, client_factory=MultiLegPositionClient)
    with client.app.state.session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        binding.pos_id = "pos-leg-a,pos-leg-b"
        first_leg = session.query(ExecutionOrderLeg).one()
        first_leg.pos_id = "pos-leg-a"
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id="strategy-web-record",
                leg_index=1,
                purpose="entry",
                order_kind="limit",
                order_id="exchange-order-web-2",
                client_order_id="client-order-web-2",
                pos_id="pos-leg-b",
                venue="deepcoin",
                attribution_status="verified",
                status="active",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert 'data-exchange-state="confirmed"' in response.text
    assert "2 个 Deepcoin 实时仓位及策略归属已确认" in response.text
    assert 'data-exchange-position-id="pos-leg-a"' in response.text
    assert 'data-exchange-position-id="pos-leg-b"' in response.text


def test_strategy_record_detail_surfaces_management_execution_drift(tmp_path):
    class DriftPositionClient:
        def list_positions(self, *, inst_id=None):
            rows = [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-web-record",
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": "67000",
                }
            ]
            if inst_id is None:
                return rows
            return [row for row in rows if row["instId"] == inst_id]

        def list_trigger_orders_pending(self, *, inst_id):
            if inst_id != "BTC-USDT-SWAP":
                return []
            return [
                {
                    "instId": inst_id,
                    "closePosId": "pos-web-record",
                    "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "ordId": "legacy-stop",
                    "slTriggerPx": "60500",
                    "sz": "0",
                },
                {
                    "instId": inst_id,
                    "closePosId": "pos-web-record",
                    "posSide": "long",
                    "triggerOrderType": "TPSL",
                    "ordId": "backup-stop",
                    "slTriggerPx": "60000",
                    "sz": "0",
                },
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            return []

        def list_position_history(self, *, inst_id=None):
            return []

    client, lifecycle_id = _client(tmp_path, client_factory=DriftPositionClient)
    with client.app.state.session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).one()
        binding = session.query(ExecutionBinding).one()
        leg = session.query(ExecutionOrderLeg).one()
        binding.status = "active"
        leg.status = "active"
        leg.attribution_evidence_json = (
            '{"evidence_type":"direct_fill","policy_version":2}'
        )
        lifecycle.stop_loss = 63_575.875
        lifecycle.management_signal_message_id = 1451
        session.add(PositionProtectionLedger(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=binding.strategy_instance_id,
            pos_id="pos-web-record",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="legacy-stop",
            purpose="stop_loss",
            trigger_price="60500",
            status="verified",
            evidence_source="test_exact_order_readback",
        ))
        session.add(PositionProtectionLedger(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=binding.strategy_instance_id,
            pos_id="pos-web-record",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            order_id="backup-stop",
            purpose="backup_stop",
            trigger_price="60000",
            status="verified",
            evidence_source="test_exact_order_readback",
        ))
        session.add(PositionBackupStopOrder(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            pos_id="pos-web-record",
            instrument_id="BTC-USDT-SWAP",
            side="long",
            trigger_price="60000",
            order_id="backup-stop",
            client_order_id="backup-stop-client",
            status="active",
            request_json='{"slTriggerPx":"60000"}',
        ))
        session.commit()

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert 'data-exchange-state="attention"' in response.text
    assert 'data-management-execution-drift="management_execution_drift"' in response.text
    assert "策略止损 63575.875" in response.text
    assert "Deepcoin 精确仓位证据为 60500" in response.text


def test_strategy_record_detail_never_confirms_closed_binding_with_exact_identity(tmp_path):
    class MatchingPositionClient:
        def list_positions(self):
            return [{"instId": "BTC-USDT-SWAP", "posId": "pos-web-record", "posSide": "long", "pos": "1"}]

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    client, lifecycle_id = _client(tmp_path, client_factory=MatchingPositionClient)
    with client.app.state.session_factory() as session:
        session.query(ExecutionBinding).one().status = "closed"
        session.commit()

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert 'data-exchange-state="conflict"' in response.text
    assert "binding.status=closed" in response.text
    assert "实时仓位及策略归属已确认" not in response.text


def test_strategy_record_detail_rejects_system_attribution_conflict_snapshot(tmp_path):
    class MatchingPositionClient:
        def list_positions(self):
            return [{"instId": "BTC-USDT-SWAP", "posId": "pos-web-record", "posSide": "long", "pos": "1"}]

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    client, lifecycle_id = _client(tmp_path, client_factory=MatchingPositionClient)
    with client.app.state.session_factory() as session:
        session.query(ExecutionBinding).one().status = "stale"
        session.commit()

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert 'data-exchange-state="conflict"' in response.text
    assert "system_attribution_conflict" in response.text
    assert "实时仓位及策略归属已确认" not in response.text


def test_strategy_record_detail_does_not_match_non_deepcoin_binding(tmp_path):
    factory_calls = []

    def forbidden_factory():
        factory_calls.append(True)
        return EmptyDeepcoinClient()

    client, lifecycle_id = _client(tmp_path, client_factory=forbidden_factory)
    with client.app.state.session_factory() as session:
        binding = session.query(ExecutionBinding).one()
        binding.venue = " GATE "
        session.commit()

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert 'data-exchange-state="not_applicable"' in response.text
    assert "非 Deepcoin 绑定" in response.text
    assert factory_calls == []


def test_strategy_record_routes_validate_filter_limit_and_missing_id(tmp_path):
    client, _ = _client(tmp_path)

    for filter_name in ("needs_attention", "all", "executing", "pending_entry", "finished"):
        assert client.get("/strategy-records", params={"filter": filter_name}).status_code == 200
    assert client.get("/strategy-records", params={"filter": "invalid"}).status_code == 422
    assert client.get("/strategy-records", params={"limit": 0}).status_code == 422
    assert client.get("/strategy-records", params={"limit": 101}).status_code == 422
    assert client.get("/strategy-records/999999").status_code == 404


def test_strategy_record_api_uses_operational_detail_route(tmp_path):
    client, lifecycle_id = _client(tmp_path)

    response = client.get("/api/strategy-records", params={"filter_name": "all"})

    assert response.status_code == 200
    record = next(row for row in response.json()["records"] if row["lifecycle_id"] == lifecycle_id)
    assert record["detail_href"] == f"/strategy-records/{lifecycle_id}"


def test_strategy_record_detail_back_link_preserves_validated_list_context(tmp_path):
    client, lifecycle_id = _client(tmp_path)

    list_response = client.get(
        "/strategy-records",
        params={"filter": "all", "chat_id": 77, "limit": 25, "page": 1},
    )
    assert list_response.status_code == 200
    expected_query = "filter=all&amp;chat_id=77&amp;limit=25&amp;page=1"
    assert f'/strategy-records/{lifecycle_id}?{expected_query}' in list_response.text

    detail_response = client.get(
        f"/strategy-records/{lifecycle_id}",
        params={"filter": "all", "chat_id": 77, "limit": 25, "page": 1},
    )
    assert detail_response.status_code == 200
    assert f'href="/strategy-records?{expected_query}"' in detail_response.text

    assert client.get(
        f"/strategy-records/{lifecycle_id}", params={"filter": "invalid"}
    ).status_code == 422


def test_list_and_detail_each_build_at_most_one_exchange_snapshot(tmp_path):
    factory_calls = []

    def tracking_factory():
        factory_calls.append(True)
        return EmptyDeepcoinClient()

    client, lifecycle_id = _client(tmp_path, client_factory=tracking_factory)

    assert client.get("/strategy-records").status_code == 200
    assert factory_calls == [True]
    assert client.get(f"/strategy-records/{lifecycle_id}").status_code == 200
    assert factory_calls == [True, True]


def test_strategy_records_attention_count_and_second_page_include_201st_record(tmp_path):
    database_path = tmp_path / "attention-pages.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        for index in range(201):
            candidate = SignalCandidate(
                raw_message_id=10_000 + index,
                symbol="BTCUSDT",
                side="long",
                parse_source="mimo_authoritative",
            )
            session.add(candidate)
            session.flush()
            session.add(
                StrategyLifecycle(
                    signal_candidate_id=candidate.id,
                    chat_id=77,
                    message_id=10_000 + index,
                    symbol="BTCUSDT",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=NOW,
                    updated_at=NOW,
                )
            )
        session.commit()
    factory_calls = []

    def tracking_factory():
        factory_calls.append(True)
        return EmptyDeepcoinClient()

    client = TestClient(
        create_web_app(database_path=database_path, deepcoin_client_factory=tracking_factory)
    )
    response = client.get(
        "/api/strategy-records",
        params={"filter_name": "needs_attention", "limit": 200, "page": 2},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary_counts"]["needs_attention"] == 201
    assert payload["page"] == 2
    assert payload["has_more"] is False
    assert len(payload["records"]) == 1
    assert payload["records"][0]["message_id"] == 10_000
    assert factory_calls == [True]


def test_strategy_records_all_count_and_page_after_1000_are_complete(tmp_path):
    database_path = tmp_path / "all-pages.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                StrategyLifecycle(
                    chat_id=77,
                    message_id=20_000 + index,
                    symbol="ETHUSDT",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=NOW,
                    updated_at=NOW,
                )
                for index in range(1_001)
            ]
        )
        session.commit()
    factory_calls = []

    def tracking_factory():
        factory_calls.append(True)
        return EmptyDeepcoinClient()

    client = TestClient(
        create_web_app(database_path=database_path, deepcoin_client_factory=tracking_factory)
    )
    response = client.get(
        "/api/strategy-records",
        params={"filter_name": "all", "limit": 200, "page": 6},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary_counts"]["all"] == 1_001
    assert payload["page"] == 6
    assert payload["has_more"] is False
    assert len(payload["records"]) == 1
    assert payload["records"][0]["message_id"] == 20_000
    assert factory_calls == [True]

    out_of_range = client.get(
        "/api/strategy-records",
        params={"filter_name": "all", "limit": 200, "page": 7},
    ).json()
    assert out_of_range["records"] == []
    assert out_of_range["summary_counts"]["all"] == 1_001
    assert out_of_range["has_more"] is False
    assert factory_calls == [True, True]


def test_strategy_record_list_renders_next_page_control(tmp_path):
    database_path = tmp_path / "list-page-control.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                StrategyLifecycle(
                    chat_id=77,
                    message_id=30_000 + index,
                    symbol="SOLUSDT",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=NOW,
                    updated_at=NOW,
                )
                for index in range(51)
            ]
        )
        session.commit()
    client = TestClient(
        create_web_app(database_path=database_path, deepcoin_client_factory=EmptyDeepcoinClient)
    )

    response = client.get("/strategy-records", params={"filter": "all", "limit": 50})

    assert response.status_code == 200
    assert 'data-strategy-record-next-page="2"' in response.text
    assert "下一页" in response.text


@pytest.mark.parametrize(
    ("filter_name", "target_status", "background_status"),
    [
        ("finished", "finished", "pending_entry"),
        ("pending_entry", "pending_entry", "finished"),
        ("executing", "entered", "pending_entry"),
    ],
)
def test_state_filters_apply_in_sql_before_page_limit(
    tmp_path,
    filter_name,
    target_status,
    background_status,
):
    database_path = tmp_path / f"{filter_name}-state-page.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        target = StrategyLifecycle(
            chat_id=77,
            message_id=40_000,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status=target_status,
            signal_at=NOW - timedelta(days=2),
            entered_at=NOW - timedelta(days=2) if target_status == "entered" else None,
            updated_at=NOW - timedelta(days=2),
        )
        session.add(target)
        session.add_all(
            [
                StrategyLifecycle(
                    chat_id=77,
                    message_id=40_001 + index,
                    symbol="ETHUSDT",
                    side="long",
                    lifecycle_status=background_status,
                    signal_at=NOW - timedelta(minutes=index),
                    updated_at=NOW - timedelta(minutes=index),
                )
                for index in range(201)
            ]
        )
        session.commit()
        target_id = target.id
    factory_calls = []

    def tracking_factory():
        factory_calls.append(True)
        return EmptyDeepcoinClient()

    client = TestClient(
        create_web_app(database_path=database_path, deepcoin_client_factory=tracking_factory)
    )
    response = client.get(
        "/api/strategy-records",
        params={"filter_name": filter_name, "limit": 100, "page": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert [row["lifecycle_id"] for row in payload["records"]] == [target_id]
    assert payload["summary_counts"][filter_name] == 1
    assert payload["has_more"] is False
    assert factory_calls == [True]


def test_executing_filter_includes_old_lifecycle_matched_by_current_position(tmp_path):
    database_path = tmp_path / "executing-current-position.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="old-current-position",
            kol_id="77",
            chat_id=77,
            message_id=50_000,
            symbol="BTCUSDT",
            side="long",
            venue="deepcoin",
            pos_id="old-current-pos",
            status="closed",
            updated_at=NOW - timedelta(days=2),
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=77,
            message_id=50_000,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW - timedelta(days=2),
            execution_binding_id=binding.id,
            updated_at=NOW - timedelta(days=2),
        )
        session.add(lifecycle)
        session.add_all(
            [
                StrategyLifecycle(
                    chat_id=77,
                    message_id=50_001 + index,
                    symbol="ETHUSDT",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=NOW - timedelta(minutes=index),
                    updated_at=NOW - timedelta(minutes=index),
                )
                for index in range(201)
            ]
        )
        session.commit()
        lifecycle_id = lifecycle.id
    factory_calls = []

    class CurrentPositionClient(EmptyDeepcoinClient):
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "old-current-pos",
                    "posSide": "long",
                    "pos": "1",
                }
            ]

    def tracking_factory():
        factory_calls.append(True)
        return CurrentPositionClient()

    payload = TestClient(
        create_web_app(database_path=database_path, deepcoin_client_factory=tracking_factory)
    ).get(
        "/api/strategy-records",
        params={"filter_name": "executing", "limit": 100, "page": 1},
    ).json()

    assert [row["lifecycle_id"] for row in payload["records"]] == [lifecycle_id]
    assert payload["records"][0]["real_position"]["pos_id"] == "old-current-pos"
    assert payload["summary_counts"]["executing"] == 1
    assert payload["has_more"] is False
    assert factory_calls == [True]


def test_exchange_unavailable_is_explicit_and_not_treated_as_empty(tmp_path):
    class BrokenDeepcoinClient:
        def list_positions(self):
            raise RuntimeError("secret-exchange-error")

    client, lifecycle_id = _client(tmp_path, client_factory=BrokenDeepcoinClient)

    list_response = client.get("/strategy-records")
    detail_response = client.get(f"/strategy-records/{lifecycle_id}")
    positions_response = client.get("/positions-panel")

    assert list_response.status_code == 200
    assert 'data-exchange-state="unknown"' in list_response.text
    assert "Deepcoin 暂时连不上，仓位相关检查暂停" in list_response.text
    assert 'data-exchange-state="unknown"' in detail_response.text
    assert "secret-exchange-error" not in list_response.text
    assert "secret-exchange-error" not in detail_response.text
    assert "secret-exchange-error" not in positions_response.text
    assert "Deepcoin 数据暂不可用" in positions_response.text


def test_strategy_detail_sanitizes_management_leg_last_error(tmp_path):
    client, lifecycle_id = _client(tmp_path)
    with client.app.state.session_factory() as session:
        leg = session.query(StrategyManagementLeg).one()
        leg.last_error = '{"type":"DeepcoinError","api_key":"last-error-secret","reason_code":"exchange_timeout"}'
        session.commit()

    json_response = client.get(f"/strategy-records/{lifecycle_id}")

    assert json_response.status_code == 200
    assert "last-error-secret" not in json_response.text
    assert "[REDACTED]" in json_response.text
    assert "exchange_timeout" in json_response.text

    with client.app.state.session_factory() as session:
        session.query(StrategyManagementLeg).one().last_error = "plain-secret-error=do-not-render"
        session.commit()

    plain_response = client.get(f"/strategy-records/{lifecycle_id}")

    assert plain_response.status_code == 200
    assert "plain-secret-error" not in plain_response.text
    assert '"raw_length"' in plain_response.text
    assert '"sha256"' in plain_response.text


def test_strategy_detail_redacts_nested_production_error_prose(tmp_path):
    client, lifecycle_id = _client(tmp_path)
    with client.app.state.session_factory() as session:
        leg = session.query(StrategyManagementLeg).one()
        leg.last_error = (
            '{"type":"DeepcoinError","message":"api_key=DO_NOT_RENDER",'
            '"api_key":"DIRECT_SECRET","context":{"code":"E_TIMEOUT",'
            '"detail":"token=NESTED_SECRET","unknown":"free form secret prose",'
            '"reason":"passphrase=HIDDEN"}}'
        )
        session.commit()

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    for secret in (
        "DO_NOT_RENDER",
        "DIRECT_SECRET",
        "NESTED_SECRET",
        "free form secret prose",
        "HIDDEN",
    ):
        assert secret not in response.text
    assert "DeepcoinError" in response.text
    assert "E_TIMEOUT" in response.text
    assert "[REDACTED]" in response.text
    assert response.text.count('"raw_length"') >= 4
    assert response.text.count('"sha256"') >= 4


def test_orphan_position_destination_has_safe_focus_contract(tmp_path):
    class OrphanPositionClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "orphan-focus-pos",
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": "3000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_order_history(self):
            return []

    app = create_web_app(
        database_path=tmp_path / "orphan-focus.db",
        deepcoin_client_factory=OrphanPositionClient,
    )
    client = TestClient(app)

    panel = client.get("/positions-panel")
    js = client.get("/static/app.js")

    assert panel.status_code == 200
    assert 'data-position-pos-id="orphan-focus-pos"' in panel.text
    assert 'tabindex="-1"' in panel.text
    assert js.status_code == 200
    focus_start = js.text.index("async function focusRequestedPosition")
    focus_end = js.text.index("\nfunction ", focus_start + 1)
    focus_block = js.text[focus_start:focus_end]
    assert "new URLSearchParams(window.location.search)" in focus_block
    assert "setWorkbenchView('positions')" in focus_block
    assert "await ensureWorkbenchViewLoaded('positions')" in focus_block
    assert "data-position-pos-id" in focus_block
    assert "CSS.escape" in focus_block
    assert "scrollIntoView" in focus_block
    assert ".focus(" in focus_block
    assert "strategy-record-position-target" in focus_block
    assert "click()" not in focus_block


# ---------------------------------------------------------------------------
# Actionable "needs you" rule, counts, Chinese cards and layout fix
# ---------------------------------------------------------------------------

from html.parser import HTMLParser
from pathlib import Path


class _PositionsDeepcoinClient(EmptyDeepcoinClient):
    def __init__(self, pos_ids=()):
        self._pos_ids = tuple(pos_ids)

    def list_positions(self):
        return [
            {
                "instId": "BTC-USDT-SWAP",
                "posId": pos_id,
                "posSide": "long",
                "pos": "1",
            }
            for pos_id in self._pos_ids
        ]


def _add_lifecycle(
    session,
    *,
    chat_id,
    message_id,
    status,
    recognition_failed=False,
    binding_status=None,
    pos_id=None,
    stop_loss=100.0,
    signal_at=None,
):
    signal_at = signal_at or NOW - timedelta(days=1)
    candidate_id = None
    if recognition_failed:
        raw = RawMessage(
            chat_id=chat_id,
            message_id=message_id,
            posted_at=signal_at,
            sender_name="kol",
            text="BTC 做多",
        )
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTCUSDT",
            side="long",
            event_type="entry_signal",
            created_at=signal_at,
        )
        session.add(candidate)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw.id,
                input_kind="text",
                authoritative_model="m",
                authoritative_status="failed",
                authoritative_payload_json="{}",
                agreement_status="agreed",
                created_at=signal_at,
                updated_at=signal_at,
            )
        )
        candidate_id = candidate.id
    binding_id = None
    if binding_status is not None:
        binding = ExecutionBinding(
            strategy_instance_id=f"strategy-{message_id}",
            kol_id=str(chat_id),
            chat_id=chat_id,
            message_id=message_id,
            symbol="BTCUSDT",
            side="long",
            venue="deepcoin",
            pos_id=pos_id,
            status=binding_status,
            created_at=signal_at,
            updated_at=signal_at,
        )
        session.add(binding)
        session.flush()
        binding_id = binding.id
    lifecycle = StrategyLifecycle(
        signal_candidate_id=candidate_id,
        execution_binding_id=binding_id,
        chat_id=chat_id,
        message_id=message_id,
        symbol="BTCUSDT",
        side="long",
        lifecycle_status=status,
        signal_at=signal_at,
        entered_at=signal_at if status in {"entered", "exited"} else None,
        stop_loss=stop_loss,
        updated_at=signal_at,
    )
    session.add(lifecycle)
    session.flush()
    return lifecycle.id


AUTO_CHAT = 77
TRACK_CHAT = 78


def _mixed_client(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    ids = {}
    with session_factory() as session:
        ids["finished_failed"] = _add_lifecycle(
            session, chat_id=AUTO_CHAT, message_id=1, status="expired",
            recognition_failed=True,
        )
        ids["track_only"] = _add_lifecycle(
            session, chat_id=TRACK_CHAT, message_id=2, status="entered"
        )
        ids["auto_unbound"] = _add_lifecycle(
            session, chat_id=AUTO_CHAT, message_id=3, status="entered"
        )
        ids["missing_stop"] = _add_lifecycle(
            session, chat_id=AUTO_CHAT, message_id=4, status="entered",
            binding_status="open", pos_id="pos-d", stop_loss=None,
        )
        ids["pending_failed"] = _add_lifecycle(
            session, chat_id=AUTO_CHAT, message_id=5, status="pending_entry",
            recognition_failed=True,
        )
        ids["pending_plain"] = _add_lifecycle(
            session, chat_id=AUTO_CHAT, message_id=6, status="pending_entry"
        )
        ids["finished_live"] = _add_lifecycle(
            session, chat_id=AUTO_CHAT, message_id=7, status="exited",
            recognition_failed=True, binding_status="open", pos_id="pos-g",
        )
        ids["odd_status"] = _add_lifecycle(
            session, chat_id=AUTO_CHAT, message_id=8, status="weird"
        )
        ids["finished_plain"] = _add_lifecycle(
            session, chat_id=TRACK_CHAT, message_id=9, status="exited"
        )
        session.commit()
    group_config = GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="auto", chat_id=AUTO_CHAT, custom_group_label="自动群",
                trading_mode="auto_trade",
            ),
            TargetGroupConfig(
                chat_title="track", chat_id=TRACK_CHAT, custom_group_label="只通知群",
                trading_mode="notify_only",
            ),
        ]
    )
    app = create_web_app(
        database_path=database_path,
        deepcoin_client_factory=lambda: _PositionsDeepcoinClient(
            ["pos-d", "pos-g", "pos-orphan"]
        ),
        group_config=group_config,
        now_provider=lambda: NOW,
    )
    return TestClient(app), ids


def _api(client, filter_name, **params):
    response = client.get(
        "/api/strategy-records",
        params={"filter_name": filter_name, "limit": 200, **params},
    )
    assert response.status_code == 200
    return response.json()


def test_finished_without_live_position_is_never_needs_attention_but_still_listed(
    tmp_path,
):
    client, ids = _mixed_client(tmp_path)

    attention = _api(client, "needs_attention")
    attention_ids = {row["lifecycle_id"] for row in attention["records"]}
    assert ids["finished_failed"] not in attention_ids
    assert ids["finished_plain"] not in attention_ids

    finished = _api(client, "finished")
    finished_row = next(
        row for row in finished["records"] if row["lifecycle_id"] == ids["finished_failed"]
    )
    # History is untouched: the row still carries the old recognition failure.
    assert finished_row["attention"]["code"] == "recognition_failed"
    assert finished_row["action_required"] is None

    detail = client.get(f"/strategy-records/{ids['finished_failed']}")
    assert detail.status_code == 200
    # The detail page is unchanged: the failed recognition evidence is intact.
    assert "authoritative_status" in detail.text
    assert "failed" in detail.text


def test_notify_only_entered_is_not_attention_and_is_labelled_track_only(tmp_path):
    client, ids = _mixed_client(tmp_path)

    attention_ids = {
        row["lifecycle_id"] for row in _api(client, "needs_attention")["records"]
    }
    assert ids["track_only"] not in attention_ids
    row = next(
        item for item in _api(client, "executing")["records"]
        if item["lifecycle_id"] == ids["track_only"]
    )
    assert row["attention_reasons"] == []
    assert row["track_only"] is True
    html = client.get("/strategy-records", params={"filter": "executing"}).text
    assert "仅跟踪，不下单" in html


def test_auto_trade_entered_without_binding_is_actionable(tmp_path):
    client, ids = _mixed_client(tmp_path)

    row = next(
        item for item in _api(client, "needs_attention")["records"]
        if item["lifecycle_id"] == ids["auto_unbound"]
    )
    assert row["action_required"]["code"] == "entered_without_binding"
    assert row["action_required"]["action_text"] == (
        "需要你：去 Deepcoin 看是否真有这个仓位；没有就不用管，系统会在策略过期后自动收掉"
    )
    assert row["action_required"]["action_href"] == "/?view=positions"


def test_live_position_missing_stop_is_actionable(tmp_path):
    client, ids = _mixed_client(tmp_path)

    row = next(
        item for item in _api(client, "needs_attention")["records"]
        if item["lifecycle_id"] == ids["missing_stop"]
    )
    assert row["action_required"]["code"] == "missing_stop"
    assert row["action_required"]["action_text"] == (
        "需要你：给这个仓位补止损，或手动平仓"
    )
    assert row["action_required"]["action_href"] == "/?view=positions"


def test_pending_entry_recognition_failed_is_actionable_with_detail_link(tmp_path):
    client, ids = _mixed_client(tmp_path)

    attention = _api(client, "needs_attention")["records"]
    row = next(item for item in attention if item["lifecycle_id"] == ids["pending_failed"])
    assert row["action_required"]["action_text"] == (
        "需要你：看一眼原消息，判断要不要手动跟；系统不会替你下单"
    )
    assert row["action_required"]["action_href"] is None
    assert ids["pending_plain"] not in {item["lifecycle_id"] for item in attention}


def test_finished_with_live_position_and_orphan_exchange_position_stay_actionable(
    tmp_path,
):
    client, ids = _mixed_client(tmp_path)

    attention = _api(client, "needs_attention")["records"]
    assert ids["finished_live"] in {row["lifecycle_id"] for row in attention}
    orphan = next(row for row in attention if row["lifecycle_id"] is None)
    assert orphan["action_required"]["code"] == "unattributed_position"
    assert orphan["action_required"]["action_text"] == (
        "需要你：确认这是不是你手动开的仓；不是就去持仓页处理"
    )


def test_every_filter_count_equals_cards_paged_through(tmp_path):
    client, _ = _mixed_client(tmp_path)

    for chat_id in (None, AUTO_CHAT, TRACK_CHAT):
        params = {} if chat_id is None else {"chat_id": chat_id}
        counts = _api(client, "all", **params)["summary_counts"]
        for filter_name in (
            "needs_attention", "all", "executing", "pending_entry", "finished", "other",
        ):
            paged = 0
            page = 1
            while True:
                payload = _api(client, filter_name, page=page, **{**params, "limit": 2})
                paged += len(payload["records"])
                if not payload["has_more"]:
                    break
                page += 1
            assert paged == counts[filter_name], (chat_id, filter_name)
        buckets = sum(
            counts[name] for name in ("executing", "pending_entry", "finished", "other")
        )
        assert buckets == counts["all"], chat_id


def test_status_buckets_partition_all_in_mixed_fixture(tmp_path):
    client, ids = _mixed_client(tmp_path)

    counts = _api(client, "all")["summary_counts"]
    # entered x3 (track_only, auto_unbound, missing_stop) + exited with a real
    # position + orphan exchange record.
    assert counts["executing"] == 5
    assert counts["pending_entry"] == 2
    assert counts["finished"] == 2
    assert counts["other"] == 1
    assert counts["all"] == 10
    other = _api(client, "other")["records"]
    assert [row["lifecycle_id"] for row in other] == [ids["odd_status"]]


def test_exchange_error_does_not_inflate_counts_and_shows_banner(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        _add_lifecycle(session, chat_id=AUTO_CHAT, message_id=1, status="pending_entry")
        _add_lifecycle(
            session, chat_id=AUTO_CHAT, message_id=2, status="entered",
            binding_status="open", pos_id="pos-x",
        )
        session.commit()

    class Broken:
        def list_positions(self):
            raise RuntimeError("down")

    client = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=Broken,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="auto", chat_id=AUTO_CHAT, trading_mode="auto_trade"
                    )
                ]
            ),
        )
    )
    counts = _api(client, "all")["summary_counts"]
    assert counts["needs_attention"] == 0
    assert counts["all"] == 2
    html = client.get("/strategy-records").text
    assert "Deepcoin 暂时连不上，仓位相关检查暂停" in html
    assert "现在没有需要你处理的事。" in html


class _AnchorNestingChecker(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.max_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)

    def handle_endtag(self, tag):
        if tag == "a":
            self.depth -= 1


def test_list_template_renders_plain_language_cards(tmp_path):
    client, ids = _mixed_client(tmp_path)

    html = client.get("/strategy-records", params={"filter": "all"}).text
    assert "每一条被识别成交易策略的喊单都在这里" in html
    assert "实盘观察台" not in html
    assert "需要你处理" in html
    assert "策略 已过期" in html
    assert "策略 已离场" in html
    assert "策略 已入场" in html
    assert "AI 识别失败" in html
    assert "执行 未下单" in html
    assert "归属 未关联仓位" in html
    assert "策略 expired" not in html
    assert "策略 weird" in html
    # 喊单 time in Asia/Shanghai: NOW - 1 day = 07-16 08:30 UTC = 07-16 16:30.
    assert "喊单 07-16 16:30" in html
    assert "最近变化" in html
    assert 'href="/?view=positions"' in html
    assert "去持仓页处理" in html
    assert "其他 1" in html
    checker = _AnchorNestingChecker()
    checker.feed(html)
    assert checker.max_depth == 1

    attention_html = client.get("/strategy-records").text
    assert 'data-strategy-record-filter="needs_attention"' in attention_html
    assert "需要你：给这个仓位补止损，或手动平仓" in attention_html


def test_list_empty_texts_and_other_tab_hidden_when_zero(tmp_path):
    client, _ = _client(tmp_path)

    quiet = client.get("/strategy-records", params={"filter": "pending_entry"}).text
    assert "当前筛选没有策略记录。" in quiet
    assert 'data-strategy-record-filter="other"' not in quiet
    with client.app.state.session_factory() as session:
        session.query(StrategyLifecycle).delete()
        session.query(ExecutionBinding).delete()
        session.commit()
    empty = client.get("/strategy-records").text
    assert "现在没有需要你处理的事。" in empty


def test_strategy_list_css_prevents_card_overlap():
    css = (
        Path(__file__).resolve().parents[1]
        / "src/telegram_kol_research/static/app.css"
    ).read_text(encoding="utf-8")
    assert (
        ".strategy-record-list .home-event-feed {\n"
        "  grid-template-columns: minmax(0, 1fr);\n"
        "  grid-auto-rows: max-content;\n"
        "  align-content: start;\n"
        "}"
    ) in css
    assert ".strategy-record-entry {" in css


def test_loader_and_sql_count_apply_same_actionable_rule_before_limit(tmp_path):
    from telegram_kol_research.strategy_records import (
        count_strategy_records,
        load_strategy_record_summaries,
    )

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        _add_lifecycle(session, chat_id=TRACK_CHAT, message_id=1, status="entered")
        _add_lifecycle(session, chat_id=AUTO_CHAT, message_id=2, status="entered")
        _add_lifecycle(
            session, chat_id=AUTO_CHAT, message_id=3, status="expired",
            recognition_failed=True,
        )
        session.commit()

    kwargs = {"group_labels_by_chat_id": {}, "filter_name": "needs_attention", "limit": 1}
    legacy = load_strategy_record_summaries(session_factory, **kwargs)
    scoped = load_strategy_record_summaries(
        session_factory, auto_trade_chat_ids={AUTO_CHAT}, **kwargs
    )
    # Default keeps the old behaviour (every chat trades); the finished record is
    # never listed either way.
    assert len(legacy) == 1
    assert [row["message_id"] for row in scoped] == [2]
    assert count_strategy_records(session_factory)["needs_attention"] == 2
    assert (
        count_strategy_records(session_factory, auto_trade_chat_ids={AUTO_CHAT})[
            "needs_attention"
        ]
        == 1
    )
