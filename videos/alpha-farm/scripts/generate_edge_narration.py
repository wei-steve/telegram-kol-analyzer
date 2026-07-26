#!/usr/bin/env python3
"""Generate the scene-timed Mandarin narration with Edge neural TTS."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import edge_tts


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "content" / "narration-spoken.zh-CN.json"
OUTPUT_DIR = ROOT / "audio" / "generated" / "narration-v2-scenes"
FINAL_PATH = ROOT / "audio" / "generated" / "narration-v2.wav"


def run(*args: str) -> None:
    subprocess.run(args, check=True)


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


async def generate() -> None:
    spec = json.loads(SCRIPT_PATH.read_text(encoding="utf-8"))
    voice = spec["voice"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    wav_paths: list[Path] = []
    for scene in spec["scenes"]:
        mp3_path = OUTPUT_DIR / f"{scene['id']}.mp3"
        wav_path = OUTPUT_DIR / f"{scene['id']}.wav"
        communicate = edge_tts.Communicate(
            text=scene["text"],
            voice=voice,
            rate=scene["rate"],
        )
        await communicate.save(str(mp3_path))
        run(
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(mp3_path),
            "-ar",
            "48000",
            "-ac",
            "2",
            str(wav_path),
        )
        duration = probe_duration(wav_path)
        available = float(scene["duration"]) - 0.8
        if duration > available:
            raise RuntimeError(
                f"{scene['id']} is {duration:.2f}s but only {available:.2f}s is available"
            )
        print(f"{scene['id']}: {duration:.3f}s / {available:.3f}s")
        wav_paths.append(wav_path)

    command = ["ffmpeg", "-y", "-loglevel", "error"]
    for wav_path in wav_paths:
        command.extend(["-i", str(wav_path)])

    filters: list[str] = []
    mix_inputs: list[str] = []
    for index, scene in enumerate(spec["scenes"]):
        delay_ms = round((float(scene["start"]) + 0.4) * 1000)
        filters.append(f"[{index}:a]adelay={delay_ms}|{delay_ms}[a{index}]")
        mix_inputs.append(f"[a{index}]")
    filters.append(
        "".join(mix_inputs)
        + f"amix=inputs={len(mix_inputs)}:normalize=0,"
        + "apad=pad_dur=180,atrim=duration=180[out]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(FINAL_PATH),
        ]
    )
    run(*command)
    print(f"wrote {FINAL_PATH} ({probe_duration(FINAL_PATH):.3f}s)")


if __name__ == "__main__":
    try:
        asyncio.run(generate())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise
