"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { createVistaImportService } = require("../vista-import-service");

const SOURCE_CHECKSUM = "a".repeat(64);

function previewFixture() {
  return {
    schema: "vista-simworld-scene/v1",
    scene_id: `mmg_040@${SOURCE_CHECKSUM.slice(0, 12)}`,
    profile: "reconstruction",
    privilege: { profile: "reconstruction" },
    source: { source_checksum: SOURCE_CHECKSUM },
    provenance: {
      importer_version: "1.0.0",
      source_checksum: SOURCE_CHECKSUM,
    },
  };
}

function fixture(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "simworld-vista-import-service-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  let previewCalls = 0;
  const importer = {
    async preview() {
      previewCalls += 1;
      return previewFixture();
    },
    async exportEvaluationSafe(scene) {
      return {
        schema: "vista-evaluation-safe/v1",
        source: { visual_id: scene.scene_id.split("@")[0] },
        dialogue: [],
      };
    },
    ...overrides.importer,
  };
  const artifactRoot = path.join(root, "artifacts");
  const service = createVistaImportService({
    importer,
    artifactRoot,
    clock: () => new Date("2026-07-14T00:00:00.000Z"),
    ...overrides.service,
  });
  return { artifactRoot, importer, root, service, getPreviewCalls: () => previewCalls };
}

const ACCESS = { ownerId: "owner-1", sessionId: "session-1" };

test("preview performs no artifact filesystem writes", async (t) => {
  const { artifactRoot, service } = fixture(t);
  const result = await service.preview({
    datasetRevision: "revision-1",
    sampleId: "mmg_040",
    attempt: 7,
  });
  assert.equal(result.schema, "vista-simworld-scene/v1");
  assert.equal(fs.existsSync(artifactRoot), false);
});

test("commit is atomic and idempotent for the same source identity", async (t) => {
  const { artifactRoot, service } = fixture(t);
  const request = { datasetRevision: "revision-1", sampleId: "mmg_040", attempt: 7 };
  const first = await service.commit(request, ACCESS);
  const second = await service.commit(request, ACCESS);

  assert.equal(first.created, true);
  assert.equal(second.created, false);
  assert.equal(first.artifact_id, second.artifact_id);
  assert.equal(first.idempotency.key, second.idempotency.key);
  assert.equal(first.access.owner_id, ACCESS.ownerId);
  assert.equal(first.access.session_id, ACCESS.sessionId);
  assert.equal(first.scene_spec.schema, "vista-simworld-scene/v1");
  assert.deepEqual(fs.readdirSync(artifactRoot), [`${first.artifact_id}.json`]);

  const stored = JSON.parse(fs.readFileSync(path.join(artifactRoot, `${first.artifact_id}.json`), "utf8"));
  assert.equal(Object.hasOwn(stored, "created"), false);
  assert.equal(stored.created_at, "2026-07-14T00:00:00.000Z");
});

test("concurrent commits select exactly one immutable winner", async (t) => {
  const { service } = fixture(t);
  const request = { datasetRevision: "revision-1", sampleId: "mmg_040", attempt: 7 };
  const results = await Promise.all(Array.from({ length: 6 }, () => service.commit(request, ACCESS)));
  assert.equal(results.filter((item) => item.created).length, 1);
  assert.equal(new Set(results.map((item) => item.artifact_id)).size, 1);
  assert.equal(new Set(results.map((item) => item.idempotency.key)).size, 1);
});

