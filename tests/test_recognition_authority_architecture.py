from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "telegram_kol_research"
PRODUCTION_AUTHORITY_MODULES = (
    "authoritative_recognition.py",
    "context_resolution.py",
    "context_resolution_worker.py",
)
FORBIDDEN_AUTHORITY_IMPORTS = {
    "parse_signal_text",
    "persist_text_signal_candidates",
    "recognize_message_now",
    "recognize_records_with_ai_config",
    "run_mimo_direct_for_message",
    "BITCOIN_JUNZHANG_PROFILE",
}
EXPECTED_LEGACY_IMPORTERS = {
    "recognize_message_now": {
        "web_app.py",
    },
    "recognize_records_with_ai_config": set(),
    "run_mimo_direct_for_message": set(),
    "persist_text_signal_candidates": set(),
}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def _application_importers(symbol: str) -> set[str]:
    return {
        path.name
        for path in SOURCE.glob("*.py")
        if symbol in _imported_names(path)
    }


@pytest.mark.architecture
def test_authoritative_modules_do_not_import_legacy_recognizers():
    violations = {
        filename: sorted(
            _imported_names(SOURCE / filename) & FORBIDDEN_AUTHORITY_IMPORTS
        )
        for filename in PRODUCTION_AUTHORITY_MODULES
    }

    assert all(not values for values in violations.values()), violations


@pytest.mark.architecture
def test_authoritative_projection_has_one_temporary_legacy_dependency():
    source = (SOURCE / "authoritative_recognition.py").read_text(
        encoding="utf-8"
    )

    assert source.count("apply_authoritative_mimo_payload") == 2


@pytest.mark.architecture
def test_legacy_recognition_importers_match_the_reviewed_inventory():
    actual = {
        symbol: _application_importers(symbol)
        for symbol in EXPECTED_LEGACY_IMPORTERS
    }

    assert actual == EXPECTED_LEGACY_IMPORTERS
