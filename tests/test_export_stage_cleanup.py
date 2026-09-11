"""Album and side exports preserve replaced stages during failure cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from groove_serpent import album, exporter
from groove_serpent.errors import ExportError
from groove_serpent.models import AnalysisSettings, AnalysisSummary, AudioSource, Project, Track
from groove_serpent.project_io import save_project


def _project(root: Path) -> tuple[Project, Path]:
    source = root / "synthetic.flac"
    payload = b"synthetic immutable source"
    source.write_bytes(payload)
    metadata = source.stat()
    project = Project(
        source=AudioSource(
            path=source.name, filename=source.name, size_bytes=len(payload),
            modified_ns=metadata.st_mtime_ns, duration_seconds=1.0, sample_rate=48_000,
            channels=2, codec_name="flac", bits_per_raw_sample=24, sample_format="s32",
            sample_count=48_000, sha256=hashlib.sha256(payload).hexdigest(),
        ),
        settings=AnalysisSettings(min_track_seconds=0.1),
        analysis=AnalysisSummary(
            music_start_seconds=0.0, music_end_seconds=1.0, noise_floor_db=-60.0,
            silence_threshold_db=-54.0, active_threshold_db=-42.0, envelope_window_seconds=0.05,
        ),
        tracks=[Track(number=1, title="One", start_sample=0, end_sample=48_000,
                      start_seconds=0.0, end_seconds=1.0)],
    )
    project_path = root / "side.groove.json"
    save_project(project, project_path)
    return project, project_path


def _replace_directory(path: Path, kind: str, outside: Path) -> None:
    path.rename(path.with_name(path.name + ".parked"))
    if kind == "directory":
        path.mkdir()
        (path / "foreign.txt").write_bytes(b"preserve foreign directory")
    elif kind == "file":
        path.write_bytes(b"preserve foreign file")
    elif kind == "junction":
        if os.name != "nt":
            pytest.skip("Native Windows junction test")
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(path), str(outside)],
            capture_output=True, check=False,
        )
        if result.returncode:
            pytest.skip("Junction creation unavailable")
    else:
        try:
            path.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("Directory symlink creation unavailable")


def _assert_foreign(path: Path, kind: str, outside: Path) -> None:
    assert os.path.lexists(path)
    if kind == "directory":
        assert (path / "foreign.txt").read_bytes() == b"preserve foreign directory"
    elif kind == "file":
        assert path.read_bytes() == b"preserve foreign file"
    else:
        assert path.samefile(outside)
    assert (outside / "sentinel.txt").read_bytes() == b"outside unchanged"


@pytest.mark.parametrize("operation", ("side", "album"))
@pytest.mark.parametrize("scope", ("stage", "work"))
@pytest.mark.parametrize("kind", ("directory", "file", "junction", "symlink"))
def test_export_failure_preserves_replaced_stage_or_work_directory(
    tmp_path: Path, operation: str, scope: str, kind: str,
) -> None:
    project, project_path = _project(tmp_path)
    source = tmp_path / project.source.path
    originals = {source: source.read_bytes(), project_path: project_path.read_bytes()}
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside unchanged")
    output = tmp_path / "output"
    replaced: list[Path] = []
    module = exporter if operation == "side" else album

    def replace_and_fail(_source: Path, target: Path, *_args: object, **_kwargs: object) -> None:
        stage = target.parents[1] if operation == "side" else target.parents[2]
        work = stage / (".operation-inputs" if operation == "side" else ".work")
        replacement = stage if scope == "stage" else work
        _replace_directory(replacement, kind, outside)
        replaced.append(replacement)
        raise ExportError("synthetic source preparation failure")

    if operation == "album":
        album_path = tmp_path / "album.json"
        value = album.AlbumProject({}, [album.AlbumSide("A", 1, project_path.name)])
        album.repin_album_sides(value, album_path)
        album.save_album_project(value, album_path)
        originals[album_path] = album_path.read_bytes()
    with (
        mock.patch.object(exporter, "probe_audio", return_value=project.source),
        mock.patch.object(exporter, "tool_version", return_value="synthetic tool"),
        mock.patch.object(module, "stage_verified_copy", side_effect=replace_and_fail),
        pytest.raises(ExportError, match="synthetic source preparation failure"),
    ):
        if operation == "side":
            exporter.export_project(project, project_path, output, formats=["flac"])
        else:
            album.export_album(value, album_path, output, formats=["flac"])
    assert len(replaced) == 1
    _assert_foreign(replaced[0], kind, outside)
    assert not output.exists()
    assert all(path.read_bytes() == payload for path, payload in originals.items())


@pytest.mark.parametrize("operation", ("side", "album"))
@pytest.mark.parametrize("kind", ("directory", "file", "junction"))
def test_direct_work_cleanup_refuses_replacement_and_outer_cleanup_preserves_it(
    tmp_path: Path, operation: str, kind: str,
) -> None:
    project, project_path = _project(tmp_path)
    source = tmp_path / project.source.path
    originals = {source: source.read_bytes(), project_path: project_path.read_bytes()}
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel.txt").write_bytes(b"outside unchanged")
    replaced: list[Path] = []
    output = tmp_path / "output"

    def replace(path: Path) -> None:
        _replace_directory(path, kind, outside)
        replaced.append(path)

    def fake_ffmpeg(command: list[str]) -> None:
        Path(command[-1]).write_bytes(b"synthetic track")

    verification = exporter._StagedAudioVerification(
        codec_name="flac", sample_rate=48_000, channels=2, bits_per_raw_sample=24,
        exact_sample_count=48_000, presentation_sample_count=None,
        decoded_pcm_sha256="c" * 64, source_range_pcm_sha256="c" * 64,
    )
    original_assert = exporter.assert_file_receipt

    def check_then_replace(path: Path, receipt: object, *, label: str) -> None:
        original_assert(path, receipt, label=label)
        if label == "Staged source snapshot":
            replace(path.parent)

    def fake_side_export(
        _project: Project, _project_path: Path, destination: Path, **_kwargs: object,
    ) -> exporter.ExportReport:
        destination.mkdir()
        payload = b"synthetic track"
        track = destination / "01 - One.flac"
        track.write_bytes(payload)
        manifest = destination / "groove-serpent-manifest.json"
        manifest.write_text(json.dumps({"files": [track.name]}), encoding="utf-8")
        return exporter.ExportReport(str(destination), [exporter.ExportedFile(
            track_number=1, format="flac", path=track.name, size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(), expected_sample_count=48_000,
        )], str(manifest))

    def fake_continuous(**kwargs: object) -> dict[str, object]:
        destination = kwargs["destination"]
        assert isinstance(destination, Path)
        payload = b"synthetic continuous side"
        destination.write_bytes(payload)
        return {"path": destination.as_posix(), "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "expected_sample_count": 48_000, "presentation_sample_count": 48_000}

    original_inventory = album._inventory_file

    def inventory_then_replace(
        root: Path, path: Path, role: str, **kwargs: object,
    ) -> dict[str, object]:
        record = original_inventory(root, path, role, **kwargs)
        if role == "exact-chapters":
            replace(root / ".work")
        return record

    if operation == "album":
        album_path = tmp_path / "album.json"
        value = album.AlbumProject({}, [album.AlbumSide("A", 1, project_path.name)])
        album.repin_album_sides(value, album_path)
        album.save_album_project(value, album_path)
        originals[album_path] = album_path.read_bytes()
    with (
        mock.patch.object(exporter, "probe_audio", return_value=project.source),
        mock.patch.object(exporter, "tool_version", return_value="synthetic tool"),
        mock.patch.object(exporter, "run_ffmpeg", side_effect=fake_ffmpeg),
        # These opaque-byte cleanup fixtures already replace encoding and its
        # verifier. Keep native stream authority unmocked in the real-audio tests.
        mock.patch.object(exporter, "_verify_render_source_geometry"),
        mock.patch.object(exporter, "_verify_staged_output", return_value=verification),
        mock.patch.object(exporter, "assert_file_receipt", side_effect=check_then_replace),
        mock.patch.object(album, "export_project", side_effect=fake_side_export),
        mock.patch.object(album, "_write_continuous_side", side_effect=fake_continuous),
        mock.patch.object(album, "_inventory_file", side_effect=inventory_then_replace),
        pytest.raises(ExportError, match="preserved"),
    ):
        if operation == "side":
            exporter.export_project(project, project_path, output, formats=["flac"])
        else:
            album.export_album(value, album_path, output, formats=["flac"])
    assert len(replaced) == 1
    _assert_foreign(replaced[0], kind, outside)
    assert not output.exists()
    assert all(path.read_bytes() == payload for path, payload in originals.items())
