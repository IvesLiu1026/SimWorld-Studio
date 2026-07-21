import { useEffect, useMemo, useRef, useState } from "react";
import {
  commitVistaImport,
  fetchVistaImportStatus,
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
  const controllerRef = useRef(null);

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
    controllerRef.current?.abort();
    setSelection(nextSelection);
    setPreview(null);
    setPreviewRequest(null);
    setArtifact(null);
    setError(null);
    setStatusError(null);
    setConfirmOpen(false);
  }

  async function handlePreview() {
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
      setPreviewRequest(request);
      setPreview(result);
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
    setStatusBusy(true);
    setStatusError(null);
    try {
      const result = await fetchVistaImportStatus(artifactId);
      setArtifact((current) => ({
        ...current,
        ...result,
        created: current?.created ?? artifactOverride?.created,
      }));
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
            disabled={previewBusy || commitBusy}
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
            disabled={previewBusy || commitBusy}
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
            disabled={previewBusy || commitBusy}
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
          disabled={previewBusy || commitBusy}
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
    </div>
  );
}
