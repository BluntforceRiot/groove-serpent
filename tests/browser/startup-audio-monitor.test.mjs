import assert from "node:assert/strict";
import test from "node:test";
import { createStartupAudioMonitor } from "./startup-audio-monitor.mjs";

const sourceUrl = "http://fixture.localhost:1234/audio";
const request = (overrides = {}) => ({
  method: () => "GET", url: () => sourceUrl, resourceType: () => "other", ...overrides,
});
const response = (status = 200, contentType = "audio/flac") => ({
  status: () => status, headers: () => ({ "content-type": contentType }),
});
const healthy = { currentSrc: sourceUrl, readyState: 2, error: null };

function setup(browserName = "webkit") {
  const problems = [];
  const monitor = createStartupAudioMonitor({ browserName, sourceUrl, problems });
  monitor.beginLoad();
  return { monitor, problems };
}

test("WebKit startup cancellation requires a later completed FLAC and native readiness", () => {
  const { monitor, problems } = setup();
  const first = request();
  monitor.requestStarted(first);
  assert.equal(monitor.holdStartupCancellation(first, "Load request cancelled", "cancelled"), true);
  const replacement = request();
  monitor.requestStarted(replacement);
  monitor.requestFinished(replacement, response(206));
  monitor.finishLoad(healthy);
  assert.deepEqual(problems, []);
});

test("an interrupted navigation cannot discard a pending cancellation", () => {
  const { monitor, problems } = setup();
  const first = request();
  monitor.requestStarted(first);
  assert.equal(monitor.holdStartupCancellation(first, "Load request cancelled", "cancelled"), true);
  monitor.beginLoad();
  assert.deepEqual(problems, ["cancelled"]);
});

test("a late cancellation from an earlier navigation is not held by the new load", () => {
  const { monitor } = setup();
  const previous = request();
  monitor.requestStarted(previous);
  monitor.beginLoad();
  assert.equal(monitor.holdStartupCancellation(previous, "Load request cancelled", "cancelled"), false);
});

for (const scenario of ["no replacement", "older response", "bad status", "wrong type", "error", "not ready", "wrong source"]) {
  test(`startup cancellation remains an error: ${scenario}`, () => {
    const { monitor, problems } = setup();
    const older = request();
    monitor.requestStarted(older);
    const first = request();
    monitor.requestStarted(first);
    assert.equal(monitor.holdStartupCancellation(first, "Load request cancelled", "cancelled"), true);
    const replacement = request();
    monitor.requestStarted(replacement);
    if (scenario !== "no replacement") {
      monitor.requestFinished(scenario === "older response" ? older : replacement,
        response(scenario === "bad status" ? 500 : 200, scenario === "wrong type" ? "text/html" : "audio/flac"));
    }
    monitor.finishLoad({
      ...healthy,
      ...(scenario === "error" ? { error: 3 } : {}),
      ...(scenario === "not ready" ? { readyState: 0 } : {}),
      ...(scenario === "wrong source" ? { currentSrc: `${sourceUrl}?changed` } : {}),
    });
    assert.deepEqual(problems, ["cancelled"]);
  });
}

for (const scenario of ["other engine", "post-load", "different origin", "different path", "query", "post", "wrong error", "wrong resource type"]) {
  test(`does not classify unrelated failure: ${scenario}`, () => {
    const { monitor } = setup(scenario === "other engine" ? "firefox" : "webkit");
    const target = request({
      ...(scenario === "different origin" ? { url: () => "http://other.localhost:1234/audio" } : {}),
      ...(scenario === "different path" ? { url: () => `${sourceUrl}/preview` } : {}),
      ...(scenario === "query" ? { url: () => `${sourceUrl}?changed` } : {}),
      ...(scenario === "post" ? { method: () => "POST" } : {}),
      ...(scenario === "wrong resource type" ? { resourceType: () => "fetch" } : {}),
    });
    monitor.requestStarted(target);
    if (scenario === "post-load") monitor.finishLoad(healthy);
    assert.equal(monitor.holdStartupCancellation(target,
      scenario === "wrong error" ? "Connection refused" : "Load request cancelled", "cancelled"), false);
  });
}
