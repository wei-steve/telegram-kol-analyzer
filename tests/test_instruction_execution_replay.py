from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from telegram_kol_research.adjacent_entry_assembly import (
    AdjacentEntryFact,
    EntryStrategyFact,
    select_adjacent_entry_fragments,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.deepcoin_order_builder import (
    build_deepcoin_order_draft,
    deepcoin_order_draft_fingerprint,
)
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec
from telegram_kol_research.instruction_execution_outcomes import (
    interpret_instruction_outcome,
)
from telegram_kol_research.instruction_execution_contracts import (
    load_or_create_instruction_execution_contract,
    transition_instruction_execution_contract,
)
from telegram_kol_research.instruction_execution_entry_adapter import (
    prepare_entry_submission_contract,
    project_entry_refusal_contract,
    project_entry_submission_result,
)
from telegram_kol_research.instruction_execution_reconciliation import (
    reconcile_instruction_execution_contracts,
)
from telegram_kol_research.instruction_execution_projection import (
    project_instruction_execution_contracts,
)
from telegram_kol_research.message_evidence import has_material_strategy_evidence
from telegram_kol_research.models import (
    InstructionExecutionContract,
    ExecutionBinding,
    MessageInstructionItem,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    TradeSignal,
)
from telegram_kol_research.trading_settings import save_trading_settings
from telegram_kol_research.recovery_live_submit import (
    EntrySubmissionProgress,
    _submit_recovery_signal_direct,
)
from telegram_kol_research.trade_signals import (
    load_trade_signal,
    mark_trade_signal_submitted,
)
from telegram_kol_research.execution_bindings import build_strategy_instance_id


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "instruction_execution"
    / "replay_corpus.json"
)
NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class _RecordingWriter:
    def __init__(self):
        self.client_order_ids: list[str] = []
        self.pending_tpsl: list[dict[str, object]] = []
        self.entry_rows: list[dict[str, object]] = []

    def submit(self, client_order_id: str) -> None:
        self.client_order_ids.append(str(client_order_id))

    def trigger_order(self, order_payload):
        self.submit(order_payload["clOrdId"])
        order_id = f"trigger-{len(self.client_order_ids)}"
        self.entry_rows.append(
            {
                "instId": order_payload["instId"],
                "clOrdId": order_payload["clOrdId"],
                "ordId": order_id,
                "state": "open",
            }
        )
        return {"code": "0", "data": {"ordId": order_id}}

    def place_order(self, order_payload):
        self.submit(order_payload["clOrdId"])
        self.entry_rows.append(
            {
                "instId": order_payload["instId"],
                "clOrdId": order_payload["clOrdId"],
                "ordId": f"market-{len(self.client_order_ids)}",
                "posId": "replay-position-1",
                "state": "active",
            }
        )
        return {
            "code": "0",
            "data": {
                "ordId": f"market-{len(self.client_order_ids)}",
                "posId": "replay-position-1",
            },
        }

    def list_positions(self, *, inst_id=None):
        return [row for row in self.entry_rows if row.get("posId")]

    def list_open_orders(self):
        return [row for row in self.entry_rows if not row.get("posId")]

    def list_order_history(self, *, inst_id):
        return []

    def list_trade_fills(self, *, inst_id):
        return []

    def list_trigger_order_history(self, *, inst_id):
        return []

    def read_trigger_orders_pending(self, *, inst_id):
        return {"code": "0", "data": []}

    def list_trigger_orders_pending(self, *, inst_id):
        return [row for row in self.pending_tpsl if row["instId"] == inst_id]

    def set_position_sltp(self, protection_payload):
        order_id = f"replay-sltp-{len(self.pending_tpsl) + 1}"
        self.pending_tpsl.append(
            {
                "ordId": order_id,
                "instId": protection_payload["instId"],
                "posId": protection_payload["posId"],
                "posSide": protection_payload["posSide"],
                "slTriggerPx": protection_payload.get("slTriggerPx"),
                "sz": protection_payload.get("sz", "0"),
            }
        )
        return {"code": "0", "data": {"ordId": order_id}}


def _cases() -> list[dict[str, object]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    return payload["cases"]


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["case_id"])
def test_redacted_instruction_execution_replay_corpus(case, tmp_path):
    kind = case["kind"]
    if kind == "adjacent_context":
        _assert_adjacent_context_case(case, tmp_path)
    elif kind == "entry_draft":
        _assert_entry_draft_case(case, tmp_path)
    elif kind == "instruction_outcomes":
        _assert_instruction_outcome_case(case, tmp_path)
    elif kind == "legacy_projection":
        _assert_legacy_projection_case(case, tmp_path)
    else:  # pragma: no cover - fixture schema guard
        raise AssertionError(f"unknown replay kind: {kind}")


