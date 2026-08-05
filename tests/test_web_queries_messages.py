import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    ExecutionBinding,
    MediaAsset,
    MessageEvidenceVersion,
    RawMessage,
    RecognitionDecision,
    StrategyManagementBatch,
    StrategyMessageLink,
    StrategyThread,
)
from telegram_kol_research.web_queries import (
    _build_message_decision_card,
    load_group_message_page,
    load_group_messages,
    load_messages_in_time_window,
)
from telegram_kol_research.web_queries import _serialize_execution_outcome


def test_load_group_messages_includes_media_and_orders_newest_first_within_page(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        older = RawMessage(
            chat_id=9,
            message_id=1,
            posted_at=datetime(2026, 4, 1, tzinfo=UTC),
            text="older",
        )
        newer = RawMessage(
            chat_id=9,
            message_id=2,
            posted_at=datetime(2026, 4, 2, tzinfo=UTC),
            text="newer",
        )
        session.add_all([older, newer])
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=newer.id, kind="photo", local_path="data/media/9/2.jpg"
            )
        )
        session.commit()

    rows = load_group_messages(session_factory, chat_id=9, limit=10)

    assert [row["message_id"] for row in rows] == [2, 1]
    assert rows[0]["media_assets"][0]["local_path"] == "data/media/9/2.jpg"


def test_load_group_messages_projects_bounded_entry_preamble_assembly(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9,
                message_id=9902,
                posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                text="BTC strategy",
            )
        )
        session.add(
            ExecutionBinding(
                strategy_instance_id="entry-preamble-ui",
                kol_id="chen",
                chat_id=9,
                message_id=9902,
                symbol="BTCUSDT",
                side="long",
                venue="deepcoin",
                payload_json=json.dumps(
                    {
                        "draft": {
                            "entry_preamble_assembly": {
                                "mode": "live",
                                "configured_risk_budget_usdt": 20,
                                "risk_multiplier": "0.5",
                                "effective_risk_budget_usdt": 10,
                                "preamble_message_id": 9901,
                                "strategy_message_id": 9902,
                                "secret": "must-not-render",
                            }
                        }
                    }
                ),
            )
        )
        session.commit()

    row = load_group_messages(session_factory, chat_id=9, limit=10)[0]

    assert row["entry_preamble_assembly"]["risk_calculation"] == (
        "基础风险预算 20 USDT × 仓位倍率 50% = 实际风险预算 10 USDT"
    )
    assert row["entry_preamble_assembly"]["message_pair"] == (
        "前置消息 9901 / 策略消息 9902"
    )
    assert "secret" not in row["entry_preamble_assembly"]


def test_load_group_messages_projects_safe_context_resolution_observability(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=-1009,
            message_id=1462,
            reply_to_message_id=1460,
            posted_at=datetime(2026, 7, 20, 8, 8, tzinfo=UTC),
            text="更新入场",
        )
        session.add(raw)
        session.flush()
        evidence = MessageEvidenceVersion(
            raw_message_id=raw.id,
            version=2,
            input_fingerprint="sha256:evidence",
            model="mimo",
            extraction_status="completed",
            confidence=0.95,
            text_evidence_json='{"observed_text":"更新入场"}',
            image_evidence_json='{"fields":{"entry":"65100"}}',
            normalized_evidence_json="{}",
        )
        thread = StrategyThread(
            chat_id=-1009,
            root_message_id=1460,
            symbol="BTC",
            side="long",
            status="active",
        )
        session.add_all([evidence, thread])
        session.flush()
        session.add_all(
            [
                StrategyMessageLink(
                    strategy_thread_id=thread.id,
                    raw_message_id=raw.id,
                    message_evidence_version_id=evidence.id,
                    relation_kind="revision",
                    resolver="deepseek_context",
                    confidence=0.96,
                    evidence_json='{"safe":"only"}',
                    decision_version="v1",
                    status="active",
                ),
                ContextResolutionAttempt(
                    raw_message_id=raw.id,
                    message_evidence_version_id=evidence.id,
                    context_fingerprint="sha256:context",
                    model="deepseek",
                    request_summary_json='{"message_context":[{"message_id":1460}]}',
                    prompt_versions_json="{}",
                    decision_json=(
                        '{"decision":"unresolved","confidence":0.61,'
                        '"supporting_message_ids":[1460,1462],'
                        '"opposing_message_ids":[],"reason":"等待入场状态"}'
                    ),
                    status="completed",
                    reanalysis_triggers_json='["strategy_state_changed"]',
                ),
            ]
        )
        session.commit()

    context = load_group_messages(
        session_factory,
        chat_id=-1009,
        limit=10,
    )[0]["context_resolution"]

    assert context["reply_to_message_id"] == 1460
    assert context["evidence_version"] == 2
    assert context["evidence_input_kind"] == "text+image"
    assert context["linked_threads"][0]["root_message_id"] == 1460
    assert context["linked_messages"][0]["message_id"] == 1462
    assert context["linked_messages"][0]["posted_at"] is not None
    assert context["confidence"] == 0.61
    assert context["supporting_message_ids"] == [1460, 1462]
    assert context["unresolved_reason"] == "等待入场状态"
    assert context["next_triggers"] == ["strategy_state_changed"]
    assert "request_summary_json" not in context


