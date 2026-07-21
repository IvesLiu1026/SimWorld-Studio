"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");

const {
  ArtifactJournalRuntimeError,
  createArtifactJournalRuntime,
  resolveArtifactJournalConfig,
} = require("../artifact-journal-runtime");
const {
  ArtifactRevisionJournalError,
  createArtifactRevisionJournal,
} = require("../artifact-revision-journal");

const NOW = "2026-07-21T00:00:00.000Z";
const OWNER = "owner-server";
const SESSION = "session-server";
const REVIEW_REQUEST_DIGEST = "f".repeat(64);
const REVIEW_INPUT_DIGEST = "0".repeat(64);
const REVIEW_ABANDON_TOKEN = "operator-abandonment-token-material-0123456789abcdef";
const REVIEW_ABANDON_TOKEN_SHA256 = crypto
  .createHash("sha256")
  .update(REVIEW_ABANDON_TOKEN, "utf8")
  .digest("hex");

async function fixture(t, env = {}, runtimeOptions = {}) {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-artifact-runtime-"));
  const root = path.join(parent, "journal");
  await fs.mkdir(root, { mode: 0o700 });
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  const runtime = createArtifactJournalRuntime({
    ...runtimeOptions,
    env: {
      NODE_ENV: "production",
      VISTA_ARTIFACT_JOURNAL_ROOT: root,
      VISTA_ARTIFACT_JOURNAL_RETENTION_DAYS: "365",
      ...env,
    },
    clock: () => new Date(NOW),
  });
  assert.equal((await runtime.readinessProbe()).status, "ready");
  return { parent, root, runtime };
}

function imported() {
  return {
    schema: "vista-import-artifact/v1",
    artifact_revision: `sha256:${"a".repeat(64)}`,
    artifact_id: `vim_${"b".repeat(64)}`,
    run_id: `vim_${"b".repeat(64)}`,
    scene_id: `vis_${"c".repeat(64)}`,
    status: "committed",
    profile: "reconstruction",
    idempotency: {
      source_checksum: "d".repeat(64),
      importer_version: "1.0.0",
      profile: "reconstruction",
    },
    access: { owner_id: OWNER, session_id: SESSION },
    created_at: NOW,
    updated_at: NOW,
    scene_spec: {
      schema: "vista-simworld-scene/v1",
      entities: [{ id: "chair" }],
      timeline: [{ at_sec: 0 }],
    },
  };
}

function buildRecord(artifact) {
  return {
    schema: "vista-scene-build-record/v1",
    plan_id: `vsp-${"1".repeat(24)}`,
    import_artifact_id: artifact.artifact_id,
    profile_id: "mmg040_verified_v1",
    status: "succeeded",
    access: {
      owner_id: OWNER,
      last_session_id: SESSION,
      slot_id: 0,
      lease_id_sha256: "2".repeat(64),
    },
    operation: { operation_id: `vsj-${"3".repeat(24)}`, state: "terminal" },
    created_at: NOW,
    updated_at: NOW,
    result: {
      schema: "vista-scene-build-result/v1",
      plan_id: `vsp-${"1".repeat(24)}`,
      scene_id: artifact.scene_id,
      status: "succeeded",
      mutation_count: 2,
      actor_manifest: [{ actor_id: "actor-1" }, { actor_id: "actor-2" }],
      evidence: [{ evidence_id: "evidence-1" }],
      rollback: { state: "not_required", failures: [] },
    },
  };
}

function timelineRecord(artifact, build, buildLineage) {
  return {
    schema: "vista-animation-timeline-run-record/v1",
    run_id: `vtr-${"4".repeat(24)}`,
    operation_id: `vtr-${"4".repeat(24)}`,
    status: "completed",
    identity: {
      import_artifact_id: artifact.artifact_id,
      profile_id: build.profile_id,
      plan_id: build.plan_id,
      scene_id: artifact.scene_id,
      preflight_id: `vap-${"5".repeat(24)}`,
      timeline_id: `vtl-${"6".repeat(24)}`,
      program_id: `vag-${"7".repeat(24)}`,
      scene_build_operation_id: build.operation.operation_id,
      scene_build_content_digest: buildLineage.content_digest,
    },
    access: { owner_id: OWNER, last_session_id: SESSION },
    runtime: {
      profile_revision: "animation-runtime-r1",
      content_digest: "8".repeat(64),
    },
    operation: { state: "terminal", replay_of: null },
    created_at: NOW,
    updated_at: NOW,
    run: {
      events: [{ state: "completed" }, { state: "failed" }],
      cleanup: { confirmed_stopped: true, ended_pie: true },
    },
    evidence: { schema: "bounded-evidence", digest: "9".repeat(64) },
    error: null,
  };
}

function reviewBinding(scopeId, snapshotDigest = "e".repeat(64), lineage = null) {
  return {
    schema: "simworld-review-scene-binding/v1",
    scope_id: scopeId,
    slot_id: 1,
    lease_id_sha256: "f".repeat(64),
    scene_revision: lineage ? lineage.scene_id : `snapshot:${snapshotDigest}`,
    scene_snapshot_digest: snapshotDigest,
    scene_build_lineage: lineage,
  };
}

