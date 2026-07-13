#!/usr/bin/env python3
"""Create a small synthetic noisy record side for local demonstrations."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np


def create_audio(output_dir: Path) -> tuple[Path, Path]:
    rate = 44_100
    rng = np.random.default_rng(17)
    plan = [
        ("gap", 1.5, 0.0),
        ("tone", 5.0, 349.23),
        ("gap", 1.3, 0.0),
        ("tone", 4.4, 523.25),
        ("gap", 1.6, 0.0),
        ("tone", 5.2, 659.25),
        ("gap", 1.2, 0.0),
    ]
    chunks: list[np.ndarray] = []
    for kind, duration, frequency in plan:
        count = round(duration * rate)
        noise = rng.uniform(-0.0028, 0.0028, count)
        # Add a little low-frequency rumble to imitate an imperfect quiet groove.
        times = np.arange(count, dtype=np.float64) / rate
        rumble = 0.0014 * np.sin(2.0 * math.pi * 31.0 * times)
        signal = noise + rumble
        if kind == "tone":
            attack = np.minimum(1.0, np.arange(count) / max(1, rate * 0.08))
            release = np.minimum(1.0, np.arange(count)[::-1] / max(1, rate * 0.12))
            envelope = np.minimum(attack, release)
            signal = signal + 0.24 * np.sin(2.0 * math.pi * frequency * times) * envelope
        chunks.append(signal)

    mono = np.concatenate(chunks)
    stereo = np.column_stack((mono, mono))
    pcm = np.clip(stereo * 32767.0, -32768, 32767).astype("<i2")

    wav_path = output_dir / "Groove Serpent Demo - Side A.wav"
    with wave.open(str(wav_path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm.tobytes())

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("FFmpeg was not found on PATH.")
    flac_path = output_dir / "Groove Serpent Demo - Side A.flac"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(wav_path),
            "-c:a",
            "flac",
            str(flac_path),
        ],
        check=True,
    )
    wav_path.unlink()

    tracklist_path = output_dir / "demo-tracklist.json"
    tracklist_path.write_text(
        json.dumps(
            {
                "artist": "The Test Pressings",
                "album": "Signals from the Workbench",
                "side": "A",
                "tracks": [
                    {"title": "Needle Down", "duration": "5:00"},
                    {"title": "Quiet Groove", "duration": "4:24"},
                    {"title": "Runout Signal", "duration": "5:12"},
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return flac_path, tracklist_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="demo")
    args = parser.parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    flac_path, tracklist_path = create_audio(output_dir)
    project_path = output_dir / "demo.groove.json"
    print(f"Created: {flac_path}")
    print(f"Created: {tracklist_path}")
    print("\nAnalyze it with:\n")
    print(
        f'groove-serpent analyze "{flac_path}" --tracklist "{tracklist_path}" '
        f'--project "{project_path}" --min-track 2'
    )
    print("\nThen review it with:\n")
    print(f'groove-serpent review "{project_path}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
