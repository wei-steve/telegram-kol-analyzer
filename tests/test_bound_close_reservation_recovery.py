from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
import signal
import sqlite3
import threading
import time

import httpx
import pytest

from telegram_kol_research.deepcoin_client import (
    DeepcoinCredentials,
    DeepcoinRestClient,
    DeepcoinRequestScope,
    build_deepcoin_bound_close_reservation_recovery_client_from_env,
)
from telegram_kol_research.deepcoin_request_policy import RequestPriority
from telegram_kol_research.bound_close_reservation_recovery import (
    ACTIVE_REASONS,
    PROVEN_TERMINAL_REASONS,
    UNKNOWN_REASONS,
    BoundCloseReservationObservation,
    BoundCloseReservationRecoveryPlan,
    BoundCloseReservationSource,
    BoundCloseReservationExchangeReader,
    BoundCloseReservationExchangeConfigurationError,
    BoundCloseReservationExchangeDeadlineExceeded,
    LocalReservationEvidence,
    ReservationClassification,
    _canonical_json,
    _sha256_json,
    build_bound_close_reservation_exchange_reader_from_env,
    load_bound_close_reservation_source,
)


FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64
FINGERPRINT_C = "c" * 64
FINGERPRINT_D = "d" * 64


def _observation(
    *,
    classification: ReservationClassification = (
        ReservationClassification.PROVEN_TERMINAL
    ),
    reason_code: str = "exact_close_and_position_terminal",
    reservation_ref: str = FINGERPRINT_A,
) -> BoundCloseReservationObservation:
    return BoundCloseReservationObservation(
        reservation_ref=reservation_ref,
        classification=classification,
        reason_code=reason_code,
        source_fingerprint=FINGERPRINT_B,
        exchange_fingerprint=FINGERPRINT_C,
    )


def _plan(
    *,
    observations: tuple[BoundCloseReservationObservation, ...] | None = None,
    status: str = "ready",
    schema_version: int = 1,
    action_count: int | None = None,
) -> BoundCloseReservationRecoveryPlan:
    selected = observations if observations is not None else (_observation(),)
    return BoundCloseReservationRecoveryPlan(
        schema_version=schema_version,
        status=status,
        observations=selected,
        source_fingerprint=FINGERPRINT_A,
        exchange_snapshot_fingerprint=FINGERPRINT_B,
        evidence_fingerprint=FINGERPRINT_C,
        confirmation_token=FINGERPRINT_D,
        action_count=len(selected) if action_count is None else action_count,
    )


def test_contract_exposes_only_the_three_closed_classifications():
    assert [(item.name, item.value) for item in ReservationClassification] == [
        ("PROVEN_TERMINAL", "PROVEN_TERMINAL"),
        ("ACTIVE", "ACTIVE"),
        ("UNKNOWN", "UNKNOWN"),
    ]


def test_contract_reason_code_sets_are_exact_and_closed():
    assert PROVEN_TERMINAL_REASONS == frozenset(
        {"exact_close_and_position_terminal"}
    )
    assert ACTIVE_REASONS == frozenset(
        {
            "exact_position_currently_live",
            "exact_close_order_currently_pending",
            "exact_close_order_nonterminal",
        }
    )
    assert UNKNOWN_REASONS == frozenset(
        {
            "local_evidence_incomplete",
            "local_identity_conflict",
            "exchange_evidence_unavailable",
            "exchange_schema_invalid",
            "exchange_identity_conflict",
            "exchange_history_incomplete",
            "exchange_capture_timeout",
            "exchange_response_size_exceeded",
            "exchange_state_conflict",
        }
    )


def test_contract_records_have_exact_fields_are_frozen_and_use_slots():
    observation = _observation()
    plan = _plan()

    assert [field.name for field in fields(observation)] == [
        "reservation_ref",
        "classification",
        "reason_code",
        "source_fingerprint",
        "exchange_fingerprint",
    ]
    assert [field.name for field in fields(plan)] == [
        "schema_version",
        "status",
        "observations",
        "source_fingerprint",
        "exchange_snapshot_fingerprint",
        "evidence_fingerprint",
        "confirmation_token",
        "action_count",
    ]
    assert not hasattr(observation, "__dict__")
    assert not hasattr(plan, "__dict__")
    with pytest.raises(FrozenInstanceError):
        observation.reason_code = "exchange_state_conflict"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.status = "refused"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("classification", "reason_code"),
    [
        (
            ReservationClassification.PROVEN_TERMINAL,
            "exact_close_and_position_terminal",
        ),
        (
            ReservationClassification.ACTIVE,
            "exact_position_currently_live",
        ),
        (
            ReservationClassification.ACTIVE,
            "exact_close_order_currently_pending",
        ),
        (
            ReservationClassification.ACTIVE,
            "exact_close_order_nonterminal",
        ),
        (
            ReservationClassification.UNKNOWN,
            "local_evidence_incomplete",
        ),
        (
            ReservationClassification.UNKNOWN,
            "exchange_state_conflict",
        ),
    ],
)
def test_observation_accepts_only_a_reason_for_its_classification(
    classification, reason_code
):
    observation = _observation(
        classification=classification,
        reason_code=reason_code,
    )
    assert observation.reason_code == reason_code


