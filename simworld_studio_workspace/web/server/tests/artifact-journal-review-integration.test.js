"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");

const { createReviewLoopCoordinator } = require("../review-loop-coordinator");
const { createArtifactJournalRuntime } = require("../artifact-journal-runtime");
const { createReviewSceneBinding } = require("../review-scene-binding");

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
    response.write(`event: critic_verdict\ndata: ${JSON.stringify({
      status: "PASS",
      round: 2,
      provider: "claude",
      model: "claude-opus-4-8",
      evidence_ids: [`sha256:${"a".repeat(64)}`],
    })}\n\n`);
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
      message: "Review the current scene against the requested intent.",
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

const TEST_BINDING = Object.freeze({
  schema: "simworld-review-scene-binding/v1",
  scope_id: `review-${"f".repeat(64)}`,
  slot_id: ACTIVE_LEASE.slotId,
  lease_id_sha256: "a".repeat(64),
  scene_revision: `snapshot:${"b".repeat(64)}`,
  scene_snapshot_digest: "b".repeat(64),
  scene_build_lineage: null,
});

function bindingForScope(scope) {
  return { ...TEST_BINDING, scope_id: scope.scopeId };
}

function reviewTicket(overrides = {}) {
  return {
    schema: "simworld-review-terminal-outbox-ticket/v3",
    lookupDigest: "d".repeat(64),
    ownerId: ACTIVE_LEASE.ownerId,
    sessionId: ACTIVE_LEASE.sessionId,
    mode: "text_loop",
    inputDigest: "9".repeat(64),
    requestDigest: "8".repeat(64),
    journalRunId: `review-journal-${"e".repeat(48)}`,
    startedAt: Date.now(),
    state: "prepared",
    created: true,
    bindingBefore: TEST_BINDING,
    terminal: null,
    ...overrides,
  };
}

