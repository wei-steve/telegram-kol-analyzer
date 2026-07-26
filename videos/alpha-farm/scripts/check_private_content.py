from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from pathlib import Path


SUPPORTED_SUFFIXES = {".json", ".md", ".markdown", ".html", ".htm", ".txt"}

PHONE_PATTERNS = (
    re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d(?:[\s-]?\d){8}(?!\d)"),
    re.compile(
        r"(?<!\w)\+\d{1,3}[\s.-]?(?:\(\d{2,4}\)|\d{2,4})"
        r"(?:[\s.-]?\d{2,4}){2,4}(?!\d)"
    ),
)
TELEGRAM_INVITE_PATTERN = re.compile(
    r"https?://(?:t(?:elegram)?\.me)/(?:\+|joinchat/)[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
CREDENTIAL_PATTERN = re.compile(
    r"(?:"
    r"\bapi[\s_-]*(?:key|secret)\b"
    r"|\bsecret(?:[\s_-]*key)?\b"
    r"|\bpassphrase\b"
    r"|\bauthorization\b"
    r"|\bdc-access-(?:key|sign|passphrase)\b"
    r")\s*[:=]",
    re.IGNORECASE,
)
EMAIL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9._%+-])"
)
ORDER_ID_PATTERN = re.compile(
    r"(?:\border[\s_-]*(?:id|number)\b|\bordid\b|订单号)"
    r"\s*[:=#]?\s*([A-Za-z0-9_-]{16,})",
    re.IGNORECASE,
)


def find_sensitive_tokens(
    text: str,
    *,
    allowed_order_ids: Iterable[str] = (),
) -> list[str]:
    """Return stable category names for sensitive values found in *text*."""
    findings: list[str] = []

    if any(pattern.search(text) for pattern in PHONE_PATTERNS):
        findings.append("phone")
    if TELEGRAM_INVITE_PATTERN.search(text):
        findings.append("telegram_invite")
    if CREDENTIAL_PATTERN.search(text):
        findings.append("credential")
    if EMAIL_PATTERN.search(text):
        findings.append("email")

    allowed = set(allowed_order_ids)
    if any(match.group(1) not in allowed for match in ORDER_ID_PATTERN.finditer(text)):
        findings.append("order_id")

    return findings


def _supported_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in SUPPORTED_SUFFIXES else []
    return sorted(
        candidate
        for candidate in path.rglob("*")
        if candidate.is_file() and candidate.suffix.lower() in SUPPORTED_SUFFIXES
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan shareable Alpha Farm text assets for sensitive content."
    )
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--allow-order-id",
        action="append",
        default=[],
        help="Allow a specific display-only exchange order ID (repeatable).",
    )
    args = parser.parse_args()

    if not args.path.exists():
        parser.error(f"path does not exist: {args.path}")

    matches: list[tuple[Path, list[str]]] = []
    for file_path in _supported_files(args.path):
        text = file_path.read_text(encoding="utf-8", errors="replace")
        findings = find_sensitive_tokens(
            text,
            allowed_order_ids=args.allow_order_id,
        )
        if findings:
            matches.append((file_path, findings))

    if matches:
        for file_path, findings in matches:
            print(f"{file_path}: {', '.join(findings)}")
        return 1

    print("No sensitive tokens found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
