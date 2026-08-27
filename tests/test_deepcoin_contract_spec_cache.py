from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
import errno
import json
import os
import stat
from time import monotonic
from zoneinfo import ZoneInfo

import pytest

from telegram_kol_research.deepcoin_contract_spec_cache import (
    DEEPCOIN_CONTRACT_SPEC_SNAPSHOT_SCHEMA_VERSION,
)
from telegram_kol_research.deepcoin_contract_spec_cache import (
    DeepcoinContractSpecSnapshot,
)
from telegram_kol_research.deepcoin_contract_spec_cache import (
    DeepcoinContractSpecRefreshOrchestrator,
)
from telegram_kol_research.deepcoin_contract_spec_cache import (
    load_deepcoin_contract_spec_snapshot,
)
from telegram_kol_research.deepcoin_contract_spec_cache import (
    publish_deepcoin_contract_spec_snapshot,
)
from telegram_kol_research.deepcoin_contract_spec_cache import (
    validate_deepcoin_instrument_snapshot,
)
from telegram_kol_research.deepcoin_contract_specs import DeepcoinContractSpec


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
TTL = timedelta(hours=24)


def _row(instrument_id: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "instType": "SWAP",
        "instId": instrument_id,
        "ctVal": "1",
        "lotSz": "1",
        "minSz": "1",
        "tickSz": "0.001",
        "state": "live",
    }
    row.update(overrides)
    return row


def test_refresh_orchestrator_uses_bounded_half_ttl_schedule():
    class Provider:
        ttl = timedelta(seconds=20)

        def refresh(self):
            return True

    orchestrator = DeepcoinContractSpecRefreshOrchestrator(
        Provider(),
        refresh_timeout_seconds=1,
        minimum_interval_seconds=2,
        maximum_interval_seconds=8,
    )

    assert orchestrator.interval_seconds == 8


def test_refresh_orchestrator_reports_timeout_without_blocking_event_loop():
    import asyncio
    import threading

    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Provider:
        ttl = timedelta(seconds=20)
        snapshot = None
        metadata = type(
            "Metadata",
            (),
            {"last_success_at": None, "expires_at": None, "last_error": None},
        )()

        def refresh(self):
            calls.append("called")
            entered.set()
            release.wait(timeout=1)
            return True

    orchestrator = DeepcoinContractSpecRefreshOrchestrator(
        Provider(),
        refresh_timeout_seconds=0.01,
        minimum_interval_seconds=1,
        maximum_interval_seconds=60,
        now_provider=lambda: NOW,
    )

    async def exercise():
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        result = await orchestrator.refresh_once()
        assert heartbeat.is_set()
        second_result = await orchestrator.refresh_once()
        release.set()
        return result, second_result

    result, second_result = asyncio.run(exercise())

    assert entered.is_set()
    assert calls == ["called"]
    assert result["state"] == "unavailable"
    assert result["refresh_succeeded"] is False
    assert result["last_error"] == "refresh_timeout"
    assert result["last_refresh_succeeded"] is False
    assert result["error_category"] == "refresh_timeout"
    assert second_result["last_error"] == "refresh_timeout"


def test_refresh_orchestrator_never_reports_fresh_with_incoherent_metadata():
    class Provider:
        ttl = timedelta(hours=24)
        snapshot = type(
            "Snapshot",
            (),
            {"fetched_at": NOW, "expires_at": NOW + timedelta(hours=24)},
        )()
        metadata = type(
            "Metadata",
            (),
            {
                "last_success_at": NOW,
                "expires_at": "malformed",
                "last_error": None,
            },
        )()

        def refresh(self):
            return False

    orchestrator = DeepcoinContractSpecRefreshOrchestrator(
        Provider(),
        refresh_timeout_seconds=1,
        now_provider=lambda: NOW,
    )

    assert orchestrator.status()["state"] == "unavailable"


