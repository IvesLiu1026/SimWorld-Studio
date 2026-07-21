"use strict";

const crypto = require("node:crypto");

const REVIEW_SCENE_BINDING_SCHEMA = "simworld-review-scene-binding/v1";
const SAFE_LABEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:@-]{0,159}$/;
const SHA256_RE = /^[a-f0-9]{64}$/;
const MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024;
const MAX_SNAPSHOT_NODES = 100_000;
const MAX_SNAPSHOT_DEPTH = 24;
const MAX_COLLECTION_ENTRIES = 50_000;
const MAX_STRING_BYTES = 256 * 1024;

function isPlainObject(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function addBytes(state, text) {
  const value = String(text);
  state.bytes += Buffer.byteLength(value, "utf8");
  if (state.bytes > MAX_SNAPSHOT_BYTES) throw new TypeError("Review scene snapshot exceeds its size limit");
  return value;
}

function boundedCanonicalJson(value, state = { bytes: 0, nodes: 0 }, depth = 0) {
  state.nodes += 1;
  if (state.nodes > MAX_SNAPSHOT_NODES || depth > MAX_SNAPSHOT_DEPTH) {
    throw new TypeError("Review scene snapshot exceeds its structural limit");
  }
  if (value === null || typeof value === "boolean") return addBytes(state, JSON.stringify(value));
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("Review scene snapshot contains a non-finite number");
    return addBytes(state, JSON.stringify(value));
  }
  if (typeof value === "string") {
    if (Buffer.byteLength(value, "utf8") > MAX_STRING_BYTES) {
      throw new TypeError("Review scene snapshot contains an oversized string");
    }
    return addBytes(state, JSON.stringify(value));
  }
  if (Array.isArray(value)) {
    if (value.length > MAX_COLLECTION_ENTRIES) throw new TypeError("Review scene snapshot array is too large");
    addBytes(state, "[");
    const children = value.map((entry) => boundedCanonicalJson(entry, state, depth + 1));
    if (children.length > 1) addBytes(state, ",".repeat(children.length - 1));
    addBytes(state, "]");
    return `[${children.join(",")}]`;
  }
  if (!isPlainObject(value)) throw new TypeError("Review scene snapshot must contain JSON data only");
  const keys = Object.keys(value).sort();
  if (keys.length > MAX_COLLECTION_ENTRIES) throw new TypeError("Review scene snapshot object is too large");
  addBytes(state, "{");
  const children = keys.map((key) => {
    if (Buffer.byteLength(key, "utf8") > 512) throw new TypeError("Review scene snapshot key is too large");
    const encodedKey = addBytes(state, JSON.stringify(key));
    addBytes(state, ":");
    return `${encodedKey}:${boundedCanonicalJson(value[key], state, depth + 1)}`;
  });
  if (children.length > 1) addBytes(state, ",".repeat(children.length - 1));
  addBytes(state, "}");
  return `{${children.join(",")}}`;
}

function digestText(text) {
  return crypto.createHash("sha256").update(text, "utf8").digest("hex");
}

function safeLabel(value, field) {
  const text = typeof value === "string" ? value.trim() : "";
  if (!SAFE_LABEL_RE.test(text)) throw new TypeError(`${field} is invalid`);
  return text;
}

function safeDigest(value, field) {
  const text = typeof value === "string" ? value.trim().toLowerCase().replace(/^sha256:/, "") : "";
  if (!SHA256_RE.test(text)) throw new TypeError(`${field} is invalid`);
  return text;
}

function exactKeys(value, expected, field) {
  if (!isPlainObject(value)) throw new TypeError(`${field} is invalid`);
  const actual = Object.keys(value).sort();
  const sorted = [...expected].sort();
  if (actual.length !== sorted.length || actual.some((key, index) => key !== sorted[index])) {
    throw new TypeError(`${field} is invalid`);
  }
}

