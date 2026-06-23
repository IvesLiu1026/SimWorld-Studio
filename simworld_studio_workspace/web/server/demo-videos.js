"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const WORKSPACE_ROOT = path.resolve(__dirname, "../..");
const DEFAULT_DEMO_ROOT = path.join(WORKSPACE_ROOT, "results", "demo_videos");
const VIDEO_EXTS = new Set([".mp4", ".webm", ".mov", ".m4v"]);

function uniq(values) {
  return [...new Set(values.filter(Boolean).map((value) => path.resolve(String(value))))];
}

function safeName(value, fallback = "demo") {
  const cleaned = String(value || "")
    .trim()
    .replace(/[^A-Za-z0-9_\-]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return cleaned || fallback;
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf-8"));
  } catch {
    return null;
  }
}

function inferMime(file) {
  const ext = path.extname(file).toLowerCase();
  if (ext === ".webm") return "video/webm";
  if (ext === ".mov") return "video/quicktime";
  return "video/mp4";
}

function findMetadataFiles(root, maxDepth = 4) {
  const out = [];
  const base = path.resolve(root);
  if (!fs.existsSync(base)) return out;

  function visit(dir, depth) {
    if (depth > maxDepth) return;
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isFile() && entry.name === "metadata.json") {
        out.push(full);
      } else if (entry.isDirectory()) {
        visit(full, depth + 1);
      }
    }
  }

  visit(base, 0);
  return out;
}

function findVideoFiles(root, maxDepth = 4) {
  const out = [];
  const base = path.resolve(root);
  if (!fs.existsSync(base)) return out;

  function visit(dir, depth) {
    if (depth > maxDepth) return;
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isFile() && VIDEO_EXTS.has(path.extname(entry.name).toLowerCase())) {
        out.push(full);
      } else if (entry.isDirectory()) {
        visit(full, depth + 1);
      }
    }
  }

  visit(base, 0);
  return out;
}

function firstVideoFile(dir) {
  try {
    return fs
      .readdirSync(dir)
      .filter((name) => VIDEO_EXTS.has(path.extname(name).toLowerCase()))
      .sort()[0] || null;
  } catch {
    return null;
  }
}

function normalizeRecord(metaPath, root) {
  const meta = readJson(metaPath);
  if (!meta || typeof meta !== "object") return null;
  const dir = path.dirname(metaPath);
  const fileName = meta.fileName || meta.videoFile || firstVideoFile(dir);
  if (!fileName || path.basename(fileName) !== fileName) return null;
  const filePath = path.join(dir, fileName);
  if (!fs.existsSync(filePath)) return null;
  const ext = path.extname(filePath).toLowerCase();
  if (!VIDEO_EXTS.has(ext)) return null;

  const stat = fs.statSync(filePath);
  const id = safeName(meta.id || path.basename(dir), `video_${stat.mtimeMs}`);
  const mapName = safeName(meta.mapName || meta.map_name || "unspecified_map", "unspecified_map");
  const viewName = String(meta.viewName || meta.view_name || meta.cameraMode || "demo");
  const cameraMode = String(meta.cameraMode || meta.camera_mode || "unknown");

  return {
    id,
    mapName,
    mapPath: meta.mapPath || meta.map_path || (mapName ? `/Game/SavedScenes/${mapName}` : ""),
    viewName,
    cameraMode,
    fileName,
    mimeType: meta.mimeType || inferMime(filePath),
    sizeBytes: stat.size,
    createdAt: meta.createdAt || meta.created_at || new Date(stat.mtimeMs).toISOString(),
    durationSec: meta.durationSec ?? meta.duration_sec ?? null,
    fps: meta.fps ?? null,
    resolution: Array.isArray(meta.resolution) ? meta.resolution : null,
    frameCount: meta.frameCount ?? meta.frame_count ?? null,
    notes: meta.notes || "",
    filePath,
    metaPath,
    root: path.resolve(root),
  };
}

