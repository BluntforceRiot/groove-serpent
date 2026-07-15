"""Command-line assembly and top-level error handling for Groove Serpent."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .analysis import analyze_audio
from .atomic_create import probe_atomic_no_replace
from .errors import GrooveSerpentError
from .exporter import export_project, suggest_output_directory
from .media import sha256_file
from .models import AnalysisSettings, MAX_TRACKS, resolve_source_path
from .project_io import load_project, load_project_with_sha256, save_project
from .tracklist import Tracklist, load_tracklist


def _configure_output_error_handling() -> None:
    """Keep redirected legacy-codepage output from crashing the CLI."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, TypeError, ValueError):
            # Test doubles and host-owned streams may reject reconfiguration.
            continue


def _default_project_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}.groove.json")


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _album_entry_path(path: Path, *, create_parent: bool = False) -> Path:
    """Canonicalize album ancestors while preserving the final component."""

    from .album import canonical_album_path

    absolute = _absolute_without_resolving(path)
    if create_parent:
        absolute.parent.mkdir(parents=True, exist_ok=True)
    return canonical_album_path(absolute)


def _stored_source_path(input_path: Path, project_path: Path) -> str:
    try:
        return os.path.relpath(input_path.resolve(), project_path.resolve().parent)
    except ValueError:
        return str(input_path.resolve())


