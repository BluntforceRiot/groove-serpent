import { existsSync, writeSync } from "node:fs";
import path from "node:path";
import { startFixture } from "./fixture-process.mjs";

const barrier = process.env.GROOVE_SERPENT_CONTROLLER_BARRIER;
while (barrier && !existsSync(barrier)) {
  await new Promise((resolve) => setTimeout(resolve, 10));
}
const repositoryRoot = path.resolve(".");
process.env.GROOVE_SERPENT_FIXTURE_TEST_DESCENDANT = "1";
const started = await startFixture({
  repositoryRoot,
  script: "tests/browser/serve_side_fixture.py",
  schema: "groove-serpent.side-browser-fixture/1",
  label: "Crash-probe fixture",
});
writeSync(
  1,
  `${JSON.stringify({
    controllerPid: process.pid,
    launcherPid: started.child.pid,
    fixturePid: started.ready.fixture_pid,
    descendantPid: started.ready.descendant_pid,
  })}\n`,
);

// Deliberately bypass stopFixture to emulate an abruptly terminated Codex wrapper.
process.exit(17);