@pytest.mark.parametrize(
    ("classification", "reason_code"),
    [
        (ReservationClassification.PROVEN_TERMINAL, "exchange_state_conflict"),
        (ReservationClassification.ACTIVE, "exact_close_and_position_terminal"),
        (ReservationClassification.UNKNOWN, "exact_position_currently_live"),
        (ReservationClassification.UNKNOWN, "future_extension"),
    ],
)
def test_observation_rejects_unknown_or_cross_classification_reason_codes(
    classification, reason_code
):
    with pytest.raises(ValueError, match="reason_code"):
        _observation(classification=classification, reason_code=reason_code)


def test_observation_requires_the_exact_enum_type():
    with pytest.raises(TypeError, match="classification"):
        _observation(  # type: ignore[arg-type]
            classification="PROVEN_TERMINAL",
        )


@pytest.mark.parametrize(
    "invalid",
    [
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "",
        1,
        True,
        None,
    ],
)
@pytest.mark.parametrize(
    "field_name",
    [
        "reservation_ref",
        "source_fingerprint",
        "exchange_fingerprint",
    ],
)
def test_observation_requires_lowercase_64_hex_references_and_fingerprints(
    field_name, invalid
):
    values = {
        "reservation_ref": FINGERPRINT_A,
        "classification": ReservationClassification.PROVEN_TERMINAL,
        "reason_code": "exact_close_and_position_terminal",
        "source_fingerprint": FINGERPRINT_B,
        "exchange_fingerprint": FINGERPRINT_C,
    }
    values[field_name] = invalid

    with pytest.raises((TypeError, ValueError), match=field_name):
        BoundCloseReservationObservation(**values)


@pytest.mark.parametrize(
    "field_name",
    [
        "source_fingerprint",
        "exchange_snapshot_fingerprint",
        "evidence_fingerprint",
    ],
)
def test_plan_requires_lowercase_64_hex_fingerprints(field_name):
    values = {
        "schema_version": 1,
        "status": "ready",
        "observations": (_observation(),),
        "source_fingerprint": FINGERPRINT_A,
        "exchange_snapshot_fingerprint": FINGERPRINT_B,
        "evidence_fingerprint": FINGERPRINT_C,
        "confirmation_token": FINGERPRINT_D,
        "action_count": 1,
    }
    values[field_name] = "F" * 64

    with pytest.raises(ValueError, match=field_name):
        BoundCloseReservationRecoveryPlan(**values)


@pytest.mark.parametrize("invalid", [True, 1.0, "1", 0, 2])
def test_plan_schema_version_is_the_exact_integer_one(invalid):
    with pytest.raises((TypeError, ValueError), match="schema_version"):
        _plan(schema_version=invalid)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid", [True, 1.0, "1", -1])
def test_plan_action_count_is_a_strict_nonnegative_integer(invalid):
    with pytest.raises((TypeError, ValueError), match="action_count"):
        _plan(action_count=invalid)  # type: ignore[arg-type]


def test_ready_plan_requires_a_nonempty_all_terminal_population():
    assert _plan().status == "ready"

    with pytest.raises(ValueError, match="status"):
        _plan(observations=(), status="ready", action_count=0)
    with pytest.raises(ValueError, match="status"):
        _plan(
            observations=(
                _observation(
                    classification=ReservationClassification.ACTIVE,
                    reason_code="exact_position_currently_live",
                ),
            ),
            status="ready",
            action_count=1,
        )
    with pytest.raises(ValueError, match="status"):
        _plan(status="refused", action_count=0)


@pytest.mark.parametrize(
    "observations",
    [
        (),
        (
            _observation(
                classification=ReservationClassification.ACTIVE,
                reason_code="exact_position_currently_live",
            ),
        ),
        (
            _observation(
                classification=ReservationClassification.UNKNOWN,
                reason_code="exchange_history_incomplete",
            ),
        ),
    ],
)
def test_refused_plan_is_required_for_every_not_ready_population(observations):
    plan = _plan(
        observations=observations,
        status="refused",
        action_count=0,
    )
    assert plan.status == "refused"
    assert plan.action_count == 0


