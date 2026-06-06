import React, { useCallback, useEffect, useState } from "react";
import { API_BASE } from "../../api/client.js";

function SavedMapsGallery({ icons, onOpenScene }) {
  const [maps, setMaps] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);

  const reload = useCallback(() => {
    setLoading(true);
    fetch(`${API_BASE}/saved-maps`)
      .then((response) => response.json())
      .then((data) => setMaps(data.maps || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return (
    <div className="saved-maps-gallery">
      <div className="saved-maps-header">
        <span className="saved-maps-title">Saved scenes / {maps.length}</span>
        <button className="saved-maps-refresh" onClick={reload}>
          Refresh
        </button>
      </div>
      {loading ? (
        <div className="saved-maps-loading">Loading...</div>
      ) : maps.length === 0 ? (
        <div className="saved-maps-empty">
          No saved scenes yet. In the Scene panel, build a scene and click <b>Save As</b>.
        </div>
      ) : (
        <div className="saved-maps-grid">
          {maps.map((map) => {
            const isBase = /^empty_map$/i.test(map.name);
            return (
              <div key={map.path} className="saved-map-card">
                <div className="saved-map-thumb">{icons.frame ? icons.frame(34) : icons.map(34)}</div>
                <div className="saved-map-body">
                  <div className="saved-map-name" title={map.path}>
                    {map.name}
                    {isBase ? " / base" : ""}
                  </div>
                  <div className="saved-map-actions">
                    <button
                      className="saved-map-open"
                      onClick={async () => {
                        setBusy(map.path);
                        try {
                          await onOpenScene?.(map.path);
                        } finally {
                          setBusy(null);
                        }
                      }}
                      disabled={busy === map.path}
                      title="Open this scene in the live viewport"
                    >
                      {busy === map.path ? "Opening..." : "Open"}
                    </button>
                    <a
                      className="saved-map-download"
                      href={`${API_BASE}/saved-maps/${encodeURIComponent(map.name)}/download`}
                      download={`${map.name}.umap`}
                      title="Download .umap"
                      onClick={(event) => event.stopPropagation()}
                    >
                      Download
                    </a>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function ResultsPage({ icons, LeaderboardPage, onOpenScene }) {
  const [tab, setTab] = useState("gallery");
  const tabs = [
    ["gallery", "Scenes", icons.frame],
    ["leaderboard", "Leaderboard", icons.trophy],
  ];

  return (
    <div className="studio-page">
      <div className="studio-page-tabs">
        <span className="studio-page-title">Results</span>
        {tabs.map(([id, label, icon]) => (
          <button key={id} className={`sw-tab-btn${tab === id ? " active" : ""}`} onClick={() => setTab(id)}>
            <span className="studio-tab-label">
              {icon(13)} {label}
            </span>
          </button>
        ))}
      </div>
      <div className="studio-page-body">
        {tab === "gallery" && <SavedMapsGallery icons={icons} onOpenScene={onOpenScene} />}
        {tab === "leaderboard" && <LeaderboardPage />}
      </div>
    </div>
  );
}
