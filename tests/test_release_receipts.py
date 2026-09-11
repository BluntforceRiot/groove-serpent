from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_release_receipts_match_the_stable_version_candidate() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    assert version == "1.1.0"

    contract = (ROOT / "BUILD_CONTRACT_1.1.md").read_text(encoding="utf-8")
    build_receipt = (ROOT / "BUILD_CYCLE_RECEIPT.md").read_text(encoding="utf-8")
    continuum_receipt = (ROOT / "CONTINUUM_PROJECT_STATE_RECEIPT.md").read_text(
        encoding="utf-8"
    )

    assert "Status: ACTIVE" in contract
    for receipt in (build_receipt, continuum_receipt):
        assert f"Groove Serpent {version}" in receipt
        assert "codex/1.1.0-dev" in receipt
        assert "local" in receipt.casefold()
        assert "no push" in receipt.casefold()
        assert "Groove Serpent 1.0.0" not in receipt


def test_prior_release_receipts_are_explicitly_historical() -> None:
    history = ROOT / "docs" / "history" / "1.0.0"
    assert (history / "BUILD_CYCLE_RECEIPT.md").is_file()
    assert (history / "CONTINUUM_PROJECT_STATE_RECEIPT.md").is_file()


def test_stable_version_package_has_matching_development_classifier() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = project["project"]["classifiers"]
    assert "Development Status :: 5 - Production/Stable" in classifiers
    assert "Development Status :: 4 - Beta" not in classifiers