def _formats(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _add_metadata_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--artist", default=None)
    parser.add_argument("--album", default=None)
    parser.add_argument("--album-artist", default=None)
    parser.add_argument("--year", default=None)
    parser.add_argument("--genre", default=None)
    parser.add_argument("--side", default=None, help="Vinyl side label, such as A or B")


def _metadata_from_args(
    args: argparse.Namespace, tracklist: Tracklist | None
) -> dict[str, str]:
    metadata = dict(tracklist.metadata) if tracklist else {}
    for attribute in ("artist", "album", "album_artist", "year", "genre", "side"):
        value = getattr(args, attribute, None)
        if value not in (None, ""):
            metadata[attribute] = str(value).strip()
    return metadata


def _settings_from_args(args: argparse.Namespace) -> AnalysisSettings:
    return AnalysisSettings(
        analysis_rate=args.analysis_rate,
        window_ms=args.window_ms,
        smoothing_windows=args.smoothing_windows,
        threshold_margin_db=args.threshold_margin,
        min_gap_seconds=args.min_gap,
        max_gap_seconds=args.max_gap,
        min_track_seconds=args.min_track,
        active_run_seconds=args.active_run,
        lead_in_seconds=args.lead_in,
        tail_seconds=args.tail,
        auto_boundary_score=args.auto_score,
        waveform_points=args.waveform_points,
    )


def _analyze(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser().resolve()
    project_path = (
        Path(args.project).expanduser().resolve()
        if args.project
        else _default_project_path(input_path)
    )
    if project_path == input_path:
        raise GrooveSerpentError(
            "The project path cannot be the source audio file. "
            "Choose a separate .groove.json path."
        )
    existing_project = None
    expected_existing_sha256: str | None = None
    if os.path.lexists(project_path):
        if not args.overwrite:
            raise GrooveSerpentError(
                f"Project already exists: {project_path}\n"
                "Use --overwrite to replace it."
            )
        existing_project, expected_existing_sha256 = load_project_with_sha256(
            project_path
        )

    tracklist = load_tracklist(Path(args.tracklist)) if args.tracklist else None
    expected_count = args.tracks
    if tracklist:
        if expected_count is not None and expected_count != len(tracklist.tracks):
            raise GrooveSerpentError(
                f"--tracks says {expected_count}, but the track list contains "
                f"{len(tracklist.tracks)} tracks."
            )
        expected_count = len(tracklist.tracks)

    if expected_count is not None and not 1 <= expected_count <= MAX_TRACKS:
        raise GrooveSerpentError(
            f"--tracks must be between 1 and {MAX_TRACKS}."
        )

    if existing_project is None:
        try:
            probe_atomic_no_replace(project_path.parent)
        except (OSError, ValueError) as exc:
            raise GrooveSerpentError(
                "The project destination filesystem cannot safely create a new "
                f"project atomically: {exc}"
            ) from exc

    print(f"Analyzing {input_path.name} ...")
    project = analyze_audio(
        input_path,
        stored_source_path=_stored_source_path(input_path, project_path),
        settings=_settings_from_args(args),
        expected_track_count=expected_count,
        track_seeds=tracklist.tracks if tracklist else None,
        metadata=_metadata_from_args(args, tracklist),
    )
    if existing_project is not None:
        project.revision = existing_project.revision
        project.created_at = existing_project.created_at
    save_project(
        project,
        project_path,
        expected_existing_sha256=expected_existing_sha256,
    )

    selected = max(0, len(project.tracks) - 1)
    print(f"Created {project_path}")
    print(
        f"Proposed {len(project.tracks)} track(s) with {selected} internal boundary/boundaries."
    )
    print(
        f"Noise floor {project.analysis.noise_floor_db:.1f} dBFS; "
        f"gap threshold {project.analysis.silence_threshold_db:.1f} dBFS."
    )
    low_confidence = [track for track in project.tracks if track.confidence < 0.45]
    if low_confidence:
        print(
            f"Review recommended: {len(low_confidence)} track(s) depend on low-confidence markers."
        )
    return 0


def _export(args: argparse.Namespace) -> int:
    project_path = Path(args.project).expanduser().resolve()
    project = load_project(project_path)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else suggest_output_directory(project, project_path)
    )
    report = export_project(
        project,
        project_path,
        output_dir,
        formats=_formats(args.formats),
        overwrite=False,
        flac_compression=args.flac_compression,
        aac_bitrate=args.aac_bitrate,
        source_speed_factor=args.source_speed_factor,
        progress=print,
    )
    print(f"Exported {len(report.files)} file(s) to {report.output_directory}")
    print(f"Manifest: {report.manifest_path}")
    return 0


def _review(args: argparse.Namespace) -> int:
    from .review_server import serve_project

    return serve_project(
        Path(args.project).expanduser().resolve(),
        port=args.port,
        open_browser=not args.no_browser,
        endpoint_proposal_path=(
            Path(args.endpoint_proposal).expanduser().resolve()
            if args.endpoint_proposal
            else None
        ),
    )


def _click_scan(args: argparse.Namespace) -> int:
    from .restoration_workflow import scan_project_clicks

    project_path = Path(args.project).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()
    report = scan_project_clicks(
        project_path,
        report_path,
        start_seconds=args.start,
        end_seconds=args.end,
        max_candidates=args.max_candidates,
    )
    summary = report["summary"]
    print(f"Created {report_path}")
    print(
        f"Found {summary['retained']} retained candidate(s); "
        f"{summary['repairable']} are previewable."
    )
    print("Source audio and project were not changed.")
    return 0


def _click_preview(args: argparse.Namespace) -> int:
    from .restoration_workflow import create_click_preview

    project_path = Path(args.project).expanduser().resolve()
    scan_path = Path(args.scan).expanduser().resolve()
    bundle_dir = Path(args.bundle).expanduser().resolve()
    result = create_click_preview(
        project_path,
        scan_path,
        args.candidate,
        bundle_dir,
        context_seconds=args.context,
    )
    print(f"Created A/B preview bundle: {result['bundle_path']}")
    print("Audition before.flac and proposed.flac; approval remains pending.")
    print("Source audio and project were not changed.")
    return 0


def _click_recipe(args: argparse.Namespace) -> int:
    import json

    from .restoration_workflow import create_restoration_recipe

    decisions_path = Path(args.decisions).expanduser().resolve()
    try:
        payload = json.loads(
            decisions_path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GrooveSerpentError(
            f"Restoration decisions JSON is invalid: {exc}"
        ) from exc
    decisions = payload.get("decisions") if isinstance(payload, dict) else payload
    if not isinstance(decisions, list):
        raise GrooveSerpentError(
            "Restoration decisions must be a JSON array or an object with a decisions array."
        )
    recipe_path = Path(args.recipe).expanduser().resolve()
    recipe = create_restoration_recipe(
        Path(args.project).expanduser().resolve(),
        Path(args.scan).expanduser().resolve(),
        decisions,
        recipe_path,
    )
    summary = recipe["summary"]
    print(f"Created restoration recipe: {recipe_path}")
    print(
        f"Decisions: {summary['approved']} approved, {summary['rejected']} rejected, "
        f"{summary['protected']} protected."
    )
    print("Source audio and project were not changed.")
    return 0


def _click_render(args: argparse.Namespace) -> int:
    from .restoration_workflow import render_restored_side

    result = render_restored_side(
        Path(args.project).expanduser().resolve(),
        Path(args.scan).expanduser().resolve(),
        Path(args.recipe).expanduser().resolve(),
        Path(args.bundle).expanduser().resolve(),
    )
    print(f"Created restored-side bundle: {result['bundle_path']}")
    print(f"Applied {len(result['repairs'])} explicitly approved repair(s).")
    print("Source audio and project were not changed.")
    return 0


def _info(args: argparse.Namespace) -> int:
    import json

    project_path = Path(args.project).expanduser().resolve()
    project, project_sha256 = load_project_with_sha256(project_path)
    source_path = resolve_source_path(project, project_path).resolve()
    source_verified = bool(project.source.sha256) and (
        source_path.stat().st_size == project.source.size_bytes
        and sha256_file(source_path).lower() == project.source.sha256.lower()
    )
    if args.json:
        payload = {
            "schema": "groove-serpent.project-info/1",
            "project": str(project_path),
            "project_sha256": project_sha256,
            "revision": project.revision,
            "source": {
                "path": str(source_path),
                "filename": project.source.filename,
                "sha256": project.source.sha256,
                "verified": source_verified,
                "sample_rate": project.source.sample_rate,
                "channels": project.source.channels,
                "sample_count": project.source.sample_count,
                "duration_seconds": project.source.duration_seconds,
                "codec": project.source.codec_name,
            },
            "metadata": dict(project.metadata),
            "tracks": [
                {
                    "number": track.number,
                    "title": track.title,
                    "artist": track.artist,
                    "side": track.side,
                    "start_sample": track.start_sample,
                    "end_sample": track.end_sample,
                    "duration_seconds": track.duration_seconds,
                    "confidence": track.confidence,
                }
                for track in project.tracks
            ],
            "history_entries": len(project.edit_history),
            "checkpoints": len(project.checkpoints),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    print(f"Project: {project_path}")
    print(f"Revision: {project.revision} | SHA-256: {project_sha256}")
    print(
        f"Source: {project.source.filename} | {project.source.duration_seconds:.2f}s | "
        f"{project.source.sample_rate} Hz | {project.source.channels} channel(s) | "
        f"{project.source.codec_name}"
    )
    print(f"Tracks: {len(project.tracks)}")
    for track in project.tracks:
        print(
            f"  {track.number:02d}  {track.start_seconds:9.3f} - {track.end_seconds:9.3f}  "
            f"{track.duration_seconds:8.3f}s  {track.title}  confidence={track.confidence:.2f}"
        )
    return 0


def _speed_estimate(args: argparse.Namespace) -> int:
    """Create review-only fixed-speed evidence without changing the project."""

    import json

    from .speed_estimation import estimate_speed, write_speed_proposal

    proposal = estimate_speed(
        Path(args.project),
        Path(args.tracklist),
        boundary_review_path=(
            Path(args.boundary_review) if args.boundary_review else None
        ),
    )
    if args.output:
        write_speed_proposal(proposal, Path(args.output))
    if args.json:
        print(
            json.dumps(
                proposal,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            )
        )
        return 0
    if args.output:
        print(f"Created sealed speed proposal: {Path(args.output).expanduser().absolute()}")
    estimate = proposal["estimate"]
    if estimate["status"] == "proposed":
        print(
            f"Proposed source speed factor {estimate['proposed_factor']:.9f} "
            f"({estimate['confidence']} confidence)."
        )
    else:
        reasons = proposal["diagnostics"]["abstention_reasons"]
        print(f"Abstained: {', '.join(reasons)}")
    print("No correction was applied, no project state was saved, and approval remains pending.")
    return 0


def _speed_review_boundaries(args: argparse.Namespace) -> int:
    """Persist an explicit, non-approving boundary-review attestation."""

    from .speed_estimation import create_boundary_review_evidence

    evidence = create_boundary_review_evidence(
        Path(args.project),
        Path(args.output),
        confirm_all_track_boundaries_reviewed=args.confirm_all_boundaries_reviewed,
        confirm_review_independent_of_reference_durations=(
            args.confirm_review_independent_of_reference_durations
        ),
    )
    output = Path(args.output).expanduser().absolute()
    print(f"Created exact boundary-review evidence: {output}")
    print(f"Raw SHA-256: {evidence.raw_sha256}")
    print(
        "This receipt records audio-and-visual boundary review only; "
        "speed-correction approval remains not granted."
    )
    return 0


def _endpoint_scope_argument(value: str) -> object:
    """Parse one exact LABEL|START_SAMPLE|END_SAMPLE scope for argparse."""

    from .endpoint_proposals import EndpointScope

    parts = value.split("|")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "Endpoint scope must be LABEL|START_SAMPLE|END_SAMPLE."
        )
    label, start_text, end_text = parts
    if not label or label != label.strip():
        raise argparse.ArgumentTypeError(
            "Endpoint scope label must be non-empty trimmed text."
        )
    for rendered, field in ((start_text, "start"), (end_text, "end")):
        if not rendered or not rendered.isascii() or not rendered.isdecimal():
            raise argparse.ArgumentTypeError(
                f"Endpoint scope {field} must be an unsigned decimal sample integer."
            )
    return EndpointScope(label, int(start_text), int(end_text))


def _print_endpoint_summary(proposal: dict[str, object]) -> None:
    scopes = proposal["scopes"]
    if not isinstance(scopes, list):
        raise GrooveSerpentError("Endpoint proposal scopes are invalid.")
    for scope in scopes:
        if not isinstance(scope, dict):
            raise GrooveSerpentError("Endpoint proposal scope is invalid.")
        label = scope["label"]
        status = scope["status"]
        if status == "proposed":
            print(
                f"{label}: proposed [{scope['proposed_music_start_sample']}, "
                f"{scope['proposed_music_end_sample_exclusive']}) "
                f"at {float(scope['confidence']) * 100:.1f}% confidence"
            )
        else:
            reasons = scope["reasons"]
            rendered_reasons = ", ".join(str(item) for item in reasons)
            print(f"{label}: abstained ({rendered_reasons})")


def _endpoints_propose(args: argparse.Namespace) -> int:
    """Create one sealed review-only endpoint proposal without editing markers."""

    import json

    from .endpoint_proposals import (
        EndpointScope,
        analyze_endpoint_proposals,
        write_endpoint_proposal_document,
    )

    project_path = Path(args.project).expanduser().resolve()
    scopes = tuple(args.scope or ())
    if not scopes:
        project = load_project(project_path)
        sample_count = project.source.sample_count
        if type(sample_count) is not int or sample_count <= 0:
            raise GrooveSerpentError(
                "Endpoint analysis requires an exact positive source sample count."
            )
        scopes = (EndpointScope("Side", 0, sample_count),)
    proposal = analyze_endpoint_proposals(project_path, scopes)
    output = Path(args.output).expanduser().absolute()
    receipt = write_endpoint_proposal_document(proposal, output)
    if args.json:
        print(
            json.dumps(
                proposal,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            )
        )
        return 0
    print(f"Created sealed endpoint proposal: {output}")
    print(f"Proposal identity: {proposal['proposal_sha256']}")
    print(f"File SHA-256: {receipt.sha256}")
    _print_endpoint_summary(proposal)
    print(
        "No endpoint was applied. Inspect waveform, spectrum, and audio before "
        "an explicit decision."
    )
    return 0


def _endpoints_inspect(args: argparse.Namespace) -> int:
    """Strictly load and inspect one sealed endpoint proposal."""

    import json

    from .endpoint_proposals import load_endpoint_proposal_document

    proposal = load_endpoint_proposal_document(Path(args.proposal))
    if args.json:
        print(
            json.dumps(
                proposal,
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
                allow_nan=False,
            )
        )
        return 0
    print(f"Endpoint proposal: {proposal['proposal_sha256']}")
    project = proposal["project"]
    if not isinstance(project, dict):
        raise GrooveSerpentError("Endpoint proposal project identity is invalid.")
    print(
        f"Project revision {project['revision']} | SHA-256 {project['sha256']}"
    )
    _print_endpoint_summary(proposal)
    print("Authority: review required; automatic application is forbidden.")
    return 0


def _project_migrate(args: argparse.Namespace) -> int:
    import json

    from .project_migration import migrate_project_file

    result = migrate_project_file(Path(args.project))
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
            )
        )
        return 0
    if result.status == "current":
        print(
            f"Project is already schema {result.target_schema}; no files were changed: "
            f"{result.project}"
        )
        return 0
    print(
        f"Project migration {result.status}: schema {result.original_schema} -> "
        f"{result.target_schema}"
    )
    print(f"Project: {result.project}")
    print(f"Exact backup: {result.backup}")
    print(f"Committed receipt: {result.receipt}")
    print(f"Original SHA-256: {result.original_sha256}")
    print(f"Migrated SHA-256: {result.migrated_sha256}")
    return 0


