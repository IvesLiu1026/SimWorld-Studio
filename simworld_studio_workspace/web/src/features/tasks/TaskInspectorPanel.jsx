import React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { API_BASE } from "../../api/client.js";
import { deleteTaskSet, getTaskSet, listTaskSets, taskQueryKeys } from "../../api/taskApi.js";
import { Btn } from "../../components/ui/primitives.jsx";

const EMPTY_TASKSETS = [];

function withAlpha(color, alpha) {
  const value = String(color || "").trim();
  if (value.startsWith("#")) {
    const hex = value.length === 4
      ? value.slice(1).split("").map((char) => char + char).join("")
      : value.slice(1, 7);
    const int = Number.parseInt(hex, 16);
    if (Number.isFinite(int)) {
      const r = (int >> 16) & 255;
      const g = (int >> 8) & 255;
      const b = int & 255;
      return `rgba(${r},${g},${b},${alpha})`;
    }
  }
  if (value.startsWith("rgb(")) return value.replace("rgb(", "rgba(").replace(")", `,${alpha})`);
  return value || `rgba(148,163,184,${alpha})`;
}

function TaskMiniMap({ episodes, selIdx }) {
  const wrapRef = React.useRef(null);
  const canvasRef = React.useRef(null);

  React.useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;

    const draw = () => {
      const dpr = window.devicePixelRatio || 1;
      const cssW = Math.max(160, wrap.clientWidth || 280);
      const cssH = 210;
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(cssH * dpr);
      canvas.style.width = `${cssW}px`;
      canvas.style.height = `${cssH}px`;

      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const W = cssW;
      const H = cssH;
      const rootStyle = getComputedStyle(document.documentElement);
      const cssVar = (name, fallback) => rootStyle.getPropertyValue(name).trim() || fallback;
      const viewportColor = cssVar("--viewport", "black");
      const mutedColor = cssVar("--ink-3", "gray");
      const easyColor = cssVar("--green", "green");
      const mediumColor = cssVar("--orange", "orange");
      const hardColor = cssVar("--red", "red");
      const pathDefaultColor = cssVar("--blue-2", "skyblue");

      ctx.clearRect(0, 0, W, H);
      ctx.fillStyle = viewportColor;
      ctx.fillRect(0, 0, W, H);
      ctx.strokeStyle = withAlpha(mutedColor, 0.15);
      ctx.lineWidth = 1;
      ctx.strokeRect(0.5, 0.5, W - 1, H - 1);

      const all = episodes || [];
      const points = [];
      all.forEach((episode) => {
        (episode.gt_path || []).forEach((point) => points.push(point));
        if (episode.start_position) points.push(episode.start_position);
        if (episode.goal_position) points.push(episode.goal_position);
      });

      if (!points.length) {
        ctx.fillStyle = mutedColor;
        ctx.font = "12px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("No episodes", W / 2, H / 2);
        return;
      }

      const xs = points.map((point) => point[0]);
      const ys = points.map((point) => point[1]);
      const minX = Math.min(...xs);
      const maxX = Math.max(...xs);
      const minY = Math.min(...ys);
      const maxY = Math.max(...ys);
      const pad = 16;
      const spanX = maxX - minX || 1;
      const spanY = maxY - minY || 1;
      const scale = Math.min((W - 2 * pad) / spanX, (H - 2 * pad) / spanY);
      const offX = (W - spanX * scale) / 2;
      const offY = (H - spanY * scale) / 2;
      const tx = (x) => offX + (x - minX) * scale;
      const ty = (y) => H - (offY + (y - minY) * scale);

      all.forEach((episode, index) => {
        if (index === selIdx) return;
        const path = episode.gt_path || [];
        if (path.length >= 2) {
          ctx.beginPath();
          ctx.moveTo(tx(path[0][0]), ty(path[0][1]));
          for (let k = 1; k < path.length; k += 1) ctx.lineTo(tx(path[k][0]), ty(path[k][1]));
          ctx.strokeStyle = withAlpha(mutedColor, 0.16);
          ctx.lineWidth = 1;
          ctx.stroke();
        }
        if (episode.start_position) {
          ctx.fillStyle = withAlpha(easyColor, 0.3);
          ctx.beginPath();
          ctx.arc(tx(episode.start_position[0]), ty(episode.start_position[1]), 2, 0, 7);
          ctx.fill();
        }
        if (episode.goal_position) {
          ctx.fillStyle = withAlpha(hardColor, 0.3);
          ctx.beginPath();
          ctx.arc(tx(episode.goal_position[0]), ty(episode.goal_position[1]), 2, 0, 7);
          ctx.fill();
        }
      });

      const selected = all[selIdx];
      if (!selected) return;

      const difficultyColors = { easy: easyColor, medium: mediumColor, hard: hardColor };
      const pathColor = difficultyColors[selected.difficulty] || pathDefaultColor;
      const path = selected.gt_path || [];
      if (path.length >= 2) {
        ctx.beginPath();
        ctx.moveTo(tx(path[0][0]), ty(path[0][1]));
        for (let k = 1; k < path.length; k += 1) ctx.lineTo(tx(path[k][0]), ty(path[k][1]));
        ctx.strokeStyle = pathColor;
        ctx.lineWidth = 2.5;
        ctx.lineJoin = "round";
        ctx.stroke();
        ctx.fillStyle = pathDefaultColor;
        for (let k = 1; k < path.length - 1; k += 1) {
          ctx.beginPath();
          ctx.arc(tx(path[k][0]), ty(path[k][1]), 2.5, 0, 7);
          ctx.fill();
        }
      }

      if (selected.start_position) {
        ctx.fillStyle = easyColor;
        ctx.beginPath();
        ctx.arc(tx(selected.start_position[0]), ty(selected.start_position[1]), 5, 0, 7);
        ctx.fill();
        ctx.strokeStyle = viewportColor;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
      if (selected.goal_position) {
        ctx.fillStyle = hardColor;
        ctx.beginPath();
        ctx.arc(tx(selected.goal_position[0]), ty(selected.goal_position[1]), 5, 0, 7);
        ctx.fill();
        ctx.strokeStyle = viewportColor;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }
    };

    draw();
    const resizeObserver = new ResizeObserver(draw);
    resizeObserver.observe(wrap);
    return () => resizeObserver.disconnect();
  }, [episodes, selIdx]);

  return (
    <div ref={wrapRef} style={{ width: "100%" }}>
      <canvas ref={canvasRef} style={{ display: "block", borderRadius: 8 }} />
    </div>
  );
}

