#!/usr/bin/env python3
"""Generate the provider preset catalogue from models.dev.

Design: ``docs/plans/2026-09-13-ai-provider-model-routing-design.md`` §9.1.

The output, ``src/telegram_kol_research/ai_provider_presets.json``, is
committed: the Web page reads it from disk, so adding a provider never depends
on reaching models.dev, and a change to the catalogue is a diff somebody
reviews rather than something that shifts under a running deployment. This
script is how that file stays reproducible.

    python scripts/build_ai_provider_presets.py                 # fetch live
    python scripts/build_ai_provider_presets.py --source snap.json
    python scripts/build_ai_provider_presets.py --check         # CI-style diff

models.dev shape: a top-level object keyed by provider id; each provider has
``name`` / ``api`` / ``doc`` and a ``models`` object keyed by model id, each
with ``name``, ``modalities.input`` / ``.output``, ``release_date`` and
``last_updated``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


MODELS_DEV_URL = "https://models.dev/api.json"
OUTPUT = Path("src/telegram_kol_research/ai_provider_presets.json")

#: At most this many models per provider, newest first. OpenRouter alone has
#: 360; a preset list is a starting point, and "拉取模型列表" is the real
#: answer for an aggregator.
MAX_MODELS_PER_PROVIDER = 8

#: Model ids that are not chat models, however their modalities read.
NON_CHAT_MODEL_ID = re.compile(
    r"realtime|tts|transcribe|whisper|audio|image-gen|imagine|video|embedding"
    r"|moderation|sora|dall-e",
    re.IGNORECASE,
)


#: Distinguishes "no upstream provider" from "the default one".
_SAME_AS_PRESET_ID = "<same>"


class Preset:
    """One entry of the catalogue, before its models are filled in."""

    def __init__(
        self,
        preset_id: str,
        group: str,
        label: str,
        base_url: str,
        *,
        source_id: str | None = _SAME_AS_PRESET_ID,
        requires_api_key: bool = True,
        note: str = "",
        append_v1: bool | None = None,
    ):
        self.preset_id = preset_id
        # None = derive from the base URL; a preset without a base URL (custom)
        # states its own default because there is nothing to derive from.
        self.append_v1 = append_v1
        self.group = group
        self.label = label
        self.base_url = base_url
        #: Which models.dev provider supplies the model list. ``None`` means
        #: "ship no preset models": a local runtime serves whatever the person
        #: downloaded, so any list we shipped would be a guess about their
        #: machine.
        self.source_id = (
            preset_id if source_id is _SAME_AS_PRESET_ID else source_id
        )
        self.requires_api_key = requires_api_key
        self.note = note


#: The whitelist, verbatim from the design's §9.1 table. ``base_url`` wins over
#: whatever models.dev says: several providers publish an Anthropic-shaped
#: address there, and this project speaks OpenAI-compatible only.
PRESETS: tuple[Preset, ...] = (
    # 国内
    Preset("deepseek", "domestic", "DeepSeek", "https://api.deepseek.com"),
    Preset(
        "zhipuai",
        "domestic",
        "智谱 GLM",
        "https://open.bigmodel.cn/api/paas/v4",
    ),
    Preset("xiaomi", "domestic", "小米 MiMo", "https://api.xiaomimimo.com/v1"),
    Preset(
        "alibaba-cn",
        "domestic",
        "阿里百炼（通义 Qwen）",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ),
    Preset(
        "moonshotai-cn",
        "domestic",
        "月之暗面 Kimi",
        "https://api.moonshot.cn/v1",
    ),
    Preset(
        "siliconflow-cn",
        "domestic",
        "硅基流动",
        "https://api.siliconflow.cn/v1",
        note="聚合平台，模型很多，建议用「拉取模型列表」",
    ),
    Preset("stepfun", "domestic", "阶跃星辰", "https://api.stepfun.com/v1"),
    Preset(
        "minimax-cn",
        "domestic",
        "MiniMax",
        # models.dev publishes the Anthropic-shaped address for this one.
        "https://api.minimaxi.com/v1",
        note="models.dev 给的是 Anthropic 格式地址，这里用 OpenAI 兼容地址",
    ),
    Preset(
        "volcengine",
        "domestic",
        "火山方舟（豆包）",
        "https://ark.cn-beijing.volces.com/api/v3",
        # The design said models.dev has no volcengine; the live data now
        # does. Keeping the preset models is the point of this phase -- the
        # complaint was "too few" -- and Ark accepts these model ids directly.
        note="也可以直接填你自己创建的接入点 id（ep-...）",
    ),
    # 国际
    Preset("openai", "international", "OpenAI", "https://api.openai.com/v1"),
    Preset(
        "anthropic",
        "international",
        "Anthropic",
        "https://api.anthropic.com/v1",
        note="走 Anthropic 的 OpenAI 兼容层",
    ),
    Preset(
        "google",
        "international",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        note="Gemini 的 OpenAI 兼容层",
    ),
    Preset("xai", "international", "xAI Grok", "https://api.x.ai/v1"),
    Preset(
        "openrouter",
        "international",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        note="聚合平台，模型很多，建议用「拉取模型列表」",
    ),
    Preset("groq", "international", "Groq", "https://api.groq.com/openai/v1"),
    Preset("mistral", "international", "Mistral", "https://api.mistral.ai/v1"),
    # 本地
    Preset(
        "ollama",
        "local",
        "Ollama（本机）",
        "http://127.0.0.1:11434/v1",
        source_id=None,
        requires_api_key=False,
        note="本机服务，不需要 Key；模型用「拉取模型列表」",
    ),
    Preset(
        "lmstudio",
        "local",
        "LM Studio（本机）",
        "http://127.0.0.1:1234/v1",
        source_id=None,
        requires_api_key=False,
        note="本机服务，不需要 Key；模型用「拉取模型列表」",
    ),
    # 自定义
    Preset(
        "custom",
        "custom",
        "自定义（OpenAI 兼容）",
        "",
        source_id=None,
        note="任何 OpenAI 兼容端点；默认追加 /v1，填主机地址即可，代理已含 /v1 时关掉开关",
        # Like OpenMinis: a hand-typed address is nearly always a bare host,
        # so the switch starts on. The live preview shows the result either way.
        append_v1=True,
    ),
)

GROUP_LABELS = {
    "domestic": "国内",
    "international": "国际",
    "local": "本地",
    "custom": "自定义",
}


def _append_v1(base_url: str) -> bool:
    """Does this base URL still need a ``/v1``.

    Same answer as ``ai_endpoints.infer_append_v1``, repeated here rather than
    imported because this script runs from the repository root without the
    package installed. A test pins the two against each other.
    """

    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        return False
    return urlsplit(normalized).path in ("", "/")


def _is_chat_model(model_id: str, record: dict[str, Any]) -> bool:
    if NON_CHAT_MODEL_ID.search(model_id):
        return False
    modalities = record.get("modalities")
    output = modalities.get("output") if isinstance(modalities, dict) else None
    if not isinstance(output, list):
        return False
    return "text" in output


def _accepts_images(record: dict[str, Any]) -> bool:
    modalities = record.get("modalities")
    inputs = modalities.get("input") if isinstance(modalities, dict) else None
    return isinstance(inputs, list) and "image" in inputs


def _recency(record: dict[str, Any]) -> str:
    """Sort key: the newest of the two dates models.dev publishes, or empty."""

    dates = [
        str(record.get(key) or "")
        for key in ("release_date", "last_updated")
    ]
    return max(dates)


def select_models(provider: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    """The newest chat models this provider serves, newest first."""

    records = provider.get("models")
    if not isinstance(records, dict):
        return []
    chat = [
        (model_id, record)
        for model_id, record in records.items()
        if isinstance(record, dict) and _is_chat_model(model_id, record)
    ]
    # Ties broken by id so the generated file is stable between runs.
    chat.sort(key=lambda item: (_recency(item[1]), item[0]), reverse=True)
    return [
        {
            "id": model_id,
            "label": str(record.get("name") or model_id),
            "supports_text": True,
            "supports_image": _accepts_images(record),
            "release_date": str(record.get("release_date") or ""),
        }
        for model_id, record in chat[:limit]
    ]


def build_catalogue(
    source: dict[str, Any],
    *,
    source_name: str,
    limit: int = MAX_MODELS_PER_PROVIDER,
) -> dict[str, Any]:
    providers = []
    for preset in PRESETS:
        upstream = (
            source.get(preset.source_id) if preset.source_id else None
        ) or {}
        providers.append(
            {
                "id": preset.preset_id,
                "group": preset.group,
                "group_label": GROUP_LABELS[preset.group],
                "label": preset.label,
                "english_label": str(upstream.get("name") or ""),
                "base_url": preset.base_url,
                # Explicit rather than inferred at read time: the page shows it
                # as a switch, so the catalogue has to state it.
                "append_v1": (
                    preset.append_v1
                    if preset.append_v1 is not None
                    else _append_v1(preset.base_url)
                ),
                "doc_url": str(upstream.get("doc") or ""),
                "requires_api_key": preset.requires_api_key,
                "note": preset.note,
                "models": select_models(upstream, limit=limit) if upstream else [],
            }
        )
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "source": source_name,
        "max_models_per_provider": limit,
        "group_order": ["domestic", "international", "local", "custom"],
        "group_labels": GROUP_LABELS,
        "providers": providers,
    }


def load_source(path: str | None) -> tuple[dict[str, Any], str]:
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8")), (
            f"models.dev snapshot: {path}"
        )
    import httpx  # imported here so --source works without network deps

    response = httpx.get(MODELS_DEV_URL, timeout=30.0)
    response.raise_for_status()
    return response.json(), MODELS_DEV_URL


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        help="Local models.dev api.json snapshot; omit to fetch the live one.",
    )
    parser.add_argument(
        "--output", default=str(OUTPUT), help=f"Where to write (default {OUTPUT})."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_MODELS_PER_PROVIDER,
        help="Models kept per provider, newest first.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 if the file would change (ignoring the date).",
    )
    args = parser.parse_args(argv)

    source, source_name = load_source(args.source)
    catalogue = build_catalogue(source, source_name=source_name, limit=args.limit)
    rendered = json.dumps(catalogue, ensure_ascii=False, indent=2, sort_keys=False)
    output = Path(args.output)

    if args.check:
        if not output.exists():
            print(f"{output} does not exist", file=sys.stderr)
            return 1
        current = json.loads(output.read_text(encoding="utf-8"))
        fresh = json.loads(rendered)
        for key in ("generated_at", "source"):
            current.pop(key, None)
            fresh.pop(key, None)
        if current != fresh:
            print(f"{output} is out of date", file=sys.stderr)
            return 1
        print(f"{output} is up to date")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered + "\n", encoding="utf-8")
    models = sum(len(item["models"]) for item in catalogue["providers"])
    print(
        f"wrote {output}: {len(catalogue['providers'])} providers, {models} models, "
        f"from {source_name}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
