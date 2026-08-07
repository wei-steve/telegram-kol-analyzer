from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.util import find_spec

import pytest

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_preambles import persist_entry_preamble_in_session
from telegram_kol_research.message_evidence import EntryPreambleEvidence
from telegram_kol_research.models import (
    EntryAssemblyFragment,
    EntryPreamble,
    EntryStrategyAssembly,
    EntryStrategyFragment,
    MessageEvidenceVersion,
    MessageEvidenceExtractionClaim,
    RawMessage,
    SignalCandidate,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def test_entry_strategy_assembly_module_exists():
    assert find_spec("telegram_kol_research.entry_strategy_assembly") is not None


def _fact(
    *,
    raw_id: int,
    message_id: int,
    kind: str,
    symbol: str | None = None,
    side: str | None = None,
    preamble_id: int | None = None,
    multiplier: str = "1",
):
    from telegram_kol_research.entry_strategy_assembly import PriorMessageFact

    return PriorMessageFact(
        raw_message_id=raw_id,
        message_id=message_id,
        posted_at=NOW + timedelta(seconds=raw_id),
        kind=kind,
        symbol=symbol,
        side=side,
        preamble_id=preamble_id,
        risk_multiplier=Decimal(multiplier),
    )


def test_selector_matches_same_chat_symbol_and_side_across_unrelated_message():
    from telegram_kol_research.entry_strategy_assembly import select_entry_preamble

    decision = select_entry_preamble(
        strategy_posted_at=NOW + timedelta(minutes=5),
        strategy_message_id=9902,
        strategy_raw_message_id=102,
        symbol="BTC",
        side="short",
        prior_facts=[
            _fact(
                raw_id=100,
                message_id=9901,
                kind="entry_preamble",
                symbol="BTC",
                side="short",
                preamble_id=7,
                multiplier="0.5",
            ),
            _fact(raw_id=101, message_id=99015, kind="unrelated"),
        ],
    )

    assert decision.status == "ready"
    assert decision.preamble_id == 7
    assert decision.risk_multiplier == Decimal("0.5")


def test_legacy_preamble_fact_adapts_to_v2_risk_fragment():
    from telegram_kol_research.entry_strategy_assembly import (
        adapt_prior_fact_to_adjacent_entry_fact,
    )

    adapted = adapt_prior_fact_to_adjacent_entry_fact(
        _fact(
            raw_id=100,
            message_id=9901,
            kind="entry_preamble",
            symbol="BTC",
            side="short",
            preamble_id=7,
            multiplier="0.5",
        )
    )

    assert adapted.kind == "fragment"
    assert adapted.fragment_id == -7
    assert adapted.fragment_kind == "risk_multiplier"
    assert adapted.payload == {"risk_multiplier": "0.5"}


def test_selector_does_not_cross_mismatch_or_hard_boundary():
    from telegram_kol_research.entry_strategy_assembly import select_entry_preamble

    mismatch = select_entry_preamble(
        strategy_posted_at=NOW + timedelta(minutes=5),
        strategy_message_id=9902,
        strategy_raw_message_id=102,
        symbol="BTC",
        side="short",
        prior_facts=[
            _fact(
                raw_id=100,
                message_id=9901,
                kind="entry_preamble",
                symbol="ETH",
                side="short",
                preamble_id=7,
                multiplier="0.5",
            )
        ],
    )
    bounded = select_entry_preamble(
        strategy_posted_at=NOW + timedelta(minutes=5),
        strategy_message_id=9902,
        strategy_raw_message_id=102,
        symbol="BTC",
        side="short",
        prior_facts=[
            _fact(
                raw_id=100,
                message_id=9900,
                kind="entry_preamble",
                symbol="BTC",
                side="short",
                preamble_id=7,
                multiplier="0.5",
            ),
            _fact(
                raw_id=101,
                message_id=9901,
                kind="complete_entry",
                symbol="BTC",
                side="short",
            ),
        ],
    )

    assert mismatch.status == "none"
    assert mismatch.risk_multiplier == Decimal("1")
    assert bounded.status == "none"


def test_selector_blocks_ambiguous_preambles_and_defers_unresolved_predecessor():
    from telegram_kol_research.entry_strategy_assembly import select_entry_preamble

    common = dict(
        strategy_posted_at=NOW + timedelta(minutes=5),
        strategy_message_id=9903,
        strategy_raw_message_id=103,
        symbol="BTC",
        side="short",
    )
    ambiguous = select_entry_preamble(
        **common,
        prior_facts=[
            _fact(raw_id=100, message_id=9901, kind="entry_preamble", symbol="BTC", side="short", preamble_id=7, multiplier="0.5"),
            _fact(raw_id=101, message_id=9902, kind="entry_preamble", symbol="BTC", side="short", preamble_id=8, multiplier="0.3"),
        ],
    )
    unresolved = select_entry_preamble(
        **common,
        prior_facts=[
            _fact(raw_id=100, message_id=9901, kind="entry_preamble", symbol="BTC", side="short", preamble_id=7, multiplier="0.5"),
            _fact(raw_id=101, message_id=9902, kind="unresolved"),
        ],
    )

    assert ambiguous.status == "blocked"
    assert ambiguous.reason_code == "entry_preamble_ambiguous"
    assert unresolved.status == "unresolved"
    assert unresolved.reason_code == "preceding_entry_context_unresolved"


def test_selector_hard_boundary_clears_older_unresolved_fact():
    from telegram_kol_research.entry_strategy_assembly import select_entry_preamble

    decision = select_entry_preamble(
        strategy_posted_at=NOW + timedelta(minutes=5),
        strategy_message_id=9903,
        strategy_raw_message_id=103,
        symbol="BTC",
        side="short",
        prior_facts=[
            _fact(raw_id=99, message_id=9899, kind="unresolved"),
            _fact(raw_id=100, message_id=9900, kind="complete_entry"),
            _fact(raw_id=101, message_id=9901, kind="entry_preamble", symbol="BTC", side="short", preamble_id=7, multiplier="0.5"),
        ],
    )

    assert decision.status == "ready"
    assert decision.preamble_id == 7


def _persist_pair(session_factory):
    with session_factory() as session:
        preamble_message = RawMessage(
            chat_id=-1002337721508,
            message_id=9901,
            posted_at=NOW,
            text="BTC做空，半仓操作",
            archived_target_group=True,
        )
        strategy_message = RawMessage(
            chat_id=-1002337721508,
            message_id=9902,
            posted_at=NOW + timedelta(minutes=1),
            text="BTC做空 63900-64200 止损64900 止盈62800",
            archived_target_group=True,
        )
        session.add_all([preamble_message, strategy_message])
        session.flush()
        evidence = MessageEvidenceVersion(
            raw_message_id=preamble_message.id,
            version=1,
            input_fingerprint="sha256:preamble",
            model="mimo-v2.5",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=0.96,
            text_evidence_json="{}",
            image_evidence_json='{"images":[]}',
            normalized_evidence_json="{}",
        )
        session.add(evidence)
        session.flush()
        preamble = persist_entry_preamble_in_session(
            session,
            raw_message=preamble_message,
            evidence_version_id=evidence.id,
            recognition_generation="generation-1",
            evidence=EntryPreambleEvidence(
                symbol="BTC",
                side="short",
                risk_multiplier=Decimal("0.5"),
                confidence=0.96,
                reason="半仓操作",
            ),
            now=NOW,
        )
        candidate = SignalCandidate(
            raw_message_id=strategy_message.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            confidence=0.95,
        )
        session.add(candidate)
        session.flush()
        ids = strategy_message.id, candidate.id, preamble.id
        session.commit()
        return ids


def test_live_assembly_atomically_consumes_once_and_reuses_result(tmp_path):
    from telegram_kol_research.entry_strategy_assembly import assemble_entry_strategy

    session_factory = create_session_factory(tmp_path / "assembly.db")
    strategy_raw_id, candidate_id, preamble_id = _persist_pair(session_factory)
    kwargs = dict(
        strategy_raw_message_id=strategy_raw_id,
        signal_candidate_id=candidate_id,
        strategy_instance_id="deepcoin:-1002337721508:9902:BTC:short",
        mode="live",
        assembled_at=NOW + timedelta(minutes=2),
    )

    first = assemble_entry_strategy(session_factory, **kwargs)
    repeated = assemble_entry_strategy(session_factory, **kwargs)

    assert first.effective_risk_multiplier == Decimal("0.5")
    assert repeated.assembly_id == first.assembly_id
    with session_factory() as session:
        assert session.get(EntryPreamble, preamble_id).status == "consumed"
        assert session.query(EntryStrategyAssembly).count() == 1


def test_non_live_retry_blocks_existing_live_assembly(tmp_path):
    from telegram_kol_research.entry_strategy_assembly import assemble_entry_strategy

    session_factory = create_session_factory(tmp_path / "disabled-retry.db")
    strategy_raw_id, candidate_id, _ = _persist_pair(session_factory)
    assemble_entry_strategy(
        session_factory,
        strategy_raw_message_id=strategy_raw_id,
        signal_candidate_id=candidate_id,
        strategy_instance_id="crash-before-binding",
        mode="live",
        assembled_at=NOW + timedelta(minutes=2),
    )

    for mode in ("disabled", "shadow"):
        retry = assemble_entry_strategy(
            session_factory,
            strategy_raw_message_id=strategy_raw_id,
            signal_candidate_id=candidate_id,
            strategy_instance_id="crash-before-binding",
            mode=mode,
            assembled_at=NOW + timedelta(minutes=3),
        )

        assert retry.status == "blocked"
        assert retry.reason_code == "existing_entry_assembly_not_live_authorized"
        assert retry.effective_risk_multiplier == Decimal("1")


def test_shadow_reports_proposal_without_consuming_or_changing_effective_risk(tmp_path):
    from telegram_kol_research.entry_strategy_assembly import assemble_entry_strategy

    session_factory = create_session_factory(tmp_path / "shadow.db")
    strategy_raw_id, candidate_id, preamble_id = _persist_pair(session_factory)

    result = assemble_entry_strategy(
        session_factory,
        strategy_raw_message_id=strategy_raw_id,
        signal_candidate_id=candidate_id,
        strategy_instance_id="deepcoin:-1002337721508:9902:BTC:short",
        mode="shadow",
        assembled_at=NOW + timedelta(minutes=2),
    )

    assert result.proposed_risk_multiplier == Decimal("0.5")
    assert result.effective_risk_multiplier == Decimal("1")
    with session_factory() as session:
        assert session.get(EntryPreamble, preamble_id).status == "pending"
        assert session.query(EntryStrategyAssembly).count() == 0


def test_expired_extraction_claim_does_not_defer_assembly(tmp_path):
    from telegram_kol_research.entry_strategy_assembly import assemble_entry_strategy

    session_factory = create_session_factory(tmp_path / "expired-claim.db")
    strategy_raw_id, candidate_id, _ = _persist_pair(session_factory)
    with session_factory() as session:
        earlier = RawMessage(
            chat_id=-1002337721508,
            message_id=9899,
            posted_at=NOW - timedelta(minutes=1),
            text="old",
        )
        session.add(earlier)
        session.flush()
        session.add(
            MessageEvidenceExtractionClaim(
                raw_message_id=earlier.id,
                input_fingerprint="expired",
                claim_token="expired-claim",
                claimed_at=NOW - timedelta(minutes=2),
                lease_expires_at=NOW - timedelta(seconds=1),
            )
        )
        session.add(
            MessageEvidenceVersion(
                raw_message_id=earlier.id,
                version=1,
                input_fingerprint="expired",
                model="mimo-v2.5",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=0.9,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json='{"recognition_result":"非策略","strategy":{},"lifecycle_event":{"event_type":"none"}}',
            )
        )
        session.commit()

    result = assemble_entry_strategy(
        session_factory,
        strategy_raw_message_id=strategy_raw_id,
        signal_candidate_id=candidate_id,
        strategy_instance_id="expired-claim-strategy",
        mode="shadow",
        assembled_at=NOW + timedelta(minutes=2),
    )

    assert result.status == "proposed"


def test_unclaimed_prior_message_defers_following_entry(tmp_path):
    from telegram_kol_research.entry_strategy_assembly import assemble_entry_strategy

    session_factory = create_session_factory(tmp_path / "queued-prior.db")
    strategy_raw_id, candidate_id, _ = _persist_pair(session_factory)
    with session_factory() as session:
        queued = RawMessage(
            chat_id=-1002337721508,
            message_id=99015,
            posted_at=NOW + timedelta(seconds=30),
            text="queued before claim",
        )
        session.add(queued)
        session.commit()

    result = assemble_entry_strategy(
        session_factory,
        strategy_raw_message_id=strategy_raw_id,
        signal_candidate_id=candidate_id,
        strategy_instance_id="queued-prior",
        mode="live",
        assembled_at=NOW + timedelta(minutes=2),
    )

    assert result.status == "unresolved"
    assert result.reason_code == "preceding_entry_context_unresolved"


def test_completed_preamble_evidence_without_persisted_row_defers_entry(tmp_path):
    from telegram_kol_research.entry_strategy_assembly import assemble_entry_strategy

    session_factory = create_session_factory(tmp_path / "release-gap.db")
    strategy_raw_id, candidate_id, _ = _persist_pair(session_factory)
    with session_factory() as session:
        gap = RawMessage(
            chat_id=-1002337721508,
            message_id=99015,
            posted_at=NOW + timedelta(seconds=30),
            text="BTC short half",
        )
        session.add(gap)
        session.flush()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=gap.id,
                version=1,
                input_fingerprint="release-gap",
                model="mimo-v2.5",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=0.96,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json='{"recognition_result":"非策略","strategy":{},"lifecycle_event":{"event_type":"none"},"entry_context":{"kind":"entry_preamble","symbol":"BTC","side":"short","risk_multiplier":"0.5","confidence":0.96,"reason":"half"}}',
            )
        )
        session.commit()

    result = assemble_entry_strategy(
        session_factory,
        strategy_raw_message_id=strategy_raw_id,
        signal_candidate_id=candidate_id,
        strategy_instance_id="release-gap",
        mode="live",
        assembled_at=NOW + timedelta(minutes=2),
    )

    assert result.status == "unresolved"


