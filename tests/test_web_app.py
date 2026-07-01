import re
from datetime import datetime

from fastapi.testclient import TestClient

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.group_config import GroupConfig
from telegram_kol_research.group_config import TargetGroupConfig
from telegram_kol_research.recovery_decisions import list_recovery_decisions
from telegram_kol_research.recovery_decisions import apply_recovery_review_decision
from telegram_kol_research.recovery_decisions import persist_recovery_evaluations
from telegram_kol_research.recovery_runner import RecoveryDryRunResult
from telegram_kol_research.recovery_scan import RecoveryDecision
from telegram_kol_research.recovery_scan import RecoveryEvaluation
from telegram_kol_research.recovery_scan import RecoverySignal
from telegram_kol_research.recovery_live_submit import enqueue_recovery_trade_signal
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.web_app import create_web_app
from telegram_kol_research.group_config import load_group_config
from telegram_kol_research.models import RawMessage
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import SignalCandidate
from telegram_kol_research.models import StrategyLifecycle


def test_root_page_renders_successfully(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200


def test_group_automation_api_updates_config_file(tmp_path):
    database_path = tmp_path / "research.db"
    config_path = tmp_path / "groups.yaml"
    config_path.write_text(
        "groups:\n"
        "  - chat_title: Demo Group\n"
        "    chat_id: 77\n"
        "    ai_strategy_enabled: false\n"
        "    trading_mode: notify_only\n",
        encoding="utf-8",
    )
    app = create_web_app(
        database_path=database_path,
        group_config=load_group_config(config_path),
        group_config_path=config_path,
    )
    client = TestClient(app)

    response = client.post(
        "/api/groups/77/automation",
        json={"ai_strategy_enabled": True, "auto_trade_enabled": True},
    )

    assert response.status_code == 200
    assert response.json()["ai_strategy_enabled"] is True
    assert response.json()["auto_trade_enabled"] is True
    reloaded = load_group_config(config_path).groups[0]
    assert reloaded.ai_strategy_enabled is True
    assert reloaded.trading_mode == "auto_trade"


def test_trading_settings_api_persists_runtime_risk_defaults(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    client = TestClient(app)

    response = client.post(
        "/api/trading-settings",
        json={
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 150,
            "daily_max_loss_usdt": 600,
            "max_concurrent_positions": 5,
            "max_market_entry_deviation_pct": 0.2,
            "min_ai_confidence": 0.8,
            "allowed_symbols": "BTC,ETH,SOL",
            "entry_range_order_style": "conservative",
            "take_profit_allocations": "50,30,20",
            "move_stop_to_breakeven_after_tp1": True,
            "allow_vision_auto_trade": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["default_max_loss_usdt"] == 150.0
    assert response.json()["allowed_symbols"] == ["BTC", "ETH", "SOL"]

    reloaded = client.get("/api/trading-settings")
    assert reloaded.status_code == 200
    assert reloaded.json()["auto_trade_enabled"] is True
    assert reloaded.json()["take_profit_allocations"] == [50.0, 30.0, 20.0]


def test_execution_dashboard_lists_global_strategy_states(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="Demo Auto Group",
                    chat_id=88,
                    custom_group_label="演示群",
                    symbol_whitelist=["BTC", "ETH"],
                )
            ]
        ),
    )
    with app.state.session_factory() as session:
        raw = RawMessage(
            chat_id=88,
            message_id=10,
            sender_name="Alice",
            posted_at=datetime(2026, 6, 30, 9, 0),
            text="BTC short 60000 SL 61000 TP 59000",
        )
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            entry_text="60000",
            stop_loss_text="61000",
            take_profit_text="59000",
            confidence=0.9,
        )
        session.add(candidate)
        session.flush()
        session.add(
            StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=88,
                message_id=10,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 6, 30, 9, 0),
                entered_at=datetime(2026, 6, 30, 9, 1),
                entry_price_actual=60000,
                stop_loss=61000,
                take_profit="59000",
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/execution?status=holding")

    assert response.status_code == 200
    assert "执行中策略" in response.text
    assert "演示群 · #10" in response.text
    assert "BTC" in response.text
    assert "标记已手动平仓" in response.text