def _album_create(args: argparse.Namespace) -> int:
    from .album import (
        AlbumProject,
        artwork_for_album_path,
        load_album_project_with_sha256,
        parse_album_side_spec,
        save_album_project,
    )

    album_path = _album_entry_path(
        Path(args.album_project),
        create_parent=True,
    )
    existing_album = None
    expected_existing_sha256: str | None = None
    if os.path.lexists(album_path):
        if not args.overwrite:
            raise GrooveSerpentError(
                f"Album project already exists: {album_path}. "
                "Use --overwrite to replace it."
            )
        existing_album, expected_existing_sha256 = (
            load_album_project_with_sha256(album_path)
        )
    sides = [
        parse_album_side_spec(value, index, album_path)
        for index, value in enumerate(args.side, start=1)
    ]
    metadata: dict[str, str] = {}
    for attribute in (
        "artist",
        "album",
        "album_artist",
        "year",
        "genre",
        "label",
        "catalog_number",
        "barcode",
        "musicbrainz_release_id",
        "musicbrainz_release_group_id",
    ):
        value = getattr(args, attribute, None)
        if value not in (None, ""):
            metadata[attribute] = str(value).strip()
    artwork = (
        artwork_for_album_path(album_path, Path(args.artwork)) if args.artwork else None
    )
    album = AlbumProject(metadata=metadata, sides=sides, artwork=artwork)
    if existing_album is not None:
        album.revision = existing_album.revision
        album.created_at = existing_album.created_at
    save_album_project(
        album,
        album_path,
        overwrite=args.overwrite,
        expected_existing_sha256=expected_existing_sha256,
    )
    print(f"Created album project: {album_path}")
    print(f"Sides: {len(album.sides)}")
    for side in album.sides:
        print(
            f"  {side.order:02d}  Side {side.label}  {side.project}  "
            f"speed={side.effective_speed_factor:.9f} ({side.speed.mode})  "
            f"pinned revision={side.pin.project_revision if side.pin else 'none'}"
        )
    return 0


def _album_inspect(args: argparse.Namespace) -> int:
    import json

    from .album import inspect_album_project, load_album_project

    album_path = _album_entry_path(Path(args.album_project))
    album = load_album_project(album_path)
    receipt = inspect_album_project(album, album_path)
    if args.json:
        print(json.dumps(receipt, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    title = receipt["metadata"].get("album") or receipt["metadata"].get(
        "title", album_path.stem
    )
    print(f"Album project: {album_path}")
    print(f"Album: {title}")
    print(f"Tracks: {receipt['total_tracks']} across {len(receipt['sides'])} side(s)")
    print(f"Ready for export: {'yes' if receipt['ready_for_export'] else 'no'}")
    for side in receipt["sides"]:
        print(
            f"  {side['order']:02d}  Side {side['label']}  "
            f"{side['tracks']} track(s)  speed={side['effective_speed_factor']:.9f}  "
            f"mode={side['speed_mode']}  "
            f"status={'pinned' if side['ready_for_export'] else 'changed/unpinned'}  "
            f"{side['project']}"
        )
        for reason in side["drift"]:
            print(f"      ! {reason}")
    return 0


def _album_review(args: argparse.Namespace) -> int:
    from .album_review_server import serve_album_project

    return serve_album_project(
        _album_entry_path(Path(args.album_project)),
        port=args.port,
        open_browser=not args.no_browser,
    )


def _album_repin(args: argparse.Namespace) -> int:
    from .album import (
        load_album_project_with_sha256,
        repin_album_sides,
        save_album_project,
    )

    album_path = _album_entry_path(Path(args.album_project))
    album, album_sha256 = load_album_project_with_sha256(album_path)
    labels = None if args.all else args.side
    repinned = repin_album_sides(album, album_path, labels)
    save_album_project(
        album,
        album_path,
        overwrite=True,
        expected_existing_sha256=album_sha256,
    )
    print(f"Repinned {len(repinned)} side(s) in {album_path}")
    for side in album.sides:
        if side.label in repinned and side.pin is not None:
            print(
                f"  Side {side.label}: revision={side.pin.project_revision}  "
                f"project={side.pin.project_sha256}  state={side.pin.editable_state_sha256}  "
                f"speed={side.pin.speed_state_sha256}"
            )
    return 0


def _album_migrate(args: argparse.Namespace) -> int:
    import json

    from .album_migration import migrate_album_file

    result = migrate_album_file(_album_entry_path(Path(args.album_project)))
    if args.json:
        print(
            json.dumps(
                result.to_dict(),
                indent=2,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
            )
        )
        return 0
    if result.status == "current":
        print(
            f"Album is already {result.target_schema}; no files were changed: "
            f"{result.album}"
        )
        return 0
    print(
        f"Album migration {result.status}: {result.original_schema} -> "
        f"{result.target_schema}"
    )
    print(f"Album: {result.album}")
    print(f"Exact backup: {result.backup}")
    print(f"Committed receipt: {result.receipt}")
    print(f"Original SHA-256: {result.original_sha256}")
    print(f"Migrated SHA-256: {result.migrated_sha256}")
    return 0


def _album_export(args: argparse.Namespace) -> int:
    from .album import export_album, load_album_project, suggest_album_output_directory

    album_path = _album_entry_path(Path(args.album_project))
    album = load_album_project(album_path)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else suggest_album_output_directory(album, album_path)
    )
    report = export_album(
        album,
        album_path,
        output_dir,
        formats=_formats(args.formats),
        flac_compression=args.flac_compression,
        aac_bitrate=args.aac_bitrate,
        progress=print,
    )
    print(f"Exported album to {report.output_directory}")
    print(f"Manifest: {report.manifest_path}")
    print(f"CUE: {report.cue_path}")
    print(f"Exact chapters: {report.chapters_path}")
    return 0


def _album_publication_plan(args: argparse.Namespace) -> int:
    from .album_publication_builder import build_album_publication_plan

    plan_path = _absolute_without_resolving(Path(args.plan))
    plan = build_album_publication_plan(
        _album_entry_path(Path(args.album_project)),
        plan_path,
        selected_profiles=_formats(args.profiles),
        restoration_mode=args.restoration,
        flac_compression=args.flac_compression,
        aac_bitrate_kbps=args.aac_bitrate_kbps,
    )
    print(f"Created immutable publication plan: {plan_path}")
    print(f"Plan SHA-256: {plan.plan_sha256}")
    print(f"Profiles: {', '.join(plan.selected_profiles)}")
    print("No audio was rendered and no existing path was replaced.")
    return 0


def _album_publication_preflight(args: argparse.Namespace) -> int:
    import json
    from dataclasses import asdict

    from .album_publication_executor import preflight_album_publication_plan

    report = preflight_album_publication_plan(Path(args.plan))
    payload = {
        "schema": "groove-serpent.album-publication-preflight/1",
        **asdict(report),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        print(f"Publication plan is current: {report.plan_sha256}")
        print(f"Album SHA-256: {report.album_sha256}")
        print(f"Sides: {report.side_count}")
        print(f"Profiles: {', '.join(report.selected_profiles)}")
        print("Preflight made no files and changed no project state.")
    return 0


def _album_publication_execute(args: argparse.Namespace) -> int:
    from .album_publication_executor import execute_album_publication_plan

    report = execute_album_publication_plan(
        Path(args.plan),
        Path(args.output_directory),
        progress=print,
    )
    print(f"Published verified album directory: {report.output_directory}")
    print(f"Manifest: {report.manifest_path}")
    print(f"Plan SHA-256: {report.plan_sha256}")
    print(f"Verified artifacts: {len(report.artifacts)}")
    return 0


def _album_publication_verify(args: argparse.Namespace) -> int:
    import json
    from dataclasses import asdict

    from .album_publication_durability import verify_album_publication

    report = verify_album_publication(Path(args.publication_directory))
    payload = {
        "schema": "groove-serpent.album-publication-verification-report/1",
        **asdict(report),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=True, allow_nan=False))
    elif report.ok:
        print(f"Publication is current and verified: {report.publication_directory}")
        print(f"Manifest SHA-256: {report.manifest_sha256}")
        print(f"Journal SHA-256: {report.journal_sha256}")
        print(f"Verified artifacts: {report.artifact_count}")
    else:
        print(f"Publication verification failed: {report.publication_directory}")
        for mismatch in report.mismatches:
            print(f"  ! {mismatch.code}: {mismatch.message}")
    return 0 if report.ok else 2


def _album_publication_replay(args: argparse.Namespace) -> int:
    import json
    from dataclasses import asdict

    from .album_publication_durability import replay_album_publication

    report = replay_album_publication(
        Path(args.publication_directory),
        Path(args.replay_output_directory),
        plan_path=Path(args.plan),
        progress=None if args.json else print,
    )
    payload = {
        "schema": "groove-serpent.album-publication-replay-report/1",
        **asdict(report),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=True, allow_nan=False))
    elif report.ok:
        print(f"Replay matches the verified publication: {report.replay_directory}")
    else:
        print(f"Replay differs: {report.replay_directory}")
        for mismatch in report.mismatches:
            print(f"  ! {mismatch.code}: {mismatch.message}")
    return 0 if report.ok else 2


