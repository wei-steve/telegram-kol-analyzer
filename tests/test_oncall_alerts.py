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
    ALERT_KIND_CASE_RESOLVED,
    ALERT_KIND_CODEX_DOWN,
    ALERT_KIND_DAILY_SUMMARY,
    ALERT_KIND_DIAGNOSIS,
    ALERT_KIND_GROUP_MERGED,
    ALERT_KIND_HEALTH_OPEN,
    ALERT_KIND_REMEDIATION_REQUEST_FAILED,
    AlertDeliveryError,
    AlertPolicy,
    TelegramAlertSender,
    alert_wants_notify_bot,
    compose_case_alerts,
    deliver_pending_alerts,
    format_case_alert,
    format_recognition_case_alert,
    format_sealed_lane_alert,
    format_unheard_incident_alert,
    format_voided_message_alert,
    maybe_compose_daily_summary,
    message_excerpt,
    reason_label,
)
from telegram_kol_research.oncall_state import AlertRecord, OncallStateStore


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


def open_recognition_case(store, *, now=NOW, reason="mimo_authoritative_failed", **evidence):
    payload = {
        "group_name": "龚有财群",
        "message_text": "ETH 这波先减一半\n止损拉到成本",
        "posted_at": "2026-09-19T05:52:00+00:00",
        "minutes_since_message": 8,
        "position_state": "verified_open",
        "group_open_positions": ["ETH short"],
    }
    payload.update(evidence)
    case, _created = store.upsert_case(
        case_key="recog:15660",
        rule="D3",
        severity="high",
        now=now,
        raw_message_id=15660,
        chat_id=-100,
        reason_code=reason,
        evidence=payload,
    )
    return case


def test_the_recognition_case_alert_says_what_was_lost_in_plain_chinese(store):
    case = open_recognition_case(store)

    text = format_recognition_case_alert(case)

    assert text.splitlines()[0] == f"⚠️ 值守提醒 #{case.id}"
    assert "群：龚有财群    消息 #15660（13:52）" in text
    assert "识别结果：识别失败（权威模型调用失败，这条消息没有被识别）" in text
    assert "原文：「ETH 这波先减一半 止损拉到成本」" in text
    assert "现状：群内有持仓（ETH 空），这条消息没有被自动处理。" in text
    # It is a recognition failure, not an instruction that did not execute.
    assert "消息要求" not in text
    assert "卡在" not in text


def test_a_recognition_case_is_composed_with_its_own_wording_not_the_d1_template(store):
    case = open_recognition_case(store)

    compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])

    body = store.pending_alerts()[0].body
    assert "识别结果：识别失败" in body
    assert "消息要求" not in body


def test_a_recovered_recognition_case_says_the_message_was_recognised_later(store):
    case = open_recognition_case(store)
    compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])

    compose_case_alerts(
        store,
        now=NOW + timedelta(minutes=5),
        new_case_ids=[],
        resolved_case_ids=[case.id],
    )

    bodies = [alert.body for alert in store.pending_alerts()]
    assert any("后来被正常识别" in body for body in bodies)


@pytest.mark.parametrize(
    "code,fragment",
    [
        ("mimo_authoritative_failed", "权威模型调用失败"),
        ("mimo_authoritative_failed_exhausted", "重试已耗尽"),
        ("authoritative_gap_recovery_expired", "补识别窗口"),
        ("authoritative_failed", "识别失败"),
        ("context_resolution_failed", "上下文解析失败"),
        ("management_recognition_unresolved", "上下文解析失败"),
    ],
)
def test_every_new_recognition_reason_code_has_a_chinese_label(code, fragment):
    label = reason_label(code)
    assert fragment in label
    assert "未收录原因" not in label


# ------------------------------------------------------- D6a / D6b / D6c


def open_sealed_lane_case(
    store,
    *,
    now=NOW,
    exit_id=310,
    reason_code="source_deletion_exit_sealed_lane",
    **evidence,
):
    payload = {
        "kind": "sealed_lane",
        "group_name": "龚有财群",
        "exit_id": exit_id,
        "exit_state": "recovery_required",
        "stall_class": "unclaimable",
        "exit_last_reason": "exit_has_no_known_position",
        "symbol": "BTC",
        "side": "long",
        "minutes_sealed": 11 * 24 * 60 + 3 * 60,
        "voided_messages": 11,
        "voided_scan_examined": 200,
    }
    payload.update(evidence)
    case, _created = store.upsert_case(
        case_key=f"lane:{exit_id}",
        rule="D6a",
        severity="high",
        now=now,
        raw_message_id=15660,
        chat_id=-100,
        reason_code=reason_code,
        evidence=payload,
    )
    return case