function normalizeSceneBuildLineage(value, field = "sceneBuildLineage") {
  if (value === null || value === undefined) return null;
  exactKeys(value, ["artifact_id", "content_digest", "kind", "revision", "scene_id"], field);
  if (value.kind !== "vista-scene-build") throw new TypeError(`${field}.kind is invalid`);
  return Object.freeze({
    kind: "vista-scene-build",
    artifact_id: safeLabel(value.artifact_id, `${field}.artifact_id`),
    revision: safeLabel(value.revision, `${field}.revision`),
    content_digest: safeDigest(value.content_digest, `${field}.content_digest`),
    scene_id: safeLabel(value.scene_id, `${field}.scene_id`),
  });
}

function normalizeReviewSceneBinding(value, field = "binding") {
  exactKeys(value, [
    "lease_id_sha256", "scene_build_lineage", "scene_revision",
    "scene_snapshot_digest", "schema", "scope_id", "slot_id",
  ], field);
  if (value.schema !== REVIEW_SCENE_BINDING_SCHEMA) throw new TypeError(`${field}.schema is invalid`);
  const slotId = value.slot_id === null ? null : Number(value.slot_id);
  const leaseDigest = value.lease_id_sha256 === null
    ? null : safeDigest(value.lease_id_sha256, `${field}.lease_id_sha256`);
  if ((slotId === null) !== (leaseDigest === null)
      || (slotId !== null && (!Number.isSafeInteger(slotId) || slotId < 0 || slotId > 65535))) {
    throw new TypeError(`${field} lease binding is invalid`);
  }
  const lineage = normalizeSceneBuildLineage(value.scene_build_lineage, `${field}.scene_build_lineage`);
  const snapshotDigest = safeDigest(value.scene_snapshot_digest, `${field}.scene_snapshot_digest`);
  const sceneRevision = safeLabel(value.scene_revision, `${field}.scene_revision`);
  if (lineage && sceneRevision !== lineage.scene_id) {
    throw new TypeError(`${field}.scene_revision does not match scene lineage`);
  }
  if (!lineage && sceneRevision !== `snapshot:${snapshotDigest}`) {
    throw new TypeError(`${field}.scene_revision does not match the actor snapshot`);
  }
  return Object.freeze({
    schema: REVIEW_SCENE_BINDING_SCHEMA,
    scope_id: safeLabel(value.scope_id, `${field}.scope_id`),
    slot_id: slotId,
    lease_id_sha256: leaseDigest,
    scene_revision: sceneRevision,
    scene_snapshot_digest: snapshotDigest,
    scene_build_lineage: lineage,
  });
}

function createReviewSceneBinding({ scope, snapshot, sceneBuildLineage = null } = {}) {
  if (!scope || typeof scope !== "object") throw new TypeError("Review scope is required for scene binding");
  const snapshotCanonical = boundedCanonicalJson(snapshot);
  const snapshotDigest = digestText(snapshotCanonical);
  const lineage = normalizeSceneBuildLineage(sceneBuildLineage);
  const lease = scope.activeLease || null;
  return normalizeReviewSceneBinding({
    schema: REVIEW_SCENE_BINDING_SCHEMA,
    scope_id: safeLabel(scope.scopeId, "scope.scopeId"),
    slot_id: lease ? Number(lease.slotId) : null,
    lease_id_sha256: lease ? digestText(String(lease.leaseId || "")) : null,
    scene_revision: lineage ? lineage.scene_id : `snapshot:${snapshotDigest}`,
    scene_snapshot_digest: snapshotDigest,
    scene_build_lineage: lineage,
  });
}

function reviewSceneBindingDigest(value) {
  return digestText(boundedCanonicalJson(normalizeReviewSceneBinding(value)));
}

function sameReviewSceneBinding(left, right) {
  return reviewSceneBindingDigest(left) === reviewSceneBindingDigest(right);
}

module.exports = {
  REVIEW_SCENE_BINDING_SCHEMA,
  boundedCanonicalJson,
  createReviewSceneBinding,
  normalizeReviewSceneBinding,
  reviewSceneBindingDigest,
  sameReviewSceneBinding,
};
