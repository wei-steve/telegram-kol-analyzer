"""Dedicated Telegram bot for operator decisions required by the system."""

from __future__ import annotations

import os
import json
import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from telegram_kol_research.config import (
    RuntimeIncidentConfig,
    load_runtime_incident_config,
)
from telegram_kol_research.entry_admission_reconciler import (
    reconcile_due_entry_admissions,
)
from telegram_kol_research.instruction_execution_reconciliation import (
    reconcile_instruction_execution_contracts,
)
from telegram_kol_research.llm_chat import _load_env_file_values
from telegram_kol_research.message_instruction_items import (
    FINISH_STATUSES,
    SUMMARY_NOTIFICATION_LEASE,
    claim_message_instruction_summary,
    finish_message_instruction_summary_delivery,
)
from telegram_kol_research.time_utils import utc_naive_to_local
from telegram_kol_research.runtime_worker_executor import (
    run_on_management_worker,
)
from telegram_kol_research.reporting import (
    format_entry_assembly_summary,
    format_entry_revision_summary,
)
from telegram_kol_research.trading_settings import load_trading_settings


logger = logging.getLogger(__name__)
RUNTIME_INCIDENT_MESSAGE_MAX_CHARS = 1200
AI_AGENT_NOTIFICATION_PREFIX = "AI agent通知：（"
TERMINAL_ENTRY_CLEANUP_NOTIFICATION_MAX_ATTEMPTS = 5
TERMINAL_ENTRY_CLEANUP_NOTIFICATION_LEASE_SECONDS = 120
MESSAGE_OPERATION_STAGE1_MAX_ATTEMPTS = 5
MESSAGE_OPERATION_STAGE2_MAX_ATTEMPTS = 5


@dataclass(slots=True)
class SystemOperatorBotConfig:
    bot_token: str
    chat_id: str
    timeout_seconds: float = 10.0


NotificationBotConfig = SystemOperatorBotConfig


def format_entry_revision_operator_notification(
    *,
    assembly_evidence: object,
    revision_evidence: object,
) -> str | None:
    """Render only bounded shared projections for an entry revision handoff."""

    assembly = format_entry_assembly_summary(assembly_evidence)
    revision = format_entry_revision_summary(revision_evidence)
    if assembly is None or revision is None:
        return None
    lines = [
        "入场组装/修订状态",
        str(assembly.get("state_label") or "历史入场仓位组装"),
        str(assembly["risk_calculation"]),
        str(revision["label"]),
    ]
    if revision.get("remaining_headroom"):
        lines.append(str(revision["remaining_headroom"]))
    if revision.get("reason_code"):
        lines.append(f"原因代码: {revision['reason_code']}")
    if revision.get("orders_changed") is True:
        lines.append("订单已变更: 是（已读回确认）")
    return "\n".join(lines)[:RUNTIME_INCIDENT_MESSAGE_MAX_CHARS]


def load_system_operator_bot_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> SystemOperatorBotConfig:
    paths = (
        [
            ".env",
            "config/telegram.env",
            "config/system_operator_bot.env",
        ]
        if env_file_paths is None
        else env_file_paths
    )
    env = dict(
        _load_env_file_values(paths) if paths else {}
    )
    env.update(environ or os.environ)
    return SystemOperatorBotConfig(
        bot_token=env.get("TELEGRAM_KOL_SYSTEM_BOT_TOKEN", ""),
        chat_id=env.get("TELEGRAM_KOL_SYSTEM_BOT_CHAT_ID", ""),
        timeout_seconds=float(env.get("TELEGRAM_KOL_SYSTEM_BOT_TIMEOUT_SECONDS", "10")),
    )


def load_notification_bot_config(
    environ: dict[str, str] | None = None,
    env_file_paths: list[str | os.PathLike[str]] | None = None,
) -> NotificationBotConfig:
    paths = (
        [
            ".env",
            "config/telegram.env",
            "config/system_operator_bot.env",
        ]
        if env_file_paths is None
        else env_file_paths
    )
    env = dict(_load_env_file_values(paths) if paths else {})
    env.update(environ or os.environ)
    return NotificationBotConfig(
        bot_token=env.get("TELEGRAM_KOL_NOTIFICATION_BOT_TOKEN", ""),
        chat_id=env.get("TELEGRAM_KOL_NOTIFICATION_BOT_CHAT_ID", ""),
        timeout_seconds=float(
            env.get("TELEGRAM_KOL_NOTIFICATION_BOT_TIMEOUT_SECONDS", "10")
        ),
    )


def system_operator_bot_enabled(config: SystemOperatorBotConfig | None) -> bool:
    return bool(config and config.bot_token and config.chat_id)


_TELEGRAM_EVIDENCE_TIMEOUT_SECONDS = 5.0


async def probe_system_operator_bot_evidence(
    *,
    config: SystemOperatorBotConfig,
) -> dict[str, bool]:
    """Fetch fixed read-only Bot API and target-chat availability evidence."""

    if not system_operator_bot_enabled(config):
        raise RuntimeError("Telegram bot evidence configuration unavailable")
    base_url = f"https://api.telegram.org/bot{config.bot_token}"
    async with httpx.AsyncClient(
        timeout=_TELEGRAM_EVIDENCE_TIMEOUT_SECONDS,
        trust_env=False,
    ) as client:
        identity_response, chat_response = await asyncio.wait_for(
            asyncio.gather(
                client.get(f"{base_url}/getMe"),
                client.post(
                    f"{base_url}/getChat",
                    json={"chat_id": config.chat_id},
                ),
            ),
            timeout=_TELEGRAM_EVIDENCE_TIMEOUT_SECONDS,
        )

    def response_ok(response: httpx.Response) -> bool:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return False
        return (
            response.status_code == 200
            and isinstance(payload, dict)
            and payload.get("ok") is True
        )

    return {
        "probe_complete": True,
        "endpoint_reachable": True,
        "bot_identity_available": response_ok(identity_response),
        "target_chat_available": response_ok(chat_response),
    }


def wrap_ai_agent_notification(content: str, *, limit: int = 3500) -> str:
    """Apply the single operator-visible AI Agent notification boundary."""

    bounded_limit = max(1, int(limit))
    safe_lines = [
        _safe_runtime_incident_value(line, limit=bounded_limit)
        for line in str(content or "-").splitlines()
    ]
    bounded = _truncate_text("\n".join(safe_lines), limit=bounded_limit)
    return f"{AI_AGENT_NOTIFICATION_PREFIX}{bounded}）"