def open_stalled_lane_case(store, *, exit_state="cancelling_entries", **evidence):
    """The other half of D6a: an exit the worker still holds and is not finishing."""

    return open_sealed_lane_case(
        store,
        exit_id=evidence.pop("exit_id", 311),
        reason_code="source_deletion_exit_stalled_lane",
        exit_state=exit_state,
        stall_class="active",
        **evidence,
    )


def test_the_sealed_lane_alert_names_the_group_the_direction_and_the_losses(store):
    case = open_sealed_lane_case(store)

    text = format_sealed_lane_alert(case)

    assert "群：龚有财群    被封的方向：BTC 多" in text
    assert "封了多久：11 天 3 小时" in text
    assert "删除退出：#310" in text
    assert "exit_has_no_known_position" in text
    assert "期间已有 11 条消息被作废" in text
    assert "只数了封锁之后这个群的 200 条消息" in text
    assert "这个方向的新策略现在一条都进不来" in text
    # The state reads in Chinese, and keeps the raw word an engineer greps for.
    assert "需要人工恢复处理（recovery_required）" in text


def test_the_unclaimable_lane_alert_says_no_automation_will_pick_it_up(store):
    """``recovery_required``: the worker never claims the exit again."""

    case = open_sealed_lane_case(store)

    text = format_sealed_lane_alert(case)

    assert "系统不会再认领" in text
    assert "只有超时清扫或人工能动它" in text
    assert "在原地打转" not in text
    assert "卡死" in reason_label(case.reason_code)


def test_the_active_stall_alert_says_a_step_is_going_round_in_circles(store):
    """An active state: a claim is holding the row and never finishing."""

    case = open_stalled_lane_case(store, exit_last_reason="cancel_entry_retry")

    text = format_sealed_lane_alert(case)

    assert "本该几秒钟走完" in text
    assert "在原地打转" in text
    assert "正在撤销原策略入场单（cancelling_entries）" in text
    # And it says what will *not* happen, which is what decides whether anybody
    # has to act: no sweep covers an active state.
    assert "超时清扫只管「需要人工恢复处理」的退出" in text
    assert "系统不会再认领" not in text
    assert "卡在处理中途" in reason_label(case.reason_code)


def test_the_two_stall_classes_do_not_read_the_same(store):
    """The whole point of the split: two causes must not print one sentence."""

    unclaimable = format_sealed_lane_alert(open_sealed_lane_case(store))
    active = format_sealed_lane_alert(open_stalled_lane_case(store))

    assert unclaimable != active
    # Both still name the same loss, in the same words, and both are urgent.
    for text in (unclaimable, active):
        assert "这个方向的新策略现在一条都进不来" in text
        assert "群：龚有财群    被封的方向：BTC 多" in text
        assert "封了多久：11 天 3 小时" in text


def test_an_unrecorded_or_unknown_exit_state_is_shown_and_flagged(store):
    from telegram_kol_research.oncall_alerts import deletion_exit_state_label

    assert deletion_exit_state_label(None) == "状态未记录"
    assert deletion_exit_state_label("") == "状态未记录"
    assert deletion_exit_state_label("teleported") == "teleported（未收录状态）"
    assert deletion_exit_state_label("pending") == "排队等处理（pending）"
    # A case whose evidence carries no stall class at all still reads: anything
    # that is not the active class tells the ``recovery_required`` story, which
    # is the one every case filed before this change was.
    case = open_sealed_lane_case(store)
    case.evidence.pop("stall_class")
    assert "系统不会再认领" in format_sealed_lane_alert(case)


def test_every_deletion_exit_state_the_watcher_can_see_has_a_label():
    """Including the two the operator bot's own report never has to name."""

    from telegram_kol_research.oncall_alerts import DELETION_EXIT_STATE_LABELS
    from telegram_kol_research.oncall_detector import (
        SEALED_LANE_RELEASED_STATE,
        SEALED_LANE_SEALING_STATES,
    )

    for state in (*SEALED_LANE_SEALING_STATES, SEALED_LANE_RELEASED_STATE, "unbound"):
        assert state in DELETION_EXIT_STATE_LABELS, state


