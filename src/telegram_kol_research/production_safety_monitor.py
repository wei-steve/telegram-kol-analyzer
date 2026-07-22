"""Pure, secret-free production safety snapshot evaluation."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from telegram_kol_research.system_operator_bot import (
    load_notification_bot_config,
    send_system_operator_bot_message,
    system_operator_bot_enabled,
)


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
        "journal_errors",
        "malformed_snapshot",
        "management_execution_mode_drift",
        "max_concurrent_positions_drift",
        "service_inactive",
        "state_invalid",
    }
)
_LOW_REPEAT_REASON_CODES = frozenset({"audit_abnormal"})
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
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_LOOKBACK = timedelta(minutes=35)
_MAX_COMMAND_OUTPUT_BYTES = 1_048_576
_MAX_HTTP_OUTPUT_BYTES = 65_536
_MAX_ABNORMAL_EVENTS = 200
_MAX_JOURNAL_ERRORS = 10_000
MONITOR_TEST_NOTIFICATION_TEXT = (
    "【监控测试】服务器安全监控通知链路验证\n"
    "本消息仅验证系统运维通知，不包含交易指令。"
)


def _load_monitor_bot_config():
    """Load only the service environment, never checkout configuration files."""

    return load_notification_bot_config(env_file_paths=[])


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
    state_invalid: bool = False


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


@dataclass(frozen=True, slots=True)
class MonitorRunOutcome:
    result: MonitorResult
    notification_status: str
    audit_ran: bool
    monitor_error: str | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.result.healthy and self.monitor_error is None else 1


@dataclass(frozen=True, slots=True)
class ProductionSafetyAdapters:
    """Bounded observation-only adapters used by the server monitor."""

    database_path: Path
    checkout_path: Path = Path(".")
    settings_url: str = "http://127.0.0.1:8000/api/trading-settings"
    service_name: str = "telegram-kol.service"
    audit_command: tuple[str, ...] = (
        sys.executable,
        "-m",
        "telegram_kol_research.cli",
    )

    def read_service_state(self) -> str:
        # A successful response from the service-owned loopback endpoint proves
        # the app is serving without granting this monitor system-bus access.
        read_loopback_settings(self.settings_url)
        return "active"

    def read_git_head(self) -> str:
        safe_checkout = self.checkout_path.resolve()
        completed = _run_bounded_command(
            (
                "git",
                "-c",
                f"safe.directory={safe_checkout}",
                "rev-parse",
                "HEAD",
            ),
            timeout_seconds=5,
            cwd=self.checkout_path,
        )
        if completed.returncode != 0:
            raise RuntimeError("git_head_unavailable")
        return completed.output.strip()

    def read_settings(self) -> Mapping[str, Any]:
        return read_loopback_settings(self.settings_url)

    def count_journal_errors(self, *, since: datetime) -> int:
        completed = _run_bounded_command(
            (
                "journalctl",
                "--unit",
                self.service_name,
                "--priority",
                "err",
                "--since",
                since.isoformat(),
                "--no-pager",
                "--output",
                "cat",
            ),
            timeout_seconds=10,
            max_output_bytes=262_144,
        )
        if completed.returncode != 0:
            raise RuntimeError("journal_unavailable")
        return min(
            _MAX_JOURNAL_ERRORS,
            sum(1 for line in completed.output.splitlines() if line.strip()),
        )

    def read_abnormal_events(
        self, *, since: datetime, limit: int
    ) -> tuple[Mapping[str, Any], ...]:
        return read_abnormal_execution_events(
            self.database_path,
            since=since,
            limit=limit,
        )

    def run_management_audit(self) -> Mapping[str, Any]:
        completed = _run_bounded_command(
            (
                *self.audit_command,
                "audit-management-batches",
                "--database-path",
                str(self.database_path),
                "--limit",
                "20",
                "--output-format",
                "json",
            ),
            timeout_seconds=180,
        )
        try:
            payload = json.loads(
                completed.output,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise RuntimeError("audit_output_invalid") from exc
        if not isinstance(payload, Mapping):
            raise RuntimeError("audit_output_invalid")
        if completed.returncode != 0:
            if payload.get("snapshot_reason") == "source_snapshots_differ":
                raise _SourceSnapshotsDiffer("audit_source_snapshots_differ")
            raise RuntimeError("audit_command_failed")
        return payload


@dataclass(frozen=True, slots=True)
class _CommandResult:
    returncode: int
    output: str


class _SourceSnapshotsDiffer(RuntimeError):
    """Signal one retry without exposing a nonzero child result as audit data."""


@dataclass(frozen=True, slots=True)
class _LoadedMonitorState:
    state: MonitorState
    invalid_existing_file: bool


def load_monitor_state(path: str | Path) -> MonitorState:
    """Load the exact monitor-state schema, rebuilding malformed state safely."""

    return _load_monitor_state(path).state


def _load_monitor_state(path: str | Path) -> _LoadedMonitorState:
    """Distinguish a normal first run from an unsafe existing state file."""

    try:
        payload = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
        return _LoadedMonitorState(
            state=_monitor_state_from_payload(payload),
            invalid_existing_file=False,
        )
    except FileNotFoundError:
        return _LoadedMonitorState(
            state=MonitorState(),
            invalid_existing_file=False,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return _LoadedMonitorState(
            state=MonitorState(),
            invalid_existing_file=True,
        )


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


def should_run_daily_audit(
    *,
    now: datetime,
    last_successful_date: str | None,
    force: bool = False,
) -> bool:
    """Schedule at most one successful audit day after 09:00 Shanghai time."""

    localized = _require_aware_datetime(now).astimezone(_SHANGHAI)
    if force:
        return True
    if localized.hour < 9:
        return False
    return last_successful_date != localized.date().isoformat()


def run_daily_management_audit(run_once) -> Mapping[str, Any]:
    """Retry only one transient source-snapshot mismatch, exactly once."""

    try:
        first = run_once()
    except _SourceSnapshotsDiffer:
        return run_once()
    if not isinstance(first, Mapping):
        raise TypeError("audit result must be a mapping")
    if first.get("snapshot_reason") != "source_snapshots_differ":
        return first
    second = run_once()
    if not isinstance(second, Mapping):
        raise TypeError("audit result must be a mapping")
    return second


def read_abnormal_execution_events(
    database_path: str | Path,
    *,
    since: datetime,
    limit: int = 100,
    connect=sqlite3.connect,
) -> tuple[Mapping[str, Any], ...]:
    """Read bounded identity/status fields from SQLite without write access."""

    if type(limit) is not int or not 1 <= limit <= _MAX_ABNORMAL_EVENTS:
        raise ValueError("invalid abnormal event limit")
    since_utc = _require_aware_datetime(since).astimezone(UTC).replace(tzinfo=None)
    uri = f"file:{database_path}?mode=ro"
    query = """
        SELECT action, status, pos_id
        FROM execution_events
        WHERE created_at >= ?
          AND (
            status NOT IN ('submitted', 'succeeded', 'confirmed', 'cancelled',
                           'skipped', 'resolved', 'restored')
            OR action = 'close_bound_position_market'
          )
        ORDER BY created_at DESC, action, status, pos_id
        LIMIT ?
    """
    with connect(uri, uri=True) as connection:
        rows = connection.execute(
            query,
            (since_utc.isoformat(sep=" "), limit + 1),
        ).fetchall()
    if len(rows) > limit:
        raise RuntimeError("events_incomplete")
    return tuple(
        {"action": row[0], "status": row[1], "pos_id": row[2]}
        for row in rows
    )


def read_loopback_settings(
    url: str,
    *,
    timeout_seconds: float = 30.0,
) -> Mapping[str, Any]:
    """Read the local settings endpoint while refusing any non-loopback URL."""

    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ValueError("settings URL must use loopback HTTP")
    body = bytearray()
    with httpx.stream(
        "GET",
        url,
        timeout=timeout_seconds,
        trust_env=False,
    ) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_HTTP_OUTPUT_BYTES:
                raise ValueError("settings response too large")
    payload = json.loads(
        body,
        object_pairs_hook=_strict_json_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(payload, Mapping):
        raise ValueError("settings response must be an object")
    return {
        "auto_trade_enabled": payload.get("auto_trade_enabled"),
        "management_execution_mode": payload.get("management_execution_mode"),
        "max_concurrent_positions": payload.get("max_concurrent_positions"),
    }


def run_production_safety_monitor(
    *,
    expectations: MonitorExpectations,
    state_path: str | Path,
    adapters,
    now: datetime,
    notify: bool,
    force_full_audit: bool = False,
    abnormal_event_limit: int = 100,
    lookback: timedelta = _DEFAULT_LOOKBACK,
    load_bot_config=_load_monitor_bot_config,
    send_bot_message=send_system_operator_bot_message,
) -> MonitorRunOutcome:
    """Collect, evaluate, deduplicate, optionally notify, and persist state."""

    checked_at = _require_aware_datetime(now)
    loaded_state = _load_monitor_state(state_path)
    state = loaded_state.state
    state_integrity_alert_pending = (
        loaded_state.invalid_existing_file
        or (
            state.anomaly_fingerprint is not None
            and state.last_notification_at is None
        )
    )
    if state.last_window_at is None:
        since = checked_at - lookback
    else:
        since = datetime.fromisoformat(state.last_window_at)

    failures: list[str] = []
    service_state = _read_adapter(adapters.read_service_state, "service", failures, "unknown")
    head = _read_adapter(adapters.read_git_head, "head", failures, "0" * 40)
    settings = _read_adapter(adapters.read_settings, "settings", failures, {})
    journal_error_count = _read_adapter(
        lambda: adapters.count_journal_errors(since=since),
        "journal",
        failures,
        0,
    )
    abnormal_events = _read_adapter(
        lambda: adapters.read_abnormal_events(
            since=since,
            limit=abnormal_event_limit,
        ),
        "events",
        failures,
        (),
    )
    window_sources_complete = not {"journal", "events"}.intersection(failures)

    audit = None
    audit_ran = should_run_daily_audit(
        now=checked_at,
        last_successful_date=state.last_full_audit_date,
        force=force_full_audit,
    )
    if audit_ran:
        audit = _read_adapter(
            lambda: run_daily_management_audit(adapters.run_management_audit),
            "audit",
            failures,
            None,
        )

    result = evaluate_monitor_snapshot(
        MonitorSnapshot(
            service_state=service_state,
            head=head,
            settings=settings,
            journal_error_count=journal_error_count,
            abnormal_events=abnormal_events,
            audit=audit,
            adapter_failures=tuple(failures),
            state_invalid=state_integrity_alert_pending,
        ),
        expectations,
    )

    successful_audit_date = state.last_full_audit_date
    if audit is not None and _audit_result_is_healthy(audit):
        successful_audit_date = checked_at.astimezone(_SHANGHAI).date().isoformat()
    base_state = MonitorState(
        last_window_at=(checked_at if window_sources_complete else since).isoformat(),
        last_full_audit_date=successful_audit_date,
        anomaly_fingerprint=state.anomaly_fingerprint,
        last_notification_at=state.last_notification_at,
    )
    decision = decide_monitor_notification(result, base_state, now=checked_at)
    if state_integrity_alert_pending and not result.healthy:
        # A fingerprint without a delivery timestamp is the four-field schema's
        # durable marker for a repaired state-integrity alert awaiting delivery.
        # Keep operational progress while notify=False, config failure, or send
        # failure prevents acknowledgement by the operator.
        next_state = MonitorState(
            last_window_at=base_state.last_window_at,
            last_full_audit_date=base_state.last_full_audit_date,
            anomaly_fingerprint=fingerprint_monitor_result(result),
            last_notification_at=None,
        )
    else:
        next_state = decision.next_state if not decision.should_notify else base_state
    notification_status = "not_needed" if result.healthy else "disabled"
    monitor_error = None

    if not result.healthy and decision.should_notify and notify:
        try:
            config = load_bot_config()
        except Exception:
            config = None
        if not system_operator_bot_enabled(config):
            notification_status = "config_missing"
            monitor_error = "notification_config_missing"
        else:
            try:
                _run_maybe_awaitable(
                    send_bot_message(
                        config=config,
                        text=format_monitor_alert(result, checked_at=checked_at),
                    )
                )
            except Exception:
                notification_status = "delivery_failed"
                monitor_error = "notification_delivery_failed"
            else:
                notification_status = "sent"
                next_state = decision.next_state
                continuing_reasons = tuple(
                    reason
                    for reason in result.reason_codes
                    if reason != "state_invalid"
                )
                if state_integrity_alert_pending and continuing_reasons:
                    # The delivered message included state_invalid, but that
                    # synthetic one-shot reason is acknowledged now. Persist
                    # only the continuing real anomaly so it keeps its normal
                    # six-hour dedupe identity on the repaired next run.
                    continuing_result = MonitorResult(
                        healthy=False,
                        reason_codes=continuing_reasons,
                        details=result.details,
                    )
                    next_state = MonitorState(
                        last_window_at=decision.next_state.last_window_at,
                        last_full_audit_date=decision.next_state.last_full_audit_date,
                        anomaly_fingerprint=fingerprint_monitor_result(
                            continuing_result
                        ),
                        last_notification_at=decision.next_state.last_notification_at,
                    )
    elif not result.healthy and not decision.should_notify:
        notification_status = "suppressed"
        next_state = decision.next_state

    try:
        save_monitor_state(state_path, next_state)
    except (OSError, TypeError, ValueError):
        monitor_error = "state_write_failed"

    return MonitorRunOutcome(
        result=result,
        notification_status=notification_status,
        audit_ran=audit_ran,
        monitor_error=monitor_error,
    )


def send_monitor_test_notification(
    *,
    load_bot_config=_load_monitor_bot_config,
    send_bot_message=send_system_operator_bot_message,
) -> str:
    """Send only the fixed, clearly labelled monitor delivery test."""

    config = load_bot_config()
    if not system_operator_bot_enabled(config):
        raise RuntimeError("notification_config_missing")
    _run_maybe_awaitable(
        send_bot_message(config=config, text=MONITOR_TEST_NOTIFICATION_TEXT)
    )
    return "sent"


def _run_bounded_command(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int = _MAX_COMMAND_OUTPUT_BYTES,
    cwd: str | Path | None = None,
) -> _CommandResult:
    if (
        not isinstance(argv, (tuple, list))
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise ValueError("command argv must contain fixed nonempty strings")
    if timeout_seconds <= 0 or max_output_bytes < 0:
        raise ValueError("command bounds must be nonnegative")
    process = subprocess.Popen(
        tuple(argv),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
    )
    if process.stdout is None:
        process.kill()
        process.wait()
        raise RuntimeError("command_output_invalid")
    output = bytearray()
    deadline = time.monotonic() + timeout_seconds
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_and_wait(process)
                raise subprocess.TimeoutExpired(tuple(argv), timeout_seconds)
            events = selector.select(remaining)
            if not events:
                _kill_and_wait(process)
                raise subprocess.TimeoutExpired(tuple(argv), timeout_seconds)
            for key, _ in events:
                chunk = os.read(
                    key.fd,
                    min(65_536, max_output_bytes - len(output) + 1),
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > max_output_bytes:
                    _kill_and_wait(process)
                    raise RuntimeError("command_output_too_large")
        remaining = max(0.0, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        _kill_and_wait(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
    return _CommandResult(
        returncode=returncode,
        output=bytes(output).decode("utf-8", errors="strict"),
    )


def _kill_and_wait(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def _read_adapter(reader, name: str, failures: list[str], fallback):
    try:
        return reader()
    except Exception:
        failures.append(name)
        return fallback


def _audit_result_is_healthy(audit: Mapping[str, Any]) -> bool:
    reasons: set[str] = set()
    details: dict[str, Any] = {}
    _evaluate_audit(audit, reasons, details)
    return not reasons


def _run_maybe_awaitable(value: object) -> object:
    if inspect.isawaitable(value):
        import asyncio

        return asyncio.run(value)
    return value


def _require_aware_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value


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
    """Apply fingerprint change and reason-aware repeat-notification policy."""

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
        elif _uses_low_repeat_policy(result):
            should_notify = False
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


def _uses_low_repeat_policy(result: MonitorResult) -> bool:
    reason_codes = set(result.reason_codes)
    return bool(reason_codes) and reason_codes <= _LOW_REPEAT_REASON_CODES


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
    elif observed_head != expected_head:
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

    if type(snapshot.state_invalid) is not bool:
        reasons.add("malformed_snapshot")
    elif snapshot.state_invalid:
        reasons.add("state_invalid")

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


def _audit_safety_evidence_is_complete(audit: Mapping[str, Any]) -> bool:
    if (
        audit.get("output_complete") is True
        and audit.get("batches_truncated") is False
    ):
        return True
    if (
        audit.get("output_complete") is not False
        or audit.get("batches_truncated") is not True
    ):
        return False

    limit = _safe_count(audit.get("limit"))
    returned = _safe_count(audit.get("batches_returned"))
    counts = audit.get("counts")
    batches = audit.get("batches")
    if (
        limit is None
        or limit < 1
        or returned != limit
        or not isinstance(counts, Mapping)
        or not isinstance(batches, Sequence)
        or isinstance(batches, (str, bytes, bytearray))
        or len(batches) != returned
        or audit.get("all_history_legs_complete") is not True
    ):
        return False
    total = _safe_count(counts.get("batches_total"))
    if total is None or total <= returned:
        return False
    return all(
        isinstance(batch, Mapping) and batch.get("legs_truncated") is False
        for batch in batches
    )


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
    if not _audit_safety_evidence_is_complete(audit):
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
