import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import telegram_kol_research.entry_admission_reconciler as reconciler_module

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_admission_reconciler import (
    reconcile_due_entry_admissions,
)
from telegram_kol_research.entry_assembly_admission import (
    assess_entry_assembly_admission,
)
from telegram_kol_research.instruction_execution_contracts import (
    transition_instruction_execution_contract,
)
from telegram_kol_research.models import (
    EntryAssemblyAttempt,
    InstructionExecutionContract,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
)


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _persist_deferred_entry(
    session_factory,
    *,
    seed: int = 0,
    contract_state: str | None = "deferred",
):
    with session_factory() as session:
        strategy = RawMessage(
            chat_id=100 + seed,
            message_id=1000 + (seed * 10),
            posted_at=NOW,
            text="BTC long strategy",
        )
        blocker = RawMessage(
            chat_id=100 + seed,
            message_id=1001 + (seed * 10),
            posted_at=NOW + timedelta(seconds=1),
            text="later structured context",
        )
        session.add_all([strategy, blocker])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            recognition_generation="generation-1",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=strategy.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind="entry",
            idempotency_key=f"{seed + 1:064x}",
            status="pending",
        )
        session.add(item)
        session.add(
            MessageEvidenceExtractionClaim(
                raw_message_id=blocker.id,
                input_fingerprint=f"blocker-input-{seed}",
                claim_token=f"blocker-claim-{seed}",
                claimed_at=NOW,
                lease_expires_at=NOW + timedelta(minutes=5),
            )
        )
        session.commit()
        ids = strategy.id, candidate.id, blocker.id, item.id

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=ids[0],
        signal_candidate_id=ids[1],
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )
    assert decision.status == "deferred"
    with session_factory() as session:
        item = session.get(MessageInstructionItem, ids[3])
        item.result_json = json.dumps(
            {
                "status": "deferred",
                "reason": "adjacent_entry_context_pending",
            }
        )
        item.visibility_next_attempt_at = NOW + timedelta(minutes=1)
        if contract_state is not None:
            session.add(
                InstructionExecutionContract(
                    message_instruction_item_id=item.id,
                    raw_message_id=ids[0],
                    signal_candidate_id=ids[1],
                    intent_kind="entry",
                    state=contract_state,
                    state_version=1,
                    attempted_exchange_write=contract_state == "submit_unknown",
                    deadline_at=decision.deadline_at,
                )
            )
        session.commit()
    return ids


def _complete_blocker(session_factory, blocker_id):
    with session_factory() as session:
        claim = session.get(MessageEvidenceExtractionClaim, blocker_id)
        input_fingerprint = claim.input_fingerprint
        session.delete(claim)
        session.add(
            MessageEvidenceVersion(
                raw_message_id=blocker_id,
                version=1,
                input_fingerprint=input_fingerprint,
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json=(
                    '{"recognition_result":"非策略","strategy":null,'
                    '"lifecycle_event":{"event_type":"none"}}'
                ),
            )
        )
        session.commit()


def test_lost_wakeup_releases_only_exact_item_without_exchange_call(tmp_path):
    session_factory = create_session_factory(tmp_path / "lost-wakeup.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    with session_factory() as session:
        unrelated_raw = RawMessage(chat_id=200, message_id=80, text="unrelated")
        session.add(unrelated_raw)
        session.flush()
        unrelated_candidate = SignalCandidate(
            raw_message_id=unrelated_raw.id,
            symbol="ETH",
            side="short",
            event_type="entry_signal",
        )
        session.add(unrelated_candidate)
        session.flush()
        unrelated = MessageInstructionItem(
            raw_message_id=unrelated_raw.id,
            signal_candidate_id=unrelated_candidate.id,
            sequence=0,
            instruction_kind="entry",
            idempotency_key="b" * 64,
            status="pending",
            visibility_next_attempt_at=NOW + timedelta(minutes=2),
        )
        session.add(unrelated)
        session.commit()
        unrelated_id = unrelated.id
    _complete_blocker(session_factory, blocker_id)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        limit=10,
        execution_contract_mode="live",
    )

    assert result.released == 1
    with session_factory() as session:
        assert session.get(MessageInstructionItem, item_id).visibility_next_attempt_at is None
        assert session.query(EntryAssemblyAttempt).one().status == "woken"
        assert session.get(MessageInstructionItem, unrelated_id).visibility_next_attempt_at == (
            NOW + timedelta(minutes=2)
        ).replace(tzinfo=None)


