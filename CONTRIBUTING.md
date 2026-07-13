# Contributing to Groove Serpent

Thank you for helping make physical-media digitization safer and more pleasant. Groove Serpent favors preservation correctness, reviewable behavior, and synthetic reproducibility over opaque automation.

## Before opening an issue

- Search existing issues at <https://github.com/BluntforceRiot/groove-serpent/issues>.
- Run `groove-serpent doctor` and note the Python, Groove Serpent, FFmpeg, and ffprobe versions.
- Reproduce the problem with synthetic audio whenever possible. `scripts/create_demo_audio.py` is the preferred starting point.
- Remove names, local paths, release metadata, hashes, and other private collection details from logs and examples.

Never attach copyrighted recordings or downloaded artwork, API keys, credentials, or private `.groove.json`, `.album.json`, scan, recipe, or publication files. Do not paste private local filesystem paths. If a reproducer needs audio, generate the smallest synthetic sample that demonstrates the behavior.

Security problems must follow [SECURITY.md](SECURITY.md), not the public issue tracker.

## Development setup

Install Python 3.11-3.13, FFmpeg/ffprobe, Node.js, and [uv](https://docs.astral.sh/uv/), then run:

```console
uv sync --frozen --group dev
uv run --frozen --group dev python -m pytest -q
uv run --frozen --group dev python scripts/check_quality.py
uv run --frozen --group dev python -m compileall -q src tests scripts
```

Tests that exercise FFmpeg should use generated audio and deterministic seeds. Tests must not depend on a contributor's collection, provider credentials, network availability, or machine-specific paths.

## Pull requests

Keep each pull request focused and explain:

- the user-visible problem;
- the preservation or identity invariants affected;
- the synthetic reproduction and tests;
- compatibility or migration consequences;
- any network, privacy, storage, or performance changes.

Do not weaken source immutability, review gates, hash binding, lossless verification, or fail-closed publication behavior to make a test pass. Processing that changes audio must remain separately rendered and reviewable.

Run the full local checks before submitting. CI is expected to pass on Windows, Ubuntu, and macOS with supported Python versions. Documentation and tests should accompany behavioral changes.

By submitting a contribution, you agree that it may be distributed under the repository's Apache-2.0 license.

