from telegram_kol_research.parsing.ocr_parser import merge_caption_and_ocr_text


def test_merge_caption_and_ocr_text_keeps_both_sources():
    merged = merge_caption_and_ocr_text(
        caption="BTC long setup",
        ocr_text="Entry 68000-68200 TP 69000 SL 67500",
    )
    assert "BTC long setup" in merged
    assert "Entry 68000-68200" in merged
