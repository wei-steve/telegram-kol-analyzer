"""2026-09-29 event-bot quality, worker side (design sections 2 and 3).

Production facts these replay, from ``docs/plans/2026-09-29-event-bot-quality-design.md``:

* execution attempt 4631 froze with no exchange write and produced **two**
  incidents at the same instant -- 2417 ``authoritative_execution_uncertain``
  and 2418 ``uncertain_without_write``, the second a superset of the first;
* an hour later batch 184 (same raw message 19598) timed out as incident 2419,
  whose text said nothing tying it to 2417/2418;
* every one of them ended "处理: 已记录，正常交易流程未等待本通知。" although each
  is the kind a person has to act on, and none named the group, the message or
  the position.

Group ids here are fake (``-1009999999999``); no production chat id appears.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import telegram_kol_research.system_operator_bot as operator_bot_module
from telegram_kol_research.config import (
    ALWAYS_NOTIFIED_INCIDENT_TYPES,
    RuntimeIncidentConfig,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    AuthoritativeExecutionAttempt,
    ExecutionBinding,
    RawMessage,
    RecognitionDecision,
    RuntimeIncident,
    StrategyLifecycle,
    StrategyManagementBatch,
)
from telegram_kol_research.runtime_incidents import record_runtime_incident
from telegram_kol_research.system_operator_bot import (
    NOTIFICATION_BOT_INCIDENT_TYPES,
    SystemOperatorBotConfig,
    format_runtime_incident_notification,
)


NOW = datetime(2026, 9, 28, 13, 45, 20, tzinfo=UTC)
FAKE_CHAT_ID = -1009999999999
GROUP_LABELS = {FAKE_CHAT_ID: "陈哥"}
RAW_MESSAGE_ID = 19598
ATTEMPT_ID = 4631
BATCH_ID = 184
BINDING_ID = 387
LIFECYCLE_ID = 1348
STRATEGY_INSTANCE_ID = f"deepcoin:{FAKE_CHAT_ID}:10792:BTC:long"
MESSAGE_TEXT = "BTC 保本，剩下的仓位拿好"
GROUP_ID_SHAPE = re.compile(r"-100\d{10,}")
OLD_SENTENCE = "处理: 已记录，正常交易流程未等待本通知。"
GENERATION = "generation-4631"
CLAIM = "claim-4631"


@pytest.fixture
def caplog(caplog, monkeypatch):
    """``caplog`` that still sees this package's records in a full run.

    ``app_logging.configure_application_logging`` sets the package logger's
    ``propagate = False`` for the whole process; the same fixture as
    ``test_runtime_incident_detailed_summaries``.
    """

    monkeypatch.setattr(
        logging.getLogger("telegram_kol_research"), "propagate", True
    )
    return caplog


def _session_factory(tmp_path, name="event-bot.db"):
    session_factory = create_session_factory(tmp_path / name)
    schema = importlib.import_module(
        "telegram_kol_research.authoritative_execution_schema"
    )
    plan = schema.build_recognition_execution_schema_plan(session_factory.kw["bind"])
    schema.apply_recognition_execution_schema(
        session_factory.kw["bind"], expected_plan_sha256=plan.plan_sha256
    )
    return session_factory


def _attempt(**overrides) -> AuthoritativeExecutionAttempt:
    values = dict(
        id=ATTEMPT_ID,
        raw_message_id=RAW_MESSAGE_ID,
        authoritative_generation=GENERATION,
        status="executing",
        claim_token=CLAIM,
        owner_runtime_role="worker",
        owner_instance_id="instance-4631",
        owner_pid=1,
        owner_boot_id="unavailable",
        owner_process_start_ticks=1,
        owner_systemd_invocation_id="invocation-4631",
        claimed_at=NOW.replace(tzinfo=None),
        heartbeat_at=NOW.replace(tzinfo=None),
        lease_expires_at=NOW.replace(tzinfo=None),
        side_effect_started_at=NOW.replace(tzinfo=None),
        exchange_effect="outcome_unknown",
        created_at=NOW.replace(tzinfo=None),
        updated_at=NOW.replace(tzinfo=None),
    )
    values.update(overrides)
    return AuthoritativeExecutionAttempt(**values)


def _seed_message_graph(session_factory, *, attempt_status="uncertain"):
    """Raw 19598 in 陈哥's (fake) group, attempt 4631, batch 184, binding 387."""

    naive = NOW.replace(tzinfo=None)
    with session_factory() as session:
        session.add(
            RawMessage(
                id=RAW_MESSAGE_ID,
                chat_id=FAKE_CHAT_ID,
                message_id=10800,
                text=MESSAGE_TEXT,
                posted_at=naive,
                created_at=naive,
            )
        )
        decision = RecognitionDecision(
            raw_message_id=RAW_MESSAGE_ID,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="是策略",
            authoritative_payload_json="{}",
            agreement_status="agreed",
            differences_json="[]",
            comparison_status="execution_uncertain",
            comparison_claim_token=GENERATION,
            created_at=naive,
            updated_at=naive,
        )
        session.add(decision)
        session.add(
            ExecutionBinding(
                id=BINDING_ID,
                strategy_instance_id=STRATEGY_INSTANCE_ID,
                kol_id=f"group:{FAKE_CHAT_ID}",
                chat_id=FAKE_CHAT_ID,
                message_id=10792,
                symbol="BTC",
                side="long",
                status="closed",
            )
        )
        session.add(
            StrategyLifecycle(
                id=LIFECYCLE_ID,
                chat_id=FAKE_CHAT_ID,
                message_id=10792,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=naive - timedelta(days=1),
                entry_range_low=100,
                entry_range_high=110,
                stop_loss=90,
                take_profit="130",
                execution_binding_id=BINDING_ID,
            )
        )
        session.add(_attempt(status=attempt_status))
        session.flush()
        session.add(
            StrategyManagementBatch(
                id=BATCH_ID,
                idempotency_fingerprint="f" * 64,
                raw_message_id=RAW_MESSAGE_ID,
                recognition_decision_id=decision.id,
                recognition_generation=GENERATION,
                target_lifecycle_id=LIFECYCLE_ID,
                strategy_instance_id=STRATEGY_INSTANCE_ID,
                execution_binding_id=BINDING_ID,
                intent="break_even",
                effective_action="break_even",
                execution_mode="live",
                status="blocked",
                reason_code="break_even_market_decision_missing_or_invalid",
                target_fingerprint="b" * 64,
                target_snapshot_json="{}",
                planned_at=naive,
                created_at=naive,
                updated_at=naive,
            )
        )
        session.commit()


