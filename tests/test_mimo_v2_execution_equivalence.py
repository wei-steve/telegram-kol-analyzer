from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.message_recognition import (
    apply_authoritative_mimo_payload,
)
from telegram_kol_research.mimo_v2_contract import parse_mimo_v2_payload
from telegram_kol_research.mimo_v2_execution_adapter import (
    _execution_projection,
    adapt_mimo_v2_to_current_payload,
)
from telegram_kol_research.models import (
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.trading_settings import save_trading_settings


STRATEGY = {
    "symbol": "ETH",
    "side": "short",
    "entry": "1880-1890",
    "stop_loss": "1940",
    "take_profit": "1800/1750",
    "leverage": "20",
    "order_type": "limit",
}


def _intent(
    kind: str,
    *,
    parameters: dict | None = None,
    lifecycle_id: int | None = 790,
    thread_id: int | None = 52,
) -> dict:
    intent_type = {
        "entry": "new_strategy",
        "cancel_pending_entry": "cancel_entry",
        "replace_entry": "strategy_revision",
        "full_exit": "exit",
        "partial_exit": "exit",
        "partial_take_profit": "position_management",
        "move_stop_to_protect": "position_management",
        "hold_update": "position_management",
    }[kind]
    if kind == "entry":
        lifecycle_id = None
        thread_id = None
    return {
        "intent_type": intent_type,
        "action": {
            "kind": kind,
            "target": {
                "lifecycle_id": lifecycle_id,
                "thread_id": thread_id,
            },
            "strategy": (
                deepcopy(STRATEGY)
                if kind in {"entry", "replace_entry"}
                else None
            ),
            "parameters": parameters or {},
        },
        "reason": f"structured {kind}",
        "confidence": 0.95,
        "evidence_refs": ["text:observed_text"],
    }


def _adapt(*intents: dict):
    parsed = parse_mimo_v2_payload(
        {
            "contract_version": "mimo-authoritative-v2",
            "summary": "authoritative summary",
            "confidence": 0.94,
            "intents": list(intents),
            "evidence": {
                "text": {
                    "observed_text": "source message",
                    "fields": {},
                },
                "images": [],
                "conflicts": [],
            },
        }
    )
    return adapt_mimo_v2_to_current_payload(parsed)


@pytest.mark.parametrize(
    ("kind", "parameters", "event_type", "management_action"),
    (
        ("cancel_pending_entry", {}, "cancel_entry", "cancel_pending_entry"),
        ("full_exit", {"exit_price": "1850"}, "exit_position", "exit_full"),
        (
            "partial_exit",
            {"management_fraction": 0.5},
            "exit_position",
            "exit_partial",
        ),
        (
            "partial_take_profit",
            {"management_fraction": 0.5, "take_profit": "1800"},
            "position_update",
            "partial_take_profit",
        ),
        (
            "move_stop_to_protect",
            {"stop_loss": "1900"},
            "position_update",
            "move_stop_to_protect",
        ),
        (
            "hold_update",
            {"stop_loss": "1900", "take_profit": "1800"},
            "position_update",
            "hold_update",
        ),
    ),
)
def test_v2_management_projection_matches_current_authoritative_contract(
    kind,
    parameters,
    event_type,
    management_action,
):
    adapted = _adapt(_intent(kind, parameters=parameters))
    expected_current_payload = {
        "instructions": [
            {
                "kind": kind,
                "confidence": 0.95,
                "reason": f"legacy {kind} wording",
                "strategy": None,
                "target": {"lifecycle_id": 790, "thread_id": 52},
                "parameters": parameters,
            }
        ],
        "recognition_result": "非策略",
        "strategy": {},
        "lifecycle_event": {
            "event_type": event_type,
            "management_action": management_action,
            "target_lifecycle_id": 790,
            **parameters,
            "confidence": 0.95,
            "reason": f"legacy {kind} wording",
        },
        "confidence": 0.94,
        "input_reading": {"observed_text": "source message"},
    }

    assert _execution_projection(adapted.payload) == _execution_projection(
        expected_current_payload
    )


@pytest.mark.parametrize("kind", ("entry", "replace_entry"))
def test_v2_strategy_projection_matches_current_authoritative_contract(kind):
    adapted = _adapt(_intent(kind))
    expected_current_payload = {
        "instructions": [
            {
                "kind": kind,
                "confidence": 0.95,
                "reason": f"legacy {kind} wording",
                "strategy": deepcopy(STRATEGY),
                "target": (
                    {"lifecycle_id": None, "thread_id": None}
                    if kind == "entry"
                    else {"lifecycle_id": 790, "thread_id": 52}
                ),
                "parameters": {},
            }
        ],
        "recognition_result": "是策略" if kind == "entry" else "非策略",
        "strategy": deepcopy(STRATEGY),
        "lifecycle_event": {
            "event_type": "none",
            "confidence": 0.0,
            "reason": "legacy no lifecycle action",
        },
        "confidence": 0.94,
        "input_reading": {"observed_text": "source message"},
    }

    assert _execution_projection(adapted.payload) == _execution_projection(
        expected_current_payload
    )


def test_v2_supported_multi_action_projection_matches_current_composite_contract():
    adapted = _adapt(
        _intent(
            "partial_take_profit",
            parameters={"management_fraction": 0.5, "take_profit": "1800"},
        ),
        _intent("move_stop_to_protect", parameters={"stop_loss": "1900"}),
    )
    expected_current_payload = {
        "instructions": [
            {
                "kind": "partial_take_profit",
                "confidence": 0.95,
                "reason": "legacy composite wording",
                "strategy": None,
                "target": {"lifecycle_id": 790, "thread_id": 52},
                "parameters": {
                    "management_fraction": 0.5,
                    "take_profit": "1800",
                    "stop_loss": "1900",
                },
            }
        ],
        "recognition_result": "非策略",
        "strategy": {},
        "lifecycle_event": {
            "event_type": "position_update",
            "management_action": (
                "partial_take_profit, move_stop_to_protect"
            ),
            "target_lifecycle_id": 790,
            "management_fraction": 0.5,
            "take_profit": "1800",
            "stop_loss": "1900",
            "confidence": 0.95,
            "reason": "legacy composite wording",
        },
        "confidence": 0.94,
        "input_reading": {"observed_text": "source message"},
    }

    assert _execution_projection(adapted.payload) == _execution_projection(
        expected_current_payload
    )


def _seed_execution_projection_database(path, *, management: bool):
    session_factory = create_session_factory(path)
    save_trading_settings(
        session_factory,
        {
            "multi_instruction_mode": "live",
            "multi_instruction_activation_after_raw_message_id": 0,
        },
    )
    with session_factory() as session:
        raw = RawMessage(
            chat_id=88,
            message_id=900 if management else 901,
            text="move the stop" if management else "ETH short strategy",
        )
        session.add(raw)
        lifecycle = None
        if management:
            lifecycle = StrategyLifecycle(
                chat_id=88,
                message_id=899,
                symbol="ETH",
                side="short",
                lifecycle_status="entered",
                signal_at=datetime(2026, 8, 11, 1, 0, tzinfo=UTC),
                entered_at=datetime(2026, 8, 11, 1, 1, tzinfo=UTC),
            )
            session.add(lifecycle)
        session.commit()
        return session_factory, raw.id, lifecycle.id if lifecycle else None


def _execution_rows_snapshot(session_factory, raw_message_id):
    with session_factory() as session:
        candidates = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.raw_message_id == raw_message_id)
            .order_by(SignalCandidate.id)
            .all()
        )
        items = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.raw_message_id == raw_message_id)
            .order_by(MessageInstructionItem.sequence, MessageInstructionItem.id)
            .all()
        )
        lifecycles = (
            session.query(StrategyLifecycle)
            .order_by(StrategyLifecycle.id)
            .all()
        )
        return {
            "candidates": [
                {
                    "symbol": row.symbol,
                    "side": row.side,
                    "entry": row.entry_text,
                    "stop_loss": row.stop_loss_text,
                    "take_profit": row.take_profit_text,
                    "event_type": row.event_type,
                    "management_action": row.management_action,
                    "target_lifecycle_id": row.target_lifecycle_id,
                    "review_status": row.review_status,
                    "parse_source": row.parse_source,
                }
                for row in candidates
            ],
            "items": [
                {
                    "sequence": row.sequence,
                    "instruction_kind": row.instruction_kind,
                    "status": row.status,
                    "retired": row.retired_at is not None,
                }
                for row in items
            ],
            "lifecycles": [
                {
                    "symbol": row.symbol,
                    "side": row.side,
                    "status": row.lifecycle_status,
                }
                for row in lifecycles
            ],
        }