def test_execution_dashboard_defaults_to_deepcoin_live_positions(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-btc",
                    "posSide": "short",
                    "pos": "9",
                    "avgPx": "59761.2",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-eth",
                    "posSide": "long",
                    "pos": "5.2",
                    "avgPx": "1592.8",
                },
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
    )
    with app.state.session_factory() as session:
        session.add(
            ExecutionBinding(
                kol_id="group:88",
                chat_id=88,
                message_id=10,
                symbol="ETH",
                side="long",
                status="active",
                pos_id="pos-eth",
                order_id="order-eth",
            )
        )
        session.add(
            StrategyLifecycle(
                chat_id=99,
                message_id=20,
                symbol="BTC",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 6, 30, 8, 0),
                entered_at=datetime(2026, 6, 30, 8, 1),
                entry_price_actual=59760,
                stop_loss=61300,
                take_profit="59600",
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/execution")

    assert response.status_code == 200
    assert "实盘持仓" in response.text
    assert "<strong>2</strong>" in response.text
    assert "未绑定实盘仓位" in response.text
    assert "pos-btc" in response.text
    assert "可能归属" in response.text
    assert "绑定" in response.text


def test_execution_dashboard_uses_pending_tpsl_orders_for_live_protection(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-protected",
                    "posSide": "short",
                    "pos": "5",
                    "avgPx": "59604.5",
                    "cTime": "1782801429000",
                    "slTriggerPx": "61500",
                    "tpTriggerPx": "57300",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-unprotected",
                    "posSide": "short",
                    "pos": "9",
                    "avgPx": "59761.2",
                    "cTime": "1782785675000",
                    "slTriggerPx": "61500",
                    "tpTriggerPx": "57300",
                },
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posSide": "short",
                    "sz": "5",
                    "cTime": "1782801429000",
                    "triggerOrderType": "TPSL",
                    "slTriggerPrice": "61500",
                    "tpTriggerPrice": "57300",
                }
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
    )

    client = TestClient(app)
    response = client.get("/execution")

    assert response.status_code == 200
    assert response.text.count("止损: 61500") == 1
    assert response.text.count("止盈: 57300") == 1
    assert "pos-protected" in response.text
    assert "pos-unprotected" in response.text
    assert response.text.count("无保护单") == 1


def test_execution_dashboard_matches_zero_size_tpsl_orders_by_position_time(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-eth",
                    "posSide": "long",
                    "pos": "5.2",
                    "avgPx": "1592.88",
                    "cTime": "1782788831000",
                },
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            assert inst_id == "ETH-USDT-SWAP"
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posSide": "long",
                    "sz": "0",
                    "cTime": "1782788831000",
                    "triggerOrderType": "TPSL",
                    "slTriggerPrice": "0",
                    "tpTriggerPrice": "1680",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "posSide": "long",
                    "sz": "0",
                    "cTime": "1782788831000",
                    "triggerOrderType": "TPSL",
                    "slTriggerPrice": "1555",
                    "tpTriggerPrice": "0",
                },
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
    )

    client = TestClient(app)
    response = client.get("/execution")

    assert response.status_code == 200
    assert "止损: 1555" in response.text
    assert "止盈: 1680" in response.text
    assert "无保护单" not in response.text


def test_manual_close_api_marks_lifecycle_and_binding_closed(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    with app.state.session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=10, text="BTC short 60000 SL 61000")
        session.add(raw)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 30, 9, 0),
            entered_at=datetime(2026, 6, 30, 9, 1),
        )
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="short",
            status="active",
            pos_id="pos-1",
            order_id="order-1",
            last_exchange_status="position_active",
        )
        session.add_all([lifecycle, binding])
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id

    client = TestClient(app)
    response = client.post(
        f"/api/strategy-lifecycles/{lifecycle_id}/manual-close",
        json={"exit_price": 59500, "note": "test"},
    )

    assert response.status_code == 200
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)

        assert lifecycle.lifecycle_status == "exited"
        assert lifecycle.exit_reason == "manual"
        assert lifecycle.exit_price_actual == 59500
        assert binding.status == "closed"
        assert binding.last_exchange_status.startswith("manual_closed_by_user")


