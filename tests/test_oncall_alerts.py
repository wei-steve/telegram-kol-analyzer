"""Alert wording, de-duplication, throttling, delivery and token hygiene."""

from __future__ import annotations

import io
import logging
import urllib.error
from datetime import UTC, datetime, timedelta

import pytest

from oncall_test_support import NOW
from telegram_kol_research.oncall_alerts import (
    ALERT_KIND_CAP_REACHED,
    ALERT_KIND_CASE_OPEN,
    ALERT_KIND_DAILY_SUMMARY,
    ALERT_KIND_GROUP_MERGED,
    AlertDeliveryError,
    AlertPolicy,
    TelegramAlertSender,
    compose_case_alerts,
    deliver_pending_alerts,
    format_case_alert,
    maybe_compose_daily_summary,
    message_excerpt,
    reason_label,
)
from telegram_kol_research.oncall_state import OncallStateStore


BEIJING_0930 = datetime(2026, 9, 19, 1, 30, tzinfo=UTC)


@pytest.fixture
def store(tmp_path) -> OncallStateStore:
    with OncallStateStore(tmp_path / "state.db") as opened:
        yield opened


def open_case(store, *, chat_id=-100, key="mgmt:1:adjust_stop_loss", now=NOW, **evidence):
    payload = {
        "group_name": "龚有财群",
        "action": "adjust_stop_loss",
        "symbol": "ETH",
        "side": "short",
        "stop_loss_text": "2484",
        "message_text": "ETH 空单\n止损上移到 2484",
        "minutes_since_message": 3,
        "posted_at": "2026-09-19T05:52:00+00:00",
        "position_state": "verified_open",
        "group_open_positions": ["BTC long", "ETH short"],
    }
    payload.update(evidence)
    case, _created = store.upsert_case(
        case_key=key,
        rule="D1a",
        severity="high",
        now=now,
        raw_message_id=15660,
        chat_id=chat_id,
        reason_code="prior_partial_batch_unresolved",
        evidence=payload,
    )
    return case


# ------------------------------------------------------------------ text


def test_the_case_alert_is_plain_chinese_with_group_message_action_and_reason(store):
    case = open_case(store)

    text = format_case_alert(case)

    assert f"值守提醒 #{case.id}" in text
    assert "群：龚有财群" in text
    assert "消息 #15660" in text
    assert "13:52" in text  # Beijing time of the posted_at above
    assert "消息要求：调整止损（ETH 空） 止损→2484" in text
    assert "已过 3 分钟，交易所没有对应操作。" in text
    assert "卡在：上一笔部分平仓还没结清（prior_partial_batch_unresolved）" in text
    assert "仓位：仍在持仓中" in text


def test_the_message_excerpt_is_one_line_and_bounded_at_eighty_characters(store):
    case = open_case(store, message_text="第一行\n第二行\r\n" + "字" * 200)

    text = format_case_alert(case)
    quoted = text.split("原文：「", 1)[1].split("」", 1)[0]

    assert "\n" not in quoted
    assert len(quoted) == 81  # 80 characters plus the ellipsis
    assert quoted.endswith("…")


def test_an_untranslated_reason_is_shown_verbatim_and_flagged():
    assert reason_label("brand_new_reason_code") == "brand_new_reason_code（未收录原因）"


@pytest.mark.parametrize(
    "code,fragment",
    [
        ("stale_pending_voided_management", "系统停摆"),
        ("protection_authority_frozen:pos-99", "保护单权限被冻结"),
        ("confirmation_timeout", "等你确认"),
        ("target_strategy_binding_visibility_retry_expired", "持仓记录"),
        ("close_final_preflight_failed", "校验失败"),
        ("revision_replacement_incomplete", "没做完"),
        ("protection_recovery_bypassed_for_full_exit", "保护单恢复"),
        ("recovery_timeout", "超时"),
    ],
)
def test_every_reason_code_phase_zero_found_has_a_translation(code, fragment):
    label = reason_label(code)
    assert "未收录原因" not in label
    assert fragment in label


def test_an_uncertain_target_names_the_groups_open_positions(store):
    case, _ = store.upsert_case(
        case_key="mgmt:2:full_exit",
        rule="D1c",
        severity="high",
        now=NOW,
        raw_message_id=15661,
        chat_id=-100,
        reason_code="confirmation_timeout",
        target_uncertain=True,
        evidence={
            "group_name": "龚有财群",
            "action": "full_exit",
            "group_open_positions": ["BTC long", "ETH short"],
            "message_text": "先撤了",
            "minutes_since_message": 12,
        },
    )

    text = format_case_alert(case)

    assert "目标仓位未确定" in text
    assert "群内在仓：BTC long / ETH short" in text


