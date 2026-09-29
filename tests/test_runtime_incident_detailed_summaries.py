"""Every adapter's detailed summary is the one that gets recorded.

``_capture_with_minimal_fallback`` swallows a refused detailed summary and
records the minimal one, so a detail field nobody admitted to
``runtime_incidents._SUMMARY_FIELDS`` costs nothing but a log line. That shape
has now been paid for four times (A-8c ``group_trading_mode``, A-10b
``pos_id``, 2026-09-25 ``release_reason``, and 2026-09-28: every
``authoritative_recognition_failed`` row since 09-25 lost ``failure_point``
and ``protection_adopted_from_exchange`` lost the orders it adopted).

Two guards, because each misses what the other catches:

* runtime: each adapter is called with realistic arguments and the stored row
  must carry exactly the detailed summary -- this also catches the length and
  opaque-secret checks, which a field-name test cannot;
* static: every ``_capture_with_minimal_fallback`` call in the module is found
  by traversal, its detailed-summary keys must all be admitted, and it must
  appear in the runtime table -- so a new adapter cannot skip the runtime
  check by not being listed.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace
import logging

import pytest

from telegram_kol_research import runtime_incident_adapters as adapters
from telegram_kol_research.config import RuntimeIncidentConfig
from telegram_kol_research.db import create_session_factory
from telegram_kol_research.models import RuntimeIncident
from telegram_kol_research.runtime_incidents import (
    _SUMMARY_FIELDS,
    _validate_redacted_json_contract,
)


@pytest.fixture
def caplog(caplog, monkeypatch):
    """``caplog`` that still sees this package's records in a full run.

    ``app_logging.configure_application_logging`` sets the package logger's
    ``propagate = False`` for the whole process, so once an earlier test has
    called it these records never reach pytest's root handler -- the same way
    ``test_recognition_execution_finding_noise`` passed alone and failed in the
    suite. Restored after the test by ``monkeypatch``.
    """

    monkeypatch.setattr(
        logging.getLogger("telegram_kol_research"), "propagate", True
    )
    return caplog


NOW = datetime(2026, 9, 28, 3, 23, 38, tzinfo=UTC)
#: A fake group id in production's shape, and the strategy instance id that
#: embeds it: ``deepcoin:<group id>:<source message id>:<symbol>:<side>``.
FAKE_GROUP_ID = -1009999999999
STRATEGY_INSTANCE_ID = f"deepcoin:{FAKE_GROUP_ID}:10792:BTC:long"
#: The minus sign is what tells a group id from a Deepcoin position or order
#: id, which are also long digit runs beginning with 100.
_GROUP_ID_SHAPE = re.compile(r"-100\d{10,}")
_OUTAGE = SimpleNamespace(
    kind="payment_required",
    scan_exhausted=False,
    key="outage_20260928T0310",
    http_status=402,
    started_at=datetime(2026, 9, 28, 3, 10, tzinfo=UTC),
    last_failure_at=datetime(2026, 9, 28, 3, 20, tzinfo=UTC),
    recovered_at=datetime(2026, 9, 28, 3, 40, tzinfo=UTC),
    failures=12,
)

#: adapter name -> (incident type, realistic keyword arguments). Values are
#: shaped like production's: 陈哥's chat and raw ids, Deepcoin-length order ids.
_CASES: dict[str, tuple[str, dict]] = {
    "capture_authoritative_execution_uncertain": (
        "authoritative_execution_uncertain",
        dict(
            attempt_id=812,
            raw_message_id=19491,
            error_class="ReadTimeout",
            error_summary="exchange did not answer before the timeout",
        ),
    ),
    "capture_duplicate_entry_needs_confirmation": (
        "duplicate_entry_needs_confirmation",
        dict(raw_message_id=19491, message_instruction_item_id=1419, existing_binding_id=77),
    ),
    "capture_unresolved_management_item_claimed": (
        "unresolved_management_item_claimed",
        dict(message_instruction_item_id=1419, raw_message_id=19490),
    ),
    "capture_management_recognition_unresolved": (
        "management_recognition_unresolved",
        dict(
            raw_message_id=19490,
            chat_id=-1002337721508,
            decision="unresolved",
            conflict_types=["multiple_candidates"],
            candidate_thread_ids=[696, 701],
            instruction_text="止盈止损有调整我删了重新发。",
            resolution_reason="target_outside_candidate_set",
        ),
    ),
    "capture_position_marked_manually_closed": (
        "position_marked_manually_closed",
        dict(execution_binding_id=301, pos_id="1001125406857038", basis="exchange_flat"),
    ),
    "capture_management_protection_authority_refused": (
        "management_protection_authority_refused",
        dict(
            batch_id=158,
            leg_id=2,
            pos_id="1001125406857038",
            reason="protection_authority_frozen",
            rolled_back_order_ids=1,
        ),
    ),
    "capture_management_close_authority_refused": (
        "management_close_authority_refused",
        dict(batch_id=158, leg_id=2, pos_id="1001125406857038", reason="ownership_not_verified"),
    ),
    "capture_manual_close_guard_degenerate": (
        "manual_close_guard_degenerate",
        dict(streak=3, live_bindings=4),
    ),
    "capture_stop_resize_replace_incomplete": (
        "stop_resize_replace_incomplete",
        dict(
            pos_id="1001125406857038",
            old_order_id="1001125406857201",
            new_order_id="1001125406857388",
            reason_code="old_stop_cancel_failed",
        ),
    ),
    "capture_protection_adopted_from_exchange": (
        "protection_adopted_from_exchange",
        dict(
            pos_id="1001125406857038",
            instrument_id="BTC-USDT-SWAP",
            adopted=[{"order_id": "1001125406857201"}, {"order_id": "1001125406857388"}],
        ),
    ),
    "capture_management_cancel_precheck_observed": (
        "management_cancel_precheck_observed",
        dict(
            pos_id="1001125406857038",
            order_id="1001125406857201",
            path="protection_replace",
            verdict="replaced",
            first_ever=True,
        ),
    ),
    "capture_uncertain_without_write": (
        "uncertain_without_write",
        dict(
            attempt_id=812,
            raw_message_id=19491,
            error_class="ReadTimeout",
            error_summary="no exchange write was attempted",
        ),
    ),
    "capture_management_refused_before_write": (
        "management_refused_before_write",
        dict(
            attempt_id=4631,
            raw_message_id=19598,
            reason_code="protection_rows_unattributed_on_exchange",
            management_batch_id=184,
        ),
    ),
    "capture_deferred_instruction_expired": (
        "deferred_instruction_expired",
        dict(raw_message_id=19073, deferred_minutes=360),
    ),
    "capture_entry_admission_expired": (
        "entry_admission_expired",
        dict(
            message_instruction_item_id=1419,
            raw_message_id=19491,
            chat_id=-1002337721508,
            defer_reason_code="adjacent_entry_context_pending",
            deadline_at=datetime(2026, 9, 28, 9, 26, 20, tzinfo=UTC),
            blockers=[
                {
                    "raw_message_id": 19490,
                    "automation_status": "skipped",
                    "automation_reason": "mimo_authoritative_failed_exhausted",
                }
            ],
        ),
    ),
    "capture_management_recovery_timeout": (
        "management_recovery_timeout",
        dict(
            management_batch_id=158,
            # Production's shape, group id and all (a fake one). The old value
            # ``chen-btc-696`` could never trip the opaque-secret scan, which
            # is how incident 2419 lost its detail unnoticed (2026-09-28).
            strategy_instance_id=STRATEGY_INSTANCE_ID,
            target_lifecycle_id=1327,
            effective_action="move_stop",
            recovery_reason_code="protection_recovery_required",
            timeout_minutes=120,
        ),
    ),
    "capture_source_deletion_exit_stuck": (
        "source_deletion_exit_stuck",
        dict(
            deletion_exit_id=310,
            state="recovery_required",
            reason_code="recovery_required",
            timeout_minutes=120,
            lane_released=False,
            release_reason="exit_has_no_known_position",
        ),
    ),
    "capture_management_target_needs_confirmation": (
        "management_target_needs_confirmation",
        dict(
            raw_message_id=19490,
            chat_id=-1002337721508,
            candidate_count=2,
            reason_code="target_ambiguous",
            candidate_digest="thread 696 BTC long, thread 701 BTC long",
        ),
    ),
    "capture_authoritative_recognition_failed": (
        "authoritative_recognition_failed",
        dict(
            raw_message_id=19490,
            chat_id=-1002337721508,
            reason_code="mimo_authoritative_failed",
            failure_point=(
                "the authoritative model produced no decision, so nothing in "
                "this message was read or executed"
            ),
        ),
    ),
    "capture_mimo_provider_unavailable": (
        "mimo_provider_unavailable",
        dict(outage=_OUTAGE, bucket=1, fallback_model="gpt-5.6-luna", head_model="mimo-v2.5"),
    ),
    "capture_mimo_provider_recovered": (
        "mimo_provider_recovered",
        dict(outage=_OUTAGE, head_model="mimo-v2.5", fallback_model="gpt-5.6-luna"),
    ),
    "capture_mimo_provider_health_check_failed": (
        "mimo_provider_health_check_failed",
        dict(consecutive_failures=3, error_type="OperationalError"),
    ),
    "capture_mimo_provider_failure_streak": (
        "mimo_provider_failure_streak",
        dict(
            streak=SimpleNamespace(
                key="streak_402_20260928T0310",
                started_at=_OUTAGE.started_at,
                last_failure_at=_OUTAGE.last_failure_at,
                failures=6,
                first_attempt_id=5021,
            ),
            failure=SimpleNamespace(
                kind="payment_required", failure_class="http", http_status=402
            ),
        ),
    ),
    "capture_mimo_provider_probe_failed": (
        "mimo_provider_probe_failed",
        dict(
            outcome=SimpleNamespace(
                kind="timeout",
                failure_class="transport",
                http_status=None,
                error_type="ReadTimeout",
            ),
            source_record_id="probe_20260928",
        ),
    ),
    "capture_provider_outage_entry_not_replayed": (
        "provider_outage_entry_not_replayed",
        dict(
            raw_message_id=19491,
            chat_id=-1002337721508,
            message_text="BTC 83000-83300 做多，止损 81400",
            posted_at=datetime(2026, 9, 28, 3, 13, 40, tzinfo=UTC),
            entry_summary="BTC long 83000-83300",
        ),
    ),
    "capture_provider_outage_management_not_replayed": (
        "provider_outage_management_not_replayed",
        dict(
            raw_message_id=19490,
            chat_id=-1002337721508,
            message_text="止盈止损有调整我删了重新发。",
            posted_at=datetime(2026, 9, 28, 3, 13, 35, tzinfo=UTC),
            reason_code="management_not_replayed",
        ),
    ),
    "capture_provider_outage_replay_started": (
        "provider_outage_replay_started",
        dict(
            outage_key="outage_20260928T0310",
            started_at=_OUTAGE.started_at,
            recovered_at=_OUTAGE.recovered_at,
            message_count=4,
        ),
    ),
    "capture_background_task_restart_exhausted": (
        "background_task_restart_exhausted",
        dict(task_name="message_processing_worker_task", consecutive_failures=5, error_type="OperationalError"),
    ),
}


def _fallback_adapters_in_source() -> dict[str, list[ast.Call]]:
    """Every function that calls ``_capture_with_minimal_fallback``."""

    tree = ast.parse(inspect.getsource(adapters))
    found: dict[str, list[ast.Call]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = [
            call
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and getattr(call.func, "id", None) == "_capture_with_minimal_fallback"
        ]
        if calls:
            found[node.name] = calls
    return found


def test_every_fallback_adapter_is_in_the_runtime_table():
    in_source = set(_fallback_adapters_in_source())

    # Sentinel: a traversal over nothing passes everything.
    assert len(in_source) >= 25
    assert in_source == set(_CASES)


def _dict_keys_by_name(function: ast.FunctionDef) -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Dict):
            keys.setdefault(target.id, set()).update(
                key.value for key in node.value.keys if isinstance(key, ast.Constant)
            )
        elif (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and isinstance(target.slice, ast.Constant)
        ):
            keys.setdefault(target.value.id, set()).add(target.slice.value)
    return keys


def test_every_detailed_summary_key_is_admitted_to_the_vocabulary():
    tree = ast.parse(inspect.getsource(adapters))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    checked = 0
    for name, calls in _fallback_adapters_in_source().items():
        dict_keys = _dict_keys_by_name(functions[name])
        for call in calls:
            detailed = next(
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "detailed_summary"
            )
            assert (
                isinstance(detailed, ast.Call)
                and getattr(detailed.func, "id", None) == "_summary"
            ), f"{name}: detailed_summary is not a direct _summary(...) call"
            keys: set[str] = set()
            for keyword in detailed.keywords:
                if keyword.arg is not None:
                    keys.add(keyword.arg)
                    continue
                assert (
                    isinstance(keyword.value, ast.Name)
                    and keyword.value.id in dict_keys
                ), f"{name}: cannot resolve **{ast.dump(keyword.value)}"
                keys |= dict_keys[keyword.value.id]
            assert keys, name
            assert keys <= _SUMMARY_FIELDS, (name, sorted(keys - _SUMMARY_FIELDS))
            checked += 1
    assert checked >= 25


@pytest.mark.parametrize("adapter_name", sorted(_CASES))
def test_the_detailed_summary_is_the_one_recorded(tmp_path, monkeypatch, adapter_name):
    incident_type, kwargs = _CASES[adapter_name]
    session_factory = create_session_factory(tmp_path / f"{adapter_name}.db")
    seen: list[dict] = []
    original = adapters._capture_with_minimal_fallback

    def spy(session_factory_, **call_kwargs):
        seen.append(call_kwargs)
        return original(session_factory_, **call_kwargs)

    monkeypatch.setattr(adapters, "_capture_with_minimal_fallback", spy)

    recorded = getattr(adapters, adapter_name)(
        session_factory,
        config=RuntimeIncidentConfig(capture_types=frozenset({incident_type})),
        occurred_at=NOW,
        **kwargs,
    )

    assert recorded is not None
    assert len(seen) == 1
    detailed = seen[0]["detailed_summary"]
    _validate_redacted_json_contract("redacted_summary", detailed)
    with session_factory() as session:
        row = session.query(RuntimeIncident).one()
        assert row.incident_type == incident_type
        assert row.redacted_summary == detailed
    # No group id rides inside a string field. The integer ``chat_id`` field is
    # the one deliberate carrier (admitted by A-3d, never printed by the
    # generic formatter); a group id welded into a string -- as
    # ``strategy_instance_id`` was -- is what the opaque-secret scan refuses.
    stored = json.loads(detailed)
    # The fake group id's digits, whatever ``_safe_label`` did to its sign.
    assert str(FAKE_GROUP_ID).lstrip("-") not in detailed
    for key, value in stored.items():
        if key == "chat_id":
            assert isinstance(value, int), (adapter_name, key)
            continue
        assert not _GROUP_ID_SHAPE.search(str(value)), (adapter_name, key, value)


def test_r3b_recovery_timeout_keeps_its_detail_without_the_group_id(tmp_path):
    """Incident 2419: batch 184's timeout recorded only its reason code.

    ``strategy_instance_id`` embeds the group id, the opaque-secret scan
    refused the whole detailed summary, and the minimal fallback was stored.
    The detail now travels as the three parts of that id a person can use.
    """

    session_factory = create_session_factory(tmp_path / "r3b.db")

    recorded = adapters.capture_management_recovery_timeout(
        session_factory,
        config=RuntimeIncidentConfig(
            capture_types=frozenset({"management_recovery_timeout"})
        ),
        management_batch_id=184,
        strategy_instance_id=STRATEGY_INSTANCE_ID,
        target_lifecycle_id=1348,
        effective_action="break_even",
        recovery_reason_code="break_even_market_decision_missing_or_invalid",
        timeout_minutes=60,
        occurred_at=NOW,
    )

    assert recorded is not None
    summary = json.loads(recorded.redacted_summary)
    assert summary["lifecycle_id"] == 1348
    assert summary["symbol"] == "BTC"
    assert summary["side"] == "long"
    assert summary["origin_message_id"] == 10792
    assert summary["effective_action"] == "break_even"
    assert summary["timeout_minutes"] == 60
    assert "strategy_instance_id" not in summary
    assert "9999999999" not in recorded.redacted_summary


@pytest.mark.parametrize(
    "strategy_instance_id",
    [
        "",
        "chen-btc-696",
        "deepcoin:-1009999999999:BTC:long",
        "deepcoin:-1009999999999:not-a-number:BTC:long",
        "deepcoin:-1009999999999:10792:BTC:long:extra",
        "deepcoin:-1009999999999:10792::",
        None,
    ],
)
def test_an_unparseable_strategy_instance_id_is_left_out_not_raised(
    tmp_path, strategy_instance_id
):
    session_factory = create_session_factory(tmp_path / "unparseable.db")

    recorded = adapters.capture_management_recovery_timeout(
        session_factory,
        config=RuntimeIncidentConfig(
            capture_types=frozenset({"management_recovery_timeout"})
        ),
        management_batch_id=184,
        strategy_instance_id=strategy_instance_id,
        target_lifecycle_id=1348,
        effective_action="break_even",
        recovery_reason_code="break_even_market_decision_missing_or_invalid",
        timeout_minutes=60,
        occurred_at=NOW,
    )

    summary = json.loads(recorded.redacted_summary)
    # Still the detailed summary, only without the parts it could not read.
    assert summary["lifecycle_id"] == 1348
    assert "strategy_instance_id" not in summary
    assert "9999999999" not in recorded.redacted_summary
    assert not {"symbol", "side", "origin_message_id"} & set(summary)


def test_a_refused_detail_logs_refused_and_not_failed_open(tmp_path, caplog):
    """The log names what happened: detail refused, minimal recorded."""

    session_factory = create_session_factory(tmp_path / "wording.db")
    config = RuntimeIncidentConfig(
        capture_types=frozenset({"authoritative_recognition_failed"})
    )

    recorded = adapters._capture_with_minimal_fallback(
        session_factory,
        config=config,
        source_kind="message_recognition",
        source_record_id="19490",
        incident_type="authoritative_recognition_failed",
        severity="high",
        detailed_summary='{"component":"x","not_admitted_field":"y"}',
        minimal_summary='{"component":"x"}',
        occurred_at=NOW,
        recorder=None,
    )

    assert recorded is not None
    assert recorded.redacted_summary == '{"component":"x"}'
    assert "detailed summary refused" in caplog.text
    assert "failed open" not in caplog.text


def test_failed_open_is_logged_only_when_the_minimal_is_refused_too(
    tmp_path, caplog, allow_incident_capture_to_fail_open
):
    session_factory = create_session_factory(tmp_path / "both-refused.db")
    config = RuntimeIncidentConfig(
        capture_types=frozenset({"authoritative_recognition_failed"})
    )

    recorded = adapters._capture_with_minimal_fallback(
        session_factory,
        config=config,
        source_kind="message_recognition",
        source_record_id="19490",
        incident_type="authoritative_recognition_failed",
        severity="high",
        detailed_summary='{"component":"x","not_admitted_field":"y"}',
        minimal_summary='{"also_not_admitted":"y"}',
        occurred_at=NOW,
        recorder=None,
    )

    assert recorded is None
    assert "detailed summary refused" in caplog.text
    assert "capture failed open" in caplog.text


def test_under_the_strict_flag_a_refused_minimal_still_raises(tmp_path):
    from telegram_kol_research.runtime_incidents import RuntimeIncidentBoundsError

    session_factory = create_session_factory(tmp_path / "strict.db")
    config = RuntimeIncidentConfig(
        capture_types=frozenset({"authoritative_recognition_failed"})
    )

    with pytest.raises(RuntimeIncidentBoundsError):
        adapters._capture_with_minimal_fallback(
            session_factory,
            config=config,
            source_kind="message_recognition",
            source_record_id="19490",
            incident_type="authoritative_recognition_failed",
            severity="high",
            detailed_summary='{"component":"x","not_admitted_field":"y"}',
            minimal_summary='{"also_not_admitted":"y"}',
            occurred_at=NOW,
            recorder=None,
        )