def test_disabled_mode_never_releases_historical_deferred_item(tmp_path):
    session_factory = create_session_factory(tmp_path / "disabled-historical.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
    )

    assert result.released == 0
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "pending"
        assert session.get(MessageInstructionItem, item_id).visibility_next_attempt_at == (
            NOW + timedelta(minutes=1)
        ).replace(tzinfo=None)


def test_live_mode_does_not_release_item_at_future_watermark(tmp_path):
    session_factory = create_session_factory(tmp_path / "live-watermark.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
        entry_after_item_id=item_id,
    )

    assert result.released == 0
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "pending"
        assert session.get(MessageInstructionItem, item_id).visibility_next_attempt_at == (
            NOW + timedelta(minutes=1)
        ).replace(tzinfo=None)


def test_historical_pending_attempt_cannot_starve_future_live_item(tmp_path):
    session_factory = create_session_factory(tmp_path / "live-starvation.db")
    _, _, old_blocker_id, old_item_id = _persist_deferred_entry(
        session_factory,
        seed=0,
        contract_state=None,
    )
    _, _, blocker_id, item_id = _persist_deferred_entry(
        session_factory,
        seed=1,
    )
    _complete_blocker(session_factory, old_blocker_id)
    _complete_blocker(session_factory, blocker_id)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        limit=1,
        execution_contract_mode="live",
        entry_after_item_id=old_item_id,
    )

    assert result.released == 1
    with session_factory() as session:
        attempts = session.query(EntryAssemblyAttempt).order_by(
            EntryAssemblyAttempt.id
        ).all()
        assert attempts[0].status == "pending"
        assert attempts[1].status == "woken"


def test_failed_future_item_cannot_starve_next_live_deferred_item(tmp_path):
    session_factory = create_session_factory(tmp_path / "failed-starvation.db")
    _, _, failed_blocker_id, _ = _persist_deferred_entry(
        session_factory,
        seed=0,
    )
    _, _, blocker_id, _ = _persist_deferred_entry(
        session_factory,
        seed=1,
    )
    _complete_blocker(session_factory, failed_blocker_id)
    _complete_blocker(session_factory, blocker_id)
    with session_factory() as session:
        first_item = session.query(MessageInstructionItem).order_by(
            MessageInstructionItem.id
        ).first()
        first_item.status = "failed"
        session.commit()

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        limit=1,
        execution_contract_mode="live",
        entry_after_item_id=0,
    )

    assert result.released == 1
    with session_factory() as session:
        attempts = session.query(EntryAssemblyAttempt).order_by(
            EntryAssemblyAttempt.id
        ).all()
        assert attempts[0].status == "pending"
        assert attempts[1].status == "woken"


def test_malformed_deferred_item_is_expired_instead_of_occupying_batch(tmp_path):
    session_factory = create_session_factory(tmp_path / "malformed-deferred.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        item.result_json = '{"status":"pending","reason":"other"}'
        session.commit()

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )

    assert result.expired == 1
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "expired"
        assert session.get(MessageInstructionItem, item_id).status == "failed"
        assert session.query(InstructionExecutionContract).one().state == "expired"


def test_not_yet_due_attempt_is_untouched(tmp_path):
    session_factory = create_session_factory(tmp_path / "not-due.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=4),
        execution_contract_mode="live",
    )

    assert result.released == 0
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "pending"
        assert session.get(MessageInstructionItem, item_id).visibility_next_attempt_at is not None


def test_expired_attempt_fails_closed_without_exchange_call(tmp_path):
    session_factory = create_session_factory(tmp_path / "expired.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        item.execution_deadline_at = NOW + timedelta(seconds=5)
        session.commit()

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )

    assert result.expired == 1
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item.status == "failed"
        assert json.loads(item.error_json)["reason"] == "entry_admission_deadline_expired"
        assert session.query(EntryAssemblyAttempt).one().status == "expired"


def test_submit_unknown_contract_is_excluded(tmp_path):
    session_factory = create_session_factory(tmp_path / "submit-unknown.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(
        session_factory,
        contract_state="submit_unknown",
    )
    _complete_blocker(session_factory, blocker_id)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )

    assert result == reconciler_module.EntryAdmissionReconcileResult()
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "pending"
        assert session.get(MessageInstructionItem, item_id).status == "pending"


def test_repeated_ticks_are_idempotent(tmp_path):
    session_factory = create_session_factory(tmp_path / "repeated.db")
    _, _, blocker_id, _ = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)

    first = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )
    repeated = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=20),
        execution_contract_mode="live",
    )

    assert first.released == 1
    assert repeated.released == 0
    assert repeated.expired == 0


