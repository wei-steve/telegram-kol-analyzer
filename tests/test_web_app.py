import asyncio
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.ai_recognition_config import AiRecognitionConfig
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
from telegram_kol_research.trade_signals import enqueue_trade_signal
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.web_app import create_web_app
from telegram_kol_research.web_app import _run_auto_trade_executor
from telegram_kol_research.web_app import _persisted_position_attribution
from telegram_kol_research.group_config import load_group_config
from telegram_kol_research.execution_bindings import reconcile_deepcoin_execution_bindings
from telegram_kol_research.models import RawMessage
from telegram_kol_research.models import ExecutionBinding
from telegram_kol_research.models import ExecutionEvent
from telegram_kol_research.models import ExecutionOrderLeg
from telegram_kol_research.models import PositionProtectionLedger
from telegram_kol_research.models import SignalCandidate
from telegram_kol_research.models import StrategyLifecycle
from telegram_kol_research.models import TradeSignal
from telegram_kol_research.models import StrategyManagementBatch, StrategyManagementLeg
from telegram_kol_research.message_recognition import MessageRecognitionResult
from telegram_kol_research.system_operator_bot import SystemOperatorBotConfig
from telegram_kol_research.telegram_bot_commands import (
    _log_system_operator_callback_processed,
)


def test_web_auto_executor_disabled_management_skips_client_factory(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=100,
            message_id=9001,
            text="BTC exit",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=raw.id,
                symbol="BTC",
                side="short",
                event_type="close_signal",
                management_action="full_exit",
                recognition_generation="web-disabled-generation",
                parse_source="mimo_authoritative",
                confidence=0.99,
            )
        )
        session.commit()
        raw_id = raw.id
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "management_execution_mode": "disabled",
        },
    )
    factory_calls = []
    app = SimpleNamespace(
        state=SimpleNamespace(
            session_factory=session_factory,
            group_config=GroupConfig(groups=[]),
            deepcoin_client_factory=lambda: factory_calls.append("called")
            or (_ for _ in ()).throw(AssertionError("factory must not run")),
            deepcoin_contract_spec_provider=None,
            now_provider=lambda: datetime(2026, 7, 15, 10, 0),
        )
    )

    result = _run_auto_trade_executor(app, raw_message_id=raw_id)

    assert result == {"status": "skipped", "reason": "management_execution_disabled"}
    assert factory_calls == []
    with session_factory() as session:
        event = session.query(ExecutionEvent).one()
        assert event.action == "management_auto_trade_skipped"


def test_web_process_next_legacy_management_skips_client_factory(tmp_path):
    factory_calls = []
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: factory_calls.append("called")
        or (_ for _ in ()).throw(AssertionError("factory must not run")),
    )
    save_trading_settings(app.state.session_factory, {"auto_trade_enabled": True})
    signal = enqueue_trade_signal(
        app.state.session_factory,
        venue="deepcoin",
        source_type="kol_management",
        kol_id="group:100",
        chat_id=100,
        message_id=9002,
        symbol="BTC",
        side="short",
        action="close_position",
        payload={"binding_id": 12},
    )

    response = TestClient(app).post("/api/trade-signals/process-next")

    assert response.status_code == 409
    assert response.json()["detail"] == "legacy_management_signal_requires_batch"
    assert factory_calls == []
    with app.state.session_factory() as session:
        row = session.get(TradeSignal, signal.id)
        assert row.status == "failed"
        assert row.attempts == 1


def test_management_batch_api_is_bounded_redacted_read_only_and_group_isolated(tmp_path):
    database_path = tmp_path / "research.db"
    sf = create_session_factory(database_path)
    now = datetime(2026, 7, 15, 10, 0)
    with sf() as session:
        for index, chat_id in enumerate((-1001, -1002), 1):
            raw = RawMessage(
                chat_id=chat_id, message_id=777, text="same text",
                raw_payload='{"DC-ACCESS-KEY":"secret","huge":"' + ("x" * 5000) + '"}',
            )
            session.add(raw); session.flush()
            batch = StrategyManagementBatch(
                idempotency_fingerprint=str(index) * 64, raw_message_id=raw.id,
                recognition_decision_id=index, recognition_generation="g",
                target_lifecycle_id=index, strategy_instance_id=f"deepcoin:{chat_id}:7:BTC:short",
                execution_binding_id=index, intent="partial_take_profit",
                effective_action="partial_close", effective_fraction=0.5,
                partial_round_before=0, status="blocked" if index == 1 else "recovery_required",
                execution_mode="shadow" if index == 1 else "live",
                reason_code="unsafe headers DC-ACCESS-SIGN should not leak",
                target_fingerprint=("a" if index == 1 else "b") * 64,
                target_snapshot_json=json.dumps({
                    "mode": "live", "targets": [{"pos_id": f"pos-{index}", "size": "0.02"}],
                    "headers": {"DC-ACCESS-KEY": "never-return"}, "raw_response": "never-return",
                }), planned_at=now, created_at=now, updated_at=now,
            )
            session.add(batch); session.flush()
            session.add(StrategyManagementLeg(
                management_batch_id=batch.id, execution_order_leg_id=index,
                pos_id=f"pos-{index}", leg_index=0, status=batch.status,
                preflight_size="0.02", planned_close_size="0.01",
                old_tpsl_json='[{"purpose":"stop_loss","order_id":"sl-1","secret":"no"}]',
                planned_tpsl_json='[{"purpose":"stop_loss","price":"65000","api_key":"no"}]',
                last_error=json.dumps({
                    "stage": "replace_protection", "reason_code": "restore_failed",
                    "type": "DeepcoinError", "message": "https://private.invalid/raw-body-content",
                    "token": "top-secret-token", "cookie": "session-cookie",
                    "headers": {"Authorization": "Bearer-never"},
                }), request_json='{"DC-ACCESS-KEY":"never-return"}',
                response_json='{"raw":"never-return"}',
            ))
        session.commit()

    client = TestClient(create_web_app(database_path=database_path))
    assert client.get("/api/management-batches").status_code == 422
    response = client.get("/api/management-batches", params={"chat_id": -1001, "limit": 500})
    assert response.status_code == 422
    response = client.get("/api/management-batches", params={"chat_id": -1001, "limit": 20})
    assert response.status_code == 200
    payload = response.json()
    assert payload["read_only"] is True
    assert len(payload["batches"]) == 1
    row = payload["batches"][0]
    assert row["source"] == {
        "chat_id": -1001, "chat_title": None, "message_id": 777,
        "raw_message_id": row["source"]["raw_message_id"],
    }
    assert row["strategy_instance_id"] == "deepcoin:-1001:7:BTC:short"
    assert row["mode_label"] == "未调用交易 API"
    assert row["legs"][0]["pos_id"] == "pos-1"
    encoded = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        "DC-ACCESS", "never-return", "raw_payload", "request_json", "response_json",
        "private.invalid", "top-secret-token", "session-cookie", "bearer-never",
        "raw-body-content", '"message"', '"headers"',
    ):
        assert forbidden not in encoded
    assert row["legs"][0]["error_summary"] == {
        "type": "DeepcoinError", "stage": "replace_protection",
        "reason_code": "restore_failed",
    }
    assert client.post("/api/management-batches/1/retry").status_code == 404
    group_b = client.get(
        "/api/management-batches", params={"chat_id": -1002, "limit": 20}
    ).json()["batches"]
    assert len(group_b) == 1
    assert group_b[0]["source"]["chat_id"] == -1002
    assert group_b[0]["strategy_instance_id"] == "deepcoin:-1002:7:BTC:short"
    assert group_b[0]["legs"][0]["pos_id"] == "pos-2"
    assert "pos-1" not in json.dumps(group_b)


def test_management_batch_api_marks_recovery_as_no_auto_retry(tmp_path):
    database_path = tmp_path / "research.db"
    sf = create_session_factory(database_path)
    now = datetime(2026, 7, 15, 10, 0)
    with sf() as session:
        raw = RawMessage(chat_id=-2002, message_id=8, text="x")
        session.add(raw); session.flush()
        session.add(StrategyManagementBatch(
            idempotency_fingerprint="f" * 64, raw_message_id=raw.id,
            recognition_decision_id=1, recognition_generation="g", target_lifecycle_id=2,
            strategy_instance_id="deepcoin:-2002:8:ETH:long", execution_binding_id=3,
            intent="adjust_stop_loss", effective_action="replace_stop_loss",
            execution_mode="shadow",
            partial_round_before=1, status="recovery_required", reason_code="restore_failed",
            target_fingerprint="e" * 64, target_snapshot_json='{"mode":"live"}',
            planned_at=now, created_at=now, updated_at=now,
        ))
        session.commit()
    row = TestClient(create_web_app(database_path=database_path)).get(
        "/api/management-batches", params={"chat_id": -2002}
    ).json()["batches"][0]
    assert row["safety_label"] == "禁止自动重试"
    assert row["mode"] == "shadow"
    assert row["mode_label"] == "未调用交易 API"


