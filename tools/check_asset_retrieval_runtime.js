#!/usr/bin/env node
"use strict";

const path = require("path");

const SERVER_DIR = path.resolve(__dirname, "../simworld_studio_workspace/web/server");
const { loadDB, buildPromptBlock, resolveAssetMode } = require(path.join(SERVER_DIR, "asset-retrieval"));
const { prefilterCategory } = require(path.join(SERVER_DIR, "asset-retrieval-db"));

const DEFAULT_SCENE = "A dense Hong Kong night alley with neon signs, shopfront clutter, benches, trash bins, carts, and apartment buildings.";

function arg(name, fallback) {
  const idx = process.argv.indexOf(name);
  if (idx >= 0 && idx + 1 < process.argv.length) return process.argv[idx + 1];
  return fallback;
}

function hasFlag(name) {
  return process.argv.includes(name);
}

function log(msg) {
  process.stdout.write(`${msg}\n`);
}

async function main() {
  const scene = arg("--scene", DEFAULT_SCENE);
  const mode = resolveAssetMode(arg("--mode", "db"));
  const topK = parseInt(arg("--top-k", process.env.PREFILTER_TOP_K || "25"), 10);
  const skipLlm = hasFlag("--skip-llm");
  const category = arg("--category", "signage");
  const model = arg("--model", process.env.CLAUDE_MODEL || "");

  log("Runtime asset retrieval smoke");
  log(`  ASSET_DB_DIR=${process.env.ASSET_DB_DIR || ""}`);
  log(`  POSTGRES_URL=${process.env.POSTGRES_URL || ""}`);
  log(`  QDRANT_URL=${process.env.QDRANT_URL || ""}`);
  log(`  QDRANT_COLLECTION=${process.env.QDRANT_COLLECTION || ""}`);
  log(`  EMBED_SERVICE_URL=${process.env.EMBED_SERVICE_URL || ""}`);
  log(`  CLAUDE_BIN=${process.env.CLAUDE_BIN || "claude"}`);
  log(`  model=${model || "(claude default)"}`);
  log(`  mode=${mode} topK=${topK}`);

  const db = loadDB();
  log(`  loaded categories=${db.categories.length} assets=${db.assets.size}`);
  if (db.assets.size < 10000) throw new Error(`expected full UE5.8/Qwen DB, got only ${db.assets.size} assets`);

  const plan = {
    semantic_query: scene,
    primary_settings: ["modern_urban"],
    hard_exclude_settings: ["medieval", "fantasy_gothic", "sci_fi"],
    must_terms: ["neon", "sign", "shop", "street", "urban"],
    avoid_terms: ["snow", "castle"],
  };

  const pref = await prefilterCategory(category, plan, {
    topK,
    log: msg => log(`  [prefilter] ${msg}`),
  });
  log(`  prefilter ${category}: ${pref.length} candidates`);
  if (!pref.length) throw new Error(`prefilter returned no candidates for ${category}`);
  for (const item of pref.slice(0, 8)) {
    log(`    - ${item.id} | ${item.name} | ${item.setting} | ${item.path}`);
  }

  if (!skipLlm) {
    const block = await buildPromptBlock(scene, mode, {
      topK,
      model,
      timeoutMs: parseInt(arg("--llm-timeout-ms", "300000"), 10),
      log: msg => log(`  [retrieval] ${msg}`),
    });
    const paths = [...block.matchAll(/(?:spawn_blueprint_actor|spawn_actor) "([^"]+)"/g)].map(m => m[1]);
    log(`  prompt block chars=${block.length} paths=${paths.length}`);
    if (!block.includes("RETRIEVED ASSETS") && mode !== "baseline_full") {
      throw new Error("prompt block did not include retrieved asset header");
    }
    if (!paths.length) throw new Error("prompt block contained no spawn paths");
    for (const p of paths.slice(0, 12)) log(`    path: ${p}`);
  }

  log("OK");
}

main().catch(err => {
  console.error(`ERROR: ${err.message}`);
  process.exit(1);
});
