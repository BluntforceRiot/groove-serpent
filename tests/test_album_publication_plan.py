from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import groove_serpent.album_publication_plan as publication_plan_module
from groove_serpent.album_publication_plan import (
    ALBUM_PUBLICATION_PLAN_SCHEMA,
    PROFILE_ARCHIVAL_SOURCE,
    PROFILE_CORRECTED_LOSSLESS,
    PROFILE_PORTABLE,
    PROFILE_RESTORED_SIDE,
    AlbumPublicationPlan,
    ProcessingInput,
    ProcessingNode,
    ProfileOutput,
    PublicationSide,
    RestorationNoDerivativeBinding,
    RestorationRenderBinding,
    SideIdentity,
    SpeedSelection,
    ToolBinding,
    load_album_publication_plan,
    load_album_publication_plan_with_sha256,
    save_album_publication_plan,
    verify_album_publication_plan_identity,
)
from groove_serpent.errors import ProjectValidationError


def digest(character: str) -> str:
    return hashlib.sha256(character.encode("utf-8")).hexdigest()


def identity(index: int) -> SideIdentity:
    return SideIdentity(
        project_revision=index,
        project_sha256=digest(str(index)),
        editable_state_sha256=digest(chr(ord("a") + index)),
        source_sha256=digest(chr(ord("d") + index)),
        project_speed_state_sha256=digest(chr(ord("g") + index)),
    )


def render_binding(side_identity: SideIdentity, index: int) -> RestorationRenderBinding:
    return RestorationRenderBinding(
        schema="groove-serpent.restoration-render/1",
        manifest_reference=f"restoration/side-{index}/render.json",
        manifest_sha256=digest(chr(ord("j") + index)),
        audio_reference=f"restoration/side-{index}/restored.flac",
        audio_sha256=digest(chr(ord("m") + index)),
        project_sha256=side_identity.project_sha256,
        source_sha256=side_identity.source_sha256,
    )


def no_derivative_binding(
    side_identity: SideIdentity, index: int
) -> RestorationNoDerivativeBinding:
    return RestorationNoDerivativeBinding(
        schema="groove-serpent.restoration-no-derivative/1",
        scan_schema="groove-serpent.click-scan/1",
        scan_reference=f"restoration/side-{index}/click-scan.json",
        scan_sha256=digest(f"clean-scan-{index}"),
        project_sha256=side_identity.project_sha256,
        source_sha256=side_identity.source_sha256,
        restoration_status="complete",
        scan_range_covers_music=True,
        candidate_scan_truncated=False,
        retained_candidates=0,
    )


def tool(operation: str) -> ToolBinding:
    configurations: dict[str, dict[str, Any]] = {
        "source-side": {
            "mode": "immutable-copy",
            "verify_sha256": True,
        },
        "restore-side": {
            "mode": "verified-render-binding",
            "preserve_unapproved_samples": True,
        },
        "correct-speed-side": {
            "filter": "aresample",
            "precision_bits": 28,
            "resampler": "soxr",
            "speed_factor_source": "publication-side",
        },
        "assemble-archival": {
            "gap_samples": 0,
            "mode": "ordered-side-concatenation",
        },
        "assemble-restored": {
            "gap_samples": 0,
            "mode": "ordered-side-concatenation",
        },
        "encode-lossless": {
            "codec": "flac",
            "compression_level": 8,
            "sample_format": "source-preserving",
        },
        "encode-portable": {
            "bitrate_kbps": 256,
            "codec": "aac",
            "container": "m4a",
            "profile": "aac-lc",
        },
    }
    return ToolBinding.create(
        name="groove-serpent",
        version="1.0.0.dev1",
        configuration=configurations[operation],
    )


def input_(role: str, node_id: str) -> ProcessingInput:
    return ProcessingInput(role=role, node_id=node_id)


def node(
    node_id: str,
    operation: str,
    *,
    side_label: str | None = None,
    inputs: tuple[ProcessingInput, ...] = (),
) -> ProcessingNode:
    return ProcessingNode(
        node_id=node_id,
        operation=operation,
        side_label=side_label,
        inputs=inputs,
        tool=tool(operation),
    )


