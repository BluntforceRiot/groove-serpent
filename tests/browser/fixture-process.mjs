import { spawn, spawnSync } from "node:child_process";
import { existsSync, writeFileSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";

const DEFAULT_FIXTURE_LIFETIME_SECONDS = "180";

function isRunning(child) {
  return child && child.exitCode === null && child.signalCode === null;
}

export function resolveFixtureCommand(repositoryRoot) {
  const override = process.env.GROOVE_SERPENT_PYTHON;
  if (override) return { command: override, prefix: [] };

  const executable = process.platform === "win32" ? "python.exe" : "python";
  const virtualEnvironments = [
    process.env.VIRTUAL_ENV,
    path.join(repositoryRoot, ".venv"),
  ].filter(Boolean);
  for (const environment of virtualEnvironments) {
    const candidate = path.join(
      environment,
      process.platform === "win32" ? "Scripts" : "bin",
      executable,
    );
    if (existsSync(candidate)) return { command: candidate, prefix: [] };
  }
  throw new Error(
    "Browser fixtures require GROOVE_SERPENT_PYTHON or the repository .venv Python.",
  );
}

function killOwnedProcessGroup(child) {
  if (!child?.pid) return;
  if (process.platform === "win32") {
    if (!isRunning(child)) return;
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      windowsHide: true,
      stdio: "ignore",
      timeout: 5_000,
    });
    return;
  }
  let groupWasMissing = false;
  try {
    process.kill(-child.pid, "SIGKILL");
  } catch (error) {
    if (error?.code !== "ESRCH") throw error;
    groupWasMissing = true;
  }
  if (groupWasMissing && isRunning(child)) {
    try {
      child.kill("SIGKILL");
    } catch (error) {
      if (error?.code !== "ESRCH") throw error;
    }
  }
}

export async function stopFixture(child, label = "Browser fixture") {
  if (!child) return;
  if (!isRunning(child)) {
    killOwnedProcessGroup(child);
    return;
  }

  const exited = new Promise((resolve) => child.once("exit", resolve));
  const stdinFailed = new Promise((resolve) => {
    child.stdin.once("error", () => resolve(false));
  });
  try {
    child.stdin.end("\n");
  } catch {
    // A failed startup can close stdin before the cleanup path runs.
  }
  const graceful = await Promise.race([
    exited.then(() => true),
    stdinFailed,
    new Promise((resolve) => setTimeout(() => resolve(false), 5_000)),
  ]);
  if (graceful || !isRunning(child)) {
    killOwnedProcessGroup(child);
    return;
  }

  killOwnedProcessGroup(child);
  await Promise.race([
    exited,
    new Promise((resolve) => setTimeout(resolve, 3_000)),
  ]);
  if (isRunning(child)) {
    throw new Error(`${label} process tree did not terminate.`);
  }
}

export async function startFixture({ repositoryRoot, script, schema, label }) {
  const runtime = resolveFixtureCommand(repositoryRoot);
  const child = spawn(runtime.command, [...runtime.prefix, script], {
    cwd: repositoryRoot,
    env: {
      ...process.env,
      GROOVE_SERPENT_FIXTURE_MAX_SECONDS:
        process.env.GROOVE_SERPENT_FIXTURE_MAX_SECONDS
        || DEFAULT_FIXTURE_LIFETIME_SECONDS,
      GROOVE_SERPENT_FIXTURE_OWNER_PID: String(process.pid),
      PYTHONUNBUFFERED: "1",
    },
    stdio: ["pipe", "pipe", "pipe"],
    detached: process.platform !== "win32",
    windowsHide: true,
  });
  const scopeFile = process.env.GROOVE_SERPENT_FIXTURE_SCOPE_FILE;
  if (scopeFile && Number.isSafeInteger(child.pid)) {
    try {
      writeFileSync(
        scopeFile,
        `${JSON.stringify({
          schema: "groove-serpent.fixture-process-scope/1",
          launcherPid: child.pid,
          processGroup: process.platform === "win32" ? null : child.pid,
        })}\n`,
        { encoding: "utf8", flag: "wx", mode: 0o600 },
      );
    } catch (error) {
      await stopFixture(child, label);
      throw error;
    }
  }
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderr = `${stderr}${chunk}`.slice(-65_536);
  });
  const lines = readline.createInterface({ input: child.stdout });
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`${label} did not become ready.\n${stderr}`));
    }, 30_000);
    child.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      reject(
        new Error(
          `${label} exited with code ${code} and signal ${signal}.\n${stderr}`,
        ),
      );
    });
    lines.once("line", (line) => {
      clearTimeout(timer);
      try {
        const payload = JSON.parse(line);
        if (payload.schema !== schema) {
          throw new Error(`Unsupported fixture payload: ${line}`);
        }
        resolve(payload);
      } catch (error) {
        reject(error);
      }
    });
  });

  try {
    const payload = await ready;
    lines.close();
    return { child, ready: payload };
  } catch (error) {
    lines.close();
    await stopFixture(child, label);
    throw error;
  }
}
