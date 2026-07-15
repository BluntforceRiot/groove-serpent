import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { EventEmitter, once } from "node:events";
import test from "node:test";
import { stopFixture } from "./fixture-process.mjs";

test("asynchronous EPIPE is handled before fallback cleanup", async () => {
  const child = new EventEmitter();
  child.exitCode = null;
  child.signalCode = null;
  child.stdin = new EventEmitter();
  child.stdin.end = () => {
    queueMicrotask(() => {
      const error = new Error("write EPIPE");
      error.code = "EPIPE";
      child.stdin.emit("error", error);
    });
    setTimeout(() => {
      child.exitCode = 0;
      child.emit("exit", 0, null);
    }, 20);
  };
  await stopFixture(child, "EPIPE fixture");
  assert.equal(child.exitCode, 0);
});

test("closed fixture stdin cannot bypass forced process cleanup", { timeout: 15_000 }, async () => {
  const child = spawn(
    process.execPath,
    [
      "-e",
      [
        'require("node:fs").closeSync(0);',
        'process.stdout.write("ready\\n");',
        "setInterval(() => {}, 1000);",
      ].join(" "),
    ],
    {
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true,
    },
  );
  try {
    await once(child.stdout, "data");
    await stopFixture(child, "Closed-stdin fixture");
    assert.ok(child.exitCode !== null || child.signalCode !== null);
  } finally {
    if (child.exitCode === null && child.signalCode === null && child.pid) {
      if (process.platform === "win32") {
        spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
          windowsHide: true,
          stdio: "ignore",
          timeout: 5_000,
        });
      } else {
        child.kill("SIGKILL");
      }
    }
  }
});
