"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { createReviewLoopCoordinator } = require("../review-loop-coordinator");

function responseFixture() {
  return {
    headersSent: false,
    writableEnded: false,
    statusCode: 200,
    writes: [],
    jsonBody: null,
    write(chunk) {
      this.headersSent = true;
      this.writes.push(String(chunk));
      return true;
    },
    end(chunk) {
      if (chunk !== undefined) this.writes.push(String(chunk));
      this.headersSent = true;
      this.writableEnded = true;
      return this;
    },
    status(code) { this.statusCode = code; return this; },
    json(body) {
      this.jsonBody = body;
      return this.end(JSON.stringify(body));
    },
  };
}

function terminalHandler(mode) {
  return async (_request, response) => {
    response.write(`event: critic_verdict\ndata: ${JSON.stringify({ status: "PASS", round: 2 })}\n\n`);
    response.write(`event: loop_done\ndata: ${JSON.stringify({
      reason: "pass", rounds: 2, finalStatus: "PASS", mode,
    })}\n\n`);
    response.write(`event: done\ndata: ${JSON.stringify({
      runId: "caller-run",
      isError: false,
      loop: { reason: "pass", rounds: 2, finalStatus: "PASS", mode },
      latestScreenshot: mode === "visual_loop" ? "/api/screenshot/file?path=/private/path" : null,
    })}\n\n`);
    response.end();
  };
}

function request(mode) {
  return {
    body: {
      loopMode: mode,
      runId: "caller-run",
      conversationId: "caller-conversation",
      ownerId: "spoof-owner",
      sessionId: "spoof-session",
    },
    query: {},
  };
}

const ACTIVE_LEASE = Object.freeze({
  ownerId: "server-owner",
  sessionId: "server-session",
  leaseId: "server-lease",
  slotId: 1,
  mcpPort: 55561,
});

function reviewTicket(overrides = {}) {
  return {
    schema: "simworld-review-terminal-outbox-ticket/v1",
    lookupDigest: "d".repeat(64),
    ownerId: ACTIVE_LEASE.ownerId,
    sessionId: ACTIVE_LEASE.sessionId,
    mode: "text_loop",
    journalRunId: `review-journal-${"e".repeat(48)}`,
    startedAt: Date.now(),
    state: "prepared",
    created: true,
    terminal: null,
    ...overrides,
  };
}

function artifactRecorderFixture({ prepare, ensure, ticket } = {}) {
  return {
    enabled: true,
    async prepareReviewTerminal(value) {
      if (prepare) {
        const prepared = await prepare(value);
        if (prepared !== undefined) return prepared;
      }
      return reviewTicket(ticket);
    },
    async ensureReviewTerminal(value) {
      if (ensure) return ensure(value);
      return { created: true };
    },
  };
}

test("Review Off invokes neither review handler nor artifact recorder", async () => {
  let handlerCalls = 0;
  let recorderCalls = 0;
  let nextCalls = 0;
  const handler = async () => { handlerCalls += 1; };
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: handler,
    visualHandler: handler,
    artifactRecorder: artifactRecorderFixture({
      prepare() { recorderCalls += 1; },
      ensure() { recorderCalls += 1; },
    }),
  });
  await coordinator.handleChat(request("vanilla"), responseFixture(), () => { nextCalls += 1; });
  assert.equal(nextCalls, 1);
  assert.equal(handlerCalls, 0);
  assert.equal(recorderCalls, 0);
});

test("loopback review journal authority ignores caller conversation and identity fields", async () => {
  let prepared = null;
  let recorded = null;
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "loopback",
    loopbackSessionId: "server-loopback-authority",
    textHandler: terminalHandler("text_loop"),
    visualHandler: terminalHandler("visual_loop"),
    artifactRecorder: artifactRecorderFixture({
      ticket: { ownerId: "server-loopback-authority", sessionId: "server-loopback-authority" },
      prepare(value) { prepared = value; },
      ensure(value) { recorded = value; },
    }),
  });
  await coordinator.handleChat(request("text_loop"), responseFixture(), () => {});
  assert.deepEqual(prepared.scope.journalAccess, {
    ownerId: "server-loopback-authority",
    sessionId: "server-loopback-authority",
  });
  assert.equal(prepared.run.runId, request("text_loop").body.runId);
  assert.notEqual(recorded.ticket.journalRunId, request("text_loop").body.runId);
});