function passingReviewTerminal(bindingAfter) {
  return {
    outcome: "completed",
    reason: "pass",
    finalVerdict: "PASS",
    rounds: 2,
    errorCode: null,
    provider: "claude",
    model: "claude-opus-4-8",
    evidenceIds: [`sha256:${"1".repeat(64)}`],
    bindingAfter,
  };
}

test("production config is explicit while development may remain disabled", async (t) => {
  assert.deepEqual(resolveArtifactJournalConfig({ NODE_ENV: "development" }), {
    schema: "simworld-artifact-journal-runtime/v1",
    production: false,
    required: false,
    enabled: false,
    root: null,
    retentionDays: 365,
    configError: null,
  });
  const missing = createArtifactJournalRuntime({ env: { NODE_ENV: "production" } });
  const report = await missing.readinessProbe();
  assert.equal(report.status, "not_ready");
  assert.equal(report.causes[0].code, "ARTIFACT_JOURNAL_ROOT_INVALID");
  await assert.rejects(
    missing.recorder.ensureImportCommitted({ artifact: imported() }),
    (error) => error instanceof ArtifactJournalRuntimeError,
  );

  const disabled = createArtifactJournalRuntime({
    env: { NODE_ENV: "production", VISTA_ARTIFACT_JOURNAL_ENABLED: "0" },
  });
  assert.equal(disabled.config.enabled, true);
  assert.equal(disabled.config.configError, "ARTIFACT_JOURNAL_REQUIRED");
  await assert.rejects(
    disabled.recorder.ensureImportCommitted({ artifact: imported() }),
    (error) => error instanceof ArtifactJournalRuntimeError
      && error.code === "ARTIFACT_JOURNAL_REQUIRED",
  );

  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-artifact-missing-"));
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  const absent = createArtifactJournalRuntime({
    env: { NODE_ENV: "production", VISTA_ARTIFACT_JOURNAL_ROOT: path.join(parent, "absent") },
  });
  const absentReport = await absent.readinessProbe();
  assert.equal(absentReport.status, "not_ready");
  assert.equal(absentReport.causes[0].code, "ARTIFACT_JOURNAL_ROOT_MISSING");
});

test("all four terminal kinds append bounded owner-scoped lineage exactly once", async (t) => {
  const { runtime } = await fixture(t);
  const artifact = imported();
  const build = buildRecord(artifact);
  const buildLineage = runtime.recorder.sceneBuildLineage({ record: build });
  const timeline = timelineRecord(artifact, build, buildLineage);

  const importFirst = await runtime.recorder.ensureImportCommitted({ artifact });
  const importRetry = await runtime.recorder.ensureImportCommitted({ artifact });
  assert.equal(importFirst.created, true);
  assert.equal(importRetry.idempotent, true);
  await runtime.recorder.ensureSceneBuildTerminal({ record: build, importArtifact: artifact });
  await runtime.recorder.ensureTimelineTerminal({ record: timeline });
  const reviewScope = {
    scopeId: `review-${"a".repeat(64)}`,
    activeLease: { ownerId: OWNER, sessionId: SESSION },
  };
  const reviewLineage = { ...buildLineage, scene_id: build.result.scene_id };
  const reviewSceneBinding = reviewBinding(reviewScope.scopeId, "e".repeat(64), reviewLineage);
  const reviewSceneAfter = reviewBinding(reviewScope.scopeId, "f".repeat(64), reviewLineage);
  const reviewTicket = await runtime.recorder.prepareReviewTerminal({
    scope: reviewScope,
    run: { runId: "provider-caller-run", startedAt: Date.parse(NOW) },
    mode: "visual_loop",
    binding: reviewSceneBinding,
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: REVIEW_INPUT_DIGEST,
  });
  assert.equal(reviewTicket.created, true);
  assert.match(reviewTicket.journalRunId, /^review-journal-[a-f0-9]{48}$/);
  assert.notEqual(reviewTicket.journalRunId, "provider-caller-run");
  await runtime.recorder.ensureReviewTerminal({
    ticket: reviewTicket,
    terminal: {
      ...passingReviewTerminal(reviewSceneAfter),
      prompt: "must-not-be-recorded",
      screenshotPath: "/home/yhliu/private.png",
      providerProse: "must-not-be-recorded",
    },
  });

  const page = await runtime.journal.listRevisions({ ownerId: OWNER, limit: 20 });
  assert.equal(page.revisions.length, 4);
  assert.deepEqual(page.revisions.map((entry) => entry.artifact.kind), [
    "vista-import",
    "vista-scene-build",
    "vista-animation-timeline",
    "vista-review",
  ]);
  assert.equal((await runtime.journal.listRevisions({ ownerId: "other-owner" })).revisions.length, 0);
  for (const revision of page.revisions) {
    const read = await runtime.journal.readRevision({
      kind: revision.artifact.kind,
      artifactId: revision.artifact.id,
      revision: revision.artifact.revision,
      ownerId: OWNER,
    });
    const bytes = JSON.stringify(read.content);
    assert.doesNotMatch(bytes, /must-not-be-recorded|\/home\/|prompt|screenshot|providerProse/);
  }
  const timelineRevision = page.revisions.find((entry) => entry.artifact.kind === "vista-animation-timeline");
  assert.equal(timelineRevision.source_lineage[0].revision, build.operation.operation_id);
  assert.equal(timelineRevision.source_lineage[0].content_digest, buildLineage.content_digest);
  const reviewRevision = page.revisions.find((entry) => entry.artifact.kind === "vista-review");
  assert.equal(reviewRevision.source_lineage[0].artifact_id, buildLineage.artifact_id);
  assert.equal(reviewRevision.source_lineage[0].revision, buildLineage.revision);
  const storedReview = await runtime.journal.readRevision({
    kind: reviewRevision.artifact.kind,
    artifactId: reviewRevision.artifact.id,
    revision: reviewRevision.artifact.revision,
    ownerId: OWNER,
  });
  assert.equal(storedReview.content.schema, "simworld-review-terminal/v2");
  assert.equal(storedReview.content.request_digest, REVIEW_REQUEST_DIGEST);
  assert.equal(storedReview.content.input_digest, REVIEW_INPUT_DIGEST);
  assert.equal(storedReview.content.provider, "claude");
  assert.deepEqual(storedReview.content.evidence_ids, [`sha256:${"1".repeat(64)}`]);
  assert.equal(storedReview.content.scene_binding.before_revision, build.result.scene_id);
  assert.equal(storedReview.content.scene_binding.after_revision, build.result.scene_id);
  assert.notEqual(
    storedReview.content.scene_binding.before_digest,
    storedReview.content.scene_binding.after_digest,
  );
  assert.equal((await runtime.readinessProbe()).status, "ready");
});