def test_execution_sync_api_marks_missing_deepcoin_position_closed(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return []

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        now_provider=lambda: datetime(2026, 6, 30, 10, 0),
    )
    with app.state.session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 30, 9, 0),
            entered_at=datetime(2026, 6, 30, 9, 1),
        )
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="short",
            status="active",
            pos_id="pos-missing",
            order_id="order-1",
        )
        session.add_all([lifecycle, binding])
        session.commit()
        lifecycle_id = lifecycle.id

    client = TestClient(app)
    response = client.post("/api/execution/sync-deepcoin")

    assert response.status_code == 200
    assert response.json()["manually_closed"] == 1
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

        assert lifecycle.lifecycle_status == "exited"
        assert lifecycle.exit_reason == "manual"


def test_bind_live_position_api_attaches_unbound_position_to_lifecycle(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-btc",
                    "posSide": "short",
                    "pos": "9",
                    "avgPx": "59761.2",
                }
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        now_provider=lambda: datetime(2026, 6, 30, 10, 0),
    )
    with app.state.session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 30, 9, 0),
            entered_at=datetime(2026, 6, 30, 9, 1),
            entry_price_actual=59761.2,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    client = TestClient(app)
    response = client.post(
        "/api/execution/bind-live-position",
        json={"pos_id": "pos-btc", "lifecycle_id": lifecycle_id},
    )

    assert response.status_code == 200
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, response.json()["binding_id"])

        assert binding.status == "active"
        assert binding.pos_id == "pos-btc"
        assert binding.last_exchange_status == "manual_bound_live_position"
        assert lifecycle.execution_binding_id == binding.id