def _record(session_factory, *, incident_type, source_kind, source_record_id, summary, at):
    redacted_summary = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    return record_runtime_incident(
        session_factory,
        source_kind=source_kind,
        source_record_id=str(source_record_id),
        incident_type=incident_type,
        severity="high",
        # Distinct per summary, as the adapters' fingerprints are; the same
        # fingerprint would coalesce into one row.
        fingerprint=hashlib.sha256(
            f"{incident_type}\0{source_kind}\0{source_record_id}\0"
            f"{redacted_summary}".encode()
        ).hexdigest(),
        redacted_summary=redacted_summary,
        occurred_at=at,
        feature_policy_version="runtime-incident-phase-2-v1",
        prompt_version="none",
        tool_policy_version="none",
    )


#: The three production summaries, as they were stored (2419 had fallen back to
#: its minimal summary, which is exactly the shape that has to be enriched).
_SUMMARY_2417 = {
    "attempt_id": ATTEMPT_ID,
    "component": "authoritative_execution",
    "error_summary": "partial_failed no_exchange_write_tracked",
    "error_type": "ManagementBatchExecutionError",
    "operation": f"raw_message_{RAW_MESSAGE_ID}",
    "raw_message_id": RAW_MESSAGE_ID,
    "source_status": "uncertain",
}
_SUMMARY_2418 = {
    **_SUMMARY_2417,
    "reason_code": "no_exchange_write_tracked",
    "impact": "frozen_without_evidence_of_contact",
}
_SUMMARY_2419 = {
    "component": "strategy_management_batch",
    "operation": f"management_batch_{BATCH_ID}",
    "reason_code": "break_even_market_decision_missing_or_invalid",
    "source_status": "recovery_timeout",
}