def _persist_contract_chain(
    session_factory,
    *,
    intent_kind: str,
    strategy_instance_id: str,
    message_id: int,
    draft: dict[str, object] | None = None,
    chat_id: int | None = None,
    symbol: str = "BTC",
    side: str = "long",
):
    source = draft.get("source") if isinstance(draft, dict) else None
    resolved_chat_id = (
        int(source.get("chat_id") or 900)
        if isinstance(source, dict)
        else int(chat_id or 900)
    )
    with session_factory() as session:
        raw = RawMessage(
            chat_id=resolved_chat_id,
            message_id=message_id,
            text="redacted replay instruction",
            posted_at=NOW,
        )
        session.add(raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=raw.id,
            symbol=symbol,
            side=side,
            event_type=("entry_signal" if intent_kind == "entry" else "close_signal"),
            parse_source="mimo_authoritative",
        )
        session.add(candidate)
        session.flush()
        item = MessageInstructionItem(
            raw_message_id=raw.id,
            signal_candidate_id=candidate.id,
            sequence=0,
            instruction_kind=intent_kind,
            strategy_instance_id=strategy_instance_id,
            idempotency_key=f"{message_id:064d}"[-64:],
            execution_deadline_at=NOW + timedelta(minutes=5),
        )
        session.add(item)
        session.flush()
        signal = None
        if draft is not None:
            signal = TradeSignal(
                signal_uid=f"replay:{message_id}",
                strategy_instance_id=strategy_instance_id,
                source_type="message_instruction",
                venue="deepcoin",
                kol_id="redacted",
                chat_id=resolved_chat_id,
                message_id=raw.message_id,
                symbol=symbol,
                side=side,
                action="open_position",
                status="processing",
                payload_json=json.dumps({"deepcoin_order_draft": draft}),
            )
            session.add(signal)
        session.commit()
        item_id = int(item.id)
        signal_id = int(signal.id) if signal is not None else None
    contract = load_or_create_instruction_execution_contract(
        session_factory,
        message_instruction_item_id=item_id,
        projected_at=NOW,
        deadline_at=NOW + timedelta(minutes=5),
    )
    return item_id, signal_id, int(contract.id)


def _assert_adjacent_context_case(case: dict[str, object], tmp_path) -> None:
    strategy_payload = case["strategy"]
    strategy = EntryStrategyFact(
        raw_message_id=int(strategy_payload["raw_message_id"]),
        message_id=int(strategy_payload["message_id"]),
        posted_at=NOW,
        symbol=str(strategy_payload["symbol"]),
        side=str(strategy_payload["side"]),
    )
    facts = []
    for index, payload in enumerate(case["facts"]):
        fact_kind = str(payload["kind"])
        if fact_kind == "completed_evidence":
            fact_kind = (
                "unresolved"
                if has_material_strategy_evidence(payload.get("strategy"))
                else "unrelated"
            )
        facts.append(
            AdjacentEntryFact(
                raw_message_id=int(payload["raw_message_id"]),
                message_id=int(payload["message_id"]),
                posted_at=NOW + timedelta(seconds=index - 1),
                kind=fact_kind,
                symbol=payload.get("symbol"),
                side=payload.get("side"),
                fragment_id=payload.get("fragment_id"),
                fragment_kind=payload.get("fragment_kind"),
                payload=payload.get("payload") or {},
            )
        )
    decision = select_adjacent_entry_fragments(
        strategy=strategy,
        facts=facts,
        cutoff=(NOW + timedelta(minutes=1), 2**31, 2**31),
    )
    configured_risk = Decimal(str(case["configured_risk_usdt"]))
    effective_risk = configured_risk * decision.risk_multiplier
    session_factory = create_session_factory(tmp_path / "context-replay.db")
    actual_target_identity = build_strategy_instance_id(
        venue=strategy_payload["venue"],
        chat_id=strategy_payload["chat_id"],
        message_id=strategy.message_id,
        symbol=strategy.symbol,
        side=strategy.side,
    )
    item_id, _, contract_id = _persist_contract_chain(
        session_factory,
        intent_kind="entry",
        strategy_instance_id=actual_target_identity,
        message_id=int(strategy.message_id),
        chat_id=int(strategy_payload["chat_id"]),
        symbol=strategy.symbol,
        side=strategy.side,
    )
    writer = _RecordingWriter()
    refusal_outcome = interpret_instruction_outcome(
        case["execution_result"], intent_kind="entry"
    )
    assert refusal_outcome.state == "verified"
    assert refusal_outcome.terminal_kind == "verified_refusal"
    project_entry_refusal_contract(
        session_factory,
        message_instruction_item_id=item_id,
        reason_code=refusal_outcome.reason_code,
        evidence_refs=[{"kind": "typed_instruction_outcome"}],
        projected_at=NOW,
        mode="live",
    )

    assert decision.status == "ready"
    assert list(decision.fragment_ids) == case["expected_fragment_ids"]
    assert effective_risk == Decimal(str(case["expected_risk_usdt"]))
    with session_factory() as session:
        contract = session.get(InstructionExecutionContract, contract_id)
        assert contract.strategy_instance_id == case["expected_target_identity"]
        assert contract.terminal_kind == case["expected_contract_outcome"]
    assert len(writer.client_order_ids) == case["expected_exchange_write_count"]


