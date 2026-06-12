#!/usr/bin/env node
"use strict";

// Shadow validation: run the current full-list retrieval, then check whether the
// DB prefilter candidates would have contained the selected final assets.
//
// Usage:
//   ASSET_PREFILTER=false \
//   ASSET_DB_DIR=/data/siddhant/asset_db \
//   POSTGRES_URL=postgresql://simworld:simworld@127.0.0.1:55432/asset_db \
//   QDRANT_URL=http://127.0.0.1:6333 \
//   EMBED_SERVICE_URL=http://127.0.0.1:7777 \
//   node tools/shadow_test.js

const path = require("path");

const SERVER = path.resolve(__dirname, "../simworld_studio_workspace/web/server");
const { loadDB, retrieve } = require(path.join(SERVER, "asset-retrieval"));
const { prefilterCategory } = require(path.join(SERVER, "asset-retrieval-db"));

const PROMPTS_FILE = process.env.PROMPTS_FILE || "/data/siddhant/asset_db/prompts_scaleup.json";
const TOP_K = parseInt(process.env.PREFILTER_TOP_K || "150", 10);
const MIN_CONTAINMENT = Number(process.env.SHADOW_MIN_CONTAINMENT || "0.95");

const FALLBACK_PROMPTS = [
  {
    id: "medieval_market",
    text: "A foggy medieval market square outside a gothic cathedral, wooden stalls, candles, wet cobblestone",
  },
  {
    id: "industrial_harbor",
    text: "An industrial harbor loading dock with shipping containers, cranes, warehouse facades and oil drums",
  },
  {
    id: "temple_courtyard",
    text: "A rural East Asian temple courtyard with stone lanterns, carved statues, shrines and tropical plants",
  },
  {
    id: "sci_fi_outpost",
    text: "A sci-fi research station interior with control panels, glowing conduits, metal grating and crates",
  },
  {
    id: "winter_village",
    text: "A winter village market with snow-covered stalls, firewood, bare trees, lanterns and snow piles",
  },
];

function loadPrompts() {
  try {
    const data = require(PROMPTS_FILE);
    if (Array.isArray(data.prompts) && data.prompts.length) return data.prompts;
  } catch {}
  return FALLBACK_PROMPTS;
}

async function run() {
  if (/^(1|true|yes)$/i.test(String(process.env.ASSET_PREFILTER || ""))) {
    console.error("ERROR: shadow_test must run with ASSET_PREFILTER=false so baseline retrieval is full-list.");
    process.exit(2);
  }

  loadDB();
  const prompts = loadPrompts();
  let globalSelected = 0;
  let globalContained = 0;
  let failed = false;

  for (const p of prompts) {
    const label = p.id || p.label || "prompt";
    console.log(`\n=== ${label} ===`);
    console.log(String(p.text || "").slice(0, 180));

    const result = await retrieve(p.text, {
      model: process.env.CLAUDE_MODEL || "",
      timeoutMs: parseInt(process.env.RETRIEVAL_TIMEOUT_MS || "180000", 10),
      log: x => console.log("  [retrieval] " + x),
    });

    const selectedIds = new Set((result.final || []).map(f => f.id).filter(Boolean));
    const routed = result.trace && result.trace.routed || [];
    const plan = result.trace && result.trace.plan || { semantic_query: p.text };

    const candidateIds = new Set();
    for (const r of routed) {
      const rows = await prefilterCategory(r.id, plan, {
        topK: TOP_K,
        log: x => console.log("  [prefilter] " + x),
      });
      for (const a of rows) if (a && a.id) candidateIds.add(a.id);
      console.log(`  [prefilter] ${r.id}: ${rows.length} candidates`);
    }

    let contained = 0;
    const missing = [];
    for (const id of selectedIds) {
      if (candidateIds.has(id)) contained += 1;
      else missing.push(id);
    }
    const total = selectedIds.size;
    const rate = total ? contained / total : 1;
    globalSelected += total;
    globalContained += contained;
    if (rate < MIN_CONTAINMENT) failed = true;
    console.log(`  containment: ${contained}/${total} (${(rate * 100).toFixed(1)}%)`);
    if (missing.length) console.log(`  missing: ${missing.join(", ")}`);
  }

  const globalRate = globalSelected ? globalContained / globalSelected : 1;
  console.log(`\nGLOBAL containment: ${globalContained}/${globalSelected} (${(globalRate * 100).toFixed(1)}%)`);
  if (failed || globalRate < MIN_CONTAINMENT) process.exit(1);
}

run().catch(err => {
  console.error(err && err.stack || err);
  process.exit(1);
});