def _record_2417(session_factory):
    return _record(
        session_factory,
        incident_type="authoritative_execution_uncertain",
        source_kind="authoritative_execution_attempt",
        source_record_id=ATTEMPT_ID,
        summary=_SUMMARY_2417,
        at=NOW,
    )


def _record_2418(session_factory):
    return _record(
        session_factory,
        incident_type="uncertain_without_write",
        source_kind="authoritative_execution_attempt",
        source_record_id=ATTEMPT_ID,
        summary=_SUMMARY_2418,
        at=NOW,
    )


def _record_2419(session_factory):
    return _record(
        session_factory,
        incident_type="management_recovery_timeout",
        source_kind="strategy_management_batch",
        source_record_id=BATCH_ID,
        summary=_SUMMARY_2419,
        at=NOW + timedelta(hours=1),
    )


def _deliver(session_factory, monkeypatch, **kwargs):
    sent: list[str] = []

    async def capture(**send_kwargs):
        sent.append(send_kwargs["text"])
        return 1

    monkeypatch.setattr(
        operator_bot_module, "send_system_operator_bot_message", capture
    )
    delivered = asyncio.run(
        operator_bot_module.deliver_runtime_incident_notifications(
            session_factory,
            config=SystemOperatorBotConfig("op-token", "op-chat"),
            runtime_config=RuntimeIncidentConfig(
                telegram_notifications_enabled=True
            ),
            claimed_at=NOW + timedelta(hours=1, seconds=5),
            **kwargs,
        )
    )
    return delivered, sent


def _incident_by_id(session_factory, incident_id):
    with session_factory() as session:
        row = session.get(RuntimeIncident, int(incident_id))
        session.expunge(row)
        return row


# --------------------------------------------------------------------------
# R2-a: one frozen attempt, one incident
# --------------------------------------------------------------------------


def _freeze_4631(tmp_path, monkeypatch, *, evidence_refs):
    from telegram_kol_research import authoritative_execution_attempts as attempts

    session_factory = _session_factory(tmp_path)
    naive = NOW.replace(tzinfo=None)
    with session_factory() as session:
        session.add(
            RawMessage(
                id=RAW_MESSAGE_ID,
                chat_id=FAKE_CHAT_ID,
                message_id=10800,
                text=MESSAGE_TEXT,
                posted_at=naive,
                created_at=naive,
            )
        )
        session.add(
            RecognitionDecision(
                raw_message_id=RAW_MESSAGE_ID,
                input_kind="text",
                authoritative_model="mimo-v2.5",
                authoritative_status="是策略",
                authoritative_payload_json="{}",
                agreement_status="agreed",
                differences_json="[]",
                comparison_status="execution_running",
                comparison_claim_token=GENERATION,
                created_at=naive,
                updated_at=naive,
            )
        )
        session.add(_attempt())
        session.commit()
    # Both types captured, as in production where both are always-notified.
    monkeypatch.setattr(
        "telegram_kol_research.config.load_runtime_incident_config",
        lambda *a, **k: RuntimeIncidentConfig(
            capture_types=frozenset(
                {"authoritative_execution_uncertain", "uncertain_without_write"}
            )
        ),
    )
    assert attempts.mark_authoritative_execution_uncertain(
        session_factory,
        attempt_id=ATTEMPT_ID,
        claim_token=CLAIM,
        uncertain_at=NOW,
        error_class="ManagementBatchExecutionError",
        error_summary="partial_failed",
        evidence_refs=evidence_refs,
    )
    with session_factory() as session:
        return [
            (row.incident_type, row.source_kind, row.source_record_id)
            for row in session.query(RuntimeIncident)
            .order_by(RuntimeIncident.id)
            .all()
        ]


