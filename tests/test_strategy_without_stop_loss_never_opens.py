"""有方向、没止损的「是策略」不算新策略，不能下单（用户规则；设计稿决策 2）.

2026-09-29, first-pass phase 3 pre-deploy check requested by the dispatcher.
Under prompt v11 (``ai_prompt_versions.id = 11``) raw 19016 comes back as
``recognition_result = 是策略`` with a complete ``strategy`` whose
``stop_loss`` is null -- the prompt's deliberate "two criteria" section makes
that a legitimate answer. It must never become a lifecycle or an order.

Production (``trading_settings`` ``global``: ``multi_instruction_mode = live``,
activation after raw 10027) already refuses it at
``authoritative_instructions._complete_strategy`` -- but only when the payload
has no ``instructions`` of its own: an explicit ``instructions`` list is
normalized as given, the ``strategy`` is never checked, and the entry went on to
``_persist_ai_result`` and ``_ensure_lifecycle_record``, which require only a
symbol and a side. The guard in ``apply_authoritative_mimo_payload`` closes
that for every mode.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from telegram_kol_research.auto_trade_execution import (
    auto_process_message_trade_signal,
)
from telegram_kol_research.message_recognition import (
    apply_authoritative_mimo_payload,
)
from telegram_kol_research.models import (
    ExecutionBinding,
    MessageRecognition,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.trading_settings import save_trading_settings

from tests.test_auto_trade_execution import (
    _FakeDeepcoinClient,
    _StaticContractSpecProvider,
)
from tests.test_entry_confirm_sizing_and_lifecycle import (
    _add_raw_message,
    _group_config,
    _session_factory,
)


#: raw 19016 under v11 (read-only re-answer on the production server,
#: 2026-09-29), contact footer removed.
_TEXT_19016 = (
    "ETH 方向：做空-低倍-分批入场 入场：当前2750附近建仓，2800附近补仓 "
    "止盈：2650附近/2550/2460 仓位：10%每次 止损：拿3-4天，具体等我那时候信号"
)


def _payload_19016(**overrides):
    payload = {
        "recognition_result": "是策略",
        "confidence": 0.96,
        "reason": "有方向有入场有止盈，无价格止损",
        "strategy": {
            "symbol": "ETH",
            "side": "short",
            "entry": "2750附近/2800附近",
            "stop_loss": None,
            "take_profit": "2650附近/2550/2460",
            "leverage": "5倍",
            "order_type": "market+limit",
        },
        "lifecycle_event": {"event_type": "none", "confidence": 0.96},
        "message_classes": [
            {
                "class": "策略管理",
                "target": {
                    "resolution": "forthcoming",
                    "lifecycle_id": None,
                    "symbol": "ETH",
                    "side": "short",
                },
            }
        ],
        "entry_context": {
            "kind": "entry_preamble",
            "symbol": "ETH",
            "side": "short",
            "risk_multiplier": "0.1",
            "confidence": 0.98,
        },
        "input_reading": {"image_quality": "none"},
    }
    payload.update(overrides)
    return payload


def _counts(session_factory, raw_message_id):
    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == raw_message_id)
            .filter(SignalCandidate.event_type == "entry_signal")
            .count()
        )
        lifecycles = session.query(StrategyLifecycle).count()
        recognition = (
            session.query(MessageRecognition)
            .filter(MessageRecognition.raw_message_id == raw_message_id)
            .one_or_none()
        )
        return candidates, lifecycles, recognition


def _setup(tmp_path, name, *, multi_instruction_mode):
    session_factory = _session_factory(tmp_path / f"{name}.db")
    save_trading_settings(
        session_factory,
        {
            "auto_trade_enabled": True,
            "default_max_loss_usdt": 20,
            "allowed_symbols": ["ETH"],
            "multi_instruction_mode": multi_instruction_mode,
            "multi_instruction_activation_after_raw_message_id": 0,
        },
    )
    posted_at = datetime(2026, 9, 25, 10, 2, 0, tzinfo=UTC)
    with session_factory() as session:
        raw = _add_raw_message(
            session, message_id=19016, posted_at=posted_at, text=_TEXT_19016
        )
        session.commit()
        return session_factory, raw.id, posted_at


@pytest.mark.parametrize(
    ("mode", "overrides"),
    [
        ("live", {}),
        # The gap: explicit instructions are normalized as given, so the
        # strategy is never checked by the instruction contract.
        ("live", {"instructions": []}),
        ("disabled", {}),
        ("shadow", {}),
    ],
    ids=["live", "live-explicit-instructions", "disabled", "shadow"],
)
def test_a_strategy_without_a_stop_loss_creates_no_candidate_and_no_lifecycle(
    tmp_path, mode, overrides
):
    session_factory, raw_message_id, _ = _setup(
        tmp_path, f"no-stop-{mode}-{len(overrides)}", multi_instruction_mode=mode
    )

    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=_payload_19016(**overrides),
        model="gpt-5.6-luna",
        authoritative_generation="generation-19016",
    )

    candidates, lifecycles, recognition = _counts(session_factory, raw_message_id)
    assert candidates == 0
    assert lifecycles == 0
    assert recognition is not None
    assert recognition.status != "是策略"


@pytest.mark.parametrize("overrides", [{}, {"instructions": []}], ids=["plain", "explicit"])
def test_a_strategy_without_a_stop_loss_never_reaches_the_exchange(tmp_path, overrides):
    session_factory, raw_message_id, posted_at = _setup(
        tmp_path, f"no-stop-exchange-{len(overrides)}", multi_instruction_mode="live"
    )
    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=_payload_19016(**overrides),
        model="gpt-5.6-luna",
        authoritative_generation="generation-19016",
    )
    client = _FakeDeepcoinClient(session_factory)
    client.ticker_prices["ETH-USDT-SWAP"] = 2750.0

    auto_process_message_trade_signal(
        session_factory,
        raw_message_id=raw_message_id,
        group_config=_group_config(),
        deepcoin_client=client,
        contract_spec_provider=_StaticContractSpecProvider(),
        processed_at=posted_at + timedelta(seconds=20),
    )

    assert client.orders == []
    assert client.trigger_orders == []
    assert client.protections == []
    with session_factory() as session:
        assert session.query(ExecutionBinding).count() == 0


def test_the_same_strategy_with_a_stop_loss_still_becomes_a_lifecycle(tmp_path):
    """The guard is about the stop, not about the shape of the message."""

    session_factory, raw_message_id, _ = _setup(
        tmp_path, "with-stop", multi_instruction_mode="live"
    )
    payload = _payload_19016()
    payload["strategy"] = dict(payload["strategy"], stop_loss="2900")
    payload["message_classes"] = [{"class": "新策略", "target": None}]
    apply_authoritative_mimo_payload(
        session_factory,
        raw_message_id=raw_message_id,
        payload=payload,
        model="gpt-5.6-luna",
        authoritative_generation="generation-19016",
    )

    candidates, lifecycles, recognition = _counts(session_factory, raw_message_id)
    assert candidates == 1
    assert lifecycles == 1
    assert recognition.status == "是策略"
