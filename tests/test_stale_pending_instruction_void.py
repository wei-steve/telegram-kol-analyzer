"""A-3 task 3: the void tool may only ever touch the rows on the frozen list."""

from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    MessageInstructionItem,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    StrategyLifecycle,
)
from telegram_kol_research.one_off.stale_pending_instruction_void import (
    DEFERRED_EXPIRED_REASON,
    build_void_notification,
    DEFERRED_HOLD_REASON,
    STALE_PENDING_ITEM_IDS,
    UNBOUND_LIFECYCLE_IDS,
    VOID_REASON,
    apply_stale_pending_void,
    plan_stale_pending_void,
)


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _fixture(tmp_path):
    """Two pending items and two lifecycles; only one of each is on the list."""

    session_factory = create_session_factory(tmp_path / "research.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=-1002337721508,
            message_id=10399,
            text="BTC long",
            archived_target_group=True,
        )
        session.add(raw)
        session.flush()
        session.add_all(
            [
                SignalCandidate(
                    raw_message_id=raw.id,
                    symbol="BTC",
                    side="long",
                    review_status="approved",
                ),
                SignalCandidate(
                    raw_message_id=raw.id,
                    symbol="ETH",
                    side="short",
                    review_status="approved",
                ),
            ]
        )
        session.flush()
        candidate_id, other_candidate_id = (
            int(row_id)
            for (row_id,) in session.query(SignalCandidate.id)
            .filter(SignalCandidate.raw_message_id == raw.id)
            .order_by(SignalCandidate.id.asc())
            .all()
        )
        listed = MessageInstructionItem(
            id=STALE_PENDING_ITEM_IDS[0],
            raw_message_id=raw.id,
            signal_candidate_id=candidate_id,
            sequence=0,
            instruction_kind="entry",
            idempotency_key="listed",
            status="pending",
        )
        bystander = MessageInstructionItem(
            id=max(STALE_PENDING_ITEM_IDS) + 1,
            raw_message_id=raw.id,
            signal_candidate_id=other_candidate_id,
            sequence=1,
            instruction_kind="entry",
            idempotency_key="bystander",
            status="pending",
        )
        listed_lifecycle = StrategyLifecycle(
            id=UNBOUND_LIFECYCLE_IDS[0],
            chat_id=-1002337721508,
            message_id=10399,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW.replace(tzinfo=None),
        )
        bystander_lifecycle = StrategyLifecycle(
            id=max(UNBOUND_LIFECYCLE_IDS) + 1,
            chat_id=-1002337721508,
            message_id=10400,
            symbol="BTC",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW.replace(tzinfo=None),
        )
        session.add_all(
            [
                listed,
                bystander,
                listed_lifecycle,
                bystander_lifecycle,
                RecognitionDecision(
                    raw_message_id=raw.id,
                    input_kind="text",
                    authoritative_model="mimo",
                    authoritative_status="是策略",
                    authoritative_payload_json="{}",
                    agreement_status="agreed",
                    automation_status="deferred",
                    automation_reason=DEFERRED_HOLD_REASON,
                ),
            ]
        )
        session.commit()
    return session_factory


def test_the_void_changes_only_the_listed_item_and_lifecycle(tmp_path):
    session_factory = _fixture(tmp_path)

    result = apply_stale_pending_void(session_factory, now=NOW)

    assert result.voided_item_ids == (STALE_PENDING_ITEM_IDS[0],)
    assert result.terminalized_lifecycle_ids == (UNBOUND_LIFECYCLE_IDS[0],)
    with session_factory() as session:
        listed = session.get(MessageInstructionItem, STALE_PENDING_ITEM_IDS[0])
        bystander = session.get(
            MessageInstructionItem, max(STALE_PENDING_ITEM_IDS) + 1
        )
        assert listed.status == "failed"
        assert listed.error_json == f'{{"reason":"{VOID_REASON}"}}'
        assert listed.escalation_state == "expired"
        assert listed.last_progress_at is not None
        # The row one id past the end of the list is untouched.
        assert bystander.status == "pending"
        assert bystander.error_json is None
        assert bystander.last_progress_at is None
        assert (
            session.get(StrategyLifecycle, UNBOUND_LIFECYCLE_IDS[0]).lifecycle_status
            == "cancelled"
        )
        assert (
            session.get(
                StrategyLifecycle, max(UNBOUND_LIFECYCLE_IDS) + 1
            ).lifecycle_status
            == "entered"
        )


