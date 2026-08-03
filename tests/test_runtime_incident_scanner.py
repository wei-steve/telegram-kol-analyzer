import pytest
from datetime import UTC, datetime, timedelta

from telegram_kol_research.db import create_session_factory
from telegram_kol_research.config import load_runtime_scanner_config
from telegram_kol_research.models import (
    BoundPositionCloseReservation,
    ExecutionBinding,
    ExecutionOrderLeg,
    PositionMutationIntent,
    PositionProtectionLedger,
    PositionProtectionLeg,
    RuntimeIncidentObservation,
)
from telegram_kol_research.runtime_incident_scanner import InvariantObservation, build_scanner_facts
from telegram_kol_research.runtime_incident_scanner import list_critical_unprotected_positions
from telegram_kol_research.runtime_incident_scanner import run_scanner_cycle
from sqlalchemy import event


def test_scanner_defaults_are_dormant_and_fail_closed():
    config = load_runtime_scanner_config(environ={}, env_file_paths=[])
    assert config.enabled is False
    assert config.shadow_only is True
    assert config.rules == frozenset()
    assert config.interval_seconds == 60.0


def test_scanner_rejects_unknown_rules_and_bounds_interval():
    config = load_runtime_scanner_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_SCANNER_ENABLED": "true",
            "TELEGRAM_KOL_RUNTIME_SCANNER_RULES": "unknown,*",
            "TELEGRAM_KOL_RUNTIME_SCANNER_INTERVAL_SECONDS": "1",
        },
        env_file_paths=[],
    )
    assert config.enabled is True
    assert config.rules == frozenset()
    assert config.interval_seconds == 10.0


def test_scanner_refuses_rules_without_a_deployed_snapshot_projection():
    config = load_runtime_scanner_config(
        environ={
            "TELEGRAM_KOL_RUNTIME_SCANNER_RULES": (
                "terminal_lifecycle_exchange_exposure_v1,"
                "cancel_outcome_stale_unknown_v1"
            )
        },
        env_file_paths=[],
    )
    assert config.rules == frozenset({"cancel_outcome_stale_unknown_v1"})


def test_observation_contract_rejects_sensitive_and_unbounded_values():
    with pytest.raises(ValueError, match="sensitive"):
        InvariantObservation(
            rule_id="terminal_lifecycle_exchange_exposure_v1",
            rule_version="1",
            object_kind="lifecycle",
            object_id="42",
            severity="critical",
            outcome="abnormal",
            evidence_references=("lifecycle:42",),
            evidence_fingerprint="e" * 64,
            summary={"api_key": "secret"},
        )


def test_critical_unprotected_position_projection_is_exact_and_close_aware(tmp_path):
    sf = create_session_factory(tmp_path / "unprotected.db")
    now = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    with sf() as session:
        binding = ExecutionBinding(
            strategy_instance_id="deepcoin:100:55:BTC:long", kol_id="group:100",
            chat_id=100, message_id=55, symbol="BTC", side="long",
            venue="deepcoin", status="active",
        )
        session.add(binding)
        session.flush()
        leg = ExecutionOrderLeg(
            execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
            leg_index=0, purpose="entry", order_kind="market", pos_id="pos-naked",
            attribution_status="verified", status="active", last_verified_at=now.replace(tzinfo=None),
            created_at=now.replace(tzinfo=None), updated_at=now.replace(tzinfo=None),
        )
        session.add(leg)
        session.flush()
        session.add(PositionProtectionLeg(
            venue="deepcoin", execution_binding_id=binding.id,
            execution_order_leg_id=leg.id, role="primary_stop", leg_index=1,
            planned_trigger_price="67500", pos_id="pos-naked", status="planned",
        ))
        session.commit()

    risks = list_critical_unprotected_positions(sf)
    assert risks == ({
        "chat_id": 100,
        "strategy_instance_id": "deepcoin:100:55:BTC:long",
        "execution_binding_id": 1,
        "execution_order_leg_id": 1,
        "pos_id": "pos-naked",
        "planned_stop": "67500",
        "exposure_started_at": now.replace(tzinfo=None).isoformat(),
        "rescue_state": "not_planned",
    },)
    facts = build_scanner_facts(
        sf, rules=frozenset({"active_position_missing_protection_v1"}), observed_at=now
    )
    assert facts["active_position_missing_protection_v1"] == ({
        "complete": True,
        "object_id": "pos-naked",
        "position_present": True,
        "primary_protection_verified": False,
        "chat_id": 100,
        "strategy_instance_id": "deepcoin:100:55:BTC:long",
        "execution_binding_id": 1,
        "execution_order_leg_id": 1,
        "planned_stop": "67500",
        "exposure_started_at": now.replace(tzinfo=None).isoformat(),
        "rescue_state": "not_planned",
        "evidence_references": ("binding:1", "entry-leg:1", "position:pos-naked"),
    },)
    with sf() as session:
        session.get(ExecutionOrderLeg, 1).last_verified_at = (
            now + timedelta(minutes=5)
        ).replace(tzinfo=None)
        session.commit()
    assert list_critical_unprotected_positions(sf)[0]["exposure_started_at"] == (
        now.replace(tzinfo=None).isoformat()
    )

    with sf() as session:
        session.add(BoundPositionCloseReservation(
            pos_id="pos-naked", execution_binding_id=1, status="submitted"
        ))
        session.commit()
    assert list_critical_unprotected_positions(sf) == ()


