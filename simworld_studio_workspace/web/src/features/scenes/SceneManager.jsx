import React, { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE } from "../../api/client.js";
import { listCheckpoints, restoreCheckpoint } from "../../api/appApi.js";

export default function SceneManager({ currentSessionId }) {
  const [tab, setTab] = useState("checkpoints");
  const [studioSessionId, setStudioSessionId] = useState(null);
  const [checkpoints, setCheckpoints] = useState([]);
  const [maps, setMaps] = useState([]);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState(null);
  const [note, setNote] = useState("");
  const containerRef = useRef(null);
  const loadedRef = useRef(false);
  const noteTimerRef = useRef(null);

  const flash = useCallback((message) => {
    if (noteTimerRef.current) clearTimeout(noteTimerRef.current);
    setNote(message);
    noteTimerRef.current = setTimeout(() => setNote(""), 2500);
  }, []);

  useEffect(() => () => {
    if (noteTimerRef.current) clearTimeout(noteTimerRef.current);
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const session = await fetch(`${API_BASE}/session`).then((response) => response.json()).catch(() => null);
      const sessionId = session?.sessionId || currentSessionId || null;
      setStudioSessionId(sessionId);
      if (sessionId) {
        setCheckpoints(await listCheckpoints(sessionId).catch(() => []));
      }
      const savedMaps = await fetch(`${API_BASE}/saved-maps`)
        .then((response) => response.json())
        .then((data) => data.maps || [])
        .catch(() => []);
      setMaps(savedMaps);
    } finally {
      setLoading(false);
    }
  }, [currentSessionId]);

  useEffect(() => {
    if (loadedRef.current) return undefined;
    const element = containerRef.current;
    if (!element) return undefined;
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting || loadedRef.current) return;
      loadedRef.current = true;
      reload();
      observer.disconnect();
    }, { threshold: 0.1 });
    observer.observe(element);
    return () => observer.disconnect();
  }, [reload]);

  const restoreCheckpointById = async (id) => {
    if (!studioSessionId) return;
    setBusyId(id);
    try {
      await restoreCheckpoint(studioSessionId, id);
      flash("Scene restored");
    } catch {
      flash("Restore failed");
    } finally {
      setBusyId(null);
    }
  };

  const loadMapVersion = async (path) => {
    setBusyId(path);
    flash("Loading map... heavy scenes may take about 30s");
    try {
      const response = await fetch(`${API_BASE}/load-map`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      if (response.ok) {
        flash("Map loaded - reconnecting viewport...");
        setTimeout(() => window.dispatchEvent(new Event("sw-reconnect-stream")), 1500);
      } else {
        flash("Load failed");
      }
    } catch {
      flash("Load failed");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div ref={containerRef} className="scene-manager">
      <div className="scene-manager-tabs">
        <SceneTabButton active={tab === "checkpoints"} onClick={() => setTab("checkpoints")}>
          Checkpoints
        </SceneTabButton>
        <SceneTabButton active={tab === "maps"} onClick={() => setTab("maps")}>
          Saved Maps
        </SceneTabButton>
        <button className="scene-refresh-btn" onClick={reload} title="Refresh">
          Refresh
        </button>
      </div>

      {note && <div className="scene-note">{note}</div>}

      <div className="scene-manager-body">
        {loading && <div className="scene-loading">Loading...</div>}

        {!loading && tab === "checkpoints" && (
          checkpoints.length === 0 ? (
            <div className="scene-empty">
              No checkpoints yet - they are created automatically as you modify the scene this session.
            </div>
          ) : (
            [...checkpoints].reverse().map((checkpoint) => (
              <div key={checkpoint.id} className="scene-card">
                {checkpoint.thumbnailUrl && (
                  <div className="scene-card-thumb">
                    <img src={checkpoint.thumbnailUrl} alt="" />
                  </div>
                )}
                <div className="scene-card-body">
                  <div className="scene-card-title">
                    {checkpoint.prompt
                      ? checkpoint.prompt.length > 70
                        ? `${checkpoint.prompt.slice(0, 70)}...`
                        : checkpoint.prompt
                      : `Turn ${checkpoint.turnIndex}`}
                  </div>
                  <div className="scene-card-row">
                    <span className="scene-card-meta">
                      {checkpoint.actorCount} obj / {new Date(checkpoint.createdAt).toLocaleTimeString()}
                    </span>
                    <button
                      className="scene-action-btn"
                      disabled={busyId === checkpoint.id}
                      onClick={() => restoreCheckpointById(checkpoint.id)}
                    >
                      {busyId === checkpoint.id ? "Restoring..." : "Restore"}
                    </button>
                  </div>
                </div>
              </div>
            ))
          )
        )}

        {!loading && tab === "maps" && (
          maps.length === 0 ? (
            <div className="scene-empty">
              No saved maps. Use Save As to persist the current scene as a reusable map.
            </div>
          ) : (
            maps.map((map) => (
              <div key={map.path} className="scene-map-card">
                <div className="scene-map-info">
                  <div className="scene-map-name">
                    {map.name}{/^empty_map$/i.test(map.name) ? " / base" : ""}
                  </div>
                  <div className="scene-map-path">{map.path}</div>
                </div>
                <button
                  className="scene-action-btn"
                  disabled={busyId === map.path}
                  onClick={() => loadMapVersion(map.path)}
                >
                  {busyId === map.path ? "Loading..." : "Load"}
                </button>
              </div>
            ))
          )
        )}
      </div>
    </div>
  );
}

function SceneTabButton({ active, children, onClick }) {
  return (
    <button className={`scene-tab-btn${active ? " active" : ""}`} onClick={onClick}>
      {children}
    </button>
  );
}
