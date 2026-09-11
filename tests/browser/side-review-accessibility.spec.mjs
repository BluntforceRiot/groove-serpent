import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { startFixture, stopFixture } from "./fixture-process.mjs";
import { createStartupAudioMonitor } from "./startup-audio-monitor.mjs";

const repositoryRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
);

let fixtureProcess;
let fixture;
let browserProblems;
let expectedMediaFailureWarning;
let startupAudioMonitor;

function monitorPage(page, problems) {
  const audioMonitor = startupAudioMonitor;
  page.on("request", (request) => audioMonitor.requestStarted(request));
  page.on("requestfinished", async (request) => {
    audioMonitor.requestFinished(request, await request.response());
  });
  page.on("console", (message) => {
    if (message.type() === "error") {
      const location = message.location();
      problems.push(
        `console: ${message.text()} @ ${location.url || "unknown"}:${location.lineNumber}`,
      );
    }
  });
  page.on("pageerror", (error) => problems.push(`page: ${error.message}`));
  page.on("requestfailed", (request) => {
    const failure = request.failure()?.errorText || "failed";
    const pathname = new URL(request.url()).pathname;
    if (
      request.resourceType() === "media"
      && request.method() === "GET"
      && pathname === "/audio"
      && ["net::ERR_ABORTED", "NS_BINDING_ABORTED"].includes(failure)
    ) {
      return;
    }
    const message = `request: ${request.method()} ${request.url()} ${failure}`;
    if (!audioMonitor.holdStartupCancellation(request, failure, message)) {
      problems.push(message);
    }
  });
}

async function loadSideReview(page) {
  startupAudioMonitor.beginLoad();
  await page.goto(fixture.url, { waitUntil: "domcontentloaded" });
  await expect(page).toHaveTitle("Groove Serpent Review");
  await expect(page.locator("#sourceIntegrity")).toHaveText("SOURCE VERIFIED");
  await expect(page.locator("#status")).toHaveText("Ready");
  await expect(page.getByRole("heading", { level: 2, name: "Drag the cut markers" }))
    .toBeVisible();
  await expect(page.locator("#evidenceStatus")).not.toContainText(
    "Select any track marker",
  );
  await expect(page.locator("#evidenceStatus")).not.toHaveClass(/busy/);
  if (startupAudioMonitor.hasPending()) {
    await expect.poll(() => startupAudioMonitor.hasReplacementProof()).toBe(true);
    await expect.poll(() => page.locator("#audioPlayer").evaluate((element) => (
      element.readyState >= 1 || element.error !== null
    ))).toBe(true);
  }
  startupAudioMonitor.finishLoad(await page.locator("#audioPlayer").evaluate((element) => ({
    currentSrc: element.currentSrc, readyState: element.readyState, error: element.error?.code ?? null,
  })));
}

async function tabUntil(page, predicate, limit = 100, key = "Tab") {
  const visited = [];
  for (let index = 0; index < limit; index += 1) {
    await page.keyboard.press(key);
    if (await page.evaluate(predicate)) return;
    visited.push(await page.evaluate(() => {
      const active = document.activeElement;
      if (!(active instanceof HTMLElement)) return "none";
      return `${active.tagName.toLowerCase()}#${active.id}.${active.className}`;
    }));
  }
  throw new Error(
    `Keyboard focus did not reach the requested control in ${limit} tabs: ${visited.join(" -> ")}`,
  );
}

test.beforeEach(async ({ page, browserName }) => {
  const started = await startFixture({
    repositoryRoot,
    script: "tests/browser/serve_side_fixture.py",
    schema: "groove-serpent.side-browser-fixture/1",
    label: "Side fixture",
  });
  fixtureProcess = started.child;
  fixture = started.ready;
  browserProblems = [];
  expectedMediaFailureWarning = null;
  startupAudioMonitor = createStartupAudioMonitor({
    browserName, sourceUrl: new URL("/audio", fixture.url).href, problems: browserProblems,
  });
  monitorPage(page, browserProblems);
});