def representative_plan(*, restored: bool = True) -> AlbumPublicationPlan:
    first_identity = identity(1)
    second_identity = identity(2)
    sides = (
        PublicationSide(
            label="A",
            order=1,
            project_reference="sides/a.groove.json",
            current_identity=first_identity,
            selected_speed_state_sha256=digest("r"),
            selected_effective_speed_factor=1.035,
            restoration_render=(render_binding(first_identity, 1) if restored else None),
            restoration_no_derivative=None,
        ),
        PublicationSide(
            label="B",
            order=2,
            project_reference="sides/b.groove.json",
            current_identity=second_identity,
            selected_speed_state_sha256=digest("s"),
            selected_effective_speed_factor=1.0,
            restoration_render=None,
            restoration_no_derivative=(
                no_derivative_binding(second_identity, 2) if restored else None
            ),
        ),
    )
    nodes = [
        node("source-a", "source-side", side_label="A"),
        node("source-b", "source-side", side_label="B"),
    ]
    profiles = [PROFILE_ARCHIVAL_SOURCE, PROFILE_CORRECTED_LOSSLESS, PROFILE_PORTABLE]
    outputs = [
        ProfileOutput(PROFILE_ARCHIVAL_SOURCE, "album-archive"),
        ProfileOutput(PROFILE_CORRECTED_LOSSLESS, "album-lossless"),
        ProfileOutput(PROFILE_PORTABLE, "album-portable"),
    ]
    corrected_upstream = {"A": "source-a", "B": "source-b"}
    if restored:
        nodes.extend(
            [
                node(
                    "restore-a",
                    "restore-side",
                    side_label="A",
                    inputs=(input_("source", "source-a"),),
                ),
                node(
                    "album-restored",
                    "assemble-restored",
                    inputs=(
                        input_("side-001", "restore-a"),
                        input_("side-002", "source-b"),
                    ),
                ),
            ]
        )
        corrected_upstream = {"A": "restore-a", "B": "source-b"}
        profiles.append(PROFILE_RESTORED_SIDE)
        outputs.append(ProfileOutput(PROFILE_RESTORED_SIDE, "album-restored"))
    nodes.extend(
        [
            node(
                "correct-a",
                "correct-speed-side",
                side_label="A",
                inputs=(input_("audio", corrected_upstream["A"]),),
            ),
            node(
                "correct-b",
                "correct-speed-side",
                side_label="B",
                inputs=(input_("audio", corrected_upstream["B"]),),
            ),
            node(
                "album-archive",
                "assemble-archival",
                inputs=(
                    input_("side-001", "source-a"),
                    input_("side-002", "source-b"),
                ),
            ),
            node(
                "album-lossless",
                "encode-lossless",
                inputs=(
                    input_("side-001", "correct-a"),
                    input_("side-002", "correct-b"),
                ),
            ),
            node(
                "album-portable",
                "encode-portable",
                inputs=(input_("lossless", "album-lossless"),),
            ),
        ]
    )
    return AlbumPublicationPlan.create(
        album_reference="album.groove-album.json",
        album_sha256=digest("a"),
        sides=sides,
        selected_profiles=profiles,
        nodes=nodes,
        profile_outputs=outputs,
    )


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def current_speeds(plan: AlbumPublicationPlan) -> dict[str, SpeedSelection]:
    return {
        side.label: SpeedSelection(
            side.selected_speed_state_sha256,
            side.selected_effective_speed_factor,
        )
        for side in plan.sides
    }


def test_representative_graph_is_explicit_hash_bound_and_valid() -> None:
    plan = representative_plan()

    assert plan.schema == ALBUM_PUBLICATION_PLAN_SCHEMA
    assert plan.selected_profiles == (
        PROFILE_ARCHIVAL_SOURCE,
        PROFILE_RESTORED_SIDE,
        PROFILE_CORRECTED_LOSSLESS,
        PROFILE_PORTABLE,
    )
    assert len(plan.nodes) == 9
    assert len(plan.body_sha256) == len(plan.plan_sha256) == 64
    assert plan.body_sha256 != plan.plan_sha256
    plan.validate()


