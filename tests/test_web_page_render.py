import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import MediaAsset
from telegram_kol_research.models import RawMessage
from telegram_kol_research.models import SignalCandidate
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal
from telegram_kol_research.web_app import create_web_app
from telegram_kol_research.web_app import _exchange_order_row


def test_logs_page_has_safe_paginated_viewer_controls(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.get("/logs")

    assert response.status_code == 200
    assert "data-log-viewer" in response.text
    assert "data-log-level-filter" in response.text
    assert "data-log-refresh" in response.text
    assert "data-log-list" in response.text


def test_index_page_shows_group_list_and_messages(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=77,
                message_id=1,
                posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                text="hello web",
            )
        )
        session.commit()

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="77",
                        chat_id=77,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            live_listener_status_reason="缂哄皯 Telegram API 鍑嵁",
            now_provider=lambda: datetime(2026, 4, 21, tzinfo=UTC),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert "data-trader-dashboard" in response.text
    assert "data-mobile-work-nav" in response.text
    assert 'data-mobile-work-view="overview"' in response.text
    assert 'data-mobile-work-view="strategies"' in response.text
    assert 'data-mobile-work-view="messages"' in response.text
    assert 'data-mobile-work-view="positions"' in response.text
    assert 'data-mobile-work-view="more"' in response.text
    assert 'data-mobile-work-region="overview"' in response.text
    assert "data-dashboard-tab" in response.text
    assert "data-ai-recognition-prompt" in response.text
    assert "data-ai-recognition-config" in response.text
    assert "data-dashboard-tab" in response.text
    assert "data-ai-recognition-prompt" in response.text
    assert "data-ai-recognition-config" in response.text
    assert "data-ai-model-selection" in response.text
    assert "data-trading-settings-form" in response.text
    assert 'data-dashboard-tab="exchange-positions"' in response.text
    assert 'data-dashboard-panel="exchange-positions"' in response.text
    assert "交易持仓" in response.text
    assert 'data-exchange-position-tabs' in response.text
    assert 'data-exchange-position-tab="positions"' in response.text
    assert 'data-exchange-position-tab="open-orders"' in response.text
    assert 'data-exchange-position-tab="order-history"' in response.text
    assert 'data-exchange-position-tab="position-history"' in response.text
    exchange_tabs = re.search(
        r'<div class="exchange-tab-strip".*?</div>', response.text, re.S
    )
    assert exchange_tabs is not None
    assert exchange_tabs.group(0).index("持仓") < exchange_tabs.group(0).index("当前委托")
    assert exchange_tabs.group(0).index("当前委托") < exchange_tabs.group(0).index(
        "历史委托"
    )
    assert exchange_tabs.group(0).index("历史委托") < exchange_tabs.group(0).index(
        "历史仓位"
    )
    assert 'data-dashboard-tab="recognition-profiles"' in response.text
    assert 'data-dashboard-panel="recognition-profiles"' in response.text
    assert "比特币军长-11分组" in response.text
    assert "junzhang_profile" in response.text
    assert "止损上移到开仓价" in response.text
    assert "默认单笔最大亏损 USDT" in response.text
    assert "data-symbol-selector" in response.text
    assert "data-symbol-search" in response.text
    assert "data-selected-symbol-list" in response.text
    assert "data-selected-symbol-risk-list" in response.text
    assert 'name="symbol_max_loss_usdt"' in response.text
    assert "单点附近市价容忍 %" in response.text
    assert "20.0" in response.text
    assert "DeepSeek V4 Flash" in response.text
    assert "MiMo V2.5" in response.text
    assert "mimo-v2.5" in response.text
    assert "data-ai-model-row" in response.text
    assert "data-active-text-model-id" in response.text
    assert "data-active-image-model-id" in response.text
    assert 'data-strategy-filter="holding"' in response.text
    assert 'data-strategy-filter="pending"' in response.text
    assert 'data-strategy-filter="exited"' in response.text
    assert "data-group-link" in response.text
    assert "data-trader-dashboard" in response.text
    assert "data-detail-panel" in response.text
    assert "77" in response.text
    assert "data-group-link" in response.text
    assert 'data-setting="ai_strategy_enabled"' in response.text
    assert 'data-setting="auto_trade_enabled"' in response.text
    assert "data-toggle-group-automation" in response.text
    assert 'data-setting="ai_strategy_enabled"' in response.text
    assert 'data-setting="auto_trade_enabled"' in response.text
    assert "is-enabled" in response.text
    assert "data-run-recovery-scan" in response.text
    assert "data-recovery-status" in response.text
    assert "data-layout-scroll-panel" in response.text
    assert "Conversation" not in response.text
    assert "data-ai-report-feed" not in response.text
    assert "data-group-prompt-panel" not in response.text
    assert "缇ょ粍鍒嗘瀽鍋忓ソ" not in response.text
    assert "data-group-prompt-panel" not in response.text
    assert "data-ai-workbench" not in response.text
    assert "data-ai-report-feed" not in response.text
    assert "data-ai-history-scroll" not in response.text
    assert "data-ai-composer" not in response.text
    assert "data-clear-ai-history" not in response.text
    assert 'textarea name="question"' not in response.text
    assert "Scope" not in response.text
    assert "Posted after" not in response.text
    assert "data-message-select" not in response.text
    assert "data-message-select" not in response.text
    assert "data-ai-output" not in response.text
    assert "data-ai-sources" not in response.text
    assert "source-preview" not in response.text

    # Message details lazy-loaded 鈥?test via tab endpoint
    msgs_resp = client.get("/groups/77/detail/tab/messages")
    assert msgs_resp.status_code == 200
    assert "hello web" in msgs_resp.text
    assert "2026-04-02 08:00" in msgs_resp.text
    assert "message-sync-state" in msgs_resp.text
    assert "Telegram API" in msgs_resp.text
    assert "2026-04-02 08:00" in msgs_resp.text
    assert "456.0" in msgs_resp.text
    assert "data-message-sticky-header" in msgs_resp.text
    assert "data-message-filter-panel" in msgs_resp.text
    assert "<summary>" in msgs_resp.text


