from datetime import UTC, datetime, timedelta

from sqlalchemy import event

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import (
    ExecutionBinding,
    ExecutionEvent,
    ExecutionOrderLeg,
    MediaAsset,
    PositionAttributionAudit,
    PositionProtectionLedger,
    PositionProtectionRevision,
    PositionBackupStopOrder,
    PositionProtectionIncident,
    PositionTakeProfitOrder,
    RawMessage,
    RecognitionDecision,
    SignalCandidate,
    SourceMessageDeletionExit,
    StrategyLifecycle,
    StrategyBreakEvenConvergence,
    StrategyBreakEvenConvergenceLeg,
    StrategyManagementBatch,
    StrategyManagementLeg,
    TriggerProtectionIntent,
    TriggerProtectionStopRescue,
)
from telegram_kol_research.source_message_deletion import record_source_message_deleted
from telegram_kol_research.strategy_records import (
    enrich_strategy_records_with_exchange,
    load_live_bindings_without_lifecycle,
    load_strategy_record_detail,
    load_strategy_record_summaries,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def test_strategy_detail_projects_shadow_break_even_decision_as_not_executed(tmp_path):
    sf = create_session_factory(tmp_path / "break-even-detail.db")
    with sf() as session:
        raw = RawMessage(
            chat_id=10,
            message_id=101,
            text="BTC short",
            archived_target_group=True,
        )
        binding = ExecutionBinding(
            strategy_instance_id="strategy-break-even-detail",
            kol_id="group:10",
            chat_id=10,
            message_id=101,
            symbol="BTC",
            side="short",
            venue="deepcoin",
            status="active",
            pos_id="pos-1",
        )
        session.add_all([raw, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=10,
            message_id=101,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            execution_binding_id=binding.id,
        )
        entry = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=1,
            purpose="entry",
            order_kind="market",
            order_id="pos-1",
            pos_id="pos-1",
            venue="deepcoin",
            attribution_status="verified",
            status="active",
        )
        session.add_all([lifecycle, entry])
        session.flush()
        convergence = StrategyBreakEvenConvergence(
            strategy_instance_id=binding.strategy_instance_id,
            execution_binding_id=binding.id,
            target_lifecycle_id=lifecycle.id,
            trigger_type="tp1_fill",
            trigger_identity="tp-1",
            trigger_evidence_json='{"evidence_tier":"exact_order_terminal"}',
            target_snapshot_json="{}",
            execution_mode="shadow",
            status="shadow_planned",
            planned_at=NOW,
            completed_at=NOW,
            updated_at=NOW,
        )
        session.add(convergence)
        session.flush()
        session.add(StrategyBreakEvenConvergenceLeg(
            convergence_id=convergence.id,
            execution_order_leg_id=entry.id,
            pos_id="pos-1",
            preflight_size="5",
            avg_entry_price="63076.7",
            old_protection_json="[]",
            decision_json='{"action":"full_exit","market_price":"63461.2"}',
            status="shadow_planned",
            reason_code="shadow_only_no_exchange_write",
            updated_at=NOW,
        ))
        session.commit()
        lifecycle_id = int(lifecycle.id)

    detail = load_strategy_record_detail(
        sf,
        lifecycle_id=lifecycle_id,
        group_labels_by_chat_id={10: "大镖客"},
    )

    convergence_detail = detail["execution"]["break_even_convergences"][0]
    assert convergence_detail["execution_mode"] == "shadow"
    assert convergence_detail["status"] == "shadow_planned"
    assert convergence_detail["legs"][0]["decision"]["action"] == "full_exit"
    assert convergence_detail["legs"][0]["mutation_intent_id"] is None


def test_strategy_records_expose_source_deletion_recovery_and_flat_proof(tmp_path):
    session_factory = create_session_factory(tmp_path / "strategy-detail.db")
    with session_factory() as session:
        raw = RawMessage(
            chat_id=10,
            message_id=101,
            text="BTC long",
            archived_target_group=True,
        )
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:10:101:BTC:long",
            kol_id="group:10",
            chat_id=10,
            message_id=101,
            symbol="BTC",
            side="long",
            venue="deepcoin",
            status="open",
        )
        session.add_all([raw, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=10,
            message_id=101,
            symbol="BTC",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            execution_binding_id=binding.id,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id
    deletion = record_source_message_deleted(
        session_factory,
        chat_id=10,
        message_id=101,
        deleted_at=NOW,
    )
    with session_factory() as session:
        deletion_exit = session.get(SourceMessageDeletionExit, deletion.exit_id)
        deletion_exit.state = "recovery_required"
        deletion_exit.last_reason = "entry_cancel_unknown"
        deletion_exit.cancellation_signal_ids_json = "[41]"
        deletion_exit.flat_proof_json = '{"verified_pos_ids":[]}'
        session.commit()

    detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=lifecycle_id,
        group_labels_by_chat_id={10: "舒琴"},
    )
    summaries = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "舒琴"},
        filter_name="all",
    )

    assert detail["source_deletion"]["state"] == "recovery_required"
    assert detail["source_deletion"]["cancellation_signal_ids"] == [41]
    assert detail["source_deletion"]["flat_proof_confirmed"] is True
    assert summaries[0]["source_deletion"]["reason"] == "entry_cancel_unknown"


def test_load_strategy_record_detail_builds_full_evidence_chain(tmp_path):
    session_factory = create_session_factory(tmp_path / "strategy-detail.db")
    with session_factory() as session:
        raw_message = RawMessage(
            chat_id=10,
            message_id=101,
            posted_at=NOW - timedelta(hours=6),
            text="BTC 现价做多",
            raw_payload='{"message_id": 101}',
        )
        session.add(raw_message)
        session.flush()
        session.add(
            MediaAsset(
                raw_message_id=raw_message.id,
                telegram_file_id="telegram-file-1",
                kind="photo",
                mime_type="image/jpeg",
                local_path="media/chart.jpg",
                ocr_text="BTC LONG",
                created_at=NOW - timedelta(hours=6),
            )
        )
        candidate = SignalCandidate(
            raw_message_id=raw_message.id,
            symbol="BTCUSDT",
            side="long",
            event_type="entry_signal",
            entry_text="market",
            stop_loss_text="65000",
            review_status="approved",
            created_at=NOW - timedelta(hours=5, minutes=50),
        )
        decision = RecognitionDecision(
            raw_message_id=raw_message.id,
            input_kind="text+image",
            authoritative_model="mimo-v2.5",
            authoritative_status="是策略",
            authoritative_payload_json='{"symbol":"BTCUSDT","api_key":"secret"}',
            auxiliary_model="other-model",
            auxiliary_status="是策略",
            auxiliary_payload_json="{}",
            agreement_status="agreed",
            differences_json="[]",
            prompt_versions_json='{"mimo":"v2"}',
            created_at=NOW - timedelta(hours=5, minutes=45),
            updated_at=NOW - timedelta(hours=5, minutes=45),
        )
        session.add_all([candidate, decision])
        session.flush()
        binding = _binding(
            chat_id=10,
            message_id=101,
            symbol="BTCUSDT",
            strategy_instance_id="strategy-detail-1",
        )
        binding.pos_id = "pos-1"
        binding.order_id = "order-1"
        binding.payload_json = '{"passphrase":"must-not-render"}'
        binding.created_at = NOW - timedelta(hours=5, minutes=20)
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=10,
            message_id=101,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW - timedelta(hours=5, minutes=30),
            entered_at=NOW - timedelta(hours=5),
            stop_loss=65_000.0,
            execution_binding_id=binding.id,
            updated_at=NOW - timedelta(hours=5),
        )
        session.add(lifecycle)
        session.flush()
        order_leg = ExecutionOrderLeg(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            leg_index=0,
            purpose="entry",
            order_kind="market",
            order_id="order-1",
            client_order_id="client-1",
            pos_id="pos-1",
            attribution_status="bound",
            request_json='{"token":"must-not-render","size":"1"}',
            response_json='{"code":"0"}',
            status="filled",
            created_at=NOW - timedelta(hours=5, minutes=15),
            updated_at=NOW - timedelta(hours=5, minutes=10),
        )
        session.add(order_leg)
        session.flush()
        session.add(
            PositionTakeProfitOrder(
                venue="deepcoin", execution_binding_id=binding.id,
                execution_order_leg_id=order_leg.id, pos_id="pos-1", order_id="tp-active-1",
                trigger_price="69000", size_text="1", status="active",
                created_at=NOW - timedelta(hours=4), updated_at=NOW - timedelta(hours=4),
            )
        )
        session.add(
            PositionProtectionLedger(
                venue="deepcoin", execution_binding_id=binding.id,
                execution_order_leg_id=order_leg.id,
                strategy_instance_id=binding.strategy_instance_id, pos_id="pos-1",
                instrument_id="BTC-USDT-SWAP", side="long", order_id="primary-stop-1",
                purpose="stop_loss", trigger_price="65000", status="stop_trigger_failed",
                evidence_source="test", evidence_json="{}",
            )
        )
        session.add_all([
            PositionBackupStopOrder(
                venue="deepcoin", execution_binding_id=binding.id, execution_order_leg_id=order_leg.id,
                pos_id="pos-1", instrument_id="BTC-USDT-SWAP", side="long",
                trigger_price="64675", order_id="backup-stop-1", client_order_id="backup-client-1",
                status="active", request_json="{}",
            ),
            PositionProtectionIncident(
                venue="deepcoin", execution_binding_id=binding.id, execution_order_leg_id=order_leg.id,
                pos_id="pos-1", incident_type="stop_trigger_failed", fingerprint="detail-incident",
                evidence_json='{"exchange":{"errorCode":"203","errorMsg":"NotEnoughMoneyToClose"}}',
                delivery_status="pending",
            ),
        ])
        fill_event = ExecutionEvent(
            execution_binding_id=binding.id,
            strategy_instance_id=binding.strategy_instance_id,
            action="fill",
            status="succeeded",
            chat_id=10,
            message_id=101,
            source_message_id=101,
            symbol="BTCUSDT",
            side="long",
            order_id="order-1",
            pos_id="pos-1",
            response_json='{"apiSecret":"must-not-render","fillPrice":"67000"}',
            exchange_event_time=NOW - timedelta(hours=5),
            created_at=NOW - timedelta(hours=5),
        )
        batch = StrategyManagementBatch(
            idempotency_fingerprint="detail-management-batch",
            raw_message_id=raw_message.id,
            recognition_decision_id=decision.id,
            recognition_generation="mimo_only_v2",
            target_lifecycle_id=lifecycle.id,
            strategy_instance_id=str(binding.strategy_instance_id),
            execution_binding_id=binding.id,
            intent="risk_update",
            effective_action="move_stop",
            execution_mode="live",
            status="succeeded",
            target_fingerprint="detail-target",
            target_snapshot_json='{"stop":"breakeven"}',
            planned_at=NOW - timedelta(hours=4),
            completed_at=NOW - timedelta(hours=3, minutes=55),
            created_at=NOW - timedelta(hours=4),
            updated_at=NOW - timedelta(hours=3, minutes=55),
        )
        session.add_all([fill_event, batch])
        session.flush()
        session.add(
            StrategyManagementLeg(
                management_batch_id=batch.id,
                execution_order_leg_id=order_leg.id,
                pos_id="pos-1",
                leg_index=0,
                status="succeeded",
                request_json='{"authorization":"must-not-render"}',
                response_json='{"code":"0"}',
                created_at=NOW - timedelta(hours=4),
                updated_at=NOW - timedelta(hours=3, minutes=55),
            )
        )
        session.commit()
        lifecycle_id = lifecycle.id

    detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=lifecycle_id,
        group_labels_by_chat_id={10: "大镖客"},
    )

    assert detail is not None
    assert detail["identity"]["lifecycle_id"] == lifecycle_id
    assert detail["overview"]["authoritative_model"] == "mimo-v2.5"
    assert [(item["kind"], item.get("role")) for item in detail["timeline"]] == [
        ("message", "entry"),
        ("message", "management"),
        ("recognition", "entry"),
        ("recognition", "management"),
        ("strategy", None),
        ("order", None),
        ("fill", None),
        ("management", "management"),
    ]
    assert all(item["source"] for item in detail["timeline"])
    assert detail["execution"]["binding"]["pos_id"] == "pos-1"
    assert detail["execution"]["management_batches"][0]["legs"][0]["pos_id"] == "pos-1"
    assert detail["evidence"]["raw_message"]["text"] == "BTC 现价做多"
    assert detail["evidence"]["media"][0]["kind"] == "photo"
    assert detail["evidence"]["recognition_decision"]["authoritative_payload"]["api_key"] == "[REDACTED]"
    assert [item["role"] for item in detail["evidence"]["recognition_decisions"]] == [
        "entry",
        "management",
    ]
    assert detail["execution"]["order_legs"][0]["request"]["token"] == "[REDACTED]"
    take_profit_detail = detail["execution"]["take_profit_orders"]
    assert take_profit_detail[0]["execution_order_leg_id"] == detail["execution"]["order_legs"][0]["id"]
    assert take_profit_detail[0]["pos_id"] == "pos-1"
    assert take_profit_detail[0]["active"][0]["order_id"] == "tp-active-1"
    assert take_profit_detail[0]["history"] == []
    assert take_profit_detail[0]["convergence"] is None
    assert detail["execution"]["backup_stops"] == [{
        "pos_id": "pos-1", "order_id": "backup-stop-1", "trigger_price": "64675", "status": "active",
    }]
    assert detail["execution"]["protection_incidents"] == [{
        "pos_id": "pos-1", "incident_type": "stop_trigger_failed", "delivery_status": "pending",
        "error": "203: NotEnoughMoneyToClose",
    }]
    assert detail["execution"]["protection_states"] == [{
        "pos_id": "pos-1",
        "primary_stop_price": "65000",
        "primary_stop_status": "stop_trigger_failed",
        "primary_order_id": "primary-stop-1",
        "backup_stop_price": "64675",
        "backup_stop_status": "active",
        "backup_order_id": "backup-stop-1",
        "backup_stop_blocker": None,
        "operator_message": "主止损失败，第二止损有效",
    }]


