"""Closing the loop on a management instruction whose target nobody could settle.

A-9. A-7 parks such an instruction in ``awaiting_user_confirmation`` and sends
a notification listing the candidates, which was half a conversation: the
operator bot had no way to answer, so every parked instruction stayed parked
and the user had to work around the system by hand.

Three ways out, and no fourth:

* ``/choose <raw_message_id> <n>`` -- the operator names one of the candidates
  they were shown. The choice is re-verified against the exchange before it is
  accepted, because minutes have passed since the question was asked and the
  position may have closed in between.
* ``/dismiss <raw_message_id>`` -- the instruction is abandoned on purpose,
  recorded as ``operator_dismissed`` rather than left to time out, so a
  deliberate decision does not look like neglect later.
* silence -- after ``management_confirmation_timeout_minutes`` the instruction
  fails as ``confirmation_timeout``, with one reminder half an hour before.
  An instruction that has been waiting two hours is stale enough that acting
  on it is its own hazard.

**Nothing here writes to the exchange.** The commands move an instruction item
between states and record why; execution stays with the worker, and every
gate the worker already applies still applies afterwards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram_kol_research.execution_events import (
    ExecutionEventRecord,
    record_execution_event,
)
from telegram_kol_research.management_target_verification import (
    AWAITING_CONFIRMATION,
    load_verified_position_ids,
    verify_lifecycle_targets,
)
from telegram_kol_research.models import (
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)

CHOOSE_COMMAND = "choose"
DISMISS_COMMAND = "dismiss"

#: How long a parked instruction may wait for an answer.
DEFAULT_CONFIRMATION_TIMEOUT_MINUTES = 120

#: How long before the deadline the operator is reminded, once.
REMINDER_LEAD_MINUTES = 30

CHOSEN = "management_target_chosen"
DISMISSED = "management_target_dismissed"
TIMED_OUT = "management_target_confirmation_timeout"
REMINDED = "management_target_confirmation_reminder"

OPERATOR_DISMISSED = "operator_dismissed"
CONFIRMATION_TIMEOUT = "confirmation_timeout"


@dataclass(frozen=True, slots=True)
class ConfirmationOutcome:
    status: str
    message: str
    raw_message_id: int
    item_ids: tuple[int, ...] = ()
    lifecycle_id: int | None = None


def _payload(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _awaiting_items(session, raw_message_id: int) -> list[MessageInstructionItem]:
    return (
        session.query(MessageInstructionItem)
        .filter(
            MessageInstructionItem.raw_message_id == int(raw_message_id),
            MessageInstructionItem.retired_at.is_(None),
            MessageInstructionItem.status == AWAITING_CONFIRMATION,
        )
        .order_by(MessageInstructionItem.sequence, MessageInstructionItem.id)
        .all()
    )


def _already_chosen(session, raw_message_id: int) -> dict[str, Any] | None:
    """The choice a previous ``/choose`` recorded, if there was one.

    Idempotency is by recorded fact, not by a lock: a repeated command finds
    the same choice already on the item and reports it instead of moving the
    item a second time.
    """

    row = (
        session.query(MessageInstructionItem)
        .filter(
            MessageInstructionItem.raw_message_id == int(raw_message_id),
            MessageInstructionItem.retired_at.is_(None),
        )
        .order_by(MessageInstructionItem.sequence, MessageInstructionItem.id)
        .first()
    )
    if row is None:
        return None
    return _payload(row.result_json).get("operator_choice")


def _audit(
    session,
    *,
    raw_message,
    action: str,
    reason: str,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    record_execution_event(
        None,
        ExecutionEventRecord(
            venue="deepcoin",
            action=action,
            status="recorded",
            chat_id=int(raw_message.chat_id) if raw_message is not None else None,
            message_id=int(raw_message.message_id) if raw_message is not None else None,
            source_message_id=int(raw_message.id) if raw_message is not None else None,
            reason=reason,
            before=before,
            after=after,
        ),
        session=session,
    )


def choose_management_target(
    session_factory,
    *,
    raw_message_id: int,
    choice_number: int,
    operator_chat_id: int,
    now: datetime | None = None,
) -> ConfirmationOutcome:
    """Accept the operator's pick, re-verifying it before letting it run."""

    moment = now or datetime.now(UTC)
    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            return ConfirmationOutcome(
                "unknown_message",
                f"找不到消息 {int(raw_message_id)}",
                int(raw_message_id),
            )
        items = _awaiting_items(session, raw_message_id)
        if not items:
            existing = _already_chosen(session, raw_message_id)
            if existing is not None:
                return ConfirmationOutcome(
                    "already_chosen",
                    "该消息已选择过候选 {number}（lifecycle {lifecycle}），未重复处理".format(
                        number=existing.get("number"),
                        lifecycle=existing.get("lifecycle_id"),
                    ),
                    int(raw_message_id),
                    lifecycle_id=existing.get("lifecycle_id"),
                )
            return ConfirmationOutcome(
                "not_awaiting",
                f"消息 {int(raw_message_id)} 没有等待确认的指令项",
                int(raw_message_id),
            )

        offered = _payload(items[0].result_json).get("confirmation_candidates") or []
        chosen = next(
            (
                row
                for row in offered
                if isinstance(row, dict) and int(row.get("number") or 0) == int(choice_number)
            ),
            None,
        )
        if chosen is None:
            return ConfirmationOutcome(
                "unknown_candidate",
                "编号 {number} 不在候选列表里（共 {count} 个）".format(
                    number=int(choice_number), count=len(offered)
                ),
                int(raw_message_id),
            )
        lifecycle_id = chosen.get("lifecycle_id")
        if lifecycle_id is None:
            return ConfirmationOutcome(
                "unknown_candidate",
                f"候选 {int(choice_number)} 没有可用的 lifecycle id",
                int(raw_message_id),
            )
        lifecycle_id = int(lifecycle_id)

        # A-7's own check, run again now: the question was asked minutes ago
        # and the position may have closed since. A stale snapshot answers
        # "unknown", which is a refusal here -- accepting a choice we cannot
        # stand behind is exactly the failure A-7 exists to prevent.
        verdict = verify_lifecycle_targets(
            session,
            [lifecycle_id],
            verified_position_ids=load_verified_position_ids(session, now=moment),
        ).get(lifecycle_id)
        if verdict is None or not verdict.verified:
            reason = verdict.reason if verdict is not None else "unknown_lifecycle"
            _audit(
                session,
                raw_message=raw_message,
                action=CHOSEN,
                reason=f"refused:{reason}",
                before={"awaiting_item_ids": [int(item.id) for item in items]},
                after={
                    "choice_number": int(choice_number),
                    "lifecycle_id": lifecycle_id,
                    "operator_chat_id": int(operator_chat_id),
                    "accepted": False,
                    "reason_code": reason,
                },
            )
            session.commit()
            return ConfirmationOutcome(
                "candidate_not_verifiable",
                "候选 {number}（lifecycle {lifecycle}）已不可用：{reason}。未执行。".format(
                    number=int(choice_number), lifecycle=lifecycle_id, reason=reason
                ),
                int(raw_message_id),
                lifecycle_id=lifecycle_id,
            )

        moved: list[int] = []
        for item in items:
            payload = _payload(item.result_json)
            payload["operator_choice"] = {
                "number": int(choice_number),
                "lifecycle_id": lifecycle_id,
                "operator_chat_id": int(operator_chat_id),
                "chosen_at": moment.isoformat(),
            }
            item.result_json = _dump(payload)
            item.status = "pending"
            item.last_progress_at = moment
            item.updated_at = moment
            moved.append(int(item.id))
            candidate = session.get(SignalCandidate, int(item.signal_candidate_id))
            if candidate is not None:
                # The target the worker resolves from; without this the item
                # would go back to pending pointing at nothing.
                candidate.target_lifecycle_id = lifecycle_id

        _audit(
            session,
            raw_message=raw_message,
            action=CHOSEN,
            reason="operator_choice",
            before={
                "awaiting_item_ids": moved,
                "offered_candidates": offered,
            },
            after={
                "choice_number": int(choice_number),
                "lifecycle_id": lifecycle_id,
                "operator_chat_id": int(operator_chat_id),
                "accepted": True,
                "item_status": "pending",
            },
        )
        session.commit()
        return ConfirmationOutcome(
            "chosen",
            "已选择候选 {number}（lifecycle {lifecycle}），{count} 条指令项转回待执行".format(
                number=int(choice_number), lifecycle=lifecycle_id, count=len(moved)
            ),
            int(raw_message_id),
            tuple(moved),
            lifecycle_id,
        )