def test_the_excerpt_helper_collapses_every_kind_of_whitespace():
    assert message_excerpt(" a\n b\tc \r\n") == "a b c"
    assert message_excerpt(None) == ""


# ------------------------------------------------------------ throttling


def test_one_case_produces_exactly_one_opening_alert_even_across_rounds(store):
    case = open_case(store)

    first = compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])
    second = compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])

    assert (first, second) == (1, 0)
    assert store.count_alerts_since(since=NOW - timedelta(days=1)) == 1


def test_a_recovery_is_only_announced_when_the_problem_was(store):
    silent = open_case(store, key="mgmt:9:full_exit")

    compose_case_alerts(store, now=NOW, new_case_ids=[], resolved_case_ids=[silent.id])

    assert store.pending_alerts() == ()


def test_the_fourth_case_in_one_group_inside_ten_minutes_is_merged(store):
    case_ids = [
        open_case(store, key=f"mgmt:{index}:full_exit").id for index in range(4)
    ]

    compose_case_alerts(store, now=NOW, new_case_ids=case_ids, resolved_case_ids=[])

    kinds = [alert.kind for alert in store.pending_alerts()]
    assert kinds.count(ALERT_KIND_CASE_OPEN) == 3
    assert kinds.count(ALERT_KIND_GROUP_MERGED) == 1
    merged = next(
        alert for alert in store.pending_alerts() if alert.kind == ALERT_KIND_GROUP_MERGED
    )
    assert "龚有财群 另有 1 条类似情况" in merged.body


def _stall_episode(store, *, now):
    case, created = store.upsert_case(
        case_key="health:D4_message_processing_stalled",
        rule="D4",
        severity="high",
        now=now,
        evidence={"stalled_jobs": 3, "minutes": 9, "oldest_raw_message_id": 7},
        reopen=True,
    )
    assert created
    return case


def test_a_health_rule_that_comes_back_inside_fifteen_minutes_stays_quiet(store):
    first = _stall_episode(store, now=NOW)
    assert compose_case_alerts(
        store, now=NOW, new_case_ids=[first.id], resolved_case_ids=[]
    ) == 1
    store.resolve_case(first.id, NOW)

    second = _stall_episode(store, now=NOW + timedelta(minutes=5))
    queued = compose_case_alerts(
        store,
        now=NOW + timedelta(minutes=5),
        new_case_ids=[second.id],
        resolved_case_ids=[],
    )

    assert queued == 0


def test_a_health_rule_that_comes_back_after_the_cooldown_alerts_again(store):
    first = _stall_episode(store, now=NOW)
    compose_case_alerts(store, now=NOW, new_case_ids=[first.id], resolved_case_ids=[])
    store.resolve_case(first.id, NOW)

    later = NOW + timedelta(minutes=20)
    second = _stall_episode(store, now=later)
    queued = compose_case_alerts(
        store, now=later, new_case_ids=[second.id], resolved_case_ids=[]
    )

    assert queued == 1


def test_the_daily_cap_stops_at_the_limit_and_resets_the_next_beijing_day(store):
    policy = AlertPolicy(daily_cap=2)
    case_ids = [open_case(store, key=f"mgmt:{index}:full_exit", chat_id=-index).id for index in range(4)]

    compose_case_alerts(
        store, now=NOW, new_case_ids=case_ids, resolved_case_ids=[], policy=policy
    )

    kinds = [alert.kind for alert in store.pending_alerts()]
    assert kinds.count(ALERT_KIND_CASE_OPEN) == 2
    assert kinds.count(ALERT_KIND_CAP_REACHED) == 1
    notice = next(
        alert for alert in store.pending_alerts() if alert.kind == ALERT_KIND_CAP_REACHED
    )
    # One notice, but it counts everything it is standing in for.
    assert "今日告警已达上限（2 条），其余 2 条" in notice.body

    tomorrow = NOW + timedelta(days=1)
    fresh = open_case(store, key="mgmt:99:full_exit", chat_id=-99, now=tomorrow)
    compose_case_alerts(
        store,
        now=tomorrow,
        new_case_ids=[fresh.id],
        resolved_case_ids=[],
        policy=policy,
    )

    assert any(
        alert.kind == ALERT_KIND_CASE_OPEN and alert.case_id == fresh.id
        for alert in store.pending_alerts()
    )


