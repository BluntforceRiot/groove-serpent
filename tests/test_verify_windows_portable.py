from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts import verify_windows_portable as verifier


APP_VERSION = "1.0.0"
PYTHON_VERSION = "3.11.9"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write(root: Path, relative: str, payload: bytes) -> None:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _payload_members(root: Path) -> list[dict[str, object]]:
    members = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path.name == verifier.MANIFEST_NAME:
            continue
        payload = path.read_bytes()
        members.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": _sha256(payload),
                "size": len(payload),
            }
        )
    return members


def _input(
    role: str,
    filename: str,
    digest: str,
    **extra: str,
) -> dict[str, object]:
    return {"role": role, "filename": filename, "sha256": digest, **extra}


def _manifest(root: Path) -> dict[str, object]:
    members = _payload_members(root)
    by_path = {str(item["path"]): item for item in members}
    inputs = [
        _input(
            "groove-serpent-license",
            "LICENSE",
            str(by_path["LICENSES/GROOVE-SERPENT-LICENSE.txt"]["sha256"]),
        ),
        _input(
            "groove-serpent-wheel",
            "groove_serpent-1.0.0-py3-none-any.whl",
            "1" * 64,
            distribution="groove-serpent",
            version=APP_VERSION,
        ),
        _input(
            "portable-verifier",
            "verify_windows_portable.py",
            str(by_path["verify-portable.py"]["sha256"]),
        ),
        _input(
            "python-embed",
            "python-3.11.9-embed-amd64.zip",
            "2" * 64,
            version=PYTHON_VERSION,
        ),
        _input(
            "third-party-notices",
            "THIRD-PARTY-NOTICES.txt",
            str(by_path["THIRD-PARTY-NOTICES.txt"]["sha256"]),
        ),
        _input(
            "windows-media-runtime",
            "groove-serpent-windows-media-8.1.2-x86_64.zip",
            "5" * 64,
        ),
        _input(
            "windows-media-corresponding-source",
            verifier.WINDOWS_MEDIA_SOURCE_FILENAME,
            str(by_path[verifier.WINDOWS_MEDIA_SOURCE_PATH]["sha256"]),
            destination=verifier.WINDOWS_MEDIA_SOURCE_PATH,
        ),
        _input(
            "dependency-wheel",
            "numpy-2.4.6-cp311-cp311-win_amd64.whl",
            "3" * 64,
            distribution="numpy",
            version="2.4.6",
        ),
    ]
    for destination in verifier.REQUIRED_SKILL_FILES:
        path = f"skills/groove-serpent/{destination}"
        inputs.append(
            _input(
                "skill-file",
                Path(destination).name,
                str(by_path[path]["sha256"]),
                destination=destination,
            )
        )
    inputs.sort(key=lambda item: (str(item["role"]), str(item["filename"])))
    return {
        "schema": verifier.MANIFEST_SCHEMA,
        "app": {"name": "groove-serpent", "version": APP_VERSION},
        "platform": verifier.PLATFORM,
        "build_epoch": 315_532_800,
        "builder": {
            "name": "scripts/build_windows_portable.py",
            "sha256": "4" * 64,
        },
        "inputs": inputs,
        "payload": {
            "member_count": len(members),
            "total_bytes": sum(int(item["size"]) for item in members),
            "members": members,
        },
        "smoke": {
            "app_version": APP_VERSION,
            "python_version": PYTHON_VERSION,
            "architecture": "64-bit",
            "ffmpeg_version": "ffmpeg version synthetic",
            "ffprobe_version": "ffprobe version synthetic",
            "libsoxr": "exercised-ready",
            "chromaprint": "ffmpeg-muxer-exercised-ready",
            "app_fingerprint_parity": "exact-match",
            "media_capability_smoke_sha256": str(by_path["tools/CAPABILITY-SMOKE.json"]["sha256"]),
            "media_runtime_profile": "groove-serpent-minimal-audio-shared-v1",
            "synthetic_supported_formats": "fresh-capability-smoke-exact-match",
            "web_resources": "six-required-assets-readable",
            "doctor_ready": True,
        },
        "publication": {
            "mode": "new-side-by-side-directory",
            "replacement": "refused",
            "rollback": "run-an-older-intact-version-directory",
        },
        "code_signing": {
            "status": "unsigned",
            "claim": "No Authenticode or other code signing was performed.",
        },
        "owner_data": ("not accepted or discovered; only explicit exact build inputs were copied"),
    }