test("stable domain revisions conflict when persisted content drifts without changing counts", async (t) => {
  const { runtime } = await fixture(t);
  const artifact = imported();
  const build = buildRecord(artifact);
  const buildLineage = runtime.recorder.sceneBuildLineage({ record: build });
  const timeline = timelineRecord(artifact, build, buildLineage);

  await runtime.recorder.ensureImportCommitted({ artifact });
  await runtime.recorder.ensureSceneBuildTerminal({ record: build, importArtifact: artifact });
  await runtime.recorder.ensureTimelineTerminal({ record: timeline });

  const changedImport = structuredClone(artifact);
  changedImport.scene_spec.entities[0].id = "same-count-different-entity";
  await assert.rejects(
    runtime.recorder.ensureImportCommitted({ artifact: changedImport }),
    (error) => error.code === "ARTIFACT_IDEMPOTENCY_CONFLICT",
  );
  assert.equal((await runtime.readinessProbe({ force: true })).status, "ready");

  const changedBuild = structuredClone(build);
  changedBuild.result.actor_manifest[0].actor_id = "same-count-different-actor";
  await assert.rejects(
    runtime.recorder.ensureSceneBuildTerminal({ record: changedBuild, importArtifact: artifact }),
    (error) => error.code === "ARTIFACT_IDEMPOTENCY_CONFLICT",
  );
  assert.equal((await runtime.readinessProbe({ force: true })).status, "ready");

  const changedTimeline = structuredClone(timeline);
  changedTimeline.run.events[0].state = "failed";
  changedTimeline.run.events[1].state = "completed";
  await assert.rejects(
    runtime.recorder.ensureTimelineTerminal({ record: changedTimeline }),
    (error) => error.code === "ARTIFACT_IDEMPOTENCY_CONFLICT",
  );
});

