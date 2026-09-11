from __future__ import annotations

import os
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from groove_serpent import (
    album_publication_executor, atomic_create, cache_storage, project_io, review_evidence,
)
from groove_serpent.models import (
    AnalysisSettings, AnalysisSummary, AudioSource, Project, Track,
)


def _project() -> Project:
    return Project(
        source=AudioSource(
            path="side.flac", filename="side.flac", size_bytes=12345, modified_ns=123,
            duration_seconds=10.0, sample_rate=1000, channels=2, codec_name="flac",
            sample_count=10000, sha256="a" * 64,
        ),
        settings=AnalysisSettings(min_track_seconds=0.1),
        analysis=AnalysisSummary(0.0, 10.0, -50.0, -44.0, -32.0, 0.05),
        tracks=[Track(1, "One", 0, 10000, 0.0, 10.0)],
    )


@pytest.mark.parametrize("writer_kind", ("project-new", "project-existing", "evidence",
                                         "cache", "publication"))
@pytest.mark.parametrize("replacement", ("none", "different", "identical", "modified"))
def test_failed_writer_preserves_every_unowned_temporary(
    tmp_path: Path, writer_kind: str, replacement: str,
) -> None:
    destination = tmp_path / "target.json"
    project = _project()
    writers: dict[str, Callable[[], object]] = {
        "project-new": lambda: project_io.save_project(project, destination),
        "project-existing": lambda: project_io.save_project(project, destination),
        "evidence": lambda: review_evidence._write_exact_new(destination, b'{"test":true}\n'),
        "cache": lambda: cache_storage._write_json_atomic(destination, {"test": True}),
        "publication": lambda: album_publication_executor._write_json(
            destination, {"test": True}
        ),
    }
    if writer_kind == "project-existing":
        project_io.save_project(project, destination)
    old_target = destination.read_bytes() if destination.exists() else None
    old_revision, old_updated_at = project.revision, project.updated_at
    if writer_kind in {"project-new", "evidence"}:
        owner = project_io if writer_kind == "project-new" else review_evidence
        operation = "rename_no_replace"
    else:
        owner = os
        operation = "replace"
    original = getattr(owner, operation)
    seen: list[tuple[Path, bytes]] = []

    def fail(source: Path, target: Path) -> None:
        if target != destination:
            original(source, target)
            return
        original_payload = source.read_bytes()
        foreign = original_payload if replacement == "identical" else b"foreign replacement"
        if replacement in {"different", "identical"}:
            source.rename(source.with_suffix(".parked"))
            source.write_bytes(foreign)
        elif replacement == "modified":
            source.write_bytes(foreign)
        seen.append((source, foreign))
        raise OSError("synthetic writer commit failure")

    with patch.object(owner, operation, side_effect=fail):
        with pytest.raises((OSError, review_evidence.ReviewEvidenceError)):
            writers[writer_kind]()
    assert len(seen) == 1
    temporary, foreign_payload = seen[0]
    if replacement == "none":
        assert not temporary.exists()
    else:
        assert temporary.read_bytes() == foreign_payload
    assert (destination.read_bytes() if destination.exists() else None) == old_target
    assert (project.revision, project.updated_at) == (old_revision, old_updated_at)


@pytest.mark.parametrize("writer_kind", ("evidence", "cache", "publication"))
@pytest.mark.parametrize("late_error", (False, True))
def test_finally_cleanup_preserves_a_new_file_after_successful_commit(
    tmp_path: Path, writer_kind: str, late_error: bool,
) -> None:
    destination = tmp_path / "target.json"
    if writer_kind == "evidence":
        owner, operation = review_evidence, "rename_no_replace"
    else:
        owner, operation = os, "replace"

    def call() -> None:
        if writer_kind == "evidence":
            review_evidence._write_exact_new(destination, b'{"test":true}\n')
        elif writer_kind == "cache":
            cache_storage._write_json_atomic(destination, {"test": True})
        else:
            album_publication_executor._write_json(destination, {"test": True})

    original = getattr(owner, operation)
    seen: list[Path] = []

    def replace_then_repopulate(source: Path, target: Path) -> None:
        original(source, target)
        source.write_bytes(b"new unrelated file")
        seen.append(source)
        if late_error:
            raise OSError("synthetic failure after commit")

    with patch.object(owner, operation, side_effect=replace_then_repopulate):
        if late_error:
            with pytest.raises((OSError, review_evidence.ReviewEvidenceError)):
                call()
        else:
            call()
    assert destination.is_file()
    assert len(seen) == 1 and seen[0].read_bytes() == b"new unrelated file"


def test_writer_receipt_cannot_adopt_an_identical_foreign_object(tmp_path: Path) -> None:
    owned = tmp_path / "owned.json"
    foreign = tmp_path / "foreign.json"
    payload = b"identical bytes are not file ownership"
    with owned.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        foreign.write_bytes(payload)
        with pytest.raises(OSError, match="cleanup identity"):
            atomic_create.capture_owned_file_receipt(
                foreign, payload, owned_descriptor=handle.fileno()
            )
        receipt = atomic_create.capture_owned_file_receipt(
            owned, payload, owned_descriptor=handle.fileno()
        )
    assert not atomic_create.remove_owned_file_if_present(foreign, receipt)
    assert foreign.read_bytes() == payload
    assert atomic_create.remove_owned_file_if_present(owned, receipt)
    assert not owned.exists()


@pytest.mark.parametrize("writer_kind", ("project", "evidence", "cache", "publication"))
def test_fsync_failure_cleans_only_the_already_receipted_temporary(
    tmp_path: Path, writer_kind: str,
) -> None:
    destination = tmp_path / "target.json"
    owners = {"project": project_io, "evidence": review_evidence,
              "cache": cache_storage, "publication": album_publication_executor}
    writers: dict[str, Callable[[], object]] = {
        "project": lambda: project_io.save_project(_project(), destination),
        "evidence": lambda: review_evidence._write_exact_new(destination, b'{"test":true}\n'),
        "cache": lambda: cache_storage._write_json_atomic(destination, {"test": True}),
        "publication": lambda: album_publication_executor._write_json(
            destination, {"test": True}
        ),
    }
    owner = owners[writer_kind]
    original_capture = owner.capture_owned_file_receipt
    original_fsync = os.fsync
    captured: list[tuple[Path, int]] = []

    def capture(path: Path, payload: bytes, *, owned_descriptor: int | None = None):
        assert owned_descriptor is not None
        receipt = original_capture(path, payload, owned_descriptor=owned_descriptor)
        captured.append((path, owned_descriptor))
        return receipt

    def fail_after_receipt(descriptor: int) -> None:
        if captured and descriptor == captured[-1][1]:
            raise OSError("synthetic fsync failure after ownership capture")
        original_fsync(descriptor)

    with patch.object(owner, "capture_owned_file_receipt", side_effect=capture):
        with patch.object(os, "fsync", side_effect=fail_after_receipt):
            with pytest.raises(OSError, match="synthetic fsync failure"):
                writers[writer_kind]()
    assert len(captured) == 1
    assert not captured[0][0].exists()
    assert not destination.exists()