def test_bound_position_close_is_not_rendered_for_unbound_exchange_position(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=77,
                message_id=1,
                posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                text="hello web",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "posId": "pos-live-1",
                    "posSide": "long",
                    "pos": "0.01",
                    "avgPx": "63200",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "ordId": "limit-1",
                    "side": "buy",
                    "ordType": "limit",
                    "state": "live",
                    "px": "63100",
                    "sz": "10",
                }
            ]

        def list_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "ordId": "hist-1",
                    "side": "sell",
                    "ordType": "market",
                    "state": "filled",
                    "px": "63300",
                    "sz": "10",
                }
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "instId": inst_id,
                    "ordId": "trigger-1",
                    "posSide": "long",
                    "side": "sell",
                    "triggerOrderType": "TPSL",
                    "state": "live",
                    "triggerPrice": "62000",
                    "sz": "10",
                }
            ]

        def list_trigger_order_history(self, *, inst_id):
            return [
                {
                    "instId": inst_id,
                    "ordId": "trigger-hist-1",
                    "side": "sell",
                    "triggerOrderType": "TPSL",
                    "state": "cancelled",
                    "triggerPrice": "62000",
                    "sz": "10",
                }
            ]

    client = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert "持仓(1)" in response.text
    assert "当前委托(2)" in response.text
    assert "历史委托(2)" in response.text
    assert "pos pos-live-1" in response.text
    assert "order limit-1" in response.text
    assert "order trigger-1" in response.text
    assert "order hist-1" in response.text
    assert "order trigger-hist-1" in response.text
    assert "止盈止损/平多" in response.text
    assert "data-close-bound-position" not in response.text