def test_a_second_run_changes_nothing(tmp_path):
    session_factory = _fixture(tmp_path)

    apply_stale_pending_void(session_factory, now=NOW)
    again = apply_stale_pending_void(session_factory, now=NOW)

    assert again.voided_item_ids == ()
    assert again.terminalized_lifecycle_ids == ()


def test_a_listed_lifecycle_that_gained_a_binding_is_left_alone(tmp_path):
    """A binding means real exchange exposure, so this tool refuses it."""

    session_factory = _fixture(tmp_path)
    with session_factory() as session:
        lifecycle = session.get(StrategyLifecycle, UNBOUND_LIFECYCLE_IDS[0])
        lifecycle.execution_binding_id = 337
        session.commit()

    plan = plan_stale_pending_void(session_factory)
    result = apply_stale_pending_void(session_factory, now=NOW)

    assert plan.lifecycle_ids == ()
    assert UNBOUND_LIFECYCLE_IDS[0] in plan.skipped_lifecycle_ids
    assert result.terminalized_lifecycle_ids == ()
    with session_factory() as session:
        assert (
            session.get(StrategyLifecycle, UNBOUND_LIFECYCLE_IDS[0]).lifecycle_status
            == "entered"
        )


def test_the_plan_is_read_only(tmp_path):
    session_factory = _fixture(tmp_path)

    plan = plan_stale_pending_void(session_factory)

    assert plan.item_ids == (STALE_PENDING_ITEM_IDS[0],)
    with session_factory() as session:
        assert (
            session.get(
                MessageInstructionItem, STALE_PENDING_ITEM_IDS[0]
            ).status
            == "pending"
        )


def test_the_void_closes_out_the_decision_so_the_new_expiry_stays_quiet(tmp_path):
    """Otherwise deploying the A-3 loop alerts on rows just voided by hand."""

    session_factory = _fixture(tmp_path)

    result = apply_stale_pending_void(session_factory, now=NOW)

    assert result.expired_decision_raw_message_ids == (1,)
    with session_factory() as session:
        decision = session.query(RecognitionDecision).one()
        assert decision.automation_status == "deferred"
        assert decision.automation_reason == DEFERRED_EXPIRED_REASON


def test_the_copied_reason_literals_match_the_online_constants():
    """The tool copies them so it can run pre-deploy; they must not drift."""

    from telegram_kol_research import deferred_instruction_recovery as online

    assert DEFERRED_HOLD_REASON == online.DEFERRED_HOLD_REASON
    assert DEFERRED_EXPIRED_REASON == online.DEFERRED_EXPIRED_REASON


def test_the_notification_is_one_message_grouped_by_chat():
    """One aggregated message, not 33: it is a single operator decision."""

    rows = [
        {
            "item_id": 988,
            "instruction_kind": "entry",
            "raw_message_id": 15169,
            "chat_title": "陈哥",
            "posted_at": "2026-09-07 00:35",
            "automation_reason": DEFERRED_HOLD_REASON,
        },
        {
            "item_id": 1014,
            "instruction_kind": "entry",
            "raw_message_id": 15372,
            "chat_title": "飞扬",
            "posted_at": "2026-09-08 02:52",
            "automation_reason": DEFERRED_HOLD_REASON,
        },
        {
            "item_id": 1012,
            "instruction_kind": "management",
            "raw_message_id": 15339,
            "chat_title": "飞扬",
            "posted_at": "2026-09-07 23:27",
            "automation_reason": DEFERRED_HOLD_REASON,
        },
    ]

    text = build_void_notification(rows)

    assert text.count("【") == 2
    assert "【飞扬】2 条" in text
    assert "· item 988 entry raw 15169 2026-09-07 00:35" in text
    assert "不补执行" in text
    # Telegram refuses a message over 4096 characters; a 33-row batch must fit.
    assert len(build_void_notification(rows * 11)) < 4000