def test_the_sealed_lane_alert_says_so_when_nothing_has_been_voided_yet(store):
    case = open_sealed_lane_case(store, voided_messages=0)

    text = format_sealed_lane_alert(case)

    assert "期间还没有消息因此被作废。" in text
    assert "条消息被作废，永不执行" not in text


def test_a_sealed_lane_is_composed_with_its_own_wording(store):
    case = open_sealed_lane_case(store)

    compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])

    body = store.pending_alerts()[0].body
    assert "这条线被封住了" in body
    assert "消息要求" not in body


def test_a_released_lane_says_the_direction_is_open_again(store):
    case = open_sealed_lane_case(store)
    compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])

    compose_case_alerts(
        store,
        now=NOW + timedelta(minutes=5),
        new_case_ids=[],
        resolved_case_ids=[case.id],
    )

    bodies = [alert.body for alert in store.pending_alerts()]
    assert any("已解封" in body and "又能进新策略" in body for body in bodies)


def open_voided_message_case(store, *, now=NOW, **evidence):
    payload = {
        "kind": "voided_message",
        "group_name": "龚有财群",
        "symbol": "BTC",
        "side": "long",
        "message_text": "BTC 83000-83300 进多\n止损 80000",
        "posted_at": "2026-09-19T05:52:00+00:00",
        "minutes_since_message": 40,
        "blocking_exit_id": 310,
        "blocking_exit_state": "recovery_required",
    }
    payload.update(evidence)
    case, _created = store.upsert_case(
        case_key="voided:15660",
        rule="D6b",
        severity="high",
        now=now,
        raw_message_id=15660,
        chat_id=-100,
        reason_code="deferred_expired",
        evidence=payload,
    )
    return case


def test_the_voided_message_alert_quotes_what_was_thrown_away(store):
    case = open_voided_message_case(store)

    text = format_voided_message_alert(case)

    assert "消息被系统作废" in text
    assert "群：龚有财群    消息 #15660（13:52）" in text
    assert "原文：「BTC 83000-83300 进多 止损 80000」" in text
    assert "系统把这条消息作废了" in text
    assert "挡住它的是删除退出 #310" in text
    assert "这条 BTC 多 的消息不会被执行" in text


def test_the_voided_message_alert_works_when_the_exit_cannot_be_named(store):
    case = open_voided_message_case(
        store, blocking_exit_id=None, blocking_exit_state=None
    )

    text = format_voided_message_alert(case)

    assert "挡住它的是" not in text
    assert "不会被执行" in text


def test_a_voided_message_is_composed_with_its_own_wording(store):
    case = open_voided_message_case(store)

    compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])

    body = store.pending_alerts()[0].body
    assert "消息被系统作废" in body
    assert "识别结果" not in body


def open_unheard_incident_case(
    store, *, now=NOW, reason=None, case_key="unheard_type:source_deletion_exit_stuck", **evidence
):
    """2026-09-29: default case key is the post-fix, per-type shape.

    ``incident_ids``/``incident_count`` is what :func:`_unheard_incident_observations`
    writes now; the formatter also falls back to a lone ``incident_id`` so a
    case still open under the pre-2026-09-29 per-id key formats identically --
    see :func:`test_the_pre_fix_unheard_case_key_still_formats`.
    """

    payload = {
        "kind": "unheard_incident_type",
        "incident_ids": [1841],
        "incident_count": 1,
        "incident_type": "source_deletion_exit_stuck",
        "incident_severity": "high",
        "source_kind": "source_deletion_exit",
        "source_record_id": "310",
        "repeat_count": 356933,
        "last_occurred_at": "2026-09-19T05:58:00+00:00",
        "notified_at": None,
        "minutes_since_last_occurrence": 2,
        "minutes_since_notified": None,
        "summary": "删除退出仍未释放：交易所缺席证明拒绝。",
    }
    payload.update(evidence)
    case, _created = store.upsert_case(
        case_key=case_key,
        rule="D6c",
        severity="high",
        now=now,
        reason_code=reason or "runtime_incident_never_notified",
        evidence=payload,
    )
    return case