def _album_publication_orphans(args: argparse.Namespace) -> int:
    import json
    from dataclasses import asdict

    from .album_publication_durability import inventory_album_publication_orphans

    inventory = inventory_album_publication_orphans(Path(args.parent_directory))
    payload = {
        "schema": "groove-serpent.album-publication-orphan-inventory/1",
        **asdict(inventory),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    print(f"Publication recovery parent: {inventory.parent_directory}")
    if not inventory.orphans:
        print("No recognized publication stages or quarantines found.")
        return 0
    for orphan in inventory.orphans:
        ownership = "owned" if orphan.owned else "untrusted"
        print(f"  {orphan.kind:10s} {ownership:9s} {orphan.path}")
        if orphan.issue:
            print(f"      ! {orphan.issue}")
        elif orphan.journal_sha256:
            print(f"      journal={orphan.journal_sha256} state={orphan.state}")
    if inventory.truncated:
        print("Inventory was truncated; narrow the recovery parent before acting.")
    return 0


def _album_publication_recover(args: argparse.Namespace) -> int:
    import json
    from dataclasses import asdict

    from .album_publication_durability import (
        RecoveryDirectoryIdentity,
        recover_album_publication_orphan,
    )

    if args.yes is not True:
        raise GrooveSerpentError(
            "Publication recovery requires --yes after reviewing an exact orphan receipt."
        )
    identity = RecoveryDirectoryIdentity(
        device=args.expected_device,
        inode=args.expected_inode,
        file_type=args.expected_file_type,
        birth_ns=args.expected_birth_ns,
        file_attributes=args.expected_file_attributes,
    )
    report = recover_album_publication_orphan(
        Path(args.orphan),
        expected_identity=identity,
        expected_journal_sha256=args.expected_journal_sha256,
        action=args.action,
    )
    payload = {
        "schema": "groove-serpent.album-publication-recovery-report/1",
        **asdict(report),
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=True, allow_nan=False))
    elif report.removed:
        print(f"Removed exact owned publication orphan: {report.original_path}")
    else:
        print(f"Quarantined exact owned publication orphan: {report.resulting_path}")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    import json

    from .doctor import build_doctor_report

    destination_path = (
        Path(args.path).expanduser() if args.path is not None else None
    )
    report = build_doctor_report(destination_path=destination_path)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False))
        return 0 if report["ready"] is True else 2
    print(f"Groove Serpent {report['groove_serpent_version']}")
    for check in report["checks"]:
        required = "required" if check["required"] else "optional"
        print(
            f"{check['capability']}: {check['status']} ({required}) - "
            f"{check['message']}"
        )
        if check["version"]:
            print(f"  {check['version']}")
    if report["ready"] is True:
        print("Required capabilities are ready.")
        return 0
    print("One or more required capabilities are unavailable.")
    return 2


def _cache_root_from_args(args: argparse.Namespace) -> Path:
    from .cache_storage import resolve_cache_root

    project = (
        Path(args.project).expanduser().resolve()
        if getattr(args, "project", None)
        else None
    )
    configured = getattr(args, "cache_dir", None)
    return resolve_cache_root(project, configured)


def _cache_status(args: argparse.Namespace) -> int:
    import json

    from .cache_storage import inspect_snapshot_cache

    report = inspect_snapshot_cache(_cache_root_from_args(args))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, allow_nan=False))
        return 0
    print(f"Snapshot cache: {report.root}")
    if not report.entries:
        print("No snapshot leases found.")
        return 0
    for entry in report.entries:
        state = entry.metadata.lease_state if entry.metadata else "invalid"
        print(
            f"  {entry.directory.name}: state={state}, owner={entry.owner_status}, "
            f"bytes={entry.bytes_on_disk}, reclaimable={'yes' if entry.reclaimable else 'no'}"
        )
        if entry.problem:
            print(f"      ! {entry.problem}")
    print(
        f"Total: {len(report.entries)} lease(s), {report.total_bytes} bytes; "
        f"reclaimable: {report.reclaimable_bytes} bytes."
    )
    return 0


def _cache_clean(args: argparse.Namespace) -> int:
    import json

    from .cache_storage import cleanup_stale_snapshots

    report = cleanup_stale_snapshots(_cache_root_from_args(args))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, allow_nan=False))
        return 0
    print(f"Snapshot cache: {report.root}")
    print(
        f"Removed {len(report.removed)} stale snapshot lease(s), "
        f"reclaiming {report.removed_bytes} bytes."
    )
    if report.skipped_live or report.skipped_unknown:
        print(
            f"Left untouched: {report.skipped_live} live, "
            f"{report.skipped_unknown} unknown lease(s)."
        )
    return 0


def _review_evidence_root_from_args(args: argparse.Namespace) -> Path:
    from .review_evidence import resolve_review_evidence_root

    return resolve_review_evidence_root(args.root)