def test_ready_action_count_equals_the_number_of_observations():
    with pytest.raises(ValueError, match="action_count"):
        _plan(action_count=0)
    with pytest.raises(ValueError, match="action_count"):
        _plan(action_count=2)


def test_refused_action_count_must_be_zero():
    active = _observation(
        classification=ReservationClassification.ACTIVE,
        reason_code="exact_close_order_nonterminal",
    )
    with pytest.raises(ValueError, match="action_count"):
        _plan(observations=(active,), status="refused", action_count=1)


def test_plan_requires_an_exact_tuple_and_at_most_64_observations():
    with pytest.raises(TypeError, match="observations"):
        _plan(observations=[_observation()])  # type: ignore[arg-type]

    observations = tuple(
        _observation(reservation_ref=f"{index:064x}") for index in range(64)
    )
    assert _plan(observations=observations).action_count == 64

    with pytest.raises(ValueError, match="64"):
        _plan(observations=observations + (_observation(),))


def test_contract_constructors_reject_unknown_fields():
    with pytest.raises(TypeError, match="unexpected"):
        BoundCloseReservationObservation(  # type: ignore[call-arg]
            reservation_ref=FINGERPRINT_A,
            classification=ReservationClassification.PROVEN_TERMINAL,
            reason_code="exact_close_and_position_terminal",
            source_fingerprint=FINGERPRINT_B,
            exchange_fingerprint=FINGERPRINT_C,
            unexpected="not-allowed",
        )
    with pytest.raises(TypeError, match="unexpected"):
        BoundCloseReservationRecoveryPlan(  # type: ignore[call-arg]
            schema_version=1,
            status="ready",
            observations=(_observation(),),
            source_fingerprint=FINGERPRINT_A,
            exchange_snapshot_fingerprint=FINGERPRINT_B,
            evidence_fingerprint=FINGERPRINT_C,
            confirmation_token=FINGERPRINT_D,
            action_count=1,
            unexpected="not-allowed",
        )


def test_canonical_json_is_stable_and_accepts_only_aware_utc_times():
    captured_at = datetime(2026, 8, 15, 12, 34, 56, 123456, tzinfo=UTC)
    left = {"captured_at": captured_at, "enabled": True, "count": 1}
    right = {"count": 1, "enabled": True, "captured_at": captured_at}

    assert _canonical_json(left) == (
        '{"captured_at":"2026-08-15T12:34:56.123456Z",'
        '"count":1,"enabled":true}'
    )
    assert _sha256_json(left) == _sha256_json(right)

    with pytest.raises(ValueError, match="aware UTC"):
        _canonical_json({"captured_at": captured_at.replace(tzinfo=None)})
    with pytest.raises(ValueError, match="aware UTC"):
        _canonical_json(
            {
                "captured_at": captured_at.astimezone(
                    timezone(timedelta(hours=1))
                )
            }
        )


def test_confirmation_token_must_be_a_nonempty_string():
    values = {
        "schema_version": 1,
        "status": "ready",
        "observations": (_observation(),),
        "source_fingerprint": FINGERPRINT_A,
        "exchange_snapshot_fingerprint": FINGERPRINT_B,
        "evidence_fingerprint": FINGERPRINT_C,
        "confirmation_token": "",
        "action_count": 1,
    }
    with pytest.raises(ValueError, match="confirmation_token"):
        BoundCloseReservationRecoveryPlan(**values)
    values["confirmation_token"] = True
    with pytest.raises(TypeError, match="confirmation_token"):
        BoundCloseReservationRecoveryPlan(**values)


SOURCE_TIME = "2026-08-14 10:00:00.000000"


