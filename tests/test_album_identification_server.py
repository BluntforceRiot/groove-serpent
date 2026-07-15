from __future__ import annotations

import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from groove_serpent.album import (
    AlbumProject,
    AlbumSide,
    load_album_project,
    repin_album_sides,
    save_album_project,
)
from groove_serpent.album_publication_policy import speed_correction_details
from groove_serpent.album_review_server import AlbumReviewServer
from groove_serpent.audio_snapshot import VerifiedAudioSnapshot
from groove_serpent.metadata import MetadataLookupError
from groove_serpent.models import (
    AnalysisSettings,
    AnalysisSummary,
    AudioSource,
    Project,
    Track,
)
from groove_serpent.project_io import load_project, save_project
from groove_serpent.publication import canonical_json_sha256
from groove_serpent.recognition import RecognitionMatch, RecognitionReadiness


RELEASE_MBID = "11111111-1111-4111-8111-111111111111"
RELEASE_GROUP_MBID = "22222222-2222-4222-8222-222222222222"
RECORDING_MBID = "33333333-3333-4333-8333-333333333333"


class _FakeRecognitionProvider:
    name = "test-recognition"

    def __init__(
        self,
        *,
        matches: list[RecognitionMatch] | None = None,
        ready: bool = True,
        on_first_call: Callable[[VerifiedAudioSnapshot], None] | None = None,
    ) -> None:
        self.matches = list(matches or [])
        self.ready = ready
        self.on_first_call = on_first_call
        self.calls: list[VerifiedAudioSnapshot] = []
        self.speed_factors: list[float] = []
        self._lock = threading.Lock()

    def readiness(self) -> RecognitionReadiness:
        return RecognitionReadiness(
            provider=self.name,
            enabled=True,
            ready=self.ready,
            message="Test recognition is ready." if self.ready else "Test disabled.",
            fingerprint_backend="test",
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
        del start_sample, end_sample, sample_rate
        if not isinstance(source_path, VerifiedAudioSnapshot):
            raise AssertionError("Album identification must use a verified snapshot.")
        with self._lock:
            first = not self.calls
            self.calls.append(source_path)
            self.speed_factors.append(source_speed_factor)
        if first and self.on_first_call is not None:
            self.on_first_call(source_path)
        return list(self.matches)


class _FakeMusicBrainzClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.on_get: Callable[[], None] | None = None

    def get_release(self, release_id: str) -> dict[str, Any]:
        self.calls.append(release_id)
        if self.on_get is not None:
            self.on_get()
        return {
            "id": release_id,
            "title": "Example Album",
            "artist": "Example Artist",
            "date": "2026-07-01",
            "country": "US",
            "status": "Official",
            "barcode": "0123456789012",
            "label": "Example Records",
            "catalog_number": "EX-001",
            "release_group_id": RELEASE_GROUP_MBID,
            "genres": ["Metal"],
            "formats": ["12\" Vinyl"],
            "track_count": 2,
            "has_artwork": True,
            "media": [
                {
                    "position": 1,
                    "title": "",
                    "format": "12\" Vinyl",
                    "track_count": 2,
                    "tracks": [
                        {
                            "position": 1,
                            "number": "A1",
                            "title": "Track side-a",
                            "artist": "Example Artist",
                            "duration_seconds": 10.0,
                        },
                        {
                            "position": 2,
                            "number": "B1",
                            "title": "Track side-b",
                            "artist": "Example Artist",
                            "duration_seconds": 10.0,
                        },
                    ],
                }
            ],
        }


class _FakeCoverArtClient:
    image = b"\x89PNG\r\n\x1a\n" + b"reviewed-cover-art"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[str, str]] = []
        self.on_download: Callable[[], None] | None = None

    def download_front_art(self, release_id: str, *, size: str) -> dict[str, Any]:
        self.calls.append((release_id, size))
        relative = Path("artwork") / "review" / f"{release_id}-front-1200.png"
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise MetadataLookupError(
                "Cover artwork already exists; Groove Serpent will not overwrite it."
            )
        destination.write_bytes(self.image)
        if self.on_download is not None:
            self.on_download()
        return {
            "relative_path": relative.as_posix(),
            "source_url": "https://coverartarchive.org/release/example/front.png",
            "mime_type": "image/png",
            "sha256": hashlib.sha256(self.image).hexdigest(),
            "size_bytes": len(self.image),
            "requested_size": size,
            "selected_size": size,
        }


