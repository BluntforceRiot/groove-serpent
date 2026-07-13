## What changed

Describe the user-visible problem and the focused solution.

## Preservation and privacy

Explain any effect on source immutability, project/source identity, restoration scope, publication receipts, network access, storage, or performance.

## Synthetic verification

List the deterministic synthetic reproduction and the checks you ran. Do not attach copyrighted recordings or artwork, API keys, credentials, private project files, collection metadata, hashes, or private local paths.

## Checklist

- [ ] The source capture remains untouched; changed audio is separately rendered and reviewable.
- [ ] Automatic decisions expose uncertainty and require the appropriate human review.
- [ ] Tests use synthetic fixtures and deterministic seeds.
- [ ] I added or updated tests for behavioral changes.
- [ ] I updated public documentation where needed.
- [ ] `uv run --frozen --group dev python -m pytest -q` passes.
- [ ] `uv run --frozen --group dev python scripts/check_quality.py` passes.
- [ ] Python compilation and relevant FFmpeg/browser checks pass.
- [ ] I included no copyrighted media, credentials, private project files, or private local paths.
- [ ] I reviewed dependency, license, security, and compatibility consequences.