def test_load_group_messages_limits_excessive_media_assets_per_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9,
            message_id=1,
            posted_at=datetime(2026, 4, 1, tzinfo=UTC),
            text="many images",
        )
        session.add(raw_message)
        session.flush()
        session.add_all(
            [
                MediaAsset(raw_message_id=raw_message.id, kind="photo"),
                MediaAsset(
                    raw_message_id=raw_message.id,
                    kind="photo",
                    local_path="data/media/9/with-path.jpg",
                ),
                MediaAsset(
                    raw_message_id=raw_message.id,
                    kind="photo",
                    ocr_text="BTC long",
                ),
                MediaAsset(raw_message_id=raw_message.id, kind="photo"),
                MediaAsset(raw_message_id=raw_message.id, kind="photo"),
            ]
        )
        session.commit()

    rows = load_group_messages(session_factory, chat_id=9, limit=10)

    media_assets = rows[0]["media_assets"]
    assert len(media_assets) == 3
    assert any(asset["ocr_text"] == "BTC long" for asset in media_assets)
    assert any(asset["local_path"] == "data/media/9/with-path.jpg" for asset in media_assets)


def test_load_group_messages_returns_posted_at_in_local_display_timezone(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add(
            RawMessage(
                chat_id=9,
                message_id=1,
                posted_at=datetime(2026, 6, 12, 8, 30),
                text="utc stored",
            )
        )
        session.commit()

    rows = load_group_messages(session_factory, chat_id=9, limit=10)

    assert rows[0]["posted_at"] == datetime(2026, 6, 12, 16, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_load_group_messages_can_load_older_page(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=9,
                    message_id=1,
                    posted_at=datetime(2026, 4, 1, tzinfo=UTC),
                    text="oldest",
                ),
                RawMessage(
                    chat_id=9,
                    message_id=2,
                    posted_at=datetime(2026, 4, 2, tzinfo=UTC),
                    text="middle",
                ),
                RawMessage(
                    chat_id=9,
                    message_id=3,
                    posted_at=datetime(2026, 4, 3, tzinfo=UTC),
                    text="newest",
                ),
            ]
        )
        session.commit()

    rows = load_group_messages(session_factory, chat_id=9, limit=2, before_message_id=3)

    assert [row["message_id"] for row in rows] == [2, 1]


def test_load_group_messages_can_filter_by_text_and_sender(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=9, message_id=1, sender_name="Alice", text="BTC long"
                ),
                RawMessage(
                    chat_id=9, message_id=2, sender_name="Bob", text="ETH short"
                ),
                RawMessage(
                    chat_id=9, message_id=3, sender_name="Alice", text="Macro note"
                ),
            ]
        )
        session.commit()

    rows = load_group_messages(
        session_factory, chat_id=9, limit=10, search_text="BTC", sender_name="Alice"
    )

    assert len(rows) == 1
    assert rows[0]["text"] == "BTC long"


def test_load_group_message_page_returns_twenty_rows_and_has_more(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=88,
                    message_id=message_id,
                    text=f"message {message_id}",
                )
                for message_id in range(1, 22)
            ]
        )
        session.commit()

    messages, has_more = load_group_message_page(
        session_factory,
        chat_id=88,
        page_size=20,
    )

    assert len(messages) == 20
    assert [message["message_id"] for message in messages] == list(range(21, 1, -1))
    assert has_more is True