def _create_reservation_source_database(path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE bound_position_close_reservations (
            id INTEGER PRIMARY KEY,
            pos_id TEXT NOT NULL,
            execution_binding_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE execution_bindings (
            id INTEGER PRIMARY KEY,
            strategy_instance_id TEXT,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            venue TEXT NOT NULL,
            pos_id TEXT,
            status TEXT NOT NULL,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE execution_events (
            id INTEGER PRIMARY KEY,
            execution_binding_id INTEGER,
            strategy_instance_id TEXT,
            venue TEXT NOT NULL,
            action TEXT NOT NULL,
            status TEXT NOT NULL,
            symbol TEXT,
            side TEXT,
            order_id TEXT,
            pos_id TEXT,
            before_json TEXT,
            after_json TEXT,
            request_json TEXT,
            response_json TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE execution_order_legs (
            id INTEGER PRIMARY KEY,
            execution_binding_id INTEGER NOT NULL,
            strategy_instance_id TEXT,
            leg_index INTEGER NOT NULL,
            purpose TEXT NOT NULL,
            order_kind TEXT NOT NULL,
            order_id TEXT,
            client_order_id TEXT,
            pos_id TEXT,
            venue TEXT NOT NULL,
            attribution_status TEXT NOT NULL,
            status TEXT NOT NULL,
            attribution_evidence_json TEXT,
            terminal_reason TEXT,
            request_json TEXT,
            response_json TEXT,
            last_verified_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE position_mutation_intents (
            id INTEGER PRIMARY KEY,
            idempotency_key TEXT NOT NULL,
            operation TEXT NOT NULL,
            strategy_instance_id TEXT NOT NULL,
            execution_binding_id INTEGER NOT NULL,
            execution_order_leg_id INTEGER NOT NULL,
            pos_id TEXT NOT NULL,
            order_id TEXT,
            authority_fingerprint TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            venue TEXT NOT NULL,
            status TEXT NOT NULL,
            request_json TEXT NOT NULL,
            response_json TEXT,
            error_json TEXT,
            reserved_at TEXT NOT NULL,
            submitted_at TEXT,
            confirmed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    return connection


def _seed_local_reservation(
    connection: sqlite3.Connection,
    *,
    row_id: int = 1,
    status: str = "submitted",
    include_binding: bool = True,
    include_event: bool = True,
    include_leg: bool = True,
    include_mutation: bool = True,
) -> None:
    pos_id = f"raw-pos-{row_id}"
    binding_id = row_id
    strategy_id = f"raw-strategy-{row_id}"
    order_id = f"raw-close-order-{row_id}"
    connection.execute(
        "INSERT INTO bound_position_close_reservations VALUES (?, ?, ?, ?, ?, ?, ?)",
        (row_id, pos_id, binding_id, status, None, SOURCE_TIME, SOURCE_TIME),
    )
    if include_binding:
        connection.execute(
            "INSERT INTO execution_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                binding_id,
                strategy_id,
                f"COIN{row_id}USDT",
                "long",
                "deepcoin",
                pos_id,
                "closed",
                '{"source":"test"}',
                SOURCE_TIME,
                SOURCE_TIME,
            ),
        )
    if include_event:
        connection.execute(
            "INSERT INTO execution_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                binding_id,
                strategy_id,
                "deepcoin",
                "close_bound_position_market",
                "submitted",
                f"COIN{row_id}USDT",
                "long",
                order_id,
                pos_id,
                '{"position_size":"1"}',
                '{"close_size":"1"}',
                '{"closePosId":"redacted-in-source-output"}',
                '{"code":"0"}',
                SOURCE_TIME,
            ),
        )
    if include_leg:
        connection.execute(
            "INSERT INTO execution_order_legs VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                binding_id,
                strategy_id,
                row_id,
                "entry",
                "market",
                f"raw-entry-order-{row_id}",
                f"raw-entry-client-{row_id}",
                pos_id,
                "deepcoin",
                "verified",
                "manually_closed",
                '{"proof":"local"}',
                "manual_position_missing",
                "{}",
                "{}",
                SOURCE_TIME,
                SOURCE_TIME,
                SOURCE_TIME,
            ),
        )
    if include_mutation:
        connection.execute(
            "INSERT INTO position_mutation_intents VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row_id,
                f"raw-mutation-key-{row_id}",
                "close_position",
                strategy_id,
                binding_id,
                row_id,
                pos_id,
                order_id,
                "a" * 64,
                "b" * 64,
                "deepcoin",
                "confirmed",
                "{}",
                '{"code":"0"}',
                None,
                SOURCE_TIME,
                SOURCE_TIME,
                SOURCE_TIME,
                SOURCE_TIME,
                SOURCE_TIME,
            ),
        )


def test_source_loader_loads_all_closed_active_statuses_and_exact_descendants(
    tmp_path,
):
    database = tmp_path / "source.sqlite3"
    connection = _create_reservation_source_database(database)
    statuses = (
        "reserved",
        "submitted",
        "submit_unknown",
        "unknown_exchange_outcome",
        "recovery_required",
    )
    for row_id, status in enumerate(statuses, start=1):
        _seed_local_reservation(connection, row_id=row_id, status=status)
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert isinstance(source, BoundCloseReservationSource)
    assert len(source.reservations) == 5
    assert [item.source_status for item in source.reservations] == list(statuses)
    assert all(isinstance(item, LocalReservationEvidence) for item in source.reservations)
    assert all(item.local_reason_code is None for item in source.reservations)
    assert all(item.close_event_ref for item in source.reservations)
    assert all(item.close_order_ref for item in source.reservations)
    assert all(item.entry_leg is not None for item in source.reservations)
    assert all(len(item.close_mutations) == 1 for item in source.reservations)
    assert all(item.close_mutations[0].status == "confirmed" for item in source.reservations)
    assert source.source_fingerprint == _sha256_json(
        {"reservations": source.reservations}
    )


def test_source_loader_accepts_exact_closed_leg_of_an_active_split_binding(
    tmp_path,
):
    database = tmp_path / "split-binding.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.execute(
        "UPDATE execution_bindings SET status = 'active', pos_id = 'raw-live-sibling'"
    )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert len(source.reservations) == 1
    assert source.reservations[0].binding_status == "active"
    assert source.reservations[0].local_reason_code is None


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("missing_binding", "local_evidence_incomplete"),
        ("missing_event", "local_evidence_incomplete"),
        ("duplicate_event", "local_identity_conflict"),
        ("mismatched_event_binding", "local_identity_conflict"),
        ("malformed_event_json", "local_evidence_incomplete"),
        ("malformed_reservation_time", "local_evidence_incomplete"),
        ("unknown_status", "local_evidence_incomplete"),
    ],
)
def test_source_loader_fail_closes_bad_rows_as_unknown_local_facts(
    tmp_path, mutation, expected_reason
):
    database = tmp_path / f"{mutation}.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(
        connection,
        include_binding=mutation != "missing_binding",
        include_event=mutation != "missing_event",
        status="future_status" if mutation == "unknown_status" else "submitted",
    )
    if mutation == "duplicate_event":
        connection.execute(
            "INSERT INTO execution_events SELECT 99, execution_binding_id, "
            "strategy_instance_id, venue, action, status, symbol, side, "
            "'second-order', pos_id, before_json, after_json, request_json, "
            "response_json, created_at FROM execution_events"
        )
    elif mutation == "mismatched_event_binding":
        connection.execute(
            "UPDATE execution_events SET execution_binding_id = 999"
        )
    elif mutation == "malformed_event_json":
        connection.execute("UPDATE execution_events SET response_json = '{'")
    elif mutation == "malformed_reservation_time":
        connection.execute(
            "UPDATE bound_position_close_reservations SET updated_at = 'not-a-time'"
        )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert len(source.reservations) == 1
    assert source.reservations[0].local_reason_code == expected_reason


