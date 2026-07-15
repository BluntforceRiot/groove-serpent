from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from groove_serpent.media import sha256_file
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    BoundaryCandidate,
    Project,
    Track,
)
from groove_serpent.project_io import save_project
from groove_serpent.review_server import ReviewServer
from fixture_lifecycle import FixtureLifecycle, install_fixture_lifecycle


def _make_source(directory: Path, lifecycle: FixtureLifecycle) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("The side browser fixture requires FFmpeg on PATH.")
    source = directory / "synthetic-side.flac"
    completed = lifecycle.run_startup_command(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-ac",
            "2",
            "-sample_fmt",
            "s16",
            str(source),
        ],
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "FFmpeg could not create the side browser fixture: "
            + completed.stderr.decode("utf-8", errors="replace")
        )
    return source


def _build_project(directory: Path, lifecycle: FixtureLifecycle) -> Path:
    source = _make_source(directory, lifecycle)
    details = source.stat()
    sample_count = 6 * 48_000
    audio = AudioSource(
        path=source.name,
        filename=source.name,
        size_bytes=details.st_size,
        modified_ns=details.st_mtime_ns,
        duration_seconds=6.0,
        sample_rate=48_000,
        channels=2,
        codec_name="flac",
        bits_per_raw_sample=16,
        sample_format="s16",
        sample_count=sample_count,
        sha256=sha256_file(source),
    )
    cut_sample = sample_count // 2
    cut_seconds = cut_sample / audio.sample_rate
    project = Project(
        source=audio,
        settings=AnalysisSettings(min_track_seconds=0.1),
        analysis=AnalysisSummary(
            music_start_seconds=0.1,
            music_end_seconds=audio.duration_seconds - 0.1,
            noise_floor_db=-60.0,
            silence_threshold_db=-54.0,
            active_threshold_db=-42.0,
            envelope_window_seconds=0.05,
            candidates=[
                BoundaryCandidate(
                    start_seconds=cut_seconds - 0.05,
                    end_seconds=cut_seconds + 0.05,
                    cut_seconds=cut_seconds,
                    cut_sample=cut_sample,
                    duration_seconds=0.1,
                    minimum_db=-58.0,
                    mean_db=-52.0,
                    contrast_db=12.0,
                    score=0.94,
                    selected=True,
                )
            ],
            waveform=[0.04, 0.15, 0.4, 0.75, 0.35, 0.1, 0.45, 0.8, 0.3, 0.05],
        ),
        tracks=[
            Track(
                number=1,
                title="Synthetic first track",
                start_sample=0,
                end_sample=cut_sample,
                start_seconds=0.0,
                end_seconds=cut_seconds,
                artist="Fixture Artist",
                album="Fixture Album",
            ),
            Track(
                number=2,
                title="Synthetic second track",
                start_sample=cut_sample,
                end_sample=sample_count,
                start_seconds=cut_seconds,
                end_seconds=sample_count / audio.sample_rate,
                artist="Fixture Artist",
                album="Fixture Album",
            ),
        ],
        metadata={
            "artist": "Fixture Artist",
            "album_artist": "Fixture Artist",
            "album": "Fixture Album",
            "side": "A",
        },
    )
    project_path = directory / "synthetic-side.groove.json"
    save_project(project, project_path)
    return project_path


def main() -> None:
    lifecycle = install_fixture_lifecycle()
    try:
        with tempfile.TemporaryDirectory(prefix="groove-serpent-side-browser-") as temporary:
            directory = Path(temporary)
            project_path = _build_project(directory, lifecycle)
            lifecycle.raise_if_stopping()
            server = ReviewServer(("127.0.0.1", 0), project_path)
            lifecycle.attach_server(server)
            if lifecycle.stopping:
                server.server_close()
                return
            descendant: subprocess.Popen[bytes] | None = None
            if os.environ.get("GROOVE_SERPENT_FIXTURE_TEST_DESCENDANT") == "1":
                descendant_ready = directory / "descendant-ready"
                descendant = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        (
                            "import signal,sys,time; "
                            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                            "open(sys.argv[1], 'x').close(); time.sleep(120)"
                        ),
                        str(descendant_ready),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                descendant_deadline = time.monotonic() + 5.0
                while not descendant_ready.is_file():
                    lifecycle.raise_if_stopping()
                    if descendant.poll() is not None:
                        raise RuntimeError("The stubborn fixture descendant failed.")
                    if time.monotonic() >= descendant_deadline:
                        raise RuntimeError("The stubborn fixture descendant did not start.")
                    time.sleep(0.01)
            _host, port = server.server_address
            print(
                json.dumps(
                    {
                        "schema": "groove-serpent.side-browser-fixture/1",
                        "fixture_pid": os.getpid(),
                        "descendant_pid": (descendant.pid if descendant is not None else None),
                        "url": server.session_auth.bootstrap_url(port=int(port)),
                        "project_path": str(project_path),
                        "source_path": str(directory / "synthetic-side.flac"),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            try:
                server.serve_forever(poll_interval=0.02)
            finally:
                server.server_close()
    finally:
        lifecycle.reap_owned_descendants()


if __name__ == "__main__":
    main()
