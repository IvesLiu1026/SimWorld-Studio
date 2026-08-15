"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { execFile } = require("node:child_process");

const { VistaWorldError } = require("./vista-world-service");

const BUILD_PLAN_SCHEMA = "simworld.vista.playable-home-build-plan/v1";
const DIGEST_RE = /^[a-f0-9]{64}$/;

function compilerError(message, { retryable = false } = {}) {
  return new VistaWorldError(
    "VISTA_WORLD_COMPILER_UNAVAILABLE",
    message,
    { status: 503, retryable },
  );
}

function resolveContained(root, relative, { directory = false } = {}) {
  const resolvedRoot = fs.realpathSync(root);
  const candidate = path.resolve(resolvedRoot, relative);
  if (candidate !== resolvedRoot && !candidate.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw compilerError("Trusted compiler path escaped the repository");
  }
  const metadata = fs.lstatSync(candidate);
  if (metadata.isSymbolicLink() || (directory ? !metadata.isDirectory() : !metadata.isFile())) {
    throw compilerError("Trusted compiler path is not accepted");
  }
  const real = fs.realpathSync(candidate);
  if (real !== resolvedRoot && !real.startsWith(`${resolvedRoot}${path.sep}`)) {
    throw compilerError("Trusted compiler path escaped the repository");
  }
  return real;
}

function validatePlan(plan, house) {
  if (!plan || typeof plan !== "object" || Array.isArray(plan)
      || plan.schema_version !== BUILD_PLAN_SCHEMA
      || !plan.house || typeof plan.house !== "object" || Array.isArray(plan.house)
      || plan.house.house_id !== house.house_id
      || plan.house.revision !== house.revision
      || plan.house.content_digest !== house.content_digest
      || !DIGEST_RE.test(String(plan.content_digest || ""))) {
    throw compilerError("Trusted compiler returned an invalid build plan");
  }
  return plan;
}

function createVistaWorldCompilerAdapter({
  repositoryRoot,
  uvBin = process.env.UV_BIN || "uv",
  timeoutMs = 20_000,
  maxOutputBytes = 8 * 1024 * 1024,
  execFileImpl = execFile,
} = {}) {
  if (!path.isAbsolute(String(repositoryRoot || ""))) {
    throw new TypeError("VISTA world compiler repository root must be absolute");
  }
  if (typeof uvBin !== "string" || !uvBin || uvBin.includes("\0")) {
    throw new TypeError("VISTA world compiler executable is invalid");
  }
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1_000 || timeoutMs > 120_000) {
    throw new TypeError("VISTA world compiler timeout is invalid");
  }
  if (!Number.isSafeInteger(maxOutputBytes)
      || maxOutputBytes < 64 * 1024 || maxOutputBytes > 32 * 1024 * 1024) {
    throw new TypeError("VISTA world compiler output bound is invalid");
  }
  if (typeof execFileImpl !== "function") {
    throw new TypeError("VISTA world compiler process launcher is required");
  }

  const fixedRoot = path.resolve(repositoryRoot);

  return Object.freeze({
    async compile(house, context = {}) {
      if (!house || typeof house !== "object" || Array.isArray(house)
          || typeof house.house_id !== "string" || !house.house_id
          || typeof house.revision !== "string" || !house.revision
          || !DIGEST_RE.test(String(house.content_digest || ""))) {
        throw compilerError("Trusted house revision is invalid");
      }
      if (context.signal !== undefined && !(context.signal instanceof AbortSignal)) {
        throw new TypeError("VISTA world compiler signal must be an AbortSignal");
      }
      if (context.signal && context.signal.aborted) {
        throw compilerError("Trusted compiler request was aborted", { retryable: true });
      }

      let toolsRoot;
      let housePath;
      let eventsRoot;
      try {
        toolsRoot = resolveContained(fixedRoot, "tools", { directory: true });
        housePath = resolveContained(fixedRoot, "world_packs/vista_playable_home_r1/house.json");
        eventsRoot = resolveContained(fixedRoot, "world_packs/vista_playable_home_r1/events", { directory: true });
        resolveContained(fixedRoot, "tools/worlds/playable_home.py");
      } catch (error) {
        if (error instanceof VistaWorldError) throw error;
        throw compilerError("Trusted compiler files are unavailable");
      }

      const args = Object.freeze([
        "run",
        "python",
        "-m",
        "worlds.playable_home",
        "compile",
        "--house",
        housePath,
        "--events-dir",
        eventsRoot,
      ]);
      const env = Object.freeze({
        HOME: process.env.HOME || fixedRoot,
        LANG: process.env.LANG || "C.UTF-8",
        PATH: process.env.PATH || "/usr/bin:/bin",
        PYTHONHASHSEED: "0",
        UV_FROZEN: "1",
        UV_NO_SYNC: "1",
        UV_OFFLINE: "1",
      });

      return new Promise((resolve, reject) => {
        execFileImpl(uvBin, args, {
          cwd: toolsRoot,
          env,
          encoding: "utf8",
          timeout: timeoutMs,
          maxBuffer: maxOutputBytes,
          killSignal: "SIGKILL",
          windowsHide: true,
          ...(context.signal ? { signal: context.signal } : {}),
        }, (error, stdout) => {
          if (error) {
            reject(compilerError("Trusted compiler process failed", {
              retryable: Boolean(error.code === "ABORT_ERR" || error.killed || error.signal),
            }));
            return;
          }
          try {
            const plan = JSON.parse(String(stdout || ""));
            resolve(validatePlan(plan, house));
          } catch (parseError) {
            reject(parseError instanceof VistaWorldError
              ? parseError
              : compilerError("Trusted compiler returned invalid JSON"));
          }
        });
      });
    },
    describe() {
      return Object.freeze({
        schema: BUILD_PLAN_SCHEMA,
        repository_root: fixedRoot,
        module_path: path.join(fixedRoot, "tools", "worlds", "playable_home.py"),
      });
    },
  });
}

module.exports = {
  BUILD_PLAN_SCHEMA,
  createVistaWorldCompilerAdapter,
  validatePlan,
};