def test_load_strategy_record_detail_returns_none_for_unknown_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "missing-detail.db")

    assert load_strategy_record_detail(
        session_factory,
        lifecycle_id=999,
        group_labels_by_chat_id={},
    ) is None


def test_strategy_detail_shows_exact_adopted_trigger_protection_order_only(tmp_path):
    session_factory = create_session_factory(tmp_path / "adopted-protection.db")
    with session_factory() as session:
        binding, lifecycle, leg = _seed_trigger_entry_strategy(session)
        session.add_all(
            [
                PositionProtectionLedger(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    pos_id="pos-adopted",
                    instrument_id="BTC-USDT-SWAP",
                    side="long",
                    order_id="tpsl-exact-1",
                    purpose="combined",
                    status="verified",
                    evidence_source="reconciliation_trigger_entry_adoption",
                    evidence_json='{"match":"trigger_entry_unique_expected_protection_shape"}',
                    created_at=NOW,
                    updated_at=NOW,
                ),
                PositionProtectionLedger(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    pos_id="pos-other",
                    instrument_id="BTC-USDT-SWAP",
                    side="long",
                    order_id="tpsl-other-position",
                    purpose="combined",
                    status="verified",
                    evidence_source="reconciliation_trigger_entry_adoption",
                    evidence_json="{}",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.commit()
        lifecycle_id = lifecycle.id

    detail = load_strategy_record_detail(
        session_factory, lifecycle_id=lifecycle_id, group_labels_by_chat_id={}
    )

    assert detail["execution"]["protection_adoption"] == {
        "state": "adopted",
        "order_ids": ["tpsl-exact-1"],
        "evidence_sources": ["reconciliation_trigger_entry_adoption"],
        "refusal_codes": [],
    }
    assert "tpsl-other-position" not in str(detail["execution"]["protection_adoption"])


def test_strategy_detail_projects_exact_protection_revision_history(tmp_path):
    session_factory = create_session_factory(tmp_path / "protection-revisions-detail.db")
    with session_factory() as session:
        binding, lifecycle, leg = _seed_trigger_entry_strategy(session)
        initial = PositionProtectionRevision(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            strategy_instance_id=binding.strategy_instance_id,
            pos_id="pos-adopted",
            source="entry_submit",
            status="superseded",
            protection_json='{"take_profit_order_ids":["tp-initial"],"stop_loss_order_ids":["sl-initial"]}',
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(initial)
        session.flush()
        session.add_all(
            [
                PositionProtectionRevision(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    strategy_instance_id=binding.strategy_instance_id,
                    pos_id="pos-adopted",
                    previous_revision_id=initial.id,
                    source="management_replace",
                    status="active",
                    protection_json='{"take_profit_order_ids":["tp-current"],"stop_loss_order_ids":["sl-current"]}',
                    created_at=NOW + timedelta(minutes=1),
                    updated_at=NOW + timedelta(minutes=1),
                ),
                PositionProtectionRevision(
                    venue="deepcoin",
                    execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id,
                    strategy_instance_id="foreign-strategy",
                    pos_id="pos-foreign",
                    source="entry_submit",
                    status="active",
                    protection_json='{"take_profit_order_ids":["foreign"]}',
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.commit()
        lifecycle_id = lifecycle.id

    detail = load_strategy_record_detail(
        session_factory, lifecycle_id=lifecycle_id, group_labels_by_chat_id={}
    )

    assert detail is not None
    assert detail["execution"]["protection_revisions"] == [
        {
            "id": 1,
            "pos_id": "pos-adopted",
            "previous_revision_id": None,
            "source": "entry_submit",
            "status": "superseded",
            "protection": {
                "take_profit_order_ids": ["tp-initial"],
                "stop_loss_order_ids": ["sl-initial"],
            },
            "timestamp": NOW,
        },
        {
            "id": 2,
            "pos_id": "pos-adopted",
            "previous_revision_id": 1,
            "source": "management_replace",
            "status": "active",
            "protection": {
                "take_profit_order_ids": ["tp-current"],
                "stop_loss_order_ids": ["sl-current"],
            },
            "timestamp": NOW + timedelta(minutes=1),
        },
    ]
    assert "foreign" not in str(detail["execution"]["protection_revisions"])


def test_strategy_detail_marks_ambiguous_trigger_protection_refusal_actionable(tmp_path):
    session_factory = create_session_factory(tmp_path / "refused-protection.db")
    with session_factory() as session:
        binding, lifecycle, leg = _seed_trigger_entry_strategy(session)
        session.add(
            PositionAttributionAudit(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                venue="deepcoin",
                pos_id="pos-adopted",
                event_type="protection_adoption_refused",
                prior_state="verified",
                new_state="protection_adoption_refused",
                fingerprint="a" * 64,
                evidence_json='{"reason":"trigger_entry_tpsl_not_unique","candidate_order_ids":["one","two"]}',
                created_at=NOW,
            )
        )
        session.commit()
        lifecycle_id = lifecycle.id

    detail = load_strategy_record_detail(
        session_factory, lifecycle_id=lifecycle_id, group_labels_by_chat_id={}
    )

    assert detail["execution"]["protection_adoption"] == {
        "state": "refused",
        "order_ids": [],
        "evidence_sources": [],
        "refusal_codes": ["trigger_entry_tpsl_not_unique"],
    }
    assert any(
        item["kind"] == "protection_adoption_refused"
        and item["status"] == "保护单归属未验证"
        for item in detail["timeline"]
    )


def test_strategy_detail_projects_safe_trigger_protection_recovery_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "trigger-recovery-detail.db")
    with session_factory() as session:
        binding, lifecycle, leg = _seed_trigger_entry_strategy(session)
        intent = TriggerProtectionIntent(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            request_fingerprint="a" * 64,
            pre_submit_tpsl_baseline_json='{"request":"must-not-render"}',
            correlation_id="recovery-detail-1",
            parent_trigger_order_id="parent-trigger-1",
            recovery_state="retry_scheduled",
            retry_attempts=2,
            adopted_order_id="adopted-tpsl-1",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(intent)
        session.flush()
        session.add(
            TriggerProtectionStopRescue(
                trigger_protection_intent_id=intent.id,
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                pos_id="pos-adopted",
                status="blocked",
                reason_code="rescue_opaque_take_profit_present",
                exchange_order_id="rescue-order-must-not-render",
                request_json='{"passphrase":"must-not-render"}',
                response_json='{"raw":"must-not-render"}',
                error_json='{"message":"must-not-render"}',
                planned_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
        lifecycle_id = lifecycle.id
        leg_id = int(leg.id)

    detail = load_strategy_record_detail(
        session_factory, lifecycle_id=lifecycle_id, group_labels_by_chat_id={}
    )

    assert detail["execution"]["trigger_protection_recovery"] == [
        {
            "intent_id": 1,
            "parent_order_id": "parent-trigger-1",
            "pos_id": "pos-adopted",
            "recovery_state": "retry_scheduled",
            "retry_attempts": 2,
            "adopted_tpsl_order_ids": ["adopted-tpsl-1"],
            "refusal_code": "rescue_opaque_take_profit_present",
            "stop_rescue": {"state": "blocked"},
        }
    ]
    recovery_timeline = next(
        item
        for item in detail["timeline"]
        if item["kind"] == "trigger_protection_recovery"
    )
    assert recovery_timeline["source"] == {
        "table": "trigger_protection_intents",
        "id": 1,
        "execution_order_leg_id": leg_id,
        "parent_order_id": "parent-trigger-1",
        "pos_id": "pos-adopted",
        "recovery_state": "retry_scheduled",
        "retry_attempts": 2,
        "adopted_tpsl_order_ids": ["adopted-tpsl-1"],
        "refusal_code": "rescue_opaque_take_profit_present",
        "stop_rescue_state": "blocked",
    }
    assert "must-not-render" not in str(detail["execution"]["trigger_protection_recovery"])
    assert "rescue-order-must-not-render" not in str(
        detail["execution"]["trigger_protection_recovery"]
    )
    assert "must-not-render" not in str(recovery_timeline)


def test_strategy_detail_fails_closed_on_trigger_recovery_foreign_key_mismatch(tmp_path):
    session_factory = create_session_factory(tmp_path / "trigger-recovery-fk.db")
    with session_factory() as session:
        binding, lifecycle, leg = _seed_trigger_entry_strategy(session)
        mismatched_intent = TriggerProtectionIntent(
            venue="deepcoin",
            execution_binding_id=binding.id + 1,
            execution_order_leg_id=leg.id,
            request_fingerprint="d" * 64,
            pre_submit_tpsl_baseline_json="{}",
            correlation_id="recovery-fk-mismatch",
            recovery_state="pending",
            retry_attempts=0,
        )
        session.add(mismatched_intent)
        session.commit()
        lifecycle_id = lifecycle.id

    detail = load_strategy_record_detail(
        session_factory, lifecycle_id=lifecycle_id, group_labels_by_chat_id={}
    )

    assert detail["execution"]["trigger_protection_recovery"] == []


def test_strategy_detail_ignores_stop_rescue_with_mismatched_foreign_keys(tmp_path):
    session_factory = create_session_factory(tmp_path / "trigger-rescue-fk.db")
    with session_factory() as session:
        binding, lifecycle, leg = _seed_trigger_entry_strategy(session)
        intent = TriggerProtectionIntent(
            venue="deepcoin",
            execution_binding_id=binding.id,
            execution_order_leg_id=leg.id,
            request_fingerprint="e" * 64,
            pre_submit_tpsl_baseline_json="{}",
            correlation_id="rescue-fk-mismatch",
            recovery_state="pending",
            retry_attempts=0,
        )
        session.add(intent)
        session.flush()
        session.add(
            TriggerProtectionStopRescue(
                trigger_protection_intent_id=intent.id,
                execution_binding_id=binding.id + 1,
                execution_order_leg_id=leg.id + 1,
                pos_id="pos-adopted",
                status="submitted",
                reason_code="rescue_opaque_take_profit_present",
            )
        )
        session.commit()
        lifecycle_id = lifecycle.id

    detail = load_strategy_record_detail(
        session_factory, lifecycle_id=lifecycle_id, group_labels_by_chat_id={}
    )

    assert detail["execution"]["trigger_protection_recovery"] == [
        {
            "intent_id": 1,
            "parent_order_id": None,
            "pos_id": "pos-adopted",
            "recovery_state": "pending",
            "retry_attempts": 0,
            "adopted_tpsl_order_ids": [],
            "refusal_code": None,
            "stop_rescue": {"state": "none"},
        }
    ]


def test_strategy_detail_ignores_stale_trigger_protection_refusal_for_old_position(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "stale-refused-protection.db")
    with session_factory() as session:
        binding, lifecycle, leg = _seed_trigger_entry_strategy(session)
        session.add(
            PositionAttributionAudit(
                execution_binding_id=binding.id,
                execution_order_leg_id=leg.id,
                venue="deepcoin",
                pos_id="pos-old",
                event_type="protection_adoption_refused",
                prior_state="verified",
                new_state="protection_adoption_refused",
                fingerprint="b" * 64,
                evidence_json='{"reason":"trigger_entry_tpsl_not_unique"}',
                created_at=NOW,
            )
        )
        session.commit()
        lifecycle_id = lifecycle.id

    detail = load_strategy_record_detail(
        session_factory, lifecycle_id=lifecycle_id, group_labels_by_chat_id={}
    )

    assert detail["execution"]["protection_adoption"] == {
        "state": "unverified",
        "order_ids": [],
        "evidence_sources": [],
        "refusal_codes": [],
    }
    assert not any(
        item["kind"] == "protection_adoption_refused" for item in detail["timeline"]
    )


def test_load_strategy_record_detail_marks_missing_evidence_explicitly(tmp_path):
    session_factory = create_session_factory(tmp_path / "partial-detail.db")
    with session_factory() as session:
        lifecycle = StrategyLifecycle(
            chat_id=20,
            message_id=202,
            symbol="ETHUSDT",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=lifecycle_id,
        group_labels_by_chat_id={20: "峰哥"},
    )

    assert detail is not None
    assert detail["execution"]["binding"] is None
    assert detail["execution"]["exchange_evidence"] == {
        "state": "missing",
        "reason": "未提供交易所快照；详情加载器不会主动调用交易所",
    }
    assert detail["evidence"]["missing"] >= [
        "signal_candidate",
        "raw_message",
        "recognition_decision",
        "execution_binding",
        "exchange_snapshot",
    ]


def test_strategy_detail_never_echoes_malformed_json_secrets(tmp_path):
    session_factory = create_session_factory(tmp_path / "malformed-secret-detail.db")
    malformed = '{"api_key":"super-secret-api-key","token":"super-secret-token"'
    with session_factory() as session:
        candidate = SignalCandidate(raw_message_id=7001, symbol="BTCUSDT", side="long")
        session.add(candidate)
        session.flush()
        decision = _decision(candidate.raw_message_id)
        decision.authoritative_payload_json = malformed
        binding = _binding(
            chat_id=10,
            message_id=701,
            symbol="BTCUSDT",
            strategy_instance_id="malformed-detail",
        )
        binding.payload_json = malformed
        session.add_all([decision, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=10,
            message_id=701,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            execution_binding_id=binding.id,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=lifecycle_id,
        group_labels_by_chat_id={10: "大镖客"},
    )

    assert detail is not None
    rendered = repr(detail)
    assert "super-secret-api-key" not in rendered
    assert "super-secret-token" not in rendered
    parse_error = detail["evidence"]["recognition_decision"]["authoritative_payload"]
    assert parse_error.keys() >= {"_parse_error", "error", "raw_length", "sha256"}
    assert parse_error["_parse_error"] is True
    assert parse_error["raw_length"] == len(malformed)
    assert len(parse_error["sha256"]) == 64
    assert "raw" not in parse_error


def test_strategy_detail_redacts_nested_secrets_recursively(tmp_path):
    session_factory = create_session_factory(tmp_path / "nested-secret-detail.db")
    with session_factory() as session:
        candidate = SignalCandidate(raw_message_id=7101, symbol="BTCUSDT", side="long")
        session.add(candidate)
        session.flush()
        decision = _decision(candidate.raw_message_id)
        decision.authoritative_payload_json = (
            '{"outer":{"api_key":"nested-key","items":'
            '[{"token":"nested-token"}]},"safe":"visible"}'
        )
        session.add(decision)
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=10,
            message_id=711,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=lifecycle_id,
        group_labels_by_chat_id={},
    )

    payload = detail["evidence"]["recognition_decision"]["authoritative_payload"]
    assert payload == {
        "outer": {
            "api_key": "[REDACTED]",
            "items": [{"token": "[REDACTED]"}],
        },
        "safe": "visible",
    }


def test_strategy_detail_preserves_role_specific_messages_and_decisions(tmp_path):
    session_factory = create_session_factory(tmp_path / "role-detail.db")
    with session_factory() as session:
        # Deliberately insert out of role order and use tied timestamps.
        exit_raw = RawMessage(chat_id=10, message_id=803, posted_at=NOW, text="平仓")
        entry_raw = RawMessage(chat_id=10, message_id=801, posted_at=NOW, text="做多")
        management_raw = RawMessage(chat_id=10, message_id=802, posted_at=NOW, text="移损")
        session.add_all([exit_raw, entry_raw, management_raw])
        session.flush()
        entry_candidate = SignalCandidate(
            raw_message_id=entry_raw.id,
            symbol="BTCUSDT",
            side="long",
            event_type="entry_signal",
        )
        exit_candidate = SignalCandidate(
            raw_message_id=exit_raw.id,
            symbol="BTCUSDT",
            side="long",
            event_type="exit_signal",
        )
        entry_decision = _decision(entry_raw.id)
        management_decision = _decision(management_raw.id)
        exit_decision = _decision(exit_raw.id)
        for decision in (entry_decision, management_decision, exit_decision):
            decision.created_at = NOW
            decision.updated_at = NOW
        binding = _binding(
            chat_id=10,
            message_id=801,
            symbol="BTCUSDT",
            strategy_instance_id="role-detail",
        )
        session.add_all(
            [
                entry_candidate,
                exit_candidate,
                entry_decision,
                management_decision,
                exit_decision,
                binding,
            ]
        )
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=entry_candidate.id,
            exit_signal_candidate_id=exit_candidate.id,
            chat_id=10,
            message_id=801,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="exited",
            signal_at=NOW,
            entered_at=NOW,
            exited_at=NOW,
            execution_binding_id=binding.id,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.flush()
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="role-detail-batch",
                raw_message_id=management_raw.id,
                recognition_decision_id=management_decision.id,
                recognition_generation="mimo_only_v2",
                target_lifecycle_id=lifecycle.id,
                strategy_instance_id=str(binding.strategy_instance_id),
                execution_binding_id=binding.id,
                intent="risk_update",
                effective_action="move_stop",
                execution_mode="live",
                status="succeeded",
                target_fingerprint="role-detail-target",
                planned_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()
        lifecycle_id = lifecycle.id
        entry_raw_id = entry_raw.id
        management_raw_id = management_raw.id
        exit_raw_id = exit_raw.id

    detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=lifecycle_id,
        group_labels_by_chat_id={},
    )

    role_events = [
        (item["kind"], item["role"], item["source"]["raw_message_id"])
        for item in detail["timeline"]
        if item["kind"] in {"message", "recognition"}
    ]
    assert role_events == [
        ("message", "entry", entry_raw_id),
        ("message", "management", management_raw_id),
        ("message", "exit", exit_raw_id),
        ("recognition", "entry", entry_raw_id),
        ("recognition", "management", management_raw_id),
        ("recognition", "exit", exit_raw_id),
    ]
    assert len({item["event_id"] for item in detail["timeline"]}) == len(
        detail["timeline"]
    )
    assert [item["role"] for item in detail["evidence"]["recognition_decisions"]] == [
        "entry",
        "management",
        "exit",
    ]


def test_strategy_detail_entry_message_fallback_is_not_management_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "entry-fallback-detail.db")
    with session_factory() as session:
        entry_raw = RawMessage(
            chat_id=20,
            message_id=901,
            posted_at=NOW - timedelta(hours=2),
            text="入口原文",
        )
        management_raw = RawMessage(
            chat_id=20,
            message_id=902,
            posted_at=NOW - timedelta(hours=1),
            text="管理原文",
        )
        session.add_all([entry_raw, management_raw])
        session.flush()
        management_decision = _decision(management_raw.id)
        binding = _binding(
            chat_id=20,
            message_id=901,
            symbol="ETHUSDT",
            strategy_instance_id="fallback-detail",
        )
        session.add_all([management_decision, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=20,
            message_id=901,
            symbol="ETHUSDT",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW - timedelta(hours=2),
            entered_at=NOW - timedelta(hours=2),
            execution_binding_id=binding.id,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.flush()
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="fallback-detail-batch",
                raw_message_id=management_raw.id,
                recognition_decision_id=management_decision.id,
                recognition_generation="mimo_only_v2",
                target_lifecycle_id=lifecycle.id,
                strategy_instance_id=str(binding.strategy_instance_id),
                execution_binding_id=binding.id,
                intent="risk_update",
                effective_action="move_stop",
                execution_mode="live",
                status="succeeded",
                target_fingerprint="fallback-detail-target",
                planned_at=NOW - timedelta(hours=1),
                created_at=NOW - timedelta(hours=1),
                updated_at=NOW,
            )
        )
        session.commit()
        lifecycle_id = lifecycle.id
        entry_raw_id = entry_raw.id
        management_raw_id = management_raw.id

    detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=lifecycle_id,
        group_labels_by_chat_id={},
    )

    assert detail["evidence"]["raw_message"]["id"] == entry_raw_id
    assert detail["evidence"]["raw_message"]["text"] == "入口原文"
    assert [
        (item["role"], item["source"]["raw_message_id"])
        for item in detail["timeline"]
        if item["kind"] == "message"
    ] == [("entry", entry_raw_id), ("management", management_raw_id)]


def test_strategy_detail_falls_back_to_raw_message_candidate_when_link_missing(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "entry-candidate-fallback.db")
    with session_factory() as session:
        entry_raw = RawMessage(
            chat_id=20,
            message_id=9610,
            posted_at=NOW,
            text="BTC short 64400-64700",
        )
        session.add(entry_raw)
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=entry_raw.id,
            symbol="BTC",
            side="short",
            parse_source="mimo_authoritative",
            event_type="entry_signal",
            review_status="pending",
        )
        decision = _decision(entry_raw.id)
        session.add_all([candidate, decision])
        session.flush()
        lifecycle = StrategyLifecycle(
            chat_id=20,
            message_id=9610,
            symbol="BTC",
            side="short",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id
        candidate_id = candidate.id

    detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=lifecycle_id,
        group_labels_by_chat_id={},
    )

    assert detail["overview"]["recognition_evidence_state"] == "present"
    assert detail["evidence"]["signal_candidate"]["id"] == candidate_id
    assert "signal_candidate" not in detail["evidence"]["missing"]
    assert "recognition_decision" not in detail["evidence"]["missing"]


def _strategy_record(
    *, pos_id: str = "pos-1", venue: str = "deepcoin"
) -> dict[str, object]:
    return {
        "lifecycle_id": 1,
        "pos_id": pos_id,
        "venue": venue,
        "attribution_state": "live_bound",
        "attention": None,
        "attention_reasons": [],
    }


def _exchange_position(
    *,
    pos_id: str = "pos-1",
    attribution_state: str,
    reasons: list[str] | None = None,
    protection_status: str = "protected",
) -> dict[str, object]:
    return {
        "pos_id": pos_id,
        "symbol": "BTCUSDT",
        "side": "long",
        "protection_status": protection_status,
        "attribution": {
            "state": attribution_state,
            "reasons": reasons or [],
        },
    }


def _seed_trigger_entry_strategy(session):
    binding = _binding(
        chat_id=88,
        message_id=808,
        symbol="BTCUSDT",
        strategy_instance_id="trigger-protection-strategy",
        status="active",
    )
    binding.pos_id = "pos-adopted"
    session.add(binding)
    session.flush()
    lifecycle = StrategyLifecycle(
        chat_id=88,
        message_id=808,
        symbol="BTCUSDT",
        side="long",
        lifecycle_status="entered",
        signal_at=NOW,
        entered_at=NOW,
        stop_loss=60_000,
        execution_binding_id=binding.id,
        updated_at=NOW,
    )
    leg = ExecutionOrderLeg(
        execution_binding_id=binding.id,
        strategy_instance_id=binding.strategy_instance_id,
        leg_index=0,
        purpose="entry",
        order_kind="trigger_limit",
        order_id="trigger-entry-1",
        client_order_id="trigger-client-1",
        pos_id="pos-adopted",
        venue="deepcoin",
        attribution_status="verified",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add_all([lifecycle, leg])
    session.flush()
    return binding, lifecycle, leg


def test_exchange_enrichment_confirms_one_uniquely_bound_position():
    enriched = enrich_strategy_records_with_exchange(
        [_strategy_record()],
        exchange_snapshot={
            "positions": [_exchange_position(attribution_state="bound")],
            "error": None,
        },
    )

    assert enriched[0]["exchange_state"] == "confirmed"
    assert enriched[0]["attribution_state"] == "bound"
    assert enriched[0]["real_position"]["pos_id"] == "pos-1"
    assert enriched[0]["attention"] is None


def test_exchange_enrichment_confirms_every_verified_position_leg():
    record = _strategy_record(pos_id="pos-a,pos-b")
    record["pos_ids"] = ["pos-a", "pos-b"]

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={
            "positions": [
                _exchange_position(pos_id="pos-a", attribution_state="bound"),
                _exchange_position(pos_id="pos-b", attribution_state="bound"),
            ],
            "error": None,
        },
    )

    assert len(enriched) == 1
    assert enriched[0]["exchange_state"] == "confirmed"
    assert enriched[0]["attribution_state"] == "bound"
    assert enriched[0]["real_position"] is None
    assert [row["pos_id"] for row in enriched[0]["real_positions"]] == [
        "pos-a",
        "pos-b",
    ]
    assert enriched[0]["attention"] is None


def test_exchange_enrichment_marks_unattributed_position_for_attention():
    enriched = enrich_strategy_records_with_exchange(
        [_strategy_record()],
        exchange_snapshot={
            "positions": [
                _exchange_position(
                    attribution_state="unassigned",
                    reasons=["交易所仓位没有持久化策略归属"],
                )
            ],
            "error": None,
        },
    )

    assert enriched[0]["exchange_state"] == "attention"
    assert enriched[0]["attribution_state"] == "unassigned"
    assert enriched[0]["attention"]["code"] == "unattributed_position"
    assert "没有持久化策略归属" in enriched[0]["attention"]["reason"]


def test_exchange_enrichment_fails_closed_for_ambiguous_or_conflicting_evidence():
    records = [_strategy_record(pos_id="pos-ambiguous"), _strategy_record(pos_id="pos-conflict")]
    snapshot = {
        "positions": [
            _exchange_position(
                pos_id="pos-ambiguous",
                attribution_state="candidate",
                reasons=["存在多个候选策略"],
            ),
            _exchange_position(
                pos_id="pos-conflict",
                attribution_state="conflict",
                reasons=["entry leg 证据冲突"],
            ),
        ],
        "error": None,
    }

    ambiguous, conflict = enrich_strategy_records_with_exchange(
        records,
        exchange_snapshot=snapshot,
    )

    assert ambiguous["exchange_state"] == "unconfirmed"
    assert ambiguous["attribution_state"] == "ambiguous"
    assert ambiguous["attention"]["code"] == "attribution_ambiguous"
    assert "多个候选" in ambiguous["attention"]["reason"]
    assert conflict["exchange_state"] == "attention"
    assert conflict["attribution_state"] == "conflict"
    assert conflict["attention"]["code"] == "attribution_conflict"
    assert "证据冲突" in conflict["attention"]["reason"]


def test_exchange_enrichment_does_not_duplicate_one_expected_conflicting_pos_id():
    enriched = enrich_strategy_records_with_exchange(
        [_strategy_record(pos_id="pos-duplicate")],
        exchange_snapshot={
            "positions": [
                _exchange_position(
                    pos_id="pos-duplicate", attribution_state="bound"
                ),
                _exchange_position(
                    pos_id="pos-duplicate", attribution_state="bound"
                ),
            ],
            "error": None,
        },
    )

    assert len(enriched) == 1
    assert enriched[0]["attention"]["code"] == "attribution_conflict"


def test_exchange_enrichment_respects_authoritative_empty_entry_leg_set():
    record = _strategy_record(pos_id="pos-a,pos-b")
    record["pos_ids"] = []
    record["position_ids_authoritative"] = True

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={
            "positions": [
                _exchange_position(pos_id="pos-a", attribution_state="unassigned"),
                _exchange_position(pos_id="pos-b", attribution_state="unassigned"),
            ],
            "error": None,
        },
    )

    lifecycle_record = next(row for row in enriched if row["lifecycle_id"] == 1)
    assert lifecycle_record["exchange_state"] == "confirmed"
    assert lifecycle_record["real_positions"] == []
    assert all(
        reason["code"] != "position_missing"
        for reason in lifecycle_record["attention_reasons"]
    )


def test_exchange_enrichment_never_renders_unavailable_exchange_as_zero():
    enriched = enrich_strategy_records_with_exchange(
        [_strategy_record()],
        exchange_snapshot={"positions": [], "error": "exchange unavailable"},
    )

    assert enriched[0]["exchange_state"] == "unknown"
    assert enriched[0]["real_position"] is None
    assert enriched[0]["attention"]["code"] == "exchange_unavailable"


def test_exchange_enrichment_fails_closed_when_live_bound_position_is_missing():
    enriched = enrich_strategy_records_with_exchange(
        [_strategy_record(pos_id="pos-missing")],
        exchange_snapshot={"positions": [], "error": None},
    )

    assert enriched[0]["exchange_state"] == "attention"
    assert enriched[0]["real_position"] is None
    assert enriched[0]["attribution_state"] == "conflict"
    assert enriched[0]["attention"]["code"] == "position_missing"
    assert "pos-missing" in enriched[0]["attention"]["reason"]


def test_exchange_enrichment_adds_safe_synthetic_row_for_exchange_orphan():
    enriched = enrich_strategy_records_with_exchange(
        [],
        exchange_snapshot={
            "positions": [
                _exchange_position(
                    pos_id="orphan/position 1",
                    attribution_state="unassigned",
                    reasons=["没有本地策略归属"],
                )
            ],
            "error": None,
        },
    )

    assert len(enriched) == 1
    orphan = enriched[0]
    assert orphan.keys() >= {
        "lifecycle_id",
        "chat_id",
        "group_name",
        "message_id",
        "symbol",
        "side",
        "lifecycle_state",
        "recognition_state",
        "execution_state",
        "attribution_state",
        "attention",
        "attention_reasons",
        "latest_changed_at",
        "detail_href",
        "exchange_state",
        "real_position",
    }
    assert orphan["lifecycle_id"] is None
    assert orphan["lifecycle_state"] == "exchange_only"
    assert orphan["attention"]["code"] == "unattributed_position"
    assert orphan["detail_href"] == "/?view=positions&pos_id=orphan%2Fposition+1"
    assert orphan["real_position"]["pos_id"] == "orphan/position 1"


def test_exchange_enrichment_never_matches_other_venue_on_reused_pos_id():
    enriched = enrich_strategy_records_with_exchange(
        [_strategy_record(pos_id="reused-pos", venue="gate")],
        exchange_snapshot={
            "positions": [
                _exchange_position(
                    pos_id="reused-pos",
                    attribution_state="unassigned",
                )
            ],
            "error": None,
        },
    )

    gate_record, deepcoin_orphan = enriched
    assert gate_record["venue"] == "gate"
    assert gate_record["exchange_state"] == "not_applicable"
    assert gate_record["real_position"] is None
    assert gate_record["attention"] is None
    assert deepcoin_orphan["venue"] == "deepcoin"
    assert deepcoin_orphan["lifecycle_id"] is None
    assert deepcoin_orphan["pos_id"] == "reused-pos"
    assert deepcoin_orphan["attention"]["code"] == "unattributed_position"


def test_load_live_bindings_without_lifecycle_emits_fail_closed_evidence(tmp_path):
    session_factory = create_session_factory(tmp_path / "orphan-binding.db")
    with session_factory() as session:
        orphan = _binding(
            chat_id=77,
            message_id=707,
            symbol="SOLUSDT",
            strategy_instance_id="orphan-binding",
        )
        orphan.pos_id = "orphan/binding 1"
        orphan.venue = " DeepCoin "
        other_venue = _binding(
            chat_id=88,
            message_id=808,
            symbol="BTCUSDT",
            strategy_instance_id="gate-orphan-binding",
        )
        other_venue.pos_id = "orphan/binding 1"
        other_venue.venue = "gate"
        session.add_all([orphan, other_venue])
        session.commit()
        binding_id = orphan.id

    rows = load_live_bindings_without_lifecycle(
        session_factory,
        group_labels_by_chat_id={77: "孤立绑定群"},
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.keys() >= {
        "lifecycle_id",
        "binding_id",
        "chat_id",
        "group_name",
        "message_id",
        "symbol",
        "side",
        "lifecycle_state",
        "recognition_state",
        "execution_state",
        "attribution_state",
        "pos_id",
        "attention",
        "attention_reasons",
        "latest_changed_at",
        "detail_href",
    }
    assert row["lifecycle_id"] is None
    assert row["binding_id"] == binding_id
    assert row["venue"] == "deepcoin"
    assert row["group_name"] == "孤立绑定群"
    assert row["lifecycle_state"] == "binding_without_lifecycle"
    assert row["attention"]["code"] == "binding_without_lifecycle"
    assert row["detail_href"] == "/?view=positions&pos_id=orphan%2Fbinding+1"


def test_exchange_enrichment_requires_concrete_protection_mismatch_evidence():
    unprotected = _exchange_position(
        pos_id="pos-unprotected",
        attribution_state="bound",
        protection_status="unprotected",
    )
    explicit_mismatch = _exchange_position(
        pos_id="pos-mismatch",
        attribution_state="bound",
        protection_status="mismatch",
    )
    explicit_mismatch["protection_mismatch_reason"] = "止损价与交易所证据不一致"

    plain, mismatch = enrich_strategy_records_with_exchange(
        [
            _strategy_record(pos_id="pos-unprotected"),
            _strategy_record(pos_id="pos-mismatch"),
        ],
        exchange_snapshot={
            "positions": [unprotected, explicit_mismatch],
            "error": None,
        },
    )

    assert plain["attention"] is None
    assert mismatch["attention"]["code"] == "protection_mismatch"
    assert "止损价" in mismatch["attention"]["reason"]


def test_exchange_enrichment_flags_management_execution_drift_from_exact_stop():
    record = _strategy_record(pos_id="pos-managed")
    record.update(
        {
            "pos_ids": ["pos-managed"],
            "expected_stop_loss": 63_575.875,
            "management_signal_message_id": 1451,
            "management_batch_statuses": [],
        }
    )
    position = _exchange_position(
        pos_id="pos-managed",
        attribution_state="bound",
        protection_status="protected",
    )
    position["stop_loss_text"] = "60500"

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={"positions": [position], "error": None},
    )

    assert enriched[0]["exchange_state"] == "attention"
    assert enriched[0]["attention"] == {
        "severity": "critical",
        "code": "management_execution_drift",
        "label": "仓位管理与交易所结果漂移",
        "reason": "消息 #1451 后策略止损 63575.875，但 Deepcoin 精确仓位证据为 60500",
    }


def test_blocked_protection_management_batch_requires_attention(tmp_path):
    session_factory = create_session_factory(tmp_path / "blocked-management.db")
    with session_factory() as session:
        entry_raw = RawMessage(chat_id=10, message_id=101, posted_at=NOW, text="BTC long")
        management_raw = RawMessage(
            chat_id=10,
            message_id=102,
            posted_at=NOW + timedelta(minutes=5),
            text="止盈一半，止损上移",
        )
        session.add_all([entry_raw, management_raw])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=entry_raw.id,
            symbol="BTCUSDT",
            side="long",
            event_type="entry_signal",
        )
        session.add(candidate)
        session.flush()
        decision = _decision(entry_raw.id)
        session.add(decision)
        session.flush()
        binding = _binding(
            chat_id=10,
            message_id=101,
            symbol="BTCUSDT",
            strategy_instance_id="blocked-protection-strategy",
            status="active",
        )
        binding.pos_id = "pos-managed"
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=10,
            message_id=101,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
            stop_loss=62000,
            execution_binding_id=binding.id,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.flush()
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="blocked-protection",
                raw_message_id=management_raw.id,
                recognition_decision_id=decision.id,
                recognition_generation="generation-1",
                target_lifecycle_id=lifecycle.id,
                strategy_instance_id=binding.strategy_instance_id,
                execution_binding_id=binding.id,
                intent="partial_then_break_even",
                effective_action="partial_then_break_even",
                execution_mode="live",
                requested_fraction=0.5,
                effective_fraction=0.5,
                partial_round_before=0,
                status="blocked",
                reason_code="protection_ambiguous_global_assignment",
                target_fingerprint="blocked-protection-target",
                target_snapshot_json="{}",
                planned_at=NOW + timedelta(minutes=6),
                created_at=NOW + timedelta(minutes=6),
                updated_at=NOW + timedelta(minutes=6),
            )
        )
        session.commit()

    rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "测试群"},
        filter_name="needs_attention",
        now=NOW + timedelta(minutes=10),
    )

    assert len(rows) == 1
    assert rows[0]["attention"] == {
        "severity": "warning",
        "code": "management_blocked",
        "label": "仓位管理已阻断待处理",
    }
    assert rows[0]["management_batch_statuses"] == ["blocked"]
    assert "management_blocked" in {
        reason["code"] for reason in rows[0]["attention_reasons"]
    }