def test_historical_succeeded_defer_is_not_replayed_or_mutated(tmp_path):
    session_factory = create_session_factory(tmp_path / "legacy-stale.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        item.status = "succeeded"
        session.commit()

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )

    assert result == reconciler_module.EntryAdmissionReconcileResult()
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "pending"
        assert session.get(MessageInstructionItem, item_id).status == "succeeded"
        assert session.query(InstructionExecutionContract).one().state == "deferred"


def test_blocked_recheck_expires_matching_execution_contract(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "blocked-contract.db")
    _persist_deferred_entry(session_factory)
    monkeypatch.setattr(
        reconciler_module,
        "assess_entry_assembly_admission",
        lambda *args, **kwargs: SimpleNamespace(status="blocked"),
    )

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )

    assert result.expired == 1
    with session_factory() as session:
        assert session.query(InstructionExecutionContract).one().state == "expired"


def test_contract_transition_race_cannot_split_item_and_attempt_truth(
    tmp_path,
    monkeypatch,
):
    session_factory = create_session_factory(tmp_path / "blocked-contract-race.db")
    _, _, _, item_id = _persist_deferred_entry(session_factory)

    def race_contract_then_block(*args, **kwargs):
        with session_factory() as session:
            contract = session.query(InstructionExecutionContract).one()
            contract_id = contract.id
            contract_version = contract.state_version
        transition_instruction_execution_contract(
            session_factory,
            contract_id=contract_id,
            expected_state="deferred",
            expected_version=contract_version,
            new_state="pending",
            reason_code="worker_claim_race",
            evidence_refs=[{"kind": "test_race"}],
            transitioned_at=NOW + timedelta(seconds=10),
        )
        return SimpleNamespace(status="blocked")

    monkeypatch.setattr(
        reconciler_module,
        "assess_entry_assembly_admission",
        race_contract_then_block,
    )

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )

    assert result.expired == 0
    with session_factory() as session:
        assert session.query(InstructionExecutionContract).one().state == "pending"
        assert session.get(MessageInstructionItem, item_id).status == "pending"
        assert session.query(EntryAssemblyAttempt).one().status == "pending"


def test_failed_release_cas_keeps_attempt_pending_for_next_tick(tmp_path, monkeypatch):
    session_factory = create_session_factory(tmp_path / "release-race.db")
    _, _, blocker_id, _ = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)
    monkeypatch.setattr(
        reconciler_module,
        "_release_adjacent_entry_visibility_delay",
        lambda *args, **kwargs: False,
    )

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="live",
    )

    assert result.released == 0
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "pending"


# --- A-3d: the retry half now runs wherever its timeout twin runs -------------
#
# ``instruction_execution_reconciliation`` expires a deferred contract whenever
# the mode is not ``disabled``, so under ``shadow`` production was timing these
# entries out while never retrying them. These tests pin the two halves to the
# same gate, and pin the alert that makes an expiry visible at all.


def test_shadow_releases_a_due_deferred_entry(tmp_path):
    session_factory = create_session_factory(tmp_path / "shadow-release.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="shadow",
    )

    assert result.released == 1
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        # Cleared, so the entry is immediately claimable again -- the same
        # state the event-driven wakeup produces, which already runs in shadow.
        assert item.visibility_next_attempt_at is None
        assert session.query(EntryAssemblyAttempt).one().status == "woken"


def test_disabled_still_releases_nothing(tmp_path):
    """The gate moved from ``live`` to ``disabled``; ``disabled`` stays inert."""

    session_factory = create_session_factory(tmp_path / "disabled-release.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)

    result = reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode="disabled",
    )

    assert result == reconciler_module.EntryAdmissionReconcileResult()
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "pending"
        assert (
            session.get(MessageInstructionItem, item_id).visibility_next_attempt_at
            is not None
        )


def _expire_under(session_factory, *, mode, item_id, reporter=None):
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        item.execution_deadline_at = NOW + timedelta(seconds=5)
        session.commit()
    return reconcile_due_entry_admissions(
        session_factory,
        now=NOW + timedelta(seconds=10),
        execution_contract_mode=mode,
        incident_reporter=reporter,
    )