test.afterEach(async ({ page }, testInfo) => {
  try {
    if (!page.isClosed()) {
      const mediaEvents = await page.evaluate(() => window.__nativeRestorationEvents ?? null);
      if (mediaEvents) {
        await testInfo.attach("native-restoration-media-events", {
          body: JSON.stringify(mediaEvents, null, 2), contentType: "application/json",
        });
      }
    }
    startupAudioMonitor?.finishLoad(null);
  } finally {
    await stopFixture(fixtureProcess, "Side fixture");
  }
  expect(
    browserProblems.filter((problem) => problem !== expectedMediaFailureWarning),
    "Unexpected browser errors",
  ).toEqual([]);
});

test("exposes named dynamic controls and no automated WCAG A/AA violations", async ({
  page,
}) => {
  await loadSideReview(page);
  await expect(page.getByRole("main", { name: "Side review workspace" })).toBeVisible();
  await expect(page.getByRole("status", { name: "Project status" })).toHaveText("Ready");
  await expect(page.getByRole("button", { name: "Undo boundary edit" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Loop across" }))
    .toHaveAttribute("aria-pressed", "false");
  await expect(page.locator("#restorationVariantBefore"))
    .toHaveAttribute("aria-checked", "true");
  await expect(page.locator("#restorationVariantBefore")).toHaveAttribute("tabindex", "0");
  await expect(page.locator("#restorationVariantProposed")).toHaveAttribute("tabindex", "-1");

  await page.locator("#markerSelect").selectOption("1");
  await page.getByRole("button", { name: "Move marker right by one sample" }).click();
  await expect(page.getByRole("button", { name: "Undo boundary edit" })).toBeEnabled();
  await expect(page.getByRole("status", { name: "Project status" }))
    .toHaveText("Unsaved changes");

  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
});

test("keeps an abstained endpoint unchanged while reviewing the clear edge", async ({
  page,
}) => {
  await page.addInitScript(() => {
    HTMLMediaElement.prototype.play = function playVerifiedRange() {
      queueMicrotask(() => this.dispatchEvent(new Event("playing")));
      return Promise.resolve();
    };
  });
  await page.route("**/api/endpoints/status", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.state = "pending-review";
    payload.proposal = {
      proposal_sha256: "b".repeat(64),
      scopes: [{
        label: "Side A",
        scope_start_sample: 0,
        scope_end_sample_exclusive: 288000,
        status: "partial",
        start: {
          status: "abstained",
          sample: null,
          confidence: 0,
          reasons: ["contradictory_endpoint_families"],
        },
        end: {
          status: "proposed",
          sample: 280000,
          confidence: 0.84,
          reasons: ["cross_family_endpoint_agreement", "human_review_required"],
        },
        evidence: {
          family_candidates: {
            waveform_energy: { start_sample: 5000, end_sample_exclusive: 280000 },
            spectral_structure: { start_sample: 4000, end_sample_exclusive: 280000 },
          },
          transition_context: {
            quiet_before_start_samples: 4000,
            quiet_after_end_samples: 8000,
            quiet_tonal_before_start_samples: 0,
            quiet_tonal_after_end_samples: 0,
          },
          needle_confirmations: [],
        },
      }],
    };
    await route.fulfill({ response, json: payload });
  });

  await loadSideReview(page);
  await expect(page.locator("#endpointProposalBadge"))
    .toHaveText("ONE SUGGESTION · OTHER EDGE ABSTAINED");
  await expect(page.locator("#reviewEndpointStart")).toBeDisabled();
  await expect(page.locator("#reviewEndpointEnd")).toBeEnabled();
  await expect(page.locator("#endpointReviewProgress"))
    .toHaveText("Start abstained · End pending");
  await expect(page.locator("#endpointIntent")).toBeDisabled();

  await page.locator("#reviewEndpointEnd").click();
  await expect(page.locator("#endpointReviewProgress"))
    .toHaveText("Start abstained · End reviewed");
  await expect(page.locator("#endpointIntent")).toBeEnabled();
  await expect(page.locator("#acceptEndpoints")).toBeDisabled();
  await page.locator("#endpointIntent").check();
  await expect(page.locator("#acceptEndpoints")).toBeEnabled();
});

test("does not credit an endpoint audition when playback fails", async ({ page }) => {
  await page.addInitScript(() => {
    HTMLMediaElement.prototype.play = () => Promise.reject(
      new DOMException("forced review playback failure", "NotSupportedError"),
    );
  });
  await page.route("**/api/endpoints/status", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.state = "pending-review";
    payload.proposal = {
      proposal_sha256: "b".repeat(64),
      scopes: [{
        label: "Side A",
        scope_start_sample: 0,
        scope_end_sample_exclusive: 288000,
        status: "partial",
        start: {
          status: "abstained",
          sample: null,
          confidence: 0,
          reasons: ["contradictory_endpoint_families"],
        },
        end: {
          status: "proposed",
          sample: 280000,
          confidence: 0.84,
          reasons: ["cross_family_endpoint_agreement", "human_review_required"],
        },
        evidence: {
          family_candidates: {
            waveform_energy: { start_sample: 5000, end_sample_exclusive: 280000 },
            spectral_structure: { start_sample: 4000, end_sample_exclusive: 280000 },
          },
          transition_context: {
            quiet_before_start_samples: 4000,
            quiet_after_end_samples: 8000,
            quiet_tonal_before_start_samples: 0,
            quiet_tonal_after_end_samples: 0,
          },
          needle_confirmations: [],
        },
      }],
    };
    await route.fulfill({ response, json: payload });
  });

  await loadSideReview(page);
  const evidenceResponse = page.waitForResponse((response) => (
    new URL(response.url()).pathname === "/api/evidence"
      && response.request().method() === "POST"
  ));
  await page.locator("#reviewEndpointEnd").click();
  await expect(page.locator("#endpointReviewProgress"))
    .toHaveText(/Start abstained .* End pending/);
  await expect(page.locator("#endpointIntent")).toBeDisabled();
  await expect(page.locator("#acceptEndpoints")).toBeDisabled();
  expect((await evidenceResponse).status()).toBe(200);
});

async function loadRestorationAudition(page, {
  mockPlayback = true, expectDecodeFailure = false,
  changedWindowStartFrame = 960,
} = {}) {
  if (mockPlayback) await page.addInitScript(() => {
    window.__restorationTestNow = 0;
    Object.defineProperty(performance, "now", {
      configurable: true,
      value: () => window.__restorationTestNow,
    });
    // Mock source assignment too: setting src otherwise starts a native decode even
    // when load/play are mocked. Real decoding is exercised by the native-playback test.
    const getAttribute = HTMLMediaElement.prototype.getAttribute;
    const removeAttribute = HTMLMediaElement.prototype.removeAttribute;
    HTMLMediaElement.prototype.getAttribute = function getMediaAttribute(name) {
      if (name === "src" && this.__testSource !== undefined) return this.__testSource;
      return getAttribute.call(this, name);
    };
    HTMLMediaElement.prototype.removeAttribute = function removeMediaAttribute(name) {
      if (name === "src") this.__testSource = undefined;
      return removeAttribute.call(this, name);
    };
    Object.defineProperty(HTMLMediaElement.prototype, "src", {
      configurable: true,
      get() { return this.__testSource || ""; },
      set(value) { this.__testSource = new URL(value, window.location.href).href; },
    });
    Object.defineProperty(HTMLMediaElement.prototype, "currentSrc", {
      configurable: true,
      get() { return this.__testSource || ""; },
    });
    Object.defineProperty(HTMLMediaElement.prototype, "duration", {
      configurable: true,
      get() { return 2; },
    });
    Object.defineProperty(HTMLMediaElement.prototype, "currentTime", {
      configurable: true,
      get() { return Number(this.__testCurrentTime || 0); },
      set(value) { this.__testCurrentTime = Number(value); },
    });
    Object.defineProperty(HTMLMediaElement.prototype, "paused", {
      configurable: true,
      get() { return this.__testPaused !== false; },
    });
    Object.defineProperty(HTMLMediaElement.prototype, "ended", {
      configurable: true,
      get() { return false; },
    });
    Object.defineProperty(HTMLMediaElement.prototype, "seeking", {
      configurable: true,
      get() { return false; },
    });
    HTMLMediaElement.prototype.load = function loadRestorationFixture() {
      queueMicrotask(() => {
        this.dispatchEvent(new Event("loadedmetadata"));
        this.dispatchEvent(new Event("durationchange"));
      });
    };
    HTMLMediaElement.prototype.play = function playRestorationFixture() {
      this.__testPaused = false;
      this.dispatchEvent(new Event("play"));
      this.dispatchEvent(new Event("playing"));
      return Promise.resolve();
    };
    HTMLMediaElement.prototype.pause = function pauseRestorationFixture() {
      this.__testPaused = true;
      this.dispatchEvent(new Event("pause"));
    };
  });

  const preview = {
    token: `preview-${"1".repeat(32)}`,
    sha256: "1".repeat(64),
    scan_token: `scan-${"2".repeat(32)}`,
    candidates: [{
      id: "clk-browser-proof",
      type: "impulse",
      peak_frame: changedWindowStartFrame,
      start_frame: changedWindowStartFrame,
      end_frame_exclusive: changedWindowStartFrame + 128,
      channels: [0],
      confidence: 0.9,
      repairable: true,
    }],
    context: {
      start_frame: 0,
      end_frame_exclusive: 96000,
      repair_windows: [{
        candidate_id: "clk-browser-proof",
        start_in_preview: changedWindowStartFrame,
        end_in_preview_exclusive: changedWindowStartFrame + 128,
        channels: [0],
      }],
    },
    audition: {
      before_linear_gain: 1,
      proposed_linear_gain: 1,
      removed_linear_gain: 16,
    },
    metrics: { changed_scalar_samples: 128 },
    proof: { source_unchanged: true },
    audio: Object.fromEntries(["before", "proposed", "removed"].map((role) => [
      role,
      {
        token: `audio-${role}`,
        url: "/audio",
        evidence_url: `/restoration-${role}-evidence`,
        sha256: role.padEnd(64, role[0]),
      },
    ])),
  };
  let responseIdentity;
  await page.route("**/api/restoration/status", async (route) => {
    const response = await route.fetch();
    const payload = await response.json();
    payload.current_scan = {
      token: preview.scan_token,
      sha256: "2".repeat(64),
      stale: false,
      candidates: preview.candidates,
      coverage: { restoration_status: "complete" },
      summary: { retained: 1, repairable: 1, impulse: 1, clipped: 0 },
    };
    payload.current_preview = { ...preview, stale: false };
    payload.current_recipe = null;
    payload.current_render = null;
    payload.decision_journal = { current: false, authorizing: false, decisions: [] };
    payload.current_decisions = [];
    responseIdentity = {
      project_revision: payload.project_revision,
      project_sha256: payload.project_sha256,
      source_receipt: payload.source_receipt,
    };
    await route.fulfill({ response, json: payload });
  });
  for (const role of ["before", "proposed", "removed"]) {
    await page.route(`**/restoration-${role}-evidence`, async (route) => {
      expect(responseIdentity).toBeTruthy();
      await route.fulfill({
        json: {
          ok: true,
          ...responseIdentity,
          preview_token: preview.token,
          audio_token: `audio-${role}`,
          audio_sha256: preview.audio[role].sha256,
          role,
          source: { sample_rate: 48000 },
          alignment: {
            matched_audio_geometry: true,
            source_start_sample: 0,
            source_end_sample_exclusive: 96000,
            focus_source_sample: changedWindowStartFrame,
            repair_start_source_sample: changedWindowStartFrame,
            repair_end_source_sample_exclusive: changedWindowStartFrame + 128,
            declared_linear_gain: role === "removed" ? 16 : 1,
          },
          selection: { start_sample: 0, end_sample_exclusive: 96000 },
          waveform: { channels: [] },
          spectrogram: { dbfs: [] },
        },
      });
    });
  }

  await loadSideReview(page);
  await expect(page.locator("#restorationTransportStatus"))
    .toContainText(expectDecodeFailure
      ? "could not decode"
      : "Sample-aligned audio + visuals ready");
}

const restorationPlayerSelectors = [
  "#restorationOriginal", "#restorationProposed", "#restorationRemoved",
];

async function auditionWholeChangedWindow(page) {
  for (const selector of restorationPlayerSelectors) {
    await page.locator(selector).evaluate(async (element) => {
      element.pause();
      element.playbackRate = 1;
      element.currentTime = 0;
      element.dispatchEvent(new Event("seeking"));
      element.dispatchEvent(new Event("seeked"));
      await element.play();
      for (const current of [0.01, 0.021, 0.021, 1088 / 48000]) {
        window.__restorationTestNow += (current - element.currentTime) * 1000;
        element.currentTime = current;
        element.dispatchEvent(new Event("timeupdate"));
      }
      element.pause();
    });
  }
  await expect(page.locator("#restorationTransportStatus"))
    .toContainText("3/3 signals played through the changed window");
  await expect(page.getByRole("button", { name: "Apply proposed" })).toBeEnabled();
}

test("credits restoration audition only after full changed-window coverage", async ({ page }) => {
  await loadRestorationAudition(page);
  const apply = page.getByRole("button", { name: "Apply proposed" });
  await expect(apply).toBeDisabled();
  for (const selector of restorationPlayerSelectors) {
    const player = page.locator(selector);
    await player.evaluate(async (element) => {
      await element.play();
      window.__restorationTestNow += 10;
      element.currentTime = 0.01;
      element.dispatchEvent(new Event("timeupdate"));
    });
    await expect(page.locator("#restorationTransportStatus"))
      .toContainText("0/3 signals played through the changed window");
    await player.evaluate((element) => {
      window.__restorationTestNow += 11;
      element.currentTime = 0.021;
      element.dispatchEvent(new Event("timeupdate"));
    });
    await expect(page.locator("#restorationTransportStatus"))
      .toContainText("0/3 signals played through the changed window");
  }
  await expect(apply).toBeDisabled();
  await auditionWholeChangedWindow(page);
});

for (const interruption of [
  "starts-inside", "pause", "seek", "backwards", "rate", "source", "waiting", "gap", "jump",
]) {
  test(`restoration audition rejects ${interruption} and permits a clean replay`, async ({ page }) => {
    await loadRestorationAudition(page);
    for (const selector of restorationPlayerSelectors) {
      await page.locator(selector).evaluate(async (element, kind) => {
        const originalSource = element.src;
        if (kind === "starts-inside") element.currentTime = 0.021;
        await element.play();
        if (kind !== "starts-inside") {
          window.__restorationTestNow += 21;
          element.currentTime = 0.021;
          element.dispatchEvent(new Event("timeupdate"));
        }
        if (kind === "pause") {
          element.pause();
          await element.play();
        } else if (kind === "seek") {
          element.dispatchEvent(new Event("seeking"));
          element.currentTime = 0.022;
          element.dispatchEvent(new Event("seeked"));
        } else if (kind === "backwards") {
          element.currentTime = 0.0205;
          element.dispatchEvent(new Event("timeupdate"));
        } else if (kind === "rate") {
          element.playbackRate = 2;
          element.dispatchEvent(new Event("ratechange"));
          element.playbackRate = 1;
          element.dispatchEvent(new Event("ratechange"));
        } else if (kind === "source") {
          element.src = `${originalSource}?changed-preview`;
          element.dispatchEvent(new Event("loadstart"));
          element.src = originalSource;
        } else if (kind === "waiting") {
          element.dispatchEvent(new Event("waiting"));
          element.dispatchEvent(new Event("playing"));
        } else if (kind === "gap") {
          window.__restorationTestNow += 1500;
        }
        window.__restorationTestNow += 3;
        element.currentTime = kind === "jump" ? 1 : 0.024;
        element.dispatchEvent(new Event("timeupdate"));
        element.pause();
        if (kind === "source") element.src = originalSource;
      }, interruption);
    }
    await expect(page.locator("#restorationTransportStatus"))
      .toContainText("0/3 signals played through the changed window");
    await expect(page.getByRole("button", { name: "Apply proposed" })).toBeDisabled();
    await auditionWholeChangedWindow(page);
  });
}

test("handles native restoration playback without bypassing failed decoders", async ({
  page, browserName,
}, testInfo) => {
  await page.addInitScript(() => {
    window.__nativeRestorationEvents = [];
    for (const name of [
      "loadedmetadata", "playing", "waiting", "stalled", "seeking", "seeked",
      "timeupdate", "pause", "ended", "error",
    ]) {
      document.addEventListener(name, (event) => {
        const element = event.target;
        if (!(element instanceof HTMLMediaElement) || !element.id.startsWith("restoration")) return;
        window.__nativeRestorationEvents.push({
          event: name, player: element.id, observed: performance.now(),
          currentTime: element.currentTime, paused: element.paused, seeking: element.seeking,
          readyState: element.readyState, error: element.error?.code ?? null,
        });
      }, true);
    }
  });
  const unavailableDecoder = browserName === "webkit" && process.platform === "win32";
  await loadRestorationAudition(page, {
    mockPlayback: false, expectDecodeFailure: unavailableDecoder,
    // Native media may settle a queued seek or cold audio sink after playing.
    // Leave one second of real preroll inside this two-second preview; keep the
    // deterministic mocked 20 ms window and all fail-closed credit gates intact.
    changedWindowStartFrame: 48000,
  });
  if (unavailableDecoder) {
    // Windows Playwright WebKit advertises FLAC but rejects the actual fixture bytes,
    // including outside the app as a data URL. Exercise the real fail-closed UI, not a skip.
    for (const selector of restorationPlayerSelectors) {
      await expect.poll(() => page.locator(selector).evaluate((element) => element.error?.code))
        .toBe(4);
    }
    await expect(page.locator("#restorationPlayPause")).toBeDisabled();
    await expect(page.getByRole("button", { name: "Apply proposed" })).toBeDisabled();
    const mediaFailureStyleWarning = "Refused to apply a stylesheet because its hash, its nonce, "
      + "or 'unsafe-inline' appears in neither the style-src directive nor the default-src "
      + "directive of the Content Security Policy.";
    expectedMediaFailureWarning =
      `console: ${mediaFailureStyleWarning} @ ${new URL(fixture.url).origin}/:0`;
    testInfo.annotations.push({
      type: "runtime-limitation",
      description: "Windows WebKit FLAC decoder unavailable; real failure remains non-authorizing.",
    });
  } else {
    for (const [index, selector] of restorationPlayerSelectors.entries()) {
      await page.locator(selector).evaluate(async (element) => {
        element.currentTime = 0;
        await element.play();
      });
      await expect(page.locator("#restorationTransportStatus"))
        .toContainText(`${index + 1}/3 signals played through the changed window`);
      await page.locator(selector).evaluate((element) => element.pause());
      if (index < 2) {
        await expect(page.getByRole("button", { name: "Apply proposed" })).toBeDisabled();
      }
    }
    await expect(page.getByRole("button", { name: "Apply proposed" })).toBeEnabled();
  }
  await page.locator("#restorationPreviewPanel").scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("restoration-audition-ready.png") });
});

