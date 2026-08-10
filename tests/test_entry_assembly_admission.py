import json
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    EntryPreamble,
    MessageEvidenceExtractionClaim,
    MessageEvidenceVersion,
    RawMessage,
    SignalCandidate,
)


NOW = datetime(2026, 8, 8, 9, 0, tzinfo=UTC)


def _persist_strategy_and_later_claim(session_factory):
    with session_factory() as session:
        strategy = RawMessage(
            chat_id=100,
            message_id=1000,
            posted_at=NOW,
            text="BTC short 63900-64200 SL 64900",
        )
        later = RawMessage(
            chat_id=100,
            message_id=1001,
            posted_at=NOW + timedelta(seconds=1),
            text="50%仓位",
        )
        session.add_all([strategy, later])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            confidence=1,
            recognition_generation="strategy-generation",
        )
        session.add(candidate)
        session.add(
            MessageEvidenceExtractionClaim(
                raw_message_id=later.id,
                input_fingerprint="later-input",
                claim_token="later-claim",
                claimed_at=NOW,
                lease_expires_at=NOW + timedelta(minutes=5),
            )
        )
        session.commit()
        return strategy.id, candidate.id, later.id


def test_live_admission_persists_defer_and_wakes_once_on_terminal_evidence(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
        claim_ready_entry_assembly_wakeups,
    )
    from telegram_kol_research.models import EntryAssemblyAttempt

    session_factory = create_session_factory(tmp_path / "admission.db")
    strategy_id, candidate_id, later_id = _persist_strategy_and_later_claim(
        session_factory
    )

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )

    assert decision.status == "deferred"
    assert decision.reason_code == "adjacent_entry_context_pending"
    with session_factory() as session:
        attempt = session.query(EntryAssemblyAttempt).one()
        assert json.loads(attempt.blocking_raw_message_ids_json) == [later_id]

        session.query(MessageEvidenceExtractionClaim).filter_by(
            raw_message_id=later_id
        ).delete()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=later_id,
                version=1,
                input_fingerprint="later-input",
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json='{"recognition_result":"非策略","strategy":{},"lifecycle_event":{"event_type":"none"}}',
            )
        )
        session.commit()

    first = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=later_id,
        now=NOW + timedelta(seconds=3),
    )
    repeated = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=later_id,
        now=NOW + timedelta(seconds=4),
    )

    assert tuple(item.strategy_raw_message_id for item in first) == (strategy_id,)
    assert repeated == ()


def test_final_wakeup_releases_matching_adjacent_deferred_entry_item(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
        claim_ready_entry_assembly_wakeups,
    )
    from telegram_kol_research.message_instruction_items import (
        claim_next_message_instruction_item,
        create_message_instruction_items_in_session,
    )
    from telegram_kol_research.models import MessageInstructionItem

    session_factory = create_session_factory(tmp_path / "wakeup-item-release.db")
    strategy_id, candidate_id, later_id = _persist_strategy_and_later_claim(
        session_factory
    )
    assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )

    with session_factory() as session:
        create_message_instruction_items_in_session(
            session,
            raw_message_id=strategy_id,
        )
        item = session.query(MessageInstructionItem).one()
        item.result_json = json.dumps(
            {
                "status": "deferred",
                "reason": "adjacent_entry_context_pending",
            }
        )
        item.visibility_first_failed_at = NOW + timedelta(seconds=2)
        item.visibility_retry_attempts = 1
        item.visibility_next_attempt_at = NOW + timedelta(minutes=1)
        item_id = item.id
        session.query(MessageEvidenceExtractionClaim).filter_by(
            raw_message_id=later_id
        ).delete()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=later_id,
                version=1,
                input_fingerprint="later-input",
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json=(
                    '{"recognition_result":"非策略","strategy":{},'
                    '"lifecycle_event":{"event_type":"none"}}'
                ),
            )
        )
        session.commit()

    claims = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=later_id,
        now=NOW + timedelta(seconds=3),
    )

    assert tuple(claim.strategy_raw_message_id for claim in claims) == (strategy_id,)
    with session_factory() as session:
        item = session.get(MessageInstructionItem, item_id)
        assert item is not None
        assert item.status == "pending"
        assert item.visibility_next_attempt_at is None

    claimed = claim_next_message_instruction_item(
        session_factory,
        raw_message_id=strategy_id,
        now=NOW + timedelta(seconds=3),
    )
    assert claimed is not None
    assert claimed.id == item_id


def test_shadow_records_proposal_without_deferring(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )
    from telegram_kol_research.models import EntryAssemblyAttempt

    session_factory = create_session_factory(tmp_path / "shadow-admission.db")
    strategy_id, candidate_id, _ = _persist_strategy_and_later_claim(session_factory)

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="shadow",
        assessed_at=NOW + timedelta(seconds=2),
    )

    assert decision.status == "ready"
    assert decision.proposed_status == "deferred"
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).one().status == "shadow"


