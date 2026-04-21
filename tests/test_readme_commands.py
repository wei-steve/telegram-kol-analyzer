from pathlib import Path


def test_readme_mentions_sync_and_report_commands():
    text = Path("README.md").read_text()
    assert "python -m telegram_kol_research.cli sync" in text
    assert "python -m telegram_kol_research.cli report" in text
