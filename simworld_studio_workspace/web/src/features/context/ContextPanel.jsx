import React, { useEffect, useState } from "react";
import { useScene } from "../../state/pollContext.jsx";

const MAX_DISPLAY = 150;

const EntityRow = React.memo(function EntityRow({ categoryIcons, entity, icons }) {
  const iconFn = categoryIcons[entity.category] || icons.box;
  const loc = Array.isArray(entity.location) && entity.location.length >= 3
    ? entity.location.map((value) => Math.round(value)).join(", ")
    : null;

  return (
    <div className="context-entity-row">
      <span className="context-entity-icon">{iconFn(14)}</span>
      <div className="context-entity-main">
        <div className="context-entity-line">
          <span className="context-entity-name">{entity.name}</span>
          {entity.cls && <span className="context-class-badge">{entity.cls}</span>}
        </div>
        {loc && <div className="context-entity-loc">@ ({loc})</div>}
      </div>
    </div>
  );
});

export default function ContextPanel({ categoryIcons, icons, refreshKey, sessionId }) {
  const scene = useScene();
  const state = scene.objects?.length > 0 || scene.environment?.ready ? scene : null;
  const [lastUpdated, setLastUpdated] = useState(null);

  useEffect(() => {
    setLastUpdated(new Date());
  }, [refreshKey, scene.round]);

  if (!state) {
    return (
      <div className="context-panel">
        <div className="context-empty">
          {sessionId ? "No scene data yet - complete an operation to populate." : "Start a build session to see scene context."}
        </div>
      </div>
    );
  }

  const byCategory = {};
  let shown = 0;
  for (const object of scene.objects || []) {
    if (shown >= MAX_DISPLAY) break;
    (byCategory[object.category] = byCategory[object.category] || []).push(object);
    shown += 1;
  }

  const totalObjects = (scene.objects || []).length;
  const truncated = totalObjects > MAX_DISPLAY;

  return (
    <div className="context-panel">
      <div className="context-header">
        <div className="context-title-row">
          <span className="context-title">Scene Context</span>
          <span className={`context-env-badge${state.environment?.ready ? " ready" : " warn"}`}>
            {state.environment?.ready ? "env ready" : "env not initialized"}
          </span>
          <span className="context-round">round {state.round ?? 0}</span>
        </div>
        {lastUpdated && <span className="context-updated">updated {lastUpdated.toLocaleTimeString()}</span>}
      </div>

      <div className="context-body">
        <section className="context-section">
          <div className="context-section-title">
            {icons.robot(13)} Agents <span className="context-count">{(scene.agents || []).length}</span>
          </div>
          {(scene.agents || []).length === 0 ? (
            <div className="context-muted">No agents in scene</div>
          ) : (
            (scene.agents || []).map((agent) => (
              <EntityRow key={agent.name} categoryIcons={categoryIcons} entity={agent} icons={icons} />
            ))
          )}
        </section>

        <section className="context-section context-section-spaced">
          <div className="context-section-title">
            {icons.box(13)} Objects <span className="context-count">{totalObjects}</span>
            {truncated && <span className="context-truncated-label">(showing {MAX_DISPLAY})</span>}
          </div>
          {totalObjects === 0 ? (
            <div className="context-muted">No objects in scene</div>
          ) : (
            Object.entries(byCategory).map(([category, items]) => (
              <div key={category} className="context-category">
                <div className="context-category-title">
                  {(categoryIcons[category] || icons.box)(11)} {category}s ({items.length})
                </div>
                {items.map((object) => (
                  <EntityRow key={object.name} categoryIcons={categoryIcons} entity={object} icons={icons} />
                ))}
              </div>
            ))
          )}
          {truncated && (
            <div className="context-truncated">
              {totalObjects - MAX_DISPLAY} more objects not shown. Use the coding agent to query specific actors.
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