def _assert_entry_draft_case(case: dict[str, object], tmp_path) -> None:
    draft = build_deepcoin_order_draft(
        case["payload"],
        contract_spec=DeepcoinContractSpec(
            instrument_id="BTC-USDT-SWAP",
            contract_value=0.001,
            quantity_step=1,
            min_quantity=1,
            price_tick=0.1,
        ),
    )
    session_factory = create_session_factory(tmp_path / "entry-replay.db")
    item_id, signal_id, contract_id = _persist_contract_chain(
        session_factory,
        intent_kind="entry",
        strategy_instance_id=str(draft["strategy_instance_id"]),
        message_id=int(case["payload"]["source"]["message_id"]),
        draft=draft,
        symbol=str(draft["symbol"]),
        side=str(case["payload"]["position_side"]),
    )
    prepare_entry_submission_contract(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        draft=draft,
        prepared_at=NOW,
        mode="live",
    )
    writer = _RecordingWriter()
    progress = EntrySubmissionProgress()
    trade_signal = load_trade_signal(session_factory, signal_id)
    result = _submit_recovery_signal_direct(
        session_factory,
        trade_signal=trade_signal,
        deepcoin_client=writer,
        submitted_at=NOW,
        validated_draft=draft,
        verified_v2_assembly=False,
        submission_progress=progress,
    )
    mark_trade_signal_submitted(
        session_factory,
        signal_id=signal_id,
        result=result,
        processed_at=NOW,
        expected_status="processing",
    )
    reconciliation = reconcile_instruction_execution_contracts(
        session_factory,
        client=writer,
        reconciled_at=NOW + timedelta(seconds=1),
        mode="live",
    )
    project_entry_submission_result(
        session_factory,
        message_instruction_item_id=item_id,
        trade_signal_id=signal_id,
        attempted_writes=progress.attempted_writes,
        confirmed_legs=progress.confirmed_legs,
        projected_at=NOW,
        mode="live",
    )

    with session_factory() as session:
        contract = session.get(InstructionExecutionContract, contract_id)
        binding = session.query(ExecutionBinding).one()
        durable_draft = json.loads(binding.payload_json)["draft"]
        assert [leg["order_type"] for leg in durable_draft["order_legs"]] == case[
            "expected_order_types"
        ]
        assert Decimal(str(durable_draft["risk_budget_usdt"])) == Decimal(
            str(case["expected_risk_usdt"])
        )
        assert contract.strategy_instance_id == case["expected_target_identity"]
        assert contract.terminal_kind == case["expected_contract_outcome"]
        assert deepcoin_order_draft_fingerprint(durable_draft) == case[
            "expected_draft_fingerprint"
        ]
    assert len(writer.client_order_ids) == case["expected_exchange_write_count"]
    assert len(set(writer.client_order_ids)) == len(writer.client_order_ids)
    assert reconciliation.transitioned == 1