test("keeps backend-valid 33 rpm to 78 rpm correction operable", async ({ page }) => {
  await loadSideReview(page);
  await page.locator("#speedCaptureRpm").fill("33.333333333");
  await page.locator("#speedIntendedRpm").fill("78.260000000");
  await page.locator("#speedFineFactor").fill("1.000000000");
  await page.locator("#speedFineFactor").evaluate((element) => {
    element.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect(page.locator("#speedSummary")).not.toHaveText("INVALID SPEED SETTINGS");
  await expect(page.locator("#listenCorrected")).toBeEnabled();
});

test("supports skip, boundary editing, and dismissible review panels by keyboard", async ({
  page,
  browserName,
}) => {
  await loadSideReview(page);
  const macWebKit = browserName === "webkit" && process.platform === "darwin";
  const forwardKey = macWebKit ? "Alt+Tab" : "Tab";
  const reverseKey = macWebKit ? "Alt+Shift+Tab" : "Shift+Tab";

  await tabUntil(
    page,
    () => document.activeElement?.classList.contains("skip-link"),
    10,
    forwardKey,
  );
  await expect(page.getByRole("link", { name: "Skip to side review" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("main", { name: "Side review workspace" })).toBeFocused();

  await tabUntil(
    page,
    () => document.activeElement?.id === "findReleaseButton",
    100,
    forwardKey,
  );
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: "Match this record" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Match this record" })).toBeHidden();
  await expect(page.locator("#findReleaseButton")).toBeFocused();

  await tabUntil(page, () => document.activeElement?.id === "waveform", 100, forwardKey);
  const before = await page.locator("#markerReadout").textContent();
  const evidenceBefore = await page.locator("#evidenceFocusReadout").textContent();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("button", { name: "Undo boundary edit" })).toBeEnabled();
  await expect(page.locator("#markerReadout")).not.toHaveText(before || "");
  await expect(page.locator("#evidenceFocusReadout")).not.toHaveText(
    evidenceBefore || "",
  );
  await expect(page.locator("#evidenceStatus")).not.toHaveClass(/busy/);
  await expect(page.locator("#evidenceStatus")).toContainText(
    "aligned to the selected marker",
  );

  await page.locator("#sideReviewMain").focus();
  await page.keyboard.press(reverseKey);
  await expect(page.locator("#exportButton")).toBeFocused();
  const focusStyle = await page.locator("#exportButton").evaluate((element) => {
    const style = getComputedStyle(element);
    return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth };
  });
  expect(focusStyle.outlineStyle).not.toBe("none");
  expect(focusStyle.outlineWidth).not.toBe("0px");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog", { name: "Export tracks" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Export tracks" })).toBeHidden();
  await expect(page.locator("#exportButton")).toBeFocused();
});