function normalizeLooseVideo(filePath, root) {
  if (!fs.existsSync(filePath)) return null;
  const ext = path.extname(filePath).toLowerCase();
  if (!VIDEO_EXTS.has(ext)) return null;
  const stat = fs.statSync(filePath);
  const parent = path.basename(path.dirname(filePath));
  const base = path.basename(filePath, ext);
  const id = safeName(`${parent}_${base}_${Math.round(stat.mtimeMs)}`);
  return {
    id,
    mapName: "unsaved",
    mapPath: "",
    viewName: base || "Demo video",
    cameraMode: "unlabeled",
    fileName: path.basename(filePath),
    mimeType: inferMime(filePath),
    sizeBytes: stat.size,
    createdAt: new Date(stat.mtimeMs).toISOString(),
    durationSec: null,
    fps: null,
    resolution: null,
    frameCount: null,
    notes: "No metadata.json found for this video.",
    filePath: path.resolve(filePath),
    metaPath: "",
    root: path.resolve(root),
  };
}

function listDemoVideos({ roots = [], mapName = null, mapPath = null } = {}) {
  const records = [];
  const seen = new Set();
  const seenFiles = new Set();
  for (const root of uniq([DEFAULT_DEMO_ROOT, ...roots])) {
    for (const metaPath of findMetadataFiles(root)) {
      const rec = normalizeRecord(metaPath, root);
      if (!rec || seen.has(rec.id)) continue;
      seen.add(rec.id);
      seenFiles.add(path.resolve(rec.filePath));
      records.push(rec);
    }
    for (const filePath of findVideoFiles(root)) {
      const resolved = path.resolve(filePath);
      if (seenFiles.has(resolved)) continue;
      const rec = normalizeLooseVideo(resolved, root);
      if (!rec || seen.has(rec.id)) continue;
      seen.add(rec.id);
      seenFiles.add(resolved);
      records.push(rec);
    }
  }

  const wantedName = mapName ? safeName(mapName) : null;
  const wantedPath = mapPath ? String(mapPath) : null;
  return records
    .filter((rec) => {
      if (wantedPath && rec.mapPath === wantedPath) return true;
      if (wantedName && rec.mapName === wantedName) return true;
      return !wantedName && !wantedPath;
    })
    .sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
}

function publicRecord(rec) {
  const { filePath, root, ...rest } = rec;
  return {
    ...rest,
    streamUrl: `/api/demo-videos/${encodeURIComponent(rec.id)}/stream`,
    downloadUrl: `/api/demo-videos/${encodeURIComponent(rec.id)}/download`,
  };
}

function deleteVideoById(id, roots) {
  const rec = findVideoById(id, roots);
  if (!rec) return false;
  const dir = path.dirname(rec.filePath);
  const canRemoveDir = rec.metaPath && path.dirname(rec.metaPath) === dir;
  if (canRemoveDir) {
    fs.rmSync(dir, { recursive: true, force: true });
    return true;
  }
  fs.rmSync(rec.filePath, { force: true });
  return true;
}

function attachDemoVideosToMaps(maps, roots) {
  const videos = listDemoVideos({ roots });
  return maps.map((map) => {
    const attached = videos.filter((video) => video.mapName === map.name || video.mapPath === map.path);
    return { ...map, videos: attached.map(publicRecord) };
  });
}

function findVideoById(id, roots) {
  const wanted = safeName(id, "");
  if (!wanted) return null;
  return listDemoVideos({ roots }).find((rec) => rec.id === wanted) || null;
}

function writeMetadata(dir, record) {
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "metadata.json"), `${JSON.stringify(record, null, 2)}\n`, "utf-8");
}

