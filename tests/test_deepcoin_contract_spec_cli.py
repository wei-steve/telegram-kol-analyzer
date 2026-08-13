import json
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.deepcoin_contract_spec_cache import (
    load_deepcoin_contract_spec_snapshot,
    publish_deepcoin_contract_spec_snapshot,
    validate_deepcoin_instrument_snapshot,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0)
ROWS = [
    {
        "instType": "SWAP",
        "instId": "SOL-USDT-SWAP",
        "ctVal": "1",
        "lotSz": "1",
        "minSz": "1",
        "tickSz": "0.001",
        "state": "live",
    }
]


def _publish(cache_path, *, fetched_at=NOW, ttl=timedelta(hours=24)):
    snapshot = validate_deepcoin_instrument_snapshot(
        ROWS,
        fetched_at=fetched_at,
        ttl=ttl,
    )
    publish_deepcoin_contract_spec_snapshot(
        cache_path,
        snapshot,
        now=fetched_at,
    )
    return snapshot


def test_contract_spec_status_is_read_only_when_cache_is_missing(tmp_path):
    cache_path = tmp_path / "missing-parent" / "specs.json"

    result = CliRunner().invoke(
        app,
        [
            "deepcoin-contract-specs",
            "status",
            "--cache-path",
            str(cache_path),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "cache_path": str(cache_path),
        "state": "missing",
    }
    assert not cache_path.parent.exists()


def test_contract_spec_status_reports_only_validated_bounded_metadata(tmp_path):
    cache_path = tmp_path / "specs.json"
    snapshot = _publish(cache_path)

    result = CliRunner().invoke(
        app,
        [
            "deepcoin-contract-specs",
            "status",
            "--cache-path",
            str(cache_path),
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload == {
        "cache_path": str(cache_path),
        "digest_sha256": snapshot.source_digest_sha256,
        "expires_at": snapshot.expires_at.isoformat().replace("+00:00", "Z"),
        "fetched_at": snapshot.fetched_at.isoformat().replace("+00:00", "Z"),
        "instrument_count": 1,
        "live_instrument_count": 1,
        "state": "fresh",
    }
    assert len(result.stdout) < 1024


def test_contract_spec_status_distinguishes_stale_from_invalid(tmp_path):
    stale_path = tmp_path / "stale.json"
    _publish(
        stale_path,
        fetched_at=NOW - timedelta(days=2),
        ttl=timedelta(hours=1),
    )
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("not-json", encoding="utf-8")

    stale = CliRunner().invoke(
        app,
        ["deepcoin-contract-specs", "status", "--cache-path", str(stale_path)],
    )
    invalid = CliRunner().invoke(
        app,
        ["deepcoin-contract-specs", "status", "--cache-path", str(invalid_path)],
    )

    assert stale.exit_code == 0
    assert json.loads(stale.stdout) == {
        "cache_path": str(stale_path),
        "state": "stale",
    }
    assert invalid.exit_code == 0
    assert json.loads(invalid.stdout) == {
        "cache_path": str(invalid_path),
        "state": "invalid",
    }


def test_contract_spec_refresh_publishes_and_prints_non_sensitive_summary(
    tmp_path, monkeypatch
):
    cache_path = tmp_path / "specs.json"
    secret = "never-print-this-api-secret"

    class _Client:
        closed = False

        def list_swap_instruments(self):
            return ROWS

        def close(self):
            self.closed = True

    client = _Client()

    monkeypatch.setattr(
        "telegram_kol_research.cli.build_deepcoin_client_from_env",
        lambda: client,
    )
    monkeypatch.setenv("DEEPCOIN_API_SECRET", secret)

    result = CliRunner().invoke(
        app,
        [
            "deepcoin-contract-specs",
            "refresh",
            "--cache-path",
            str(cache_path),
            "--ttl-hours",
            "12",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["state"] == "fresh"
    assert payload["refresh_succeeded"] is True
    assert payload["instrument_count"] == 1
    assert payload["live_instrument_count"] == 1
    assert len(payload["digest_sha256"]) == 64
    assert secret not in result.stdout
    assert "ctVal" not in result.stdout
    assert len(result.stdout) < 1024
    assert load_deepcoin_contract_spec_snapshot(
        cache_path,
        now=datetime.now(timezone.utc),
    ).source_digest_sha256 == payload["digest_sha256"]
    assert client.closed is True


def test_contract_spec_refresh_fails_nonzero_without_replacing_valid_cache(
    tmp_path, monkeypatch
):
    cache_path = tmp_path / "specs.json"
    previous = _publish(cache_path)
    before = cache_path.read_bytes()
    secret = "signed-request-secret"

    class _Client:
        closed = False

        def list_swap_instruments(self):
            raise RuntimeError(f"upstream rejected {secret}")

        def close(self):
            self.closed = True

    client = _Client()

    monkeypatch.setattr(
        "telegram_kol_research.cli.build_deepcoin_client_from_env",
        lambda: client,
    )

    result = CliRunner().invoke(
        app,
        [
            "deepcoin-contract-specs",
            "refresh",
            "--cache-path",
            str(cache_path),
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "cache_preserved": True,
        "refresh_succeeded": False,
        "state": "refresh_failed",
    }
    assert secret not in result.stdout
    assert len(result.stdout) < 1024
    assert cache_path.read_bytes() == before
    assert load_deepcoin_contract_spec_snapshot(
        cache_path,
        now=datetime.now(timezone.utc),
    ) == previous
    assert client.closed is True


def test_contract_spec_refresh_rejects_non_positive_ttl_without_creating_cache(
    tmp_path,
):
    cache_path = tmp_path / "specs.json"

    result = CliRunner().invoke(
        app,
        [
            "deepcoin-contract-specs",
            "refresh",
            "--cache-path",
            str(cache_path),
            "--ttl-hours",
            "0",
        ],
    )

    assert result.exit_code != 0
    assert not cache_path.exists()
