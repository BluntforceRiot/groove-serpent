from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from groove_serpent.album import AlbumProject, AlbumSide, repin_album_sides, save_album_project
from groove_serpent.album_review_server import AlbumReviewServer
from groove_serpent.media import sha256_file
from groove_serpent.metadata import MetadataLookupError
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import load_project, save_project
from groove_serpent.recognition import RecognitionMatch, RecognitionReadiness
from groove_serpent.audio_snapshot import VerifiedAudioSnapshot
from fixture_lifecycle import FixtureLifecycle, install_fixture_lifecycle


class FixtureRecognitionProvider:
    """Deterministic network-free provider for browser contract acceptance."""

    name = "fixture-recognition"

    def readiness(self) -> RecognitionReadiness:
        return RecognitionReadiness(
            provider=self.name,
            enabled=True,
            ready=True,
            message="Deterministic browser-fixture recognition is ready.",
            fingerprint_backend="fixture-local-fingerprint",
        )

    def identify_track(
        self,
        source_path: str | Path | VerifiedAudioSnapshot,
        start_sample: int,
        end_sample: int,
        sample_rate: int,
        *,
        source_speed_factor: float = 1.0,
    ) -> list[RecognitionMatch]:
        del source_path, start_sample, end_sample, sample_rate, source_speed_factor
        return [
            RecognitionMatch(
                title="Fixture track",
                artist_credit="Fixture Artist",
                score=0.97,
                release_candidates=(
                    {
                        "release_mbid": "11111111-1111-4111-8111-111111111111",
                        "title": "Fixture Album",
                        "release_group_mbid": "22222222-2222-4222-8222-222222222222",
                        "country": "US",
                        "date": "2026",
                        "status": "Official",
                        "release_group_title": "Fixture Album",
                        "release_group_type": "Album",
                        "release_group_secondary_types": [],
                    },
                ),
                release_group_ids=("22222222-2222-4222-8222-222222222222",),
                provider=self.name,
            )
        ]


class FixtureMusicBrainzClient:
    """Deterministic network-free release details for browser acceptance."""

    def get_release(self, release_id: str) -> dict[str, object]:
        tracks = [
            (1, "A1", "First"),
            (2, "A2", "First reprise"),
            (3, "B1", "Second"),
            (4, "B2", "Second reprise"),
        ]
        return {
            "id": release_id,
            "title": "Fixture Album",
            "artist": "Fixture Artist",
            "date": "2026-07-13",
            "country": "US",
            "status": "Official",
            "barcode": "0123456789012",
            "label": "Fixture Records",
            "catalog_number": "FIX-001",
            "release_group_id": "22222222-2222-4222-8222-222222222222",
            "genres": ["Metal"],
            "formats": ["12\" Vinyl"],
            "track_count": len(tracks),
            "has_artwork": True,
            "media": [
                {
                    "position": 1,
                    "title": "",
                    "format": "12\" Vinyl",
                    "track_count": len(tracks),
                    "tracks": [
                        {
                            "position": position,
                            "number": number,
                            "title": title,
                            "artist": "Fixture Artist",
                            "duration_seconds": 0.5,
                        }
                        for position, number, title in tracks
                    ],
                }
            ],
        }


class FixtureCoverArtClient:
    """Write one real tiny PNG without network or overwrite authority."""

    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+"
        "A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    def __init__(self, root: Path) -> None:
        self.root = root

    def download_front_art(self, release_id: str, *, size: str) -> dict[str, object]:
        relative = Path("artwork") / "review" / f"{release_id}-front-1200.png"
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise MetadataLookupError("Fixture artwork refuses overwrite.")
        destination.write_bytes(self.image)
        return {
            "relative_path": relative.as_posix(),
            "source_url": "https://coverartarchive.org/release/fixture/front.png",
            "mime_type": "image/png",
            "sha256": hashlib.sha256(self.image).hexdigest(),
            "size_bytes": len(self.image),
            "requested_size": size,
            "selected_size": size,
        }