def test_incomplete_equivalent_assignment_does_not_render_reviewed_provenance():
    leg = SimpleNamespace(
        attribution_evidence_json=json.dumps(
            {
                "evidence_type": "equivalent_permutation_assignment",
                "mapping_basis": "stable_sorted_canonicalization",
            }
        ),
        attribution_status="verified",
        status="active",
        strategy_instance_id="deepcoin:9527:56:ETH:short",
        leg_index=1,
        pos_id="pos-miya-1",
        last_verified_at=None,
    )
    binding = SimpleNamespace(
        chat_id=9527,
        strategy_instance_id="deepcoin:9527:56:ETH:short",
        symbol="ETH",
        side="short",
    )

    attribution = _persisted_position_attribution(
        leg=leg,
        binding=binding,
        group_label_by_chat_id={9527: "米娅 vip 会员群 11分组"},
    )

    assert attribution is not None
    assert attribution["group_name"] == "米娅 vip 会员群 11分组"
    assert attribution["state"] == "conflict"
    assert attribution["label"] == "归属待确认"
    assert attribution["provenance_label"] is None
    assert "等价腿确定性归属" not in attribution["reasons"]


def test_semantic_review_worker_lifespan_starts_once_without_telegram_and_stops_first(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "groups.yaml"
    config_path.write_text(
        "groups:\n"
        "  - chat_title: Demo Group\n"
        "    chat_id: 77\n"
        "    enabled: true\n"
        "    ai_strategy_enabled: false\n",
        encoding="utf-8",
    )
    ai_config_path = tmp_path / "ai_recognition.yaml"
    started = threading.Event()
    calls = []
    shutdown_order = []

    async def fake_semantic_review_runner(**kwargs):
        calls.append(kwargs)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            shutdown_order.append("semantic_review_stopped")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        live_target_titles=set(),
        telegram_client=None,
        group_config=load_group_config(config_path),
        group_config_path=config_path,
        ai_recognition_config_path=ai_config_path,
        semantic_review_runner=fake_semantic_review_runner,
    )
    app.state.system_operator_bot_config = SystemOperatorBotConfig(
        bot_token="system-token",
        chat_id="system-chat",
    )
    app.state.notification_bot_config = SystemOperatorBotConfig(
        bot_token="notification-token",
        chat_id="system-chat",
    )
    broker_type = type(app.state.live_update_broker)
    original_close = broker_type.close

    def record_resource_close(broker):
        shutdown_order.append("resources_closed")
        original_close(broker)

    monkeypatch.setattr(broker_type, "close", record_resource_close)

    with TestClient(app) as client:
        assert started.wait(timeout=1)
        assert len(calls) == 1
        assert calls[0]["session_factory"] is app.state.session_factory
        assert calls[0]["config_path"] == ai_config_path
        assert callable(calls[0]["notifier"])
        assert app.state.semantic_review_task is not None

        response = client.post(
            "/api/groups/77/automation",
            json={"ai_strategy_enabled": True},
        )

        assert response.status_code == 200
        assert len(calls) == 1

    assert app.state.semantic_review_task is None
    assert shutdown_order == ["semantic_review_stopped", "resources_closed"]


def test_management_worker_lifespan_starts_once_and_is_cancelled(tmp_path):
    started = threading.Event()
    stopped = threading.Event()
    calls = []

    async def fake_management_worker(**kwargs):
        calls.append(kwargs)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    app = create_web_app(
        database_path=tmp_path / "research.db",
        strategy_management_worker_runner=fake_management_worker,
        strategy_management_worker_startup_delay_seconds=0,
        strategy_management_worker_interval_seconds=7,
        strategy_management_worker_max_batches=3,
    )

    with TestClient(app):
        assert started.wait(timeout=1)
        assert len(calls) == 1
        assert calls[0]["session_factory"] is app.state.session_factory
        assert calls[0]["deepcoin_client_factory"] is app.state.deepcoin_client_factory
        assert calls[0]["interval_seconds"] == 7
        assert calls[0]["max_batches"] == 3
        assert app.state.strategy_management_worker_task is not None

    assert stopped.wait(timeout=1)
    assert app.state.strategy_management_worker_task is None


def test_lifespan_disconnects_shared_telegram_client_before_stopping_listener(tmp_path):
    class ShieldedDisconnectClient:
        def __init__(self):
            self.disconnected = asyncio.Event()
            self.cleanup_complete = asyncio.Event()
            self.disconnect_calls = 0

        async def disconnect(self):
            self.disconnect_calls += 1
            self.disconnected.set()
            self.cleanup_complete.set()

    async def shielded_listener(*, client, **kwargs):
        try:
            await client.disconnected.wait()
        finally:
            await asyncio.shield(client.cleanup_complete.wait())

    async def exercise_lifespan():
        client = ShieldedDisconnectClient()
        app = create_web_app(
            database_path=tmp_path / "research.db",
            live_target_titles={"Demo Group"},
            telegram_client=client,
            live_listener_runner=shielded_listener,
        )
        app.state.strategy_alert_config = None
        app.state.system_operator_bot_config = None

        async def enter_and_exit():
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)

        task = asyncio.create_task(enter_and_exit())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
            timed_out = False
        except TimeoutError:
            timed_out = True
            client.cleanup_complete.set()
            await task
        return timed_out, client.disconnect_calls

    timed_out, disconnect_calls = asyncio.run(exercise_lifespan())

    assert timed_out is False
    assert disconnect_calls == 1


def test_lifespan_bounds_listener_shutdown_when_telegram_disconnect_hangs(
    tmp_path, monkeypatch
):
    import telegram_kol_research.web_app as web_module

    monkeypatch.setattr(
        web_module,
        "_TELEGRAM_SHUTDOWN_TIMEOUT_SECONDS",
        0.05,
        raising=False,
    )

    class HangingDisconnectClient:
        def __init__(self):
            self.cleanup_complete = asyncio.Event()

        async def disconnect(self):
            await asyncio.Event().wait()

    async def shielded_listener(*, client, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.shield(client.cleanup_complete.wait())

    async def exercise_lifespan():
        client = HangingDisconnectClient()
        app = create_web_app(
            database_path=tmp_path / "research.db",
            live_target_titles={"Demo Group"},
            telegram_client=client,
            live_listener_runner=shielded_listener,
        )
        app.state.strategy_alert_config = None
        app.state.system_operator_bot_config = None

        async def enter_and_exit():
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)

        task = asyncio.create_task(enter_and_exit())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)
            timed_out = False
        except TimeoutError:
            timed_out = True
            client.cleanup_complete.set()
            await task
        return timed_out

    assert asyncio.run(exercise_lifespan()) is False


def test_semantic_review_worker_uses_system_operator_notifier(tmp_path, monkeypatch):
    started = threading.Event()
    sent = []
    bot_config = SystemOperatorBotConfig(
        bot_token="system-token",
        chat_id="system-chat",
    )
    app = None

    async def fake_sender(**kwargs):
        sent.append(kwargs)

    async def fake_semantic_review_runner(**kwargs):
        payload = {
            "chat_id": 88,
            "message_id": 12,
            "sender_name": "Demo",
            "posted_at": None,
            "text": "BTC 全部出局",
            "agreement_status": "disagreed",
            "conflict_types": ["urgent_exit_missed"],
            "deepseek": {
                "status": "exit_full",
                "kind": "semantic_review",
                "reason": "DeepSeek 独立复核认为需要退出",
                "evidence": ["全部出局"],
                "conflict_types": ["urgent_exit_missed"],
            },
            "mimo": {
                "status": "exit_full",
                "kind": "authoritative",
                "reason": "MiMo 识别为空仓退出",
            },
            "automation": {
                "status": "submitted",
                "reason": "close_position",
            },
        }
        await kwargs["notifier"](
            raw_message_id=1,
            payload=payload,
        )
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "telegram_kol_research.web_app.send_semantic_disagreement_notification",
        fake_sender,
    )
    app = create_web_app(
        database_path=tmp_path / "research.db",
        semantic_review_runner=fake_semantic_review_runner,
    )
    app.state.notification_bot_config = bot_config

    with TestClient(app):
        assert started.wait(timeout=1)

    assert len(sent) == 1
    assert sent[0]["config"] is bot_config
    assert sent[0]["payload"]["chat_id"] == 88
    assert sent[0]["payload"]["message_id"] == 12
    assert sent[0]["payload"]["sender_name"] == "Demo"
    assert sent[0]["payload"]["text"] == "BTC 全部出局"
    assert sent[0]["payload"]["deepseek"] == {
        "status": "exit_full",
        "kind": "semantic_review",
        "reason": "DeepSeek 独立复核认为需要退出",
        "evidence": ["全部出局"],
        "conflict_types": ["urgent_exit_missed"],
    }
    assert sent[0]["payload"]["conflict_types"] == ["urgent_exit_missed"]
    assert sent[0]["payload"]["mimo"] == {
        "status": "exit_full",
        "kind": "authoritative",
        "reason": "MiMo 识别为空仓退出",
    }
    assert sent[0]["payload"]["automation"] == {
        "status": "submitted",
        "reason": "close_position",
    }


