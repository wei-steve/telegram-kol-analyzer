"""Management preflight refusal proof and its terminal state.

Design: ``docs/plans/2026-09-29-management-preflight-refusal-uncertain-design.md``.
Status: ``docs/management-preflight-refusal-status.md``.

R5-a/R5-b replay the two production samples (attempts 4631, 4705) at the
proof level: every management item failed with
``ManagementBatchExecutionError`` and the batch ledger never left its initial
state. R5-c..g are the regression guards that must still freeze -- most of
them already froze on the code before this change (A-6's own per-item proof
never reaches the management-batch ledger at all), so they double as A-6
regression coverage, not just new coverage.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from telegram_kol_research.authoritative_execution_attempts import (
    CLOSED_NO_WRITE,
    CLOSEOUT_STATUSES,
)
from telegram_kol_research.authoritative_execution_schema import (
    apply_recognition_execution_schema,
    build_recognition_execution_schema_plan,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.execution_boundary import (
    management_batches_prove_no_exchange_contact,
)
from telegram_kol_research.models import (
    AuthoritativeExecutionAttempt,
    ExecutionEvent,
    MessageInstructionItem,
    RawMessage,
    RecognitionDecision,
    StrategyManagementBatch,
    StrategyManagementComponent,
    StrategyManagementLeg,
    StrategyManagementMarketDecision,
    TriggerProtectionStopRescue,
)


NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
#: A fake group id, never production's: -1009999999999.
FAKE_CHAT_ID = -1009999999999


def _prepared(tmp_path, name="preflight.db"):
    session_factory = create_session_factory(tmp_path / name)
    engine = session_factory.kw["bind"]
    plan = build_recognition_execution_schema_plan(engine)
    apply_recognition_execution_schema(engine, expected_plan_sha256=plan.plan_sha256)
    return session_factory


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _seed_raw_message(session, *, raw_message_id: int, message_id: int) -> None:
    session.add(
        RawMessage(
            id=raw_message_id,
            chat_id=FAKE_CHAT_ID,
            message_id=message_id,
            sender_name="chen-ge",
            posted_at=_naive(NOW) - timedelta(minutes=5),
            text="保本",
        )
    )


def _seed_management_item(
    session,
    *,
    raw_message_id: int,
    item_id: int,
    error_message: str,
    instruction_kind: str = "management",
    status: str = "failed",
) -> None:
    session.add(
        MessageInstructionItem(
            id=item_id,
            raw_message_id=raw_message_id,
            signal_candidate_id=item_id,
            sequence=1,
            instruction_kind=instruction_kind,
            idempotency_key=f"item-{item_id}",
            status=status,
            error_json=json.dumps(
                {"type": "ManagementBatchExecutionError", "message": error_message}
            ),
        )
    )


def _seed_batch(
    session,
    *,
    batch_id: int,
    raw_message_id: int,
    leg_ids: tuple[int, ...] = (),
    leg_status: str = "planned",
    leg_client_order_id: str | None = None,
    leg_request_json: str | None = None,
) -> None:
    session.add(
        StrategyManagementBatch(
            id=batch_id,
            idempotency_fingerprint=f"fp-{batch_id}" * 4,
            raw_message_id=raw_message_id,
            recognition_decision_id=1,
            recognition_generation="g1",
            target_lifecycle_id=1,
            strategy_instance_id=f"deepcoin:{FAKE_CHAT_ID}:{raw_message_id}:BTC:long",
            execution_binding_id=1,
            intent="move_stop_to_break_even",
            effective_action="break_even_by_market",
            status="blocked",
            reason_code="protection_rows_unattributed_on_exchange",
            target_fingerprint="t" * 64,
            planned_at=_naive(NOW),
            created_at=_naive(NOW),
            updated_at=_naive(NOW),
        )
    )
    for index, leg_id in enumerate(leg_ids, start=1):
        session.add(
            StrategyManagementLeg(
                id=leg_id,
                management_batch_id=batch_id,
                execution_order_leg_id=leg_id,
                pos_id=f"1001125406857{leg_id:03d}",
                leg_index=index,
                status=leg_status,
                client_order_id=leg_client_order_id,
                request_json=leg_request_json,
            )
        )


def test_r5a_the_break_even_batch_184_shape_proves_no_contact(tmp_path):
    """R5-a: attempt 4631's shape -- batch 184, legs 158/159, no evidence."""

    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        _seed_raw_message(session, raw_message_id=4631, message_id=19598)
        _seed_management_item(
            session,
            raw_message_id=4631,
            item_id=1426,
            error_message=(
                "protection_rows_unattributed_on_exchange:"
                "1001125406857038:1001125407523144,1001125407523252"
            ),
        )
        _seed_batch(
            session,
            batch_id=184,
            raw_message_id=4631,
            leg_ids=(158, 159),
        )
        session.commit()

    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=4631
    )
    assert proven is True
    assert len(refs) == 1
    assert refs[0]["kind"] == "management_batch_no_contact"
    assert refs[0]["management_batch_id"] == 184
    assert refs[0]["leg_count"] == 2