function registerExternalVideo(body, roots = []) {
  const sourcePath = path.resolve(String(body.sourcePath || ""));
  const allowedRoots = uniq([WORKSPACE_ROOT, os.tmpdir(), ...roots]);
  if (!sourcePath || !fs.existsSync(sourcePath)) {
    const err = new Error("sourcePath not found");
    err.status = 404;
    throw err;
  }
  if (!allowedRoots.some((root) => sourcePath === root || sourcePath.startsWith(root + path.sep))) {
    const err = new Error("sourcePath must be under the SimWorld workspace or system temp directory");
    err.status = 400;
    throw err;
  }
  const ext = path.extname(sourcePath).toLowerCase();
  if (!VIDEO_EXTS.has(ext)) {
    const err = new Error("sourcePath must be an mp4, webm, mov, or m4v file");
    err.status = 400;
    throw err;
  }

  const mapName = safeName(body.mapName, "unspecified_map");
  const id = safeName(body.id || `${mapName}_${safeName(body.viewName, "demo")}_${Date.now().toString(36)}`);
  const dir = path.join(DEFAULT_DEMO_ROOT, mapName, id);
  const fileName = "demo" + ext;
  fs.mkdirSync(dir, { recursive: true });
  fs.copyFileSync(sourcePath, path.join(dir, fileName));

  const record = {
    id,
    mapName,
    mapPath: body.mapPath || `/Game/SavedScenes/${mapName}`,
    viewName: body.viewName || "demo",
    cameraMode: body.cameraMode || "external",
    fileName,
    mimeType: inferMime(fileName),
    createdAt: new Date().toISOString(),
    durationSec: body.durationSec ?? null,
    fps: body.fps ?? null,
    resolution: Array.isArray(body.resolution) ? body.resolution : null,
    frameCount: body.frameCount ?? null,
    notes: body.notes || "",
  };
  writeMetadata(dir, record);
  return publicRecord(normalizeRecord(path.join(dir, "metadata.json"), DEFAULT_DEMO_ROOT));
}

function sendVideo(req, res, rec, download = false) {
  if (download) {
    const name = `${safeName(rec.mapName)}_${safeName(rec.viewName)}${path.extname(rec.fileName)}`;
    return res.download(rec.filePath, name);
  }

  const stat = fs.statSync(rec.filePath);
  const range = req.headers.range;
  res.setHeader("Accept-Ranges", "bytes");
  res.setHeader("Content-Type", rec.mimeType || inferMime(rec.filePath));

  if (!range) {
    res.setHeader("Content-Length", stat.size);
    return fs.createReadStream(rec.filePath).pipe(res);
  }

  const match = range.match(/bytes=(\d*)-(\d*)/);
  if (!match) {
    res.status(416).end();
    return;
  }
  const start = match[1] ? parseInt(match[1], 10) : 0;
  const end = match[2] ? parseInt(match[2], 10) : stat.size - 1;
  if (start >= stat.size || end >= stat.size || start > end) {
    res.status(416);
    res.setHeader("Content-Range", `bytes */${stat.size}`);
    res.end();
    return;
  }
  res.status(206);
  res.setHeader("Content-Range", `bytes ${start}-${end}/${stat.size}`);
  res.setHeader("Content-Length", end - start + 1);
  fs.createReadStream(rec.filePath, { start, end }).pipe(res);
}

function registerDemoVideoRoutes(app, { getRoots } = {}) {
  async function roots() {
    const extra = typeof getRoots === "function" ? await getRoots() : [];
    return uniq([DEFAULT_DEMO_ROOT, ...(Array.isArray(extra) ? extra : [])]);
  }

  app.get("/api/demo-videos", async (req, res) => {
    try {
      const allRoots = await roots();
      const videos = listDemoVideos({
        roots: allRoots,
        mapName: req.query.mapName || null,
        mapPath: req.query.mapPath || null,
      }).map(publicRecord);
      res.json({ videos });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });

  app.post("/api/demo-videos/register", async (req, res) => {
    try {
      const video = registerExternalVideo(req.body || {}, await roots());
      res.status(201).json(video);
    } catch (err) {
      res.status(err.status || 500).json({ error: err.message });
    }
  });

  app.get("/api/demo-videos/:id/stream", async (req, res) => {
    const rec = findVideoById(req.params.id, await roots());
    if (!rec) return res.status(404).json({ error: "demo video not found" });
    return sendVideo(req, res, rec, false);
  });

  app.get("/api/demo-videos/:id/download", async (req, res) => {
    const rec = findVideoById(req.params.id, await roots());
    if (!rec) return res.status(404).json({ error: "demo video not found" });
    return sendVideo(req, res, rec, true);
  });

  app.delete("/api/demo-videos/:id", async (req, res) => {
    try {
      const ok = deleteVideoById(req.params.id, await roots());
      if (!ok) return res.status(404).json({ error: "demo video not found" });
      res.json({ ok: true });
    } catch (err) {
      res.status(500).json({ error: err.message });
    }
  });
}

module.exports = {
  DEFAULT_DEMO_ROOT,
  attachDemoVideosToMaps,
  deleteVideoById,
  listDemoVideos,
  publicRecord,
  registerDemoVideoRoutes,
  safeName,
};
