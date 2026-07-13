"""Command-line assembly and top-level error handling for Groove Serpent."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .analysis import analyze_audio
from .errors import GrooveSerpentError
from .exporter import export_project, suggest_output_directory
from .media import sha256_file, tool_version
from .models import AnalysisSettings, MAX_TRACKS, resolve_source_path
from .project_io import load_project, load_project_with_sha256, save_project
from .tracklist import Tracklist, load_tracklist


def _default_project_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.stem}.groove.json")


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
    if project_path.exists() and not args.overwrite:
        raise GrooveSerpentError(
            f"Project already exists: {project_path}\nUse --overwrite to replace it."
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

    print(f"Analyzing {input_path.name} ...")
    project = analyze_audio(
        input_path,
        stored_source_path=_stored_source_path(input_path, project_path),
        settings=_settings_from_args(args),
        expected_track_count=expected_count,
        track_seeds=tracklist.tracks if tracklist else None,
        metadata=_metadata_from_args(args, tracklist),
    )
    save_project(project, project_path)

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
        print(json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False))
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


def _album_create(args: argparse.Namespace) -> int:
    from .album import (
        AlbumProject,
        artwork_for_album_path,
        parse_album_side_spec,
        save_album_project,
    )

    album_path = Path(args.album_project).expanduser().resolve()
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
    save_album_project(album, album_path, overwrite=args.overwrite)
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

    album_path = Path(args.album_project).expanduser().resolve()
    album = load_album_project(album_path)
    receipt = inspect_album_project(album, album_path)
    if args.json:
        print(json.dumps(receipt, indent=2, ensure_ascii=False, allow_nan=False))
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


def _album_repin(args: argparse.Namespace) -> int:
    from .album import load_album_project, repin_album_sides, save_album_project

    album_path = Path(args.album_project).expanduser().resolve()
    album = load_album_project(album_path)
    labels = None if args.all else args.side
    repinned = repin_album_sides(album, album_path, labels)
    save_album_project(album, album_path, overwrite=True)
    print(f"Repinned {len(repinned)} side(s) in {album_path}")
    for side in album.sides:
        if side.label in repinned and side.pin is not None:
            print(
                f"  Side {side.label}: revision={side.pin.project_revision}  "
                f"project={side.pin.project_sha256}  state={side.pin.editable_state_sha256}  "
                f"speed={side.pin.speed_state_sha256}"
            )
    return 0


def _album_export(args: argparse.Namespace) -> int:
    from .album import export_album, load_album_project, suggest_album_output_directory

    album_path = Path(args.album_project).expanduser().resolve()
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


def _doctor(_: argparse.Namespace) -> int:
    from .audacity import discover_audacity
    from .recognition import AcoustIDRecognitionProvider

    print(f"Groove Serpent {__version__}")
    print(tool_version("ffmpeg"))
    print(tool_version("ffprobe"))
    recognition = AcoustIDRecognitionProvider().readiness()
    print(f"Acoustic ID: {recognition.message}")
    audacity = discover_audacity()
    print(f"Audacity: {audacity.message}")
    print("Dependencies look ready.")
    return 0


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

    album_parser = subparsers.add_parser(
        "album", help="Create, inspect, and export a multi-side album project"
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

    album_export_parser = album_commands.add_parser(
        "export", help="Publish one atomic album directory"
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
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one CLI command and translate expected failures into stable exit codes."""

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