def test_r2a_a_freeze_with_no_write_records_only_uncertain_without_write(
    tmp_path, monkeypatch
):
    """Attempt 4631's shape: before the fix this wrote 2417 *and* 2418."""

    incidents = _freeze_4631(tmp_path, monkeypatch, evidence_refs=[])

    assert incidents == [
        ("uncertain_without_write", "authoritative_execution_attempt", str(ATTEMPT_ID))
    ]


def test_r2a_a_freeze_with_a_write_records_only_authoritative_execution_uncertain(
    tmp_path, monkeypatch
):
    incidents = _freeze_4631(
        tmp_path,
        monkeypatch,
        evidence_refs=[
            {
                "kind": "deepcoin_write",
                "method": "place_order",
                "ordinal": 1,
                "outcome": "outcome_unknown",
            }
        ],
    )

    assert incidents == [
        (
            "authoritative_execution_uncertain",
            "authoritative_execution_attempt",
            str(ATTEMPT_ID),
        )
    ]


# --------------------------------------------------------------------------
# R3-a / R2-b: the text a person reads
# --------------------------------------------------------------------------


def test_r3a_event_handling_text_says_what_to_do_not_that_nothing_waited(tmp_path):
    """No context needed for the wording itself: the old sentence is gone."""

    session_factory = _session_factory(tmp_path)
    incident = _record_2419(session_factory)

    text = format_runtime_incident_notification(incident)

    assert "未等待本通知" not in text
    assert "需要你：这条管理指令已放弃执行、冻结已解除" in text


def test_r3a_delivered_texts_name_group_message_position_and_the_earlier_reports(
    tmp_path, monkeypatch
):
    session_factory = _session_factory(tmp_path)
    _seed_message_graph(session_factory)
    first = _record_2417(session_factory)
    second = _record_2418(session_factory)
    third = _record_2419(session_factory)

    delivered, sent = _deliver(
        session_factory,
        monkeypatch,
        group_label_for=GROUP_LABELS.get,
    )

    assert delivered == 3
    by_id = {
        incident.id: next(text for text in sent if f"事件ID: {incident.id}" in text)
        for incident in (first, second, third)
    }
    for text in by_id.values():
        assert "未等待本通知" not in text
        assert "需要你：" in text
        assert "群: 陈哥" in text
        assert "原文: BTC 保本，剩下的仓位拿好" in text
        assert GROUP_ID_SHAPE.search(text) is None, text
        assert str(FAKE_CHAT_ID) not in text
    assert "需要你：这条消息已冻结、不会再自动执行" in by_id[first.id]
    assert "需要你：这条消息已冻结、不会再自动执行" in by_id[second.id]
    timeout_text = by_id[third.id]
    assert "需要你：这条管理指令已放弃执行、冻结已解除" in timeout_text
    assert "仓位: BTC 多（绑定 #387，closed）" in timeout_text
    # R2-b: the timeout says it is the same message as the two earlier reports.
    assert f"#{second.id}（uncertain_without_write）" in timeout_text
    assert f"#{first.id}（authoritative_execution_uncertain）" in timeout_text
    assert "关联：同一消息此前已报" in timeout_text
    # The first report has nothing earlier to point at.
    assert "关联" not in by_id[first.id]


def test_r2b_the_timeout_context_lists_the_earlier_incident_of_the_same_message(
    tmp_path,
):
    from telegram_kol_research.system_operator_bot import load_incident_context

    session_factory = _session_factory(tmp_path)
    _seed_message_graph(session_factory)
    earlier = _record_2418(session_factory)
    timeout = _record_2419(session_factory)

    context = load_incident_context(
        session_factory, timeout, group_label_for=GROUP_LABELS.get
    )

    assert context is not None
    assert context.raw_message_id == RAW_MESSAGE_ID
    assert context.group_label == "陈哥"
    assert context.message_excerpt == MESSAGE_TEXT
    assert context.position == "BTC 多（绑定 #387，closed）"
    assert context.related == (f"#{earlier.id}（uncertain_without_write）",)
    text = format_runtime_incident_notification(timeout, context=context)
    assert f"关联：同一消息此前已报 #{earlier.id}（uncertain_without_write）" in text