def test_minimal_archival_and_restored_only_graphs_are_valid() -> None:
    archival_base = representative_plan(restored=False)
    archival_node_ids = {"source-a", "source-b", "album-archive"}
    archival = recreate(
        archival_base,
        sides=tuple(
            replace(side, restoration_no_derivative=None)
            for side in archival_base.sides
        ),
        profiles=(PROFILE_ARCHIVAL_SOURCE,),
        nodes=tuple(
            item for item in archival_base.nodes if item.node_id in archival_node_ids
        ),
        outputs=(ProfileOutput(PROFILE_ARCHIVAL_SOURCE, "album-archive"),),
    )
    archival.validate()

    restored_base = representative_plan()
    restored_node_ids = {
        "source-a",
        "source-b",
        "restore-a",
        "album-restored",
    }
    restored = recreate(
        restored_base,
        profiles=(PROFILE_RESTORED_SIDE,),
        nodes=tuple(
            item for item in restored_base.nodes if item.node_id in restored_node_ids
        ),
        outputs=(ProfileOutput(PROFILE_RESTORED_SIDE, "album-restored"),),
    )
    restored.validate()


def test_hash_is_deterministic_across_unordered_constructor_inputs() -> None:
    plan = representative_plan()
    reversed_nodes = tuple(
        replace(node_item, inputs=tuple(reversed(node_item.inputs)))
        for node_item in reversed(plan.nodes)
    )
    rebuilt = AlbumPublicationPlan.create(
        album_reference=plan.album_reference,
        album_sha256=plan.album_sha256,
        sides=reversed(plan.sides),
        selected_profiles=reversed(plan.selected_profiles),
        nodes=reversed_nodes,
        profile_outputs=reversed(plan.profile_outputs),
    )

    assert rebuilt.body_sha256 == plan.body_sha256
    assert rebuilt.plan_sha256 == plan.plan_sha256
    assert rebuilt.to_dict() == plan.to_dict()


def test_speed_factor_integer_and_float_have_one_canonical_hash() -> None:
    plan = representative_plan(restored=False)
    sides = list(plan.sides)
    sides[1] = replace(sides[1], selected_effective_speed_factor=1)  # type: ignore[arg-type]
    rebuilt = AlbumPublicationPlan.create(
        album_reference=plan.album_reference,
        album_sha256=plan.album_sha256,
        sides=sides,
        selected_profiles=plan.selected_profiles,
        nodes=plan.nodes,
        profile_outputs=plan.profile_outputs,
    )

    assert rebuilt.plan_sha256 == plan.plan_sha256
    assert type(rebuilt.sides[1].selected_effective_speed_factor) is float


def test_tool_bindings_persist_concrete_execution_settings() -> None:
    plan = representative_plan()
    by_operation = {item.operation: item.tool.configuration for item in plan.nodes}

    assert by_operation["encode-lossless"] == {
        "codec": "flac",
        "compression_level": 8,
        "sample_format": "source-preserving",
    }
    assert by_operation["encode-portable"]["bitrate_kbps"] == 256
    assert by_operation["correct-speed-side"]["resampler"] == "soxr"
    assert by_operation["source-side"]["mode"] == "immutable-copy"


def test_tool_configuration_is_bounded_finite_and_hash_bound() -> None:
    with pytest.raises(ProjectValidationError, match="finite JSON number"):
        ToolBinding.create(
            name="ffmpeg",
            version="8.1",
            configuration={"factor": float("nan")},
        )
    with pytest.raises(ProjectValidationError, match="maximum JSON depth"):
        ToolBinding.create(
            name="ffmpeg",
            version="8.1",
            configuration={"a": {"b": {"c": {"d": {"e": 1}}}}},
        )
    with pytest.raises(ProjectValidationError, match="unsupported JSON value"):
        ToolBinding.create(
            name="ffmpeg",
            version="8.1",
            configuration={"bad": {"not", "json"}},
        )
    oversized = {
        f"setting_{index:02d}": ["x" * 256, "y" * 256, "z" * 256]
        for index in range(31)
    }
    with pytest.raises(ProjectValidationError, match="canonical bytes"):
        ToolBinding.create(
            name="ffmpeg",
            version="8.1",
            configuration=oversized,
        )

    plan = representative_plan()
    first = plan.nodes[0]
    bad_tool = replace(first.tool, configuration_sha256=digest("forged-config"))
    with pytest.raises(ProjectValidationError, match="does not match"):
        recreate(
            plan,
            nodes=(replace(first, tool=bad_tool),) + plan.nodes[1:],
        )


