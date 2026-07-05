from telegram_kol_research.system_operator_bot import (
    SystemOperatorBotConfig,
    format_pending_entry_expiry_review_message,
    load_system_operator_bot_config,
    system_operator_bot_enabled,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import ExecutionBinding, StrategyLifecycle
from telegram_kol_research.telegram_bot_commands import process_system_operator_command
from datetime import UTC, datetime


def test_load_system_operator_bot_config_uses_dedicated_env_vars():
    config = load_system_operator_bot_config(
        {
            "TELEGRAM_KOL_SYSTEM_BOT_TOKEN": "system-token",
            "TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID": "987654",
            "TELEGRAM_KOL_SYSTEM_BOT_TIMEOUT_SECONDS": "12",
        },
        env_file_paths=[],
    )

    assert config.bot_token == "system-token"
    assert config.chat_id == "987654"
    assert config.timeout_seconds == 12
    assert system_operator_bot_enabled(config)


def test_format_pending_entry_expiry_review_message_includes_operator_choices():
    message = format_pending_entry_expiry_review_message(
        {
            "lifecycle_id": 442,
            "chat_id": -1001,
            "message_id": 442,
            "symbol": "BTC",
            "side": "short",
            "max_age_hours": 6,
            "entry_range_low": 62900,
            "entry_range_high": 63200,
            "stop_loss": 64200,
            "take_profit": "61000",
        }
    )

    assert "待入场策略超时复核" in message
    assert "#442" in message
    assert "BTC short" in message
    assert "62900-63200" in message
    assert "/expiry_continue 442" in message
    assert "/expiry_expire_cancel 442" in message
    assert "/expiry_expire_keep 442" in message


def test_system_operator_bot_disabled_without_dedicated_destination():
    assert not system_operator_bot_enabled(
        SystemOperatorBotConfig(bot_token="", chat_id="", timeout_seconds=10)
    )


def test_process_expiry_continue_keeps_pending_and_suppresses_repeat_review(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_command(
        session_factory,
        f"/expiry_continue {lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "继续等待" in response
    assert lifecycle.lifecycle_status == "pending_entry"
    assert lifecycle.management_action == "expiry_review_continued"


def test_process_expiry_expire_cancel_does_not_expire_while_live_binding_needs_cancel(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="mia",
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="open",
            order_id="order-1",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            execution_binding_id=binding.id,
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_command(
        session_factory,
        f"/expiry_expire_cancel {lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "已请求撤销交易所挂单" in response
    assert lifecycle.lifecycle_status == "pending_entry"
    assert lifecycle.exit_reason is None
    assert lifecycle.management_action == "expiry_cancel_requested"


def test_process_expiry_expire_cancel_executes_deepcoin_cancel_when_client_is_available(tmp_path):
    class FakeDeepcoinClient:
        def __init__(self):
            self.cancel_payloads = []

        def list_trigger_orders_pending(self, inst_id):
            return []

        def list_open_orders(self, inst_id):
            return [{"instId": inst_id, "ordId": "order-1"}]

        def cancel_order(self, cancel_payload):
            self.cancel_payloads.append(cancel_payload)
            return {"code": "0", "data": {"ordId": cancel_payload.get("ordId")}}

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="mia",
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="open",
            order_id="order-1",
            position_mode="split",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            execution_binding_id=binding.id,
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id
        binding_id = binding.id

    fake_client = FakeDeepcoinClient()
    response = process_system_operator_command(
        session_factory,
        f"/expiry_expire_cancel {lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
        deepcoin_client=fake_client,
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        binding = session.get(ExecutionBinding, binding_id)

    assert "已撤销交易所挂单并标记过期" in response
    assert fake_client.cancel_payloads == [
        {"instId": "BTC-USDT-SWAP", "ordId": "order-1", "mrgPosition": "split"}
    ]
    assert binding.status == "cancelled"
    assert lifecycle.lifecycle_status == "expired"
    assert lifecycle.management_action == "expiry_cancelled_and_expired"


def test_process_expiry_expire_keep_marks_expired_without_cancelling_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_command(
        session_factory,
        f"/expiry_expire_keep {lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "已标记过期" in response
    assert lifecycle.lifecycle_status == "expired"
    assert lifecycle.exit_reason == "expired"
