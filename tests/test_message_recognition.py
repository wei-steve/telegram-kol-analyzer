from datetime import UTC, datetime

import httpx
import pytest

from telegram_kol_research import message_recognition as message_recognition_module
from telegram_kol_research import config as config_module
from telegram_kol_research.ai_recognition_config import AiProviderConfig, AiRecognitionConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_recognition import (
    _apply_lifecycle_event_decision,
    _authoritative_current_message_text,
    _chat_completions_url,
    _ensure_lifecycle_record,
    _exit_decision_looks_like_management_update,
    _load_lifecycle_event_context,
    _management_action_for_exit_downgrade,
    _parse_explicit_exit_signal,
    _result_from_ai_payload,
    _upsert_ai_signal_candidate,
    _validate_explicit_management_targets_in_session,
    apply_authoritative_mimo_payload,
    recognize_message_now,
)
from telegram_kol_research.message_instruction_items import (
    create_message_instruction_items_in_session,
)
from telegram_kol_research.models import (
    AiPromptInvocation,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    MediaAsset,
    MessageInstructionItem,
    ManagementMessageEnvelope,
    ManagementMessageTarget,
    MessageRecognition,
    RawMessage,
    RuntimeIncident,
    SignalCandidate,
    StrategyLifecycle,
    TradingSetting,
    TradeIdea,
)


def _mock_deepseek_lifecycle_event(monkeypatch, payload, *, seen_requests=None):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            if seen_requests is not None:
                seen_requests.append(json)
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": __import__("json").dumps(payload, ensure_ascii=False)
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("telegram_kol_research.message_recognition.httpx.Client", FakeClient)


def _add_exact_live_lifecycle(
    session,
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    side: str,
    verified_entry: bool = True,
):
    strategy_id = f"deepcoin:{chat_id}:{message_id}:{symbol}:{side}"
    binding = ExecutionBinding(
        strategy_instance_id=strategy_id,
        kol_id=f"group:{chat_id}",
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        venue="deepcoin",
        pos_id=f"pos-{symbol.lower()}",
        status="active",
    )
    session.add(binding)
    session.flush()
    lifecycle = StrategyLifecycle(
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side=side,
        lifecycle_status="entered",
        signal_at=datetime(2026, 7, 20, 12, tzinfo=UTC),
        entered_at=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        execution_binding_id=binding.id,
    )
    session.add(lifecycle)
    if verified_entry:
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id=strategy_id,
                leg_index=1,
                purpose="entry",
                order_kind="market",
                order_id=f"order-{symbol.lower()}",
                pos_id=f"pos-{symbol.lower()}",
                status="active",
                attribution_status="verified",
            )
        )
    session.flush()
    return lifecycle


def test_chat_completions_url_does_not_duplicate_v1_path():
    assert (
        _chat_completions_url("https://api.xiaomimimo.com/v1")
        == "https://api.xiaomimimo.com/v1/chat/completions"
    )


def test_exit_downgrade_treats_andy_add_on_breakeven_message_as_combined_management():
    action = _management_action_for_exit_downgrade(
        "回成本了，时间太久，注意保护成本，如果有在上面补仓的一定要现在平加仓，甚至还有微弱利润",
        {"management_action": ""},
    )

    assert action == "partial_take_profit, move_stop_to_protect"
    assert (
        _chat_completions_url("https://api.deepseek.com")
        == "https://api.deepseek.com/v1/chat/completions"
    )


def test_explicit_full_exit_is_not_downgraded_by_cost_price_reason() -> None:
    assert _exit_decision_looks_like_management_update(
        "",
        {
            "event_type": "exit_position",
            "management_action": "exit_full",
            "reason": "当前消息明确指示BTC空单成本价附近出局",
        },
    ) is False


def test_context_risk_reduction_marker_from_model_cannot_lower_threshold(tmp_path):
    session_factory = create_session_factory(tmp_path / "forged-context-marker.db")
    with session_factory() as session:
        raw = RawMessage(chat_id=88, message_id=4168, text="求稳就找机会出局")
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=4167,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 8, 3, 7, 50, tzinfo=UTC),
        )
        session.add_all([raw, lifecycle])
        session.commit()
        raw_id = raw.id
        lifecycle_id = lifecycle.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "exit_position",
                "target_lifecycle_id": lifecycle_id,
                "management_action": "exit_full",
                "confidence": 0.62,
                "_exact_context_risk_reduction_authorized": True,
            },
        },
        model="mimo-v2.5",
    )

    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        assert session.query(MessageInstructionItem).count() == 0


def test_exact_context_exit_rejects_conflicting_reply_target(tmp_path):
    session_factory = create_session_factory(tmp_path / "context-reply-conflict.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=88, message_id=4168, reply_to_message_id=4100,
            text="63100没站稳，求稳就找机会出局",
        )
        explicit = StrategyLifecycle(
            chat_id=88, message_id=4167, symbol="BTC", side="long",
            lifecycle_status="entered", signal_at=datetime(2026, 8, 3, 7, 50),
        )
        reply = StrategyLifecycle(
            chat_id=88, message_id=4100, symbol="BTC", side="long",
            lifecycle_status="entered", signal_at=datetime(2026, 8, 2, 7, 50),
        )
        session.add_all([raw, explicit, reply])
        session.flush()

        applied = _apply_lifecycle_event_decision(
            session,
            raw,
            {
                "event_type": "exit_position",
                "target_lifecycle_id": explicit.id,
                "symbol": "BTC",
                "side": "long",
                "management_action": "exit_full",
                "confidence": 0.62,
                "_exact_context_risk_reduction_authorized": True,
            },
            parse_source="mimo_authoritative",
            authoritative_generation="context-exact-exit",
        )

        assert applied is False
        assert session.query(SignalCandidate).count() == 0


def test_authoritative_current_message_text_excludes_model_reasons() -> None:
    assert _authoritative_current_message_text(
        "继续持有",
        {
            "reason": "建议全部出局",
            "input_reading": {"observed_text": "保护成本"},
            "lifecycle_event": {"reason": "全平"},
        },
    ) == "继续持有\n保护成本"


def test_normalize_management_intent_extracts_explicit_management_fraction():
    action, fraction = message_recognition_module.normalize_management_intent(
        {
            "event_type": "position_update",
            "management_action": "partial_take_profit",
        },
        "分批止盈30％！！！",
    )

    assert action == "partial_take_profit"
    assert fraction == pytest.approx(0.3)


def test_normalize_management_intent_converts_retained_fraction_to_close_fraction():
    action, fraction = message_recognition_module.normalize_management_intent(
        {
            "event_type": "position_update",
            "management_action": "partial_take_profit",
        },
        "先拿利润，其余可以保留40%底仓",
    )

    assert action == "partial_take_profit"
    assert fraction == pytest.approx(0.6)


def test_normalize_management_intent_rejects_conflicting_close_and_retained_fractions():
    with pytest.raises(ValueError, match="management_fraction_ambiguous"):
        message_recognition_module.normalize_management_intent(
            {
                "event_type": "position_update",
                "management_action": "partial_take_profit",
            },
            "止盈30%，但是保留40%底仓",
        )


def test_normalize_management_intent_defaults_unqualified_partial_to_half():
    action, fraction = message_recognition_module.normalize_management_intent(
        {
            "event_type": "position_update",
            "management_action": "partial_take_profit",
        },
        "现在先分批止盈",
    )

    assert action == "partial_take_profit"
    assert fraction == pytest.approx(0.5)


def test_position_update_persists_intent_without_mutating_confirmed_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=-1002337721508,
            message_id=9519,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 15, 13, 8, 42, tzinfo=UTC),
            entered_at=datetime(2026, 7, 15, 13, 9, 13, tzinfo=UTC),
            entry_price_actual=65550,
            stop_loss=67100,
            take_profit="63300/62100",
            management_signal_message_id=9519,
            management_action="entry_confirmed",
            management_note="confirmed exchange state",
        )
        raw_message = RawMessage(
            chat_id=-1002337721508,
            message_id=9527,
            posted_at=datetime(2026, 7, 16, 2, 53, 42, tzinfo=UTC),
            text="空单剩余仓位，做好成本保护，有变动我会在会员群通知。",
        )
        session.add_all([lifecycle, raw_message])
        session.flush()

        applied = _apply_lifecycle_event_decision(
            session,
            raw_message,
            {
                "event_type": "position_update",
                "target_lifecycle_id": lifecycle.id,
                "symbol": "BTC",
                "side": "short",
                "management_action": "move_stop_to_protect",
                "confidence": 0.9,
                "reason": "剩余仓位做好成本保护",
            },
            parse_source="mimo_authoritative",
            authoritative_generation="chen-9527",
        )
        session.flush()
        candidate = session.query(SignalCandidate).one()

        assert applied is True
        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.stop_loss == 67100
        assert lifecycle.take_profit == "63300/62100"
        assert lifecycle.management_signal_message_id == 9519
        assert lifecycle.management_action == "entry_confirmed"
        assert lifecycle.management_note == "confirmed exchange state"
        assert candidate.target_lifecycle_id == lifecycle.id
        assert candidate.management_action == "move_stop_to_break_even"
        assert candidate.stop_loss_text == "65550"
        assert candidate.take_profit_text == "63300/62100"


@pytest.mark.parametrize(
    ("text", "model_action", "expected_action", "expected_fraction"),
    [
        ("现在先分批止盈", None, "partial_take_profit", 0.5),
        (
            "提前止盈一半并移动止损至成本价",
            "partial_take_profit, move_stop_to_protect",
            "partial_then_break_even",
            0.5,
        ),
    ],
)
def test_applied_management_intent_persists_canonical_action(
    tmp_path,
    text,
    model_action,
    expected_action,
    expected_fraction,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2124,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 3, 6, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 3, 7, tzinfo=UTC),
            entry_price_actual=1705,
            stop_loss=1740,
        )
        raw_message = RawMessage(chat_id=88, message_id=2131, text=text)
        session.add_all([lifecycle, raw_message])
        session.flush()

        applied = _apply_lifecycle_event_decision(
            session,
            raw_message,
            {
                "event_type": "position_update",
                "target_lifecycle_id": lifecycle.id,
                "symbol": "ETH",
                "side": "short",
                "management_action": model_action,
                "confidence": 0.93,
                "reason": text,
            },
            parse_source="mimo_authoritative",
            authoritative_generation="generation-canonical",
        )
        session.flush()
        candidate = session.query(SignalCandidate).one()

        assert applied is True
        assert lifecycle.management_action is None
        assert candidate.management_action == expected_action
        if expected_fraction is None:
            assert candidate.management_fraction is None
        else:
            assert candidate.management_fraction == pytest.approx(expected_fraction)


def test_authoritative_position_update_persists_target_lifecycle_and_generation(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2124,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 3, 6, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 3, 7, tzinfo=UTC),
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=2131,
            posted_at=datetime(2026, 6, 19, 10, 12, tzinfo=UTC),
            text="空单分批止盈30%",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "position_update",
                "target_lifecycle_id": lifecycle_id,
                "symbol": "ETH",
                "side": "short",
                "management_action": "partial_take_profit",
                "confidence": 0.93,
                "reason": "空单分批止盈30%",
            },
        },
        model="mimo-v2.5",
        authoritative_generation="generation-claimed-42",
    )

    assert result.parse_source == "mimo_authoritative"
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        item = session.query(MessageInstructionItem).one()
        assert candidate.target_lifecycle_id == lifecycle_id
        assert candidate.management_action == "partial_take_profit"
        assert candidate.management_fraction == pytest.approx(0.3)
        assert candidate.recognition_generation == "generation-claimed-42"
        assert item.signal_candidate_id == candidate.id
        assert item.instruction_kind == "management"
        assert item.sequence == 0


def test_authoritative_position_update_accepts_explicit_eth_target_after_btc_comment(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3359,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 20, 19, 19, tzinfo=UTC),
            entered_at=datetime(2026, 7, 21, 5, 48, tzinfo=UTC),
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=3360,
            posted_at=datetime(2026, 7, 21, 6, 56, tzinfo=UTC),
            text=(
                "比特币可能突破了，ETH不知道会不会被拉着往上，所以ETH这单1940的"
                "用小仓位来做吧，平一半。比特币咱们会在6.7万空，ETH1950突破的话，"
                "就是2050空"
            ),
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "position_update",
                "target_lifecycle_id": lifecycle_id,
                "symbol": "ETH",
                "side": "short",
                "management_action": "partial_take_profit",
                "confidence": 0.9,
                "reason": "ETH 1940 空单平一半。",
            },
        },
        model="mimo-v2.5",
        authoritative_generation="mixed-symbol-management",
    )

    assert result.status == "非策略"
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        item = session.query(MessageInstructionItem).one()
        assert candidate.target_lifecycle_id == lifecycle_id
        assert candidate.symbol == "ETH"
        assert candidate.management_action == "partial_take_profit"
        assert candidate.management_fraction == pytest.approx(0.5)
        assert item.instruction_kind == "management"


def test_authoritative_prudent_exit_accepts_empty_targets_for_single_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=2132,
            posted_at=datetime(2026, 7, 22, 3, 15, tzinfo=UTC),
            text="空单综合成本65800，当前66000附近，求稳可走",
        )
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2124,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 21, 1, 45, tzinfo=UTC),
            entered_at=datetime(2026, 7, 21, 1, 46, tzinfo=UTC),
        )
        session.add_all([raw_message, lifecycle])
        session.flush()
        binding = ExecutionBinding(
            kol_id="group:88",
            chat_id=88,
            message_id=2124,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="pos-prudent-exit",
            status="active",
        )
        session.add(binding)
        session.flush()
        lifecycle.execution_binding_id = binding.id
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "exit_position",
                "target_lifecycle_id": lifecycle_id,
                "symbol": "BTC",
                "side": "short",
                "targets": [],
                "confidence": 0.9,
                "reason": "当前空单求稳可走",
            },
        },
        model="mimo-v2.5",
        authoritative_generation="prudent-exit-2132",
    )

    assert result.status == "非策略"
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        item = session.query(MessageInstructionItem).one()
    assert candidate.event_type == "close_signal"
    assert candidate.target_lifecycle_id == lifecycle_id
    assert candidate.management_action == "full_exit"
    assert item.instruction_kind == "management"


def test_authoritative_image_only_exit_uses_current_observed_text(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=4125,
            posted_at=datetime(2026, 8, 3, 4, 6, tzinfo=UTC),
            text="",
        )
        strategy_id = "deepcoin:88:4100:BTC:short"
        binding = ExecutionBinding(
            strategy_instance_id=strategy_id,
            kol_id="group:88",
            chat_id=88,
            message_id=4100,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            pos_id="pos-feiyang",
            status="active",
        )
        session.add_all([raw_message, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=4100,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 8, 3, 2, tzinfo=UTC),
            entered_at=datetime(2026, 8, 3, 2, 1, tzinfo=UTC),
            execution_binding_id=binding.id,
        )
        session.add_all(
            [
                lifecycle,
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=strategy_id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id="order-feiyang",
                    pos_id="pos-feiyang",
                    status="active",
                    attribution_status="verified",
                ),
            ]
        )
        session.flush()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id
        session.commit()

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "非策略",
            "input_reading": {
                "observed_text": "BTC空单，目前成本价附近，出局吧",
            },
            "lifecycle_event": {
                "event_type": "position_update",
                "target_lifecycle_id": lifecycle_id,
                "symbol": "BTC",
                "side": "short",
                "confidence": 0.95,
                "reason": "当前图片是对已有BTC空单的仓位管理",
            },
        },
        model="mimo-v2.5",
        authoritative_generation="image-only-full-exit-4125",
    )

    assert result.status == "非策略"
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        item = session.query(MessageInstructionItem).one()
    assert candidate.event_type == "close_signal"
    assert candidate.management_action == "full_exit"
    assert candidate.target_lifecycle_id == lifecycle_id
    assert item.instruction_kind == "management"
    assert item.signal_candidate_id == candidate.id