def test_bound_position_close_renders_exact_context_for_bound_exchange_position(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=56,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="alice",
            text="BTC long Entry 62400 SL 60800 TP 63600",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="62400",
            stop_loss_text="60800",
            take_profit_text="63600",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:100",
            chat_id=100,
            message_id=56,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            order_id="order-56",
            pos_id="pos-live-1",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                execution_binding_id=binding.id,
                chat_id=100,
                message_id=56,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=raw_message.posted_at,
                entered_at=raw_message.posted_at,
                entry_range_low=62400,
                entry_range_high=62400,
                entry_price_actual=62400,
                stop_loss=60800,
                take_profit="63600",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "posId": "pos-live-1",
                    "posSide": "long",
                    "pos": "0.01",
                    "avgPx": "62400",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="Alpha Group",
                        chat_id=100,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert 'data-exchange-view-mode="list"' in response.text
    assert 'data-exchange-view-mode="grouped"' in response.text
    assert "已绑定" in response.text
    assert "Alpha Group" in response.text
    assert "BTC long" in response.text
    assert 'data-exchange-group-section' in response.text
    close_buttons = re.findall(
        r'<button[^>]+data-close-bound-position[^>]*>', response.text,
    )
    assert close_buttons
    for button in close_buttons:
        assert 'data-pos-id="pos-live-1"' in button
        assert 'data-live-action-symbol="BTC"' in button
        assert 'data-live-action-side="long"' in button
        assert 'data-live-action-size="0.01 contracts BTCUSDT"' in button
        assert 'data-live-action-label="市价全平"' in button
    assert 'data-close-bound-position-status aria-live="polite"' in response.text


def test_exchange_current_order_candidate_attribution(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=200,
            message_id=10,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="bravo",
            text="BTC long 62400-62500 SL 61800 TP 63600",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="62400-62500",
            stop_loss_text="61800",
            take_profit_text="63600",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=200,
                message_id=10,
                symbol="BTC",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=raw_message.posted_at,
                entry_range_low=62400,
                entry_range_high=62500,
                stop_loss=61800,
                take_profit="63600",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return []

        def list_open_orders(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "ordId": "candidate-order-1",
                    "side": "long",
                    "ordType": "limit",
                    "state": "live",
                    "px": "62420",
                    "sz": "10",
                }
            ]

        def list_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="Bravo Group",
                        chat_id=200,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert "可能归属" in response.text
    assert "Bravo Group" in response.text
    assert "BTC long entry 62400-62500" in response.text
    assert "order candidate-order-1" in response.text
    assert 'data-exchange-group-section' in response.text


def test_exchange_current_order_uses_execution_binding_attribution(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=210,
            message_id=20,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="charlie",
            text="BTC long 60700 SL 59800 TP 62000",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="60700",
            stop_loss_text="59800",
            take_profit_text="62000",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            ExecutionBinding(
                kol_id="group:210",
                chat_id=210,
                message_id=20,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                order_id="bound-open-order-1",
                status="open",
            )
        )
        session.commit()

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return []

        def list_open_orders(self, *, inst_id=None):
            return [
                {
                    "instId": "BTCUSDT",
                    "ordId": "bound-open-order-1",
                    "side": "buy",
                    "ordType": "limit",
                    "state": "live",
                    "px": "60700",
                    "sz": "5",
                }
            ]

        def list_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            group_config=GroupConfig(
                groups=[
                    TargetGroupConfig(
                        chat_title="Charlie Group",
                        chat_id=210,
                        ai_strategy_enabled=True,
                        trading_mode="auto_trade",
                    )
                ]
            ),
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert "order bound-open-order-1" in response.text
    assert "已绑定" in response.text
    assert "Charlie Group" in response.text


def test_exchange_tpsl_order_row_uses_non_zero_trigger_price():
    row = _exchange_order_row(
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "tpsl-order-1",
            "posSide": "long",
            "side": "sell",
            "ordPx": "0",
            "triggerPx": "0",
            "slTriggerPrice": "0",
            "tpTriggerPrice": "64100",
            "triggerOrderType": "TPSL",
            "sz": "0",
        },
        source="触发委托",
    )

    assert row["price_text"] == "64100"
    assert row["side"] == "long"
    assert row["order_direction_label"] == "止盈止损/平多"
    assert row["order_direction_side"] == "short"