def test_semantic_review_worker_clean_exit_is_logged_and_restarted(tmp_path):
    restarted = threading.Event()
    calls = 0
    active = 0
    max_active = 0

    async def returning_semantic_review_runner(**kwargs):
        nonlocal calls, active, max_active
        calls += 1
        active += 1
        max_active = max(max_active, active)
        try:
            if calls == 1:
                return
            restarted.set()
            await asyncio.Event().wait()
        finally:
            active -= 1

    app = create_web_app(
        database_path=tmp_path / "research.db",
        semantic_review_runner=returning_semantic_review_runner,
        semantic_review_restart_delay_seconds=0,
    )

    log_path = app.state.log_directory / "telegram-kol.log"
    with TestClient(app) as client:
        assert restarted.wait(timeout=1)
        for _ in range(20):
            log_text = log_path.read_text(encoding="utf-8")
            if "Semantic review runner exited unexpectedly; restarting" in log_text:
                break
            client.get("/api/freshness")
            time.sleep(0.01)

        assert calls == 2
        assert max_active == 1

    assert "Semantic review runner exited unexpectedly; restarting" in log_text


def test_semantic_review_worker_failure_is_logged_and_restarted(tmp_path):
    restarted = threading.Event()
    calls = 0

    async def failing_semantic_review_runner(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("semantic worker crashed")
        restarted.set()
        await asyncio.Event().wait()

    app = create_web_app(
        database_path=tmp_path / "research.db",
        semantic_review_runner=failing_semantic_review_runner,
        semantic_review_restart_delay_seconds=0,
    )

    log_path = app.state.log_directory / "telegram-kol.log"
    with TestClient(app) as client:
        assert restarted.wait(timeout=1)
        for _ in range(20):
            log_text = log_path.read_text(encoding="utf-8")
            if "Semantic review runner exited with error; restarting" in log_text:
                break
            client.get("/api/freshness")
            time.sleep(0.01)

    assert calls == 2
    assert "Semantic review runner exited with error; restarting" in log_text
    assert "semantic worker crashed" in log_text


def test_semantic_review_worker_is_cleaned_up_when_lifespan_startup_fails(
    tmp_path, monkeypatch
):
    async def fake_semantic_review_runner(**kwargs):
        await asyncio.Event().wait()

    def fail_live_listener_startup(**kwargs):
        raise RuntimeError("live listener startup failed")

    monkeypatch.setattr(
        "telegram_kol_research.web_app.launch_live_listener_task",
        fail_live_listener_startup,
    )
    app = create_web_app(
        database_path=tmp_path / "research.db",
        live_target_titles={"Demo Group"},
        telegram_client=object(),
        semantic_review_runner=fake_semantic_review_runner,
    )

    with pytest.raises(RuntimeError, match="live listener startup failed"):
        with TestClient(app):
            pass

    assert app.state.semantic_review_task is None


def test_root_page_renders_successfully(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200


def test_logs_page_and_api_return_paginated_application_log_entries(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    log_path = app.state.log_directory / "telegram-kol.log"
    log_path.write_text(
        "2026-07-10 10:00:00,000 ERROR telegram_kol_research.web failed <script>alert(1)</script>\n",
        encoding="utf-8",
    )
    client = TestClient(app)

    page = client.get("/logs")
    response = client.get("/api/logs?offset=0&limit=100&level=ERROR")

    assert page.status_code == 200
    assert "系统日志" in page.text
    assert response.status_code == 200
    assert response.json()["items"][0]["level"] == "ERROR"
    assert "<script>alert(1)</script>" in response.json()["items"][0]["message"]


def test_logs_api_rejects_invalid_pagination(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    assert client.get("/api/logs?offset=-1").status_code == 422
    assert client.get("/api/logs?limit=201").status_code == 422
    assert client.get("/api/logs?level=DEBUG").status_code == 422


def test_logs_api_does_not_expose_system_operator_callback_data(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    callback_data = "expiry_expire_cancel:confidential-operator-payload"

    _log_system_operator_callback_processed(update_id=42, callback_data=callback_data)

    response = TestClient(app).get("/api/logs")

    messages = [item["message"] for item in response.json()["items"]]
    assert callback_data not in "\n".join(messages)
    assert "System operator bot processing callback update_id=42" in messages


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
            "nearby_entry_market_deviation_pct": 1.25,
            "min_ai_confidence": 0.8,
            "allowed_symbols": "BTC,ETH,SOL",
            "symbol_max_loss_usdt": {"BTC": 20, "ETH": 15, "SOL": 10},
            "entry_range_order_style": "conservative",
            "take_profit_allocations": "50,30,20",
            "move_stop_to_breakeven_after_tp1": True,
            "allow_vision_auto_trade": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["default_max_loss_usdt"] == 150.0
    assert response.json()["nearby_entry_market_deviation_pct"] == 1.25
    assert response.json()["allowed_symbols"] == ["BTC", "ETH", "SOL"]
    assert response.json()["symbol_max_loss_usdt"] == {"BTC": 20.0, "ETH": 15.0, "SOL": 10.0}

    reloaded = client.get("/api/trading-settings")
    assert reloaded.status_code == 200
    assert reloaded.json()["auto_trade_enabled"] is True
    assert reloaded.json()["nearby_entry_market_deviation_pct"] == 1.25
    assert reloaded.json()["take_profit_allocations"] == [50.0, 30.0, 20.0]


def test_management_execution_mode_api_persists_shadow_with_auto_trade_disabled(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.post(
        "/api/trading-settings",
        json={
            "management_execution_mode": "shadow",
            "auto_trade_enabled": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["management_execution_mode"] == "shadow"
    assert response.json()["auto_trade_enabled"] is False
    assert client.get("/api/trading-settings").json()["management_execution_mode"] == "shadow"


def test_management_execution_mode_api_rejects_invalid_value(tmp_path):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.post(
        "/api/trading-settings",
        json={"management_execution_mode": "unsafe"},
    )

    assert response.status_code == 422
    assert "management_execution_mode" in response.json()["detail"]


@pytest.mark.parametrize("value", ["false", "0", 0, 1])
def test_trading_settings_api_rejects_non_boolean_auto_trade_enabled(tmp_path, value):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.post(
        "/api/trading-settings",
        json={
            "management_execution_mode": "live",
            "auto_trade_enabled": value,
        },
    )

    assert response.status_code == 422
    assert "auto_trade_enabled" in response.json()["detail"]


@pytest.mark.parametrize("value", [[], {}, 1, None])
def test_management_execution_mode_api_rejects_non_string_values(tmp_path, value):
    client = TestClient(create_web_app(database_path=tmp_path / "research.db"))

    response = client.post(
        "/api/trading-settings",
        json={"management_execution_mode": value},
    )

    assert response.status_code == 422
    assert "management_execution_mode" in response.json()["detail"]


def test_trading_settings_symbols_api_lists_deepcoin_symbols_with_selection(tmp_path):
    class FakeDeepcoinClient:
        def list_swap_symbols(self):
            return [
                {"symbol": "ETH", "instrument_id": "ETH-USDT-SWAP"},
                {"symbol": "BTC", "instrument_id": "BTC-USDT-SWAP"},
                {"symbol": "SOL", "instrument_id": "SOL-USDT-SWAP"},
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
    )
    save_trading_settings(
        app.state.session_factory,
        {
            "allowed_symbols": ["BTC", "SOL"],
            "symbol_max_loss_usdt": {"BTC": 20, "SOL": 10},
        },
    )
    client = TestClient(app)

    response = client.get("/api/trading-settings/symbols")

    assert response.status_code == 200
    assert response.json()["symbols"] == [
        {
            "symbol": "BTC",
            "instrument_id": "BTC-USDT-SWAP",
            "selected": True,
            "max_loss_usdt": 20.0,
        },
        {
            "symbol": "ETH",
            "instrument_id": "ETH-USDT-SWAP",
            "selected": False,
            "max_loss_usdt": None,
        },
        {
            "symbol": "SOL",
            "instrument_id": "SOL-USDT-SWAP",
            "selected": True,
            "max_loss_usdt": 10.0,
        },
    ]


def test_trading_settings_symbols_api_falls_back_to_saved_symbols(tmp_path):
    class BrokenDeepcoinClient:
        def list_swap_symbols(self):
            raise RuntimeError("offline")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: BrokenDeepcoinClient(),
    )
    save_trading_settings(
        app.state.session_factory,
        {
            "allowed_symbols": ["BTC", "ETH"],
            "symbol_max_loss_usdt": {"ETH": 15},
        },
    )
    client = TestClient(app)

    response = client.get("/api/trading-settings/symbols")

    assert response.status_code == 200
    assert response.json()["symbols"] == [
        {
            "symbol": "BTC",
            "instrument_id": "BTC-USDT-SWAP",
            "selected": True,
            "max_loss_usdt": None,
        },
        {
            "symbol": "ETH",
            "instrument_id": "ETH-USDT-SWAP",
            "selected": True,
            "max_loss_usdt": 15.0,
        },
    ]


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


def test_execution_dashboard_does_not_treat_stale_binding_as_persisted_ownership(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-stale-system",
                    "posSide": "short",
                    "pos": "10",
                    "avgPx": "61351",
                }
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
                symbol="BTC",
                side="short",
                status="stale",
                pos_id="pos-stale-system",
                order_id="trigger-entry",
                last_exchange_status="expired_pending_entry_not_attributed",
            )
        )
        session.add(
            StrategyLifecycle(
                chat_id=88,
                message_id=10,
                symbol="BTC",
                side="short",
                lifecycle_status="expired",
                exit_reason="expired",
                signal_at=datetime(2026, 6, 30, 8, 0),
                exited_at=datetime(2026, 6, 30, 14, 0),
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/execution")

    assert response.status_code == 200
    assert "pos-stale-system" in response.text
    assert "unbound_live_position" in response.text
    assert "system_attribution_conflict" not in response.text


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


def test_execution_dashboard_uses_position_tpsl_fields_for_live_protection(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-with-position-fields",
                    "posSide": "short",
                    "pos": "11",
                    "avgPx": "61563",
                    "cTime": "1783004197000",
                    "slTriggerPx": "62440",
                    "tpTriggerPx": "59588",
                },
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            assert inst_id == "BTC-USDT-SWAP"
            return []

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
    )

    client = TestClient(app)
    response = client.get("/execution")

    assert response.status_code == 200
    assert "pos-with-position-fields" in response.text
    assert "62440" in response.text
    assert "59588" in response.text


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


def test_execution_dashboard_matches_tpsl_one_second_after_position(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-smart-market",
                    "posSide": "long",
                    "pos": "1.5",
                    "avgPx": "1840",
                    "cTime": "1782788876000",
                }
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "instId": inst_id,
                    "posSide": "long",
                    "sz": "0",
                    "cTime": "1782788877000",
                    "triggerOrderType": "TPSL",
                    "ordId": "sl-smart-market",
                    "slTriggerPrice": "1820",
                }
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=FakeDeepcoinClient,
    )

    response = TestClient(app).get("/execution")

    assert response.status_code == 200
    assert "pos-smart-market" in response.text
    assert "止损: 1820" in response.text
    assert "无止损" not in response.text


def test_execution_dashboard_uses_exact_ledger_evidence_for_late_managed_stop(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-managed-stop",
                    "posSide": "short",
                    "pos": "7",
                    "avgPx": "65287.5",
                    "cTime": "10000",
                    "tpTriggerPx": "63100",
                    "slTriggerPx": "",
                }
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "instId": inst_id,
                    "posSide": "short",
                    "sz": "0",
                    "cTime": "24010000",
                    "triggerOrderType": "TPSL",
                    "ordId": "late-managed-stop",
                    "slTriggerPrice": "67200",
                }
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=FakeDeepcoinClient,
    )
    with app.state.session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="short",
            status="active",
            pos_id="pos-managed-stop",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            pos_id="pos-managed-stop",
            venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json=json.dumps({"policy_version": 2}),
            status="active",
        )
        session.add(leg)
        session.flush()
        session.add(
            PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                pos_id="pos-managed-stop",
                instrument_id="BTC-USDT-SWAP",
                side="short",
                order_id="late-managed-stop",
                purpose="stop_loss",
                trigger_price="67200",
                status="verified",
                evidence_source="management_tpsl_replacement",
            )
        )
        session.commit()

    response = TestClient(app).get("/execution")

    assert response.status_code == 200
    assert "止损: 67200" in response.text
    assert "无止损" not in response.text


