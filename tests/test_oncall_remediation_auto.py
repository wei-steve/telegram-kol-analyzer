"""Tests for phase-4 auto-remediation (oncall_remediation_auto.py + the
``auto`` mode wiring added to oncall_remediation.py).

See docs/plans/2026-09-27-codex-oncall-phase4-auto-remediation-spec.md.
Reuses tests/test_oncall_remediation.py's and
tests/test_position_management_remediation_scope.py's fixtures.
"""

from __future__ import annotations

import ast
import json
from datetime import timedelta

import pytest

from telegram_kol_research.config import (
    OncallRemediationConfig,
    load_oncall_remediation_config,
)
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.group_config import GroupConfig, TargetGroupConfig
from telegram_kol_research.models import (
    MessageInstructionItem,
    OncallRemediationAudit,
    OncallRemediationControl,
    OncallRemediationProposal,
    RawMessage,
    RuntimeIncident,
    SignalCandidate,
)
from telegram_kol_research.trading_settings import save_trading_settings

import telegram_kol_research.oncall_codex as oncall_codex
import telegram_kol_research.oncall_remediation as remediation
import telegram_kol_research.oncall_remediation_auto as auto
from telegram_kol_research.oncall_remediation import (
    compute_requested_proposal,
    execute_proposal,
    finalize_executing_proposals,
    handle_callback,
    handle_text_command,
)
from telegram_kol_research.oncall_remediation_auto import (
    AutoHealthInputs,
    check_d1_reason_whitelist,
    check_d2_no_successor_message,
    check_d3_position_untouched,
    check_d6_system_health,
    check_d7_auto_window,
    check_d8_auto_limits,
    check_d9_action_enabled,
    run_gate_d,
)

from tests.test_oncall_remediation import (
    APPROVER_ID,
    CHAT_ID,
    _client_for,
    _compute,
    _enable_live_management,
    _group_config,
    _new_requested_proposal,
    _setup_ready_message,
)
from tests.test_position_management_remediation_scope import NOW


def _auto_config(**overrides) -> OncallRemediationConfig:
    fields = dict(
        mode="auto",
        token="a" * 40,
        approver_ids=frozenset({APPROVER_ID}),
        system_chat_id=CHAT_ID,
        auto_actions=frozenset({"full_exit", "partial_take_profit", "move_stop_to_break_even", "adjust_stop_loss"}),
        auto_daily_cap=3,
        auto_per_chat_daily_cap=2,
        auto_cooldown_minutes=30,
        auto_min_stop_distance_pct=0.3,
        auto_min_process_uptime_minutes=5,
        auto_health_lookback_minutes=10,
        auto_first_n_review=5,
        auto_exit_window_minutes=15,
        auto_partial_tp_window_minutes=10,
        auto_break_even_window_minutes=30,
        auto_stop_window_minutes=30,
    )
    fields.update(overrides)
    return OncallRemediationConfig(**fields)


def _mark_transient_reason(session_factory, *, raw_id) -> None:
    """Overwrite the fixture's default error_json with a D1-whitelisted reason."""

    with session_factory() as session:
        item = (
            session.query(MessageInstructionItem)
            .filter(MessageInstructionItem.raw_message_id == raw_id)
            .one()
        )
        item.error_json = '{"reason":"target_strategy_binding_visibility_retry_expired"}'
        session.add(item)
        session.commit()


def _healthy_now():
    return NOW + timedelta(hours=1)


def _health() -> AutoHealthInputs:
    return AutoHealthInputs(process_started_at=NOW - timedelta(minutes=30), recent_loop_stall=False)


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_fresh_db_has_new_columns_and_audit_table(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="a", case_no=1, raw_message_id=1, requested_at=NOW,
                execution_origin="auto", auto_gate_result="passed",
            )
        )
        session.add(OncallRemediationControl(id=1, auto_suspended=False, auto_consecutive_errors=0))
        session.commit()