def test_atomic_no_overwrite_save_load_and_raw_receipt(tmp_path: Path) -> None:
    plan = representative_plan()
    path = tmp_path / "album.publication-plan.json"

    save_album_publication_plan(plan, path)
    original = path.read_bytes()
    loaded, raw_sha256 = load_album_publication_plan_with_sha256(path)

    assert loaded == plan
    assert raw_sha256 == hashlib.sha256(original).hexdigest()
    with pytest.raises(ProjectValidationError, match="already exists"):
        save_album_publication_plan(representative_plan(restored=False), path)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "reference",
    [
        "../outside.json",
        "/absolute.json",
        "C:/absolute.json",
        "dir\\file.json",
        "a//b",
        "CON.json",
    ],
)
def test_stored_paths_must_be_safe_portable_relative_references(reference: str) -> None:
    plan = representative_plan()
    with pytest.raises(ProjectValidationError, match="relative|portable|inside"):
        AlbumPublicationPlan.create(
            album_reference=reference,
            album_sha256=plan.album_sha256,
            sides=plan.sides,
            selected_profiles=plan.selected_profiles,
            nodes=plan.nodes,
            profile_outputs=plan.profile_outputs,
        )


def test_stored_text_requires_canonical_unicode() -> None:
    plan = representative_plan()
    with pytest.raises(ProjectValidationError, match="NFC"):
        AlbumPublicationPlan.create(
            album_reference="cafe\u0301.json",
            album_sha256=plan.album_sha256,
            sides=plan.sides,
            selected_profiles=plan.selected_profiles,
            nodes=plan.nodes,
            profile_outputs=plan.profile_outputs,
        )


def test_render_path_and_provenance_are_strictly_bound() -> None:
    plan = representative_plan()
    side = plan.sides[0]
    assert side.restoration_render is not None
    unsafe_render = replace(side.restoration_render, audio_reference="../restored.flac")
    with pytest.raises(ProjectValidationError, match="inside"):
        replace(side, restoration_render=unsafe_render).validate()
    stale_render = replace(side.restoration_render, source_sha256=digest("z"))
    with pytest.raises(ProjectValidationError, match="source SHA-256"):
        replace(side, restoration_render=stale_render).validate()


def test_all_stored_file_references_are_duplicate_free() -> None:
    plan = representative_plan()
    first = plan.sides[0]
    assert first.restoration_render is not None
    duplicate = replace(
        first.restoration_render,
        audio_reference=first.restoration_render.manifest_reference,
    )
    with pytest.raises(ProjectValidationError, match="duplicates"):
        recreate(plan, sides=(replace(first, restoration_render=duplicate), plan.sides[1]))