def test_authoritative_empty_targets_without_single_lifecycle_stays_fail_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=2133,
            text="求稳可走",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "exit_position",
                "targets": [],
                "confidence": 0.9,
                "reason": "没有可验证的唯一策略目标",
            },
        },
        model="mimo-v2.5",
    )

    assert result.status == "识别失败"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        assert session.query(MessageInstructionItem).count() == 0


def test_authoritative_multi_target_partial_take_profit_persists_one_candidate_per_strategy(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        btc = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3365, symbol="BTC", side="short"
        )
        eth = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3359, symbol="ETH", side="short"
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=3366,
            posted_at=datetime(2026, 7, 21, 18, 5, tzinfo=UTC),
            text="BTC和ETH的单子可以先止盈一半",
        )
        session.add_all([btc, eth, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        btc_id = btc.id
        eth_id = eth.id

    payload = {
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "position_update",
                "management_action": "partial_take_profit",
                "management_fraction": 0.5,
                "confidence": 0.95,
                "reason": "BTC 和 ETH 均止盈一半",
                "targets": [
                    {"target_lifecycle_id": btc_id, "symbol": "BTC", "side": "short"},
                    {"target_lifecycle_id": eth_id, "symbol": "ETH", "side": "short"},
                ],
            },
        }
    for _ in range(2):
        apply_authoritative_mimo_payload(
            session_factory,
            raw_message_id=raw_message_id,
            payload=payload,
            model="mimo-v2.5",
            authoritative_generation="multi-target-3366",
        )

    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter_by(raw_message_id=raw_message_id)
            .order_by(SignalCandidate.target_lifecycle_id)
            .all()
        )
        items = (
            session.query(MessageInstructionItem)
            .filter_by(raw_message_id=raw_message_id)
            .order_by(MessageInstructionItem.sequence)
            .all()
        )

    assert {
        (candidate.target_lifecycle_id, candidate.symbol, candidate.side, candidate.management_fraction)
        for candidate in candidates
    } == {
        (btc_id, "BTC", "short", 0.5),
        (eth_id, "ETH", "short", 0.5),
    }
    assert [item.instruction_kind for item in items] == ["management", "management"]


def test_authoritative_shadow_projection_records_targets_without_changing_work(
    tmp_path,
):
    assert hasattr(config_module, "MultiTargetManagementConfig")
    session_factory = create_session_factory(tmp_path / "multi-target-shadow.db")
    with session_factory() as session:
        btc = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3390, symbol="BTC", side="short"
        )
        eth = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3391, symbol="ETH", side="short"
        )
        raw = RawMessage(
            chat_id=88,
            message_id=3392,
            posted_at=datetime(2026, 7, 21, 18, 5, tzinfo=UTC),
            text="BTC ETH空单可以止盈一部分",
        )
        session.add(raw)
        session.flush()
        raw_id, btc_id, eth_id = raw.id, btc.id, eth.id
        session.commit()

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "position_update",
                "management_action": "partial_take_profit",
                "management_fraction": 0.5,
                "confidence": 0.95,
                "targets": [
                    {
                        "target_lifecycle_id": btc_id,
                        "symbol": "BTC",
                        "side": "short",
                    },
                    {
                        "target_lifecycle_id": eth_id,
                        "symbol": "ETH",
                        "side": "short",
                    },
                ],
            },
        },
        model="mimo-v2.5",
        authoritative_generation="multi-target-shadow-v1",
        multi_target_management_config=config_module.MultiTargetManagementConfig(
            projection_enabled=True,
            shadow_only=True,
        ),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        assert session.query(ManagementMessageEnvelope).count() == 1
        targets = (
            session.query(ManagementMessageTarget)
            .order_by(ManagementMessageTarget.target_ordinal)
            .all()
        )
        assert [target.target_lifecycle_id for target in targets] == [
            btc_id,
            eth_id,
        ]
        assert {target.admission_state for target in targets} == {"identified"}
        assert session.query(SignalCandidate).count() == 2
        assert session.query(MessageInstructionItem).count() == 2


def test_shadow_projection_failure_captures_committed_envelope_after_commit(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES",
        "unclassified_operation_failure",
    )
    session_factory = create_session_factory(tmp_path / "envelope-failure.db")
    with session_factory() as session:
        btc = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3400, symbol="BTC", side="short"
        )
        eth = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3401, symbol="ETH", side="short"
        )
        raw = RawMessage(chat_id=88, message_id=3402, text="BTC ETH partial")
        session.add(raw)
        session.flush()
        raw_id, btc_id, eth_id = raw.id, btc.id, eth.id
        session.commit()

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "position_update",
                "management_action": "partial_take_profit",
                "management_fraction": 0.5,
                "confidence": 0.95,
                "targets": [
                    {
                        "target_lifecycle_id": btc_id,
                        "symbol": "BTC",
                        "side": "short",
                    },
                    {
                        "target_lifecycle_id": eth_id,
                        "symbol": "ETH",
                    },
                ],
            },
        },
        model="mimo-v2.5",
        authoritative_generation="envelope-failure",
        multi_target_management_config=config_module.MultiTargetManagementConfig(
            projection_enabled=True,
            shadow_only=True,
        ),
    )

    with session_factory() as session:
        envelope = session.query(ManagementMessageEnvelope).one()
        incident = session.query(RuntimeIncident).one()
        assert incident.source_kind == "management_message_envelope"
        assert incident.source_record_id == str(envelope.id)
        assert incident.incident_type == "unclassified_operation_failure"


@pytest.mark.parametrize("management_action", ["exit_full", None])
def test_persistence_validator_accepts_context_approved_multi_target_full_exit(
    tmp_path,
    management_action,
):
    session_factory = create_session_factory(tmp_path / "multi-exit-policy.db")
    with session_factory() as session:
        btc = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3430, symbol="BTC", side="short"
        )
        eth = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3431, symbol="ETH", side="short"
        )
        raw = RawMessage(
            chat_id=88,
            message_id=3432,
            posted_at=datetime(2026, 7, 21, 18, 5, tzinfo=UTC),
            text="BTC ETH 空单全部平仓",
        )
        session.add(raw)
        session.flush()

        accepted = _validate_explicit_management_targets_in_session(
            session,
            raw_message=raw,
            instruction_text=raw.text,
            target_decisions=[
                {
                    "event_type": "exit_position",
                    "management_action": management_action,
                    "target_lifecycle_id": btc.id,
                    "symbol": "BTC",
                    "side": "short",
                },
                {
                    "event_type": "exit_position",
                    "management_action": management_action,
                    "target_lifecycle_id": eth.id,
                    "symbol": "ETH",
                    "side": "short",
                },
            ],
        )

        assert accepted is True


def test_authoritative_multi_target_full_exit_stays_dormant_without_live_allowlist(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "multi-exit-dormant.db")
    with session_factory() as session:
        btc = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3440, symbol="BTC", side="short"
        )
        eth = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3441, symbol="ETH", side="short"
        )
        raw = RawMessage(
            chat_id=88,
            message_id=3442,
            posted_at=datetime(2026, 7, 21, 18, 5, tzinfo=UTC),
            text="BTC ETH 空单全部平仓",
        )
        session.add(raw)
        session.flush()
        raw_id, btc_id, eth_id = raw.id, btc.id, eth.id
        session.commit()

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "exit_position",
                "management_action": "exit_full",
                "confidence": 0.95,
                "targets": [
                    {
                        "target_lifecycle_id": btc_id,
                        "symbol": "BTC",
                        "side": "short",
                    },
                    {
                        "target_lifecycle_id": eth_id,
                        "symbol": "ETH",
                        "side": "short",
                    },
                ],
            },
        },
        model="mimo-v2.5",
        authoritative_generation="multi-exit-dormant",
        multi_target_management_config=config_module.MultiTargetManagementConfig(),
    )

    assert result.status == "识别失败"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        assert session.query(MessageInstructionItem).count() == 0


def test_authoritative_multi_target_persistence_is_all_or_nothing(tmp_path):
    session_factory = create_session_factory(tmp_path / "multi-target-atomic.db")
    with session_factory() as session:
        btc = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3463, symbol="BTC", side="short"
        )
        eth = _add_exact_live_lifecycle(
            session,
            chat_id=88,
            message_id=3464,
            symbol="ETH",
            side="short",
            verified_entry=False,
        )
        raw = RawMessage(
            chat_id=88,
            message_id=3465,
            posted_at=datetime(2026, 7, 21, 18, 5, tzinfo=UTC),
            text="BTC ETH空单可以止盈一部分",
        )
        session.add(raw)
        session.flush()
        raw_id, btc_id, eth_id = raw.id, btc.id, eth.id
        session.commit()

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "position_update",
                "management_action": "partial_take_profit",
                "management_fraction": 0.5,
                "confidence": 0.95,
                "targets": [
                    {"target_lifecycle_id": btc_id, "symbol": "BTC", "side": "short"},
                    {"target_lifecycle_id": eth_id, "symbol": "ETH", "side": "short"},
                ],
            },
        },
        model="mimo-v2.5",
        authoritative_generation="multi-target-3465",
    )

    assert result.status == "识别失败"
    with session_factory() as session:
        assert session.query(SignalCandidate).filter_by(raw_message_id=raw_id).count() == 0
        assert session.query(MessageInstructionItem).filter_by(raw_message_id=raw_id).count() == 0


@pytest.mark.parametrize("target_order", [("BTC", "ETH"), ("ETH", "BTC")])
def test_live_multi_target_admission_refuses_one_target_and_continues_others(
    tmp_path,
    target_order,
    monkeypatch,
):
    monkeypatch.setenv(
        "TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES",
        "management_target_refused",
    )
    session_factory = create_session_factory(
        tmp_path / f"multi-target-isolated-{'-'.join(target_order)}.db"
    )
    with session_factory() as session:
        btc = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3463, symbol="BTC", side="short"
        )
        eth = _add_exact_live_lifecycle(
            session,
            chat_id=88,
            message_id=3464,
            symbol="ETH",
            side="short",
            verified_entry=False,
        )
        raw = RawMessage(
            chat_id=88,
            message_id=3465,
            posted_at=datetime(2026, 7, 21, 18, 5, tzinfo=UTC),
            text="BTC ETH空单可以止盈一部分",
        )
        session.add(raw)
        session.flush()
        raw_id = raw.id
        targets_by_symbol = {
            "BTC": {
                "target_lifecycle_id": btc.id,
                "symbol": "BTC",
                "side": "short",
            },
            "ETH": {
                "target_lifecycle_id": eth.id,
                "symbol": "ETH",
                "side": "short",
            },
        }
        session.commit()

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "position_update",
                "management_action": "partial_take_profit",
                "management_fraction": 0.5,
                "confidence": 0.95,
                "targets": [
                    targets_by_symbol[symbol] for symbol in target_order
                ],
            },
        },
        model="mimo-v2.5",
        authoritative_generation="multi-target-3465-isolated",
        multi_target_management_config=config_module.MultiTargetManagementConfig(
            projection_enabled=True,
            shadow_only=False,
            live_actions=frozenset({"partial_take_profit"}),
        ),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter_by(raw_message_id=raw_id)
            .all()
        )
        items = (
            session.query(MessageInstructionItem)
            .filter_by(raw_message_id=raw_id)
            .all()
        )
        target_rows = {
            target.symbol: target
            for target in session.query(ManagementMessageTarget)
            .filter_by(raw_message_id=raw_id)
            .all()
        }

        assert [(candidate.symbol, candidate.management_fraction) for candidate in candidates] == [
            ("BTC", 0.5)
        ]
        assert len(items) == 1
        assert target_rows["BTC"].admission_state == "admitted"
        assert target_rows["BTC"].closed_reason_code is None
        assert target_rows["BTC"].signal_candidate_id == candidates[0].id
        assert target_rows["BTC"].message_instruction_item_id == items[0].id
        assert target_rows["BTC"].execution_state == "pending"
        assert target_rows["ETH"].admission_state == "refused"
        assert target_rows["ETH"].closed_reason_code == "target_not_verified"
        assert target_rows["ETH"].signal_candidate_id is None
        assert target_rows["ETH"].message_instruction_item_id is None
        incident = session.query(RuntimeIncident).one()
        assert incident.source_kind == "management_message_target"
        assert incident.source_record_id == str(target_rows["ETH"].id)
        assert incident.incident_type == "management_target_refused"
        assert target_rows["ETH"].latest_runtime_incident_id == incident.id


def test_live_multi_target_admission_freezes_overlapping_pos_id_sets(tmp_path):
    session_factory = create_session_factory(tmp_path / "multi-target-collision.db")
    with session_factory() as session:
        btc = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3470, symbol="BTC", side="short"
        )
        eth = _add_exact_live_lifecycle(
            session, chat_id=88, message_id=3471, symbol="ETH", side="short"
        )
        btc_binding = session.get(ExecutionBinding, btc.execution_binding_id)
        eth_binding = session.get(ExecutionBinding, eth.execution_binding_id)
        btc_binding.pos_id = "pos-shared"
        eth_binding.pos_id = None
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=eth_binding.id,
                strategy_instance_id=eth_binding.strategy_instance_id,
                leg_index=2,
                purpose="entry",
                order_kind="market",
                order_id="order-eth-overlap",
                pos_id="pos-shared",
                status="active",
                attribution_status="verified",
            )
        )
        raw = RawMessage(
            chat_id=88,
            message_id=3472,
            posted_at=datetime(2026, 7, 21, 18, 5, tzinfo=UTC),
            text="BTC ETH空单可以止盈一部分",
        )
        session.add(raw)
        session.flush()
        raw_id, btc_id, eth_id = raw.id, btc.id, eth.id
        session.commit()

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "position_update",
                "management_action": "partial_take_profit",
                "management_fraction": 0.5,
                "confidence": 0.95,
                "targets": [
                    {
                        "target_lifecycle_id": btc_id,
                        "symbol": "BTC",
                        "side": "short",
                    },
                    {
                        "target_lifecycle_id": eth_id,
                        "symbol": "ETH",
                        "side": "short",
                    },
                ],
            },
        },
        model="mimo-v2.5",
        authoritative_generation="multi-target-collision",
        multi_target_management_config=config_module.MultiTargetManagementConfig(
            projection_enabled=True,
            shadow_only=False,
            live_actions=frozenset({"partial_take_profit"}),
        ),
    )

    assert result.status == "识别失败"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        assert session.query(MessageInstructionItem).count() == 0
        rows = session.query(ManagementMessageTarget).all()
        assert {row.closed_reason_code for row in rows} == {"target_collision"}
        assert len({row.collision_group_fingerprint for row in rows}) == 1


