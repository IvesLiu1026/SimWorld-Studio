"use strict";
// Lightweight per-component telemetry sink for A/B runs.
// Each codex spawn site records {component, durationMs, model, reasoning, usage}.
// All records append to AB_TELEMETRY_FILE (one global NDJSON). The harness runs cells
// sequentially, so it attributes records to a cell by timestamp window. No-op if env unset.
const fs = require("fs");

function normUsage(u) {
  if (!u || typeof u !== "object") return null;
  const n = (k) => Number(u[k] || 0);
  const input = n("input_tokens") || n("input") || n("prompt_tokens");
  const output = n("output_tokens") || n("output") || n("completion_tokens");
  const reasoning = n("reasoning_output_tokens") || n("reasoning_tokens");
  const cached = n("cached_input_tokens") || n("cache_read_input_tokens");
  const total = n("total_tokens") || n("total") || (input + output + reasoning);
  return { input, output, reasoning, cached, total, raw: u };
}

function record(rec) {
  const file = process.env.AB_TELEMETRY_FILE;
  if (!file) return;
  try {
    fs.appendFileSync(file, JSON.stringify({ ts: Date.now(), ...rec }) + "\n");
  } catch (_e) {}
}

module.exports = { record, normUsage };