def test_critical_projection_bounds_after_protection_filtering(tmp_path):
    sf = create_session_factory(tmp_path / "bounded-unprotected.db")
    now = datetime(2026, 8, 3, 8, 0)
    with sf() as session:
        for index in range(21):
            binding = ExecutionBinding(
                strategy_instance_id=f"strategy-{index}", kol_id="group:100",
                chat_id=100, message_id=1000 + index, symbol="BTC", side="long",
                venue="deepcoin", status="active",
            )
            session.add(binding)
            session.flush()
            leg = ExecutionOrderLeg(
                execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
                leg_index=0, purpose="entry", order_kind="market", pos_id=f"pos-{index}",
                attribution_status="verified", status="active", created_at=now, updated_at=now,
            )
            session.add(leg)
            session.flush()
            if index < 20:
                session.add(PositionProtectionLedger(
                    venue="deepcoin", execution_binding_id=binding.id,
                    execution_order_leg_id=leg.id, strategy_instance_id=binding.strategy_instance_id,
                    pos_id=f"pos-{index}", instrument_id="BTC-USDT-SWAP", side="long",
                    order_id=f"stop-{index}", purpose="stop_loss", status="verified",
                    evidence_source="test", evidence_json="{}",
                ))
        session.commit()

    risks = list_critical_unprotected_positions(sf, chat_id=100, limit=20)

    assert [row["pos_id"] for row in risks] == ["pos-20"]


def test_uncertain_prior_observations_do_not_starve_new_critical_position(tmp_path):
    sf = create_session_factory(tmp_path / "unprotected-priority.db")
    now = datetime(2026, 8, 3, 8, 0)
    with sf() as session:
        for index in range(100):
            session.add(RuntimeIncidentObservation(
                rule_id="active_position_missing_protection_v1", rule_version="1",
                fingerprint=f"{index + 1:064x}", object_kind="position",
                object_id=f"old-uncertain-{index}", severity="critical", state="observing",
                consecutive_count=1, first_observed_at=now, last_observed_at=now,
                evidence_refs_json=f'["position:old-uncertain-{index}"]',
                evidence_fingerprint=f"{index + 1000:064x}", summary_json="{}",
            ))
        binding = ExecutionBinding(
            strategy_instance_id="new-critical", kol_id="group:100", chat_id=100,
            message_id=9001, symbol="BTC", side="long", venue="deepcoin", status="active",
        )
        session.add(binding)
        session.flush()
        session.add(ExecutionOrderLeg(
            execution_binding_id=binding.id, strategy_instance_id=binding.strategy_instance_id,
            leg_index=0, purpose="entry", order_kind="market", pos_id="new-critical-pos",
            attribution_status="verified", status="active", created_at=now, updated_at=now,
        ))
        session.commit()

    facts = build_scanner_facts(
        sf, rules=frozenset({"active_position_missing_protection_v1"}),
        observed_at=now.replace(tzinfo=UTC),
    )

    assert facts["active_position_missing_protection_v1"][0]["object_id"] == "new-critical-pos"
    assert len(facts["active_position_missing_protection_v1"]) <= 100


