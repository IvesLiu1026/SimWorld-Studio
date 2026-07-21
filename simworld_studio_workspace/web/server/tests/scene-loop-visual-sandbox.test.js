"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { formatVisualFeedback, runVisualSceneLoop } = require("../scene-loop-visual");

test("visual feedback never exposes managed host paths to the builder", () => {
  const feedback = formatVisualFeedback({
    issues: ["chair alignment"],
    suggestions: ["rotate the chair"],
    screenshots: [{ name: "current", path: "/private/review-evidence/secret.png" }],
  });

  assert.match(feedback, /visual critic inspected 1 server-managed scene capture/i);
  assert.doesNotMatch(feedback, /\/private\/review-evidence|secret\.png|Read tool/i);
});

test("later Visual Review builders receive critic text but no unaudited image input", async () => {
  const builderInputs = [];
  let criticRound = 0;
  const result = await runVisualSceneLoop({
    prompt: "build a room",
    intentSummary: "build a room",
    sessionId: "review-scope",
    maxRounds: 2,
    builderRunner: async (input) => {
      builderInputs.push(input);
      return { isError: false, costUsd: 0 };
    },
    captureRunner: async () => [{
      name: "current",
      path: "/private/review-evidence/secret.png",
    }],
    criticRunner: async ({ screenshots }) => {
      criticRound += 1;
      return {
        status: criticRound === 1 ? "NEEDS_IMPROVEMENT" : "PASS",
        issues: criticRound === 1 ? ["chair alignment"] : [],
        suggestions: criticRound === 1 ? ["rotate the chair"] : [],
        screenshots,
      };
    },
  });

  assert.equal(result.reason, "pass");
  assert.equal(builderInputs.length, 2);
  assert.deepEqual(builderInputs.map((input) => input.visualFeedbackImages), [[], []]);
  assert.match(builderInputs[1].feedback, /chair alignment/);
  assert.doesNotMatch(builderInputs[1].feedback, /\/private\/review-evidence|secret\.png|Read tool/i);
});