def test_the_unheard_incident_alert_says_it_is_happening_now_and_nobody_knows(store):
    case = open_unheard_incident_case(store)

    text = format_unheard_incident_alert(case)

    assert "告警在喊，没人听见" in text
    assert "source_deletion_exit_stuck" in text
    assert "记录 #1841" in text
    assert "对象：source_deletion_exit 310" in text
    assert "仍在发生：2 分钟前还在报，累计 356933 次。" in text
    assert "上次通知：从来没有通知过。" in text
    assert "系统的说法：「删除退出仍未释放：交易所缺席证明拒绝。」" in text


def test_the_unheard_incident_alert_dates_a_stale_notification(store):
    case = open_unheard_incident_case(
        store,
        reason="runtime_incident_notification_stale",
        notified_at="2026-09-08T06:00:00+00:00",
        minutes_since_notified=11 * 24 * 60,
    )

    text = format_unheard_incident_alert(case)

    assert "上次通知：11 天 0 小时前（2026-09-08）。" in text


def test_an_unheard_incident_is_composed_and_recovers_with_its_own_wording(store):
    case = open_unheard_incident_case(store)
    compose_case_alerts(store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[])
    assert "告警在喊，没人听见" in store.pending_alerts()[0].body

    compose_case_alerts(
        store,
        now=NOW + timedelta(minutes=5),
        new_case_ids=[],
        resolved_case_ids=[case.id],
    )

    bodies = [alert.body for alert in store.pending_alerts()]
    assert any("已经不再发生，或者已经重新通知过了" in body for body in bodies)


def test_the_pre_fix_unheard_case_key_still_formats(store):
    """A case still open under the pre-2026-09-29 per-id key opens normally."""

    case = open_unheard_incident_case(
        store,
        case_key="unheard:1841",
        incident_ids=None,
        incident_id=1841,
    )
    text = format_unheard_incident_alert(case)
    assert "记录 #1841" in text


def test_a_legacy_keyed_case_resolves_silently(store):
    """2026-09-29 (event-bot quality design 1.2 A.1.3): a case still open under
    the retired per-id key at deploy time (e.g. production's cases 35/36, both
    quiet-by-design types) must still be resolved by the state machine, but
    its "ended" message is noise -- the object should never have opened a case
    to begin with -- so it is never sent.
    """

    case = open_unheard_incident_case(store, case_key="unheard:2422")
    store.mark_case_alerted(case.id, NOW)  # it *was* announced when it opened

    queued = compose_case_alerts(
        store, now=NOW, new_case_ids=[], resolved_case_ids=[case.id]
    )

    assert queued == 0
    assert store.pending_alerts() == ()


@pytest.mark.parametrize(
    "code,fragment",
    [
        ("source_deletion_exit_sealed_lane", "卡死（系统不会再认领）"),
        ("source_deletion_exit_stalled_lane", "卡在处理中途不动了"),
        ("deferred_expired", "系统把这条消息作废了"),
        ("waiting_source_deletion_exit", "暂时被挡下"),
        ("runtime_incident_never_notified", "从来没有通知过"),
        ("runtime_incident_notification_stale", "上次通知已经很久以前"),
    ],
)
def test_every_new_d6_reason_code_has_a_chinese_label(code, fragment):
    label = reason_label(code)
    assert fragment in label
    assert "未收录原因" not in label


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
    assert "群内在仓：BTC 多 / ETH 空" in text


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


def open_capped_case(store, *, key, chat_id, now=NOW):
    """A case the daily cap still applies to: medium severity (D1d)."""

    case, _created = store.upsert_case(
        case_key=key,
        rule="D1d",
        severity="medium",
        now=now,
        raw_message_id=15660,
        chat_id=chat_id,
        reason_code="instruction_stuck_submitted",
        evidence={"group_name": "龚有财群", "action": "full_exit"},
    )
    return case


def test_the_daily_cap_stops_at_the_limit_and_resets_the_next_beijing_day(store):
    # Medium-severity openings: since the 2026-09-28 audit fix, a high-severity
    # opening about a message is exempt from the cap (tests below).
    policy = AlertPolicy(daily_cap=2)
    case_ids = [
        open_capped_case(store, key=f"mgmt:{index}:full_exit", chat_id=-index).id
        for index in range(4)
    ]

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
    fresh = open_capped_case(store, key="mgmt:99:full_exit", chat_id=-99, now=tomorrow)
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