def test_blocked_unavailable_protection_evidence_requires_attention(tmp_path):
    session_factory = create_session_factory(tmp_path / "blocked-unavailable.db")
    with session_factory() as session:
        entry_raw = RawMessage(chat_id=10, message_id=201, posted_at=NOW, text="ETH long")
        management_raw = RawMessage(
            chat_id=10,
            message_id=202,
            posted_at=NOW + timedelta(minutes=5),
            text="移动止损",
        )
        session.add_all([entry_raw, management_raw])
        session.flush()
        candidate = SignalCandidate(
            raw_message_id=entry_raw.id,
            symbol="ETHUSDT",
            side="long",
            event_type="entry_signal",
        )
        session.add(candidate)
        session.flush()
        decision = _decision(entry_raw.id)
        session.add(decision)
        session.flush()
        binding = _binding(
            chat_id=10,
            message_id=201,
            symbol="ETHUSDT",
            strategy_instance_id="blocked-unavailable-strategy",
            status="active",
        )
        binding.pos_id = "pos-unavailable"
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=10,
            message_id=201,
            symbol="ETHUSDT",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
            stop_loss=1800,
            execution_binding_id=binding.id,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.flush()
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="blocked-unavailable",
                raw_message_id=management_raw.id,
                recognition_decision_id=decision.id,
                recognition_generation="generation-1",
                target_lifecycle_id=lifecycle.id,
                strategy_instance_id=binding.strategy_instance_id,
                execution_binding_id=binding.id,
                intent="move_stop_to_break_even",
                effective_action="move_stop_to_break_even",
                execution_mode="live",
                partial_round_before=0,
                status="blocked",
                reason_code="target_protection_evidence_unavailable",
                target_fingerprint="blocked-unavailable-target",
                target_snapshot_json="{}",
                planned_at=NOW + timedelta(minutes=6),
                created_at=NOW + timedelta(minutes=6),
                updated_at=NOW + timedelta(minutes=6),
            )
        )
        session.commit()

    rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "测试群"},
        filter_name="needs_attention",
        now=NOW + timedelta(minutes=10),
    )

    assert len(rows) == 1
    assert "management_blocked" in {
        reason["code"] for reason in rows[0]["attention_reasons"]
    }