@pytest.mark.parametrize("kind", ("entry", "move_stop_to_protect"))
def test_v2_and_current_payload_create_identical_execution_rows(
    tmp_path,
    kind,
):
    management = kind == "move_stop_to_protect"
    v1_factory, v1_raw_id, v1_lifecycle_id = _seed_execution_projection_database(
        tmp_path / f"{kind}-v1.db",
        management=management,
    )
    v2_factory, v2_raw_id, v2_lifecycle_id = _seed_execution_projection_database(
        tmp_path / f"{kind}-v2.db",
        management=management,
    )
    parameters = {"stop_loss": "1900"} if management else {}
    v2_intent = _intent(
        kind,
        parameters=parameters,
        lifecycle_id=v2_lifecycle_id,
        thread_id=None,
    )
    v2_payload = _adapt(v2_intent).payload
    v1_payload = deepcopy(v2_payload)
    if management:
        v1_payload["instructions"][0]["target"]["lifecycle_id"] = v1_lifecycle_id
        v1_payload["lifecycle_event"]["target_lifecycle_id"] = v1_lifecycle_id

    apply_authoritative_mimo_payload(
        v1_factory,
        raw_message_id=v1_raw_id,
        payload=v1_payload,
        model="mimo-v2.5",
        authoritative_generation="current-contract",
    )
    apply_authoritative_mimo_payload(
        v2_factory,
        raw_message_id=v2_raw_id,
        payload=v2_payload,
        model="mimo-v2.5",
        authoritative_generation="v2-adapter",
    )

    assert _execution_rows_snapshot(
        v2_factory,
        v2_raw_id,
    ) == _execution_rows_snapshot(v1_factory, v1_raw_id)