# ----------------------------------------- 2026-09-28 audit fixes, problem 3b
# Production 2026-09-28: the cap of 30 was spent by 13:25 Beijing, mostly on
# "resolved" and diagnosis follow-ups, and every case opened afterwards -- a D3
# in an auto_trade group, two D6c "never notified" incidents -- was silenced.


def _fill_cap_with_ordinary_openings(store, *, count=30):
    case_ids = [
        open_capped_case(store, key=f"mgmt:{9000 + index}:full_exit", chat_id=-9000 - index).id
        for index in range(count)
    ]
    queued = compose_case_alerts(
        store, now=NOW, new_case_ids=case_ids, resolved_case_ids=[]
    )
    assert queued == count
    return case_ids


def _daily_count(store):
    from telegram_kol_research.oncall_alerts import beijing_date

    return store.get_int_meta(f"daily_alerts:{beijing_date(NOW)}", 0)


def test_r3b_high_severity_message_and_d6c_openings_outrun_a_spent_cap(store):
    _fill_cap_with_ordinary_openings(store)
    assert _daily_count(store) == 30

    d3 = open_recognition_case(store)
    d6c = open_unheard_incident_case(store)
    ordinary = open_capped_case(store, key="mgmt:9999:full_exit", chat_id=-9999)
    health = _stall_episode(store, now=NOW)

    queued = compose_case_alerts(
        store,
        now=NOW,
        new_case_ids=[d3.id, d6c.id, ordinary.id, health.id],
        resolved_case_ids=[],
    )

    assert queued == 2
    opened = {
        alert.case_id
        for alert in store.pending_alerts(limit=100)
        if alert.kind == ALERT_KIND_CASE_OPEN
    }
    assert {d3.id, d6c.id} <= opened
    assert store.get_case(d3.id).alerted_at is not None
    assert store.get_case(d6c.id).alerted_at is not None
    # Everything else keeps the cap.
    assert ordinary.id not in opened
    assert store.get_case(ordinary.id).alerted_at is None
    assert store.get_case(health.id).alerted_at is None
    # The exempt openings still count; the cap notice stays as it was.
    assert _daily_count(store) == 32
    notice = next(
        alert
        for alert in store.pending_alerts(limit=100)
        if alert.kind == ALERT_KIND_CAP_REACHED
    )
    assert "今日告警已达上限（30 条），其余 2 条" in notice.body


def test_r3b_a_merged_d6c_rule_list_still_counts_as_d6c(store):
    _fill_cap_with_ordinary_openings(store)
    case, _created = store.upsert_case(
        case_key="unheard:77",
        rule="D5b",
        severity="high",
        now=NOW,
        reason_code="runtime_incident_never_notified",
        evidence={"kind": "unheard_incident", "rules": ["D5b", "D6c"]},
    )

    assert compose_case_alerts(
        store, now=NOW, new_case_ids=[case.id], resolved_case_ids=[]
    ) == 1


def test_r1d_a_capped_case_is_backfilled_after_the_beijing_day_resets(store):
    """R1-d (event-bot quality design 1.2 A.2, case 32 / 2026-09-28): a case
    capped away stays ``alerted_at IS NULL`` forever before the fix -- nothing
    ever revisits it once the round that opened it has passed. After the fix,
    the next round past the cap reset backfills it, prefixed, with its
    already-finished diagnosis right behind it.
    """
    from telegram_kol_research.oncall_alerts import compose_backfill_alerts

    _fill_cap_with_ordinary_openings(store)
    capped = open_capped_case(store, key="mgmt:19598:move_stop_to_break_even", chat_id=-19598)
    queued = compose_case_alerts(
        store, now=NOW, new_case_ids=[capped.id], resolved_case_ids=[]
    )
    assert queued == 0  # capped away, exactly like case 32
    assert store.get_case(capped.id).alerted_at is None

    # Before the fix: nothing ever revisits a capped case.
    before_fix = compose_case_alerts(
        store, now=NOW + timedelta(hours=11), new_case_ids=[], resolved_case_ids=[]
    )
    assert before_fix == 0
    assert store.get_case(capped.id).alerted_at is None
    assert store.get_case(capped.id).status != "resolved"

    # A diagnosis that finished while the opening alert was still capped.
    store.record_diagnosis_request(
        case_id=capped.id, attempt=1, fingerprint="fp", prompt_version="v1", now=NOW
    )
    store.record_diagnosis_result(
        case_id=capped.id,
        status="done",
        now=NOW,
        verdict={
            "urgency": "immediate",
            "category": "missed_execution",
            "what_message_wanted_zh": "把 BTC 多单止损移到保本",
            "explanation_zh": "预检拒绝，从未接触交易所",
            "recommended_action_zh": "到交易所核对仓位止损",
            "should_have_executed": "yes",
            "confidence": "high",
        },
    )

    # Past midnight Beijing (NOW is 2026-09-19 06:00Z == 14:00 Beijing).
    next_day = NOW + timedelta(hours=11)
    queued = compose_backfill_alerts(store, now=next_day)

    assert queued == 1
    case = store.get_case(capped.id)
    assert case.alerted_at is not None
    bodies = [alert.body for alert in store.pending_alerts(limit=100)]
    opening = next(body for body in bodies if body.startswith("（补发：原"))
    assert "时因当日告警上限未发出）" in opening
    assert "值守提醒" in opening
    assert any("🔎 值守诊断" in body for body in bodies)


