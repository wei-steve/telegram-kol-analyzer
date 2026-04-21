from pathlib import Path


def test_readme_mentions_web_command():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "telegram-kol-research web" in readme