def _release_match() -> RecognitionMatch:
    return RecognitionMatch(
        title="Example Track",
        artist_credit="Example Artist",
        score=0.98,
        recording_mbid=RECORDING_MBID,
        release_candidates=(
            {
                "release_mbid": RELEASE_MBID,
                "title": "Example Album",
                "release_group_mbid": RELEASE_GROUP_MBID,
                "country": "US",
                "date": "2026",
                "status": "Official",
                "release_group_title": "Example Album",
                "release_group_type": "Album",
                "release_group_secondary_types": [],
            },
        ),
        release_group_ids=(RELEASE_GROUP_MBID,),
        provider="test-recognition",
    )


class AlbumIdentificationServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        side_a = self._write_project("side-a")
        side_b = self._write_project("side-b")
        self.side_paths = [side_a, side_b]
        self.source_paths = [
            self.directory / "side-a.flac",
            self.directory / "side-b.flac",
        ]
        self.album_path = self.directory / "album.groove-album.json"
        album = AlbumProject(
            metadata={"artist": "Example Artist", "album": "Example Album"},
            sides=[
                AlbumSide("A", 1, side_a.name),
                AlbumSide("B", 2, side_b.name),
            ],
        )
        repin_album_sides(album, self.album_path)
        save_album_project(album, self.album_path)
        self.provider = _FakeRecognitionProvider(matches=[_release_match()])
        self.musicbrainz = _FakeMusicBrainzClient()
        self.cover_art = _FakeCoverArtClient(self.directory)
        self.server = AlbumReviewServer(
            ("127.0.0.1", 0),
            self.album_path,
            recognition_provider=self.provider,
            musicbrainz_client=self.musicbrainz,  # type: ignore[arg-type]
            cover_art_client=self.cover_art,  # type: ignore[arg-type]
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.port = self.server.server_address[1]
        self.authority = f"{self.server.session_auth.public_host}:{self.port}"
        self.base = self.server.session_auth.origin(port=self.port)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary_directory.cleanup()

    def restart_server(self, provider: _FakeRecognitionProvider) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.provider = provider
        self.server = AlbumReviewServer(
            ("127.0.0.1", 0),
            self.album_path,
            recognition_provider=provider,
            musicbrainz_client=self.musicbrainz,  # type: ignore[arg-type]
            cover_art_client=self.cover_art,  # type: ignore[arg-type]
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        self.port = self.server.server_address[1]
        self.authority = f"{self.server.session_auth.public_host}:{self.port}"
        self.base = self.server.session_auth.origin(port=self.port)

    def _write_project(self, stem: str) -> Path:
        source = self.directory / f"{stem}.flac"
        payload = (f"immutable-{stem}".encode("utf-8")) * 64
        source.write_bytes(payload)
        source_stat = source.stat()
        project = Project(
            source=AudioSource(
                path=source.name,
                filename=source.name,
                size_bytes=source_stat.st_size,
                modified_ns=source_stat.st_mtime_ns,
                duration_seconds=10.0,
                sample_rate=1_000,
                channels=2,
                codec_name="flac",
                bits_per_raw_sample=16,
                sample_format="s16",
                sample_count=10_000,
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
            settings=AnalysisSettings(min_track_seconds=0.1),
            analysis=AnalysisSummary(
                music_start_seconds=0.0,
                music_end_seconds=10.0,
                noise_floor_db=-60.0,
                silence_threshold_db=-54.0,
                active_threshold_db=-42.0,
                envelope_window_seconds=0.05,
            ),
            tracks=[
                Track(
                    number=1,
                    title=f"Track {stem}",
                    start_sample=0,
                    end_sample=10_000,
                    start_seconds=0.0,
                    end_seconds=10.0,
                    artist="Example Artist",
                    album="Example Album",
                )
            ],
            metadata={"artist": "Example Artist", "album": "Example Album"},
        )
        project_path = self.directory / f"{stem}.groove.json"
        save_project(project, project_path)
        return project_path

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
    ) -> tuple[int, dict[str, Any]]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": self.server.session_auth.authorization_header,
            "Host": self.authority,
        }
        if body is not None:
            headers.update({"Content-Type": "application/json", "Origin": self.base})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read()
        status = response.status
        connection.close()
        parsed = json.loads(response_body)
        self.assertIsInstance(parsed, dict)
        return status, parsed

    def raw_request(self, path: str) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request(
            "GET",
            path,
            headers={
                "Authorization": self.server.session_auth.authorization_header,
                "Host": self.authority,
            },
        )
        response = connection.getresponse()
        body = response.read()
        status = response.status
        headers = {key.casefold(): value for key, value in response.getheaders()}
        connection.close()
        return status, headers, body

    def state(self) -> dict[str, Any]:
        status, state = self.request("GET", "/api/album/state")
        self.assertEqual(status, 200, state)
        return state

    @staticmethod
    def mutation_preconditions(state: dict[str, Any]) -> dict[str, object]:
        sides = state["sides"]
        assert isinstance(sides, list)
        return {
            "expected_album_sha256": state["album_project_sha256"],
            "expected_album_revision": state["album_revision"],
            "expected_sides": [
                {
                    "side_label": side["label"],
                    "current_identity": side["current_identity"],
                }
                for side in sides
            ],
        }

    @classmethod
    def scan_payload(cls, state: dict[str, Any]) -> dict[str, object]:
        return {
            **cls.mutation_preconditions(state),
            "action": "scan-current-track-fingerprints",
            "network_reviewed": True,
        }

    @classmethod
    def release_payload(
        cls,
        state: dict[str, Any],
        proposal: dict[str, Any],
        entry: dict[str, Any],
    ) -> dict[str, object]:
        return {
            **cls.mutation_preconditions(state),
            "action": "fetch-current-candidate-release-details",
            "network_reviewed": True,
            "proposal_filename": entry["filename"],
            "proposal_file_sha256": entry["file_sha256"],
            "proposal_sha256": proposal["proposal_sha256"],
            "release_mbid": RELEASE_MBID,
        }

    def test_state_exposes_strict_review_only_identification_contract(self) -> None:
        state = self.state()
        self.assertEqual(state["schema"], "groove-serpent.album-workbench/4")
        identification = state["identification"]
        self.assertTrue(identification["readiness"]["can_scan"])
        self.assertEqual(identification["provider"]["provider"], self.provider.name)
        self.assertEqual(
            identification["catalog"]["summary"],
            {"total": 0, "current": 0, "stale": 0, "invalid": 0, "selectable": 0},
        )
        authority = identification["authority"]
        self.assertFalse(authority["automatic_network_requests"])
        self.assertTrue(authority["explicit_network_review_required"])
        self.assertFalse(authority["automatic_metadata_application"])
        self.assertFalse(authority["automatic_artwork_download_or_application"])
        self.assertFalse(authority["physical_pressing_proven"])

    def test_scan_requires_exact_explicit_network_review(self) -> None:
        state = self.state()
        payload = self.scan_payload(state)
        payload["network_reviewed"] = False
        status, response = self.request(
            "POST", "/api/album/identification/scan", payload=payload
        )
        self.assertEqual(status, 400, response)
        self.assertEqual(self.provider.calls, [])

        payload = self.scan_payload(state)
        payload["unexpected"] = True
        status, response = self.request(
            "POST", "/api/album/identification/scan", payload=payload
        )
        self.assertEqual(status, 400, response)
        self.assertEqual(self.provider.calls, [])

    def test_release_details_are_current_candidate_bound_and_review_only(self) -> None:
        initial = self.state()
        status, scanned = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(initial),
        )
        self.assertEqual(status, 201, scanned)
        proposal = scanned["proposal"]
        entry = scanned["catalog_entry"]
        state = scanned["state"]
        album_before = self.album_path.read_bytes()
        projects_before = [path.read_bytes() for path in self.side_paths]
        sources_before = [path.read_bytes() for path in self.source_paths]

        status, response = self.request(
            "POST",
            "/api/album/identification/release-details",
            payload=self.release_payload(state, proposal, entry),
        )
        self.assertEqual(status, 200, response)
        self.assertTrue(response["network_request_performed"])
        review = response["review"]
        self.assertEqual(review["schema"], "groove-serpent.album-release-review/1")
        self.assertEqual(review["release"]["release_mbid"], RELEASE_MBID)
        self.assertEqual(review["release"]["label"], "Example Records")
        self.assertEqual(review["release"]["catalog_number"], "EX-001")
        self.assertEqual(review["release"]["track_count"], 2)
        self.assertEqual(len(review["release"]["tracklist"]), 2)
        binding = review["binding"]
        self.assertEqual(binding["album_sha256"], state["album_project_sha256"])
        self.assertEqual(binding["proposal_file_sha256"], entry["file_sha256"])
        self.assertEqual(binding["proposal_sha256"], proposal["proposal_sha256"])
        self.assertEqual(
            binding["source_bindings_sha256"],
            canonical_json_sha256(proposal["album"]["sides"]),
        )
        self.assertEqual(
            review["release_sha256"],
            canonical_json_sha256(review["release"]),
        )
        authority = review["authority"]
        self.assertTrue(authority["read_only"])
        self.assertFalse(authority["metadata_applied"])
        self.assertFalse(authority["artwork_downloaded"])
        self.assertFalse(authority["may_modify_album_project"])
        self.assertFalse(authority["may_modify_side_projects"])
        self.assertFalse(authority["physical_pressing_proven"])
        self.assertEqual(self.musicbrainz.calls, [RELEASE_MBID])
        self.assertEqual(self.album_path.read_bytes(), album_before)
        self.assertEqual([path.read_bytes() for path in self.side_paths], projects_before)
        self.assertEqual([path.read_bytes() for path in self.source_paths], sources_before)

    def test_release_details_require_exact_candidate_and_network_review(self) -> None:
        initial = self.state()
        status, scanned = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(initial),
        )
        self.assertEqual(status, 201, scanned)
        payload = self.release_payload(
            scanned["state"], scanned["proposal"], scanned["catalog_entry"]
        )
        payload["network_reviewed"] = False
        status, _response = self.request(
            "POST", "/api/album/identification/release-details", payload=payload
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.musicbrainz.calls, [])

        payload = self.release_payload(
            scanned["state"], scanned["proposal"], scanned["catalog_entry"]
        )
        payload["release_mbid"] = "44444444-4444-4444-8444-444444444444"
        status, _response = self.request(
            "POST", "/api/album/identification/release-details", payload=payload
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.musicbrainz.calls, [])

    def test_artwork_requires_review_then_downloads_hash_bound_preview_without_apply(
        self,
    ) -> None:
        initial = self.state()
        status, scanned = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(initial),
        )
        self.assertEqual(status, 201, scanned)
        proposal = scanned["proposal"]
        entry = scanned["catalog_entry"]
        state = scanned["state"]
        common = {
            **self.mutation_preconditions(state),
            "action": "download-reviewed-candidate-front-artwork",
            "network_reviewed": True,
            "proposal_filename": entry["filename"],
            "proposal_file_sha256": entry["file_sha256"],
            "proposal_sha256": proposal["proposal_sha256"],
            "release_mbid": RELEASE_MBID,
            "expected_release_review_sha256": "0" * 64,
        }
        status, _response = self.request(
            "POST", "/api/album/identification/download-artwork", payload=common
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.cover_art.calls, [])

        status, details = self.request(
            "POST",
            "/api/album/identification/release-details",
            payload=self.release_payload(state, proposal, entry),
        )
        self.assertEqual(status, 200, details)
        album_before = self.album_path.read_bytes()
        projects_before = [path.read_bytes() for path in self.side_paths]
        sources_before = [path.read_bytes() for path in self.source_paths]
        common["expected_release_review_sha256"] = details["review"]["review_sha256"]
        status, response = self.request(
            "POST", "/api/album/identification/download-artwork", payload=common
        )
        self.assertEqual(status, 201, response)
        artwork_review = response["artwork"]
        self.assertEqual(
            artwork_review["schema"], "groove-serpent.album-artwork-review/1"
        )
        artwork = artwork_review["artwork"]
        self.assertEqual(artwork["sha256"], hashlib.sha256(self.cover_art.image).hexdigest())
        self.assertEqual(artwork["size_bytes"], len(self.cover_art.image))
        self.assertEqual(
            artwork_review["binding"]["release_review_sha256"],
            details["review"]["review_sha256"],
        )
        self.assertTrue(artwork_review["authority"]["artwork_downloaded"])
        self.assertFalse(artwork_review["authority"]["artwork_applied"])
        self.assertFalse(artwork_review["authority"]["physical_pressing_proven"])
        status, headers, body = self.raw_request(artwork["preview_url"])
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "image/png")
        self.assertEqual(headers["cache-control"], "private, no-store")
        self.assertEqual(body, self.cover_art.image)
        self.assertEqual(self.album_path.read_bytes(), album_before)
        self.assertEqual([path.read_bytes() for path in self.side_paths], projects_before)
        self.assertEqual([path.read_bytes() for path in self.source_paths], sources_before)

        saved = self.directory / artwork["relative_path"]
        saved_before = saved.read_bytes()
        status, refused = self.request(
            "POST", "/api/album/identification/download-artwork", payload=common
        )
        self.assertEqual(status, 400, refused)
        self.assertEqual(saved.read_bytes(), saved_before)

        saved.write_bytes(saved_before + b"tampered")
        status, _headers, _body = self.raw_request(artwork["preview_url"])
        self.assertEqual(status, 400)

    def test_release_detail_race_fails_closed_without_registering_review(self) -> None:
        initial = self.state()
        status, scanned = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(initial),
        )
        self.assertEqual(status, 201, scanned)
        self.musicbrainz.on_get = lambda: self.album_path.write_bytes(
            self.album_path.read_bytes() + b"\n"
        )
        status, _response = self.request(
            "POST",
            "/api/album/identification/release-details",
            payload=self.release_payload(
                scanned["state"], scanned["proposal"], scanned["catalog_entry"]
            ),
        )
        self.assertEqual(status, 409)
        self.assertEqual(self.server.release_reviews, {})

    def test_artwork_race_removes_only_new_unbound_review_file(self) -> None:
        initial = self.state()
        status, scanned = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(initial),
        )
        self.assertEqual(status, 201, scanned)
        status, details = self.request(
            "POST",
            "/api/album/identification/release-details",
            payload=self.release_payload(
                scanned["state"], scanned["proposal"], scanned["catalog_entry"]
            ),
        )
        self.assertEqual(status, 200, details)
        self.cover_art.on_download = lambda: self.album_path.write_bytes(
            self.album_path.read_bytes() + b"\n"
        )
        entry = scanned["catalog_entry"]
        proposal = scanned["proposal"]
        payload = {
            **self.mutation_preconditions(scanned["state"]),
            "action": "download-reviewed-candidate-front-artwork",
            "network_reviewed": True,
            "proposal_filename": entry["filename"],
            "proposal_file_sha256": entry["file_sha256"],
            "proposal_sha256": proposal["proposal_sha256"],
            "release_mbid": RELEASE_MBID,
            "expected_release_review_sha256": details["review"]["review_sha256"],
        }
        status, _response = self.request(
            "POST", "/api/album/identification/download-artwork", payload=payload
        )
        self.assertEqual(status, 409)
        destination = (
            self.directory
            / "artwork"
            / "review"
            / f"{RELEASE_MBID}-front-1200.png"
        )
        self.assertFalse(destination.exists())
        self.assertEqual(self.server.artwork_previews, {})

    def test_scan_uses_snapshots_persists_and_reopens_without_authority(self) -> None:
        state = self.state()
        album_before = self.album_path.read_bytes()
        projects_before = [path.read_bytes() for path in self.side_paths]
        sources_before = [path.read_bytes() for path in self.source_paths]

        status, result = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(state),
        )
        self.assertEqual(status, 201, result)
        self.assertEqual(result["completion"], "proposal-created")
        self.assertEqual(result["scan"]["matched_track_count"], 2)
        self.assertEqual(result["scan"]["unmatched_track_count"], 0)
        proposal = result["proposal"]
        self.assertEqual(proposal["decision"]["status"], "proposed")
        self.assertEqual(proposal["decision"]["selected_release_mbid"], RELEASE_MBID)
        self.assertEqual(
            proposal["authority"],
            {
                "may_modify_album_project": False,
                "may_modify_side_projects": False,
                "may_apply_metadata": False,
                "may_download_or_apply_artwork": False,
                "may_change_topology_speed_or_restoration": False,
                "may_publish": False,
                "human_review_required": True,
                "physical_pressing_proven": False,
            },
        )
        self.assertEqual(self.album_path.read_bytes(), album_before)
        self.assertEqual([path.read_bytes() for path in self.side_paths], projects_before)
        self.assertEqual([path.read_bytes() for path in self.source_paths], sources_before)
        self.assertEqual(len(self.provider.calls), 2)
        for snapshot, source_path in zip(self.provider.calls, self.source_paths, strict=True):
            self.assertNotEqual(snapshot.path, snapshot.live_path)
            self.assertEqual(snapshot.live_path, source_path.resolve())

        self.restart_server(_FakeRecognitionProvider(matches=[_release_match()]))
        reopened_state = self.state()
        reopened_entries = reopened_state["identification"]["catalog"]["entries"]
        self.assertEqual(len(reopened_entries), 1)
        entry = reopened_entries[0]
        self.assertEqual(entry, result["catalog_entry"])
        open_payload = {
            **self.mutation_preconditions(reopened_state),
            "action": "open-current-identification-proposal",
            "filename": entry["filename"],
            "file_sha256": entry["file_sha256"],
            "proposal_sha256": entry["proposal_sha256"],
        }
        status, opened = self.request(
            "POST",
            "/api/album/identification/open-proposal",
            payload=open_payload,
        )
        self.assertEqual(status, 200, opened)
        self.assertTrue(opened["read_only"])
        self.assertEqual(opened["proposal"], proposal)
        self.assertEqual(opened["catalog_entry"], entry)

        status, repeated = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(opened["state"]),
        )
        self.assertEqual(status, 200, repeated)
        self.assertEqual(repeated["completion"], "proposal-reused")
        self.assertEqual(
            len(list(self.directory.glob("album-identification-*.proposal.json"))),
            1,
        )

    def test_scan_normalizes_reviewed_speed_and_binds_exact_geometry(self) -> None:
        requested_factor = 1.039482143
        project = load_project(self.side_paths[0])
        project.metadata.update(
            {
                "speed_capture_rpm": str(100.0 / 3.0),
                "speed_intended_rpm": str(100.0 / 3.0),
                "speed_fine_factor": str(requested_factor),
            }
        )
        save_project(project, self.side_paths[0])
        album = load_album_project(self.album_path)
        repin_album_sides(album, self.album_path)
        save_album_project(album, self.album_path, overwrite=True)
        provider = _FakeRecognitionProvider(matches=[_release_match()])
        self.server.recognition_provider = provider
        source_bytes = [path.read_bytes() for path in self.source_paths]

        state = self.state()
        status, result = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(state),
        )
        self.assertEqual(status, 201, result)
        self.assertEqual(provider.speed_factors, [requested_factor, 1.0])
        asetrate_hz, effective_factor = speed_correction_details(
            1_000,
            requested_factor,
        )
        proposal_side = result["proposal"]["album"]["sides"][0]
        self.assertEqual(proposal_side["requested_speed_factor"], requested_factor)
        self.assertEqual(proposal_side["fingerprint_asetrate_hz"], asetrate_hz)
        self.assertEqual(
            proposal_side["fingerprint_effective_speed_factor"],
            effective_factor,
        )
        self.assertEqual(
            proposal_side["fingerprint_speed_transform"],
            "integer-asetrate-pitch-and-tempo/1",
        )
        evidence = result["proposal"]["evidence"]["items"][0]
        self.assertEqual(evidence["requested_speed_factor"], requested_factor)
        self.assertEqual(evidence["fingerprint_asetrate_hz"], asetrate_hz)
        self.assertEqual([path.read_bytes() for path in self.source_paths], source_bytes)

    def test_zero_matches_abstains_without_manufacturing_a_proposal(self) -> None:
        self.server.recognition_provider = _FakeRecognitionProvider(matches=[])
        state = self.state()
        status, result = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(state),
        )
        self.assertEqual(status, 200, result)
        self.assertEqual(result["completion"], "abstained-no-matches")
        self.assertEqual(result["scan"]["matched_track_count"], 0)
        self.assertEqual(result["scan"]["unmatched_track_count"], 2)
        self.assertIsNone(result["proposal"])
        self.assertIsNone(result["catalog_entry"])
        self.assertEqual(
            list(self.directory.glob("album-identification-*.proposal.json")), []
        )

    def test_provider_match_fanout_is_bounded_before_evidence_creation(self) -> None:
        self.server.recognition_provider = _FakeRecognitionProvider(
            matches=[_release_match()] * 21
        )
        state = self.state()
        status, response = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(state),
        )
        self.assertEqual(status, 400, response)
        self.assertEqual(
            list(self.directory.glob("album-identification-*.proposal.json")), []
        )

    def test_state_reclassifies_proposal_stale_after_side_project_change(self) -> None:
        state = self.state()
        status, created = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(state),
        )
        self.assertEqual(status, 201, created)
        project = load_project(self.side_paths[0])
        project.metadata["genre"] = "Changed after identification"
        save_project(project, self.side_paths[0])

        stale = self.state()
        catalog = stale["identification"]["catalog"]
        self.assertFalse(catalog["live_context_available"])
        self.assertEqual(catalog["summary"]["current"], 0)
        self.assertEqual(catalog["summary"]["stale"], 1)
        self.assertEqual(catalog["summary"]["invalid"], 0)
        self.assertFalse(stale["identification"]["readiness"]["can_scan"])
        self.assertIn(
            "current_album_context_unavailable",
            stale["identification"]["readiness"]["reason_codes"],
        )

    def test_live_source_mutation_fails_closed_and_writes_no_proposal(self) -> None:
        def mutate(snapshot: VerifiedAudioSnapshot) -> None:
            snapshot.live_path.write_bytes(b"changed-during-recognition")

        self.server.recognition_provider = _FakeRecognitionProvider(
            matches=[_release_match()],
            on_first_call=mutate,
        )
        state = self.state()
        status, response = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(state),
        )
        self.assertEqual(status, 409, response)
        self.assertEqual(
            list(self.directory.glob("album-identification-*.proposal.json")), []
        )

    def test_external_album_race_fails_closed_before_proposal_persistence(self) -> None:
        def mutate_album(_snapshot: VerifiedAudioSnapshot) -> None:
            self.album_path.write_bytes(self.album_path.read_bytes() + b"\n")

        self.server.recognition_provider = _FakeRecognitionProvider(
            matches=[_release_match()],
            on_first_call=mutate_album,
        )
        state = self.state()
        status, response = self.request(
            "POST",
            "/api/album/identification/scan",
            payload=self.scan_payload(state),
        )
        self.assertEqual(status, 409, response)
        self.assertEqual(
            list(self.directory.glob("album-identification-*.proposal.json")), []
        )

    def test_concurrent_scan_is_refused(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def block(_snapshot: VerifiedAudioSnapshot) -> None:
            started.set()
            if not release.wait(timeout=10):
                raise AssertionError("Concurrent scan test timed out.")

        self.server.recognition_provider = _FakeRecognitionProvider(
            matches=[_release_match()],
            on_first_call=block,
        )
        state = self.state()
        payload = self.scan_payload(state)
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(
                self.request,
                "POST",
                "/api/album/identification/scan",
                payload=payload,
            )
            self.assertTrue(started.wait(timeout=10))
            status, response = self.request(
                "POST", "/api/album/identification/scan", payload=payload
            )
            self.assertEqual(status, 400, response)
            release.set()
            first_status, first_response = first.result(timeout=30)
        self.assertEqual(first_status, 201, first_response)


if __name__ == "__main__":
    unittest.main()
