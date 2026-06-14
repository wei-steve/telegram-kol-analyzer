from typer.testing import CliRunner

from telegram_kol_research.cli import app
from telegram_kol_research.recovery_runner import RecoveryDryRunProviderMissingError


def test_recovery_dry_run_cli_reports_provider_not_configured(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")

    def fake_run_recovery_dry_run(*args, **kwargs):
        raise RecoveryDryRunProviderMissingError("market data provider is not configured")

    monkeypatch.setattr(
        "telegram_kol_research.cli.run_recovery_dry_run",
        fake_run_recovery_dry_run,
    )

    result = CliRunner().invoke(
        app,
        [
            "recovery-dry-run",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code == 1
    assert "Recovery dry-run unavailable" in result.stdout
    assert "market data provider is not configured" in result.stdout


def test_recovery_dry_run_cli_prints_summary(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")

    class FakeResult:
        total_candidates = 2
        action_counts = {
            "manual_review": 1,
            "eligible_for_recovery_limit_order": 1,
        }
        evaluations = []

    monkeypatch.setattr(
        "telegram_kol_research.cli.run_recovery_dry_run",
        lambda *args, **kwargs: FakeResult(),
    )

    result = CliRunner().invoke(
        app,
        [
            "recovery-dry-run",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
        ],
    )

    assert result.exit_code == 0
    assert "Recovery dry-run candidates: 2" in result.stdout
    assert "manual_review: 1" in result.stdout
    assert "eligible_for_recovery_limit_order: 1" in result.stdout


def test_recovery_dry_run_cli_passes_persist_flag(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")
    captured = {}

    class FakeResult:
        total_candidates = 0
        action_counts = {}
        evaluations = []

    def fake_run_recovery_dry_run(*args, **kwargs):
        captured["persist"] = kwargs["persist"]
        return FakeResult()

    monkeypatch.setattr(
        "telegram_kol_research.cli.run_recovery_dry_run",
        fake_run_recovery_dry_run,
    )

    result = CliRunner().invoke(
        app,
        [
            "recovery-dry-run",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
            "--persist",
        ],
    )

    assert result.exit_code == 0
    assert captured["persist"] is True


def test_recovery_dry_run_cli_can_enable_binance_market_provider(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")
    captured = {}

    class FakeBinanceMarketDataProvider:
        def __init__(self):
            captured["provider_created"] = True
            self.closed = False

        def close(self):
            self.closed = True

    class FakeResult:
        total_candidates = 0
        action_counts = {}
        evaluations = []

    def fake_run_recovery_dry_run(*args, **kwargs):
        captured["market_data"] = kwargs["market_data"]
        return FakeResult()

    monkeypatch.setattr(
        "telegram_kol_research.cli.BinanceMarketDataProvider",
        FakeBinanceMarketDataProvider,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.run_recovery_dry_run",
        fake_run_recovery_dry_run,
    )

    result = CliRunner().invoke(
        app,
        [
            "recovery-dry-run",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
            "--market-provider",
            "binance",
        ],
    )

    assert result.exit_code == 0
    assert captured["provider_created"] is True
    assert isinstance(captured["market_data"], FakeBinanceMarketDataProvider)
    assert captured["market_data"].closed is True


def test_recovery_dry_run_cli_can_enable_gate_market_provider(monkeypatch, tmp_path):
    config_path = tmp_path / "groups.yaml"
    database_path = tmp_path / "research.db"
    config_path.write_text("groups: []", encoding="utf-8")
    captured = {}

    class FakeGateMarketDataProvider:
        def __init__(self):
            captured["provider_created"] = True

        def close(self):
            captured["provider_closed"] = True

    class FakeResult:
        total_candidates = 0
        action_counts = {}
        evaluations = []

    def fake_run_recovery_dry_run(*args, **kwargs):
        captured["market_data"] = kwargs["market_data"]
        return FakeResult()

    monkeypatch.setattr(
        "telegram_kol_research.cli.GateMarketDataProvider",
        FakeGateMarketDataProvider,
    )
    monkeypatch.setattr(
        "telegram_kol_research.cli.run_recovery_dry_run",
        fake_run_recovery_dry_run,
    )

    result = CliRunner().invoke(
        app,
        [
            "recovery-dry-run",
            "--config-path",
            str(config_path),
            "--database-path",
            str(database_path),
            "--market-provider",
            "gate",
        ],
    )

    assert result.exit_code == 0
    assert captured["provider_created"] is True
    assert isinstance(captured["market_data"], FakeGateMarketDataProvider)
    assert captured["provider_closed"] is True