def test_multi_target_rejects_target_level_policy_overrides_before_persistence(tmp_path):
    session_factory = create_session_factory(tmp_path / "multi-target-override.db")
    with session_factory() as session:
        btc = _add_exact_live_lifecycle(session, chat_id=88, message_id=3463, symbol="BTC", side="short")
        eth = _add_exact_live_lifecycle(session, chat_id=88, message_id=3464, symbol="ETH", side="short")
        raw = RawMessage(chat_id=88, message_id=3465, text="BTC ETH空单可以止盈一部分")
        session.add(raw)
        session.flush()
        raw_id, btc_id, eth_id = raw.id, btc.id, eth.id
        session.commit()

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_id,
        payload={"recognition_result": "非策略", "lifecycle_event": {
            "event_type": "position_update", "management_action": "partial_take_profit",
            "confidence": 0.95, "targets": [
                {"target_lifecycle_id": btc_id, "symbol": "BTC", "side": "short"},
                {"target_lifecycle_id": eth_id, "symbol": "ETH", "side": "short", "confidence": 0.1},
            ],
        }},
        model="mimo-v2.5",
        authoritative_generation="hostile-target-override",
    )

    assert result.status == "识别失败"
    with session_factory() as session:
        assert session.query(SignalCandidate).filter_by(raw_message_id=raw_id).count() == 0


def test_authoritative_unscoped_break_even_does_not_guess_same_group_positions(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=9719,
            posted_at=datetime(2026, 7, 26, 4, 29, tzinfo=UTC),
            text="BTC多单浮盈700点左右，修改止损好成本保护，继续持有。",
        )
        session.add(raw_message)
        for message_id, pos_id in ((9654, "pos-1"), (9701, "pos-2")):
            strategy_id = f"deepcoin:88:{message_id}:BTC:long"
            binding = ExecutionBinding(
                strategy_instance_id=strategy_id,
                kol_id="group:88",
                chat_id=88,
                message_id=message_id,
                symbol="BTC",
                side="long",
                venue="deepcoin",
                pos_id=pos_id,
                status="active",
            )
            session.add(binding)
            session.flush()
            lifecycle = StrategyLifecycle(
                chat_id=88,
                message_id=message_id,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 25, 8, tzinfo=UTC),
                entered_at=datetime(2026, 7, 25, 8, 1, tzinfo=UTC),
                execution_binding_id=binding.id,
            )
            session.add(lifecycle)
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=strategy_id,
                    leg_index=1,
                    purpose="entry",
                    order_kind="market",
                    order_id=f"order-{pos_id}",
                    pos_id=pos_id,
                    status="active",
                    attribution_status="verified",
                )
            )
            session.flush()
        session.commit()
        raw_message_id = raw_message.id

    for _ in range(2):
        apply_authoritative_mimo_payload(
            session_factory,
            raw_message_id=raw_message_id,
            payload={
                "recognition_result": "非策略",
                "reason": "当前消息是对已有仓位的管理",
                "lifecycle_event": {
                    "event_type": "position_update",
                    "target_lifecycle_id": None,
                    "symbol": "BTC",
                    "side": "long",
                    "management_action": "move_stop_to_protect",
                    "confidence": 0.9,
                    "reason": "修改止损保护成本，但未指定具体策略",
                },
            },
            model="mimo-v2.5",
            authoritative_generation="group-break-even-9719",
        )

    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter_by(raw_message_id=raw_message_id)
            .order_by(SignalCandidate.target_lifecycle_id)
            .all()
        )
        items = (
            session.query(MessageInstructionItem)
            .filter_by(raw_message_id=raw_message_id)
            .order_by(MessageInstructionItem.sequence)
            .all()
        )

    assert candidates == []
    assert items == []


def test_authoritative_reply_target_wins_over_conflicting_model_target(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=9720,
            reply_to_message_id=9654,
            posted_at=datetime(2026, 7, 26, 4, 29, tzinfo=UTC),
            text="BTC多单修改止损到成本保护。",
        )
        session.add(raw_message)
        target_ids = []
        for message_id, pos_id in ((9654, "pos-1"), (9701, "pos-2")):
            strategy_id = f"deepcoin:88:{message_id}:BTC:long"
            binding = None
            if message_id != 9654:
                binding = ExecutionBinding(
                    strategy_instance_id=strategy_id,
                    kol_id="group:88",
                    chat_id=88,
                    message_id=message_id,
                    symbol="BTC",
                    side="long",
                    venue="deepcoin",
                    pos_id=pos_id,
                    status="active",
                )
                session.add(binding)
                session.flush()
            lifecycle = StrategyLifecycle(
                chat_id=88,
                message_id=message_id,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=datetime(2026, 7, 25, 8, tzinfo=UTC),
                entered_at=datetime(2026, 7, 25, 8, 1, tzinfo=UTC),
                execution_binding_id=(binding.id if binding is not None else None),
            )
            session.add(lifecycle)
            if binding is not None:
                session.add(
                    ExecutionOrderLeg(
                        execution_binding_id=binding.id,
                        strategy_instance_id=strategy_id,
                        leg_index=1,
                        purpose="entry",
                        order_kind="market",
                        order_id=f"order-{pos_id}",
                        pos_id=pos_id,
                        status="active",
                        attribution_status="verified",
                    )
                )
            session.flush()
            target_ids.append(lifecycle.id)
        session.commit()
        raw_message_id = raw_message.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "非策略",
            "reason": "回复管理已有仓位",
            "lifecycle_event": {
                "event_type": "position_update",
                "target_lifecycle_id": target_ids[1],
                "symbol": "BTC",
                "side": "long",
                "management_action": "move_stop_to_protect",
                "confidence": 0.9,
            },
        },
        model="mimo-v2.5",
        authoritative_generation="reply-wins-9720",
    )

    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter_by(raw_message_id=raw_message_id)
            .all()
        )

    assert [candidate.target_lifecycle_id for candidate in candidates] == [
        target_ids[0]
    ]


def test_authoritative_ambiguous_management_fraction_fails_closed(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=9721,
            posted_at=datetime(2026, 7, 26, 4, 30, tzinfo=UTC),
            text="BTC多单止盈30%，保留50%",
        )
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9654,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 25, 8, tzinfo=UTC),
            entered_at=datetime(2026, 7, 25, 8, 1, tzinfo=UTC),
        )
        session.add_all([raw_message, lifecycle])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "position_update",
                "target_lifecycle_id": lifecycle_id,
                "symbol": "BTC",
                "side": "long",
                "management_action": "partial_take_profit",
                "confidence": 0.95,
            },
        },
        model="mimo-v2.5",
        authoritative_generation="ambiguous-fraction-9721",
    )

    assert result.status == "识别失败"
    with session_factory() as session:
        assert (
            session.query(SignalCandidate)
            .filter_by(raw_message_id=raw_message_id)
            .count()
            == 0
        )


def test_authoritative_low_confidence_group_exit_fans_out_to_same_chat_btc_and_eth(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        btc = StrategyLifecycle(
            chat_id=88,
            message_id=4001,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 22, 1, tzinfo=UTC),
            entered_at=datetime(2026, 7, 22, 1, 1, tzinfo=UTC),
        )
        eth = StrategyLifecycle(
            chat_id=88,
            message_id=4002,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 22, 1, tzinfo=UTC),
            entered_at=datetime(2026, 7, 22, 1, 1, tzinfo=UTC),
        )
        elsewhere = StrategyLifecycle(
            chat_id=99,
            message_id=4003,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 22, 1, tzinfo=UTC),
            entered_at=datetime(2026, 7, 22, 1, 1, tzinfo=UTC),
        )
        unbound = StrategyLifecycle(
            chat_id=88,
            message_id=4005,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 22, 1, tzinfo=UTC),
            entered_at=datetime(2026, 7, 22, 1, 1, tzinfo=UTC),
        )
        exited = StrategyLifecycle(
            chat_id=88,
            message_id=4006,
            symbol="ETH",
            side="short",
            lifecycle_status="exited",
            signal_at=datetime(2026, 7, 22, 1, tzinfo=UTC),
            entered_at=datetime(2026, 7, 22, 1, 1, tzinfo=UTC),
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=4004,
            posted_at=datetime(2026, 7, 22, 6, 16, tzinfo=UTC),
            text="空单解套的人就可以先平加仓或者平仓等新机会",
        )
        session.add_all([btc, eth, elsewhere, unbound, exited, raw_message])
        session.add(
            TradingSetting(
                key="low_confidence_group_exit_cutoff",
                value_json='{"min_raw_message_id": 0}',
            )
        )
        session.flush()
        for lifecycle, pos_id in ((btc, "btc-short"), (eth, "eth-short")):
            binding = ExecutionBinding(
                kol_id=f"group:{lifecycle.chat_id}",
                chat_id=lifecycle.chat_id,
                message_id=lifecycle.message_id,
                symbol=lifecycle.symbol,
                side=lifecycle.side,
                venue="deepcoin",
                pos_id=pos_id,
                status="active",
            )
            session.add(binding)
            session.flush()
            lifecycle.execution_binding_id = binding.id
        session.commit()
        raw_message_id = raw_message.id
        btc_id = btc.id
        eth_id = eth.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "非策略",
            "lifecycle_event": {
                "event_type": "exit_position",
                "target_lifecycle_id": btc_id,
                "symbol": "BTC",
                "side": "short",
                "confidence": 0.95,
            },
        },
        model="mimo-v2.5",
        authoritative_generation="low-confidence-group-exit",
    )

    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter_by(raw_message_id=raw_message_id)
            .order_by(SignalCandidate.target_lifecycle_id)
            .all()
        )

    assert {
        (
            candidate.target_lifecycle_id,
            candidate.symbol,
            candidate.side,
            candidate.management_action,
            candidate.management_fraction,
        )
        for candidate in candidates
    } == {
        (btc_id, "BTC", "short", "partial_take_profit", 0.5),
        (eth_id, "ETH", "short", "partial_take_profit", 0.5),
    }


def test_low_confidence_group_exit_scope_requires_direction_and_honors_symbol():
    assert message_recognition_module._low_confidence_group_exit_scope(
        "BTC 空单求稳可以先平仓"
    ) == ("short", {"BTC"})
    assert message_recognition_module._low_confidence_group_exit_scope(
        "BTC 空单求稳可走"
    ) == ("short", {"BTC"})
    assert message_recognition_module._low_confidence_group_exit_scope(
        "求稳可以先平仓"
    ) is None
    assert message_recognition_module._low_confidence_group_exit_scope(
        "BTC 空单继续持有"
    ) is None


def test_dual_candidate_recognition_preserves_entry_and_management_items(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        old_lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2124,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 3, 6, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 3, 7, tzinfo=UTC),
            entry_price_actual=1705,
            stop_loss=1740,
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=2131,
            posted_at=datetime(2026, 6, 19, 10, 12, tzinfo=UTC),
            text="ETH 旧空单先分批止盈30%，同时新多单 1680 入场，止损 1650，止盈 1730。",
        )
        session.add_all([old_lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        old_lifecycle_id = old_lifecycle.id

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "是策略",
            "reason": "包含旧策略仓位管理和独立新策略",
            "lifecycle_event": {
                "event_type": "position_update",
                "target_lifecycle_id": old_lifecycle_id,
                "symbol": "ETH",
                "side": "short",
                "management_action": "partial_take_profit",
                "management_fraction": 0.3,
                "confidence": 0.96,
                "reason": "旧空单分批止盈30%",
            },
            "strategy": {
                "symbol": "ETH",
                "side": "long",
                "entry": "1680",
                "stop_loss": "1650",
                "take_profit": "1730",
                "order_type": "market",
            },
            "confidence": 0.94,
        },
        model="mimo-v2.5",
        authoritative_generation="generation-dual-1",
    )

    assert result.status == "是策略"
    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter_by(raw_message_id=raw_message_id)
            .order_by(SignalCandidate.id)
            .all()
        )
        items = (
            session.query(MessageInstructionItem)
            .filter_by(raw_message_id=raw_message_id)
            .order_by(MessageInstructionItem.sequence)
            .all()
        )

    assert {(row.event_type, row.target_lifecycle_id) for row in candidates} == {
        ("position_update", old_lifecycle_id),
        ("entry_signal", None),
    }
    assert [(item.instruction_kind, item.sequence) for item in items] == [
        ("management", 0),
        ("entry", 1),
    ]


def test_dabiaoke_4206_projects_cancel_and_independent_long_from_instructions(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "dabiaoke-4206.db")
    with session_factory() as session:
        old_lifecycle = StrategyLifecycle(
            chat_id=-1003048800035,
            message_id=4205,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 8, 8, 5, 35, tzinfo=UTC),
            entry_range_low=65600,
            entry_range_high=66200,
            stop_loss=66700,
            take_profit="64900/64200/63500",
        )
        raw_message = RawMessage(
            chat_id=-1003048800035,
            message_id=4206,
            posted_at=datetime(2026, 8, 9, 0, 48, tzinfo=UTC),
            text=(
                "撤，不挂了，没挂到\nBTC\n方向：多\n建仓：64700-63800\n"
                "止损：63400\n止盈：65400-66100-66800"
            ),
        )
        session.add_all([old_lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        old_lifecycle_id = old_lifecycle.id

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "instructions": [
                {
                    "kind": "cancel_pending_entry",
                    "confidence": 0.95,
                    "reason": "撤销未成交旧空单",
                    "target": {"lifecycle_id": old_lifecycle_id},
                },
                {
                    "kind": "entry",
                    "confidence": 0.95,
                    "reason": "独立的新多单",
                    "strategy": {
                        "symbol": "BTC",
                        "side": "long",
                        "entry": "64700-63800",
                        "stop_loss": "63400",
                        "take_profit": "65400-66100-66800",
                    },
                },
            ],
            "recognition_result": "非策略",
            "reason": "消息级兼容视图只保留管理动作",
            "strategy": {},
            "lifecycle_event": {
                "event_type": "cancel_entry",
                "target_lifecycle_id": old_lifecycle_id,
                "management_action": "cancel_pending_entry",
                "confidence": 0.95,
                "reason": "撤销未成交旧空单",
            },
            "confidence": 0.95,
        },
        model="mimo-v2.5",
        authoritative_generation="dabiaoke-4206",
    )

    assert result.status == "是策略"
    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter_by(raw_message_id=raw_message_id)
            .order_by(SignalCandidate.id)
            .all()
        )
        items = (
            session.query(MessageInstructionItem)
            .filter_by(raw_message_id=raw_message_id)
            .order_by(MessageInstructionItem.sequence)
            .all()
        )

    assert [
        (
            row.event_type,
            row.management_action,
            row.target_lifecycle_id,
            row.side,
        )
        for row in candidates
    ] == [
        ("close_signal", "cancel_pending_entry", old_lifecycle_id, "short"),
        ("entry_signal", None, None, "long"),
    ]
    assert [(item.sequence, item.instruction_kind) for item in items] == [
        (0, "management"),
        (1, "entry"),
    ]