def test_related_rows_are_bounded_to_three_and_to_the_last_24_hours(tmp_path):
    from telegram_kol_research.system_operator_bot import load_incident_context

    session_factory = _session_factory(tmp_path)
    _seed_message_graph(session_factory)
    stale = _record(
        session_factory,
        incident_type="authoritative_execution_uncertain",
        source_kind="authoritative_execution_attempt",
        source_record_id=ATTEMPT_ID,
        summary={**_SUMMARY_2417, "error_type": "Stale"},
        at=NOW - timedelta(days=2),
    )
    recent = [
        _record(
            session_factory,
            incident_type="uncertain_without_write",
            source_kind="authoritative_execution_attempt",
            source_record_id=ATTEMPT_ID,
            summary={**_SUMMARY_2418, "error_type": f"Recent{index}"},
            at=NOW + timedelta(minutes=index),
        )
        for index in range(4)
    ]
    timeout = _record_2419(session_factory)

    context = load_incident_context(
        session_factory, timeout, group_label_for=GROUP_LABELS.get
    )

    assert len(context.related) == 3
    listed = " ".join(context.related)
    assert f"#{stale.id}（" not in listed
    # Newest first: the three nearest the timeout.
    for incident in recent[1:]:
        assert f"#{incident.id}（" in listed


def test_an_unknown_group_is_said_as_such_and_never_as_its_chat_id(tmp_path):
    from telegram_kol_research.system_operator_bot import load_incident_context

    session_factory = _session_factory(tmp_path)
    _seed_message_graph(session_factory)
    incident = _record_2418(session_factory)

    for group_label_for in (lambda _chat: None, _raise_on_call):
        context = load_incident_context(
            session_factory, incident, group_label_for=group_label_for
        )
        assert context.group_label == "未知群"
        text = format_runtime_incident_notification(incident, context=context)
        assert "群: 未知群" in text
        assert GROUP_ID_SHAPE.search(text) is None


def _raise_on_call(_chat_id):
    raise RuntimeError("group config unreadable")


# --------------------------------------------------------------------------
# R3-c: context is display-only and can never cost the delivery
# --------------------------------------------------------------------------


def test_r3c_a_failing_database_gives_no_context_and_a_warning(tmp_path, caplog):
    from telegram_kol_research.system_operator_bot import load_incident_context

    session_factory = _session_factory(tmp_path)
    incident = _record_2418(session_factory)

    def broken_session_factory():
        raise RuntimeError("database is locked")

    caplog.set_level(logging.WARNING, logger="telegram_kol_research")
    context = load_incident_context(
        broken_session_factory, incident, group_label_for=GROUP_LABELS.get
    )

    assert context is None or context.is_empty()
    assert "incident context" in caplog.text
    assert "database is locked" not in caplog.text


def test_r3c_a_missing_message_row_leaves_only_what_could_be_read(tmp_path):
    """The attempt points at a raw message that is not there."""

    from telegram_kol_research.system_operator_bot import load_incident_context

    session_factory = _session_factory(tmp_path)
    with session_factory() as session:
        session.add(_attempt(status="uncertain"))
        session.commit()
    incident = _record_2419(session_factory)  # batch 184 does not exist either

    context = load_incident_context(
        session_factory, incident, group_label_for=GROUP_LABELS.get
    )

    assert context is None or context.group_label is None
    text = format_runtime_incident_notification(incident, context=context)
    assert "需要你：" in text
    assert "群:" not in text