def _write_manifest(root: Path, manifest: dict[str, object]) -> str:
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (root / verifier.MANIFEST_NAME).write_bytes(payload)
    return _sha256(payload)


def _bundle(root: Path) -> str:
    files = {
        verifier.WINDOWS_MEDIA_SOURCE_PATH: b"synthetic corresponding source archive",
        "LICENSES/GROOVE-SERPENT-LICENSE.txt": b"Synthetic Apache fixture\n",
        "README-PORTABLE.txt": b"Synthetic portable fixture\n",
        "THIRD-PARTY-NOTICES.txt": b"Synthetic notices fixture\n",
        "app/groove_serpent/__init__.py": b'__version__ = "1.0.0"\n',
        "app/groove_serpent-1.0.0.dist-info/METADATA": (
            b"Metadata-Version: 2.4\nName: groove-serpent\nVersion: 1.0.0\n"
        ),
        "groove-serpent.cmd": verifier.expected_app_launcher(),
        "runtime/python.exe": b"synthetic python runtime",
        "runtime/python311._pth": b"python311.zip\n.\n..\\app\nimport site\n",
        "runtime/python311.zip": b"synthetic stdlib",
        "verify-portable.cmd": verifier.expected_verifier_launcher(),
        "verify-portable.py": b"# synthetic verifier fixture\n",
    }
    files.update(
        {
            f"tools/{name}": f"synthetic:{name}\n".encode("utf-8")
            for name in verifier.WINDOWS_MEDIA_BINARIES
        }
    )
    capability = (
        json.dumps(
            {
                "schema": "groove-serpent.windows-media-capability-smoke/1",
                "result": "passed",
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    files["tools/CAPABILITY-SMOKE.json"] = capability
    files["tools/FFMPEG-CONFIGURE.txt"] = b"synthetic configure\n"
    media_payloads = {
        path.removeprefix("tools/"): payload
        for path, payload in files.items()
        if path.startswith("tools/")
    }
    runtime_files = [
        {
            "path": name,
            "sha256": _sha256(payload),
            "size_bytes": len(payload),
        }
        for name, payload in sorted(media_payloads.items())
    ]
    build_manifest = (
        json.dumps(
            {
                "schema": "groove-serpent.windows-media-runtime-manifest/1",
                "artifact": {
                    "architecture": "x86_64-w64-mingw32",
                    "ffmpeg_version": "8.1.2",
                    "profile": "groove-serpent-minimal-audio-shared-v1",
                },
                "runtime_files": runtime_files,
                "capability_smoke_sha256": _sha256(capability),
            },
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    files["tools/BUILD-MANIFEST.json"] = build_manifest
    media_payloads["BUILD-MANIFEST.json"] = build_manifest
    files["tools/SHA256SUMS"] = "".join(
        f"{_sha256(payload)}  {name}\n" for name, payload in sorted(media_payloads.items())
    ).encode("utf-8")
    for name in verifier.REQUIRED_WEB_ASSETS:
        files[f"app/groove_serpent/web/{name}"] = f"asset:{name}\n".encode("utf-8")
    for name in verifier.REQUIRED_SKILL_FILES:
        files[f"skills/groove-serpent/{name}"] = f"skill:{name}\n".encode("utf-8")
    for relative, payload in files.items():
        _write(root, relative, payload)
    return _write_manifest(root, _manifest(root))


class WindowsPortableVerifierTests(unittest.TestCase):
    def test_valid_bundle_supports_external_anchor_and_consistency_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            digest = _bundle(root)
            anchored = verifier.verify_portable_directory(
                root,
                expected_manifest_sha256=digest.upper(),
            )
            consistency = verifier.verify_portable_directory(root)
            self.assertIn(b'--root "%~dp0."', verifier.expected_verifier_launcher())
            self.assertTrue(anchored["ok"])
            self.assertEqual(
                anchored["authenticity"],
                "anchored-to-expected-manifest-sha256",
            )
            self.assertEqual(
                consistency["authenticity"],
                "consistency-only-no-external-trust-anchor",
            )
            self.assertEqual(anchored["manifest_sha256"], digest)

    def test_cli_emits_one_json_document_and_nonzero_for_anchor_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _bundle(root)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = verifier.main(
                    [
                        "--root",
                        str(root),
                        "--expected-manifest-sha256",
                        "0" * 64,
                    ]
                )
            self.assertNotEqual(status, 0)
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            report = json.loads(lines[0])
            self.assertFalse(report["ok"])
            self.assertEqual(report["error"]["code"], "manifest_hash")

    def test_member_tamper_truncation_missing_and_extra_are_rejected(self) -> None:
        cases = ("tamper", "truncate", "missing", "extra")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _bundle(root)
                target = root / "tools" / "ffmpeg.exe"
                if case == "tamper":
                    target.write_bytes(b"same-length-bad!")
                elif case == "truncate":
                    target.write_bytes(b"short")
                elif case == "missing":
                    target.unlink()
                else:
                    (root / "unexpected.txt").write_text("extra", encoding="utf-8")
                with self.assertRaises(verifier.VerificationError):
                    verifier.verify_portable_directory(root)

    def test_duplicate_json_keys_and_nan_are_rejected(self) -> None:
        for case in ("duplicate", "nan"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _bundle(root)
                path = root / verifier.MANIFEST_NAME
                text = path.read_text(encoding="utf-8")
                if case == "duplicate":
                    text = text.replace(
                        "{\n",
                        '{\n  "schema": "duplicate",\n',
                        1,
                    )
                else:
                    text = text.replace('"build_epoch": 315532800', '"build_epoch": NaN')
                path.write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(
                    verifier.VerificationError,
                    "duplicate|non-finite",
                ):
                    verifier.verify_portable_directory(root)

    def test_manifest_portable_collision_count_and_size_drift_are_rejected(self) -> None:
        for case in ("collision", "count", "size"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                _bundle(root)
                manifest = json.loads((root / verifier.MANIFEST_NAME).read_text(encoding="utf-8"))
                payload = manifest["payload"]
                if case == "collision":
                    duplicate = dict(payload["members"][0])
                    duplicate["path"] = duplicate["path"].swapcase()
                    payload["members"].append(duplicate)
                    payload["member_count"] += 1
                    payload["total_bytes"] += duplicate["size"]
                elif case == "count":
                    payload["member_count"] += 1
                else:
                    payload["total_bytes"] += 1
                _write_manifest(root, manifest)
                with self.assertRaises(verifier.VerificationError):
                    verifier.verify_portable_directory(root)

    def test_manifest_paths_require_exact_canonical_segments(self) -> None:
        for value in ("tools//ffmpeg.exe", "tools/./ffmpeg.exe", "./tools/ffmpeg.exe"):
            with self.subTest(value=value), self.assertRaises(verifier.VerificationError):
                verifier._safe_relative(value, "fixture member")

    def test_static_launcher_identity_is_checked_after_member_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _bundle(root)
            (root / "groove-serpent.cmd").write_bytes(b"@echo off\r\npython app.py\r\n")
            _write_manifest(root, _manifest(root))
            with self.assertRaisesRegex(verifier.VerificationError, "launcher"):
                verifier.verify_portable_directory(root)

    def test_symlink_is_rejected_when_platform_can_create_one(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _bundle(root)
            link = root / "linked.txt"
            try:
                link.symlink_to(root / "README-PORTABLE.txt")
            except OSError as exc:
                self.skipTest(f"Symlink creation unavailable: {exc}")
            with self.assertRaisesRegex(verifier.VerificationError, "symlink|reparse"):
                verifier.verify_portable_directory(root)


if __name__ == "__main__":
    unittest.main()