def test_exchange_order_row_uses_deepcoin_app_direction_labels():
    short_tpsl = _exchange_order_row(
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "tpsl-short-1",
            "posSide": "short",
            "side": "buy",
            "triggerOrderType": "TPSL",
            "tpTriggerPrice": "62600",
            "slTriggerPrice": "63600",
        },
        source="触发委托",
    )
    long_conditional = _exchange_order_row(
        {
            "instId": "ETH-USDT-SWAP",
            "ordId": "conditional-long-1",
            "posSide": "long",
            "side": "buy",
            "triggerOrderType": "Conditional",
            "triggerPx": "1720",
        },
        source="触发委托",
    )
    short_conditional = _exchange_order_row(
        {
            "instId": "ETH-USDT-SWAP",
            "ordId": "conditional-short-1",
            "posSide": "short",
            "side": "sell",
            "triggerOrderType": "Conditional",
            "triggerPx": "1767.5",
        },
        source="触发委托",
    )

    assert short_tpsl["order_direction_label"] == "止盈止损/平空"
    assert short_tpsl["order_direction_side"] == "long"
    assert long_conditional["order_direction_label"] == "条件/开多"
    assert long_conditional["order_direction_side"] == "long"
    assert short_conditional["order_direction_label"] == "条件/开空"
    assert short_conditional["order_direction_side"] == "short"


def test_exchange_order_row_formats_deepcoin_times_in_china_timezone():
    row = _exchange_order_row(
        {
            "instId": "BTC-USDT-SWAP",
            "ordId": "conditional-time-1",
            "posSide": "long",
            "side": "buy",
            "triggerOrderType": "Conditional",
            "cTime": "1783004912000",
            "uTime": "1783009610",
        },
        source="触发委托",
    )

    assert row["created_at"] == "2026-07-02 23:08:32"
    assert row["updated_at"] == "2026-07-03 00:26:50"


