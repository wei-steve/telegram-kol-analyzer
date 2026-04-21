"""Helpers for packaging model-adjudication inputs and contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import sessionmaker

from telegram_kol_research.dataset_export import export_dataset_jsonl


def export_llm_adjudication_pack(
    session_factory: sessionmaker,
    output_dir: str | Path,
    *,
    review_only: bool = True,
    confidence_threshold: float = 0.8,
    signal_like_only: bool = True,
) -> dict[str, str | int]:
    """Write dataset, prompt, schema, and manifest for LLM adjudication."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    dataset_path = output_dir / "dataset.jsonl"
    prompt_path = output_dir / "prompt.md"
    schema_path = output_dir / "schema.json"
    manifest_path = output_dir / "manifest.json"
    response_template_path = output_dir / "response-template.json"

    export_dataset_jsonl(
        session_factory,
        dataset_path,
        review_only=review_only,
        confidence_threshold=confidence_threshold,
        signal_like_only=signal_like_only,
    )

    record_count = _count_jsonl_rows(dataset_path)
    response_template_path.write_text(
        json.dumps(build_llm_response_template(dataset_path), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    prompt_path.write_text(build_llm_adjudication_prompt(record_count), encoding="utf-8")
    schema_path.write_text(
        json.dumps(build_llm_response_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "record_count": record_count,
        "dataset_path": str(dataset_path.resolve()),
        "prompt_path": str(prompt_path.resolve()),
        "schema_path": str(schema_path.resolve()),
        "response_template_path": str(response_template_path.resolve()),
        "review_only": review_only,
        "confidence_threshold": confidence_threshold,
        "signal_like_only": signal_like_only,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def build_llm_adjudication_prompt(record_count: int) -> str:
    """Return a reusable prompt for batch Telegram message adjudication."""

    return f"""# Telegram Signal Adjudication

You are adjudicating Telegram trading messages from archived strategy groups.

Your job is to read each JSONL record and decide whether it contains a real trading signal, a signal update, a trade close, or only commentary/noise.

The input records include:
- `raw_message_id`, `chat_id`, `message_id`
- `sender_name`
- `text`
- `reply_context`
- `media_assets` with optional `ocr_text`
- existing rule-based `candidate`, `source`, and `trade_idea`

Process exactly {record_count} input record(s), one decision per record.

## Output rules

- Return valid JSON only.
- Follow the provided JSON schema exactly.
- Keep `raw_message_id` unchanged.
- If the message is ambiguous, set `classification` to `needs_review`.
- Use `signal_kind` to distinguish `entry_signal`, `update_signal`, `close_signal`, `market_commentary`, or `unknown`.
- If no reliable trade fields are present, set `normalized_signal` to `null`.
- Keep `reasoning_short` brief and factual.

## Classification guidance

- `entry_signal`: a fresh trade setup with direction, asset, and actionable entry context.
- `update_signal`: a modification to an existing idea, such as add, reduce, move stop, partial take-profit, or hold guidance.
- `close_signal`: explicit exit, full take-profit, stop-out, or cancel/close instruction.
- `not_signal`: commentary, market analysis, promotion, or chat noise without an actionable instruction.
- `needs_review`: likely signal-like but too ambiguous to trust automatically.

## Expected response shape

- Top-level object with `items`.
- One item per input record.
- Each item must include `raw_message_id`, `classification`, `signal_kind`, `confidence`, `needs_review`, `reasoning_short`, and `normalized_signal`.
"""


def build_llm_response_schema() -> dict:
    """Return the strict JSON schema expected from the model."""

    normalized_signal = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "properties": {
            "symbol": {"type": ["string", "null"]},
            "side": {"type": ["string", "null"], "enum": ["long", "short", None]},
            "entry_text": {"type": ["string", "null"]},
            "stop_loss_text": {"type": ["string", "null"]},
            "take_profit_text": {"type": ["string", "null"]},
            "leverage_text": {"type": ["string", "null"]},
            "time_horizon": {"type": ["string", "null"]},
        },
        "required": [
            "symbol",
            "side",
            "entry_text",
            "stop_loss_text",
            "take_profit_text",
            "leverage_text",
            "time_horizon",
        ],
    }

    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "raw_message_id": {"type": "integer"},
                        "classification": {
                            "type": "string",
                            "enum": [
                                "entry_signal",
                                "update_signal",
                                "close_signal",
                                "not_signal",
                                "needs_review",
                            ],
                        },
                        "signal_kind": {
                            "type": "string",
                            "enum": [
                                "entry_signal",
                                "update_signal",
                                "close_signal",
                                "market_commentary",
                                "unknown",
                            ],
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "needs_review": {"type": "boolean"},
                        "reasoning_short": {"type": "string"},
                        "normalized_signal": normalized_signal,
                    },
                    "required": [
                        "raw_message_id",
                        "classification",
                        "signal_kind",
                        "confidence",
                        "needs_review",
                        "reasoning_short",
                        "normalized_signal",
                    ],
                },
            }
        },
        "required": ["items"],
    }