def _assert_instruction_outcome_case(case: dict[str, object], tmp_path) -> None:
    outcomes = [
        interpret_instruction_outcome(
            instruction["result"], intent_kind=instruction["intent_kind"]
        )
        for instruction in case["instructions"]
    ]
    session_factory = create_session_factory(tmp_path / "outcome-replay.db")
    writer = _RecordingWriter()
    source_target = case["source_target"]
    actual_target_identity = build_strategy_instance_id(
        venue=source_target["venue"],
        chat_id=source_target["chat_id"],
        message_id=source_target["message_id"],
        symbol=source_target["symbol"],
        side=source_target["side"],
    )
    if len(case["instructions"]) > 1:
        with session_factory() as session:
            raw = RawMessage(
                chat_id=source_target["chat_id"],
                message_id=source_target["message_id"],
                text="redacted management then entry",
                posted_at=NOW,
            )
            session.add(raw)
            session.flush()
            item_ids = []
            for sequence, instruction in enumerate(case["instructions"]):
                candidate = SignalCandidate(
                    raw_message_id=raw.id,
                    symbol="BTC",
                    side="long",
                    event_type=(
                        "entry_signal"
                        if instruction["intent_kind"] == "entry"
                        else "close_signal"
                    ),
                    parse_source="mimo_authoritative",
                )
                session.add(candidate)
                session.flush()
                item = MessageInstructionItem(
                    raw_message_id=raw.id,
                    signal_candidate_id=candidate.id,
                    sequence=sequence,
                    instruction_kind=instruction["intent_kind"],
                    strategy_instance_id=actual_target_identity,
                    idempotency_key=f"multi-{sequence}".ljust(64, "0"),
                )
                session.add(item)
                session.flush()
                item_ids.append(int(item.id))
            session.commit()
        contract_ids = [
            int(
                load_or_create_instruction_execution_contract(
                    session_factory,
                    message_instruction_item_id=item_id,
                    projected_at=NOW,
                ).id
            )
            for item_id in item_ids
        ]
    else:
        _, _, contract_id = _persist_contract_chain(
            session_factory,
            intent_kind=case["instructions"][0]["intent_kind"],
            strategy_instance_id=actual_target_identity,
            message_id=source_target["message_id"],
            chat_id=source_target["chat_id"],
            symbol=source_target["symbol"],
            side=source_target["side"],
        )
        contract_ids = [contract_id]
    for sequence, (instruction, outcome, contract_id) in enumerate(
        zip(case["instructions"], outcomes, contract_ids, strict=True), start=1
    ):
        with session_factory() as session:
            contract = session.get(InstructionExecutionContract, contract_id)
            expected_state = contract.state
            expected_version = contract.state_version
        if outcome.attempted_exchange_write:
            writer.submit(f"REPLAY-INSTRUCTION-{sequence}")
            contract = transition_instruction_execution_contract(
                session_factory,
                contract_id=contract_id,
                expected_state=expected_state,
                expected_version=expected_version,
                new_state="submitting",
                reason_code="replay_writer_imminent",
                evidence_refs=[{"kind": "redacted_replay"}],
                transitioned_at=NOW,
                attempted_exchange_write=True,
            )
            expected_state = contract.state
            expected_version = contract.state_version
        transition_instruction_execution_contract(
            session_factory,
            contract_id=contract_id,
            expected_state=expected_state,
            expected_version=expected_version,
            new_state=outcome.state,
            reason_code=outcome.reason_code,
            evidence_refs=[{"kind": "typed_instruction_outcome"}],
            transitioned_at=NOW,
            terminal_kind=outcome.terminal_kind,
            completion_scope=("full" if outcome.state == "verified" else None),
        )

    with session_factory() as session:
        contracts = [
            session.get(InstructionExecutionContract, contract_id)
            for contract_id in contract_ids
        ]
        assert [contract.state for contract in contracts] == case["expected_states"]
        assert [contract.terminal_kind for contract in contracts] == case[
            "expected_terminal_kinds"
        ]
        assert {contract.strategy_instance_id for contract in contracts} == {
            case["expected_target_identity"]
        }
        if len(case["instructions"]) > 1:
            items = (
                session.query(MessageInstructionItem)
                .order_by(MessageInstructionItem.sequence.asc())
                .all()
            )
            assert [item.instruction_kind for item in items] == [
                instruction["intent_kind"] for instruction in case["instructions"]
            ]
    assert len(writer.client_order_ids) == case["expected_exchange_write_count"]
    aggregate_state = (
        "verified"
        if all(outcome.state == "verified" for outcome in outcomes)
        else outcomes[0].state
    )
    assert aggregate_state == case["expected_contract_outcome"]


def _assert_legacy_projection_case(case: dict[str, object], tmp_path) -> None:
    session_factory = create_session_factory(tmp_path / "legacy-lifecycle.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=88,
            message_id=4171,
            text="redacted legacy strategy",
            posted_at=NOW,
        )
        session.add(raw)
        session.flush()
        session.add(
            StrategyLifecycle(
                chat_id=raw.chat_id,
                message_id=raw.message_id,
                symbol="BTC",
                side="long",
                lifecycle_status="entered",
                signal_at=NOW,
            )
        )
        session.commit()
        raw_id = int(raw.id)
    save_trading_settings(
        session_factory,
        {
            "instruction_execution_contract_mode": "live",
            "instruction_execution_entry_after_item_id": 0,
            "instruction_execution_management_after_item_id": 0,
        },
    )

    projected = project_instruction_execution_contracts(
        session_factory,
        raw_message_id=raw_id,
        projected_at=NOW,
    )

    assert projected == ()
    with session_factory() as session:
        assert session.query(InstructionExecutionContract).count() == 0
        assert session.query(TradeSignal).count() == 0
    with session_factory() as session:
        lifecycle = session.query(StrategyLifecycle).one()
        actual_target_identity = (
            "legacy:lifecycle-only"
            if session.query(MessageInstructionItem).count() == 0
            and lifecycle.execution_binding_id is None
            else "legacy:projected"
        )
    assert case["expected_contract_outcome"] == "not_projected"
    assert case["expected_exchange_write_count"] == 0
    assert actual_target_identity == case["expected_target_identity"]
