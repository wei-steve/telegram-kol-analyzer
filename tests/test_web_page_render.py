from datetime import UTC, datetime

from fastapi.testclient import TestClient

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import MediaAsset
from telegram_kol_research.models import RawMessage
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal
from telegram_kol_research.web_app import create_web_app


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
            live_listener_status_reason="缺少 Telegram API 凭据",
            now_provider=lambda: datetime(2026, 4, 21, tzinfo=UTC),
        )
    )
    response = client.get("/")

    assert response.status_code == 200
    assert "交易执行台" in response.text
    assert "主界面" in response.text
    assert "AI识别提示词" in response.text
    assert "AI配置" in response.text
    assert "data-dashboard-tab" in response.text
    assert "data-ai-recognition-prompt" in response.text
    assert "data-ai-recognition-config" in response.text
    assert "今日 / 48小时策略" in response.text
    assert "待人工审核" in response.text
    assert "可模拟提交" in response.text
    assert "阻断项" in response.text
    assert "群组 / KOL 策略" in response.text
    assert "策略执行队列" in response.text
    assert "策略详情 / 原始消息" in response.text
    assert "data-trader-dashboard" in response.text
    assert "data-strategy-worklist" in response.text
    assert "data-strategy-detail-panel" in response.text
    assert "trader-status-strip" not in response.text
    assert "hello web" in response.text
    assert "77" in response.text
    assert "data-group-link" in response.text
    assert "AI识别策略" in response.text
    assert "自动交易" in response.text
    assert "data-toggle-group-automation" in response.text
    assert 'data-setting="ai_strategy_enabled"' in response.text
    assert 'data-setting="auto_trade_enabled"' in response.text
    assert "is-enabled" in response.text
    assert "data-run-recovery-scan" in response.text
    assert "data-recovery-status" in response.text
    assert "data-layout-scroll-panel" in response.text
    assert "AI Analysis" not in response.text
    assert "Conversation" not in response.text
    assert "研究报告流" not in response.text
    assert "该群默认提示词" not in response.text
    assert "群组分析偏好" not in response.text
    assert "data-group-prompt-panel" not in response.text
    assert "data-ai-workbench" not in response.text
    assert "data-ai-report-feed" not in response.text
    assert "data-ai-history-scroll" not in response.text
    assert "data-ai-composer" not in response.text
    assert "data-clear-ai-history" not in response.text
    assert 'textarea name="question"' not in response.text
    assert "Scope" not in response.text
    assert "Posted after" not in response.text
    assert "默认分析当前群最近 50 条消息" not in response.text
    assert "data-message-select" not in response.text
    assert "data-ai-output" not in response.text
    assert "data-ai-sources" not in response.text
    assert "source-preview" not in response.text
    assert "最后入库时间：2026-04-02 08:00" in response.text
    assert "实时监听未启用" in response.text
    assert "缺少 Telegram API 凭据" in response.text
    assert "数据库最新消息时间：2026-04-02 08:00" in response.text
    assert "数据新鲜度：456.0 小时未刷新" in response.text
    assert "刷新模式：仅本地快照" not in response.text
    assert "data-message-sticky-header" in response.text
    assert "data-message-filter-panel" in response.text
    assert "<summary>筛选</summary>" in response.text
    assert '<details class="message-filter-panel" data-message-filter-panel>' in response.text


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
    assert 'class="media-link"' in response.text
    assert 'class="message-media-preview"' in response.text


def test_index_page_renders_message_cards_with_hierarchy_and_media_labels(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=77,
            message_id=2,
            posted_at=datetime(2026, 4, 2, 12, 30, tzinfo=UTC),
            sender_name="欧阳火箭滚仓班🚀 11分组",
            text="多单继续持有\n设置好止损点",
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
    response = client.get("/")

    assert response.status_code == 200
    assert 'class="message-card-header"' in response.text
    assert 'class="message-sender-name"' in response.text
    assert 'class="message-posted-at"' in response.text
    assert 'class="message-id-badge"' in response.text
    assert 'class="message-card-body"' in response.text
    assert 'class="message-text"' in response.text
    assert "欧阳火箭滚仓班🚀 11分组" in response.text
    assert "多单继续持有" in response.text
    assert "/local-media/77/2.jpg" in response.text
    assert 'class="media-token">[视频]</span>' in response.text
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
    assert "Telegram session 被占用" in response.text
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
    assert "交易工单" in response.text
    assert "策略执行队列" in response.text
    assert "alice" in response.text
    assert "BTC long" in response.text
    assert "待人工审核" in response.text
    assert "恢复扫描" in response.text
    assert "alice" in response.text
    assert "BTC long" in response.text
    assert "manual_review" in response.text
    assert "current_price_in_entry_range" in response.text
    assert "68000-68200" in response.text
    assert "100" in response.text
    assert "待审核" in response.text
    assert "同意补挂单" in response.text
    assert "忽略" in response.text
    assert "data-review-recovery" in response.text


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
    assert "执行前队列" in response.text
    assert "pending_execution" in response.text
    assert "BTC-USDT" in response.text
    assert "BTC-USDT-SWAP" in response.text
    assert "缺少规格校验" in response.text
    assert "base_asset_estimate" in response.text
    assert "contract_size_unverified" in response.text
    assert "最终确认" in response.text
    assert "data-confirm-recovery-order" in response.text
    assert "data-recovery-order-confirm-status" in response.text
    assert "模拟提交" in response.text
    assert "data-simulate-recovery-submit" in response.text
    assert "data-recovery-submit-gate-status" in response.text
    assert "0.071429" in response.text
    assert "buy" in response.text
    assert "68000-68200" in response.text
    assert "data-recovery-execution-queue" in response.text