def test_loader_rejects_unknown_missing_duplicate_and_nonfinite_json(tmp_path: Path) -> None:
    plan = representative_plan()
    payload = plan.to_dict()

    unknown = copy.deepcopy(payload)
    unknown["body"]["sides"][0]["surprise"] = True
    unknown_path = tmp_path / "unknown.json"
    write_payload(unknown_path, unknown)
    with pytest.raises(ProjectValidationError, match="unsupported field"):
        load_album_publication_plan(unknown_path)

    missing = copy.deepcopy(payload)
    del missing["body"]["sides"][0]["current_identity"]["source_sha256"]
    missing_path = tmp_path / "missing.json"
    write_payload(missing_path, missing)
    with pytest.raises(ProjectValidationError, match="missing required field"):
        load_album_publication_plan(missing_path)

    duplicate_path = tmp_path / "duplicate.json"
    duplicate_text = json.dumps(payload, allow_nan=False)
    duplicate_text = duplicate_text.replace(
        '"schema": "groove-serpent.album-publication-plan/1",',
        '"schema": "groove-serpent.album-publication-plan/1", '
        '"schema": "groove-serpent.album-publication-plan/1",',
        1,
    )
    duplicate_path.write_text(duplicate_text, encoding="utf-8")
    with pytest.raises(ProjectValidationError, match="Duplicate JSON object field"):
        load_album_publication_plan(duplicate_path)

    duplicate_config_path = tmp_path / "duplicate-config.json"
    duplicate_config_text = json.dumps(payload, allow_nan=False).replace(
        '"codec": "flac"',
        '"codec": "flac", "codec": "pcm_s24le"',
        1,
    )
    duplicate_config_path.write_text(duplicate_config_text, encoding="utf-8")
    with pytest.raises(ProjectValidationError, match="Duplicate JSON object field"):
        load_album_publication_plan(duplicate_config_path)

    nonfinite_path = tmp_path / "nan.json"
    nonfinite_text = json.dumps(payload, allow_nan=False).replace(
        '"selected_effective_speed_factor": 1.035',
        '"selected_effective_speed_factor": NaN',
        1,
    )
    nonfinite_path.write_text(nonfinite_text, encoding="utf-8")
    with pytest.raises(ProjectValidationError, match="Invalid JSON number"):
        load_album_publication_plan(nonfinite_path)


def test_loader_rejects_tampered_body_and_envelope_hashes(tmp_path: Path) -> None:
    plan = representative_plan()
    body_tamper = plan.to_dict()
    body_tamper["body"]["album_sha256"] = digest("b")
    body_path = tmp_path / "body-tamper.json"
    write_payload(body_path, body_tamper)
    with pytest.raises(ProjectValidationError, match="body SHA-256"):
        load_album_publication_plan(body_path)

    envelope_tamper = plan.to_dict()
    envelope_tamper["plan_sha256"] = digest("f")
    envelope_path = tmp_path / "envelope-tamper.json"
    write_payload(envelope_path, envelope_tamper)
    with pytest.raises(ProjectValidationError, match="plan SHA-256"):
        load_album_publication_plan(envelope_path)


def recreate(
    plan: AlbumPublicationPlan,
    *,
    sides: tuple[PublicationSide, ...] | None = None,
    profiles: tuple[str, ...] | None = None,
    nodes: tuple[ProcessingNode, ...] | None = None,
    outputs: tuple[ProfileOutput, ...] | None = None,
) -> AlbumPublicationPlan:
    return AlbumPublicationPlan.create(
        album_reference=plan.album_reference,
        album_sha256=plan.album_sha256,
        sides=plan.sides if sides is None else sides,
        selected_profiles=plan.selected_profiles if profiles is None else profiles,
        nodes=plan.nodes if nodes is None else nodes,
        profile_outputs=plan.profile_outputs if outputs is None else outputs,
    )


def test_dag_rejects_cycle_missing_dependency_and_duplicate_nodes() -> None:
    plan = representative_plan()
    source_a = next(item for item in plan.nodes if item.node_id == "source-a")
    cyclic = tuple(
        replace(item, inputs=(input_("loop", "restore-a"),))
        if item.node_id == "source-a"
        else item
        for item in plan.nodes
    )
    with pytest.raises(ProjectValidationError, match="cycle"):
        recreate(plan, nodes=cyclic)

    missing = tuple(
        replace(item, inputs=(input_("source", "missing-source"),))
        if item.node_id == "restore-a"
        else item
        for item in plan.nodes
    )
    with pytest.raises(ProjectValidationError, match="missing dependency"):
        recreate(plan, nodes=missing)

    with pytest.raises(ProjectValidationError, match="Duplicate processing node ID"):
        recreate(plan, nodes=plan.nodes + (source_a,))


def test_dag_rejects_duplicate_inputs_and_profile_bindings() -> None:
    with pytest.raises(ProjectValidationError, match="duplicate input role"):
        node(
            "bad-inputs",
            "assemble-archival",
            inputs=(input_("side-001", "a"), input_("side-001", "b")),
        ).validate()

    plan = representative_plan()
    with pytest.raises(ProjectValidationError, match="Duplicate output binding"):
        recreate(plan, outputs=plan.profile_outputs + (plan.profile_outputs[0],))
    with pytest.raises(ProjectValidationError, match="profiles are duplicated"):
        recreate(plan, profiles=plan.selected_profiles + (PROFILE_PORTABLE,))
    with pytest.raises(ProjectValidationError, match="Unsupported publication profile"):
        recreate(plan, profiles=(PROFILE_ARCHIVAL_SOURCE, "unsupported-profile"))