def test_r3c_a_context_loader_that_raises_does_not_stop_delivery(
    tmp_path, monkeypatch
):
    session_factory = _session_factory(tmp_path)
    _seed_message_graph(session_factory)
    incident = _record_2418(session_factory)

    def exploding(*_args, **_kwargs):
        raise RuntimeError("context exploded")

    monkeypatch.setattr(operator_bot_module, "load_incident_context", exploding)
    delivered, sent = _deliver(
        session_factory, monkeypatch, group_label_for=GROUP_LABELS.get
    )

    assert delivered == 1
    assert f"事件ID: {incident.id}" in sent[0]
    assert "需要你：" in sent[0]
    assert "群:" not in sent[0]
    assert _incident_by_id(session_factory, incident.id).notification_status == (
        "delivered"
    )


def test_notification_bot_types_are_not_enriched(tmp_path, monkeypatch):
    """Context is for the incidents a person acts on; the rest are not queried."""

    session_factory = _session_factory(tmp_path)
    _seed_message_graph(session_factory)
    incident = _record(
        session_factory,
        incident_type="entry_admission_expired",
        source_kind="message_instruction_item",
        source_record_id=1419,
        summary={
            "component": "entry_admission",
            "raw_message_id": RAW_MESSAGE_ID,
            "reason_code": "adjacent_entry_context_pending",
        },
        at=NOW,
    )
    calls: list[int] = []
    original = operator_bot_module.load_incident_context

    def spy(session_factory_, incident_, **kwargs):
        calls.append(int(incident_.id))
        return original(session_factory_, incident_, **kwargs)

    monkeypatch.setattr(operator_bot_module, "load_incident_context", spy)
    delivered, sent = _deliver(
        session_factory, monkeypatch, group_label_for=GROUP_LABELS.get
    )

    assert delivered == 1
    assert calls == []
    assert OLD_SENTENCE in sent[0]
    assert incident.incident_type in NOTIFICATION_BOT_INCIDENT_TYPES


# --------------------------------------------------------------------------
# Every production-whitelisted type: wording by destination, and no group id
# --------------------------------------------------------------------------

_PRODUCTION_EXTRA_TYPES = frozenset(
    {
        "management_partial_failed",
        "severe_protection_incident",
        "management_target_refused",
        "management_target_orchestration_failed",
        "management_target_visibility_exhausted",
        "management_target_drift",
        "management_target_collision",
        "unclassified_operation_failure",
    }
)
_ALL_TYPES = sorted(
    ALWAYS_NOTIFIED_INCIDENT_TYPES
    | NOTIFICATION_BOT_INCIDENT_TYPES
    | _PRODUCTION_EXTRA_TYPES
)
#: A summary shaped like the adapters' output, including the integer ``chat_id``
#: several of them carry. The provider-outage formatter used to print that one.
_REALISTIC_SUMMARY = {
    "component": "authoritative_execution",
    "source_status": "uncertain",
    "reason_code": "no_exchange_write_tracked",
    "error_type": "ReadTimeout",
    "impact": "frozen_without_evidence_of_contact",
    "operation": f"raw_message_{RAW_MESSAGE_ID}",
    "raw_message_id": RAW_MESSAGE_ID,
    "chat_id": FAKE_CHAT_ID,
    "message_posted_at": "2026-09-28T13:45Z",
    "entry_summary": "BTC long 83000-83300",
    "instruction_excerpt": MESSAGE_TEXT,
    "episode_started_at": "2026-09-28T13:10Z",
    "last_failure_at": "2026-09-28T13:20Z",
    "recovered_at": "2026-09-28T13:40Z",
    "consecutive_failures": 3,
    "retry_count": 2,
}


def _incident(incident_type: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=2419,
        incident_type=incident_type,
        severity="high",
        source_kind="strategy_management_batch",
        source_record_id=str(BATCH_ID),
        repeat_count=1,
        redacted_summary=json.dumps(_REALISTIC_SUMMARY),
    )