def dismiss_management_target(
    session_factory,
    *,
    raw_message_id: int,
    operator_chat_id: int,
    now: datetime | None = None,
) -> ConfirmationOutcome:
    """Abandon the instruction on purpose, and say that is what happened."""

    moment = now or datetime.now(UTC)
    with session_factory() as session:
        raw_message = session.get(RawMessage, int(raw_message_id))
        if raw_message is None:
            return ConfirmationOutcome(
                "unknown_message",
                f"找不到消息 {int(raw_message_id)}",
                int(raw_message_id),
            )
        items = _awaiting_items(session, raw_message_id)
        if not items:
            return ConfirmationOutcome(
                "not_awaiting",
                f"消息 {int(raw_message_id)} 没有等待确认的指令项",
                int(raw_message_id),
            )
        dismissed = _fail_items(
            session,
            items=items,
            reason=OPERATOR_DISMISSED,
            now=moment,
            extra={"operator_chat_id": int(operator_chat_id)},
        )
        _audit(
            session,
            raw_message=raw_message,
            action=DISMISSED,
            reason=OPERATOR_DISMISSED,
            before={"awaiting_item_ids": dismissed},
            after={
                "operator_chat_id": int(operator_chat_id),
                "item_status": "failed",
            },
        )
        session.commit()
        return ConfirmationOutcome(
            "dismissed",
            f"已放弃消息 {int(raw_message_id)} 的 {len(dismissed)} 条指令项",
            int(raw_message_id),
            tuple(dismissed),
        )


