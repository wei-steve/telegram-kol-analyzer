from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from telegram_kol_research.production_safety_monitor import MAX_ALERT_LENGTH
from telegram_kol_research.production_safety_monitor import MonitorExpectations
from telegram_kol_research.production_safety_monitor import MonitorResult
from telegram_kol_research.production_safety_monitor import MonitorSnapshot
from telegram_kol_research.production_safety_monitor import evaluate_monitor_snapshot
from telegram_kol_research.production_safety_monitor import format_monitor_alert


REVIEWED_HEAD = "3ec22ca77a1362167ce9d2cf702cfc50b1491967"
OTHER_HEAD = "a94c7b4cbb41331858be886bf341e79bd6bc2f4a"


EXPECTATIONS = MonitorExpectations(
    head=REVIEWED_HEAD,
    auto_trade_enabled=True,
    management_execution_mode="live",
    max_concurrent_positions=4,
)


def _snapshot(**overrides):
    values = {
        "service_state": "active",
        "head": REVIEWED_HEAD,
        "settings": {
            "auto_trade_enabled": True,
            "management_execution_mode": "live",
            "max_concurrent_positions": 4,
        },
        "journal_error_count": 0,
        "abnormal_events": (),
        "audit": None,
    }
    values.update(overrides)
    return MonitorSnapshot(**values)


def _healthy_audit(**overrides):
    values = {
        "snapshot_status": "stable",
        "snapshot_validation": "ok",
        "schema_status": "available",
        "output_complete": True,
        "batches_truncated": False,
        "counts": {
            "blocked": 0,
            "partial_failed": 0,
            "submit_unknown": 0,
            "recovery_required": 0,
        },
        "malformed_row_count": 0,
        "malformed_field_count": 0,
        "legacy_pending_management": {
            "complete": True,
            "truncated": False,
            "scan_truncated": False,
        },
    }
    values.update(overrides)
    return values


def test_exact_healthy_snapshot_has_no_reasons():
    result = evaluate_monitor_snapshot(_snapshot(), EXPECTATIONS)

    assert result.healthy is True
    assert result.reason_codes == ()
    assert result.details == {}


def test_monitor_inputs_and_result_are_frozen():
    result = evaluate_monitor_snapshot(_snapshot(), EXPECTATIONS)

    with pytest.raises(FrozenInstanceError):
        EXPECTATIONS.head = "other"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.healthy = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("snapshot", "expected_reasons"),
    [
        (_snapshot(service_state="inactive"), ("service_inactive",)),
        (_snapshot(head=OTHER_HEAD), ("head_drift",)),
        (
            _snapshot(
                settings={
                    "auto_trade_enabled": False,
                    "management_execution_mode": "shadow",
                    "max_concurrent_positions": 8,
                }
            ),
            (
                "auto_trade_enabled_drift",
                "management_execution_mode_drift",
                "max_concurrent_positions_drift",
            ),
        ),
        (_snapshot(journal_error_count=2), ("journal_errors",)),
        (
            _snapshot(adapter_failures=("settings",)),
            ("adapter_failure",),
        ),
    ],
)
def test_system_drift_and_adapter_failure_alert(snapshot, expected_reasons):
    result = evaluate_monitor_snapshot(snapshot, EXPECTATIONS)

    assert result.healthy is False
    assert result.reason_codes == tuple(sorted(expected_reasons))


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("unknown_exchange_outcome", "event_unknown_status"),
        ("submit_unknown", "event_unknown_status"),
        ("recovery_required", "event_recovery_status"),
    ],
)
def test_unknown_and_recovery_event_statuses_alert(status, reason):
    result = evaluate_monitor_snapshot(
        _snapshot(abnormal_events=({"action": "open_market_position", "status": status},)),
        EXPECTATIONS,
    )

    assert result.reason_codes == (reason,)