@pytest.mark.parametrize(
    "normalized_json",
    [
        '{"strategy":{"symbol":"BTC","side":"short"},"lifecycle_event":{"event_type":"none"}}',
        '{"strategy":{},"lifecycle_event":{"event_type":"position_update"}}',
        "{malformed",
    ],
)
def test_incomplete_authoritative_application_gap_defers_entry(tmp_path, normalized_json):
    from telegram_kol_research.entry_strategy_assembly import assemble_entry_strategy

    session_factory = create_session_factory(tmp_path / "application-gap.db")
    strategy_raw_id, candidate_id, _ = _persist_pair(session_factory)
    with session_factory() as session:
        gap = RawMessage(
            chat_id=-1002337721508,
            message_id=99015,
            posted_at=NOW + timedelta(seconds=30),
            text="pending authoritative application",
        )
        session.add(gap)
        session.flush()
        session.add(
            MessageEvidenceVersion(
                raw_message_id=gap.id,
                version=1,
                input_fingerprint="application-gap",
                model="mimo-v2.5",
                prompt_versions_json="{}",
                extraction_status="completed",
                confidence=0.9,
                text_evidence_json="{}",
                image_evidence_json="{}",
                normalized_evidence_json=normalized_json,
            )
        )
        session.commit()

    result = assemble_entry_strategy(
        session_factory,
        strategy_raw_message_id=strategy_raw_id,
        signal_candidate_id=candidate_id,
        strategy_instance_id=f"gap-{hash(normalized_json)}",
        mode="live",
        assembled_at=NOW + timedelta(minutes=2),
    )

    assert result.status == "unresolved"