def test_bind_live_position_api_rejects_wrong_attribution_candidate(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-btc",
                    "posSide": "short",
                    "pos": "9",
                    "avgPx": "58100",
                }
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
    )
    with app.state.session_factory() as session:
        wrong_lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 30, 9, 0),
            entered_at=datetime(2026, 6, 30, 9, 1),
            entry_price_actual=59000,
        )
        session.add(wrong_lifecycle)
        session.commit()
        lifecycle_id = wrong_lifecycle.id

    client = TestClient(app)
    response = client.post(
        "/api/execution/bind-live-position",
        json={"pos_id": "pos-btc", "lifecycle_id": lifecycle_id},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "live position does not match this KOL strategy"


def test_execution_dashboard_disables_ambiguous_live_position_bindings(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-btc",
                    "posSide": "long",
                    "pos": "9",
                    "avgPx": "58100",
                }
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
    )
    with app.state.session_factory() as session:
        session.add_all(
            [
                StrategyLifecycle(
                    chat_id=88,
                    message_id=10,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="entered",
                    signal_at=datetime(2026, 6, 30, 9, 0),
                    entered_at=datetime(2026, 6, 30, 9, 1),
                    entry_price_actual=58100,
                ),
                StrategyLifecycle(
                    chat_id=99,
                    message_id=20,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="entered",
                    signal_at=datetime(2026, 6, 30, 9, 5),
                    entered_at=datetime(2026, 6, 30, 9, 6),
                    entry_price_actual=58102,
                ),
            ]
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/execution")

    assert response.status_code == 200
    assert response.text.count("需核对") == 2
    assert 'title="归属不唯一或置信度不足，禁止一键绑定"' in response.text


def test_message_recognition_api_updates_message_result(tmp_path):
    database_path = tmp_path / "research.db"
    app = create_web_app(
        database_path=database_path,
        ai_recognition_config_path=tmp_path / "ai_recognition.yaml",
    )
    with app.state.session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=1,
            text="BTC long 68000-68200 SL 67500 TP 69000",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    client = TestClient(app)
    response = client.post(f"/api/messages/{raw_message_id}/recognize")

    assert response.status_code == 200
    assert response.json()["status"] == "是策略"
    refreshed = client.get("/groups/88/messages")
    assert "AI识别结果：是策略" in refreshed.text
    assert "策略内容：" in refreshed.text
    assert "BTC long" in refreshed.text


def test_message_recognition_api_runs_auto_trade_executor_after_recognition(tmp_path):
    database_path = tmp_path / "research.db"
    app = create_web_app(
        database_path=database_path,
        ai_recognition_config_path=tmp_path / "ai_recognition.yaml",
    )
    calls = []
    app.state.auto_trade_executor = lambda raw_message_id: (
        calls.append(raw_message_id)
        or {"status": "submitted", "management_action": "close_position"}
    )
    with app.state.session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=2,
            text="BTC short profit all out",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    client = TestClient(app)
    response = client.post(f"/api/messages/{raw_message_id}/recognize")

    assert response.status_code == 200
    assert calls == [raw_message_id]
    assert response.json()["auto_trade"] == {
        "status": "submitted",
        "management_action": "close_position",
    }


def test_strategy_mid_panel_loads_only_visible_strategy_list(tmp_path, monkeypatch):
    calls: list[str] = []

    def fake_holding(*args, **kwargs):
        calls.append("holding")
        return [
            {
                "chat_id": 88,
                "symbol": "BTC",
                "side": "long",
                "entry_text": "68000",
                "stop_loss_text": "67500",
                "take_profit_text": "69000",
            }
        ]

    def fake_pending(*args, **kwargs):
        calls.append("pending")
        return []

    def fake_exited(*args, **kwargs):
        calls.append("exited")
        return []

    monkeypatch.setattr("telegram_kol_research.web_app.list_holding_strategies", fake_holding)
    monkeypatch.setattr("telegram_kol_research.web_app.list_pending_strategies", fake_pending)
    monkeypatch.setattr("telegram_kol_research.web_app.list_exited_strategies", fake_exited)
    monkeypatch.setattr(
        "telegram_kol_research.web_app.load_lifecycle_counts",
        lambda *args, **kwargs: {"entered": 1, "pending_entry": 0, "exited": 0},
    )

    app = create_web_app(database_path=tmp_path / "research.db")
    client = TestClient(app)

    response = client.get("/groups/88/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    assert calls == ["holding"]

    calls.clear()
    response = client.get("/groups/88/strategy-mid-panel?filter=exited")

    assert response.status_code == 200
    assert calls == ["exited"]

    calls.clear()
    response = client.get("/groups/88/strategy-mid-panel?filter=pending")

    assert response.status_code == 200
    assert calls == ["pending"]


def test_strategy_mid_panel_pending_kpi_matches_actionable_list(tmp_path):
    database_path = tmp_path / "research.db"
    session_factory = create_session_factory(database_path)
    with session_factory() as session:
        session.add_all(
            [
                StrategyLifecycle(
                    chat_id=88,
                    message_id=1,
                    symbol="QQ",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 6, 12, 8, 0),
                ),
                StrategyLifecycle(
                    chat_id=88,
                    message_id=2,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 6, 12, 8, 1),
                ),
                StrategyLifecycle(
                    chat_id=88,
                    message_id=3,
                    symbol="BTC",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 6, 12, 8, 2),
                    entry_range_low=6.22,
                    entry_range_high=6.27,
                ),
                StrategyLifecycle(
                    chat_id=88,
                    message_id=4,
                    symbol="ETH",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 6, 12, 8, 3),
                    entry_range_low=3200,
                    entry_range_high=3220,
                ),
                StrategyLifecycle(
                    chat_id=89,
                    message_id=5,
                    symbol="SOL",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 6, 12, 8, 4),
                    entry_range_low=180,
                    entry_range_high=181,
                ),
            ]
        )
        session.commit()

    app = create_web_app(
        database_path=database_path,
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="Demo",
                    chat_id=88,
                    symbol_whitelist=["BTC"],
                )
            ]
        ),
    )
    client = TestClient(app)

    response = client.get("/groups/88/strategy-mid-panel?filter=pending")

    assert response.status_code == 200
    assert re.search(
        r'class="kpi-badge kpi-pending[^"]*"[^>]*>\s*<strong>0</strong>',
        response.text,
    )
    assert re.search(r'class="strategy-section-count">\s*0\s*</span>', response.text)
    assert "ETH" not in response.text
    assert "BTC" not in response.text


def test_group_detail_logs_route_timings(tmp_path, caplog):
    database_path = tmp_path / "research.db"
    app = create_web_app(database_path=database_path)
    with app.state.session_factory() as session:
        session.add(
            RawMessage(
                chat_id=88,
                message_id=1,
                sender_name="Demo",
                text="BTC long",
            )
        )
        session.commit()

    caplog.set_level("INFO", logger="uvicorn.error")
    client = TestClient(app)

    response = client.get("/groups/88/detail")

    assert response.status_code == 200
    assert "web_perf route=/groups/{chat_id}/detail chat_id=88" in caplog.text
    assert "messages_ms=" in caplog.text
    assert "template_ms=" in caplog.text