def test_refresh_orchestrator_projects_bounded_fresh_health_after_failed_refresh():
    class Provider:
        ttl = timedelta(hours=24)
        snapshot = type(
            "Snapshot",
            (),
            {"fetched_at": NOW, "expires_at": NOW + timedelta(hours=24)},
        )()
        metadata = type(
            "Metadata",
            (),
            {
                "last_success_at": NOW,
                "expires_at": NOW + timedelta(hours=24),
                "last_error": None,
            },
        )()

        def refresh(self):
            raise PermissionError("secret absolute path and response body")

    orchestrator = DeepcoinContractSpecRefreshOrchestrator(
        Provider(),
        refresh_timeout_seconds=1,
        now_provider=lambda: NOW,
    )

    import asyncio

    status = asyncio.run(orchestrator.refresh_once())

    assert status == {
        "state": "fresh",
        "refresh_succeeded": False,
        "fetched_at": "2026-08-08T12:00:00Z",
        "last_success_at": "2026-08-08T12:00:00Z",
        "expires_at": "2026-08-09T12:00:00Z",
        "last_refresh_succeeded": False,
        "error_category": "permission_denied",
        "last_error": "refresh_failed:PermissionError",
    }
    assert "secret" not in repr(status)


def test_validate_builds_immutable_complete_snapshot_with_exact_specs():
    rows = [
        _row("BTC-USDT-SWAP", ctVal="0.001", tickSz="0.1"),
        _row("ETH-USDT-SWAP", ctVal="0.1", lotSz="0.1", minSz="0.1", tickSz="0.01"),
        _row("SOL-USDT-SWAP"),
    ]

    snapshot = validate_deepcoin_instrument_snapshot(rows, fetched_at=NOW, ttl=TTL)

    assert isinstance(snapshot, DeepcoinContractSpecSnapshot)
    assert snapshot.schema_version == DEEPCOIN_CONTRACT_SPEC_SNAPSHOT_SCHEMA_VERSION
    assert snapshot.venue == "deepcoin"
    assert snapshot.source_path == "/deepcoin/market/instruments?instType=SWAP"
    assert snapshot.fetched_at == NOW
    assert snapshot.expires_at == NOW + TTL
    assert snapshot.specs_by_instrument_id["SOL-USDT-SWAP"] == DeepcoinContractSpec(
        instrument_id="SOL-USDT-SWAP",
        contract_value=1,
        quantity_step=1,
        min_quantity=1,
        price_tick=0.001,
    )
    assert snapshot.states_by_instrument_id == {
        "BTC-USDT-SWAP": "live",
        "ETH-USDT-SWAP": "live",
        "SOL-USDT-SWAP": "live",
    }
    assert len(snapshot.source_digest_sha256) == 64
    with pytest.raises(TypeError):
        snapshot.specs_by_instrument_id["DOGE-USDT-SWAP"] = snapshot.specs_by_instrument_id[
            "SOL-USDT-SWAP"
        ]
    with pytest.raises(FrozenInstanceError):
        snapshot.venue = "other"


def test_validate_retains_non_live_capability_but_excludes_it_from_live_specs():
    snapshot = validate_deepcoin_instrument_snapshot(
        [
            _row("BTC-USDT-SWAP"),
            _row("SOL-USDT-SWAP", state="suspend"),
            _row("DOGE-USDT-SWAP", state="preopen"),
        ],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert set(snapshot.specs_by_instrument_id) == {"BTC-USDT-SWAP"}
    assert snapshot.states_by_instrument_id == {
        "BTC-USDT-SWAP": "live",
        "DOGE-USDT-SWAP": "preopen",
        "SOL-USDT-SWAP": "suspend",
    }


@pytest.mark.parametrize(
    "missing_field",
    ["instType", "instId", "ctVal", "lotSz", "minSz", "tickSz", "state"],
)
def test_validate_rejects_missing_required_fields(missing_field):
    row = _row("BTC-USDT-SWAP")
    del row[missing_field]

    with pytest.raises(ValueError, match=missing_field):
        validate_deepcoin_instrument_snapshot([row], fetched_at=NOW, ttl=TTL)


@pytest.mark.parametrize("field", ["ctVal", "lotSz", "minSz", "tickSz"])
def test_validate_rejects_boolean_numeric_fields(field):
    with pytest.raises(ValueError, match=field):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", **{field: True})],
            fetched_at=NOW,
            ttl=TTL,
        )


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("field", ["ctVal", "lotSz", "minSz", "tickSz"])
def test_validate_rejects_non_finite_numeric_fields(field, value):
    with pytest.raises(ValueError, match=field):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", **{field: value})],
            fetched_at=NOW,
            ttl=TTL,
        )