def test_restored_and_corrected_profiles_require_explicit_dependencies() -> None:
    plan = representative_plan()
    missing_clean_outcome = (
        plan.sides[0],
        replace(plan.sides[1], restoration_no_derivative=None),
    )
    with pytest.raises(ProjectValidationError, match="explicit render or no-derivative"):
        recreate(plan, sides=missing_clean_outcome)

    corrected_only = representative_plan(restored=False)
    corrected_only.validate()
    partial_reviewed_outcome = (
        replace(
            corrected_only.sides[0],
            restoration_no_derivative=no_derivative_binding(
                corrected_only.sides[0].current_identity, 1
            ),
        ),
        corrected_only.sides[1],
    )
    with pytest.raises(ProjectValidationError, match="explicit render or no-derivative"):
        recreate(corrected_only, sides=partial_reviewed_outcome)

    no_audio_dependency = tuple(
        replace(item, inputs=()) if item.node_id == "correct-a" else item
        for item in plan.nodes
    )
    with pytest.raises(ProjectValidationError, match="semantic dependencies"):
        recreate(plan, nodes=no_audio_dependency)

    without_corrected = tuple(
        profile for profile in plan.selected_profiles if profile != PROFILE_CORRECTED_LOSSLESS
    )
    outputs = tuple(
        output
        for output in plan.profile_outputs
        if output.profile != PROFILE_CORRECTED_LOSSLESS
    )
    with pytest.raises(ProjectValidationError, match="portable profile requires"):
        recreate(plan, profiles=without_corrected, outputs=outputs)


def test_mixed_restoration_outcomes_and_forgery_guards() -> None:
    plan = representative_plan()
    assert plan.sides[0].restoration_render is not None
    clean = plan.sides[1].restoration_no_derivative
    assert clean is not None
    assert next(
        item for item in plan.nodes if item.node_id == "album-restored"
    ).inputs == (
        input_("side-001", "restore-a"),
        input_("side-002", "source-b"),
    )

    with pytest.raises(ProjectValidationError, match="zero retained candidates"):
        replace(clean, retained_candidates=1).validate(
            plan.sides[1].current_identity
        )
    with pytest.raises(ProjectValidationError, match="complete coverage"):
        replace(clean, restoration_status="partial").validate(
            plan.sides[1].current_identity
        )
    with pytest.raises(ProjectValidationError, match="project SHA-256"):
        replace(clean, project_sha256=digest("forged-project")).validate(
            plan.sides[1].current_identity
        )


def test_restored_profile_rejects_redundant_all_clean_outcome() -> None:
    plan = representative_plan()
    first = plan.sides[0]
    all_clean_sides = (
        replace(
            first,
            restoration_render=None,
            restoration_no_derivative=no_derivative_binding(
                first.current_identity, 1
            ),
        ),
        plan.sides[1],
    )
    all_clean_nodes = tuple(
        replace(item, inputs=(input_("side-001", "source-a"), input_("side-002", "source-b")))
        if item.node_id == "album-restored"
        else replace(item, inputs=(input_("audio", "source-a"),))
        if item.node_id == "correct-a"
        else item
        for item in plan.nodes
        if item.node_id != "restore-a"
    )

    with pytest.raises(ProjectValidationError, match="at least one rendered derivative"):
        recreate(plan, sides=all_clean_sides, nodes=all_clean_nodes)


def test_unbound_nodes_and_unused_render_bindings_are_rejected() -> None:
    plan = representative_plan(restored=False)
    extra = node("unused", "source-side", side_label="A")
    with pytest.raises(ProjectValidationError, match="duplicate 'source-side' nodes"):
        recreate(plan, nodes=plan.nodes + (extra,))

    first = plan.sides[0]
    sides = (
        replace(
            first,
            restoration_render=render_binding(first.current_identity, 1),
            restoration_no_derivative=None,
        ),
        plan.sides[1],
    )
    with pytest.raises(ProjectValidationError, match="exactly match"):
        recreate(plan, sides=sides)