def _review_evidence_status(args: argparse.Namespace) -> int:
    import json

    from .review_evidence import inspect_review_evidence

    status = inspect_review_evidence(_review_evidence_root_from_args(args))
    if args.json:
        print(json.dumps(status.to_dict(), indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    print(f"Review evidence: {status.root}")
    print(f"Collection enabled: {'yes' if status.enabled else 'no'}")
    print(f"Verified records: {status.record_count}")
    print("Authority: evidence only; records can never approve or apply an action.")
    return 0


def _review_evidence_set_enabled(args: argparse.Namespace) -> int:
    import json

    from .review_evidence import set_review_evidence_enabled

    enabled = args.evidence_command == "enable"
    status = set_review_evidence_enabled(_review_evidence_root_from_args(args), enabled)
    if args.json:
        print(json.dumps(status.to_dict(), indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    state = "enabled" if enabled else "disabled"
    print(f"Review-evidence collection {state}: {status.root}")
    print("Existing records were not changed.")
    return 0


def _review_evidence_list(args: argparse.Namespace) -> int:
    import json

    from .review_evidence import list_review_evidence

    records = list_review_evidence(_review_evidence_root_from_args(args))
    summaries = [record.summary_dict() for record in records]
    if args.json:
        print(json.dumps(summaries, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    if not records:
        print("No review-evidence records found.")
        return 0
    for record in records:
        print(
            f"{record.record_sha256}  {record.category}  "
            f"{record.outcome}  {record.recorded_at}"
        )
    return 0


def _review_evidence_export(args: argparse.Namespace) -> int:
    import json

    from .review_evidence import export_review_evidence, load_review_evidence_export

    output = Path(args.output)
    export_sha256 = export_review_evidence(_review_evidence_root_from_args(args), output)
    payload = load_review_evidence_export(output)
    result = {
        "schema": payload["schema"],
        "output": str(output.expanduser().absolute()),
        "sha256": export_sha256,
        "record_count": payload["record_count"],
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    print(f"Exported {result['record_count']} review-evidence record(s): {result['output']}")
    print(f"SHA-256: {export_sha256}")
    return 0


def _review_evidence_evaluate(args: argparse.Namespace) -> int:
    import json

    from .review_evidence_evaluation import (
        ConfigComparison,
        evaluate_review_evidence_export,
        load_review_evidence_evaluation,
        write_review_evidence_evaluation,
    )

    baseline = args.baseline_config_sha256
    candidate = args.candidate_config_sha256
    if (baseline is None) != (candidate is None):
        raise ValueError(
            "Both --baseline-config-sha256 and --candidate-config-sha256 are required "
            "for a paired comparison."
        )
    comparison = (
        ConfigComparison(baseline, candidate)
        if baseline is not None and candidate is not None
        else None
    )
    export_path = Path(args.export)
    output_path = Path(args.output) if args.output is not None else None
    if output_path is None:
        receipt = evaluate_review_evidence_export(
            export_path, comparison=comparison
        )
        receipt_sha256 = None
    else:
        receipt_sha256 = write_review_evidence_evaluation(
            export_path, output_path, comparison=comparison
        )
        receipt = load_review_evidence_evaluation(output_path, export_path)
    if args.json:
        if output_path is None:
            print(json.dumps(receipt, indent=2, ensure_ascii=True, allow_nan=False))
        else:
            print(
                json.dumps(
                    {
                        "schema": receipt["schema"],
                        "output": str(output_path.expanduser().absolute()),
                        "sha256": receipt_sha256,
                        "corpus_export_sha256": receipt["corpus_export"]["sha256"],
                        "record_count": receipt["data_sufficiency"]["record_count"],
                        "abstained": receipt["data_sufficiency"]["abstained"],
                    },
                    indent=2,
                    ensure_ascii=True,
                    allow_nan=False,
                )
            )
        return 0
    sufficiency = receipt["data_sufficiency"]
    print(
        "Review-evidence evaluation: "
        f"{sufficiency['record_count']} record(s), "
        f"{sufficiency['source_count']} source group(s)."
    )
    print(
        "Evaluation split: "
        f"{sufficiency['evaluation_record_count']} record(s) from "
        f"{sufficiency['evaluation_source_count']} source group(s)."
    )
    for metric in receipt["metrics"]:
        status = "sufficient" if metric["data_sufficient"] else "abstained"
        print(
            f"{metric['id']}: {status}; "
            f"{metric['eligible_record_count']} typed record(s), "
            f"{metric['source_count']} source(s)."
        )
    if receipt["comparison"] is not None:
        status = (
            "has a sufficient paired metric"
            if receipt["comparison"]["data_sufficient_for_any_metric"]
            else "abstained"
        )
        print(f"Paired config comparison: {status}.")
    print("Authority: descriptive only; cannot approve, apply, or change defaults.")
    if output_path is not None:
        print(f"Receipt: {output_path.expanduser().absolute()}")
        print(f"SHA-256: {receipt_sha256}")
    return 0


def _review_evidence_delete(args: argparse.Namespace) -> int:
    import json

    from .review_evidence import delete_review_evidence

    deleted = delete_review_evidence(
        _review_evidence_root_from_args(args),
        args.record_sha256,
        expected_record_sha256=args.expected_record_sha256,
        deliberate=args.yes,
    )
    result = {
        "deleted": True,
        "record_sha256": deleted.record_sha256,
        "category": deleted.category,
        "outcome": deleted.outcome,
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
        return 0
    print(f"Deleted exact review-evidence record: {deleted.record_sha256}")
    return 0


def _add_review_evidence_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        help=(
            "Private evidence root; defaults to GROOVE_SERPENT_REVIEW_EVIDENCE_DIR "
            "or per-user local storage"
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit strict JSON")


def _add_cache_location_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--project",
        help="Project whose local .groove-serpent cache should be inspected",
    )
    parser.add_argument(
        "--cache-dir",
        help="Explicit snapshot cache root (overrides project and environment)",
    )
    parser.add_argument("--json", action="store_true", help="Emit strict JSON")


def _continuous_reference_argument(value: str) -> object:
    """Parse LABEL|ROLE|START_SAMPLE|END_SAMPLE without hidden defaults."""

    from .continuous_preview_workflow import ReviewedNoiseReference

    parts = value.split("|")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "Noise reference must be LABEL|ROLE|START_SAMPLE|END_SAMPLE."
        )
    label, role, start_text, end_text = parts
    if not label or label != label.strip():
        raise argparse.ArgumentTypeError("Noise-reference label must be trimmed text.")
    if role not in {"lead_in", "lead_out", "inter_track", "user_selected"}:
        raise argparse.ArgumentTypeError("Noise-reference role is unsupported.")
    if any(
        not item or not item.isascii() or not item.isdecimal()
        for item in (start_text, end_text)
    ):
        raise argparse.ArgumentTypeError("Noise-reference bounds must be unsigned integers.")
    return ReviewedNoiseReference(
        label=label,
        role=role,  # type: ignore[arg-type]
        start_sample=int(start_text),
        end_sample_exclusive=int(end_text),
        owner_attested_noise_only=True,
    )


def _continuous_context(args: argparse.Namespace) -> int:
    import json

    from .continuous_preview_workflow import (
        current_continuous_preview_context,
        write_continuous_expected_context,
    )

    context = current_continuous_preview_context(Path(args.project), args.kind)
    digest = write_continuous_expected_context(context, Path(args.output))
    if args.json:
        print(json.dumps(context, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        print(f"Wrote exact continuous-preview context: {Path(args.output).absolute()}")
        print(f"File SHA-256: {digest}")
        print(f"Context SHA-256: {context['context_sha256']}")
    return 0


def _continuous_propose(args: argparse.Namespace) -> int:
    import json

    from .continuous_preview_workflow import (
        load_continuous_expected_context,
        propose_continuous_preview,
    )

    if not args.owner_reviewed_scope or not args.owner_reviewed_references:
        raise GrooveSerpentError(
            "Proposal creation requires both explicit owner review acknowledgements."
        )
    output, proposal = propose_continuous_preview(
        Path(args.project),
        kind=args.kind,
        start_sample=args.start_sample,
        end_sample_exclusive=args.end_sample,
        references=tuple(args.reference),
        expected_context=load_continuous_expected_context(Path(args.expected_context)),
    )
    result = {
        "path": str(output),
        "proposal_sha256": proposal["proposal_sha256"],
        "status": proposal["status"],
        "proposal": proposal,
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        print(f"Persistent {args.kind} proposal: {output}")
        print(f"Status: {proposal['status']}")
        print(f"Proposal SHA-256: {proposal['proposal_sha256']}")
        print("Authority: review-only; no project/source change and no automatic application.")
    return 0


def _continuous_attest(args: argparse.Namespace) -> int:
    import json

    from .continuous_preview_workflow import (
        CONTINUOUS_REVIEW_ACKNOWLEDGEMENT,
        continuous_attestation_template,
        load_continuous_proposal,
        write_continuous_attestation,
    )

    if (
        not args.owner_reviewed_scope
        or not args.owner_reviewed_references
        or not args.acknowledge_limited_authority
    ):
        raise GrooveSerpentError(
            "Attestation requires exact scope/reference review and "
            "limited-authority acknowledgement."
        )
    proposal = load_continuous_proposal(Path(args.proposal))
    attestation = continuous_attestation_template(proposal)
    attestation["attestation_token"] = args.attestation_token
    attestation["acknowledgement"] = CONTINUOUS_REVIEW_ACKNOWLEDGEMENT
    digest = write_continuous_attestation(
        attestation, proposal, Path(args.output)
    )
    if args.json:
        print(json.dumps(attestation, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        print(f"Wrote exact preview-request attestation: {Path(args.output).absolute()}")
        print(f"File SHA-256: {digest}")
        print("This requests an audition; it does not prove listening or approval.")
    return 0


def _continuous_preview(args: argparse.Namespace) -> int:
    import json

    from .continuous_preview_workflow import (
        load_continuous_attestation,
        load_continuous_proposal,
        render_continuous_preview,
    )

    proposal = load_continuous_proposal(Path(args.proposal))
    attestation = load_continuous_attestation(Path(args.attestation), proposal)
    bundle, receipt = render_continuous_preview(
        Path(args.project), proposal, attestation
    )
    result = {
        "bundle": str(bundle),
        "receipt_sha256": receipt["receipt_sha256"],
        "audio": receipt["audio"],
        "authority": receipt["authority"],
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        print(f"Created persistent Original/Proposed/Removed bundle: {bundle}")
        print(f"Receipt SHA-256: {receipt['receipt_sha256']}")
        print("Authority: audition preview only; nothing was applied or published.")
    return 0


def _continuous_reject(args: argparse.Namespace) -> int:
    import json

    from .continuous_preview_workflow import (
        load_continuous_proposal,
        reject_continuous_proposal,
    )

    proposal = load_continuous_proposal(Path(args.proposal))
    output, decision = reject_continuous_proposal(
        Path(args.project), proposal, reason=args.reason
    )
    result = {
        "path": str(output),
        "decision_sha256": decision["decision_sha256"],
        "decision": decision,
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        print(f"Recorded exact non-mutating rejection: {output}")
        print(f"Decision SHA-256: {decision['decision_sha256']}")
    return 0


def _continuous_catalog(args: argparse.Namespace) -> int:
    import json

    from .continuous_preview_workflow import discover_continuous_preview_catalog

    catalog = discover_continuous_preview_catalog(Path(args.project))
    if args.json:
        print(json.dumps(catalog, indent=2, ensure_ascii=True, allow_nan=False))
    else:
        summary = catalog["summary"]
        print(
            "Continuous-preview catalog: "
            f"{summary['current']} current, {summary['stale']} stale, "
            f"{summary['invalid']} invalid."
        )
        for entry in catalog["entries"]:
            print(
                f"{entry['artifact_kind']} {entry['kind']} {entry['status']} "
                f"{entry['identity_sha256']}"
            )
    return 0


def _continuous_open(args: argparse.Namespace) -> int:
    import json

    from .continuous_preview_workflow import find_current_continuous_artifact

    entry = find_current_continuous_artifact(
        Path(args.project),
        artifact_kind=args.artifact_kind,
        identity_sha256=args.sha256,
    )
    print(json.dumps(entry, indent=2, ensure_ascii=True, allow_nan=False))
    return 0


def _add_analysis_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("input", help="Long source recording, preferably FLAC")
    parser.add_argument("--project", help="Output .groove.json project path")
    parser.add_argument("--tracklist", help="JSON or text track list")
    parser.add_argument(
        "--tracks", type=int, help="Expected number of tracks on this side"
    )
    parser.add_argument("--analysis-rate", type=int, default=8_000)
    parser.add_argument("--window-ms", type=int, default=50)
    parser.add_argument("--smoothing-windows", type=int, default=5)
    parser.add_argument("--threshold-margin", type=float, default=6.0)
    parser.add_argument("--min-gap", type=float, default=0.75)
    parser.add_argument("--max-gap", type=float, default=15.0)
    parser.add_argument("--min-track", type=float, default=30.0)
    parser.add_argument("--active-run", type=float, default=0.45)
    parser.add_argument("--lead-in", type=float, default=8.0)
    parser.add_argument("--tail", type=float, default=20.0)
    parser.add_argument("--auto-score", type=float, default=0.55)
    parser.add_argument("--waveform-points", type=int, default=4_000)
    parser.add_argument("--overwrite", action="store_true")
    _add_metadata_arguments(parser)


def build_parser() -> argparse.ArgumentParser:
    """Build the complete, side-effect-free Groove Serpent argument parser."""

    parser = argparse.ArgumentParser(
        prog="groove-serpent",
        description="Split long vinyl-side recordings into tagged FLAC and AAC/M4A tracks.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser(
        "analyze", help="Detect cuts and create a project"
    )
    _add_analysis_arguments(analyze_parser)
    analyze_parser.set_defaults(handler=_analyze)

    review_parser = subparsers.add_parser(
        "review", help="Review markers in a local browser"
    )
    review_parser.add_argument("project")
    review_parser.add_argument("--port", type=int, default=0)
    review_parser.add_argument("--no-browser", action="store_true")
    review_parser.add_argument(
        "--endpoint-proposal",
        help=(
            "Strictly load one sealed current proposal at server startup; stale or "
            "abstained evidence is refused"
        ),
    )
    review_parser.set_defaults(handler=_review)

    click_scan_parser = subparsers.add_parser(
        "click-scan",
        help="Scan source PCM for review-only click candidates",
    )
    click_scan_parser.add_argument("project")
    click_scan_parser.add_argument("--report", required=True)
    click_scan_parser.add_argument("--start", type=float)
    click_scan_parser.add_argument("--end", type=float)
    click_scan_parser.add_argument("--max-candidates", type=int, default=500)
    click_scan_parser.set_defaults(handler=_click_scan)

    click_preview_parser = subparsers.add_parser(
        "click-preview",
        help="Create a lossless A/B preview for one short click event",
    )
    click_preview_parser.add_argument("project")
    click_preview_parser.add_argument("scan")
    click_preview_parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        help="Candidate ID; repeat for channel-specific windows in one event",
    )
    click_preview_parser.add_argument("--bundle", required=True)
    click_preview_parser.add_argument("--context", type=float, default=2.0)
    click_preview_parser.set_defaults(handler=_click_preview)

    click_recipe_parser = subparsers.add_parser(
        "click-recipe",
        help="Bind an explicit approve/reject/protect decision to every click candidate",
    )
    click_recipe_parser.add_argument("project")
    click_recipe_parser.add_argument("scan")
    click_recipe_parser.add_argument(
        "--decisions", required=True, help="Strict decisions JSON"
    )
    click_recipe_parser.add_argument("--recipe", required=True)
    click_recipe_parser.set_defaults(handler=_click_recipe)

    click_render_parser = subparsers.add_parser(
        "click-render",
        help="Render one full restored side from a reviewed recipe",
    )
    click_render_parser.add_argument("project")
    click_render_parser.add_argument("scan")
    click_render_parser.add_argument("recipe")
    click_render_parser.add_argument("--bundle", required=True)
    click_render_parser.set_defaults(handler=_click_render)

    continuous_parser = subparsers.add_parser(
        "continuous-preview",
        help="Persist bounded hum, rumble, hiss, or crackle proposal and audition receipts",
    )
    continuous_commands = continuous_parser.add_subparsers(
        dest="continuous_preview_command", required=True
    )
    continuous_context_parser = continuous_commands.add_parser(
        "context",
        help="Seal exact project/source/speed/config/tool/module expectations",
    )
    continuous_context_parser.add_argument("project")
    continuous_context_parser.add_argument(
        "--kind", choices=("hum", "rumble", "hiss", "crackle"), required=True
    )
    continuous_context_parser.add_argument("--output", required=True)
    continuous_context_parser.add_argument("--json", action="store_true")
    continuous_context_parser.set_defaults(handler=_continuous_context)

    continuous_propose_parser = continuous_commands.add_parser(
        "propose",
        help="Analyze one bounded exact scope from owner-reviewed noise references",
    )
    continuous_propose_parser.add_argument("project")
    continuous_propose_parser.add_argument(
        "--kind", choices=("hum", "rumble", "hiss", "crackle"), required=True
    )
    continuous_propose_parser.add_argument("--start-sample", type=int, required=True)
    continuous_propose_parser.add_argument("--end-sample", type=int, required=True)
    continuous_propose_parser.add_argument(
        "--reference",
        action="append",
        type=_continuous_reference_argument,
        required=True,
        metavar="LABEL|ROLE|START|END",
    )
    continuous_propose_parser.add_argument("--expected-context", required=True)
    continuous_propose_parser.add_argument(
        "--owner-reviewed-scope", action="store_true"
    )
    continuous_propose_parser.add_argument(
        "--owner-reviewed-references", action="store_true"
    )
    continuous_propose_parser.add_argument("--json", action="store_true")
    continuous_propose_parser.set_defaults(handler=_continuous_propose)

    continuous_attest_parser = continuous_commands.add_parser(
        "attest",
        help="Create one exact audition request without claiming completed listening",
    )
    continuous_attest_parser.add_argument("proposal")
    continuous_attest_parser.add_argument("--attestation-token", required=True)
    continuous_attest_parser.add_argument("--output", required=True)
    continuous_attest_parser.add_argument(
        "--owner-reviewed-scope", action="store_true"
    )
    continuous_attest_parser.add_argument(
        "--owner-reviewed-references", action="store_true"
    )
    continuous_attest_parser.add_argument(
        "--acknowledge-limited-authority", action="store_true"
    )
    continuous_attest_parser.add_argument("--json", action="store_true")
    continuous_attest_parser.set_defaults(handler=_continuous_attest)

    continuous_render_parser = continuous_commands.add_parser(
        "render",
        help="Render persistent Original/Proposed/Removed audition files",
    )
    continuous_render_parser.add_argument("project")
    continuous_render_parser.add_argument("proposal")
    continuous_render_parser.add_argument("attestation")
    continuous_render_parser.add_argument("--json", action="store_true")
    continuous_render_parser.set_defaults(handler=_continuous_preview)

    continuous_reject_parser = continuous_commands.add_parser(
        "reject", help="Persist an exact non-mutating proposal rejection"
    )
    continuous_reject_parser.add_argument("project")
    continuous_reject_parser.add_argument("proposal")
    continuous_reject_parser.add_argument("--reason", required=True)
    continuous_reject_parser.add_argument("--json", action="store_true")
    continuous_reject_parser.set_defaults(handler=_continuous_reject)

    continuous_catalog_parser = continuous_commands.add_parser(
        "catalog", help="Rediscover current, stale, and invalid persistent artifacts"
    )
    continuous_catalog_parser.add_argument("project")
    continuous_catalog_parser.add_argument("--json", action="store_true")
    continuous_catalog_parser.set_defaults(handler=_continuous_catalog)

    continuous_open_parser = continuous_commands.add_parser(
        "open", help="Open one exact current proposal, preview, or decision"
    )
    continuous_open_parser.add_argument("project")
    continuous_open_parser.add_argument(
        "--artifact-kind", choices=("proposal", "preview", "decision"), required=True
    )
    continuous_open_parser.add_argument("--sha256", required=True)
    continuous_open_parser.set_defaults(handler=_continuous_open)

    export_parser = subparsers.add_parser("export", help="Export approved tracks")
    export_parser.add_argument("project")
    export_parser.add_argument("--output-dir")
    export_parser.add_argument("--formats", default="flac,m4a")
    export_parser.add_argument("--flac-compression", type=int, default=8)
    export_parser.add_argument("--aac-bitrate", default="256k")
    export_parser.add_argument(
        "--source-speed-factor",
        type=float,
        help=(
            "Measured source playback rate divided by reference rate; for example, "
            "1.039 slows a 3.9%%-fast capture and lowers its pitch by the same factor"
        ),
    )
    export_parser.set_defaults(handler=_export)

    speed_parser = subparsers.add_parser(
        "speed", help="Create review-only constant-speed evidence"
    )
    speed_commands = speed_parser.add_subparsers(
        dest="speed_command", required=True
    )
    speed_review_parser = speed_commands.add_parser(
        "review-boundaries",
        help="Record explicit audio-and-visual boundary review without approving correction",
    )
    speed_review_parser.add_argument("project")
    speed_review_parser.add_argument("--output", required=True)
    speed_review_parser.add_argument(
        "--confirm-all-boundaries-reviewed",
        action="store_true",
        help="Attest that every track boundary was reviewed using audio and visuals",
    )
    speed_review_parser.add_argument(
        "--confirm-review-independent-of-reference-durations",
        action="store_true",
        help="Attest that reference durations were not used to place the reviewed boundaries",
    )
    speed_review_parser.set_defaults(handler=_speed_review_boundaries)
    speed_estimate_parser = speed_commands.add_parser(
        "estimate", help="Estimate one constant source-speed factor from track durations"
    )
    speed_estimate_parser.add_argument("project")
    speed_estimate_parser.add_argument(
        "--tracklist",
        required=True,
        help="Strict JSON reference track list with a duration for every track",
    )
    speed_estimate_parser.add_argument(
        "--boundary-review",
        help=(
            "Exact project-bound audio+visual boundary-review evidence; without it "
            "the estimator reports diagnostics and abstains"
        ),
    )
    speed_estimate_parser.add_argument(
        "--output", help="Write a new sealed proposal JSON without replacing a file"
    )
    speed_estimate_parser.add_argument(
        "--json", action="store_true", help="Emit the complete proposal as strict JSON"
    )
    speed_estimate_parser.set_defaults(handler=_speed_estimate)

    endpoints_parser = subparsers.add_parser(
        "endpoints",
        help="Create and inspect review-only music endpoint proposals",
    )
    endpoints_commands = endpoints_parser.add_subparsers(
        dest="endpoints_command",
        required=True,
    )
    endpoints_propose_parser = endpoints_commands.add_parser(
        "propose",
        help="Create one sealed multimodal endpoint proposal without applying it",
    )
    endpoints_propose_parser.add_argument("project")
    endpoints_propose_parser.add_argument("--output", required=True)
    endpoints_propose_parser.add_argument(
        "--scope",
        action="append",
        type=_endpoint_scope_argument,
        metavar="LABEL|START|END",
        help=(
            "Exact ordered side scope in source samples; repeat for multiple sides. "
            "The default is one full-source scope."
        ),
    )
    endpoints_propose_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the complete deterministic proposal as strict JSON",
    )
    endpoints_propose_parser.set_defaults(handler=_endpoints_propose)

    endpoints_inspect_parser = endpoints_commands.add_parser(
        "inspect",
        aliases=["load"],
        help="Strictly load and inspect one sealed endpoint proposal",
    )
    endpoints_inspect_parser.add_argument("proposal")
    endpoints_inspect_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the complete validated proposal as strict JSON",
    )
    endpoints_inspect_parser.set_defaults(handler=_endpoints_inspect)

    project_parser = subparsers.add_parser(
        "project", help="Inspect and explicitly migrate project files"
    )
    project_commands = project_parser.add_subparsers(
        dest="project_command", required=True
    )
    project_migrate_parser = project_commands.add_parser(
        "migrate", help="Safely migrate one legacy project to the current schema"
    )
    project_migrate_parser.add_argument("project")
    project_migrate_parser.add_argument(
        "--json", action="store_true", help="Emit a portable migration result"
    )
    project_migrate_parser.set_defaults(handler=_project_migrate)

    album_parser = subparsers.add_parser(
        "album", help="Create, inspect, review, and export a multi-side album project"
    )
    album_commands = album_parser.add_subparsers(dest="album_command", required=True)

    album_create_parser = album_commands.add_parser(
        "create", help="Create a strict multi-side album project"
    )
    album_create_parser.add_argument("album_project")
    album_create_parser.add_argument(
        "--side",
        action="append",
        required=True,
        metavar="SPEC",
        help=(
            "Repeat LABEL|PROJECT to inherit and pin reviewed project speed, or "
            "LABEL|PROJECT|CAPTURE_RPM|INTENDED_RPM|FINE_FACTOR for an explicit pinned override"
        ),
    )
    album_create_parser.add_argument("--artist")
    album_create_parser.add_argument("--album")
    album_create_parser.add_argument("--album-artist")
    album_create_parser.add_argument("--year")
    album_create_parser.add_argument("--genre")
    album_create_parser.add_argument("--label")
    album_create_parser.add_argument("--catalog-number")
    album_create_parser.add_argument("--barcode")
    album_create_parser.add_argument("--musicbrainz-release-id")
    album_create_parser.add_argument("--musicbrainz-release-group-id")
    album_create_parser.add_argument("--artwork")
    album_create_parser.add_argument("--overwrite", action="store_true")
    album_create_parser.set_defaults(handler=_album_create)

    album_inspect_parser = album_commands.add_parser(
        "inspect", help="Verify and inspect an album project"
    )
    album_inspect_parser.add_argument("album_project")
    album_inspect_parser.add_argument("--json", action="store_true")
    album_inspect_parser.set_defaults(handler=_album_inspect)

    album_review_parser = album_commands.add_parser(
        "review", help="Review all album sides in the local Album Workbench"
    )
    album_review_parser.add_argument("album_project")
    album_review_parser.add_argument("--port", type=int, default=0)
    album_review_parser.add_argument("--no-browser", action="store_true")
    album_review_parser.set_defaults(handler=_album_review)

    album_repin_parser = album_commands.add_parser(
        "repin", help="Explicitly approve and pin current side-project state"
    )
    album_repin_parser.add_argument("album_project")
    repin_selection = album_repin_parser.add_mutually_exclusive_group(required=True)
    repin_selection.add_argument(
        "--side",
        action="append",
        metavar="LABEL",
        help="Side label to repin; repeat for multiple reviewed sides",
    )
    repin_selection.add_argument(
        "--all",
        action="store_true",
        help="Repin every side after reviewing all current changes",
    )
    album_repin_parser.set_defaults(handler=_album_repin)

    album_migrate_parser = album_commands.add_parser(
        "migrate", help="Safely migrate one legacy album project"
    )
    album_migrate_parser.add_argument("album_project")
    album_migrate_parser.add_argument(
        "--json", action="store_true", help="Emit a portable migration result"
    )
    album_migrate_parser.set_defaults(handler=_album_migrate)

    album_publication_parser = album_commands.add_parser(
        "publication",
        help="Plan, publish, verify, replay, and recover the unified album graph",
    )
    publication_commands = album_publication_parser.add_subparsers(
        dest="album_publication_command",
        required=True,
    )

    publication_plan_parser = publication_commands.add_parser(
        "plan", help="Create one immutable publication plan beside the album project"
    )
    publication_plan_parser.add_argument("album_project")
    publication_plan_parser.add_argument("plan")
    publication_plan_parser.add_argument(
        "--profiles",
        default="archival-source,corrected-lossless,portable",
        help=(
            "Comma-separated archival-source, restored-side, "
            "corrected-lossless, and portable profiles"
        ),
    )
    publication_plan_parser.add_argument(
        "--restoration",
        choices=("none", "reviewed"),
        default="none",
        help="Use no restoration or only exact reviewed restoration outcomes",
    )
    publication_plan_parser.add_argument("--flac-compression", type=int, default=8)
    publication_plan_parser.add_argument("--aac-bitrate-kbps", type=int, default=256)
    publication_plan_parser.set_defaults(handler=_album_publication_plan)

    publication_preflight_parser = publication_commands.add_parser(
        "preflight", help="Revalidate a plan and every live input without writing"
    )
    publication_preflight_parser.add_argument("plan")
    publication_preflight_parser.add_argument("--json", action="store_true")
    publication_preflight_parser.set_defaults(handler=_album_publication_preflight)

    publication_execute_parser = publication_commands.add_parser(
        "execute", help="Atomically execute an exact immutable publication plan"
    )
    publication_execute_parser.add_argument("plan")
    publication_execute_parser.add_argument("output_directory")
    publication_execute_parser.set_defaults(handler=_album_publication_execute)

    publication_verify_parser = publication_commands.add_parser(
        "verify", help="Fully decode and verify a published album directory"
    )
    publication_verify_parser.add_argument("publication_directory")
    publication_verify_parser.add_argument("--json", action="store_true")
    publication_verify_parser.set_defaults(handler=_album_publication_verify)

    publication_replay_parser = publication_commands.add_parser(
        "replay", help="Publish anew from an explicit plan and compare identities"
    )
    publication_replay_parser.add_argument("publication_directory")
    publication_replay_parser.add_argument("plan")
    publication_replay_parser.add_argument("replay_output_directory")
    publication_replay_parser.add_argument("--json", action="store_true")
    publication_replay_parser.set_defaults(handler=_album_publication_replay)

    publication_orphans_parser = publication_commands.add_parser(
        "orphans", help="Inventory bounded owned partial stages without changing them"
    )
    publication_orphans_parser.add_argument("parent_directory")
    publication_orphans_parser.add_argument("--json", action="store_true")
    publication_orphans_parser.set_defaults(handler=_album_publication_orphans)

    publication_recover_parser = publication_commands.add_parser(
        "recover", help="Quarantine or remove one exact receipted owned orphan"
    )
    publication_recover_parser.add_argument("orphan")
    publication_recover_parser.add_argument(
        "--action", choices=("quarantine", "remove"), required=True
    )
    publication_recover_parser.add_argument(
        "--expected-journal-sha256", required=True
    )
    publication_recover_parser.add_argument("--expected-device", type=int, required=True)
    publication_recover_parser.add_argument("--expected-inode", type=int, required=True)
    publication_recover_parser.add_argument(
        "--expected-file-type", type=int, required=True
    )
    publication_recover_parser.add_argument("--expected-birth-ns", type=int)
    publication_recover_parser.add_argument("--expected-file-attributes", type=int)
    publication_recover_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm the exact receipted recovery action",
    )
    publication_recover_parser.add_argument("--json", action="store_true")
    publication_recover_parser.set_defaults(handler=_album_publication_recover)

    album_export_parser = album_commands.add_parser(
        "export", help="Legacy direct album export (unified publication is preferred)"
    )
    album_export_parser.add_argument("album_project")
    album_export_parser.add_argument("--output-dir")
    album_export_parser.add_argument("--formats", default="flac,m4a")
    album_export_parser.add_argument("--flac-compression", type=int, default=8)
    album_export_parser.add_argument("--aac-bitrate", default="256k")
    album_export_parser.set_defaults(handler=_album_export)

    info_parser = subparsers.add_parser("info", help="Print project and marker details")
    info_parser.add_argument("project")
    info_parser.add_argument(
        "--json", action="store_true", help="Emit one compact JSON receipt"
    )
    info_parser.set_defaults(handler=_info)

    doctor_parser = subparsers.add_parser("doctor", help="Check local dependencies")
    doctor_parser.add_argument(
        "--path",
        default=None,
        help=(
            "also exercise atomic no-replace creation in this existing or future "
            "destination directory"
        ),
    )
    doctor_parser.add_argument(
        "--json", action="store_true", help="Emit a strict machine-readable report"
    )
    doctor_parser.set_defaults(handler=_doctor)

    cache_parser = subparsers.add_parser(
        "cache", help="Inspect or safely reclaim verified-audio snapshot storage"
    )
    cache_commands = cache_parser.add_subparsers(
        dest="cache_command", required=True
    )
    cache_status_parser = cache_commands.add_parser(
        "status", help="List snapshot leases without changing them"
    )
    _add_cache_location_arguments(cache_status_parser)
    cache_status_parser.set_defaults(handler=_cache_status)
    cache_clean_parser = cache_commands.add_parser(
        "clean", help="Remove only snapshots whose owner is provably gone"
    )
    _add_cache_location_arguments(cache_clean_parser)
    cache_clean_parser.set_defaults(handler=_cache_clean)

    evidence_parser = subparsers.add_parser(
        "evidence", help="Manage the optional private review-evidence corpus"
    )
    evidence_commands = evidence_parser.add_subparsers(
        dest="evidence_command", required=True
    )
    evidence_status_parser = evidence_commands.add_parser(
        "status", help="Inspect settings and verify all records"
    )
    _add_review_evidence_common_arguments(evidence_status_parser)
    evidence_status_parser.set_defaults(handler=_review_evidence_status)
    for command in ("enable", "disable"):
        command_parser = evidence_commands.add_parser(
            command, help=f"Explicitly {command} future evidence collection"
        )
        _add_review_evidence_common_arguments(command_parser)
        command_parser.set_defaults(handler=_review_evidence_set_enabled)
    evidence_list_parser = evidence_commands.add_parser(
        "list", help="List deterministic path-free record summaries"
    )
    _add_review_evidence_common_arguments(evidence_list_parser)
    evidence_list_parser.set_defaults(handler=_review_evidence_list)
    evidence_export_parser = evidence_commands.add_parser(
        "export", help="Write a deterministic verified corpus export"
    )
    evidence_export_parser.add_argument("output")
    _add_review_evidence_common_arguments(evidence_export_parser)
    evidence_export_parser.set_defaults(handler=_review_evidence_export)
    evidence_evaluate_parser = evidence_commands.add_parser(
        "evaluate",
        help="Evaluate one canonical export without approval or mutation authority",
    )
    evidence_evaluate_parser.add_argument("export")
    evidence_evaluate_parser.add_argument(
        "--output",
        help="Write a new canonical no-overwrite evaluation receipt",
    )
    evidence_evaluate_parser.add_argument("--baseline-config-sha256")
    evidence_evaluate_parser.add_argument("--candidate-config-sha256")
    evidence_evaluate_parser.add_argument(
        "--json", action="store_true", help="Emit strict JSON"
    )
    evidence_evaluate_parser.set_defaults(handler=_review_evidence_evaluate)
    evidence_delete_parser = evidence_commands.add_parser(
        "delete", help="Delete exactly one hash-confirmed record"
    )
    evidence_delete_parser.add_argument("record_sha256")
    evidence_delete_parser.add_argument("--expected-record-sha256", required=True)
    evidence_delete_parser.add_argument(
        "--yes", action="store_true", help="Confirm deliberate deletion"
    )
    _add_review_evidence_common_arguments(evidence_delete_parser)
    evidence_delete_parser.set_defaults(handler=_review_evidence_delete)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one CLI command and translate expected failures into stable exit codes."""

    _configure_output_error_handling()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (GrooveSerpentError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
