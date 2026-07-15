import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
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

function monitorPage(page, problems) {
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
    problems.push(`request: ${request.method()} ${request.url()} ${failure}`);
  });
}

async function loadSideReview(page) {
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

test.beforeEach(async ({ page }) => {
  const started = await startFixture({
    repositoryRoot,
    script: "tests/browser/serve_side_fixture.py",
    schema: "groove-serpent.side-browser-fixture/1",
    label: "Side fixture",
  });
  fixtureProcess = started.child;
  fixture = started.ready;
  browserProblems = [];
  monitorPage(page, browserProblems);
});

test.afterEach(async () => {
  await stopFixture(fixtureProcess, "Side fixture");
  expect(browserProblems, "Unexpected browser errors").toEqual([]);
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