def test_r1d_resolved_and_stale_cases_are_never_backfilled(store):
    from telegram_kol_research.oncall_alerts import compose_backfill_alerts

    _fill_cap_with_ordinary_openings(store)
    capped = open_capped_case(store, key="mgmt:1:full_exit", chat_id=-1)
    compose_case_alerts(store, now=NOW, new_case_ids=[capped.id], resolved_case_ids=[])
    assert store.get_case(capped.id).alerted_at is None
    store.resolve_case(capped.id, NOW)

    queued = compose_backfill_alerts(store, now=NOW + timedelta(hours=11))

    assert queued == 0
    assert not any(
        alert.case_id == capped.id for alert in store.pending_alerts(limit=100)
    )


def test_r3c_follow_ups_neither_wait_on_nor_spend_the_cap(store):
    from telegram_kol_research.oncall_alerts import (
        ALERT_KIND_CASE_RESOLVED,
        ALERT_KIND_DIAGNOSIS,
        compose_diagnosis_alert,
    )

    policy = AlertPolicy(daily_cap=3)
    first = open_capped_case(store, key="mgmt:1:full_exit", chat_id=-1)
    compose_case_alerts(
        store, now=NOW, new_case_ids=[first.id], resolved_case_ids=[], policy=policy
    )
    verdict = {
        "urgency": "low",
        "category": "transient_failure",
        "should_have_executed": "yes",
        "confidence": "high",
        "what_message_wanted_zh": "平仓",
        "explanation_zh": "暂时失败",
        "recommended_action_zh": "观察",
    }
    assert compose_diagnosis_alert(
        store, case=store.get_case(first.id), verdict=verdict, now=NOW, policy=policy
    )
    store.resolve_case(first.id, NOW)
    assert compose_case_alerts(
        store, now=NOW, new_case_ids=[], resolved_case_ids=[first.id], policy=policy
    ) == 1
    # Opening + diagnosis + resolved: only the opening was counted.
    assert _daily_count(store) == 1

    second = open_capped_case(store, key="mgmt:2:full_exit", chat_id=-2)
    third = open_capped_case(store, key="mgmt:3:full_exit", chat_id=-3)
    fourth = open_capped_case(store, key="mgmt:4:full_exit", chat_id=-4)
    queued = compose_case_alerts(
        store,
        now=NOW,
        new_case_ids=[second.id, third.id, fourth.id],
        resolved_case_ids=[],
        policy=policy,
    )
    assert queued == 2
    assert _daily_count(store) == 3
    assert store.get_case(fourth.id).alerted_at is None

    # With the cap spent, follow-ups for already-announced cases still go out.
    assert compose_diagnosis_alert(
        store, case=store.get_case(second.id), verdict=verdict, now=NOW, policy=policy
    )
    store.resolve_case(second.id, NOW)
    assert compose_case_alerts(
        store, now=NOW, new_case_ids=[], resolved_case_ids=[second.id], policy=policy
    ) == 1
    assert _daily_count(store) == 3
    kinds = [
        (alert.kind, alert.case_id) for alert in store.pending_alerts(limit=100)
    ]
    assert (ALERT_KIND_DIAGNOSIS, second.id) in kinds
    assert (ALERT_KIND_CASE_RESOLVED, second.id) in kinds


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