function artifactRecorderFixture({ prepare, ensure, ticket } = {}) {
  return {
    enabled: true,
    async captureReviewBinding({ scope }) { return bindingForScope(scope); },
    async prepareReviewTerminal(value) {
      if (prepare) {
        const prepared = await prepare(value);
        if (prepared !== undefined) {
          return { ...prepared, bindingBefore: prepared.bindingBefore || value.binding };
        }
      }
      return reviewTicket({ bindingBefore: value.binding, ...ticket });
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

test("durable Review rejects a missing client run ID before registry or provider execution", async () => {
  let starts = 0;
  let handlerCalls = 0;
  let recorderCalls = 0;
  const coordinator = createReviewLoopCoordinator({
    registry: {
      start() { starts += 1; throw new Error("must not start"); },
      cancel() { return null; },
      complete() { return false; },
    },
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: async () => { handlerCalls += 1; },
    visualHandler: async () => { handlerCalls += 1; },
    artifactRecorder: artifactRecorderFixture({
      prepare() { recorderCalls += 1; },
      ensure() { recorderCalls += 1; },
    }),
  });
  const missing = request("text_loop");
  delete missing.body.runId;
  const response = responseFixture();
  await coordinator.handleChat(missing, response, () => {});
  assert.equal(response.statusCode, 400);
  assert.equal(response.jsonBody.code, "REVIEW_RUN_ID_REQUIRED");
  assert.equal(starts, 0);
  assert.equal(handlerCalls, 0);
  assert.equal(recorderCalls, 0);
});

test("exact pre-provider input failure explicitly permits a safe new Review ID", async () => {
  let starts = 0;
  let handlerCalls = 0;
  let recorderCalls = 0;
  const coordinator = createReviewLoopCoordinator({
    registry: {
      start() { starts += 1; throw new Error("must not start"); },
      cancel() { return null; },
      complete() { return false; },
    },
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: async () => { handlerCalls += 1; },
    visualHandler: async () => { handlerCalls += 1; },
    artifactRecorder: artifactRecorderFixture({
      prepare() { recorderCalls += 1; },
      ensure() { recorderCalls += 1; },
    }),
  });
  const invalid = request("text_loop");
  invalid.body.message = "";
  const response = responseFixture();
  await coordinator.handleChat(invalid, response, () => {});
  assert.equal(response.statusCode, 400);
  assert.deepEqual(response.jsonBody, {
    error: "message is required",
    code: "REVIEW_INPUT_INVALID",
    sessionId: `review-${"5d948d45c9ca690251c2de3caa6cd9e13e767b82bb18cf9a7df2549fceaa26dc"}`,
    conversationId: "caller-conversation",
    runId: "caller-run",
    providerAttempted: false,
  });
  assert.equal(starts, 0);
  assert.equal(handlerCalls, 0);
  assert.equal(recorderCalls, 0);
});

test("registry conflict never claims that an existing Review provider was not attempted", async () => {
  let handlerCalls = 0;
  let recorderCalls = 0;
  const coordinator = createReviewLoopCoordinator({
    registry: {
      start() {
        throw Object.assign(new Error("existing run"), {
          code: "REVIEW_RUN_CONFLICT",
          statusCode: 409,
        });
      },
      cancel() { return null; },
      complete() { return false; },
    },
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: async () => { handlerCalls += 1; },
    visualHandler: async () => { handlerCalls += 1; },
    artifactRecorder: artifactRecorderFixture({
      prepare() { recorderCalls += 1; },
      ensure() { recorderCalls += 1; },
    }),
  });
  const response = responseFixture();
  await coordinator.handleChat(request("text_loop"), response, () => {});
  assert.equal(response.statusCode, 409);
  assert.equal(response.jsonBody.code, "REVIEW_RUN_CONFLICT");
  assert.equal(response.jsonBody.runId, "caller-run");
  assert.equal(response.jsonBody.conversationId, "caller-conversation");
  assert.equal(Object.hasOwn(response.jsonBody, "providerAttempted"), false);
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
    assert.equal(recorded.terminal.provider, "claude");
    assert.equal(recorded.terminal.model, "claude-opus-4-8");
    assert.deepEqual(recorded.terminal.evidenceIds, [`sha256:${"a".repeat(64)}`]);
    assert.equal(recorded.terminal.bindingAfter.scope_id, recorded.ticket.bindingBefore.scope_id);
    release();
    await pending;
    assert.equal(response.writes.length, 3);
    assert.match(response.writes[0], /event: critic_verdict/);
    assert.match(response.writes[1], /event: loop_done/);
    assert.match(response.writes[2], /event: done/);
    assert.match(response.writes[1], /"conversationId":"caller-conversation"/);
    assert.match(response.writes[2], /"conversationId":"caller-conversation"/);
    assert.match(response.writes[2], /"sessionId":"review-[a-f0-9]{64}"/);
    assert.match(response.writes[2], /"runId":"caller-run"/);
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
    sessionId: `review-${"5d948d45c9ca690251c2de3caa6cd9e13e767b82bb18cf9a7df2549fceaa26dc"}`,
    conversationId: "caller-conversation",
    runId: "caller-run",
  });
  assert.equal(JSON.stringify(response).includes("PASS"), false);
  assert.equal(JSON.stringify(response).includes("private disk error"), false);
});

test("coordinator overwrites handler-supplied Review stream identity with server scope", async () => {
  const handler = async (requestValue, response, dependencies) => {
    response.write(`event: run_start\ndata: ${JSON.stringify({
      runId: "spoof-run",
      sessionId: "spoof-session",
      conversationId: "spoof-conversation",
    })}\n\n`);
    return terminalHandler("text_loop")(requestValue, response, dependencies);
  };
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: handler,
    visualHandler: handler,
    artifactRecorder: artifactRecorderFixture(),
  });
  const response = responseFixture();
  await coordinator.handleChat(request("text_loop"), response, () => {});
  const stream = response.writes.join("");
  assert.match(stream, /event: run_start/);
  assert.match(stream, /"runId":"caller-run"/);
  assert.match(stream, /"conversationId":"caller-conversation"/);
  assert.match(stream, /"sessionId":"review-[a-f0-9]{64}"/);
  assert.doesNotMatch(stream, /spoof-(?:run|session|conversation)/);
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
      response.write(`verdict\ndata: ${JSON.stringify({
        status: "PASS",
        round: 1,
        provider: "claude",
        model: "claude-opus-4-8",
        evidence_ids: [`sha256:${"b".repeat(64)}`],
      })}\n\n`);
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
        `event: critic_verdict\ndata: ${JSON.stringify({
          status: "PASS",
          provider: "claude",
          model: "claude-opus-4-8",
          evidence_ids: [`sha256:${"c".repeat(64)}`],
        })}\n\n`,
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

test("a handler without an explicit terminal journals failure and releases no success", async () => {
  let recorded = null;
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: async (_request, response) => { response.end(); },
    visualHandler: terminalHandler("visual_loop"),
    artifactRecorder: artifactRecorderFixture({
      ensure(value) { recorded = value; },
    }),
  });
  const response = responseFixture();
  await coordinator.handleChat(request("text_loop"), response, () => {});
  assert.equal(recorded.terminal.outcome, "failed");
  assert.equal(recorded.terminal.errorCode, "REVIEW_TERMINAL_INVALID");
  assert.equal(response.statusCode, 503);
  assert.equal(response.jsonBody.code, "REVIEW_TERMINAL_INVALID");
  assert.equal(JSON.stringify(response).includes("PASS"), false);
});