def test_strategy_mid_panel_logs_route_timings(tmp_path, caplog):
    app = create_web_app(database_path=tmp_path / "research.db")
    caplog.set_level("INFO", logger="uvicorn.error")
    client = TestClient(app)

    response = client.get("/groups/88/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    assert "web_perf route=/groups/{chat_id}/strategy-mid-panel chat_id=88" in caplog.text
    assert "filter=holding" in caplog.text
    assert "lifecycle_counts_ms=" in caplog.text
    assert "holding_ms=" in caplog.text


def test_ai_recognition_config_api_saves_prompt(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=config_path,
    )
    client = TestClient(app)

    response = client.post(
        "/api/ai-recognition-config",
        json={
            "recognition_prompt": "只识别明确策略。",
            "lifecycle_event_prompt": "识别生命周期事件。",
            "mimo_direct_prompt": "直接阅读图片和文字。",
        },
    )

    assert response.status_code == 200
    assert response.json()["recognition_prompt"].startswith("只识别明确策略。")
    assert response.json()["lifecycle_event_prompt"].startswith("识别生命周期事件。")
    assert "价格简写" in response.json()["lifecycle_event_prompt"]
    assert response.json()["mimo_direct_prompt"].startswith("直接阅读图片和文字。")
    assert "\u5386\u53f2\u7b56\u7565\u622a\u56fe" in response.json()["mimo_direct_prompt"]
    page = client.get("/")
    assert "只识别明确策略。" in page.text
    assert "识别生命周期事件。" in page.text
    assert "直接阅读图片和文字。" in page.text


def test_index_page_renders_registered_ai_prompts(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=config_path,
    )
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert 'data-ai-prompt-id="recognition_prompt"' in response.text
    assert 'data-ai-prompt-id="lifecycle_event_prompt"' in response.text
    assert 'data-ai-prompt-id="mimo_direct_prompt"' in response.text
    assert 'data-ai-prompt-input="recognition_prompt"' in response.text
    assert 'data-ai-prompt-input="lifecycle_event_prompt"' in response.text
    assert 'data-ai-prompt-input="mimo_direct_prompt"' in response.text
    assert 'name="recognition_prompt"' in response.text
    assert 'name="lifecycle_event_prompt"' in response.text
    assert 'name="mimo_direct_prompt"' in response.text


def test_ai_recognition_config_api_returns_registered_ai_prompts(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=config_path,
    )
    client = TestClient(app)

    response = client.post(
        "/api/ai-recognition-config",
        json={
            "recognition_prompt": "Only strict strategies.",
            "lifecycle_event_prompt": "Only lifecycle events.",
            "mimo_direct_prompt": "Only multimodal strategies.",
        },
    )

    assert response.status_code == 200
    prompt_ids = {item["id"] for item in response.json()["prompts"]}
    assert {"recognition_prompt", "lifecycle_event_prompt", "mimo_direct_prompt"} <= prompt_ids
    prompt_values = {item["id"]: item["value"] for item in response.json()["prompts"]}
    assert prompt_values["recognition_prompt"].startswith("Only strict strategies.")
    assert prompt_values["lifecycle_event_prompt"].startswith("Only lifecycle events.")
    assert prompt_values["mimo_direct_prompt"].startswith("Only multimodal strategies.")


def test_ai_recognition_config_api_saves_mimo_provider(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=config_path,
    )
    client = TestClient(app)

    response = client.post(
        "/api/ai-recognition-config",
        json={
            "recognition_prompt": "Only strict strategies.",
            "text_provider": {
                "base_url": "https://api.deepseek.com",
                "api_key": "deepseek-key",
                "model": "deepseek-v4-flash",
                "timeout_seconds": 60,
            },
            "image_provider": {
                "base_url": "https://api.xiaomimimo.com/v1",
                "api_key": "mimo-key",
                "model": "mimo-v2.5",
                "timeout_seconds": 60,
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["text_provider"]["model"] == "deepseek-v4-flash"
    assert payload["image_provider"]["base_url"] == "https://api.xiaomimimo.com/v1"
    assert payload["image_provider"]["model"] == "mimo-v2.5"
    assert payload["active_text_model_id"] == "deepseek-v4-flash"
    assert payload["active_image_model_id"] == "mimo-v2.5"
    assert any(model["id"] == "mimo-v2.5" for model in payload["ai_models"])


def test_ai_recognition_config_api_saves_model_list_and_active_selection(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=config_path,
    )
    client = TestClient(app)

    response = client.post(
        "/api/ai-recognition-config",
        json={
            "recognition_prompt": "Only strict strategies.",
            "active_text_model_id": "deepseek-v4-flash",
            "active_image_model_id": "mimo-v2.5",
            "ai_models": [
                {
                    "id": "deepseek-v4-flash",
                    "label": "DeepSeek V4 Flash",
                    "base_url": "https://api.deepseek.com",
                    "api_key": "deepseek-key",
                    "model": "deepseek-v4-flash",
                    "timeout_seconds": 60,
                    "supports_text": True,
                    "supports_image": False,
                },
                {
                    "id": "mimo-v2.5",
                    "label": "MiMo V2.5",
                    "base_url": "https://api.xiaomimimo.com/v1",
                    "api_key": "mimo-key",
                    "model": "mimo-v2.5",
                    "timeout_seconds": 60,
                    "supports_text": True,
                    "supports_image": True,
                },
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["active_text_model_id"] == "deepseek-v4-flash"
    assert payload["active_image_model_id"] == "mimo-v2.5"
    assert payload["text_provider"]["api_key"] == "deepseek-key"
    assert payload["image_provider"]["api_key"] == "mimo-key"


def test_recovery_dry_run_api_persists_decisions_with_configured_gate_provider(tmp_path):
    captured = {}
    market_data = object()

    def fake_runner(**kwargs):
        captured.update(kwargs)
        return RecoveryDryRunResult(
            total_candidates=2,
            action_counts={
                "manual_review": 1,
                "eligible_for_recovery_limit_order": 1,
            },
            evaluations=[],
        )

    app = create_web_app(
        database_path=tmp_path / "research.db",
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="Auto Group",
                    chat_id=100,
                    trading_mode="auto_trade",
                )
            ]
        ),
        recovery_runner=fake_runner,
        recovery_market_data_factory=lambda: market_data,
        now_provider=lambda: datetime(2026, 6, 12, 18, 0),
    )
    client = TestClient(app)

    response = client.post("/api/recovery-dry-run")

    assert response.status_code == 200
    assert response.json() == {
        "total_candidates": 2,
        "persisted_decisions": 0,
        "action_counts": {
            "manual_review": 1,
            "eligible_for_recovery_limit_order": 1,
        },
    }
    assert captured["group_config"].groups[0].chat_title == "Auto Group"
    assert captured["market_data"] is market_data
    assert captured["persist"] is True
    assert captured["lookback_hours"] == 48


def test_recovery_review_api_records_manual_decision(tmp_path):
    database_path = tmp_path / "research.db"
    app = create_web_app(
        database_path=database_path,
        now_provider=lambda: datetime(2026, 6, 12, 19, 0),
    )
    persist_recovery_evaluations(
        app.state.session_factory,
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
        run_at=datetime(2026, 6, 12, 18, 0),
    )
    client = TestClient(app)

    response = client.post(
        "/api/recovery-decisions/review",
        json={
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
            "review_status": "approved_for_order",
            "note": "人工确认补挂单",
        },
    )

    assert response.status_code == 200
    assert response.json()["review_status"] == "approved_for_order"
    rows = list_recovery_decisions(app.state.session_factory, limit=10)
    assert rows[0]["review_status"] == "approved_for_order"
    assert rows[0]["review_note"] == "人工确认补挂单"


def test_recovery_execution_queue_api_returns_payload_preview_only(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    persist_recovery_evaluations(
        app.state.session_factory,
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
        run_at=datetime(2026, 6, 12, 18, 0),
    )
    apply_recovery_review_decision(
        app.state.session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0),
    )
    client = TestClient(app)

    response = client.get("/api/recovery-execution-queue")

    assert response.status_code == 200
    assert response.json()["items"][0]["payload_preview"]["contract"] == "BTC-USDT"
    assert response.json()["items"][0]["deepcoin_order_draft"]["instrument_id"] == "BTC-USDT-SWAP"
    assert response.json()["items"][0]["deepcoin_order_draft"]["blocking_reason_codes"] == ["contract_size_unverified"]
    assert response.json()["items"][0]["deepcoin_order_draft"]["order_legs"][0]["quantity"] == 0.071429
    assert response.json()["items"][0]["contract_spec_status"] == {
        "code": "missing",
        "label": "缺少规格校验",
        "detail": "contract_size_unverified",
        "quantity_unit": "base_asset_estimate",
    }
    assert response.json()["items"][0]["execution_status"] == "pending_execution"


class _StaticContractSpecProvider:
    def get_contract_spec(self, instrument_id):
        return DeepcoinContractSpec(
            instrument_id=instrument_id,
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        )


def _persist_execution_candidate(session_factory):
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=55,
            sender_name="alice",
            posted_at=datetime(2026, 6, 12, 8, 0),
            text="BTC long 68000-68200 SL 67500 TP 69000 / 70000",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="long",
                event_type="entry_signal",
                entry_text="68000-68200",
                stop_loss_text="67500",
                take_profit_text="69000 / 70000",
                parse_source="text",
                confidence=0.9,
            )
        )
        session.commit()


def test_recovery_execution_queue_api_applies_configured_contract_specs(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_contract_spec_provider=_StaticContractSpecProvider(),
    )
    persist_recovery_evaluations(
        app.state.session_factory,
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
        run_at=datetime(2026, 6, 12, 18, 0),
    )
    apply_recovery_review_decision(
        app.state.session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0),
    )
    client = TestClient(app)

    response = client.get("/api/recovery-execution-queue")

    draft = response.json()["items"][0]["deepcoin_order_draft"]
    assert response.json()["items"][0]["contract_spec_status"] == {
        "code": "verified",
        "label": "已应用合约规格",
        "detail": "contracts",
        "quantity_unit": "contracts",
    }
    assert draft["blocking_reason_codes"] == []
    assert draft["order_legs"][0]["quantity"] == 71.0
    assert draft["order_legs"][0]["quantity_unit"] == "contracts"


def test_recovery_order_confirm_dry_run_api_returns_readiness(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_contract_spec_provider=_StaticContractSpecProvider(),
    )
    persist_recovery_evaluations(
        app.state.session_factory,
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
        run_at=datetime(2026, 6, 12, 18, 0),
    )
    apply_recovery_review_decision(
        app.state.session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0),
    )
    client = TestClient(app)

    response = client.post(
        "/api/recovery-order-confirm-dry-run",
        json={
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
        },
    )

    assert response.status_code == 200
    assert response.json()["ready_for_live_order"] is True
    assert response.json()["reason_codes"] == []
    assert response.json()["contract_spec_status"]["code"] == "verified"
    assert response.json()["ready_confirmation"]["status"] == "ready_confirmed"