def test_execution_dashboard_does_not_use_ledger_for_closed_entry_leg(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [{
                "instId": "BTC-USDT-SWAP", "posId": "pos-closed-leg",
                "posSide": "short", "pos": "7", "avgPx": "65287.5",
                "cTime": "10000", "tpTriggerPx": "63100", "slTriggerPx": "",
            }]

        def list_trigger_orders_pending(self, *, inst_id):
            return [{
                "instId": inst_id, "posSide": "short", "sz": "0",
                "cTime": "24010000", "triggerOrderType": "TPSL",
                "ordId": "closed-leg-stop", "slTriggerPrice": "67200",
            }]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=FakeDeepcoinClient,
    )
    with app.state.session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:88", chat_id=88, message_id=10, symbol="BTC",
            side="short", status="active", pos_id="pos-closed-leg",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id, leg_index=1, purpose="entry",
            order_kind="market", pos_id="pos-closed-leg", venue="deepcoin",
            attribution_status="verified",
            attribution_evidence_json=json.dumps({"policy_version": 2}),
            status="closed",
        )
        session.add(leg)
        session.flush()
        session.add(
            PositionProtectionLedger(
                venue="deepcoin", execution_binding_id=binding.id,
                execution_order_leg_id=leg.id, pos_id="pos-closed-leg",
                instrument_id="BTC-USDT-SWAP", side="short",
                order_id="closed-leg-stop", purpose="stop_loss",
                trigger_price="67200", status="verified",
                evidence_source="management_tpsl_replacement",
            )
        )
        session.commit()

    response = TestClient(app).get("/execution")

    assert response.status_code == 200
    assert "止损: 67200" not in response.text
    assert "无止损" in response.text


def test_execution_dashboard_renders_ambiguous_stop_truthfully(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": pos_id,
                    "posSide": "long",
                    "pos": "1.5",
                    "avgPx": "1840",
                    "cTime": "1782788876000",
                }
                for pos_id in ("pos-a", "pos-b")
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "instId": inst_id,
                    "posSide": "long",
                    "sz": "0",
                    "cTime": "1782788877000",
                    "triggerOrderType": "TPSL",
                    "ordId": "sl-ambiguous",
                    "slTriggerPrice": "1820",
                }
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=FakeDeepcoinClient,
    )

    response = TestClient(app).get("/execution")

    assert response.status_code == 200
    assert response.text.count("止损存在，归属待确认") == 2
    assert "无止损" not in response.text


def test_execution_dashboard_shows_inline_stop_while_pending_tpsl_is_ambiguous(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-btc",
                    "posSide": "short",
                    "pos": "5",
                    "avgPx": "64800",
                    "slTriggerPx": "66500",
                    "tpTriggerPx": "63300",
                    "cTime": "10000",
                }
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            return [
                {
                    "instId": inst_id,
                    "posSide": "short",
                    "sz": "5",
                    "cTime": "10000",
                    "triggerOrderType": "TPSL",
                    "ordId": "position-tpsl",
                    "slTriggerPrice": "66500",
                },
                {
                    "instId": inst_id,
                    "posSide": "short",
                    "sz": "7",
                    "cTime": "20000",
                    "triggerOrderType": "TPSL",
                    "ordId": "other-tpsl",
                    "slTriggerPrice": "66500",
                },
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=FakeDeepcoinClient,
    )

    response = TestClient(app).get("/execution")

    assert response.status_code == 200
    assert "止损: 66500" in response.text
    assert "止损存在，归属待确认" not in response.text


def test_execution_dashboard_renders_tpsl_evidence_error_truthfully(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-evidence-error",
                    "posSide": "long",
                    "pos": "1.5",
                    "avgPx": "1840",
                    "cTime": "1782788876000",
                }
            ]

        def list_trigger_orders_pending(self, *, inst_id):
            raise RuntimeError("tpsl unavailable")

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=FakeDeepcoinClient,
    )

    response = TestClient(app).get("/execution")

    assert response.status_code == 200
    assert "止损证据暂不可用" in response.text
    assert "无止损" not in response.text


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


