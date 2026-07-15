from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import groove_serpent.album_publication_catalog as catalog_module
from groove_serpent.album_publication_catalog import (
    discover_album_publication_plan_catalog,
)
from groove_serpent.album_publication_plan import ALBUM_PUBLICATION_PLAN_SCHEMA
from groove_serpent.errors import ProjectValidationError


def _plan(
    album_path: Path,
    album_sha256: str,
    digest: str,
) -> SimpleNamespace:
    side = SimpleNamespace(
        restoration_render=None,
        restoration_no_derivative=None,
    )
    return SimpleNamespace(
        schema=ALBUM_PUBLICATION_PLAN_SCHEMA,
        album_reference=album_path.name,
        album_sha256=album_sha256,
        plan_sha256=digest,
        selected_profiles=("archival-source",),
        sides=(side,),
    )


class AlbumPublicationCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.album_path = self.root / "record.groove-album.json"
        self.album_path.write_bytes(b"album")
        self.album_sha256 = hashlib.sha256(b"album").hexdigest()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _album_loader(self, _path: Path) -> tuple[object, str]:
        return object(), self.album_sha256

    def test_rediscovers_current_stale_and_invalid_without_mutating(self) -> None:
        current_path = self.root / "record-current.publication-plan.json"
        stale_path = self.root / "record-stale.publication-plan.json"
        invalid_path = self.root / "record-invalid.publication-plan.json"
        current_path.write_bytes(b"current")
        stale_path.write_bytes(b"stale")
        invalid_path.write_bytes(b"invalid")
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in (self.album_path, current_path, stale_path, invalid_path)
        }
        current_digest = "1" * 64
        stale_digest = "2" * 64
        current = _plan(self.album_path, self.album_sha256, current_digest)
        stale = _plan(self.album_path, "3" * 64, stale_digest)

        def load(path: Path) -> tuple[SimpleNamespace, str]:
            if path.name == current_path.name:
                return current, hashlib.sha256(b"current").hexdigest()
            if path.name == stale_path.name:
                return stale, hashlib.sha256(b"stale").hexdigest()
            raise ProjectValidationError("Publication plan is malformed.")

        with mock.patch.object(
            catalog_module,
            "load_album_project_with_sha256",
            side_effect=self._album_loader,
        ), mock.patch.object(
            catalog_module,
            "load_album_publication_plan_with_sha256",
            side_effect=load,
        ), mock.patch.object(
            catalog_module,
            "preflight_album_publication_plan",
            return_value=SimpleNamespace(plan_sha256=current_digest),
        ) as preflight:
            catalog = discover_album_publication_plan_catalog(
                self.album_path,
                expected_album_sha256=self.album_sha256,
            )

        self.assertEqual(
            [(entry.filename, entry.status) for entry in catalog.entries],
            [
                (current_path.name, "current"),
                (invalid_path.name, "invalid"),
                (stale_path.name, "stale"),
            ],
        )
        self.assertEqual(preflight.call_count, 1)
        self.assertEqual(preflight.call_args.args[0].name, current_path.name)
        self.assertTrue(catalog.scan_complete)
        self.assertEqual(catalog.to_dict()["summary"], {
            "total": 3,
            "current": 1,
            "stale": 1,
            "invalid": 1,
        })
        for path, receipt in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), receipt)

    def test_portable_equivalent_candidates_fail_closed(self) -> None:
        first = self.root / "Plan.publication-plan.json"
        second = self.root / "plan.publication-plan.json"
        plan = _plan(self.album_path, self.album_sha256, "4" * 64)
        candidates = [
            catalog_module._Candidate(first, plan, "5" * 64, None),
            catalog_module._Candidate(second, plan, "6" * 64, None),
        ]
        with mock.patch.object(
            catalog_module,
            "load_album_project_with_sha256",
            side_effect=self._album_loader,
        ), mock.patch.object(
            catalog_module,
            "_scan_candidates",
            return_value=(candidates, True, []),
        ), mock.patch.object(
            catalog_module,
            "preflight_album_publication_plan",
        ) as preflight:
            catalog = discover_album_publication_plan_catalog(self.album_path)

        self.assertEqual([entry.status for entry in catalog.entries], ["invalid", "invalid"])
        self.assertEqual(
            {entry.issues[0].code for entry in catalog.entries},
            {"portable_name_collision"},
        )
        preflight.assert_not_called()

    def test_live_preflight_work_is_bounded_and_excess_is_not_current(self) -> None:
        candidates = []
        by_path: dict[Path, SimpleNamespace] = {}
        for index in range(9):
            path = self.root / f"record-{index}.publication-plan.json"
            plan = _plan(self.album_path, self.album_sha256, f"{index + 1:x}" * 64)
            candidates.append(
                catalog_module._Candidate(path, plan, f"{index + 2:x}" * 64, None)
            )
            by_path[path] = plan

        def load(path: Path) -> tuple[SimpleNamespace, str]:
            candidate = next(item for item in candidates if item.path == path)
            return by_path[path], candidate.file_sha256 or "0" * 64

        def preflight(path: Path) -> SimpleNamespace:
            return SimpleNamespace(plan_sha256=by_path[path].plan_sha256)

        with mock.patch.object(
            catalog_module,
            "load_album_project_with_sha256",
            side_effect=self._album_loader,
        ), mock.patch.object(
            catalog_module,
            "_scan_candidates",
            return_value=(candidates, True, []),
        ), mock.patch.object(
            catalog_module,
            "load_album_publication_plan_with_sha256",
            side_effect=load,
        ), mock.patch.object(
            catalog_module,
            "preflight_album_publication_plan",
            side_effect=preflight,
        ) as run_preflight:
            catalog = discover_album_publication_plan_catalog(self.album_path)

        self.assertEqual(run_preflight.call_count, 8)
        self.assertFalse(catalog.scan_complete)
        self.assertEqual(sum(entry.status == "current" for entry in catalog.entries), 8)
        excess = next(
            entry
            for entry in catalog.entries
            if entry.issues and entry.issues[0].code == "live_preflight_not_run"
        )
        self.assertEqual(excess.status, "stale")
        self.assertIn(
            "live_preflight_limit_exceeded",
            {issue.code for issue in catalog.issues},
        )

    def test_plan_change_after_preflight_is_invalid(self) -> None:
        plan_path = self.root / "record-race.publication-plan.json"
        plan_path.write_bytes(b"plan")
        plan = _plan(self.album_path, self.album_sha256, "a" * 64)
        calls = 0

        def load(_path: Path) -> tuple[SimpleNamespace, str]:
            nonlocal calls
            calls += 1
            return plan, ("b" * 64 if calls == 1 else "c" * 64)

        with mock.patch.object(
            catalog_module,
            "load_album_project_with_sha256",
            side_effect=self._album_loader,
        ), mock.patch.object(
            catalog_module,
            "load_album_publication_plan_with_sha256",
            side_effect=load,
        ), mock.patch.object(
            catalog_module,
            "preflight_album_publication_plan",
            return_value=SimpleNamespace(plan_sha256=plan.plan_sha256),
        ):
            catalog = discover_album_publication_plan_catalog(self.album_path)

        self.assertEqual(catalog.entries[0].status, "invalid")
        self.assertEqual(
            catalog.entries[0].issues[0].code,
            "plan_changed_during_discovery",
        )


if __name__ == "__main__":
    unittest.main()