test("Review outbox preserves terminal identity across append failure and readiness replay", async (t) => {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-review-outbox-"));
  const root = path.join(parent, "journal");
  await fs.mkdir(root, { mode: 0o700 });
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  const durableJournal = createArtifactRevisionJournal({ root, now: () => new Date(NOW) });
  let failAppend = true;
  const runtime = createArtifactJournalRuntime({
    env: { NODE_ENV: "production", VISTA_ARTIFACT_JOURNAL_ROOT: root },
    journal: {
      limits: durableJournal.limits,
      async append(value) {
        if (failAppend) throw Object.assign(new Error("simulated disk interruption"), { code: "EIO" });
        return durableJournal.append(value);
      },
      async verifyIntegrity() { return durableJournal.verifyIntegrity(); },
    },
    clock: () => new Date(NOW),
  });
  assert.equal((await runtime.readinessProbe()).status, "ready");
  const scope = {
    scopeId: `review-${"b".repeat(64)}`,
    journalAccess: { ownerId: OWNER, sessionId: SESSION },
  };
  const run = { runId: "paid-provider-run", startedAt: Date.parse(NOW) };
  const binding = reviewBinding(scope.scopeId);
  const ticket = await runtime.recorder.prepareReviewTerminal({
    scope, run, mode: "text_loop", binding,
    requestDigest: REVIEW_REQUEST_DIGEST, inputDigest: REVIEW_INPUT_DIGEST,
  });
  const terminal = { ...passingReviewTerminal(binding), rounds: 1 };
  await assert.rejects(
    runtime.recorder.ensureReviewTerminal({ ticket, terminal }),
    (error) => error.code === "ARTIFACT_JOURNAL_WRITE_FAILED",
  );

  const outboxPath = path.join(root, "review-outbox", `${ticket.lookupDigest}.json`);
  const pending = JSON.parse(await fs.readFile(outboxPath, "utf8"));
  assert.equal(pending.state, "terminal_pending");
  assert.equal(pending.journal_run_id, ticket.journalRunId);
  assert.equal((await fs.stat(outboxPath)).mode & 0o777, 0o600);
  assert.doesNotMatch(JSON.stringify(pending), /paid-provider-run|prompt|providerProse|screenshot/);

  failAppend = false;
  const recovered = await runtime.readinessProbe({ force: true });
  assert.equal(recovered.status, "ready");
  assert.deepEqual(recovered.revision.review_outbox, {
    prepared: 0,
    terminal_pending: 0,
    published: 1,
    abandoned: 0,
  });
  const published = JSON.parse(await fs.readFile(outboxPath, "utf8"));
  assert.equal(published.state, "published");
  assert.equal(published.journal_run_id, ticket.journalRunId);

  const retryTicket = await runtime.recorder.prepareReviewTerminal({
    scope, run, mode: "text_loop", binding,
    requestDigest: REVIEW_REQUEST_DIGEST, inputDigest: REVIEW_INPUT_DIGEST,
  });
  assert.equal(retryTicket.created, false);
  assert.equal(retryTicket.state, "published");
  assert.equal(retryTicket.journalRunId, ticket.journalRunId);
  const idempotent = await runtime.recorder.ensureReviewTerminal({ ticket: retryTicket, terminal });
  assert.equal(idempotent.idempotent, true);
  assert.equal((await durableJournal.verifyIntegrity()).revision_count, 1);

  const otherOwner = await runtime.recorder.prepareReviewTerminal({
    scope: {
      scopeId: `review-${"c".repeat(64)}`,
      journalAccess: { ownerId: "different-owner", sessionId: "different-session" },
    },
    run,
    mode: "text_loop",
    binding: reviewBinding(`review-${"c".repeat(64)}`),
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: REVIEW_INPUT_DIGEST,
  });
  assert.notEqual(otherOwner.lookupDigest, ticket.lookupDigest);
  assert.notEqual(otherOwner.journalRunId, ticket.journalRunId);
});

test("published Review replay requires the exact persisted post-scene binding", async (t) => {
  const { runtime } = await fixture(t);
  const scope = {
    scopeId: `review-${"9".repeat(64)}`,
    journalAccess: { ownerId: OWNER, sessionId: SESSION },
  };
  const run = { runId: "scene-bound-paid-run", startedAt: Date.parse(NOW) };
  const before = reviewBinding(scope.scopeId, "2".repeat(64));
  const after = reviewBinding(scope.scopeId, "3".repeat(64));
  const ticket = await runtime.recorder.prepareReviewTerminal({
    scope, run, mode: "visual_loop", binding: before,
    requestDigest: REVIEW_REQUEST_DIGEST, inputDigest: REVIEW_INPUT_DIGEST,
  });
  await runtime.recorder.ensureReviewTerminal({
    ticket,
    terminal: passingReviewTerminal(after),
  });
  await assert.rejects(
    runtime.recorder.prepareReviewTerminal({
      scope, run, mode: "visual_loop", binding: before,
      requestDigest: REVIEW_REQUEST_DIGEST, inputDigest: REVIEW_INPUT_DIGEST,
    }),
    (error) => error.code === "ARTIFACT_IDEMPOTENCY_CONFLICT",
  );
  const replay = await runtime.recorder.prepareReviewTerminal({
    scope, run, mode: "visual_loop", binding: after,
    requestDigest: REVIEW_REQUEST_DIGEST, inputDigest: REVIEW_INPUT_DIGEST,
  });
  assert.equal(replay.created, false);
  assert.equal(replay.state, "published");
  assert.equal(replay.terminal.bindingAfter.scene_snapshot_digest, after.scene_snapshot_digest);
  const replayAfterIntentMutation = await runtime.recorder.prepareReviewTerminal({
    scope,
    run,
    mode: "visual_loop",
    binding: after,
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: "3".repeat(64),
  });
  assert.equal(replayAfterIntentMutation.created, false);
  assert.equal(replayAfterIntentMutation.inputDigest, REVIEW_INPUT_DIGEST);
  await assert.rejects(
    runtime.recorder.prepareReviewTerminal({
      scope,
      run,
      mode: "visual_loop",
      binding: after,
      requestDigest: "1".repeat(64),
      inputDigest: "1".repeat(64),
    }),
    (error) => error.code === "ARTIFACT_IDEMPOTENCY_CONFLICT",
  );
});