test("reflows at 200 and 400 percent equivalents and tolerates 200 percent text", async ({
  page,
}) => {
  for (const width of [640, 320]) {
    await page.setViewportSize({ width, height: 720 });
    await loadSideReview(page);
    const overflow = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      document: document.documentElement.scrollWidth,
      body: document.body.scrollWidth,
      tableViewport: document.querySelector(".table-wrap")?.clientWidth || 0,
      tableContent: document.querySelector(".table-wrap")?.scrollWidth || 0,
    }));
    expect(overflow.document).toBeLessThanOrEqual(overflow.viewport);
    expect(overflow.body).toBeLessThanOrEqual(overflow.viewport);
    expect(overflow.tableContent).toBeGreaterThanOrEqual(overflow.tableViewport);
  }

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.evaluate(() => {
    document.styleSheets[0].insertRule(":root { font-size: 200% !important; }", 0);
  });
  const scaled = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
    rootFontSize: getComputedStyle(document.documentElement).fontSize,
    overflowing: [...document.querySelectorAll("body *")]
      .filter((element) => element.getBoundingClientRect().right > innerWidth + 1)
      .slice(0, 20)
      .map((element) => ({
        tag: element.tagName,
        id: element.id,
        className: String(element.className),
        right: Math.round(element.getBoundingClientRect().right),
        width: Math.round(element.getBoundingClientRect().width),
      })),
    headings: [...document.querySelectorAll("h1, h2")]
      .filter((heading) => heading.checkVisibility())
      .map((heading) => ({
        text: heading.textContent?.trim() || "",
        height: heading.getBoundingClientRect().height,
        clientHeight: heading.clientHeight,
        scrollHeight: heading.scrollHeight,
        overflow: getComputedStyle(heading).overflow,
      })),
  }));
  expect(scaled.rootFontSize).toBe("32px");
  expect(scaled.document, JSON.stringify(scaled.overflowing)).toBeLessThanOrEqual(
    scaled.viewport,
  );
  expect(scaled.body, JSON.stringify(scaled.overflowing)).toBeLessThanOrEqual(
    scaled.viewport,
  );
  expect(
    scaled.headings.every((heading) => (
      heading.height > 0
      && (heading.overflow === "visible" || heading.scrollHeight <= heading.clientHeight + 1)
    )),
    JSON.stringify(scaled.headings),
  ).toBe(true);
});