def test_authoritative_rerecognition_retires_superseded_pending_item(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        old_lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2124,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 3, 6, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 3, 7, tzinfo=UTC),
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=2131,
            posted_at=datetime(2026, 6, 19, 10, 12, tzinfo=UTC),
            text="ETH 新多单 1680 入场，止损 1650，止盈 1730。",
        )
        session.add_all([old_lifecycle, raw_message])
        session.flush()
        management_candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="ETH",
            side="short",
            event_type="position_update",
            target_lifecycle_id=old_lifecycle.id,
            management_action="partial_take_profit",
            parse_source="mimo_authoritative",
            confidence=0.96,
        )
        entry_candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            entry_text="1680",
            stop_loss_text="1650",
            take_profit_text="1730",
            parse_source="mimo_authoritative",
            confidence=0.94,
        )
        session.add_all([management_candidate, entry_candidate])
        session.flush()
        create_message_instruction_items_in_session(
            session,
            raw_message_id=raw_message.id,
        )
        session.commit()
        raw_message_id = raw_message.id
        management_candidate_id = management_candidate.id
        entry_candidate_id = entry_candidate.id

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "是策略",
            "reason": "只包含新策略",
            "lifecycle_event": {"event_type": "none", "confidence": 0.0},
            "strategy": {
                "symbol": "ETH",
                "side": "long",
                "entry": "1680",
                "stop_loss": "1650",
                "take_profit": "1730",
                "order_type": "market",
            },
            "confidence": 0.94,
        },
        model="mimo-v2.5",
        authoritative_generation="generation-entry-only-2",
    )

    with session_factory() as session:
        items = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.retired_at.is_(None))
            .all()
        )
        management_candidate = session.get(SignalCandidate, management_candidate_id)
        entry_candidate = session.get(SignalCandidate, entry_candidate_id)

    assert [
        (item.signal_candidate_id, item.instruction_kind, item.sequence)
        for item in items
    ] == [
        (entry_candidate_id, "entry", 0),
    ]
    assert management_candidate.parse_source == "mimo_authoritative"
    assert entry_candidate.parse_source == "mimo_authoritative"


def test_authoritative_rerecognition_never_mutates_item_linked_candidate_semantics(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "immutable-candidate.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=9001,
            text="new authoritative interpretation",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    first_payload = {
        "recognition_result": "是策略",
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "strategy": {
            "symbol": "ETH",
            "side": "long",
            "entry": "1680",
            "stop_loss": "1650",
            "take_profit": "1730",
            "leverage": "5x",
        },
        "confidence": 0.94,
    }
    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=first_payload,
        model="mimo-v2.5",
        authoritative_generation="generation-1",
    )
    with session_factory() as session:
        old_candidate = session.query(SignalCandidate).one()
        old_item = session.query(MessageInstructionItem).one()
        old_item.status = "submitted"
        old_item.result_json = '{"status":"submitted"}'
        old_candidate_id = old_candidate.id
        old_item_id = old_item.id
        old_semantics = (
            old_candidate.symbol,
            old_candidate.side,
            old_candidate.entry_text,
            old_candidate.stop_loss_text,
            old_candidate.take_profit_text,
            old_candidate.leverage_text,
            old_candidate.confidence,
        )
        session.commit()

    changed_payload = {
        "recognition_result": "是策略",
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "strategy": {
            "symbol": "BTC",
            "side": "short",
            "entry": "68100",
            "stop_loss": "68800",
            "take_profit": "67000",
            "leverage": "10x",
        },
        "confidence": 0.97,
    }
    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=changed_payload,
        model="mimo-v2.5",
        authoritative_generation="generation-2",
    )

    with session_factory() as session:
        old_candidate = session.get(SignalCandidate, old_candidate_id)
        old_item = session.get(MessageInstructionItem, old_item_id)
        active_item = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.retired_at.is_(None))
            .filter(MessageInstructionItem.status == "pending")
            .one()
        )
        new_candidate = session.get(
            SignalCandidate,
            active_item.signal_candidate_id,
        )

    assert old_candidate is not None
    assert (
        old_candidate.symbol,
        old_candidate.side,
        old_candidate.entry_text,
        old_candidate.stop_loss_text,
        old_candidate.take_profit_text,
        old_candidate.leverage_text,
        old_candidate.confidence,
    ) == old_semantics
    assert old_item is not None and old_item.retired_at is None
    assert active_item.id != old_item_id
    assert new_candidate is not None
    assert (new_candidate.symbol, new_candidate.side) == ("BTC", "short")


def test_identical_authoritative_rerecognition_reuses_durable_identity(tmp_path):
    session_factory = create_session_factory(tmp_path / "stable-candidate.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=9002, text="ETH long")
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    payload = {
        "recognition_result": "是策略",
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "strategy": {
            "symbol": "ETH",
            "side": "long",
            "entry": "1680",
            "stop_loss": "1650",
            "take_profit": "1730",
        },
        "confidence": 0.94,
    }
    for generation in ("generation-1", "generation-2"):
        apply_authoritative_mimo_payload(
            session_factory,
            raw_message_id=raw_message_id,
            payload=payload,
            model="mimo-v2.5",
            authoritative_generation=generation,
        )

    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 1
        assert session.query(MessageInstructionItem).count() == 1
        assert session.query(MessageInstructionItem).one().retired_at is None


def test_entry_upsert_tolerates_duplicate_role_rows_without_overwriting_management(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=2131, text="ETH 新多单")
        session.add(raw_message)
        session.flush()
        first_entry = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_superseded",
            confidence=0.8,
        )
        duplicate_entry = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="ETH",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_superseded",
            confidence=0.7,
        )
        management = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="ETH",
            side="short",
            event_type="position_update",
            management_action="partial_take_profit",
            parse_source="mimo_authoritative",
            confidence=0.96,
        )
        session.add_all([first_entry, duplicate_entry, management])
        session.flush()

        candidate = _upsert_ai_signal_candidate(
            session,
            raw_message,
            strategy={
                "symbol": "ETH",
                "side": "long",
                "entry": "1680",
                "stop_loss": "1650",
                "take_profit": "1730",
            },
            confidence=0.94,
            parse_source="mimo_authoritative",
        )

        assert candidate.id == first_entry.id
        assert candidate.entry_text == "1680"
        assert duplicate_entry.parse_source == "mimo_superseded"
        assert management.event_type == "position_update"
        assert management.management_action == "partial_take_profit"


def test_partial_dual_acceptance_keeps_management_actionable(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=7000,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 20, 8, 30, tzinfo=UTC),
            entered_at=datetime(2026, 7, 20, 8, 31, tzinfo=UTC),
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=7001,
            posted_at=datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
            text=(
                "BTC 旧空单分批止盈30%，同时新空单进场 1840-1860，"
                "止损 1905，止盈 1780/1720"
            ),
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        lifecycle_id = lifecycle.id
        raw_message_id = raw_message.id

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload={
            "recognition_result": "是策略",
            "reason": "旧仓位管理可执行，新策略价格尺度异常",
            "lifecycle_event": {
                "event_type": "position_update",
                "target_lifecycle_id": lifecycle_id,
                "symbol": "BTC",
                "side": "short",
                "management_action": "partial_take_profit",
                "management_fraction": 0.3,
                "confidence": 0.96,
                "reason": "BTC 旧空单分批止盈30%",
            },
            "strategy": {
                "symbol": "BTC",
                "side": "short",
                "entry": "1840-1860",
                "stop_loss": "1905",
                "take_profit": "1780/1720",
                "order_type": "limit",
            },
            "confidence": 0.92,
        },
        model="mimo-v2.5",
        authoritative_generation="generation-partial-dual-1",
    )

    with session_factory() as session:
        candidates = session.query(SignalCandidate).all()
        items = session.query(MessageInstructionItem).all()

    assert result.status == "非策略"
    assert "symbol_price_scale_conflict" in (result.reason or "")
    assert [(item.instruction_kind, item.sequence) for item in items] == [
        ("management", 0),
    ]
    assert any(
        candidate.event_type == "position_update"
        and candidate.parse_source == "mimo_authoritative"
        for candidate in candidates
    )
    assert any(
        candidate.event_type == "entry_signal"
        and candidate.parse_source == "mimo_symbol_review"
        and candidate.review_status == "needs_review"
        for candidate in candidates
    )


def test_recognize_message_now_persists_text_strategy_candidate(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=1,
            posted_at=datetime(2026, 6, 14, tzinfo=UTC),
            text="BTC long 68000-68200 SL 67500 TP 69000/70000 20x",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "是策略"
    assert "BTC long" in result.summary
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        recognition = session.query(MessageRecognition).one()
    assert candidate.symbol == "BTC"
    assert candidate.side == "long"
    assert candidate.entry_text == "68000-68200"
    assert recognition.status == "是策略"


def test_ai_strategy_payload_normalizes_targets_and_backfills_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    payload = {
        "recognition_result": "是策略",
        "reason": "has entry, stop loss and take profits",
        "strategy": {
            "symbol": "btc",
            "side": "long",
            "entry": "Entry: 62400 nearby",
            "stop_loss": "SL: 60800",
            "take_profit": ["TP: 63600", "64800"],
        },
        "confidence": 0.91,
    }

    result = _result_from_ai_payload(
        raw_message_id=1,
        payload=payload,
        parse_source="text_ai",
    )

    assert "BTC long" in (result.summary or "")
    assert "Entry 62400nearby" in (result.summary or "")
    assert "TP 63600/64800" in (result.summary or "")

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=9,
            posted_at=datetime(2026, 6, 14, tzinfo=UTC),
            text="BTC long Entry 62400 nearby SL 60800 TP 63600/64800",
        )
        session.add(raw_message)
        session.flush()
        candidate = _upsert_ai_signal_candidate(
            session,
            raw_message,
            strategy=payload["strategy"],
            confidence=0.91,
            parse_source="text_ai",
        )
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=raw_message.chat_id,
            message_id=raw_message.message_id,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=raw_message.posted_at,
        )
        session.add(lifecycle)
        session.flush()

        _ensure_lifecycle_record(session, raw_message, candidate)

        assert candidate.symbol == "BTC"
        assert candidate.side == "long"
        assert candidate.entry_text == "62400nearby"
        assert candidate.stop_loss_text == "60800"
        assert candidate.take_profit_text == "63600/64800"
        assert lifecycle.entry_range_low == 62400
        assert lifecycle.entry_range_high == 62400
        assert lifecycle.stop_loss == 60800
        assert lifecycle.take_profit == "63600/64800"