@pytest.mark.parametrize(
    "event",
    [
        {"action": "submit_entry_order", "status": "submitted", "pos_id": "p-1"},
        {"action": "set_position_tpsl", "status": "submitted", "pos_id": "p-1"},
        {"action": "adjust_position_tpsl", "status": "submitted", "pos_id": "p-1"},
        {
            "action": "close_bound_position_market",
            "status": "submitted",
            "pos_id": "p-1",
        },
    ],
)
def test_normal_submitted_trade_events_do_not_alert(event):
    result = evaluate_monitor_snapshot(
        _snapshot(abnormal_events=(event,)), EXPECTATIONS
    )

    assert result.healthy is True
    assert result.reason_codes == ()


def test_real_manual_close_reservation_and_submission_do_not_alert():
    result = evaluate_monitor_snapshot(
        _snapshot(
            abnormal_events=(
                {
                    "action": "close_bound_position_reservation",
                    "status": "reserved",
                    "pos_id": "1001124101591781",
                },
                {
                    "action": "close_bound_position_reservation",
                    "status": "submitted",
                    "pos_id": "1001124101591781",
                },
                {
                    "action": "close_bound_position_market",
                    "status": "submitted",
                    "pos_id": "1001124101591781",
                },
            )
        ),
        EXPECTATIONS,
    )

    assert result.healthy is True
    assert result.reason_codes == ()


def test_reserved_is_not_globally_normal_for_other_actions():
    result = evaluate_monitor_snapshot(
        _snapshot(
            abnormal_events=(
                {"action": "submit_entry_order", "status": "reserved", "pos_id": "p-1"},
            )
        ),
        EXPECTATIONS,
    )

    assert result.reason_codes == ("event_unknown_status",)


def test_only_duplicate_exact_manual_close_for_same_pos_id_alerts():
    duplicate = {"action": "close_bound_position_market", "status": "submitted", "pos_id": "p-1"}
    result = evaluate_monitor_snapshot(
        _snapshot(
            abnormal_events=(
                duplicate,
                dict(duplicate),
                {**duplicate, "pos_id": "p-2"},
                {**duplicate, "action": "close_position_market"},
            )
        ),
        EXPECTATIONS,
    )

    assert result.reason_codes == ("duplicate_manual_close",)
    assert result.details["duplicate_manual_close_count"] == 1


@pytest.mark.parametrize(
    "audit",
    [
        _healthy_audit(output_complete=False),
        _healthy_audit(snapshot_status="snapshot_unstable"),
        _healthy_audit(
            legacy_pending_management={
                "complete": False,
                "truncated": True,
                "scan_truncated": True,
            }
        ),
    ],
)
def test_incomplete_audit_alerts(audit):
    result = evaluate_monitor_snapshot(_snapshot(audit=audit), EXPECTATIONS)

    assert "audit_incomplete" in result.reason_codes


@pytest.mark.parametrize(
    "audit",
    [
        _healthy_audit(
            counts={
                "blocked": 1,
                "partial_failed": 0,
                "submit_unknown": 0,
                "recovery_required": 0,
            }
        ),
        _healthy_audit(malformed_row_count=1),
        _healthy_audit(schema_status="management_schema_missing"),
    ],
)
def test_abnormal_audit_alerts(audit):
    result = evaluate_monitor_snapshot(_snapshot(audit=audit), EXPECTATIONS)

    assert "audit_abnormal" in result.reason_codes


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("settings", {"auto_trade_enabled": "definitely"}),
        ("journal_error_count", "not-an-int"),
        ("abnormal_events", ({"action": None, "status": {"raw": "boom"}},)),
        ("audit", {"exception": "raw adapter traceback"}),
    ],
)
def test_malformed_inputs_use_fixed_reason_without_raw_values(field, value):
    result = evaluate_monitor_snapshot(_snapshot(**{field: value}), EXPECTATIONS)

    assert "malformed_snapshot" in result.reason_codes
    rendered = repr(result)
    assert "definitely" not in rendered
    assert "not-an-int" not in rendered
    assert "raw adapter traceback" not in rendered
    assert "boom" not in rendered