def test_exchange_enrichment_does_not_infer_drift_without_management_message():
    record = _strategy_record(pos_id="pos-entry-only")
    record["expected_stop_loss"] = 63_575.875
    position = _exchange_position(
        pos_id="pos-entry-only",
        attribution_state="bound",
        protection_status="protected",
    )
    position["stop_loss_text"] = "60500"

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={"positions": [position], "error": None},
    )

    assert enriched[0]["exchange_state"] == "confirmed"
    assert enriched[0]["attention"] is None


def test_exchange_enrichment_keeps_absent_protection_separate_from_numeric_drift():
    record = _strategy_record(pos_id="pos-unprotected")
    record.update(
        {
            "expected_stop_loss": 63_575.875,
            "management_signal_message_id": 1451,
            "management_batch_statuses": [],
        }
    )
    position = _exchange_position(
        pos_id="pos-unprotected",
        attribution_state="bound",
        protection_status="unprotected",
    )

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={"positions": [position], "error": None},
    )

    assert all(
        reason["code"] != "management_execution_drift"
        for reason in enriched[0]["attention_reasons"]
    )


def test_exchange_enrichment_accepts_only_confirmed_matching_management_evidence():
    record = _strategy_record(pos_id="pos-confirmed-management")
    record.update(
        {
            "expected_stop_loss": 63_575.875,
            "management_signal_message_id": 1451,
            "management_batch_statuses": ["succeeded"],
            "management_confirmations": [
                {
                    "message_id": 1451,
                    "status": "succeeded",
                    "effective_action": "move_stop",
                    "planned_stops": [60_500],
                }
            ],
        }
    )
    position = _exchange_position(
        pos_id="pos-confirmed-management",
        attribution_state="bound",
        protection_status="protected",
    )
    position["stop_loss_text"] = "60500"

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={"positions": [position], "error": None},
    )

    assert enriched[0]["attention"] is None


