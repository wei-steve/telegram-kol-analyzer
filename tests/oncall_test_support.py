"""Shared fixtures for the on-call watcher tests.

The production database is a real temporary SQLite file built from the
application's own metadata, because the watcher reads it with raw SQL: a
mocked schema would prove nothing about the column names and statuses it
actually depends on.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    MessageInstructionItem,
    MessageProcessingJob,
    RawMessage,
    SignalCandidate,
    Source,
    StrategyAlert,
    StrategyLifecycle,
    StrategyManagementBatch,
)


NOW = datetime(2026, 9, 19, 6, 0, 0, tzinfo=UTC)
CHAT_ID = -100123456
GROUP_NAME = "龚有财群"


def naive(moment: datetime) -> datetime:
    """Production stores naive UTC; the fixtures must too."""

    return moment.astimezone(UTC).replace(tzinfo=None)


class ProductionFixture:
    """A minimal but real production database, plus row builders."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.session_factory = create_session_factory(self.path)
        self._message_id = 1000

    # ------------------------------------------------------------- writers

    def add_group_name(self, *, chat_id: int = CHAT_ID, title: str = GROUP_NAME) -> None:
        with self.session_factory() as session:
            session.add(
                StrategyAlert(
                    chat_id=chat_id,
                    message_id=1,
                    chat_title=title,
                    status="forwarded",
                )
            )
            session.commit()

    def add_source(self, *, chat_id: int = CHAT_ID, display_name: str = "峰哥") -> None:
        with self.session_factory() as session:
            session.add(
                Source(chat_id=chat_id, display_name=display_name, is_active=True)
            )
            session.commit()

    def add_raw_message(
        self,
        *,
        text: str = "止损上移到 2484",
        chat_id: int = CHAT_ID,
        posted_at: datetime | None = None,
    ) -> int:
        self._message_id += 1
        with self.session_factory() as session:
            row = RawMessage(
                chat_id=chat_id,
                message_id=self._message_id,
                sender_name="峰哥",
                posted_at=naive(posted_at or NOW - timedelta(minutes=8)),
                text=text,
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_binding(
        self,
        *,
        chat_id: int = CHAT_ID,
        symbol: str = "ETH",
        side: str = "short",
        status: str = "active",
        pos_id: str | None = "pos-1",
        last_exchange_status: str | None = "position_ownership_verified",
        recovered_at: datetime | None = None,
        strategy_instance_id: str | None = None,
        message_id: int | None = None,
    ) -> int:
        self._message_id += 1
        with self.session_factory() as session:
            row = ExecutionBinding(
                strategy_instance_id=strategy_instance_id,
                kol_id="kol-1",
                chat_id=chat_id,
                message_id=message_id if message_id is not None else self._message_id,
                symbol=symbol,
                side=side,
                venue="deepcoin",
                pos_id=pos_id,
                status=status,
                last_exchange_status=last_exchange_status,
                recovered_at=naive(recovered_at or NOW - timedelta(seconds=30)),
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_lifecycle(
        self,
        *,
        chat_id: int = CHAT_ID,
        symbol: str = "ETH",
        side: str = "short",
        execution_binding_id: int | None = None,
    ) -> int:
        self._message_id += 1
        with self.session_factory() as session:
            row = StrategyLifecycle(
                chat_id=chat_id,
                message_id=self._message_id,
                symbol=symbol,
                side=side,
                lifecycle_status="entered",
                signal_at=naive(NOW - timedelta(hours=2)),
                execution_binding_id=execution_binding_id,
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_candidate(
        self,
        *,
        raw_message_id: int,
        management_action: str = "adjust_stop_loss",
        symbol: str = "ETH",
        side: str = "short",
        target_lifecycle_id: int | None = None,
        stop_loss_text: str | None = "2484",
    ) -> int:
        with self.session_factory() as session:
            row = SignalCandidate(
                raw_message_id=raw_message_id,
                symbol=symbol,
                side=side,
                event_type="management",
                target_lifecycle_id=target_lifecycle_id,
                management_action=management_action,
                stop_loss_text=stop_loss_text,
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_instruction_item(
        self,
        *,
        raw_message_id: int,
        signal_candidate_id: int,
        status: str = "failed",
        instruction_kind: str = "management",
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        strategy_instance_id: str | None = None,
        updated_at: datetime | None = None,
        sequence: int = 1,
    ) -> int:
        with self.session_factory() as session:
            row = MessageInstructionItem(
                raw_message_id=raw_message_id,
                signal_candidate_id=signal_candidate_id,
                sequence=sequence,
                instruction_kind=instruction_kind,
                strategy_instance_id=strategy_instance_id,
                idempotency_key=f"item-{raw_message_id}-{signal_candidate_id}-{sequence}",
                status=status,
                result_json=json.dumps(result) if result is not None else None,
                error_json=json.dumps(error) if error is not None else None,
                created_at=naive(updated_at or NOW - timedelta(minutes=8)),
                updated_at=naive(updated_at or NOW - timedelta(minutes=8)),
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def set_item_status(
        self,
        item_id: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        with self.session_factory() as session:
            row = session.get(MessageInstructionItem, int(item_id))
            row.status = status
            row.result_json = json.dumps(result) if result is not None else None
            row.error_json = None
            row.updated_at = naive(updated_at or NOW)
            session.commit()

    def add_management_batch(
        self,
        *,
        raw_message_id: int,
        target_lifecycle_id: int,
        execution_binding_id: int,
        status: str = "blocked",
        reason_code: str | None = None,
        effective_action: str = "adjust_stop_loss",
        updated_at: datetime | None = None,
        strategy_instance_id: str = "strategy-1",
    ) -> int:
        with self.session_factory() as session:
            row = StrategyManagementBatch(
                idempotency_fingerprint=f"fp-{raw_message_id}-{status}-{effective_action}",
                raw_message_id=raw_message_id,
                recognition_decision_id=1,
                recognition_generation="gen-1",
                target_lifecycle_id=target_lifecycle_id,
                strategy_instance_id=strategy_instance_id,
                execution_binding_id=execution_binding_id,
                intent=effective_action,
                effective_action=effective_action,
                status=status,
                reason_code=reason_code,
                target_fingerprint="tf-1",
                planned_at=naive(updated_at or NOW - timedelta(minutes=10)),
                updated_at=naive(updated_at or NOW - timedelta(minutes=10)),
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def set_batch_status(
        self, batch_id: int, *, status: str, updated_at: datetime | None = None
    ) -> None:
        with self.session_factory() as session:
            row = session.get(StrategyManagementBatch, int(batch_id))
            row.status = status
            row.updated_at = naive(updated_at or NOW)
            session.commit()

    def add_processing_job(
        self,
        *,
        raw_message_id: int,
        chat_id: int = CHAT_ID,
        status: str = "pending",
        enqueued_at: datetime | None = None,
    ) -> int:
        with self.session_factory() as session:
            row = MessageProcessingJob(
                raw_message_id=raw_message_id,
                chat_id=chat_id,
                status=status,
                enqueued_at=naive(enqueued_at or NOW - timedelta(minutes=10)),
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def set_job_status(self, job_id: int, *, status: str) -> None:
        with self.session_factory() as session:
            row = session.get(MessageProcessingJob, int(job_id))
            row.status = status
            session.commit()


def build_open_position_case(
    fixture: ProductionFixture,
    *,
    status: str = "failed",
    reason: str = "prior_partial_batch_unresolved",
    management_action: str = "adjust_stop_loss",
    with_binding: bool = True,
    text: str = "ETH 空单止损上移到 2484\n注意风险",
) -> dict[str, int]:
    """The canonical shape: a management instruction over a real position."""

    fixture.add_group_name()
    raw_message_id = fixture.add_raw_message(text=text)
    binding_id = fixture.add_binding() if with_binding else None
    lifecycle_id = fixture.add_lifecycle(execution_binding_id=binding_id)
    candidate_id = fixture.add_candidate(
        raw_message_id=raw_message_id,
        management_action=management_action,
        target_lifecycle_id=lifecycle_id,
    )
    item_id = fixture.add_instruction_item(
        raw_message_id=raw_message_id,
        signal_candidate_id=candidate_id,
        status=status,
        error={"reason": reason} if status in {"failed", "unknown"} else None,
    )
    return {
        "raw_message_id": raw_message_id,
        "binding_id": binding_id or 0,
        "lifecycle_id": lifecycle_id,
        "candidate_id": candidate_id,
        "item_id": item_id,
    }


def sqlite_write_authorizer(recorded: list[str]):
    """Deny (and record) every write action the detector might attempt."""

    write_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_ATTACH,
    }

    def authorizer(action: int, arg1: Any, arg2: Any, arg3: Any, arg4: Any) -> int:
        if action in write_actions:
            recorded.append(f"{action}:{arg1}")
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorizer
