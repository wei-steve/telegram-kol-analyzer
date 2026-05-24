import pytesseract

from telegram_kol_research.parsing.ocr_parser import extract_text_from_image
from telegram_kol_research.parsing.ocr_parser import merge_caption_and_ocr_text


def test_merge_caption_and_ocr_text_keeps_both_sources():
    merged = merge_caption_and_ocr_text(
        caption="BTC long setup",
        ocr_text="Entry 68000-68200 TP 69000 SL 67500",
    )
    assert "BTC long setup" in merged
    assert "Entry 68000-68200" in merged


def test_extract_text_from_image_wraps_missing_tesseract_binary(monkeypatch, tmp_path):
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"fake-image")

    class FakeImage:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr("PIL.Image.open", lambda path: FakeImage())

    def raise_missing_binary(image):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr("pytesseract.image_to_string", raise_missing_binary)

    try:
        extract_text_from_image(image_path)
    except RuntimeError as exc:
        assert "OCR failed" in str(exc)
    else:
        raise AssertionError("missing tesseract binary should be wrapped as RuntimeError")