for (const mode of ["text_loop", "visual_loop"]) {
  test(`${mode} holds terminal SSE until server-owned journal append succeeds`, async () => {
    let release;
    const barrier = new Promise((resolve) => { release = resolve; });
    let recorded = null;
    const coordinator = createReviewLoopCoordinator({
      transportProfile: "trusted_proxy",
      resolveActiveSession: () => ACTIVE_LEASE,
      isActiveSessionBinding: () => true,
      loopbackSessionId: "loopback-session",
      textHandler: terminalHandler("text_loop"),
      visualHandler: terminalHandler("visual_loop"),
      artifactRecorder: artifactRecorderFixture({
        ticket: { mode },
        prepare(value) { recorded = { prepared: value }; },
        async ensure(value) {
          recorded = value;
          await barrier;
        },
      }),
    });
    const response = responseFixture();
    const pending = coordinator.handleChat(request(mode), response, () => {
      throw new Error("loop mode must not fall through");
    });
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(response.writes, []);
    assert.equal(response.writableEnded, false);
    assert.equal(recorded.ticket.ownerId, ACTIVE_LEASE.ownerId);
    assert.equal(recorded.ticket.sessionId, ACTIVE_LEASE.sessionId);
    assert.match(recorded.ticket.journalRunId, /^review-journal-[a-f0-9]{48}$/);
    assert.notEqual(recorded.ticket.journalRunId, request(mode).body.runId);
    assert.equal(recorded.terminal.reason, "pass");
    assert.equal(recorded.terminal.finalVerdict, "PASS");
    release();
    await pending;
    assert.equal(response.writes.length, 3);
    assert.match(response.writes[0], /event: critic_verdict/);
    assert.match(response.writes[1], /event: loop_done/);
    assert.match(response.writes[2], /event: done/);
    assert.equal(response.writableEnded, true);
  });
}

test("review journal failure discards a successful terminal frame and fails closed", async () => {
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: terminalHandler("text_loop"),
    visualHandler: terminalHandler("visual_loop"),
    artifactRecorder: artifactRecorderFixture({
      async ensure() {
        throw Object.assign(new Error("private disk error"), { code: "ARTIFACT_JOURNAL_WRITE_FAILED" });
      },
    }),
  });
  const response = responseFixture();
  await coordinator.handleChat(request("text_loop"), response, () => {});
  assert.equal(response.statusCode, 503);
  assert.deepEqual(response.jsonBody, {
    error: "Durable artifact journal is unavailable.",
    code: "ARTIFACT_JOURNAL_UNAVAILABLE",
    runId: "caller-run",
  });
  assert.equal(JSON.stringify(response).includes("PASS"), false);
  assert.equal(JSON.stringify(response).includes("private disk error"), false);
});

test("split PASS and terminal SSE frames remain held until the journal commits", async () => {
  let release;
  const barrier = new Promise((resolve) => { release = resolve; });
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: async (_request, response) => {
      response.write("event: critic_");
      response.write('verdict\ndata: {"status":"PASS","round":1}\n\n');
      response.write('event: loop_done\ndata: {"reason":"pass",');
      response.write('"rounds":1,"finalStatus":"PASS"}\n\n');
      response.end('event: done\ndata: {"isError":false,"loop":{"reason":"pass","rounds":1,"finalStatus":"PASS"}}\n\n');
    },
    visualHandler: terminalHandler("visual_loop"),
    artifactRecorder: artifactRecorderFixture({
      async ensure() { await barrier; },
    }),
  });
  const response = responseFixture();
  const pending = coordinator.handleChat(request("text_loop"), response, () => {});
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(response.writes, []);
  release();
  await pending;
  assert.match(response.writes.join(""), /critic_verdict/);
  assert.match(response.writes.join(""), /loop_done/);
  assert.match(response.writes.join(""), /event: done/);
});

test("terminal gate acknowledges buffered write callbacks without releasing PASS", async () => {
  let release;
  let ensureReached = false;
  const barrier = new Promise((resolve) => { release = resolve; });
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: async (_request, response) => {
      await new Promise((resolve) => response.write(
        'event: critic_verdict\ndata: {"status":"PASS"}\n\n',
        resolve,
      ));
      await new Promise((resolve) => response.end(
        'event: done\ndata: {"isError":false,"loop":{"reason":"pass","rounds":1,"finalStatus":"PASS"}}\n\n',
        resolve,
      ));
    },
    visualHandler: terminalHandler("visual_loop"),
    artifactRecorder: artifactRecorderFixture({
      async ensure() {
        ensureReached = true;
        await barrier;
      },
    }),
  });
  const response = responseFixture();
  const pending = coordinator.handleChat(request("text_loop"), response, () => {});
  for (let attempt = 0; attempt < 20 && !ensureReached; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(ensureReached, true);
  assert.deepEqual(response.writes, []);
  release();
  await pending;
  assert.match(response.writes.join(""), /critic_verdict/);
  assert.match(response.writes.join(""), /event: done/);
});