test("PASS followed by an inconsistent done terminal is discarded and journaled as failed", async () => {
  let recorded = null;
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: async (_request, response) => {
      response.write(`event: critic_verdict\ndata: ${JSON.stringify({
        status: "PASS",
        provider: "claude",
        model: "claude-opus-4-8",
        evidence_ids: [`sha256:${"d".repeat(64)}`],
      })}\n\n`);
      response.write('event: loop_done\ndata: {"reason":"pass","rounds":1,"finalStatus":"PASS"}\n\n');
      response.end('event: done\ndata: {"isError":false,"loop":{"reason":"max_iterations","rounds":1,"finalStatus":"NEEDS_IMPROVEMENT"}}\n\n');
    },
    visualHandler: terminalHandler("visual_loop"),
    artifactRecorder: artifactRecorderFixture({
      ensure(value) { recorded = value; },
    }),
  });
  const response = responseFixture();
  await coordinator.handleChat(request("text_loop"), response, () => {});
  assert.equal(recorded.terminal.outcome, "failed");
  assert.equal(recorded.terminal.errorCode, "REVIEW_TERMINAL_INVALID");
  assert.equal(response.statusCode, 503);
  assert.equal(response.jsonBody.code, "REVIEW_TERMINAL_INVALID");
  assert.equal(JSON.stringify(response).includes("PASS"), false);
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

test("real durable runtime binds provider evidence and pre/post scene snapshots without paid replay", async (t) => {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-review-runtime-e2e-"));
  const root = path.join(parent, "journal");
  await fs.mkdir(root, { mode: 0o700 });
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  const runtime = createArtifactJournalRuntime({
    env: { NODE_ENV: "production", VISTA_ARTIFACT_JOURNAL_ROOT: root },
    clock: () => new Date("2026-07-21T00:00:00.000Z"),
  });
  assert.equal((await runtime.readinessProbe()).status, "ready");

  let providerCalls = 0;
  let currentSnapshot = { result: { actors: [{ name: "chair", x: 0 }] } };
  const captureReviewBinding = async ({ scope, phase }) => {
    if (phase === "after") {
      currentSnapshot = { result: { actors: [{ name: "chair", x: 100 }] } };
    }
    return createReviewSceneBinding({ scope, snapshot: currentSnapshot });
  };
  const handler = async (...args) => {
    providerCalls += 1;
    return terminalHandler("visual_loop")(...args);
  };
  const coordinator = createReviewLoopCoordinator({
    transportProfile: "trusted_proxy",
    resolveActiveSession: () => ACTIVE_LEASE,
    isActiveSessionBinding: () => true,
    loopbackSessionId: "loopback-session",
    textHandler: handler,
    visualHandler: handler,
    artifactRecorder: runtime.recorder,
    captureReviewBinding,
  });
  const first = responseFixture();
  await coordinator.handleChat(request("visual_loop"), first, () => {});
  assert.equal(first.statusCode, 200);
  assert.equal(providerCalls, 1);

  const page = await runtime.journal.listRevisions({ ownerId: ACTIVE_LEASE.ownerId, limit: 5 });
  assert.equal(page.revisions.length, 1);
  const revision = await runtime.journal.readRevision({
    kind: "vista-review",
    artifactId: page.revisions[0].artifact.id,
    revision: "terminal",
    ownerId: ACTIVE_LEASE.ownerId,
  });
  assert.equal(revision.content.schema, "simworld-review-terminal/v2");
  assert.equal(revision.content.provider, "claude");
  assert.deepEqual(revision.content.evidence_ids, [`sha256:${"a".repeat(64)}`]);
  assert.notEqual(
    revision.content.scene_binding.before_digest,
    revision.content.scene_binding.after_digest,
  );

  const changedRequest = request("visual_loop");
  changedRequest.body.message = "Review a different safety criterion with the same public run id.";
  const changed = responseFixture();
  await coordinator.handleChat(changedRequest, changed, () => {});
  assert.equal(changed.statusCode, 503);
  assert.equal(providerCalls, 1, "changed input must conflict before provider execution");
  assert.equal(JSON.stringify(changed).includes('"recovered":true'), false);

  const replay = responseFixture();
  await coordinator.handleChat(request("visual_loop"), replay, () => {});
  assert.equal(replay.statusCode, 200);
  assert.equal(providerCalls, 1, "published terminal must replay without invoking the provider");
  assert.match(replay.writes.join(""), /"recovered":true/);
  assert.equal((await runtime.journal.verifyIntegrity()).revision_count, 1);
});