test("prepared Review replay rejects a changed server-owned input digest", async (t) => {
  const { runtime } = await fixture(t);
  const scope = {
    scopeId: `review-${"8".repeat(64)}`,
    journalAccess: { ownerId: OWNER, sessionId: SESSION },
  };
  const run = { runId: "prepared-input-bound-run", startedAt: Date.parse(NOW) };
  const binding = reviewBinding(scope.scopeId, "7".repeat(64));
  await runtime.recorder.prepareReviewTerminal({
    scope,
    run,
    mode: "text_loop",
    binding,
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: REVIEW_INPUT_DIGEST,
  });
  const sameRequestAfterIntentMutation = await runtime.recorder.prepareReviewTerminal({
    scope,
    run,
    mode: "text_loop",
    binding,
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: "3".repeat(64),
  });
  assert.equal(sameRequestAfterIntentMutation.created, false);
  assert.equal(sameRequestAfterIntentMutation.state, "prepared");
  assert.equal(sameRequestAfterIntentMutation.inputDigest, REVIEW_INPUT_DIGEST);
  await assert.rejects(
    runtime.recorder.prepareReviewTerminal({
      scope,
      run,
      mode: "text_loop",
      binding,
      requestDigest: "2".repeat(64),
      inputDigest: "2".repeat(64),
    }),
    (error) => error.code === "ARTIFACT_IDEMPOTENCY_CONFLICT",
  );
  await assert.rejects(
    runtime.recorder.prepareReviewTerminal({
      scope,
      run,
      mode: "visual_loop",
      binding,
      requestDigest: REVIEW_REQUEST_DIGEST,
      inputDigest: REVIEW_INPUT_DIGEST,
    }),
    (error) => error.code === "ARTIFACT_IDEMPOTENCY_CONFLICT",
  );
});

test("Review outbox marks readiness exhausted immediately at the exact capacity boundary", async (t) => {
  const { runtime } = await fixture(t, {}, { maxReviewOutboxRecords: 2 });
  const scope = {
    scopeId: `review-${"d".repeat(64)}`,
    journalAccess: { ownerId: OWNER, sessionId: SESSION },
  };
  await runtime.recorder.prepareReviewTerminal({
    scope,
    run: { runId: "capacity-run-1", startedAt: Date.parse(NOW) },
    mode: "text_loop",
    binding: reviewBinding(scope.scopeId),
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: REVIEW_INPUT_DIGEST,
  });
  assert.equal((await runtime.readinessProbe()).status, "ready");

  await runtime.recorder.prepareReviewTerminal({
    scope,
    run: { runId: "capacity-run-2", startedAt: Date.parse(NOW) },
    mode: "visual_loop",
    binding: reviewBinding(scope.scopeId),
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: REVIEW_INPUT_DIGEST,
  });
  const exhausted = await runtime.readinessProbe();
  assert.equal(exhausted.status, "not_ready");
  assert.equal(exhausted.causes[0].code, "ARTIFACT_REVIEW_OUTBOX_CAPACITY_EXHAUSTED");
  assert.equal(exhausted.revision.review_outbox_record_count, 2);
  assert.equal(exhausted.revision.max_review_outbox_record_count, 2);
  await assert.rejects(
    runtime.recorder.prepareReviewTerminal({
      scope,
      run: { runId: "capacity-run-3", startedAt: Date.parse(NOW) },
      mode: "text_loop",
      binding: reviewBinding(scope.scopeId),
      requestDigest: REVIEW_REQUEST_DIGEST,
      inputDigest: REVIEW_INPUT_DIGEST,
    }),
    (error) => error.code === "ARTIFACT_REVIEW_OUTBOX_CAPACITY_EXHAUSTED"
      && error.retryable === false,
  );
});

test("one owner cannot consume another owner's Review outbox allocation", async (t) => {
  const { runtime } = await fixture(t, {}, {
    maxReviewOutboxRecords: 4,
    maxReviewOutboxRecordsPerOwner: 1,
  });
  const firstScope = {
    scopeId: `review-${"1".repeat(64)}`,
    journalAccess: { ownerId: "owner-first", sessionId: "session-first" },
  };
  const secondScope = {
    scopeId: `review-${"2".repeat(64)}`,
    journalAccess: { ownerId: "owner-second", sessionId: "session-second" },
  };
  await runtime.recorder.prepareReviewTerminal({
    scope: firstScope,
    run: { runId: "first-owner-run", startedAt: Date.parse(NOW) },
    mode: "text_loop",
    binding: reviewBinding(firstScope.scopeId),
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: REVIEW_INPUT_DIGEST,
  });
  await assert.rejects(
    runtime.recorder.prepareReviewTerminal({
      scope: firstScope,
      run: { runId: "first-owner-over-quota", startedAt: Date.parse(NOW) },
      mode: "visual_loop",
      binding: reviewBinding(firstScope.scopeId),
      requestDigest: "1".repeat(64),
      inputDigest: "2".repeat(64),
    }),
    (error) => error.code === "ARTIFACT_REVIEW_OWNER_QUOTA_EXHAUSTED"
      && error.retryable === false,
  );
  const other = await runtime.recorder.prepareReviewTerminal({
    scope: secondScope,
    run: { runId: "second-owner-run", startedAt: Date.parse(NOW) },
    mode: "visual_loop",
    binding: reviewBinding(secondScope.scopeId),
    requestDigest: "3".repeat(64),
    inputDigest: "4".repeat(64),
  });
  assert.equal(other.created, true);
  assert.equal((await runtime.readinessProbe()).status, "ready");
});

