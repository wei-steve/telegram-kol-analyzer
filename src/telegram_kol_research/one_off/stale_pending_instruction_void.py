"""Void the 33 instruction items that were left ``pending`` and never moved.

A-3 task 3, an L3 production data change, approved by the user on 2026-09-07
(``stale_pending_items_void_and_notify: true``) and re-confirmed item by item
before it was applied.

What these rows are: ``message_instruction_items`` created for a recognised
instruction that then stopped. 25 of them are the deferral bug this step fixes
(``deferred / waiting_source_deletion_exit`` with no resume path), 4 were
correctly blocked because their source message was deleted, 3 came from a
``mimo_no_action`` decision on 2026-07-22, and 1 from an expired gap recovery.
Every one of them had ``last_progress_at`` and ``escalation_state`` null: no
process had ever touched them, and none ever would.

They are **voided, never executed late**. The oldest is from 2026-07-22 and the
newest had already sat about seven hours; submitting any of them now would act
on a price and a market that no longer exist.

The id list is frozen at the moment the manifest was shown for approval, and
every write is a compare-and-set against the status the manifest recorded, so
this tool can only ever act on those exact rows in that exact state. Run it
once; a second run reports zero changes rather than touching anything.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.models import (
    MessageInstructionItem,
    StrategyLifecycle,
)


#: Frozen 2026-09-08 from the production manifest reviewed before approval.
STALE_PENDING_ITEM_IDS: tuple[int, ...] = (
    46, 47, 48, 337, 354, 366, 414, 706, 776, 778, 865, 872, 900, 905, 912,
    917, 920, 931, 943, 963, 964, 975, 985, 988, 993, 997, 998, 1001, 1002,
    1008, 1009, 1012, 1014,
)
#: Lifecycles left non-terminal with no ``execution_binding_id`` by those
#: instructions. 1096 is the incident's own (``entered``, entry price recorded,
#: no binding, no execution events); 1107 is the same shape one step earlier
#: (``pending_entry``). Both were confirmed to have zero exchange exposure:
#: no ``execution_bindings``, no ``execution_order_legs``, no
#: ``execution_events``.
UNBOUND_LIFECYCLE_IDS: tuple[int, ...] = (1096, 1107)
#: The only lifecycle states this tool will terminalize, and the terminal state
#: it moves them to -- ``cancelled`` is already one of the project's terminal
#: lifecycle states (``_TERMINAL_LIFECYCLE_STATES``).
VOIDABLE_LIFECYCLE_STATES: tuple[str, ...] = ("entered", "pending_entry")
TERMINAL_LIFECYCLE_STATE = "cancelled"
VOID_REASON = "stale_pending_voided_2026_09_07"
VOID_ERROR_JSON = json.dumps({"reason": VOID_REASON}, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class VoidPlan:
    """Read-only: exactly which frozen rows are still in a voidable state."""

    item_ids: tuple[int, ...] = ()
    lifecycle_ids: tuple[int, ...] = ()
    skipped_item_ids: tuple[int, ...] = ()
    skipped_lifecycle_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class VoidResult:
    voided_item_ids: tuple[int, ...] = ()
    terminalized_lifecycle_ids: tuple[int, ...] = ()
    notifications: tuple[dict[str, object], ...] = field(default=())


def plan_stale_pending_void(
    session_factory: sessionmaker,
    *,
    item_ids: tuple[int, ...] = STALE_PENDING_ITEM_IDS,
    lifecycle_ids: tuple[int, ...] = UNBOUND_LIFECYCLE_IDS,
) -> VoidPlan:
    """Report what would change. Touches nothing."""

    with session_factory() as session:
        voidable_items = [
            int(row_id)
            for (row_id,) in session.query(MessageInstructionItem.id)
            .filter(
                MessageInstructionItem.id.in_(tuple(int(v) for v in item_ids)),
                MessageInstructionItem.status == "pending",
            )
            .order_by(MessageInstructionItem.id.asc())
            .all()
        ]
        voidable_lifecycles = [
            int(row_id)
            for (row_id,) in session.query(StrategyLifecycle.id)
            .filter(
                StrategyLifecycle.id.in_(tuple(int(v) for v in lifecycle_ids)),
                StrategyLifecycle.lifecycle_status.in_(VOIDABLE_LIFECYCLE_STATES),
                StrategyLifecycle.execution_binding_id.is_(None),
            )
            .order_by(StrategyLifecycle.id.asc())
            .all()
        ]
    return VoidPlan(
        item_ids=tuple(voidable_items),
        lifecycle_ids=tuple(voidable_lifecycles),
        skipped_item_ids=tuple(
            sorted(set(int(v) for v in item_ids) - set(voidable_items))
        ),
        skipped_lifecycle_ids=tuple(
            sorted(set(int(v) for v in lifecycle_ids) - set(voidable_lifecycles))
        ),
    )


def build_void_notification(row: dict[str, object]) -> str:
    """One Telegram message per voided item, in the operator's language."""

    return (
        "⚠️ 积压指令已作废（不补执行）\n"
        f"指令项：{row['item_id']}（{row['instruction_kind']}）\n"
        f"消息：raw {row['raw_message_id']}\n"
        f"群：{row['chat_title']}\n"
        f"策略：{row['strategy_instance_id'] or '—'}\n"
        f"发布时间：{row['posted_at']}\n"
        f"原状态：pending，卡住原因 {row['automation_reason'] or '—'}\n"
        f"处置：标记为 failed，理由 {VOID_REASON}。"
        "该指令永不补执行；如仍需操作请人工下单。"
    )