def test_ai_text_recognition_preserves_labeled_entry_price_when_model_returns_market_only(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    source_text = (
        "\U0001f3c6 \u300e1000u\u51b2\u523a100w\u5343\u500d\u7ffb\u4ed3\u300f \U0001f3c6\n"
        "\u4ea4\u6613\u6807\u7684\uff1aEth(\u5e02\u4ef7\u8fdb\u573a)\n"
        "\u8fdb\u573a\u65b9\u5411\uff1a\u7a7a\n"
        "\u8fdb\u573a\u70b9\u4f4d\uff1a1730\u9644\u8fd1\n"
        "\u6b62\u76c8\u9884\u8ba1\uff1a1650\n"
        "\u6b62\u635f\u9884\u8ba1\uff1a1765"
    )
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=2167,
            posted_at=datetime(2026, 6, 22, 11, 2, tzinfo=UTC),
            text=source_text,
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    payload = {
        "recognition_result": "\u662f\u7b56\u7565",
        "reason": "\u660e\u786e\u4ea4\u6613\u6807\u7684ETH\u3001\u505a\u7a7a\u65b9\u5411\u3001\u5e02\u4ef7\u8fdb\u573a\u3001\u6b62\u635f1765\u3001\u6b62\u76c81650",
        "strategy": {
            "symbol": "ETH",
            "side": "short",
            "entry": "\u5e02\u4ef7\u8fdb\u573a",
            "stop_loss": "1765",
            "take_profit": "1650",
            "leverage": None,
            "order_type": "market",
        },
        "confidence": 0.95,
    }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": __import__("json").dumps(payload, ensure_ascii=False)
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("telegram_kol_research.message_recognition.httpx.Client", FakeClient)

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    expected_entry = "\u5e02\u4ef7\u8fdb\u573a/1730\u9644\u8fd1"
    assert result.status == "\u662f\u7b56\u7565"
    assert f"Entry {expected_entry}" in (result.summary or "")
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        lifecycle = session.query(StrategyLifecycle).one()
        recognition = session.query(MessageRecognition).one()
        invocation = session.query(AiPromptInvocation).one()

    assert candidate.entry_text == expected_entry
    assert lifecycle.entry_range_low == 1730
    assert lifecycle.entry_range_high == 1730
    assert f"Entry {expected_entry}" in (recognition.summary or "")
    assert invocation.feature == "message_recognition"


def test_ensure_lifecycle_record_deduplicates_recent_active_same_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_payload = {
        "symbol": "ETH",
        "side": "short",
        "entry": "1710/1788",
        "stop_loss": "1850",
        "take_profit": "第一止盈1673（70%仓位），第二止盈1618",
    }
    duplicate_payload = {
        "symbol": "ETH",
        "side": "short",
        "entry": "1710-1788",
        "stop_loss": "1850",
        "take_profit": "1673/1618",
    }

    with session_factory() as session:
        first_message = RawMessage(
            chat_id=88,
            message_id=12918,
            posted_at=datetime(2026, 6, 19, 1, 30, 50, tzinfo=UTC),
            text="ETH 1710市价直接空 再挂1788 止盈1673/1618 止损1850",
        )
        duplicate_message = RawMessage(
            chat_id=88,
            message_id=12924,
            posted_at=datetime(2026, 6, 19, 13, 46, 42, tzinfo=UTC),
            text="ETH 1710市价直接空 再挂1788 止盈1673/1618 止损1850",
        )
        session.add_all([first_message, duplicate_message])
        session.flush()
        first_candidate = _upsert_ai_signal_candidate(
            session,
            first_message,
            strategy=first_payload,
            confidence=0.95,
            parse_source="text_ai",
        )
        first_lifecycle = _ensure_lifecycle_record(session, first_message, first_candidate)
        first_lifecycle.lifecycle_status = "entered"
        first_lifecycle.entered_at = first_message.posted_at
        first_lifecycle.entry_price_actual = 1710
        duplicate_candidate = _upsert_ai_signal_candidate(
            session,
            duplicate_message,
            strategy=duplicate_payload,
            confidence=0.95,
            parse_source="text_ai",
        )
        duplicate_lifecycle = _ensure_lifecycle_record(
            session,
            duplicate_message,
            duplicate_candidate,
        )
        session.flush()

        assert duplicate_lifecycle.id == first_lifecycle.id
        assert session.query(StrategyLifecycle).count() == 1
        assert duplicate_candidate.event_type == "duplicate_entry_signal"
        assert "Duplicate active strategy lifecycle" in duplicate_candidate.review_note


def test_ensure_lifecycle_record_applies_active_entry_correction(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    first_payload = {
        "symbol": "BTC",
        "side": "short",
        "entry": "64600-69000",
        "stop_loss": "66100",
        "take_profit": "62300/61200",
    }
    correction_payload = {
        "symbol": "BTC",
        "side": "short",
        "entry": "64600-64900",
        "stop_loss": "66100",
        "take_profit": "62300/61200",
    }

    with session_factory() as session:
        first_message = RawMessage(
            chat_id=88,
            message_id=9079,
            posted_at=datetime(2026, 6, 22, 11, 57, 47, tzinfo=UTC),
            text="BTC 64600-69000附近做空 止损66100 止盈62300/61200",
        )
        correction_message = RawMessage(
            chat_id=88,
            message_id=9080,
            posted_at=datetime(2026, 6, 22, 12, 18, 46, tzinfo=UTC),
            text="BTC 64600-64900附近做空 止损66100 止盈62300/61200",
        )
        session.add_all([first_message, correction_message])
        session.flush()
        first_candidate = _upsert_ai_signal_candidate(
            session,
            first_message,
            strategy=first_payload,
            confidence=0.95,
            parse_source="glm_ocr_image",
        )
        first_lifecycle = _ensure_lifecycle_record(session, first_message, first_candidate)
        first_lifecycle.lifecycle_status = "entered"
        first_lifecycle.entered_at = first_message.posted_at
        correction_candidate = _upsert_ai_signal_candidate(
            session,
            correction_message,
            strategy=correction_payload,
            confidence=0.95,
            parse_source="glm_ocr_image",
        )
        correction_lifecycle = _ensure_lifecycle_record(
            session,
            correction_message,
            correction_candidate,
        )
        session.flush()

        assert correction_lifecycle.id == first_lifecycle.id
        assert session.query(StrategyLifecycle).count() == 1
        assert first_lifecycle.entry_range_low == 64600
        assert first_lifecycle.entry_range_high == 64900
        assert first_lifecycle.management_signal_message_id == 9080
        assert first_lifecycle.management_action == "strategy_correction"
        assert "64600-69000" in (first_lifecycle.management_note or "")
        assert "64600-64900" in (first_lifecycle.management_note or "")
        assert correction_candidate.event_type == "strategy_correction"
        assert "Strategy correction" in (correction_candidate.review_note or "")


def test_ensure_lifecycle_record_allows_reentry_after_exit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    payload = {
        "symbol": "BTC",
        "side": "short",
        "entry": "64900",
        "stop_loss": "66100",
        "take_profit": "62300/61200",
    }

    with session_factory() as session:
        first_message = RawMessage(
            chat_id=88,
            message_id=9080,
            posted_at=datetime(2026, 6, 22, 12, 18, 46, tzinfo=UTC),
            text="BTC 64900做空 止损66100 止盈62300/61200",
        )
        reentry_message = RawMessage(
            chat_id=88,
            message_id=9090,
            posted_at=datetime(2026, 6, 22, 15, 0, tzinfo=UTC),
            text="价格又反弹到64900 继续空 止损66100 止盈62300/61200",
        )
        session.add_all([first_message, reentry_message])
        session.flush()
        first_candidate = _upsert_ai_signal_candidate(
            session,
            first_message,
            strategy=payload,
            confidence=0.95,
            parse_source="text_ai",
        )
        first_lifecycle = _ensure_lifecycle_record(session, first_message, first_candidate)
        first_lifecycle.lifecycle_status = "exited"
        first_lifecycle.exit_reason = "take_profit"
        first_lifecycle.entered_at = first_message.posted_at
        first_lifecycle.exited_at = datetime(2026, 6, 22, 13, 0, tzinfo=UTC)
        session.flush()
        reentry_candidate = _upsert_ai_signal_candidate(
            session,
            reentry_message,
            strategy=payload,
            confidence=0.95,
            parse_source="text_ai",
        )
        reentry_lifecycle = _ensure_lifecycle_record(
            session,
            reentry_message,
            reentry_candidate,
        )
        session.flush()

        assert reentry_lifecycle.id != first_lifecycle.id
        assert session.query(StrategyLifecycle).count() == 2
        assert reentry_lifecycle.lifecycle_status == "pending_entry"
        assert reentry_candidate.event_type == "entry_signal"
        assert reentry_candidate.review_note is None


def test_ensure_lifecycle_record_keeps_distinct_active_entry_range(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with session_factory() as session:
        first_message = RawMessage(
            chat_id=88,
            message_id=9100,
            posted_at=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
            text="BTC 64600-64900做空 止损66100 止盈62300/61200",
        )
        second_message = RawMessage(
            chat_id=88,
            message_id=9101,
            posted_at=datetime(2026, 6, 22, 12, 30, tzinfo=UTC),
            text="BTC 65000-65300做空 止损66100 止盈62300/61200",
        )
        session.add_all([first_message, second_message])
        session.flush()
        first_candidate = _upsert_ai_signal_candidate(
            session,
            first_message,
            strategy={
                "symbol": "BTC",
                "side": "short",
                "entry": "64600-64900",
                "stop_loss": "66100",
                "take_profit": "62300/61200",
            },
            confidence=0.95,
            parse_source="text_ai",
        )
        first_lifecycle = _ensure_lifecycle_record(session, first_message, first_candidate)
        first_lifecycle.lifecycle_status = "entered"
        first_lifecycle.entered_at = first_message.posted_at
        second_candidate = _upsert_ai_signal_candidate(
            session,
            second_message,
            strategy={
                "symbol": "BTC",
                "side": "short",
                "entry": "65000-65300",
                "stop_loss": "66100",
                "take_profit": "62300/61200",
            },
            confidence=0.95,
            parse_source="text_ai",
        )
        second_lifecycle = _ensure_lifecycle_record(
            session,
            second_message,
            second_candidate,
        )
        session.flush()

        assert second_lifecycle.id != first_lifecycle.id
        assert session.query(StrategyLifecycle).count() == 2
        assert second_candidate.event_type == "entry_signal"
        assert second_candidate.review_note is None


def test_recognize_message_now_marks_plain_text_as_not_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=2, text="今天市场波动很大")
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),  # local rule parser
    )

    assert result.status == "非策略"
    assert result.summary is None
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        recognition = session.query(MessageRecognition).one()
    assert recognition.status == "非策略"
    assert recognition.reason == "未识别到可执行新入场策略"


def test_recognize_message_now_rejects_single_direction_hint(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=6, text="多单继续持有，注意风险")
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0


def test_recognize_message_now_rejects_position_management_update(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=7,
            text=(
                "🔥注意，现目前多单略微浮亏中…\n"
                "🔥多单继续持有，设置好止损点！\n"
                "@Tarderfengge QQ:158241758"
            ),
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),  # local rule parser
    )

    assert result.status == "非策略"
    assert result.reason == "未识别到可执行新入场策略"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0