def test_exchange_enrichment_compares_expected_take_profit_evidence():
    record = _strategy_record(pos_id="pos-tp-drift")
    record.update(
        {
            "expected_stop_loss": 63_575.875,
            "expected_take_profit": [64_800, 66_500],
            "management_signal_message_id": 1451,
            "management_confirmations": [],
        }
    )
    position = _exchange_position(
        pos_id="pos-tp-drift",
        attribution_state="bound",
        protection_status="protected",
    )
    position.update(
        {"stop_loss_text": "63575.875", "take_profit_text": "65000/66500"}
    )

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={"positions": [position], "error": None},
    )

    assert enriched[0]["attention"]["code"] == "management_execution_drift"
    assert "策略止盈 64800/66500" in enriched[0]["attention"]["reason"]


def test_exchange_enrichment_flags_unconfirmed_partial_take_profit_action():
    record = _strategy_record(pos_id="pos-partial-drift")
    record.update(
        {
            "expected_stop_loss": 63_575.875,
            "expected_management_action": "partial_take_profit, move_stop_to_protect",
            "management_signal_message_id": 1451,
            "management_confirmations": [],
        }
    )
    position = _exchange_position(
        pos_id="pos-partial-drift",
        attribution_state="bound",
        protection_status="protected",
    )
    position["stop_loss_text"] = "63575.875"

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={"positions": [position], "error": None},
    )

    assert enriched[0]["attention"]["code"] == "management_execution_drift"
    assert "要求部分止盈" in enriched[0]["attention"]["reason"]