test("published receipts do not consume an owner's active Review allocation", async (t) => {
  const { runtime } = await fixture(t, {}, {
    maxReviewOutboxRecords: 4,
    maxReviewOutboxRecordsPerOwner: 1,
  });
  const scope = {
    scopeId: `review-${"4".repeat(64)}`,
    journalAccess: { ownerId: OWNER, sessionId: SESSION },
  };
  const binding = reviewBinding(scope.scopeId);
  const first = await runtime.recorder.prepareReviewTerminal({
    scope,
    run: { runId: "published-owner-run", startedAt: Date.parse(NOW) },
    mode: "text_loop",
    binding,
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: REVIEW_INPUT_DIGEST,
  });
  await runtime.recorder.ensureReviewTerminal({
    ticket: first,
    terminal: passingReviewTerminal(binding),
  });
  const second = await runtime.recorder.prepareReviewTerminal({
    scope,
    run: { runId: "next-owner-run", startedAt: Date.parse(NOW) },
    mode: "visual_loop",
    binding,
    requestDigest: "5".repeat(64),
    inputDigest: "6".repeat(64),
  });
  assert.equal(second.created, true);
});

test("operator-authenticated abandonment is audited and permanently forbids old-run replay", async (t) => {
  const { root, runtime } = await fixture(t, {
    VISTA_REVIEW_ABANDON_OPERATOR_ID: "operator-primary",
    VISTA_REVIEW_ABANDON_TOKEN_SHA256: REVIEW_ABANDON_TOKEN_SHA256,
  }, {
    maxReviewOutboxRecords: 4,
    maxReviewOutboxRecordsPerOwner: 1,
  });
  const scope = {
    scopeId: `review-${"5".repeat(64)}`,
    journalAccess: { ownerId: OWNER, sessionId: SESSION },
  };
  const run = { runId: "orphaned-paid-run", startedAt: Date.parse(NOW) };
  const binding = reviewBinding(scope.scopeId);
  const ticket = await runtime.recorder.prepareReviewTerminal({
    scope,
    run,
    mode: "text_loop",
    binding,
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: REVIEW_INPUT_DIGEST,
  });
  const decision = {
    decisionId: "abandon-orphaned-paid-run-001",
    decidedAt: NOW,
    reasonCode: "provider_charge_reconciled_no_result",
    evidenceDigest: "a".repeat(64),
  };
  const identity = {
    lookupDigest: ticket.lookupDigest,
    ownerId: ticket.ownerId,
    sessionId: ticket.sessionId,
    journalRunId: ticket.journalRunId,
  };

  await assert.rejects(
    runtime.recorder.abandonPreparedReview({
      identity,
      authorizationToken: "wrong-operator-token-material-0123456789abcdef",
      decision,
    }),
    (error) => error.code === "ARTIFACT_REVIEW_OPERATOR_AUTH_REQUIRED"
      && error.retryable === false,
  );
  const outboxPath = path.join(root, "review-outbox", `${ticket.lookupDigest}.json`);
  assert.equal(JSON.parse(await fs.readFile(outboxPath, "utf8")).state, "prepared");

  const abandoned = await runtime.recorder.abandonPreparedReview({
    identity,
    authorizationToken: REVIEW_ABANDON_TOKEN,
    decision,
  });
  assert.equal(abandoned.state, "abandoned");
  assert.equal(abandoned.idempotent, false);
  const stored = JSON.parse(await fs.readFile(outboxPath, "utf8"));
  assert.equal(stored.state, "abandoned");
  assert.equal(stored.abandonment.operator_id, "operator-primary");
  assert.equal(stored.abandonment.reason_code, decision.reasonCode);
  assert.equal(stored.abandonment.evidence_digest, decision.evidenceDigest);
  assert.doesNotMatch(JSON.stringify(stored), new RegExp(REVIEW_ABANDON_TOKEN));

  const repeat = await runtime.recorder.abandonPreparedReview({
    identity,
    authorizationToken: REVIEW_ABANDON_TOKEN,
    decision,
  });
  assert.equal(repeat.idempotent, true);
  assert.equal(repeat.abandonmentDigest, abandoned.abandonmentDigest);
  const auditPage = await runtime.journal.listRevisions({
    ownerId: OWNER,
    kind: "vista-review-abandonment",
  });
  assert.equal(auditPage.revisions.length, 1);
  const audit = await runtime.journal.readRevision({
    kind: "vista-review-abandonment",
    artifactId: auditPage.revisions[0].artifact.id,
    revision: "abandoned",
    ownerId: OWNER,
  });
  assert.equal(audit.content.operator_id, "operator-primary");
  assert.equal(audit.content.outbox_abandonment_digest, abandoned.abandonmentDigest);

  await assert.rejects(
    runtime.recorder.prepareReviewTerminal({
      scope,
      run,
      mode: "text_loop",
      binding,
      requestDigest: REVIEW_REQUEST_DIGEST,
      inputDigest: REVIEW_INPUT_DIGEST,
    }),
    (error) => error.code === "ARTIFACT_REVIEW_RUN_ABANDONED" && error.retryable === false,
  );
  await assert.rejects(
    runtime.recorder.ensureReviewTerminal({
      ticket,
      terminal: passingReviewTerminal(binding),
    }),
    (error) => error.code === "ARTIFACT_REVIEW_RUN_ABANDONED" && error.retryable === false,
  );

  const replacement = await runtime.recorder.prepareReviewTerminal({
    scope,
    run: { runId: "operator-approved-new-run", startedAt: Date.parse(NOW) },
    mode: "text_loop",
    binding,
    requestDigest: "b".repeat(64),
    inputDigest: "c".repeat(64),
  });
  assert.equal(replacement.created, true, "abandoned receipts must release active owner quota");
  const readiness = await runtime.readinessProbe({ force: true });
  assert.equal(readiness.status, "ready");
  assert.deepEqual(readiness.revision.review_outbox, {
    prepared: 1,
    terminal_pending: 0,
    published: 0,
    abandoned: 1,
  });
});