def test_bitcoin_junzhang_profile_recognizes_market_short_with_stop_loss(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=-1002282384698,
            message_id=5382,
            posted_at=datetime(2026, 6, 23, 7, 19, 13, tzinfo=UTC),
            text="BTC现价开一层空，止损65200 @Tarderfengge QQ:158241758",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.parse_source == "junzhang_profile"
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        lifecycle = session.query(StrategyLifecycle).one()

    assert candidate.symbol == "BTC"
    assert candidate.side == "short"
    assert candidate.event_type == "entry_signal"
    assert candidate.entry_text == "现价"
    assert candidate.stop_loss_text == "65200"
    assert candidate.parse_source == "junzhang_profile"
    assert lifecycle.lifecycle_status == "pending_entry"
    assert lifecycle.symbol == "BTC"
    assert lifecycle.side == "short"
    assert lifecycle.stop_loss == 65200
    assert lifecycle.entry_range_low is None
    assert lifecycle.entry_range_high is None


def test_bitcoin_junzhang_profile_rejects_entry_without_risk_controls(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=-1002282384698,
            message_id=5586,
            posted_at=datetime(2026, 7, 3, 22, 34, tzinfo=UTC),
            text="比特与以太现价开一层空单，10倍杠干，做好加一次仓的预期",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.parse_source == "junzhang_profile"
    assert "缺少止损/止盈" in (result.reason or "")
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        assert session.query(StrategyLifecycle).count() == 0


def test_bitcoin_junzhang_profile_closes_unique_active_long_on_take_profit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=-1002282384698,
            message_id=5538,
            symbol="AAVE",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 1, 9, 40, tzinfo=UTC),
            entered_at=datetime(2026, 7, 1, 9, 41, tzinfo=UTC),
            entry_price_actual=85.98,
            stop_loss=84,
        )
        raw_message = RawMessage(
            chat_id=-1002282384698,
            message_id=5576,
            posted_at=datetime(2026, 7, 3, 19, 18, tzinfo=UTC),
            text="多单止盈掉 @Tarderfengge QQ:158241758",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.parse_source == "junzhang_profile"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert lifecycle.exit_signal_message_id == 5576
    assert candidate.event_type == "close_signal"
    assert candidate.symbol == "AAVE"
    assert candidate.side == "long"
    assert candidate.parse_source == "junzhang_profile"


def test_bitcoin_junzhang_profile_moves_stop_loss_to_entry_price(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=-1002282384698,
            message_id=5538,
            symbol="AAVE",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 1, 9, 40, tzinfo=UTC),
            entered_at=datetime(2026, 7, 1, 9, 41, tzinfo=UTC),
            entry_price_actual=85.98,
            stop_loss=84,
        )
        raw_message = RawMessage(
            chat_id=-1002282384698,
            message_id=5575,
            posted_at=datetime(2026, 7, 3, 19, 9, tzinfo=UTC),
            text="止损上移到开仓价 @Tarderfengge QQ:158241758",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.parse_source == "junzhang_profile"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.stop_loss == 85.98
    assert lifecycle.management_signal_message_id == 5575
    assert lifecycle.management_action == "move_stop_to_entry"
    assert candidate.event_type == "position_update"
    assert candidate.stop_loss_text == "85.98"
    assert candidate.parse_source == "junzhang_profile"


def test_recognize_message_now_closes_matching_short_position(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        trade_idea = TradeIdea(
            chat_id=88,
            symbol="BTC",
            side="short",
            status="open",
            created_at=datetime(2026, 6, 15, 13, 43, tzinfo=UTC),
        )
        session.add(trade_idea)
        session.flush()
        lifecycle = StrategyLifecycle(
            trade_idea_id=trade_idea.id,
            chat_id=88,
            message_id=430,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 15, 13, 43, tzinfo=UTC),
            entered_at=datetime(2026, 6, 16, 0, 32, tzinfo=UTC),
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=435,
            posted_at=datetime(2026, 6, 17, 1, 32, tzinfo=UTC),
            text="当前价格接近成本价：65540，空单全部平仓！整体亏损170点左右吧！",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id
        trade_idea_id = trade_idea.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    assert result.reason == "本地规则识别到明确入场/取消/离场消息，已更新匹配的策略状态。"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        trade_idea = session.get(TradeIdea, trade_idea_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert lifecycle.exit_signal_message_id == 435
    assert trade_idea.status == "closed"
    assert candidate.event_type == "close_signal"
    assert candidate.symbol == "BTC"
    assert candidate.side == "short"


def test_recognize_message_now_cancels_recent_pending_limit_order(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entry_range_low=63200,
            entry_range_high=63500,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=376,
            posted_at=datetime(2026, 6, 19, 9, 24, tzinfo=UTC),
            text="取消限价，等我后续信号！",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "cancelled"
    assert lifecycle.exit_signal_message_id == 376
    assert candidate.event_type == "close_signal"
    assert candidate.parse_source == "cancel_heuristic"
    assert candidate.symbol == "BTC"
    assert candidate.side == "short"


def test_recognize_message_now_cancels_expired_order_with_live_binding(tmp_path):
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
            order_id="order-442",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="expired",
            exit_reason="expired",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            exited_at=datetime(2026, 7, 2, 21, 14, tzinfo=UTC),
            entry_range_low=62900,
            entry_range_high=63200,
            stop_loss=64200,
            take_profit="61000",
            execution_binding_id=binding.id,
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=443,
            posted_at=datetime(2026, 7, 3, 1, 0, tzinfo=UTC),
            text="BTC \u53d6\u6d88\u9650\u4ef7\u6302\u5355\uff0c\u7b49\u540e\u7eed\u901a\u77e5",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "expired"
    assert lifecycle.exit_reason == "expired"
    assert lifecycle.management_action == "exit_requested"
    assert lifecycle.exit_signal_message_id == 443
    assert candidate.event_type == "close_signal"
    assert candidate.parse_source == "cancel_heuristic"


def test_recognize_message_now_invalidates_pending_entry_from_later_context(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3888,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 30, 2, 51, tzinfo=UTC),
            entry_range_low=60300,
            entry_range_high=60800,
            stop_loss=61300,
            take_profit="59600/58900/58200",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=3903,
            posted_at=datetime(2026, 6, 30, 8, 24, tzinfo=UTC),
            text="BTC 59500 broke down, wait for next signal.",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "invalidated"
    assert lifecycle.exit_reason == "context_invalidated"
    assert lifecycle.exit_signal_message_id == 3903
    assert candidate.event_type == "context_invalidation"
    assert candidate.parse_source == "context_invalidation_heuristic"
    assert candidate.symbol == "BTC"
    assert candidate.side == "short"


def test_ai_lifecycle_event_cancels_pending_order(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entry_range_low=63200,
            entry_range_high=63500,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=376,
            posted_at=datetime(2026, 6, 19, 9, 24, tzinfo=UTC),
            text="取消限价，等我后续信号！",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "cancel_entry",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "entry_price": None,
            "exit_price": None,
            "confidence": 0.92,
            "reason": "当前消息取消前面的限价挂单",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "cancelled"
    assert lifecycle.exit_signal_message_id == 376
    assert candidate.parse_source == "lifecycle_ai"


def test_lifecycle_event_context_includes_exact_replied_pending_strategy(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=4004,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 21, 8, 50, tzinfo=UTC),
            entry_range_low=65500,
            entry_range_high=65800,
        )
        original = RawMessage(
            chat_id=88,
            message_id=4004,
            posted_at=datetime(2026, 7, 21, 8, 50, tzinfo=UTC),
            text="BTC 多单，65500-65800 挂单",
        )
        reply = RawMessage(
            chat_id=88,
            message_id=4007,
            posted_at=datetime(2026, 7, 21, 15, 15, tzinfo=UTC),
            reply_to_message_id=4004,
            text="这笔也取消，我们明日再战！",
        )
        session.add_all([lifecycle, original, reply])
        session.commit()

        context = _load_lifecycle_event_context(session, reply)

    assert context["reply_context"] == {
        "message_id": 4004,
        "lifecycle_id": lifecycle.id,
        "lifecycle_status": "pending_entry",
        "symbol": "BTC",
        "side": "long",
        "entry_range": "65500-65800",
        "original_text": "BTC 多单，65500-65800 挂单",
    }


def test_ai_lifecycle_event_cancels_replied_pending_strategy(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=4004,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 21, 8, 50, tzinfo=UTC),
        )
        session.add_all(
            [
                lifecycle,
                RawMessage(
                    chat_id=88,
                    message_id=4004,
                    posted_at=datetime(2026, 7, 21, 8, 50, tzinfo=UTC),
                    text="BTC 多单挂单",
                ),
                RawMessage(
                    chat_id=88,
                    message_id=4007,
                    posted_at=datetime(2026, 7, 21, 15, 15, tzinfo=UTC),
                    reply_to_message_id=4004,
                    text="这笔也取消，我们明日再战！",
                ),
            ]
        )
        session.commit()
        raw_message_id = session.query(RawMessage.id).filter_by(message_id=4007).scalar()
        lifecycle_id = lifecycle.id

    seen_requests = []
    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "cancel_entry",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "long",
            "confidence": 0.92,
            "reason": "回复的 BTC 挂单策略已取消",
        },
        seen_requests=seen_requests,
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    request = seen_requests[0]["messages"][1]["content"]
    assert '"reply_to_message_id": 4004' in request
    assert f'"lifecycle_id": {lifecycle_id}' in request
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "cancelled"
    assert candidate.target_lifecycle_id == lifecycle_id
    assert candidate.event_type == "close_signal"


def test_replied_cancel_after_entry_is_blocked_without_full_exit_candidate(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:88:4004:BTC:long",
            kol_id="group:88",
            chat_id=88,
            message_id=4004,
            symbol="BTC",
            side="long",
            status="active",
            pos_id="pos-4004",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=4004,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 21, 8, 50, tzinfo=UTC),
            entered_at=datetime(2026, 7, 22, 6, 26, tzinfo=UTC),
            execution_binding_id=binding.id,
        )
        original = RawMessage(chat_id=88, message_id=4004, text="BTC 多单挂单")
        reply = RawMessage(
            chat_id=88,
            message_id=4007,
            reply_to_message_id=4004,
            text="这笔也取消，我们明日再战！",
        )
        session.add_all([lifecycle, original, reply])
        session.flush()

        applied = _apply_lifecycle_event_decision(
            session,
            reply,
            {
                "event_type": "cancel_entry",
                "target_lifecycle_id": lifecycle.id,
                "symbol": "BTC",
                "side": "long",
                "confidence": 0.92,
                "reason": "回复策略后要求取消挂单",
            },
        )
        session.commit()

        candidates = session.query(SignalCandidate).all()
        events = session.query(ExecutionEvent).all()

    assert applied is True
    assert candidates == []
    assert [(event.action, event.status, event.reason) for event in events] == [
        ("reply_cancel_after_entry", "blocked", "manual_review_required")
    ]


def test_replied_cancel_after_entry_blocks_mismatched_ai_exit_target(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        replied_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:88:4004:BTC:long",
            kol_id="group:88",
            chat_id=88,
            message_id=4004,
            symbol="BTC",
            side="long",
            status="active",
            pos_id="pos-replied",
        )
        other_binding = ExecutionBinding(
            strategy_instance_id="deepcoin:88:4003:BTC:long",
            kol_id="group:88",
            chat_id=88,
            message_id=4003,
            symbol="BTC",
            side="long",
            status="active",
            pos_id="pos-other",
        )
        session.add_all([replied_binding, other_binding])
        session.flush()
        replied_lifecycle = StrategyLifecycle(
            chat_id=88, message_id=4004, symbol="BTC", side="long",
            lifecycle_status="entered", signal_at=datetime(2026, 7, 21, 8, 50, tzinfo=UTC),
            execution_binding_id=replied_binding.id,
        )
        other_lifecycle = StrategyLifecycle(
            chat_id=88, message_id=4003, symbol="BTC", side="long",
            lifecycle_status="entered", signal_at=datetime(2026, 7, 21, 8, 40, tzinfo=UTC),
            execution_binding_id=other_binding.id,
        )
        reply = RawMessage(
            chat_id=88, message_id=4007, reply_to_message_id=4004,
            text="这笔也取消，我们明日再战！",
        )
        session.add_all([
            replied_lifecycle,
            other_lifecycle,
            RawMessage(chat_id=88, message_id=4004, text="BTC 多单挂单"),
            RawMessage(chat_id=88, message_id=4003, text="另一笔 BTC 多单"),
            reply,
        ])
        session.flush()

        applied = _apply_lifecycle_event_decision(
            session,
            reply,
            {
                "event_type": "cancel_entry",
                "target_lifecycle_id": other_lifecycle.id,
                "symbol": "BTC",
                "side": "long",
                "confidence": 0.92,
                "reason": "模型错误选择另一条策略",
            },
        )
        session.commit()

        candidates = session.query(SignalCandidate).all()
        events = session.query(ExecutionEvent).all()

    assert applied is True
    assert candidates == []
    assert [(event.action, event.pos_id) for event in events] == [
        ("reply_cancel_after_entry", "pos-replied")
    ]


def test_configured_ai_falls_back_to_local_cancel_for_unentered_btc_orders(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        first_lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=370,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 8, 9, 15, tzinfo=UTC),
            entry_range_low=62300,
            entry_range_high=62500,
            stop_loss=64000,
            take_profit="60740",
        )
        second_lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=371,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 8, 14, 20, tzinfo=UTC),
            entry_range_low=62500,
            entry_range_high=62700,
            stop_loss=62700,
            take_profit="65620",
        )
        later_lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=390,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 9, 11, 45, tzinfo=UTC),
            entry_range_low=61900,
            entry_range_high=62100,
            stop_loss=63688,
            take_profit="60740",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=376,
            posted_at=datetime(2026, 7, 8, 23, 44, tzinfo=UTC),
            text="今日两次BTC策略都没有入场，取消吧",
        )
        session.add_all([first_lifecycle, second_lifecycle, later_lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        first_lifecycle_id = first_lifecycle.id
        second_lifecycle_id = second_lifecycle.id
        later_lifecycle_id = later_lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "none",
            "target_lifecycle_id": None,
            "symbol": "BTC",
            "side": None,
            "confidence": 0.4,
            "reason": "模型未识别为生命周期事件",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "text"
    with session_factory() as session:
        first_lifecycle = session.get(StrategyLifecycle, first_lifecycle_id)
        second_lifecycle = session.get(StrategyLifecycle, second_lifecycle_id)
        later_lifecycle = session.get(StrategyLifecycle, later_lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert first_lifecycle.lifecycle_status == "exited"
    assert first_lifecycle.exit_reason == "cancelled"
    assert first_lifecycle.exit_signal_message_id == 376
    assert second_lifecycle.lifecycle_status == "exited"
    assert second_lifecycle.exit_reason == "cancelled"
    assert second_lifecycle.exit_signal_message_id == 376
    assert later_lifecycle.lifecycle_status == "pending_entry"
    assert candidate.event_type == "close_signal"
    assert candidate.parse_source == "cancel_heuristic"


def test_unentered_cancel_reverts_lifecycle_entered_after_cancel_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3883,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 8, 14, 15, tzinfo=UTC),
            entered_at=datetime(2026, 7, 8, 17, 31, tzinfo=UTC),
            entry_range_low=62300,
            entry_range_high=62500,
            stop_loss=64000,
            take_profit="60740",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=3885,
            posted_at=datetime(2026, 7, 8, 15, 44, tzinfo=UTC),
            text="今日两次BTC策略都没有入场，取消吧",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.parse_source == "text"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "cancelled"
    assert lifecycle.exited_at == datetime(2026, 7, 8, 15, 44)
    assert lifecycle.exit_signal_message_id == 3885
    assert candidate.event_type == "close_signal"
    assert candidate.parse_source == "cancel_heuristic"


def test_ai_cancel_event_also_runs_local_plural_cancel_fallback(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        pending_lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3878,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 7, 8, 7, 37, tzinfo=UTC),
            entry_range_low=63000,
            entry_range_high=63100,
            stop_loss=65170,
            take_profit="60740",
        )
        later_entered_lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=3883,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 8, 14, 15, tzinfo=UTC),
            entered_at=datetime(2026, 7, 8, 17, 31, tzinfo=UTC),
            entry_range_low=62300,
            entry_range_high=62500,
            stop_loss=64000,
            take_profit="60740",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=3885,
            posted_at=datetime(2026, 7, 8, 15, 44, tzinfo=UTC),
            text="今日两次BTC策略都没有入场，取消吧",
        )
        session.add_all([pending_lifecycle, later_entered_lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        pending_lifecycle_id = pending_lifecycle.id
        later_entered_lifecycle_id = later_entered_lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "cancel_entry",
            "target_lifecycle_id": pending_lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "confidence": 0.92,
            "reason": "模型只锁定了其中一条pending挂单",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        pending_lifecycle = session.get(StrategyLifecycle, pending_lifecycle_id)
        later_entered_lifecycle = session.get(StrategyLifecycle, later_entered_lifecycle_id)

    assert pending_lifecycle.lifecycle_status == "exited"
    assert pending_lifecycle.exit_reason == "cancelled"
    assert later_entered_lifecycle.lifecycle_status == "exited"
    assert later_entered_lifecycle.exit_reason == "cancelled"
    assert later_entered_lifecycle.entered_at is None


def test_local_exit_signal_closes_btc_long_all_out_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=381,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 8, 15, 10, tzinfo=UTC),
            entered_at=datetime(2026, 7, 8, 15, 12, tzinfo=UTC),
            entry_range_low=62500,
            entry_range_high=62700,
            stop_loss=62700,
            take_profit="65620",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=386,
            posted_at=datetime(2026, 7, 9, 8, 12, tzinfo=UTC),
            text="BTC多单，全部出局吧，加上昨晚止盈的，整体盈利约1000点",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.parse_source == "text"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert lifecycle.exit_signal_message_id == 386
    assert candidate.event_type == "close_signal"
    assert candidate.parse_source == "exit_heuristic"


def test_remaining_position_all_exit_is_an_explicit_btc_long_exit():
    assert _parse_explicit_exit_signal("BTC多单余仓全出") == ("BTC", "long")


def test_ai_lifecycle_event_confirms_market_entry(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entry_range_low=63200,
            entry_range_high=63500,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=377,
            posted_at=datetime(2026, 6, 19, 9, 40, tzinfo=UTC),
            text="BTC 现价 63320 入场",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "entry_confirm",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "entry_price": 63320,
            "exit_price": None,
            "confidence": 0.93,
            "reason": "当前消息确认按现价入场",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entry_signal_message_id == 377
    assert lifecycle.entry_price_actual == 63320
    assert candidate.parse_source == "lifecycle_ai"


def test_ai_lifecycle_event_exits_entered_position(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 9, 40, tzinfo=UTC),
            entry_price_actual=63320,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=378,
            posted_at=datetime(2026, 6, 19, 10, 5, tzinfo=UTC),
            text="先临时离场，等下一步通知",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "exit_position",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "entry_price": None,
            "exit_price": None,
            "confidence": 0.9,
            "reason": "当前消息要求临时离场",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert lifecycle.exit_signal_message_id == 378
    assert candidate.parse_source == "lifecycle_ai"


def test_ai_lifecycle_event_treats_breakeven_exit_as_position_exit(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9024,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 18, 15, 57, tzinfo=UTC),
            entered_at=datetime(2026, 6, 18, 15, 58, tzinfo=UTC),
            entry_price_actual=62400,
            stop_loss=60800,
            take_profit="63600/64800",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=9030,
            posted_at=datetime(2026, 6, 19, 11, 15, tzinfo=UTC),
            text="目前还在成本附近，保本出局。",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "exit_position",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "long",
            "entry_price": None,
            "exit_price": None,
            "confidence": 0.91,
            "reason": "当前消息说明成本附近保本出局，应关闭已有持仓策略",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "exited"
    assert lifecycle.exit_reason == "kol_signal"
    assert lifecycle.exit_signal_message_id == 9030
    assert candidate.event_type == "close_signal"
    assert candidate.parse_source == "lifecycle_ai"


def test_ai_lifecycle_event_exits_expired_strategy_when_live_binding_exists(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="mia",
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="active",
            pos_id="pos-442",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="expired",
            exit_reason="expired",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            exited_at=datetime(2026, 7, 2, 21, 14, tzinfo=UTC),
            entry_range_low=62900,
            entry_range_high=63200,
            entry_price_actual=63100,
            stop_loss=64200,
            take_profit="61000",
            execution_binding_id=binding.id,
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=443,
            posted_at=datetime(2026, 7, 3, 1, 0, tzinfo=UTC),
            text="BTC \u4fdd\u672c\u51fa\u5c40",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        applied = _apply_lifecycle_event_decision(
            session,
            raw_message,
            {
                "event_type": "exit_position",
                "target_lifecycle_id": lifecycle_id,
                "symbol": "BTC",
                "side": "short",
                "exit_price": None,
                "confidence": 0.9,
                "reason": "KOL 要求保本出局",
            },
        )
        session.commit()

    assert applied
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_reason is None
    assert lifecycle.exited_at is None
    assert lifecycle.exit_signal_message_id == 443
    assert lifecycle.management_action == "exit_requested"
    assert candidate.event_type == "close_signal"


def test_lifecycle_event_context_includes_expired_strategy_with_live_binding(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        binding = ExecutionBinding(
            kol_id="mia",
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="active",
            pos_id="pos-442",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=442,
            symbol="BTC",
            side="short",
            lifecycle_status="expired",
            exit_reason="expired",
            signal_at=datetime(2026, 7, 2, 15, 14, tzinfo=UTC),
            exited_at=datetime(2026, 7, 2, 21, 14, tzinfo=UTC),
            entry_range_low=62900,
            entry_range_high=63200,
            execution_binding_id=binding.id,
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=443,
            posted_at=datetime(2026, 7, 3, 1, 0, tzinfo=UTC),
            text="BTC \u4fdd\u672c\u51fa\u5c40",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    with session_factory() as session:
        raw_message = session.get(RawMessage, raw_message_id)
        context = _load_lifecycle_event_context(session, raw_message)

    assert [item["lifecycle_id"] for item in context["active_strategies"]] == [lifecycle_id]
    assert context["active_strategies"][0]["status"] == "expired"


def test_ai_lifecycle_event_records_partial_take_profit_update(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=1395,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 17, 10, 26, tzinfo=UTC),
            entered_at=datetime(2026, 6, 18, 4, 11, tzinfo=UTC),
            entry_price_actual=63794.4,
            stop_loss=61000,
            take_profit="65500/66500/67500",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=1400,
            posted_at=datetime(2026, 6, 18, 8, 36, tzinfo=UTC),
            text="大饼反弹一般，现价64500附近提前止盈一半带保护，整体思路还是高抛低吸为主",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "long",
            "entry_price": None,
            "exit_price": None,
            "management_action": "partial_take_profit",
            "confidence": 0.92,
            "reason": "当前消息要求提前止盈一半并带保护，属于持仓管理更新",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_signal_message_id is None
    assert lifecycle.management_action is None
    assert lifecycle.stop_loss == 61000
    assert lifecycle.management_note is None
    assert candidate.event_type == "position_update"
    assert candidate.parse_source == "lifecycle_ai"
    assert candidate.management_action == "partial_then_break_even"
    assert candidate.management_fraction == pytest.approx(0.5)
    assert candidate.stop_loss_text == "63794.4"


def test_ai_lifecycle_event_explicit_stop_overrides_protection_price(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2176,
            symbol="ETH",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 22, 12, 54, 36, tzinfo=UTC),
            entered_at=datetime(2026, 6, 22, 12, 54, 41, tzinfo=UTC),
            entry_price_actual=1760,
            stop_loss=1760,
            take_profit="1845",
            management_action="partial_take_profit, move_stop_to_protect",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=2182,
            posted_at=datetime(2026, 6, 22, 15, 50, 42, tzinfo=UTC),
            text="设置好止盈止损持仓过夜！止盈位：1845！！！止损位：1725！！！",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "ETH",
            "side": "long",
            "entry_price": None,
            "exit_price": None,
            "stop_loss": "1725",
            "take_profit": "1845",
            "management_action": "risk_update",
            "confidence": 0.94,
            "reason": "当前消息明确要求设置止盈1845、止损1725并持仓过夜，属于持仓风控更新",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_signal_message_id is None
    assert lifecycle.management_action == "partial_take_profit, move_stop_to_protect"
    assert lifecycle.stop_loss == 1760
    assert lifecycle.take_profit == "1845"
    assert lifecycle.management_note is None
    assert candidate.event_type == "position_update"
    assert candidate.management_action == "adjust_stop_loss"
    assert candidate.stop_loss_text == "1725"
    assert candidate.take_profit_text == "1845"


@pytest.mark.parametrize(
    "management_text",
    [
        "BTC市价62600附近，止损下移动500点，调整61900。",
        "BTC 61900止损，移动保护。",
    ],
)
def test_ai_lifecycle_event_explicit_stop_never_becomes_break_even_close(
    tmp_path,
    monkeypatch,
    management_text,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=-1002337721508,
            message_id=9818,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 31, 10, 31, 47, tzinfo=UTC),
            entered_at=datetime(2026, 7, 31, 11, 48, tzinfo=UTC),
            entry_price_actual=63695,
            stop_loss=62400,
            take_profit="65000/66100",
        )
        raw_message = RawMessage(
            chat_id=-1002337721508,
            message_id=9824,
            posted_at=datetime(2026, 7, 31, 14, 10, 24, tzinfo=UTC),
            text=management_text,
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "long",
            "stop_loss": 61900.0,
            "management_action": "move_stop_to_protect",
            "confidence": 0.95,
            "reason": "BTC多单止损调整至61900。",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()

    assert candidate.event_type == "position_update"
    assert candidate.management_action == "adjust_stop_loss"
    assert candidate.stop_loss_text == "61900"
    assert candidate.stop_price_source == "current_message_text"


def test_ai_lifecycle_event_ignores_implausible_stop_and_uses_protection_price(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=1072,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 2, 14, 55, 54, tzinfo=UTC),
            entered_at=datetime(2026, 7, 2, 14, 56, 25, tzinfo=UTC),
            entry_price_actual=61563,
            stop_loss=62440,
            take_profit="59588",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=1073,
            posted_at=datetime(2026, 7, 2, 15, 1, 26, tzinfo=UTC),
            text=(
                "背靠6万2阻力区，入场做空\n"
                "第一止盈位 60950 移动止损至成本价"
            ),
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "entry_price": None,
            "exit_price": None,
            "stop_loss": "2",
            "take_profit": "60950",
            "management_action": "move_stop_to_protect",
            "confidence": 0.85,
            "reason": "移动止损至成本价，不是明确把止损设为2。",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_signal_message_id is None
    assert lifecycle.management_action is None
    assert lifecycle.stop_loss == 62440
    assert lifecycle.take_profit == "59588"
    assert candidate.event_type == "position_update"
    assert candidate.management_action == "partial_then_break_even"
    assert candidate.stop_loss_text == "61563"
    assert candidate.take_profit_text == "60950"


def test_ai_lifecycle_event_downgrades_first_take_profit_exit_to_management_update(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=1072,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 7, 2, 14, 55, 54, tzinfo=UTC),
            entered_at=datetime(2026, 7, 2, 14, 56, 25, tzinfo=UTC),
            entry_price_actual=61563,
            stop_loss=62440,
            take_profit="59588",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=1073,
            posted_at=datetime(2026, 7, 2, 15, 1, 26, tzinfo=UTC),
            text=(
                "🚩🚩空单入场理由🚩🚩\n"
                "背靠6万2阻力区，入场做空\n"
                "止损给的很小，有回调需求\n"
                "过夜单，三姐没法实时盯盘\n"
                "第一止盈位 60950 移动止损至成本价\n"
                "@Tarderfengge QQ:158241758"
            ),
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "exit_position",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "short",
            "entry_price": None,
            "exit_price": "60950",
            "take_profit": "60950",
            "management_action": None,
            "confidence": 0.91,
            "reason": "模型误把第一止盈位和移动止损理解成全量离场",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.exit_signal_message_id is None
    assert lifecycle.management_signal_message_id is None
    assert lifecycle.management_action is None
    assert lifecycle.stop_loss == 62440
    assert lifecycle.take_profit == "59588"
    assert candidate.event_type == "position_update"
    assert candidate.management_action == "partial_then_break_even"
    assert candidate.stop_loss_text == "61563"
    assert candidate.take_profit_text == "60950"


def test_ai_lifecycle_event_extracts_explicit_stop_from_management_text(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9118,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 23, 8, 20, 54, tzinfo=UTC),
            entered_at=datetime(2026, 6, 23, 8, 24, tzinfo=UTC),
            entry_price_actual=62214,
            stop_loss=62214,
            take_profit="66500",
            management_action="partial_take_profit, move_stop_to_protect",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=9123,
            posted_at=datetime(2026, 6, 23, 16, 11, 37, tzinfo=UTC),
            text="目前已经东八区凌晨12点，做短线收益700点可以全部止盈出局，剩余仓位过夜持仓做好成本保护，止损修改入场价62000附近。",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "BTC",
            "side": "long",
            "entry_price": None,
            "exit_price": None,
            "stop_loss": None,
            "take_profit": None,
            "management_action": "risk_update",
            "confidence": 0.94,
            "reason": "当前消息要求剩余仓位继续持有并做成本保护，属于持仓风险更新。",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_signal_message_id is None
    assert lifecycle.management_action == "partial_take_profit, move_stop_to_protect"
    assert lifecycle.stop_loss == 62214
    assert candidate.event_type == "position_update"
    assert candidate.management_action == "adjust_stop_loss"
    assert candidate.stop_loss_text == "62000"


def test_lifecycle_event_ignores_stop_update_after_protective_stop_exit(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2176,
            symbol="ETH",
            side="long",
            lifecycle_status="exited",
            exit_reason="stop_loss",
            signal_at=datetime(2026, 6, 22, 12, 54, 36, tzinfo=UTC),
            entered_at=datetime(2026, 6, 22, 12, 54, 41, tzinfo=UTC),
            exited_at=datetime(2026, 6, 22, 14, 20, tzinfo=UTC),
            entry_price_actual=1760,
            exit_price_actual=1760,
            stop_loss=1760,
            take_profit="1845",
            management_action="partial_take_profit, move_stop_to_protect",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=2182,
            posted_at=datetime(2026, 6, 22, 15, 50, 42, tzinfo=UTC),
            text="设置好止盈止损持仓过夜！止盈位：1845！！！止损位：1725！！！",
        )
        session.add_all([lifecycle, raw_message])
        session.flush()

        applied = _apply_lifecycle_event_decision(
            session,
            raw_message,
            {
                "event_type": "position_update",
                "target_lifecycle_id": lifecycle.id,
                "symbol": "ETH",
                "side": "long",
                "stop_loss": "1725",
                "take_profit": "1845",
                "management_action": "risk_update",
                "confidence": 0.94,
                "reason": "当前消息明确要求设置止盈1845、止损1725并持仓过夜",
            },
        )

        assert applied is False
        assert lifecycle.lifecycle_status == "exited"
        assert lifecycle.exit_reason == "stop_loss"
        assert lifecycle.stop_loss == 1760
        assert lifecycle.exited_at == datetime(2026, 6, 22, 14, 20, tzinfo=UTC)
        assert lifecycle.management_signal_message_id is None
        assert session.query(SignalCandidate).count() == 0


def test_lifecycle_event_rejects_target_with_different_explicit_symbol(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9181,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 28, 1, 0, tzinfo=UTC),
            entered_at=datetime(2026, 6, 28, 1, 10, tzinfo=UTC),
            entry_range_low=60300,
            entry_range_high=60600,
            stop_loss=60300,
            take_profit="58400/57000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=9188,
            posted_at=datetime(2026, 6, 28, 5, 29, 38, tzinfo=UTC),
            text="一对一指导SOL空单，市价70全部止盈出局。",
        )
        session.add_all([lifecycle, raw_message])
        session.flush()

        applied = _apply_lifecycle_event_decision(
            session,
            raw_message,
            {
                "event_type": "exit_position",
                "target_lifecycle_id": lifecycle.id,
                "symbol": "BTC",
                "side": "short",
                "confidence": 0.9,
                "reason": "模型误把 SOL 消息指向 BTC 策略",
            },
        )
        session.flush()

        assert applied is False
        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.exit_signal_message_id is None
        assert session.query(SignalCandidate).count() == 0


def test_exit_heuristic_does_not_close_btc_when_message_names_sol(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        btc_lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9181,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 28, 1, 0, tzinfo=UTC),
            entered_at=datetime(2026, 6, 28, 1, 10, tzinfo=UTC),
            entry_range_low=60300,
            entry_range_high=60600,
            stop_loss=60300,
            take_profit="58400/57000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=9188,
            posted_at=datetime(2026, 6, 28, 5, 29, 38, tzinfo=UTC),
            text="一对一指导SOL空单，市价70全部止盈出局。",
        )
        session.add_all([btc_lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = btc_lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.exit_signal_message_id is None
        assert session.query(SignalCandidate).count() == 0


def test_ai_lifecycle_event_records_scaled_take_profit_percentage_update(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=2124,
            symbol="ETH",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 19, 3, 6, tzinfo=UTC),
            entered_at=datetime(2026, 6, 19, 3, 7, tzinfo=UTC),
            entry_price_actual=1705,
            stop_loss=1740,
            take_profit="1620",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=2131,
            posted_at=datetime(2026, 6, 19, 10, 12, tzinfo=UTC),
            text="现目前空单获利16个点！\n持仓收益达到100％！\n分批止盈30％！！！",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "position_update",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "ETH",
            "side": "short",
            "entry_price": None,
            "exit_price": None,
            "management_action": "partial_take_profit",
            "confidence": 0.93,
            "reason": "当前消息说明空单已盈利并要求分批止盈30%，属于已有持仓的部分止盈管理。",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.parse_source == "lifecycle_ai"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.management_signal_message_id is None
    assert lifecycle.management_action is None
    assert lifecycle.management_note is None
    assert candidate.event_type == "position_update"
    assert candidate.parse_source == "lifecycle_ai"
    assert candidate.management_action == "partial_take_profit"
    assert candidate.management_fraction == pytest.approx(0.3)


def test_recognize_message_now_confirms_recent_pending_market_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entry_range_low=63200,
            entry_range_high=63500,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=377,
            posted_at=datetime(2026, 6, 19, 9, 40, tzinfo=UTC),
            text="BTC 现价 63320 入场",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entered_at == datetime(2026, 6, 19, 9, 40)
    assert lifecycle.entry_signal_message_id == 377
    assert lifecycle.entry_price_actual == 63320
    assert candidate.event_type == "entry_signal"
    assert candidate.parse_source == "entry_confirm_heuristic"
    assert candidate.symbol == "BTC"
    assert candidate.side == "short"
    assert candidate.entry_text == "63320"


def test_recognize_message_now_confirms_unique_pending_entry_without_symbol(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=374,
            symbol="BTC",
            side="short",
            lifecycle_status="pending_entry",
            signal_at=datetime(2026, 6, 19, 4, 29, tzinfo=UTC),
            entry_range_low=63200,
            entry_range_high=63500,
            stop_loss=64200,
            take_profit="62000",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=377,
            posted_at=datetime(2026, 6, 19, 9, 40, tzinfo=UTC),
            text="现价入场",
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        candidate = session.query(SignalCandidate).one()

    assert lifecycle.lifecycle_status == "entered"
    assert lifecycle.entry_signal_message_id == 377
    assert lifecycle.entry_price_actual is None
    assert candidate.parse_source == "entry_confirm_heuristic"


def test_recognize_message_now_rejects_trading_education_content(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=8,
            text=(
                "今天讲一个合约短线交易里面非常重要的知识点，不要逆势加仓。\n"
                "趋势对的时候可以考虑扩大盈利，趋势错的时候首先考虑控制风险。"
            ),
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0


def test_exit_heuristic_ignores_long_trading_education_article(tmp_path):
    education_text = (
        "亲爱的朋友们：\n"
        "今天讲一个合约短线交易中最容易被忽略的知识点——开仓位置，比方向更重要。\n"
        "很多人总是在研究：做多还是做空？但真正决定一笔交易盈亏的，往往不是方向，而是你的进场位置。\n"
        "举个例子。同样都是看涨。有人追高进场，止损很大，盈亏比很差。\n"
        "有人等回踩支撑再进，止损很小，盈亏比却很好。两个人方向一样，结果却完全不同。\n"
        "陈哥一直强调：宁可错过，也不要追价。等你离场之后，行情又按原来的方向走了。\n"
        "交易不是比谁判断方向最准，而是比谁能够在最有优势的位置出手。位置决定风险，风险决定利润。"
    )
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9210,
            symbol="SOL",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 29, 5, 0, tzinfo=UTC),
            entered_at=datetime(2026, 6, 29, 5, 5, tzinfo=UTC),
            entry_range_low=73.02,
            entry_range_high=73.02,
            stop_loss=77,
            take_profit="64",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=9220,
            posted_at=datetime(2026, 6, 29, 6, 38, 5, tzinfo=UTC),
            text=education_text,
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.exit_signal_message_id is None
        assert session.query(SignalCandidate).count() == 0


def test_ai_lifecycle_event_ignores_long_trading_education_article(tmp_path, monkeypatch):
    education_text = (
        "亲爱的朋友们：今天讲一个合约短线交易中最容易被忽略的知识点——开仓位置，比方向更重要。\n"
        "很多人总是在研究：做多还是做空？但真正决定一笔交易盈亏的，往往不是方向，而是你的进场位置。\n"
        "举个例子，有人追高进场，止损很大，盈亏比很差；有人等回踩支撑再进，止损很小。\n"
        "陈哥一直强调，宁可错过，也不要追价。等你离场之后，行情又按原来的方向走了。\n"
        "交易不是比谁判断方向最准，而是比谁能够在最有优势的位置出手。位置决定风险，风险决定利润。"
    )
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=88,
            message_id=9210,
            symbol="SOL",
            side="short",
            lifecycle_status="entered",
            signal_at=datetime(2026, 6, 29, 5, 0, tzinfo=UTC),
            entered_at=datetime(2026, 6, 29, 5, 5, tzinfo=UTC),
            entry_range_low=73.02,
            entry_range_high=73.02,
            stop_loss=77,
            take_profit="64",
        )
        raw_message = RawMessage(
            chat_id=88,
            message_id=9220,
            posted_at=datetime(2026, 6, 29, 6, 38, 5, tzinfo=UTC),
            text=education_text,
        )
        session.add_all([lifecycle, raw_message])
        session.commit()
        raw_message_id = raw_message.id
        lifecycle_id = lifecycle.id

    _mock_deepseek_lifecycle_event(
        monkeypatch,
        {
            "event_type": "exit_position",
            "target_lifecycle_id": lifecycle_id,
            "symbol": "SOL",
            "side": "short",
            "entry_price": None,
            "exit_price": None,
            "confidence": 0.9,
            "reason": "模型误把教学长文里的离场词当成临时离场",
        },
    )

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type("Provider", (), {
                "is_configured": True,
                "base_url": "http://deepseek.test",
                "api_key": "",
                "model": "deepseek-chat",
                "timeout_seconds": 10,
            })(),
        ),
    )

    assert result.status == "非策略"
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)

        assert lifecycle.lifecycle_status == "entered"
        assert lifecycle.exit_signal_message_id is None
        assert session.query(SignalCandidate).count() == 0


def test_recognize_message_now_skips_video_media(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=3, text="视频复盘")
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="messagemediadocument",
                mime_type="video/mp4",
            )
        )
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(session_factory, raw_message_id=raw_message_id)

    assert result.status == "非策略"
    assert result.reason == "视频消息默认跳过"


