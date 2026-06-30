import pytest

from telegram_kol_research.kol_codes import load_kol_code_map
from telegram_kol_research.kol_codes import normalize_kol_code
from telegram_kol_research.kol_codes import resolve_kol_code


def test_project_kol_codes_are_unique_and_alphanumeric():
    codes = load_kol_code_map()

    assert codes[-1002409877375] == "FG"
    assert codes[-1002199068560] == "SMG"
    assert len(set(codes.values())) == len(codes)
    for code in codes.values():
        assert code.isalnum()
        assert code.isupper()
        assert len(code) <= 8


def test_resolve_kol_code_prefers_explicit_value():
    assert resolve_kol_code(chat_id=-1002409877375, explicit_code="fg2") == "FG2"


def test_normalize_kol_code_rejects_empty_value():
    with pytest.raises(ValueError):
        normalize_kol_code("???")