test("Review abandonment is unavailable unless both operator identity and token digest are configured", async (t) => {
  const { runtime } = await fixture(t, {
    VISTA_REVIEW_ABANDON_OPERATOR_ID: "operator-primary",
  });
  const scope = {
    scopeId: `review-${"6".repeat(64)}`,
    journalAccess: { ownerId: OWNER, sessionId: SESSION },
  };
  const ticket = await runtime.recorder.prepareReviewTerminal({
    scope,
    run: { runId: "misconfigured-abandonment", startedAt: Date.parse(NOW) },
    mode: "text_loop",
    binding: reviewBinding(scope.scopeId),
    requestDigest: REVIEW_REQUEST_DIGEST,
    inputDigest: REVIEW_INPUT_DIGEST,
  });
  await assert.rejects(
    runtime.recorder.abandonPreparedReview({
      identity: {
        lookupDigest: ticket.lookupDigest,
        ownerId: ticket.ownerId,
        sessionId: ticket.sessionId,
        journalRunId: ticket.journalRunId,
      },
      authorizationToken: REVIEW_ABANDON_TOKEN,
      decision: {
        decisionId: "misconfigured-decision",
        decidedAt: NOW,
        reasonCode: "provider_not_started",
        evidenceDigest: "d".repeat(64),
      },
    }),
    (error) => error.code === "ARTIFACT_REVIEW_ABANDONMENT_CONFIG_INVALID"
      && error.retryable === false,
  );
});

test("terminal races conflict instead of creating a second revision", async (t) => {
  const { runtime } = await fixture(t);
  const artifact = imported();
  const build = buildRecord(artifact);
  const record = timelineRecord(artifact, build, runtime.recorder.sceneBuildLineage({ record: build }));
  await runtime.recorder.ensureTimelineTerminal({ record });
  await assert.rejects(
    runtime.recorder.ensureTimelineTerminal({
      record: { ...record, status: "failed", error: { code: "ANIMATION_RUN_FAILED", retryable: false } },
    }),
    (error) => error instanceof ArtifactJournalRuntimeError
      && error.code === "ARTIFACT_IDEMPOTENCY_CONFLICT",
  );
  assert.equal((await runtime.journal.verifyIntegrity()).revision_count, 1);
});

test("write failure fails closed and corrupt production chains are not ready", async (t) => {
  const root = path.join(os.tmpdir(), `simworld-artifact-failure-${process.pid}-${Date.now()}`);
  await fs.mkdir(root, { mode: 0o700 });
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const failing = createArtifactJournalRuntime({
    env: { NODE_ENV: "production", VISTA_ARTIFACT_JOURNAL_ROOT: root },
    journal: {
      async append() { throw Object.assign(new Error("disk failure at /private/path"), { code: "EIO" }); },
      async verifyIntegrity() { return { status: "valid", entry_count: 0, revision_count: 0, retained_revision_count: 0, orphan_blob_count: 0, head_digest: null }; },
    },
    clock: () => new Date(NOW),
  });
  assert.equal((await failing.readinessProbe()).status, "ready");
  await assert.rejects(
    failing.recorder.ensureImportCommitted({ artifact: imported() }),
    (error) => error.code === "ARTIFACT_JOURNAL_WRITE_FAILED"
      && !error.message.includes("/private/path"),
  );

  const { root: liveRoot, runtime } = await fixture(t);
  await runtime.recorder.ensureImportCommitted({ artifact: imported() });
  const entryDir = path.join(liveRoot, "entries");
  const [entry] = await fs.readdir(entryDir);
  const file = path.join(entryDir, entry);
  const value = JSON.parse(await fs.readFile(file, "utf8"));
  value.artifact.id = "tampered";
  await fs.writeFile(file, JSON.stringify(value), { mode: 0o600 });
  const report = await runtime.readinessProbe({ force: true });
  assert.equal(report.status, "not_ready");
  assert.equal(report.causes[0].code, "ARTIFACT_JOURNAL_CORRUPT");
});

