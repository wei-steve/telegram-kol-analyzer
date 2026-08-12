#!/usr/bin/env python3
"""Send bounded text-and-image smoke tests to a local OpenAI-compatible VLM."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any, Sequence

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:18080/v1"
DEFAULT_MODEL = "mlx-community/Qwen3-VL-4B-Instruct-4bit"
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_TOKENS = 512
DEFAULT_TIMEOUT_SECONDS = 60.0
SYSTEM_PROMPT = (
    "You are a strict image reader. Use only directly visible evidence, "
    "do not invent unreadable details, and follow the requested output format."
)
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_ERROR_TEXT_CHARS = 2_000
DATA_URL_PATTERN = re.compile(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+")


def image_to_data_url(path: Path, *, max_image_bytes: int) -> str:
    """Return a bounded image as a base64 data URL without logging its contents."""

    size = path.stat().st_size
    if size > max_image_bytes:
        raise ValueError(f"image {path} exceeds {max_image_bytes} bytes")
    media_type = mimetypes.guess_type(path.name)[0]
    if media_type not in SUPPORTED_IMAGE_TYPES:
        raise ValueError(f"unsupported image type for {path}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def build_payload(
    *,
    model: str,
    prompt: str,
    image_paths: Sequence[Path],
    max_tokens: int,
    max_image_bytes: int,
    max_total_image_bytes: int = DEFAULT_MAX_TOTAL_IMAGE_BYTES,
) -> dict[str, Any]:
    """Build the same OpenAI content-block shape used by message recognition."""

    total_image_bytes = sum(path.stat().st_size for path in image_paths)
    if total_image_bytes > max_total_image_bytes:
        raise ValueError(
            f"combined images exceed {max_total_image_bytes} bytes"
        )
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    content.extend(
        {
            "type": "image_url",
            "image_url": {
                "url": image_to_data_url(path, max_image_bytes=max_image_bytes)
            },
        }
        for path in image_paths
    )
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
    }


def send_request(
    *,
    client: httpx.Client,
    base_url: str,
    model: str,
    prompt: str,
    image_paths: Sequence[Path],
    max_tokens: int,
    max_image_bytes: int,
    max_total_image_bytes: int = DEFAULT_MAX_TOTAL_IMAGE_BYTES,
) -> dict[str, Any]:
    """Send one smoke request and return a redacted timing/result summary."""

    payload = build_payload(
        model=model,
        prompt=prompt,
        image_paths=image_paths,
        max_tokens=max_tokens,
        max_image_bytes=max_image_bytes,
        max_total_image_bytes=max_total_image_bytes,
    )
    started_at = time.monotonic()
    response = client.post(_chat_completions_url(base_url), json=payload)
    elapsed_seconds = time.monotonic() - started_at
    if not response.is_success:
        error_text = DATA_URL_PATTERN.sub("[redacted image data]", response.text)
        return {
            "status_code": response.status_code,
            "ok": False,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "image_count": len(image_paths),
            "error": error_text[:MAX_ERROR_TEXT_CHARS],
        }
    try:
        body = response.json()
    except json.JSONDecodeError:
        error_text = DATA_URL_PATTERN.sub("[redacted image data]", response.text)
        return {
            "status_code": response.status_code,
            "ok": False,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "image_count": len(image_paths),
            "error": f"non-JSON response: {error_text[:MAX_ERROR_TEXT_CHARS]}",
        }
    choices = body.get("choices") or []
    message = choices[0].get("message") if choices else {}
    content = (message or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        return {
            "status_code": response.status_code,
            "ok": False,
            "elapsed_seconds": round(elapsed_seconds, 3),
            "image_count": len(image_paths),
            "error": "malformed response: missing assistant content",
        }
    return {
        "status_code": response.status_code,
        "ok": True,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "image_count": len(image_paths),
        "model": body.get("model"),
        "content": content,
        "usage": body.get("usage"),
    }


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--image", action="append", type=Path, required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--max-image-bytes", type=int, default=DEFAULT_MAX_IMAGE_BYTES
    )
    parser.add_argument(
        "--max-total-image-bytes",
        type=int,
        default=DEFAULT_MAX_TOTAL_IMAGE_BYTES,
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    with httpx.Client(timeout=args.timeout) as client:
        result = send_request(
            client=client,
            base_url=args.base_url,
            model=args.model,
            prompt=args.prompt,
            image_paths=args.image,
            max_tokens=args.max_tokens,
            max_image_bytes=args.max_image_bytes,
            max_total_image_bytes=args.max_total_image_bytes,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