def test_source_loader_fail_closes_65_rows_without_returning_a_partial_population(
    tmp_path,
):
    database = tmp_path / "overflow.sqlite3"
    connection = _create_reservation_source_database(database)
    for row_id in range(1, 66):
        _seed_local_reservation(
            connection,
            row_id=row_id,
            include_leg=False,
            include_mutation=False,
        )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert len(source.reservations) == 1
    assert source.reservations[0].local_reason_code == "local_evidence_incomplete"
    assert source.reservations[0].source_status == "source_overflow"


def test_source_loader_fail_closes_schema_drift_instead_of_raising(tmp_path):
    database = tmp_path / "schema.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.execute("ALTER TABLE execution_events RENAME TO old_execution_events")
    connection.execute(
        "CREATE TABLE execution_events AS SELECT id, action, pos_id FROM old_execution_events"
    )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert len(source.reservations) == 1
    assert source.reservations[0].source_status == "source_schema_invalid"
    assert source.reservations[0].local_reason_code == "local_evidence_incomplete"


def test_source_loader_uses_one_read_only_wal_snapshot(
    tmp_path, monkeypatch
):
    database = tmp_path / "wal.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.commit()
    connection.close()
    baseline = load_bound_close_reservation_source(database)

    import telegram_kol_research.bound_close_reservation_recovery as recovery

    real_connect = sqlite3.connect
    opened = {}

    def recording_connect(*args, **kwargs):
        result = real_connect(*args, **kwargs)
        if kwargs.get("uri") is True and "mode=ro" in str(args[0]):
            opened["reader"] = result
        return result

    monkeypatch.setattr(recovery.sqlite3, "connect", recording_connect)

    def writer_between_selects():
        with pytest.raises(sqlite3.OperationalError):
            opened["reader"].execute(
                "UPDATE execution_bindings SET symbol = 'ILLEGAL' WHERE id = 1"
            )
        writer = real_connect(database)
        writer.execute(
            "UPDATE execution_bindings SET symbol = 'CHANGEDUSDT' WHERE id = 1"
        )
        writer.commit()
        writer.close()

    captured = load_bound_close_reservation_source(
        database,
        between_selects_hook=writer_between_selects,
    )
    changed = load_bound_close_reservation_source(database)

    assert captured.source_fingerprint == baseline.source_fingerprint
    assert changed.source_fingerprint != baseline.source_fingerprint