test("readiness verification coalesces and request abort preserves a prior production gate", async (t) => {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-artifact-abort-"));
  const root = path.join(parent, "journal");
  await fs.mkdir(root, { mode: 0o700 });
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  let verifyCalls = 0;
  let releaseVerify;
  const waitingVerify = new Promise((resolve) => { releaseVerify = resolve; });
  const integrity = {
    status: "valid",
    entry_count: 0,
    revision_count: 0,
    retained_revision_count: 0,
    orphan_blob_count: 0,
    head_digest: null,
  };
  const runtime = createArtifactJournalRuntime({
    env: { NODE_ENV: "production", VISTA_ARTIFACT_JOURNAL_ROOT: root },
    journal: {
      async append() { return { created: true }; },
      async verifyIntegrity() {
        verifyCalls += 1;
        if (verifyCalls === 1) return integrity;
        await waitingVerify;
        return integrity;
      },
    },
    clock: () => new Date(NOW),
  });
  assert.equal((await runtime.readinessProbe()).status, "ready");

  const controller = new AbortController();
  const abortedPending = runtime.readinessProbe({ signal: controller.signal, force: true });
  const sharedPending = runtime.readinessProbe({ force: true });
  for (let attempt = 0; attempt < 100 && verifyCalls < 2; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  assert.equal(verifyCalls, 2, "concurrent probes must share one full verification");
  controller.abort();
  assert.deepEqual(
    await runtime.recorder.ensureImportCommitted({ artifact: imported() }),
    { created: true },
    "request cancellation must not revoke a previously verified write gate",
  );
  releaseVerify();
  const aborted = await abortedPending;
  assert.equal(aborted.status, "not_ready");
  assert.equal(aborted.causes[0].code, "ARTIFACT_JOURNAL_CHECK_ABORTED");
  assert.equal((await sharedPending).status, "ready");

  assert.equal((await runtime.readinessProbe()).status, "ready");
  assert.equal(verifyCalls, 2, "cached readiness must not reacquire the journal lock");
  await fs.chmod(root, 0o755);
  const insecure = await runtime.readinessProbe();
  assert.equal(insecure.status, "not_ready");
  assert.equal(insecure.causes[0].code, "ARTIFACT_JOURNAL_ROOT_INSECURE");
  await fs.chmod(root, 0o700);
  await assert.rejects(
    runtime.recorder.ensureImportCommitted({ artifact: imported() }),
    (error) => error.code === "ARTIFACT_JOURNAL_NOT_VERIFIED",
  );
  assert.equal((await runtime.readinessProbe({ force: true })).status, "ready");
  await fs.chmod(path.join(root, "review-outbox"), 0o755);
  const insecureOutbox = await runtime.readinessProbe();
  assert.equal(insecureOutbox.status, "not_ready");
  assert.equal(insecureOutbox.causes[0].code, "ARTIFACT_JOURNAL_ROOT_INSECURE");
});

test("a full journal is integrity-valid but not production-writable", async (t) => {
  const parent = await fs.mkdtemp(path.join(os.tmpdir(), "simworld-artifact-full-"));
  const root = path.join(parent, "journal");
  await fs.mkdir(root, { mode: 0o700 });
  t.after(() => fs.rm(parent, { recursive: true, force: true }));
  const runtime = createArtifactJournalRuntime({
    env: { NODE_ENV: "production", VISTA_ARTIFACT_JOURNAL_ROOT: root },
    journal: {
      limits: { maxJournalEntries: 10_000 },
      async append() { throw new ArtifactRevisionJournalError("ARTIFACT_JOURNAL_LIMIT_EXCEEDED"); },
      async verifyIntegrity() {
        return {
          status: "valid",
          entry_count: 10_000,
          revision_count: 10_000,
          retained_revision_count: 0,
          orphan_blob_count: 0,
          head_digest: "a".repeat(64),
        };
      },
    },
    clock: () => new Date(NOW),
  });
  const report = await runtime.readinessProbe();
  assert.equal(report.status, "not_ready");
  assert.equal(report.causes[0].code, "ARTIFACT_JOURNAL_CAPACITY_EXHAUSTED");
  await assert.rejects(
    runtime.recorder.ensureImportCommitted({ artifact: imported() }),
    (error) => error.code === "ARTIFACT_JOURNAL_LIMIT_EXCEEDED",
  );
});