def test_r5b_the_partial_close_batch_187_shape_proves_no_contact(tmp_path):
    """R5-b: attempt 4705's shape -- batch 187, leg 163, provenance refusal."""

    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        _seed_raw_message(session, raw_message_id=4705, message_id=19670)
        _seed_management_item(
            session,
            raw_message_id=4705,
            item_id=1500,
            error_message="management_stop_provenance_invalid",
        )
        _seed_batch(
            session,
            batch_id=187,
            raw_message_id=4705,
            leg_ids=(163,),
        )
        session.commit()

    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=4705
    )
    assert proven is True
    assert refs[0]["management_batch_id"] == 187
    assert refs[0]["leg_count"] == 1


def test_r5c_a_reserved_leg_with_client_order_id_keeps_it_frozen(tmp_path):
    """R5-c: one leg already reserved -- must not be proven."""

    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        _seed_raw_message(session, raw_message_id=1, message_id=1)
        _seed_management_item(
            session, raw_message_id=1, item_id=1, error_message="some_refusal"
        )
        _seed_batch(session, batch_id=1, raw_message_id=1, leg_ids=(1, 2))
        session.flush()
        # Leg 2 already reserved with a client_order_id.
        leg = session.get(StrategyManagementLeg, 2)
        leg.status = "reserved"
        leg.client_order_id = "TMABCDEF0123456789"
        session.commit()

    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=1
    )
    assert proven is False
    assert refs == ()


def test_r5d_a_component_with_an_attempt_keeps_it_frozen(tmp_path):
    """R5-d: a component past its initial state -- must not be proven."""

    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        _seed_raw_message(session, raw_message_id=2, message_id=2)
        _seed_management_item(
            session, raw_message_id=2, item_id=2, error_message="some_refusal"
        )
        _seed_batch(session, batch_id=2, raw_message_id=2, leg_ids=(3,))
        session.add(
            StrategyManagementComponent(
                management_batch_id=2,
                strategy_management_leg_id=3,
                strategy_management_leg_scope=3,
                component_kind="protection_replace",
                sequence=1,
                status="pending",
                idempotency_key="component-2",
                attempt_count=1,
            )
        )
        session.commit()

    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=2
    )
    assert proven is False
    assert refs == ()


def test_r5e_a_market_decision_row_keeps_it_frozen(tmp_path):
    """R5-e (market decision branch): a reserved decision -- must not be proven."""

    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        _seed_raw_message(session, raw_message_id=3, message_id=3)
        _seed_management_item(
            session, raw_message_id=3, item_id=3, error_message="some_refusal"
        )
        _seed_batch(session, batch_id=3, raw_message_id=3, leg_ids=(4,))
        session.add(
            StrategyManagementMarketDecision(
                management_batch_id=3,
                strategy_instance_id=f"deepcoin:{FAKE_CHAT_ID}:3:BTC:long",
                instrument_id="BTC-USDT-SWAP",
                quote_price="65000",
                quote_price_field="last",
                decisions_json="[]",
                decision_fingerprint="fp",
                observed_at=_naive(NOW),
                created_at=_naive(NOW),
            )
        )
        session.commit()

    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=3
    )
    assert proven is False
    assert refs == ()


def test_r5e_a_non_ready_rescue_for_the_same_position_keeps_it_frozen(tmp_path):
    """R5-e (rescue branch): a rescue past ``ready`` on the batch's own pos_id."""

    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        _seed_raw_message(session, raw_message_id=4, message_id=4)
        _seed_management_item(
            session, raw_message_id=4, item_id=4, error_message="some_refusal"
        )
        _seed_batch(session, batch_id=4, raw_message_id=4, leg_ids=(5,))
        session.flush()
        leg = session.get(StrategyManagementLeg, 5)
        session.add(
            TriggerProtectionStopRescue(
                trigger_protection_intent_id=1,
                execution_binding_id=1,
                execution_order_leg_id=leg.execution_order_leg_id,
                pos_id=leg.pos_id,
                status="reserved",
            )
        )
        session.commit()

    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=4
    )
    assert proven is False
    assert refs == ()