def test_manual_close_rejects_entered_without_deepcoin_binding_and_keeps_all_state(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    with app.state.session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=9,
            symbol="ETH",
            side="short",
            venue="other-exchange",
            status="active",
            last_exchange_status="position_active",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 15, 8, 0),
            entered_at=datetime(2026, 7, 15, 8, 1),
        )
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            leg_index=1,
            purpose="entry",
            venue="other-exchange",
            status="active",
            attribution_status="unassigned",
        )
        session.add_all([lifecycle, leg])
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id
        leg_id = leg.id

    response = TestClient(app).post(
        f"/api/strategy-lifecycles/{lifecycle_id}/manual-close",
        json={"note": "must not apply"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "manual close requires unique execution binding and either entered lifecycle "
        "or legacy pending_entry with entered_at"
    )
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.get(ExecutionOrderLeg, leg_id)
        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.exit_reason is None
        assert lifecycle.exited_at is None
        assert (binding.status, binding.last_exchange_status) == (
            "active",
            "position_active",
        )
        assert (leg.status, leg.terminal_reason) == ("active", None)


def test_manual_close_accepts_legacy_demoted_pending_and_reconcile_cannot_revive_it(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    with app.state.session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=10,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 15, 9, 0),
            entered_at=datetime(2026, 7, 15, 9, 1),
        )
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=10,
            symbol="ETH",
            side="short",
            status="open",
            order_id="old-trigger,new-position",
            client_order_id="old-client,new-client",
            last_exchange_status="entry_order_pending",
        )
        session.add_all([lifecycle, binding])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id="old-trigger",
                    client_order_id="old-client",
                    venue="deepcoin",
                    status="open",
                    attribution_status="unassigned",
                ),
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    leg_index=2,
                    purpose="entry",
                    order_kind="trigger_limit",
                    order_id="new-position",
                    client_order_id="new-client",
                    venue="deepcoin",
                    status="open",
                    attribution_status="unassigned",
                ),
            ]
        )
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id

    client = TestClient(app)
    first = client.post(
        f"/api/strategy-lifecycles/{lifecycle_id}/manual-close",
        json={"note": "exchange position closed by operator"},
    )

    assert first.status_code == 200
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)
        legs = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id, purpose="entry")
            .order_by(ExecutionOrderLeg.leg_index)
            .all()
        )
        assert (lifecycle.lifecycle_status, lifecycle.exit_reason) == ("exited", "manual")
        assert (binding.status, binding.last_exchange_status) == (
            "closed",
            "manual_closed_by_user: exchange position closed by operator",
        )
        assert [leg.status for leg in legs] == ["manually_closed", "manually_closed"]
        assert [leg.terminal_reason for leg in legs] == [
            "manual_closed_by_user",
            "manual_closed_by_user",
        ]
        assert [leg.pos_id for leg in legs] == [None, None]
        assert [leg.attribution_status for leg in legs] == ["unassigned", "unassigned"]

    class FakeSnapshotClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "new-position",
                    "posSide": "short",
                    "pos": "2",
                    "avgPx": "3000",
                    "cTime": "1784077320000",
                }
            ]

        def list_open_orders(self):
            return []

        def list_trigger_orders_pending(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
            return []

        def list_trade_fills(self, *, inst_id=None):
            return []

        def list_trigger_order_history(self, *, inst_id=None):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "old-trigger",
                    "clOrdId": "old-client",
                    "state": "filled",
                    "posSide": "short",
                    "side": "sell",
                    "sz": "2",
                    "px": "3000",
                    "triggerTime": "1784070000000",
                    "errorCode": "0",
                },
                {
                    "instId": "ETH-USDT-SWAP",
                    "ordId": "new-position",
                    "clOrdId": "new-client",
                    "state": "filled",
                    "posSide": "short",
                    "side": "sell",
                    "sz": "2",
                    "px": "3000",
                    "triggerTime": "1784077320000",
                    "errorCode": "0",
                },
            ]

    reconcile_deepcoin_execution_bindings(
        app.state.session_factory,
        client=FakeSnapshotClient(),
        recovered_at=datetime(2026, 7, 15, 10, 30),
    )
    second = client.post(f"/api/strategy-lifecycles/{lifecycle_id}/manual-close", json={})

    assert second.status_code == 409
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)
        legs = (
            session.query(ExecutionOrderLeg)
            .filter_by(execution_binding_id=binding_id, purpose="entry")
            .order_by(ExecutionOrderLeg.leg_index)
            .all()
        )
        assert (lifecycle.lifecycle_status, lifecycle.exit_reason) == ("exited", "manual")
        assert (binding.status, binding.last_exchange_status) == (
            "closed",
            "manual_closed_by_user: exchange position closed by operator",
        )
        assert [leg.status for leg in legs] == ["manually_closed", "manually_closed"]
        assert [leg.terminal_reason for leg in legs] == [
            "manual_closed_by_user",
            "manual_closed_by_user",
        ]
        assert [leg.pos_id for leg in legs] == [None, None]
        assert [leg.attribution_status for leg in legs] == ["unassigned", "unassigned"]


def test_manual_close_rejects_never_entered_pending_without_writes(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    with app.state.session_factory() as session:
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=11,
            symbol="ETH",
            side="short",
            status="open",
            last_exchange_status="entry_order_pending",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=11,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 15, 11, 0),
            entered_at=None,
            execution_binding_id=binding.id,
        )
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            leg_index=1,
            purpose="entry",
            order_kind="trigger_limit",
            order_id="pending-order",
            venue="deepcoin",
            status="pending",
            attribution_status="unassigned",
        )
        session.add_all([lifecycle, leg])
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id
        leg_id = leg.id

    response = TestClient(app).post(
        f"/api/strategy-lifecycles/{lifecycle_id}/manual-close",
        json={"note": "must not apply"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "manual close requires unique execution binding and either entered lifecycle "
        "or legacy pending_entry with entered_at"
    )
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.get(ExecutionOrderLeg, leg_id)
        assert lifecycle.lifecycle_status == "pending_entry"
        assert lifecycle.exit_reason is None
        assert lifecycle.exited_at is None
        assert binding.status == "open"
        assert binding.last_exchange_status == "entry_order_pending"
        assert leg.status == "pending"
        assert leg.terminal_reason is None


def test_manual_close_rejects_wrong_explicit_binding_even_with_matching_fallback(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    with app.state.session_factory() as session:
        wrong_binding = ExecutionBinding(
            kol_id="group:999",
            chat_id=999,
            message_id=99,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="active",
            last_exchange_status="position_active",
        )
        other_venue_binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=12,
            symbol="ETH",
            side="short",
            venue="other-exchange",
            status="active",
            last_exchange_status="position_active",
        )
        matching_binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=12,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            status="active",
            last_exchange_status="position_active",
        )
        session.add_all([wrong_binding, other_venue_binding, matching_binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=12,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 15, 12, 0),
            entered_at=datetime(2026, 7, 15, 12, 1),
            execution_binding_id=wrong_binding.id,
        )
        legs = [
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                leg_index=1,
                purpose="entry",
                venue=binding.venue,
                status="active",
                attribution_status="unassigned",
            )
            for binding in (wrong_binding, other_venue_binding, matching_binding)
        ]
        session.add_all([lifecycle, *legs])
        session.commit()
        lifecycle_id = lifecycle.id
        wrong_binding_id = wrong_binding.id
        other_venue_binding_id = other_venue_binding.id
        matching_binding_id = matching_binding.id
        leg_ids = [leg.id for leg in legs]

    response = TestClient(app).post(
        f"/api/strategy-lifecycles/{lifecycle_id}/manual-close",
        json={"note": "must not apply"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "manual close requires unique execution binding and either entered lifecycle "
        "or legacy pending_entry with entered_at"
    )
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        wrong_binding = session.get(ExecutionBinding, wrong_binding_id)
        other_venue_binding = session.get(ExecutionBinding, other_venue_binding_id)
        matching_binding = session.get(ExecutionBinding, matching_binding_id)
        legs = [session.get(ExecutionOrderLeg, leg_id) for leg_id in leg_ids]
        assert lifecycle.lifecycle_status == "pending_entry"
        assert lifecycle.exit_reason is None
        assert lifecycle.exited_at is None
        assert (wrong_binding.status, wrong_binding.last_exchange_status) == (
            "active",
            "position_active",
        )
        assert (other_venue_binding.status, other_venue_binding.last_exchange_status) == (
            "active",
            "position_active",
        )
        assert (matching_binding.status, matching_binding.last_exchange_status) == (
            "active",
            "position_active",
        )
        assert [leg.status for leg in legs] == ["active", "active", "active"]
        assert [leg.terminal_reason for leg in legs] == [None, None, None]


@pytest.mark.parametrize(
    ("use_explicit_binding", "lifecycle_status"),
    [
        (True, "pending_entry"),
        (False, "pending_entry"),
        (True, "entered"),
        (False, "entered"),
    ],
    ids=[
        "pending-valid-explicit-fk",
        "pending-no-explicit-fk",
        "entered-valid-explicit-fk",
        "entered-no-explicit-fk",
    ],
)
def test_manual_close_rejects_duplicated_legacy_deepcoin_key_without_writes(
    tmp_path, use_explicit_binding, lifecycle_status
):
    database_path = tmp_path / "research.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE execution_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kol_id VARCHAR(255),
            chat_id INTEGER,
            message_id INTEGER,
            symbol VARCHAR(64),
            side VARCHAR(16),
            venue VARCHAR(64),
            order_id VARCHAR(255),
            created_at DATETIME
        )
        """
    )
    connection.commit()
    connection.close()
    app = create_web_app(database_path=database_path)
    with app.state.session_factory() as session:
        bindings = [
            ExecutionBinding(
                kol_id=f"duplicate:{index}",
                chat_id=88,
                message_id=13,
                symbol="ETH",
                side="short",
                venue="deepcoin",
                status="active",
                last_exchange_status="position_active",
            )
            for index in (1, 2)
        ]
        session.add_all(bindings)
        session.flush()
        legs = [
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                leg_index=1,
                purpose="entry",
                venue="deepcoin",
                status="active",
                attribution_status="unassigned",
            )
            for binding in bindings
        ]
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=13,
            symbol="ETH",
            side="short",
            lifecycle_status=lifecycle_status,
            signal_at=datetime(2026, 7, 15, 13, 0),
            entered_at=datetime(2026, 7, 15, 13, 1),
            execution_binding_id=bindings[0].id if use_explicit_binding else None,
        )
        session.add_all([lifecycle, *legs])
        session.commit()
        lifecycle_id = lifecycle.id
        binding_ids = [binding.id for binding in bindings]
        leg_ids = [leg.id for leg in legs]

    response = TestClient(app, raise_server_exceptions=False).post(
        f"/api/strategy-lifecycles/{lifecycle_id}/manual-close",
        json={"note": "must not apply"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "manual close requires unique execution binding and either entered lifecycle "
        "or legacy pending_entry with entered_at"
    )
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        bindings = [session.get(ExecutionBinding, binding_id) for binding_id in binding_ids]
        legs = [session.get(ExecutionOrderLeg, leg_id) for leg_id in leg_ids]
        assert lifecycle.lifecycle_status == lifecycle_status
        assert lifecycle.exit_reason is None
        assert lifecycle.exited_at is None
        assert [binding.status for binding in bindings] == ["active", "active"]
        assert [binding.last_exchange_status for binding in bindings] == [
            "position_active",
            "position_active",
        ]
        assert [leg.status for leg in legs] == ["active", "active"]
        assert [leg.terminal_reason for leg in legs] == [None, None]


def test_manual_close_rejects_nullable_legacy_explicit_binding_without_writes(tmp_path):
    database_path = tmp_path / "research.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE execution_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kol_id VARCHAR(255),
            chat_id INTEGER,
            message_id INTEGER,
            symbol VARCHAR(64),
            side VARCHAR(16),
            venue VARCHAR(64),
            order_id VARCHAR(255),
            created_at DATETIME
        )
        """
    )
    connection.commit()
    connection.close()
    app = create_web_app(database_path=database_path)
    with app.state.session_factory() as session:
        binding = ExecutionBinding(
            kol_id="legacy:null-key",
            chat_id=None,
            message_id=14,
            symbol="ETH",
            side="short",
            venue="deepcoin",
            status="active",
            last_exchange_status="position_active",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=14,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 15, 14, 0),
            entered_at=datetime(2026, 7, 15, 14, 1),
            execution_binding_id=binding.id,
        )
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            leg_index=1,
            purpose="entry",
            venue="deepcoin",
            status="active",
            attribution_status="unassigned",
        )
        session.add_all([lifecycle, leg])
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id
        leg_id = leg.id

    response = TestClient(app, raise_server_exceptions=False).post(
        f"/api/strategy-lifecycles/{lifecycle_id}/manual-close",
        json={"note": "must not apply"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "manual close requires unique execution binding and either entered lifecycle "
        "or legacy pending_entry with entered_at"
    )
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)
        leg = session.get(ExecutionOrderLeg, leg_id)
        assert lifecycle.lifecycle_status == "pending_entry"
        assert lifecycle.exit_reason is None
        assert lifecycle.exited_at is None
        assert (binding.status, binding.last_exchange_status) == (
            "active",
            "position_active",
        )
        assert (leg.status, leg.terminal_reason) == ("active", None)