test("journal gate uses persisted server identity and replays after an append failure", async (t) => {
  let failJournal = true;
  const seen = [];
  const { artifactRoot, service } = fixture(t, {
    service: {
      artifactRecorder: {
        async ensureImportCommitted({ artifact }) {
          seen.push(artifact);
          if (failJournal) throw Object.assign(new Error("private journal detail"), {
            name: "ArtifactJournalRuntimeError",
            code: "ARTIFACT_JOURNAL_WRITE_FAILED",
          });
        },
      },
    },
  });
  const request = {
    datasetRevision: "revision-1",
    sampleId: "mmg_040",
    attempt: 7,
    ownerId: "caller-spoof",
    sessionId: "caller-spoof",
  };
  await assert.rejects(
    service.commit(request, ACCESS),
    (error) => error.code === "ARTIFACT_JOURNAL_WRITE_FAILED",
  );
  assert.equal(fs.readdirSync(artifactRoot).length, 1, "immutable domain artifact remains replayable");
  assert.deepEqual(seen[0].access, { owner_id: ACCESS.ownerId, session_id: ACCESS.sessionId });

  failJournal = false;
  const replay = await service.commit(request, { ...ACCESS, sessionId: "reattached-session" });
  assert.equal(replay.created, false);
  assert.deepEqual(seen.at(-1).access, { owner_id: ACCESS.ownerId, session_id: ACCESS.sessionId });
});

test("status is owner-bound and permits a new authenticated browser session to reattach", async (t) => {
  const { service } = fixture(t);
  const committed = await service.commit({
    datasetRevision: "revision-1",
    sampleId: "mmg_040",
    attempt: 7,
  }, ACCESS);
  const status = await service.status(committed.artifact_id, ACCESS);
  assert.equal(status.artifact_id, committed.artifact_id);

  const reattached = await service.status(committed.artifact_id, {
    ownerId: ACCESS.ownerId,
    sessionId: "session-2",
  });
  assert.equal(reattached.artifact_id, committed.artifact_id);
  await assert.rejects(
    service.status(committed.artifact_id, { ownerId: "owner-2", sessionId: ACCESS.sessionId }),
    (error) => error.code === "VISTA_IMPORT_ACCESS_DENIED" && error.statusCode === 403,
  );
});

test("caller filesystem selectors are rejected before importer execution", async (t) => {
  const { getPreviewCalls, service } = fixture(t);
  for (const request of [
    { datasetRevision: "revision-1", sampleId: "mmg_040", attempt: 7, sourcePath: "/etc/passwd" },
    { datasetRevision: "../private", sampleId: "mmg_040", attempt: 7 },
    { datasetRevision: "revision-1", sampleId: "mmg_040", attempt: 7, nested: { file: "x" } },
  ]) {
    await assert.rejects(
      service.preview(request),
      (error) => ["VISTA_IMPORT_PATH_FORBIDDEN", "VISTA_IMPORT_REQUEST_INVALID"].includes(error.code),
    );
  }
  assert.equal(getPreviewCalls(), 0);
});

test("evaluation-safe export is opt-in and delegated from a validated preview", async (t) => {
  const { service } = fixture(t);
  const committed = await service.commit({
    datasetRevision: "revision-1",
    sampleId: "mmg_040",
    attempt: 7,
  }, { ...ACCESS, includeEvaluationSafe: true });
  assert.deepEqual(committed.evaluation_safe, {
    schema: "vista-evaluation-safe/v1",
    source: { visual_id: "mmg_040" },
    dialogue: [],
  });
  assert.equal(Object.hasOwn(committed.evaluation_safe, "timeline"), false);
  assert.equal(Object.hasOwn(committed.evaluation_safe, "environment"), false);
});

test("typed importer validation failures remain readable without exposing a raw cause", async (t) => {
  const { service } = fixture(t, {
    importer: {
      async preview() {
        const error = new Error("Curated source checksum does not match the manifest");
        error.code = "VISTA_CHECKSUM_MISMATCH";
        error.retryable = false;
        throw error;
      },
    },
  });
  await assert.rejects(
    service.preview({ datasetRevision: "revision-1", sampleId: "mmg_040", attempt: 7 }),
    (error) => error.code === "VISTA_CHECKSUM_MISMATCH"
      && error.statusCode === 422
      && error.cause === undefined,
  );
});
