"use strict";
// Build↔Critic orchestrator. Wraps the framework iteration around the existing builder.
// The caller (index.js /api/chat) injects:
//   - builderRunner(roundInput): runs ONE builder turn (spawn claude, stream tool/text events) and resolves
//     with {isError, ...}. The runner is responsible for the per-turn SSE events (tool_start, text, etc.).
//   - emit(eventName, payload): emits SSE events on the active chat stream.
// This module only adds the loop-level events: round_start, builder_done, critic_verdict, loop_done.
const { critique } = require("./scene-critic");

// Format the critic's verdict into a "USER FEEDBACK" string that the builder's existing refine prompt
// already knows how to consume ("modify what exists, don't start from scratch").
function formatFeedback({ issues, suggestions }) {
  const i = (issues || []).filter(Boolean);
  const s = (suggestions || []).filter(Boolean);
  const parts = ["The critic reviewed the current scene and flagged the following:"];
  parts.push(i.length ? "Issues:\n" + i.map((x) => "- " + x).join("\n") : "Issues: (none specified)");
  if (s.length) parts.push("Suggested fixes:\n" + s.map((x) => "- " + x).join("\n"));
  parts.push("Please refine the existing scene to address these. Do NOT start from scratch — modify what already exists.");
  return parts.join("\n\n");
}

async function runSceneLoop({
  prompt,
  intentSummary,
  sessionId,
  maxRounds = 5,
  criticModel,
  criticTimeoutMs = 120000,
  builderRunner,
  emit,
}) {
  if (typeof builderRunner !== "function") throw new Error("builderRunner is required");
  let lastStatus = "NEEDS_IMPROVEMENT";
  let lastIssues = [];
  let lastSuggestions = [];
  let lastScreenshot = null;
  let lastBuilderResult = null;
  let reason = "max_iterations";
  let actualRound = 0;

  for (let round = 1; round <= maxRounds; round++) {
    actualRound = round;
    if (emit) emit("round_start", { round, max: maxRounds });

    // ---- Builder turn ----
    const roundInput = {
      prompt,
      intentSummary,
      feedback: round === 1 ? null : formatFeedback({ issues: lastIssues, suggestions: lastSuggestions }),
      round,
      maxRounds,
    };
    let builderResult;
    try {
      builderResult = await builderRunner(roundInput);
    } catch (e) {
      if (emit) emit("builder_done", { round, isError: true, error: String(e && e.message) });
      reason = "builder_error";
      break;
    }
    lastBuilderResult = builderResult;
    const builderErr = !!(builderResult && builderResult.isError);
    if (emit) emit("builder_done", { round, isError: builderErr });
    if (builderErr) {
      reason = "builder_error";
      break;
    }

    // ---- Critic call ----
    let critic;
    try {
      critic = await critique({
        originalPrompt: intentSummary || prompt,
        model: criticModel,
        timeoutMs: criticTimeoutMs,
      });
    } catch (e) {
      if (emit) emit("critic_verdict", {
        round, status: "FAIL", issues: ["Critic error: " + String(e && e.message)], suggestions: [], error: true,
      });
      reason = "critic_error";
      break;
    }
    lastStatus = critic.status;
    lastIssues = critic.issues || [];
    lastSuggestions = critic.suggestions || [];
    lastScreenshot = critic.screenshot;
    if (emit) emit("critic_verdict", {
      round,
      status: critic.status,
      issues: lastIssues,
      suggestions: lastSuggestions,
      screenshotUrl: critic.screenshot ? `/api/screenshot/file?path=${encodeURIComponent(critic.screenshot)}` : null,
      actorsCount: critic.actorsCount || 0,
    });

    // ---- Stop conditions ----
    if (critic.status === "PASS") { reason = "pass"; break; }
    if (round >= maxRounds) { reason = "max_iterations"; break; }
    // otherwise continue with critic feedback as the next round's input
  }

  const payload = {
    finalStatus: lastStatus,
    rounds: actualRound,
    reason,
    issues: lastIssues,
    suggestions: lastSuggestions,
    latestScreenshot: lastScreenshot,
    builderResult: lastBuilderResult, // for the caller to thread into the final `done` event
  };
  if (emit) emit("loop_done", payload);
  return payload;
}