test("honors reduced motion and retains operable focus in forced colors", async ({
  page,
  browserName,
}) => {
  test.skip(browserName !== "chromium", "Forced-colors emulation is Chromium-scoped.");
  await page.emulateMedia({ reducedMotion: "reduce", forcedColors: "active" });
  await loadSideReview(page);
  await page.evaluate(() => {
    window.__sideReviewScrollBehaviors = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function patchedScrollIntoView(options) {
      window.__sideReviewScrollBehaviors.push(options?.behavior || "auto");
      return original.call(this, options);
    };
  });
  await page.locator("#findReleaseButton").click();
  const media = await page.evaluate(() => ({
    reduced: matchMedia("(prefers-reduced-motion: reduce)").matches,
    forced: matchMedia("(forced-colors: active)").matches,
    scrollBehaviors: window.__sideReviewScrollBehaviors,
  }));
  expect(media.reduced).toBe(true);
  expect(media.forced).toBe(true);
  expect(media.scrollBehaviors).not.toContain("smooth");

  await page.keyboard.press("Escape");
  await expect(page.locator("#findReleaseButton")).toBeFocused();
  const forcedFocus = await page.locator("#findReleaseButton").evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      outlineStyle: style.outlineStyle,
      outlineWidth: style.outlineWidth,
      visible: element.getBoundingClientRect().width > 0,
    };
  });
  expect(forcedFocus.visible).toBe(true);
  expect(forcedFocus.outlineStyle).not.toBe("none");
  expect(forcedFocus.outlineWidth).not.toBe("0px");
});