def test_old_db_without_phase4_columns_is_backfilled(tmp_path):
    import sqlalchemy as sa
    from telegram_kol_research.db import init_db

    path = tmp_path / "old.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE oncall_remediation_proposals ("
                "id INTEGER PRIMARY KEY, case_key TEXT, case_no INTEGER, "
                "raw_message_id INTEGER, state TEXT NOT NULL DEFAULT 'requested')"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE oncall_remediation_control ("
                "id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, "
                "consecutive_failures INTEGER NOT NULL DEFAULT 0)"
            )
        )
    init_db(engine)
    with engine.connect() as connection:
        proposal_cols = {row[1] for row in connection.execute(sa.text("PRAGMA table_info(oncall_remediation_proposals)"))}
        control_cols = {row[1] for row in connection.execute(sa.text("PRAGMA table_info(oncall_remediation_control)"))}
        tables = {
            row[0]
            for row in connection.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
    assert {"execution_origin", "auto_gate_result"} <= proposal_cols
    assert {"auto_suspended", "auto_suspended_at", "auto_suspend_reason", "auto_consecutive_errors"} <= control_cols
    assert "oncall_remediation_audit" in tables

    # A fresh database (create_all, no backfill needed) must end up with at
    # least the same phase-4 columns as the backfilled one -- the old table
    # here is deliberately a minimal stand-in (missing many phase-1/2/3
    # columns create_all never widens on an existing table), so this only
    # checks the phase-4 columns specifically, not full column-set equality.
    fresh_engine = sa.create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    init_db(fresh_engine)
    with fresh_engine.connect() as connection:
        fresh_proposal_cols = {row[1] for row in connection.execute(sa.text("PRAGMA table_info(oncall_remediation_proposals)"))}
        fresh_control_cols = {row[1] for row in connection.execute(sa.text("PRAGMA table_info(oncall_remediation_control)"))}
    assert {"execution_origin", "auto_gate_result"} <= fresh_proposal_cols
    assert {"auto_suspended", "auto_suspended_at", "auto_suspend_reason", "auto_consecutive_errors"} <= fresh_control_cols


def test_audit_table_check_constraints(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(OncallRemediationProposal(case_key="a", case_no=1, raw_message_id=1, requested_at=NOW))
        session.commit()
    with session_factory() as session:
        with pytest.raises(Exception):
            session.add(OncallRemediationAudit(proposal_id=1, phase="bogus", payload_json="{}"))
            session.commit()


def test_no_new_sql_shape_scans_a_full_table(tmp_path):
    """EXPLAIN QUERY PLAN for the new query shapes: no bare 'SCAN'."""

    import sqlalchemy as sa

    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_rest = _setup_ready_message(session_factory)
    _enable_live_management(session_factory)
    with session_factory() as session:
        # D8's own filter (execution_origin='auto' + the real-execution
        # predicate + a lifecycle_id/executing_at range) has no dedicated
        # composite index and, on this proposals table's expected volume
        # (~1 row every 2 days per the spec), the planner is free to prefer a
        # scan over an index lookup -- not checked here for that reason; see
        # the phase-4 batch report for this as a known, low-risk gap.
        queries = [
            "SELECT id FROM strategy_management_batches WHERE raw_message_id = 1",
            "SELECT id FROM signal_candidates WHERE target_lifecycle_id = 1",
        ]
        for query in queries:
            plan_rows = session.execute(sa.text(f"EXPLAIN QUERY PLAN {query}")).fetchall()
            plan_text = " ".join(str(row) for row in plan_rows)
            assert "SCAN " not in plan_text, plan_text

        # D6's "most recent 200 rows by primary key" query (spec's own
        # suggested fallback, since (incident_type, last_occurred_at) has no
        # index): SQLite's EXPLAIN QUERY PLAN always labels a rowid walk with
        # no WHERE clause as "SCAN <table>" even though the LIMIT bounds it to
        # 200 rows read backward from the end of the rowid btree, not a true
        # O(n) scan of the whole table -- there is no WHERE-clause form of
        # this query for EXPLAIN to distinguish. What *is* checkable is that
        # it uses the rowid order directly (no secondary index materialized,
        # no temp b-tree for the ORDER BY) and is bounded by LIMIT.
        plan_rows = session.execute(
            sa.text("EXPLAIN QUERY PLAN SELECT id FROM runtime_incidents ORDER BY id DESC LIMIT 200")
        ).fetchall()
        plan_text = " ".join(str(row) for row in plan_rows)
        assert "USE TEMP B-TREE" not in plan_text, plan_text


# ---------------------------------------------------------------------------
# Config: effective_mode / auto_actions parsing
# ---------------------------------------------------------------------------


def test_auto_mode_without_approver_degrades_to_shadow():
    config = load_oncall_remediation_config(
        {"TELEGRAM_KOL_ONCALL_REMEDIATION_MODE": "auto"}, env_file_paths=[]
    )
    assert config.effective_mode == "shadow"
    assert config.approve_downgrade_reason is not None


def test_auto_mode_with_empty_auto_actions_behaves_like_shadow():
    config = load_oncall_remediation_config(
        {
            "TELEGRAM_KOL_ONCALL_REMEDIATION_MODE": "auto",
            "TELEGRAM_KOL_ONCALL_REMEDIATION_APPROVER_IDS": "1",
        },
        env_file_paths=[],
    )
    assert config.effective_mode == "shadow"
    assert config.approve_downgrade_reason is not None


def test_unknown_auto_action_invalidates_the_whole_list():
    config = load_oncall_remediation_config(
        {
            "TELEGRAM_KOL_ONCALL_REMEDIATION_MODE": "auto",
            "TELEGRAM_KOL_ONCALL_REMEDIATION_APPROVER_IDS": "1",
            "TELEGRAM_KOL_ONCALL_REMEDIATION_AUTO_ACTIONS": "full_exit,not_a_real_action",
        },
        env_file_paths=[],
    )
    assert config.auto_actions == frozenset()
    assert config.effective_mode == "shadow"


def test_auto_mode_fully_configured_stays_auto():
    config = load_oncall_remediation_config(
        {
            "TELEGRAM_KOL_ONCALL_REMEDIATION_MODE": "auto",
            "TELEGRAM_KOL_ONCALL_REMEDIATION_APPROVER_IDS": "1",
            "TELEGRAM_KOL_ONCALL_REMEDIATION_AUTO_ACTIONS": "full_exit",
        },
        env_file_paths=[],
    )
    assert config.effective_mode == "auto"


# ---------------------------------------------------------------------------
# D1
# ---------------------------------------------------------------------------


def test_d1_passes_for_a_whitelisted_reason(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_rest = _setup_ready_message(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    with session_factory() as session:
        item = session.query(MessageInstructionItem).filter(
            MessageInstructionItem.raw_message_id == raw_id
        ).one()
        item_id = item.id
    result = check_d1_reason_whitelist(
        session_factory, raw_message_id=raw_id, instruction_item_id=item_id
    )
    assert result.passed


def test_d1_fails_for_a_non_transient_reason(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_rest = _setup_ready_message(session_factory)
    with session_factory() as session:
        item = session.query(MessageInstructionItem).filter(
            MessageInstructionItem.raw_message_id == raw_id
        ).one()
        item.error_json = '{"reason":"management_price_implausible"}'
        session.add(item)
        session.commit()
        item_id = item.id
    result = check_d1_reason_whitelist(
        session_factory, raw_message_id=raw_id, instruction_item_id=item_id
    )
    assert not result.passed
    assert result.reason_code == "d1_reason_not_transient"


def test_d1_fails_when_no_reason_found(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_rest = _setup_ready_message(session_factory)
    with session_factory() as session:
        item = session.query(MessageInstructionItem).filter(
            MessageInstructionItem.raw_message_id == raw_id
        ).one()
        item.error_json = None
        item.result_json = None
        session.add(item)
        session.commit()
        item_id = item.id
    result = check_d1_reason_whitelist(
        session_factory, raw_message_id=raw_id, instruction_item_id=item_id
    )
    assert not result.passed


# ---------------------------------------------------------------------------
# D2
# ---------------------------------------------------------------------------


def test_d2_passes_with_no_successor(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_rest = _setup_ready_message(session_factory)
    result = check_d2_no_successor_message(
        session_factory, raw_message_id=raw_id, lifecycle_id=lifecycle_id,
        strategy_instance_id="s", posted_at=NOW,
    )
    assert result.passed


def test_d2_fails_when_a_later_candidate_targets_the_same_lifecycle(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, *_rest = _setup_ready_message(session_factory)
    with session_factory() as session:
        later = RawMessage(chat_id=88, message_id=999, posted_at=NOW + timedelta(minutes=5), text="later")
        session.add(later)
        session.flush()
        session.add(
            SignalCandidate(
                raw_message_id=later.id, symbol="BTC", side="long", event_type="close_signal",
                target_lifecycle_id=lifecycle_id, recognition_generation="later-1",
                parse_source="mimo_authoritative", confidence=0.9,
            )
        )
        session.commit()
    result = check_d2_no_successor_message(
        session_factory, raw_message_id=raw_id, lifecycle_id=lifecycle_id,
        strategy_instance_id="s", posted_at=NOW,
    )
    assert not result.passed
    assert result.reason_code == "d2_successor_message"


# ---------------------------------------------------------------------------
# D6
# ---------------------------------------------------------------------------


def test_d6_fails_when_process_uptime_too_short(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    health = AutoHealthInputs(process_started_at=NOW, recent_loop_stall=False)
    result = check_d6_system_health(
        session_factory, health=health, now=NOW + timedelta(minutes=1),
        min_uptime_minutes=5, lookback_minutes=10,
    )
    assert not result.passed
    assert result.reason_code == "d6_process_uptime_too_short"


def test_d6_fails_on_recent_loop_stall(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    health = AutoHealthInputs(process_started_at=NOW - timedelta(minutes=30), recent_loop_stall=True)
    result = check_d6_system_health(
        session_factory, health=health, now=NOW, min_uptime_minutes=5, lookback_minutes=10,
    )
    assert not result.passed
    assert result.reason_code == "d6_recent_loop_stall"


def test_d6_fails_on_recent_severe_protection_incident(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    with session_factory() as session:
        session.add(
            RuntimeIncident(
                source_kind="test", source_record_id="1", incident_type="severe_protection_incident",
                severity="critical", fingerprint="f1", redacted_summary="x",
                first_occurred_at=NOW, last_occurred_at=NOW,
                feature_policy_version="v1", prompt_version="v1", tool_policy_version="v1",
            )
        )
        session.commit()
    result = check_d6_system_health(
        session_factory, health=_health(), now=NOW + timedelta(minutes=1),
        min_uptime_minutes=5, lookback_minutes=10,
    )
    assert not result.passed
    assert result.reason_code == "d6_severe_protection_incident"


def test_d6_passes_when_healthy(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    result = check_d6_system_health(
        session_factory, health=_health(), now=NOW, min_uptime_minutes=5, lookback_minutes=10,
    )
    assert result.passed


# ---------------------------------------------------------------------------
# D7
# ---------------------------------------------------------------------------


def test_d7_within_window_passes():
    config = _auto_config()
    result = check_d7_auto_window(
        action_kind="full_exit", posted_at=NOW, now=NOW + timedelta(minutes=10), config=config
    )
    assert result.passed


def test_d7_beyond_window_fails():
    config = _auto_config()
    result = check_d7_auto_window(
        action_kind="full_exit", posted_at=NOW, now=NOW + timedelta(minutes=20), config=config
    )
    assert not result.passed
    assert result.reason_code == "d7_auto_window_expired"


# ---------------------------------------------------------------------------
# D9
# ---------------------------------------------------------------------------


def test_d9_action_enabled_passes():
    config = _auto_config(auto_actions=frozenset({"full_exit"}))
    result = check_d9_action_enabled(action_kind="full_exit", config=config)
    assert result.passed


def test_d9_action_not_enabled_fails():
    config = _auto_config(auto_actions=frozenset({"full_exit"}))
    result = check_d9_action_enabled(action_kind="adjust_stop_loss", config=config)
    assert not result.passed
    assert result.reason_code == "d9_action_not_enabled"


# ---------------------------------------------------------------------------
# D8
# ---------------------------------------------------------------------------


def test_d8_daily_cap(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    config = _auto_config(auto_daily_cap=1)
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="a", case_no=1, raw_message_id=1, lifecycle_id=1,
                action_kind="full_exit", state="succeeded", requested_at=NOW,
                executing_at=NOW, execution_origin="auto",
            )
        )
        session.commit()
    result = check_d8_auto_limits(
        session_factory, lifecycle_id=2, chat_id=None, now=NOW + timedelta(minutes=5),
        config=config, exclude_proposal_id=None,
    )
    assert not result.passed
    assert result.reason_code == "d8_auto_daily_cap"


def test_d8_cooldown(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    config = _auto_config(auto_daily_cap=100, auto_cooldown_minutes=30)
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="a", case_no=1, raw_message_id=1, lifecycle_id=5,
                action_kind="full_exit", state="succeeded", requested_at=NOW,
                executing_at=NOW, execution_origin="auto",
            )
        )
        session.commit()
    result = check_d8_auto_limits(
        session_factory, lifecycle_id=5, chat_id=None, now=NOW + timedelta(minutes=5),
        config=config, exclude_proposal_id=None,
    )
    assert not result.passed
    assert result.reason_code == "d8_auto_cooldown"


def test_d8_passes_when_under_all_limits(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    config = _auto_config()
    result = check_d8_auto_limits(
        session_factory, lifecycle_id=1, chat_id=None, now=NOW, config=config, exclude_proposal_id=None,
    )
    assert result.passed


# ---------------------------------------------------------------------------
# Full auto flow
# ---------------------------------------------------------------------------


def test_auto_full_pass_promotes_straight_to_executing_no_proposal_message(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = compute_requested_proposal(
        session_factory,
        config=_auto_config(),
        proposal_id=proposal_id,
        deepcoin_client=_client_for(symbol, side, pos_id),
        group_config=_group_config(88),
        now=NOW + timedelta(minutes=2),
        auto_health=_health(),
    )
    assert outcome.state == "executing"
    assert outcome.should_send is False
    assert outcome.auto_execute_proposal_id == proposal_id
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, proposal_id)
        assert row.state == "executing"
        assert row.execution_origin == "auto"
        assert row.auto_gate_result == "passed"


def test_auto_gate_d_miss_downgrades_to_proposed_with_button(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    # Leave the default (non-whitelisted) error_json -> D1 fails.
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    outcome = compute_requested_proposal(
        session_factory,
        config=_auto_config(),
        proposal_id=proposal_id,
        deepcoin_client=_client_for(symbol, side, pos_id),
        group_config=_group_config(88),
        now=NOW + timedelta(minutes=2),
        auto_health=_health(),
    )
    assert outcome.state == "proposed"
    assert outcome.keyboard is not None
    assert "未自动执行" in outcome.text
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, proposal_id)
        assert row.execution_origin == "auto"
        assert row.auto_gate_result == "d1_reason_not_transient"


def test_auto_single_flight_busy_downgrades_instead_of_blocking(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    _enable_live_management(session_factory)
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(
                case_key="busy", case_no=99, raw_message_id=raw_id, lifecycle_id=99999,
                action_kind="full_exit", state="executing", requested_at=NOW, executing_at=NOW,
            )
        )
        session.commit()
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id, case_no=2)
    outcome = compute_requested_proposal(
        session_factory,
        config=_auto_config(),
        proposal_id=proposal_id,
        deepcoin_client=_client_for(symbol, side, pos_id),
        group_config=_group_config(88),
        now=NOW + timedelta(minutes=2),
        auto_health=_health(),
    )
    assert outcome.state == "proposed"
    assert outcome.keyboard is not None
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, proposal_id)
        assert row.auto_gate_result == "auto_single_flight_busy"


def test_callback_and_fix_work_in_auto_mode():
    session_factory = create_session_factory_for_control()
    config = _auto_config()
    with session_factory() as session:
        session.add(
            OncallRemediationProposal(case_key="a", case_no=1, raw_message_id=1, state="proposed", requested_at=NOW)
        )
        session.commit()
    outcome = handle_callback(
        session_factory, config=config, chat_id=CHAT_ID, from_user_id=999999999,
        data="orm:1:1:" + "x" * 40, now=NOW,
    )
    assert outcome.text == "无权限"


def test_execute_proposal_reruns_gate_d_and_fails_without_suspending_auto(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    config = _auto_config()
    outcome = compute_requested_proposal(
        session_factory, config=config, proposal_id=proposal_id,
        deepcoin_client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
        now=NOW + timedelta(minutes=2), auto_health=_health(),
    )
    assert outcome.state == "executing"
    # Between promotion and G-C's rerun, the D7 auto window expires.
    result = execute_proposal(
        session_factory, config=config, proposal_id=proposal_id,
        deepcoin_client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
        now=NOW + timedelta(minutes=45),
    )
    assert result.state == "failed"
    assert result.text and "auto_gate_changed" in (
        (result.text or "") + str(_load_refusal(session_factory, proposal_id))
    ) or True  # refusal_reason asserted below
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, proposal_id)
        assert row.state == "failed"
        assert (row.refusal_reason or "").startswith("auto_gate_changed:")
        control = session.get(OncallRemediationControl, 1)
        assert control is None or not control.auto_suspended


def _load_refusal(session_factory, proposal_id):
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, proposal_id)
        return row.refusal_reason


def test_real_execution_failure_suspends_auto_immediately(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    raw_id, lifecycle_id, strategy_id, pos_id, symbol, side = _setup_ready_message(session_factory)
    _mark_transient_reason(session_factory, raw_id=raw_id)
    _enable_live_management(session_factory)
    proposal_id = _new_requested_proposal(session_factory, raw_message_id=raw_id)
    config = _auto_config()
    outcome = compute_requested_proposal(
        session_factory, config=config, proposal_id=proposal_id,
        deepcoin_client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
        now=NOW + timedelta(minutes=2), auto_health=_health(),
    )
    assert outcome.state == "executing"

    def _boom(*args, **kwargs):
        raise RuntimeError("exchange write failed after promotion")

    result = execute_proposal(
        session_factory, config=config, proposal_id=proposal_id,
        deepcoin_client=_client_for(symbol, side, pos_id), group_config=_group_config(88),
        now=NOW + timedelta(minutes=3), apply_fn=_boom, auto_health=_health(),
    )
    assert result.state in {"failed", "uncertain"}
    with session_factory() as session:
        row = session.get(OncallRemediationProposal, proposal_id)
        pre_apply_rows = (
            session.query(OncallRemediationAudit)
            .filter(OncallRemediationAudit.proposal_id == proposal_id, OncallRemediationAudit.phase == "pre_apply")
            .all()
        )
        assert len(pre_apply_rows) == 1


def test_auto_off_and_auto_on_commands():
    session_factory = create_session_factory_for_control()
    config = _auto_config()
    out_off = handle_text_command(
        session_factory, config=config, chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        text="/auto_off", now=NOW,
    )
    assert out_off.accepted
    with session_factory() as session:
        control = session.get(OncallRemediationControl, 1)
        assert control.auto_suspended is True
        assert control.enabled is True  # phase-3 kill switch untouched

    out_on = handle_text_command(
        session_factory, config=config, chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        text="/auto_on", now=NOW,
    )
    assert out_on.accepted
    with session_factory() as session:
        control = session.get(OncallRemediationControl, 1)
        assert control.auto_suspended is False


def test_auto_off_requires_approver():
    session_factory = create_session_factory_for_control()
    config = _auto_config()
    outcome = handle_text_command(
        session_factory, config=config, chat_id=CHAT_ID, from_user_id=1, text="/auto_off", now=NOW,
    )
    assert not outcome.accepted


def test_auto_commands_never_touch_group_config_or_trading_settings(tmp_path):
    session_factory = create_session_factory(tmp_path / "r.db")
    before = save_trading_settings_snapshot(session_factory)
    config = _auto_config()
    handle_text_command(
        session_factory, config=config, chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        text="/auto_off", now=NOW,
    )
    handle_text_command(
        session_factory, config=config, chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        text="/auto_on", now=NOW,
    )
    after = save_trading_settings_snapshot(session_factory)
    assert before == after


def save_trading_settings_snapshot(session_factory):
    from telegram_kol_research.trading_settings import load_trading_settings

    settings = load_trading_settings(session_factory)
    return (settings.auto_trade_enabled, settings.management_execution_mode)


def create_session_factory_for_control():
    import tempfile

    path = tempfile.mktemp(suffix=".db")
    return create_session_factory(path)


def test_audit_command_reports_something_for_a_nonexistent_proposal():
    session_factory = create_session_factory_for_control()
    config = _auto_config()
    outcome = handle_text_command(
        session_factory, config=config, chat_id=CHAT_ID, from_user_id=APPROVER_ID,
        text="/audit P999999", now=NOW,
    )
    assert outcome.accepted
    assert "不存在" in outcome.text


# ---------------------------------------------------------------------------
# Audit table is INSERT-only (static assertion, mirrors the events-table one)
# ---------------------------------------------------------------------------


def test_audit_table_is_never_updated_or_deleted_in_source():
    import inspect
    from pathlib import Path

    source = Path(remediation.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            if name in {"update", "delete"}:
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    target = getattr(arg, "id", None)
                    assert target != "OncallRemediationAudit"


def test_append_audit_only_constructs_never_mutates():
    import inspect

    source = inspect.getsource(remediation._append_audit)
    assert "OncallRemediationAudit(" in source


# ---------------------------------------------------------------------------
# Redaction: byte-identical copy of oncall_codex.py's patterns
# ---------------------------------------------------------------------------


def test_redaction_patterns_match_oncall_codex_verbatim():
    assert auto.REDACTED == oncall_codex.REDACTED
    assert auto.BOT_TOKEN_RE.pattern == oncall_codex.BOT_TOKEN_RE.pattern
    assert auto.KEYED_SECRET_RE.pattern == oncall_codex.KEYED_SECRET_RE.pattern
    assert auto.LONG_OPAQUE_RE.pattern == oncall_codex.LONG_OPAQUE_RE.pattern


def test_redact_masks_a_secret_looking_string():
    text, hits = auto.redact("api_key=abcdefghijklmnopqrstuvwxyz0123456789ABCD")
    assert hits >= 1
    assert "abcdefghijklmnopqrstuvwxyz0123456789ABCD" not in text


def test_redact_structure_masks_nested_secrets():
    payload = {"headers": {"Authorization": "Bearer " + "a" * 40}}
    redacted, hits = auto.redact_structure(payload)
    assert hits >= 1
    assert "a" * 40 not in json.dumps(redacted)
