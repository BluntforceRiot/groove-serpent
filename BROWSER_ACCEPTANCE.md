# Browser and accessibility acceptance

This report records the checked-in Groove Serpent 1.0 browser gate. It distinguishes automated
browser evidence from spoken screen-reader and native-platform evidence that has not been obtained.

## Reproduce

From the repository root:

```powershell
uv sync --frozen --group dev
npm ci
npx playwright install chromium firefox webkit
npm run test:browser
```

The fixtures create short synthetic FLAC sources and temporary side/album projects, serve the real
loopback applications, and remove the complete temporary workspace and process tree after every
test. They never read or write an owner capture. Browser binaries are development dependencies, not
Groove Serpent runtime dependencies.

CI repeats the suite on Ubuntu with isolated declared Playwright projects. A checked-in workflow is
not itself proof that an exact public release commit passed; the final release receipt must identify
the corresponding hosted jobs.

## Exact 1.0 candidate local result

After the package was aligned to `1.0.0`, the complete local matrix passed **42 tests with 2
expected engine-inapplicable skips** in **225.4 seconds**:

| Project | Engine | Result |
| --- | --- | --- |
| `chromium` | Chromium | 11 passed |
| `firefox` | Firefox | 10 passed, 1 forced-colors skip |
| `webkit` | Playwright WebKit | 10 passed, 1 forced-colors skip |
| `mobile-chromium` | Chromium, Pixel 7 emulation | 11 passed |

The Firefox/WebKit skips are explicit capability skips for a forced-colors emulation path those
Playwright engines do not support. They are not retries or hidden failures. The run used Windows
10.0.26200.8457, Python 3.11.15, Node.js 22.22.3, Playwright 1.61.1,
`@axe-core/playwright` 4.12.1, and a full FFmpeg 8.1.x build.

The command returned zero, used one worker with no retries, and post-run inspection found zero
Groove Serpent Python or browser-fixture processes. The final source/artifact hashes and hosted-job
status still belong in `BUILD_CYCLE_RECEIPT.md`.

### Sanitized public-tree rerun

The independently materialized public tree at
`public-release/groove-serpent-1.0.0` repeated the complete matrix from its own frozen environment.
It passed the same **42 tests with 2 expected engine-inapplicable forced-colors skips** in
**224.4 seconds**. The command returned zero, used one worker with no retries, and post-run
inspection again found zero Groove Serpent Python, fixture, or relevant Playwright processes.
This proves the sanitized tree retained the checked-in browser behavior; it does not replace the
later hash-bound source-archive and hosted-job gates.

## Directly exercised behavior

- Dynamic two-side Album Workbench state through the real local server.
- Automated axe WCAG 2.0/2.1 A/AA checks with no retained violations.
- Exact navigation into a hash-verified side cockpit with visible waveform and synchronized
  spectrogram, one-sample marker movement, and undo.
- Keyboard-only skip navigation, exception navigation, deliberate repin, decision controls, and
  focus return.
- Reviewed album metadata persistence and rediscovery after reload.
- Explicit album-wide fingerprint consent, deterministic ranked release consensus, immutable
  proposal persistence, physical-pressing warning, close/reopen review, and unchanged source hash.
- Reviewed publication-plan creation, archival-source execution, strict verification, final-receipt
  rediscovery, and another full reload through product UI.
- Current evidence after a one-sample marker nudge, including synchronized non-busy evidence state.
- Desktop and mobile containment, 200% text zoom, 200% and 400% page reflow, reduced motion, and
  forced colors where the engine supports emulation.
- No unexpected console errors, uncaught page exceptions, failed requests, or leaked fixture
  servers.
- The session cookie was absent at raw-IP and sibling-host loopback origins. This is deliberately
  not described as generic cross-port isolation: browser cookies are host-only, not port-bound.

## Findings corrected by the gate

- Low-contrast helper text was raised to an accessible palette value.
- WebKit native form controls received explicit foreground and text-fill colors.
- The side cockpit skip link received explicit `tabindex="0"` for deterministic keyboard entry.
- The keyboard harness now uses host-appropriate WebKit traversal instead of applying a macOS-only
  key sequence on Windows.
- Exact evidence requests now finish one bounded current request, ignore stale results, and queue
  the newest exact bounds. This avoids overlapping WebKit request-body rewinds while preserving
  newest-bounds behavior.
- Tests wait for the post-nudge evidence refresh rather than tearing down while a real request is
  still in flight.
- Node fixture launchers own an stdin control pipe; Python servers shut down when it closes. Windows
  teardown has a scoped process-tree fallback and fails if any fixture process survives.

Five consecutive focused WebKit reruns of the corrected evidence/keyboard path passed before the
complete matrix was replayed.

## Honest limits

- A same-user process that learns the exact random review hostname from terminal or browser-profile
  state and can navigate the browser to that hostname on another port can receive and replay the
  host-only cookie. Hostile same-user processes and browser-profile disclosure are outside the
  tested boundary. Running without automatic browser launch intentionally prints the one-time
  bootstrap URL once; normal browser launch and HTTP request logging do not print it.
- Axe and keyboard automation do not substitute for an owner completing tasks with Narrator, NVDA,
  VoiceOver, or another screen reader. Spoken-output evidence remains open.
- Playwright WebKit is not branded Safari, and the local Windows run is not native macOS evidence.
- Forced-colors emulation is unavailable in the declared Firefox/WebKit projects.
- Identification uses a deterministic offline provider to prove browser/server contracts; it does
  not establish current live AcoustID, MusicBrainz, or Cover Art Archive accuracy.
- Publication uses short synthetic audio. Real owner-album audition and player/library behavior are
  separate acceptance evidence.
- Localization and operating-system-level text scaling outside the tested browser transformations
  remain open.

No open item above is converted into an implied pass.