def build_llm_response_template(dataset_path: str | Path) -> dict:
    """Return a starter response file aligned to dataset raw_message_ids."""

    items: list[dict[str, Any]] = []
    path = Path(dataset_path)
    text = path.read_text(encoding="utf-8")
    if text.strip():
        for line in text.strip().splitlines():
            record = json.loads(line)
            items.append(
                {
                    "raw_message_id": record["raw_message_id"],
                    "classification": "needs_review",
                    "signal_kind": "unknown",
                    "confidence": 0.5,
                    "needs_review": True,
                    "reasoning_short": "",
                    "normalized_signal": {
                        "symbol": None,
                        "side": None,
                        "entry_text": None,
                        "stop_loss_text": None,
                        "take_profit_text": None,
                        "leverage_text": None,
                        "time_horizon": None,
                    },
                }
            )
    return {"items": items}


def export_llm_submission_sample(
    pack_dir: str | Path,
    output_path: str | Path,
    *,
    limit: int = 5,
) -> Path:
    """Write a copy-ready markdown sample from an adjudication pack."""

    pack_dir = Path(pack_dir)
    dataset_path = pack_dir / "dataset.jsonl"
    schema_path = pack_dir / "schema.json"
    response_template_path = pack_dir / "response-template.json"

    dataset_records = _read_jsonl_records(dataset_path)[:limit]
    sample_jsonl = "\n".join(
        json.dumps(record, ensure_ascii=False) for record in dataset_records
    )
    sample_template = {
        "items": [
            item
            for item in build_llm_response_template_from_records(dataset_records)["items"]
        ]
    }
    schema_payload = json.loads(schema_path.read_text(encoding="utf-8"))

    content = f"""# Copy-Ready LLM Submission

Use this as a small first pass before sending the full dataset.

## Task

{build_llm_adjudication_prompt(len(dataset_records))}

## JSON Schema

```json
{json.dumps(schema_payload, ensure_ascii=False, indent=2)}
```

## Input Dataset JSONL

```jsonl
{sample_jsonl}
```

## Response Template

Fill this shape and save the model output as JSON. The full-pack template is also available at `{response_template_path.name}`.

```json
{json.dumps(sample_template, ensure_ascii=False, indent=2)}
```
"""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def build_llm_response_template_from_records(records: list[dict[str, Any]]) -> dict:
    """Return starter response items for already-loaded dataset records."""

    return {
        "items": [
            {
                "raw_message_id": record["raw_message_id"],
                "classification": "needs_review",
                "signal_kind": "unknown",
                "confidence": 0.5,
                "needs_review": True,
                "reasoning_short": "",
                "normalized_signal": {
                    "symbol": None,
                    "side": None,
                    "entry_text": None,
                    "stop_loss_text": None,
                    "take_profit_text": None,
                    "leverage_text": None,
                    "time_horizon": None,
                },
            }
            for record in records
        ]
    }


def _count_jsonl_rows(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return 0
    return len(text.strip().splitlines())


def _read_jsonl_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    return [json.loads(line) for line in text.strip().splitlines()]
