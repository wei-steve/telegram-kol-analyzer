"""Pure, secret-free production safety snapshot evaluation."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any


MAX_ALERT_LENGTH = 1200

_SAFE_TOKEN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
_SAFE_TIMESTAMP = re.compile(r"[0-9T:+.-]{1,40}\Z")
_ADAPTER_NAMES = frozenset({"service", "head", "settings", "journal", "events", "audit"})
_AUDIT_ALERT_STATES = (
    "blocked",
    "partial_failed",
    "recovery_required",
    "submit_unknown",
)
_NORMAL_EVENT_STATUSES = frozenset(
    {"submitted", "succeeded", "confirmed", "cancelled", "skipped", "resolved", "restored"}
)
_RECOVERY_EVENT_STATUSES = frozenset({"recovery_required"})
_FIXED_REASON_CODES = frozenset(
    {
        "adapter_failure",
        "audit_abnormal",
        "audit_incomplete",
        "auto_trade_enabled_drift",
        "duplicate_manual_close",
        "event_recovery_status",
        "event_unknown_status",
        "head_drift",
        "journal_errors",
        "malformed_snapshot",
        "management_execution_mode_drift",
        "max_concurrent_positions_drift",
        "service_inactive",
    }
)


@dataclass(frozen=True, slots=True)
class MonitorExpectations:
    head: str
    auto_trade_enabled: bool
    management_execution_mode: str
    max_concurrent_positions: int


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    service_state: str
    head: str
    settings: Mapping[str, Any]
    journal_error_count: int
    abnormal_events: Sequence[Mapping[str, Any]]
    audit: Mapping[str, Any] | None
    adapter_failures: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class MonitorResult:
    healthy: bool
    reason_codes: tuple[str, ...]
    details: Mapping[str, Any]


def evaluate_monitor_snapshot(
    snapshot: MonitorSnapshot,
    expectations: MonitorExpectations,
) -> MonitorResult:
    """Evaluate one already-collected snapshot without I/O or raw error retention."""

    reasons: set[str] = set()
    details: dict[str, Any] = {}

    service_state = _safe_token_value(snapshot.service_state)
    if service_state is None:
        reasons.add("malformed_snapshot")
        service_state = "invalid"
    if service_state != "active":
        reasons.add("service_inactive")
        details["service_state"] = service_state

    observed_head = _safe_token_value(snapshot.head)
    expected_head = _safe_token_value(expectations.head)
    if observed_head is None or expected_head is None:
        reasons.add("malformed_snapshot")
    if observed_head != expected_head:
        reasons.add("head_drift")
        details["head"] = observed_head or "invalid"
        details["expected_head"] = expected_head or "invalid"

    _evaluate_settings(snapshot.settings, expectations, reasons, details)

    journal_error_count = _safe_count(snapshot.journal_error_count)
    if journal_error_count is None:
        reasons.add("malformed_snapshot")
    elif journal_error_count:
        reasons.add("journal_errors")
        details["journal_error_count"] = journal_error_count

    adapter_failures = _safe_adapter_failures(snapshot.adapter_failures)
    if adapter_failures is None:
        reasons.add("malformed_snapshot")
    elif adapter_failures:
        reasons.add("adapter_failure")
        details["adapter_failures"] = adapter_failures

    _evaluate_events(snapshot.abnormal_events, reasons, details)
    if snapshot.audit is not None:
        _evaluate_audit(snapshot.audit, reasons, details)

    reason_codes = tuple(sorted(reasons))
    return MonitorResult(
        healthy=not reason_codes,
        reason_codes=reason_codes,
        details=details,
    )


def _evaluate_settings(
    settings: object,
    expectations: MonitorExpectations,
    reasons: set[str],
    details: dict[str, Any],
) -> None:
    if not isinstance(settings, Mapping):
        reasons.add("malformed_snapshot")
        return

    auto_trade_enabled = settings.get("auto_trade_enabled")
    if type(auto_trade_enabled) is not bool:
        reasons.add("malformed_snapshot")
    elif auto_trade_enabled != expectations.auto_trade_enabled:
        reasons.add("auto_trade_enabled_drift")
        details["auto_trade_enabled"] = auto_trade_enabled
        details["expected_auto_trade_enabled"] = expectations.auto_trade_enabled

    management_mode = _safe_token_value(settings.get("management_execution_mode"))
    expected_mode = _safe_token_value(expectations.management_execution_mode)
    if management_mode is None or expected_mode is None:
        reasons.add("malformed_snapshot")
    elif management_mode != expected_mode:
        reasons.add("management_execution_mode_drift")
        details["management_execution_mode"] = management_mode
        details["expected_management_execution_mode"] = expected_mode

    position_limit = _safe_count(settings.get("max_concurrent_positions"))
    expected_limit = _safe_count(expectations.max_concurrent_positions)
    if position_limit is None or expected_limit is None:
        reasons.add("malformed_snapshot")
    elif position_limit != expected_limit:
        reasons.add("max_concurrent_positions_drift")
        details["max_concurrent_positions"] = position_limit
        details["expected_max_concurrent_positions"] = expected_limit


def _evaluate_events(
    events: object,
    reasons: set[str],
    details: dict[str, Any],
) -> None:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes, bytearray)):
        reasons.add("malformed_snapshot")
        return

    manual_close_positions: list[str] = []
    unknown_count = 0
    recovery_count = 0
    for event in events:
        if not isinstance(event, Mapping):
            reasons.add("malformed_snapshot")
            continue
        action = _safe_token_value(event.get("action"))
        status = _safe_token_value(event.get("status"))
        if action is None or status is None:
            reasons.add("malformed_snapshot")
            continue
        if status in _RECOVERY_EVENT_STATUSES:
            recovery_count += 1
        elif status not in _NORMAL_EVENT_STATUSES:
            unknown_count += 1

        if action == "close_bound_position_market":
            pos_id = _safe_token_value(event.get("pos_id"))
            if pos_id is None:
                reasons.add("malformed_snapshot")
            else:
                manual_close_positions.append(pos_id)

    if unknown_count:
        reasons.add("event_unknown_status")
        details["unknown_event_count"] = unknown_count
    if recovery_count:
        reasons.add("event_recovery_status")
        details["recovery_event_count"] = recovery_count

    duplicate_count = sum(
        count - 1 for count in Counter(manual_close_positions).values() if count > 1
    )
    if duplicate_count:
        reasons.add("duplicate_manual_close")
        details["duplicate_manual_close_count"] = duplicate_count


def _evaluate_audit(
    audit: object,
    reasons: set[str],
    details: dict[str, Any],
) -> None:
    if not isinstance(audit, Mapping):
        reasons.update({"audit_incomplete", "malformed_snapshot"})
        return

    incomplete = False
    abnormal = False

    if audit.get("snapshot_status") != "stable":
        incomplete = True
    if audit.get("snapshot_validation") != "ok":
        incomplete = True
    if audit.get("output_complete") is not True:
        incomplete = True
    if audit.get("batches_truncated") is not False:
        incomplete = True
    if audit.get("schema_status") != "available":
        abnormal = True

    legacy = audit.get("legacy_pending_management")
    if not isinstance(legacy, Mapping):
        incomplete = True
        reasons.add("malformed_snapshot")
    elif (
        legacy.get("complete") is not True
        or legacy.get("truncated") is not False
        or legacy.get("scan_truncated") is not False
    ):
        incomplete = True

    counts = audit.get("counts")
    abnormal_count = 0
    if not isinstance(counts, Mapping):
        incomplete = True
        reasons.add("malformed_snapshot")
    else:
        for state in _AUDIT_ALERT_STATES:
            count = _safe_count(counts.get(state))
            if count is None:
                incomplete = True
                reasons.add("malformed_snapshot")
            else:
                abnormal_count += count
    if abnormal_count:
        abnormal = True
        details["audit_abnormal_count"] = abnormal_count

    for field in ("malformed_row_count", "malformed_field_count"):
        count = _safe_count(audit.get(field))
        if count is None:
            incomplete = True
            reasons.add("malformed_snapshot")
        elif count:
            abnormal = True

    if incomplete:
        reasons.add("audit_incomplete")
        details["audit_complete"] = False
    if abnormal:
        reasons.add("audit_abnormal")
        details["audit_abnormal"] = True


def format_monitor_alert(
    result: MonitorResult,
    *,
    checked_at: datetime | str,
) -> str:
    """Build one bounded Telegram alert from fixed labels and allowlisted details."""

    reason_codes = sorted(
        code for code in result.reason_codes if code in _FIXED_REASON_CODES
    )
    if not reason_codes:
        reason_codes = ["malformed_snapshot"]
    lines = [
        "【生产安全监控异常】",
        f"检查时间：{_safe_checked_at(checked_at)}",
        f"异常代码：{','.join(reason_codes)}",
    ]

    labels = {
        "service_state": "服务状态",
        "head": "当前版本",
        "expected_head": "期望版本",
        "auto_trade_enabled": "自动交易",
        "expected_auto_trade_enabled": "期望自动交易",
        "management_execution_mode": "管理执行模式",
        "expected_management_execution_mode": "期望管理模式",
        "max_concurrent_positions": "单组持仓上限",
        "expected_max_concurrent_positions": "期望持仓上限",
        "journal_error_count": "日志错误数",
        "unknown_event_count": "未知事件数",
        "recovery_event_count": "恢复事件数",
        "duplicate_manual_close_count": "重复精确平仓数",
        "audit_abnormal_count": "审计异常数",
        "audit_complete": "审计完整",
        "audit_abnormal": "审计异常",
        "adapter_failures": "适配器失败",
    }
    for key, label in labels.items():
        if key not in result.details:
            continue
        rendered = _safe_detail_value(key, result.details[key])
        if rendered is not None:
            lines.append(f"{label}：{rendered}")

    text = "\n".join(lines)
    if len(text) > MAX_ALERT_LENGTH:
        return text[: MAX_ALERT_LENGTH - 1] + "…"
    return text


def _safe_checked_at(value: datetime | str) -> str:
    rendered = value.isoformat() if isinstance(value, datetime) else value
    if isinstance(rendered, str) and _SAFE_TIMESTAMP.fullmatch(rendered):
        return rendered
    return "unknown"


def _safe_detail_value(key: str, value: object) -> str | None:
    if key == "adapter_failures":
        failures = _safe_adapter_failures(value)
        return ",".join(failures) if failures else None
    if type(value) is bool:
        return "true" if value else "false"
    if _safe_count(value) is not None:
        return str(value)
    token = _safe_token_value(value)
    return token or "invalid"


def _safe_token_value(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_TOKEN.fullmatch(value):
        return None
    return value


def _safe_count(value: object) -> int | None:
    if type(value) is not int or value < 0:
        return None
    return value


def _safe_adapter_failures(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    failures: set[str] = set()
    for item in value:
        failures.add(item if isinstance(item, str) and item in _ADAPTER_NAMES else "unknown")
    return tuple(sorted(failures))
