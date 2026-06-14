from datetime import datetime

from fastapi.testclient import TestClient

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
from telegram_kol_research.web_app import create_web_app
from telegram_kol_research.group_config import load_group_config
from telegram_kol_research.models import RawMessage


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


def test_message_recognition_api_updates_message_result(tmp_path):
    database_path = tmp_path / "research.db"
    app = create_web_app(database_path=database_path)
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


def test_ai_recognition_config_api_saves_prompt(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=config_path,
    )
    client = TestClient(app)

    response = client.post(
        "/api/ai-recognition-config",
        json={"recognition_prompt": "只识别明确策略。"},
    )

    assert response.status_code == 200
    assert response.json()["recognition_prompt"] == "只识别明确策略。"
    page = client.get("/")
    assert "只识别明确策略。" in page.text


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