def test_succeeded_partial_close_confirms_partial_take_profit_action():
    record = _strategy_record(pos_id="pos-partial-confirmed")
    record.update(
        {
            "expected_stop_loss": 63_575.875,
            "expected_management_action": "partial_take_profit",
            "management_signal_message_id": 1451,
            "management_confirmations": [
                {
                    "message_id": 1451,
                    "status": "succeeded",
                    "intent": "partial_take_profit",
                    "effective_action": "partial_close",
                    "planned_stops": [],
                }
            ],
        }
    )
    position = _exchange_position(
        pos_id="pos-partial-confirmed",
        attribution_state="bound",
        protection_status="protected",
    )
    position["stop_loss_text"] = "63575.875"

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={"positions": [position], "error": None},
    )

    assert enriched[0]["attention"] is None


def test_multi_leg_identical_take_profits_are_compared_as_one_set():
    record = _strategy_record(pos_id="pos-a,pos-b")
    record.update(
        {
            "pos_ids": ["pos-a", "pos-b"],
            "expected_stop_loss": 63_575.875,
            "expected_take_profit": [64_800, 66_500],
            "management_signal_message_id": 1451,
            "management_confirmations": [],
        }
    )
    positions = []
    for pos_id in ("pos-a", "pos-b"):
        position = _exchange_position(
            pos_id=pos_id,
            attribution_state="bound",
            protection_status="protected",
        )
        position.update(
            {"stop_loss_text": "63575.875", "take_profit_text": "64800/66500"}
        )
        positions.append(position)

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={"positions": positions, "error": None},
    )

    assert enriched[0]["attention"] is None


def test_resolved_batch_is_not_exchange_confirmation_for_drift_suppression():
    record = _strategy_record(pos_id="pos-resolved")
    record.update(
        {
            "expected_stop_loss": 63_575.875,
            "management_signal_message_id": 1451,
            "management_confirmations": [
                {
                    "message_id": 1451,
                    "status": "resolved",
                    "effective_action": "move_stop",
                    "planned_stops": [60_500],
                }
            ],
        }
    )
    position = _exchange_position(
        pos_id="pos-resolved",
        attribution_state="bound",
        protection_status="protected",
    )
    position["stop_loss_text"] = "60500"

    enriched = enrich_strategy_records_with_exchange(
        [record],
        exchange_snapshot={"positions": [position], "error": None},
    )

    assert enriched[0]["attention"]["code"] == "management_execution_drift"


def _decision(raw_message_id: int, *, severity: str = "normal") -> RecognitionDecision:
    return RecognitionDecision(
        raw_message_id=raw_message_id,
        input_kind="text",
        authoritative_model="mimo-v2.5",
        authoritative_status="是策略",
        authoritative_payload_json="{}",
        agreement_status="agreed" if severity == "normal" else "disagreed",
        differences_json="[]",
        prompt_versions_json="{}",
        disagreement_severity=severity,
        updated_at=NOW - timedelta(minutes=30),
    )


def _binding(
    *,
    chat_id: int,
    message_id: int,
    symbol: str,
    strategy_instance_id: str,
    status: str = "open",
) -> ExecutionBinding:
    return ExecutionBinding(
        strategy_instance_id=strategy_instance_id,
        kol_id=str(chat_id),
        chat_id=chat_id,
        message_id=message_id,
        symbol=symbol,
        side="long",
        status=status,
        updated_at=NOW - timedelta(minutes=20),
    )


def _seed_strategy_records(session_factory) -> dict[str, int]:
    with session_factory() as session:
        candidates = []
        lifecycles = []
        for index, (chat_id, message_id, symbol, lifecycle_status, stop_loss) in enumerate(
            [
                (10, 101, "BTCUSDT", "pending_entry", 65_000.0),
                (10, 102, "ETHUSDT", "entered", None),
                (20, 201, "SOLUSDT", "entered", 130.0),
                (20, 202, "XRPUSDT", "pending_entry", 0.45),
                (10, 103, "DOGEUSDT", "pending_entry", 0.15),
            ],
            start=1,
        ):
            candidate = SignalCandidate(
                raw_message_id=1_000 + index,
                symbol=symbol,
                side="long",
                review_status="approved",
                created_at=NOW - timedelta(hours=index),
            )
            candidates.append(candidate)
            session.add(candidate)
            session.flush()
            lifecycle = StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=chat_id,
                message_id=message_id,
                symbol=symbol,
                side="long",
                lifecycle_status=lifecycle_status,
                signal_at=NOW - timedelta(hours=index),
                entered_at=(
                    NOW - timedelta(hours=index)
                    if lifecycle_status == "entered"
                    else None
                ),
                stop_loss=stop_loss,
                updated_at=NOW - timedelta(minutes=40 + index),
            )
            lifecycles.append(lifecycle)
            session.add(lifecycle)

        session.flush()
        for candidate in candidates:
            session.add(_decision(candidate.raw_message_id))

        missing_stop_binding = _binding(
            chat_id=10,
            message_id=102,
            symbol="ETHUSDT",
            strategy_instance_id="strategy-missing-stop",
        )
        failed_binding = _binding(
            chat_id=10,
            message_id=103,
            symbol="DOGEUSDT",
            strategy_instance_id="strategy-failed",
            status="rejected",
        )
        session.add_all([missing_stop_binding, failed_binding])
        session.flush()
        lifecycles[1].execution_binding_id = missing_stop_binding.id
        lifecycles[1].updated_at = NOW - timedelta(minutes=2)
        lifecycles[4].execution_binding_id = failed_binding.id

        disagreement = session.query(RecognitionDecision).filter_by(
            raw_message_id=candidates[3].raw_message_id
        ).one()
        disagreement.disagreement_severity = "critical"
        disagreement.agreement_status = "disagreed"
        disagreement.updated_at = NOW - timedelta(minutes=5)

        session.add(
            ExecutionEvent(
                execution_binding_id=failed_binding.id,
                strategy_instance_id="strategy-failed",
                action="open_position",
                status="failed",
                chat_id=10,
                message_id=103,
                symbol="DOGEUSDT",
                side="long",
                created_at=NOW - timedelta(minutes=10),
            )
        )
        session.commit()

        return {
            "normal": lifecycles[0].id,
            "missing_stop": lifecycles[1].id,
            "without_binding": lifecycles[2].id,
            "disagreement": lifecycles[3].id,
            "execution_failed": lifecycles[4].id,
        }