def apply_stale_pending_void(
    session_factory: sessionmaker,
    *,
    now: datetime | None = None,
    item_ids: tuple[int, ...] = STALE_PENDING_ITEM_IDS,
    lifecycle_ids: tuple[int, ...] = UNBOUND_LIFECYCLE_IDS,
) -> VoidResult:
    """Void exactly the frozen rows that are still voidable, and nothing else."""

    moment = now or datetime.now(UTC)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    frozen_items = tuple(int(value) for value in item_ids)
    frozen_lifecycles = tuple(int(value) for value in lifecycle_ids)
    with session_factory() as session:
        items = (
            session.query(MessageInstructionItem)
            .filter(
                MessageInstructionItem.id.in_(frozen_items),
                MessageInstructionItem.status == "pending",
            )
            .order_by(MessageInstructionItem.id.asc())
            .all()
        )
        voided: list[int] = []
        for item in items:
            item.status = "failed"
            item.error_json = VOID_ERROR_JSON
            item.result_json = None
            item.last_progress_at = moment
            item.escalation_state = "expired"
            item.updated_at = moment
            voided.append(int(item.id))
        lifecycles = (
            session.query(StrategyLifecycle)
            .filter(
                StrategyLifecycle.id.in_(frozen_lifecycles),
                StrategyLifecycle.lifecycle_status.in_(VOIDABLE_LIFECYCLE_STATES),
                StrategyLifecycle.execution_binding_id.is_(None),
            )
            .order_by(StrategyLifecycle.id.asc())
            .all()
        )
        terminalized: list[int] = []
        for lifecycle in lifecycles:
            lifecycle.lifecycle_status = TERMINAL_LIFECYCLE_STATE
            lifecycle.exit_reason = VOID_REASON
            lifecycle.management_action = "stale_pending_instruction_voided"
            lifecycle.management_note = (
                "Instruction voided with no exchange exposure; never executed"
            )
            lifecycle.last_checked_at = moment
            lifecycle.updated_at = moment
            terminalized.append(int(lifecycle.id))
        session.commit()
    return VoidResult(
        voided_item_ids=tuple(voided),
        terminalized_lifecycle_ids=tuple(terminalized),
    )