def test_source_loader_never_serializes_or_reprs_raw_identifiers(tmp_path):
    database = tmp_path / "redaction.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)
    serialized = _canonical_json({"reservations": source.reservations})
    rendered = repr(source)

    for raw_value in (
        "raw-pos-1",
        "raw-strategy-1",
        "raw-close-order-1",
        "raw-entry-order-1",
        "raw-entry-client-1",
        "raw-mutation-key-1",
    ):
        assert raw_value not in serialized
        assert raw_value not in rendered
    assert source.reservations[0].reservation_ref != "1"
    assert len(source.reservations[0].reservation_ref) == 64


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [
        ("execution_bindings", "status"),
        ("execution_events", "status"),
        ("execution_order_legs", "status"),
        ("execution_order_legs", "attribution_status"),
        ("position_mutation_intents", "status"),
    ],
)
def test_source_loader_fail_closes_future_local_status_without_raw_capability(
    tmp_path,
    table_name,
    column_name,
):
    database = tmp_path / f"future-{table_name}-{column_name}.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.execute(
        f'UPDATE "{table_name}" SET "{column_name}" = ?',
        ("future_typo_status",),
    )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)
    evidence = source.reservations[0]

    assert evidence.local_reason_code == "local_evidence_incomplete"
    with pytest.raises(KeyError):
        source._capability._get(evidence.reservation_ref)


@pytest.mark.parametrize(
    ("table_name", "missing_column"),
    [
        ("execution_order_legs", "leg_index"),
        ("execution_order_legs", "order_kind"),
        ("execution_order_legs", "client_order_id"),
        ("execution_order_legs", "terminal_reason"),
        ("position_mutation_intents", "idempotency_key"),
        ("position_mutation_intents", "authority_fingerprint"),
        ("position_mutation_intents", "request_fingerprint"),
    ],
)
def test_source_loader_requires_every_real_identity_column(
    tmp_path,
    table_name,
    missing_column,
):
    database = tmp_path / f"missing-{table_name}-{missing_column}.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    columns = [
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{table_name}")')
        if row[1] != missing_column
    ]
    projection = ", ".join(f'"{column}"' for column in columns)
    connection.execute(f'ALTER TABLE "{table_name}" RENAME TO "old_{table_name}"')
    connection.execute(
        f'CREATE TABLE "{table_name}" AS '
        f'SELECT {projection} FROM "old_{table_name}"'
    )
    connection.commit()
    connection.close()

    source = load_bound_close_reservation_source(database)

    assert len(source.reservations) == 1
    assert source.reservations[0].source_status == "source_schema_invalid"
    assert source.reservations[0].local_reason_code == "local_evidence_incomplete"


@pytest.mark.parametrize(
    ("table_name", "column_name", "changed_value"),
    [
        ("execution_order_legs", "leg_index", 9),
        ("execution_order_legs", "order_kind", "trigger_limit"),
        ("execution_order_legs", "client_order_id", "raw-other-client"),
        ("execution_order_legs", "terminal_reason", "raw-other-terminal"),
        (
            "position_mutation_intents",
            "idempotency_key",
            "raw-other-idempotency",
        ),
        ("position_mutation_intents", "authority_fingerprint", "c" * 64),
        ("position_mutation_intents", "request_fingerprint", "d" * 64),
    ],
)
def test_source_identity_field_drift_changes_fingerprint_without_raw_disclosure(
    tmp_path,
    table_name,
    column_name,
    changed_value,
):
    database = tmp_path / f"drift-{table_name}-{column_name}.sqlite3"
    connection = _create_reservation_source_database(database)
    _seed_local_reservation(connection)
    connection.commit()
    baseline = load_bound_close_reservation_source(database)
    connection.execute(
        f'UPDATE "{table_name}" SET "{column_name}" = ?',
        (changed_value,),
    )
    connection.commit()
    connection.close()

    changed = load_bound_close_reservation_source(database)
    serialized = _canonical_json({"reservations": changed.reservations})

    assert changed.source_fingerprint != baseline.source_fingerprint
    if isinstance(changed_value, str):
        assert changed_value not in serialized
        assert changed_value not in repr(changed)


class _RecoveryReaderHttpClient:
    def __init__(self, *, stream_mode="normal"):
        self.requests = []
        self.close_calls = 0
        self.stream_mode = stream_mode
        self.yielded = 0

    def request(self, method, request_path, content="", headers=None, timeout=None):
        raise AssertionError("bounded recovery reader must use streaming HTTP")

    def stream(self, method, request_path, content="", headers=None, timeout=None):
        from contextlib import contextmanager

        @contextmanager
        def response_context():
            self.requests.append((method, request_path, timeout))

            def iter_raw(_response):
                if self.stream_mode == "blocking":
                    time.sleep(0.5)
                elif self.stream_mode == "slow_drip":
                    for _ in range(50):
                        time.sleep(0.01)
                        self.yielded += 1
                        yield b" "
                self.yielded += 1
                yield b'{"code":"0","data":[]}'

            yield type(
                "Response",
                (),
                {
                    "status_code": 200,
                    "headers": {},
                    "iter_raw": iter_raw,
                },
            )()

        return response_context()

    def close(self):
        self.close_calls += 1


