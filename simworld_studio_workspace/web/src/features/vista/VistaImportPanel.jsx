import { useEffect, useMemo, useRef, useState } from "react";
import {
  commitVistaImport,
  executeVistaSceneBuild,
  fetchVistaImportStatus,
  fetchVistaSceneBuildStatus,
  preflightVistaSceneBuild,
  prepareVistaSceneBuild,
  previewVistaImport,
} from "../../api/appApi.js";
import {
  Badge,
  Btn,
  ModalFooter,
  ModalHeader,
  ModalOverlay,
} from "../../components/ui/primitives.jsx";
import {
  createVistaImportRequest,
  DEFAULT_VISTA_IMPORT_SELECTION,
  shortVistaHash,
  summarizeVistaPreview,
  VISTA_IMPORT_CATALOG,
} from "./vistaImportModel.js";
import "./vistaImportPanel.css";

function waitForPoll(ms, signal) {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      const error = new Error("Aborted");
      error.name = "AbortError";
      reject(error);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    if (signal?.aborted) onAbort();
    else signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function ErrorNotice({ error }) {
  if (!error) return null;
  return (
    <div className="vista-import-notice error" role="alert">
      <strong>{error.code || "VISTA_IMPORT_REQUEST_FAILED"}</strong>
      <span>{error.message || "The import request failed."}</span>
      {error.retryable ? <span>Retry is allowed.</span> : null}
    </div>
  );
}

function SourceFields({ summary }) {
  const fields = [
    ["Dataset revision", summary.source.datasetRevision],
    ["Source row", summary.source.sourceRowId],
    ["Visual ID", summary.source.visualId],
    ["Case scope", summary.source.caseScope],
    ["Scenario", summary.source.scenarioType],
    ["Selected attempt", `${summary.source.attemptProvider} / ${summary.source.attemptIndex}`],
  ];
  return (
    <dl className="vista-import-definition-list">
      {fields.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd title={String(value)}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

function PreviewHeader({ icons, onPrepareCommit, summary }) {
  const blockingCount = summary.unresolved.filter((item) => item?.blocking).length;
  return (
    <div className="vista-import-preview-header">
      <div>
        <div className="vista-import-kicker">Validated preview</div>
        <div className="vista-import-preview-title">
          <span>{summary.sceneId}</span>
          <Badge variant="muted">{summary.durationSec} seconds</Badge>
          <Badge variant={blockingCount ? "orange" : "green"}>
            {blockingCount ? `${blockingCount} blocking mappings` : "No blocking mappings"}
          </Badge>
        </div>
      </div>
      <Btn
        data-testid="vista-import-prepare-commit"
        disabled={!summary.canCommit}
        onClick={onPrepareCommit}
        variant="primary"
      >
        {icons?.folder?.(13)}
        Prepare commit
      </Btn>
    </div>
  );
}

function TimelineSection({ summary }) {
  return (
    <section className="vista-import-card vista-import-card-wide" aria-labelledby="vista-import-timeline-title">
      <div className="vista-import-card-header">
        <h3 id="vista-import-timeline-title">12-second timeline</h3>
        <span>{summary.timeline.length} beats</span>
      </div>
      <div className="vista-import-table-wrap">
        <table className="vista-import-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Action</th>
              <th>Target</th>
              <th>Description</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {summary.timeline.map((event) => (
              <tr key={event.eventId} data-testid={`vista-import-beat-${event.atSec}`}>
                <td className="vista-import-mono">{event.timecode}</td>
                <td className="vista-import-mono">{event.action}</td>
                <td className="vista-import-mono">{event.targetId || "—"}</td>
                <td>{event.description}</td>
                <td>
                  <Badge variant={event.unsupported ? "orange" : "green"}>
                    {event.unsupported ? "Unsupported" : "Mapped"}
                  </Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function AssetsSection({ summary }) {
  return (
    <section className="vista-import-card" aria-labelledby="vista-import-assets-title">
      <div className="vista-import-card-header">
        <h3 id="vista-import-assets-title">Asset bindings</h3>
        <span>{summary.entities.filter((entity) => entity.resolved).length}/{summary.entities.length} resolved</span>
      </div>
      <div className="vista-import-list">
        {summary.entities.map((entity) => (
          <div className="vista-import-list-row" key={entity.id}>
            <div className="vista-import-list-copy">
              <strong>{entity.query}</strong>
              <span className="vista-import-mono" title={entity.path || entity.sourcePointer || ""}>
                {entity.path || "No verified asset selected"}
              </span>
              {entity.resolved ? (
                <span>
                  {entity.assetId ? `${entity.assetId} · ` : ""}
                  {entity.confidence !== null
                    ? `confidence ${entity.confidence <= 1 ? `${Math.round(entity.confidence * 100)}%` : entity.confidence}`
                    : "confidence not reported"}
                  {entity.selectedBy ? ` · ${entity.selectedBy.replaceAll("_", " ")}` : ""}
                </span>
              ) : null}
              {entity.candidates.length ? (
                <span title={entity.candidates.map((candidate) => candidate?.asset_id || candidate).join(", ")}>
                  {entity.candidates.length} candidate{entity.candidates.length === 1 ? "" : "s"} retained
                </span>
              ) : null}
            </div>
            <Badge variant={entity.resolved ? "green" : "orange"}>
              {entity.resolved ? "Bound" : "Unresolved"}
            </Badge>
          </div>
        ))}
      </div>
    </section>
  );
}

function ValidationSection({ summary }) {
  return (
    <section className="vista-import-card" aria-labelledby="vista-import-validation-title">
      <div className="vista-import-card-header">
        <h3 id="vista-import-validation-title">Validation</h3>
        <span>Client contract checks</span>
      </div>
      <div className="vista-import-list">
        {summary.checks.map((check) => (
          <div className="vista-import-list-row compact" key={check.id}>
            <div className="vista-import-list-copy">
              <strong>{check.label}</strong>
              <span title={check.detail}>{check.detail}</span>
            </div>
            <Badge variant={check.passed ? "green" : "red"}>
              {check.passed ? "Pass" : "Fail"}
            </Badge>
          </div>
        ))}
      </div>
    </section>
  );
}

function UnresolvedSection({ summary }) {
  return (
    <section className="vista-import-card" aria-labelledby="vista-import-unresolved-title">
      <div className="vista-import-card-header">
        <h3 id="vista-import-unresolved-title">Unresolved mappings</h3>
        <span>{summary.unresolved.length} reported</span>
      </div>
      {summary.unresolved.length ? (
        <div className="vista-import-list">
          {summary.unresolved.map((item, index) => (
            <div className="vista-import-list-row" key={item?.mapping_id || index}>
              <div className="vista-import-list-copy">
                <strong>{item?.reason_code || "unresolved_mapping"}</strong>
                <span>{item?.message || "No mapping detail was provided."}</span>
                <span className="vista-import-mono">{item?.source_pointer || "No source pointer"}</span>
              </div>
              <Badge variant={item?.blocking ? "orange" : "muted"}>
                {item?.blocking ? "Blocking" : "Review"}
              </Badge>
            </div>
          ))}
        </div>
      ) : (
        <div className="vista-import-empty-line">No unresolved mappings were reported.</div>
      )}
    </section>
  );
}

function ProvenanceSection({ summary }) {
  return (
    <section className="vista-import-card" aria-labelledby="vista-import-provenance-title">
      <div className="vista-import-card-header">
        <h3 id="vista-import-provenance-title">Source and provenance</h3>
        <Badge variant="muted">reconstruction-only</Badge>
      </div>
      <SourceFields summary={summary} />
      <div className="vista-import-provenance">
        <span>{summary.provenance.importerName} {summary.provenance.importerVersion}</span>
        <code title={summary.provenance.sourceChecksum}>
          sha256:{shortVistaHash(summary.provenance.sourceChecksum, 16)}
        </code>
        <code title={summary.provenance.bundleId}>{summary.provenance.bundleId}</code>
      </div>
    </section>
  );
}

function ArtifactStatus({ artifact, icons, onRefresh, statusBusy, statusError }) {
  if (!artifact) return null;
  const artifactId = artifact.artifact_id || artifact.run_id || "Not available";
  return (
    <div className="vista-import-artifact" data-testid="vista-import-artifact-status">
      <div className="vista-import-artifact-main">
        <Badge variant={artifact.status === "committed" ? "green" : "muted"}>
          {artifact.status || "Committed"}
        </Badge>
        <div>
          <strong>{artifact.created === false ? "Existing artifact returned" : "Scene artifact committed"}</strong>
          <code title={artifactId}>{artifactId}</code>
        </div>
      </div>
      <Btn disabled={statusBusy} onClick={onRefresh} size="xs" variant="ghost">
        {icons?.refresh?.(12)}
        {statusBusy ? "Checking" : "Refresh status"}
      </Btn>
      {statusError ? <ErrorNotice error={statusError} /> : null}
    </div>
  );
}

function SceneBuildStatus({
  build,
  buildError,
  busy,
  onExecute,
  onPrepare,
  onPreflight,
  preflight,
}) {
  const plan = build?.plan;
  const result = build?.last_result;
  const state = result?.status || build?.state || "not prepared";
  return (
    <section className="vista-import-build" data-testid="vista-scene-build-status">
      <div className="vista-import-build-header">
        <div>
          <div className="vista-import-kicker">Unreal scene build</div>
          <strong>{plan ? plan.plan_id : "No BuildPlan prepared"}</strong>
          <span>
            {plan
              ? "Server-pinned assets, numeric transforms, strict preflight, and rollback."
              : "Prepare an exact plan from the committed dataset artifact before touching Unreal."}
          </span>
        </div>
        <Badge variant={state === "succeeded" || state === "already_applied" ? "green" : state === "failed" ? "red" : "muted"}>
          {state.replaceAll("_", " ")}
        </Badge>
      </div>
      {plan ? (
        <dl className="vista-import-build-grid">
          <div><dt>Actors</dt><dd>{plan.actors.length}</dd></div>
          <div><dt>Layout</dt><dd title={plan.layout_revision}>{plan.layout_revision}</dd></div>
          <div><dt>Asset snapshot</dt><dd title={plan.asset_snapshot_id}>{plan.asset_snapshot_id}</dd></div>
          <div><dt>Content revision</dt><dd title={plan.content_revision}>{plan.content_revision}</dd></div>
        </dl>
      ) : null}
      {preflight ? (
        <div className="vista-import-notice success" data-testid="vista-scene-build-preflight-ready">
          <strong>UE preflight ready</strong>
          <span>{preflight.preflight?.assets?.length || 0} pinned assets and {preflight.preflight?.actors?.length || 0} deterministic actor names checked.</span>
        </div>
      ) : null}
      {result?.rollback?.state && result.rollback.state !== "not_required" ? (
        <div className={`vista-import-notice ${result.rollback.state === "completed" ? "success" : "warning"}`}>
          <strong>Rollback {result.rollback.state}</strong>
          <span>{result.rollback.deleted_actor_names?.length || 0} new actors removed.</span>
        </div>
      ) : null}
      <ErrorNotice error={buildError} />
      <div className="vista-import-build-actions">
        <Btn data-testid="vista-scene-build-prepare" disabled={Boolean(busy)} onClick={onPrepare} variant={plan ? "ghost" : "primary"}>
          {busy === "plan" ? "Preparing" : plan ? "Recheck plan" : "Prepare 3D BuildPlan"}
        </Btn>
        <Btn data-testid="vista-scene-build-preflight" disabled={!plan || Boolean(busy)} onClick={onPreflight} variant="ghost">
          {busy === "preflight" ? "Checking UE" : "Run UE preflight"}
        </Btn>
        <Btn data-testid="vista-scene-build-execute" disabled={!plan || !preflight?.ready || Boolean(busy)} onClick={onExecute} variant="primary">
          {busy === "execute" ? "Building scene" : "Build scene in Unreal"}
        </Btn>
      </div>
    </section>
  );
}

function BuildConfirmDialog({ busy, build, onClose, onConfirm }) {
  const plan = build.plan;
  return (
    <ModalOverlay maxWidth={640} onClose={busy ? undefined : onClose}>
      <ModalHeader
        onClose={busy ? undefined : onClose}
        subtitle="Only the exact preflighted BuildPlan will run. New actors are removed and PlayerStart is restored if a required step fails."
        title="Build verified scene in Unreal"
      />
      <div className="vista-import-confirm-body">
        <div className="vista-import-confirm-grid">
          <span>Plan</span><code>{plan.plan_id}</code>
          <span>Scene</span><strong>{plan.scene_id}</strong>
          <span>Actors</span><strong>{plan.actors.length}</strong>
          <span>Asset snapshot</span><code>{plan.asset_snapshot_id}</code>
          <span>Content revision</span><code>{plan.content_revision}</code>
        </div>
        <div className="vista-import-notice warning">
          <strong>Unreal scene mutation</strong>
          <span>This operation spawns the listed actors, updates PlayerStart when needed, then captures collision, floating, actor, and viewport evidence.</span>
        </div>
      </div>
      <ModalFooter>
        <Btn disabled={busy} onClick={onClose} variant="cancel">Cancel</Btn>
        <Btn data-testid="vista-scene-build-confirm" disabled={busy} onClick={onConfirm} variant="primary">
          {busy ? "Building scene" : "Confirm exact BuildPlan"}
        </Btn>
      </ModalFooter>
    </ModalOverlay>
  );
}

function CommitDialog({ busy, onClose, onCommit, summary }) {
  const blockingCount = summary.unresolved.filter((item) => item?.blocking).length;
  return (
    <ModalOverlay maxWidth={620} onClose={busy ? undefined : onClose}>
      <ModalHeader
        onClose={busy ? undefined : onClose}
        subtitle="Creates an immutable, session-bound artifact. This does not start Unreal or mutate the active scene."
        title="Commit VISTA scene artifact"
      />
      <div className="vista-import-confirm-body">
        <div className="vista-import-confirm-grid">
          <span>Dataset revision</span><strong>{summary.source.datasetRevision}</strong>
          <span>Source</span><strong>{summary.source.visualId} / attempt {summary.source.attemptIndex}</strong>
          <span>Scene</span><strong>{summary.sceneId}</strong>
          <span>Duration</span><strong>{summary.durationSec} seconds</strong>
          <span>Source checksum</span><code>{shortVistaHash(summary.source.sourceChecksum, 20)}</code>
        </div>
        <div className={`vista-import-notice ${blockingCount ? "warning" : "success"}`}>
          <strong>{blockingCount ? "Unresolved mappings retained" : "Preview ready"}</strong>
          <span>
            {blockingCount
              ? `${blockingCount} blocking mappings will remain explicit in the artifact; no basic geometry fallback will be inserted.`
              : "All preview contract checks passed."}
          </span>
        </div>
      </div>
      <ModalFooter>
        <Btn disabled={busy} onClick={onClose} variant="cancel">Cancel</Btn>
        <Btn data-testid="vista-import-confirm-commit" disabled={busy} onClick={onCommit} variant="primary">
          {busy ? "Committing" : "Commit scene artifact"}
        </Btn>
      </ModalFooter>
    </ModalOverlay>
  );
}

export default function VistaImportPanel({ icons }) {
  const [selection, setSelection] = useState(DEFAULT_VISTA_IMPORT_SELECTION);
  const [preview, setPreview] = useState(null);
  const [previewRequest, setPreviewRequest] = useState(null);
  const [artifact, setArtifact] = useState(null);
  const [error, setError] = useState(null);
  const [statusError, setStatusError] = useState(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [commitBusy, setCommitBusy] = useState(false);
  const [statusBusy, setStatusBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [build, setBuild] = useState(null);
  const [buildPreflight, setBuildPreflight] = useState(null);
  const [buildError, setBuildError] = useState(null);
  const [buildBusy, setBuildBusy] = useState(null);
  const [buildConfirmOpen, setBuildConfirmOpen] = useState(false);
  const controllerRef = useRef(null);
  const generationRef = useRef(0);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const revision = VISTA_IMPORT_CATALOG.find(
    (entry) => entry.revision === selection.datasetRevision,
  ) || VISTA_IMPORT_CATALOG[0];
  const sample = revision.samples.find((entry) => entry.sampleId === selection.sampleId)
    || revision.samples[0];
  const summary = useMemo(
    () => (preview ? summarizeVistaPreview(preview, previewRequest) : null),
    [preview, previewRequest],
  );

  function updateSelection(nextSelection) {
    generationRef.current += 1;
    controllerRef.current?.abort();
    setSelection(nextSelection);
    setPreview(null);
    setPreviewRequest(null);
    setArtifact(null);
    setError(null);
    setStatusError(null);
    setConfirmOpen(false);
    setBuild(null);
    setBuildPreflight(null);
    setBuildError(null);
    setBuildBusy(null);
    setBuildConfirmOpen(false);
  }

  async function handlePreview() {
    const generation = generationRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setPreviewBusy(true);
    setError(null);
    setStatusError(null);
    setArtifact(null);
    try {
      const request = createVistaImportRequest(selection);
      const result = await previewVistaImport(request, controller.signal);
      if (generation === generationRef.current) {
        setPreviewRequest(request);
        setPreview(result);
      }
    } catch (requestError) {
      if (requestError?.name !== "AbortError") setError(requestError);
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null;
      setPreviewBusy(false);
    }
  }

  async function refreshStatus(artifactOverride = artifact) {
    const artifactId = artifactOverride?.artifact_id || artifactOverride?.run_id;
    if (!artifactId) return;
    const generation = generationRef.current;
    setStatusBusy(true);
    setStatusError(null);
    try {
      const result = await fetchVistaImportStatus(artifactId);
      if (generation === generationRef.current) {
        setArtifact((current) => ({
          ...current,
          ...result,
          created: current?.created ?? artifactOverride?.created,
        }));
      }
      try {
        const buildStatus = await fetchVistaSceneBuildStatus(artifactId, build?.profile_id);
        if (generation === generationRef.current) setBuild(buildStatus);
      } catch (buildStatusError) {
        if (build?.plan && generation === generationRef.current) throw buildStatusError;
      }
    } catch (requestError) {
      setStatusError(requestError);
    } finally {
      setStatusBusy(false);
    }
  }

  async function handleCommit() {
    if (!summary?.canCommit || !previewRequest) return;
    setCommitBusy(true);
    setError(null);
    setStatusError(null);
    try {
      const result = await commitVistaImport(previewRequest);
      setArtifact(result);
      setConfirmOpen(false);
      await refreshStatus(result);
    } catch (requestError) {
      setConfirmOpen(false);
      setError(requestError);
    } finally {
      setCommitBusy(false);
    }
  }

  async function handlePrepareBuild() {
    const artifactId = artifact?.artifact_id || artifact?.run_id;
    if (!artifactId) return;
    const generation = generationRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBuildBusy("plan");
    setBuildError(null);
    setBuildPreflight(null);
    try {
      const prepared = await prepareVistaSceneBuild(artifactId, build?.profile_id, controller.signal);
      if (generation === generationRef.current) setBuild(prepared);
    } catch (requestError) {
      if (requestError?.name !== "AbortError") setBuildError(requestError);
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setBuildBusy(null);
      }
    }
  }

  async function handleBuildPreflight() {
    const artifactId = artifact?.artifact_id || artifact?.run_id;
    if (!artifactId || !build?.plan?.plan_id) return;
    const generation = generationRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBuildBusy("preflight");
    setBuildError(null);
    setBuildPreflight(null);
    try {
      const checked = await preflightVistaSceneBuild(
        artifactId,
        build.plan.plan_id,
        build.profile_id,
        controller.signal,
      );
      if (generation === generationRef.current) setBuildPreflight(checked);
    } catch (requestError) {
      if (requestError?.name !== "AbortError") setBuildError(requestError);
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setBuildBusy(null);
      }
    }
  }

  async function handleBuildExecute() {
    const artifactId = artifact?.artifact_id || artifact?.run_id;
    if (!artifactId || !build?.plan?.plan_id || !buildPreflight?.ready) return;
    const generation = generationRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setBuildBusy("execute");
    setBuildError(null);
    try {
      const execution = await executeVistaSceneBuild(
        artifactId,
        build.plan.plan_id,
        build.profile_id,
        controller.signal,
      );
      if (generation !== generationRef.current) return;
      setBuild((current) => ({ ...current, state: execution.status, last_result: execution.result }));
      setBuildConfirmOpen(false);
      setBuildPreflight(null);
      let status = execution;
      let executionState = execution.status;
      for (let attempt = 0; executionState === "pending" && attempt < 180; attempt += 1) {
        await waitForPoll(1_000, controller.signal);
        status = await fetchVistaSceneBuildStatus(artifactId, build.profile_id, controller.signal);
        executionState = status.state;
        if (generation !== generationRef.current) return;
        setBuild(status);
      }
      if (executionState === "pending") {
        const timeout = new Error("Scene build is still running; refresh status to continue monitoring.");
        timeout.code = "SCENE_BUILD_STATUS_TIMEOUT";
        throw timeout;
      }
    } catch (requestError) {
      if (requestError?.name !== "AbortError") setBuildError(requestError);
      if (requestError?.name !== "AbortError" && requestError?.result) {
        setBuild((current) => ({ ...current, state: "failed", last_result: requestError.result }));
      }
      if (requestError?.name !== "AbortError") {
        setBuildConfirmOpen(false);
        setBuildPreflight(null);
      }
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null;
        setBuildBusy(null);
      }
    }
  }

  return (
    <div className="vista-import-panel" data-testid="vista-import-panel">
      <div className="vista-import-toolbar">
        <div className="vista-import-toolbar-copy">
          <strong>Verified dataset import</strong>
          <span>Operator-curated sources only. Filesystem paths are not accepted.</span>
        </div>
        <label>
          <span>Dataset revision</span>
          <select
            aria-label="VISTA dataset revision"
            disabled={previewBusy || commitBusy || Boolean(buildBusy)}
            onChange={(event) => {
              const nextRevision = VISTA_IMPORT_CATALOG.find((entry) => entry.revision === event.target.value);
              const nextSample = nextRevision.samples[0];
              updateSelection({
                datasetRevision: nextRevision.revision,
                sampleId: nextSample.sampleId,
                attempt: nextSample.attempts[0],
              });
            }}
            value={selection.datasetRevision}
          >
            {VISTA_IMPORT_CATALOG.map((entry) => (
              <option key={entry.revision} value={entry.revision}>{entry.label}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Sample</span>
          <select
            aria-label="VISTA sample"
            disabled={previewBusy || commitBusy || Boolean(buildBusy)}
            onChange={(event) => {
              const nextSample = revision.samples.find((entry) => entry.sampleId === event.target.value);
              updateSelection({
                ...selection,
                sampleId: nextSample.sampleId,
                attempt: nextSample.attempts[0],
              });
            }}
            value={selection.sampleId}
          >
            {revision.samples.map((entry) => (
              <option key={entry.sampleId} value={entry.sampleId}>{entry.label}</option>
            ))}
          </select>
        </label>
        <label>
          <span>Selected attempt</span>
          <select
            aria-label="VISTA selected attempt"
            disabled={previewBusy || commitBusy || Boolean(buildBusy)}
            onChange={(event) => updateSelection({ ...selection, attempt: Number(event.target.value) })}
            value={selection.attempt}
          >
            {sample.attempts.map((attempt) => (
              <option key={attempt} value={attempt}>Attempt {attempt}</option>
            ))}
          </select>
        </label>
        <Btn
          data-testid="vista-import-preview"
          disabled={previewBusy || commitBusy || Boolean(buildBusy)}
          onClick={handlePreview}
          variant="primary"
        >
          {icons?.eye?.(13)}
          {previewBusy ? "Validating" : "Preview import"}
        </Btn>
      </div>

      <ErrorNotice error={error} />

      {!summary && !error ? (
        <div className="vista-import-empty">
          <div className="vista-import-empty-icon">{icons?.folder?.(20)}</div>
          <strong>No preview loaded</strong>
          <span>Select an allowlisted source and validate it before creating an artifact.</span>
        </div>
      ) : null}

      {summary ? (
        <div className="vista-import-preview" data-testid="vista-import-preview-result">
          <PreviewHeader
            icons={icons}
            onPrepareCommit={() => setConfirmOpen(true)}
            summary={summary}
          />
          <ArtifactStatus
            artifact={artifact}
            icons={icons}
            onRefresh={() => refreshStatus()}
            statusBusy={statusBusy}
            statusError={statusError}
          />
          {artifact ? (
            <SceneBuildStatus
              build={build}
              buildError={buildError}
              busy={buildBusy}
              onExecute={() => setBuildConfirmOpen(true)}
              onPrepare={handlePrepareBuild}
              onPreflight={handleBuildPreflight}
              preflight={buildPreflight}
            />
          ) : null}
          <div className="vista-import-grid">
            <ProvenanceSection summary={summary} />
            <ValidationSection summary={summary} />
            <TimelineSection summary={summary} />
            <AssetsSection summary={summary} />
            <UnresolvedSection summary={summary} />
          </div>
        </div>
      ) : null}

      {confirmOpen && summary ? (
        <CommitDialog
          busy={commitBusy}
          onClose={() => setConfirmOpen(false)}
          onCommit={handleCommit}
          summary={summary}
        />
      ) : null}

      {buildConfirmOpen && build?.plan ? (
        <BuildConfirmDialog
          build={build}
          busy={buildBusy === "execute"}
          onClose={() => setBuildConfirmOpen(false)}
          onConfirm={handleBuildExecute}
        />
      ) : null}
    </div>
  );
}