// ── /api/chat loop handler ──────────────────────────────────────────────────
// Acts as an SSE relay: invokes /api/chat (with useLoop:false to avoid recursion) once per round,
// pipes inner events (tool_start, text, screenshot, ...) through to the client, and inserts the
// orchestrator's loop-level events (round_start, builder_done, critic_verdict, loop_done).
// This sidesteps any refactor of the existing /api/chat builder spawn — it's a clean additive layer.
async function handleSceneLoop(req, res, deps) {
  const { updateIntentSummary } = require("./intent-summarizer");
  const http = require("http");
  const NL = String.fromCharCode(10);
  const { message, sessionId, skills, feedback: userFeedback, runner: outerRunner } = req.body || {};
  if (!message) { res.status(400).json({ error: "message required" }); return; }

  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders();
  const emit = (name, data) => { if (!res.writableEnded) res.write(`event: ${name}\ndata: ${JSON.stringify(data || {})}\n\n`); };
  const ping = setInterval(() => { if (!res.writableEnded) res.write(`: ping\n\n`); }, 5000);

  const STUDIO_SESSION = deps.STUDIO_SESSION;
  const port = parseInt(process.env.PORT || "3002", 10);
  const log = deps.logToFile || (() => {});

  // 1. Update rolling user-intent summary (recency-wins on contradictions).
  emit("intent_start", {});
  const prior = (deps.intentStore && deps.intentStore.get(STUDIO_SESSION)) || "";
  let intentSummary = prior;
  try {
    intentSummary = await updateIntentSummary({
      priorSummary: prior, newPrompt: message,
      model: process.env.SUMMARIZER_MODEL || "claude-sonnet-4-6",
      timeoutMs: parseInt(process.env.SUMMARIZER_TIMEOUT_MS || "60000", 10),
    });
    if (deps.intentStore) deps.intentStore.set(STUDIO_SESSION, intentSummary);
    emit("intent_updated", { summary: intentSummary });
    log("loop", "intent summary updated (" + intentSummary.length + " chars)");
  } catch (e) {
    intentSummary = (prior ? prior + "\n\nNEW: " : "") + message;
    log("loop", "summarizer failed (" + e.message + ") — using fallback intent");
    emit("intent_updated", { summary: intentSummary, fallback: true });
  }

  // 2. Per-round builder runner: POST /api/chat (useLoop:false) and relay SSE events.
  async function builderRunner({ prompt, intentSummary, feedback, round }) {
    return new Promise((resolve) => {
      const combinedPrompt =
        `USER INTENT (cumulative across all prior prompts in this session):\n${intentSummary}\n\n` +
        `CURRENT TURN INSTRUCTION:\n${prompt}`;
      // Round 1: use any user-supplied feedback. Round 2+: critic feedback overrides.
      const combinedFeedback = feedback || userFeedback || undefined;
      const body = JSON.stringify({
        message: combinedPrompt,
        sessionId,
        skills: skills || [],
        feedback: combinedFeedback,
        useLoop: false, // force the inner call to take the existing single-turn path
        ...(outerRunner ? { runner: outerRunner } : {}),
      });
      const opts = {
        host: "127.0.0.1", port, path: "/api/chat", method: "POST",
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
      };
      let buf = "", isError = false, latestScreenshot = null;
      const r = http.request(opts, (inner) => {
        inner.setEncoding("utf8");
        inner.on("data", (chunk) => {
          buf += chunk;
          const parts = buf.split("\n\n");
          buf = parts.pop() || "";
          for (const part of parts) {
            if (!part.trim() || part.startsWith(":")) continue;
            const lines = part.split("\n");
            let name = null, dataStr = "";
            for (const line of lines) {
              if (line.startsWith("event:")) name = line.slice(6).trim();
              else if (line.startsWith("data:")) dataStr += line.slice(5).trim();
            }
            if (!name) continue;
            let data; try { data = JSON.parse(dataStr); } catch { data = dataStr; }
            if (name === "done") { isError = !!(data && data.isError); latestScreenshot = data && data.latestScreenshot; continue; }
            emit(name, data);
          }
        });
        inner.on("end", () => resolve({ isError, latestScreenshot }));
      });
      r.on("error", (e) => resolve({ isError: true, error: e.message }));
      r.write(body); r.end();
    });
  }

  // 3. Run the loop.
  const result = await runSceneLoop({
    prompt: message, intentSummary, sessionId: STUDIO_SESSION,
    maxRounds: parseInt(process.env.SCENE_LOOP_MAX_ROUNDS || "5", 10),
    criticModel: process.env.CRITIC_MODEL || "claude-sonnet-4-6",
    criticTimeoutMs: parseInt(process.env.CRITIC_TIMEOUT_MS || "120000", 10),
    builderRunner, emit,
  });

  // 4. Final SSE done event (closes the stream).
  clearInterval(ping);
  const fatalReasons = new Set(["builder_error", "critic_error"]);
  emit("done", {
    sessionId: STUDIO_SESSION,
    isError: fatalReasons.has(result.reason),
    loop: { reason: result.reason, rounds: result.rounds, finalStatus: result.finalStatus },
    latestScreenshot: result.latestScreenshot ? `/api/screenshot/file?path=${encodeURIComponent(result.latestScreenshot)}` : null,
  });
  res.end();
}

module.exports = { runSceneLoop, formatFeedback, handleSceneLoop };