def test_cancel_snapshot_is_bounded_read_only_and_respects_transition_window(tmp_path):
    sf = create_session_factory(tmp_path / "scanner.db")
    now = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    with sf() as session:
        session.add(PositionMutationIntent(
            idempotency_key="cancel-1", venue="deepcoin",
            operation="cancel_trigger_order", strategy_instance_id="s1",
            execution_binding_id=1, execution_order_leg_id=1, pos_id="p1",
            order_id="o1", authority_fingerprint="a" * 64,
            request_fingerprint="r" * 64, status="reserved",
            request_json="{}", reserved_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
            created_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
            updated_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
        ))
        session.commit()
    facts = build_scanner_facts(
        sf, rules=frozenset({"cancel_outcome_stale_unknown_v1"}), observed_at=now
    )
    assert facts["cancel_outcome_stale_unknown_v1"][0]["cancel_unknown"] is True
    assert facts["cancel_outcome_stale_unknown_v1"][0]["transition_window_expired"] is True
    with sf() as session:
        assert session.get(PositionMutationIntent, 1).status == "reserved"


def test_blocked_cancel_is_a_known_terminal_refusal_not_unknown(tmp_path):
    sf = create_session_factory(tmp_path / "scanner.db")
    now = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    with sf() as session:
        session.add(PositionMutationIntent(
            idempotency_key="cancel-blocked", venue="deepcoin",
            operation="cancel_trigger_order", strategy_instance_id="s1",
            execution_binding_id=1, execution_order_leg_id=1, pos_id="p1",
            order_id="o1", authority_fingerprint="a" * 64,
            request_fingerprint="r" * 64, status="blocked",
            request_json="{}", reserved_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
            created_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
            updated_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
        ))
        session.commit()
    facts = build_scanner_facts(
        sf, rules=frozenset({"cancel_outcome_stale_unknown_v1"}), observed_at=now
    )
    assert facts["cancel_outcome_stale_unknown_v1"] == ()


def test_candidate_filter_precedes_bound_so_old_unknown_is_not_hidden(tmp_path):
    sf = create_session_factory(tmp_path / "scanner.db")
    now = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    with sf() as session:
        for index in range(102):
            session.add(PositionMutationIntent(
                idempotency_key=f"cancel-{index}", venue="deepcoin",
                operation="cancel_trigger_order", strategy_instance_id="s1",
                execution_binding_id=1, execution_order_leg_id=1, pos_id="p1",
                order_id=f"o{index}", authority_fingerprint=f"{index:064x}"[-64:],
                request_fingerprint=f"{index + 1000:064x}"[-64:],
                status="reserved" if index == 0 else "confirmed",
                request_json="{}", reserved_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
                created_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
                updated_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
            ))
        session.commit()
    facts = build_scanner_facts(
        sf, rules=frozenset({"cancel_outcome_stale_unknown_v1"}), observed_at=now
    )
    assert [item["object_id"] for item in facts["cancel_outcome_stale_unknown_v1"]] == ["1"]


def test_scanner_cycle_commits_all_observations_in_one_transaction(tmp_path):
    sf = create_session_factory(tmp_path / "scanner.db")
    now = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    config = load_runtime_scanner_config(
        environ={"TELEGRAM_KOL_RUNTIME_SCANNER_RULES": "cancel_outcome_stale_unknown_v1"},
        env_file_paths=[],
    )
    facts = {
        "cancel_outcome_stale_unknown_v1": tuple(
            {
                "complete": True, "object_id": str(index),
                "cancel_unknown": True, "transition_window_expired": True,
                "evidence_references": [f"mutation-intent:{index}"],
            }
            for index in (1, 2)
        )
    }
    commits = []
    event.listen(sf.class_, "after_commit", lambda session: commits.append(1))
    run_scanner_cycle(
        session_factory=sf, config=config, facts_by_rule=facts, observed_at=now
    )
    assert commits == [1]


