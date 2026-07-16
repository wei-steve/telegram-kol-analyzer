"""Pure, secret-free production safety snapshot evaluation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


MAX_ALERT_LENGTH = 1200
MAX_SAFE_COUNT = 1_000_000_000

_SAFE_EVENT_VALUE = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_GIT_HEAD = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_TIMESTAMP = re.compile(r"[0-9T:+.-]{1,40}\Z")
_SHA256_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_ADAPTER_NAMES = frozenset({"service", "head", "settings", "journal", "events", "audit"})
_MANAGEMENT_MODES = frozenset({"disabled", "shadow", "live"})
_SERVICE_STATES = frozenset(
    {
        "active",
        "activating",
        "deactivating",
        "failed",
        "inactive",
        "maintenance",
        "reloading",
        "unknown",
    }
)
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
_STATE_FIELDS = frozenset(
    {
        "last_window_at",
        "last_full_audit_date",
        "anomaly_fingerprint",
        "last_notification_at",
    }
)
_FINGERPRINT_DETAIL_KEYS = (
    "service_state",
    "head",
    "expected_head",
    "auto_trade_enabled",
    "expected_auto_trade_enabled",
    "management_execution_mode",
    "expected_management_execution_mode",
    "max_concurrent_positions",
    "expected_max_concurrent_positions",
    "journal_error_count",
    "unknown_event_count",
    "recovery_event_count",
    "duplicate_manual_close_count",
    "audit_abnormal_count",
    "audit_complete",
    "audit_abnormal",
    "adapter_failures",
)
_NOTIFICATION_SUPPRESSION = timedelta(hours=6)


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


@dataclass(frozen=True, slots=True)
class MonitorState:
    last_window_at: str | None = None
    last_full_audit_date: str | None = None
    anomaly_fingerprint: str | None = None
    last_notification_at: str | None = None


@dataclass(frozen=True, slots=True)
class MonitorNotificationDecision:
    should_notify: bool
    next_state: MonitorState


def load_monitor_state(path: str | Path) -> MonitorState:
    """Load the exact monitor-state schema, rebuilding malformed state safely."""

    try:
        payload = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        return _monitor_state_from_payload(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return MonitorState()


def save_monitor_state(path: str | Path, state: MonitorState) -> None:
    """Atomically persist only the allowlisted monitor state with mode 0600."""

    destination = Path(path)
    payload = _monitor_state_payload(state)
    rendered = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(rendered)
            temporary_file.flush()
            os.fchmod(temporary_file.fileno(), 0o600)
            os.fsync(temporary_file.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def fingerprint_monitor_result(result: MonitorResult) -> str:
    """Return a canonical SHA-256 for one bounded, secret-free result summary."""

    reason_codes = sorted(
        set(code for code in result.reason_codes if code in _FIXED_REASON_CODES)
    )
    if not reason_codes and not result.healthy:
        reason_codes = ["malformed_snapshot"]
    details: dict[str, str] = {}
    for key in _FINGERPRINT_DETAIL_KEYS:
        if key not in result.details:
            continue
        rendered = _safe_detail_value(key, result.details[key])
        if rendered is not None:
            details[key] = rendered
    canonical = json.dumps(
        {
            "details": details,
            "healthy": result.healthy is True,
            "reason_codes": reason_codes,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def decide_monitor_notification(
    result: MonitorResult,
    state: MonitorState,
    *,
    now: datetime,
) -> MonitorNotificationDecision:
    """Apply fingerprint change and six-hour repeat-notification policy."""

    _monitor_state_payload(state)
    now_rendered = _canonical_aware_datetime(now)
    if result.healthy:
        return MonitorNotificationDecision(
            should_notify=False,
            next_state=MonitorState(
                last_window_at=state.last_window_at,
                last_full_audit_date=state.last_full_audit_date,
                anomaly_fingerprint=None,
                last_notification_at=state.last_notification_at,
            ),
        )

    fingerprint = fingerprint_monitor_result(result)
    should_notify = fingerprint != state.anomaly_fingerprint
    if not should_notify:
        if state.last_notification_at is None:
            should_notify = True
        else:
            last_notification = datetime.fromisoformat(state.last_notification_at)
            should_notify = now - last_notification >= _NOTIFICATION_SUPPRESSION
    if not should_notify:
        return MonitorNotificationDecision(should_notify=False, next_state=state)
    return MonitorNotificationDecision(
        should_notify=True,
        next_state=MonitorState(
            last_window_at=state.last_window_at,
            last_full_audit_date=state.last_full_audit_date,
            anomaly_fingerprint=fingerprint,
            last_notification_at=now_rendered,
        ),
    )


def evaluate_monitor_snapshot(
    snapshot: MonitorSnapshot,
    expectations: MonitorExpectations,
) -> MonitorResult:
    """Evaluate one already-collected snapshot without I/O or raw error retention."""

    reasons: set[str] = set()
    details: dict[str, Any] = {}

    service_state = _safe_service_state(snapshot.service_state)
    if service_state is None:
        reasons.add("malformed_snapshot")
        service_state = "invalid"
    if service_state != "active":
        reasons.add("service_inactive")
        details["service_state"] = service_state

    observed_head = _safe_git_head(snapshot.head)
    expected_head = _safe_git_head(expectations.head)
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
    expected_auto_trade_enabled = expectations.auto_trade_enabled
    if (
        type(auto_trade_enabled) is not bool
        or type(expected_auto_trade_enabled) is not bool
    ):
        reasons.add("malformed_snapshot")
    elif auto_trade_enabled != expected_auto_trade_enabled:
        reasons.add("auto_trade_enabled_drift")
        details["auto_trade_enabled"] = auto_trade_enabled
        details["expected_auto_trade_enabled"] = expected_auto_trade_enabled

    management_mode = _safe_management_mode(settings.get("management_execution_mode"))
    expected_mode = _safe_management_mode(expectations.management_execution_mode)
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
        action = _safe_event_value(event.get("action"))
        status = _safe_event_value(event.get("status"))
        if action is None or status is None:
            reasons.add("malformed_snapshot")
            continue
        if status in _RECOVERY_EVENT_STATUSES:
            recovery_count += 1
        elif status not in _NORMAL_EVENT_STATUSES and not (
            action == "close_bound_position_reservation" and status == "reserved"
        ):
            unknown_count += 1

        if action == "close_bound_position_market":
            pos_id = _safe_event_value(event.get("pos_id"))
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
    if key == "service_state":
        return _safe_service_state(value) or "invalid"
    if key in {"head", "expected_head"}:
        head = _safe_git_head(value)
        return head[:12] if head is not None else "invalid"
    if key in {"management_execution_mode", "expected_management_execution_mode"}:
        return _safe_management_mode(value) or "invalid"
    if type(value) is bool:
        return "true" if value else "false"
    if _safe_count(value) is not None:
        return str(value)
    return "invalid"


def _safe_event_value(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_EVENT_VALUE.fullmatch(value):
        return None
    return value


def _safe_git_head(value: object) -> str | None:
    if not isinstance(value, str) or not _GIT_HEAD.fullmatch(value):
        return None
    return value


def _safe_management_mode(value: object) -> str | None:
    if not isinstance(value, str) or value not in _MANAGEMENT_MODES:
        return None
    return value


def _safe_service_state(value: object) -> str | None:
    if not isinstance(value, str) or value not in _SERVICE_STATES:
        return None
    return value


def _safe_count(value: object) -> int | None:
    if type(value) is not int or not 0 <= value <= MAX_SAFE_COUNT:
        return None
    return value


def _safe_adapter_failures(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    failures: set[str] = set()
    for item in value:
        failures.add(item if isinstance(item, str) and item in _ADAPTER_NAMES else "unknown")
    return tuple(sorted(failures))


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _monitor_state_from_payload(payload: object) -> MonitorState:
    if not isinstance(payload, dict) or set(payload) != _STATE_FIELDS:
        raise ValueError("invalid monitor state schema")
    state = MonitorState(
        last_window_at=payload["last_window_at"],
        last_full_audit_date=payload["last_full_audit_date"],
        anomaly_fingerprint=payload["anomaly_fingerprint"],
        last_notification_at=payload["last_notification_at"],
    )
    _monitor_state_payload(state)
    return state


def _monitor_state_payload(state: MonitorState) -> dict[str, str | None]:
    if not isinstance(state, MonitorState):
        raise TypeError("state must be MonitorState")
    if state.last_window_at is not None:
        _validate_canonical_aware_datetime(state.last_window_at)
    if state.last_full_audit_date is not None:
        try:
            parsed_date = date.fromisoformat(state.last_full_audit_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid full-audit date") from exc
        if parsed_date.isoformat() != state.last_full_audit_date:
            raise ValueError("non-canonical full-audit date")
    if state.anomaly_fingerprint is not None and (
        not isinstance(state.anomaly_fingerprint, str)
        or not _SHA256_FINGERPRINT.fullmatch(state.anomaly_fingerprint)
    ):
        raise ValueError("invalid anomaly fingerprint")
    if state.last_notification_at is not None:
        _validate_canonical_aware_datetime(state.last_notification_at)
    return {
        "last_window_at": state.last_window_at,
        "last_full_audit_date": state.last_full_audit_date,
        "anomaly_fingerprint": state.anomaly_fingerprint,
        "last_notification_at": state.last_notification_at,
    }


def _canonical_aware_datetime(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.isoformat()


def _validate_canonical_aware_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError("timestamp must be canonical and timezone-aware")
    return parsed