def test_the_daily_summary_is_sent_once_after_nine_in_beijing(store):
    counters = {"skipped_no_position": 7, "read_failed_rounds": 2}

    before = maybe_compose_daily_summary(
        store, now=BEIJING_0930 - timedelta(hours=2), counters=counters
    )
    first = maybe_compose_daily_summary(store, now=BEIJING_0930, counters=counters)
    second = maybe_compose_daily_summary(
        store, now=BEIJING_0930 + timedelta(hours=3), counters=counters
    )

    assert (before, first, second) == (False, True, False)
    summary = next(
        alert for alert in store.pending_alerts() if alert.kind == ALERT_KIND_DAILY_SUMMARY
    )
    assert "值守正常" in summary.body
    assert "因为没有真实仓位而忽略 7 条" in summary.body
    assert "读库失败 2 轮" in summary.body


def test_the_daily_summary_reports_the_delta_since_the_last_one(store):
    maybe_compose_daily_summary(
        store, now=BEIJING_0930, counters={"skipped_no_position": 7, "read_failed_rounds": 2}
    )
    maybe_compose_daily_summary(
        store,
        now=BEIJING_0930 + timedelta(days=1),
        counters={"skipped_no_position": 10, "read_failed_rounds": 2},
    )

    bodies = [alert.body for alert in store.pending_alerts()]
    assert any("忽略 3 条" in body for body in bodies)


# -------------------------------------------------------------- delivery


def test_dry_run_records_the_alert_and_calls_no_sender(store):
    case = open_case(store)
    compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])

    sent, failed = deliver_pending_alerts(store, now=NOW, sender=None)

    assert (sent, failed) == (0, 0)
    assert store.pending_alerts() == ()


def test_a_failed_send_is_retried_next_round_and_never_raises(store):
    case = open_case(store)
    compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])
    attempts: list[str] = []

    def flaky(text: str) -> None:
        attempts.append(text)
        if len(attempts) < 3:
            raise AlertDeliveryError("URLError")

    deliver_pending_alerts(store, now=NOW, sender=flaky)
    deliver_pending_alerts(store, now=NOW, sender=flaky)
    sent, _failed = deliver_pending_alerts(store, now=NOW, sender=flaky)

    assert len(attempts) == 3
    assert sent == 1
    assert store.pending_alerts() == ()


def test_an_alert_gives_up_after_five_attempts_without_taking_the_loop_down(store):
    case = open_case(store)
    compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])

    def always_fails(text: str) -> None:
        raise RuntimeError("boom")

    for _ in range(6):
        deliver_pending_alerts(store, now=NOW, sender=always_fails)

    assert store.pending_alerts() == ()
    row = store.connection.execute("SELECT status, attempts, delivery_error FROM alerts").fetchone()
    assert row["status"] == "failed"
    assert row["attempts"] == 5
    assert row["delivery_error"] == "RuntimeError"


def test_the_bot_token_never_reaches_the_log_the_state_database_or_an_exception(
    store, tmp_path
):
    token = "1234567:SUPERSECRETTOKENVALUE"
    case = open_case(store)
    compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])

    def sender(text: str) -> None:
        raise AlertDeliveryError(
            type(
                urllib.error.HTTPError(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    401,
                    "Unauthorized",
                    {},
                    None,
                )
            ).__name__
        )

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("telegram_kol_research.oncall_alerts")
    logger.addHandler(handler)
    try:
        deliver_pending_alerts(store, now=NOW, sender=sender)
    finally:
        logger.removeHandler(handler)

    assert token not in stream.getvalue()
    assert token not in (tmp_path / "state.db").read_bytes().decode("utf-8", "ignore")


def test_the_sender_turns_an_http_error_into_a_status_only_failure():
    sender = TelegramAlertSender(bot_token="secret-token", chat_id="42")

    def explode(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://api.telegram.org/botsecret-token/sendMessage",
            401,
            "Unauthorized",
            {},
            None,
        )

    import telegram_kol_research.oncall_alerts as alerts_module

    original = alerts_module.urllib.request.urlopen
    alerts_module.urllib.request.urlopen = explode
    try:
        with pytest.raises(AlertDeliveryError) as caught:
            sender.send("hello")
    finally:
        alerts_module.urllib.request.urlopen = original

    assert caught.value.error_type == "HTTP401"
    assert "secret-token" not in str(caught.value)
    assert "secret-token" not in repr(sender)