def test_recovery_order_confirm_dry_run_api_requires_identifiers(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    client = TestClient(app)

    response = client.post("/api/recovery-order-confirm-dry-run", json={"chat_id": 100})

    assert response.status_code == 422
    assert "missing required fields" in response.json()["detail"]


def test_recovery_live_submit_gate_api_returns_would_submit_after_ready_confirmation(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_contract_spec_provider=_StaticContractSpecProvider(),
    )
    persist_recovery_evaluations(
        app.state.session_factory,
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
        run_at=datetime(2026, 6, 12, 18, 0),
    )
    apply_recovery_review_decision(
        app.state.session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0),
    )
    client = TestClient(app)
    client.post(
        "/api/recovery-order-confirm-dry-run",
        json={
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
        },
    )

    response = client.post(
        "/api/recovery-live-submit-gate",
        json={
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
        },
    )

    assert response.status_code == 200
    assert response.json()["would_submit"] is True
    assert response.json()["dry_run_only"] is True
    assert response.json()["checks"]["ready_confirmation"] is True
    assert response.json()["checks"]["order_draft_ready"] is True


def test_recovery_live_submit_api_places_orders_with_injected_client(tmp_path):
    class FakeDeepcoinClient:
        def __init__(self):
            self.payloads = []
            self.trigger_payloads = []
            self.protection_payloads = []

        def place_order(self, order_payload):
            self.payloads.append(order_payload)
            return {"code": "0", "data": {"ordId": f"order-{len(self.payloads)}"}}

        def trigger_order(self, order_payload):
            self.trigger_payloads.append(order_payload)
            return {"code": "0", "data": {"ordId": f"trigger-{len(self.trigger_payloads)}"}}

        def set_position_sltp(self, protection_payload):
            self.protection_payloads.append(protection_payload)
            return {"code": "0", "data": {"ordId": "sltp-1"}}

        def replace_order_sltp(self, protection_payload):
            self.protection_payloads.append(protection_payload)
            return {"code": "0", "data": {"orderSysID": protection_payload["orderSysID"]}}

        def cancel_order(self, cancel_payload):
            return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

        def list_positions(self, *, inst_id=None):
            return []

        def get_ticker_price(self, *, inst_id):
            return 68100.0

    fake_client = FakeDeepcoinClient()
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_contract_spec_provider=_StaticContractSpecProvider(),
        deepcoin_client_factory=lambda: fake_client,
    )
    save_trading_settings(app.state.session_factory, {"auto_trade_enabled": True})
    _persist_execution_candidate(app.state.session_factory)
    persist_recovery_evaluations(
        app.state.session_factory,
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
        run_at=datetime(2026, 6, 12, 18, 0),
    )
    apply_recovery_review_decision(
        app.state.session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0),
    )
    client = TestClient(app)
    client.post(
        "/api/recovery-order-confirm-dry-run",
        json={
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
        },
    )

    response = client.post(
        "/api/recovery-live-submit",
        json={
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
        },
    )

    assert response.status_code == 200
    assert response.json()["submitted"] is True
    assert response.json()["order_count"] == 2
    assert [payload["ordType"] for payload in fake_client.payloads] == ["limit", "limit"]
    assert fake_client.payloads[0]["tdMode"] == "cross"
    assert fake_client.protection_payloads[0]["orderSysID"] == "order-1"
    assert fake_client.protection_payloads[0]["tpTriggerPx"] == "69000.0"
    assert fake_client.protection_payloads[0]["slTriggerPx"] == "67500.0"


