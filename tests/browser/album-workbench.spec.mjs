import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import http from "node:http";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { startFixture, stopFixture } from "./fixture-process.mjs";

const repositoryRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
);

let fixtureProcess;
let fixture;
let browserProblems;

async function loadWorkbench(page) {
  await page.goto(fixture.url, { waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench")).toHaveAttribute("aria-busy", "false");
  await expect(page.getByRole("heading", { level: 1, name: "Fixture Album" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Needs Attention" })).toBeVisible();
}

async function tabUntil(page, predicate, limit = 80) {
  for (let index = 0; index < limit; index += 1) {
    await page.keyboard.press("Tab");
    if (await page.evaluate(predicate)) return;
  }
  throw new Error(`Keyboard focus did not reach the requested control in ${limit} tabs.`);
}

async function shiftTabUntil(page, predicate, limit = 80) {
  for (let index = 0; index < limit; index += 1) {
    await page.keyboard.press("Shift+Tab");
    if (await page.evaluate(predicate)) return;
  }
  throw new Error(
    "Reverse keyboard focus did not reach the requested control in " + limit + " tabs.",
  );
}

function monitorPage(page) {
  page.on("console", (message) => {
    if (message.type() === "error") browserProblems.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => browserProblems.push(`page: ${error.message}`));
  page.on("requestfailed", (request) => {
    const failure = request.failure()?.errorText || "failed";
    const pathname = new URL(request.url()).pathname;
    // Media engines may abort a speculative /audio range after buffering enough data.
    // No other URL, resource type, method, or failure is exempted.
    if (
      request.resourceType() === "media"
      && request.method() === "GET"
      && pathname === "/audio"
      && ["net::ERR_ABORTED", "NS_BINDING_ABORTED"].includes(failure)
    ) {
      return;
    }
    browserProblems.push(
      `request: ${request.method()} ${request.url()} ${failure}`,
    );
  });
}

async function sha256File(filename) {
  return createHash("sha256").update(await readFile(filename)).digest("hex");
}

test.beforeEach(async ({ page }) => {
  const started = await startFixture({
    repositoryRoot,
    script: "tests/browser/serve_album_fixture.py",
    schema: "groove-serpent.browser-fixture/1",
    label: "Album fixture",
  });
  fixtureProcess = started.child;
  fixture = started.ready;
  browserProblems = [];
  monitorPage(page);
});

test.afterEach(async () => {
  await stopFixture(fixtureProcess, "Album fixture");
  expect(browserProblems, "Unexpected browser errors").toEqual([]);
});

test("has an accessible dynamic album state with no serious WCAG A/AA violations", async ({
  page,
}) => {
  await loadWorkbench(page);
  await expect(page.getByRole("article", { name: "Side A" })).toBeVisible();
  await expect(page.getByRole("article", { name: "Side B" })).toBeVisible();
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
});

test("does not disclose its session cookie to raw-IP or sibling-host loopback origins", async ({
  page,
}) => {
  await loadWorkbench(page);
  const workbenchHost = new URL(fixture.url).hostname;
  const sessionCookie = (await page.context().cookies()).find(
    (cookie) => cookie.name.startsWith("groove_serpent_") && cookie.domain === workbenchHost,
  );
  expect(sessionCookie).toBeTruthy();
  expect(workbenchHost).toMatch(/^groove-serpent-[a-f0-9]{32}\.localhost$/);

  const observed = [];
  const attacker = http.createServer((request, response) => {
    observed.push({
      host: request.headers.host || "",
      cookie: request.headers.cookie || "",
      path: request.url || "",
    });
    response.setHeader("Content-Type", "text/plain; charset=utf-8");
    response.end("unrelated loopback service");
  });
  await new Promise((resolve, reject) => {
    attacker.once("error", reject);
    attacker.listen(0, "127.0.0.1", resolve);
  });
  try {
    const address = attacker.address();
    expect(typeof address).toBe("object");
    const port = address.port;
    await page.goto(`http://127.0.0.1:${port}/raw-loopback`);
    await page.goto(`http://unrelated-groove-serpent-probe.localhost:${port}/sibling-host`);
  } finally {
    await new Promise((resolve) => attacker.close(resolve));
  }

  const targetCookie = `${sessionCookie.name}=${sessionCookie.value}`;
  const navigationPaths = observed
    .map((request) => request.path)
    .filter((path) => path !== "/favicon.ico");
  expect(navigationPaths).toEqual([
    "/raw-loopback",
    "/sibling-host",
  ]);
  for (const request of observed) {
    expect(request.cookie.split(/;\s*/)).not.toContain(targetCookie);
  }
});

test("scans, persists, closes, and reopens ranked release evidence without changing audio", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const sourcePath = path.join(path.dirname(fixture.album_path), "shared-source.flac");
  const beforeSourceSha256 = await sha256File(sourcePath);
  await loadWorkbench(page);

  // Identification requires every exact side identity to be current. The
  // fixture's deliberate Side A drift must be reviewed first.
  await page.locator("#reviewedCheckbox").check();
  await page.getByRole("button", { name: "Repin reviewed side" }).click();
  await expect(page.locator("#identificationReadiness")).toHaveText(
    "Ready for explicit scan",
  );
  await expect(page.locator("#identificationProvider")).toContainText(
    "fixture-local-fingerprint",
  );
  const beforeAlbumSha256 = await sha256File(fixture.album_path);

  await page.locator("#identificationNetworkReviewed").check();
  await page.getByRole("button", { name: "Scan for release candidates" }).click();
  await expect(page.locator("#identificationStatus")).toContainText(
    "4 of 4 tracks matched",
    { timeout: 60_000 },
  );
  await expect(page.locator("#identificationReview")).toBeVisible();
  await expect(page.locator("#identificationCandidates")).toContainText("Fixture Album");
  await expect(page.locator("#identificationCandidates")).toContainText("100.0%", {
    ignoreCase: true,
  });
  await expect(page.locator("#identificationPressing")).toContainText(
    "Physical pressing is not proven",
  );
  await expect(page.locator("#identificationDecision")).toContainText(
    "source audio was not modified",
  );
  await expect(page.locator("#identificationCatalog .current")).toHaveCount(1);

  await page.locator("#releaseDetailsNetworkReviewed").check();
  await page.getByRole("button", { name: "Fetch details for Fixture Album" }).click();
  await expect(page.locator("#releaseDetailsReview")).toBeVisible();
  await expect(page.locator("#releaseDetailsContent")).toContainText("Fixture Records");
  await expect(page.locator("#releaseDetailsContent")).toContainText("FIX-001");
  await expect(page.locator("#releaseDetailsContent")).toContainText("0123456789012");
  await expect(page.locator("#releaseDetailsContent")).toContainText(
    "not physical-pressing proof",
  );
  await expect(page.locator("#releaseDetailsContent .release-tracklist li")).toHaveCount(4);
  await page.getByRole("button", { name: "Copy facts to manual form" }).click();
  await expect(page.locator("#metadataAlbum")).toHaveValue("Fixture Album");
  await expect(page.locator("#detailsStatus")).toContainText("Nothing is saved");

  await page.locator("#artworkNetworkReviewed").check();
  await page.getByRole("button", { name: "Download front artwork for review" }).click();
  await expect(page.locator("#artworkReview")).toBeVisible();
  await expect(page.locator("#artworkReview")).toContainText("not applied", {
    ignoreCase: true,
  });
  const preview = page.locator("#artworkReview img");
  await expect(preview).toBeVisible();
  await expect.poll(() => preview.evaluate((image) => image.naturalWidth)).toBeGreaterThan(0);
  await page.getByRole("button", { name: "Copy reviewed path to manual form" }).click();
  await expect(page.locator("#artworkPath")).toHaveValue(
    "artwork/review/11111111-1111-4111-8111-111111111111-front-1200.png",
  );
  expect(await sha256File(fixture.album_path)).toBe(beforeAlbumSha256);
  expect(await sha256File(sourcePath)).toBe(beforeSourceSha256);

  const axe = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(axe.violations).toEqual([]);

  await page.getByRole("button", { name: "Close evidence" }).click();
  await expect(page.locator("#identificationReview")).toBeHidden();
  await page.getByRole("button", { name: "Review ranked candidates" }).click();
  await expect(page.locator("#identificationStatus")).toContainText("read-only", {
    timeout: 30_000,
  });
  await expect(page.locator("#identificationReview")).toBeVisible();
  expect(await sha256File(sourcePath)).toBe(beforeSourceSha256);
});

test("opens the exact side cockpit with synchronized visual and marker controls", async ({
  page,
}) => {
  await loadWorkbench(page);
  const popupPromise = page.waitForEvent("popup");
  await page.getByRole("button", { name: "Open exact Side A review" }).click();
  const cockpit = await popupPromise;
  monitorPage(cockpit);
  await cockpit.waitForURL(
    (url) => url.protocol === "http:" && url.pathname === "/",
    { waitUntil: "domcontentloaded" },
  );
  await expect(cockpit.locator("#sourceIntegrity")).toHaveText("SOURCE VERIFIED");
  await expect(cockpit.getByRole("heading", { level: 2, name: "Drag the cut markers" }))
    .toBeVisible();
  await expect(
    cockpit.getByRole("heading", {
      level: 2,
      name: "Waveform + spectrum + audio context",
    }),
  ).toBeVisible();

  await cockpit.locator("#markerSelect").selectOption("1");
  await expect(cockpit.locator("#markerReadout")).toContainText("Marker 2/3");
  await expect(cockpit.locator("#evidenceStatus")).not.toContainText(
    "Select any track marker",
  );
  const dimensions = await cockpit.evaluate(() => ({
    waveformWidth: document.getElementById("waveform")?.getBoundingClientRect().width || 0,
    waveformHeight: document.getElementById("waveform")?.getBoundingClientRect().height || 0,
    evidenceWidth: document.getElementById("evidenceCanvas")?.getBoundingClientRect().width || 0,
    evidenceHeight: document.getElementById("evidenceCanvas")?.getBoundingClientRect().height || 0,
  }));
  expect(dimensions.waveformWidth).toBeGreaterThan(0);
  expect(dimensions.waveformHeight).toBeGreaterThan(0);
  expect(dimensions.evidenceWidth).toBeGreaterThan(0);
  expect(dimensions.evidenceHeight).toBeGreaterThan(0);

  const axe = await new AxeBuilder({ page: cockpit })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  const computedStyles = await cockpit.evaluate(() => (
    [".evidence-zoom > label", "#evidenceZoom", ".history-restore > label"].map(
      (selector) => {
        const element = document.querySelector(selector);
        const style = element ? getComputedStyle(element) : null;
        return {
          selector,
          color: style?.color || "",
          backgroundColor: style?.backgroundColor || "",
          webkitTextFillColor: style?.webkitTextFillColor || "",
        };
      },
    )
  ));
  expect(
    axe.violations,
    "Computed side-cockpit styles: " + JSON.stringify(computedStyles),
  ).toEqual([]);

  await cockpit.getByRole("button", { name: "Move marker right by one sample" }).click();
  await expect(cockpit.getByRole("button", { name: "Undo boundary edit" })).toBeEnabled();
  await cockpit.getByRole("button", { name: "Undo boundary edit" }).click();
  await expect(cockpit.locator("#status")).toContainText("undone", { ignoreCase: true });
  cockpit.removeAllListeners("console");
  cockpit.removeAllListeners("pageerror");
  cockpit.removeAllListeners("requestfailed");
  await cockpit.close();
});

test("supports skip navigation and resolves a side decision using only the keyboard", async ({
  page,
}) => {
  await loadWorkbench(page);
  await tabUntil(page, () => document.activeElement?.classList.contains("skip-link"), 10);
  await expect(page.getByRole("link", { name: "Skip to needs attention" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 2, name: "Needs Attention" })).toBeFocused();

  await tabUntil(page, () => document.activeElement?.classList.contains("exception-item"));
  const focusedBefore = await page.evaluate(
    () => document.activeElement?.getAttribute("data-exception-id") || "",
  );
  await page.keyboard.press("ArrowDown");
  const focusedAfter = await page.evaluate(
    () => document.activeElement?.getAttribute("data-exception-id") || "",
  );
  expect(focusedAfter).not.toBe(focusedBefore);
  await page.keyboard.press("Enter");
  await expect(
    page.locator(
      "#exceptionQueue [data-exception-id='" + focusedAfter + "']",
    ),
  ).toHaveAttribute("aria-current", "true");
  await shiftTabUntil(
    page,
    () => document.activeElement?.classList.contains("exception-item"),
  );
  await page.keyboard.press("Home");
  await page.keyboard.press("Enter");

  await tabUntil(page, () => document.activeElement?.id === "reviewedCheckbox");
  await page.keyboard.press("Space");
  await expect(page.locator("#reviewedCheckbox")).toBeChecked();
  await page.keyboard.press("Tab");
  await expect(page.locator("#repinButton")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#globalNoticeText")).toContainText("repinned", {
    ignoreCase: true,
  });
  await expect(page.getByRole("heading", { level: 2, name: "Needs Attention" })).toBeFocused();
});

test("persists reviewed album metadata and rediscovers it after reload", async ({ page }) => {
  await loadWorkbench(page);
  await page.locator("#pairingEditor summary").click();
  await page.locator("#metadataGenre").fill("Metalcore");
  await page.getByRole("button", { name: "Save album details" }).click();
  await expect(page.locator("#detailsStatus")).toContainText("saved", { ignoreCase: true });

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench")).toHaveAttribute("aria-busy", "false");
  await page.locator("#pairingEditor summary").click();
  await expect(page.locator("#metadataGenre")).toHaveValue("Metalcore");
});

test("creates, executes, verifies, and reopens one publication through the workbench", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await loadWorkbench(page);

  // The fixture deliberately drifts Side A after pinning it. Resolve that exact
  // review decision through the product before publication can become eligible.
  await page.locator("#reviewedCheckbox").check();
  await page.getByRole("button", { name: "Repin reviewed side" }).click();
  await expect(page.locator("#globalNoticeText")).toContainText("repinned", {
    ignoreCase: true,
  });

  await page.getByLabel(/Archival source objects/).check();
  await page.locator("#publicationReviewed").check();
  await page.getByRole("button", { name: "Create reviewed plan" }).click();
  await expect(page.locator("#publicationPlanStatus")).toContainText(
    "Created and reopened",
    { timeout: 30_000 },
  );

  await page.getByRole("button", { name: "Execute this plan" }).click();
  const destination = "browser-e2e-publication";
  await page.locator("#publicationDestinationName").fill(destination);
  await page.locator("#publicationExecutionConfirmation").fill(`PUBLISH ${destination}`);
  await page.locator("#publicationExecutionConfirmed").check();
  await page.getByRole("button", { name: "Execute and verify" }).click();
  await expect(page.locator("#publicationExecutionStatus")).toContainText(
    `Verified and reopened ${destination}`,
    { timeout: 60_000 },
  );
  await expect(page.locator("#publicationProgress")).toContainText(
    /Published \d+ verified artifacts\./,
  );

  await page.reload({ waitUntil: "domcontentloaded" });
  await expect(page.locator("#workbench")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#publicationReceipts")).toContainText(destination);
  await expect(page.locator("#publicationReceipts .publication-receipt-card.current"))
    .toHaveCount(1);
});

test("keeps the complete workbench inside a narrow mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await loadWorkbench(page);
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
  }));
  expect(dimensions.document).toBeLessThanOrEqual(dimensions.viewport);
  expect(dimensions.body).toBeLessThanOrEqual(dimensions.viewport);
  await expect(page.getByRole("heading", { level: 2, name: "Publication plans" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Needs Attention" })).toBeVisible();
});