def _fail_items(
    session,
    *,
    items: list[MessageInstructionItem],
    reason: str,
    now: datetime,
    extra: dict[str, Any] | None = None,
) -> list[int]:
    failed: list[int] = []
    for item in items:
        error = _payload(item.error_json)
        error["reason"] = reason
        error.update(extra or {})
        item.error_json = _dump(error)
        item.status = "failed"
        item.last_progress_at = now
        item.updated_at = now
        failed.append(int(item.id))
    return failed


def expire_stale_management_confirmations(
    session_factory,
    *,
    now: datetime | None = None,
    timeout_minutes: int = DEFAULT_CONFIRMATION_TIMEOUT_MINUTES,
    notify=None,
) -> dict[str, Any]:
    """Remind once, then fail. Never execute on a question nobody answered."""

    moment = now or datetime.now(UTC)
    naive = moment.replace(tzinfo=None) if moment.tzinfo is not None else moment
    deadline = naive - timedelta(minutes=int(timeout_minutes))
    reminder_at = deadline + timedelta(minutes=REMINDER_LEAD_MINUTES)
    reminded: list[int] = []
    expired: list[int] = []
    with session_factory() as session:
        rows = (
            session.query(MessageInstructionItem)
            .filter(
                MessageInstructionItem.retired_at.is_(None),
                MessageInstructionItem.status == AWAITING_CONFIRMATION,
            )
            .order_by(MessageInstructionItem.raw_message_id, MessageInstructionItem.id)
            .all()
        )
        by_message: dict[int, list[MessageInstructionItem]] = {}
        for row in rows:
            by_message.setdefault(int(row.raw_message_id), []).append(row)
        for raw_message_id, items in by_message.items():
            waiting_since = min(
                (item.last_progress_at or item.updated_at) for item in items
            )
            raw_message = session.get(RawMessage, raw_message_id)
            if waiting_since <= deadline:
                failed = _fail_items(
                    session, items=items, reason=CONFIRMATION_TIMEOUT, now=naive
                )
                expired.extend(failed)
                _audit(
                    session,
                    raw_message=raw_message,
                    action=TIMED_OUT,
                    reason=CONFIRMATION_TIMEOUT,
                    before={"awaiting_item_ids": failed},
                    after={
                        "timeout_minutes": int(timeout_minutes),
                        "item_status": "failed",
                    },
                )
                if notify is not None:
                    notify(
                        raw_message_id=raw_message_id,
                        kind=CONFIRMATION_TIMEOUT,
                        item_ids=tuple(failed),
                    )
                continue
            if waiting_since > reminder_at:
                continue
            # One reminder only: the marker rides on the item, so a sweeper
            # that runs every minute does not send sixty of them.
            already = any(
                _payload(item.result_json).get("confirmation_reminded_at")
                for item in items
            )
            if already:
                continue
            for item in items:
                payload = _payload(item.result_json)
                payload["confirmation_reminded_at"] = naive.isoformat()
                item.result_json = _dump(payload)
                item.updated_at = naive
                reminded.append(int(item.id))
            _audit(
                session,
                raw_message=raw_message,
                action=REMINDED,
                reason="confirmation_reminder",
                before={"awaiting_item_ids": [int(item.id) for item in items]},
                after={"minutes_left": REMINDER_LEAD_MINUTES},
            )
            if notify is not None:
                notify(
                    raw_message_id=raw_message_id,
                    kind="confirmation_reminder",
                    item_ids=tuple(int(item.id) for item in items),
                )
        session.commit()
    return {"reminded": tuple(reminded), "expired": tuple(expired)}