def test_case_alert_shows_stop_price_only_for_a_stop_instruction() -> None:
    """A full exit carrying the strategy's old stop must not read as "move the stop"."""

    from telegram_kol_research.oncall_alerts import _position_label

    assert _position_label("ETH short") == "ETH 空"
    assert _position_label("BTC long") == "BTC 多"
    assert _position_label("odd") == "odd"


def test_a_full_exit_alert_does_not_print_the_strategys_old_stop(store):
    case = open_case(store, key="mgmt:2:full_exit", action="full_exit")

    text = format_case_alert(case)

    assert "消息要求：全部平仓 / 离场（ETH 空）" in text
    assert "止损→" not in text


def open_churning_lane_case(store, *, exit_state="closing_positions", **evidence):
    """D6a's third cause: an exit that is worked on constantly and never ends."""

    return open_sealed_lane_case(
        store,
        exit_id=evidence.pop("exit_id", 312),
        reason_code="source_deletion_exit_churning_lane",
        exit_state=exit_state,
        stall_class="churning",
        minutes_sealed=evidence.pop("minutes_sealed", 0),
        minutes_unfinished=evidence.pop(
            "minutes_unfinished", 11 * 24 * 60 + 3 * 60
        ),
        attempt_count=evidence.pop("attempt_count", 2097),
        **evidence,
    )


def test_the_churning_lane_alert_says_somebody_is_working_on_it_constantly(store):
    """The opposite story to the other two, so it must not read like neglect."""

    case = open_churning_lane_case(store, exit_last_reason="cancel_entry_retry")

    text = format_sealed_lane_alert(case)

    assert "不是没人管它，而是一直有人在动它却完不成" in text
    # ``attempt_count`` is the evidence, so it is on the page.
    assert "已经被认领 2097 次" in text
    assert "正在市价退出原策略持仓（closing_positions）" in text
    assert "超时清扫只管「需要人工恢复处理」的退出" in text
    assert "系统不会再认领" not in text
    assert "在原地打转" not in text
    assert "一直在被处理却始终完不成" in reason_label(case.reason_code)


def test_the_churning_lane_alert_measures_the_seal_from_the_rows_own_age(store):
    """Its ``updated_at`` is seconds old; that is not how long the lane was shut.

    Reading ``minutes_sealed`` here would print "封了多久：0 分钟" about a lane
    that has been shut for eleven days.
    """

    case = open_churning_lane_case(store)

    text = format_sealed_lane_alert(case)

    assert "封了多久：11 天 3 小时" in text
    assert "封了多久：0 分钟" not in text
    assert "从建立到现在已经 11 天 3 小时都没走完" in text


def test_the_churning_alert_still_reads_when_the_attempt_count_is_missing(store):
    """An old case, or a row whose counter is zero, must not print "0 次"."""

    case = open_churning_lane_case(store, attempt_count=0)

    text = format_sealed_lane_alert(case)

    assert "反复被认领" in text
    assert "被认领 0 次" not in text


def test_the_three_stall_classes_do_not_read_the_same(store):
    """The whole point of the split: three causes must not print one sentence."""

    unclaimable = format_sealed_lane_alert(open_sealed_lane_case(store))
    active = format_sealed_lane_alert(open_stalled_lane_case(store))
    churning = format_sealed_lane_alert(open_churning_lane_case(store))

    assert len({unclaimable, active, churning}) == 3
    # All three still name the same loss, in the same words, and all are urgent.
    for text in (unclaimable, active, churning):
        assert "这个方向的新策略现在一条都进不来" in text
        assert "群：龚有财群    被封的方向：BTC 多" in text
        assert "封了多久：11 天 3 小时" in text


# ---------------------------------------- A3: which bot (2026-09-29)
#
# Event-bot quality design section 6 / 1.2 A.3: an optional second bot for
# alerts that only need acknowledging. Absent (the default in every test
# above, and in production until the two extra env keys are set), nothing
# here is even consulted -- ``deliver_pending_alerts`` falls back to the one
# ``sender`` exactly as before.


def _alert(*, kind, case_id=None):
    return AlertRecord(
        id=1,
        case_id=case_id,
        kind=kind,
        dedupe_key=None,
        body="x",
        status="pending",
        attempts=0,
        delivery_error=None,
    )