def test_recovered_observation_is_prioritized_over_newer_candidate_overflow(tmp_path):
    sf = create_session_factory(tmp_path / "scanner.db")
    now = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    config = load_runtime_scanner_config(
        environ={"TELEGRAM_KOL_RUNTIME_SCANNER_RULES": "cancel_outcome_stale_unknown_v1"},
        env_file_paths=[],
    )
    with sf() as session:
        for index in range(101):
            session.add(PositionMutationIntent(
                idempotency_key=f"overflow-{index}", venue="deepcoin",
                operation="cancel_trigger_order", strategy_instance_id="s1",
                execution_binding_id=1, execution_order_leg_id=1, pos_id="p1",
                order_id=f"overflow-order-{index}", authority_fingerprint=f"{index + 1:064x}",
                request_fingerprint=f"{index + 2000:064x}", status="reserved",
                request_json="{}", reserved_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
                created_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
                updated_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
            ))
        session.commit()
    first_facts = {
        "cancel_outcome_stale_unknown_v1": ({
            "complete": True, "object_id": "1", "cancel_unknown": True,
            "transition_window_expired": True,
            "evidence_references": ["mutation-intent:1"],
        },)
    }
    run_scanner_cycle(
        session_factory=sf, config=config, facts_by_rule=first_facts, observed_at=now
    )
    with sf() as session:
        session.get(PositionMutationIntent, 1).status = "confirmed"
        session.commit()
    facts = build_scanner_facts(
        sf, rules=config.rules, observed_at=now + timedelta(minutes=1)
    )
    assert facts["cancel_outcome_stale_unknown_v1"][0]["object_id"] == "1"
    run_scanner_cycle(
        session_factory=sf, config=config, facts_by_rule=facts,
        observed_at=now + timedelta(minutes=1),
    )
    with sf() as session:
        row = session.query(RuntimeIncidentObservation).filter_by(object_id="1").one()
        assert row.state == "resolved_without_incident"


def test_terminal_recovery_is_filtered_before_observation_limit(tmp_path):
    sf = create_session_factory(tmp_path / "scanner.db")
    now = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)
    with sf() as session:
        for index in range(101):
            source = PositionMutationIntent(
                idempotency_key=f"confirmed-overflow-{index}", venue="deepcoin",
                operation="cancel_trigger_order", strategy_instance_id="s1",
                execution_binding_id=1, execution_order_leg_id=1, pos_id="p1",
                order_id=f"confirmed-order-{index}", authority_fingerprint=f"{index + 4000:064x}",
                request_fingerprint=f"{index + 5000:064x}",
                status="confirmed" if index == 100 else "reserved",
                request_json="{}", reserved_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
                created_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
                updated_at=(now - timedelta(minutes=20)).replace(tzinfo=None),
            )
            session.add(source)
            session.flush()
            observed = (now - timedelta(minutes=200 - index)).replace(tzinfo=None)
            session.add(RuntimeIncidentObservation(
                rule_id="cancel_outcome_stale_unknown_v1", rule_version="1",
                fingerprint=f"{index + 6000:064x}", object_kind="cancel-operation",
                object_id=str(source.id), severity="high", state="shadow_confirmed",
                consecutive_count=2, first_observed_at=observed,
                last_observed_at=observed, evidence_refs_json=f'["mutation-intent:{source.id}"]',
                evidence_fingerprint=f"{index + 7000:064x}", summary_json="{}",
                created_at=observed, updated_at=observed,
            ))
        session.commit()
    facts = build_scanner_facts(
        sf, rules=frozenset({"cancel_outcome_stale_unknown_v1"}), observed_at=now
    )
    assert [item["object_id"] for item in facts["cancel_outcome_stale_unknown_v1"]] == ["101"]
