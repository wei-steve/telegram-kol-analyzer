import json
from datetime import UTC, datetime

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    EntryStrategyAssembly,
    ExecutionBinding,
    ExecutionOrderLeg,
    RawMessage,
    SignalCandidate,
    StrategyLifecycle,
    StrategyRevisionBatch,
)
from telegram_kol_research.strategy_threads import create_strategy_thread_for_lifecycle


NOW = datetime(2026, 8, 8, 12, tzinfo=UTC)


def _persist_target(
    session_factory,
    *,
    leg_states=("submitted", "submitted"),
    pos_ids=(None, None),
    verified=True,
):
    with session_factory() as session:
        strategy = RawMessage(chat_id=101, message_id=1001, text="BTC long")
        revision = RawMessage(chat_id=101, message_id=1002, text="50% position")
        session.add_all([strategy, revision])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=strategy.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            parse_source="authoritative",
            confidence=1,
        )
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:101:1001:BTC:long",
            kol_id="group:101",
            chat_id=101,
            message_id=1001,
            symbol="BTC",
            side="long",
            status="open",
        )
        session.add_all([candidate, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=101,
            message_id=1001,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.flush()
        assembly_evidence = {
            "configured_risk_budget_usdt": "20",
            "effective_risk_budget_usdt": "10",
            "order_draft_snapshot": {
                "strategy_instance_id": binding.strategy_instance_id,
                "instrument_id": "BTC-USDT-SWAP",
                "stop_loss": 63000,
                "risk_budget_usdt": 10,
                "order_legs": [
                    {
                        "price": 64000,
                        "order_type": "limit",
                        "quantity": 0.005,
                        "risk_budget_usdt": 5,
                        "client_order_id": "new-0",
                    },
                    {
                        "price": 63800,
                        "order_type": "limit",
                        "quantity": 0.00625,
                        "risk_budget_usdt": 5,
                        "client_order_id": "new-1",
                    },
                ],
            },
        }
        assembly = EntryStrategyAssembly(
            strategy_raw_message_id=strategy.id,
            signal_candidate_id=candidate.id,
            strategy_instance_id=binding.strategy_instance_id,
            risk_multiplier="0.5",
            evidence_json=json.dumps(assembly_evidence, sort_keys=True),
            fingerprint="a" * 64,
        )
        session.add(assembly)
        session.flush()
        for index, (state, pos_id) in enumerate(zip(leg_states, pos_ids, strict=True)):
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=index,
                    purpose="entry",
                    order_kind="limit",
                    order_id=f"old-{index}",
                    client_order_id=f"old-client-{index}",
                    pos_id=pos_id,
                    attribution_status="verified" if verified else "unassigned",
                    last_verified_at=NOW if verified else None,
                    status=state,
                    request_json=json.dumps({"sz": "0.01", "px": str(64000-index*200)}),
                )
            )
        session.commit()
        ids = revision.id, lifecycle.id, assembly.id
    thread = create_strategy_thread_for_lifecycle(
        session_factory, lifecycle_id=ids[1]
    )
    return ids[0], thread.id, ids[2]


def test_entry_revision_modes_and_idempotent_snapshot(tmp_path):
    from telegram_kol_research.entry_revision_planner import plan_entry_revision

    session_factory = create_session_factory(tmp_path / "revision-plan.db")
    raw_id, thread_id, assembly_id = _persist_target(session_factory)

    disabled = plan_entry_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        entry_strategy_assembly_id=assembly_id,
        mode="disabled",
        planned_at=NOW,
    )
    shadow = plan_entry_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        entry_strategy_assembly_id=assembly_id,
        mode="shadow",
        planned_at=NOW,
    )
    repeated = plan_entry_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        entry_strategy_assembly_id=assembly_id,
        mode="shadow",
        planned_at=NOW,
    )

    assert disabled.status == "disabled"
    assert disabled.batch_id is None
    assert shadow.status == "shadow_planned"
    assert repeated.batch_id == shadow.batch_id
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, shadow.batch_id)
        assert batch.revision_kind == "entry_sizing"
        assert batch.target_assembly_fingerprint == "a" * 64
        snapshot = json.loads(batch.target_snapshot_json)
        assert [leg["order_id"] for leg in snapshot["entry_legs"]] == [
            "old-0",
            "old-1",
        ]
        assert len(json.loads(batch.replacement_json)["order_legs"]) == 2


def test_entry_revision_blocks_unverified_or_ambiguous_submission(tmp_path):
    from telegram_kol_research.entry_revision_planner import plan_entry_revision

    session_factory = create_session_factory(tmp_path / "unknown.db")
    raw_id, thread_id, assembly_id = _persist_target(
        session_factory, verified=False
    )

    result = plan_entry_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        entry_strategy_assembly_id=assembly_id,
        mode="live",
        planned_at=NOW,
    )

    assert result.status == "blocked"
    assert result.reason_code == "revision_submission_state_unknown"
    with session_factory() as session:
        assert session.query(StrategyRevisionBatch).count() == 0


def test_entry_revision_accepts_exact_filled_and_pending_mix(tmp_path):
    from telegram_kol_research.entry_revision_planner import plan_entry_revision

    session_factory = create_session_factory(tmp_path / "partial.db")
    raw_id, thread_id, assembly_id = _persist_target(
        session_factory,
        leg_states=("filled", "submitted"),
        pos_ids=("pos-1", None),
    )

    result = plan_entry_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        entry_strategy_assembly_id=assembly_id,
        mode="live",
        planned_at=NOW,
    )

    assert result.status == "planned"
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, result.batch_id)
        snapshot = json.loads(batch.target_snapshot_json)
    assert [leg["action"] for leg in snapshot["entry_legs"]] == [
        "retain_filled",
        "cancel_pending",
    ]