def test_trade_signal_process_next_api_consumes_pending_signal(tmp_path):
    class FakeDeepcoinClient:
        def __init__(self):
            self.payloads = []
            self.trigger_payloads = []
            self.protection_payloads = []

        def place_order(self, order_payload):
            self.payloads.append(order_payload)
            return {"code": "0", "data": {"ordId": f"order-{len(self.payloads)}"}}

        def trigger_order(self, order_payload):
            self.trigger_payloads.append(order_payload)
            return {"code": "0", "data": {"ordId": f"trigger-{len(self.trigger_payloads)}"}}

        def set_position_sltp(self, protection_payload):
            self.protection_payloads.append(protection_payload)
            return {"code": "0", "data": {"ordId": "sltp-1"}}

        def replace_order_sltp(self, protection_payload):
            self.protection_payloads.append(protection_payload)
            return {"code": "0", "data": {"orderSysID": protection_payload["orderSysID"]}}

        def cancel_order(self, cancel_payload):
            return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

        def list_positions(self, *, inst_id=None):
            return []

        def get_ticker_price(self, *, inst_id):
            return 68100.0

    fake_client = FakeDeepcoinClient()
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_contract_spec_provider=_StaticContractSpecProvider(),
        deepcoin_client_factory=lambda: fake_client,
    )
    save_trading_settings(app.state.session_factory, {"auto_trade_enabled": True})
    _persist_execution_candidate(app.state.session_factory)
    persist_recovery_evaluations(
        app.state.session_factory,
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
        run_at=datetime(2026, 6, 12, 18, 0),
    )
    apply_recovery_review_decision(
        app.state.session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        review_status="approved_for_order",
        reviewed_at=datetime(2026, 6, 12, 19, 0),
    )
    client = TestClient(app)
    client.post(
        "/api/recovery-order-confirm-dry-run",
        json={
            "chat_id": 100,
            "message_id": 55,
            "symbol": "BTC",
            "side": "long",
        },
    )
    signal = enqueue_recovery_trade_signal(
        app.state.session_factory,
        chat_id=100,
        message_id=55,
        symbol="BTC",
        side="long",
        contract_spec_provider=_StaticContractSpecProvider(),
    )

    list_response = client.get("/api/trade-signals")
    process_response = client.post("/api/trade-signals/process-next")

    assert list_response.status_code == 200
    assert list_response.json()["items"][0]["id"] == signal.id
    assert process_response.status_code == 200
    assert process_response.json()["processed"] is True
    assert process_response.json()["result"]["signal_id"] == signal.id
    assert fake_client.payloads[0]["tdMode"] == "cross"
    assert fake_client.protection_payloads[0]["tpTriggerPx"] == "69000.0"