def test_failed_wakeup_releases_claim_for_durable_retry(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
        claim_ready_entry_assembly_wakeups,
        finish_entry_assembly_wakeup,
    )

    session_factory = create_session_factory(tmp_path / "wakeup-retry.db")
    strategy_id, candidate_id, later_id = _persist_strategy_and_later_claim(
        session_factory
    )
    assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )

    first_claims = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=later_id,
        now=NOW + timedelta(seconds=3),
    )
    assert tuple(item.strategy_raw_message_id for item in first_claims) == (strategy_id,)
    first_claim = first_claims[0]
    finish_entry_assembly_wakeup(
        session_factory,
        attempt_id=first_claim.attempt_id,
        claim_token=first_claim.claim_token,
        succeeded=False,
        now=NOW + timedelta(seconds=4),
    )

    retried = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=later_id,
        now=NOW + timedelta(seconds=5),
    )
    assert tuple(item.strategy_raw_message_id for item in retried) == (strategy_id,)


def test_stale_wakeup_claim_is_reclaimed_on_later_worker_scan(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
        claim_ready_entry_assembly_wakeups,
    )

    session_factory = create_session_factory(tmp_path / "stale-wakeup.db")
    strategy_id, candidate_id, later_id = _persist_strategy_and_later_claim(
        session_factory
    )
    assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )
    first = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=later_id,
        now=NOW + timedelta(seconds=3),
    )
    assert len(first) == 1

    recovered = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=999999,
        now=NOW + timedelta(minutes=6),
    )

    assert len(recovered) == 1
    assert recovered[0].attempt_id == first[0].attempt_id


def test_two_blockers_are_removed_without_lost_wakeup(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
        claim_ready_entry_assembly_wakeups,
    )

    session_factory = create_session_factory(tmp_path / "two-blockers.db")
    strategy_id, candidate_id, first_later_id = _persist_strategy_and_later_claim(
        session_factory
    )
    with session_factory() as session:
        first_later = session.get(RawMessage, first_later_id)
        second_later = RawMessage(
            chat_id=first_later.chat_id,
            message_id=first_later.message_id + 1,
            posted_at=first_later.posted_at + timedelta(seconds=1),
            text="more context",
        )
        session.add(second_later)
        session.flush()
        session.add(
            MessageEvidenceExtractionClaim(
                raw_message_id=second_later.id,
                input_fingerprint="second",
                claim_token="second-claim",
                claimed_at=NOW,
                lease_expires_at=NOW + timedelta(minutes=5),
            )
        )
        second_later_id = second_later.id
        session.commit()
    assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=3),
    )

    assert claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=first_later_id,
        now=NOW + timedelta(seconds=4),
    ) == ()
    final = claim_ready_entry_assembly_wakeups(
        session_factory,
        completed_raw_message_id=second_later_id,
        now=NOW + timedelta(seconds=5),
    )

    assert tuple(item.strategy_raw_message_id for item in final) == (strategy_id,)


def test_different_chat_and_later_hard_boundary_do_not_defer(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )

    session_factory = create_session_factory(tmp_path / "boundary-admission.db")
    strategy_id, candidate_id, later_id = _persist_strategy_and_later_claim(
        session_factory
    )
    with session_factory() as session:
        session.get(RawMessage, later_id).chat_id = 200
        boundary = RawMessage(
            chat_id=100,
            message_id=1001,
            posted_at=NOW + timedelta(seconds=1),
            text="new entry",
        )
        session.add(boundary)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=boundary.id,
                symbol="ETH",
                side="long",
                event_type="entry_signal",
                parse_source="mimo_authoritative",
                confidence=1,
            )
        )
        session.commit()

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )

    assert decision.status == "ready"


def test_completed_fragment_evidence_without_durable_fragment_stays_deferred(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )

    session_factory = create_session_factory(tmp_path / "fragment-apply-gap.db")
    strategy_id, candidate_id, later_id = _persist_strategy_and_later_claim(
        session_factory
    )
    with session_factory() as session:
        session.query(MessageEvidenceExtractionClaim).filter_by(
            raw_message_id=later_id
        ).delete()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=later_id,
                version=1,
                input_fingerprint="later-input",
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json='{"recognition_result":"非策略","strategy":{},"lifecycle_event":{"event_type":"none"},"entry_fragments":[{"kind":"risk_multiplier","symbol":"BTC","side":"short","risk_multiplier":"0.5","confidence":1,"reason":"50%"}]}',
            )
        )
        session.commit()

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )

    assert decision.status == "deferred"