@pytest.mark.parametrize("value", ["0", "-0.1"])
@pytest.mark.parametrize("field", ["ctVal", "lotSz", "minSz", "tickSz"])
def test_validate_rejects_non_positive_numeric_fields(field, value):
    with pytest.raises(ValueError, match=field):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", **{field: value})],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_rejects_duplicate_instrument_ids_even_when_identical():
    row = _row("BTC-USDT-SWAP")

    with pytest.raises(ValueError, match="duplicate.*BTC-USDT-SWAP"):
        validate_deepcoin_instrument_snapshot([row, dict(row)], fetched_at=NOW, ttl=TTL)


def test_validate_rejects_conflicting_duplicate_instrument_ids():
    with pytest.raises(ValueError, match="duplicate.*BTC-USDT-SWAP"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP"), _row("BTC-USDT-SWAP", lotSz="0.1")],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_rejects_non_swap_product_in_swap_response():
    with pytest.raises(ValueError, match="instType"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", instType="SPOT")],
            fetched_at=NOW,
            ttl=TTL,
        )


@pytest.mark.parametrize(
    "instrument_id",
    ["BTC_USDT_SWAP", "BTC-USDT", "-USDT-SWAP", "btc-usdt-swap", " BTC-USDT-SWAP"],
)
def test_validate_rejects_malformed_instrument_ids(instrument_id):
    with pytest.raises(ValueError, match="instId"):
        validate_deepcoin_instrument_snapshot(
            [_row(instrument_id)],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_ignores_well_formed_non_usdt_swap_instruments():
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP"), _row("BTC-USDC-SWAP")],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert set(snapshot.states_by_instrument_id) == {"BTC-USDT-SWAP"}


def test_validate_rejects_minimum_quantity_incompatible_with_quantity_step():
    with pytest.raises(ValueError, match="minSz.*lotSz"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", lotSz="0.3", minSz="1")],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_uses_decimal_before_converting_to_existing_spec_type():
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP", lotSz="0.1", minSz="0.3")],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert snapshot.specs_by_instrument_id["BTC-USDT-SWAP"].min_quantity == 0.3


def test_validate_digest_is_deterministic_across_row_and_key_order():
    btc = _row("BTC-USDT-SWAP", ctVal="0.001")
    sol = _row("SOL-USDT-SWAP")
    reversed_btc = dict(reversed(list(btc.items())))

    first = validate_deepcoin_instrument_snapshot([btc, sol], fetched_at=NOW, ttl=TTL)
    second = validate_deepcoin_instrument_snapshot(
        [sol, reversed_btc], fetched_at=NOW, ttl=TTL
    )

    assert first.source_digest_sha256 == second.source_digest_sha256


def test_validate_high_precision_incompatible_minimum_is_not_rounded_to_multiple():
    with pytest.raises(ValueError, match="minSz.*lotSz"):
        validate_deepcoin_instrument_snapshot(
            [
                _row(
                    "BTC-USDT-SWAP",
                    lotSz="1",
                    minSz="10000000000000000000000000000.1",
                )
            ],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_high_precision_exact_multiple_is_accepted():
    snapshot = validate_deepcoin_instrument_snapshot(
        [
            _row(
                "BTC-USDT-SWAP",
                lotSz="0.12345678901234567890123456789",
                minSz="0.37037036703703703670370370367",
            )
        ],
        fetched_at=NOW,
        ttl=TTL,
    )

    capability = snapshot.capabilities_by_instrument_id["BTC-USDT-SWAP"]
    assert capability.quantity_step == Decimal("0.12345678901234567890123456789")
    assert capability.min_quantity == Decimal("0.37037036703703703670370370367")


def test_validate_high_precision_values_do_not_collide_in_digest():
    first = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP", ctVal="1.00000000000000000000000000001")],
        fetched_at=NOW,
        ttl=TTL,
    )
    second = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP", ctVal="1.00000000000000000000000000002")],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert first.source_digest_sha256 != second.source_digest_sha256


@pytest.mark.parametrize("field", ["ctVal", "lotSz", "minSz", "tickSz"])
@pytest.mark.parametrize("value", ["1e1000000", "1e-1000000"])
def test_validate_rejects_extreme_numeric_exponents_promptly(field, value):
    started_at = monotonic()

    with pytest.raises(ValueError, match=rf"{field}.*safe numeric bounds"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", **{field: value})],
            fetched_at=NOW,
            ttl=TTL,
        )

    assert monotonic() - started_at < 1


@pytest.mark.parametrize("field", ["ctVal", "lotSz", "minSz", "tickSz"])
def test_validate_rejects_excessive_significant_digits(field):
    value = "1." + ("2" * 64)

    with pytest.raises(ValueError, match=rf"{field}.*safe numeric bounds"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", **{field: value})],
            fetched_at=NOW,
            ttl=TTL,
        )


@pytest.mark.parametrize("field", ["ctVal", "lotSz", "minSz", "tickSz"])
def test_validate_rejects_oversized_numeric_input_before_decimal_work(field):
    value = "1" * 129

    with pytest.raises(ValueError, match=rf"{field}.*safe numeric bounds"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", **{field: value})],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_snapshot_canonical_rows_round_trip_losslessly_through_json():
    snapshot = validate_deepcoin_instrument_snapshot(
        [
            _row(
                "BTC-USDT-SWAP",
                ctVal="1.0000000000000000000000000000100",
                lotSz="0.1234567890123456789012345678900",
                minSz="0.3703703670370370367037037036700",
                tickSz="0.0000000000000000000000000000100",
                state=" SUSPEND ",
            )
        ],
        fetched_at=NOW,
        ttl=TTL,
    )

    serialized_rows = json.loads(json.dumps(snapshot.to_instrument_rows()))
    reloaded = validate_deepcoin_instrument_snapshot(
        serialized_rows,
        fetched_at=snapshot.fetched_at,
        ttl=snapshot.expires_at - snapshot.fetched_at,
        source_path=snapshot.source_path,
    )

    capability = snapshot.capabilities_by_instrument_id["BTC-USDT-SWAP"]
    assert capability.contract_value == Decimal("1.00000000000000000000000000001")
    assert capability.quantity_step == Decimal("0.12345678901234567890123456789")
    assert capability.min_quantity == Decimal("0.37037036703703703670370370367")
    assert capability.price_tick == Decimal("0.00000000000000000000000000001")
    assert reloaded.to_instrument_rows() == snapshot.to_instrument_rows()
    assert reloaded.source_digest_sha256 == snapshot.source_digest_sha256


@pytest.mark.parametrize(
    ("fetched_at", "ttl", "match"),
    [
        (datetime(2026, 8, 8, 12, 0), TTL, "timezone-aware"),
        (NOW, timedelta(0), "ttl"),
        (NOW, timedelta(seconds=-1), "ttl"),
    ],
)
def test_validate_rejects_invalid_snapshot_timing(fetched_at, ttl, match):
    with pytest.raises(ValueError, match=match):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP")],
            fetched_at=fetched_at,
            ttl=ttl,
        )


def test_validate_normalizes_fetched_at_to_utc_before_ttl_across_dst_fallback():
    # Vancouver falls back at 02:00 on 2026-11-01. Two elapsed hours after
    # 00:30 PDT is 01:30 PST, not 02:30 PST.
    local_fetched_at = datetime(
        2026, 11, 1, 0, 30, tzinfo=ZoneInfo("America/Vancouver")
    )

    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")],
        fetched_at=local_fetched_at,
        ttl=timedelta(hours=2),
    )

    assert snapshot.fetched_at == datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)
    assert snapshot.fetched_at.tzinfo is timezone.utc
    assert snapshot.expires_at == datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)
    assert snapshot.expires_at.tzinfo is timezone.utc