def _make_source(directory: Path, lifecycle: FixtureLifecycle) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("The browser fixture requires FFmpeg on PATH.")
    source = directory / "shared-source.flac"
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
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-ac",
            "2",
            "-sample_fmt",
            "s16",
            str(source),
        ],
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "FFmpeg could not create the browser fixture: "
            + completed.stderr.decode("utf-8", errors="replace")
        )
    return source


def _write_project(directory: Path, source: Path, stem: str, title: str) -> Path:
    details = source.stat()
    sample_count = 48_000
    audio = AudioSource(
        path=source.name,
        filename=source.name,
        size_bytes=details.st_size,
        modified_ns=details.st_mtime_ns,
        duration_seconds=1.0,
        sample_rate=48_000,
        channels=2,
        codec_name="flac",
        bits_per_raw_sample=16,
        sample_format="s16",
        sample_count=sample_count,
        sha256=sha256_file(source),
    )
    project = Project(
        source=audio,
        settings=AnalysisSettings(min_track_seconds=0.1),
        analysis=AnalysisSummary(
            music_start_seconds=0.0,
            music_end_seconds=audio.duration_seconds,
            noise_floor_db=-60.0,
            silence_threshold_db=-54.0,
            active_threshold_db=-42.0,
            envelope_window_seconds=0.05,
        ),
        tracks=[
            Track(
                number=1,
                title=title,
                start_sample=0,
                end_sample=sample_count // 2,
                start_seconds=0.0,
                end_seconds=audio.duration_seconds / 2,
                artist="Fixture Artist",
                album="Fixture Album",
            ),
            Track(
                number=2,
                title=f"{title} reprise",
                start_sample=sample_count // 2,
                end_sample=sample_count,
                start_seconds=audio.duration_seconds / 2,
                end_seconds=audio.duration_seconds,
                artist="Fixture Artist",
                album="Fixture Album",
            ),
        ],
        metadata={"artist": "Fixture Artist", "album": "Fixture Album"},
    )
    project_path = directory / f"{stem}.groove.json"
    save_project(project, project_path)
    return project_path


def _build_album(directory: Path, lifecycle: FixtureLifecycle) -> Path:
    source = _make_source(directory, lifecycle)
    side_a = _write_project(directory, source, "side-a", "First")
    side_b = _write_project(directory, source, "side-b", "Second")
    album_path = directory / "fixture-album.groove-album.json"
    album = AlbumProject(
        metadata={"artist": "Fixture Artist", "album": "Fixture Album"},
        sides=[AlbumSide("A", 1, side_a.name), AlbumSide("B", 2, side_b.name)],
    )
    repin_album_sides(album, album_path)
    save_album_project(album, album_path)

    changed = load_project(side_a)
    changed.metadata["genre"] = "Metal"
    save_project(changed, side_a)
    return album_path


def main() -> None:
    lifecycle = install_fixture_lifecycle()
    try:
        with tempfile.TemporaryDirectory(prefix="groove-serpent-browser-") as temporary:
            directory = Path(temporary)
            album_path = _build_album(directory, lifecycle)
            lifecycle.raise_if_stopping()
            server = AlbumReviewServer(
                ("127.0.0.1", 0),
                album_path,
                recognition_provider=FixtureRecognitionProvider(),
                musicbrainz_client=FixtureMusicBrainzClient(),  # type: ignore[arg-type]
                cover_art_client=FixtureCoverArtClient(directory),  # type: ignore[arg-type]
            )

            lifecycle.attach_server(server)
            if lifecycle.stopping:
                server.server_close()
                return
            _host, port = server.server_address
            print(
                json.dumps(
                    {
                        "schema": "groove-serpent.browser-fixture/1",
                        "fixture_pid": os.getpid(),
                        "url": server.session_auth.bootstrap_url(port=int(port)),
                        "album_path": str(album_path),
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