def test_bound_position_close_api_rejects_unbound_or_ambiguous_position_before_order_submission(tmp_path):
    class FakeDeepcoinClient:
        def __init__(self):
            self.order_payloads = []

        def list_positions(self, *, inst_id=None):
            return []

        def place_order(self, payload):
            self.order_payloads.append(payload)
            return {"code": "0", "data": {"ordId": "should-not-exist"}}

    fake_client = FakeDeepcoinClient()
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: fake_client,
    )
    with app.state.session_factory() as session:
        session.add_all(
            [
                ExecutionBinding(
                    kol_id="group:88",
                    chat_id=88,
                    message_id=10,
                    symbol="BTC",
                    side="short",
                    status="active",
                    pos_id="pos-ambiguous",
                ),
                ExecutionBinding(
                    kol_id="group:89",
                    chat_id=89,
                    message_id=11,
                    symbol="BTC",
                    side="short",
                    status="active",
                    pos_id="pos-ambiguous",
                ),
            ]
        )
        session.commit()

    client = TestClient(app)

    unbound = client.post("/api/execution/close-bound-position", json={"pos_id": "pos-unbound"})
    ambiguous = client.post("/api/execution/close-bound-position", json={"pos_id": "pos-ambiguous"})
    extra_field = client.post(
        "/api/execution/close-bound-position",
        json={"pos_id": "pos-unbound", "size": "999999"},
    )

    assert unbound.status_code == 409
    assert ambiguous.status_code == 409
    assert extra_field.status_code == 400
    assert fake_client.order_payloads == []


def test_bound_position_close_api_submits_exact_live_position_and_keeps_lifecycle_open(tmp_path):
    class FakeDeepcoinClient:
        def __init__(self):
            self.position_calls = []
            self.order_payloads = []

        def list_positions(self, *, inst_id=None):
            self.position_calls.append(inst_id)
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-target",
                    "posSide": "short",
                    "pos": "11",
                    "avgPx": "64350",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-other",
                    "posSide": "short",
                    "pos": "7",
                },
            ]

        def place_order(self, payload):
            self.order_payloads.append(payload)
            return {"code": "0", "data": {"ordId": "close-target"}}

    fake_client = FakeDeepcoinClient()
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: fake_client,
        now_provider=lambda: datetime(2026, 7, 11, 11, 0),
    )
    with app.state.session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 11, 10, 0),
            entered_at=datetime(2026, 7, 11, 10, 1),
        )
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="short",
            status="active",
            pos_id="pos-target",
            margin_mode="cross",
            position_mode="split",
        )
        session.add_all([lifecycle, binding])
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                leg_index=1,
                purpose="entry",
                order_kind="market",
                pos_id="pos-target",
                venue="deepcoin",
                attribution_status="verified",
                attribution_evidence_json=(
                    '{"policy_version":2,"evidence_type":"test_verified_entry"}'
                ),
                status="active",
            )
        )
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id

    response = TestClient(app).post(
        "/api/execution/close-bound-position",
        json={"pos_id": "pos-target"},
    )

    assert response.status_code == 200
    assert response.json()["submitted"] is True
    assert response.json()["pos_id"] == "pos-target"
    assert fake_client.position_calls == ["BTC-USDT-SWAP"]
    assert fake_client.order_payloads == [
        {
            "instId": "BTC-USDT-SWAP",
            "tdMode": "cross",
            "side": "buy",
            "posSide": "short",
            "ordType": "market",
            "sz": "11",
            "mrgPosition": "split",
            "closePosId": "pos-target",
        }
    ]
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)
        event = (
            session.query(ExecutionEvent)
            .filter(ExecutionEvent.action == "close_bound_position_market")
            .one()
        )

        assert lifecycle.lifecycle_status == "entered"
        assert binding.status == "active"
        assert event.action == "close_bound_position_market"
        assert event.pos_id == "pos-target"
        assert event.request_json is not None and '"closePosId": "pos-target"' in event.request_json


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


def test_execution_sync_api_never_submits_position_protection_orders(tmp_path):
    class FakeDeepcoinClient:
        def __init__(self):
            self.protection_payloads = []

        def list_positions(self, *, inst_id=None):
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
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

        def list_order_history(self, *, inst_id=None):
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

        def list_trigger_orders_pending(self, *, inst_id):
            return []

        def set_position_sltp(self, payload):
            self.protection_payloads.append(payload)
            return {"code": "0", "data": {"ordId": "tpsl-new"}}

    fake_client = FakeDeepcoinClient()
    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: fake_client,
        now_provider=lambda: datetime(2026, 7, 2, 10, 5),
    )
    with app.state.session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=10,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 2, 10, 0),
            entered_at=datetime(2026, 7, 2, 10, 1),
            stop_loss=1640,
            take_profit="1608/1600/1580",
        )
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=10,
            symbol="ETH",
            side="short",
            status="active",
            order_id="order-market,order-limit",
            client_order_id="client-market,client-limit",
            pos_id="pos-market",
        )
        session.add_all([lifecycle, binding])
        session.flush()
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id="order-market",
                    client_order_id="client-market",
                    pos_id="pos-market",
                    venue="deepcoin",
                    status="active",
                    attribution_status="verified",
                    attribution_evidence_json=(
                        '{"policy_version":2,"evidence_type":"test_verified_entry"}'
                    ),
                ),
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    leg_index=2,
                    purpose="entry",
                    order_kind="limit",
                    order_id="order-limit",
                    client_order_id="client-limit",
                    venue="deepcoin",
                    status="open",
                    attribution_status="unassigned",
                ),
            ]
        )
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id

    client = TestClient(app)
    response = client.post("/api/execution/sync-deepcoin")

    assert response.status_code == 200
    assert response.json()["reconciled_active"] == 1
    assert response.json()["manually_closed"] == 0
    assert "protection_recovered" not in response.json()
    assert fake_client.protection_payloads == []
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)

        assert lifecycle.lifecycle_status == "entered"
        assert binding.status == "active"
        assert binding.pos_id == "pos-market,order-limit"


