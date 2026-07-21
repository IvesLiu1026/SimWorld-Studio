import { expect, test } from "@playwright/test";

const TEST_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

function evidenceRef(seed) {
  const value = String(seed).slice(0, 1).toLowerCase();
  return {
    schema: "simworld-review-evidence-ref/v1",
    scope_digest: value.repeat(64),
    evidence_id: `sha256:${value.repeat(64)}`,
    handle: `evidence-${value.repeat(48)}`,
  };
}

function authenticatedPath() {
  const token = process.env.STUDIO_ACCESS_TOKEN;
  return token ? `/?token=${encodeURIComponent(token)}` : "/";
}

async function stubUnlistedApis(page) {
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    (route) => {
      const pathname = new URL(route.request().url()).pathname;
      const json = pathname === "/api/session/heartbeat"
        || pathname === "/api/session/acquire"
        ? { dev: true, slotId: 0 }
        : {};
      return route.fulfill({ contentType: "application/json", json });
    },
  );
}

test("review remains opt-in and preserves visual evidence plus request identity", async ({ page }) => {
  const chatBodies = [];
  let chatCall = 0;
  let sceneCheckCalls = 0;

  // Keep this test entirely local to the browser: every unlisted API is a
  // deterministic stub, so no live Studio, UE, or model service is touched.
  await stubUnlistedApis(page);
  await page.route("**/api/screenshot/file?**", (route) => route.fulfill({
    body: TEST_PNG,
    contentType: "image/png",
  }));
  await page.route("**/api/screenshot/latest?**", (route) => route.fulfill({
    body: TEST_PNG,
    contentType: "image/png",
  }));
  await page.route("**/api/review-evidence/**", (route) => route.fulfill({
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
  await page.route("**/api/chat", async (route) => {
    chatCall += 1;
    chatBodies.push(route.request().postDataJSON());
    const requestedRunId = chatBodies.at(-1).runId;
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
          `data: ${JSON.stringify({ runId: requestedRunId, sessionId: "studio-session-test", conversationId: chatBodies.at(-1).conversationId, mode: "visual_loop" })}`,
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
          `data: ${JSON.stringify({ round: 1, count: 2, evidence: [evidenceRef("a"), evidenceRef("b")] })}`,
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
            screenshotRefs: [evidenceRef("a"), evidenceRef("b")],
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
            runId: requestedRunId,
            conversationId: chatBodies.at(-1).conversationId,
            isError: true,
            code: "ARTIFACT_JOURNAL_UNAVAILABLE",
            latestScreenshot: null,
            latestScreenshotRef: evidenceRef("a"),
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
  expect(chatBodies[0].loopMode).toBe("vanilla");
  expect(chatBodies[0].agent).toBeTruthy();
  expect(chatBodies[0]).toHaveProperty("model");
  expect(chatBodies[0].conversationId).toMatch(/^chat_/);
  expect(chatBodies[0].requireRealAssets).toBe(true);
  expect(chatBodies[0].assetDegradedMode).toBe("disabled");
  expect(chatBodies[0]).not.toHaveProperty("runId");
  await expect(page.getByText("Asset source: ready", { exact: true })).toBeVisible();
  await expect(page.getByText("mode hybrid · snapshot fixture-snapshot-1", { exact: true })).toBeVisible();
  await expect(page.getByText("Geometry validation", { exact: true })).toBeVisible();
  await expect(page.getByText(
    "Choose Text or Visual in the Chat Review control, then send a review request.",
    { exact: true },
  )).toBeVisible();
  await expect(page.getByRole("button", { name: "Run Review" })).toHaveCount(0);

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
  expect(chatBodies[1].runId).toMatch(/^review-client-[A-Za-z0-9-]+$/);
  await expect(page.getByText("Asset source: degraded", { exact: true })).toBeVisible();
  await expect(page.getByText("ASSET_RETRIEVAL_UNAVAILABLE — Fixture dependency outage.", { exact: true })).toBeVisible();
  await expect(page.getByRole("img", { name: /Visual review evidence/ })).toHaveCount(2);
  await expect(page.getByText("Review: FAIL · round 1 · fake · fake-review-model", { exact: true })).toBeVisible();
  await expect(page.getByText("Failure: critic provider unavailable", { exact: true })).toBeVisible();
  await expect(page.getByText("Failure: review cycle aborted", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Review budget")).toHaveCount(1);
  await expect(page.getByLabel("Review budget")).toContainText("Review cost $0.15 / $2.00 · $1.85 remaining");
  await expect(page.getByLabel("Review budget")).toContainText("Builder $0.12 · Critic $0.03");

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
  await expect.poll(() => page.evaluate(() => {
    const conversations = JSON.parse(localStorage.getItem("simworld.chat.conversations.v1") || "[]");
    const activeId = localStorage.getItem("simworld.chat.activeConversation.v1");
    const messages = conversations.find((item) => item.id === activeId)?.state?.messages || [];
    return messages.find((message) => message.reviewRetryAvailable)?.reviewRequest?.runId || null;
  })).toBe(chatBodies[1].runId);
  await expect.poll(() => page.evaluate(() => {
    const pending = JSON.parse(localStorage.getItem("simworld.review.pending.v2") || "null");
    return pending ? {
      runId: pending.request?.runId,
      generation: pending.generation,
      ownerId: pending.ownerId,
      state: pending.state,
      receiptId: pending.terminalReceipt?.receiptId,
    } : null;
  })).toEqual({
    runId: chatBodies[1].runId,
    generation: 2,
    ownerId: expect.stringMatching(/^review-owner-[A-Za-z0-9-]{8,180}$/),
    state: "terminal_received",
    receiptId: expect.stringMatching(/^review-terminal-[A-Za-z0-9-]{8,180}$/),
  });

  await page.reload();
  await expect(page.getByText("Review: Visual", { exact: true })).toBeVisible();
  await expect(page.getByText("Assets: Fallback allowed", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Retry Review" }).first().click();
  await expect.poll(() => chatBodies.length).toBe(3);
  expect(chatBodies[2].runId).toBe(chatBodies[1].runId);
  expect(chatBodies[2].loopMode).toBe(chatBodies[1].loopMode);
  expect(chatBodies[2].conversationId).toBe(chatBodies[1].conversationId);
  await expect.poll(() => page.evaluate(() => {
    const pending = JSON.parse(localStorage.getItem("simworld.review.pending.v2") || "null");
    return pending?.generation || null;
  })).toBe(4);
});

test("Review preparation blocks scene and conversation mutations until its locked claim is durable", async ({ page }) => {
  await page.addInitScript(() => {
    const originalSetItem = Storage.prototype.setItem;
    const originalRemoveItem = Storage.prototype.removeItem;
    window.__reviewPersistenceOrder = [];
    Storage.prototype.setItem = function patchedSetItem(key, value) {
      if (key === "simworld.review.pending.v2" || key === "simworld.chat.conversations.v1") {
        let state = null;
        try { state = JSON.parse(value)?.state || null; } catch {}
        window.__reviewPersistenceOrder.push({ key, state });
      }
      return originalSetItem.call(this, key, value);
    };
    Storage.prototype.removeItem = function patchedRemoveItem(key) {
      if (key === "simworld.review.pending.v2") {
        let matched = false;
        try {
          const pending = JSON.parse(this.getItem(key) || "null");
          const conversations = JSON.parse(this.getItem("simworld.chat.conversations.v1") || "[]");
          const conversation = conversations.find((item) => item.id === pending?.conversationId);
          const message = conversation?.state?.messages?.find(
            (item) => item.id === pending?.assistantId,
          );
          matched = Boolean(
            pending?.state === "terminal_received"
            && message?.reviewRequest?.runId === pending.request?.runId
            && message?.reviewTerminalReceiptId === pending.terminalReceipt?.receiptId,
          );
        } catch {}
        window.__terminalPersistedBeforePendingClear = matched;
      }
      return originalRemoveItem.call(this, key);
    };
    let releaseClaim;
    const claimGate = new Promise((resolve) => { releaseClaim = resolve; });
    const lockManager = {
      request: async (name, _options, callback) => {
        if (name === "simworld.review.pending.v2"
            && !localStorage.getItem("simworld.review.pending.v2")) {
          window.__reviewClaimWaiting = true;
          await claimGate;
        }
        return callback();
      },
    };
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: lockManager,
    });
    window.__releaseReviewClaim = () => releaseClaim();
  });

  let chatCalls = 0;
  await stubUnlistedApis(page);
  await page.route("**/api/chat", async (route) => {
    chatCalls += 1;
    const body = route.request().postDataJSON();
    const done = {
      sessionId: "studio-session-test",
      isError: false,
      latestScreenshot: null,
      ...(body.runId ? {
        runId: body.runId,
        conversationId: body.conversationId,
      } : {}),
    };
    const frames = body.runId ? [
      "event: run_start",
      `data: ${JSON.stringify({
        runId: body.runId,
        conversationId: body.conversationId,
        sessionId: "studio-session-test",
      })}`,
      "",
      "event: done",
      `data: ${JSON.stringify(done)}`,
      "",
    ] : [
      "event: done",
      `data: ${JSON.stringify(done)}`,
      "",
    ];
    return route.fulfill({
      body: `${frames.join("\n")}\n`,
      contentType: "text/event-stream",
    });
  });

  await page.goto(authenticatedPath());
  const command = page.locator(".chat-command-input");
  await command.fill("Bootstrap a session");
  await page.locator(".chat-command-submit").click();
  await expect.poll(() => chatCalls).toBe(1);
  await page.getByTitle("Operation details").click();
  await expect(page.getByRole("button", { name: "New Session" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Clear" })).toBeVisible();

  await page.getByText("Review: Off", { exact: true }).click();
  await command.fill("Prepare a locked Review");
  await page.locator(".chat-command-submit").click();
  await expect.poll(() => page.evaluate(() => window.__reviewClaimWaiting === true)).toBe(true);
  expect(chatCalls).toBe(1);
  await expect(command).toBeDisabled();
  await expect(page.getByRole("button", { name: "New Session" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Clear" })).toHaveCount(0);

  const activeConversationBefore = await page.evaluate(() => (
    localStorage.getItem("simworld.chat.activeConversation.v1")
  ));
  await page.getByTitle("Open build sessions").click();
  await expect(page.getByTitle("New build session")).toBeDisabled();
  await expect(page.locator(".chat-session-select").first()).toBeDisabled();
  expect(await page.evaluate(() => (
    localStorage.getItem("simworld.chat.activeConversation.v1")
  ))).toBe(activeConversationBefore);

  await page.evaluate(() => window.__releaseReviewClaim());
  await expect.poll(() => chatCalls).toBe(2);
  await expect.poll(() => page.evaluate(() => (
    localStorage.getItem("simworld.review.pending.v2")
  ))).toBe(null);
  expect(await page.evaluate(() => window.__terminalPersistedBeforePendingClear)).toBe(true);
  const persistenceOrder = await page.evaluate(() => window.__reviewPersistenceOrder);
  const terminalReceiptIndex = persistenceOrder.findIndex((item) => (
    item.key === "simworld.review.pending.v2" && item.state === "terminal_received"
  ));
  const conversationIndex = persistenceOrder.findIndex((item, index) => (
    index > terminalReceiptIndex && item.key === "simworld.chat.conversations.v1"
  ));
  expect(terminalReceiptIndex).toBeGreaterThanOrEqual(0);
  expect(conversationIndex).toBeGreaterThan(terminalReceiptIndex);
});

test("a crash after terminal receipt keeps same-ID recovery across reload", async ({ page }) => {
  await page.addInitScript(() => {
    const originalSetItem = Storage.prototype.setItem;
    Storage.prototype.setItem = function crashAfterReceipt(key, value) {
      const shouldCrash = sessionStorage.getItem("disable-terminal-write-crash") !== "1";
      if (shouldCrash && key === "simworld.chat.conversations.v1") {
        try {
          const pending = JSON.parse(this.getItem("simworld.review.pending.v2") || "null");
          if (pending?.state === "terminal_received") {
            window.__terminalWriteCrashObserved = true;
            throw new Error("simulated terminal conversation write crash");
          }
        } catch (error) {
          if (error?.message === "simulated terminal conversation write crash") throw error;
        }
      }
      return originalSetItem.call(this, key, value);
    };
  });

  const chatBodies = [];
  await stubUnlistedApis(page);
  await page.route("**/api/chat", async (route) => {
    const body = route.request().postDataJSON();
    chatBodies.push(body);
    const frames = [
      "event: run_start",
      `data: ${JSON.stringify({
        runId: body.runId,
        conversationId: body.conversationId,
        sessionId: "studio-session-test",
      })}`,
      "",
      "event: done",
      `data: ${JSON.stringify({
        runId: body.runId,
        conversationId: body.conversationId,
        sessionId: "studio-session-test",
        isError: false,
      })}`,
      "",
    ];
    return route.fulfill({
      body: `${frames.join("\n")}\n`,
      contentType: "text/event-stream",
    });
  });

  await page.goto(authenticatedPath());
  await page.getByText("Review: Off", { exact: true }).click();
  const command = page.locator(".chat-command-input");
  await command.fill("Crash after terminal receipt");
  await page.locator(".chat-command-submit").click();
  await expect.poll(() => chatBodies.length).toBe(1);
  await expect.poll(() => page.evaluate(() => window.__terminalWriteCrashObserved === true)).toBe(true);
  const firstRunId = chatBodies[0].runId;
  await expect.poll(() => page.evaluate(() => {
    const pending = JSON.parse(localStorage.getItem("simworld.review.pending.v2") || "null");
    return {
      runId: pending?.request?.runId,
      state: pending?.state,
      generation: pending?.generation,
    };
  })).toEqual({ runId: firstRunId, state: "terminal_received", generation: 2 });

  await page.evaluate(() => sessionStorage.setItem("disable-terminal-write-crash", "1"));
  await page.reload();
  await expect(page.getByRole("button", { name: "Retry Review" }).first()).toBeVisible();
  await page.getByRole("button", { name: "Retry Review" }).first().click();
  await expect.poll(() => chatBodies.length).toBe(2);
  expect(chatBodies[1].runId).toBe(firstRunId);
  expect(chatBodies[1].conversationId).toBe(chatBodies[0].conversationId);
  await expect.poll(() => page.evaluate(() => (
    localStorage.getItem("simworld.review.pending.v2")
  ))).toBe(null);
});

test("a valid legacy v1 recovery reload migrates atomically and retries the same ID", async ({ page }) => {
  const legacy = {
    schema: "simworld-review-pending/v1",
    conversationId: "chat_legacy_recovery",
    userMessageId: "legacy-user-message",
    assistantId: "legacy-assistant-message",
    createdAt: 1,
    request: {
      runId: "review-client-legacy1234",
      prompt: "Recover the paid legacy Review",
      sessionId: null,
      options: {
        loopMode: "visual_loop",
        runId: "review-client-legacy1234",
        conversationId: "chat_legacy_recovery",
      },
    },
    generation: 6,
    ownerId: "review-owner-legacy1234",
  };
  await page.addInitScript((record) => {
    localStorage.setItem("simworld.review.pending.v1", JSON.stringify(record));
  }, legacy);

  let chatBody = null;
  let migrationSnapshot = null;
  await stubUnlistedApis(page);
  await page.route("**/api/chat", async (route) => {
    chatBody = route.request().postDataJSON();
    migrationSnapshot = await page.evaluate(() => ({
      legacy: localStorage.getItem("simworld.review.pending.v1"),
      current: JSON.parse(localStorage.getItem("simworld.review.pending.v2") || "null"),
    }));
    const frames = [
      "event: run_start",
      `data: ${JSON.stringify({
        runId: chatBody.runId,
        conversationId: chatBody.conversationId,
        sessionId: "studio-session-test",
      })}`,
      "",
      "event: done",
      `data: ${JSON.stringify({
        runId: chatBody.runId,
        conversationId: chatBody.conversationId,
        sessionId: "studio-session-test",
        isError: false,
      })}`,
      "",
    ];
    return route.fulfill({
      body: `${frames.join("\n")}\n`,
      contentType: "text/event-stream",
    });
  });

  await page.goto(authenticatedPath());
  await expect(page.getByRole("button", { name: "Retry Review" }).first()).toBeVisible();
  await page.getByRole("button", { name: "Retry Review" }).first().click();
  await expect.poll(() => chatBody?.runId || null).toBe(legacy.request.runId);
  expect(chatBody.conversationId).toBe(legacy.conversationId);
  expect(chatBody.loopMode).toBe(legacy.request.options.loopMode);
  expect(migrationSnapshot).toEqual({
    legacy: null,
    current: {
      ...legacy,
      schema: "simworld-review-pending/v2",
      generation: legacy.generation + 1,
      ownerId: expect.stringMatching(/^review-owner-[A-Za-z0-9-]{8,180}$/),
      state: "claimed",
    },
  });
  await expect.poll(() => page.evaluate(() => ({
    legacy: localStorage.getItem("simworld.review.pending.v1"),
    current: localStorage.getItem("simworld.review.pending.v2"),
  }))).toEqual({ legacy: null, current: null });
});