export default function TaskInspectorPanel() {
  const queryClient = useQueryClient();
  const [selId, setSelId] = React.useState(null);
  const [epIdx, setEpIdx] = React.useState(0);

  const taskSetsQuery = useQuery({
    queryKey: taskQueryKeys.lists(),
    queryFn: listTaskSets,
  });
  const sets = taskSetsQuery.data?.taskSets || EMPTY_TASKSETS;

  const detailQuery = useQuery({
    queryKey: taskQueryKeys.detail(selId),
    queryFn: () => getTaskSet(selId),
    enabled: !!selId,
  });
  const detail = detailQuery.data?.id ? detailQuery.data : null;
  const loading = taskSetsQuery.isLoading || detailQuery.isFetching;

  const deleteMutation = useMutation({
    mutationFn: deleteTaskSet,
    onSuccess: async () => {
      setSelId(null);
      setEpIdx(0);
      await queryClient.invalidateQueries({ queryKey: taskQueryKeys.all });
    },
  });

  React.useEffect(() => {
    const handler = (event) => {
      const nextId = event.detail && event.detail.id;
      if (nextId) setSelId(nextId);
      queryClient.invalidateQueries({ queryKey: taskQueryKeys.all });
    };
    window.addEventListener("sw-taskset-changed", handler);
    return () => window.removeEventListener("sw-taskset-changed", handler);
  }, [queryClient]);

  React.useEffect(() => {
    if (!selId && sets[0]?.id) setSelId(sets[0].id);
  }, [selId, sets]);

  React.useEffect(() => {
    setEpIdx(0);
  }, [selId]);

  const deleteSelected = () => {
    if (!selId) return;
    deleteMutation.mutate(selId);
  };
  const fmtM = (cm) => `${(cm / 100).toFixed(1)} m`;
  const episodes = detail?.episodes || [];
  const episode = episodes[epIdx] || null;
  const difficultyColor = { easy: "var(--green)", medium: "var(--orange)", hard: "var(--red)" };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "auto" }}>
      <div className="config-section">
        <div className="config-section-title">Task Set</div>
        {sets.length === 0 ? (
          <div style={{ fontSize: 12, color: "var(--ink-3)", padding: "4px 2px" }}>
            No task sets yet - generate one on the left.
          </div>
        ) : (
          <select className="config-select" style={{ width: "100%" }} value={selId || ""} onChange={(event) => setSelId(event.target.value)}>
            {sets.map((set) => (
              <option key={set.id} value={set.id}>
                {set.name} · {set.summary?.episodeCount || 0} eps
              </option>
            ))}
          </select>
        )}
      </div>

      {loading && <div style={{ fontSize: 12, color: "var(--ink-3)", padding: "8px 14px" }}>Loading...</div>}

      {detail && (
        <>
          <div className="config-section" style={{ paddingTop: 4 }}>
            <TaskMiniMap episodes={episodes} selIdx={epIdx} />
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, marginTop: 6, fontSize: 11, color: "var(--ink-3)" }}>
              <span style={{ flexShrink: 0 }}>
                <span style={{ color: "var(--green)" }}>●</span> start&nbsp;&nbsp;
                <span style={{ color: "var(--red)" }}>●</span> goal&nbsp;&nbsp;
                <span style={{ color: "var(--blue-2)" }}>-</span> path
              </span>
              <span style={{ minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }} title={detail.mapName || ""}>
                {detail.taskType} · {detail.mapName || "-"}
              </span>
            </div>
            <div style={{ marginTop: 4, fontSize: 11, color: "var(--ink-3)" }}>
              {episodes.length} eps · {fmtM(detail.summary?.minDistanceCm || 0)}-{fmtM(detail.summary?.maxDistanceCm || 0)} (avg {fmtM(detail.summary?.avgDistanceCm || 0)})
            </div>
            {detail.summary?.difficulty && (
              <div style={{ marginTop: 4, fontSize: 11, display: "flex", gap: 10 }}>
                <span style={{ color: difficultyColor.easy }}>● easy {detail.summary.difficulty.easy}</span>
                <span style={{ color: difficultyColor.medium }}>● medium {detail.summary.difficulty.medium}</span>
                <span style={{ color: difficultyColor.hard }}>● hard {detail.summary.difficulty.hard}</span>
              </div>
            )}
            {episode && (
              <div style={{ marginTop: 6, fontSize: 12, color: "var(--ink-2)", lineHeight: 1.5 }}>
                <b style={{ color: "var(--ink)" }}>{episode.episode_id}</b>
                {episode.difficulty && (
                  <>
                    {" "}· <span style={{ color: difficultyColor[episode.difficulty], fontWeight: 600 }}>{episode.difficulty}</span>
                  </>
                )}
                {" "}· {fmtM(episode.geodesic_distance_cm)} geodesic · {(episode.gt_path || []).length} waypoints · tort {episode.tortuosity ?? "-"}
                {episode.object_category && <> · target {episode.object_category}</>}
              </div>
            )}
          </div>

          <div className="config-section" style={{ minHeight: 0 }}>
            <div className="config-section-title">Episodes ({episodes.length})</div>
            <div style={{ maxHeight: 150, overflow: "auto", border: "1px solid var(--line)", borderRadius: 6 }}>
              {episodes.map((item, index) => (
                <div
                  key={item.episode_id}
                  onClick={() => setEpIdx(index)}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    padding: "4px 8px",
                    cursor: "pointer",
                    fontSize: 11,
                    fontFamily: "monospace",
                    color: index === epIdx ? "var(--ink)" : "var(--ink-2)",
                    background: index === epIdx ? "var(--bg-tertiary)" : "transparent",
                    borderLeft: `3px solid ${difficultyColor[item.difficulty] || "transparent"}`,
                    borderBottom: "1px solid var(--line)",
                  }}
                >
                  <span>{item.episode_id}</span>
                  <span>{fmtM(item.geodesic_distance_cm)}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="config-section">
            <div style={{ display: "flex", gap: 6 }}>
              <Btn
                variant="success"
                size="sm"
                style={{ flex: 1, justifyContent: "center" }}
                title="Training-ready JSON (loads via gym_env.batch_runner --episodes-file)"
                onClick={() => window.open(`${API_BASE}/tasksets/${detail.id}/download`, "_blank")}
              >
                Export (train)
              </Btn>
              <Btn
                variant="ghost"
                size="sm"
                style={{ flex: 1, justifyContent: "center" }}
                disabled={deleteMutation.isPending}
                onClick={deleteSelected}
              >
                {deleteMutation.isPending ? "Deleting..." : "Delete"}
              </Btn>
            </div>
            {deleteMutation.isError && (
              <div style={{ marginTop: 6, fontSize: 11, color: "var(--red)" }}>
                {deleteMutation.error?.message || "Failed to delete task set"}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