def test_execution_sync_api_keeps_payload_only_position_unassigned(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self, *, inst_id=None):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "1001123877920316",
                    "posSide": "short",
                    "pos": "12",
                    "avgPx": "62300.0",
                }
            ]

        def list_open_orders(self, *, inst_id=None):
            return []

    app = create_web_app(
        database_path=tmp_path / "research.db",
        group_config=GroupConfig(
            groups=[
                TargetGroupConfig(
                    chat_title="舒琴会员群-11分组",
                    chat_id=-1002370796392,
                    custom_group_label="舒琴会员群-11分组",
                )
            ]
        ),
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        now_provider=lambda: datetime(2026, 7, 3, 16, 0),
    )
    with app.state.session_factory() as session:
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
        session.add(
            ExecutionBinding(
                kol_id="group:-1002370796392",
                chat_id=-1002370796392,
                message_id=3240,
                symbol="BTC",
                side="short",
                status="open",
                order_id="1001123853022859,1001123853022867",
                client_order_id="TKSQ3240E1,TKSQ3240E2",
                payload_json=json.dumps(
                    {
                        "submitted_orders": [
                            {
                                "request": {
                                    "instId": "BTC-USDT-SWAP",
                                    "posSide": "short",
                                    "price": "62300.0",
                                    "triggerPrice": "62300.0",
                                    "sz": "12.0",
                                }
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.commit()

    client = TestClient(app)
    sync_response = client.post("/api/execution/sync-deepcoin")

    assert sync_response.status_code == 200
    assert sync_response.json()["reconciled_active"] == 0
    page_response = client.get("/execution")
    assert page_response.status_code == 200
    assert "1001123877920316" in page_response.text
    assert "舒琴会员群-11分组" in page_response.text
    assert "unbound_live_position" in page_response.text


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
        leg = session.query(ExecutionOrderLeg).filter_by(pos_id="pos-btc").one()
        assert leg.execution_binding_id == binding.id
        assert leg.attribution_status == "verified"
        assert leg.order_kind == "manual_bind"


@pytest.mark.parametrize(
    "attribution_status", ["attribution_conflict", "evidence_unavailable"]
)
def test_bind_live_position_api_does_not_override_unresolved_attribution(
    tmp_path, attribution_status
):
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
        unresolved_binding = ExecutionBinding(
            kol_id="unresolved",
            chat_id=999,
            message_id=999,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="pos-btc",
            status="unknown",
        )
        session.add_all([lifecycle, unresolved_binding])
        session.flush()
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=unresolved_binding.id,
                leg_index=1,
                purpose="entry",
                order_kind="market",
                pos_id="pos-btc",
                venue="deepcoin",
                attribution_status=attribution_status,
                status="active",
            )
        )
        session.commit()
        lifecycle_id = lifecycle.id

    response = TestClient(app).post(
        "/api/execution/bind-live-position",
        json={"pos_id": "pos-btc", "lifecycle_id": lifecycle_id},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        f"position attribution cannot be manually overridden:{attribution_status}"
    )
    with app.state.session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        leg = session.query(ExecutionOrderLeg).one()
    assert lifecycle.execution_binding_id is None
    assert leg.attribution_status == attribution_status


def test_bind_live_position_api_accepts_entry_range_when_actual_entry_drifted(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-btc",
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": "62600",
                }
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        now_provider=lambda: datetime(2026, 7, 8, 15, 0),
    )
    with app.state.session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=424,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 8, 14, 30),
            entered_at=datetime(2026, 7, 8, 14, 35),
            entry_range_low=62200,
            entry_range_high=63300,
            entry_price_actual=63270.95,
            stop_loss=2.0,
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


def test_bind_live_position_api_appends_second_split_position_to_lifecycle(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-btc-1",
                    "posSide": "long",
                    "pos": "1",
                    "avgPx": "62600",
                },
                {
                    "instId": "BTC-USDT-SWAP",
                    "posId": "pos-btc-2",
                    "posSide": "long",
                    "pos": "2",
                    "avgPx": "62600",
                },
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        now_provider=lambda: datetime(2026, 7, 8, 15, 0),
    )
    with app.state.session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=424,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 8, 14, 30),
            entered_at=datetime(2026, 7, 8, 14, 35),
            entry_range_low=62200,
            entry_range_high=63300,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    client = TestClient(app)
    first_response = client.post(
        "/api/execution/bind-live-position",
        json={"pos_id": "pos-btc-1", "lifecycle_id": lifecycle_id},
    )
    second_response = client.post(
        "/api/execution/bind-live-position",
        json={"pos_id": "pos-btc-2", "lifecycle_id": lifecycle_id},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    with app.state.session_factory() as session:
        binding = session.get(ExecutionBinding, first_response.json()["binding_id"])

    assert binding.pos_id == "pos-btc-1,pos-btc-2"


def test_bind_live_position_api_normalizes_deepcoin_sell_side(tmp_path):
    class FakeDeepcoinClient:
        def list_positions(self):
            return [
                {
                    "instId": "ETH-USDT-SWAP",
                    "posId": "pos-eth",
                    "side": "sell",
                    "pos": "6.4",
                    "avgPx": "1624.5",
                }
            ]

    app = create_web_app(
        database_path=tmp_path / "research.db",
        deepcoin_client_factory=lambda: FakeDeepcoinClient(),
        now_provider=lambda: datetime(2026, 7, 2, 11, 30),
    )
    with app.state.session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=30,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 2, 10, 0),
            entered_at=datetime(2026, 7, 2, 10, 1),
            entry_price_actual=1624.5,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    client = TestClient(app)
    response = client.post(
        "/api/execution/bind-live-position",
        json={"pos_id": "pos-eth", "lifecycle_id": lifecycle_id},
    )

    assert response.status_code == 200
    with app.state.session_factory() as session:
        binding = session.get(ExecutionBinding, response.json()["binding_id"])

        assert binding.symbol == "ETH"
        assert binding.side == "short"
        assert binding.pos_id == "pos-eth"


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


def test_groups_partial_uses_lifecycle_counts_for_holding_badges(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    with app.state.session_factory() as session:
        session.add(
            RawMessage(
                chat_id=88,
                message_id=10,
                sender_name="Demo Group",
                text="BTC long 60000 SL 59000 TP 61000",
                posted_at=datetime(2026, 7, 3, 10, 0),
            )
        )
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=10,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="active",
            pos_id="pos-live",
        )
        session.add(binding)
        session.flush()
        session.add(
            StrategyLifecycle(
                chat_id=88,
                message_id=10,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 3, 10, 0),
                entered_at=datetime(2026, 7, 3, 10, 1),
                execution_binding_id=binding.id,
            )
        )
        session.commit()

    client = TestClient(app)
    response = client.get("/groups?selected_chat_id=88")

    assert response.status_code == 200
    assert re.search(r"kol-status-holding[\s\S]*?1", response.text)
    assert re.search(r"kol-status-text[\s\S]*?1", response.text)


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

    def fake_authoritative_processor(message_id):
        result = app.state.message_recognizer(
            app.state.session_factory,
            raw_message_id=message_id,
            ai_recognition_config=AiRecognitionConfig(),
        )
        return SimpleNamespace(
            recognition=result,
            assessment=SimpleNamespace(
                agreement_status="agreed",
                differences=[],
                mimo=SimpleNamespace(model="mimo-v2.5", payload={}, status=result.status),
                deepseek_payload=None,
            ),
            automation={"status": "skipped", "reason": "test"},
        )

    app.state.authoritative_processor = fake_authoritative_processor

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

    def fake_authoritative_processor(message_id):
        result = app.state.message_recognizer(
            app.state.session_factory,
            raw_message_id=message_id,
            ai_recognition_config=AiRecognitionConfig(),
        )
        return SimpleNamespace(
            recognition=result,
            assessment=SimpleNamespace(
                agreement_status="agreed",
                differences=[],
                mimo=SimpleNamespace(model="mimo-v2.5", payload={}, status=result.status),
                deepseek_payload=None,
            ),
            automation=app.state.auto_trade_executor(message_id),
        )

    app.state.authoritative_processor = fake_authoritative_processor

    client = TestClient(app)
    response = client.post(f"/api/messages/{raw_message_id}/recognize")

    assert response.status_code == 200
    assert calls == [raw_message_id]
    assert response.json()["auto_trade"] == {
        "status": "submitted",
        "management_action": "close_position",
    }


def test_message_recognition_api_delivers_completed_instruction_summary(
    tmp_path,
    monkeypatch,
):
    app = create_web_app(
        database_path=tmp_path / "manual-summary.db",
        ai_recognition_config_path=tmp_path / "ai_recognition.yaml",
    )
    app.state.notification_bot_config = SystemOperatorBotConfig(
        bot_token="system-token",
        chat_id="system-chat",
    )
    with app.state.session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=2002,
            sender_name="VIP room",
            text="ETH long",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = MessageRecognitionResult(
        raw_message_id=raw_message_id,
        status="是策略",
        parse_source="mimo_authoritative",
    )
    app.state.authoritative_processor = lambda _message_id: SimpleNamespace(
        recognition=result,
        assessment=SimpleNamespace(
            agreement_status="pending",
            semantic_review_status="pending",
            differences=[],
            mimo=SimpleNamespace(model="mimo-v2.5"),
        ),
        automation={
            "status": "completed",
            "items": [
                {
                    "item_id": 9,
                    "sequence": 0,
                    "instruction_kind": "entry",
                    "strategy_instance_id": "deepcoin:88:2002:ETH:long",
                    "status": "submitted",
                }
            ],
        },
    )
    deliveries: list[dict] = []

    async def fake_deliver(**kwargs):
        deliveries.append(kwargs)
        return True

    monkeypatch.setattr(
        "telegram_kol_research.web_app."
        "_deliver_authoritative_instruction_summary",
        fake_deliver,
    )

    response = TestClient(app).post(f"/api/messages/{raw_message_id}/recognize")

    assert response.status_code == 200
    assert len(deliveries) == 1
    assert deliveries[0]["raw_message_id"] == raw_message_id
    assert deliveries[0]["chat_title"] == "VIP room"