def test_recognize_message_now_keeps_image_pending_for_later_ocr(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(chat_id=88, message_id=4, text="")
        session.add(raw_message)
        session.flush()
        session.add(MediaAsset(raw_message_id=raw_message.id, kind="messagemediaphoto"))
        session.commit()
        raw_message_id = raw_message.id

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(),  # local rule parser
    )

    assert result.status == "待识别"
    assert result.reason == "图片识别等待 OCR/AI 接入"


def test_glm_ocr_caption_message_falls_back_to_text_strategy(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "research.db")
    image_path = tmp_path / "chart.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")
    source_text = (
        "以太币 1555-1535 这里 可以考虑做多\n"
        "止损：15分钟有效跌破1520\n"
        "止盈：1575-1600-1625-1640-1675\n"
        "今天策略已经都盈利了，正常所长就不做单了，给各位一个参考"
    )
    seen_chat_inputs: list[str] = []

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=6699,
            sender_name="币圈所长会员群-11分组",
            posted_at=datetime(2026, 6, 26, 14, 4, 27, tzinfo=UTC),
            text=source_text,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="messagemediaphoto",
                local_path=str(image_path),
                mime_type="image/jpeg",
            )
        )
        session.commit()
        raw_message_id = raw_message.id

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            if url.endswith("/layout_parsing"):
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={
                        "md_results": (
                            "<table><tr><td>1641.54</td></tr>"
                            "<tr><td>1624.78</td></tr></table>"
                        )
                    },
                )
            seen_chat_inputs.append(json["messages"][1]["content"])
            if len(seen_chat_inputs) == 1:
                payload = {
                    "recognition_result": "非策略",
                    "reason": "OCR 表格缺少方向和完整入场说明",
                    "strategy": {},
                    "confidence": 0.3,
                }
            else:
                payload = {
                    "recognition_result": "是策略",
                    "reason": "caption 含 ETH 做多、入场、止损和止盈",
                    "strategy": {
                        "symbol": "ETH",
                        "side": "long",
                        "entry": "1555-1535",
                        "stop_loss": "1520",
                        "take_profit": "1575/1600/1625/1640/1675",
                        "order_type": "limit",
                    },
                    "confidence": 0.91,
                }
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": __import__("json").dumps(
                                    payload,
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("telegram_kol_research.message_recognition.httpx.Client", FakeClient)

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=AiProviderConfig(
                base_url="http://deepseek.test",
                model="deepseek-chat",
                timeout_seconds=10,
            ),
            image_provider=AiProviderConfig(
                base_url="http://glm.test",
                model="glm-ocr",
                timeout_seconds=10,
            ),
        ),
    )

    assert result.status == "是策略"
    assert result.parse_source == "text_ai"
    assert len(seen_chat_inputs) == 2
    assert "<table>" in seen_chat_inputs[0]
    assert seen_chat_inputs[1] == source_text
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        recognition = session.query(MessageRecognition).one()
        media_asset = session.query(MediaAsset).one()

    assert candidate.symbol == "ETH"
    assert candidate.side == "long"
    assert candidate.entry_text == "1555-1535"
    assert candidate.parse_source == "text_ai"
    assert recognition.status == "是策略"
    assert media_asset.ocr_text.startswith("<table>")


