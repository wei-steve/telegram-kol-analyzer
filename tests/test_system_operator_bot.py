import asyncio

import telegram_kol_research.system_operator_bot as operator_bot_module
from telegram_kol_research.system_operator_bot import (
    SystemOperatorBotConfig,
    build_pending_entry_expiry_review_reply_markup,
    format_ai_recognition_conflict_review_message,
    format_semantic_disagreement_notification,
    format_pending_entry_expiry_review_message,
    load_system_operator_bot_config,
    send_semantic_disagreement_notification,
    system_operator_bot_enabled,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import ExecutionBinding, StrategyLifecycle
from telegram_kol_research.telegram_bot_commands import (
    _bot_http_timeout,
    _format_callback_resolution_text,
    process_system_operator_callback_data,
    process_system_operator_command,
)
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
    assert "/expiry_continue" not in message


def test_system_operator_bot_disabled_without_dedicated_destination():
    assert not system_operator_bot_enabled(
        SystemOperatorBotConfig(bot_token="", chat_id="", timeout_seconds=10)
    )


def test_format_ai_recognition_conflict_review_message_includes_both_model_results():
    message = format_ai_recognition_conflict_review_message(
        {
            "chat_title": "比特币飞扬 11分组",
            "chat_id": -1002960443256,
            "message_id": 3885,
            "posted_at": datetime(2026, 7, 8, 15, 44, 58, tzinfo=UTC),
            "text": "今日两次BTC策略都没有入场，取消吧",
            "deepseek": {
                "status": "非策略",
                "kind": "non_strategy",
                "reason": "DeepSeek 未识别为生命周期事件",
            },
            "mimo": {
                "status": "取消入场",
                "kind": "strategy_related",
                "reason": "MiMo 认为是取消未入场挂单",
            },
        }
    )

    assert "AI识别分歧告警" in message
    assert "比特币飞扬 11分组" in message
    assert "#3885" in message
    assert "DeepSeek: 非策略 / non_strategy" in message
    assert "MiMo: 取消入场 / strategy_related" in message
    assert "权威结果: MiMo" in message
    assert "已按 MiMo 结果继续" in message
    assert "已暂停" not in message
    assert "今日两次BTC策略都没有入场" in message


def test_format_semantic_disagreement_notification_is_critical_and_evidence_backed():
    message = format_semantic_disagreement_notification(
        {
            "chat_title": "峰哥高级会员群-11分组",
            "chat_id": -1001,
            "message_id": 8401,
            "posted_at": datetime(2026, 7, 13, 8, 1, tzinfo=UTC),
            "text": "现价62800附近出局，空仓等待。",
            "mimo": {
                "status": "exit_full",
                "reason": "原文要求全部出局",
            },
            "deepseek": {
                "status": "exit_partial",
                "reason": "独立复核认为只是部分止盈",
                "evidence": ["现价62800附近出局", "空仓等待"],
            },
            "automation": {
                "status": "submitted",
                "reason": "close_position",
            },
            "conflict_types": ["full_vs_partial_exit"],
        }
    )

    assert "【AI语义严重分歧】" in message
    assert "原始来源: 峰哥高级会员群-11分组 / -1001 / #8401" in message
    assert "权威结果: MiMo / exit_full / 原文要求全部出局" in message
    assert "自动化结果: submitted / close_position" in message
    assert "复核结果: DeepSeek / exit_partial / 独立复核认为只是部分止盈" in message
    assert "冲突类型: full_vs_partial_exit" in message
    assert "依据: 现价62800附近出局；空仓等待" in message
    assert "已按MiMo结果继续，未等待人工复核" in message
    assert "消息已处理，不需要审批" in message


def test_format_semantic_disagreement_notification_truncates_source_and_evidence():
    source = "原文" + "甲" * 2_000 + "SOURCE_END"
    evidence = "证据" + "乙" * 2_000 + "EVIDENCE_END"

    message = format_semantic_disagreement_notification(
        {
            "text": source,
            "mimo": {"status": "exit_full"},
            "deepseek": {"status": "exit_partial", "evidence": [evidence]},
            "automation": {"status": "submitted", "reason": "close_position"},
            "conflict_types": ["full_vs_partial_exit"],
        }
    )

    assert "SOURCE_END" not in message
    assert "EVIDENCE_END" not in message
    assert message.count("...") >= 2
    assert len(message) < 3_000


def test_send_semantic_disagreement_notification_is_read_only(monkeypatch):
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(operator_bot_module, "send_system_operator_bot_message", fake_send)
    config = SystemOperatorBotConfig(bot_token="token", chat_id="chat")

    asyncio.run(
        send_semantic_disagreement_notification(
            config=config,
            payload={
                "text": "全部出局",
                "mimo": {"status": "exit_full"},
                "deepseek": {"status": "none", "evidence": ["全部出局"]},
                "automation": {"status": "submitted", "reason": "close_position"},
                "conflict_types": ["urgent_exit_missed"],
            },
        )
    )

    assert len(sent) == 1
    assert sent[0]["config"] is config
    assert sent[0].get("reply_markup") is None
    assert "inline_keyboard" not in sent[0]


def test_bot_http_timeout_allows_long_polling_read_to_finish():
    timeout = _bot_http_timeout(10)

    assert timeout.read >= 35
    assert timeout.connect == 10


def test_callback_resolution_text_keeps_strategy_context_after_button_click():
    original_message = "\n".join(
        [
            "\u3010\u5f85\u5165\u573a\u7b56\u7565\u8d85\u65f6\u590d\u6838\u3011",
            "\u7fa4\u7ec4: \u7c73\u5a05 VIP 11\u5206\u7ec4",
            "\u7fa4ID: -1002370796392",
            "\u7b56\u7565\u4ee3\u7801: #3251",
            "\u5185\u90e8ID: 354",
            "\u4ea4\u6613\u5bf9: BTC short",
            "\u539f\u7b56\u7565\u65f6\u95f4: 2026-07-05 09:32:29 Asia/Shanghai",
            "\u8d85\u65f6\u65f6\u95f4: 2026-07-05 15:32:29 Asia/Shanghai",
            "\u5165\u573a\u533a\u95f4: 62900-63200",
            "\u6b62\u635f: 64200",
            "\u6b62\u76c8: 61000",
        ]
    )

    message = _format_callback_resolution_text(
        callback_data="expiry_continue:354",
        response_text="\u7b56\u7565 #354 \u5df2\u7ee7\u7eed\u7b49\u5f85\u3002",
        operator_name="weichang tan",
        original_message_text=original_message,
    )

    assert "\u2705 \u5df2\u5904\u7406\uff1a\u7ee7\u7eed\u7b49\u5f85" in message
    assert "\u64cd\u4f5c\u4eba: weichang tan" in message
    assert "\u7fa4\u7ec4: \u7c73\u5a05 VIP 11\u5206\u7ec4" in message
    assert "\u539f\u7b56\u7565\u65f6\u95f4: 2026-07-05 09:32:29 Asia/Shanghai" in message
    assert "\u4ea4\u6613\u5bf9: BTC short" in message
    assert "\u5165\u573a\u533a\u95f4: 62900-63200" in message
    assert "\u6b62\u635f: 64200" in message
    assert "\u6b62\u76c8: 61000" in message


def test_format_pending_entry_expiry_review_message_shows_strategy_code_and_internal_id():
    message = format_pending_entry_expiry_review_message(
        {
            "lifecycle_id": 354,
            "chat_id": -1002370796392,
            "chat_title": "\u7c73\u5a05 VIP 11\u5206\u7ec4",
            "message_id": 3251,
            "symbol": "ETH",
            "side": "short",
            "max_age_hours": 6,
            "signal_at": datetime(2026, 7, 4, 15, 54, 12, tzinfo=UTC),
            "expiry_at": datetime(2026, 7, 4, 21, 54, 12, tzinfo=UTC),
            "entry_range_low": 1830,
            "entry_range_high": 1850,
            "stop_loss": 1860,
            "take_profit": "1785/1735/1670",
        }
    )

    assert "\u7b56\u7565\u4ee3\u7801: #3251" in message
    assert "\u5185\u90e8ID: 354" in message
    assert "\u7fa4\u7ec4: \u7c73\u5a05 VIP 11\u5206\u7ec4" in message
    assert "\u7fa4ID: -1002370796392" in message
    assert "\u539f\u7b56\u7565\u65f6\u95f4: 2026-07-04 23:54:12 Asia/Shanghai" in message
    assert "\u8d85\u65f6\u65f6\u95f4: 2026-07-05 05:54:12 Asia/Shanghai" in message


def test_format_pending_entry_expiry_review_message_shows_repeated_review_context():
    message = format_pending_entry_expiry_review_message(
        {
            "lifecycle_id": 354,
            "chat_id": -1002370796392,
            "chat_title": "\u7c73\u5a05 VIP 11\u5206\u7ec4",
            "message_id": 3251,
            "symbol": "BTC",
            "side": "short",
            "max_age_hours": 6,
            "signal_at": datetime(2026, 7, 4, 15, 54, 12, tzinfo=UTC),
            "expiry_at": datetime(2026, 7, 5, 3, 54, 12, tzinfo=UTC),
            "previous_review_at": datetime(2026, 7, 4, 21, 54, 12, tzinfo=UTC),
            "review_reason": "\u4e0a\u6b21\u4eba\u5de5\u9009\u62e9\u7ee7\u7eed\u7b49\u5f85\u540e\u53c8\u8d85\u8fc7 6 \u5c0f\u65f6",
            "entry_range_low": 62900,
            "entry_range_high": 63200,
            "stop_loss": 64200,
            "take_profit": "61000",
        }
    )

    assert "\u4e0a\u6b21\u4eba\u5de5\u7ee7\u7eed\u7b49\u5f85: 2026-07-05 05:54:12 Asia/Shanghai" in message
    assert "\u539f\u56e0: \u4e0a\u6b21\u4eba\u5de5\u9009\u62e9\u7ee7\u7eed\u7b49\u5f85\u540e\u53c8\u8d85\u8fc7 6 \u5c0f\u65f6" in message


def test_build_pending_entry_expiry_review_reply_markup_uses_lifecycle_id_callbacks():
    markup = build_pending_entry_expiry_review_reply_markup({"lifecycle_id": 354})

    assert markup == {
        "inline_keyboard": [
            [{"text": "\u7ee7\u7eed\u7b49\u5f85", "callback_data": "expiry_continue:354"}],
            [
                {
                    "text": "\u8fc7\u671f\u5e76\u64a4\u5355",
                    "callback_data": "expiry_expire_cancel:354",
                },
                {
                    "text": "\u8fc7\u671f\u4f46\u4fdd\u7559\u6302\u5355",
                    "callback_data": "expiry_expire_keep:354",
                },
            ],
        ]
    }


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


def test_process_expiry_continue_accepts_strategy_code_message_id(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3251,
            symbol="ETH",
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
        "/expiry_continue #3251",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "\u7ee7\u7eed\u7b49\u5f85" in response
    assert lifecycle.management_action == "expiry_review_continued"


def test_process_system_operator_callback_data_dispatches_expiry_action(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3251,
            symbol="ETH",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_callback_data(
        session_factory,
        f"expiry_continue:{lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "\u7ee7\u7b49" in response or "\u7ee7\u7eed\u7b49\u5f85" in response
    assert lifecycle.management_action == "expiry_review_continued"


def test_process_expiry_continue_does_not_revert_already_entered_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3251,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            entered_at=datetime(2026, 7, 2, 21, 0, tzinfo=UTC),
            management_action="expiry_review_requested",
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    response = process_system_operator_callback_data(
        session_factory,
        f"expiry_continue:{lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "\u5df2\u4e0d\u662f\u5f85\u5165\u573a" in response
    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entered_at is not None
    assert lifecycle.management_action == "expiry_review_requested"


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


def test_process_expiry_expire_cancel_without_live_binding_keeps_pending_for_manual_review(tmp_path):
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
        f"/expiry_expire_cancel {lifecycle_id}",
        now=datetime(2026, 7, 3, 0, 0, tzinfo=UTC),
        deepcoin_client=object(),
    )

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

    assert "未找到本地 live 挂单" in response
    assert "未标记过期" in response
    assert lifecycle.lifecycle_status == "pending_entry"
    assert lifecycle.exit_reason is None
    assert lifecycle.management_action == "expiry_cancel_failed_no_live_order"


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