def test_r5e_an_execution_event_for_the_message_keeps_it_frozen(tmp_path):
    """R5-e (execution event branch): any event for the message disproves it."""

    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        _seed_raw_message(session, raw_message_id=5, message_id=5)
        _seed_management_item(
            session, raw_message_id=5, item_id=5, error_message="some_refusal"
        )
        _seed_batch(session, batch_id=5, raw_message_id=5, leg_ids=(6,))
        session.add(
            ExecutionEvent(
                action="strategy_management_deferred_entry_cancel_diagnostic",
                venue="deepcoin",
                status="failed",
                chat_id=FAKE_CHAT_ID,
                message_id=5,
                source_message_id=5,
                created_at=_naive(NOW),
            )
        )
        session.commit()

    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=5
    )
    assert proven is False
    assert refs == ()


def test_r5f_a_different_error_class_keeps_it_frozen(tmp_path):
    """R5-f: an ``IntegrityError`` item -- not a management preflight refusal."""

    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        _seed_raw_message(session, raw_message_id=6, message_id=6)
        session.add(
            MessageInstructionItem(
                id=6,
                raw_message_id=6,
                signal_candidate_id=6,
                sequence=1,
                instruction_kind="management",
                idempotency_key="item-6",
                status="failed",
                error_json=json.dumps(
                    {"type": "IntegrityError", "message": "unique constraint"}
                ),
            )
        )
        _seed_batch(session, batch_id=6, raw_message_id=6, leg_ids=(7,))
        session.commit()

    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=6
    )
    assert proven is False
    assert refs == ()


def test_r5f_a_mixed_entry_item_keeps_it_frozen(tmp_path):
    """R5-f: one entry item mixed in with the management refusal."""

    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        _seed_raw_message(session, raw_message_id=7, message_id=7)
        _seed_management_item(
            session, raw_message_id=7, item_id=7, error_message="some_refusal"
        )
        session.add(
            MessageInstructionItem(
                id=8,
                raw_message_id=7,
                signal_candidate_id=8,
                sequence=2,
                instruction_kind="entry",
                idempotency_key="item-8",
                status="failed",
                error_json=json.dumps({"type": "SomeOtherError", "message": "x"}),
            )
        )
        _seed_batch(session, batch_id=7, raw_message_id=7, leg_ids=(9,))
        session.commit()

    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=7
    )
    assert proven is False
    assert refs == ()


def test_r5g_a_read_failure_fails_closed(tmp_path):
    """R5-g: an unreadable database still returns "not proven", never raises."""

    def _broken_session_factory():
        raise RuntimeError("database unavailable")

    proven, refs = management_batches_prove_no_exchange_contact(
        _broken_session_factory, raw_message_id=1
    )
    assert proven is False
    assert refs == ()


def test_no_management_items_at_all_is_not_proven(tmp_path):
    """A message with no instruction items cannot prove anything about itself."""

    session_factory = _prepared(tmp_path)
    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=999
    )
    assert proven is False
    assert refs == ()


def test_a_batch_with_no_legs_is_not_proven(tmp_path):
    """A batch that somehow has no legs cannot prove the leg condition."""

    session_factory = _prepared(tmp_path)
    with session_factory() as session:
        _seed_raw_message(session, raw_message_id=8, message_id=8)
        _seed_management_item(
            session, raw_message_id=8, item_id=9, error_message="some_refusal"
        )
        _seed_batch(session, batch_id=8, raw_message_id=8, leg_ids=())
        session.commit()

    proven, refs = management_batches_prove_no_exchange_contact(
        session_factory, raw_message_id=8
    )
    assert proven is False
    assert refs == ()


# ---------------------------------------------------------------------------
# The terminal state once the proof holds (Q1 = B).
# ---------------------------------------------------------------------------


def _attempt_ready_for_refusal(session_factory, *, raw_message_id: int) -> tuple[int, str]:
    """Seed one ``executing`` attempt with a decision row still ``execution_running``."""

    generation = f"generation-{raw_message_id}"
    with session_factory() as session:
        session.add(
            RecognitionDecision(
                raw_message_id=raw_message_id,
                input_kind="text",
                authoritative_model="recognition",
                authoritative_status="持仓策略",
                authoritative_payload_json="{}",
                agreement_status="pending",
                differences_json="[]",
                prompt_versions_json="{}",
                comparison_status="execution_running",
                comparison_claim_token=generation,
                comparison_started_at=_naive(NOW),
                created_at=_naive(NOW),
                updated_at=_naive(NOW),
            )
        )
        attempt = AuthoritativeExecutionAttempt(
            raw_message_id=raw_message_id,
            authoritative_generation=generation,
            status="executing",
            claim_token=f"token-{raw_message_id}",
            owner_runtime_role="worker",
            owner_instance_id="instance",
            owner_pid=1,
            owner_boot_id="boot",
            owner_process_start_ticks="1",
            claimed_at=_naive(NOW),
            heartbeat_at=_naive(NOW),
            lease_expires_at=_naive(NOW) + timedelta(minutes=5),
            side_effect_started_at=_naive(NOW),
        )
        session.add(attempt)
        session.commit()
        attempt_id = int(attempt.id)
    return attempt_id, generation