@pytest.mark.parametrize("message_count", [19, 20])
def test_load_group_message_page_omits_has_more_on_final_page(
    tmp_path,
    message_count,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=88,
                    message_id=message_id,
                    text=f"message {message_id}",
                )
                for message_id in range(1, message_count + 1)
            ]
        )
        session.commit()

    messages, has_more = load_group_message_page(
        session_factory,
        chat_id=88,
        page_size=20,
    )

    assert len(messages) == message_count
    assert has_more is False


def test_load_group_message_page_applies_cursor_and_filters_before_has_more(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(chat_id=88, message_id=1, sender_name="Alice", text="BTC 1"),
                RawMessage(chat_id=88, message_id=2, sender_name="Alice", text="BTC 2"),
                RawMessage(chat_id=88, message_id=3, sender_name="Alice", text="BTC 3"),
                RawMessage(chat_id=88, message_id=4, sender_name="Bob", text="BTC 4"),
                RawMessage(chat_id=88, message_id=5, sender_name="Alice", text="ETH 5"),
                RawMessage(chat_id=88, message_id=6, sender_name="Alice", text="BTC 6"),
            ]
        )
        session.commit()

    first_page, first_has_more = load_group_message_page(
        session_factory,
        chat_id=88,
        page_size=2,
        before_message_id=6,
        search_text="BTC",
        sender_name="Alice",
    )
    final_page, final_has_more = load_group_message_page(
        session_factory,
        chat_id=88,
        page_size=2,
        before_message_id=3,
        search_text="BTC",
        sender_name="Alice",
    )

    assert [message["message_id"] for message in first_page] == [3, 2]
    assert first_has_more is True
    assert [message["message_id"] for message in final_page] == [2, 1]
    assert final_has_more is False