def test_authoritative_mimo_routes_btc_eth_price_scale_conflict_to_manual_review(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=7001,
            sender_name="智哥",
            posted_at=datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
            text="BTC 空单，进场 1840-1860，止损 1905，止盈 1780/1720",
        )
        session.add(raw_message)
        session.commit()
        raw_message_id = raw_message.id

    payload = {
        "recognition_result": "是策略",
        "reason": "当前消息包含空单入场、止损和止盈",
        "strategy": {
            "symbol": "BTC",
            "side": "short",
            "entry": "1840-1860",
            "stop_loss": "1905",
            "take_profit": "1780/1720",
            "order_type": "limit",
        },
        "lifecycle_event": {"event_type": "none", "confidence": 0.0},
        "input_reading": {
            "observed_text": "BTC 空单，进场 1840-1860，止损 1905，止盈 1780/1720",
            "image_quality": "none",
        },
        "confidence": 0.92,
    }

    result = apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=payload,
        model="mimo-v2.5",
        authoritative_generation="generation-1",
    )

    assert result.status == "识别失败"
    assert result.reason is not None
    assert "symbol_price_scale_conflict" in result.reason
    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
        recognition = session.query(MessageRecognition).one()
        lifecycle_count = session.query(StrategyLifecycle).count()

    assert candidate.symbol == "ETH"
    assert candidate.side == "short"
    assert candidate.entry_text == "1840-1860"
    assert candidate.parse_source == "mimo_symbol_review"
    assert candidate.review_status == "needs_review"
    assert candidate.confidence == pytest.approx(0.69)
    assert "BTC/ETH" in (candidate.review_note or "")
    assert recognition.status == "识别失败"
    assert lifecycle_count == 0


def test_glm_ocr_recap_caption_does_not_import_old_screenshot_strategy(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    image_path = tmp_path / "hype.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")
    source_text = (
        "💵💵 HYPE 会员空单 盈利 各位也可以做个参考，"
        "目前看四小时这个阴线只要无法突破，那么三日线传导的双顶部"
        "是还有可能继续下跌的。"
    )
    seen_chat_inputs: list[str] = []

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=6694,
            sender_name="币圈所长会员群-11分组",
            posted_at=datetime(2026, 6, 26, 13, 31, 23, tzinfo=UTC),
            text=source_text,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="messagemediaphoto",
                local_path=str(image_path),
                mime_type="image/jpeg",
            )
        )
        session.commit()
        raw_message_id = raw_message.id

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            if url.endswith("/layout_parsing"):
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={
                        "md_results": (
                            "HYPE 现在63.6附近开空\n"
                            "止损：站上65.5\n"
                            "止盈；62-60-58-56"
                        )
                    },
                )
            seen_chat_inputs.append(json["messages"][1]["content"])
            payload = {
                "recognition_result": "是策略",
                "reason": "合并文本包含 HYPE 做空、入场、止损和止盈",
                "strategy": {
                    "symbol": "HYPE",
                    "side": "short",
                    "entry": "63.6附近",
                    "stop_loss": "65.5",
                    "take_profit": "62/60/58/56",
                    "order_type": "market",
                },
                "confidence": 0.95,
            }
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": __import__("json").dumps(
                                    payload,
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("telegram_kol_research.message_recognition.httpx.Client", FakeClient)

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=AiProviderConfig(
                base_url="http://deepseek.test",
                model="deepseek-chat",
                timeout_seconds=10,
            ),
            image_provider=AiProviderConfig(
                base_url="http://glm.test",
                model="glm-ocr",
                timeout_seconds=10,
            ),
        ),
    )

    assert result.status == "非策略"
    assert "历史截图" in (result.reason or "")
    assert len(seen_chat_inputs) == 1
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        assert session.query(StrategyLifecycle).count() == 0
        recognition = session.query(MessageRecognition).one()
        media_asset = session.query(MediaAsset).one()

    assert recognition.status == "非策略"
    assert "HYPE 现在63.6附近开空" in (media_asset.ocr_text or "")


def test_mimo_image_recognition_uses_caption_and_raw_image_without_ocr(
    tmp_path, monkeypatch
):
    session_factory = create_session_factory(tmp_path / "research.db")
    image_path = tmp_path / "old-strategy.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")
    source_text = "\u8fd9\u4e2a\u7b56\u7565\u5df2\u7ecf\u76c8\u5229\uff0c\u505a\u4e2a\u590d\u76d8\u53c2\u8003"
    seen_requests: list[dict] = []

    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=88,
            message_id=9189,
            sender_name="\u6bd4\u7279\u5e01\u9648\u54e5\u4f1a\u5458\u7fa4-11\u5206\u7ec4",
            text=source_text,
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                kind="messagemediaphoto",
                local_path=str(image_path),
                mime_type="image/jpeg",
            )
        )
        session.commit()
        raw_message_id = raw_message.id

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def post(self, url, json, headers):
            seen_requests.append(json)
            payload = {
                "recognition_result": "\u975e\u7b56\u7565",
                "input_reading": {
                    "observed_text": "\u914d\u6587\u8bf4\u5df2\u7ecf\u76c8\u5229\uff0c\u56fe\u7247\u662f\u5386\u53f2\u7b56\u7565\u622a\u56fe",
                    "image_quality": "clear",
                },
                "reason": "\u540c\u6761\u6d88\u606f\u662f\u76c8\u5229\u590d\u76d8\uff0c\u4e0d\u662f\u65b0\u5f00\u4ed3\u7b56\u7565",
                "strategy": {},
                "confidence": 0.2,
            }
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "choices": [
                        {
                            "message": {
                                "content": __import__("json").dumps(
                                    payload,
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                },
            )

    monkeypatch.setattr("telegram_kol_research.message_recognition.httpx.Client", FakeClient)

    result = recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            recognition_prompt="Use strict DeepSeek text rules.",
            mimo_direct_prompt="Use MiMo direct prompt.",
            image_provider=AiProviderConfig(
                base_url="https://api.xiaomimimo.com/v1",
                model="mimo-v2.5",
                timeout_seconds=10,
            ),
        ),
    )

    assert result.status == "\u975e\u7b56\u7565"
    assert result.parse_source == "image_ai"
    assert len(seen_requests) == 1
    request = seen_requests[0]
    assert request["model"] == "mimo-v2.5"
    system_prompt = request["messages"][0]["content"]
    assert "Use strict DeepSeek text rules." in system_prompt
    assert "Use MiMo direct prompt." not in system_prompt
    assert "\u5fc5\u987b\u7ed3\u5408\u5f53\u524d\u6b63\u6587/caption \u4e0e\u56fe\u7247\u6574\u4f53\u5224\u65ad" in system_prompt
    user_content = request["messages"][1]["content"]
    assert isinstance(user_content, list)
    assert source_text in user_content[0]["text"]
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    with session_factory() as session:
        assert session.query(SignalCandidate).count() == 0
        media_asset = session.query(MediaAsset).one()
    assert media_asset.ocr_text is None


def test_recognize_message_now_raises_for_missing_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "research.db")

    with pytest.raises(LookupError, match="raw message not found"):
        recognize_message_now(session_factory, raw_message_id=999)


def _assert_composite_message_contract(
    tmp_path,
    monkeypatch,
    *,
    text,
    decision,
    expected_stop_mode,
    expected_stop_price,
):
    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=8804,
            message_id=100,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=datetime(2026, 8, 4, 1, tzinfo=UTC),
            entered_at=datetime(2026, 8, 4, 1, 1, tzinfo=UTC),
            entry_price_actual=64000,
            stop_loss=62000,
            take_profit="65000/66000/67000",
        )
        raw_message = RawMessage(
            chat_id=8804,
            message_id=101,
            posted_at=datetime(2026, 8, 4, 2, tzinfo=UTC),
            text=text,
        )
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:8804:100:BTC:long",
            kol_id="group:8804",
            chat_id=8804,
            message_id=100,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="active",
        )
        session.add_all([lifecycle, raw_message, binding])
        session.flush()
        lifecycle.execution_binding_id = binding.id
        session.commit()
        lifecycle_id = lifecycle.id
        raw_message_id = raw_message.id

    payload = {
        "event_type": "position_update",
        "target_lifecycle_id": lifecycle_id,
        "symbol": "BTC",
        "side": "long",
        "management_action": "partial_take_profit",
        "confidence": 0.95,
        **decision,
    }
    _mock_deepseek_lifecycle_event(monkeypatch, payload)

    recognize_message_now(
        session_factory,
        raw_message_id=raw_message_id,
        ai_recognition_config=AiRecognitionConfig(
            text_provider=type(
                "Provider",
                (),
                {
                    "is_configured": True,
                    "base_url": "http://deepseek.test",
                    "api_key": "",
                    "model": "deepseek-chat",
                    "timeout_seconds": 10,
                },
            )(),
        ),
    )

    with session_factory() as session:
        candidate = session.query(SignalCandidate).one()
    contract = __import__("json").loads(candidate.management_contract_json)
    assert candidate.management_action == "partial_then_break_even"
    assert candidate.management_fraction == pytest.approx(0.5)
    assert candidate.management_contract_fingerprint
    assert contract["target_lifecycle_id"] == lifecycle_id
    assert contract["strategy_instance_id"] == "deepcoin:8804:100:BTC:long"
    assert contract["close_fraction"] == "0.5"
    assert contract["stop_mode"] == expected_stop_mode
    assert contract["stop_price"] == expected_stop_price
    assert contract["required_components"] == [
        "consume_take_profit_stage",
        "converge_partial_close",
        "replace_remaining_protection",
    ]


def test_miya_composite_message_persists_complete_contract(tmp_path, monkeypatch):
    _assert_composite_message_contract(
        tmp_path,
        monkeypatch,
        text="BTC多单目前浮盈1100点，止盈50%，剩余仓位止损位移动至62700，做无风险持仓",
        decision={"stop_loss": "62700"},
        expected_stop_mode="explicit_price",
        expected_stop_price="62700",
    )


def test_sanjie_composite_message_persists_complete_contract(tmp_path, monkeypatch):
    _assert_composite_message_contract(
        tmp_path,
        monkeypatch,
        text="比特币多单止盈50%，止损位移动至开仓价！",
        decision={},
        expected_stop_mode="actual_entry_price",
        expected_stop_price=None,
    )