def _factory_recovery_reader(monkeypatch, http_client=None):
    selected_http_client = http_client or _RecoveryReaderHttpClient()
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **_kwargs: selected_http_client,
    )
    transport = build_deepcoin_bound_close_reservation_recovery_client_from_env(
        environ={
            "DEEPCOIN_API_KEY": "recovery-key",
            "DEEPCOIN_API_SECRET": "recovery-secret",
            "DEEPCOIN_API_PASSPHRASE": "recovery-passphrase",
        },
        env_file_paths=[],
    )
    return (
        BoundCloseReservationExchangeReader(transport),
        transport,
        selected_http_client,
    )


def test_recovery_reader_exposes_only_closed_get_capabilities_and_one_scope(
    monkeypatch,
):
    reader, _transport, http_client = _factory_recovery_reader(monkeypatch)

    public_names = {name for name in dir(reader) if not name.startswith("_")}
    assert public_names == {
        "read_open_orders",
        "read_order_history",
        "read_position_history",
        "read_positions",
        "read_trade_fills",
        "request_scope",
    }
    for forbidden in (
        "place_order",
        "cancel_order",
        "cancel_trigger_order",
        "replace_order_sltp",
        "transport",
        "client",
    ):
        assert not hasattr(reader, forbidden)

    with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
        assert reader.read_positions() == {"code": "0", "data": []}
        assert reader.read_open_orders() == {"code": "0", "data": []}
        assert reader.read_order_history(
            inst_id="BTC-USDT-SWAP", order_id="order-1", limit=100
        ) == {"code": "0", "data": []}
        assert reader.read_trade_fills(
            inst_id="BTC-USDT-SWAP", order_id="order-1", limit=100
        ) == {"code": "0", "data": []}
        assert reader.read_position_history(
            inst_id="BTC-USDT-SWAP", pos_id="position-1"
        ) == {"code": "0", "data": []}

    assert len(http_client.requests) == 5
    assert {request[0] for request in http_client.requests} == {"GET"}


def test_recovery_reader_scope_cleanup_closes_transport_after_failure(monkeypatch):
    reader, _transport, _http_client = _factory_recovery_reader(monkeypatch)

    with pytest.raises(RuntimeError, match="capture failed"):
        with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
            raise RuntimeError("capture failed")

    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="active recovery scope",
    ):
        reader.read_positions()


def test_recovery_reader_builder_returns_capability_not_raw_transport(monkeypatch):
    http_client = _RecoveryReaderHttpClient()
    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: http_client)

    reader = build_bound_close_reservation_exchange_reader_from_env(
        environ={
            "DEEPCOIN_API_KEY": "key",
            "DEEPCOIN_API_SECRET": "secret",
            "DEEPCOIN_API_PASSPHRASE": "passphrase",
        },
        env_file_paths=[],
    )

    assert isinstance(reader, BoundCloseReservationExchangeReader)
    assert http_client.requests == []


def test_recovery_reader_rejects_non_factory_transport_before_http():
    http_client = _RecoveryReaderHttpClient()
    unsafe_transport = DeepcoinRestClient(
        DeepcoinCredentials(api_key="key", api_secret="secret", passphrase="pass"),
        http_client=http_client,
        read_only=True,
        trust_env=False,
    )

    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="dedicated recovery factory",
    ):
        BoundCloseReservationExchangeReader(unsafe_transport)

    assert http_client.requests == []


def test_recovery_reader_refuses_reads_outside_owned_scope_and_external_scope(
    monkeypatch,
):
    reader, transport, http_client = _factory_recovery_reader(monkeypatch)

    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="active recovery scope",
    ):
        reader.read_positions()

    with transport.request_scope(
        DeepcoinRequestScope(
            phase="bound_close_reservation_recovery",
            priority=RequestPriority.BACKGROUND,
            deadline_monotonic=time.monotonic() + 1.0,
            max_response_bytes=1024 * 1024,
        )
    ):
        with pytest.raises(
            BoundCloseReservationExchangeConfigurationError,
            match="active recovery scope",
        ):
            reader.read_positions()

    assert http_client.requests == []
    transport.close()


def test_recovery_reader_refuses_nested_scope_and_reuse(monkeypatch):
    reader, _transport, http_client = _factory_recovery_reader(monkeypatch)

    with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
        with pytest.raises(
            BoundCloseReservationExchangeConfigurationError,
            match="already active",
        ):
            with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
                pass

    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="already consumed",
    ):
        with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
            pass

    assert http_client.requests == []