def test_load_strategy_record_summaries_orders_attention_and_exposes_stable_shape(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "records.db")
    ids = _seed_strategy_records(session_factory)

    rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "大镖客", 20: "峰哥"},
        filter_name="needs_attention",
        limit=50,
        now=NOW,
    )

    assert rows[0].keys() >= {
        "lifecycle_id",
        "chat_id",
        "group_name",
        "message_id",
        "symbol",
        "side",
        "lifecycle_state",
        "recognition_state",
        "execution_state",
        "attribution_state",
        "attention",
        "latest_changed_at",
        "detail_href",
    }
    assert rows[0]["lifecycle_id"] == ids["missing_stop"]
    assert rows[0]["attention"] == {
        "severity": "critical",
        "code": "missing_stop",
        "label": "真实持仓缺少止损",
    }
    assert all(row["attention"] is not None for row in rows)
    assert [row["attention"]["severity"] for row in rows] == sorted(
        [row["attention"]["severity"] for row in rows],
        key={"critical": 0, "warning": 1, "review": 2}.get,
    )
    assert {row["attention"]["code"] for row in rows} == {
        "missing_stop",
        "entered_without_binding",
        "recognition_disagreement",
        "execution_failed",
    }


def test_strategy_summary_uses_verified_active_entry_leg_position_ids(tmp_path):
    session_factory = create_session_factory(tmp_path / "multi-leg-summary.db")
    with session_factory() as session:
        binding = _binding(
            chat_id=10,
            message_id=501,
            symbol="BTCUSDT",
            strategy_instance_id="multi-leg-summary",
            status="active",
        )
        binding.pos_id = "pos-a,pos-b"
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            execution_binding_id=binding.id,
            chat_id=10,
            message_id=501,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW,
            entered_at=NOW,
            stop_loss=60_000,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.add_all(
            [
                ExecutionOrderLeg(
                    execution_binding_id=binding.id,
                    strategy_instance_id="multi-leg-summary",
                    leg_index=index,
                    purpose="entry",
                    order_kind="market",
                    pos_id=pos_id,
                    venue="deepcoin",
                    attribution_status="verified",
                    status="active",
                    created_at=NOW,
                    updated_at=NOW,
                )
                for index, pos_id in enumerate(("pos-a", "pos-b"))
            ]
        )
        session.add(
            ExecutionOrderLeg(
                execution_binding_id=binding.id,
                strategy_instance_id="multi-leg-summary",
                leg_index=2,
                purpose="entry",
                order_kind="market",
                pos_id="pos-manually-closed",
                venue="deepcoin",
                attribution_status="verified",
                status="manually_closed",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.commit()

    rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "大漂亮"},
        filter_name="all",
        now=NOW,
    )

    assert rows[0]["pos_id"] == "pos-a,pos-b"
    assert rows[0]["pos_ids"] == ["pos-a", "pos-b"]


def test_load_strategy_record_summaries_all_includes_normal_and_filters_group(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "records.db")
    ids = _seed_strategy_records(session_factory)

    all_rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "大镖客", 20: "峰哥"},
        filter_name="all",
        limit=50,
        now=NOW,
    )
    group_rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "大镖客", 20: "峰哥"},
        filter_name="all",
        chat_id=20,
        limit=50,
        now=NOW,
    )

    normal_row = next(row for row in all_rows if row["lifecycle_id"] == ids["normal"])
    assert normal_row["attention"] is None
    assert normal_row["attention_reasons"] == []
    assert normal_row["recognition_state"] == "是策略"
    assert normal_row["detail_href"] == f"/strategy-records/{ids['normal']}"
    assert {row["chat_id"] for row in group_rows} == {20}
    assert {row["group_name"] for row in group_rows} == {"峰哥"}


def test_missing_authoritative_decision_requires_attention_without_legacy_noise(tmp_path):
    session_factory = create_session_factory(tmp_path / "missing-recognition.db")
    with session_factory() as session:
        unlinked_raw_message = RawMessage(
            chat_id=10,
            message_id=702,
            posted_at=NOW,
            text="ETH 做多",
        )
        session.add(unlinked_raw_message)
        session.flush()
        session.add(_decision(unlinked_raw_message.id))
        candidate = SignalCandidate(
            raw_message_id=7001,
            symbol="BTCUSDT",
            side="long",
            parse_source="mimo_authoritative",
        )
        session.add(candidate)
        session.flush()
        without_decision = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=10,
            message_id=701,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            stop_loss=65_000,
            updated_at=NOW,
        )
        without_candidate = StrategyLifecycle(
            chat_id=10,
            message_id=702,
            symbol="ETHUSDT",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW - timedelta(minutes=1),
            stop_loss=2_900,
            updated_at=NOW - timedelta(minutes=1),
        )
        session.add_all([without_decision, without_candidate])
        session.commit()
        without_decision_id = without_decision.id
        without_candidate_id = without_candidate.id

    rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "测试群"},
        filter_name="needs_attention",
        limit=10,
        now=NOW,
    )

    assert len(rows) == 1
    assert rows[0]["lifecycle_id"] == without_decision_id
    assert rows[0]["attention"]["code"] == "recognition_evidence_missing"
    assert rows[0]["recognition_state"] == "unknown"
    all_rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "测试群"},
        filter_name="all",
        limit=10,
        now=NOW,
    )
    without_candidate_row = next(
        row for row in all_rows if row["lifecycle_id"] == without_candidate_id
    )
    assert without_candidate_row["attention"] is None
    assert without_candidate_row["recognition_state"] == "legacy"
    detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=without_decision_id,
        group_labels_by_chat_id={10: "测试群"},
    )
    assert detail["overview"]["recognition_evidence_state"] == "missing"
    assert "recognition_decision" in detail["evidence"]["missing"]
    unlinked_detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=without_candidate_id,
        group_labels_by_chat_id={10: "测试群"},
    )
    assert unlinked_detail["overview"]["recognition_evidence_state"] == "present"
    assert unlinked_detail["overview"]["authoritative_model"] == "mimo-v2.5"
    assert "signal_candidate" in unlinked_detail["evidence"]["missing"]
    assert "recognition_decision" not in unlinked_detail["evidence"]["missing"]


def test_legacy_candidate_without_authoritative_decision_does_not_fill_attention(
    tmp_path,
):
    session_factory = create_session_factory(tmp_path / "legacy-recognition.db")
    with session_factory() as session:
        candidate = SignalCandidate(
            raw_message_id=7_101,
            symbol="BTCUSDT",
            side="long",
            parse_source="text",
            review_status="approved",
        )
        session.add(candidate)
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=10,
            message_id=710,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            stop_loss=65_000,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.commit()
        lifecycle_id = lifecycle.id

    attention_rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "测试群"},
        filter_name="needs_attention",
        limit=10,
        now=NOW,
    )
    all_rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "测试群"},
        filter_name="all",
        limit=10,
        now=NOW,
    )

    assert attention_rows == []
    assert len(all_rows) == 1
    assert all_rows[0]["lifecycle_id"] == lifecycle_id
    assert all_rows[0]["attention"] is None
    assert all_rows[0]["attention_reasons"] == []
    assert all_rows[0]["recognition_state"] == "legacy"


def test_execution_events_fail_closed_when_legacy_strategy_instance_is_not_unique(tmp_path):
    session_factory = create_session_factory(tmp_path / "event-ownership.db")
    with session_factory() as session:
        bindings = [
            _binding(chat_id=10, message_id=801 + index, symbol="BTCUSDT", strategy_instance_id="reused")
            for index in range(2)
        ]
        session.add_all(bindings)
        session.flush()
        lifecycles = []
        for index, binding in enumerate(bindings):
            candidate = SignalCandidate(
                raw_message_id=8_001 + index,
                symbol="BTCUSDT",
                side="long",
            )
            session.add(candidate)
            session.flush()
            session.add(_decision(candidate.raw_message_id))
            lifecycle = StrategyLifecycle(
                signal_candidate_id=candidate.id,
                chat_id=10,
                message_id=801 + index,
                symbol="BTCUSDT",
                side="long",
                lifecycle_status="pending_entry",
                signal_at=NOW,
                execution_binding_id=binding.id,
                updated_at=NOW,
            )
            lifecycles.append(lifecycle)
            session.add(lifecycle)
        session.flush()
        session.add_all(
            [
                ExecutionEvent(
                    execution_binding_id=bindings[0].id,
                    strategy_instance_id="reused",
                    action="submit",
                    status="succeeded",
                    created_at=NOW,
                ),
                ExecutionEvent(
                    execution_binding_id=None,
                    strategy_instance_id="reused",
                    action="legacy-reconcile",
                    status="failed",
                    created_at=NOW - timedelta(minutes=1),
                ),
            ]
        )
        session.commit()
        lifecycle_ids = [row.id for row in lifecycles]

    rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "测试群"},
        filter_name="all",
        limit=10,
        now=NOW,
    )
    by_id = {row["lifecycle_id"]: row for row in rows}
    assert by_id[lifecycle_ids[0]]["execution_state"] == "succeeded"
    assert by_id[lifecycle_ids[1]]["execution_state"] == "open"
    assert "execution_failed" not in {
        reason["code"] for reason in by_id[lifecycle_ids[0]]["attention_reasons"]
    }
    assert "execution_failed" not in {
        reason["code"] for reason in by_id[lifecycle_ids[1]]["attention_reasons"]
    }
    attention_rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "测试群"},
        filter_name="needs_attention",
        limit=10,
        now=NOW,
    )
    assert attention_rows == []

    first_detail = load_strategy_record_detail(
        session_factory, lifecycle_id=lifecycle_ids[0], group_labels_by_chat_id={10: "测试群"}
    )
    second_detail = load_strategy_record_detail(
        session_factory, lifecycle_id=lifecycle_ids[1], group_labels_by_chat_id={10: "测试群"}
    )
    assert [event["action"] for event in first_detail["execution"]["events"]] == ["submit"]
    assert second_detail["execution"]["events"] == []