def test_formatter_uses_fixed_chinese_labels_and_sorted_reason_codes():
    result = evaluate_monitor_snapshot(
        _snapshot(service_state="inactive", head=OTHER_HEAD, journal_error_count=2),
        EXPECTATIONS,
    )

    text = format_monitor_alert(
        result,
        checked_at=datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
    )

    assert text.startswith("【生产安全监控异常】")
    assert "检查时间：2026-07-16T09:30:00+00:00" in text
    assert "异常代码：head_drift,journal_errors,service_inactive" in text
    assert "服务状态：inactive" in text
    assert f"当前版本：{OTHER_HEAD[:12]}" in text
    assert "日志错误数：2" in text


def test_formatter_is_bounded_and_never_emits_sensitive_or_raw_fields():
    secrets = (
        "bot-token-secret",
        "passphrase-secret",
        "DC-ACCESS-KEY",
        "raw-request-secret",
        "raw-response-secret",
        "raw-exception-secret",
    )
    result = evaluate_monitor_snapshot(
        _snapshot(
            service_state="inactive\n" + secrets[0] * 500,
            head=secrets[1] * 500,
            settings={
                "auto_trade_enabled": False,
                "management_execution_mode": secrets[2] * 500,
                "max_concurrent_positions": 999,
                "token": secrets[0],
                "passphrase": secrets[1],
                "headers": secrets[2],
            },
            abnormal_events=(
                {
                    "action": "open_market_position",
                    "status": "submit_unknown",
                    "request": secrets[3],
                    "response": secrets[4],
                    "exception": secrets[5],
                },
            ),
            adapter_failures=(secrets[5],),
        ),
        EXPECTATIONS,
    )

    text = format_monitor_alert(result, checked_at="2026-07-16T09:30:00+00:00")

    assert len(text) <= MAX_ALERT_LENGTH
    assert all(secret not in text for secret in secrets)


def test_realistic_short_bot_token_is_rejected_from_every_string_detail():
    bot_token = "1234567890:AAEabcDEF_ghIJK-lmnOPQrstUVWxyz12345"
    result = evaluate_monitor_snapshot(
        _snapshot(
            service_state=bot_token,
            head=bot_token,
            settings={
                "auto_trade_enabled": False,
                "management_execution_mode": bot_token,
                "max_concurrent_positions": 8,
            },
            adapter_failures=(bot_token,),
        ),
        MonitorExpectations(
            head=bot_token,
            auto_trade_enabled=True,
            management_execution_mode=bot_token,
            max_concurrent_positions=4,
        ),
    )

    text = format_monitor_alert(result, checked_at=bot_token)

    assert "malformed_snapshot" in result.reason_codes
    assert bot_token not in repr(result)
    assert bot_token not in text


def test_formatter_revalidates_each_string_detail_field():
    bot_token = "1234567890:AAEabcDEF_ghIJK-lmnOPQrstUVWxyz12345"
    result = MonitorResult(
        healthy=False,
        reason_codes=("malformed_snapshot",),
        details={
            "service_state": bot_token,
            "head": bot_token,
            "expected_head": bot_token,
            "management_execution_mode": bot_token,
            "expected_management_execution_mode": bot_token,
            "adapter_failures": (bot_token,),
        },
    )

    text = format_monitor_alert(result, checked_at=bot_token)

    assert bot_token not in text


def test_oversized_integer_is_malformed_and_formatter_never_crashes():
    oversized = 10**5000
    result = evaluate_monitor_snapshot(
        _snapshot(journal_error_count=oversized),
        EXPECTATIONS,
    )

    assert result.reason_codes == ("malformed_snapshot",)
    assert oversized not in result.details.values()

    injected = MonitorResult(
        healthy=False,
        reason_codes=("malformed_snapshot",),
        details={"journal_error_count": oversized},
    )
    text = format_monitor_alert(injected, checked_at="2026-07-16T09:30:00+00:00")

    assert "日志错误数：invalid" in text
    assert len(text) <= MAX_ALERT_LENGTH
