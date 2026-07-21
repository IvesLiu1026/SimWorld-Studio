"use strict";

// VLM-based scene critic. Provider execution is delegated to the tool-free,
// strict-schema review adapter; this module owns evidence capture/validation
// and preserves the loop-facing verdict contract.
const fs = require("fs");
const path = require("path");
const { ReviewProviderError, review: reviewScene } = require("./review-provider");
const { getUeBroker } = require("./unreal-bridge");

const ARENA_ROOT = path.resolve(__dirname, "..", "..");
const SCREENSHOT_DIR = path.join(ARENA_ROOT, "tmp", "screens");
const MAX_SCREENSHOT_BYTES = 25 * 1024 * 1024;
const MAX_ACTOR_CONTEXT_BYTES = 2 * 1024 * 1024;

const CRITIC_SYSTEM_PROMPT = `You are a 3D scene verification expert for SimWorld Studio (Unreal Engine 5).
Analyze only the supplied scene screenshot, actor list, and scene request. Do not invoke tools or
infer that you can inspect the host system.

Evaluate:
1. Completeness: Are all requested objects present?
2. Placement: Are objects in good positions? (X/Y within -9500 to 9500, not overlapping, not outside ground)
3. Scale: Do objects look appropriately sized relative to each other?
4. Realism: Does the scene match the original request?
5. Issues: Any obvious problems (floating objects above ground, buried below ground, misaligned, upside-down)?
6. Navigation/walkability: Large buildings (BP_Building_*) block navigation near PlayerStart and belong as
   background scenery far from center (>2500 UU). Small props are fine anywhere. Trees belong at the edge.

Return one JSON object matching the supplied schema. status must be PASS, NEEDS_IMPROVEMENT, or FAIL.
issues and suggestions must be concise string arrays. raw_notes may contain a concise evidence summary.`;

function typedError(code, message, details = {}) {
  return new ReviewProviderError(code, message, details);
}

function throwIfCaptureAborted(signal) {
  if (signal && signal.aborted) {
    throw typedError("REVIEW_ABORTED", "Review evidence capture was aborted");
  }
}

function awaitBrokerResult(pending, signal) {
  if (!signal) return Promise.resolve(pending);
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", onAbort);
      fn(value);
    };
    const onAbort = () => finish(reject, typedError("REVIEW_ABORTED", "Review evidence capture was aborted"));
    if (signal.aborted) { onAbort(); return; }
    signal.addEventListener("abort", onAbort, { once: true });
    Promise.resolve(pending).then(
      (value) => finish(resolve, value),
      (error) => finish(reject, error),
    );
  });
}

async function brokerCommand(type, params, { ueBroker, signal, timeoutMs, queueDeadlineMs }) {
  throwIfCaptureAborted(signal);
  const broker = ueBroker || getUeBroker();
  if (!broker || typeof broker.send !== "function") {
    throw typedError("REVIEW_EVIDENCE_UNAVAILABLE", "Shared UE broker is unavailable", { retryable: true });
  }
  let result;
  try {
    const brokerOptions = { timeoutMs, queueDeadlineMs };
    if (signal) brokerOptions.signal = signal;
    result = await awaitBrokerResult(
      broker.send(type, params || {}, brokerOptions),
      signal,
    );
  } catch (cause) {
    if (cause instanceof ReviewProviderError && cause.code === "REVIEW_ABORTED") throw cause;
    throw typedError("REVIEW_EVIDENCE_UNAVAILABLE", `UE evidence command '${type}' failed`, {
      retryable: true,
      cause,
    });
  }
  throwIfCaptureAborted(signal);
  return result;
}

async function takeScreenshot(options = {}) {
  const { signal, ueBroker } = options || {};
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  const filepath = path.join(SCREENSHOT_DIR, `critic_${Date.now()}.png`);
  try { fs.rmSync(filepath, { force: true }); } catch (_error) {}
  await brokerCommand("take_screenshot", { filepath }, {
    ueBroker,
    signal,
    timeoutMs: 30000,
    queueDeadlineMs: 45000,
  });
  return fs.existsSync(filepath) ? filepath : null;
}

async function getActors(options = {}) {
  const { signal, ueBroker } = options || {};
  const result = await brokerCommand("get_actors_in_level", {}, {
    ueBroker,
    signal,
    timeoutMs: 15000,
    queueDeadlineMs: 30000,
  });
  if (!result || typeof result !== "object") {
    throw typedError("REVIEW_EVIDENCE_INVALID", "UE actor snapshot was malformed");
  }
  return result;
}