def test_validate_rejects_empty_or_non_list_snapshot():
    with pytest.raises(ValueError, match="non-empty list"):
        validate_deepcoin_instrument_snapshot([], fetched_at=NOW, ttl=TTL)

    with pytest.raises(ValueError, match="non-empty list"):
        validate_deepcoin_instrument_snapshot("not rows", fetched_at=NOW, ttl=TTL)


def test_validate_rejects_non_mapping_row():
    with pytest.raises(ValueError, match="row 0"):
        validate_deepcoin_instrument_snapshot(["not a mapping"], fetched_at=NOW, ttl=TTL)


def test_validate_rejects_malformed_non_live_required_row():
    with pytest.raises(ValueError, match="tickSz"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP"), _row("SOL-USDT-SWAP", state="suspend", tickSz="bad")],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_validate_rejects_non_string_or_empty_state():
    for state in (None, "", "   ", True):
        with pytest.raises(ValueError, match="state"):
            validate_deepcoin_instrument_snapshot(
                [_row("BTC-USDT-SWAP", state=state)],
                fetched_at=NOW,
                ttl=TTL,
            )


def test_validate_normalizes_state_case_and_whitespace():
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP", state=" LIVE ")],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert snapshot.states_by_instrument_id["BTC-USDT-SWAP"] == "live"
    assert "BTC-USDT-SWAP" in snapshot.specs_by_instrument_id


def test_validate_accepts_decimal_numeric_inputs_without_float_rounding():
    snapshot = validate_deepcoin_instrument_snapshot(
        [
            _row(
                "BTC-USDT-SWAP",
                ctVal=Decimal("0.001"),
                lotSz=Decimal("0.1"),
                minSz=Decimal("0.3"),
                tickSz=Decimal("0.01"),
            )
        ],
        fetched_at=NOW,
        ttl=TTL,
    )

    assert snapshot.specs_by_instrument_id["BTC-USDT-SWAP"].quantity_step == 0.1


@pytest.mark.parametrize("value", ["1e10000", "1e-10000"])
def test_validate_rejects_value_that_cannot_convert_to_a_positive_finite_existing_spec(value):
    with pytest.raises(ValueError, match="ctVal"):
        validate_deepcoin_instrument_snapshot(
            [_row("BTC-USDT-SWAP", ctVal=value)],
            fetched_at=NOW,
            ttl=TTL,
        )


def test_cache_publish_and_load_round_trip_atomically(tmp_path):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP", ctVal="0.001"), _row("SOL-USDT-SWAP")],
        fetched_at=NOW,
        ttl=TTL,
    )

    publish_deepcoin_contract_spec_snapshot(cache_path, snapshot, now=NOW)
    loaded = load_deepcoin_contract_spec_snapshot(cache_path, now=NOW)

    assert loaded == snapshot
    assert cache_path.stat().st_mode & 0o777 == 0o660
    assert list(tmp_path.glob(f".{cache_path.name}.*.tmp")) == []


