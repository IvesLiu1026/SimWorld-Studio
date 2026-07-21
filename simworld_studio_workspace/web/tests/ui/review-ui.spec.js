import { expect, test } from "@playwright/test";

const TEST_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

function authenticatedPath() {
  const token = process.env.STUDIO_ACCESS_TOKEN;
  return token ? `/?token=${encodeURIComponent(token)}` : "/";
}

test("review remains opt-in and preserves visual evidence plus request identity", async ({ page }) => {
  const chatBodies = [];
  let chatCall = 0;
  let sceneCheckCalls = 0;
  let vlmCalls = 0;

  // Keep this test entirely local to the browser: every unlisted API is a
  // deterministic stub, so no live Studio, UE, or model service is touched.
  await page.route(
    (url) => url.origin === "http://127.0.0.1:4179" && url.pathname.startsWith("/api/"),
    (route) => route.fulfill({ contentType: "application/json", json: {} }),
  );
  await page.route("**/api/screenshot/file?**", (route) => route.fulfill({
    body: TEST_PNG,
    contentType: "image/png",
  }));
  await page.route("**/api/screenshot/latest?**", (route) => route.fulfill({
    body: TEST_PNG,
    contentType: "image/png",
  }));
  await page.route("**/api/scene-check", (route) => {
    sceneCheckCalls += 1;
    return route.fulfill({
      contentType: "application/json",
      json: {
        checked_actors_count: 3,
        collision_count: 0,
        collision_pairs: [],
        floating_actors: [],
        floating_count: 0,
      },
    });
  });
  await page.route("**/api/vlm-score", async (route) => {
    vlmCalls += 1;
    const body = route.request().postDataJSON();
    expect(body.sessionId).toBe("studio-session-test");
    expect(body.imageDataUrl).toMatch(/^data:image\/png;base64,/);
    return route.fulfill({
      contentType: "application/json",
      json: {
        feedback: "Scene composition is consistent.",
        label: "ready",
        model: "fake-review-model",
        provider: "fake",
        score: 8,
      },
    });
  });
  await page.route("**/api/chat", async (route) => {
    chatCall += 1;
    chatBodies.push(route.request().postDataJSON());
    const common = [
      "event: system",
      `data: ${JSON.stringify({ sessionId: "studio-session-test", mcpServers: [] })}`,
      "",
    ];
    const events = chatCall === 1
      ? [
          ...common,
          "event: retrieval",
          `data: ${JSON.stringify({ status: "ready", mode: "hybrid", snapshot_revision: "fixture-snapshot-1" })}`,
          "",
          "event: screenshot",
          `data: ${JSON.stringify({ filepath: "/api/screenshot/file?path=%2Ftmp%2Flatest.png" })}`,
          "",
          "event: done",
          `data: ${JSON.stringify({ sessionId: "studio-session-test", isError: false, latestScreenshot: "/api/screenshot/file?path=%2Ftmp%2Flatest.png" })}`,
          "",
        ]
      : [
          ...common,
          "event: retrieval",
          `data: ${JSON.stringify({
            status: "degraded",
            mode: "hybrid",
            degraded_mode: "basic_geometry",
            snapshot_revision: "fixture-snapshot-1",
            reason: { code: "ASSET_RETRIEVAL_UNAVAILABLE", message: "Fixture dependency outage." },
          })}`,
          "",
          "event: run_start",
          `data: ${JSON.stringify({ runId: "review-run-test", sessionId: "studio-session-test", conversationId: "conversation-test", mode: "visual_loop" })}`,
          "",
          "event: round_start",
          `data: ${JSON.stringify({ round: 1, max: 2, mode: "visual_loop" })}`,
          "",
          "event: builder_done",
          `data: ${JSON.stringify({
            round: 1,
            isError: false,
            cost_usd: 0.12,
            budget: {
              limit_usd: 2,
              spent_usd: 0.12,
              remaining_usd: 1.88,
              exhausted: false,
              stages: { builder: { cost_usd: 0.12 }, critic: { cost_usd: 0 } },
            },
          })}`,
          "",
          "event: multi_shots",
          `data: ${JSON.stringify({ round: 1, count: 2, paths: ["/tmp/front.png", "/tmp/side.png"] })}`,
          "",
          "event: critic_verdict",
          `data: ${JSON.stringify({
            error: "critic provider unavailable",
            model: "fake-review-model",
            provider: "fake",
            round: 1,
            status: "FAIL",
            issues: ["Provider request failed"],
            suggestions: [],
            screenshotUrls: [
              "/api/screenshot/file?path=%2Ftmp%2Ffront.png",
              "/api/screenshot/file?path=%2Ftmp%2Fside.png",
            ],
            cost_usd: 0.03,
            budget: {
              limit_usd: 2,
              spent_usd: 0.15,
              remaining_usd: 1.85,
              exhausted: false,
              stages: { builder: { cost_usd: 0.12 }, critic: { cost_usd: 0.03 } },
            },
          })}`,
          "",
          "event: loop_done",
          `data: ${JSON.stringify({
            error: "review cycle aborted",
            finalStatus: "FAIL",
            reason: "critic_error",
            rounds: 1,
            budget: {
              limit_usd: 2,
              spent_usd: 0.15,
              remaining_usd: 1.85,
              exhausted: false,
              stages: { builder: { cost_usd: 0.12 }, critic: { cost_usd: 0.03 } },
            },
          })}`,
          "",
          "event: done",
          `data: ${JSON.stringify({
            sessionId: "studio-session-test",
            isError: true,
            latestScreenshot: "/api/screenshot/file?path=%2Ftmp%2Ffront.png",
            budget: {
              limit_usd: 2,
              spent_usd: 0.15,
              remaining_usd: 1.85,
              exhausted: false,
              stages: { builder: { cost_usd: 0.12 }, critic: { cost_usd: 0.03 } },
            },
          })}`,
          "",
        ];
    return route.fulfill({
      body: `${events.join("\n")}\n`,
      contentType: "text/event-stream",
      headers: { "Cache-Control": "no-cache" },
    });
  });

  await page.goto(authenticatedPath());

  const command = page.locator(".chat-command-input");
  await command.fill("Create a review fixture");
  await page.locator(".chat-command-submit").click();
  await expect.poll(() => sceneCheckCalls).toBeGreaterThan(0);
  expect(vlmCalls).toBe(0);
  expect(chatBodies[0].loopMode).toBe("vanilla");
  expect(chatBodies[0].agent).toBeTruthy();
  expect(chatBodies[0]).toHaveProperty("model");
  expect(chatBodies[0].conversationId).toMatch(/^chat_/);
  expect(chatBodies[0].requireRealAssets).toBe(true);
  expect(chatBodies[0].assetDegradedMode).toBe("disabled");
  await expect(page.getByText("Asset source: ready", { exact: true })).toBeVisible();
  await expect(page.getByText("mode hybrid · snapshot fixture-snapshot-1", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Visual Review" }).click();
  await page.getByRole("button", { name: "Run Review" }).click();
  await expect.poll(() => vlmCalls).toBe(1);
  await expect(page.locator(".scene-verifier-score")).toContainText("8/10");

  await page.getByText("Review: Off", { exact: true }).click();
  await page.getByText("Review: Text", { exact: true }).click();
  await expect(page.getByText("Review: Visual", { exact: true })).toBeVisible();
  await page.getByText("Assets: Verified only", { exact: true }).click();
  await expect(page.getByText("Assets: Fallback allowed", { exact: true })).toBeVisible();

  await command.fill("Run visual review");
  await page.locator(".chat-command-submit").click();
  await expect.poll(() => chatBodies.length).toBe(2);
  expect(chatBodies[1].loopMode).toBe("visual_loop");
  expect(chatBodies[1].agent).toBe(chatBodies[0].agent);
  expect(chatBodies[1].model).toBe(chatBodies[0].model);
  expect(chatBodies[1].conversationId).toBe(chatBodies[0].conversationId);
  expect(chatBodies[1].requireRealAssets).toBe(false);
  expect(chatBodies[1].assetDegradedMode).toBe("basic_geometry");
  await expect(page.getByText("Asset source: degraded", { exact: true })).toBeVisible();
  await expect(page.getByText("ASSET_RETRIEVAL_UNAVAILABLE — Fixture dependency outage.", { exact: true })).toBeVisible();
  await expect(page.getByRole("img", { name: /Visual review evidence/ })).toHaveCount(2);
  await expect(page.getByText("Review: FAIL · round 1 · fake · fake-review-model", { exact: true })).toBeVisible();
  await expect(page.getByText("Failure: critic provider unavailable", { exact: true })).toBeVisible();
  await expect(page.getByText("Failure: review cycle aborted", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Review budget")).toHaveCount(1);
  await expect(page.getByLabel("Review budget")).toContainText("Review cost $0.15 / $2.00 · $1.85 remaining");
  await expect(page.getByLabel("Review budget")).toContainText("Builder $0.12 · Critic $0.03");
  expect(vlmCalls).toBe(1);

  await expect.poll(() => page.evaluate(() => {
    const conversations = JSON.parse(localStorage.getItem("simworld.chat.conversations.v1") || "[]");
    const activeId = localStorage.getItem("simworld.chat.activeConversation.v1");
    return conversations.find((item) => item.id === activeId)?.state?.loopMode;
  })).toBe("visual_loop");
  await expect.poll(() => page.evaluate(() => {
    const conversations = JSON.parse(localStorage.getItem("simworld.chat.conversations.v1") || "[]");
    const activeId = localStorage.getItem("simworld.chat.activeConversation.v1");
    return conversations.find((item) => item.id === activeId)?.state?.assetPolicy;
  })).toBe("basic_geometry");

  await page.reload();
  await expect(page.getByText("Review: Visual", { exact: true })).toBeVisible();
  await expect(page.getByText("Assets: Fallback allowed", { exact: true })).toBeVisible();
  expect(vlmCalls).toBe(1);
});
