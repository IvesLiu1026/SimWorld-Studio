import React, { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE } from "../../api/client.js";

const PAGE_SIZE = 40;
const PREVIEW_BATCH_SIZE = 1;

export default function AssetBrowser({ icons, onInsert }) {
  const [browsePath, setBrowsePath] = useState("/Game/");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [dirCache, setDirCache] = useState(() => new Map());
  const [loadingMap, setLoadingMap] = useState(null);
  const [previewMap, setPreviewMap] = useState(() => new Map());
  const containerRef = useRef(null);
  const dirCacheRef = useRef(new Map());
  const pendingRef = useRef(new Set());
  const previewMapRef = useRef(new Map());
  const pendingPreviewRef = useRef(new Set());

  dirCacheRef.current = dirCache;
  previewMapRef.current = previewMap;

  const fetchDir = useCallback((path) => {
    const entry = dirCacheRef.current.get(path);
    if (entry?.loading || entry?.loaded || pendingRef.current.has(path)) return;

    pendingRef.current.add(path);
    setDirCache((prev) => {
      const next = new Map(prev);
      next.set(path, { loading: true, loaded: false, dirs: [], assets: [], source: null });
      return next;
    });

    fetch(`${API_BASE}/asset-ls?path=${encodeURIComponent(path)}`)
      .then((response) => response.json())
      .then((data) => {
        setDirCache((prev) => {
          const next = new Map(prev);
          next.set(path, {
            loading: false,
            loaded: true,
            dirs: data.dirs || [],
            assets: data.assets || [],
            source: data.source,
          });
          return next;
        });
      })
      .catch(() => {
        setDirCache((prev) => {
          const next = new Map(prev);
          next.set(path, { loading: false, loaded: true, dirs: [], assets: [], source: "error" });
          return next;
        });
      })
      .finally(() => pendingRef.current.delete(path));
  }, []);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return undefined;
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return;
      fetchDir("/Game/");
      observer.disconnect();
    }, { threshold: 0.1 });
    observer.observe(element);
    return () => observer.disconnect();
  }, [fetchDir]);

  useEffect(() => {
    fetchDir(browsePath);
  }, [browsePath, fetchDir]);

  useEffect(() => {
    const current = dirCache.get(browsePath);
    if (current?.source !== "unavailable" && current?.source !== "error") return undefined;
    const timer = setTimeout(() => {
      pendingRef.current.delete(browsePath);
      setDirCache((prev) => {
        const next = new Map(prev);
        next.delete(browsePath);
        return next;
      });
    }, 3000);
    return () => clearTimeout(timer);
  }, [browsePath, dirCache]);

  const navigate = (path) => {
    setBrowsePath(path);
    setSearch("");
    setPage(0);
  };

  const navigateUp = () => {
    const parts = browsePath.replace(/\/$/, "").split("/").filter(Boolean);
    if (parts.length <= 1) {
      navigate("/Game/");
      return;
    }
    parts.pop();
    navigate(`/${parts.join("/")}/`);
  };

  const retry = () => {
    pendingRef.current.delete(browsePath);
    setDirCache((prev) => {
      const next = new Map(prev);
      next.delete(browsePath);
      return next;
    });
  };

  const handleDoubleClick = (asset) => {
    if (asset.type === "map") {
      setLoadingMap(asset.fullPath);
      fetch(`${API_BASE}/load-map`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: asset.fullPath }),
      })
        .then((response) => response.json())
        .then((payload) => {
          if (payload.error) console.warn("load-map:", payload.error);
        })
        .catch(() => {})
        .finally(() => setLoadingMap(null));
      return;
    }

    if (asset.type === "blueprint" || asset.type === "static_mesh") {
      onInsert?.(asset.fullPath);
    }
  };

  const current = dirCache.get(browsePath) || { loading: false, loaded: false, dirs: [], assets: [], source: null };
  const { loading, loaded, dirs, assets, source } = current;
  const query = search.toLowerCase();
  const filteredAssets = query ? assets.filter((asset) => asset.name.toLowerCase().includes(query)) : assets;
  const pageAssets = filteredAssets.slice(0, (page + 1) * PAGE_SIZE);
  const hasMore = pageAssets.length < filteredAssets.length;
  const breadcrumbs = browsePath.replace(/\/$/, "").split("/").filter(Boolean);
  const previewRequestKey = pageAssets.map((asset) => `${asset.fullPath}:${asset.previewUrl || ""}`).join("|");
  const previewStateKey = pageAssets
    .map((asset) => {
      const preview = previewMap.get(asset.fullPath);
      return `${asset.fullPath}:${preview?.status || ""}:${preview?.previewUrl || ""}`;
    })
    .join("|");

  useEffect(() => {
    if (loading || source === "unavailable" || source === "error") return undefined;

    const visiblePaths = new Set(pageAssets.map((asset) => asset.fullPath).filter(Boolean));
    for (const path of pendingPreviewRef.current) {
      if (visiblePaths.has(path)) return undefined;
    }

    const candidates = pageAssets
      .filter((asset) => {
        if (!asset.fullPath || asset.type === "map") return false;
        if (asset.previewSupported === false) return false;
        const cached = previewMapRef.current.get(asset.fullPath);
        if (asset.previewUrl || cached?.previewUrl) return false;
        if (cached?.status === "error" || cached?.status === "missing" || cached?.status === "unsupported") return false;
        return !pendingPreviewRef.current.has(asset.fullPath);
      })
      .slice(0, PREVIEW_BATCH_SIZE);

    if (!candidates.length) return undefined;

    const clearPending = () => {
      candidates.forEach((asset) => pendingPreviewRef.current.delete(asset.fullPath));
    };

    candidates.forEach((asset) => pendingPreviewRef.current.add(asset.fullPath));
    setPreviewMap((prev) => {
      const next = new Map(prev);
      candidates.forEach((asset) => {
        next.set(asset.fullPath, { ...(next.get(asset.fullPath) || {}), status: "loading" });
      });
      return next;
    });

    fetch(`${API_BASE}/asset-previews`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        limit: PREVIEW_BATCH_SIZE,
        assets: candidates.map((asset) => ({
          name: asset.name,
          fullPath: asset.fullPath,
          type: asset.type,
          category: asset.category,
        })),
      }),
    })
      .then(async (response) => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload?.error || `Preview request failed: ${response.status}`);
        return payload;
      })
      .then((payload) => {
        clearPending();
        setPreviewMap((prev) => {
          const next = new Map(prev);
          const seen = new Set();
          for (const item of payload.results || []) {
            if (!item.fullPath) continue;
            seen.add(item.fullPath);
            next.set(item.fullPath, {
              previewUrl: item.previewUrl || null,
              status: item.status || item.previewStatus || "missing",
              error: item.error || null,
            });
          }
          candidates.forEach((asset) => {
            if (!seen.has(asset.fullPath)) {
              next.set(asset.fullPath, { status: "error", previewUrl: null, error: payload.error || "Preview unavailable" });
            }
          });
          return next;
        });
      })
      .catch(() => {
        clearPending();
        setPreviewMap((prev) => {
          const next = new Map(prev);
          candidates.forEach((asset) => {
            next.set(asset.fullPath, { status: "error", previewUrl: null });
          });
          return next;
        });
      });

    return undefined;
  }, [loading, previewRequestKey, previewStateKey, source]);

  if (!loaded && !loading) {
    return <AssetBrowserState ref={containerRef} icon={icons.cube(24)} label="Assets" />;
  }

  if (loading) {
    return <AssetBrowserState ref={containerRef} icon={icons.cube(24)} label="Loading..." />;
  }

  if (source === "unavailable" || source === "error") {
    return (
      <AssetBrowserState
        ref={containerRef}
        icon={icons.cube(24)}
        label="UE not running"
        detail="Start Unreal Engine to browse assets"
        action={<button className="asset-browser-action" onClick={retry}>Retry</button>}
      />
    );
  }

  return (
    <div ref={containerRef} className="asset-browser">
      <div className="asset-browser-toolbar">
        {browsePath !== "/Game/" && (
          <button className="asset-browser-up" onClick={navigateUp}>
            Up
          </button>
        )}
        <div className="asset-browser-breadcrumbs">
          <button className="asset-browser-crumb active" onClick={() => navigate("/Game/")}>
            Game
          </button>
          {breadcrumbs.filter((segment) => segment !== "Game").map((segment, index, segments) => (
            <span key={`${segment}-${index}`} className="asset-browser-crumb-wrap">
              <span className="asset-browser-separator">/</span>
              <button
                className={`asset-browser-crumb${index === segments.length - 1 ? " current" : ""}`}
                onClick={() => {
                  const nextSegments = ["Game", ...segments.slice(0, index + 1)];
                  navigate(`/${nextSegments.join("/")}/`);
                }}
              >
                {segment}
              </button>
            </span>
          ))}
        </div>
        <input
          className="asset-browser-search"
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setPage(0);
          }}
          placeholder="Filter..."
        />
      </div>

      <div className="asset-browser-body">
        {!search && dirs.length > 0 && (
          <div className="asset-folder-list">
            {dirs.map((dir) => (
              <button key={dir.path} className="asset-folder-btn" onClick={() => navigate(dir.path)}>
                {icons.folder(13)} {dir.name}
              </button>
            ))}
          </div>
        )}

        {pageAssets.length > 0 && (
          <div className="asset-grid">
            {pageAssets.map((asset, index) => {
              const isActionable = asset.type === "map" || asset.type === "blueprint" || asset.type === "static_mesh";
              const isLoading = loadingMap === asset.fullPath;
              const preview = previewMap.get(asset.fullPath) || {};
              const previewUrl = preview.previewUrl || asset.previewUrl || null;
              const previewStatus = preview.status || asset.previewStatus || "missing";
              const previewLoading = previewStatus === "loading";
              return (
                <button
                  key={`${asset.fullPath}-${index}`}
                  className={`asset-tile${isActionable ? " actionable" : ""}${isLoading ? " loading" : ""}`}
                  disabled={!isActionable}
                  title={
                    isActionable
                      ? asset.type === "map"
                        ? "Double-click to open map in UE"
                        : "Double-click to add to context"
                      : asset.fullPath
                  }
                  onDoubleClick={() => handleDoubleClick(asset)}
                >
                  <span className={`asset-tile-preview${previewUrl ? " has-image" : ""}${previewLoading ? " loading" : ""}`}>
                    {previewUrl ? (
                      <img src={previewUrl} alt="" loading="lazy" draggable={false} />
                    ) : (
                      <span className="asset-tile-preview-fallback">
                        {isLoading || previewLoading
                          ? icons.refresh(22)
                          : asset.type === "blueprint"
                            ? icons.building(22)
                            : asset.type === "map"
                              ? icons.map(22)
                              : icons.cube(22)}
                      </span>
                    )}
                  </span>
                  <span className="asset-tile-name">
                    {asset.name.replace(/^BP_/, "").replace(/_/g, " ")}
                  </span>
                </button>
              );
            })}
          </div>
        )}

        {pageAssets.length === 0 && dirs.length === 0 && (
          <div className="asset-browser-empty">
            {search ? `No results for "${search}"` : "Empty folder"}
          </div>
        )}

        {hasMore && (
          <div className="asset-load-more-wrap">
            <button className="asset-load-more" onClick={() => setPage((value) => value + 1)}>
              Load more ({filteredAssets.length - pageAssets.length} remaining)
            </button>
          </div>
        )}
      </div>

      <div className="asset-browser-footer">
        <span>
          {!search && dirs.length > 0 && `${dirs.length} folders / `}
          {pageAssets.length}/{filteredAssets.length} assets{search && " (filtered)"}
        </span>
        <span className="asset-live-indicator">
          <span />
          Live UE
        </span>
      </div>
    </div>
  );
}

const AssetBrowserState = React.forwardRef(function AssetBrowserState({ action, detail, icon, label }, ref) {
  return (
    <div ref={ref} className="asset-browser-state">
      <div className="asset-browser-state-icon">{icon}</div>
      <div className="asset-browser-state-label">{label}</div>
      {detail && <div className="asset-browser-state-detail">{detail}</div>}
      {action}
    </div>
  );
});