def test_cache_publish_clears_inherited_acl_then_restores_agent_deny(
    tmp_path, monkeypatch
):
    from telegram_kol_research import contract_cache_permissions

    cache_path = tmp_path / "deepcoin_contract_specs.json"
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")],
        source_path="/deepcoin/market/instruments?instType=SWAP",
        fetched_at=NOW,
        ttl=TTL,
    )
    events = []
    real_fchmod = os.fchmod

    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_contract_spec_cache.sys.platform", "linux"
    )

    def recording_run(args, **_kwargs):
        events.append(("clear_acl", tuple(args)))

    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_contract_spec_cache.subprocess.run",
        recording_run,
    )

    def recording_fchmod(fd, mode):
        events.append(("fchmod", mode))
        real_fchmod(fd, mode)

    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_contract_spec_cache.os.fchmod",
        recording_fchmod,
    )

    def recording_agent_deny(fd, *, agent_user):
        metadata = os.fstat(fd)
        events.append(
            (
                "agent_deny",
                agent_user,
                stat.S_ISREG(metadata.st_mode),
                stat.S_IMODE(metadata.st_mode),
            )
        )

    monkeypatch.setattr(
        contract_cache_permissions,
        "set_contract_cache_agent_deny_acl_fd",
        recording_agent_deny,
        raising=False,
    )

    publish_deepcoin_contract_spec_snapshot(cache_path, snapshot, now=NOW)

    assert events[0][0] == "clear_acl"
    assert events[0][1][:3] == ("/usr/bin/setfacl", "-b", "--")
    assert events[1] == ("fchmod", 0o660)
    assert events[2] == ("agent_deny", "telegram-kol-agent", True, 0o660)