def _context() -> "operator_bot_module.IncidentContext":
    return operator_bot_module.IncidentContext(
        raw_message_id=RAW_MESSAGE_ID,
        group_label="陈哥",
        message_excerpt=MESSAGE_TEXT,
        position="BTC 多（绑定 #387，closed）",
        related=("#2418（uncertain_without_write）",),
    )


def test_the_type_list_covers_what_production_notifies():
    assert len(_ALL_TYPES) >= 40
    assert "management_recovery_timeout" in _ALL_TYPES
    assert "provider_outage_entry_not_replayed" in _ALL_TYPES


@pytest.mark.parametrize("incident_type", _ALL_TYPES)
def test_every_notified_type_is_worded_by_destination_and_carries_no_group_id(
    incident_type,
):
    provider_formatted = (
        incident_type in operator_bot_module._MIMO_PROVIDER_INCIDENT_TYPES
    )
    stays_in_event_handling = incident_type not in NOTIFICATION_BOT_INCIDENT_TYPES
    rendered = [format_runtime_incident_notification(_incident(incident_type))]
    if stays_in_event_handling:
        rendered.append(
            format_runtime_incident_notification(
                _incident(incident_type), context=_context()
            )
        )

    for text in rendered:
        assert GROUP_ID_SHAPE.search(text) is None, (incident_type, text)
        if stays_in_event_handling:
            assert "未等待本通知" not in text, incident_type
            assert "需要你：" in text, incident_type
        elif not provider_formatted:
            assert OLD_SENTENCE in text, incident_type
            assert "需要你" not in text, incident_type


def test_the_action_sentences_are_the_approved_ones():
    hints = operator_bot_module.INCIDENT_ACTION_HINTS
    frozen = "这条消息已冻结、不会再自动执行；请到交易所核对相关仓位是否需要手动处理。"
    assert hints["uncertain_without_write"] == frozen
    assert hints["authoritative_execution_uncertain"] == frozen
    assert hints["management_recovery_timeout"] == (
        "这条管理指令已放弃执行、冻结已解除；请核对仓位的止损 / 止盈是否符合原意。"
    )
    for incident_type in (
        "severe_protection_incident",
        "management_recovery_required",
        "source_deletion_exit_stuck",
    ):
        assert hints[incident_type] == "到交易所核对并人工恢复。"
    for incident_type in (
        "management_target_needs_confirmation",
        "duplicate_entry_needs_confirmation",
    ):
        assert hints[incident_type] == "回复 /choose 或 /dismiss。"
    # No hint is written for a type that goes to the notification bot: those
    # keep the old sentence, and a hint there would never be read.
    assert not set(hints) & NOTIFICATION_BOT_INCIDENT_TYPES
    text = format_runtime_incident_notification(_incident("notification_delivery_failure"))
    assert "需要你：自动处理已停止，需要人工核对" in text


# --------------------------------------------------------------------------
# Wiring: the worker names groups from the live group config
# --------------------------------------------------------------------------


def test_the_worker_label_lookup_reads_the_live_group_config(tmp_path):
    from telegram_kol_research import web_app
    from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig

    app = web_app.create_web_app(database_path=tmp_path / "research.db")
    lookup = web_app._group_label_lookup(app)
    app.state.group_config = GroupConfig(
        groups=[TargetGroupConfig(chat_title="原始群名", chat_id=FAKE_CHAT_ID)]
    )
    assert lookup(FAKE_CHAT_ID) == "原始群名"
    # A reload replaces the object; the lookup must see the new one.
    app.state.group_config = GroupConfig(
        groups=[
            TargetGroupConfig(
                chat_title="原始群名", chat_id=FAKE_CHAT_ID, custom_group_label="陈哥"
            )
        ]
    )
    assert lookup(FAKE_CHAT_ID) == "陈哥"
    assert lookup(-1008888888888) is None
    # The worker-command path hands the same lookup to the cleanup deliverer.
    assert app.state.worker_command_dependencies.group_label_for is not None
