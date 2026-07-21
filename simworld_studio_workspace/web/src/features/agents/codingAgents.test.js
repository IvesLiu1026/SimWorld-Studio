import assert from "node:assert/strict";
import test from "node:test";

import { resolveCodingSelection } from "./codingAgents.js";

const productionRegistry = {
  locked: true,
  default: "claude",
  agents: {
    claude: {
      label: "Claude Code",
      defaultModel: "claude-opus-4-8",
      models: ["claude-opus-4-8"],
    },
  },
};

test("a locked registry replaces stale browser agent and model state", () => {
  assert.deepEqual(
    resolveCodingSelection(productionRegistry, { agent: "codex", model: "gpt-5" }),
    { agent: "claude", model: "claude-opus-4-8", locked: true },
  );
});

test("a locked registry replaces a stale model for the managed agent", () => {
  assert.deepEqual(
    resolveCodingSelection(productionRegistry, { agent: "claude", model: "claude-sonnet-4-6" }),
    { agent: "claude", model: "claude-opus-4-8", locked: true },
  );
});

test("an unlocked registry preserves an explicit development profile", () => {
  const registry = {
    ...productionRegistry,
    locked: false,
  };
  assert.deepEqual(
    resolveCodingSelection(registry, { agent: "claude", model: "local-profile" }),
    { agent: "claude", model: "local-profile", locked: false },
  );
});
