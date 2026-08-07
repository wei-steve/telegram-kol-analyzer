import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    EntryStrategyAssembly,
    EntryStrategyFragment,
    ExecutionBinding,
    ExecutionOrderLeg,
    MessageEvidenceVersion,
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
        strategy = RawMessage(
            chat_id=101,
            message_id=1001,
            posted_at=NOW - timedelta(minutes=1),
            text="BTC long",
        )
        revision = RawMessage(
            chat_id=101,
            message_id=1002,
            posted_at=NOW,
            text="50% position",
        )
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
                "contract_spec": {
                    "contract_value": 1,
                    "quantity_step": 0.000001,
                    "min_quantity": 0.000001,
                },
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
    live = plan_entry_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        entry_strategy_assembly_id=assembly_id,
        mode="live",
        planned_at=NOW,
    )

    assert disabled.status == "disabled"
    assert disabled.batch_id is None
    assert shadow.status == "shadow_planned"
    assert repeated.batch_id == shadow.batch_id
    assert live.batch_id == shadow.batch_id
    assert live.status == "planned"
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


def test_entry_revision_reuses_assembly_fingerprint_across_duplicate_messages(tmp_path):
    from telegram_kol_research.entry_revision_planner import plan_entry_revision

    session_factory = create_session_factory(tmp_path / "assembly-idempotency.db")
    raw_id, thread_id, assembly_id = _persist_target(session_factory)
    first = plan_entry_revision(
        session_factory,
        raw_message_id=raw_id,
        strategy_thread_id=thread_id,
        entry_strategy_assembly_id=assembly_id,
        mode="live",
        planned_at=NOW,
    )
    with session_factory() as session:
        duplicate = RawMessage(chat_id=101, message_id=1003, text="same revision")
        session.add(duplicate)
        session.commit()
        duplicate_id = duplicate.id

    repeated = plan_entry_revision(
        session_factory,
        raw_message_id=duplicate_id,
        strategy_thread_id=thread_id,
        entry_strategy_assembly_id=assembly_id,
        mode="live",
        planned_at=NOW,
    )

    assert repeated.batch_id == first.batch_id


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


def test_entry_revision_blocks_unassigned_pending_order_even_when_recently_seen(tmp_path):
    from telegram_kol_research.entry_revision_planner import plan_entry_revision

    session_factory = create_session_factory(tmp_path / "unassigned-pending.db")
    raw_id, thread_id, assembly_id = _persist_target(session_factory)
    with session_factory() as session:
        for leg in session.query(ExecutionOrderLeg).all():
            leg.attribution_status = "unassigned"
        session.commit()

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