def test_execution_events_use_null_binding_legacy_fallback_when_binding_is_unique(tmp_path):
    session_factory = create_session_factory(tmp_path / "unique-legacy-event.db")
    with session_factory() as session:
        candidate = SignalCandidate(raw_message_id=8_100, symbol="ETHUSDT", side="long")
        session.add(candidate)
        session.flush()
        session.add(_decision(candidate.raw_message_id))
        binding = _binding(
            chat_id=10,
            message_id=810,
            symbol="ETHUSDT",
            strategy_instance_id="unique-legacy",
        )
        session.add(binding)
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=10,
            message_id=810,
            symbol="ETHUSDT",
            side="long",
            lifecycle_status="pending_entry",
            signal_at=NOW,
            execution_binding_id=binding.id,
            updated_at=NOW,
        )
        session.add(lifecycle)
        session.flush()
        session.add(
            ExecutionEvent(
                execution_binding_id=None,
                strategy_instance_id="unique-legacy",
                action="legacy-submit",
                status="failed",
                created_at=NOW,
            )
        )
        session.commit()
        lifecycle_id = lifecycle.id

    rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "测试群"},
        filter_name="needs_attention",
        limit=10,
        now=NOW,
    )
    assert [row["lifecycle_id"] for row in rows] == [lifecycle_id]
    assert "execution_failed" in {
        reason["code"] for reason in rows[0]["attention_reasons"]
    }
    detail = load_strategy_record_detail(
        session_factory,
        lifecycle_id=lifecycle_id,
        group_labels_by_chat_id={10: "测试群"},
    )
    assert [event["action"] for event in detail["execution"]["events"]] == [
        "legacy-submit"
    ]


def test_needs_attention_is_not_hidden_by_newer_normal_records(tmp_path):
    session_factory = create_session_factory(tmp_path / "records.db")
    with session_factory() as session:
        for index in range(101):
            candidate = SignalCandidate(
                raw_message_id=20_000 + index,
                symbol="BTCUSDT",
                side="long",
            )
            session.add(candidate)
            session.flush()
            session.add_all(
                [
                    _decision(candidate.raw_message_id),
                    StrategyLifecycle(
                        signal_candidate_id=candidate.id,
                        chat_id=10,
                        message_id=1_000 + index,
                        symbol="BTCUSDT",
                        side="long",
                        lifecycle_status="pending_entry",
                        signal_at=NOW - timedelta(minutes=index),
                        stop_loss=65_000.0,
                        updated_at=NOW - timedelta(minutes=index),
                    ),
                ]
            )
        critical = StrategyLifecycle(
            chat_id=20,
            message_id=999,
            symbol="ETHUSDT",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW - timedelta(days=2),
            entered_at=NOW - timedelta(days=2),
            stop_loss=2_900.0,
            updated_at=NOW - timedelta(days=2),
        )
        session.add(critical)
        session.commit()
        critical_id = critical.id

    rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "大镖客", 20: "峰哥"},
        filter_name="needs_attention",
        limit=10,
        now=NOW,
    )

    assert [row["lifecycle_id"] for row in rows] == [critical_id]
    assert rows[0]["attention"]["code"] == "entered_without_binding"
    assert "entered_without_binding" in {
        reason["code"] for reason in rows[0]["attention_reasons"]
    }


def test_attention_reasons_and_latest_changed_at_include_all_batched_sources(tmp_path):
    session_factory = create_session_factory(tmp_path / "records.db")
    with session_factory() as session:
        candidate = SignalCandidate(
            raw_message_id=7_001,
            symbol="BTCUSDT",
            side="long",
            created_at=NOW - timedelta(hours=6),
        )
        session.add(candidate)
        session.flush()
        decision = RecognitionDecision(
            raw_message_id=candidate.raw_message_id,
            input_kind="text",
            authoritative_model="mimo-v2.5",
            authoritative_status="识别失败",
            authoritative_payload_json="{}",
            agreement_status="disagreed",
            differences_json="[]",
            prompt_versions_json="{}",
            disagreement_severity="critical",
            updated_at=NOW - timedelta(hours=4),
        )
        binding = _binding(
            chat_id=10,
            message_id=701,
            symbol="BTCUSDT",
            strategy_instance_id="strategy-multiple-reasons",
        )
        binding.updated_at = NOW - timedelta(hours=3)
        session.add_all([decision, binding])
        session.flush()
        lifecycle = StrategyLifecycle(
            signal_candidate_id=candidate.id,
            chat_id=10,
            message_id=701,
            symbol="BTCUSDT",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW - timedelta(hours=8),
            entered_at=NOW - timedelta(hours=7),
            stop_loss=None,
            execution_binding_id=binding.id,
            updated_at=NOW - timedelta(hours=5),
        )
        session.add(lifecycle)
        session.flush()
        session.add(
            ExecutionEvent(
                execution_binding_id=binding.id,
                strategy_instance_id=binding.strategy_instance_id,
                action="open_position",
                status="succeeded",
                created_at=NOW - timedelta(hours=2),
            )
        )
        latest_at = NOW - timedelta(minutes=1)
        session.add(
            StrategyManagementBatch(
                idempotency_fingerprint="multi-reason-batch",
                raw_message_id=candidate.raw_message_id,
                recognition_decision_id=decision.id,
                recognition_generation="mimo_only_v2",
                target_lifecycle_id=lifecycle.id,
                strategy_instance_id=str(binding.strategy_instance_id),
                execution_binding_id=binding.id,
                intent="risk_update",
                effective_action="move_stop",
                execution_mode="live",
                status="reconciling",
                target_fingerprint="target-multiple-reasons",
                planned_at=NOW - timedelta(hours=1),
                updated_at=latest_at,
            )
        )
        session.commit()

    rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "大镖客"},
        filter_name="needs_attention",
        limit=10,
        now=NOW,
    )

    assert len(rows) == 1
    assert rows[0]["attention"] == {
        "severity": "critical",
        "code": "recognition_failed",
        "label": "AI识别失败",
    }
    assert [reason["code"] for reason in rows[0]["attention_reasons"]] == [
        "recognition_failed",
        "recognition_disagreement",
        "missing_stop",
        "management_unconfirmed",
    ]
    assert rows[0]["latest_changed_at"] == latest_at


def test_critical_attention_precedes_newer_review_rows_before_limit(tmp_path):
    session_factory = create_session_factory(tmp_path / "records.db")
    with session_factory() as session:
        for index in range(11):
            candidate = SignalCandidate(
                raw_message_id=9_000 + index,
                symbol="XRPUSDT",
                side="long",
            )
            session.add(candidate)
            session.flush()
            session.add(_decision(candidate.raw_message_id, severity="critical"))
            session.add(
                StrategyLifecycle(
                    signal_candidate_id=candidate.id,
                    chat_id=10,
                    message_id=9_000 + index,
                    symbol="XRPUSDT",
                    side="long",
                    lifecycle_status="pending_entry",
                    signal_at=NOW - timedelta(minutes=index),
                    stop_loss=0.45,
                    updated_at=NOW - timedelta(minutes=index),
                )
            )
        critical = StrategyLifecycle(
            chat_id=20,
            message_id=8_999,
            symbol="ETHUSDT",
            side="long",
            lifecycle_status="entered",
            signal_at=NOW - timedelta(days=2),
            entered_at=NOW - timedelta(days=2),
            stop_loss=2_900.0,
            updated_at=NOW - timedelta(days=2),
        )
        session.add(critical)
        session.commit()
        critical_id = critical.id

    rows = load_strategy_record_summaries(
        session_factory,
        group_labels_by_chat_id={10: "大镖客", 20: "峰哥"},
        filter_name="needs_attention",
        limit=10,
        now=NOW,
    )

    assert len(rows) == 10
    assert rows[0]["lifecycle_id"] == critical_id
    assert rows[0]["attention"]["severity"] == "critical"
    assert all(row["attention"]["severity"] == "review" for row in rows[1:])


def test_loader_query_count_does_not_scale_with_strategy_count(tmp_path):
    session_factory = create_session_factory(tmp_path / "records.db")
    _seed_strategy_records(session_factory)
    engine = session_factory.kw["bind"]
    select_count = 0

    def count_selects(_connection, _cursor, statement, _parameters, _context, _many):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        load_strategy_record_summaries(
            session_factory,
            group_labels_by_chat_id={10: "大镖客", 20: "峰哥"},
            filter_name="all",
            limit=50,
            now=NOW,
        )
        baseline_count = select_count
        with session_factory() as session:
            for index in range(20):
                session.add(
                    StrategyLifecycle(
                        chat_id=30,
                        message_id=8_000 + index,
                        symbol="SOLUSDT",
                        side="long",
                        lifecycle_status="pending_entry",
                        signal_at=NOW - timedelta(days=1, minutes=index),
                        updated_at=NOW - timedelta(days=1, minutes=index),
                    )
                )
            session.commit()
        select_count = 0
        load_strategy_record_summaries(
            session_factory,
            group_labels_by_chat_id={10: "大镖客", 20: "峰哥", 30: "测试组"},
            filter_name="all",
            limit=50,
            now=NOW,
        )
        expanded_count = select_count
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert baseline_count == expanded_count == 11