@pytest.mark.parametrize(
    "kind",
    [
        ALERT_KIND_HEALTH_OPEN,
        ALERT_KIND_DAILY_SUMMARY,
        ALERT_KIND_CODEX_DOWN,
        ALERT_KIND_CAP_REACHED,
    ],
)
def test_alert_wants_notify_bot_for_kinds_that_carry_no_case(store, kind):
    assert alert_wants_notify_bot(store, _alert(kind=kind)) is True


@pytest.mark.parametrize(
    "kind", [ALERT_KIND_GROUP_MERGED, ALERT_KIND_REMEDIATION_REQUEST_FAILED]
)
def test_alert_wants_notify_bot_is_false_for_message_scoped_kinds(store, kind):
    assert alert_wants_notify_bot(store, _alert(kind=kind, case_id=1)) is False


@pytest.mark.parametrize(
    "kind", [ALERT_KIND_CASE_OPEN, ALERT_KIND_CASE_RESOLVED, ALERT_KIND_DIAGNOSIS]
)
def test_alert_wants_notify_bot_follows_the_cases_own_rule(store, kind):
    d6c, _ = store.upsert_case(
        case_key="unheard_type:management_fraction_rejected",
        rule="D6c",
        severity="high",
        now=NOW,
    )
    d1a, _ = store.upsert_case(
        case_key="mgmt:1:full_exit",
        rule="D1a",
        severity="high",
        now=NOW,
        raw_message_id=1,
    )

    assert alert_wants_notify_bot(store, _alert(kind=kind, case_id=d6c.id)) is True
    assert alert_wants_notify_bot(store, _alert(kind=kind, case_id=d1a.id)) is False


def test_alert_wants_notify_bot_follows_a_merged_rule_list(store):
    """A case that D6c and another rule share must still travel as one -- the
    design's "同一个案件的开案、诊断、已结束始终走同一个 bot" rule reads the
    case's combined ``rule`` string, not just its first component.
    """

    case, _ = store.upsert_case(
        case_key="unheard_type:x", rule="D5b", severity="high", now=NOW
    )
    store.upsert_case(
        case_key="unheard_type:x",
        rule="D6c",
        severity="high",
        now=NOW,
        evidence={"rules": ["D5b", "D6c"]},
    )
    refreshed = store.get_case(case.id)
    assert "D6c" in refreshed.rule.split("+")

    assert (
        alert_wants_notify_bot(store, _alert(kind=ALERT_KIND_CASE_OPEN, case_id=case.id))
        is True
    )


def test_alert_wants_notify_bot_defaults_false_for_an_unknown_case(store):
    assert (
        alert_wants_notify_bot(store, _alert(kind=ALERT_KIND_CASE_OPEN, case_id=99999))
        is False
    )


def test_deliver_pending_alerts_uses_the_default_sender_with_no_router(store):
    store.enqueue_alert(kind=ALERT_KIND_CASE_OPEN, body="a", now=NOW, dedupe_key="a")
    sent = []

    deliver_pending_alerts(store, now=NOW, sender=sent.append)

    assert sent == ["a"]


def test_deliver_pending_alerts_routes_each_alert_through_sender_for(store):
    d6c, _ = store.upsert_case(
        case_key="unheard_type:x", rule="D6c", severity="high", now=NOW
    )
    d1a, _ = store.upsert_case(
        case_key="mgmt:1:full_exit",
        rule="D1a",
        severity="high",
        now=NOW,
        raw_message_id=1,
    )
    store.enqueue_alert(
        kind=ALERT_KIND_CASE_OPEN,
        body="d6c body",
        now=NOW,
        case_id=d6c.id,
        dedupe_key="d6c",
    )
    store.enqueue_alert(
        kind=ALERT_KIND_CASE_OPEN,
        body="d1a body",
        now=NOW,
        case_id=d1a.id,
        dedupe_key="d1a",
    )
    event_sent, notify_sent = [], []

    deliver_pending_alerts(
        store,
        now=NOW,
        sender=event_sent.append,
        sender_for=lambda alert: (
            notify_sent.append if alert_wants_notify_bot(store, alert) else None
        ),
    )

    assert notify_sent == ["d6c body"]
    assert event_sent == ["d1a body"]
