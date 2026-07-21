export const VISTA_IMPORT_CATALOG = Object.freeze([
  Object.freeze({
    revision: "round1_reviewed_latest",
    label: "Round 1 reviewed (latest)",
    samples: Object.freeze([
      Object.freeze({
        sampleId: "mmg_040",
        label: "mmg_040",
        attempts: Object.freeze([7]),
      }),
    ]),
  }),
]);

export const DEFAULT_VISTA_IMPORT_SELECTION = Object.freeze({
  datasetRevision: VISTA_IMPORT_CATALOG[0].revision,
  sampleId: VISTA_IMPORT_CATALOG[0].samples[0].sampleId,
  attempt: VISTA_IMPORT_CATALOG[0].samples[0].attempts[0],
});

const SHA256_PATTERN = /^[a-f0-9]{64}$/;

function sourceForRevision(datasetRevision) {
  return VISTA_IMPORT_CATALOG.find((entry) => entry.revision === datasetRevision) || null;
}

function sampleForSelection(selection) {
  return sourceForRevision(selection?.datasetRevision)?.samples.find(
    (entry) => entry.sampleId === selection?.sampleId,
  ) || null;
}

export function createVistaImportRequest(selection) {
  const source = sourceForRevision(selection?.datasetRevision);
  const sample = sampleForSelection(selection);
  const attempt = Number(selection?.attempt);
  if (!source || !sample || !sample.attempts.includes(attempt)) {
    throw new Error("Select an allowlisted VISTA source before previewing it.");
  }
  return Object.freeze({
    datasetRevision: source.revision,
    sampleId: sample.sampleId,
    attempt,
  });
}

export function formatVistaTimecode(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "--:--";
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds - minutes * 60;
  const renderedSeconds = Number.isInteger(remainder)
    ? String(remainder).padStart(2, "0")
    : remainder.toFixed(2).padStart(5, "0");
  return `${String(minutes).padStart(2, "0")}:${renderedSeconds}`;
}

export function shortVistaHash(value, visible = 12) {
  const text = typeof value === "string" ? value : "";
  if (text.length <= visible) return text || "Not available";
  return `${text.slice(0, visible)}...`;
}

function normalizedBinding(binding, resolution) {
  const candidates = Array.isArray(resolution?.candidates)
    ? resolution.candidates
    : (Array.isArray(binding?.candidates) ? binding.candidates : []);
  if (typeof binding === "string" && binding.trim()) {
    return {
      path: binding.trim(),
      assetId: null,
      snapshotId: resolution?.snapshot_id || null,
      confidence: null,
      candidates,
      selectedBy: resolution?.selected_by || null,
      override: resolution?.selected_by === "manual_override",
    };
  }
  if (!binding || typeof binding !== "object" || Array.isArray(binding)) {
    return {
      path: null,
      assetId: null,
      snapshotId: resolution?.snapshot_id || null,
      confidence: null,
      candidates,
      selectedBy: resolution?.selected_by || null,
      override: resolution?.selected_by === "manual_override",
    };
  }
  const path = binding.ue_path
    || binding.selected_path
    || binding.asset_path
    || binding.path
    || null;
  const confidence = binding.confidence === null || binding.confidence === undefined
    ? Number.NaN
    : Number(binding.confidence);
  return {
    path: typeof path === "string" && path.trim() ? path.trim() : null,
    assetId: binding.asset_id || binding.id || null,
    snapshotId: binding.snapshot_id || resolution?.snapshot_id || null,
    confidence: Number.isFinite(confidence) ? confidence : null,
    candidates,
    selectedBy: resolution?.selected_by || null,
    override: Boolean(
      binding.override
      || binding.manual_override
      || resolution?.selected_by === "manual_override",
    ),
  };
}