def test_live_assembly_fingerprint_is_unique_across_chats_with_same_message_ids(tmp_path):
    from telegram_kol_research.entry_strategy_assembly import assemble_entry_strategy

    session_factory = create_session_factory(tmp_path / "cross-chat.db")
    pairs = []
    for offset, chat_id in enumerate((-1001, -1002), start=1):
        with session_factory() as session:
            preamble_raw = RawMessage(chat_id=chat_id, message_id=9901, posted_at=NOW, text="half")
            strategy_raw = RawMessage(chat_id=chat_id, message_id=9902, posted_at=NOW + timedelta(minutes=1), text="entry")
            session.add_all([preamble_raw, strategy_raw])
            session.flush()
            evidence = MessageEvidenceVersion(raw_message_id=preamble_raw.id, version=1, input_fingerprint=f"fp-{offset}", model="mimo", prompt_versions_json="{}", extraction_status="completed", confidence=1, text_evidence_json="{}", image_evidence_json="{}", normalized_evidence_json="{}")
            session.add(evidence)
            session.flush()
            persist_entry_preamble_in_session(session, raw_message=preamble_raw, evidence_version_id=evidence.id, recognition_generation="g1", evidence=EntryPreambleEvidence(symbol="BTC", side="short", risk_multiplier=Decimal("0.5"), confidence=1, reason="half"), now=NOW)
            candidate = SignalCandidate(raw_message_id=strategy_raw.id, symbol="BTC", side="short", event_type="entry_signal", parse_source="mimo", confidence=1)
            session.add(candidate)
            session.flush()
            pairs.append((strategy_raw.id, candidate.id, chat_id))
            session.commit()

    results = [
        assemble_entry_strategy(session_factory, strategy_raw_message_id=raw_id, signal_candidate_id=candidate_id, strategy_instance_id=f"strategy-{chat_id}", mode="live", assembled_at=NOW + timedelta(minutes=2))
        for raw_id, candidate_id, chat_id in pairs
    ]

    assert len({result.assembly_fingerprint for result in results}) == 2