def test_exchange_unmatched_order_stays_unassigned(tmp_path):
    database_path = tmp_path / "research.db"

    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return []

        def list_open_orders(self, *, inst_id=None):
            return [
                {
                    "instId": "ETHUSDT",
                    "ordId": "unmatched-order-1",
                    "side": "short",
                    "ordType": "limit",
                    "state": "live",
                    "px": "2500",
                    "sz": "1",
                }
            ]

        def list_order_history(self, *, inst_id=None):
            return []

    client = TestClient(
        create_web_app(
            database_path=database_path,
            deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert "未归属" in response.text
    assert "order unmatched-order-1" in response.text
    assert 'data-exchange-group-section' in response.text


def test_index_page_renders_media_as_compact_preview_links(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=77,
            message_id=1,
            posted_at=datetime(2026, 4, 2, tzinfo=UTC),
            sender_name="Demo Group",
            text="hello media",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="photo",
                local_path="data/media/77/1.jpg",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/")

    assert response.status_code == 200
    # Media previews now lazy-loaded in messages tab
    msgs_resp = client.get("/groups/77/detail/tab/messages")
    assert msgs_resp.status_code == 200
    assert 'class="media-link"' in msgs_resp.text
    assert 'class="message-media-preview"' in msgs_resp.text


def test_index_page_renders_message_cards_with_hierarchy_and_media_labels(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=77,
            message_id=2,
            posted_at=datetime(2026, 4, 2, 12, 30, tzinfo=UTC),
            sender_name="娆ч槼鐏婊氫粨鐝煔€ 11鍒嗙粍",
            text="澶氬崟缁х画鎸佹湁\n璁剧疆濂芥鎹熺偣",
        )
        session.add(raw_message)
        session.flush()
        session.add_all(
            [
                MediaAsset(
                    raw_message_id=raw_message.id,
                    kind="messagemediaphoto",
                    local_path="data/media/77/2.jpg",
                ),
                MediaAsset(
                    raw_message_id=raw_message.id,
                    kind="messagemediadocument",
                    mime_type="video/mp4",
                    local_path="data/media/77/2.mp4",
                ),
            ]
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    # Messages are lazy-loaded 鈥?test via tab endpoint
    response = client.get("/groups/77/detail/tab/messages")

    assert response.status_code == 200
    assert 'class="message-card-header"' in response.text
    assert 'class="message-sender-name"' in response.text
    assert 'class="message-posted-at"' in response.text
    assert 'class="message-id-badge"' in response.text
    assert 'class="message-card-body"' in response.text
    assert 'class="message-text"' in response.text
    assert "娆ч槼鐏婊氫粨鐝煔€ 11鍒嗙粍" in response.text
    assert "澶氬崟缁х画鎸佹湁" in response.text
    assert "/local-media/77/2.jpg" in response.text
    assert 'class="media-token"' in response.text
    assert "/local-media/77/2.mp4" not in response.text


def test_index_page_versions_static_assets_to_avoid_stale_browser_cache(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))
    response = client.get("/")

    assert response.status_code == 200
    assert "/static/app.css?v=" in response.text
    assert "/static/app.js?v=" in response.text


def test_index_page_shows_actionable_session_lock_hint(tmp_path):
    client = TestClient(
        create_web_app(
            database_path=tmp_path / "research.db",
            live_listener_status_reason=(
                "Telegram session data/telegram.session is already in use by another process. "
                "owner pid=12345 status=S command=telegram-kol-research web"
            ),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert "session" in response.text.lower()
    assert "session-status" in response.text
    assert "session-release --pid 12345" in response.text
    assert "owner pid=12345" in response.text


def test_index_page_shows_recovery_decisions_for_manual_review(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="alice",
                    chat_id=100,
                    message_id=55,
                    posted_at=datetime(2026, 6, 12, 8, 0),
                    symbol="BTC",
                    side="long",
                    entry_range=(68000.0, 68200.0),
                    stop_loss_text="67500",
                    take_profit_text="69000 / 70000",
                    trading_mode="auto_trade",
                    max_loss_usdt=100.0,
                ),
                decision=RecoveryDecision(
                    action="manual_review",
                    reason_codes=["current_price_in_entry_range"],
                    entry_range=(68000.0, 68200.0),
                    max_loss_usdt=100.0,
                ),
            )
        ],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/")

    assert response.status_code == 200
    assert "data-trader-dashboard" in response.text
    assert 'data-strategy-filter="holding"' in response.text
    assert 'data-strategy-filter="pending"' in response.text

    # Recovery detail renders in the detail panel per-group
    detail_response = client.get("/groups/100/detail")
    assert detail_response.status_code == 200
    assert "data-recovery-status" in detail_response.text
    assert "data-recovery-status" in detail_response.text


def test_index_page_shows_recovery_execution_preview_queue(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    persist_recovery_evaluations(
        session_factory,
        [
            RecoveryEvaluation(
                signal=RecoverySignal(
                    kol_id="alice",
                    chat_id=100,
                    message_id=55,
                    posted_at=datetime(2026, 6, 12, 8, 0),
                    symbol="BTC",
                    side="long",
                    entry_range=(68000.0, 68200.0),
                    stop_loss_text="67500",
                    take_profit_text="69000 / 70000",
                    trading_mode="auto_trade",
                    max_loss_usdt=100.0,
                ),
                decision=RecoveryDecision(
                    action="eligible_for_recovery_limit_order",
                    reason_codes=["recovery_checks_passed"],
                    entry_range=(68000.0, 68200.0),
                    max_loss_usdt=100.0,
                ),
            )
        ],
        run_at=datetime(2026, 6, 12, 18, 0, tzinfo=UTC),
    )
    apply_recovery_review_decision(
        session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0, tzinfo=UTC),
    )

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/")

    assert response.status_code == 200
    assert 'data-strategy-filter="pending"' in response.text

    # Execution preview renders in the pending tab (lazy-loaded)
    queue_response = client.get("/api/recovery-execution-queue")
    assert queue_response.status_code == 200
    payload = queue_response.json()
    assert payload["items"]
    item = payload["items"][0]
    assert item["symbol"] == "BTC"
    assert item["side"] == "long"
    assert item["entry_range_text"] == "68000-68200"
    assert item["payload_preview"]["open_side"] == "buy"


def test_strategy_mid_panel_shows_take_profit_for_entered_lifecycle(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=56,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="alice",
            text="BTC long Entry 62400 SL 60800 TP 63600/64800",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="62400",
            stop_loss_text="60800",
            take_profit_text="63600/64800",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:100",
            chat_id=100,
            message_id=56,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            order_id="order-56",
            pos_id="pos-56",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                execution_binding_id=binding.id,
                chat_id=100,
                message_id=56,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=raw_message.posted_at,
                entered_at=raw_message.posted_at,
                entry_range_low=62400,
                entry_range_high=62400,
                stop_loss=60800,
                take_profit=None,
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    assert "BTC" in response.text
    assert "62400" in response.text
    assert "60800" in response.text
    assert "63600/64800" in response.text
    assert "数量" in response.text
    assert "0.625 BTC" in response.text
    assert "止损1000U" in response.text
    assert 'data-strategy-card' in response.text
    assert 'class="strategy-card-summary"' in response.text
    assert "strategy-card-details" in response.text
    assert response.text.index('class="strategy-card-summary"') < response.text.index(
        "strategy-card-details"
    )
    summary_match = re.search(
        r'<summary class="strategy-card-summary">(.*?)</summary>',
        response.text,
        re.S,
    )
    assert summary_match
    summary_html = summary_match.group(1)
    assert "BTC" in summary_html
    assert "62400" in summary_html
    assert "63600/64800" in summary_html
    assert "60800" in summary_html
    assert "0.625 BTC" in summary_html
    assert "止损1000U" in summary_html
    assert "事件时间线" not in summary_html


def test_strategy_mid_panel_hides_unbound_entered_lifecycle_from_holding(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=58,
            posted_at=datetime(2026, 7, 1, 13, 56, tzinfo=UTC),
            sender_name="chen",
            text="BTC short 59400-59800 SL 60900 TP 57800/56000",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            StrategyLifecycle(
                chat_id=100,
                message_id=58,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                signal_at=raw_message.posted_at,
                entered_at=raw_message.posted_at,
                entry_range_low=59400,
                entry_range_high=59800,
                stop_loss=60900,
                take_profit="57800/56000",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    assert "BTC" not in response.text
    assert re.search(r'class="strategy-section-count">\s*0\s*</span>', response.text)


def test_strategy_mid_panel_shows_exited_lifecycle_filter(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=100,
            message_id=57,
            posted_at=datetime(2026, 6, 12, 8, 0, tzinfo=UTC),
            sender_name="alice",
            text="BTC long Entry 62400 SL 60800 TP 63600",
        )
        session.add(raw_message)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="62400",
            stop_loss_text="60800",
            take_profit_text="63600",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=100,
                message_id=57,
                symbol="BTC",
                side="long",
                lifecycle_status="exited",
                exit_reason="take_profit",
                signal_at=raw_message.posted_at,
                entered_at=raw_message.posted_at,
                exited_at=raw_message.posted_at,
                entry_range_low=62400,
                entry_range_high=62400,
                entry_price_actual=62400,
                exit_price_actual=63600,
                stop_loss=60800,
                take_profit="63600",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=exited")

    assert response.status_code == 200
    assert 'data-strategy-filter="exited"' in response.text
    assert 'data-strategy-filter="exited"' in response.text
    assert "BTC" in response.text
    assert "63600" in response.text
    assert "63600" in response.text


def test_strategy_mid_panel_keeps_multiple_exited_same_symbol_side(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        for message_id, entry_price, exit_price in [
            (57, 62400, 63600),
            (58, 62500, 63700),
        ]:
            raw_message = RawMessage(
                chat_id=100,
                message_id=message_id,
                posted_at=datetime(2026, 6, 12, 8, message_id - 57, tzinfo=UTC),
                sender_name="alice",
                text=f"BTC long Entry {entry_price} SL 60800 TP {exit_price}",
            )
            session.add(raw_message)
            session.flush()
            candidate = SignalCandidate(
                raw_message_id=raw_message.id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                entry_text=str(entry_price),
                stop_loss_text="60800",
                take_profit_text=str(exit_price),
                parse_source="text_ai",
                confidence=0.91,
                review_status="pending",
            )
            session.add(candidate)
            session.flush()
            session.add(
                StrategyLifecycle(
                    signal_candidate_id=candidate.id,
                    chat_id=100,
                    message_id=message_id,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="exited",
                    exit_reason="take_profit",
                    signal_at=raw_message.posted_at,
                    entered_at=raw_message.posted_at,
                    exited_at=raw_message.posted_at,
                    entry_range_low=entry_price,
                    entry_range_high=entry_price,
                    entry_price_actual=entry_price,
                    exit_price_actual=exit_price,
                    stop_loss=60800,
                    take_profit=str(exit_price),
                )
            )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=exited")

    assert response.status_code == 200
    assert response.text.count("<strong>BTC</strong>") == 2
    assert "62400" in response.text
    assert "62500" in response.text
    assert "63600" in response.text
    assert "63700" in response.text


def test_strategy_mid_panel_shows_cancelled_order_lifecycle(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        original_message = RawMessage(
            chat_id=100,
            message_id=374,
            posted_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            sender_name="mia",
            text=(
                "米娅BTC短线合约交易策略 做空（限价） "
                "进场点位：63200-63500 止损点位：64200 止盈点位：62000"
            ),
        )
        cancel_message = RawMessage(
            chat_id=100,
            message_id=376,
            posted_at=datetime(2026, 6, 19, 9, 24, tzinfo=UTC),
            sender_name="mia",
            text="取消限价，等我后续信号！",
        )
        session.add_all([original_message, cancel_message])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=original_message.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            entry_text="63200-63500",
            stop_loss_text="64200",
            take_profit_text="62000",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=100,
                message_id=374,
                symbol="BTC",
                side="short",
                lifecycle_status="exited",
                exit_reason="cancelled",
                exit_signal_message_id=376,
                signal_at=original_message.posted_at,
                exited_at=cancel_message.posted_at,
                entry_range_low=63200,
                entry_range_high=63500,
                stop_loss=64200,
                take_profit="62000",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=exited")

    assert response.status_code == 200
    assert "取消挂单" in response.text
    assert "原策略" in response.text
    assert "#374" in response.text
    assert "最新事件" in response.text
    assert "#376" in response.text
    assert "pending_entry → cancelled" in response.text
    assert "取消限价，等我后续信号！" in response.text
    assert "离场确认时间" in response.text
    assert "2026-06-19 17:24:00 UTC+8" in response.text


def test_strategy_mid_panel_shows_market_entry_confirmation_event(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        original_message = RawMessage(
            chat_id=100,
            message_id=374,
            posted_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            sender_name="mia",
            text="BTC 做空 限价 63200-63500 止损 64200 止盈 62000",
        )
        entry_message = RawMessage(
            chat_id=100,
            message_id=377,
            posted_at=datetime(2026, 6, 19, 9, 40, tzinfo=UTC),
            sender_name="mia",
            text="BTC 现价 63320 入场",
        )
        session.add_all([original_message, entry_message])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=original_message.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            entry_text="63200-63500",
            stop_loss_text="64200",
            take_profit_text="62000",
            parse_source="text_ai",
            confidence=0.91,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:100",
            chat_id=100,
            message_id=374,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            order_id="order-374",
            pos_id="pos-374",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                execution_binding_id=binding.id,
                chat_id=100,
                message_id=374,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                entry_signal_message_id=377,
                signal_at=original_message.posted_at,
                entered_at=entry_message.posted_at,
                entry_range_low=63200,
                entry_range_high=63500,
                entry_price_actual=63320,
                stop_loss=64200,
                take_profit="62000",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    assert "原策略" in response.text
    assert "#374" in response.text
    assert "最新事件" in response.text
    assert "#377" in response.text
    assert "入场确认" in response.text
    assert "BTC 现价 63320 入场" in response.text
    assert "策略消息接收时间" in response.text
    assert "策略创建/识别时间" in response.text
    assert "入场确认时间" in response.text
    assert "最新事件时间" in response.text
    assert "2026-06-19 12:29:00 UTC+8" in response.text
    assert "2026-06-19 17:40:00 UTC+8" in response.text


def test_strategy_mid_panel_shows_position_management_event(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        original_message = RawMessage(
            chat_id=100,
            message_id=1395,
            posted_at=datetime(2026, 6, 17, 10, 26, tzinfo=UTC),
            sender_name="nick",
            text="BTC 63800-61800附近做多，均价62800，65500-66500-67500止盈，止损61000",
        )
        management_message = RawMessage(
            chat_id=100,
            message_id=1400,
            posted_at=datetime(2026, 6, 18, 8, 36, tzinfo=UTC),
            sender_name="nick",
            text="现价64500附近提前止盈一半带保护",
        )
        session.add_all([original_message, management_message])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=original_message.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            entry_text="63800-61800",
            stop_loss_text="61000",
            take_profit_text="65500/66500/67500",
            parse_source="text_ai",
            confidence=0.95,
            review_status="pending",
        )
        session.add(candidate)
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:100",
            chat_id=100,
            message_id=1395,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            order_id="order-1395",
            pos_id="pos-1395",
            status="active",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                execution_binding_id=binding.id,
                chat_id=100,
                message_id=1395,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=original_message.posted_at,
                entered_at=datetime(2026, 6, 18, 4, 11, tzinfo=UTC),
                entry_range_low=61800,
                entry_range_high=63800,
                entry_price_actual=63794.4,
                stop_loss=61000,
                take_profit="65500/66500/67500",
                management_signal_message_id=1400,
                management_action="partial_take_profit",
                management_note="当前消息要求提前止盈一半并带保护，属于持仓管理更新",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    assert "部分止盈" in response.text
    assert "#1400" in response.text
    assert "提前止盈一半带保护" in response.text
    assert "持仓中" in response.text
    assert "最新事件时间" in response.text
    assert "2026-06-18 16:36:00 UTC+8" in response.text


def test_strategy_mid_panel_shows_chronological_lifecycle_events(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        original_message = RawMessage(
            chat_id=100,
            message_id=2225,
            posted_at=datetime(2026, 6, 26, 3, 42, 37, tzinfo=UTC),
            sender_name="ouyang",
            text="ETH 市价进场 1535 止损 1500 止盈 1615",
        )
        partial_message = RawMessage(
            chat_id=100,
            message_id=2229,
            posted_at=datetime(2026, 6, 26, 3, 52, 14, tzinfo=UTC),
            sender_name="ouyang",
            text="现目前多单盈利18个点，分批止盈30%，多单继续持有！",
        )
        hold_message = RawMessage(
            chat_id=100,
            message_id=2233,
            posted_at=datetime(2026, 6, 26, 6, 18, 46, tzinfo=UTC),
            sender_name="ouyang",
            text="现目前多单获利11点，多单继续持有，等待拉升！",
        )
        session.add_all([original_message, partial_message, hold_message])
        session.flush()
        entry_candidate = SignalCandidate(
            raw_message_id=original_message.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            entry_text="1535",
            stop_loss_text="1500",
            take_profit_text="1615",
            parse_source="text_ai",
            confidence=0.95,
            review_status="pending",
        )
        partial_candidate = SignalCandidate(
            raw_message_id=partial_message.id,
            symbol="ETH",
            side="long",
            event_type="position_update",
            stop_loss_text="1500",
            take_profit_text="1615",
            parse_source="lifecycle_ai",
            confidence=0.85,
            review_status="pending",
        )
        hold_candidate = SignalCandidate(
            raw_message_id=hold_message.id,
            symbol="ETH",
            side="long",
            event_type="position_update",
            stop_loss_text="1500",
            take_profit_text="1615",
            parse_source="lifecycle_ai",
            confidence=0.85,
            review_status="pending",
        )
        session.add_all([entry_candidate, partial_candidate, hold_candidate])
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=entry_candidate.id,
                chat_id=100,
                message_id=2225,
                symbol="ETH",
                side="long",
                lifecycle_status="exited",
                exit_reason="stop_loss",
                signal_at=original_message.posted_at,
                entered_at=datetime(2026, 6, 26, 3, 43, tzinfo=UTC),
                exited_at=datetime(2026, 6, 26, 12, 39, tzinfo=UTC),
                entry_range_low=1535,
                entry_range_high=1535,
                entry_price_actual=1535,
                exit_price_actual=1535,
                stop_loss=1535,
                take_profit="1615",
                management_signal_message_id=2229,
                management_action="partial_take_profit",
                management_note="分批止盈30%，多单继续持有",
            )
        )
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    response = client.get("/groups/100/strategy-mid-panel?filter=exited")

    assert response.status_code == 200
    assert "事件时间线" in response.text
    assert "原策略" in response.text
    assert "入场确认" in response.text
    assert "分批止盈30%" in response.text
    assert "多单继续持有，等待拉升" in response.text
    assert "止损触发" in response.text
    assert (
        response.text.index("#2225")
        < response.text.index("分批止盈30%")
        < response.text.index("多单继续持有，等待拉升")
        < response.text.index("止损触发")
    )