def test_cache_publish_closes_descriptor_when_agent_deny_acl_fails(
    tmp_path, monkeypatch
):
    from telegram_kol_research import contract_cache_permissions
    from telegram_kol_research import deepcoin_contract_spec_cache

    cache_path = tmp_path / "deepcoin_contract_specs.json"
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )
    descriptors: list[int] = []
    real_mkstemp = deepcoin_contract_spec_cache.tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        descriptor, temporary_name = real_mkstemp(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor, temporary_name

    monkeypatch.setattr(deepcoin_contract_spec_cache.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(deepcoin_contract_spec_cache.sys, "platform", "linux")
    monkeypatch.setattr(deepcoin_contract_spec_cache.subprocess, "run", lambda *_a, **_k: None)

    def fail_agent_deny(_descriptor, *, agent_user):
        assert agent_user == "telegram-kol-agent"
        raise RuntimeError("acl failed")

    monkeypatch.setattr(
        contract_cache_permissions,
        "set_contract_cache_agent_deny_acl_fd",
        fail_agent_deny,
    )

    with pytest.raises(RuntimeError, match="acl failed"):
        publish_deepcoin_contract_spec_snapshot(cache_path, snapshot, now=NOW)

    assert len(descriptors) == 1
    try:
        os.fstat(descriptors[0])
    except OSError as exc:
        assert exc.errno == errno.EBADF
    else:
        os.close(descriptors[0])
        pytest.fail("temporary cache descriptor remained open")
    assert list(tmp_path.glob(f".{cache_path.name}.*.tmp")) == []


def test_cache_digest_verification_is_independent_of_json_key_and_row_order(tmp_path):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP"), _row("SOL-USDT-SWAP")],
        fetched_at=NOW,
        ttl=TTL,
    )
    publish_deepcoin_contract_spec_snapshot(cache_path, snapshot, now=NOW)
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["instruments"].reverse()
    reordered_payload = dict(reversed(list(payload.items())))
    cache_path.write_text(json.dumps(reordered_payload, indent=3), encoding="utf-8")

    loaded = load_deepcoin_contract_spec_snapshot(cache_path, now=NOW)

    assert loaded.source_digest_sha256 == snapshot.source_digest_sha256
    assert loaded.to_instrument_rows() == snapshot.to_instrument_rows()


def test_cache_publish_fsyncs_file_before_atomic_replace(tmp_path, monkeypatch):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )
    events = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd):
        event = "directory_fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_fsync"
        events.append(event)
        return real_fsync(fd)

    def recording_replace(source, destination):
        events.append("replace")
        return real_replace(source, destination)

    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_contract_spec_cache.os.fsync",
        recording_fsync,
    )
    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_contract_spec_cache.os.replace",
        recording_replace,
    )

    publish_deepcoin_contract_spec_snapshot(cache_path, snapshot, now=NOW)

    assert events == ["file_fsync", "replace", "directory_fsync"]


def test_cache_publish_failure_preserves_previous_valid_cache(tmp_path, monkeypatch):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    previous = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )
    replacement = validate_deepcoin_instrument_snapshot(
        [_row("SOL-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )
    publish_deepcoin_contract_spec_snapshot(cache_path, previous, now=NOW)

    def fail_fsync(_fd):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_contract_spec_cache.os.fsync", fail_fsync
    )

    with pytest.raises(OSError, match="injected fsync failure"):
        publish_deepcoin_contract_spec_snapshot(cache_path, replacement, now=NOW)

    assert load_deepcoin_contract_spec_snapshot(cache_path, now=NOW) == previous
    assert list(tmp_path.glob(f".{cache_path.name}.*.tmp")) == []


def test_cache_publish_rejects_oversized_candidate_before_writing(tmp_path, monkeypatch):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    previous = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )
    oversized = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP"), _row("SOL-USDT-SWAP")],
        fetched_at=NOW,
        ttl=TTL,
    )
    publish_deepcoin_contract_spec_snapshot(cache_path, previous, now=NOW)
    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_contract_spec_cache._MAX_CACHE_INSTRUMENT_COUNT",
        1,
    )

    with pytest.raises(ValueError, match="instrument count"):
        publish_deepcoin_contract_spec_snapshot(cache_path, oversized, now=NOW)

    assert load_deepcoin_contract_spec_snapshot(cache_path, now=NOW) == previous
    assert list(tmp_path.glob(f".{cache_path.name}.*.tmp")) == []


