"""Read-only strategy record projections for the operator web UI."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
import hashlib
import json
import re
from types import MappingProxyType
from typing import Iterable
from urllib.parse import urlencode

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import aliased

from telegram_kol_research.position_attribution import TERMINAL_ENTRY_LEG_STATES
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    MediaAsset,
    MessageRecognition,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
    StrategyManagementBatch,
    StrategyManagementLeg,
)


ATTENTION_SEVERITY_RANK = MappingProxyType(
    {"critical": 0, "warning": 1, "review": 2}
)
FAILED_EXECUTION_STATUSES = frozenset({"failed", "rejected", "error"})
LIVE_BINDING_STATUSES = frozenset({"open", "active"})
SAFE_MANAGEMENT_STATUSES = frozenset({"succeeded", "blocked", "resolved"})
AUTHORITATIVE_CANDIDATE_PARSE_SOURCES = frozenset({"mimo_authoritative"})
FINISHED_LIFECYCLE_STATUSES = frozenset(
    {"cancelled", "exited", "expired", "finished", "invalidated", "rejected"}
)

ATTENTION_LABELS = MappingProxyType(
    {
        "recognition_failed": ("critical", "AI识别失败"),
        "recognition_evidence_missing": ("critical", "AI权威识别证据缺失"),
        "recognition_disagreement": ("review", "AI识别存在关键分歧"),
        "entered_without_binding": ("critical", "策略已入场但没有唯一真实仓位"),
        "missing_stop": ("critical", "真实持仓缺少止损"),
        "execution_failed": ("critical", "交易执行失败"),
        "management_unconfirmed": ("warning", "仓位管理尚未确认交易所结果"),
        "exchange_unavailable": ("warning", "Deepcoin 当前状态不可用"),
        "unattributed_position": ("critical", "交易所仓位未归属策略"),
        "attribution_ambiguous": ("warning", "交易所仓位归属不唯一"),
        "attribution_conflict": ("critical", "交易所仓位归属证据冲突"),
        "protection_mismatch": ("critical", "交易所保护证据与策略不一致"),
        "management_blocked": ("warning", "仓位管理已阻断待处理"),
        "management_execution_drift": ("critical", "仓位管理与交易所结果漂移"),
        "position_missing": ("critical", "本地实盘绑定在交易所快照中缺失"),
        "binding_without_lifecycle": ("critical", "实盘绑定缺少策略生命周期"),
    }
)

_FAILED_RECOGNITION_STATUSES = frozenset(
    {"failed", "failure", "error", "识别失败"}
)
_NORMAL_DISAGREEMENT_SEVERITIES = frozenset({"", "none", "normal"})
ACTIONABLE_MANAGEMENT_BLOCK_REASONS = frozenset(
    {
        "protection_ambiguous_global_assignment",
        "protection_missing_cancellable_order_id",
        "protection_price_or_size_mismatch",
        "protection_ledger_stale",
        "protection_evidence_unavailable",
        "target_protection_evidence_unavailable",
        "target_protection_not_verified",
        "target_protection_order_identity_unavailable",
    }
)
_TIMELINE_KIND_RANK = MappingProxyType(
    {
        "message": 0,
        "recognition": 1,
        "strategy": 2,
        "order": 3,
        "fill": 4,
        "execution": 5,
        "management": 6,
    }
)
_MESSAGE_ROLE_RANK = MappingProxyType({"entry": 0, "management": 1, "exit": 2})
_SECRET_FIELD_NAMES = frozenset(
    {
        "apikey",
        "apisecret",
        "authorization",
        "password",
        "passphrase",
        "secret",
        "signature",
        "token",
        "dcaccesskey",
        "dcaccesssign",
        "dcaccesspassphrase",
    }
)
_SAFE_ERROR_METADATA_FIELD_NAMES = frozenset(
    {
        "code",
        "httpstatus",
        "httpstatuscode",
        "reasoncode",
        "status",
        "statuscode",
        "type",
    }
)
_ERROR_PROSE_FIELD_NAMES = frozenset(
    {
        "description",
        "detail",
        "error",
        "errormessage",
        "exception",
        "lasterror",
        "message",
        "reason",
    }
)


def load_strategy_record_detail(
    session_factory,
    *,
    lifecycle_id: int,
    group_labels_by_chat_id: dict[int, str],
) -> dict[str, object] | None:
    """Build a read-only evidence chain from persisted strategy records.

    Exchange evidence is deliberately never fetched here.  Callers must enrich
    it from an already captured snapshot so an unavailable exchange cannot be
    misrepresented as an empty position.
    """

    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, lifecycle_id)
        if lifecycle is None:
            return None

        candidate_ids = {
            int(value)
            for value in (
                lifecycle.signal_candidate_id,
                lifecycle.exit_signal_candidate_id,
            )
            if value is not None
        }
        candidates = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.id.in_(candidate_ids))
            .all()
            if candidate_ids
            else []
        )
        candidates_by_id = {int(row.id): row for row in candidates}
        entry_candidate = candidates_by_id.get(int(lifecycle.signal_candidate_id or 0))
        exit_candidate = candidates_by_id.get(
            int(lifecycle.exit_signal_candidate_id or 0)
        )

        binding = (
            session.get(ExecutionBinding, int(lifecycle.execution_binding_id))
            if lifecycle.execution_binding_id is not None
            else None
        )
        binding_id = int(binding.id) if binding is not None else None
        strategy_instance_id = (
            str(binding.strategy_instance_id)
            if binding is not None and binding.strategy_instance_id
            else None
        )
        legacy_strategy_instance_is_unique = bool(
            strategy_instance_id
            and session.query(func.count(ExecutionBinding.id))
            .filter(ExecutionBinding.strategy_instance_id == strategy_instance_id)
            .scalar()
            == 1
        )

        order_legs = (
            session.query(ExecutionOrderLeg)
            .filter(ExecutionOrderLeg.execution_binding_id == binding_id)
            .order_by(ExecutionOrderLeg.created_at, ExecutionOrderLeg.id)
            .all()
            if binding_id is not None
            else []
        )
        event_predicates = []
        if binding_id is not None:
            event_predicates.append(ExecutionEvent.execution_binding_id == binding_id)
        if strategy_instance_id and legacy_strategy_instance_is_unique:
            event_predicates.append(
                and_(
                    ExecutionEvent.execution_binding_id.is_(None),
                    ExecutionEvent.strategy_instance_id == strategy_instance_id,
                )
            )
        execution_events = (
            session.query(ExecutionEvent)
            .filter(or_(*event_predicates))
            .order_by(ExecutionEvent.created_at, ExecutionEvent.id)
            .all()
            if event_predicates
            else []
        )
        management_batches = (
            session.query(StrategyManagementBatch)
            .filter(StrategyManagementBatch.target_lifecycle_id == lifecycle.id)
            .order_by(StrategyManagementBatch.planned_at, StrategyManagementBatch.id)
            .all()
        )
        management_batch_ids = {int(row.id) for row in management_batches}
        management_legs = (
            session.query(StrategyManagementLeg)
            .filter(
                StrategyManagementLeg.management_batch_id.in_(management_batch_ids)
            )
            .order_by(
                StrategyManagementLeg.management_batch_id,
                StrategyManagementLeg.leg_index,
                StrategyManagementLeg.id,
            )
            .all()
            if management_batch_ids
            else []
        )

        lifecycle_entry_message = (
            session.query(RawMessage)
            .filter(
                RawMessage.chat_id == lifecycle.chat_id,
                RawMessage.message_id == lifecycle.message_id,
            )
            .one_or_none()
        )
        if entry_candidate is None and lifecycle_entry_message is not None:
            fallback_entry_candidate = (
                session.query(SignalCandidate)
                .filter(SignalCandidate.raw_message_id == lifecycle_entry_message.id)
                .order_by(
                    case(
                        (SignalCandidate.target_lifecycle_id == lifecycle.id, 0),
                        else_=1,
                    ),
                    case(
                        (
                            func.lower(func.coalesce(SignalCandidate.parse_source, ""))
                            .in_(AUTHORITATIVE_CANDIDATE_PARSE_SOURCES),
                            0,
                        ),
                        else_=1,
                    ),
                    case(
                        (
                            func.lower(func.coalesce(SignalCandidate.symbol, ""))
                            == str(lifecycle.symbol or "").lower(),
                            0,
                        ),
                        else_=1,
                    ),
                    case(
                        (
                            func.lower(func.coalesce(SignalCandidate.side, ""))
                            == str(lifecycle.side or "").lower(),
                            0,
                        ),
                        else_=1,
                    ),
                    case((SignalCandidate.event_type == "entry_signal", 0), else_=1),
                    SignalCandidate.id.desc(),
                )
                .first()
            )
            if fallback_entry_candidate is not None:
                entry_candidate = fallback_entry_candidate
                candidates.append(fallback_entry_candidate)
                candidates_by_id[int(fallback_entry_candidate.id)] = (
                    fallback_entry_candidate
                )
        raw_message_ids = {
            int(row.raw_message_id) for row in candidates
        } | {int(row.raw_message_id) for row in management_batches}
        if lifecycle_entry_message is not None:
            raw_message_ids.add(int(lifecycle_entry_message.id))
        raw_messages = (
            session.query(RawMessage)
            .filter(RawMessage.id.in_(raw_message_ids))
            .order_by(RawMessage.posted_at, RawMessage.id)
            .all()
            if raw_message_ids
            else []
        )
        raw_messages_by_id = {int(row.id): row for row in raw_messages}
        entry_raw_message = (
            raw_messages_by_id.get(int(entry_candidate.raw_message_id))
            if entry_candidate is not None
            else lifecycle_entry_message
        )
        if entry_raw_message is None:
            entry_raw_message = lifecycle_entry_message

        media = (
            session.query(MediaAsset)
            .filter(MediaAsset.raw_message_id.in_(raw_message_ids))
            .order_by(MediaAsset.created_at, MediaAsset.id)
            .all()
            if raw_message_ids
            else []
        )
        management_decision_ids = {
            int(row.recognition_decision_id) for row in management_batches
        }
        decision_predicates = []
        if raw_message_ids:
            decision_predicates.append(
                RecognitionDecision.raw_message_id.in_(raw_message_ids)
            )
        if management_decision_ids:
            decision_predicates.append(
                RecognitionDecision.id.in_(management_decision_ids)
            )
        decisions = (
            session.query(RecognitionDecision)
            .filter(or_(*decision_predicates))
            .all()
            if decision_predicates
            else []
        )
        decisions_by_raw_message_id = {
            int(row.raw_message_id): row for row in decisions
        }
        decisions_by_id = {int(row.id): row for row in decisions}
        entry_recognition_raw_message_id = (
            int(entry_candidate.raw_message_id)
            if entry_candidate is not None
            else (
                int(entry_raw_message.id) if entry_raw_message is not None else None
            )
        )
        authoritative_decision = (
            decisions_by_raw_message_id.get(entry_recognition_raw_message_id)
            if entry_recognition_raw_message_id is not None
            else None
        )

    legs_by_batch_id: dict[int, list[StrategyManagementLeg]] = defaultdict(list)
    for leg in management_legs:
        legs_by_batch_id[int(leg.management_batch_id)].append(leg)

    missing: list[str] = []
    if entry_candidate is None:
        missing.append("signal_candidate")
    if entry_raw_message is None:
        missing.append("raw_message")
    if authoritative_decision is None:
        missing.append("recognition_decision")
    if binding is None:
        missing.append("execution_binding")
    missing.append("exchange_snapshot")

    role_evidence: list[dict[str, object]] = []
    if (
        entry_raw_message is not None
        or entry_candidate is not None
        or authoritative_decision is not None
    ):
        role_evidence.append(
            {
                "role": "entry",
                "raw_message": entry_raw_message,
                "decision": authoritative_decision,
                "context_table": "strategy_lifecycles",
                "context_id": int(lifecycle.id),
            }
        )
    for batch in management_batches:
        management_raw_message = raw_messages_by_id.get(int(batch.raw_message_id))
        if management_raw_message is not None:
            role_evidence.append(
                {
                    "role": "management",
                    "raw_message": management_raw_message,
                    "decision": decisions_by_id.get(int(batch.recognition_decision_id)),
                    "context_table": "strategy_management_batches",
                    "context_id": int(batch.id),
                }
            )
    if exit_candidate is not None:
        exit_raw_message = raw_messages_by_id.get(int(exit_candidate.raw_message_id))
        if exit_raw_message is not None:
            role_evidence.append(
                {
                    "role": "exit",
                    "raw_message": exit_raw_message,
                    "decision": decisions_by_raw_message_id.get(
                        int(exit_candidate.raw_message_id)
                    ),
                    "context_table": "signal_candidates",
                    "context_id": int(exit_candidate.id),
                }
            )
    role_evidence.sort(
        key=lambda item: (
            _MESSAGE_ROLE_RANK.get(str(item["role"]), 99),
            int(item["context_id"]),
        )
    )

    timeline: list[dict[str, object]] = []
    for role_item in role_evidence:
        raw_message = role_item["raw_message"]
        if raw_message is None:
            continue
        role = str(role_item["role"])
        context_table = str(role_item["context_table"])
        context_id = int(role_item["context_id"])
        timeline.append(
            _detail_timeline_item(
                kind="message",
                timestamp=raw_message.posted_at or raw_message.created_at,
                database_id=int(raw_message.id),
                role=role,
                event_id=f"message:{role}:{context_table}:{context_id}:{int(raw_message.id)}",
                source={
                    "table": "raw_messages",
                    "id": int(raw_message.id),
                    "raw_message_id": int(raw_message.id),
                    "chat_id": int(raw_message.chat_id),
                    "message_id": int(raw_message.message_id),
                    "role": role,
                    "context_table": context_table,
                    "context_id": context_id,
                },
                status="received",
            )
        )
    for role_item in role_evidence:
        role_decision = role_item["decision"]
        if role_decision is None:
            continue
        role = str(role_item["role"])
        context_table = str(role_item["context_table"])
        context_id = int(role_item["context_id"])
        timeline.append(
            _detail_timeline_item(
                kind="recognition",
                timestamp=role_decision.updated_at,
                database_id=int(role_decision.id),
                role=role,
                event_id=(
                    f"recognition:{role}:{context_table}:{context_id}:"
                    f"{int(role_decision.id)}"
                ),
                source={
                    "table": "recognition_decisions",
                    "id": int(role_decision.id),
                    "raw_message_id": int(role_decision.raw_message_id),
                    "role": role,
                    "context_table": context_table,
                    "context_id": context_id,
                },
                status=str(role_decision.authoritative_status),
            )
        )
    timeline.append(
        _detail_timeline_item(
            kind="strategy",
            timestamp=lifecycle.signal_at,
            database_id=int(lifecycle.id),
            event_id=f"strategy:{int(lifecycle.id)}",
            source={"table": "strategy_lifecycles", "id": int(lifecycle.id)},
            status=str(lifecycle.lifecycle_status),
        )
    )
    for leg in order_legs:
        timeline.append(
            _detail_timeline_item(
                kind="order",
                timestamp=leg.created_at,
                database_id=int(leg.id),
                event_id=f"order:{int(leg.id)}",
                source={
                    "table": "execution_order_legs",
                    "id": int(leg.id),
                    "execution_binding_id": int(leg.execution_binding_id),
                    "purpose": str(leg.purpose),
                },
                status=str(leg.status),
            )
        )
    for execution_event in execution_events:
        event_kind = (
            "fill"
            if str(execution_event.action or "").lower()
            in {"fill", "filled", "position_filled", "entry_filled"}
            else "execution"
        )
        timeline.append(
            _detail_timeline_item(
                kind=event_kind,
                timestamp=(
                    execution_event.exchange_event_time
                    or execution_event.created_at
                ),
                database_id=int(execution_event.id),
                event_id=f"{event_kind}:{int(execution_event.id)}",
                source={
                    "table": "execution_events",
                    "id": int(execution_event.id),
                    "execution_binding_id": execution_event.execution_binding_id,
                    "action": str(execution_event.action),
                    "source_message_id": execution_event.source_message_id,
                },
                status=str(execution_event.status),
            )
        )
    for batch in management_batches:
        timeline.append(
            _detail_timeline_item(
                kind="management",
                timestamp=batch.planned_at,
                database_id=int(batch.id),
                role="management",
                event_id=f"management:{int(batch.id)}",
                source={
                    "table": "strategy_management_batches",
                    "id": int(batch.id),
                    "raw_message_id": int(batch.raw_message_id),
                    "recognition_decision_id": int(batch.recognition_decision_id),
                    "role": "management",
                },
                status=str(batch.status),
            )
        )
    timeline.sort(
        key=lambda item: (
            _timestamp_value(item["timestamp"]),
            _TIMELINE_KIND_RANK.get(str(item["kind"]), 99),
            _MESSAGE_ROLE_RANK.get(str(item.get("role") or ""), 99),
            int(item["database_id"]),
            str(item["event_id"]),
        )
    )

    raw_message_evidence = (
        _raw_message_detail(entry_raw_message) if entry_raw_message is not None else None
    )
    recognition_evidence = (
        _recognition_decision_detail(authoritative_decision)
        if authoritative_decision is not None
        else None
    )
    role_message_evidence = [
        _role_message_detail(
            role=str(item["role"]),
            raw_message=item["raw_message"],
            context_table=str(item["context_table"]),
            context_id=int(item["context_id"]),
        )
        for item in role_evidence
        if item["raw_message"] is not None
    ]
    role_recognition_evidence = [
        _role_recognition_decision_detail(
            role=str(item["role"]),
            decision=item["decision"],
            context_table=str(item["context_table"]),
            context_id=int(item["context_id"]),
        )
        for item in role_evidence
        if item["decision"] is not None
    ]
    return {
        "identity": {
            "lifecycle_id": int(lifecycle.id),
            "chat_id": int(lifecycle.chat_id),
            "message_id": int(lifecycle.message_id),
            "group_name": group_labels_by_chat_id.get(
                int(lifecycle.chat_id), str(lifecycle.chat_id)
            ),
            "symbol": lifecycle.symbol,
            "side": lifecycle.side,
        },
        "overview": {
            "lifecycle_status": lifecycle.lifecycle_status,
            "recognition_evidence_state": (
                "present" if authoritative_decision is not None else "missing"
            ),
            "authoritative_model": (
                authoritative_decision.authoritative_model
                if authoritative_decision is not None
                else None
            ),
            "authoritative_status": (
                authoritative_decision.authoritative_status
                if authoritative_decision is not None
                else "unknown"
            ),
            "agreement_status": (
                authoritative_decision.agreement_status
                if authoritative_decision is not None
                else "unknown"
            ),
            "entry_range_low": lifecycle.entry_range_low,
            "entry_range_high": lifecycle.entry_range_high,
            "stop_loss": lifecycle.stop_loss,
            "take_profit": _safe_json_value(lifecycle.take_profit),
            "management_signal_message_id": lifecycle.management_signal_message_id,
            "management_action": lifecycle.management_action,
        },
        "timeline": timeline,
        "execution": {
            "binding": _binding_detail(binding) if binding is not None else None,
            "order_legs": [_order_leg_detail(row) for row in order_legs],
            "position_ids": _verified_active_entry_leg_position_ids(order_legs),
            "position_ids_authoritative": any(
                row.purpose == "entry" for row in order_legs
            ),
            "events": [_execution_event_detail(row) for row in execution_events],
            "management_batches": [
                _management_batch_detail(
                    row,
                    legs=legs_by_batch_id.get(int(row.id), []),
                )
                for row in management_batches
            ],
            "management_confirmations": [
                _management_confirmation_summary(
                    row,
                    legs=legs_by_batch_id.get(int(row.id), []),
                    message_id=(
                        int(raw_messages_by_id[int(row.raw_message_id)].message_id)
                        if int(row.raw_message_id) in raw_messages_by_id
                        else None
                    ),
                )
                for row in management_batches
            ],
            "exchange_evidence": {
                "state": "missing",
                "reason": "未提供交易所快照；详情加载器不会主动调用交易所",
            },
        },
        "evidence": {
            "raw_message": raw_message_evidence,
            "messages": role_message_evidence,
            "media": [_media_detail(row) for row in media],
            "signal_candidate": (
                _candidate_detail(entry_candidate)
                if entry_candidate is not None
                else None
            ),
            "exit_signal_candidate": (
                _candidate_detail(exit_candidate) if exit_candidate is not None else None
            ),
            "recognition_decision": recognition_evidence,
            "recognition_decisions": role_recognition_evidence,
            "missing": missing,
        },
    }


def _detail_timeline_item(
    *,
    kind: str,
    timestamp: datetime | None,
    database_id: int,
    event_id: str,
    source: dict[str, object],
    status: str,
    role: str | None = None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "kind": kind,
        "timestamp": _as_utc(timestamp),
        "database_id": database_id,
        "event_id": event_id,
        "source": source,
        "status": status,
    }
    if role is not None:
        item["role"] = role
    return item


def _safe_json_value(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        return _redact_secret_fields(value)
    if not value.strip():
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as exc:
        return {
            "_parse_error": True,
            "error": type(exc).__name__,
            "raw_length": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }
    return _redact_secret_fields(parsed)


def _safe_error_detail(value: object) -> object:
    """Expose structured error metadata without copying exception prose."""

    if value is None:
        return None
    if not isinstance(value, str):
        return _redact_secret_fields(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, (dict, list)):
        return _sanitize_error_value(parsed)
    return _redacted_text_metadata(value)


def _redacted_text_metadata(value: object) -> dict[str, object]:
    if isinstance(value, str):
        serialized = value
    else:
        try:
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            serialized = type(value).__name__
    return {
        "_redacted_error": True,
        "raw_length": len(serialized),
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    }


def _sanitize_error_value(value: object, *, field_name: str | None = None) -> object:
    normalized_field = (
        "".join(character for character in field_name.lower() if character.isalnum())
        if field_name is not None
        else ""
    )
    if normalized_field in _SECRET_FIELD_NAMES:
        return "[REDACTED]"
    if normalized_field in _ERROR_PROSE_FIELD_NAMES:
        return _redacted_text_metadata(value)
    if isinstance(value, dict):
        return {
            str(key): _sanitize_error_value(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_error_value(item) for item in value]
    if normalized_field in _SAFE_ERROR_METADATA_FIELD_NAMES:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str) and 0 < len(value) <= 80 and all(
            character.isascii()
            and (character.isalnum() or character in {"_", "-", "."})
            for character in value
        ):
            return value
        return _redacted_text_metadata(value)
    if isinstance(value, str):
        return _redacted_text_metadata(value)
    return value


def _redact_secret_fields(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = "".join(character for character in str(key).lower() if character.isalnum())
            redacted[str(key)] = (
                "[REDACTED]"
                if normalized_key in _SECRET_FIELD_NAMES
                else _redact_secret_fields(item)
            )
        return redacted
    if isinstance(value, list):
        return [_redact_secret_fields(item) for item in value]
    return value


def _raw_message_detail(row: RawMessage) -> dict[str, object]:
    return {
        "id": int(row.id),
        "chat_id": int(row.chat_id),
        "message_id": int(row.message_id),
        "sender_id": row.sender_id,
        "sender_name": row.sender_name,
        "posted_at": _as_utc(row.posted_at),
        "text": row.text,
        "raw_payload": _safe_json_value(row.raw_payload),
        "reply_to_message_id": row.reply_to_message_id,
    }


def _role_message_detail(
    *,
    role: str,
    raw_message: RawMessage,
    context_table: str,
    context_id: int,
) -> dict[str, object]:
    detail = _raw_message_detail(raw_message)
    detail.update(
        {
            "role": role,
            "source": {
                "raw_message_id": int(raw_message.id),
                "context_table": context_table,
                "context_id": context_id,
            },
        }
    )
    return detail


def _media_detail(row: MediaAsset) -> dict[str, object]:
    return {
        "id": int(row.id),
        "raw_message_id": int(row.raw_message_id),
        "telegram_file_id": row.telegram_file_id,
        "kind": row.kind,
        "mime_type": row.mime_type,
        "local_path": row.local_path,
        "ocr_text": row.ocr_text,
    }


def _candidate_detail(row: SignalCandidate) -> dict[str, object]:
    return {
        "id": int(row.id),
        "raw_message_id": int(row.raw_message_id),
        "event_type": row.event_type,
        "target_lifecycle_id": row.target_lifecycle_id,
        "symbol": row.symbol,
        "side": row.side,
        "entry_text": row.entry_text,
        "stop_loss_text": row.stop_loss_text,
        "take_profit_text": row.take_profit_text,
        "review_status": row.review_status,
        "review_note": row.review_note,
    }


def _recognition_decision_detail(row: RecognitionDecision) -> dict[str, object]:
    return {
        "id": int(row.id),
        "raw_message_id": int(row.raw_message_id),
        "authoritative_model": row.authoritative_model,
        "authoritative_status": row.authoritative_status,
        "authoritative_payload": _safe_json_value(row.authoritative_payload_json),
        "auxiliary_model": row.auxiliary_model,
        "auxiliary_status": row.auxiliary_status,
        "auxiliary_payload": _safe_json_value(row.auxiliary_payload_json),
        "agreement_status": row.agreement_status,
        "differences": _safe_json_value(row.differences_json),
        "prompt_versions": _safe_json_value(row.prompt_versions_json),
        "automation_status": row.automation_status,
        "automation_reason": row.automation_reason,
    }


def _role_recognition_decision_detail(
    *,
    role: str,
    decision: RecognitionDecision,
    context_table: str,
    context_id: int,
) -> dict[str, object]:
    detail = _recognition_decision_detail(decision)
    detail.update(
        {
            "role": role,
            "source": {
                "recognition_decision_id": int(decision.id),
                "raw_message_id": int(decision.raw_message_id),
                "context_table": context_table,
                "context_id": context_id,
            },
        }
    )
    return detail


def _binding_detail(row: ExecutionBinding) -> dict[str, object]:
    return {
        "id": int(row.id),
        "strategy_instance_id": row.strategy_instance_id,
        "chat_id": int(row.chat_id),
        "message_id": int(row.message_id),
        "symbol": row.symbol,
        "side": row.side,
        "venue": row.venue,
        "order_id": row.order_id,
        "client_order_id": row.client_order_id,
        "pos_id": row.pos_id,
        "status": row.status,
        "last_exchange_status": row.last_exchange_status,
        "payload": _safe_json_value(row.payload_json),
    }


def _order_leg_detail(row: ExecutionOrderLeg) -> dict[str, object]:
    return {
        "id": int(row.id),
        "execution_binding_id": int(row.execution_binding_id),
        "purpose": row.purpose,
        "leg_index": int(row.leg_index),
        "order_kind": row.order_kind,
        "order_id": row.order_id,
        "client_order_id": row.client_order_id,
        "pos_id": row.pos_id,
        "attribution_status": row.attribution_status,
        "attribution_evidence": _safe_json_value(row.attribution_evidence_json),
        "status": row.status,
        "request": _safe_json_value(row.request_json),
        "response": _safe_json_value(row.response_json),
    }


def _verified_active_entry_leg_position_ids(
    rows: Iterable[ExecutionOrderLeg],
) -> list[str]:
    pos_ids: list[str] = []
    for row in rows:
        pos_id = str(row.pos_id or "").strip()
        if (
            row.purpose == "entry"
            and row.attribution_status == "verified"
            and str(row.status or "").strip().lower() not in TERMINAL_ENTRY_LEG_STATES
            and pos_id
            and pos_id not in pos_ids
        ):
            pos_ids.append(pos_id)
    return pos_ids


def _execution_event_detail(row: ExecutionEvent) -> dict[str, object]:
    return {
        "id": int(row.id),
        "execution_binding_id": row.execution_binding_id,
        "strategy_instance_id": row.strategy_instance_id,
        "action": row.action,
        "status": row.status,
        "message_id": row.message_id,
        "source_message_id": row.source_message_id,
        "order_id": row.order_id,
        "client_order_id": row.client_order_id,
        "pos_id": row.pos_id,
        "reason": row.reason,
        "before": _safe_json_value(row.before_json),
        "after": _safe_json_value(row.after_json),
        "request": _safe_json_value(row.request_json),
        "response": _safe_json_value(row.response_json),
        "exchange_event_time": _as_utc(row.exchange_event_time),
    }


def _management_batch_detail(
    row: StrategyManagementBatch,
    *,
    legs: list[StrategyManagementLeg],
) -> dict[str, object]:
    return {
        "id": int(row.id),
        "raw_message_id": int(row.raw_message_id),
        "recognition_decision_id": int(row.recognition_decision_id),
        "target_lifecycle_id": int(row.target_lifecycle_id),
        "strategy_instance_id": row.strategy_instance_id,
        "execution_binding_id": int(row.execution_binding_id),
        "intent": row.intent,
        "effective_action": row.effective_action,
        "execution_mode": row.execution_mode,
        "status": row.status,
        "reason_code": row.reason_code,
        "target_snapshot": _safe_json_value(row.target_snapshot_json),
        "planned_at": _as_utc(row.planned_at),
        "started_at": _as_utc(row.started_at),
        "reconciled_at": _as_utc(row.reconciled_at),
        "completed_at": _as_utc(row.completed_at),
        "legs": [_management_leg_detail(leg) for leg in legs],
    }


def _management_leg_detail(row: StrategyManagementLeg) -> dict[str, object]:
    return {
        "id": int(row.id),
        "execution_order_leg_id": int(row.execution_order_leg_id),
        "pos_id": row.pos_id,
        "leg_index": int(row.leg_index),
        "status": row.status,
        "preflight_size": row.preflight_size,
        "planned_close_size": row.planned_close_size,
        "avg_entry_price": row.avg_entry_price,
        "quantity_step": row.quantity_step,
        "old_tpsl": _safe_json_value(row.old_tpsl_json),
        "planned_tpsl": _safe_json_value(row.planned_tpsl_json),
        "client_order_id": row.client_order_id,
        "exchange_order_id": row.exchange_order_id,
        "request": _safe_json_value(row.request_json),
        "response": _safe_json_value(row.response_json),
        "last_error": _safe_error_detail(row.last_error),
        "last_exchange_snapshot": _safe_json_value(row.last_exchange_snapshot_json),
    }


def _management_confirmation_summary(
    row: StrategyManagementBatch,
    *,
    legs: list[StrategyManagementLeg],
    message_id: int | None,
) -> dict[str, object]:
    planned_stops: list[float] = []
    for leg in legs:
        planned_tpsl = _safe_json_value(leg.planned_tpsl_json)
        if not isinstance(planned_tpsl, dict):
            continue
        planned_stop = _finite_float(planned_tpsl.get("stop_loss_text"))
        if planned_stop is not None and planned_stop not in planned_stops:
            planned_stops.append(planned_stop)
    return {
        "message_id": message_id,
        "status": str(row.status),
        "intent": str(row.intent),
        "effective_action": str(row.effective_action),
        "planned_stops": planned_stops,
    }


def load_strategy_record_summaries(
    session_factory,
    *,
    group_labels_by_chat_id: dict[int, str],
    filter_name: str = "needs_attention",
    chat_id: int | None = None,
    pos_ids: set[str] | None = None,
    live_binding_only: bool = False,
    limit: int | None = 100,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    """Return batched strategy summaries without mutating trading state."""

    if filter_name not in {
        "needs_attention",
        "all",
        "executing",
        "pending_entry",
        "finished",
    }:
        raise ValueError(f"unsupported strategy record filter: {filter_name}")
    if limit is not None and limit <= 0:
        return []

    with session_factory() as session:
        lifecycle_query = session.query(StrategyLifecycle)
        if chat_id is not None:
            lifecycle_query = lifecycle_query.filter(StrategyLifecycle.chat_id == chat_id)
        if filter_name == "pending_entry":
            lifecycle_query = lifecycle_query.filter(
                StrategyLifecycle.lifecycle_status == "pending_entry"
            )
        elif filter_name == "finished":
            lifecycle_query = lifecycle_query.filter(
                StrategyLifecycle.lifecycle_status.in_(FINISHED_LIFECYCLE_STATUSES)
            )
        elif filter_name == "executing":
            lifecycle_query = lifecycle_query.filter(
                StrategyLifecycle.lifecycle_status == "entered"
            )
        binding_scope_predicates = [
            ExecutionBinding.id == StrategyLifecycle.execution_binding_id
        ]
        if pos_ids is not None:
            normalized_pos_ids = {str(item) for item in pos_ids if str(item).strip()}
            if not normalized_pos_ids:
                return []
            binding_scope_predicates.append(
                or_(
                    ExecutionBinding.pos_id.in_(normalized_pos_ids),
                    exists().where(
                        ExecutionOrderLeg.execution_binding_id
                        == ExecutionBinding.id,
                        ExecutionOrderLeg.purpose == "entry",
                        ExecutionOrderLeg.attribution_status == "verified",
                        ExecutionOrderLeg.pos_id.in_(normalized_pos_ids),
                        func.lower(func.coalesce(ExecutionOrderLeg.status, "")).not_in(
                            TERMINAL_ENTRY_LEG_STATES
                        ),
                    ),
                )
            )
        if live_binding_only:
            binding_scope_predicates.extend(
                (
                    func.lower(func.coalesce(ExecutionBinding.status, "")).in_(
                        LIVE_BINDING_STATUSES
                    ),
                    ExecutionBinding.pos_id.is_not(None),
                    ExecutionBinding.pos_id != "",
                )
            )
        if pos_ids is not None or live_binding_only:
            binding_scope_predicates.append(
                func.lower(func.trim(func.coalesce(ExecutionBinding.venue, "")))
                == "deepcoin"
            )
            lifecycle_query = lifecycle_query.filter(
                exists().where(*binding_scope_predicates)
            )
        if filter_name == "needs_attention":
            lifecycle_query, severity_expression, latest_expression = (
                _attention_lifecycle_query(lifecycle_query)
            )
            lifecycle_query = lifecycle_query.order_by(
                severity_expression,
                latest_expression.desc(),
                StrategyLifecycle.id.desc(),
            )
        elif filter_name in {"executing", "pending_entry", "finished"}:
            lifecycle_query, severity_expression, latest_expression = (
                _attention_lifecycle_query(
                    lifecycle_query,
                    only_attention=False,
                )
            )
            lifecycle_query = lifecycle_query.order_by(
                severity_expression,
                latest_expression.desc(),
                StrategyLifecycle.id.desc(),
            )
        else:
            lifecycle_query = lifecycle_query.order_by(
                StrategyLifecycle.updated_at.desc(),
                StrategyLifecycle.id.desc(),
            )
        lifecycles = (
            lifecycle_query.all()
            if limit is None
            else lifecycle_query.limit(limit).all()
        )
        if not lifecycles:
            return []

        candidate_ids = {
            int(row.signal_candidate_id)
            for row in lifecycles
            if row.signal_candidate_id is not None
        }
        candidates = (
            session.query(SignalCandidate)
            .filter(SignalCandidate.id.in_(candidate_ids))
            .all()
            if candidate_ids
            else []
        )
        candidates_by_id = {int(row.id): row for row in candidates}
        raw_message_ids = {int(row.raw_message_id) for row in candidates}

        decisions = (
            session.query(RecognitionDecision)
            .filter(RecognitionDecision.raw_message_id.in_(raw_message_ids))
            .all()
            if raw_message_ids
            else []
        )
        recognitions = (
            session.query(MessageRecognition)
            .filter(MessageRecognition.raw_message_id.in_(raw_message_ids))
            .all()
            if raw_message_ids
            else []
        )
        decisions_by_raw_message_id = {int(row.raw_message_id): row for row in decisions}
        recognitions_by_raw_message_id = {
            int(row.raw_message_id): row for row in recognitions
        }

        binding_ids = {
            int(row.execution_binding_id)
            for row in lifecycles
            if row.execution_binding_id is not None
        }
        bindings = (
            session.query(ExecutionBinding)
            .filter(ExecutionBinding.id.in_(binding_ids))
            .all()
            if binding_ids
            else []
        )
        bindings_by_id = {int(row.id): row for row in bindings}
        entry_legs = (
            session.query(ExecutionOrderLeg)
            .filter(
                ExecutionOrderLeg.execution_binding_id.in_(binding_ids),
                ExecutionOrderLeg.purpose == "entry",
            )
            .order_by(
                ExecutionOrderLeg.execution_binding_id,
                ExecutionOrderLeg.leg_index,
                ExecutionOrderLeg.id,
            )
            .all()
            if binding_ids
            else []
        )
        strategy_instance_ids = {
            str(row.strategy_instance_id)
            for row in bindings
            if row.strategy_instance_id
        }
        strategy_instance_binding_counts = dict(
            session.query(
                ExecutionBinding.strategy_instance_id,
                func.count(ExecutionBinding.id),
            )
            .filter(ExecutionBinding.strategy_instance_id.in_(strategy_instance_ids))
            .group_by(ExecutionBinding.strategy_instance_id)
            .all()
        ) if strategy_instance_ids else {}

        event_predicates = []
        if binding_ids:
            event_predicates.append(ExecutionEvent.execution_binding_id.in_(binding_ids))
        if strategy_instance_ids:
            event_predicates.append(
                and_(
                    ExecutionEvent.execution_binding_id.is_(None),
                    ExecutionEvent.strategy_instance_id.in_(strategy_instance_ids),
                )
            )
        events = (
            session.query(ExecutionEvent)
            .filter(or_(*event_predicates))
            .order_by(ExecutionEvent.created_at.desc(), ExecutionEvent.id.desc())
            .all()
            if event_predicates
            else []
        )

        lifecycle_ids = {int(row.id) for row in lifecycles}
        management_batches = (
            session.query(StrategyManagementBatch)
            .filter(StrategyManagementBatch.target_lifecycle_id.in_(lifecycle_ids))
            .order_by(
                StrategyManagementBatch.updated_at.desc(),
                StrategyManagementBatch.id.desc(),
            )
            .all()
        )
        management_batch_ids = {int(batch.id) for batch in management_batches}
        management_legs = (
            session.query(StrategyManagementLeg)
            .filter(StrategyManagementLeg.management_batch_id.in_(management_batch_ids))
            .order_by(
                StrategyManagementLeg.management_batch_id,
                StrategyManagementLeg.leg_index,
                StrategyManagementLeg.id,
            )
            .all()
            if management_batch_ids
            else []
        )
        management_raw_message_ids = {
            int(batch.raw_message_id) for batch in management_batches
        }
        management_raw_messages = (
            session.query(RawMessage)
            .filter(RawMessage.id.in_(management_raw_message_ids))
            .all()
            if management_raw_message_ids
            else []
        )

    events_by_binding_id, events_by_strategy_instance_id = _index_events(events)
    batches_by_lifecycle_id: dict[int, list[StrategyManagementBatch]] = defaultdict(list)
    for batch in management_batches:
        batches_by_lifecycle_id[int(batch.target_lifecycle_id)].append(batch)
    management_legs_by_batch_id: dict[int, list[StrategyManagementLeg]] = defaultdict(list)
    for leg in management_legs:
        management_legs_by_batch_id[int(leg.management_batch_id)].append(leg)
    management_message_id_by_raw_id = {
        int(row.id): int(row.message_id) for row in management_raw_messages
    }
    entry_leg_binding_ids = {int(leg.execution_binding_id) for leg in entry_legs}
    pos_ids_by_binding_id: dict[int, list[str]] = defaultdict(list)
    for leg in entry_legs:
        pos_id = str(leg.pos_id or "").strip()
        if (
            leg.attribution_status != "verified"
            or str(leg.status or "").strip().lower() in TERMINAL_ENTRY_LEG_STATES
        ):
            continue
        binding_pos_ids = pos_ids_by_binding_id[int(leg.execution_binding_id)]
        if pos_id and pos_id not in binding_pos_ids:
            binding_pos_ids.append(pos_id)

    fallback_time = _as_utc(now) or datetime.now(UTC)
    rows: list[dict[str, object]] = []
    for lifecycle in lifecycles:
        candidate = candidates_by_id.get(int(lifecycle.signal_candidate_id or 0))
        raw_message_id = int(candidate.raw_message_id) if candidate is not None else None
        decision = decisions_by_raw_message_id.get(raw_message_id or 0)
        recognition = recognitions_by_raw_message_id.get(raw_message_id or 0)
        binding = bindings_by_id.get(int(lifecycle.execution_binding_id or 0))
        lifecycle_events = _events_for_binding(
            binding,
            events_by_binding_id=events_by_binding_id,
            events_by_strategy_instance_id=events_by_strategy_instance_id,
            strategy_instance_binding_counts=strategy_instance_binding_counts,
        )
        batches = batches_by_lifecycle_id.get(int(lifecycle.id), [])
        attention_reasons = _attention_reasons(
            lifecycle=lifecycle,
            candidate=candidate,
            decision=decision,
            recognition=recognition,
            binding=binding,
            events=lifecycle_events,
            management_batches=batches,
        )
        attention = min(
            attention_reasons,
            key=lambda item: ATTENTION_SEVERITY_RANK[str(item["severity"])],
            default=None,
        )
        if filter_name == "needs_attention" and attention is None:
            continue

        latest_changed_at = _latest_timestamp(
            lifecycle,
            decision,
            recognition,
            binding,
            *lifecycle_events,
            *batches,
        ) or fallback_time
        rows.append(
            {
                "lifecycle_id": int(lifecycle.id),
                "chat_id": int(lifecycle.chat_id),
                "group_name": group_labels_by_chat_id.get(
                    int(lifecycle.chat_id), str(lifecycle.chat_id)
                ),
                "message_id": int(lifecycle.message_id),
                "symbol": lifecycle.symbol,
                "side": lifecycle.side,
                "lifecycle_state": lifecycle.lifecycle_status,
                "recognition_state": _recognition_state(
                    decision,
                    recognition,
                    candidate,
                ),
                "execution_state": _execution_state(binding, lifecycle_events),
                "attribution_state": _attribution_state(binding),
                "pos_id": str(binding.pos_id) if binding is not None and binding.pos_id else None,
                "pos_ids": (
                    pos_ids_by_binding_id.get(int(binding.id), [])
                    if binding is not None
                    else []
                ),
                "position_ids_authoritative": (
                    binding is not None and int(binding.id) in entry_leg_binding_ids
                ),
                "expected_stop_loss": lifecycle.stop_loss,
                "expected_take_profit": _safe_json_value(lifecycle.take_profit),
                "expected_management_action": lifecycle.management_action,
                "management_signal_message_id": lifecycle.management_signal_message_id,
                "management_batch_statuses": [str(batch.status) for batch in batches],
                "management_confirmations": [
                    _management_confirmation_summary(
                        batch,
                        legs=management_legs_by_batch_id.get(int(batch.id), []),
                        message_id=management_message_id_by_raw_id.get(
                            int(batch.raw_message_id)
                        ),
                    )
                    for batch in batches
                ],
                "venue": (
                    str(binding.venue or "").strip().lower()
                    if binding is not None
                    else None
                ),
                "attention": attention,
                "attention_reasons": attention_reasons,
                "latest_changed_at": latest_changed_at,
                "detail_href": f"/strategy-records/{int(lifecycle.id)}",
            }
        )

    rows.sort(
        key=lambda row: (
            ATTENTION_SEVERITY_RANK.get(
                str((row["attention"] or {}).get("severity")), 3  # type: ignore[union-attr]
            ),
            -_timestamp_value(row["latest_changed_at"]),
            -int(row["lifecycle_id"]),
        )
    )
    return rows if limit is None else rows[:limit]


def count_strategy_records(
    session_factory,
    *,
    chat_id: int | None = None,
) -> dict[str, int]:
    """Count lifecycle-backed records without materializing projection rows."""

    with session_factory() as session:
        base = session.query(StrategyLifecycle)
        if chat_id is not None:
            base = base.filter(StrategyLifecycle.chat_id == chat_id)
        all_count = base.count()
        attention_query, _severity, _latest = _attention_lifecycle_query(base)
        needs_attention_count = attention_query.with_entities(
            func.count(func.distinct(StrategyLifecycle.id))
        ).scalar() or 0
        base_exchange_applicable = or_(
            StrategyLifecycle.execution_binding_id.is_(None),
            exists().where(
                ExecutionBinding.id == StrategyLifecycle.execution_binding_id,
                func.lower(func.trim(func.coalesce(ExecutionBinding.venue, "")))
                == "deepcoin",
            ),
        )
        joined_exchange_applicable = or_(
            StrategyLifecycle.execution_binding_id.is_(None),
            func.lower(func.trim(func.coalesce(ExecutionBinding.venue, "")))
            == "deepcoin",
        )
        exchange_applicable_count = base.filter(base_exchange_applicable).count()
        attention_exchange_applicable_count = attention_query.filter(
            joined_exchange_applicable
        ).with_entities(
            func.count(func.distinct(StrategyLifecycle.id))
        ).scalar() or 0
        state_counts = dict(
            base.with_entities(
                StrategyLifecycle.lifecycle_status,
                func.count(StrategyLifecycle.id),
            )
            .group_by(StrategyLifecycle.lifecycle_status)
            .all()
        )

    normalized = {
        str(state or "").strip().lower(): int(count)
        for state, count in state_counts.items()
    }
    return {
        "all": int(all_count),
        "needs_attention": int(needs_attention_count),
        "executing": normalized.get("entered", 0),
        "pending_entry": normalized.get("pending_entry", 0),
        "finished": sum(
            normalized.get(state, 0) for state in FINISHED_LIFECYCLE_STATUSES
        ),
        "_exchange_applicable": int(exchange_applicable_count),
        "_attention_exchange_applicable": int(
            attention_exchange_applicable_count
        ),
    }


def enrich_strategy_records_with_exchange(
    records: list[dict[str, object]],
    *,
    exchange_snapshot: dict[str, object],
) -> list[dict[str, object]]:
    """Attach only already-annotated Deepcoin evidence to strategy records.

    The projection deliberately matches by persisted ``pos_id`` only.  It does not
    score or rematch exchange rows, so ambiguous attribution remains fail-closed.
    """

    positions_by_pos_id: dict[str, list[dict[str, object]]] = defaultdict(list)
    raw_positions = exchange_snapshot.get("positions")
    if isinstance(raw_positions, list):
        for position in raw_positions:
            if not isinstance(position, dict):
                continue
            pos_id = position.get("pos_id") or position.get("posId")
            if pos_id:
                positions_by_pos_id[str(pos_id)].append(position)

    exchange_error = exchange_snapshot.get("error")
    enriched: list[dict[str, object]] = []
    matched_exchange_pos_ids: set[str] = set()
    for source_record in records:
        record = dict(source_record)
        record["real_position"] = None
        record["real_positions"] = []
        venue = str(record.get("venue") or "deepcoin").strip().lower()
        if venue != "deepcoin":
            record["venue"] = venue
            record["exchange_state"] = "not_applicable"
            enriched.append(record)
            continue
        if exchange_error:
            record["exchange_state"] = "unknown"
            _add_exchange_attention(
                record,
                code="exchange_unavailable",
                reason="Deepcoin 仓位快照暂不可用，无法确认真实持仓",
            )
            enriched.append(record)
            continue

        expected_pos_ids = _record_position_ids(record)
        if not expected_pos_ids:
            record["exchange_state"] = "confirmed"
            enriched.append(record)
            continue

        matched_positions: list[dict[str, object]] = []
        missing_pos_ids: list[str] = []
        duplicate_pos_ids: list[str] = []
        for pos_id in expected_pos_ids:
            matches = positions_by_pos_id.get(pos_id, [])
            if not matches:
                missing_pos_ids.append(pos_id)
                continue
            if len(matches) != 1:
                matched_exchange_pos_ids.add(pos_id)
                duplicate_pos_ids.append(pos_id)
                continue
            matched_exchange_pos_ids.add(pos_id)
            matched_positions.append(matches[0])

        record["real_positions"] = matched_positions
        if len(matched_positions) == 1 and len(expected_pos_ids) == 1:
            record["real_position"] = matched_positions[0]

        if missing_pos_ids:
            if str(record.get("attribution_state") or "").lower() in {
                "bound",
                "live_bound",
            }:
                record["exchange_state"] = "attention"
                record["attribution_state"] = "conflict"
                _add_exchange_attention(
                    record,
                    code="position_missing",
                    reason=(
                        "本地已验证 entry leg 的 pos_id 未出现在当前 Deepcoin 仓位快照中："
                        + "、".join(missing_pos_ids)
                    ),
                )
            else:
                record["exchange_state"] = "confirmed"
            enriched.append(record)
            continue
        if duplicate_pos_ids:
            record["exchange_state"] = "attention"
            record["attribution_state"] = "conflict"
            _add_exchange_attention(
                record,
                code="attribution_conflict",
                reason=(
                    "以下 pos_id 各自对应多条交易所仓位记录："
                    + "、".join(duplicate_pos_ids)
                ),
            )
            enriched.append(record)
            continue

        attribution_rows = []
        for position in matched_positions:
            attribution = position.get("attribution")
            attribution = attribution if isinstance(attribution, dict) else {}
            attribution_rows.append(
                (
                    str(attribution.get("state") or "unassigned").lower(),
                    _exchange_attribution_reason(attribution),
                )
            )
        attribution_states = {state for state, _reason in attribution_rows}

        if attribution_states == {"bound"}:
            record["exchange_state"] = "confirmed"
            record["attribution_state"] = "bound"
        elif attribution_states & {"conflict"}:
            record["exchange_state"] = "attention"
            record["attribution_state"] = "conflict"
            _add_exchange_attention(
                record,
                code="attribution_conflict",
                reason="；".join(reason for _state, reason in attribution_rows),
            )
        elif attribution_states & {"candidate", "ambiguous"}:
            record["exchange_state"] = "unconfirmed"
            record["attribution_state"] = "ambiguous"
            _add_exchange_attention(
                record,
                code="attribution_ambiguous",
                reason="；".join(reason for _state, reason in attribution_rows),
            )
        else:
            record["exchange_state"] = "attention"
            record["attribution_state"] = "unassigned"
            _add_exchange_attention(
                record,
                code="unattributed_position",
                reason="；".join(reason for _state, reason in attribution_rows),
            )

        for position in matched_positions:
            if _has_concrete_protection_mismatch(position):
                _add_exchange_attention(
                    record,
                    code="protection_mismatch",
                    reason=str(
                        position.get("protection_mismatch_reason")
                        or "交易所保护证据标记为不一致"
                    ),
                )
        management_drift_reason = management_execution_drift_reason(
            record,
            matched_positions,
        )
        if management_drift_reason is not None:
            record["exchange_state"] = "attention"
            _add_exchange_attention(
                record,
                code="management_execution_drift",
                reason=management_drift_reason,
            )
        enriched.append(record)

    if not exchange_error:
        for pos_id, unmatched_positions in positions_by_pos_id.items():
            if pos_id in matched_exchange_pos_ids:
                continue
            enriched.append(
                _synthetic_exchange_position_record(
                    pos_id=pos_id,
                    positions=unmatched_positions,
                )
            )
    return enriched


def _record_position_ids(record: dict[str, object]) -> list[str]:
    values = record.get("pos_ids")
    if isinstance(values, (list, tuple, set)):
        normalized = [str(value).strip() for value in values if str(value).strip()]
        if normalized:
            return list(dict.fromkeys(normalized))
        if record.get("position_ids_authoritative") is True:
            return []
    pos_id = str(record.get("pos_id") or "").strip()
    return [pos_id] if pos_id else []


def load_live_bindings_without_lifecycle(
    session_factory,
    *,
    group_labels_by_chat_id: dict[int, str],
    chat_id: int | None = None,
) -> list[dict[str, object]]:
    """Return active persisted position bindings that no lifecycle references."""

    with session_factory() as session:
        query = session.query(ExecutionBinding).filter(
            func.lower(func.coalesce(ExecutionBinding.status, "")).in_(
                LIVE_BINDING_STATUSES
            ),
            ExecutionBinding.pos_id.is_not(None),
            ExecutionBinding.pos_id != "",
            func.lower(func.trim(func.coalesce(ExecutionBinding.venue, "")))
            == "deepcoin",
            ~exists().where(
                StrategyLifecycle.execution_binding_id == ExecutionBinding.id
            ),
        )
        if chat_id is not None:
            query = query.filter(ExecutionBinding.chat_id == chat_id)
        bindings = query.order_by(
            ExecutionBinding.updated_at.desc(),
            ExecutionBinding.id.desc(),
        ).all()

    records: list[dict[str, object]] = []
    for binding in bindings:
        pos_id = str(binding.pos_id)
        binding_chat_id = int(binding.chat_id)
        record: dict[str, object] = {
            "lifecycle_id": None,
            "binding_id": int(binding.id),
            "chat_id": binding_chat_id,
            "group_name": group_labels_by_chat_id.get(
                binding_chat_id, str(binding_chat_id)
            ),
            "message_id": int(binding.message_id),
            "symbol": binding.symbol,
            "side": binding.side,
            "lifecycle_state": "binding_without_lifecycle",
            "recognition_state": "unknown",
            "execution_state": str(binding.status),
            "attribution_state": "live_bound",
            "pos_id": pos_id,
            "venue": "deepcoin",
            "attention": None,
            "attention_reasons": [],
            "latest_changed_at": _latest_timestamp(binding),
            "detail_href": f"/?{urlencode({'view': 'positions', 'pos_id': pos_id})}",
            "orphan_execution_binding": True,
        }
        _add_exchange_attention(
            record,
            code="binding_without_lifecycle",
            reason=(
                f"执行绑定 #{int(binding.id)} 持有 pos_id {pos_id}，"
                "但没有 StrategyLifecycle 引用它"
            ),
        )
        records.append(record)
    return records


def _synthetic_exchange_position_record(
    *,
    pos_id: str,
    positions: list[dict[str, object]],
) -> dict[str, object]:
    position = positions[0]
    attribution = position.get("attribution")
    attribution = attribution if isinstance(attribution, dict) else {}
    annotated_state = str(attribution.get("state") or "unassigned").lower()
    reason = _exchange_attribution_reason(attribution)
    if len(positions) != 1 or annotated_state == "conflict":
        attribution_state = "conflict"
        code = "attribution_conflict"
        exchange_state = "attention"
        if len(positions) != 1:
            reason = f"pos_id {pos_id} 对应多个交易所仓位记录"
    elif annotated_state in {"candidate", "ambiguous"}:
        attribution_state = "ambiguous"
        code = "attribution_ambiguous"
        exchange_state = "unconfirmed"
    elif annotated_state == "unassigned":
        attribution_state = "unassigned"
        code = "unattributed_position"
        exchange_state = "attention"
    else:
        attribution_state = "conflict"
        code = "attribution_conflict"
        exchange_state = "attention"
        reason = reason or "交易所归属了策略，但当前策略记录范围内无对应生命周期"

    chat_id = attribution.get("chat_id")
    record: dict[str, object] = {
        "lifecycle_id": None,
        "chat_id": chat_id,
        "group_name": attribution.get("group_name") or "未归属",
        "message_id": None,
        "symbol": position.get("symbol"),
        "side": position.get("side"),
        "lifecycle_state": "exchange_only",
        "recognition_state": "not_applicable",
        "execution_state": position.get("execution_status") or "live_position",
        "attribution_state": attribution_state,
        "pos_id": pos_id,
        "venue": "deepcoin",
        "attention": None,
        "attention_reasons": [],
        "latest_changed_at": position.get("latest_event_at"),
        "detail_href": f"/?{urlencode({'view': 'positions', 'pos_id': pos_id})}",
        "exchange_state": exchange_state,
        "real_position": position,
        "orphan_exchange_position": True,
    }
    _add_exchange_attention(record, code=code, reason=reason)
    return record


def _exchange_attribution_reason(attribution: dict[str, object]) -> str:
    reasons = attribution.get("reasons")
    if isinstance(reasons, list):
        rendered = "；".join(str(item) for item in reasons if str(item).strip())
        if rendered:
            return rendered
    return str(attribution.get("label") or "交易所归属证据不充分")


def _has_concrete_protection_mismatch(position: dict[str, object]) -> bool:
    return position.get("protection_mismatch") is True or str(
        position.get("protection_status") or ""
    ).lower() in {"mismatch", "protection_mismatch"}


def management_execution_drift_reason(
    record: dict[str, object],
    positions: list[dict[str, object]],
) -> str | None:
    message_id = record.get("management_signal_message_id")
    expected_stop = _finite_float(record.get("expected_stop_loss"))
    if message_id in {None, ""} or not positions:
        return None

    expected_action = str(record.get("expected_management_action") or "").lower()
    if "partial_take_profit" in expected_action and not _has_confirmed_management_action(
        record,
        message_id=message_id,
        action="partial_take_profit",
    ):
        return f"消息 #{message_id} 要求部分止盈，但没有同消息的交易所确认管理批次"

    actual_take_profits: list[float] = []
    for position in positions:
        protection_status = str(position.get("protection_status") or "").lower()
        if protection_status != "protected":
            return None
        actual_take_profits.extend(_price_values(position.get("take_profit_text")))
        if expected_stop is not None:
            actual_stop = _finite_float(
                position.get("stop_loss_text") or position.get("stop_loss")
            )
            if actual_stop is None:
                return None
            if abs(actual_stop - expected_stop) > max(
                1e-8, abs(expected_stop) * 1e-8
            ) and not _confirmed_management_explains_stop(
                record,
                message_id=message_id,
                actual_stop=actual_stop,
            ):
                actual_text = f"{actual_stop:.15g}"
                return (
                    f"消息 #{message_id} 后策略止损 {expected_stop:.15g}，"
                    f"但 Deepcoin 精确仓位证据为 {actual_text}"
                )

    actual_take_profits = list(dict.fromkeys(actual_take_profits))
    expected_take_profits = _price_values(record.get("expected_take_profit"))
    if expected_take_profits and actual_take_profits and not _same_price_set(
        expected_take_profits,
        actual_take_profits,
    ):
        return (
            f"消息 #{message_id} 后策略止盈 {_format_prices(expected_take_profits)}，"
            f"但 Deepcoin 精确仓位证据为 {_format_prices(actual_take_profits)}"
        )
    return None


def _has_confirmed_management_action(
    record: dict[str, object],
    *,
    message_id: object,
    action: str,
) -> bool:
    confirmations = record.get("management_confirmations")
    if not isinstance(confirmations, list):
        return False
    for confirmation in confirmations:
        if not isinstance(confirmation, dict):
            continue
        if str(confirmation.get("status") or "").strip().lower() != "succeeded":
            continue
        if str(confirmation.get("message_id") or "") != str(message_id):
            continue
        intent = str(confirmation.get("intent") or "").lower()
        effective_action = str(confirmation.get("effective_action") or "").lower()
        if action == "partial_take_profit" and (
            intent == "partial_take_profit"
            or effective_action in {"partial_close", "partial_then_break_even"}
        ):
            return True
        if action in {intent, effective_action}:
            return True
    return False


def _confirmed_management_explains_stop(
    record: dict[str, object],
    *,
    message_id: object,
    actual_stop: float,
) -> bool:
    confirmations = record.get("management_confirmations")
    if not isinstance(confirmations, list):
        return False
    for confirmation in confirmations:
        if not isinstance(confirmation, dict):
            continue
        if str(confirmation.get("status") or "").strip().lower() != "succeeded":
            continue
        if str(confirmation.get("message_id") or "") != str(message_id):
            continue
        planned_stops = confirmation.get("planned_stops")
        if not isinstance(planned_stops, list):
            continue
        if any(
            (planned := _finite_float(value)) is not None
            and abs(planned - actual_stop) <= max(1e-8, abs(actual_stop) * 1e-8)
            for value in planned_stops
        ):
            return True
    return False


def _price_values(value: object) -> list[float]:
    if isinstance(value, dict):
        values = [item for nested in value.values() for item in _price_values(nested)]
    elif isinstance(value, (list, tuple, set)):
        values = [item for nested in value for item in _price_values(nested)]
    else:
        values = [
            float(match)
            for match in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", str(value or ""))
        ]
    return list(dict.fromkeys(values))


def _same_price_set(first: list[float], second: list[float]) -> bool:
    if len(first) != len(second):
        return False
    unmatched = list(second)
    for expected in first:
        match_index = next(
            (
                index
                for index, actual in enumerate(unmatched)
                if abs(expected - actual) <= max(1e-8, abs(expected) * 1e-8)
            ),
            None,
        )
        if match_index is None:
            return False
        unmatched.pop(match_index)
    return True


def _format_prices(values: list[float]) -> str:
    return "/".join(f"{value:.15g}" for value in values)


def _finite_float(value: object) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _add_exchange_attention(
    record: dict[str, object],
    *,
    code: str,
    reason: str,
) -> None:
    severity, label = ATTENTION_LABELS[code]
    attention = {
        "severity": severity,
        "code": code,
        "label": label,
        "reason": reason,
    }
    existing_reasons = record.get("attention_reasons")
    reasons = list(existing_reasons) if isinstance(existing_reasons, list) else []
    reasons.append(attention)
    record["attention_reasons"] = reasons
    record["attention"] = min(
        reasons,
        key=lambda item: ATTENTION_SEVERITY_RANK.get(str(item.get("severity")), 3),
    )


def _attention_lifecycle_query(lifecycle_query, *, only_attention: bool = True):
    """Add SQL attention predicates so filtering happens before ``LIMIT``."""

    lifecycle_query = (
        lifecycle_query.outerjoin(
            SignalCandidate,
            StrategyLifecycle.signal_candidate_id == SignalCandidate.id,
        )
        .outerjoin(
            RecognitionDecision,
            RecognitionDecision.raw_message_id == SignalCandidate.raw_message_id,
        )
        .outerjoin(
            MessageRecognition,
            MessageRecognition.raw_message_id == SignalCandidate.raw_message_id,
        )
        .outerjoin(
            ExecutionBinding,
            StrategyLifecycle.execution_binding_id == ExecutionBinding.id,
        )
    )
    authoritative_decision_missing = and_(
        SignalCandidate.id.is_not(None),
        func.lower(func.coalesce(SignalCandidate.parse_source, "")).in_(
            AUTHORITATIVE_CANDIDATE_PARSE_SOURCES
        ),
        RecognitionDecision.id.is_(None),
    )
    recognition_evidence_missing = authoritative_decision_missing
    recognition_failed = or_(
        func.lower(RecognitionDecision.authoritative_status).in_(
            _FAILED_RECOGNITION_STATUSES
        ),
        and_(
            RecognitionDecision.id.is_(None),
            func.lower(MessageRecognition.status).in_(_FAILED_RECOGNITION_STATUSES),
        ),
    )
    recognition_disagreement = and_(
        RecognitionDecision.id.is_not(None),
        func.lower(func.coalesce(RecognitionDecision.disagreement_severity, "")).notin_(
            _NORMAL_DISAGREEMENT_SEVERITIES
        ),
    )
    binding_is_live = func.lower(func.coalesce(ExecutionBinding.status, "")).in_(
        LIVE_BINDING_STATUSES
    )
    entered_without_binding = and_(
        StrategyLifecycle.lifecycle_status == "entered",
        ~binding_is_live,
    )
    missing_stop = and_(
        StrategyLifecycle.lifecycle_status == "entered",
        binding_is_live,
        StrategyLifecycle.stop_loss.is_(None),
    )
    other_binding = aliased(ExecutionBinding)
    strategy_instance_is_unique = ~exists().where(
        other_binding.strategy_instance_id == ExecutionBinding.strategy_instance_id,
        other_binding.id != ExecutionBinding.id,
    )
    related_event = or_(
        ExecutionEvent.execution_binding_id == ExecutionBinding.id,
        and_(
            ExecutionEvent.execution_binding_id.is_(None),
            ExecutionBinding.strategy_instance_id.is_not(None),
            strategy_instance_is_unique,
            ExecutionEvent.strategy_instance_id == ExecutionBinding.strategy_instance_id,
        ),
    )
    execution_failed = exists().where(
        related_event,
        func.lower(ExecutionEvent.status).in_(FAILED_EXECUTION_STATUSES),
    )
    management_unconfirmed = exists().where(
        StrategyManagementBatch.target_lifecycle_id == StrategyLifecycle.id,
        func.lower(StrategyManagementBatch.status).notin_(SAFE_MANAGEMENT_STATUSES),
    )
    management_blocked = exists().where(
        StrategyManagementBatch.target_lifecycle_id == StrategyLifecycle.id,
        func.lower(StrategyManagementBatch.status) == "blocked",
        StrategyManagementBatch.reason_code.in_(ACTIONABLE_MANAGEMENT_BLOCK_REASONS),
    )
    critical = or_(
        recognition_evidence_missing,
        recognition_failed,
        entered_without_binding,
        missing_stop,
        execution_failed,
    )
    severity_expression = case(
        (critical, 0),
        (or_(management_unconfirmed, management_blocked), 1),
        (recognition_disagreement, 2),
        else_=3,
    )

    latest_event_at = (
        select(func.max(ExecutionEvent.created_at))
        .where(related_event)
        .correlate(ExecutionBinding)
        .scalar_subquery()
    )
    latest_management_at = (
        select(func.max(StrategyManagementBatch.updated_at))
        .where(StrategyManagementBatch.target_lifecycle_id == StrategyLifecycle.id)
        .correlate(StrategyLifecycle)
        .scalar_subquery()
    )
    epoch = datetime(1970, 1, 1)
    latest_expression = func.max(
        func.coalesce(StrategyLifecycle.updated_at, epoch),
        func.coalesce(RecognitionDecision.updated_at, epoch),
        func.coalesce(MessageRecognition.updated_at, epoch),
        func.coalesce(ExecutionBinding.updated_at, epoch),
        func.coalesce(latest_event_at, epoch),
        func.coalesce(latest_management_at, epoch),
    )
    if only_attention:
        lifecycle_query = lifecycle_query.filter(
            or_(
                critical,
                management_unconfirmed,
                management_blocked,
                recognition_disagreement,
            )
        )
    return lifecycle_query, severity_expression, latest_expression


def _index_events(
    events: Iterable[ExecutionEvent],
) -> tuple[dict[int, list[ExecutionEvent]], dict[str, list[ExecutionEvent]]]:
    by_binding_id: dict[int, list[ExecutionEvent]] = defaultdict(list)
    by_strategy_instance_id: dict[str, list[ExecutionEvent]] = defaultdict(list)
    for event in events:
        if event.execution_binding_id is not None:
            by_binding_id[int(event.execution_binding_id)].append(event)
        if event.execution_binding_id is None and event.strategy_instance_id:
            by_strategy_instance_id[str(event.strategy_instance_id)].append(event)
    return by_binding_id, by_strategy_instance_id


def _events_for_binding(
    binding: ExecutionBinding | None,
    *,
    events_by_binding_id: dict[int, list[ExecutionEvent]],
    events_by_strategy_instance_id: dict[str, list[ExecutionEvent]],
    strategy_instance_binding_counts: dict[str, int],
) -> list[ExecutionEvent]:
    if binding is None:
        return []
    indexed = list(events_by_binding_id.get(int(binding.id), []))
    if (
        binding.strategy_instance_id
        and strategy_instance_binding_counts.get(str(binding.strategy_instance_id)) == 1
    ):
        indexed.extend(
            events_by_strategy_instance_id.get(str(binding.strategy_instance_id), [])
        )
    unique = {int(event.id): event for event in indexed}
    return sorted(
        unique.values(),
        key=lambda event: (_timestamp_value(event.created_at), int(event.id)),
        reverse=True,
    )


def _attention_reasons(
    *,
    lifecycle: StrategyLifecycle,
    candidate: SignalCandidate | None,
    decision: RecognitionDecision | None,
    recognition: MessageRecognition | None,
    binding: ExecutionBinding | None,
    events: list[ExecutionEvent],
    management_batches: list[StrategyManagementBatch],
) -> list[dict[str, str]]:
    codes: list[str] = []
    if (
        candidate is not None
        and decision is None
        and _candidate_requires_authoritative_decision(candidate)
    ):
        codes.append("recognition_evidence_missing")
    recognition_status = _recognition_state(
        decision,
        recognition,
        candidate,
    ).strip().lower()
    if recognition_status in _FAILED_RECOGNITION_STATUSES:
        codes.append("recognition_failed")
    if decision is not None and str(decision.disagreement_severity or "").lower() not in (
        _NORMAL_DISAGREEMENT_SEVERITIES
    ):
        codes.append("recognition_disagreement")

    binding_is_live = _is_live_binding(binding)
    if lifecycle.lifecycle_status == "entered" and not binding_is_live:
        codes.append("entered_without_binding")
    if (
        lifecycle.lifecycle_status == "entered"
        and binding_is_live
        and lifecycle.stop_loss is None
    ):
        codes.append("missing_stop")
    if any(str(event.status or "").lower() in FAILED_EXECUTION_STATUSES for event in events):
        codes.append("execution_failed")
    if any(
        str(batch.status or "").lower() not in SAFE_MANAGEMENT_STATUSES
        for batch in management_batches
    ):
        codes.append("management_unconfirmed")
    if any(
        str(batch.status or "").lower() == "blocked"
        and str(batch.reason_code or "") in ACTIONABLE_MANAGEMENT_BLOCK_REASONS
        for batch in management_batches
    ):
        codes.append("management_blocked")

    return [
        {"severity": ATTENTION_LABELS[code][0], "code": code, "label": ATTENTION_LABELS[code][1]}
        for code in codes
    ]


def _recognition_state(
    decision: RecognitionDecision | None,
    recognition: MessageRecognition | None,
    candidate: SignalCandidate | None = None,
) -> str:
    if decision is not None:
        return str(decision.authoritative_status)
    if recognition is not None:
        return str(recognition.status)
    if candidate is None or not _candidate_requires_authoritative_decision(candidate):
        return "legacy"
    return "unknown"


def _candidate_requires_authoritative_decision(candidate: SignalCandidate) -> bool:
    return (
        str(candidate.parse_source or "").strip().lower()
        in AUTHORITATIVE_CANDIDATE_PARSE_SOURCES
    )


def _execution_state(
    binding: ExecutionBinding | None, events: list[ExecutionEvent]
) -> str:
    if events:
        return str(events[0].status)
    if binding is not None:
        return str(binding.status)
    return "not_started"


def _attribution_state(binding: ExecutionBinding | None) -> str:
    if _is_live_binding(binding):
        return "live_bound"
    if binding is not None:
        return "bound_non_live"
    return "unbound"


def _is_live_binding(binding: ExecutionBinding | None) -> bool:
    return binding is not None and str(binding.status or "").lower() in LIVE_BINDING_STATUSES


def _latest_timestamp(*records: object) -> datetime | None:
    timestamps: list[datetime] = []
    for record in records:
        for field_name in ("updated_at", "created_at"):
            value = _as_utc(getattr(record, field_name, None))
            if value is not None:
                timestamps.append(value)
                break
    return max(timestamps, default=None)


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp_value(value: object) -> float:
    timestamp = _as_utc(value)
    return timestamp.timestamp() if timestamp is not None else 0.0