def test_the_proven_attempt_closes_no_write_not_uncertain(tmp_path):
    from telegram_kol_research.authoritative_execution_attempts import (
        record_management_preflight_refusal,
    )

    session_factory = _prepared(tmp_path)
    attempt_id, generation = _attempt_ready_for_refusal(
        session_factory, raw_message_id=4631
    )
    with session_factory() as session:
        row = session.get(AuthoritativeExecutionAttempt, attempt_id)
        claim_token = str(row.claim_token)

    ok = record_management_preflight_refusal(
        session_factory,
        attempt_id=attempt_id,
        claim_token=claim_token,
        reason_code="protection_rows_unattributed_on_exchange",
        evidence_refs=[
            {
                "kind": "management_batch_no_contact",
                "management_batch_id": 184,
                "leg_count": 2,
                "note": "all_legs_planned_no_request",
            }
        ],
        refused_at=NOW,
    )
    assert ok is True

    with session_factory() as session:
        attempt = session.get(AuthoritativeExecutionAttempt, attempt_id)
        assert attempt.status == CLOSED_NO_WRITE
        assert attempt.status in CLOSEOUT_STATUSES
        assert attempt.exchange_effect == "not_started"
        assert attempt.error_class == "ManagementPreflightRefusal"
        assert "refused_before_write" in attempt.error_summary
        assert json.loads(attempt.evidence_refs_json)

        decision = (
            session.query(RecognitionDecision)
            .filter_by(raw_message_id=4631)
            .one()
        )
        assert decision.comparison_status == "completed"
        assert decision.automation_status == "failed"
        assert decision.automation_reason == "protection_rows_unattributed_on_exchange"
        # Not ``uncertain_without_write``/``authoritative_execution_uncertain``.
        assert attempt.status != "uncertain"


def test_the_cas_refuses_a_stale_claim_token(tmp_path):
    from telegram_kol_research.authoritative_execution_attempts import (
        record_management_preflight_refusal,
    )

    session_factory = _prepared(tmp_path)
    attempt_id, _generation = _attempt_ready_for_refusal(
        session_factory, raw_message_id=1
    )
    ok = record_management_preflight_refusal(
        session_factory,
        attempt_id=attempt_id,
        claim_token="wrong-token",
        reason_code="some_refusal",
        evidence_refs=[{"kind": "management_batch_no_contact", "management_batch_id": 1}],
        refused_at=NOW,
    )
    assert ok is False
    with session_factory() as session:
        attempt = session.get(AuthoritativeExecutionAttempt, attempt_id)
        assert attempt.status == "executing"


def test_r5h_a_closed_no_write_message_blocks_automatic_retry(tmp_path):
    """R5-h: neither an automatic retry nor an explicit one may proceed."""

    from telegram_kol_research.authoritative_recognition import (
        AutomaticRetryBlocked,
        _load_completed_execution_for_automatic_retry,
    )
    from telegram_kol_research.models import MessageProcessingJob

    session_factory = _prepared(tmp_path)
    attempt_id, generation = _attempt_ready_for_refusal(
        session_factory, raw_message_id=42
    )
    with session_factory() as session:
        attempt = session.get(AuthoritativeExecutionAttempt, attempt_id)
        attempt.status = CLOSED_NO_WRITE
        attempt.exchange_effect = "not_started"
        session.add(
            MessageProcessingJob(
                raw_message_id=42,
                chat_id=FAKE_CHAT_ID,
                status="claimed",
                attempt_count=1,
                last_reason="stale_claim_reclaimed",
            )
        )
        session.commit()

    import pytest

    with pytest.raises(AutomaticRetryBlocked):
        _load_completed_execution_for_automatic_retry(
            session_factory, raw_message_id=42, explicitly_retrying=False
        )
    with pytest.raises(AutomaticRetryBlocked):
        _load_completed_execution_for_automatic_retry(
            session_factory, raw_message_id=42, explicitly_retrying=True
        )
