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

from telegram_kol_research.position_attribution import (
    TERMINAL_ENTRY_LEG_STATES,
    has_authoritative_persisted_position,
    PositionAttributionError,
    require_verified_position_ownership,
)
from telegram_kol_research.reporting import (
    format_entry_assembly_summary,
    format_entry_revision_summary,
)
from telegram_kol_research.models import (
    ContextResolutionAttempt,
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    EntryRevisionReplacement,
    MediaAsset,
    MessageRecognition,
    MessageEvidenceVersion,
    PositionAttributionAudit,
    PositionBackupStopOrder,
    PositionMutationIntent,
    PendingTpslSnapshotObservation,
    PositionProtectionIncident,
    PositionProtectionLedger,
    PositionProtectionRevision,
    PositionTakeProfitOrder,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    StrategyBreakEvenConvergence,
    StrategyBreakEvenConvergenceLeg,
    StrategyManagementBatch,
    StrategyManagementLeg,
    StrategyRevisionBatch,
    StrategyMessageLink,
    StrategyThread,
    TriggerProtectionIntent,
    TriggerProtectionStopRescue,
    TriggerTakeProfitConvergence,
    TelegramSourceMessageEvent,
)
from telegram_kol_research.position_management_capabilities import (
    evaluate_position_management_capabilities,
)
from telegram_kol_research.deepcoin_normalization import (
    normalize_deepcoin_swap_instrument,
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


class CompositeManagementCompletionError(ValueError):
    """Raised when a composite batch cannot be truthfully called complete."""


def _composite_value(row, name: str, default=None):
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _composite_json(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def validate_composite_management_completion(
    *,
    source_text: str,
    contract: dict,
    batch_status: str,
    components: list,
    pending_orders: list,
    expected_leg_ids: set[str] | None = None,
) -> dict:
    """Fail closed when source, contract, execution, and exchange evidence differ."""

    source = str(source_text or "")
    close_fraction = contract.get("close_fraction")
    source_has_half_close = bool(
        re.search(r"(?:止盈|减仓|平仓|卖出)[^\n]{0,16}50\s*[%％]|50\s*[%％]", source)
    )
    try:
        contract_has_half_close = abs(float(close_fraction) - 0.5) < 1e-9
    except (TypeError, ValueError):
        contract_has_half_close = False
    if source_has_half_close and not contract_has_half_close:
        raise CompositeManagementCompletionError(
            "source_close_clause_missing_from_contract"
        )

    required = {
        str(item.get("component_kind") if isinstance(item, dict) else item)
        for item in (contract.get("required_components") or [])
    }
    source_has_stop_move = bool(
        re.search(r"止损[^\n]{0,24}(?:移|推|开仓价|成本|保本|价格)", source)
    )
    if (source_has_stop_move or contract.get("stop_mode")) and (
        "replace_remaining_protection" not in required
    ):
        raise CompositeManagementCompletionError(
            "source_stop_clause_missing_component"
        )

    component_keys = [
        (
            str(_composite_value(component, "strategy_management_leg_id", "batch")),
            str(_composite_value(component, "component_kind", "")),
        )
        for component in components
    ]
    by_scope_kind = dict(zip(component_keys, components, strict=True))
    if len(by_scope_kind) != len(component_keys):
        raise CompositeManagementCompletionError(
            "management_instruction_component_topology_invalid"
        )
    if expected_leg_ids is not None:
        expected_keys = {
            (str(scope), kind)
            for scope in expected_leg_ids
            for kind in required
        }
        if set(component_keys) != expected_keys:
            raise CompositeManagementCompletionError(
                "management_instruction_component_topology_invalid"
            )
    pending_ids = {
        str(
            _composite_value(order, "ordId")
            or _composite_value(order, "order_id")
            or _composite_value(order, "orderId")
            or ""
        )
        for order in pending_orders
    }
    consumes = [
        component
        for (_scope, kind), component in by_scope_kind.items()
        if kind == "consume_take_profit_stage"
    ]
    for consume in consumes:
        desired = _composite_json(
            _composite_value(
                consume,
                "desired",
                _composite_value(consume, "desired_json"),
            ),
            {},
        )
        execution = desired.get("take_profit_consumption_execution") or {}
        cancelled_ids = {str(value) for value in execution.get("cancel_order_ids", [])}
        if cancelled_ids & pending_ids:
            raise CompositeManagementCompletionError(
                "consumed_take_profit_still_pending"
            )

    if str(batch_status).lower() == "succeeded":
        scopes = (
            {str(value) for value in expected_leg_ids}
            if expected_leg_ids is not None
            else ({scope for scope, _kind in by_scope_kind} or {"batch"})
        )
        for scope in scopes:
            for kind in required:
                component = by_scope_kind.get((scope, kind))
                evidence = _composite_json(
                    _composite_value(
                        component,
                        "evidence",
                        (
                            _composite_value(component, "evidence_json")
                            if component
                            else None
                        ),
                    ),
                    [],
                )
                if (
                    component is None
                    or str(_composite_value(component, "status", "")).lower()
                    != "confirmed"
                    or not evidence
                ):
                    raise CompositeManagementCompletionError(
                        "completed_batch_missing_component_evidence"
                    )

    return {
        "batch_status": str(batch_status),
        "required_components": sorted(required),
        "component_statuses": {
            f"{scope}:{kind}": str(_composite_value(component, "status", ""))
            for (scope, kind), component in sorted(by_scope_kind.items())
        },
    }

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
        "take_profit_convergence_unknown": ("critical", "分段止盈提交结果未知"),
        "take_profit_convergence_conflicted": ("warning", "分段止盈需人工复核"),
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
        "protection_adopted": 6,
        "protection_adoption_refused": 7,
        "management": 8,
        "trigger_protection_recovery": 9,
        "protection_revision": 10,
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

        deletion_exit = (
            session.query(SourceMessageDeletionExit)
            .filter(SourceMessageDeletionExit.target_lifecycle_id == lifecycle.id)
            .order_by(SourceMessageDeletionExit.id.desc())
            .first()
        )
        deletion_event = (
            session.get(TelegramSourceMessageEvent, deletion_exit.source_event_id)
            if deletion_exit is not None
            else None
        )
        source_deletion = _source_deletion_detail(
            deletion_exit, event=deletion_event
        )

        strategy_thread = (
            session.get(StrategyThread, int(lifecycle.strategy_thread_id))
            if lifecycle.strategy_thread_id is not None
            else None
        )
        context_resolution: dict[str, object] | None = None
        if strategy_thread is not None:
            from telegram_kol_research.trading_settings import load_trading_settings

            thread_links = (
                session.query(StrategyMessageLink, RawMessage)
                .join(RawMessage, RawMessage.id == StrategyMessageLink.raw_message_id)
                .filter(
                    StrategyMessageLink.strategy_thread_id == strategy_thread.id,
                    StrategyMessageLink.status == "active",
                )
                .order_by(RawMessage.posted_at, RawMessage.message_id)
                .all()
            )
            linked_raw_ids = [raw.id for _link, raw in thread_links]
            latest_attempt = (
                session.query(ContextResolutionAttempt)
                .filter(ContextResolutionAttempt.raw_message_id.in_(linked_raw_ids))
                .order_by(ContextResolutionAttempt.id.desc())
                .first()
                if linked_raw_ids
                else None
            )
            decision = (
                _safe_json_value(latest_attempt.decision_json)
                if latest_attempt is not None
                else {}
            )
            triggers = (
                _safe_json_value(latest_attempt.reanalysis_triggers_json)
                if latest_attempt is not None
                else []
            )
            evidence_version = (
                session.get(
                    MessageEvidenceVersion,
                    int(latest_attempt.message_evidence_version_id),
                )
                if latest_attempt is not None
                and latest_attempt.message_evidence_version_id is not None
                else None
            )
            context_resolution = {
                "thread_id": int(strategy_thread.id),
                "root_message_id": int(strategy_thread.root_message_id),
                "thread_status": strategy_thread.status,
                "automation_enabled": load_trading_settings(
                    session_factory
                ).context_resolution_enabled_for_chat(int(lifecycle.chat_id)),
                "linked_messages": [
                    {
                        "message_id": int(raw.message_id),
                        "relation": link.relation_kind,
                        "posted_at": _as_utc(raw.posted_at),
                        "reply_to_message_id": raw.reply_to_message_id,
                    }
                    for link, raw in thread_links
                ],
                "evidence_version": (
                    int(evidence_version.version)
                    if evidence_version is not None
                    else None
                ),
                "evidence_input_kind": (
                    "text+image"
                    if evidence_version is not None
                    and bool(_safe_json_value(evidence_version.image_evidence_json))
                    else "text" if evidence_version is not None else None
                ),
                "decision": (
                    decision.get("decision") if isinstance(decision, dict) else None
                ),
                "confidence": (
                    decision.get("confidence") if isinstance(decision, dict) else None
                ),
                "supporting_message_ids": (
                    decision.get("supporting_message_ids", [])
                    if isinstance(decision, dict)
                    else []
                ),
                "opposing_message_ids": (
                    decision.get("opposing_message_ids", [])
                    if isinstance(decision, dict)
                    else []
                ),
                "unresolved_reason": (
                    decision.get("reason")
                    if isinstance(decision, dict)
                    and decision.get("decision") in {"unresolved", "hold"}
                    else None
                ),
                "next_triggers": triggers if isinstance(triggers, list) else [],
            }

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
        entry_revision_batch = (
            session.query(StrategyRevisionBatch)
            .filter(
                StrategyRevisionBatch.execution_binding_id == binding_id,
                StrategyRevisionBatch.revision_kind == "entry_sizing",
            )
            .order_by(StrategyRevisionBatch.id.desc())
            .first()
            if binding_id is not None
            else None
        )
        entry_revision_replacement_count = (
            session.query(func.count(EntryRevisionReplacement.id))
            .filter(
                EntryRevisionReplacement.revision_batch_id
                == int(entry_revision_batch.id)
            )
            .scalar()
            if entry_revision_batch is not None
            else 0
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
        break_even_convergences = (
            session.query(StrategyBreakEvenConvergence)
            .filter(
                StrategyBreakEvenConvergence.target_lifecycle_id == lifecycle.id
            )
            .order_by(
                StrategyBreakEvenConvergence.planned_at,
                StrategyBreakEvenConvergence.id,
            )
            .all()
        )
        break_even_ids = {int(row.id) for row in break_even_convergences}
        break_even_legs = (
            session.query(StrategyBreakEvenConvergenceLeg)
            .filter(
                StrategyBreakEvenConvergenceLeg.convergence_id.in_(
                    break_even_ids
                )
            )
            .order_by(
                StrategyBreakEvenConvergenceLeg.convergence_id,
                StrategyBreakEvenConvergenceLeg.id,
            )
            .all()
            if break_even_ids
            else []
        )
        entry_leg_ids = {int(row.id) for row in order_legs if row.purpose == "entry"}
        protection_ledger_rows = (
            session.query(PositionProtectionLedger)
            .filter(
                PositionProtectionLedger.execution_order_leg_id.in_(entry_leg_ids),
                PositionProtectionLedger.evidence_source
                == "reconciliation_trigger_entry_adoption",
            )
            .order_by(PositionProtectionLedger.created_at, PositionProtectionLedger.id)
            .all()
            if entry_leg_ids
            else []
        )
        primary_stop_rows = (
            session.query(PositionProtectionLedger)
            .filter(PositionProtectionLedger.execution_order_leg_id.in_(entry_leg_ids))
            .filter(PositionProtectionLedger.purpose.in_(("stop_loss", "combined")))
            .order_by(PositionProtectionLedger.created_at, PositionProtectionLedger.id)
            .all()
            if entry_leg_ids
            else []
        )
        protection_revisions = (
            session.query(PositionProtectionRevision)
            .filter(
                PositionProtectionRevision.execution_binding_id == binding_id,
                PositionProtectionRevision.execution_order_leg_id.in_(entry_leg_ids),
            )
            .order_by(
                PositionProtectionRevision.created_at,
                PositionProtectionRevision.id,
            )
            .all()
            if binding_id is not None and entry_leg_ids
            else []
        )
        protection_refusal_rows = (
            session.query(PositionAttributionAudit)
            .filter(
                PositionAttributionAudit.execution_order_leg_id.in_(entry_leg_ids),
                PositionAttributionAudit.event_type == "protection_adoption_refused",
            )
            .order_by(PositionAttributionAudit.created_at, PositionAttributionAudit.id)
            .all()
            if entry_leg_ids
            else []
        )
        trigger_protection_intents = (
            session.query(TriggerProtectionIntent)
            .filter(TriggerProtectionIntent.execution_order_leg_id.in_(entry_leg_ids))
            .order_by(TriggerProtectionIntent.created_at, TriggerProtectionIntent.id)
            .all()
            if entry_leg_ids
            else []
        )
        trigger_protection_intent_ids = {
            int(row.id) for row in trigger_protection_intents
        }
        trigger_protection_stop_rescues = (
            session.query(TriggerProtectionStopRescue)
            .filter(
                TriggerProtectionStopRescue.trigger_protection_intent_id.in_(
                    trigger_protection_intent_ids
                )
            )
            .order_by(
                TriggerProtectionStopRescue.planned_at,
                TriggerProtectionStopRescue.id,
            )
            .all()
            if trigger_protection_intent_ids
            else []
        )
        take_profit_order_rows = (
            session.query(PositionTakeProfitOrder)
            .filter(PositionTakeProfitOrder.execution_order_leg_id.in_(entry_leg_ids))
            .order_by(PositionTakeProfitOrder.created_at, PositionTakeProfitOrder.id)
            .all()
            if entry_leg_ids
            else []
        )
        backup_stop_rows = (
            session.query(PositionBackupStopOrder)
            .filter(PositionBackupStopOrder.execution_order_leg_id.in_(entry_leg_ids))
            .order_by(PositionBackupStopOrder.created_at, PositionBackupStopOrder.id)
            .all()
            if entry_leg_ids else []
        )
        protection_incident_rows = (
            session.query(PositionProtectionIncident)
            .filter(PositionProtectionIncident.execution_order_leg_id.in_(entry_leg_ids))
            .order_by(PositionProtectionIncident.created_at, PositionProtectionIncident.id)
            .all()
            if entry_leg_ids else []
        )
        trigger_take_profit_convergences = (
            session.query(TriggerTakeProfitConvergence)
            .filter(TriggerTakeProfitConvergence.execution_order_leg_id.in_(entry_leg_ids))
            .order_by(TriggerTakeProfitConvergence.created_at, TriggerTakeProfitConvergence.id)
            .all()
            if entry_leg_ids
            else []
        )
        entry_pos_ids = {
            str(row.pos_id) for row in order_legs
            if row.purpose == "entry" and str(row.pos_id or "")
        }
        authoritative_owner_leg_ids: set[int] = set()
        for entry_pos_id in entry_pos_ids:
            try:
                owner = require_verified_position_ownership(
                    session, venue="deepcoin", pos_id=entry_pos_id
                )
            except PositionAttributionError:
                continue
            authoritative_owner_leg_ids.add(int(owner.id))
        active_position_mutations = (
            session.query(PositionMutationIntent)
            .filter(PositionMutationIntent.pos_id.in_(entry_pos_ids))
            .filter(PositionMutationIntent.status.in_((
                "reserved", "submitted", "submit_unknown", "recovery_required",
            )))
            .all()
            if entry_pos_ids else []
        )
        pending_snapshot = (
            session.query(PendingTpslSnapshotObservation)
            .filter(
                PendingTpslSnapshotObservation.venue == "deepcoin",
                PendingTpslSnapshotObservation.instrument_id
                == normalize_deepcoin_swap_instrument(binding.symbol),
            )
            .order_by(
                PendingTpslSnapshotObservation.observed_at.desc(),
                PendingTpslSnapshotObservation.id.desc(),
            )
            .first()
            if binding is not None else None
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
    protection_adoption = _protection_adoption_detail(
        order_legs=order_legs,
        ledger_rows=protection_ledger_rows,
        refusal_rows=protection_refusal_rows,
    )
    protection_revision_history = _protection_revision_detail(
        revisions=protection_revisions,
        order_legs=order_legs,
        binding_id=binding_id,
    )
    trigger_protection_recovery = _trigger_protection_recovery_detail(
        intents=trigger_protection_intents,
        rescues=trigger_protection_stop_rescues,
        order_legs=order_legs,
    )
    take_profit_orders = _take_profit_order_detail(
        order_legs=order_legs,
        rows=take_profit_order_rows,
        convergences=trigger_take_profit_convergences,
    )
    for row in protection_adoption["adopted_rows"]:
        timeline.append(
            _detail_timeline_item(
                kind="protection_adopted",
                timestamp=row.created_at,
                database_id=int(row.id),
                event_id=f"protection_adopted:{int(row.id)}",
                source={
                    "table": "position_protection_ledger",
                    "id": int(row.id),
                    "execution_order_leg_id": int(row.execution_order_leg_id),
                    "order_id": str(row.order_id),
                },
                status="保护单归属已验证",
            )
        )
    for row, refusal_code in protection_adoption["refusal_rows"]:
        timeline.append(
            _detail_timeline_item(
                kind="protection_adoption_refused",
                timestamp=row.created_at,
                database_id=int(row.id),
                event_id=f"protection_adoption_refused:{int(row.id)}",
                source={
                    "table": "position_attribution_audits",
                    "id": int(row.id),
                    "execution_order_leg_id": int(row.execution_order_leg_id),
                    "reason_code": refusal_code,
                },
                status="保护单归属未验证",
            )
        )
    for row in trigger_protection_recovery:
        timeline.append(
            _detail_timeline_item(
                kind="trigger_protection_recovery",
                timestamp=row["timestamp"],
                database_id=int(row["intent_id"]),
                event_id=f"trigger_protection_recovery:{int(row['intent_id'])}",
                source={
                    "table": "trigger_protection_intents",
                    "id": int(row["intent_id"]),
                    "execution_order_leg_id": int(row["execution_order_leg_id"]),
                    "parent_order_id": row["parent_order_id"],
                    "pos_id": row["pos_id"],
                    "recovery_state": row["recovery_state"],
                    "retry_attempts": row["retry_attempts"],
                    "adopted_tpsl_order_ids": row["adopted_tpsl_order_ids"],
                    "refusal_code": row["refusal_code"],
                    "stop_rescue_state": row["stop_rescue"]["state"],
                },
                status=str(row["recovery_state"]),
            )
        )
    for row in protection_revision_history:
        timeline.append(
            _detail_timeline_item(
                kind="protection_revision",
                timestamp=row["timestamp"],
                database_id=int(row["id"]),
                event_id=f"protection_revision:{int(row['id'])}",
                source={
                    "table": "position_protection_revisions",
                    "id": int(row["id"]),
                    "pos_id": str(row["pos_id"]),
                    "source": str(row["source"]),
                    "status": str(row["status"]),
                },
                status=str(row["status"]),
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
        "context_resolution": context_resolution,
        "source_deletion": source_deletion,
        "timeline": timeline,
        "execution": {
            "binding": _binding_detail(binding) if binding is not None else None,
            "entry_revision": (
                format_entry_revision_summary(
                    {
                        "status": entry_revision_batch.status,
                        "reason_code": entry_revision_batch.reason_code,
                        "replacement_count": entry_revision_replacement_count,
                        "market_snapshot": _safe_json_value(
                            entry_revision_batch.market_snapshot_json
                        ),
                    }
                )
                if entry_revision_batch is not None
                else None
            ),
            "order_legs": [_order_leg_detail(row) for row in order_legs],
            "position_ids": _verified_active_entry_leg_position_ids(order_legs),
            "position_ids_authoritative": any(
                row.purpose == "entry" for row in order_legs
            ),
            "protection_adoption": {
                "state": protection_adoption["state"],
                "order_ids": protection_adoption["order_ids"],
                "evidence_sources": protection_adoption["evidence_sources"],
                "refusal_codes": protection_adoption["refusal_codes"],
            },
            "protection_revisions": protection_revision_history,
            "trigger_protection_recovery": [
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"timestamp", "execution_order_leg_id"}
                }
                for row in trigger_protection_recovery
            ],
            "take_profit_orders": take_profit_orders,
            "backup_stops": [_backup_stop_detail(row) for row in backup_stop_rows],
            "protection_states": _protection_state_detail(
                order_legs=order_legs,
                primary_rows=primary_stop_rows,
                backup_rows=backup_stop_rows,
                incidents=protection_incident_rows,
                intents=trigger_protection_intents,
                convergences=trigger_take_profit_convergences,
                active_mutations=active_position_mutations,
                snapshot_complete=bool(
                    pending_snapshot is not None and pending_snapshot.complete
                ),
                authoritative_owner_leg_ids=authoritative_owner_leg_ids,
            ),
            "protection_incidents": [
                _protection_incident_detail(row) for row in protection_incident_rows
            ],
            "events": [_execution_event_detail(row) for row in execution_events],
            "management_batches": [
                _management_batch_detail(
                    row,
                    legs=legs_by_batch_id.get(int(row.id), []),
                )
                for row in management_batches
            ],
            "break_even_convergences": [
                _break_even_convergence_detail(
                    row,
                    legs=[
                        leg
                        for leg in break_even_legs
                        if int(leg.convergence_id) == int(row.id)
                    ],
                )
                for row in break_even_convergences
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


def _protection_adoption_detail(
    *,
    order_legs: list[ExecutionOrderLeg],
    ledger_rows: list[PositionProtectionLedger],
    refusal_rows: list[PositionAttributionAudit],
) -> dict[str, object]:
    """Project only exact trigger-entry ownership evidence already in the DB."""

    verified_entry_pos_ids = {
        int(leg.id): str(leg.pos_id)
        for leg in order_legs
        if leg.purpose == "entry"
        and str(leg.attribution_status or "") == "verified"
        and str(leg.pos_id or "").strip()
    }
    adopted_rows = [
        row
        for row in ledger_rows
        if verified_entry_pos_ids.get(int(row.execution_order_leg_id)) == str(row.pos_id)
    ]
    parsed_refusals: list[tuple[PositionAttributionAudit, str]] = []
    for row in refusal_rows:
        if verified_entry_pos_ids.get(int(row.execution_order_leg_id or 0)) != str(
            row.pos_id or ""
        ):
            continue
        evidence = _safe_json_value(row.evidence_json)
        reason = evidence.get("reason") if isinstance(evidence, dict) else None
        if isinstance(reason, str) and reason.strip():
            parsed_refusals.append((row, reason.strip()))
    order_ids = sorted({str(row.order_id) for row in adopted_rows})
    refusal_codes = sorted({reason for _row, reason in parsed_refusals})
    return {
        "state": "adopted" if order_ids else "refused" if refusal_codes else "unverified",
        "order_ids": order_ids,
        "evidence_sources": sorted({str(row.evidence_source) for row in adopted_rows}),
        "refusal_codes": refusal_codes,
        "adopted_rows": adopted_rows,
        "refusal_rows": parsed_refusals,
    }


def _source_deletion_detail(
    deletion_exit: SourceMessageDeletionExit | None,
    *,
    event: TelegramSourceMessageEvent | None = None,
) -> dict[str, object] | None:
    if deletion_exit is None:
        return None
    cancellation_ids = _safe_json_value(
        deletion_exit.cancellation_signal_ids_json
    )
    flat_proof = _safe_json_value(deletion_exit.flat_proof_json)
    return {
        "event_id": int(deletion_exit.source_event_id),
        "exit_id": int(deletion_exit.id),
        "state": str(deletion_exit.state),
        "reason": deletion_exit.last_reason,
        "attempt_count": int(deletion_exit.attempt_count or 0),
        "management_batch_id": (
            int(deletion_exit.management_batch_id)
            if deletion_exit.management_batch_id is not None
            else None
        ),
        "cancellation_signal_ids": (
            cancellation_ids if isinstance(cancellation_ids, list) else []
        ),
        "flat_proof_confirmed": bool(deletion_exit.flat_proof_json),
        "flat_proof": flat_proof if isinstance(flat_proof, dict) else None,
        "recovery_required": deletion_exit.state == "recovery_required",
        "completed_at": deletion_exit.completed_at,
        "deleted_at": event.occurred_at if event is not None else None,
        "event_processing_status": (
            event.processing_status if event is not None else None
        ),
    }


def _protection_revision_detail(
    *,
    revisions: list[PositionProtectionRevision],
    order_legs: list[ExecutionOrderLeg],
    binding_id: int | None,
) -> list[dict[str, object]]:
    """Project only history tied to this strategy's verified entry position."""

    exact_positions_by_leg_id = {
        int(leg.id): str(leg.pos_id)
        for leg in order_legs
        if leg.purpose == "entry"
        and str(leg.attribution_status or "") == "verified"
        and str(leg.pos_id or "").strip()
    }
    projected: list[dict[str, object]] = []
    for revision in revisions:
        if binding_id is None or int(revision.execution_binding_id) != binding_id:
            continue
        if exact_positions_by_leg_id.get(int(revision.execution_order_leg_id)) != str(
            revision.pos_id
        ):
            continue
        projected.append(
            {
                "id": int(revision.id),
                "pos_id": str(revision.pos_id),
                "previous_revision_id": revision.previous_revision_id,
                "source": str(revision.source),
                "status": str(revision.status),
                "protection": _safe_json_value(revision.protection_json),
                "timestamp": _as_utc(revision.updated_at or revision.created_at),
            }
        )
    return projected


def _trigger_protection_recovery_detail(
    *,
    intents: list[TriggerProtectionIntent],
    rescues: list[TriggerProtectionStopRescue],
    order_legs: list[ExecutionOrderLeg],
) -> list[dict[str, object]]:
    """Project recovery state without exposing payloads or message content."""

    exact_legs = {
        int(leg.id): (int(leg.execution_binding_id), str(leg.pos_id))
        for leg in order_legs
        if leg.purpose == "entry"
        and str(leg.attribution_status or "") == "verified"
        and str(leg.pos_id or "").strip()
    }
    rescues_by_intent = {
        int(rescue.trigger_protection_intent_id): rescue for rescue in rescues
    }
    projected: list[dict[str, object]] = []
    for intent in intents:
        exact_leg = exact_legs.get(int(intent.execution_order_leg_id))
        if exact_leg is None:
            continue
        binding_id, pos_id = exact_leg
        if int(intent.execution_binding_id) != binding_id:
            continue
        rescue = rescues_by_intent.get(int(intent.id))
        rescue_matches_position = (
            rescue is not None
            and int(rescue.execution_binding_id) == binding_id
            and int(rescue.execution_order_leg_id) == int(intent.execution_order_leg_id)
            and int(rescue.execution_binding_id) == int(intent.execution_binding_id)
            and str(rescue.pos_id) == pos_id
        )
        refusal_code = (
            _bounded_reason_code(rescue.reason_code)
            if rescue_matches_position and rescue is not None
            else None
        )
        rescue_state = str(rescue.status) if rescue_matches_position and rescue else "none"
        adopted_order_ids = (
            [str(intent.adopted_order_id)] if intent.adopted_order_id else []
        )
        projected.append(
            {
                "intent_id": int(intent.id),
                "execution_order_leg_id": int(intent.execution_order_leg_id),
                "timestamp": intent.updated_at or intent.created_at,
                "parent_order_id": intent.parent_trigger_order_id,
                "pos_id": pos_id,
                "recovery_state": str(intent.recovery_state),
                **(
                    {
                        "recovery_disposition": _bounded_reason_code(
                            intent.recovery_disposition
                        )
                    }
                    if intent.recovery_disposition
                    else {}
                ),
                "retry_attempts": int(intent.retry_attempts),
                "adopted_tpsl_order_ids": adopted_order_ids,
                "refusal_code": refusal_code,
                "stop_rescue": {"state": rescue_state},
            }
        )
    return projected


def _take_profit_order_detail(
    *,
    order_legs: list[ExecutionOrderLeg],
    rows: list[PositionTakeProfitOrder],
    convergences: list[TriggerTakeProfitConvergence],
) -> list[dict[str, object]]:
    """Project active TP orders separately from permanent terminal history."""

    exact_legs = {
        int(leg.id): str(leg.pos_id)
        for leg in order_legs
        if leg.purpose == "entry" and str(leg.pos_id or "").strip()
    }
    convergences_by_leg = {
        int(row.execution_order_leg_id): row for row in convergences
        if int(row.execution_order_leg_id) in exact_legs
    }
    rows_by_leg: dict[int, list[PositionTakeProfitOrder]] = defaultdict(list)
    for row in rows:
        if exact_legs.get(int(row.execution_order_leg_id)) != str(row.pos_id):
            continue
        rows_by_leg[int(row.execution_order_leg_id)].append(row)
    result: list[dict[str, object]] = []
    for leg_id in sorted(set(rows_by_leg) | set(convergences_by_leg)):
        current = rows_by_leg.get(leg_id, [])
        active = [row for row in current if str(row.status) == "active"]
        history = [row for row in current if str(row.status) != "active"]
        convergence = convergences_by_leg.get(leg_id)
        result.append(
            {
                "execution_order_leg_id": leg_id,
                "pos_id": exact_legs[leg_id],
                "active": [_take_profit_order_item(row) for row in active],
                "history": [_take_profit_order_item(row) for row in history],
                "convergence": (
                    {
                        "status": str(convergence.status),
                        "reason_code": _bounded_reason_code(convergence.reason_code),
                    }
                    if convergence is not None
                    else None
                ),
            }
        )
    return result


def _backup_stop_detail(row: PositionBackupStopOrder) -> dict[str, object]:
    return {
        "pos_id": str(row.pos_id),
        "order_id": str(row.order_id) if row.order_id else None,
        "trigger_price": str(row.trigger_price),
        "status": str(row.status),
    }


def _protection_state_detail(
    *,
    order_legs: list[ExecutionOrderLeg],
    primary_rows: list[PositionProtectionLedger],
    backup_rows: list[PositionBackupStopOrder],
    incidents: list[PositionProtectionIncident],
    intents: list[TriggerProtectionIntent] = (),
    convergences: list[TriggerTakeProfitConvergence] = (),
    active_mutations: list[PositionMutationIntent] = (),
    snapshot_complete: bool = False,
    authoritative_owner_leg_ids: set[int] = frozenset(),
) -> list[dict[str, object]]:
    """Project exact primary/backup protection health without exposing raw payloads."""

    exact_legs = {
        (int(leg.id), str(leg.pos_id))
        for leg in order_legs
        if leg.purpose == "entry" and leg.id is not None and str(leg.pos_id or "")
    }
    result: list[dict[str, object]] = []
    for leg_id, pos_id in sorted(exact_legs, key=lambda item: item[1]):
        primary = next(
            (
                row for row in reversed(primary_rows)
                if int(row.execution_order_leg_id) == leg_id and str(row.pos_id) == pos_id
            ),
            None,
        )
        backup = next(
            (
                row for row in reversed(backup_rows)
                if int(row.execution_order_leg_id) == leg_id and str(row.pos_id) == pos_id
            ),
            None,
        )
        matching_incidents = [
            row for row in incidents
            if int(row.execution_order_leg_id) == leg_id and str(row.pos_id) == pos_id
        ]
        matching_intents = [
            row for row in intents if int(row.execution_order_leg_id) == leg_id
        ]
        intent = matching_intents[-1] if matching_intents else None
        convergence = next(
            (
                row for row in reversed(convergences)
                if int(row.execution_order_leg_id) == leg_id
                and str(row.pos_id or pos_id) == pos_id
            ),
            None,
        )
        primary_status = str(primary.status) if primary is not None else "not_verified"
        backup_status = str(backup.status) if backup is not None else "not_created"
        position_owner_verified = any(
            int(leg.id) == leg_id
            and str(leg.attribution_status or "") == "verified"
            and str(leg.pos_id or "") == pos_id
            and has_authoritative_persisted_position(leg)
            and int(leg.id) in authoritative_owner_leg_ids
            for leg in order_legs
        )
        exact_backup_stop_verified = bool(
            backup is not None
            and backup_status == "active"
            and str(backup.order_id or "").strip()
        )
        native_stop_assignment_pending = bool(
            intent is not None
            and str(intent.recovery_state) in {"pending", "retrying", "failed"}
            and str(intent.recovery_disposition or "retry") in {"retry", "exact_backup"}
            and primary_status not in {"verified", "active"}
        )
        convergence_status = str(convergence.status) if convergence is not None else "none"
        take_profit_convergence_waiting = convergence_status in {
            "waiting_position", "waiting_backup_stop"
        }
        take_profit_convergence_ready = convergence_status in {
            "ready", "reserved", "submitted"
        }
        manual_review_required = bool(
            intent is not None
            and str(intent.recovery_disposition or "") in {"manual_review", "terminal"}
        ) or bool(
            convergence is not None
            and (
                convergence_status == "submit_unknown"
                or str(convergence.reason_code or "")
                in {"convergence_exact_leg_not_verified", "convergence_submit_unknown"}
            )
        )
        active_or_unknown_mutation = any(
            str(row.pos_id) == pos_id for row in active_mutations
        )
        conflicting_unknown_take_profit = bool(
            convergence is not None
            and (
                convergence_status == "submit_unknown"
                or "unowned" in str(convergence.reason_code or "")
                or "immutable" in str(convergence.reason_code or "")
            )
        )
        capabilities = evaluate_position_management_capabilities(
            exact_position_verified=position_owner_verified,
            native_stop_owned=primary_status in {"verified", "active"},
            exact_owned_stop=(
                primary_status in {"verified", "active"}
                or exact_backup_stop_verified
            ),
            conflicting_unknown_take_profit=conflicting_unknown_take_profit,
            retained_take_profit_safe=not conflicting_unknown_take_profit,
            snapshot_complete=snapshot_complete,
            active_or_unknown_mutation=active_or_unknown_mutation,
        )
        risk_reduction_capability_available = bool(
            not manual_review_required
            and (
                capabilities.may_reduce_exact_position
                or capabilities.may_close_exact_position
            )
        )
        blocker = next(
            (
                str(row.incident_type)
                for row in reversed(matching_incidents)
                if str(row.incident_type) != "stop_trigger_failed"
            ),
            None,
        )
        if not position_owner_verified:
            message = "持仓归属未验证"
        elif manual_review_required:
            message = "仓位管理需要人工复核"
        elif native_stop_assignment_pending and risk_reduction_capability_available:
            message = "原生止损归属待恢复；精确仓位可继续风险降低操作"
        elif primary_status == "stop_trigger_failed" and backup_status == "active":
            message = "主止损失败，第二止损有效"
        elif backup is None:
            message = "第二止损未创建/证据未知"
        elif backup_status == "unknown_exchange_outcome":
            message = "第二止损提交结果未知"
        elif primary_status in {"protection_missing", "not_verified"}:
            message = "主止损证据未知，自动管理已冻结"
        else:
            message = "主止损与第二止损已记录"
        result.append(
            {
                "pos_id": pos_id,
                "primary_stop_price": str(primary.trigger_price) if primary and primary.trigger_price else None,
                "primary_stop_status": primary_status,
                "primary_order_id": str(primary.order_id) if primary else None,
                "backup_stop_price": str(backup.trigger_price) if backup else None,
                "backup_stop_status": backup_status,
                "backup_order_id": str(backup.order_id) if backup and backup.order_id else None,
                "backup_stop_blocker": blocker,
                "position_owner_verified": position_owner_verified,
                "native_stop_assignment_pending": native_stop_assignment_pending,
                "exact_backup_stop_verified": exact_backup_stop_verified,
                "take_profit_convergence_waiting": take_profit_convergence_waiting,
                "take_profit_convergence_ready": take_profit_convergence_ready,
                "risk_reduction_capability_available": risk_reduction_capability_available,
                "manual_review_required": manual_review_required,
                "operator_message": message,
            }
        )
    return result


def _protection_incident_detail(row: PositionProtectionIncident) -> dict[str, object]:
    evidence = _safe_json_value(row.evidence_json)
    exchange = evidence.get("exchange") if isinstance(evidence, dict) else {}
    code = str(exchange.get("errorCode") or "") if isinstance(exchange, dict) else ""
    message = str(exchange.get("errorMsg") or "") if isinstance(exchange, dict) else ""
    return {
        "pos_id": str(row.pos_id),
        "incident_type": str(row.incident_type),
        "delivery_status": str(row.delivery_status),
        "error": ": ".join(part for part in (code, message) if part),
    }


def _take_profit_order_item(row: PositionTakeProfitOrder) -> dict[str, object]:
    return {
        "order_id": str(row.order_id),
        "price": str(row.trigger_price),
        "size": row.size_text,
        "status": str(row.status),
        "created_at": _as_utc(row.created_at),
        "cancel_requested_at": _as_utc(row.cancel_requested_at),
        "completed_at": _as_utc(row.completed_at),
    }


def _bounded_reason_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip()
    if not code or len(code) > 96 or not re.fullmatch(r"[a-z0-9_:-]+", code):
        return "unknown"
    return code


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
    payload = _safe_json_value(row.payload_json)
    assembly_evidence = None
    if isinstance(payload, dict):
        draft = payload.get("draft")
        if isinstance(draft, dict):
            assembly_evidence = draft.get("entry_preamble_assembly")
        if assembly_evidence is None:
            assembly_evidence = payload.get("entry_preamble_assembly")
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
        "payload": payload,
        "entry_preamble_assembly": format_entry_assembly_summary(
            assembly_evidence
        ),
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


def _break_even_convergence_detail(
    row: StrategyBreakEvenConvergence,
    *,
    legs: list[StrategyBreakEvenConvergenceLeg],
) -> dict[str, object]:
    return {
        "id": int(row.id),
        "strategy_instance_id": row.strategy_instance_id,
        "trigger_type": row.trigger_type,
        "trigger_identity": row.trigger_identity,
        "trigger_evidence": _safe_json_value(row.trigger_evidence_json),
        "execution_mode": row.execution_mode,
        "status": row.status,
        "reason_code": row.reason_code,
        "planned_at": _as_utc(row.planned_at),
        "completed_at": _as_utc(row.completed_at),
        "legs": [
            {
                "id": int(leg.id),
                "execution_order_leg_id": int(leg.execution_order_leg_id),
                "pos_id": leg.pos_id,
                "preflight_size": leg.preflight_size,
                "avg_entry_price": leg.avg_entry_price,
                "decision": _safe_json_value(leg.decision_json),
                "status": leg.status,
                "reason_code": leg.reason_code,
                "mutation_intent_id": leg.mutation_intent_id,
                "exchange_order_id": leg.exchange_order_id,
            }
            for leg in legs
        ],
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
        deletion_exits = (
            session.query(SourceMessageDeletionExit)
            .filter(SourceMessageDeletionExit.target_lifecycle_id.in_(lifecycle_ids))
            .order_by(SourceMessageDeletionExit.id.desc())
            .all()
        )
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
        take_profit_convergences = (
            session.query(TriggerTakeProfitConvergence)
            .filter(TriggerTakeProfitConvergence.execution_binding_id.in_(binding_ids))
            .all()
            if binding_ids
            else []
        )

    events_by_binding_id, events_by_strategy_instance_id = _index_events(events)
    batches_by_lifecycle_id: dict[int, list[StrategyManagementBatch]] = defaultdict(list)
    for batch in management_batches:
        batches_by_lifecycle_id[int(batch.target_lifecycle_id)].append(batch)
    deletion_exit_by_lifecycle_id = {
        int(row.target_lifecycle_id): row
        for row in deletion_exits
        if row.target_lifecycle_id is not None
    }
    management_legs_by_batch_id: dict[int, list[StrategyManagementLeg]] = defaultdict(list)
    for leg in management_legs:
        management_legs_by_batch_id[int(leg.management_batch_id)].append(leg)
    management_message_id_by_raw_id = {
        int(row.id): int(row.message_id) for row in management_raw_messages
    }
    take_profit_convergences_by_binding_id: dict[int, list[TriggerTakeProfitConvergence]] = defaultdict(list)
    for convergence in take_profit_convergences:
        take_profit_convergences_by_binding_id[int(convergence.execution_binding_id)].append(convergence)
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
        deletion_exit = deletion_exit_by_lifecycle_id.get(int(lifecycle.id))
        attention_reasons = _attention_reasons(
            lifecycle=lifecycle,
            candidate=candidate,
            decision=decision,
            recognition=recognition,
            binding=binding,
            events=lifecycle_events,
            management_batches=batches,
            take_profit_convergences=take_profit_convergences_by_binding_id.get(int(binding.id), []) if binding else [],
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
                "source_deletion": _source_deletion_detail(deletion_exit),
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
    take_profit_unknown = exists().where(
        TriggerTakeProfitConvergence.execution_binding_id == ExecutionBinding.id,
        TriggerTakeProfitConvergence.status == "submit_unknown",
    )
    take_profit_conflicted = exists().where(
        TriggerTakeProfitConvergence.execution_binding_id == ExecutionBinding.id,
        TriggerTakeProfitConvergence.status.in_(("conflicted", "blocked")),
    )
    critical = or_(
        recognition_evidence_missing,
        recognition_failed,
        entered_without_binding,
        missing_stop,
        execution_failed,
        take_profit_unknown,
    )
    severity_expression = case(
        (critical, 0),
        (or_(management_unconfirmed, management_blocked, take_profit_conflicted), 1),
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
                take_profit_unknown,
                take_profit_conflicted,
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
    take_profit_convergences: list[TriggerTakeProfitConvergence] = (),
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
    if any(str(row.status) == "submit_unknown" for row in take_profit_convergences):
        codes.append("take_profit_convergence_unknown")
    elif any(str(row.status) in {"conflicted", "blocked"} for row in take_profit_convergences):
        codes.append("take_profit_convergence_conflicted")

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