def test_cache_load_rejects_corrupt_json(tmp_path):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    cache_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="valid JSON"):
        load_deepcoin_contract_spec_snapshot(cache_path, now=NOW)


def test_cache_load_normalizes_json_parser_resource_failure(tmp_path, monkeypatch):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    cache_path.write_text("{}", encoding="utf-8")

    def fail_from_nesting(_value):
        raise RecursionError("maximum JSON nesting exceeded")

    monkeypatch.setattr(
        "telegram_kol_research.deepcoin_contract_spec_cache.json.loads",
        fail_from_nesting,
    )

    with pytest.raises(ValueError, match="valid JSON"):
        load_deepcoin_contract_spec_snapshot(cache_path, now=NOW)


def test_cache_load_rejects_unsupported_schema_version(tmp_path):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )
    publish_deepcoin_contract_spec_snapshot(cache_path, snapshot, now=NOW)
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["schema_version"] += 1
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="schema_version"):
        load_deepcoin_contract_spec_snapshot(cache_path, now=NOW)


def test_cache_load_rejects_digest_mismatch(tmp_path):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )
    publish_deepcoin_contract_spec_snapshot(cache_path, snapshot, now=NOW)
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    payload["instruments"][0]["ctVal"] = "2"
    cache_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="digest"):
        load_deepcoin_contract_spec_snapshot(cache_path, now=NOW)


def test_cache_load_rejects_future_fetched_at(tmp_path):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )
    publish_deepcoin_contract_spec_snapshot(cache_path, snapshot, now=NOW)

    with pytest.raises(ValueError, match="future"):
        load_deepcoin_contract_spec_snapshot(cache_path, now=NOW - timedelta(microseconds=1))


def test_cache_is_stale_at_exact_expiry_boundary(tmp_path):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )
    publish_deepcoin_contract_spec_snapshot(cache_path, snapshot, now=NOW)

    with pytest.raises(ValueError, match="stale"):
        load_deepcoin_contract_spec_snapshot(cache_path, now=NOW + TTL)


def test_cache_publish_creates_missing_parent_directory(tmp_path):
    cache_path = tmp_path / "not-yet-created" / "nested" / "specs.json"
    snapshot = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )

    publish_deepcoin_contract_spec_snapshot(cache_path, snapshot, now=NOW)

    assert load_deepcoin_contract_spec_snapshot(cache_path, now=NOW) == snapshot


@pytest.mark.parametrize(
    "replacement",
    [
        validate_deepcoin_instrument_snapshot(
            [_row("SOL-USDT-SWAP")],
            fetched_at=NOW + timedelta(microseconds=1),
            ttl=TTL,
        ),
        validate_deepcoin_instrument_snapshot(
            [_row("SOL-USDT-SWAP")],
            fetched_at=NOW - (TTL * 2),
            ttl=TTL,
        ),
    ],
)
def test_cache_publish_rejects_unusable_candidate_and_preserves_previous(
    tmp_path, replacement
):
    cache_path = tmp_path / "deepcoin_contract_specs.json"
    previous = validate_deepcoin_instrument_snapshot(
        [_row("BTC-USDT-SWAP")], fetched_at=NOW, ttl=TTL
    )
    publish_deepcoin_contract_spec_snapshot(cache_path, previous, now=NOW)

    with pytest.raises(ValueError, match="future|stale"):
        publish_deepcoin_contract_spec_snapshot(cache_path, replacement, now=NOW)

    assert load_deepcoin_contract_spec_snapshot(cache_path, now=NOW) == previous