def test_identity_verification_passes_exact_state() -> None:
    plan = representative_plan()
    verification = verify_album_publication_plan_identity(
        plan,
        current_album_sha256=plan.album_sha256,
        current_side_identities={
            side.label: side.current_identity.to_dict() for side in plan.sides
        },
        current_side_speed_selections=current_speeds(plan),
    )

    assert verification.ok is True
    assert verification.mismatches == ()
    assert verification.to_dict()["plan_sha256"] == plan.plan_sha256


def test_identity_verification_reports_stable_fail_closed_codes() -> None:
    plan = representative_plan()
    current_a = replace(
        plan.sides[0].current_identity,
        project_revision=99,
        project_sha256=digest("z"),
        editable_state_sha256=digest("y"),
        source_sha256=digest("x"),
        project_speed_state_sha256=digest("w"),
    )
    verification = verify_album_publication_plan_identity(
        plan,
        current_album_sha256=digest("b"),
        current_side_identities={"A": current_a, "C": identity(3)},
        current_side_speed_selections=current_speeds(plan),
    )

    assert verification.ok is False
    assert [item.code for item in verification.mismatches] == [
        "album_sha256_mismatch",
        "side_project_revision_mismatch",
        "side_project_sha256_mismatch",
        "side_editable_state_sha256_mismatch",
        "side_source_sha256_mismatch",
        "side_project_speed_state_sha256_mismatch",
        "side_missing",
        "side_unexpected",
    ]


def test_identity_verification_rejects_malformed_current_values() -> None:
    plan = representative_plan()
    verification = verify_album_publication_plan_identity(
        plan,
        current_album_sha256="BAD",
        current_side_identities={
            "A": {"project_revision": 1},
            "B": plan.sides[1].current_identity,
        },
        current_side_speed_selections=current_speeds(plan),
    )

    assert [item.code for item in verification.mismatches] == [
        "current_album_identity_invalid",
        "current_side_identity_invalid",
    ]


def test_identity_verification_fails_closed_on_stale_speed_selection() -> None:
    plan = representative_plan()
    speeds = current_speeds(plan)
    speeds["A"] = SpeedSelection(digest("different-speed"), 0.99)

    verification = verify_album_publication_plan_identity(
        plan,
        current_album_sha256=plan.album_sha256,
        current_side_identities={
            side.label: side.current_identity for side in plan.sides
        },
        current_side_speed_selections=speeds,
    )

    assert [item.code for item in verification.mismatches] == [
        "side_selected_speed_state_sha256_mismatch",
        "side_selected_effective_speed_factor_mismatch",
    ]


def test_loader_rejects_noncanonical_array_order(tmp_path: Path) -> None:
    payload = representative_plan().to_dict()
    payload["body"]["sides"].reverse()
    path = tmp_path / "reordered.json"
    write_payload(path, payload)

    with pytest.raises(ProjectValidationError, match="canonical deterministic order"):
        load_album_publication_plan(path)


def test_bounded_loader_rejects_oversized_file(tmp_path: Path) -> None:
    path = tmp_path / "huge.json"
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))

    with pytest.raises(ProjectValidationError, match="exceeds"):
        load_album_publication_plan(path)


def test_loader_rejects_path_swap_between_lstat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "plan.json"
    path.write_text("{}", encoding="utf-8")
    replacement = tmp_path / "replacement.json"
    with replacement.open("wb") as handle:
        handle.seek(4 * 1024 * 1024)
        handle.write(b"x")
    real_open = publication_plan_module.os.open
    swapped = False

    def swapping_open(path_value: object, flags: int) -> int:
        nonlocal swapped
        if not swapped and Path(path_value) == path:
            replacement.replace(path)
            swapped = True
        return real_open(path_value, flags)

    monkeypatch.setattr(publication_plan_module.os, "open", swapping_open)
    with pytest.raises(ProjectValidationError, match="changed before"):
        load_album_publication_plan(path)
    assert swapped is True
