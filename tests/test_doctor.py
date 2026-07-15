from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from groove_serpent.cli import main
from groove_serpent.doctor import (
    CapabilityCheck,
    _soxr_check,
    build_doctor_report,
)
from groove_serpent.subprocess_policy import BoundedProcessResult


class DoctorTests(unittest.TestCase):
    def test_soxr_check_executes_exact_bounded_smoke_path(self) -> None:
        completed = BoundedProcessResult(0, b"", b"", False, False)
        with (
            patch("groove_serpent.doctor.find_tool", return_value="ffmpeg"),
            patch(
                "groove_serpent.doctor.run_bounded_capture",
                return_value=completed,
            ) as run,
        ):
            check = _soxr_check()

        self.assertEqual(check.status, "ready")
        command = run.call_args.args[0]
        self.assertIn("-nostdin", command)
        self.assertIn("aresample=44100:resampler=soxr", command)
        self.assertEqual(command[-3:], ["-f", "null", "-"])

    def test_soxr_check_reports_backend_failure_without_throwing(self) -> None:
        completed = BoundedProcessResult(
            1,
            b"",
            b"Option 'resampler' not found",
            False,
            False,
        )
        with (
            patch("groove_serpent.doctor.find_tool", return_value="ffmpeg"),
            patch(
                "groove_serpent.doctor.run_bounded_capture",
                return_value=completed,
            ),
        ):
            check = _soxr_check()

        self.assertTrue(check.required)
        self.assertEqual(check.status, "missing")
        self.assertIn("Option 'resampler' not found", check.message)

    def test_report_is_strict_json_ready_and_required_checks_control_readiness(
        self,
    ) -> None:
        required = (
            lambda: CapabilityCheck("first", True, "ready", "ok"),
            lambda: CapabilityCheck("second", True, "missing", "no"),
        )
        with (
            patch(
                "groove_serpent.doctor.AcoustIDRecognitionProvider.readiness"
            ) as recognition,
            patch("groove_serpent.doctor.fingerprint_backend_readiness") as fingerprinting,
            patch("groove_serpent.doctor.discover_audacity") as audacity,
        ):
            fingerprinting.return_value.ready = True
            fingerprinting.return_value.message = "local fingerprinting ready"
            fingerprinting.return_value.ffmpeg = "ffmpeg"
            fingerprinting.return_value.backend = "ffmpeg-chromaprint"
            recognition.return_value.ready = False
            recognition.return_value.message = "not configured"
            audacity.return_value.script_pipe_enabled = False
            audacity.return_value.message = "not enabled"
            audacity.return_value.executable = ""
            report = build_doctor_report(required_checks=required)

        self.assertEqual(report["schema"], "groove-serpent.doctor/1")
        self.assertFalse(report["ready"])
        self.assertEqual(
            [item["capability"] for item in report["checks"]],
            [
                "first",
                "second",
                "acoustic-fingerprinting",
                "acoustic-identification",
                "audacity-script-pipe",
            ],
        )
        self.assertTrue(all(type(item) is dict for item in report["checks"]))

    def test_cli_json_is_machine_readable_and_fails_when_required_check_fails(
        self,
    ) -> None:
        output = StringIO()
        report = {
            "schema": "groove-serpent.doctor/1",
            "groove_serpent_version": "test",
            "ready": False,
            "platform": {},
            "checks": [],
        }
        with (
            patch(
                "groove_serpent.doctor.build_doctor_report",
                return_value=report,
            ),
            redirect_stdout(output),
        ):
            result = main(["doctor", "--json"])

        self.assertEqual(result, 2)
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_destination_path_adds_required_atomic_filesystem_check(self) -> None:
        required = (lambda: CapabilityCheck("base", True, "ready", "ok"),)
        with (
            tempfile.TemporaryDirectory() as directory_value,
            patch(
                "groove_serpent.doctor.AcoustIDRecognitionProvider.readiness"
            ) as recognition,
            patch("groove_serpent.doctor.fingerprint_backend_readiness") as fingerprinting,
            patch("groove_serpent.doctor.discover_audacity") as audacity,
        ):
            fingerprinting.return_value.ready = True
            fingerprinting.return_value.message = "local fingerprinting ready"
            fingerprinting.return_value.ffmpeg = "ffmpeg"
            fingerprinting.return_value.backend = "ffmpeg-chromaprint"
            recognition.return_value.ready = False
            recognition.return_value.message = "not configured"
            audacity.return_value.script_pipe_enabled = False
            audacity.return_value.message = "not enabled"
            audacity.return_value.executable = ""
            report = build_doctor_report(
                required_checks=required,
                destination_path=Path(directory_value),
            )

        atomic = next(
            item
            for item in report["checks"]
            if item["capability"] == "atomic-no-replace-filesystem"
        )
        self.assertEqual(atomic["status"], "ready")
        self.assertTrue(atomic["required"])
        self.assertTrue(report["ready"])

    def test_cli_passes_explicit_destination_path_to_doctor(self) -> None:
        report = {
            "schema": "groove-serpent.doctor/1",
            "groove_serpent_version": "test",
            "ready": True,
            "platform": {},
            "checks": [],
        }
        output = StringIO()
        with (
            patch(
                "groove_serpent.doctor.build_doctor_report",
                return_value=report,
            ) as build,
            redirect_stdout(output),
        ):
            result = main(["doctor", "--path", "future", "--json"])

        self.assertEqual(result, 0)
        build.assert_called_once_with(destination_path=Path("future"))


if __name__ == "__main__":
    unittest.main()
