import React, { useCallback, useEffect, useState } from "react";
import { API_BASE } from "../../api/client.js";
import { Badge, ModalHeader, ModalOverlay } from "../../components/ui/primitives.jsx";
import LeaderboardPage from "./LeaderboardPage.jsx";

function formatBytes(bytes) {
  if (!bytes) return "";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDuration(seconds) {
  if (!seconds) return "";
  const rounded = Math.round(seconds);
  return `${rounded}s`;
}

function videoMeta(video) {
  return [video.cameraMode, formatDuration(video.durationSec), formatBytes(video.sizeBytes)].filter(Boolean).join(" / ");
}

function formatDate(value) {
  if (!value) return "";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return "";
  }
}

function DemoVideoModal({ icons, video, onClose }) {
  if (!video) return null;
  return (
    <ModalOverlay onClose={onClose} maxWidth={980} maxHeight="90vh">
      <ModalHeader
        title={video.viewName || "Demo video"}
        subtitle={`${video.mapName || "Map"}${video.cameraMode ? ` / ${video.cameraMode}` : ""}`}
        onClose={onClose}
        closeIcon={icons.close(14)}
      />
      <div className="demo-video-modal-body">
        <video className="demo-video-player" src={video.streamUrl} controls autoPlay />
        <div className="demo-video-modal-footer">
          <div className="demo-video-modal-meta">
            <Badge variant="blue">{video.cameraMode || "demo"}</Badge>
            <span>{video.mapName || "unsaved"}</span>
            {video.resolution && <span>{video.resolution.join("x")}</span>}
            {video.fps && <span>{video.fps} fps</span>}
            {video.sizeBytes && <span>{formatBytes(video.sizeBytes)}</span>}
          </div>
          <a className="saved-map-download demo-video-download" href={video.downloadUrl} download>
            Download video
          </a>
        </div>
        {(video.notes || video.createdAt) && (
          <div className="demo-video-modal-notes">
            {video.createdAt && <span>Created {formatDate(video.createdAt)}</span>}
            {video.notes && <span>{video.notes}</span>}
          </div>
        )}
      </div>
    </ModalOverlay>
  );
}

function DemoVideosPanel({ icons }) {
  const [videos, setVideos] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedVideo, setSelectedVideo] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const reload = useCallback(() => {
    setLoading(true);
    fetch(`${API_BASE}/demo-videos`)
      .then((response) => response.json())
      .then((data) => setVideos(Array.isArray(data.videos) ? data.videos : []))
      .catch(() => setVideos([]))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const deleteVideo = async (video) => {
    if (!window.confirm(`Delete demo video "${video.viewName || video.id}"?`)) return;
    setBusyId(video.id);
    try {
      const response = await fetch(`${API_BASE}/demo-videos/${encodeURIComponent(video.id)}`, { method: "DELETE" });
      if (response.ok) reload();
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="demo-videos-page">
      <DemoVideoModal icons={icons} video={selectedVideo} onClose={() => setSelectedVideo(null)} />
      <div className="saved-maps-header">
        <span className="saved-maps-title">Demo videos / {videos.length}</span>
        <button className="saved-maps-refresh" onClick={reload}>Refresh</button>
      </div>
      {loading ? (
        <div className="saved-maps-loading">Loading...</div>
      ) : videos.length === 0 ? (
        <div className="saved-maps-empty">
          No demo videos found yet. Recorded videos will appear here even when they are not attached to a saved map.
        </div>
      ) : (
        <div className="demo-videos-grid">
          {videos.map((video) => (
            <div className="demo-video-card" key={video.id}>
              <button className="demo-video-thumb" onClick={() => setSelectedVideo(video)} title="View demo video">
                {icons.video(34)}
              </button>
              <div className="demo-video-card-body">
                <div className="demo-video-title" title={video.viewName || video.id}>
                  {video.viewName || video.id}
                </div>
                <div className="demo-video-meta-line">{video.mapName || "unsaved"} / {videoMeta(video) || "unlabeled"}</div>
                <div className="demo-video-meta-line">{formatDate(video.createdAt)}</div>
                {video.notes && <div className="demo-video-notes">{video.notes}</div>}
                <div className="demo-video-card-actions">
                  <button className="saved-map-open" onClick={() => setSelectedVideo(video)}>
                    View
                  </button>
                  <a className="saved-map-download" href={video.downloadUrl} download>
                    Download
                  </a>
                  <button
                    className="demo-video-delete"
                    onClick={() => deleteVideo(video)}
                    disabled={busyId === video.id}
                    title="Delete demo video"
                  >
                    {busyId === video.id ? "Deleting..." : "Delete"}
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SavedMapsGallery({ icons, onOpenScene }) {
  const [maps, setMaps] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(null);
  const [selectedVideo, setSelectedVideo] = useState(null);

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
      <DemoVideoModal icons={icons} video={selectedVideo} onClose={() => setSelectedVideo(null)} />
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
            const videos = Array.isArray(map.videos) ? map.videos : [];
            return (
              <div key={map.path} className="saved-map-card">
                <div className="saved-map-thumb">{icons.frame ? icons.frame(34) : icons.map(34)}</div>
                <div className="saved-map-body">
                  <div className="saved-map-name" title={map.path}>
                    {map.name}
                    {isBase ? " / base" : ""}
                  </div>
                  <div className="saved-map-subline">
                    {videos.length > 0 ? `${videos.length} demo video${videos.length === 1 ? "" : "s"}` : "No demo videos"}
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
                  {videos.length > 0 && (
                    <div className="saved-map-videos">
                      {videos.map((video) => (
                        <div className="saved-map-video-row" key={video.id}>
                          <button
                            className="saved-map-video-play"
                            onClick={() => setSelectedVideo(video)}
                            title="View demo video"
                          >
                            {icons.video(14)}
                            <span className="saved-map-video-text">
                              <span>{video.viewName || "Demo video"}</span>
                              <small>{videoMeta(video)}</small>
                            </span>
                          </button>
                          <a
                            className="saved-map-video-download"
                            href={video.downloadUrl}
                            download
                            title="Download demo video"
                            onClick={(event) => event.stopPropagation()}
                          >
                            Download
                          </a>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function ResultsPage({ icons, onOpenScene }) {
  const [tab, setTab] = useState("gallery");
  const tabs = [
    ["gallery", "Scenes", icons.frame],
    ["videos", "Videos", icons.video],
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
        {tab === "videos" && <DemoVideosPanel icons={icons} />}
        {tab === "leaderboard" && <LeaderboardPage icons={icons} />}
      </div>
    </div>
  );
}
