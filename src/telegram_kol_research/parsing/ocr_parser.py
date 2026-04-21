"""OCR-assisted parsing helpers for image-based Telegram posts."""

from __future__ import annotations

from pathlib import Path


def merge_caption_and_ocr_text(caption: str | None, ocr_text: str | None) -> str:
    """Merge caption text and OCR text into a single parseable string."""

    parts = [part.strip() for part in (caption, ocr_text) if part and part.strip()]
    return "\n".join(parts)


def image_signal_confidence(base_confidence: float, *, image_only: bool) -> float:
    """Lower confidence by default for image-only signals."""

    if image_only:
        return max(0.0, round(base_confidence - 0.2, 2))
    return round(base_confidence, 2)


def extract_text_from_image(image_path: str | Path) -> str:
    """Run OCR on a local image path using Pillow and pytesseract."""

    try:
        from PIL import Image
        import pytesseract
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "OCR dependencies are not installed in the current environment. Install Pillow and pytesseract first."
        ) from exc

    with Image.open(Path(image_path)) as image:
        return pytesseract.image_to_string(image).strip()
