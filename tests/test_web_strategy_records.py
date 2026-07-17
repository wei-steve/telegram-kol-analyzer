from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
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
            attribution_evidence_json='{"evidence_type":"direct_fill"}',
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


def test_strategy_record_detail_is_semantic_read_only_and_escapes_evidence(tmp_path):
    client, lifecycle_id = _client(tmp_path)

    response = client.get(f"/strategy-records/{lifecycle_id}")

    assert response.status_code == 200
    assert "data-strategy-record-detail" in response.text
    assert 'data-strategy-detail-section="overview"' in response.text
    assert 'data-strategy-detail-section="timeline"' in response.text
    assert 'data-strategy-detail-section="execution"' in response.text
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
        session.add_all(
            [
                StrategyLifecycle(
                    chat_id=77,
                    message_id=10_000 + index,
                    symbol="BTCUSDT",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=NOW,
                    updated_at=NOW,
                )
                for index in range(201)
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
    assert "Deepcoin 仓位快照暂不可用" in list_response.text
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
