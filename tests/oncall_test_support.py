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
    ExecutionEvent,
    ExecutionOrderLeg,
    MessageInstructionItem,
    MessageProcessingJob,
    PositionMutationIntent,
    PositionProtectionLedger,
    RawMessage,
    RecognitionDecision,
    RuntimeIncident,
    SignalCandidate,
    Source,
    SourceMessageDeletionExit,
    StrategyAlert,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
    TelegramSourceMessageEvent,
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
        # ``strategy_alerts`` is unique on (chat_id, message_id), so repeated
        # calls have to move the message id along rather than collide.
        self._message_id += 1
        with self.session_factory() as session:
            session.add(
                StrategyAlert(
                    chat_id=chat_id,
                    message_id=self._message_id,
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
        intent: str | None = None,
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
                intent=intent or effective_action,
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

    # ------------------------------------------- phase 2: case-file sources

    def add_recognition_decision(
        self,
        *,
        raw_message_id: int,
        authoritative_status: str = "succeeded",
        automation_status: str | None = "blocked",
        automation_reason: str | None = "management_stop_action_conflict",
        agreement_status: str = "agree",
        payload: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> int:
        with self.session_factory() as session:
            row = RecognitionDecision(
                raw_message_id=raw_message_id,
                input_kind="text",
                authoritative_model="mimo-7b",
                authoritative_status=authoritative_status,
                # The prompt and the model's raw reply live here. The case
                # file must never select this column; a test asserts it.
                authoritative_payload_json=json.dumps(
                    payload or {"secret_prompt": "do not export me"}
                ),
                agreement_status=agreement_status,
                automation_status=automation_status,
                automation_reason=automation_reason,
                prompt_versions_json=json.dumps({"authoritative": "v9"}),
                created_at=naive(updated_at or NOW - timedelta(minutes=30)),
                updated_at=naive(updated_at or NOW - timedelta(minutes=30)),
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def set_recognition_decision(
        self,
        decision_id: int,
        *,
        agreement_status: str | None = None,
        automation_status: str | None = None,
        automation_reason: str | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        with self.session_factory() as session:
            row = session.get(RecognitionDecision, int(decision_id))
            if agreement_status is not None:
                row.agreement_status = agreement_status
            row.automation_status = automation_status
            row.automation_reason = automation_reason
            row.updated_at = naive(updated_at or NOW)
            session.commit()

    def add_order_leg(
        self,
        *,
        execution_binding_id: int,
        pos_id: str = "pos-1",
        purpose: str = "entry",
    ) -> int:
        with self.session_factory() as session:
            row = ExecutionOrderLeg(
                execution_binding_id=execution_binding_id,
                strategy_instance_id="strategy-1",
                leg_index=0,
                purpose=purpose,
                order_kind="limit",
                pos_id=pos_id,
                venue="deepcoin",
                attribution_status="assigned",
                status="filled",
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_management_leg(
        self,
        *,
        management_batch_id: int,
        execution_order_leg_id: int,
        pos_id: str = "pos-1",
        status: str = "planned",
        last_error: str | None = None,
    ) -> int:
        with self.session_factory() as session:
            row = StrategyManagementLeg(
                management_batch_id=management_batch_id,
                execution_order_leg_id=execution_order_leg_id,
                pos_id=pos_id,
                leg_index=0,
                status=status,
                planned_close_size="0.5",
                avg_entry_price="2500",
                last_error=last_error,
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_management_component(
        self,
        *,
        management_batch_id: int,
        component_kind: str = "replace_protection",
        status: str = "blocked",
        reason_code: str | None = "management_stop_action_conflict",
        sequence: int = 1,
    ) -> int:
        with self.session_factory() as session:
            row = StrategyManagementComponent(
                management_batch_id=management_batch_id,
                strategy_management_leg_id=None,
                strategy_management_leg_scope=-1,
                component_kind=component_kind,
                sequence=sequence,
                status=status,
                idempotency_key=f"component-{management_batch_id}-{sequence}",
                desired_json=json.dumps({"stop_price": "2484"}),
                evidence_json="[]",
                reason_code=reason_code,
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_mutation_intent(
        self,
        *,
        execution_binding_id: int,
        execution_order_leg_id: int,
        operation: str = "cancel_protection",
        status: str = "failed",
        pos_id: str = "pos-1",
        sequence: int = 1,
    ) -> int:
        with self.session_factory() as session:
            row = PositionMutationIntent(
                idempotency_key=f"intent-{execution_binding_id}-{sequence}",
                venue="deepcoin",
                operation=operation,
                strategy_instance_id="strategy-1",
                execution_binding_id=execution_binding_id,
                execution_order_leg_id=execution_order_leg_id,
                pos_id=pos_id,
                authority_fingerprint="af-1",
                request_fingerprint=f"rf-{sequence}",
                status=status,
                request_json=json.dumps({"operation": operation}),
                error_json=json.dumps({"reason": "protection_authority_frozen"}),
                reserved_at=naive(NOW - timedelta(minutes=5)),
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_execution_event(
        self,
        *,
        execution_binding_id: int | None = None,
        message_id: int | None = None,
        action: str = "adjust_stop_loss",
        status: str = "failed",
        reason: str | None = "management_stop_action_conflict",
        created_at: datetime | None = None,
        pos_id: str | None = None,
        after: dict[str, Any] | None = None,
    ) -> int:
        with self.session_factory() as session:
            row = ExecutionEvent(
                execution_binding_id=execution_binding_id,
                venue="deepcoin",
                action=action,
                status=status,
                symbol="ETH",
                side="short",
                message_id=message_id,
                pos_id=pos_id,
                reason=reason,
                after_json=(
                    json.dumps(after, ensure_ascii=False, sort_keys=True)
                    if after is not None
                    else None
                ),
                created_at=naive(created_at or NOW - timedelta(minutes=3)),
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_protection_ledger_row(
        self,
        *,
        execution_binding_id: int,
        execution_order_leg_id: int,
        purpose: str = "stop_loss",
        trigger_price: str = "2500",
        status: str = "verified",
        order_id: str = "order-1",
        pos_id: str = "pos-1",
        evidence: dict[str, Any] | None = None,
        updated_at: datetime | None = None,
    ) -> int:
        with self.session_factory() as session:
            row = PositionProtectionLedger(
                venue="deepcoin",
                execution_binding_id=execution_binding_id,
                execution_order_leg_id=execution_order_leg_id,
                strategy_instance_id="strategy-1",
                pos_id=pos_id,
                instrument_id="ETH-USDT-SWAP",
                side="short",
                order_id=order_id,
                purpose=purpose,
                trigger_price=trigger_price,
                size_text="1.0",
                status=status,
                evidence_source="reconcile",
                evidence_json=json.dumps(
                    evidence or {}, ensure_ascii=False, sort_keys=True
                ),
                updated_at=naive(updated_at or NOW),
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def add_runtime_incident(
        self,
        *,
        source_kind: str,
        source_record_id: str,
        incident_type: str = "management_stop_rejected",
        severity: str = "high",
        summary: str = "管理批次被拦下：同仓位有冲突的止损动作。",
        status: str = "pending",
        repeat_count: int = 1,
        first_occurred_at: datetime | None = None,
        last_occurred_at: datetime | None = None,
        notified_at: datetime | None = None,
    ) -> int:
        with self.session_factory() as session:
            row = RuntimeIncident(
                source_kind=source_kind,
                source_record_id=source_record_id,
                incident_type=incident_type,
                severity=severity,
                status=status,
                repeat_count=int(repeat_count),
                fingerprint=f"fp-{source_kind}-{source_record_id}",
                first_occurred_at=naive(
                    first_occurred_at or NOW - timedelta(minutes=20)
                ),
                last_occurred_at=naive(last_occurred_at or NOW - timedelta(minutes=2)),
                notified_at=naive(notified_at) if notified_at is not None else None,
                redacted_summary=summary,
                feature_policy_version="v1",
                prompt_version="v1",
                tool_policy_version="v1",
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def set_runtime_incident(
        self,
        incident_id: int,
        *,
        status: str | None = None,
        last_occurred_at: datetime | None = None,
        notified_at: datetime | None = None,
    ) -> None:
        with self.session_factory() as session:
            row = session.get(RuntimeIncident, int(incident_id))
            if status is not None:
                row.status = status
            if last_occurred_at is not None:
                row.last_occurred_at = naive(last_occurred_at)
            if notified_at is not None:
                row.notified_at = naive(notified_at)
            session.commit()

    # ------------------------------------------- D6a: source deletion exits

    def add_deletion_exit(
        self,
        *,
        raw_message_id: int | None,
        chat_id: int = CHAT_ID,
        message_id: int | None = None,
        state: str = "recovery_required",
        last_reason: str | None = "exit_has_no_known_position",
        execution_binding_id: int | None = None,
        updated_at: datetime | None = None,
    ) -> int:
        """One deletion exit plus the immutable Telegram event it belongs to."""

        self._message_id += 1
        event_message_id = message_id if message_id is not None else self._message_id
        with self.session_factory() as session:
            event = TelegramSourceMessageEvent(
                event_type="message_deleted",
                chat_id=chat_id,
                message_id=event_message_id,
                raw_message_id=raw_message_id,
                event_fingerprint=f"del-{chat_id}-{event_message_id}",
                binding_state="unbound" if raw_message_id is None else "bound",
                telegram_event_json="{}",
                occurred_at=naive(updated_at or NOW - timedelta(hours=12)),
            )
            session.add(event)
            session.commit()
            row = SourceMessageDeletionExit(
                source_event_id=int(event.id),
                raw_message_id=raw_message_id,
                execution_binding_id=execution_binding_id,
                state=state,
                last_reason=last_reason,
                created_at=naive(updated_at or NOW - timedelta(hours=12)),
                updated_at=naive(updated_at or NOW - timedelta(hours=12)),
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def set_deletion_exit_state(
        self, exit_id: int, *, state: str, updated_at: datetime | None = None
    ) -> None:
        with self.session_factory() as session:
            row = session.get(SourceMessageDeletionExit, int(exit_id))
            row.state = state
            row.updated_at = naive(updated_at or NOW)
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


def build_recognition_failure_case(
    fixture: ProductionFixture,
    *,
    agreement_status: str = "authoritative_failed",
    automation_status: str | None = "skipped",
    automation_reason: str | None = "mimo_authoritative_failed",
    with_binding: bool = True,
    text: str = "ETH 这波先减一半，止损拉到成本",
    updated_at: datetime | None = None,
) -> dict[str, int]:
    """Rule D3's canonical shape: a recognition that lost a live position's message."""

    fixture.add_group_name()
    raw_message_id = fixture.add_raw_message(text=text)
    binding_id = fixture.add_binding() if with_binding else None
    decision_id = fixture.add_recognition_decision(
        raw_message_id=raw_message_id,
        authoritative_status="识别失败",
        agreement_status=agreement_status,
        automation_status=automation_status,
        automation_reason=automation_reason,
        updated_at=updated_at,
    )
    return {
        "raw_message_id": raw_message_id,
        "binding_id": binding_id or 0,
        "decision_id": decision_id,
    }


def build_sealed_lane_case(
    fixture: ProductionFixture,
    *,
    chat_id: int = CHAT_ID,
    symbol: str = "BTC",
    side: str = "long",
    state: str = "recovery_required",
    updated_at: datetime | None = None,
    unbound: bool = False,
    with_candidate: bool = True,
) -> dict[str, int]:
    """Rule D6a's canonical shape: 陈哥's BTC-long lane, sealed by exit 310.

    ``unbound=True`` reproduces the 91 rows whose ``raw_message_id`` is NULL and
    which the design says seal nothing, and ``with_candidate=False`` the exit
    whose message never named a symbol and a side -- neither of which the
    barrier's join reaches.
    """

    fixture.add_group_name(chat_id=chat_id)
    deleted_raw_id: int | None = None
    if not unbound:
        deleted_raw_id = fixture.add_raw_message(
            chat_id=chat_id, text=f"{symbol} 现价附近进多，止损 74800"
        )
        if with_candidate:
            fixture.add_candidate(
                raw_message_id=deleted_raw_id,
                management_action="",
                symbol=symbol,
                side=side,
            )
    exit_id = fixture.add_deletion_exit(
        raw_message_id=deleted_raw_id,
        chat_id=chat_id,
        state=state,
        updated_at=updated_at or (NOW - timedelta(hours=12)),
    )
    return {"raw_message_id": deleted_raw_id, "exit_id": exit_id}


def build_voided_message_case(
    fixture: ProductionFixture,
    *,
    chat_id: int = CHAT_ID,
    symbol: str = "BTC",
    side: str = "long",
    automation_reason: str = "deferred_expired",
    text: str = "BTC 83000-83300 进多，止损 80000",
    with_binding: bool = False,
    updated_at: datetime | None = None,
) -> dict[str, int]:
    """Rule D6b's canonical shape: an *entry* the system voided by itself.

    ``with_binding`` defaults to false on purpose. This is the case D3's
    position filter would swallow, and the reason D6b is a separate rule.
    """

    fixture.add_group_name(chat_id=chat_id)
    raw_message_id = fixture.add_raw_message(chat_id=chat_id, text=text)
    fixture.add_candidate(
        raw_message_id=raw_message_id,
        management_action="",
        symbol=symbol,
        side=side,
    )
    if with_binding:
        fixture.add_binding(chat_id=chat_id, symbol=symbol, side=side)
    decision_id = fixture.add_recognition_decision(
        raw_message_id=raw_message_id,
        agreement_status="agree",
        automation_status="deferred",
        automation_reason=automation_reason,
        updated_at=updated_at,
    )
    return {"raw_message_id": raw_message_id, "decision_id": decision_id}


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
