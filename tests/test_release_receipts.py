from __future__ import annotations

import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_active_release_receipts_match_the_publication_boundary() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    assert version == "1.1.0"

    contract = (ROOT / "BUILD_CONTRACT_1.1.md").read_text(encoding="utf-8")
    build_receipt = (ROOT / "BUILD_CYCLE_RECEIPT.md").read_text(encoding="utf-8")
    continuum_receipt = (ROOT / "CONTINUUM_PROJECT_STATE_RECEIPT.md").read_text(
        encoding="utf-8"
    )

    assert "Status: ACTIVE — owner-authorized GitHub publication" in contract
    current_contract = contract.split("## Historical preparation boundaries", 1)[0]
    assert "codex/publication-ci-1.1.0" in contract
    assert "conditional on current gates" in current_contract
    assert "stable tag/release requires the final exact public commit" in current_contract
    assert "no remote publication is authorized" not in current_contract
    assert "Publication remains out of scope" not in current_contract
    for receipt in (build_receipt, continuum_receipt):
        assert f"Groove Serpent {version}" in receipt
        assert "Status: RELEASE_PUBLICATION_BOUNDARY" in receipt
        assert "codex/publication-ci-1.1.0" in receipt
        assert "local" in receipt.casefold()
        assert "Groove Serpent 1.0.0" not in receipt
        assert "exact" in receipt.casefold()
        assert "external" in receipt.casefold()

    assert "HOLD_REVIEW" in build_receipt
    assert "successful current gates before stable release" in build_receipt
    assert "not a claim that" in build_receipt
    assert "Stable publication remains conditional on exact-byte review" in (
        continuum_receipt
    )
    assert "does not assert that either pending operation has run" in continuum_receipt
    assert "After the authorized repair/publication and final receipts, stop" in (
        continuum_receipt
    )


def test_prior_release_receipts_are_explicitly_historical() -> None:
    history = ROOT / "docs" / "history" / "1.0.0"
    assert (history / "BUILD_CYCLE_RECEIPT.md").is_file()
    assert (history / "CONTINUUM_PROJECT_STATE_RECEIPT.md").is_file()


def test_stable_version_package_has_matching_development_classifier() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = project["project"]["classifiers"]
    assert "Development Status :: 5 - Production/Stable" in classifiers
    assert "Development Status :: 4 - Beta" not in classifiers