@pytest.mark.skipif(
    not all(
        hasattr(signal, name)
        for name in ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")
    ),
    reason="POSIX interval timers are unavailable",
)
@pytest.mark.parametrize("stream_mode", ["blocking", "slow_drip"])
def test_recovery_reader_interrupts_real_blocking_stream_at_50ms(
    monkeypatch, stream_mode
):
    http_client = _RecoveryReaderHttpClient(stream_mode=stream_mode)
    reader, _transport, _http_client = _factory_recovery_reader(
        monkeypatch, http_client
    )

    started = time.monotonic()
    with pytest.raises(BoundCloseReservationExchangeDeadlineExceeded):
        with reader.request_scope(deadline_monotonic=started + 0.05):
            reader.read_positions()
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    if stream_mode == "slow_drip":
        assert http_client.yielded < 25
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) is signal.SIG_DFL
    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="already consumed",
    ):
        with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
            pass


@pytest.mark.skipif(
    not all(
        hasattr(signal, name)
        for name in ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")
    ),
    reason="POSIX interval timers are unavailable",
)
@pytest.mark.parametrize("stage", ["json_decode", "collection_parse"])
def test_recovery_deadline_covers_decode_and_caller_collection_parse(
    monkeypatch, stage
):
    reader, _transport, _http_client = _factory_recovery_reader(monkeypatch)
    if stage == "json_decode":
        import telegram_kol_research.deepcoin_client as deepcoin_client

        real_json_loads = deepcoin_client.json.loads

        def slow_json_loads(*args, **kwargs):
            time.sleep(0.5)
            return real_json_loads(*args, **kwargs)

        monkeypatch.setattr(deepcoin_client.json, "loads", slow_json_loads)

    started = time.monotonic()
    with pytest.raises(BoundCloseReservationExchangeDeadlineExceeded):
        with reader.request_scope(deadline_monotonic=started + 0.05):
            reader.read_positions()
            if stage == "collection_parse":
                time.sleep(0.5)

    assert time.monotonic() - started < 0.25


def test_recovery_reader_fails_closed_off_main_thread_before_http(monkeypatch):
    reader, _transport, http_client = _factory_recovery_reader(monkeypatch)
    captured = []

    def run():
        try:
            with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
                reader.read_positions()
        except BaseException as exc:
            captured.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=1.0)

    assert worker.is_alive() is False
    assert len(captured) == 1
    assert isinstance(captured[0], BoundCloseReservationExchangeConfigurationError)
    assert "main thread" in str(captured[0])
    assert http_client.requests == []


@pytest.mark.skipif(
    not all(
        hasattr(signal, name)
        for name in ("SIGALRM", "ITIMER_REAL", "getitimer", "setitimer")
    ),
    reason="POSIX interval timers are unavailable",
)
def test_recovery_reader_fails_closed_on_existing_timer_or_custom_handler(
    monkeypatch,
):
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        pytest.skip("test process already owns the real-time interval timer")

    reader_with_timer, _transport, timer_http = _factory_recovery_reader(monkeypatch)
    signal.setitimer(signal.ITIMER_REAL, 5.0)
    try:
        with pytest.raises(
            BoundCloseReservationExchangeConfigurationError,
            match="timer conflict",
        ):
            with reader_with_timer.request_scope(
                deadline_monotonic=time.monotonic() + 1.0
            ):
                reader_with_timer.read_positions()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
    assert timer_http.requests == []

    reader_with_handler, _transport, handler_http = _factory_recovery_reader(
        monkeypatch
    )
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, lambda _signum, _frame: None)
    try:
        with pytest.raises(
            BoundCloseReservationExchangeConfigurationError,
            match="timer conflict",
        ):
            with reader_with_handler.request_scope(
                deadline_monotonic=time.monotonic() + 1.0
            ):
                reader_with_handler.read_positions()
    finally:
        signal.signal(signal.SIGALRM, previous_handler)
    assert handler_http.requests == []


def test_recovery_reader_fails_closed_without_posix_timer_capability(
    monkeypatch,
):
    import telegram_kol_research.bound_close_reservation_recovery as recovery

    reader, _transport, http_client = _factory_recovery_reader(monkeypatch)
    monkeypatch.setattr(recovery.signal, "getitimer", None)

    with pytest.raises(
        BoundCloseReservationExchangeConfigurationError,
        match="unavailable",
    ):
        with reader.request_scope(deadline_monotonic=time.monotonic() + 1.0):
            reader.read_positions()

    assert http_client.requests == []