// Legacy parser remains exported for callers that render stored historical
// verdicts. New provider output never passes through this permissive parser.
function parseCriticFeedback(text) {
  const value = String(text || "");
  const statusMatch = value.match(/(?:\*\*Status\*\*|\bStatus\b)\s*:\s*([A-Za-z_]+)/i);
  let status = "NEEDS_IMPROVEMENT";
  if (statusMatch) {
    const candidate = statusMatch[1].toUpperCase();
    if (["PASS", "NEEDS_IMPROVEMENT", "FAIL"].includes(candidate)) status = candidate;
  }
  const labelPattern = /(?:\*\*(Status|Issues|Suggestions)\*\*|\b(Status|Issues|Suggestions)\b)\s*:\s*/gi;
  const positions = [];
  let match;
  while ((match = labelPattern.exec(value)) !== null) {
    positions.push({
      label: (match[1] || match[2] || "").toLowerCase(),
      start: match.index,
      contentStart: match.index + match[0].length,
    });
  }
  const sectionBody = (label) => {
    const index = positions.findIndex((entry) => entry.label === label.toLowerCase());
    if (index < 0) return "";
    const end = index + 1 < positions.length ? positions[index + 1].start : value.length;
    return value.slice(positions[index].contentStart, end);
  };
  const toBullets = (section) => String(section || "")
    .split(/\n/)
    .map((line) => line.replace(/^[\s\-•*\d.)]+/, "").trim())
    .filter((item) => item && !/^\(?none\)?$/i.test(item));
  return {
    status,
    issues: toBullets(sectionBody("Issues")),
    suggestions: toBullets(sectionBody("Suggestions")),
  };
}

function screenshotEvidence(filepath) {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  let resolved;
  let root;
  let stat;
  let data;
  try {
    root = fs.realpathSync(SCREENSHOT_DIR);
    resolved = fs.realpathSync(String(filepath || ""));
    stat = fs.statSync(resolved);
    data = fs.readFileSync(resolved);
  } catch (cause) {
    throw typedError("REVIEW_EVIDENCE_UNAVAILABLE", "Review screenshot is unavailable", { retryable: true, cause });
  }
  if (resolved !== root && !resolved.startsWith(root + path.sep)) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot must be inside the managed evidence directory");
  }
  if (!stat.isFile() || stat.size < 4 || stat.size > MAX_SCREENSHOT_BYTES) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot has an invalid file size");
  }
  const isJpeg = data[0] === 0xff && data[1] === 0xd8 && data[2] === 0xff;
  const isPng = data.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
  if (!isJpeg && !isPng) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Review screenshot is not a supported PNG or JPEG file");
  }
  return { filepath: resolved, data, mediaType: isJpeg ? "image/jpeg" : "image/png" };
}

function actorContext(actors) {
  let serialized;
  try {
    serialized = JSON.stringify(actors || {}, null, 2);
  } catch (cause) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Actor evidence is not serializable", { cause });
  }
  if (Buffer.byteLength(serialized) > MAX_ACTOR_CONTEXT_BYTES) {
    throw typedError("REVIEW_EVIDENCE_INVALID", "Actor evidence exceeds the review size limit");
  }
  return serialized;
}

function countActors(actors) {
  const list = actors && actors.result && actors.result.actors;
  return Array.isArray(list) ? list.length : 0;
}

// Loop-facing contract. `runner` remains a compatibility alias for provider;
// unknown providers and unversioned models fail before a child is spawned.
async function runCritic({
  originalPrompt,
  focus,
  screenshot,
  actors,
  model,
  maxBudgetUsd,
  timeoutMs = 120000,
  provider,
  runner,
  signal,
} = {}) {
  if (signal && signal.aborted) throw typedError("REVIEW_ABORTED", "Review was aborted before evidence loading");
  const evidence = screenshotEvidence(screenshot);
  const actorsJson = actorContext(actors);
  const prompt = [
    "Evaluate the supplied screenshot and actor list for this SimWorld Studio scene.",
    originalPrompt ? `Original scene request: ${JSON.stringify(String(originalPrompt))}` : "",
    focus ? `Review focus: ${String(focus)}` : "",
    "Current actors in the scene:",
    actorsJson,
  ].filter(Boolean).join("\n\n");
  const verdict = await reviewScene({
    provider: provider || runner,
    model,
    maxBudgetUsd,
    timeoutMs,
    signal,
    systemPrompt: CRITIC_SYSTEM_PROMPT,
    prompt,
    images: [{ mediaType: evidence.mediaType, data: evidence.data }],
    evidenceIds: [evidence.filepath],
  });
  return {
    ...verdict,
    screenshot: evidence.filepath,
    actorsCount: countActors(actors),
  };
}

async function critique({ originalPrompt, focus, model, maxBudgetUsd, timeoutMs, provider, runner, signal, ueBroker } = {}) {
  const [screenshot, actors] = await Promise.all([
    takeScreenshot({ signal, ueBroker }),
    getActors({ signal, ueBroker }),
  ]);
  return runCritic({
    originalPrompt,
    focus,
    screenshot,
    actors,
    model,
    maxBudgetUsd,
    timeoutMs,
    provider,
    runner,
    signal,
  });
}

module.exports = {
  critique,
  runCritic,
  takeScreenshot,
  getActors,
  parseCriticFeedback,
  screenshotEvidence,
  CRITIC_SYSTEM_PROMPT,
};
