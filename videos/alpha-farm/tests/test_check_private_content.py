from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from check_private_content import find_sensitive_tokens


def test_rejects_phone_number():
    assert find_sensitive_tokens("联系 +86 13800138000") == ["phone"]


def test_rejects_international_phone_number():
    assert find_sensitive_tokens("Call +1 (415) 555-2671") == ["phone"]


def test_rejects_telegram_invite_link():
    assert find_sensitive_tokens("https://t.me/+secretinvite") == ["telegram_invite"]


def test_rejects_legacy_telegram_invite_link():
    assert (
        find_sensitive_tokens("https://telegram.me/joinchat/SecretInvite")
        == ["telegram_invite"]
    )


def test_rejects_private_credential_labels():
    assert find_sensitive_tokens("DC-ACCESS-KEY: abc123") == ["credential"]
    assert find_sensitive_tokens("api_secret = abc123") == ["credential"]
    assert find_sensitive_tokens("Authorization: Bearer abc123") == ["credential"]


def test_rejects_email_address():
    assert find_sensitive_tokens("operator@example.com") == ["email"]


def test_rejects_long_exchange_order_id():
    assert find_sensitive_tokens("order_id: 987654321012345678") == ["order_id"]


def test_accepts_allowlisted_display_order_id():
    order_id = "987654321012345678"
    assert (
        find_sensitive_tokens(
            f"order_id: {order_id}",
            allowed_order_ids={order_id},
        )
        == []
    )


def test_accepts_strategy_prices():
    assert find_sensitive_tokens("BTC 62000-62400 SL 61500") == []


def test_cli_scans_supported_files_and_exits_nonzero(tmp_path):
    (tmp_path / "safe.json").write_text('{"strategy": "BTC 62000 SL 61500"}')
    (tmp_path / "unsafe.md").write_text("联系 +86 13800138000")
    (tmp_path / "ignored.csv").write_text("operator@example.com")

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "check_private_content.py"), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "unsafe.md: phone" in result.stdout
    assert "ignored.csv" not in result.stdout


def test_cli_reports_clean_supported_file(tmp_path):
    safe_file = tmp_path / "strategy.txt"
    safe_file.write_text("BTC 62000-62400 SL 61500")

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "check_private_content.py"), str(safe_file)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "No sensitive tokens found"