def test_post_submit_fragment_creates_new_revision_target_without_second_assembly(tmp_path):
    from telegram_kol_research.entry_revision_planner import (
        plan_post_submit_entry_fragment_revisions,
    )

    session_factory = create_session_factory(tmp_path / "post-submit.db")
    fragment_raw_id, thread_id, assembly_id = _persist_target(session_factory)
    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, assembly_id)
        strategy = session.get(RawMessage, assembly.strategy_raw_message_id)
        fragment_raw = session.get(RawMessage, fragment_raw_id)
        fragment_raw.posted_at = NOW
        fragment_raw.text = "add 63400"
        evidence = MessageEvidenceVersion(
            raw_message_id=fragment_raw.id,
            version=1,
            input_fingerprint="post-submit",
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
        fragment = EntryStrategyFragment(
            raw_message_id=fragment_raw.id,
            chat_id=fragment_raw.chat_id,
            message_id=fragment_raw.message_id,
            symbol="BTC",
            side="long",
            fragment_kind="supplemental_entry",
            payload_json='{"entry_price":"63400"}',
            evidence_version_id=evidence.id,
            recognition_generation="post-submit",
            source_relationship="unresolved",
            status="pending",
            reason="add",
            fingerprint="c" * 64,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(fragment)
        session.commit()
        fragment_id = fragment.id

    immature = plan_post_submit_entry_fragment_revisions(
        session_factory,
        fragment_ids=(fragment_id,),
        mode="live",
        planned_at=NOW,
    )
    assert immature == ()

    gated = plan_post_submit_entry_fragment_revisions(
        session_factory,
        fragment_ids=(fragment_id,),
        mode="live",
        planned_at=NOW + timedelta(minutes=31),
    )
    assert gated[0].reason_code == "revision_supplemental_live_not_enabled"

    results = plan_post_submit_entry_fragment_revisions(
        session_factory,
        fragment_ids=(fragment_id,),
        mode="live",
        planned_at=NOW + timedelta(minutes=31),
        allow_supplemental_live=True,
    )

    assert len(results) == 1
    assert results[0].status == "planned", results[0]
    with session_factory() as session:
        batch = session.get(StrategyRevisionBatch, results[0].batch_id)
        replacement = json.loads(batch.replacement_json)
        fragment = session.get(EntryStrategyFragment, fragment_id)
        assert session.query(EntryStrategyAssembly).count() == 1
    assert [leg["price"] for leg in replacement["order_legs"]] == [
        64000,
        63800,
        63400.0,
    ]
    assert batch.target_assembly_fingerprint != "a" * 64
    assert fragment.status == "consumed"
    assert fragment.target_strategy_thread_id == thread_id


def test_fragment_before_new_strategy_is_not_applied_to_previous_strategy(tmp_path):
    from telegram_kol_research.entry_revision_planner import (
        plan_post_submit_entry_fragment_revisions,
    )

    session_factory = create_session_factory(tmp_path / "before-next-strategy.db")
    fragment_raw_id, _, _ = _persist_target(session_factory)
    with session_factory() as session:
        fragment_raw = session.get(RawMessage, fragment_raw_id)
        fragment_raw.posted_at = NOW
        evidence = MessageEvidenceVersion(
            raw_message_id=fragment_raw.id,
            version=1,
            input_fingerprint="before-next",
            model="mimo",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=1,
            text_evidence_json="{}",
            image_evidence_json="{}",
            normalized_evidence_json="{}",
        )
        next_strategy = RawMessage(
            chat_id=fragment_raw.chat_id,
            message_id=fragment_raw.message_id + 1,
            posted_at=NOW + timedelta(minutes=1),
            text="BTC long next strategy",
        )
        session.add_all([evidence, next_strategy])
        session.flush()
        fragment = EntryStrategyFragment(
            raw_message_id=fragment_raw.id,
            chat_id=fragment_raw.chat_id,
            message_id=fragment_raw.message_id,
            symbol="BTC",
            side="long",
            fragment_kind="risk_multiplier",
            payload_json='{"risk_multiplier":"0.5"}',
            evidence_version_id=evidence.id,
            recognition_generation="before-next",
            source_relationship="unresolved",
            status="pending",
            reason="half before next strategy",
            fingerprint="d" * 64,
            created_at=NOW,
            updated_at=NOW,
        )
        next_candidate = SignalCandidate(
            raw_message_id=next_strategy.id,
            symbol="BTC",
            side="long",
            event_type="entry_signal",
            parse_source="authoritative",
            confidence=1,
        )
        session.add_all([fragment, next_candidate])
        session.commit()
        fragment_id = int(fragment.id)

    results = plan_post_submit_entry_fragment_revisions(
        session_factory,
        fragment_ids=(fragment_id,),
        mode="live",
        planned_at=NOW + timedelta(minutes=31),
    )

    assert results[0].status == "blocked"
    assert results[0].reason_code == "revision_fragment_target_ambiguous"
    with session_factory() as session:
        assert session.get(EntryStrategyFragment, fragment_id).status == "pending"
        assert session.query(StrategyRevisionBatch).count() == 0


def test_sequential_post_submit_revisions_build_on_latest_succeeded_generation(tmp_path):
    from telegram_kol_research.entry_revision_planner import (
        plan_post_submit_entry_fragment_revisions,
    )

    session_factory = create_session_factory(tmp_path / "cumulative-generation.db")
    first_raw_id, _, assembly_id = _persist_target(session_factory)
    with session_factory() as session:
        assembly = session.get(EntryStrategyAssembly, assembly_id)
        evidence_payload = json.loads(assembly.evidence_json)
        evidence_payload["effective_risk_budget_usdt"] = "20"
        evidence_payload["order_draft_snapshot"]["risk_budget_usdt"] = 20
        for leg in evidence_payload["order_draft_snapshot"]["order_legs"]:
            leg["risk_budget_usdt"] = 10
            leg["quantity"] = float(Decimal(str(leg["quantity"])) * 2)
        assembly.evidence_json = json.dumps(evidence_payload, sort_keys=True)
        first_raw = session.get(RawMessage, first_raw_id)
        first_evidence = MessageEvidenceVersion(
            raw_message_id=first_raw.id,
            version=1,
            input_fingerprint="cumulative-half",
            model="mimo",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=1,
            text_evidence_json="{}",
            image_evidence_json="{}",
            normalized_evidence_json="{}",
        )
        session.add(first_evidence)
        session.flush()
        first_fragment = EntryStrategyFragment(
            raw_message_id=first_raw.id,
            chat_id=first_raw.chat_id,
            message_id=first_raw.message_id,
            symbol="BTC",
            side="long",
            fragment_kind="risk_multiplier",
            payload_json='{"risk_multiplier":"0.5"}',
            evidence_version_id=first_evidence.id,
            recognition_generation="cumulative-half",
            source_relationship="unresolved",
            status="pending",
            reason="half",
            fingerprint="e" * 64,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(first_fragment)
        session.commit()
        first_fragment_id = int(first_fragment.id)

    first_plan = plan_post_submit_entry_fragment_revisions(
        session_factory,
        fragment_ids=(first_fragment_id,),
        mode="live",
        planned_at=NOW + timedelta(minutes=31),
    )[0]
    with session_factory() as session:
        first_batch = session.get(StrategyRevisionBatch, first_plan.batch_id)
        first_replacement = json.loads(first_batch.replacement_json)
        assert first_replacement["risk_budget_usdt"] == 10.0
        first_batch.status = "succeeded"
        first_batch.completed_at = NOW + timedelta(minutes=32)
        binding_id = int(first_batch.execution_binding_id)
        binding = session.get(ExecutionBinding, binding_id)
        for old_leg in session.query(ExecutionOrderLeg).filter_by(
            execution_binding_id=binding_id
        ):
            old_leg.status = "cancelled"
        for index, desired in enumerate(first_replacement["order_legs"], start=2):
            session.add(
                ExecutionOrderLeg(
                    execution_binding_id=binding_id,
                    strategy_instance_id=binding.strategy_instance_id,
                    leg_index=index,
                    purpose="entry",
                    order_kind="limit",
                    order_id=f"revised-{index}",
                    client_order_id=f"revised-client-{index}",
                    attribution_status="verified",
                    last_verified_at=NOW + timedelta(minutes=32),
                    status="submitted",
                    request_json=json.dumps(
                        {"sz": str(desired["quantity"]), "px": str(desired["price"])}
                    ),
                )
            )
        second_raw = RawMessage(
            chat_id=101,
            message_id=1003,
            posted_at=NOW + timedelta(minutes=2),
            text="add 63400",
        )
        session.add(second_raw)
        session.flush()
        second_evidence = MessageEvidenceVersion(
            raw_message_id=second_raw.id,
            version=1,
            input_fingerprint="cumulative-add",
            model="mimo",
            prompt_versions_json="{}",
            extraction_status="completed",
            confidence=1,
            text_evidence_json="{}",
            image_evidence_json="{}",
            normalized_evidence_json="{}",
        )
        session.add(second_evidence)
        session.flush()
        second_fragment = EntryStrategyFragment(
            raw_message_id=second_raw.id,
            chat_id=101,
            message_id=1003,
            symbol="BTC",
            side="long",
            fragment_kind="supplemental_entry",
            payload_json='{"entry_price":"63400"}',
            evidence_version_id=second_evidence.id,
            recognition_generation="cumulative-add",
            source_relationship="unresolved",
            status="pending",
            reason="add",
            fingerprint="f" * 64,
            created_at=NOW + timedelta(minutes=2),
            updated_at=NOW + timedelta(minutes=2),
        )
        session.add(second_fragment)
        session.commit()
        second_fragment_id = int(second_fragment.id)

    second_plan = plan_post_submit_entry_fragment_revisions(
        session_factory,
        fragment_ids=(second_fragment_id,),
        mode="live",
        planned_at=NOW + timedelta(minutes=33),
        allow_supplemental_live=True,
    )[0]

    assert second_plan.status == "planned"
    with session_factory() as session:
        replacement = json.loads(
            session.get(StrategyRevisionBatch, second_plan.batch_id).replacement_json
        )
    assert replacement["risk_budget_usdt"] == 10.0
    assert [leg["price"] for leg in replacement["order_legs"]] == [
        64000,
        63800,
        63400.0,
    ]