def test_v2_live_assembly_atomically_consumes_multiple_fragments(tmp_path):
    from telegram_kol_research.entry_strategy_assembly import (
        assemble_adjacent_entry_strategy,
    )

    session_factory = create_session_factory(tmp_path / "v2-multi.db")
    with session_factory() as session:
        before = RawMessage(chat_id=100, message_id=1000, posted_at=NOW, text="half")
        strategy = RawMessage(
            chat_id=100,
            message_id=1001,
            posted_at=NOW + timedelta(seconds=1),
            text="BTC short 63900-64200 SL 64900",
        )
        after = RawMessage(
            chat_id=100,
            message_id=1002,
            posted_at=NOW + timedelta(seconds=2),
            text="supplement 63400",
        )
        session.add_all([before, strategy, after])
        session.flush()
        evidence_before = MessageEvidenceVersion(
            raw_message_id=before.id,
            version=1,
            input_fingerprint="before",
            model="mimo",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=1,
            text_evidence_json="{}",
            image_evidence_json="{}",
            normalized_evidence_json="{}",
        )
        evidence_after = MessageEvidenceVersion(
            raw_message_id=after.id,
            version=1,
            input_fingerprint="after",
            model="mimo",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=1,
            text_evidence_json="{}",
            image_evidence_json="{}",
            normalized_evidence_json="{}",
        )
        session.add_all([evidence_before, evidence_after])
        session.flush()
        risk = EntryStrategyFragment(
            raw_message_id=before.id,
            chat_id=100,
            message_id=1000,
            symbol="BTC",
            side="short",
            fragment_kind="risk_multiplier",
            payload_json='{"risk_multiplier":"0.5"}',
            evidence_version_id=evidence_before.id,
            recognition_generation="g-before",
            source_relationship="unresolved",
            status="pending",
            reason="half",
            fingerprint="1" * 64,
            created_at=NOW,
            updated_at=NOW,
        )
        legacy_preamble = EntryPreamble(
            raw_message_id=before.id,
            chat_id=100,
            message_id=1000,
            symbol="BTC",
            side="short",
            risk_multiplier="0.5",
            evidence_version_id=evidence_before.id,
            recognition_generation="g-before",
            fingerprint="3" * 64,
            status="pending",
            reason="half",
            created_at=NOW,
            updated_at=NOW,
        )
        supplemental = EntryStrategyFragment(
            raw_message_id=after.id,
            chat_id=100,
            message_id=1002,
            symbol="BTC",
            side="short",
            fragment_kind="supplemental_entry",
            payload_json='{"prices":["63400"]}',
            evidence_version_id=evidence_after.id,
            recognition_generation="g-after",
            source_relationship="unresolved",
            status="pending",
            reason="supplement",
            fingerprint="2" * 64,
            created_at=NOW,
            updated_at=NOW,
        )
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="short",
            event_type="entry_signal",
            parse_source="mimo_authoritative",
            confidence=1,
        )
        session.add_all([risk, supplemental, legacy_preamble, candidate])
        session.flush()
        ids = strategy.id, candidate.id, risk.id, supplemental.id, legacy_preamble.id
        session.commit()

    kwargs = dict(
        strategy_raw_message_id=ids[0],
        signal_candidate_id=ids[1],
        strategy_instance_id="deepcoin:100:1001:BTC:short",
        mode="live",
        assembled_at=NOW + timedelta(seconds=3),
    )
    first = assemble_adjacent_entry_strategy(session_factory, **kwargs)
    repeated = assemble_adjacent_entry_strategy(session_factory, **kwargs)

    assert first.fragment_ids == (ids[2], ids[3])
    assert first.effective_risk_multiplier == Decimal("0.5")
    assert first.supplemental_prices == (Decimal("63400"),)
    assert repeated.assembly_id == first.assembly_id
    with session_factory() as session:
        assert session.query(EntryStrategyAssembly).count() == 1
        assert session.query(EntryAssemblyFragment).count() == 2
        assert {
            session.get(EntryStrategyFragment, ids[2]).status,
            session.get(EntryStrategyFragment, ids[3]).status,
        } == {"consumed"}
        assert session.get(EntryPreamble, ids[4]).status == "consumed"