def test_load_messages_in_time_window_normalizes_aware_local_bounds_to_utc(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        session.add_all(
            [
                RawMessage(
                    chat_id=9,
                    message_id=1,
                    posted_at=datetime(2026, 6, 12, 7, 59),
                    text="before",
                ),
                RawMessage(
                    chat_id=9,
                    message_id=2,
                    posted_at=datetime(2026, 6, 12, 8, 30),
                    text="inside",
                ),
            ]
        )
        session.commit()

    rows = load_messages_in_time_window(
        session_factory,
        chat_id=9,
        posted_after=datetime(2026, 6, 12, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        posted_before=datetime(2026, 6, 12, 17, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        limit=10,
    )

    assert [row["message_id"] for row in rows] == [2]


def test_load_group_messages_serializes_semantic_review_decisions_in_one_bulk_query(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        messages = [
            RawMessage(chat_id=9, message_id=index, text=f"message {index}")
            for index in range(1, 4)
        ]
        session.add_all(messages)
        session.flush()
        session.add_all(
            [
                RecognitionDecision(
                    raw_message_id=message.id,
                    input_kind="text",
                    authoritative_model="mimo-v2.5",
                    authoritative_status="是策略",
                    authoritative_payload_json="{}",
                    agreement_status="disagreed",
                    differences_json='["take_profit"]',
                    comparison_status="completed",
                    disagreement_severity="normal",
                    comparison_model="deepseek-v4-flash",
                    comparison_payload_json=json.dumps(
                        {
                            "reason": "止盈细节不同",
                            "conflict_types": ["non_material_price_detail"],
                        },
                        ensure_ascii=False,
                    ),
                )
                for message in messages
            ]
        )
        session.commit()

    decision_queries: list[str] = []
    engine = session_factory.kw["bind"]

    def track_decision_queries(_conn, _cursor, statement, _parameters, _context, _many):
        if "recognition_decisions" in statement.lower():
            decision_queries.append(statement)

    event.listen(engine, "before_cursor_execute", track_decision_queries)
    try:
        rows = load_group_messages(session_factory, chat_id=9, limit=10)
    finally:
        event.remove(engine, "before_cursor_execute", track_decision_queries)

    assert len(decision_queries) == 1
    assert rows[0]["semantic_review"] == {
        "status": "completed",
        "severity": "normal",
        "label": "普通差异",
        "reason": "止盈细节不同",
        "conflict_types": ["non_material_price_detail"],
        "model": "deepseek-v4-flash",
    }


def test_load_group_messages_distinguishes_submission_from_exchange_confirmation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        pending = RawMessage(chat_id=9, message_id=1, text="先出来，保留40%")
        confirmed = RawMessage(chat_id=9, message_id=2, text="全部离场")
        session.add_all([pending, confirmed])
        session.flush()
        session.add_all(
            [
                RecognitionDecision(
                    raw_message_id=message.id,
                    input_kind="text",
                    authoritative_model="mimo-v2.5",
                    authoritative_status="是策略",
                    authoritative_payload_json="{}",
                    agreement_status="authoritative_only",
                    differences_json="[]",
                    automation_status="executed",
                    automation_reason="close_submitted",
                )
                for message in (pending, confirmed)
            ]
        )
        session.flush()
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="a" * 64,
                raw_message_id=confirmed.id,
                recognition_decision_id=2,
                recognition_generation="generation-1",
                target_lifecycle_id=1,
                strategy_instance_id="deepcoin:9:1:BTC:short",
                execution_binding_id=1,
                intent="full_exit",
                effective_action="full_exit",
                execution_mode="live",
                partial_round_before=0,
                status="succeeded",
                reason_code="management_close_exchange_confirmed",
                target_fingerprint="b" * 64,
                target_snapshot_json="{}",
            )
        )
        session.commit()

    rows = load_group_messages(session_factory, chat_id=9, limit=10)
    by_message_id = {row["message_id"]: row for row in rows}

    assert by_message_id[1]["execution_outcome"] == {
        "state": "pending_confirmation",
        "label": "已提交，等待交易所确认",
        "detail": "平仓请求已提交",
    }
    assert by_message_id[2]["execution_outcome"] == {
        "state": "confirmed",
        "label": "交易所已确认执行",
        "detail": "已根据交易所仓位快照确认",
    }


@pytest.mark.parametrize("status", ["reconciling", "succeeded"])
def test_execution_outcome_without_batch_never_promotes_to_confirmed(status):
    decision = RecognitionDecision(
        raw_message_id=1,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="是策略",
        authoritative_payload_json="{}",
        agreement_status="authoritative_only",
        differences_json="[]",
        automation_status=status,
    )

    outcome = _serialize_execution_outcome(decision, None)

    assert outcome["state"] == "pending_confirmation"
    assert outcome["label"] == "已提交，等待交易所确认"


@pytest.mark.parametrize("status", ["partial_failed", "recovery_required"])
def test_execution_outcome_without_batch_marks_terminal_failures(status):
    decision = RecognitionDecision(
        raw_message_id=1,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="是策略",
        authoritative_payload_json="{}",
        agreement_status="authoritative_only",
        differences_json="[]",
        automation_status=status,
    )

    outcome = _serialize_execution_outcome(decision, None)

    assert outcome["state"] == "error"
    assert outcome["label"] == "未执行成功"


def test_load_group_messages_defensively_serializes_malformed_semantic_review_json(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=9, message_id=1, text="BTC long")
        session.add(raw_message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="是策略",
                authoritative_payload_json="{}",
                agreement_status="disagreed",
                differences_json="[]",
                comparison_status="completed",
                disagreement_severity="critical",
                comparison_model="deepseek-v4-flash",
                comparison_payload_json="not-json",
            )
        )
        session.commit()

    row = load_group_messages(session_factory, chat_id=9, limit=10)[0]

    assert row["semantic_review"] == {
        "status": "completed",
        "severity": "critical",
        "label": "严重分歧",
        "reason": None,
        "conflict_types": [],
        "model": "deepseek-v4-flash",
    }


def test_load_group_messages_builds_manual_review_decision_card_from_authoritative_mimo(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=9,
            message_id=1,
            text="调整一下止损防止插针。",
        )
        session.add(raw_message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload_json=json.dumps(
                    {
                        "reason": "识别到调整止损意图，未提供新的止损价格。",
                        "lifecycle_event": {
                            "event_type": "position_update",
                            "management_action": "move_stop_to_protect",
                            "symbol": "BTC",
                            "side": "short",
                            "stop_loss": None,
                        },
                    },
                    ensure_ascii=False,
                ),
                agreement_status="agreed",
                differences_json="[]",
                comparison_status="completed",
                disagreement_severity="none",
                comparison_model="deepseek-v4-flash",
                comparison_payload_json=json.dumps(
                    {
                        "reason": "同意不可自动执行，建议补充价格后再处理。",
                        "conflict_types": [],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.commit()

    card = load_group_messages(session_factory, chat_id=9, limit=10)[0][
        "decision_card"
    ]

    assert card == {
        "state": "manual_review",
        "state_label": "需人工确认",
        "recommended_action": "不执行",
        "blocker": "未提供新的止损价格",
        "message_facts": [
            {"label": "标的", "value": "BTC"},
            {"label": "方向", "value": "空"},
        ],
        "inherited_context": [],
        "primary_analysis": {
            "label": "主分析 · MiMo",
            "conclusion": "仓位管理",
            "reason": "识别到调整止损意图，未提供新的止损价格。",
        },
        "secondary_review": {
            "label": "辅助复核 · DeepSeek",
            "conclusion": "一致",
            "reason": "同意不可自动执行，建议补充价格后再处理。",
        },
        "agreement": {"label": "一致 · 不自动执行", "tone": "agreed"},
        "execution": {
            "state": "not_executed",
            "label": "未发送交易所请求",
            "detail": None,
        },
    }


def test_hold_update_decision_card_is_record_only():
    decision = RecognitionDecision(
        raw_message_id=1,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="非策略",
        authoritative_payload_json=json.dumps(
            {
                "reason": "消息是现有空单的持仓状态更新，不是新开仓。",
                "lifecycle_event": {
                    "event_type": "position_update",
                    "management_action": "hold_update",
                    "symbol": "BTC",
                    "side": "short",
                },
            },
            ensure_ascii=False,
        ),
        agreement_status="agreed",
        differences_json="[]",
        comparison_status="completed",
        disagreement_severity="none",
    )

    card = _build_message_decision_card(decision=decision, semantic_review=None)

    assert card is not None
    assert card["state"] == "record_only"
    assert card["state_label"] == "仅记录"
    assert card["recommended_action"] == "无需操作"


def test_complete_strategy_decision_card_is_identified_strategy():
    decision = RecognitionDecision(
        raw_message_id=1,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="是策略",
        authoritative_payload_json=json.dumps(
            {
                "lifecycle_event": {"event_type": "none"},
                "strategy": {
                    "symbol": "ETH",
                    "side": "long",
                    "entry": "1930-1910",
                    "stop_loss": "1890",
                    "take_profit": "1950-1970-1990",
                },
            }
        ),
        agreement_status="agreed",
        differences_json="[]",
        comparison_status="completed",
        disagreement_severity="none",
    )

    card = _build_message_decision_card(decision=decision, semantic_review=None)

    assert card is not None
    assert card["state"] == "strategy_identified"
    assert card["state_label"] == "策略已识别"
    assert card["recommended_action"] == "查看执行记录"


def test_agreed_targeted_lifecycle_event_is_linked_strategy():
    decision = RecognitionDecision(
        raw_message_id=1,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="非策略",
        authoritative_payload_json=json.dumps(
            {
                "lifecycle_event": {
                    "event_type": "entry_confirm",
                    "symbol": "BTC",
                    "side": "short",
                    "target_lifecycle_id": 548,
                }
            }
        ),
        agreement_status="agreed",
        differences_json="[]",
        comparison_status="completed",
        disagreement_severity="none",
    )

    card = _build_message_decision_card(decision=decision, semantic_review=None)

    assert card is not None
    assert card["state"] == "strategy_linked"
    assert card["state_label"] == "已关联策略"
    assert card["recommended_action"] == "查看执行记录"


def test_semantically_agreed_exit_with_execution_guard_is_linked_strategy():
    decision = RecognitionDecision(
        raw_message_id=1,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="识别失败",
        authoritative_payload_json=json.dumps(
            {
                "reason": "当前消息是在讨论已有空单的仓位管理，建议离场。",
                "lifecycle_event": {
                    "event_type": "exit_position",
                    "symbol": "BTC",
                    "side": "short",
                    "target_lifecycle_id": 548,
                },
            },
            ensure_ascii=False,
        ),
        agreement_status="disagreed",
        differences_json="[]",
        comparison_status="completed",
        disagreement_severity="critical",
        automation_status="skipped",
        automation_reason="mimo_authoritative_not_safely_applied",
    )
    semantic_review = {
        "status": "completed",
        "severity": "critical",
        "label": "严重分歧",
        "reason": "当前消息明确建议平仓已有空单，独立解读为全部退出，与mimo识别的exit_position一致，无实质分歧。",
        "conflict_types": ["execution_unresolved"],
        "model": "deepseek-v4-flash",
    }

    card = _build_message_decision_card(
        decision=decision, semantic_review=semantic_review
    )

    assert card is not None
    assert card["state"] == "strategy_linked"
    assert card["state_label"] == "已关联策略"
    assert card["recommended_action"] == "查看执行记录"
    assert card["agreement"] == {"label": "一致 · 查看执行记录", "tone": "agreed"}
    assert card["execution"] == {
        "state": "not_executed",
        "label": "自动执行未发出",
        "detail": "MiMo 生命周期事件未能安全落地",
    }


def test_load_group_messages_labels_context_only_target_disagreement(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=9, message_id=1, text="多单移动止损至开仓价")
        session.add(raw_message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="非策略",
                authoritative_payload_json=json.dumps(
                    {
                        "lifecycle_event": {
                            "event_type": "position_update",
                            "symbol": "BTC",
                            "side": "long",
                            "target_lifecycle_id": 504,
                            "management_action": "partial_then_break_even",
                        }
                    }
                ),
                agreement_status="disagreed",
                differences_json='["target_lifecycle_id", "symbol"]',
                comparison_status="completed",
                disagreement_severity="critical",
                comparison_model="deepseek-v4-flash",
                comparison_payload_json=json.dumps(
                    {
                        "reason": "当前消息未指定目标生命周期，独立判断无法确认504。",
                        "conflict_types": ["symbol", "target_lifecycle"],
                        "independent_action": {
                            "action_type": "position_update",
                            "symbol": None,
                            "side": "long",
                            "target_lifecycle_id": None,
                            "management_action": "partial_take_profit, move_stop_to_protect",
                            "stop_loss": None,
                            "take_profit": None,
                        },
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.commit()

    row = load_group_messages(session_factory, chat_id=9, limit=10)[0]

    assert row["semantic_review"] == {
        "status": "completed",
        "severity": "context",
        "label": "上下文待核对",
        "reason": "当前消息未指定目标生命周期，独立判断无法确认504。",
        "conflict_types": ["symbol", "target_lifecycle"],
        "model": "deepseek-v4-flash",
    }


@pytest.mark.parametrize("agreement_status", ["disagreed", "unknown", "agreed"])
def test_load_group_messages_marks_completed_legacy_review_without_severity_unclassified(
    tmp_path, agreement_status
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=9, message_id=1, text="legacy comparison")
        session.add(raw_message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="是策略",
                authoritative_payload_json="{}",
                agreement_status=agreement_status,
                differences_json='["side"]',
                comparison_status="completed",
                disagreement_severity=None,
                comparison_model="legacy-field-comparison",
                comparison_payload_json=json.dumps(
                    {
                        "reason": "legacy field comparison",
                        "raw_provider_response": "never-expose-legacy-provider-data",
                        "notification_claim_token": "never-expose-legacy-claim",
                    }
                ),
            )
        )
        session.commit()

    review = load_group_messages(session_factory, chat_id=9, limit=10)[0][
        "semantic_review"
    ]

    assert review == {
        "status": "completed",
        "severity": "unclassified",
        "label": "待重新复核",
        "reason": "历史记录没有语义分歧等级，需重新复核",
        "conflict_types": [],
        "model": "legacy-field-comparison",
    }


@pytest.mark.parametrize(
    ("agreement_status", "expected_severity", "expected_label"),
    [
        ("agreed", "agreed", "一致"),
        ("disagreed", "unclassified", "待重新复核"),
        ("unknown", "unclassified", "待重新复核"),
    ],
)
def test_load_group_messages_requires_agreement_for_completed_none_severity(
    tmp_path, agreement_status, expected_severity, expected_label
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=9, message_id=1, text="semantic comparison")
        session.add(raw_message)
        session.flush()
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message.id,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="是策略",
                authoritative_payload_json="{}",
                agreement_status=agreement_status,
                differences_json="[]",
                comparison_status="completed",
                disagreement_severity="none",
                comparison_model="deepseek-v4-flash",
                comparison_payload_json="{}",
            )
        )
        session.commit()

    review = load_group_messages(session_factory, chat_id=9, limit=10)[0][
        "semantic_review"
    ]

    assert review["severity"] == expected_severity
    assert review["label"] == expected_label