function checksForPreview(scene, expectedRequest) {
  const source = scene?.source || {};
  const timeline = Array.isArray(scene?.timeline) ? scene.timeline : [];
  const duration = Number(scene?.duration_sec);
  const eventIds = new Set();
  const ordered = timeline.every((event, index) => {
    const timestamp = Number(event?.at_sec);
    const eventId = typeof event?.event_id === "string" ? event.event_id : "";
    const previous = timeline[index - 1];
    const previousTimestamp = Number(previous?.at_sec);
    const previousEventId = typeof previous?.event_id === "string" ? previous.event_id : "";
    const valid = Number.isFinite(timestamp)
      && timestamp >= 0
      && timestamp <= duration
      && eventId.length > 0
      && !eventIds.has(eventId)
      && (index === 0
        || timestamp > previousTimestamp
        || (timestamp === previousTimestamp && previousEventId.localeCompare(eventId) < 0));
    eventIds.add(eventId);
    return valid;
  });
  const checksum = source.source_checksum || scene?.provenance?.source_checksum || "";
  const attempt = Number(source?.attempt?.index);

  return Object.freeze([
    Object.freeze({
      id: "schema",
      label: "Scene schema",
      passed: scene?.schema === "vista-simworld-scene/v1",
      detail: scene?.schema || "Missing",
    }),
    Object.freeze({
      id: "identity",
      label: "Allowlisted identity",
      passed: Boolean(expectedRequest)
        && source.dataset_revision === expectedRequest.datasetRevision
        && source.visual_id === expectedRequest.sampleId
        && attempt === expectedRequest.attempt
        && source?.attempt?.selected === true,
      detail: `${source.visual_id || "unknown"} / attempt ${Number.isFinite(attempt) ? attempt : "?"}`,
    }),
    Object.freeze({
      id: "duration",
      label: "Duration",
      passed: Number.isFinite(duration) && duration > 0,
      detail: Number.isFinite(duration) ? `${duration} seconds` : "Missing",
    }),
    Object.freeze({
      id: "timeline",
      label: "Timeline order",
      passed: timeline.length > 0 && ordered,
      detail: `${timeline.length} verified beats · deterministic same-frame order`,
    }),
    Object.freeze({
      id: "checksum",
      label: "Source checksum",
      passed: SHA256_PATTERN.test(checksum),
      detail: shortVistaHash(checksum),
    }),
    Object.freeze({
      id: "privilege",
      label: "Privilege boundary",
      passed: scene?.profile === "reconstruction"
        && scene?.privilege?.classification === "reconstruction_only"
        && scene?.privilege?.evaluation_input_allowed === false,
      detail: scene?.privilege?.classification || "Missing",
    }),
  ]);
}

export function summarizeVistaPreview(scene, expectedRequest) {
  const source = scene?.source || {};
  const provenance = scene?.provenance || {};
  const unresolved = Array.isArray(scene?.unresolved) ? scene.unresolved : [];
  const unsupportedPointers = new Set(
    unresolved
      .filter((item) => item?.kind === "action" || item?.reason_code === "unsupported_action")
      .map((item) => item?.source_pointer)
      .filter(Boolean),
  );
  const timeline = (Array.isArray(scene?.timeline) ? scene.timeline : []).map((event) => ({
    eventId: event?.event_id || "Unidentified beat",
    atSec: Number(event?.at_sec),
    timecode: formatVistaTimecode(event?.at_sec),
    action: event?.action || "unresolved_action",
    description: event?.description || "No description",
    actorId: event?.actor_id || null,
    targetId: event?.target_id || null,
    sourcePointer: event?.source_pointer || null,
    unsupported: unsupportedPointers.has(event?.source_pointer),
  }));
  const entities = (Array.isArray(scene?.entities) ? scene.entities : []).map((entity) => {
    const binding = normalizedBinding(entity?.asset_binding, entity?.asset_resolution);
    return {
      id: entity?.id || "Unidentified entity",
      query: entity?.asset_resolution?.query || entity?.semantic_query || "No semantic query",
      required: entity?.required === true,
      sourcePointer: entity?.source_pointer || null,
      ...binding,
      resolved: Boolean(binding.path),
    };
  });
  const checks = checksForPreview(scene, expectedRequest);

  return Object.freeze({
    scene,
    sceneId: scene?.scene_id || "Not available",
    durationSec: Number(scene?.duration_sec),
    source: Object.freeze({
      datasetRevision: source.dataset_revision || "Not available",
      sourceRowId: source.source_row_id || "Not available",
      visualId: source.visual_id || "Not available",
      caseScope: source.case_scope || "Not available",
      scenarioType: source.scenario_type || "Not available",
      attemptProvider: source?.attempt?.provider || "Not available",
      attemptIndex: Number(source?.attempt?.index),
      sourceChecksum: source.source_checksum || provenance.source_checksum || "",
    }),
    provenance: Object.freeze({
      importerName: provenance.importer_name || "Not available",
      importerVersion: provenance.importer_version || "Not available",
      bundleId: provenance.bundle_id || "Not available",
      sourceChecksum: provenance.source_checksum || source.source_checksum || "",
    }),
    timeline: Object.freeze(timeline),
    entities: Object.freeze(entities),
    unresolved: Object.freeze(unresolved),
    checks,
    canCommit: checks.every((check) => check.passed),
  });
}

export function artifactSceneSpec(artifact) {
  if (artifact?.scene_spec && typeof artifact.scene_spec === "object") return artifact.scene_spec;
  return null;
}