def test_completed_strategy_evidence_without_candidate_stays_deferred(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )

    session_factory = create_session_factory(tmp_path / "candidate-apply-gap.db")
    strategy_id, candidate_id, later_id = _persist_strategy_and_later_claim(
        session_factory
    )
    with session_factory() as session:
        session.query(MessageEvidenceExtractionClaim).filter_by(
            raw_message_id=later_id
        ).delete()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=later_id,
                version=1,
                input_fingerprint="later-input",
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json='{"recognition_result":"是策略","strategy":{"symbol":"ETH","side":"long"},"lifecycle_event":{"event_type":"none"}}',
            )
        )
        session.commit()

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )

    assert decision.status == "deferred"


def test_completed_lifecycle_evidence_without_candidate_stays_deferred(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )

    session_factory = create_session_factory(tmp_path / "lifecycle-apply-gap.db")
    strategy_id, candidate_id, later_id = _persist_strategy_and_later_claim(
        session_factory
    )
    with session_factory() as session:
        session.query(MessageEvidenceExtractionClaim).filter_by(
            raw_message_id=later_id
        ).delete()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=later_id,
                version=1,
                input_fingerprint="later-input",
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json=(
                    '{"recognition_result":"非策略","strategy":null,'
                    '"lifecycle_event":{"event_type":"cancel_entry"}}'
                ),
            )
        )
        session.commit()

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )

    assert decision.status == "deferred"


def test_completed_malformed_normalized_evidence_stays_deferred(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )

    session_factory = create_session_factory(tmp_path / "malformed-evidence.db")
    strategy_id, candidate_id, later_id = _persist_strategy_and_later_claim(
        session_factory
    )
    with session_factory() as session:
        session.query(MessageEvidenceExtractionClaim).filter_by(
            raw_message_id=later_id
        ).delete()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=later_id,
                version=1,
                input_fingerprint="later-input",
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json="{not-json",
            )
        )
        session.commit()

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )

    assert decision.status == "deferred"


def test_completed_non_strategy_placeholder_is_not_treated_as_pending_action(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )
    from telegram_kol_research.models import EntryAssemblyAttempt

    session_factory = create_session_factory(tmp_path / "null-strategy-placeholder.db")
    strategy_id, candidate_id, later_id = _persist_strategy_and_later_claim(
        session_factory
    )
    with session_factory() as session:
        session.query(MessageEvidenceExtractionClaim).filter_by(
            raw_message_id=later_id
        ).delete()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=later_id,
                version=1,
                input_fingerprint="later-input",
                model="mimo",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=1,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json=(
                    '{"recognition_result":"非策略","strategy":'
                    '{"entry":null,"leverage":null,"order_type":null,'
                    '"side":null,"stop_loss":null,"symbol":null,'
                    '"take_profit":null},'
                    '"lifecycle_event":{"event_type":"none"}}'
                ),
            )
        )
        session.commit()

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=strategy_id,
        signal_candidate_id=candidate_id,
        mode="live",
        assessed_at=NOW + timedelta(seconds=2),
    )

    assert decision.status == "ready"
    assert decision.reason_code is None
    with session_factory() as session:
        assert session.query(EntryAssemblyAttempt).count() == 0


def test_preamble_outside_adjacent_time_window_is_not_selected(tmp_path):
    from telegram_kol_research.entry_assembly_admission import (
        assess_entry_assembly_admission,
    )

    session_factory = create_session_factory(tmp_path / "expired-adjacency.db")
    with session_factory() as session:
        old = RawMessage(
            chat_id=100,
            message_id=900,
            posted_at=NOW - timedelta(minutes=31),
            text="half",
        )
        strategy = RawMessage(
            chat_id=100,
            message_id=1000,
            posted_at=NOW,
            text="entry",
        )
        session.add_all([old, strategy])
        session.flush()
        evidence = MessageEvidenceVersion(
            raw_message_id=old.id,
            version=1,
            input_fingerprint="old",
            model="mimo",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=1,
            text_evidence_json="{}",
            image_evidence_json="{}",
            normalized_evidence_json="{}",
        )
        session.add(evidence)
        session.flush()
        session.add(
            EntryPreamble(
                raw_message_id=old.id,
                chat_id=100,
                message_id=900,
                symbol="BTC",
                side="short",
                risk_multiplier="0.5",
                evidence_version_id=evidence.id,
                recognition_generation="old",
                fingerprint="9" * 64,
                status="pending",
                reason="half",
                created_at=old.posted_at,
                updated_at=old.posted_at,
            )
        )
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            confidence=1,
        )
        session.add(candidate)
        session.flush()
        ids = strategy.id, candidate.id
        session.commit()

    decision = assess_entry_assembly_admission(
        session_factory,
        strategy_raw_message_id=ids[0],
        signal_candidate_id=ids[1],
        mode="live",
        assessed_at=NOW,
    )

    assert decision.status == "ready"
    assert decision.selection.risk_multiplier == 1
    assert decision.selection.legacy_preamble_ids == ()