def test_an_expired_entry_reports_an_incident_instead_of_dying_silently(tmp_path):
    session_factory = create_session_factory(tmp_path / "expiry-alert.db")
    raw_id, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)
    captured = []

    result = _expire_under(
        session_factory,
        mode="shadow",
        item_id=item_id,
        reporter=lambda **kwargs: captured.append(kwargs) or object(),
    )

    assert (result.expired, result.incidents) == (1, 1)
    assert len(captured) == 1
    alert = captured[0]
    assert alert["message_instruction_item_id"] == item_id
    assert alert["raw_message_id"] == raw_id
    assert alert["chat_id"] == 100
    # Read from the snapshot taken before the expiry blanked ``result_json``:
    # afterwards this reason exists nowhere else.
    assert alert["defer_reason_code"] == "adjacent_entry_context_pending"
    assert alert["deadline_at"] == NOW + timedelta(seconds=5)


def test_a_failing_alert_never_undoes_the_expiry(tmp_path):
    """The ledger change is committed first and is the durable fact."""

    session_factory = create_session_factory(tmp_path / "expiry-alert-raises.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)

    def raising(**_kwargs):
        raise RuntimeError("notification channel down")

    result = _expire_under(
        session_factory, mode="shadow", item_id=item_id, reporter=raising
    )

    assert result.expired == 1
    assert result.incidents == 0
    with session_factory() as session:
        assert session.get(MessageInstructionItem, item_id).status == "failed"
        assert session.query(EntryAssemblyAttempt).one().status == "expired"


def test_a_refused_alert_is_counted_as_not_reported(tmp_path):
    """A capture that returns ``None`` was refused; it must not read as sent."""

    session_factory = create_session_factory(tmp_path / "expiry-alert-refused.db")
    _, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)

    result = _expire_under(
        session_factory, mode="shadow", item_id=item_id, reporter=lambda **_: None
    )

    assert result.expired == 1
    assert result.incidents == 0


def test_entry_admission_expired_is_always_notified(tmp_path):
    """A whitelist edit must not be able to silence this one."""

    from telegram_kol_research.config import ALWAYS_NOTIFIED_INCIDENT_TYPES

    assert "entry_admission_expired" in ALWAYS_NOTIFIED_INCIDENT_TYPES


def test_the_default_path_writes_a_real_incident_row(tmp_path, monkeypatch):
    """Exercise the wiring production actually uses, not an injected double."""

    from telegram_kol_research.config import RuntimeIncidentConfig
    from telegram_kol_research.models import RuntimeIncident
    import telegram_kol_research.runtime_incident_adapters as adapters

    session_factory = create_session_factory(tmp_path / "expiry-alert-real.db")
    raw_id, _, blocker_id, item_id = _persist_deferred_entry(session_factory)
    _complete_blocker(session_factory, blocker_id)
    monkeypatch.setattr(
        adapters,
        "load_runtime_incident_config",
        lambda **_kwargs: RuntimeIncidentConfig(
            capture_types=frozenset({"entry_admission_expired"})
        ),
    )

    result = _expire_under(session_factory, mode="shadow", item_id=item_id)

    assert (result.expired, result.incidents) == (1, 1)
    with session_factory() as session:
        incident = (
            session.query(RuntimeIncident)
            .filter(RuntimeIncident.incident_type == "entry_admission_expired")
            .one()
        )
        assert incident.severity == "high"
        assert incident.source_kind == "message_instruction_item"
        assert incident.source_record_id == str(item_id)
        summary = json.loads(incident.redacted_summary)
        assert summary["component"] == "entry_admission"
        assert summary["reason_code"] == "adjacent_entry_context_pending"
        assert summary["raw_message_id"] == raw_id
        assert summary["chat_id"] == 100
        assert summary["operation"] == f"instruction_item_{item_id}"
        assert summary["impact"] == "entry_never_submitted"
        assert summary["deadline_at"] == "2026-08-10T12:00Z"


def test_the_deadline_never_costs_us_the_detailed_summary():
    """A refused detailed summary silently drops the group and the deadline.

    The composite form of this label tripped the opaque-secret heuristic on
    every one of these instants, which is why the deadline is its own field.
    """

    from telegram_kol_research.runtime_incident_adapters import _deadline_label, _summary
    from telegram_kol_research.runtime_incidents import (
        _validate_redacted_json_contract,
    )

    moment = datetime(2026, 1, 1, 0, 0)
    for step in range(400):
        deadline = moment + timedelta(hours=step * 8 + step)
        summary = _summary(
            component="entry_admission",
            source_status="expired",
            reason_code="adjacent_entry_context_pending",
            operation="instruction_item_5",
            raw_message_id=1,
            chat_id=-1002409877375,
            deadline_at=_deadline_label(deadline),
            impact="entry_never_submitted",
        )
        _validate_redacted_json_contract("redacted_summary", summary)
