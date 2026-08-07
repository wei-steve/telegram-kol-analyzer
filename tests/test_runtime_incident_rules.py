from telegram_kol_research.runtime_incident_rules import evaluate_rule
import json
from pathlib import Path


def test_terminal_exposure_requires_complete_snapshot():
    incomplete = evaluate_rule(
        "terminal_lifecycle_exchange_exposure_v1",
        {"complete": False, "object_id": "42"},
    )
    abnormal = evaluate_rule(
        "terminal_lifecycle_exchange_exposure_v1",
        {
            "complete": True,
            "object_id": "42",
            "lifecycle_terminal": True,
            "exchange_position_present": True,
            "live_entry_order_present": False,
            "evidence_references": ["lifecycle:42", "exchange-position:7"],
        },
    )
    normal = evaluate_rule(
        "terminal_lifecycle_exchange_exposure_v1",
        {
            "complete": True,
            "object_id": "42",
            "lifecycle_terminal": True,
            "exchange_position_present": False,
            "live_entry_order_present": False,
            "evidence_references": ["lifecycle:42", "exchange-snapshot:9"],
        },
    )
    assert incomplete.outcome == "evidence_insufficient"
    assert abnormal.outcome == "abnormal"
    assert abnormal.severity == "critical"
    assert normal.outcome == "normal"


def test_rule_catalog_covers_normal_abnormal_and_transition_windows():
    cases = [
        ("active_position_missing_protection_v1", {"position_present": True, "primary_protection_verified": False}),
        ("cancel_outcome_stale_unknown_v1", {"cancel_unknown": True, "transition_window_expired": True}),
        ("tp1_break_even_nonterminal_v1", {"tp1_confirmed": True, "break_even_terminal": False, "transition_window_expired": True}),
        ("monitor_incident_ledger_silence_v1", {"monitor_abnormal": True, "incident_present": False}),
    ]
    for rule_id, facts in cases:
        payload = {"complete": True, "object_id": "1", "evidence_references": ["snapshot:1"], **facts}
        assert evaluate_rule(rule_id, payload).outcome == "abnormal"


def test_terminal_cleanup_event_3158_is_normal_during_reviewed_transition():
    result = evaluate_rule(
        "cancel_outcome_stale_unknown_v1",
        {
            "complete": True,
            "object_id": "3158",
            "cancel_unknown": True,
            "transition_window_expired": False,
            "evidence_references": ["cleanup-event:3158"],
        },
    )
    assert result.outcome == "normal"


def test_reviewed_rule_fixtures_replay_with_expected_outcomes():
    fixture_dir = Path(__file__).parent / "fixtures/runtime_incident_observations"
    for path in sorted(fixture_dir.glob("*.json")):
        case = json.loads(path.read_text())
        result = evaluate_rule(case["rule_id"], case["facts"])
        assert result.outcome == case["expected_outcome"], path.name
        assert result.severity == case["expected_severity"], path.name


def test_terminal_high_risk_management_without_instruction_rule_is_closed():
    base = {
        "complete": True,
        "object_id": "decision-1",
        "terminal_high_risk_management": True,
        "executable_instruction_present": False,
        "evidence_references": ["recognition-decision:1"],
    }

    assert evaluate_rule(
        "terminal_high_risk_management_without_instruction_v1", base
    ).outcome == "abnormal"
    assert evaluate_rule(
        "terminal_high_risk_management_without_instruction_v1",
        {**base, "executable_instruction_present": True},
    ).outcome == "normal"
    assert evaluate_rule(
        "terminal_high_risk_management_without_instruction_v1",
        {**base, "complete": False},
    ).outcome == "evidence_insufficient"


def test_verified_replacement_role_gap_rule_requires_primary_and_backup():
    base = {
        "complete": True,
        "object_id": "revision-1",
        "replacement_verified": True,
        "primary_role_verified": True,
        "backup_role_verified": True,
        "evidence_references": ["protection-revision:1"],
    }

    assert evaluate_rule(
        "verified_replacement_role_gap_v1", base
    ).outcome == "normal"
    for missing_role in ("primary_role_verified", "backup_role_verified"):
        assert evaluate_rule(
            "verified_replacement_role_gap_v1",
            {**base, missing_role: False},
        ).outcome == "abnormal"
    assert evaluate_rule(
        "verified_replacement_role_gap_v1",
        {**base, "complete": False},
    ).outcome == "evidence_insufficient"