def format_runtime_incident_notification(incident) -> str:
    """Render one bounded deterministic report without AI interpretation."""

    try:
        summary = json.loads(incident.redacted_summary or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    labels = {
        "component": "组件",
        "source_status": "源状态",
        "reason_code": "原因代码",
        "error_code": "错误代码",
        "error_type": "错误类型",
        "provider_status": "提供方状态",
        "notification_status": "通知状态",
        "containment": "当前遏制",
        "impact": "影响",
    }
    lines = [
        "运行异常",
        f"事件ID: {int(incident.id)}",
        f"类型: {_safe_runtime_incident_value(incident.incident_type, limit=64)}",
        f"严重程度: {_safe_runtime_incident_value(incident.severity, limit=16)}",
        (
            "来源: "
            f"{_safe_runtime_incident_value(incident.source_kind, limit=64)}:"
            f"{_safe_runtime_incident_value(incident.source_record_id, limit=255)}"
        ),
        f"重复次数: {max(1, int(incident.repeat_count or 1))}",
    ]
    for key, label in labels.items():
        if key in summary:
            lines.append(
                f"{label}: {_safe_runtime_incident_value(summary[key], limit=180)}"
            )
    lines.extend(
        (
            "AI诊断: 未启用",
            "自动操作: 未执行",
            "处理: 已记录，正常交易流程未等待本通知。",
        )
    )
    return wrap_ai_agent_notification(
        "\n".join(lines),
        limit=(
            RUNTIME_INCIDENT_MESSAGE_MAX_CHARS
            - len(AI_AGENT_NOTIFICATION_PREFIX)
            - 1
        ),
    )


def format_message_operation_stage1_notification(
    *,
    notification,
    incident,
    raw_message,
    contract,
) -> str:
    """Render one immediate bounded alert without an AI hypothesis."""

    try:
        summary = json.loads(incident.redacted_summary or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    source_time = raw_message.posted_at or raw_message.created_at
    source_text = _safe_runtime_incident_value(
        raw_message.text or "-", limit=320
    )
    impact = _safe_runtime_incident_value(
        summary.get("impact") or "请求的操作尚未获得可验证结果",
        limit=180,
    )
    lines = [
        "消息操作异常（第1阶段）",
        f"事件ID: {int(incident.id)}",
        f"源消息ID: {int(notification.raw_message_id)}",
        f"源时间: {source_time.isoformat() if source_time else '-'}",
        f"原消息: {source_text}",
        (
            "可执行意图: "
            f"{_safe_runtime_incident_value(contract.intent_kind, limit=32)}"
        ),
        (
            "失败检查点: "
            f"{_safe_runtime_incident_value(contract.violation_code, limit=64)}"
        ),
        f"已知影响: {impact}",
        "状态: 只读AI调查进行中",
        "自动操作: 未执行",
    ]
    return wrap_ai_agent_notification(
        "\n".join(lines),
        limit=(
            RUNTIME_INCIDENT_MESSAGE_MAX_CHARS
            - len(AI_AGENT_NOTIFICATION_PREFIX)
            - 1
        ),
    )


def _message_operation_stage1_claimable(
    *, now: datetime, lease_seconds: float, max_attempts: int
):
    from telegram_kol_research.models import MessageOperationStage1Notification

    stale_before = now - timedelta(seconds=max(5.0, float(lease_seconds)))
    return and_(
        MessageOperationStage1Notification.attempt_count < int(max_attempts),
        or_(
            MessageOperationStage1Notification.status == "pending",
            and_(
                MessageOperationStage1Notification.status == "failed",
                or_(
                    MessageOperationStage1Notification.next_attempt_at.is_(None),
                    MessageOperationStage1Notification.next_attempt_at <= now,
                ),
            ),
            and_(
                MessageOperationStage1Notification.status == "delivering",
                or_(
                    MessageOperationStage1Notification.claimed_at.is_(None),
                    MessageOperationStage1Notification.claimed_at <= stale_before,
                ),
            ),
        ),
    )


def claim_next_message_operation_stage1_notification(
    session_factory,
    *,
    claimed_at: datetime | None = None,
    lease_seconds: float = 120.0,
    max_attempts: int = MESSAGE_OPERATION_STAGE1_MAX_ATTEMPTS,
    after_contract_id: int = 0,
):
    """CAS one pending, retryable, or stale per-message Stage 1 delivery."""

    from telegram_kol_research.models import MessageOperationStage1Notification

    now = claimed_at or datetime.now(UTC)
    claimable = and_(
        _message_operation_stage1_claimable(
            now=now, lease_seconds=lease_seconds, max_attempts=max_attempts
        ),
        MessageOperationStage1Notification.message_operation_contract_id
        > int(after_contract_id),
    )
    with session_factory() as session:
        row_id = session.execute(
            select(MessageOperationStage1Notification.id)
            .where(claimable)
            .order_by(
                MessageOperationStage1Notification.next_attempt_at,
                MessageOperationStage1Notification.id,
            )
            .limit(1)
        ).scalar_one_or_none()
    if row_id is None:
        return None
    token = uuid.uuid4().hex
    with session_factory() as session:
        result = session.execute(
            update(MessageOperationStage1Notification)
            .where(
                MessageOperationStage1Notification.id == row_id,
                claimable,
            )
            .values(
                status="delivering",
                claim_token=token,
                claimed_at=now,
                attempt_count=(
                    MessageOperationStage1Notification.attempt_count + 1
                ),
                next_attempt_at=None,
                error_code=None,
                updated_at=now,
            )
        )
        session.commit()
        if result.rowcount != 1:
            return None
        row = session.get(MessageOperationStage1Notification, row_id)
        session.expunge(row)
        return {"notification": row, "claim_token": token}


async def deliver_message_operation_stage1_notifications(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    after_contract_id: int,
    limit: int = 20,
    claimed_at: datetime | None = None,
    lease_seconds: float = 120.0,
    max_attempts: int = MESSAGE_OPERATION_STAGE1_MAX_ATTEMPTS,
    runtime_config: RuntimeIncidentConfig | None = None,
) -> int:
    """Materialize and deliver Stage 1 independently from Agent diagnosis."""

    from telegram_kol_research.message_operation_supervisor import (
        materialize_message_operation_stage1_outbox,
    )
    from telegram_kol_research.models import (
        MessageOperationContract,
        MessageOperationStage1Notification,
        RawMessage,
        RuntimeIncident,
    )

    operation_now = claimed_at or datetime.now(UTC)
    materialize_message_operation_stage1_outbox(
        session_factory,
        after_contract_id=after_contract_id,
        created_at=operation_now,
        limit=max(1, min(int(limit), 100)),
    )
    delivered = 0
    for _ in range(max(1, min(int(limit), 100))):
        claim = claim_next_message_operation_stage1_notification(
            session_factory,
            claimed_at=operation_now,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
            after_contract_id=after_contract_id,
        )
        if claim is None:
            break
        notification = claim["notification"]
        token = claim["claim_token"]
        with session_factory() as session:
            incident = session.get(RuntimeIncident, notification.runtime_incident_id)
            raw = session.get(RawMessage, notification.raw_message_id)
            contract = session.get(
                MessageOperationContract,
                notification.message_operation_contract_id,
            )
            valid = bool(
                incident is not None
                and incident.incident_type == "message_operation_failure"
                and raw is not None
                and contract is not None
                and contract.raw_message_id == raw.id
                and contract.runtime_incident_id == incident.id
                and contract.status == "violated"
            )
            if valid:
                session.expunge(incident)
                session.expunge(raw)
                session.expunge(contract)
        try:
            if not valid:
                raise ValueError("stage1_evidence_incomplete")
            message_id = await send_system_operator_bot_message(
                config=config,
                text=format_message_operation_stage1_notification(
                    notification=notification,
                    incident=incident,
                    raw_message=raw,
                    contract=contract,
                ),
            )
        except Exception as exc:
            retry_at = operation_now + timedelta(
                seconds=max(5.0, float(lease_seconds))
            )
            with session_factory() as session:
                row = session.get(
                    MessageOperationStage1Notification, notification.id
                )
                exhausted = bool(
                    row is not None and row.attempt_count >= int(max_attempts)
                )
                session.execute(
                    update(MessageOperationStage1Notification)
                    .where(
                        MessageOperationStage1Notification.id == notification.id,
                        MessageOperationStage1Notification.status == "delivering",
                        MessageOperationStage1Notification.claim_token == token,
                    )
                    .values(
                        status="exhausted" if exhausted else "failed",
                        claim_token=None,
                        claimed_at=None,
                        next_attempt_at=None if exhausted else retry_at,
                        error_code=type(exc).__name__[:128],
                        updated_at=operation_now,
                    )
                )
                session.commit()
            if runtime_config is not None:
                from telegram_kol_research.runtime_incident_adapters import (
                    capture_notification_failure,
                )

                capture_notification_failure(
                    session_factory,
                    config=runtime_config,
                    source_kind="message_operation_stage1_notification",
                    source_record_id=str(notification.id),
                    error_type=type(exc).__name__,
                    occurred_at=operation_now,
                    severity="high",
                )
            break
        with session_factory() as session:
            result = session.execute(
                update(MessageOperationStage1Notification)
                .where(
                    MessageOperationStage1Notification.id == notification.id,
                    MessageOperationStage1Notification.status == "delivering",
                    MessageOperationStage1Notification.claim_token == token,
                )
                .values(
                    status="delivered",
                    claim_token=None,
                    claimed_at=None,
                    next_attempt_at=None,
                    telegram_message_id=(
                        str(message_id)[:255] if message_id is not None else None
                    ),
                    delivered_at=operation_now,
                    error_code=None,
                    updated_at=operation_now,
                )
            )
            session.commit()
            delivered += int(result.rowcount == 1)
    return delivered


def format_runtime_incident_stage2_notification(artifact, incident) -> str:
    outcome_labels = {
        "diagnosed": "诊断完成",
        "reused": "复用已验证诊断",
        "provider_failed": "模型服务失败",
        "tool_failed": "调查工具失败",
        "evidence_incomplete": "证据不完整",
        "timed_out": "调查超时",
    }
    lines = [
        "消息操作异常（第2阶段，终态）",
        f"事件ID: {int(incident.id)}",
        f"交接ID: {int(artifact.id)}",
        f"交接版本: {int(artifact.diagnosis_revision)}",
        f"调查结果: {outcome_labels.get(artifact.outcome_kind, '未知')}",
        f"SHA256: {artifact.content_fingerprint}",
        "Codex可复制请求:",
        artifact.codex_prompt,
        "完整红化证据已作为JSON文档发送；Agent无Telegram凭据或交易权限。",
    ]
    return wrap_ai_agent_notification("\n".join(lines), limit=3500)


def _require_telegram_message_id(value: Any) -> str:
    if isinstance(value, bool):
        raise RuntimeError("Telegram did not return a durable message id")
    normalized = str(value or "").strip()
    if (
        not normalized.isdigit()
        or int(normalized) <= 0
        or len(normalized) > 255
    ):
        raise RuntimeError("Telegram did not return a durable message id")
    return normalized


def claim_next_runtime_incident_stage2_notification(
    session_factory,
    *,
    after_handoff_id: int,
    claimed_at: datetime | None = None,
    lease_seconds: float = 120.0,
    max_attempts: int = MESSAGE_OPERATION_STAGE2_MAX_ATTEMPTS,
):
    from telegram_kol_research.models import (
        MessageOperationStage1Notification,
        RuntimeIncident,
        RuntimeIncidentHandoffArtifact,
    )

    now = claimed_at or datetime.now(UTC)
    stale_before = now - timedelta(seconds=max(5.0, min(lease_seconds, 3600.0)))
    attempts = max(1, min(int(max_attempts), 20))
    artifact_eligible = and_(
        RuntimeIncidentHandoffArtifact.id > int(after_handoff_id),
        RuntimeIncidentHandoffArtifact.attempt_count < attempts,
        or_(
            RuntimeIncidentHandoffArtifact.status == "pending",
            and_(
                RuntimeIncidentHandoffArtifact.status == "failed",
                or_(
                    RuntimeIncidentHandoffArtifact.next_attempt_at.is_(None),
                    RuntimeIncidentHandoffArtifact.next_attempt_at <= now,
                ),
            ),
            and_(
                RuntimeIncidentHandoffArtifact.status == "delivering",
                or_(
                    RuntimeIncidentHandoffArtifact.claimed_at.is_(None),
                    RuntimeIncidentHandoffArtifact.claimed_at <= stale_before,
                ),
            ),
        ),
    )
    eligible = and_(
        artifact_eligible,
        RuntimeIncident.incident_type == "message_operation_failure",
        exists(
            select(MessageOperationStage1Notification.id).where(
                MessageOperationStage1Notification.runtime_incident_id
                == RuntimeIncidentHandoffArtifact.runtime_incident_id
            )
        ),
        ~exists(
            select(MessageOperationStage1Notification.id).where(
                MessageOperationStage1Notification.runtime_incident_id
                == RuntimeIncidentHandoffArtifact.runtime_incident_id,
                MessageOperationStage1Notification.status.not_in(
                    ("delivered", "exhausted")
                ),
            )
        ),
    )
    token = uuid.uuid4().hex
    with session_factory() as session:
        row_id = (
            session.query(RuntimeIncidentHandoffArtifact.id)
            .join(
                RuntimeIncident,
                RuntimeIncident.id
                == RuntimeIncidentHandoffArtifact.runtime_incident_id,
            )
            .filter(eligible)
            .order_by(RuntimeIncidentHandoffArtifact.id)
            .limit(1)
            .scalar()
        )
        if row_id is None:
            return None
        result = session.execute(
            update(RuntimeIncidentHandoffArtifact)
            .where(
                RuntimeIncidentHandoffArtifact.id == row_id,
                artifact_eligible,
            )
            .values(
                status="delivering",
                claim_token=token,
                claimed_at=now,
                attempt_count=RuntimeIncidentHandoffArtifact.attempt_count + 1,
                next_attempt_at=None,
                error_code=None,
                updated_at=now,
            )
        )
        session.commit()
        if result.rowcount != 1:
            return None
        artifact = session.get(RuntimeIncidentHandoffArtifact, row_id)
        incident = session.get(RuntimeIncident, artifact.runtime_incident_id)
        session.expunge(artifact)
        session.expunge(incident)
        return {"artifact": artifact, "incident": incident, "claim_token": token}


async def deliver_runtime_incident_stage2_notifications(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    after_handoff_id: int,
    limit: int = 20,
    claimed_at: datetime | None = None,
    lease_seconds: float = 120.0,
    max_attempts: int = MESSAGE_OPERATION_STAGE2_MAX_ATTEMPTS,
) -> int:
    from telegram_kol_research.models import RuntimeIncidentHandoffArtifact
    from telegram_kol_research.runtime_incident_adapters import (
        capture_notification_failure,
    )

    now = claimed_at or datetime.now(UTC)
    delivered = 0
    for _ in range(max(1, min(int(limit), 100))):
        claim = claim_next_runtime_incident_stage2_notification(
            session_factory,
            after_handoff_id=after_handoff_id,
            claimed_at=now,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
        if claim is None:
            break
        artifact = claim["artifact"]
        incident = claim["incident"]
        token = claim["claim_token"]
        try:
            message_id = (
                _require_telegram_message_id(artifact.telegram_message_id)
                if artifact.telegram_message_id is not None
                else None
            )
            if message_id is None:
                message_id = _require_telegram_message_id(
                    await send_system_operator_bot_message(
                        config=config,
                        text=format_runtime_incident_stage2_notification(
                            artifact, incident
                        ),
                    )
                )
                with session_factory() as session:
                    persisted = session.execute(
                        update(RuntimeIncidentHandoffArtifact)
                        .where(
                            RuntimeIncidentHandoffArtifact.id == artifact.id,
                            RuntimeIncidentHandoffArtifact.status == "delivering",
                            RuntimeIncidentHandoffArtifact.claim_token == token,
                        )
                        .values(
                            telegram_message_id=(
                                str(message_id)[:255]
                                if message_id is not None else None
                            ),
                            updated_at=now,
                        )
                    )
                    session.commit()
                    if persisted.rowcount != 1:
                        raise RuntimeError("stage2 claim lost after message delivery")
            document_message_id = (
                _require_telegram_message_id(
                    artifact.telegram_document_message_id
                )
                if artifact.telegram_document_message_id is not None
                else None
            )
            if document_message_id is None:
                document_message_id = _require_telegram_message_id(
                    await send_system_operator_bot_document(
                        config=config,
                        filename=f"runtime-incident-handoff-{artifact.id}.json",
                        content=artifact.evidence_document_json,
                        caption=(
                            f"incident={incident.id} handoff={artifact.id} "
                            f"sha256={artifact.content_fingerprint}"
                        ),
                    )
                )
                with session_factory() as session:
                    persisted = session.execute(
                        update(RuntimeIncidentHandoffArtifact)
                        .where(
                            RuntimeIncidentHandoffArtifact.id == artifact.id,
                            RuntimeIncidentHandoffArtifact.status == "delivering",
                            RuntimeIncidentHandoffArtifact.claim_token == token,
                        )
                        .values(
                            telegram_document_message_id=(
                                str(document_message_id)[:255]
                                if document_message_id is not None else None
                            ),
                            updated_at=now,
                        )
                    )
                    session.commit()
                    if persisted.rowcount != 1:
                        raise RuntimeError("stage2 claim lost after document delivery")
        except Exception as exc:
            exhausted = int(artifact.attempt_count) >= max(1, int(max_attempts))
            with session_factory() as session:
                session.execute(
                    update(RuntimeIncidentHandoffArtifact)
                    .where(
                        RuntimeIncidentHandoffArtifact.id == artifact.id,
                        RuntimeIncidentHandoffArtifact.status == "delivering",
                        RuntimeIncidentHandoffArtifact.claim_token == token,
                    )
                    .values(
                        status="exhausted" if exhausted else "failed",
                        claim_token=None,
                        claimed_at=None,
                        next_attempt_at=(None if exhausted else now + timedelta(seconds=5)),
                        error_code=type(exc).__name__[:128],
                        updated_at=now,
                    )
                )
                session.commit()
            capture_notification_failure(
                session_factory,
                config=RuntimeIncidentConfig(
                    capture_types=frozenset({"notification_delivery_failure"})
                ),
                source_kind="runtime_incident_stage2_notification",
                source_record_id=str(artifact.id),
                error_type=type(exc).__name__,
                occurred_at=now,
                severity="high",
            )
            break
        with session_factory() as session:
            result = session.execute(
                update(RuntimeIncidentHandoffArtifact)
                .where(
                    RuntimeIncidentHandoffArtifact.id == artifact.id,
                    RuntimeIncidentHandoffArtifact.status == "delivering",
                    RuntimeIncidentHandoffArtifact.claim_token == token,
                )
                .values(
                    status="delivered",
                    claim_token=None,
                    claimed_at=None,
                    next_attempt_at=None,
                    telegram_message_id=(str(message_id)[:255] if message_id is not None else None),
                    telegram_document_message_id=(
                        str(document_message_id)[:255]
                        if document_message_id is not None else None
                    ),
                    delivered_at=now,
                    error_code=None,
                    updated_at=now,
                )
            )
            session.commit()
            delivered += int(result.rowcount == 1)
    return delivered


def format_runtime_incident_diagnosis_notification(incident) -> str:
    """Render a bounded Phase 3 hypothesis report with no action authority."""

    try:
        diagnosis = json.loads(incident.diagnosis_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        diagnosis = {}
    if not isinstance(diagnosis, dict):
        diagnosis = {}
    handoff_required = diagnosis.get("codex_handoff_required") is True
    shadow_policy = diagnosis.get("shadow_playbook_policy")
    if not isinstance(shadow_policy, dict):
        shadow_policy = {}
    shadow_policy_present = bool(shadow_policy)
    shadow_policy_valid = (
        shadow_policy.get("mode") == "shadow"
        and shadow_policy.get("policy_version") == "runtime-shadow-policy-v1"
        and shadow_policy.get("would_execute") is False
        and shadow_policy.get("action_executed") is False
        and isinstance(shadow_policy.get("accepted"), bool)
    )
    nominated_playbook = (
        shadow_policy.get("nominated_playbook")
        if shadow_policy_valid
        else None
    )
    if shadow_policy_present and not shadow_policy_valid:
        shadow_result = "无效记录"
    elif nominated_playbook:
        shadow_result = (
            "接受（仅影子）"
            if shadow_policy.get("accepted") is True
            else "拒绝"
        )
    else:
        shadow_result = "未提名"
    recovery_policy = diagnosis.get("recovery_playbook_policy")
    if not isinstance(recovery_policy, dict):
        recovery_policy = {}
    recovery_valid = (
        recovery_policy.get("mode") == "execute"
        and recovery_policy.get("policy_version")
        == "runtime-execution-policy-v1"
        and isinstance(recovery_policy.get("accepted"), bool)
        and isinstance(recovery_policy.get("would_execute"), bool)
        and isinstance(recovery_policy.get("action_executed"), bool)
    )
    executed_playbook = (
        recovery_policy.get("nominated_playbook")
        if recovery_valid
        else None
    )
    verification_status = (
        recovery_policy.get("verification_status")
        if recovery_valid
        else None
    )
    action_evidence = recovery_policy.get("evidence_references")
    if isinstance(action_evidence, list):
        action_evidence_text = "; ".join(
            str(reference) for reference in action_evidence[:4]
        ) or "-"
    else:
        action_evidence_text = "-"
    if shadow_policy_present and not shadow_policy_valid:
        action_result = "记录异常，需人工核验"
    elif not recovery_policy:
        action_result = "未执行"
    elif not recovery_valid:
        action_result = "记录异常，需人工核验"
    elif (
        recovery_policy.get("action_executed") is True
        and verification_status in {"verified", "already_verified"}
    ):
        action_result = "已执行并验证"
    elif recovery_policy.get("action_executed") is True:
        action_result = "已执行但验证失败，已冻结"
    else:
        action_result = f"未执行（{verification_status or '已拒绝'}）"
    missing = diagnosis.get("missing_evidence")
    if isinstance(missing, list):
        missing_text = "; ".join(str(item) for item in missing[:4]) or "-"
    else:
        missing_text = "-"
    lines = [
        "运行异常 AI 诊断",
        f"事件ID: {int(incident.id)}",
        f"类型: {_safe_runtime_incident_value(incident.incident_type, limit=64)}",
        (
            "AI诊断假设: "
            f"{_safe_runtime_incident_value(diagnosis.get('hypothesis'), limit=320)}"
        ),
        f"置信度: {_safe_runtime_incident_value(diagnosis.get('confidence'), limit=16)}",
        f"缺失证据: {_safe_runtime_incident_value(missing_text, limit=240)}",
        (
            "剩余风险: "
            f"{_safe_runtime_incident_value(diagnosis.get('remaining_risk'), limit=240)}"
        ),
        f"Codex交接: {'需要' if handoff_required else '不需要'}",
        (
            "影子预案: "
            f"{_safe_runtime_incident_value(nominated_playbook, limit=128)}"
        ),
        f"策略评估: {shadow_result}",
        (
            "执行预案: "
            f"{_safe_runtime_incident_value(executed_playbook, limit=128)}"
        ),
        (
            "验证状态: "
            f"{_safe_runtime_incident_value(verification_status, limit=32)}"
        ),
        (
            "操作证据: "
            f"{_safe_runtime_incident_value(action_evidence_text, limit=240)}"
        ),
        f"自动操作: {action_result}",
        "边界: 未参与策略识别或上下文策略解析。",
    ]
    return wrap_ai_agent_notification(
        "\n".join(lines),
        limit=(
            RUNTIME_INCIDENT_MESSAGE_MAX_CHARS
            - len(AI_AGENT_NOTIFICATION_PREFIX)
            - 1
        ),
    )


def _safe_runtime_incident_value(value: Any, *, limit: int) -> str:
    text = str(value or "-").strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return "[redacted]"
    text = "".join(character if character.isprintable() else "?" for character in text)
    return _truncate_text(text, limit=max(1, int(limit)))


COMPOSITE_MANAGEMENT_MESSAGE_MAX_CHARS = 1200


def format_composite_management_notification(payload: dict[str, Any]) -> str | None:
    """Render only a fully successful composite-management outcome."""

    if str(payload.get("overall_state") or "").lower() != "succeeded":
        return None
    fields = (
        ("批次", "batch_id"),
        ("第一止盈", "first_take_profit"),
        ("剩余仓位", "partial_close"),
        ("保护单", "protection"),
        ("保留止盈总量", "retained_take_profit_total"),
        ("总体状态", "overall_state"),
    )
    lines = ["【复合仓位管理已完成】"]
    for label, key in fields:
        lines.append(
            f"{label}: {_safe_runtime_incident_value(payload.get(key), limit=180)}"
        )
    return "\n".join(lines)[:COMPOSITE_MANAGEMENT_MESSAGE_MAX_CHARS]


def persist_composite_management_completion_in_session(
    session, batch, *, summary: dict[str, Any]
) -> bool:
    """Persist one success-only composite completion outbox row."""

    from telegram_kol_research.models import StrategyManagementNotification

    payload = {**summary, "notification_kind": "composite_completion"}
    rendered = format_composite_management_notification(payload)
    if rendered is None:
        return False
    fingerprint = _management_payload_fingerprint(payload)
    row = StrategyManagementNotification(
        management_batch_id=int(batch.id),
        state="succeeded",
        payload_fingerprint=fingerprint,
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        status="pending",
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return False
    return True


def format_pending_entry_expiry_review_message(payload: dict[str, Any]) -> str:
    lifecycle_id = payload.get("lifecycle_id")
    message_id = payload.get("message_id") or "-"
    chat_title = payload.get("chat_title") or payload.get("group_label") or "-"
    chat_id = payload.get("chat_id") or "-"
    symbol = payload.get("symbol") or "-"
    side = payload.get("side") or "-"
    entry = _format_range(payload.get("entry_range_low"), payload.get("entry_range_high"))
    max_age_hours = payload.get("max_age_hours") or "-"
    review_reason = payload.get("review_reason") or (
        f"\u5f85\u5165\u573a\u5df2\u8d85\u8fc7 {max_age_hours} \u5c0f\u65f6"
    )
    lines = [
        "\u3010\u5f85\u5165\u573a\u7b56\u7565\u8d85\u65f6\u590d\u6838\u3011",
        f"\u7fa4\u7ec4: {chat_title}",
        f"\u7fa4ID: {chat_id}",
        f"\u7b56\u7565\u4ee3\u7801: #{message_id}",
        f"\u5185\u90e8ID: {lifecycle_id}",
        f"\u4ea4\u6613\u5bf9: {symbol} {side}",
        f"\u539f\u7b56\u7565\u65f6\u95f4: {_format_local_time(payload.get('signal_at'))}",
        f"\u8d85\u65f6\u65f6\u95f4: {_format_local_time(payload.get('expiry_at'))}",
    ]
    if payload.get("previous_review_at") is not None:
        lines.append(
            f"\u4e0a\u6b21\u4eba\u5de5\u7ee7\u7eed\u7b49\u5f85: {_format_local_time(payload.get('previous_review_at'))}"
        )
    lines.extend([
        f"\u5165\u573a\u533a\u95f4: {entry}",
        f"\u6b62\u635f: {_format_value(payload.get('stop_loss'))}",
        f"\u6b62\u76c8: {_format_value(payload.get('take_profit'))}",
        f"\u539f\u56e0: {review_reason}\uff0c\u8bf7\u786e\u8ba4\u5982\u4f55\u5904\u7406\u3002",
    ])
    pending_order_ids = [
        str(item)
        for item in (payload.get("pending_order_ids") or [])
        if str(item or "").strip()
    ]
    if pending_order_ids:
        lines.append(
            f"\u672a\u89e6\u53d1\u5165\u573a\u6302\u5355: {', '.join(pending_order_ids)}"
        )
    return "\n".join(lines)


def format_ai_recognition_conflict_review_message(payload: dict[str, Any]) -> str:
    deepseek = (
        payload.get("deepseek")
        if isinstance(payload.get("deepseek"), dict)
        else {}
    )
    mimo = payload.get("mimo") if isinstance(payload.get("mimo"), dict) else {}
    text = str(payload.get("text") or "").strip()
    if len(text) > 700:
        text = text[:697] + "..."
    automation = (
        payload.get("automation")
        if isinstance(payload.get("automation"), dict)
        else {}
    )
    agreement_status = str(payload.get("agreement_status") or "disagreed")
    if agreement_status == "authoritative_failed":
        handling = "处理: MiMo 权威识别失败，未执行自动交易；DeepSeek 结果仅供参考。"
    else:
        handling = "处理: 已按 MiMo 结果继续，未等待人工复核。"
    lines = [
        "【AI识别分歧告警】",
        f"群组: {payload.get('chat_title') or payload.get('group_label') or '-'}",
        f"群ID: {payload.get('chat_id') or '-'}",
        f"消息: #{payload.get('message_id') or '-'}",
        f"时间: {_format_local_time(payload.get('posted_at'))}",
        f"DeepSeek: {_format_value(deepseek.get('status'))} / {_format_value(deepseek.get('kind'))}",
        f"DeepSeek原因: {_format_value(deepseek.get('reason'))}",
        f"MiMo: {_format_value(mimo.get('status'))} / {_format_value(mimo.get('kind'))}",
        f"MiMo原因: {_format_value(mimo.get('reason'))}",
        "权威结果: MiMo",
        f"自动化结果: {_format_value(automation.get('status'))} / {_format_value(automation.get('reason'))}",
        handling,
        "原文:",
        text or "-",
    ]
    return "\n".join(lines)


def format_stall_induced_expiry_notification(payload: dict[str, Any]) -> str:
    """Format the aggregate notice for a burst of stall-induced gap expiry.

    One notification stands for the whole burst a single recovery pass may
    expire after a stall - never one per message - so the wording says "N
    messages", not "message #X".
    """

    expired_count = int(payload.get("expired_count") or 0)
    raw_message_ids = [str(item) for item in (payload.get("raw_message_ids") or [])]
    chat_titles = [str(item) for item in (payload.get("chat_titles") or []) if item]
    shown_ids = raw_message_ids[:20]
    lines = [
        "【系统故障导致信号丢失告警】",
        (
            f"因系统故障（事件循环阻塞），{expired_count} 条消息未能在窗口期内"
            "完成权威识别，已按过期处理，未自动交易。"
        ),
        f"涉及群组: {', '.join(chat_titles) if chat_titles else '-'}",
        (
            f"消息内部ID: {', '.join(shown_ids) if shown_ids else '-'}"
            + ("..." if len(raw_message_ids) > len(shown_ids) else "")
        ),
        f"记录时间: {_format_local_time(payload.get('occurred_at'))}",
        "这是系统性故障导致的信号丢失，不是正常的过期判定，请人工复核。",
    ]
    return "\n".join(lines)


async def send_stall_induced_expiry_notification(
    *,
    config: SystemOperatorBotConfig,
    payload: dict[str, Any],
) -> None:
    await send_system_operator_bot_message(
        config=config,
        text=format_stall_induced_expiry_notification(payload),
    )


def format_semantic_disagreement_notification(payload: dict[str, Any]) -> str:
    """Format a final critical semantic review as a read-only audit notice."""

    deepseek = payload.get("deepseek") if isinstance(payload.get("deepseek"), dict) else {}
    mimo = payload.get("mimo") if isinstance(payload.get("mimo"), dict) else {}
    automation = payload.get("automation") if isinstance(payload.get("automation"), dict) else {}
    evidence = deepseek.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    grounded_evidence = "；".join(
        str(item).strip() for item in evidence if str(item).strip()
    )
    conflict_types = payload.get("conflict_types")
    if not isinstance(conflict_types, list):
        conflict_types = deepseek.get("conflict_types")
    if not isinstance(conflict_types, list):
        conflict_types = []
    conflicts = ", ".join(
        str(item).strip() for item in conflict_types if str(item).strip()
    )
    source_label = (
        payload.get("chat_title")
        or payload.get("group_label")
        or payload.get("sender_name")
        or "-"
    )
    source = (
        f"{source_label} / {payload.get('chat_id') or '-'} / "
        f"#{payload.get('message_id') or '-'}"
    )
    lines = [
        "【AI语义严重分歧】",
        f"原始来源: {source}",
        f"时间: {_format_local_time(payload.get('posted_at'))}",
        (
            "权威结果: MiMo / "
            f"{_format_value(mimo.get('status'))} / "
            f"{_truncate_text(mimo.get('reason'), limit=400)}"
        ),
        (
            "自动化结果: "
            f"{_format_value(automation.get('status'))} / "
            f"{_truncate_text(automation.get('reason'), limit=400)}"
        ),
        (
            "复核结果: DeepSeek / "
            f"{_format_value(deepseek.get('status'))} / "
            f"{_truncate_text(deepseek.get('reason'), limit=400)}"
        ),
        f"冲突类型: {_truncate_text(conflicts, limit=400)}",
        f"依据: {_truncate_text(grounded_evidence, limit=700)}",
        "处理状态: 已按MiMo结果继续，未等待人工复核；消息已处理，不需要审批。",
        "原文:",
        _truncate_text(payload.get("text"), limit=900),
    ]
    return "\n".join(lines)


def format_position_attribution_incident_message(payload: dict[str, Any]) -> str:
    state = str(payload.get("state") or "unassigned")
    state_label = {
        "attribution_conflict": "归属冲突",
        "evidence_unavailable": "归属证据暂不可用",
    }.get(state, state)
    candidate_leg_ids = payload.get("candidate_leg_ids")
    if not isinstance(candidate_leg_ids, list):
        candidate_leg_ids = []
    source_errors = payload.get("evidence_source_errors")
    if not isinstance(source_errors, dict):
        source_errors = {}
    error_text = "；".join(
        f"{key}: {value}" for key, value in sorted(source_errors.items())
    )
    return "\n".join(
        [
            "【仓位归属异常】",
            f"交易所: {payload.get('venue') or 'deepcoin'}",
            f"仓位ID: {payload.get('pos_id') or '-'}",
            f"状态: {state_label}",
            f"候选 entry legs: {', '.join(str(item) for item in candidate_leg_ids) or '-'}",
            f"证据源错误: {error_text or '-'}",
            "处理状态: 自动管理已冻结，不会自动平仓或修改止盈止损。",
        ]
    )


def format_position_protection_incident_message(payload: dict[str, Any]) -> str:
    """Format a high-priority, non-actionable stop-protection incident."""

    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    if str(payload.get("incident_type") or "").startswith(
        "automatic_break_even_"
    ):
        return "\n".join([
            "【自动成本保护异常】",
            f"策略: {evidence.get('strategy_instance_id') or '-'}",
            f"收敛任务: {evidence.get('convergence_id') or '-'}",
            f"仓位ID: {payload.get('pos_id') or '-'}",
            f"触发类型: {evidence.get('trigger_type') or '-'}",
            f"状态: {evidence.get('status') or '-'}",
            f"原因: {evidence.get('reason_code') or '-'}",
            f"人工处理: {evidence.get('manual_action') or '只读核对交易所状态后再决定。'}",
        ])
    exchange = evidence.get("exchange") if isinstance(evidence.get("exchange"), dict) else {}
    return "\n".join([
        "【止损保护严重异常】",
        f"交易所: {payload.get('venue') or 'deepcoin'}",
        f"仓位ID: {payload.get('pos_id') or '-'}",
        f"类型: {payload.get('incident_type') or '-'}",
        f"交易所错误: {exchange.get('errorCode') or '-'} {exchange.get('errorMsg') or ''}".strip(),
        "处理状态: 自动管理已冻结；系统不会补单、撤单或平仓，请人工决定。",
    ])
MANAGEMENT_ALERT_STATES = frozenset(
    {"blocked", "partial_failed", "submit_unknown", "recovery_required"}
)
_BYPASS_ALERT_STATES = frozenset({"reconciling", "succeeded", "recovery_required", "partial_failed", "submit_unknown"})
_MANAGEMENT_TELEGRAM_MAX_CHARS = 3900
_DEFERRED_CANCEL_EVENT_ACTIONS = frozenset({
    "strategy_management_cancel_deferred_trigger_entry",
    "strategy_management_cancel_deferred_regular_entry",
    "strategy_management_deferred_entry_cancel_diagnostic",
})
_SENSITIVE_MARKERS = (
    "dc-access", "api-key", "api_key", "authorization", "passphrase",
    "secret", "signing", "raw_header", "raw_payload", "raw_response",
)


def format_strategy_management_notification(payload: dict[str, Any]) -> str:
    """Format only bounded, explicitly selected management business fields."""

    state = str(payload.get("state") or "-")
    mode = str(payload.get("mode") or "shadow")
    bypass = payload.get("protection_recovery_bypass")
    is_bypass = isinstance(bypass, dict)
    lines = [
        "【保护异常旁路全平】" if is_bypass else "【策略管理批次异常】",
        f"通知ID: {payload.get('notification_id') or '-'}",
        f"batch #{payload.get('batch_id') or '-'} / 状态: {state}",
        (
            f"来源: {payload.get('source_chat_title') or '-'} / "
            f"{payload.get('source_chat_id') or '-'} / "
            f"#{payload.get('source_message_id') or '-'} / "
            f"raw={payload.get('raw_message_id') or '-'}"
        ),
        (
            f"归属: lifecycle={payload.get('lifecycle_id') or '-'} / "
            f"strategy={payload.get('strategy_instance_id') or '-'} / "
            f"binding={payload.get('execution_binding_id') or '-'}"
        ),
        f"动作: {payload.get('intent') or '-'} -> {payload.get('effective_action') or '-'}",
        f"原因: {_safe_management_text(payload.get('reason'))}",
        f"模式: {mode}" + (" / 未调用交易 API" if mode != "live" else ""),
    ]
    if state == "recovery_required":
        lines.append("安全限制: 禁止自动重试")
    if is_bypass:
        pos_ids = bypass.get("target_pos_ids")
        if not isinstance(pos_ids, list):
            pos_ids = []
        lines.extend(
            [
                "旁路原因: " + _safe_management_text(bypass.get("reason"), limit=80),
                "精确仓位: " + ", ".join(
                    _safe_management_text(pos_id, limit=120)
                    for pos_id in pos_ids[:20]
                    if pos_id not in (None, "")
                ),
            ]
        )
    deferred_entry_legs = (
        payload.get("deferred_entry_legs")
        if isinstance(payload.get("deferred_entry_legs"), list) else []
    )
    if payload.get("reason") == "deferred_entry_cancel_preflight_failed":
        lines.extend([
            "未成交进场腿撤单未完成：为防止旧策略残留订单成交，系统未提交平仓单。",
            "请核对交易所挂单与以下腿的订单ID；确认撤销后再执行恢复处理。",
            "恢复处理完成前，请勿启用替代策略。",
            f"批次ID: {payload.get('batch_id') or '-'}",
            "未成交进场腿:",
        ])
        for entry in deferred_entry_legs[:20]:
            if not isinstance(entry, dict):
                continue
            identity_state = _safe_management_text(
                entry.get("identity_state") or "snapshotted", limit=40
            )
            unavailable_identifier = (
                "不可用(快照腿缺失或漂移)"
                if identity_state in {
                    "snapshot_leg_missing",
                    "snapshot_leg_reassigned",
                    "snapshot_leg_state_drift",
                }
                else "-"
            )
            lines.append(
                "- "
                f"腿: {_safe_management_text(entry.get('execution_order_leg_id'), limit=24)} "
                f"身份: {identity_state} "
                f"订单: {_safe_management_text(entry.get('order_id') or unavailable_identifier, limit=120)} "
                f"客户订单: {_safe_management_text(entry.get('client_order_id') or unavailable_identifier, limit=120)}"
            )
            diagnostic = entry.get("cancellation_diagnostic")
            if isinstance(diagnostic, dict):
                lines.append(
                    "  撤单诊断: "
                    f"live={_safe_management_text(diagnostic.get('live_match_source'), limit=40)} "
                    f"type={_safe_management_text(diagnostic.get('match_type'), limit=24)} "
                    f"status={_safe_management_text(diagnostic.get('status'), limit=32)} "
                    f"reason={_safe_management_text(diagnostic.get('reason'), limit=80)}"
                )
        if not deferred_entry_legs:
            lines.append("- 身份漂移诊断未持久化，请立即人工核对快照和待处理进场腿。")
    legs = payload.get("legs") if isinstance(payload.get("legs"), list) else []
    lines.append("仓位/腿结果:")
    for leg in legs[:20]:
        if not isinstance(leg, dict):
            continue
        lines.append(
            "- "
            f"leg={leg.get('leg_id') or '-'} entry_leg={leg.get('execution_order_leg_id') or '-'} "
            f"pos={leg.get('pos_id') or '-'} index={leg.get('leg_index')} "
            f"status={leg.get('status') or '-'} planned={leg.get('planned_close_size') or '-'} "
            f"clOrdId={leg.get('client_order_id') or '-'} ordId={leg.get('exchange_order_id') or '-'} "
            f"error={json.dumps(leg.get('error_summary') or {}, ensure_ascii=False, sort_keys=True)}"
        )
    if not legs:
        lines.append("- -")
    return "\n".join(lines)


def split_strategy_management_notification(
    payload: dict[str, Any], *, max_chars: int = _MANAGEMENT_TELEGRAM_MAX_CHARS
) -> list[str]:
    """Return deterministic line-bounded Telegram messages below 4096 chars."""

    if payload.get("notification_kind") == "composite_completion":
        rendered = format_composite_management_notification(payload)
        if rendered is None:
            return []
    else:
        rendered = format_strategy_management_notification(payload)
    return _split_telegram_notification_text(rendered, max_chars=max_chars)


def _split_telegram_notification_text(text: str, *, max_chars: int) -> list[str]:
    """Split display text deterministically on lines within Telegram's limit."""

    limit = max(1, min(int(max_chars), 4095))
    if text.startswith(AI_AGENT_NOTIFICATION_PREFIX) and text.endswith("）"):
        inner = text[len(AI_AGENT_NOTIFICATION_PREFIX) : -1]
        inner_limit = max(
            1,
            limit - len(AI_AGENT_NOTIFICATION_PREFIX) - 1,
        )
        return [
            f"{AI_AGENT_NOTIFICATION_PREFIX}{chunk}）"
            for chunk in _split_telegram_notification_text(
                inner,
                max_chars=inner_limit,
            )
        ]
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def format_message_instruction_summary(payload: dict[str, Any]) -> str:
    """Render a bounded, payload-only outcome summary for one source message."""

    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    ordered_items = sorted(
        (item for item in items if isinstance(item, dict)),
        key=lambda item: (_summary_sequence(item), _summary_item_id(item)),
    )
    targets = payload.get("targets") if isinstance(payload.get("targets"), list) else []
    ordered_targets = sorted(
        (target for target in targets if isinstance(target, dict)),
        key=lambda target: int(target.get("target_ordinal") or 0),
    )
    lines = [
        "【同消息策略指令结果】",
        (
            f"来源: {payload.get('chat_title') or payload.get('group_label') or '-'} / "
            f"{payload.get('chat_id') or '-'} / #{payload.get('message_id') or '-'}"
        ),
    ]
    if payload.get("notification_id"):
        lines.append(f"通知ID: {payload['notification_id']}")
    if ordered_targets:
        lines.append("目标结果:")
        for target in ordered_targets[:100]:
            admission_state = _safe_management_text(
                target.get("admission_state"), limit=32
            )
            execution_state = _safe_management_text(
                target.get("execution_state"), limit=32
            )
            outcome = (
                "refused" if admission_state == "refused" else execution_state
            )
            lines.append(
                "- "
                f"{_safe_management_text(target.get('symbol'), limit=32)} "
                f"{_safe_management_text(target.get('side'), limit=16)}: {outcome} / "
                "原因: "
                f"{_safe_management_text(target.get('reason_code'), limit=128)}"
            )
    management_failed = False
    entry_attempted = False
    for item in ordered_items[:100]:
        kind = str(item.get("instruction_kind") or "-").strip().lower()
        status = _safe_management_text(item.get("status"), limit=48)
        label = {"management": "仓位管理", "entry": "新策略开仓"}.get(kind, kind or "-")
        strategy = _safe_management_text(item.get("strategy_instance_id"), limit=255)
        reason = _safe_management_text(_summary_item_reason(item), limit=180)
        lines.append(
            f"#{_summary_sequence(item)} {label}: {status} / "
            f"strategy={strategy} / 原因: {reason}"
        )
        management_failed = management_failed or (
            kind == "management" and status in {"failed", "unknown"}
        )
        entry_attempted = entry_attempted or (
            kind == "entry" and status not in {"pending", "executing", "-"}
        )
    if not ordered_items:
        lines.append("未持久化可展示的指令结果。")
    elif management_failed and entry_attempted:
        lines.append("仓位管理异常；后续开仓已继续尝试。")
    assembly = payload.get("entry_assembly")
    revision = payload.get("entry_revision")
    if isinstance(assembly, dict):
        lines.append(_safe_management_text(assembly.get("state_label"), limit=48))
        lines.append(_safe_management_text(assembly.get("risk_calculation"), limit=96))
    if isinstance(revision, dict):
        lines.append(_safe_management_text(revision.get("label"), limit=48))
        if revision.get("orders_changed") is True:
            lines.append("订单已变更: 是（已读回确认）")
    return wrap_ai_agent_notification("\n".join(lines))


def _load_entry_status_for_instruction_summary(
    session_factory,
    *,
    raw_message_id: int,
) -> dict[str, object]:
    from telegram_kol_research.models import (
        EntryRevisionReplacement,
        ExecutionBinding,
        RawMessage,
        StrategyRevisionBatch,
        StrategyRevisionLeg,
    )

    with session_factory() as session:
        raw = session.get(RawMessage, int(raw_message_id))
        if raw is None:
            return {}
        batch = (
            session.query(StrategyRevisionBatch)
            .filter(StrategyRevisionBatch.raw_message_id == int(raw.id))
            .order_by(StrategyRevisionBatch.id.desc())
            .first()
        )
        binding = (
            session.get(ExecutionBinding, int(batch.execution_binding_id))
            if batch is not None
            else session.query(ExecutionBinding)
            .filter(
                ExecutionBinding.chat_id == int(raw.chat_id),
                ExecutionBinding.message_id == int(raw.message_id),
            )
            .order_by(ExecutionBinding.id.desc())
            .first()
        )
        assembly = None
        if binding is not None:
            try:
                binding_payload = json.loads(binding.payload_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                binding_payload = {}
            draft = binding_payload.get("draft") if isinstance(binding_payload, dict) else None
            evidence = draft.get("entry_preamble_assembly") if isinstance(draft, dict) else None
            assembly = format_entry_assembly_summary(evidence)
        revision = None
        if batch is not None:
            replacements = session.query(EntryRevisionReplacement).filter(
                EntryRevisionReplacement.revision_batch_id == int(batch.id)
            )
            confirmed = replacements.filter(
                EntryRevisionReplacement.status == "verified"
            ).count() + session.query(StrategyRevisionLeg).filter(
                StrategyRevisionLeg.revision_batch_id == int(batch.id),
                StrategyRevisionLeg.action == "cancel_pending",
                StrategyRevisionLeg.status == "cancelled",
                StrategyRevisionLeg.response_json.is_not(None),
            ).count()
            try:
                market = json.loads(batch.market_snapshot_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                market = {}
            revision = format_entry_revision_summary(
                {
                    "status": batch.status,
                    "reason_code": batch.reason_code,
                    "replacement_count": replacements.count(),
                    "confirmed_change_count": confirmed,
                    "market_snapshot": market,
                }
            )
    return {"entry_assembly": assembly, "entry_revision": revision}


def split_message_instruction_summary(
    payload: dict[str, Any], *, max_chars: int = _MANAGEMENT_TELEGRAM_MAX_CHARS
) -> list[str]:
    """Split a message-level instruction summary using the Telegram-safe splitter."""

    return _split_telegram_notification_text(
        format_message_instruction_summary(payload), max_chars=max_chars
    )


def _summary_sequence(item: dict[str, Any]) -> int:
    sequence = item.get("sequence")
    return sequence if type(sequence) is int and sequence >= 0 else 999_999_999


def _summary_item_id(item: dict[str, Any]) -> int:
    item_id = item.get("item_id")
    return item_id if type(item_id) is int and item_id >= 0 else 999_999_999


def _summary_item_reason(item: dict[str, Any]) -> Any:
    if item.get("reason") not in (None, ""):
        return item["reason"]
    result = item.get("result")
    if isinstance(result, dict):
        for key in ("reason", "reason_code", "status"):
            if result.get(key) not in (None, ""):
                return result[key]
    return "-"


def _safe_management_text(value: Any, *, limit: int = 300) -> str:
    text = str(value or "-").strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _SENSITIVE_MARKERS):
        return "[redacted]"
    return _truncate_text(text, limit=limit)


def _decode_mapping(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _decode_value(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def canonical_management_error_summary(value: Any) -> dict[str, str]:
    """Select machine identifiers only; never copy exception prose or transport data."""

    decoded = _decode_value(value)
    if not isinstance(decoded, dict):
        return {}
    summary: dict[str, str] = {}
    error_type = decoded.get("type")
    if isinstance(error_type, str) and re.fullmatch(
        r"[A-Z][A-Za-z0-9]{0,70}(?:Error|Exception)", error_type
    ):
        summary["type"] = error_type
    for key in ("stage", "reason_code", "reason"):
        item = decoded.get(key)
        if isinstance(item, str) and re.fullmatch(r"[a-z][a-z0-9_]{2,79}", item):
            summary[key] = item
    status = decoded.get("status")
    if status in {
        "planned", "reserved", "submitted", "submit_unknown", "failed",
        "confirmed", "partial", "inconsistent", "restored", "recovery_required",
    }:
        summary["status"] = status
    return summary


def _management_payload_for_batch(session, batch, *, group_labels=None) -> dict[str, Any]:
    from telegram_kol_research.models import (
        ExecutionEvent, ExecutionOrderLeg, RawMessage, StrategyManagementLeg,
    )

    raw = session.get(RawMessage, batch.raw_message_id)
    # The raw row is the source-of-truth isolation boundary. Never infer a chat
    # from a strategy string or join another group's similarly numbered message.
    mode = str(batch.execution_mode or "disabled")
    labels = group_labels or {}
    legs = []
    for leg in (
        session.query(StrategyManagementLeg)
        .filter(StrategyManagementLeg.management_batch_id == batch.id)
        .order_by(StrategyManagementLeg.leg_index.asc(), StrategyManagementLeg.id.asc())
        .limit(20)
    ):
        legs.append(
            {
                "leg_id": leg.id,
                "execution_order_leg_id": leg.execution_order_leg_id,
                "pos_id": _safe_management_text(leg.pos_id, limit=120),
                "leg_index": leg.leg_index,
                "status": _safe_management_text(leg.status, limit=64),
                "preflight_size": _safe_management_text(leg.preflight_size, limit=64),
                "planned_close_size": _safe_management_text(leg.planned_close_size, limit=64),
                "client_order_id": _safe_management_text(leg.client_order_id, limit=120),
                "exchange_order_id": _safe_management_text(leg.exchange_order_id, limit=120),
                "error_summary": canonical_management_error_summary(leg.last_error),
            }
        )
    deferred_entry_legs = []
    cancellation_diagnostics: dict[int, dict[str, Any]] = {}
    for event in (
        session.query(ExecutionEvent)
        .filter(
            ExecutionEvent.execution_binding_id == batch.execution_binding_id,
            ExecutionEvent.source_message_id == batch.raw_message_id,
            ExecutionEvent.action.in_(_DEFERRED_CANCEL_EVENT_ACTIONS),
        )
        .order_by(ExecutionEvent.id.desc())
        .limit(100)
    ):
        diagnostic = _canonical_deferred_cancel_diagnostic(event.after_json)
        leg_id = diagnostic.get("execution_order_leg_id")
        if isinstance(leg_id, int) and leg_id not in cancellation_diagnostics:
            cancellation_diagnostics[leg_id] = {
                key: value
                for key, value in diagnostic.items()
                if key != "execution_order_leg_id"
            }
    snapshot = _decode_mapping(batch.target_snapshot_json)
    bypass = _management_protection_recovery_bypass(snapshot)
    identity = snapshot.get("identity")
    deferred_entry_leg_ids = (
        identity.get("deferred_entry_leg_ids") if isinstance(identity, dict) else []
    )
    if (
        isinstance(deferred_entry_leg_ids, list)
        and all(type(leg_id) is int and leg_id > 0 for leg_id in deferred_entry_leg_ids)
    ):
        diagnostic_leg_ids = list(cancellation_diagnostics)
        display_leg_ids = []
        for leg_id in [*diagnostic_leg_ids, *deferred_entry_leg_ids]:
            if leg_id not in display_leg_ids:
                display_leg_ids.append(leg_id)
            if len(display_leg_ids) == 20:
                break
        entries_by_id = {
            int(entry.id): entry
            for entry in (
                session.query(ExecutionOrderLeg)
                .filter(
                    ExecutionOrderLeg.id.in_(display_leg_ids),
                    ExecutionOrderLeg.execution_binding_id
                    == batch.execution_binding_id,
                    ExecutionOrderLeg.strategy_instance_id
                    == batch.strategy_instance_id,
                    ExecutionOrderLeg.purpose == "entry",
                )
                .order_by(ExecutionOrderLeg.id.asc())
            )
        }
        for leg_id in display_leg_ids:
            entry = entries_by_id.get(leg_id)
            diagnostic = cancellation_diagnostics.get(leg_id, {})
            identity_state = diagnostic.get("identity_state") or "snapshotted"
            order_id = (
                entry.order_id if entry is not None else diagnostic.get("order_id")
            )
            client_order_id = (
                entry.client_order_id
                if entry is not None
                else diagnostic.get("client_order_id")
            )
            deferred_entry_legs.append(
                {
                    "execution_order_leg_id": leg_id,
                    "identity_state": _safe_management_text(
                        identity_state, limit=40
                    ),
                    "order_id": (
                        _safe_management_text(order_id, limit=120)
                        if order_id not in (None, "")
                        else None
                    ),
                    "client_order_id": (
                        _safe_management_text(client_order_id, limit=120)
                        if client_order_id not in (None, "")
                        else None
                    ),
                    "cancellation_diagnostic": diagnostic or {
                        "live_match_source": "unknown",
                        "match_type": (
                            "trigger"
                            if entry is not None
                            and "trigger" in str(entry.order_kind or "").lower()
                            else "regular"
                        ),
                        "status": (
                            "resolved"
                            if entry is not None
                            and str(entry.status or "").lower() == "cancelled"
                            else "unresolved"
                        ),
                        "reason": _safe_management_text(
                            (
                                entry.terminal_reason
                                if entry is not None
                                else None
                            )
                            or batch.reason_code,
                            limit=80,
                        ),
                    },
                }
            )
    return {
        "batch_id": batch.id,
        "state": batch.status,
        "mode": mode if mode in {"disabled", "shadow", "live"} else "disabled",
        "source_chat_id": raw.chat_id if raw is not None else None,
        "source_chat_title": (
            _safe_management_text(labels.get(raw.chat_id), limit=120)
            if raw is not None and labels.get(raw.chat_id) is not None else None
        ),
        "source_message_id": raw.message_id if raw is not None else None,
        "raw_message_id": raw.id if raw is not None else batch.raw_message_id,
        "lifecycle_id": batch.target_lifecycle_id,
        "strategy_instance_id": _safe_management_text(batch.strategy_instance_id, limit=255),
        "execution_binding_id": batch.execution_binding_id,
        "intent": _safe_management_text(batch.intent, limit=64),
        "effective_action": _safe_management_text(batch.effective_action, limit=64),
        "reason": _safe_management_text(batch.reason_code, limit=240),
        "protection_recovery_bypass": bypass,
        "deferred_entry_legs": deferred_entry_legs,
        "legs": legs,
    }


def _management_protection_recovery_bypass(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    marker = snapshot.get("protection_recovery_bypass")
    if not isinstance(marker, dict) or marker.get("version") != 1:
        return None
    if (
        marker.get("reason") != "protection_recovery_required"
        or marker.get("allowed_action") != "full_exit"
        or not isinstance(marker.get("target_pos_ids"), list)
    ):
        return None
    return {
        "reason": "protection_recovery_required",
        "allowed_action": "full_exit",
        "target_pos_ids": [
            _safe_management_text(pos_id, limit=120)
            for pos_id in marker["target_pos_ids"][:20]
            if pos_id not in (None, "")
        ],
    }


def _canonical_deferred_cancel_diagnostic(value: Any) -> dict[str, Any]:
    decoded = _decode_value(value)
    if not isinstance(decoded, dict):
        return {}
    leg_id = decoded.get("execution_order_leg_id")
    if type(leg_id) is not int or leg_id <= 0:
        return {}
    diagnostic = {
        "execution_order_leg_id": leg_id,
        "live_match_source": _safe_management_text(
            decoded.get("live_match_source"), limit=40
        ),
        "match_type": _safe_management_text(decoded.get("match_type"), limit=24),
        "status": _safe_management_text(decoded.get("status"), limit=32),
        "reason": _safe_management_text(decoded.get("reason"), limit=80),
    }
    identity_state = decoded.get("identity_state")
    if identity_state in {
        "snapshot_leg_missing",
        "snapshot_leg_reassigned",
        "snapshot_leg_state_drift",
        "unsnapshotted_pending",
    }:
        diagnostic["identity_state"] = identity_state
    for key in ("order_id", "client_order_id"):
        if decoded.get(key) not in (None, ""):
            diagnostic[key] = _safe_management_text(decoded.get(key), limit=120)
    return diagnostic


def _management_payload_fingerprint(payload: dict[str, Any]) -> str:
    fingerprint_payload = dict(payload)
    # The display title is mutable configuration, not attribution identity.
    # Excluding it keeps transition-time outbox rows and delivery-time label
    # enrichment from producing duplicate alerts.
    fingerprint_payload.pop("source_chat_title", None)
    canonical = json.dumps(
        fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def persist_strategy_management_notification_in_session(
    session, batch, *, group_labels=None
) -> bool:
    """Write the alert outbox in the same transaction as an alert transition."""

    from telegram_kol_research.models import StrategyManagementNotification

    payload = _management_payload_for_batch(session, batch, group_labels=group_labels)
    bypass = payload.get("protection_recovery_bypass")
    if batch.status not in MANAGEMENT_ALERT_STATES and not (
        isinstance(bypass, dict) and batch.status in _BYPASS_ALERT_STATES
    ):
        return False
    fingerprint = _management_payload_fingerprint(payload)
    row = StrategyManagementNotification(
        management_batch_id=batch.id,
        state=batch.status,
        payload_fingerprint=fingerprint,
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        status="pending",
    )
    # A nested savepoint lets an unchanged duplicate lose without rolling back
    # the enclosing management transition.
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return False
    return True


def enqueue_strategy_management_notifications(session_factory, *, group_labels=None) -> int:
    """Persist immutable alert identities; duplicate ticks are harmless."""

    from telegram_kol_research.models import StrategyManagementBatch
    from telegram_kol_research.models import StrategyManagementNotification

    with session_factory() as session:
        batch_ids = [
            row[0] for row in session.query(StrategyManagementBatch.id)
            .filter(
                StrategyManagementBatch.status.in_(
                    MANAGEMENT_ALERT_STATES | _BYPASS_ALERT_STATES
                )
            )
            .order_by(StrategyManagementBatch.id.asc()).limit(100).all()
        ]
    created = 0
    for batch_id in batch_ids:
        with session_factory() as session:
            batch = session.get(StrategyManagementBatch, batch_id)
            if batch is None:
                continue
            if persist_strategy_management_notification_in_session(
                session, batch, group_labels=group_labels
            ):
                created += 1
            session.commit()
    return created


def claim_next_strategy_management_notification(
    session_factory, *, claimed_at: datetime | None = None, lease_seconds: float = 120.0
):
    """CAS one pending/failed delivery; concurrent notifiers have one winner."""

    from telegram_kol_research.models import StrategyManagementNotification

    now = claimed_at or datetime.now(UTC)
    lease_expiry = now + timedelta(seconds=max(5.0, float(lease_seconds)))
    claimable = or_(
        StrategyManagementNotification.status.in_(("pending", "failed")),
        (
            (StrategyManagementNotification.status == "delivering")
            & or_(
                StrategyManagementNotification.lease_expires_at.is_(None),
                StrategyManagementNotification.lease_expires_at <= now,
            )
        ),
    )
    with session_factory() as session:
        row_id = (
            session.query(StrategyManagementNotification.id)
            .filter(claimable)
            .order_by(StrategyManagementNotification.id.asc()).limit(1).scalar()
        )
    if row_id is None:
        return None
    token = uuid.uuid4().hex
    with session_factory() as session:
        result = session.execute(
            update(StrategyManagementNotification)
            .where(
                StrategyManagementNotification.id == row_id,
                claimable,
            )
            .values(
                status="delivering", claim_token=token, delivery_error=None,
                claimed_at=now, lease_expires_at=lease_expiry, updated_at=now,
            )
        )
        session.commit()
        if result.rowcount != 1:
            return None
        row = session.get(StrategyManagementNotification, row_id)
        return {
            "id": row.id, "claim_token": token,
            "payload": _decode_mapping(row.payload_json),
        }


def format_terminal_entry_cleanup_notification(event) -> str:
    payload = _decode_mapping(event.response_json)
    if event.action == "source_message_deletion_outcome":
        state_labels = {
            "cancelling_entries": "正在撤销原策略入场单",
            "closing_positions": "正在市价退出原策略持仓",
            "reconciling": "等待交易所归零证明",
            "recovery_required": "需要人工恢复处理",
            "succeeded": "原策略订单与仓位已确认归零",
        }
        return (
            "【Telegram 原策略删除退出】\n"
            f"删除退出ID: {payload.get('exit_id')}\n"
            f"生命周期: {payload.get('lifecycle_id')}\n"
            f"执行绑定: {payload.get('binding_id')}\n"
            f"管理批次: {payload.get('management_batch_id') or '无'}\n"
            f"状态: {state_labels.get(str(event.status), str(event.status))}\n"
            f"原因: {_safe_management_text(payload.get('reason'), limit=160)}\n"
            f"归零证明: {'已保存' if payload.get('flat_proof_confirmed') else '尚未完成'}"
        )[:1200]
    terminal_anomaly = (
        payload.get("reason") == "terminal_lifecycle_entry_exposure"
    )
    status_labels = {
        "resolved": "已确认取消",
        "already_absent": "已确认挂单不存在",
        "blocked": (
            "取消未确认，已终态生命周期存在不变量异常"
            if terminal_anomaly
            else "取消未确认，生命周期保持非终态"
        ),
        "unknown": (
            "交易所结果未知，已终态生命周期存在不变量异常"
            if terminal_anomaly
            else "交易所结果未知，生命周期保持非终态"
        ),
    }
    order_ids = [
        str(value)[:64]
        for value in payload.get("order_ids", [])
        if str(value)
    ][:10]
    return (
        "【终态挂单清理】\n"
        f"清理事件ID: {event.id}\n"
        f"生命周期: {payload.get('lifecycle_id')}\n"
        f"执行绑定: {payload.get('binding_id')}\n"
        f"结果: {status_labels.get(str(event.status), '结果未知')}\n"
        f"挂单: {', '.join(order_ids) if order_ids else '无可用订单号'}\n"
        "自动操作: 仅发送持久化结果通知，不会重复提交撤单"
    )[:1200]


def claim_next_terminal_entry_cleanup_notification(
    session_factory,
    *,
    claimed_at: datetime | None = None,
    lease_seconds: float = TERMINAL_ENTRY_CLEANUP_NOTIFICATION_LEASE_SECONDS,
):
    """Atomically lease one cleanup notification without touching the exchange."""

    from telegram_kol_research.models import ExecutionEvent

    now = claimed_at or datetime.now(UTC)
    _suppress_non_cancellable_terminal_cleanup_notifications(
        session_factory,
        suppressed_at=now,
    )
    stale_before = now - timedelta(seconds=max(5.0, float(lease_seconds)))
    claimable = and_(
        ExecutionEvent.action.in_(
            (
                "terminal_entry_cleanup_outcome",
                "source_message_deletion_outcome",
            )
        ),
        ExecutionEvent.notification_attempts
        < TERMINAL_ENTRY_CLEANUP_NOTIFICATION_MAX_ATTEMPTS,
        or_(
            ExecutionEvent.notification_status == "pending",
            and_(
                ExecutionEvent.notification_status == "failed",
                or_(
                    ExecutionEvent.notification_next_attempt_at.is_(None),
                    ExecutionEvent.notification_next_attempt_at <= now,
                ),
            ),
            and_(
                ExecutionEvent.notification_status == "delivering",
                or_(
                    ExecutionEvent.notification_claimed_at.is_(None),
                    ExecutionEvent.notification_claimed_at <= stale_before,
                ),
            ),
        ),
    )
    with session_factory() as session:
        row_id = (
            session.query(ExecutionEvent.id)
            .filter(claimable)
            .order_by(ExecutionEvent.id.asc())
            .limit(1)
            .scalar()
        )
    if row_id is None:
        return None
    token = uuid.uuid4().hex
    with session_factory() as session:
        result = session.execute(
            update(ExecutionEvent)
            .where(ExecutionEvent.id == row_id, claimable)
            .values(
                notification_status="delivering",
                notification_claim_token=token,
                notification_claimed_at=now,
                notification_next_attempt_at=None,
                notification_error=None,
                notification_attempts=ExecutionEvent.notification_attempts + 1,
            )
        )
        session.commit()
        if result.rowcount != 1:
            return None
        row = session.get(ExecutionEvent, row_id)
        session.expunge(row)
        return {"event": row, "claim_token": token}


def _suppress_non_cancellable_terminal_cleanup_notifications(
    session_factory,
    *,
    suppressed_at: datetime,
) -> int:
    """Suppress legacy backstop noise that never represented a pending order."""

    from telegram_kol_research.models import ExecutionEvent, ExecutionOrderLeg

    cancellable_states = {
        "pending",
        "open",
        "submitted",
        "partially_filled",
        "partial",
    }
    suppressed = 0
    with session_factory() as session:
        rows = (
            session.query(ExecutionEvent)
            .filter(
                ExecutionEvent.action == "terminal_entry_cleanup_outcome",
                ExecutionEvent.status == "unknown",
                ExecutionEvent.notification_status.in_(("pending", "failed")),
            )
            .order_by(ExecutionEvent.id.asc())
            .limit(100)
            .all()
        )
        for row in rows:
            payload = _decode_mapping(row.response_json)
            if payload.get("reason") != "terminal_lifecycle_entry_exposure":
                continue
            if payload.get("notification_policy_version"):
                continue
            leg_ids = [
                int(value)
                for value in payload.get("leg_ids", [])
                if str(value).isdigit()
            ]
            if not leg_ids:
                continue
            leg_states = {
                str(status or "").lower()
                for (status,) in (
                    session.query(ExecutionOrderLeg.status)
                    .filter(ExecutionOrderLeg.id.in_(leg_ids))
                    .all()
                )
            }
            if leg_states & cancellable_states:
                continue
            row.notification_status = "not_needed"
            row.notification_error = "non_cancellable_entry_state"
            row.notification_next_attempt_at = None
            row.notification_claim_token = None
            row.notification_claimed_at = None
            row.notified_at = suppressed_at
            suppressed += 1
        session.commit()
    return suppressed


async def deliver_terminal_entry_cleanup_notifications(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    delivered_at: datetime | None = None,
    limit: int = 20,
) -> int:
    """Deliver a bounded durable outbox; this worker performs no exchange writes."""

    from telegram_kol_research.models import ExecutionEvent

    operation_now = delivered_at or datetime.now(UTC)
    delivered = 0
    for _ in range(max(1, min(int(limit), 100))):
        claim = claim_next_terminal_entry_cleanup_notification(
            session_factory,
            claimed_at=operation_now,
        )
        if claim is None:
            break
        event = claim["event"]
        token = claim["claim_token"]
        try:
            message_id = await send_system_operator_bot_message(
                config=config,
                text=format_terminal_entry_cleanup_notification(event),
            )
        except Exception as exc:
            attempts = int(event.notification_attempts or 0)
            exhausted = (
                attempts >= TERMINAL_ENTRY_CLEANUP_NOTIFICATION_MAX_ATTEMPTS
            )
            retry_at = None
            if not exhausted:
                retry_at = operation_now + timedelta(
                    seconds=min(900, 60 * (2 ** max(0, attempts - 1)))
                )
            with session_factory() as session:
                session.execute(
                    update(ExecutionEvent)
                    .where(
                        ExecutionEvent.id == event.id,
                        ExecutionEvent.notification_status == "delivering",
                        ExecutionEvent.notification_claim_token == token,
                    )
                    .values(
                        notification_status="exhausted" if exhausted else "failed",
                        notification_claim_token=None,
                        notification_claimed_at=None,
                        notification_error=type(exc).__name__[:128],
                        notification_next_attempt_at=retry_at,
                    )
                )
                session.commit()
            break
        with session_factory() as session:
            result = session.execute(
                update(ExecutionEvent)
                .where(
                    ExecutionEvent.id == event.id,
                    ExecutionEvent.notification_status == "delivering",
                    ExecutionEvent.notification_claim_token == token,
                )
                .values(
                    notification_status="delivered",
                    notification_claim_token=None,
                    notification_claimed_at=None,
                    notification_error=None,
                    notification_next_attempt_at=None,
                    notification_message_id=(
                        str(message_id)[:255] if message_id is not None else None
                    ),
                    notified_at=operation_now,
                )
            )
            session.commit()
            delivered += int(result.rowcount == 1)
    return delivered


def claim_next_runtime_incident_notification(
    session_factory,
    *,
    claimed_at: datetime | None = None,
    lease_seconds: float = 120.0,
    notification_types: frozenset[str] | None = None,
    after_incident_id: int | None = None,
    excluded_notification_types: frozenset[str] = frozenset(),
):
    """CAS one pending, failed, or stale runtime-incident delivery."""

    from telegram_kol_research.models import RuntimeIncident

    now = claimed_at or datetime.now(UTC)
    stale_before = now - timedelta(seconds=max(5.0, float(lease_seconds)))
    claimable_status = or_(
        RuntimeIncident.notification_status == "pending",
        and_(
            RuntimeIncident.notification_status == "failed",
            or_(
                RuntimeIncident.notification_claimed_at.is_(None),
                RuntimeIncident.notification_claimed_at <= stale_before,
            ),
        ),
        (
            (RuntimeIncident.notification_status == "delivering")
            & or_(
                RuntimeIncident.notification_claimed_at.is_(None),
                RuntimeIncident.notification_claimed_at <= stale_before,
            )
        ),
    )
    if notification_types is not None:
        if not notification_types:
            return None
        claimable = and_(
            claimable_status,
            RuntimeIncident.incident_type.in_(tuple(sorted(notification_types))),
        )
    else:
        claimable = claimable_status
    if excluded_notification_types:
        claimable = and_(
            claimable,
            RuntimeIncident.incident_type.not_in(
                tuple(sorted(excluded_notification_types))
            ),
        )
    if after_incident_id is not None:
        claimable = and_(claimable, RuntimeIncident.id > int(after_incident_id))
    with session_factory() as session:
        row_id = (
            session.query(RuntimeIncident.id)
            .filter(claimable)
            .order_by(RuntimeIncident.id.asc())
            .limit(1)
            .scalar()
        )
    if row_id is None:
        return None
    token = uuid.uuid4().hex
    with session_factory() as session:
        result = session.execute(
            update(RuntimeIncident)
            .where(RuntimeIncident.id == row_id, claimable)
            .values(
                notification_status="delivering",
                notification_claim_token=token,
                notification_claimed_at=now,
                updated_at=now,
            )
        )
        session.commit()
        if result.rowcount != 1:
            return None
        row = session.get(RuntimeIncident, row_id)
        session.expunge(row)
        return {"incident": row, "claim_token": token}


async def deliver_runtime_incident_notifications(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    runtime_config: RuntimeIncidentConfig | None = None,
    limit: int = 20,
    claimed_at: datetime | None = None,
) -> int:
    """Deliver with at-least-once crash semantics and stable incident IDs.

    A process death after Telegram accepts a report but before ``delivered``
    commits can cause one repeat after lease expiry. The visible incident ID is
    the operator deduplication marker; committed successes are not reclaimed.
    """

    from telegram_kol_research.models import RuntimeIncident
    from telegram_kol_research.runtime_incident_adapters import (
        capture_notification_failure,
    )

    feature_config = runtime_config or load_runtime_incident_config()
    delivered = 0
    if feature_config.message_operation_stage1_enabled:
        try:
            delivered += await deliver_message_operation_stage1_notifications(
                session_factory,
                config=config,
                after_contract_id=(
                    feature_config.message_operation_stage1_after_contract_id
                ),
                limit=limit,
                claimed_at=claimed_at,
                lease_seconds=feature_config.notification_lease_seconds,
                max_attempts=(
                    feature_config.message_operation_stage1_max_attempts
                ),
                runtime_config=feature_config,
            )
        except Exception as exc:
            logger.warning(
                "Message-operation Stage 1 delivery failed open: error=%s",
                type(exc).__name__,
            )
    if (
        feature_config.message_operation_stage1_enabled
        and feature_config.message_operation_stage2_enabled
    ):
        try:
            delivered += await deliver_runtime_incident_stage2_notifications(
                session_factory,
                config=config,
                after_handoff_id=(
                    feature_config.message_operation_stage2_after_handoff_id
                ),
                limit=limit,
                claimed_at=claimed_at,
                lease_seconds=feature_config.notification_lease_seconds,
                max_attempts=feature_config.message_operation_stage2_max_attempts,
            )
        except Exception as exc:
            logger.warning(
                "Message-operation Stage 2 delivery failed open: error=%s",
                type(exc).__name__,
            )
    if not feature_config.telegram_notifications_enabled:
        return delivered
    notification_types = feature_config.telegram_notification_types
    if (
        feature_config.message_operation_stage2_enabled
        and notification_types is not None
    ):
        notification_types = frozenset(
            value
            for value in notification_types
            if value != "message_operation_failure"
        )
    if notification_types == frozenset():
        return delivered
    operation_now = claimed_at or datetime.now(UTC)
    for _ in range(max(1, min(int(limit), 100))):
        claim = claim_next_runtime_incident_notification(
            session_factory,
            claimed_at=claimed_at,
            lease_seconds=feature_config.notification_lease_seconds,
            notification_types=notification_types,
            after_incident_id=(
                feature_config.telegram_notification_after_incident_id
            ),
            excluded_notification_types=(
                frozenset({"message_operation_failure"})
                if feature_config.message_operation_stage2_enabled
                else frozenset()
            ),
        )
        if claim is None:
            break
        incident = claim["incident"]
        token = claim["claim_token"]
        try:
            await send_system_operator_bot_message(
                config=config,
                text=(
                    format_runtime_incident_diagnosis_notification(incident)
                    if incident.status == "diagnosed"
                    and incident.diagnosis_json
                    else format_runtime_incident_notification(incident)
                ),
            )
        except Exception as exc:
            failed_at = operation_now
            with session_factory() as session:
                session.execute(
                    update(RuntimeIncident)
                    .where(
                        RuntimeIncident.id == incident.id,
                        RuntimeIncident.notification_status == "delivering",
                        RuntimeIncident.notification_claim_token == token,
                    )
                    .values(
                        notification_status="failed",
                        notification_claim_token=None,
                        notification_claimed_at=failed_at,
                        updated_at=failed_at,
                    )
                )
                session.commit()
            if incident.incident_type != "notification_delivery_failure":
                capture_notification_failure(
                    session_factory,
                    config=feature_config,
                    source_kind="runtime_incident_notification",
                    source_record_id=str(incident.id),
                    error_type=type(exc).__name__,
                    occurred_at=failed_at,
                )
            # Do not retry the same failed row in a hot loop.
            break
        delivered_at = operation_now
        with session_factory() as session:
            result = session.execute(
                update(RuntimeIncident)
                .where(
                    RuntimeIncident.id == incident.id,
                    RuntimeIncident.notification_status == "delivering",
                    RuntimeIncident.notification_claim_token == token,
                )
                .values(
                    notification_status="delivered",
                    notification_claim_token=None,
                    notification_claimed_at=None,
                    notified_at=delivered_at,
                    updated_at=delivered_at,
                )
            )
            session.commit()
            delivered += int(result.rowcount == 1)
    return delivered


async def deliver_strategy_management_notifications(
    session_factory, *, config: SystemOperatorBotConfig, group_labels=None, limit: int = 20,
    claimed_at: datetime | None = None, lease_seconds: float = 120.0,
) -> int:
    """Deliver with a durable lease and at-least-once crash semantics.

    Telegram ``sendMessage`` has no idempotency key. A process death after
    Telegram accepts a message but before ``delivered`` commits can therefore
    cause one repeat after lease expiry. The stable notification ID embedded in
    the text is the dedup marker; committed successes are never reclaimed.
    """
    from telegram_kol_research.models import StrategyManagementNotification

    enqueue_strategy_management_notifications(session_factory, group_labels=group_labels)
    delivered = 0
    for _ in range(max(1, min(int(limit), 100))):
        claim = claim_next_strategy_management_notification(
            session_factory, claimed_at=claimed_at, lease_seconds=lease_seconds
        )
        if claim is None:
            break
        payload = dict(claim["payload"])
        payload["notification_id"] = claim["id"]
        if group_labels and payload.get("source_chat_id") is not None:
            payload["source_chat_title"] = _safe_management_text(
                group_labels.get(payload["source_chat_id"]), limit=120
            )
        try:
            for message in split_strategy_management_notification(payload):
                await send_system_operator_bot_message(config=config, text=message)
        except Exception as exc:
            failed_at = datetime.now(UTC)
            with session_factory() as session:
                session.execute(
                    update(StrategyManagementNotification)
                    .where(
                        StrategyManagementNotification.id == claim["id"],
                        StrategyManagementNotification.status == "delivering",
                        StrategyManagementNotification.claim_token == claim["claim_token"],
                    )
                    .values(
                        status="failed", claim_token=None,
                        lease_expires_at=None,
                        delivery_error=type(exc).__name__,
                        updated_at=failed_at,
                    )
                )
                session.commit()
            from telegram_kol_research.runtime_incident_adapters import (
                capture_notification_failure,
                capture_runtime_incident_best_effort,
            )

            capture_runtime_incident_best_effort(
                capture_notification_failure,
                session_factory,
                source_kind="strategy_management_notification",
                source_record_id=str(claim["id"]),
                error_type=type(exc).__name__,
                occurred_at=failed_at,
            )
            # Retry on a later worker tick. Reclaiming the just-failed row in the
            # same tick would create an unbounded hot loop during an outage.
            break
        with session_factory() as session:
            result = session.execute(
                update(StrategyManagementNotification)
                .where(
                    StrategyManagementNotification.id == claim["id"],
                    StrategyManagementNotification.status == "delivering",
                    StrategyManagementNotification.claim_token == claim["claim_token"],
                )
                .values(
                    status="delivered", claim_token=None, delivery_error=None,
                    lease_expires_at=None,
                    notified_at=datetime.now(UTC), updated_at=datetime.now(UTC),
                )
            )
            session.commit()
            delivered += int(result.rowcount == 1)
    return delivered


async def run_strategy_management_notification_loop(
    *, session_factory, config: SystemOperatorBotConfig, group_labels=None,
    interval_seconds: float = 5.0,
) -> None:
    while True:
        try:
            await deliver_strategy_management_notifications(
                session_factory, config=config, group_labels=group_labels
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(max(0.1, float(interval_seconds)))


def _run_operator_maintenance_cycle(
    session_factory,
    *,
    deepcoin_client_factory=None,
) -> None:
    """Run one operator maintenance cycle as a single blocking unit.

    The settings read, the exchange client construction, the tick, and the
    client close all stay on one thread: the client's lifecycle must not be
    split across threads, and closing an ``httpx.Client`` back on the event
    loop would reintroduce the blocking call this indirection removes.
    """

    execution_client = None
    try:
        execution_settings = load_trading_settings(session_factory)
        execution_mode = str(
            execution_settings.instruction_execution_contract_mode
        )
        if execution_mode != "disabled":
            if deepcoin_client_factory is None:
                from telegram_kol_research.deepcoin_client import (
                    build_deepcoin_client_from_env,
                )

                execution_client = build_deepcoin_client_from_env()
            else:
                execution_client = deepcoin_client_factory()
        run_operator_maintenance_tick(
            session_factory,
            now=datetime.now(UTC),
            execution_contract_mode=execution_mode,
            execution_entry_after_item_id=int(
                execution_settings.instruction_execution_entry_after_item_id
            ),
            execution_reconciliation_client=execution_client,
        )
    finally:
        close_client = getattr(execution_client, "close", None)
        if callable(close_client):
            close_client()


async def run_runtime_incident_notification_loop(
    *,
    session_factory,
    config: SystemOperatorBotConfig,
    interval_seconds: float = 5.0,
    runtime_config: RuntimeIncidentConfig | None = None,
    deepcoin_client_factory=None,
) -> None:
    """Poll the Phase 2 outbox through the dedicated system operator bot."""

    if runtime_config is None:
        try:
            feature_config = load_runtime_incident_config()
        except Exception:
            feature_config = RuntimeIncidentConfig()
    else:
        feature_config = runtime_config
    while True:
        try:
            await run_on_management_worker(
                _run_operator_maintenance_cycle,
                session_factory,
                deepcoin_client_factory=deepcoin_client_factory,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        try:
            await deliver_runtime_incident_notifications(
                session_factory,
                config=config,
                runtime_config=feature_config,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(max(0.1, float(interval_seconds)))


def run_operator_maintenance_tick(
    session_factory,
    *,
    now: datetime,
    entry_admission_limit: int = 20,
    execution_contract_mode: str = "disabled",
    execution_entry_after_item_id: int = 0,
    execution_reconciliation_client=None,
    execution_reconciliation_limit: int = 20,
):
    """Run bounded non-exchange maintenance owned by the operator loop."""

    entry_result = reconcile_due_entry_admissions(
        session_factory,
        now=now,
        limit=max(0, min(int(entry_admission_limit), 100)),
        execution_contract_mode=execution_contract_mode,
        entry_after_item_id=int(execution_entry_after_item_id),
    )
    if (
        execution_contract_mode != "disabled"
        and execution_reconciliation_client is not None
    ):
        reconciliation_result = reconcile_instruction_execution_contracts(
            session_factory,
            client=execution_reconciliation_client,
            reconciled_at=now,
            mode=execution_contract_mode,
            limit=max(0, min(int(execution_reconciliation_limit), 100)),
        )
        _record_execution_reconciliation_monitor_facts(
            session_factory,
            result=reconciliation_result,
            observed_at=now,
        )
    return entry_result


def _record_execution_reconciliation_monitor_facts(
    session_factory,
    *,
    result,
    observed_at: datetime,
) -> None:
    """Bridge transient exact-readback failures into the dormant shadow scanner."""

    from telegram_kol_research.config import load_runtime_scanner_config
    from telegram_kol_research.models import RuntimeIncidentObservation
    from telegram_kol_research.runtime_incident_observations import record_observations
    from telegram_kol_research.runtime_incident_rules import evaluate_rule

    config = load_runtime_scanner_config(
        environ=dict(os.environ),
    )
    rule_id = "instruction_execution_contradiction_v1"
    if not config.enabled or not config.shadow_only or rule_id not in config.rules:
        return
    transient_codes = {
        "exchange_snapshot_incomplete",
        "exchange_evidence_duplicate",
    }
    resolved_keys = set(
        str(value)
        for value in tuple(
            getattr(result, "resolved_transient_fact_keys", ()) or ()
        )
    )
    current: dict[str, dict[str, object]] = {}
    for fact in tuple(getattr(result, "facts", ()) or ()):
        code = str(getattr(fact, "code", "") or "")
        contract_id = getattr(fact, "contract_id", None)
        item_id = getattr(fact, "message_instruction_item_id", None)
        raw_id = getattr(fact, "raw_message_id", None)
        if code not in transient_codes or not all(
            isinstance(value, int) and value > 0
            for value in (contract_id, item_id, raw_id)
        ):
            continue
        object_id = f"{contract_id}-{code}"
        current[object_id] = {
            "complete": True,
            "object_id": object_id,
            "execution_contradiction": True,
            "reason_code": code,
            "contract_id": contract_id,
            "message_instruction_item_id": item_id,
            "raw_message_id": raw_id,
            "future_contract": False,
            "exact_historical": True,
            "evidence_references": (
                f"instruction-contract:{contract_id}",
                f"instruction-item:{item_id}",
                f"raw-message:{raw_id}",
            ),
        }
    with session_factory() as session:
        prior = (
            session.query(RuntimeIncidentObservation)
            .filter(
                RuntimeIncidentObservation.rule_id == rule_id,
                RuntimeIncidentObservation.state.in_(("observing", "shadow_confirmed")),
            )
            .order_by(RuntimeIncidentObservation.last_observed_at.asc())
            .limit(100)
            .all()
        )
        facts: list[dict[str, object]] = []
        for observation in prior:
            object_id = str(observation.object_id)
            if not object_id.endswith(
                ("-exchange_snapshot_incomplete", "-exchange_evidence_duplicate")
            ):
                continue
            existing = current.pop(object_id, None)
            if existing is not None:
                facts.append(existing)
                continue
            if object_id not in resolved_keys:
                continue
            try:
                summary = json.loads(observation.summary_json or "{}")
            except (json.JSONDecodeError, TypeError):
                summary = {}
            if not isinstance(summary, dict):
                summary = {}
            contract_id = int(summary.get("contract_id") or object_id.split("-", 1)[0])
            item_id = int(summary.get("message_instruction_item_id") or 0)
            raw_id = int(summary.get("raw_message_id") or 0)
            reason_code = str(summary.get("reason_code") or object_id.split("-", 1)[1])
            references = [f"instruction-contract:{contract_id}"]
            if item_id > 0:
                references.append(f"instruction-item:{item_id}")
            if raw_id > 0:
                references.append(f"raw-message:{raw_id}")
            facts.append(
                {
                    "complete": True,
                    "object_id": object_id,
                    "execution_contradiction": False,
                    "reason_code": reason_code,
                    "contract_id": contract_id,
                    "message_instruction_item_id": item_id,
                    "raw_message_id": raw_id,
                    "future_contract": bool(summary.get("future_contract")),
                    "exact_historical": bool(summary.get("exact_historical", True)),
                    "evidence_references": tuple(references),
                }
            )
            if len(facts) == 100:
                break
    facts.extend(list(current.values())[: max(0, 100 - len(facts))])
    if not facts:
        return
    record_observations(
        session_factory,
        observations=tuple(evaluate_rule(rule_id, fact) for fact in facts),
        observed_at=observed_at,
    )


def build_pending_entry_expiry_review_reply_markup(payload: dict[str, Any]) -> dict[str, Any]:
    lifecycle_id = payload.get("lifecycle_id")
    return {
        "inline_keyboard": [
            [
                {
                    "text": "\u7ee7\u7eed\u7b49\u5f85",
                    "callback_data": f"expiry_continue:{lifecycle_id}",
                }
            ],
            [
                {
                    "text": "\u8fc7\u671f\u5e76\u64a4\u5355",
                    "callback_data": f"expiry_expire_cancel:{lifecycle_id}",
                },
                {
                    "text": "\u66f4\u65b0\u72b6\u6001",
                    "callback_data": f"expiry_refresh:{lifecycle_id}",
                },
            ],
        ]
    }


async def send_system_operator_bot_message(
    *,
    config: SystemOperatorBotConfig,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> int | str | None:
    payload: dict[str, Any] = {
        "chat_id": config.chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{config.bot_token}/sendMessage",
            json=payload,
        )
        response.raise_for_status()
        try:
            body = response.json()
        except (TypeError, ValueError):
            return None
        if not isinstance(body, dict):
            return None
        result = body.get("result")
        if not isinstance(result, dict):
            return None
        return result.get("message_id")


async def send_system_operator_bot_document(
    *,
    config: SystemOperatorBotConfig,
    filename: str,
    content: str,
    caption: str,
) -> int | str | None:
    if len(content.encode("utf-8")) > 32768:
        raise ValueError("handoff document exceeds byte budget")
    async with httpx.AsyncClient(timeout=config.timeout_seconds, trust_env=False) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{config.bot_token}/sendDocument",
            data={"chat_id": config.chat_id, "caption": caption[:1024]},
            files={"document": (filename[:128], content.encode("utf-8"), "application/json")},
        )
        response.raise_for_status()
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        return result.get("message_id") if isinstance(result, dict) else None


async def send_message_instruction_summary_notification(
    *, config: SystemOperatorBotConfig, payload: dict[str, Any]
) -> None:
    """Deliver every bounded chunk of a persisted message outcome payload."""

    for text in split_message_instruction_summary(payload):
        await send_system_operator_bot_message(config=config, text=text)


async def deliver_message_instruction_summary_notification(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    raw_message_id: int,
    chat_title: str | None = None,
    claimed_at: datetime | None = None,
) -> bool:
    """Deliver a completed summary through the leased durable outbox."""

    payload = claim_message_instruction_summary(
        session_factory,
        raw_message_id=raw_message_id,
        claimed_at=claimed_at or datetime.now(UTC),
        chat_title=chat_title,
    )
    if payload is None:
        return False
    payload.update(
        _load_entry_status_for_instruction_summary(
            session_factory, raw_message_id=raw_message_id
        )
    )
    completed_at = claimed_at or datetime.now(UTC)
    try:
        await send_message_instruction_summary_notification(
            config=config,
            payload=payload,
        )
    except Exception as exc:
        finish_message_instruction_summary_delivery(
            session_factory,
            claim_token=payload["notification_claim_token"],
            item_ids=payload["notification_item_ids"],
            delivered=False,
            completed_at=completed_at,
            error=str(exc),
        )
        from telegram_kol_research.runtime_incident_adapters import (
            capture_notification_failure,
            capture_runtime_incident_best_effort,
        )

        capture_runtime_incident_best_effort(
            capture_notification_failure,
            session_factory,
            source_kind="message_instruction_summary",
            source_record_id=str(raw_message_id),
            error_type=type(exc).__name__,
            occurred_at=completed_at,
        )
        logger.exception(
            "message instruction summary delivery failed for raw_message_id=%s",
            raw_message_id,
        )
        return False
    finish_message_instruction_summary_delivery(
        session_factory,
        claim_token=payload["notification_claim_token"],
        item_ids=payload["notification_item_ids"],
        delivered=True,
        completed_at=completed_at,
    )
    return True


async def deliver_pending_message_instruction_summaries(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    claimed_at: datetime | None = None,
    limit: int = 20,
) -> int:
    """Retry a bounded batch of pending, failed, or expired summary deliveries."""

    from telegram_kol_research.models import MessageInstructionItem

    now = claimed_at or datetime.now(UTC)
    lease_cutoff = now - SUMMARY_NOTIFICATION_LEASE
    with session_factory() as session:
        candidate_item = aliased(MessageInstructionItem)
        blocking_item = aliased(MessageInstructionItem)
        raw_message_ids = [
            int(raw_message_id)
            for (raw_message_id,) in (
                session.query(candidate_item.raw_message_id)
                .filter(candidate_item.retired_at.is_(None))
                .filter(candidate_item.status.in_(sorted(FINISH_STATUSES)))
                .filter(
                    or_(
                        candidate_item.summary_notification_status.in_(
                            ["pending", "failed"]
                        ),
                        candidate_item.summary_notification_status.is_(None),
                        and_(
                            candidate_item.summary_notification_status == "delivering",
                            or_(
                                candidate_item.summary_notification_claimed_at.is_(None),
                                candidate_item.summary_notification_claimed_at
                                <= lease_cutoff,
                            ),
                        ),
                    )
                )
                .filter(
                    ~exists(
                        select(blocking_item.id).where(
                            blocking_item.raw_message_id
                            == candidate_item.raw_message_id,
                            blocking_item.retired_at.is_(None),
                            ~blocking_item.status.in_(sorted(FINISH_STATUSES)),
                        )
                    )
                )
                .distinct()
                .order_by(candidate_item.raw_message_id.asc())
                .limit(max(1, int(limit)))
                .all()
            )
        ]

    delivered = 0
    for raw_message_id in raw_message_ids:
        if await deliver_message_instruction_summary_notification(
            session_factory,
            config=config,
            raw_message_id=raw_message_id,
            claimed_at=now,
        ):
            delivered += 1
            if delivered >= max(1, int(limit)):
                break
    return delivered


async def deliver_pending_position_attribution_incidents(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    delivered_at: datetime | None = None,
    limit: int = 20,
) -> int:
    """Claim and deliver new attribution incidents without mutating ownership."""

    from telegram_kol_research.models import PositionAttributionAudit

    now = delivered_at or datetime.now(UTC)
    with session_factory() as session:
        candidate_ids = [
            int(row_id)
            for (row_id,) in (
                session.query(PositionAttributionAudit.id)
            .filter(PositionAttributionAudit.notification_status == "pending")
            .order_by(PositionAttributionAudit.id.asc())
            .limit(max(1, int(limit)))
            .all()
            )
        ]

    incident_ids: list[int] = []
    for incident_id in candidate_ids:
        with session_factory() as session:
            claimed = (
                session.query(PositionAttributionAudit)
                .filter(PositionAttributionAudit.id == incident_id)
                .filter(PositionAttributionAudit.notification_status == "pending")
                .update(
                    {
                        PositionAttributionAudit.notification_status: "delivering",
                        PositionAttributionAudit.notification_error: None,
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            if claimed == 1:
                incident_ids.append(incident_id)

    delivered = 0
    for incident_id in incident_ids:
        with session_factory() as session:
            row = session.get(PositionAttributionAudit, incident_id)
            if row is None or row.notification_status != "delivering":
                continue
            try:
                evidence = json.loads(row.evidence_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = {}
            if not isinstance(evidence, dict):
                evidence = {}
            payload = {
                "venue": row.venue,
                "pos_id": row.pos_id,
                "state": row.new_state,
                "candidate_leg_ids": evidence.get("candidate_leg_ids", []),
                "evidence_source_errors": evidence.get("errors", {}),
            }
        try:
            await send_system_operator_bot_message(
                config=config,
                text=format_position_attribution_incident_message(payload),
            )
        except Exception as exc:
            with session_factory() as session:
                row = session.get(PositionAttributionAudit, incident_id)
                if row is not None:
                    row.notification_status = "failed"
                    row.notification_error = str(exc)
                    session.commit()
            continue
        with session_factory() as session:
            row = session.get(PositionAttributionAudit, incident_id)
            if row is not None:
                row.notification_status = "delivered"
                row.notification_error = None
                row.notified_at = now
                session.commit()
                delivered += 1
    return delivered


async def deliver_pending_position_protection_incidents(
    session_factory,
    *,
    config: SystemOperatorBotConfig,
    delivered_at: datetime | None = None,
    limit: int = 20,
) -> int:
    """Deliver each fingerprinted protection incident once, without trade I/O."""

    from telegram_kol_research.models import PositionProtectionIncident

    now = delivered_at or datetime.now(UTC)
    with session_factory() as session:
        ids = [int(item[0]) for item in session.query(PositionProtectionIncident.id)
               .filter(PositionProtectionIncident.delivery_status == "pending")
               .order_by(PositionProtectionIncident.id.asc()).limit(max(1, int(limit))).all()]
    delivered = 0
    for incident_id in ids:
        with session_factory() as session:
            claimed = session.query(PositionProtectionIncident).filter(
                PositionProtectionIncident.id == incident_id,
                PositionProtectionIncident.delivery_status == "pending",
            ).update({PositionProtectionIncident.delivery_status: "delivering"}, synchronize_session=False)
            session.commit()
            if claimed != 1:
                continue
            row = session.get(PositionProtectionIncident, incident_id)
            try:
                evidence = json.loads(row.evidence_json or "{}") if row else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = {}
        try:
            await send_system_operator_bot_message(
                config=config,
                text=format_position_protection_incident_message({
                    "venue": row.venue, "pos_id": row.pos_id,
                    "incident_type": row.incident_type, "evidence": evidence,
                }),
            )
        except Exception as exc:
            with session_factory() as session:
                row = session.get(PositionProtectionIncident, incident_id)
                if row is not None:
                    row.delivery_status, row.delivery_error = "failed", str(exc)
                    session.commit()
            continue
        with session_factory() as session:
            row = session.get(PositionProtectionIncident, incident_id)
            if row is not None:
                row.delivery_status, row.delivery_error, row.notified_at = "delivered", None, now
                session.commit()
                delivered += 1
    return delivered


async def send_pending_entry_expiry_review(
    *,
    config: SystemOperatorBotConfig,
    payload: dict[str, Any],
) -> None:
    await send_system_operator_bot_message(
        config=config,
        text=format_pending_entry_expiry_review_message(payload),
        reply_markup=build_pending_entry_expiry_review_reply_markup(payload),
    )


async def send_ai_recognition_conflict_review(
    *,
    config: SystemOperatorBotConfig,
    payload: dict[str, Any],
) -> None:
    await send_system_operator_bot_message(
        config=config,
        text=format_ai_recognition_conflict_review_message(payload),
    )


async def send_semantic_disagreement_notification(
    *,
    config: SystemOperatorBotConfig,
    payload: dict[str, Any],
) -> None:
    await send_system_operator_bot_message(
        config=config,
        text=format_semantic_disagreement_notification(payload),
        reply_markup=None,
    )


def _format_range(low: Any, high: Any) -> str:
    if low is None and high is None:
        return "-"
    if high is None or low == high:
        return _format_value(low)
    if low is None:
        return _format_value(high)
    return f"{_format_value(low)}-{_format_value(high)}"


def _format_value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _truncate_text(value: Any, *, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _format_local_time(value: Any) -> str:
    local = utc_naive_to_local(value)
    if local is None:
        return "-"
    return local.strftime("%Y-%m-%d %H:%M:%S Asia/Shanghai")