def test_message_recognition_api_reports_pending_without_scheduling_review(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "research.db"

    def fake_recognizer(*args, **kwargs):
        return MessageRecognitionResult(
            raw_message_id=kwargs["raw_message_id"],
            status="非策略",
            summary=None,
            reason="DeepSeek认为只是取消说明",
            parse_source="text_ai",
        )

    app = create_web_app(
        database_path=database_path,
        ai_recognition_config_path=tmp_path / "ai_recognition.yaml",
        message_recognizer=fake_recognizer,
    )
    app.state.system_operator_bot_config = SystemOperatorBotConfig(
        bot_token="system-token",
        chat_id="system-chat",
    )
    auto_trade_calls: list[int] = []
    app.state.auto_trade_executor = lambda raw_message_id: (
        auto_trade_calls.append(raw_message_id) or {"status": "submitted"}
    )
    def fake_schedule_authoritative_notification(**kwargs):
        raise AssertionError("manual recognition must not schedule semantic review")

    monkeypatch.setattr(
        "telegram_kol_research.web_app._schedule_authoritative_notification",
        fake_schedule_authoritative_notification,
    )

    with app.state.session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=3885,
            sender_name="比特币飞扬 11分组",
            text="今日两次BTC策略都没有入场，取消吧",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    def fake_authoritative_processor(message_id):
        return SimpleNamespace(
            recognition=fake_recognizer(raw_message_id=message_id),
            assessment=SimpleNamespace(
                agreement_status="pending",
                differences=[],
                mimo=SimpleNamespace(
                    model="mimo-v2.5",
                    status="是策略",
                    payload={"reason": "MiMo认为这是取消旧挂单"},
                    error_message=None,
                ),
                deepseek_payload=None,
            ),
            automation=app.state.auto_trade_executor(message_id),
        )

    app.state.authoritative_processor = fake_authoritative_processor

    client = TestClient(app)
    response = client.post(f"/api/messages/{raw_message_id}/recognize")

    assert response.status_code == 200
    assert response.json()["ai_conflict"] is False
    assert response.json()["agreement_status"] == "pending"
    assert response.json()["semantic_review_status"] == "pending"
    assert response.json()["notification_scheduled"] is False
    assert response.json()["auto_trade"] == {"status": "submitted"}
    assert auto_trade_calls == [raw_message_id]


def test_message_recognition_api_suppresses_low_value_authoritative_failure(
    tmp_path, monkeypatch
):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=tmp_path / "ai_recognition.yaml",
    )
    app.state.notification_bot_config = SystemOperatorBotConfig(
        bot_token="system-token",
        chat_id="system-chat",
    )
    audit: list[dict] = []

    monkeypatch.setattr(
        "telegram_kol_research.telegram_live_listener.update_recognition_execution_outcome",
        lambda *args, **kwargs: audit.append(kwargs),
    )

    with app.state.session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=3346,
            sender_name="舒琴会员群-11分组",
            text="美光MU 800出头比如810附近还能再吃一次，850和880分批走。",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    def fake_authoritative_processor(message_id):
        return SimpleNamespace(
            recognition=MessageRecognitionResult(
                raw_message_id=message_id,
                status="识别失败",
                summary=None,
                reason="timeout",
                parse_source="mimo_authoritative",
            ),
            assessment=SimpleNamespace(
                agreement_status="authoritative_failed",
                semantic_review_status="completed",
                differences=[],
                mimo=SimpleNamespace(
                    model="mimo-v2.5",
                    status="识别失败",
                    payload={},
                    error_message="The read operation timed out",
                ),
                deepseek_payload=None,
            ),
            automation={"status": "skipped", "reason": "mimo_authoritative_failed"},
        )

    app.state.authoritative_processor = fake_authoritative_processor

    response = TestClient(app).post(f"/api/messages/{raw_message_id}/recognize")

    assert response.status_code == 200
    assert response.json()["notification_scheduled"] is False
    assert [row["notification_status"] for row in audit] == [
        "suppressed_low_value"
    ]


@pytest.mark.parametrize("semantic_review_status", ["execution_pending", "execution_running"])
def test_message_recognition_api_preserves_execution_review_state(
    tmp_path, semantic_review_status
):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=tmp_path / "ai_recognition.yaml",
    )
    with app.state.session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=4, text="BTC long")
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = MessageRecognitionResult(
        raw_message_id=raw_message_id,
        status="是策略",
        summary="BTC long",
        reason=None,
        parse_source="mimo",
    )
    app.state.authoritative_processor = lambda _message_id: SimpleNamespace(
        recognition=result,
        assessment=SimpleNamespace(
            agreement_status="agreed",
            semantic_review_status=semantic_review_status,
            differences=["must-not-be-returned-before-review"],
            mimo=SimpleNamespace(model="mimo-v2.5"),
        ),
        automation={"status": "pending"},
    )

    response = TestClient(app).post(f"/api/messages/{raw_message_id}/recognize")

    assert response.status_code == 200
    assert response.json()["semantic_review_status"] == semantic_review_status
    assert response.json()["agreement_status"] == "pending"
    assert response.json()["ai_conflict"] is False
    assert response.json()["differences"] == []


def test_message_recognition_api_defaults_review_to_pending_not_immediate_agreement(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=tmp_path / "ai_recognition.yaml",
    )
    with app.state.session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=5, text="BTC long")
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = MessageRecognitionResult(
        raw_message_id=raw_message_id,
        status="是策略",
        summary="BTC long",
        reason=None,
        parse_source="mimo",
    )
    app.state.authoritative_processor = lambda _message_id: SimpleNamespace(
        recognition=result,
        assessment=SimpleNamespace(
            agreement_status="agreed",
            differences=[],
            mimo=SimpleNamespace(model="mimo-v2.5"),
        ),
        automation={"status": "pending"},
    )

    response = TestClient(app).post(f"/api/messages/{raw_message_id}/recognize")

    assert response.status_code == 200
    assert response.json()["semantic_review_status"] == "pending"
    assert response.json()["agreement_status"] == "pending"
    assert response.json()["ai_conflict"] is False


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
        binding = ExecutionBinding(
            kol_id="alice",
            chat_id=88,
            message_id=6,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="active",
            pos_id="pos-live",
        )
        session.add(binding)
        session.flush()
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
                StrategyLifecycle(
                    chat_id=88,
                    message_id=6,
                    symbol="BTC",
                    side="short",
                    lifecycle_status="pending_entry",
                    signal_at=datetime(2026, 6, 12, 8, 5),
                    entry_range_low=60300,
                    entry_range_high=60800,
                    execution_binding_id=binding.id,
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


def test_group_detail_logs_route_timings(tmp_path):
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

    client = TestClient(app)

    response = client.get("/groups/88/detail")

    assert response.status_code == 200
    log_text = (app.state.log_directory / "telegram-kol.log").read_text(
        encoding="utf-8"
    )
    assert "web_perf route=/groups/{chat_id}/detail chat_id=88" in log_text
    assert "messages_ms=" in log_text
    assert "template_ms=" in log_text


def test_strategy_mid_panel_logs_route_timings(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    client = TestClient(app)

    response = client.get("/groups/88/strategy-mid-panel?filter=holding")

    assert response.status_code == 200
    log_text = (app.state.log_directory / "telegram-kol.log").read_text(
        encoding="utf-8"
    )
    assert "web_perf route=/groups/{chat_id}/strategy-mid-panel chat_id=88" in log_text
    assert "filter=holding" in log_text
    assert "lifecycle_counts_ms=" in log_text
    assert "holding_ms=" in log_text


def test_ai_recognition_config_api_ignores_legacy_prompt_fields(tmp_path):
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
    assert "recognition_prompt" not in response.json()
    assert "lifecycle_event_prompt" not in response.json()
    assert "mimo_direct_prompt" not in response.json()
    assert "prompts" not in response.json()
    page = client.get("/")
    assert "只识别明确策略。" not in page.text
    assert "识别生命周期事件。" not in page.text
    assert "直接阅读图片和文字。" not in page.text


def test_more_panel_renders_prompt_registry_without_legacy_prompt_inputs(tmp_path):
    config_path = tmp_path / "ai_recognition.yaml"
    app = create_web_app(
        database_path=tmp_path / "research.db",
        ai_recognition_config_path=config_path,
    )
    client = TestClient(app)

    response = client.get("/more-panel")

    assert response.status_code == 200
    assert "data-ai-prompt-center" in response.text
    assert 'data-ai-prompt-input="recognition_prompt"' not in response.text
    assert 'data-ai-prompt-input="lifecycle_event_prompt"' not in response.text
    assert 'data-ai-prompt-input="mimo_direct_prompt"' not in response.text


def test_ai_recognition_config_api_does_not_return_legacy_prompt_authority(tmp_path):
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
    assert "prompts" not in response.json()
    assert "recognition_prompt" not in response.json()


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
    assert payload["text_provider"]["api_key"] == ""
    assert payload["image_provider"]["api_key"] == ""
    assert payload["text_provider"]["api_key_configured"] is True
    assert payload["image_provider"]["api_key_configured"] is True


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

        def list_trigger_orders_pending(self, *, inst_id):
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
    assert fake_client.payloads == []
    assert [payload["orderType"] for payload in fake_client.trigger_payloads] == [
        "limit",
        "limit",
    ]
    assert fake_client.trigger_payloads[0]["tdMode"] == "cross"
    assert all(not any(key.startswith("tp") for key in payload) for payload in fake_client.trigger_payloads)
    assert fake_client.trigger_payloads[0]["slTriggerPx"] == 67500.0


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

        def list_trigger_orders_pending(self, *, inst_id):
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
    assert fake_client.payloads == []
    assert fake_client.trigger_payloads[0]["tdMode"] == "cross"
    assert fake_client.trigger_payloads[0]["tpTriggerPx"] == 69000.0
