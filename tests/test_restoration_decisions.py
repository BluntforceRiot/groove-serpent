from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from groove_serpent.errors import ProjectValidationError
from groove_serpent.restoration_decisions import (
    decision_journal_token,
    read_decision_journal,
    seal_decision_journal,
    validate_decision_journal,
    write_decision_journal,
)


class RestorationDecisionJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name).resolve()
        self.path = self.directory / "owner-decisions.json"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _journal(*, decision: str = "rejected") -> dict[str, object]:
        return seal_decision_journal(
            project={
                "path": "side.groove.json",
                "revision": 7,
                "state_sha256": "1" * 64,
                "sha256": "2" * 64,
            },
            source={
                "path": "side.flac",
                "sha256": "3" * 64,
                "size_bytes": 1_000,
                "sample_rate": 48_000,
                "channels": 2,
                "bits_per_raw_sample": 24,
                "sample_count": 96_000,
                "codec_name": "flac",
            },
            scan={"token": f"scan-{'4' * 32}", "sha256": "4" * 64},
            decisions=[
                {
                    "candidate_id": "clk-one",
                    "candidate_sha256": "5" * 64,
                    "decision": decision,
                    "preview": {
                        "token": f"preview-{'6' * 32}",
                        "sha256": "6" * 64,
                    },
                }
            ],
        )

    def test_round_trip_is_exact_self_hashed_and_non_authorizing(self) -> None:
        journal = self._journal()
        digest = write_decision_journal(
            self.path,
            journal,
            expected_file_sha256=None,
        )
        loaded, observed = read_decision_journal(self.path)
        self.assertEqual(loaded, journal)
        self.assertEqual(observed, digest)
        self.assertEqual(decision_journal_token(digest), f"decision-{digest[:32]}")
        self.assertEqual(
            loaded["authority"],
            {
                "channel": "same-origin-owner-cookie",
                "scope": "non-authorizing-partial-decisions",
                "claim": "owner-channel-action-not-human-perception",
            },
        )

    def test_compare_failure_preserves_exact_existing_journal(self) -> None:
        first = self._journal()
        first_sha256 = write_decision_journal(
            self.path,
            first,
            expected_file_sha256=None,
        )
        first_bytes = self.path.read_bytes()
        with self.assertRaisesRegex(ProjectValidationError, "another process"):
            write_decision_journal(
                self.path,
                self._journal(decision="pending-approval"),
                expected_file_sha256="0" * 64,
            )
        self.assertEqual(self.path.read_bytes(), first_bytes)
        self.assertEqual(hashlib.sha256(first_bytes).hexdigest(), first_sha256)

    def test_tamper_duplicate_json_and_linked_path_fail_closed(self) -> None:
        journal = self._journal()
        tampered = json.loads(json.dumps(journal))
        tampered["decisions"][0]["decision"] = "pending-approval"
        with self.assertRaisesRegex(ProjectValidationError, "body hash"):
            validate_decision_journal(tampered)

        duplicate = (
            b'{"schema":"groove-serpent.restoration-decision-journal/1",'
            b'"schema":"groove-serpent.restoration-decision-journal/1"}'
        )
        self.path.write_bytes(duplicate)
        with self.assertRaisesRegex(ProjectValidationError, "invalid JSON"):
            read_decision_journal(self.path)

        target = self.directory / "foreign.json"
        target.write_text("{}", encoding="utf-8")
        self.path.unlink()
        try:
            os.symlink(target, self.path)
        except (NotImplementedError, OSError):
            self.skipTest("This host does not permit an unprivileged symlink fixture.")
        with self.assertRaises(ProjectValidationError):
            read_decision_journal(self.path)


if __name__ == "__main__":
    unittest.main()
