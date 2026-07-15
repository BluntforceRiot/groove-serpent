from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from groove_serpent.cli import build_parser

try:
    from scripts import build_handoff
except ImportError:  # Sanitized public releases intentionally omit private handoff tooling.
    build_handoff = None  # type: ignore[assignment]

try:
    from scripts import build_public_release
except ImportError:  # The generated public tree cannot recursively publicize itself.
    build_public_release = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent.parent
SKILL_DIRECTORY = ROOT / "skills" / "groove-serpent"
SKILL_PATH = SKILL_DIRECTORY / "SKILL.md"
CONTRACT_PATH = SKILL_DIRECTORY / "references" / "authority-contract.json"
OPENAI_PATH = SKILL_DIRECTORY / "agents" / "openai.yaml"
SEMANTIC_VERSION = re.compile(
    r"(?<![A-Za-z0-9])\d+\.\d+\.\d+(?:[._-]?(?:a|b|rc|dev)\d+)?(?![A-Za-z0-9])",
    re.IGNORECASE,
)


class AgentSkillContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = SKILL_PATH.read_text(encoding="utf-8")
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_runtime_and_version_are_resolved_not_hardcoded(self) -> None:
        runtime = self.contract["runtime"]
        resolved_root = (SKILL_DIRECTORY / runtime["root_from_skill_directory"]).resolve()
        self.assertEqual(resolved_root, ROOT.resolve())
        source = runtime["source_checkout"]
        portable = runtime["windows_portable"]
        self.assertEqual(runtime["selection_order"], ["windows-portable", "source-checkout"])
        self.assertEqual(source["version_source"], "src/groove_serpent/__init__.py")
        self.assertTrue((resolved_root / source["version_source"]).is_file())
        self.assertEqual(
            source["version_probe"],
            ["uv", "run", "--frozen", "python", "-m", "groove_serpent", "--version"],
        )
        self.assertEqual(portable["version_probe"], ["groove-serpent.cmd", "--version"])
        self.assertEqual(
            portable["required_files"],
            [
                "groove-serpent.cmd",
                "PORTABLE-MANIFEST.json",
                "runtime/python.exe",
                "verify-portable.cmd",
                "verify-portable.py",
            ],
        )
        self.assertEqual(
            portable["verification"],
            {
                "before_application_execution": True,
                "expected_manifest_sha256_source": (
                    "separately-trusted-release-receipt-or-explicit-owner-value"
                ),
                "derive_expected_hash_from_bundle": "forbidden",
                "without_external_expected_hash": "consistency-only-not-authenticity",
                "required_authenticity": "anchored-to-expected-manifest-sha256",
                "required_result": "strict-json-zero-exit-and-ok-true",
                "required_fingerprint_backend": "ffmpeg-chromaprint",
            },
        )
        self.assertEqual(
            source["fingerprint_backend"],
            "ffmpeg-chromaprint-or-fpcalc-after-confirmed-muxer-absence",
        )
        self.assertEqual(portable["system_python"], "forbidden")
        self.assertEqual(portable["uv"], "not-required")
        self.assertEqual(runtime["missing_or_ambiguous"], "stop")
        self.assertIsNone(SEMANTIC_VERSION.search(self.skill))
        self.assertIsNone(SEMANTIC_VERSION.search(CONTRACT_PATH.read_text(encoding="utf-8")))
        if build_public_release is not None:
            self.assertNotIn(
                Path("skills/groove-serpent/SKILL.md"),
                build_public_release.VERSION_REWRITE_PATHS,
            )

    def test_machine_contract_preserves_agent_authority_barriers(self) -> None:
        self.assertEqual(self.contract["schema"], "groove-serpent.agent-authority/1")
        self.assertEqual(
            self.contract["privacy"],
            {
                "audio_upload": "forbidden",
                "audio_excerpt_upload": "forbidden",
                "review_server_binding": "loopback-only",
                "metadata_and_artwork_network": "explicit-owner-action-only",
            },
        )
        self.assertEqual(
            self.contract["identity"],
            {
                "stale_or_mismatched_state": "stop",
                "automatic_retry": False,
                "automatic_overwrite": False,
                "next_step": "reload-current-state-create-new-artifact-require-new-review",
            },
        )
        self.assertEqual(
            self.contract["json"],
            {
                "completed_handler_stdout": "one-strict-json-document",
                "domain_negative_result": "json-report-with-authoritative-nonzero-exit",
                "usage_or_validation_exception": (
                    "nonzero-exit-with-stderr-diagnostic-not-success"
                ),
                "parse": "json-parser-only",
                "allow_nan": False,
                "coerce_values": False,
                "strict_input_unknown_fields": "reject",
            },
        )

        actions = self.contract["agent_actions"]
        expected_owner_only = {
            "attest-audition",
            "accept-or-reject-endpoint-proposal",
            "approve-reject-or-protect-restoration-candidate",
            "apply-metadata-or-topology",
            "save-reviewed-markers",
            "repin-album-side",
            "render-reviewed-restoration",
            "recover-publication",
            "reject-continuous-preview-proposal",
            "render-continuous-preview-audition",
        }
        expected_claim_barriers = {
            "owner-audition-without-current-owner-statement",
            "fabricated-human-review",
            "fabricated-checkbox-or-gesture",
            "approval-inferred-from-score",
        }
        expected_forbidden_routes = {
            "/api/save",
            "/api/export",
            "/api/checkpoint",
            "/api/metadata/apply",
            "/api/topology/apply",
            "/api/endpoints/reject",
            "/api/endpoints/accept",
            "/api/restoration/recipe",
            "/api/restoration/render",
            "/api/album/repin",
            "/api/album/add-side",
            "/api/album/remove-side",
            "/api/album/reorder-sides",
            "/api/album/update-details",
            "/api/album/publication/create-plan",
            "/api/album/publication/execute",
            "/api/album/publication/replay",
            "/api/album/publication/recover",
        }
        allowed = set(actions["allowed"])
        owner_only = set(actions["owner_only"])
        self.assertEqual(actions["unlisted"], "forbidden")
        forbidden_routes = actions["forbidden_http_post_routes"]
        self.assertEqual(owner_only, expected_owner_only)
        self.assertEqual(set(actions["forbidden_claims"]), expected_claim_barriers)
        self.assertEqual(set(forbidden_routes), expected_forbidden_routes)
        self.assertEqual(len(forbidden_routes), len(expected_forbidden_routes))
        self.assertTrue(allowed.isdisjoint(owner_only))
        self.assertNotIn("repin-album-side", allowed)
        self.assertNotIn("create-restoration-recipe", allowed)
        self.assertNotIn("replay-publication-to-new-path", allowed)
        self.assertEqual(
            actions["forbidden_agent_commands"],
            [
                ["album", "repin"],
                ["click-recipe"],
                ["click-render"],
                ["continuous-preview", "attest"],
                ["continuous-preview", "render"],
                ["continuous-preview", "reject"],
            ],
        )

        execute = actions["conditional"]["execute-immutable-publication-plan"]
        self.assertEqual(
            execute,
            {
                "explicit_owner_request_for_exact_plan": True,
                "plan_already_owner_reviewed": True,
                "fresh_preflight_required": True,
                "destination_must_not_exist": True,
            },
        )
        replay = actions["conditional"]["replay-publication-to-new-path"]
        self.assertEqual(
            replay,
            {
                "explicit_owner_request_for_exact_publication_and_plan": True,
                "publication_and_plan_already_owner_reviewed": True,
                "fresh_preflight_required": True,
                "destination_must_not_exist": True,
                "original_publication_must_be_retained": True,
            },
        )

    def test_skill_points_to_contract_and_owner_loopback_surfaces(self) -> None:
        self.assertIn(
            "[references/authority-contract.json](references/authority-contract.json)",
            self.skill,
        )
        self.assertIn("Invoke-GrooveSerpent review PROJECT", self.skill)
        self.assertIn("Invoke-GrooveSerpent album review ALBUM_PROJECT", self.skill)
        self.assertIn('Join-Path $GrooveRoot "groove-serpent.cmd"', self.skill)
        self.assertIn("do not invoke `uv`, a system Python", self.skill)
        self.assertIn("$GroovePortable -eq $GrooveSource", self.skill)
        self.assertIn("verify-portable.cmd", self.skill)
        self.assertIn("A separately trusted portable manifest SHA-256 is required", self.skill)
        self.assertIn("Never call the Album Workbench publication create-plan", self.skill)
        self.assertIn("Do not automate the approval controls.", self.skill)
        self.assertLess(len(self.skill.splitlines()), 220)

        openai = OPENAI_PATH.read_text(encoding="utf-8")
        self.assertIn("$groove-serpent", openai)

    def test_documented_cli_spellings_are_current_parser_surfaces(self) -> None:
        parser = build_parser()
        cases = (
            (["doctor", "--path", "DESTINATION", "--json"], "doctor", None, None),
            (["info", "PROJECT", "--json"], "info", None, None),
            (["review", "PROJECT"], "review", None, None),
            (["review", "PROJECT", "--endpoint-proposal", "PROPOSAL"], "review", None, None),
            (["album", "review", "ALBUM_PROJECT"], "album", "review", None),
            (
                ["endpoints", "propose", "PROJECT", "--output", "PROPOSAL", "--json"],
                "endpoints",
                "propose",
                None,
            ),
            (
                ["endpoints", "inspect", "PROPOSAL", "--json"],
                "endpoints",
                "inspect",
                None,
            ),
            (
                [
                    "continuous-preview",
                    "context",
                    "PROJECT",
                    "--kind",
                    "crackle",
                    "--output",
                    "EXPECTED",
                    "--json",
                ],
                "continuous-preview",
                "context",
                None,
            ),
            (
                ["continuous-preview", "catalog", "PROJECT", "--json"],
                "continuous-preview",
                "catalog",
                None,
            ),
            (
                [
                    "speed",
                    "estimate",
                    "PROJECT",
                    "--tracklist",
                    "TRACKLIST",
                    "--boundary-review",
                    "BOUNDARY_REVIEW",
                    "--output",
                    "PROPOSAL",
                    "--json",
                ],
                "speed",
                "estimate",
                None,
            ),
            (["click-scan", "PROJECT", "--report", "SCAN"], "click-scan", None, None),
            (
                [
                    "click-preview",
                    "PROJECT",
                    "SCAN",
                    "--candidate",
                    "CANDIDATE",
                    "--bundle",
                    "NEW_PREVIEW_DIRECTORY",
                ],
                "click-preview",
                None,
                None,
            ),
            (["album", "inspect", "ALBUM_PROJECT", "--json"], "album", "inspect", None),
            (
                ["album", "publication", "plan", "ALBUM_PROJECT", "PLAN"],
                "album",
                "publication",
                "plan",
            ),
            (
                ["album", "publication", "preflight", "PLAN", "--json"],
                "album",
                "publication",
                "preflight",
            ),
            (
                ["album", "publication", "execute", "PLAN", "NEW_DIRECTORY"],
                "album",
                "publication",
                "execute",
            ),
            (
                ["album", "publication", "verify", "NEW_DIRECTORY", "--json"],
                "album",
                "publication",
                "verify",
            ),
            (
                [
                    "album",
                    "publication",
                    "replay",
                    "PUBLICATION",
                    "PLAN",
                    "NEW_REPLAY_DIRECTORY",
                    "--json",
                ],
                "album",
                "publication",
                "replay",
            ),
        )
        for arguments, command, nested, publication in cases:
            with self.subTest(arguments=arguments):
                parsed = parser.parse_args(arguments)
                self.assertEqual(parsed.command, command)
                if command == "endpoints":
                    self.assertEqual(parsed.endpoints_command, nested)
                elif command == "speed":
                    self.assertEqual(parsed.speed_command, nested)
                elif command == "continuous-preview":
                    self.assertEqual(parsed.continuous_preview_command, nested)
                elif command == "album":
                    self.assertEqual(parsed.album_command, nested)
                    if nested == "publication":
                        self.assertEqual(parsed.album_publication_command, publication)

    def test_contract_is_in_public_release_and_handoff_packages(self) -> None:
        if build_public_release is None or build_handoff is None:
            self.skipTest("Private release/handoff builders are intentionally absent.")
        relative = Path("skills/groove-serpent/references/authority-contract.json")
        self.assertIn("skills/groove-serpent", build_public_release.SOURCE_TREES)
        self.assertIn("skills/groove-serpent", build_handoff.TREES)
        handoff_members = {path.relative_to(ROOT) for path in build_handoff.included_files()}
        self.assertIn(relative, handoff_members)

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "public-release"
            build_public_release.build_public_release(output)
            packaged_contract = output / relative
            self.assertTrue(packaged_contract.is_file())
            self.assertEqual(packaged_contract.read_bytes(), CONTRACT_PATH.read_bytes())


if __name__ == "__main__":
    unittest.main()
