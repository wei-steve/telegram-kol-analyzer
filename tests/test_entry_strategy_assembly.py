from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.util import find_spec

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.entry_preambles import persist_entry_preamble_in_session
from telegram_kol_research.message_evidence import EntryPreambleEvidence
from telegram_kol_research.models import (
    EntryPreamble,
    EntryStrategyAssembly,
    MessageEvidenceVersion,
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
        live_chat_ids={-1002337721508},
        assembled_at=NOW + timedelta(minutes=2),
    )

    first = assemble_entry_strategy(session_factory, **kwargs)
    repeated = assemble_entry_strategy(session_factory, **kwargs)

    assert first.effective_risk_multiplier == Decimal("0.5")
    assert repeated.assembly_id == first.assembly_id
    with session_factory() as session:
        assert session.get(EntryPreamble, preamble_id).status == "consumed"
        assert session.query(EntryStrategyAssembly).count() == 1


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
        live_chat_ids=set(),
        assembled_at=NOW + timedelta(minutes=2),
    )

    assert result.proposed_risk_multiplier == Decimal("0.5")
    assert result.effective_risk_multiplier == Decimal("1")
    with session_factory() as session:
        assert session.get(EntryPreamble, preamble_id).status == "pending"
        assert session.query(EntryStrategyAssembly).count() == 0
