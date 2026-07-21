import { useEffect, useMemo, useRef, useState } from "react";
import {
  commitVistaImport,
  executeVistaSceneBuild,
  fetchVistaAnimationTimelineStatus,
  fetchVistaImportStatus,
  fetchVistaSceneBuildStatus,
  preflightVistaAnimationTimeline,
  preflightVistaSceneBuild,
  prepareVistaSceneBuild,
  previewVistaImport,
  replayVistaAnimationTimeline,
  startVistaAnimationTimeline,
  stopVistaAnimationTimeline,
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

const TERMINAL_ANIMATION_STATES = new Set(["completed", "failed", "cancelled"]);
const ACTIVE_ANIMATION_STATES = new Set(["pending", "running", "stopping"]);

function isSuccessfulSceneBuild(build) {
  const state = build?.last_result?.status || build?.state;
  return state === "succeeded" || state === "already_applied";
}

function animationStateVariant(state) {
  if (state === "completed") return "green";
  if (state === "failed") return "red";
  if (state === "cancelled") return "orange";
  if (ACTIVE_ANIMATION_STATES.has(state)) return "blue";
  return "muted";
}

function eventStateVariant(state) {
  if (state === "completed") return "green";
  if (["failed", "timed_out"].includes(state)) return "red";
  if (state === "cancelled") return "orange";
  if (["running", "dispatched"].includes(state)) return "blue";
  return "muted";
}

function formatAnimationSeconds(value) {
  if (value === null || value === undefined || value === "") return "—";
  const seconds = Number(value);
  return Number.isFinite(seconds) ? `${Math.max(0, seconds).toFixed(1)} s` : "—";
}

function formatAnimationDrift(value) {
  if (value === null || value === undefined || value === "") return "—";
  const drift = Number(value);
  if (!Number.isFinite(drift)) return "—";
  return `${drift > 0 ? "+" : ""}${Math.round(drift)} ms`;
}

function animationElapsed(status, nowMs) {
  const run = status?.run;
  if (!run) return 0;
  const actual = Math.max(
    0,
    ...(run.events || []).map((event) => (
      event.actual_sec !== null
      && event.actual_sec !== undefined
      && Number.isFinite(Number(event.actual_sec))
        ? Number(event.actual_sec)
        : 0
    )),
  );
  const startedAt = Date.parse(run.started_at || "");
  const endedAt = Date.parse(run.ended_at || "");
  const wallElapsed = Number.isFinite(startedAt)
    ? Math.max(0, ((Number.isFinite(endedAt) ? endedAt : nowMs) - startedAt) / 1000)
    : 0;
  const duration = Number(run.duration_sec);
  const elapsed = Math.max(actual, wallElapsed);
  return Number.isFinite(duration) && duration > 0 ? Math.min(duration, elapsed) : elapsed;
}

function evidenceByEvent(evidence) {
  const result = new Map();
  for (const checkpoint of evidence?.checkpoints || []) {
    if (!checkpoint?.event_id) continue;
    const current = result.get(checkpoint.event_id) || { artifacts: 0, assertion: null };
    const artifacts = Array.isArray(checkpoint.evidence) ? checkpoint.evidence : [];
    current.artifacts += artifacts.length;
    if (artifacts.some((item) => item?.assertion === "fail")) current.assertion = "fail";
    else if (current.assertion !== "fail" && artifacts.some((item) => item?.assertion === "pass")) {
      current.assertion = "pass";
    }
    result.set(checkpoint.event_id, current);
  }
  return result;
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

function PreviewHeader({ icons, locked, onPrepareCommit, summary }) {
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
        disabled={!summary.canCommit || locked}
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
  locked,
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
        <Btn data-testid="vista-scene-build-prepare" disabled={Boolean(busy) || locked} onClick={onPrepare} variant={plan ? "ghost" : "primary"}>
          {busy === "plan" ? "Preparing" : plan ? "Recheck plan" : "Prepare 3D BuildPlan"}
        </Btn>
        <Btn data-testid="vista-scene-build-preflight" disabled={!plan || Boolean(busy) || locked} onClick={onPreflight} variant="ghost">
          {busy === "preflight" ? "Checking UE" : "Run UE preflight"}
        </Btn>
        <Btn data-testid="vista-scene-build-execute" disabled={!plan || !preflight?.ready || Boolean(busy) || locked} onClick={onExecute} variant="primary">
          {busy === "execute" ? "Building scene" : "Build scene in Unreal"}
        </Btn>
      </div>
    </section>
  );
}

function AnimationTimelineWorkbench({
  busy,
  error,
  nowMs,
  onPreflight,
  onRefresh,
  onReplay,
  onStart,
  onStop,
  polling,
  preflight,
  status,
}) {
  const state = status?.status || (preflight?.ready ? "ready" : "not_checked");
  const run = status?.run;
  const terminal = TERMINAL_ANIMATION_STATES.has(status?.status);
  const active = ACTIVE_ANIMATION_STATES.has(status?.status);
  const runEvents = new Map((run?.events || []).map((event) => [event.event_id, event]));
  const eventEvidence = evidenceByEvent(status?.evidence);
  const timelineEvents = (preflight?.events || []).map((event) => ({
    ...event,
    runtime: runEvents.get(event.event_id) || null,
    evidence: eventEvidence.get(event.event_id) || null,
  }));
  const duration = Number(run?.duration_sec || preflight?.duration_sec || 12);
  const elapsed = animationElapsed(status, nowMs);
  const completedEvents = (run?.events || []).filter((event) => event.state === "completed").length;
  const totalEvents = timelineEvents.length || run?.events?.length || 0;
  const drifts = (run?.events || [])
    .filter((event) => event.drift_ms !== null && event.drift_ms !== undefined)
    .map((event) => Number(event.drift_ms))
    .filter(Number.isFinite);
  const peakDrift = drifts.length ? Math.max(...drifts.map(Math.abs)) : null;
  const coverage = status?.evidence?.coverage;
  const progress = duration > 0 ? Math.min(100, Math.max(0, elapsed / duration * 100)) : 0;
  const readyAt = Date.parse(preflight?.expires_at || "");
  const readyUntil = Number.isFinite(readyAt) ? new Date(readyAt).toLocaleTimeString() : "Not checked";

  return (
    <section className="vista-animation-workbench" data-testid="vista-animation-workbench">
      <div className="vista-animation-header">
        <div>
          <div className="vista-import-kicker">12-second execution</div>
          <strong>Verified animation timeline</strong>
          <span>Fixed action program with runtime readiness, timing drift, cleanup, and evidence tracking.</span>
        </div>
        <div className="vista-animation-state">
          {polling ? <span>Live status</span> : null}
          <Badge variant={animationStateVariant(state)}>{state.replaceAll("_", " ")}</Badge>
        </div>
      </div>

      <dl className="vista-animation-readiness" data-testid="vista-animation-readiness">
        <div>
          <dt>Runtime</dt>
          <dd>{preflight?.ready ? "Verified" : "Not checked"}</dd>
        </div>
        <div>
          <dt>Profile</dt>
          <dd title={preflight?.profile_id}>{preflight?.profile_id || "—"}</dd>
        </div>
        <div>
          <dt>Clock</dt>
          <dd>{preflight?.fps ? `${preflight.fps} fps` : "—"}</dd>
        </div>
        <div>
          <dt>Ready until</dt>
          <dd title={preflight?.expires_at}>{readyUntil}</dd>
        </div>
      </dl>

      <div className="vista-animation-metrics">
        <div>
          <span>Elapsed</span>
          <strong data-testid="vista-animation-elapsed">{formatAnimationSeconds(elapsed)} / {formatAnimationSeconds(duration)}</strong>
        </div>
        <div>
          <span>Events</span>
          <strong>{completedEvents} / {totalEvents || "—"}</strong>
        </div>
        <div>
          <span>Peak drift</span>
          <strong data-testid="vista-animation-drift">{formatAnimationDrift(peakDrift)}</strong>
        </div>
        <div>
          <span>Evidence</span>
          <strong>{coverage ? (coverage.complete ? "Complete" : `${coverage.checkpoint_count} checkpoints`) : "Pending"}</strong>
        </div>
      </div>

      <div
        aria-label="Animation timeline progress"
        aria-valuemax={duration}
        aria-valuemin={0}
        aria-valuenow={Number(elapsed.toFixed(1))}
        className="vista-animation-progress"
        role="progressbar"
      >
        <span style={{ width: `${progress}%` }} />
      </div>

      {timelineEvents.length ? (
        <div className="vista-animation-table-wrap">
          <table className="vista-animation-table">
            <thead>
              <tr>
                <th>Planned</th>
                <th>Action</th>
                <th>Actual</th>
                <th>Drift</th>
                <th>State</th>
                <th>Evidence</th>
              </tr>
            </thead>
            <tbody>
              {timelineEvents.map((event) => (
                <tr data-testid={`vista-animation-event-${event.event_id}`} key={event.event_id}>
                  <td className="vista-import-mono">{formatAnimationSeconds(event.at_sec)}</td>
                  <td className="vista-import-mono">{event.action}</td>
                  <td className="vista-import-mono">{formatAnimationSeconds(event.runtime?.actual_sec)}</td>
                  <td className="vista-import-mono">{formatAnimationDrift(event.runtime?.drift_ms)}</td>
                  <td>
                    <Badge variant={eventStateVariant(event.runtime?.state)}>
                      {(event.runtime?.state || "queued").replaceAll("_", " ")}
                    </Badge>
                  </td>
                  <td>
                    {event.evidence ? (
                      <span className={`vista-animation-evidence-state ${event.evidence.assertion === "fail" ? "failed" : ""}`}>
                        {event.evidence.assertion === "pass" ? "Verified" : `${event.evidence.artifacts} artifacts`}
                      </span>
                    ) : <span className="vista-animation-evidence-state">Pending</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="vista-animation-empty">Check the trusted runtime before starting the timeline.</div>
      )}

      {status?.evidence ? (
        <div className="vista-animation-evidence" data-testid="vista-animation-evidence">
          <div>
            <strong>Evidence manifest</strong>
            <code title={status.evidence.manifest_id}>{status.evidence.manifest_id}</code>
          </div>
          <dl>
            <div><dt>Coverage</dt><dd>{coverage?.completed_event_count || 0}/{coverage?.required_event_count || 0} events</dd></div>
            <div><dt>Checkpoints</dt><dd>{coverage?.checkpoint_count || 0}</dd></div>
            <div><dt>Cleanup</dt><dd>{run?.cleanup?.state || "—"}</dd></div>
            <div><dt>Run digest</dt><dd title={status.evidence.run_digest}>{shortVistaHash(status.evidence.run_digest, 14)}</dd></div>
          </dl>
        </div>
      ) : null}

      {status?.error ? (
        <div className="vista-import-notice error">
          <strong>{status.error.code || "ANIMATION_RUN_FAILED"}</strong>
          <span>{status.error.message || "The animation run did not complete."}</span>
        </div>
      ) : null}
      <ErrorNotice error={error} />

      <div className="vista-animation-actions">
        {!status ? (
          <Btn
            data-testid="vista-animation-preflight"
            disabled={Boolean(busy)}
            onClick={onPreflight}
            variant={preflight?.ready ? "ghost" : "primary"}
          >
            {busy === "preflight" ? "Checking runtime" : preflight?.ready ? "Recheck runtime" : "Check runtime and timeline"}
          </Btn>
        ) : null}
        {preflight?.ready && !status ? (
          <Btn data-testid="vista-animation-start" disabled={Boolean(busy)} onClick={onStart} variant="primary">
            Start 12-second execution
          </Btn>
        ) : null}
        {status ? (
          <Btn data-testid="vista-animation-refresh" disabled={Boolean(busy)} onClick={onRefresh} variant="ghost">
            {busy === "refresh" ? "Refreshing" : "Refresh status"}
          </Btn>
        ) : null}
        {active ? (
          <Btn
            data-testid="vista-animation-stop"
            disabled={Boolean(busy) || status.status === "stopping"}
            onClick={onStop}
            variant="cancel"
          >
            {busy === "stop" ? "Stopping" : status.status === "stopping" ? "Stop requested" : "Stop execution"}
          </Btn>
        ) : null}
        {terminal ? (
          <Btn data-testid="vista-animation-replay" disabled={Boolean(busy)} onClick={onReplay} variant="primary">
            {busy === "replay-preflight" ? "Checking replay" : "Replay 12-second execution"}
          </Btn>
        ) : null}
      </div>
    </section>
  );
}

function AnimationConfirmDialog({ busy, mode, onClose, onConfirm, preflight, sourceRunId }) {
  const replay = mode === "replay";
  return (
    <ModalOverlay maxWidth={660} onClose={busy ? undefined : onClose}>
      <ModalHeader
        onClose={busy ? undefined : onClose}
        subtitle="The server will accept only this exact preflight, timeline, program, and verified Scene BuildPlan."
        title={replay ? "Replay verified 12-second execution" : "Start verified 12-second execution"}
      />
      <div className="vista-import-confirm-body">
        <div className="vista-import-confirm-grid">
          <span>Plan</span><code>{preflight.plan_id}</code>
          <span>Preflight</span><code>{preflight.preflight_id}</code>
          <span>Timeline</span><code>{preflight.timeline_id}</code>
          <span>Program</span><code>{preflight.program_id}</code>
          <span>Schedule</span><strong>{preflight.duration_sec} seconds at {preflight.fps} fps</strong>
          {replay ? <><span>Replay source</span><code>{sourceRunId}</code></> : null}
        </div>
        <div className="vista-import-notice warning">
          <strong>Live Unreal execution</strong>
          <span>{replay ? "A new run will execute the same verified revision." : "Character pose, IK contacts, object interactions, and recovery actions will execute in the active scene."} Stop remains available while the run is active.</span>
        </div>
      </div>
      <ModalFooter>
        <Btn disabled={busy} onClick={onClose} variant="cancel">Cancel</Btn>
        <Btn data-testid="vista-animation-confirm" disabled={busy} onClick={onConfirm} variant="primary">
          {busy ? (replay ? "Starting replay" : "Starting execution") : replay ? "Confirm exact replay" : "Confirm exact execution"}
        </Btn>
      </ModalFooter>
    </ModalOverlay>
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
  const [animationPreflight, setAnimationPreflight] = useState(null);
  const [animationStatus, setAnimationStatus] = useState(null);
  const [animationError, setAnimationError] = useState(null);
  const [animationBusy, setAnimationBusy] = useState(null);
  const [animationPolling, setAnimationPolling] = useState(false);
  const [animationConfirmMode, setAnimationConfirmMode] = useState(null);
  const [animationNowMs, setAnimationNowMs] = useState(() => Date.now());
  const controllerRef = useRef(null);
  const animationActionControllerRef = useRef(null);
  const animationPollControllerRef = useRef(null);
  const generationRef = useRef(0);

  useEffect(() => () => {
    controllerRef.current?.abort();
    animationActionControllerRef.current?.abort();
    animationPollControllerRef.current?.abort();
  }, []);

  useEffect(() => {
    if (!ACTIVE_ANIMATION_STATES.has(animationStatus?.status)) return undefined;
    const interval = setInterval(() => setAnimationNowMs(Date.now()), 250);
    return () => clearInterval(interval);
  }, [animationStatus?.status]);

  const revision = VISTA_IMPORT_CATALOG.find(
    (entry) => entry.revision === selection.datasetRevision,
  ) || VISTA_IMPORT_CATALOG[0];
  const sample = revision.samples.find((entry) => entry.sampleId === selection.sampleId)
    || revision.samples[0];
  const summary = useMemo(
    () => (preview ? summarizeVistaPreview(preview, previewRequest) : null),
    [preview, previewRequest],
  );
  const animationActive = ACTIVE_ANIMATION_STATES.has(animationStatus?.status);

  function resetAnimationState() {
    animationActionControllerRef.current?.abort();
    animationActionControllerRef.current = null;
    animationPollControllerRef.current?.abort();
    animationPollControllerRef.current = null;
    setAnimationPreflight(null);
    setAnimationStatus(null);
    setAnimationError(null);
    setAnimationBusy(null);
    setAnimationPolling(false);
    setAnimationConfirmMode(null);
    setAnimationNowMs(Date.now());
  }

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
    resetAnimationState();
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
    setBuild(null);
    setBuildPreflight(null);
    setBuildError(null);
    setBuildConfirmOpen(false);
    resetAnimationState();
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
    resetAnimationState();
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
    resetAnimationState();
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

  function monitorAnimationRun(artifactId, animationRunId, generation) {
    animationPollControllerRef.current?.abort();
    const controller = new AbortController();
    animationPollControllerRef.current = controller;
    setAnimationPolling(true);
    void (async () => {
      try {
        for (let attempt = 0; attempt < 180; attempt += 1) {
          const current = await fetchVistaAnimationTimelineStatus(
            artifactId,
            animationRunId,
            controller.signal,
          );
          if (generation !== generationRef.current) return;
          setAnimationStatus(current);
          setAnimationNowMs(Date.now());
          if (TERMINAL_ANIMATION_STATES.has(current.status)) return;
          await waitForPoll(500, controller.signal);
        }
        const timeout = new Error("Animation status monitoring timed out. Refresh the run to continue.");
        timeout.code = "ANIMATION_STATUS_TIMEOUT";
        timeout.retryable = true;
        throw timeout;
      } catch (requestError) {
        if (requestError?.name !== "AbortError" && generation === generationRef.current) {
          setAnimationError(requestError);
        }
      } finally {
        if (animationPollControllerRef.current === controller) {
          animationPollControllerRef.current = null;
          setAnimationPolling(false);
        }
      }
    })();
  }

  async function requestAnimationPreflight(mode) {
    const artifactId = artifact?.artifact_id || artifact?.run_id;
    if (!artifactId || !build?.plan?.plan_id || !isSuccessfulSceneBuild(build)) return null;
    const generation = generationRef.current;
    animationActionControllerRef.current?.abort();
    const controller = new AbortController();
    animationActionControllerRef.current = controller;
    setAnimationBusy(mode === "replay" ? "replay-preflight" : "preflight");
    setAnimationError(null);
    try {
      const checked = await preflightVistaAnimationTimeline(
        artifactId,
        build.plan.plan_id,
        build.profile_id,
        controller.signal,
      );
      if (generation !== generationRef.current) return null;
      setAnimationPreflight(checked);
      setAnimationNowMs(Date.now());
      return checked;
    } catch (requestError) {
      if (requestError?.name !== "AbortError" && generation === generationRef.current) {
        setAnimationError(requestError);
      }
      return null;
    } finally {
      if (animationActionControllerRef.current === controller) {
        animationActionControllerRef.current = null;
        setAnimationBusy(null);
      }
    }
  }

  async function handleAnimationPreflight() {
    await requestAnimationPreflight("start");
  }

  async function handleAnimationReplayPrepare() {
    if (!TERMINAL_ANIMATION_STATES.has(animationStatus?.status)) return;
    const checked = await requestAnimationPreflight("replay");
    if (checked) setAnimationConfirmMode("replay");
  }

  async function handleAnimationLaunch() {
    const artifactId = artifact?.artifact_id || artifact?.run_id;
    if (!artifactId || !animationPreflight?.ready || !animationConfirmMode) return;
    const generation = generationRef.current;
    const mode = animationConfirmMode;
    const sourceRunId = animationStatus?.run_id;
    animationActionControllerRef.current?.abort();
    const controller = new AbortController();
    animationActionControllerRef.current = controller;
    setAnimationBusy(mode === "replay" ? "replay" : "start");
    setAnimationError(null);
    try {
      const accepted = mode === "replay"
        ? await replayVistaAnimationTimeline(
          artifactId,
          sourceRunId,
          animationPreflight,
          controller.signal,
        )
        : await startVistaAnimationTimeline(artifactId, animationPreflight, controller.signal);
      if (generation !== generationRef.current) return;
      setAnimationStatus(accepted);
      setAnimationConfirmMode(null);
      setAnimationNowMs(Date.now());
      monitorAnimationRun(artifactId, accepted.run_id, generation);
    } catch (requestError) {
      if (requestError?.name !== "AbortError" && generation === generationRef.current) {
        setAnimationError(requestError);
      }
    } finally {
      if (animationActionControllerRef.current === controller) {
        animationActionControllerRef.current = null;
        setAnimationBusy(null);
      }
    }
  }

  async function handleAnimationRefresh() {
    const artifactId = artifact?.artifact_id || artifact?.run_id;
    const animationRunId = animationStatus?.run_id;
    if (!artifactId || !animationRunId) return;
    const generation = generationRef.current;
    setAnimationBusy("refresh");
    setAnimationError(null);
    try {
      const current = await fetchVistaAnimationTimelineStatus(artifactId, animationRunId);
      if (generation === generationRef.current) {
        setAnimationStatus(current);
        setAnimationNowMs(Date.now());
      }
    } catch (requestError) {
      if (generation === generationRef.current) setAnimationError(requestError);
    } finally {
      if (generation === generationRef.current) setAnimationBusy(null);
    }
  }

  async function handleAnimationStop() {
    const artifactId = artifact?.artifact_id || artifact?.run_id;
    const animationRunId = animationStatus?.run_id;
    if (!artifactId || !animationRunId || !ACTIVE_ANIMATION_STATES.has(animationStatus?.status)) return;
    const generation = generationRef.current;
    setAnimationBusy("stop");
    setAnimationError(null);
    try {
      const stopping = await stopVistaAnimationTimeline(artifactId, animationRunId);
      if (generation === generationRef.current) {
        setAnimationStatus(stopping);
        setAnimationNowMs(Date.now());
        if (!animationPollControllerRef.current && !TERMINAL_ANIMATION_STATES.has(stopping.status)) {
          monitorAnimationRun(artifactId, animationRunId, generation);
        }
      }
    } catch (requestError) {
      if (generation === generationRef.current) setAnimationError(requestError);
    } finally {
      if (generation === generationRef.current) setAnimationBusy(null);
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
            disabled={previewBusy || commitBusy || Boolean(buildBusy) || Boolean(animationBusy) || animationActive}
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
            disabled={previewBusy || commitBusy || Boolean(buildBusy) || Boolean(animationBusy) || animationActive}
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
            disabled={previewBusy || commitBusy || Boolean(buildBusy) || Boolean(animationBusy) || animationActive}
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
          disabled={previewBusy || commitBusy || Boolean(buildBusy) || Boolean(animationBusy) || animationActive}
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
            locked={animationActive || Boolean(animationBusy)}
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
              locked={animationActive || Boolean(animationBusy)}
              onExecute={() => setBuildConfirmOpen(true)}
              onPrepare={handlePrepareBuild}
              onPreflight={handleBuildPreflight}
              preflight={buildPreflight}
            />
          ) : null}
          {artifact && isSuccessfulSceneBuild(build) ? (
            <AnimationTimelineWorkbench
              busy={animationBusy}
              error={animationError}
              nowMs={animationNowMs}
              onPreflight={handleAnimationPreflight}
              onRefresh={handleAnimationRefresh}
              onReplay={handleAnimationReplayPrepare}
              onStart={() => setAnimationConfirmMode("start")}
              onStop={handleAnimationStop}
              polling={animationPolling}
              preflight={animationPreflight}
              status={animationStatus}
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

      {animationConfirmMode && animationPreflight?.ready ? (
        <AnimationConfirmDialog
          busy={animationBusy === "start" || animationBusy === "replay"}
          mode={animationConfirmMode}
          onClose={() => setAnimationConfirmMode(null)}
          onConfirm={handleAnimationLaunch}
          preflight={animationPreflight}
          sourceRunId={animationStatus?.run_id}
        />
      ) : null}
    </div>
  );
}