test("terminal gate overflow discards every held PASS frame", async () => {
  let recorded = null;
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: async (_request, response) => {
      response.write('event: critic_verdict\ndata: {"status":"PASS"}\n\n');
      response.write(`event: loop_done\ndata: ${JSON.stringify({
        reason: "pass",
        rounds: 1,
        finalStatus: "PASS",
        oversized: "x".repeat(70 * 1024),
      })}\n\n`);
    },
    visualHandler: terminalHandler("visual_loop"),
    artifactRecorder: artifactRecorderFixture({
      ensure(value) { recorded = value; },
    }),
  });
  const response = responseFixture();
  await coordinator.handleChat(request("text_loop"), response, () => {});
  assert.equal(recorded.terminal.outcome, "failed");
  assert.equal(recorded.terminal.reason, "handler_error");
  assert.equal(JSON.stringify(response).includes("PASS"), false);
  assert.equal(response.jsonBody.code, "REVIEW_TERMINAL_GATE_FAILED");
});

test("handler-owned HTTP failures journal as failed before their body is released", async () => {
  let release;
  const barrier = new Promise((resolve) => { release = resolve; });
  let recorded = null;
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: async (_request, response) => {
      response.status(400).json({ error: "message required" });
    },
    visualHandler: terminalHandler("visual_loop"),
    artifactRecorder: artifactRecorderFixture({
      async ensure(value) {
        recorded = value;
        await barrier;
      },
    }),
  });
  const response = responseFixture();
  const pending = coordinator.handleChat(request("text_loop"), response, () => {});
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(recorded.terminal.outcome, "failed");
  assert.equal(recorded.terminal.errorCode, "REVIEW_HANDLER_HTTP_ERROR");
  assert.deepEqual(response.writes, []);
  assert.equal(response.headersSent, false);
  release();
  await pending;
  assert.equal(response.statusCode, 400);
  assert.equal(response.headersSent, true);
});

test("terminal-pending retry replays the same durable terminal without rerunning provider", async () => {
  let handlerCalls = 0;
  let ensureCalls = 0;
  let savedTerminal = null;
  let state = "prepared";
  const recorder = artifactRecorderFixture({
    prepare() {
      return reviewTicket({
        created: state === "prepared" && savedTerminal === null,
        state,
        terminal: savedTerminal,
      });
    },
    async ensure({ terminal }) {
      ensureCalls += 1;
      savedTerminal = terminal;
      state = "terminal_pending";
      if (ensureCalls === 1) {
        throw Object.assign(new Error("append interrupted"), { code: "ARTIFACT_JOURNAL_WRITE_FAILED" });
      }
      state = "published";
      return { created: true };
    },
  });
  const handler = async (...args) => {
    handlerCalls += 1;
    return terminalHandler("text_loop")(...args);
  };
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: handler,
    visualHandler: terminalHandler("visual_loop"),
    artifactRecorder: recorder,
  });

  const first = responseFixture();
  await coordinator.handleChat(request("text_loop"), first, () => {});
  assert.equal(first.statusCode, 503);
  assert.equal(first.jsonBody.runId, "caller-run");
  assert.equal(handlerCalls, 1);
  assert.equal(state, "terminal_pending");

  const retry = responseFixture();
  await coordinator.handleChat(request("text_loop"), retry, () => {});
  assert.equal(handlerCalls, 1, "paid provider must not run again for terminal_pending");
  assert.equal(ensureCalls, 2);
  assert.equal(state, "published");
  assert.equal(retry.statusCode, 200);
  assert.match(retry.writes.join(""), /"recovered":true/);
  assert.match(retry.writes.join(""), /"finalStatus":"PASS"/);
});

test("an unresolved prepared outbox refuses a duplicate provider invocation", async () => {
  let handlerCalls = 0;
  let ensureCalls = 0;
  const handler = async () => { handlerCalls += 1; };
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: handler,
    visualHandler: handler,
    artifactRecorder: artifactRecorderFixture({
      prepare() { return reviewTicket({ created: false, state: "prepared" }); },
      ensure() { ensureCalls += 1; },
    }),
  });
  const response = responseFixture();
  await coordinator.handleChat(request("text_loop"), response, () => {});
  assert.equal(response.statusCode, 409);
  assert.equal(response.jsonBody.code, "REVIEW_TERMINAL_RECOVERY_REQUIRED");
  assert.equal(handlerCalls, 0);
  assert.equal(ensureCalls, 0);
});
