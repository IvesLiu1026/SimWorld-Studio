import React, { useEffect, useRef, useState } from "react";
import { API_BASE } from "../../api/client.js";
import { trainingStore, useTraining } from "./trainingStore.js";
import { OK, FAIL, PEND, frameUrl, useJson } from "./datahub.js";

// Agent-training monitor — DATAHUB-DRIVEN. Polls /api/training/datahub/latest so opening the
// page always shows the latest run. Hierarchy: RUN → EPOCH → TASK → STEP. An "epoch" is one
// pass over all tasks (memory carries across epochs), so success-rate-per-epoch is the LEARNING
// CURVE (line chart). A "task" is one pointnav episode. Each step shows the agent's observation
// frame + the exact LLM input/output. Shared colors / frameUrl / polling live in ./datahub.js.

// SR-per-epoch LINE chart — the learning curve. Hover a point for that epoch's detail.
function LearningCurve({ curve, epochsTotal }) {
  const [hover, setHover] = useState(null);
  const W = 320, H = 96, padL = 26, padR = 10, padT = 10, padB = 18;
  const n = Math.max(epochsTotal || 0, curve.length, 1);
  const xOf = (e) => padL + (n <= 1 ? 0 : (e / (n - 1)) * (W - padL - padR));
  const yOf = (sr) => padT + (1 - (sr || 0)) * (H - padT - padB);
  const pts = curve.filter((c) => c.SR != null);
  const poly = pts.map((c) => `${xOf(c.epoch).toFixed(1)},${yOf(c.SR).toFixed(1)}`).join(" ");
  // Closed path for the gradient area fill under the line.
  const area = pts.length
    ? `M${xOf(pts[0].epoch).toFixed(1)},${yOf(0).toFixed(1)} `
      + pts.map((c) => `L${xOf(c.epoch).toFixed(1)},${yOf(c.SR).toFixed(1)}`).join(" ")
      + ` L${xOf(pts[pts.length - 1].epoch).toFixed(1)},${yOf(0).toFixed(1)} Z`
    : "";
  const first = pts[0]?.SR, last = pts[pts.length - 1]?.SR;
  const lift = (first != null && last != null) ? Math.round((last - first) * 100) : null;
  return (
    <div style={{ padding: "10px 12px", borderBottom: "1px solid var(--line)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
        <span style={{ fontSize: 10, color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: 0.6, fontWeight: 700 }}>
          learning curve · success rate / epoch
        </span>
        {lift != null && pts.length > 1 && (
          <span style={{ fontSize: 12, fontWeight: 800, color: lift >= 0 ? OK : FAIL,
            background: lift >= 0 ? "rgba(61,220,132,0.12)" : "rgba(224,103,58,0.12)", padding: "1px 7px", borderRadius: 10 }}>
            {lift >= 0 ? "▲ +" : "▼ "}{lift} pts vs epoch 0
          </span>
        )}
      </div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} style={{ display: "block", overflow: "visible" }} preserveAspectRatio="none">
        <defs>
          <linearGradient id="lc-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--violet)" stopOpacity="0.30" />
            <stop offset="100%" stopColor="var(--violet)" stopOpacity="0.02" />
          </linearGradient>
        </defs>
        {[0, 0.5, 1].map((g) => (
          <g key={g}>
            <line x1={padL} y1={yOf(g)} x2={W - padR} y2={yOf(g)} stroke="var(--line)"
              strokeWidth="0.5" strokeDasharray={g === 0 ? "0" : "2 3"} />
            <text x={2} y={yOf(g) + 3} fontSize="8" fill="var(--ink-3)">{Math.round(g * 100)}</text>
          </g>
        ))}
        {/* x-axis epoch ticks */}
        {Array.from({ length: n }).map((_, e) => (
          <text key={`x${e}`} x={xOf(e)} y={H - 4} fontSize="7.5" fill="var(--ink-3)" textAnchor="middle">{e}</text>
        ))}
        {area && <path d={area} fill="url(#lc-area)" />}
        {pts.length > 1 && <polyline points={poly} fill="none" stroke="var(--violet)" strokeWidth="2"
          strokeLinejoin="round" strokeLinecap="round" />}
        {pts.map((c) => (
          <circle key={c.epoch} cx={xOf(c.epoch)} cy={yOf(c.SR)} r={hover === c.epoch ? 4.5 : 3}
            fill={c.SR >= 0.5 ? OK : "var(--violet)"} stroke="var(--bg)" strokeWidth="1.2"
            onMouseEnter={() => setHover(c.epoch)} onMouseLeave={() => setHover(null)} style={{ cursor: "pointer" }} />
        ))}
        {curve.filter((c) => c.SR == null).map((c) => (
          <circle key={`p${c.epoch}`} cx={xOf(c.epoch)} cy={yOf(0)} r="2" fill={PEND} opacity="0.4" />
        ))}
      </svg>
      {hover != null && (() => {
        const c = curve.find((x) => x.epoch === hover);
        return c ? (
          <div style={{ fontSize: 10, color: "var(--ink-2)", marginTop: 2 }}>
            epoch {c.epoch}: <b>{c.SR != null ? `${Math.round(c.SR * 100)}%` : "…"}</b>
            {" "}({c.success}/{c.tasksTotal} tasks){c.lessons != null ? ` · ${c.lessons} feedback records` : ""}
          </div>
        ) : null;
      })()}
    </div>
  );
}

function StepRow({ runId, task, st, open, onToggle }) {
  const act = st.action || "?";
  const dist = st.distanceCm != null ? `${(st.distanceCm / 100).toFixed(1)}m` : "—";
  const col = act.includes("FORWARD") ? "var(--violet)" : act === "STOP" ? OK : "var(--ink-2)";
  return (
    <div style={{ borderTop: "1px solid var(--line)" }}>
      <div onClick={onToggle} style={{ display: "flex", gap: 8, alignItems: "center", padding: "3px 8px", cursor: "pointer", fontSize: 11 }}>
        <span style={{ width: 26, color: "var(--ink-3)" }}>t{st.step}</span>
        <span style={{ fontWeight: 700, color: col, width: 96 }}>{act}</span>
        <span style={{ color: "var(--ink-3)" }}>dist {dist}{st.bearingDeg != null ? ` · brg ${Math.round(st.bearingDeg)}°` : ""}</span>
        {st.stuck > 0 && <span style={{ color: FAIL, fontSize: 10 }}>stuck {st.stuck}</span>}
      </div>
      {open && (
        <div style={{ display: "flex", gap: 10, padding: "4px 8px 8px 34px" }}>
          <div style={{ flex: "0 0 150px" }}>
            <div className="training-camera-frame" style={{ width: 150 }}>
              <img src={frameUrl(runId, task.runName, st.step)} alt="obs" style={{ width: "100%", display: "block", borderRadius: 4 }}
                onError={(e) => { e.currentTarget.style.opacity = 0.2; }} />
            </div>
            <div style={{ fontSize: 9, color: "var(--ink-3)", marginTop: 2 }}>First-person observation</div>
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontSize: 9, color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: 0.4 }}>Controller input</div>
            <div style={{ fontSize: 10, color: "var(--ink-2)", fontFamily: "monospace", whiteSpace: "pre-wrap", maxHeight: 64, overflow: "auto", margin: "2px 0 6px" }}>
              {(st.input && st.input.prompt) || "(image + goal/bearing/distance)"}
            </div>
            <div style={{ fontSize: 9, color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: 0.4 }}>Controller output</div>
            <div style={{ fontSize: 11, color: "var(--ink)", marginTop: 2 }}>{(st.output && st.output.text) || act}</div>
          </div>
        </div>
      )}
    </div>
  );
}

function TaskRow({ runId, task, open, onToggle }) {
  const [openStep, setOpenStep] = useState(null);
  const icon = !task.done ? "•" : (task.SR >= 1 ? "✓" : "✗");
  const col = !task.done ? PEND : (task.SR >= 1 ? OK : FAIL);
  const steps = task.steps || [];
  return (
    <div style={{ borderTop: "1px solid var(--line)" }}>
      <div onClick={onToggle} style={{ display: "flex", gap: 8, alignItems: "center", padding: "5px 10px", cursor: "pointer" }}>
        <span style={{ color: col, fontWeight: 800, width: 14 }}>{icon}</span>
        <span style={{ fontSize: 12, fontWeight: 600 }}>task {task.idx}</span>
        <span style={{ fontSize: 11, color: "var(--ink-3)" }}>{steps.length} steps</span>
        {task.endedReason && <span style={{ fontSize: 10, color: task.endedReason === "success" ? OK : "var(--ink-3)" }}>{task.endedReason}</span>}
        {task.lesson && <span title={task.lesson} style={{ fontSize: 10, color: "var(--violet)" }}>📝</span>}
      </div>
      {open && (
        <div style={{ background: "rgba(0,0,0,0.12)" }}>
          {task.lesson && <div style={{ fontSize: 10, color: "var(--violet)", padding: "2px 10px 4px 32px", fontStyle: "italic" }}>feedback: {task.lesson}</div>}
          {steps.map((st) => (
            <StepRow key={st.step} runId={runId} task={task} st={st} open={openStep === st.step}
              onToggle={() => setOpenStep(openStep === st.step ? null : st.step)} />
          ))}
          {!steps.length && <div style={{ fontSize: 10, color: "var(--ink-3)", padding: "4px 32px" }}>collecting steps…</div>}
        </div>
      )}
    </div>
  );
}

function EpochRow({ runId, ep, open, onToggle, defaultOpen }) {
  const [openTask, setOpenTask] = useState(null);
  const tasks = ep.tasks || [];
  const srPct = ep.SR != null ? Math.round(ep.SR * 100) : null;
  return (
    <div style={{ borderTop: "2px solid var(--line)" }}>
      <div onClick={onToggle} style={{ display: "flex", gap: 8, alignItems: "center", padding: "7px 10px", cursor: "pointer", background: "var(--bg-2,rgba(0,0,0,0.18))" }}>
        <span style={{ fontSize: 12, fontWeight: 800, color: "var(--violet)" }}>epoch {ep.epoch}</span>
        <span style={{ fontSize: 12, fontWeight: 800, color: srPct != null ? (srPct >= 50 ? OK : "var(--ink)") : PEND }}>
          {srPct != null ? `${srPct}%` : "…"}
        </span>
        <span style={{ fontSize: 10, color: "var(--ink-3)" }}>{ep.successCount}/{ep.tasksTotal} tasks</span>
        {ep.lessons != null && <span style={{ fontSize: 10, color: "var(--violet)" }}>{ep.lessons} feedback records</span>}
        {/* color bar: one cell per task (green=success, red=fail, grey=pending) */}
        <div style={{ display: "flex", gap: 1, flex: 1, marginLeft: 6, height: 12 }}>
          {Array.from({ length: ep.tasksTotal || tasks.length }).map((_, i) => {
            const t = tasks.find((x) => x.idx === i);
            const c = !t || !t.done ? PEND : (t.SR >= 1 ? OK : FAIL);
            return <div key={i} title={`task ${i}`} style={{ flex: 1, background: c, borderRadius: 1, opacity: !t || !t.done ? 0.25 : 1 }} />;
          })}
        </div>
      </div>
      {open && (
        <div>
          {tasks.map((t) => (
            <TaskRow key={t.idx} runId={runId} task={t} open={openTask === t.idx}
              onToggle={() => setOpenTask(openTask === t.idx ? null : t.idx)} />
          ))}
          {!tasks.length && <div style={{ fontSize: 10, color: "var(--ink-3)", padding: "6px 32px" }}>epoch booting…</div>}
        </div>
      )}
    </div>
  );
}

export default function TrainingMonitorPanel() {
  const selectedRunId = useTraining().selectedRunId;
  const viewingPast = !!selectedRunId;
  // When a Run History row is selected, show THAT run (by id); otherwise the latest/live run.
  const raw = useJson(viewingPast
    ? `${API_BASE}/training/datahub?runId=${encodeURIComponent(selectedRunId)}`
    : `${API_BASE}/training/datahub/latest`, 1500);
  const [openEpoch, setOpenEpoch] = useState(null);
  // Normalize the two response shapes: /latest → {status, run}; /datahub?runId= → {runs:[run]}.
  const run = viewingPast ? ((raw && raw.runs && raw.runs[0]) || null) : (raw && raw.run);
  const status = viewingPast
    ? (run ? (run.ended ? "done" : "running") : "idle")
    : ((raw && raw.status) || "idle");

  const prevRunId = useRef(null);
  useEffect(() => {
    if (!run) return;
    if (prevRunId.current !== run.runId) { prevRunId.current = run.runId; setOpenEpoch(null); }
    if (openEpoch == null && run.epochs && run.epochs.length) {
      const live = run.epochs.find((e) => !e.done);
      setOpenEpoch(live ? live.epoch : run.epochs[run.epochs.length - 1].epoch);
    }
  }, [run]);  // eslint-disable-line

  if (!run) {
    return (
      <div style={{ padding: 16, fontSize: 12, color: "var(--ink-3)", lineHeight: 1.7 }}>
        No training runs yet. Pick a task set and controller on the left, then select <b>Start Training</b>.
        Each run includes viewport playback, a success-rate curve, and a task-by-task observation and control trace.
      </div>
    );
  }

  const epochs = run.epochs || [];
  const srPct = run.tasksDone ? Math.round((run.SR || 0) * 100) : 0;
  const isLive = status === "running" || status === "starting";
  const statusCol = isLive ? OK : status === "failed" ? FAIL : "var(--ink-2)";
  const curEpoch = epochs.find((e) => !e.done);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden" }}>
      {viewingPast && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
          padding: "6px 12px", background: "var(--violet-soft)", borderBottom: "1px solid var(--line)" }}>
          <span style={{ color: "var(--violet)", fontWeight: 700, fontSize: 11 }}>Viewing past run (not live)</span>
          <button onClick={() => trainingStore.set({ selectedRunId: null })}
            style={{ fontSize: 11, padding: "2px 9px", borderRadius: 6, border: "1px solid var(--violet)",
              background: "transparent", color: "var(--violet)", cursor: "pointer", fontWeight: 700 }}>
            ← Back to live
          </button>
        </div>
      )}
      {/* SESSION / RUN */}
      <div style={{ padding: "10px 12px", borderBottom: "1px solid var(--line)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 7, flexWrap: "wrap" }}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 10, fontWeight: 800,
            color: statusCol, background: `color-mix(in srgb, ${statusCol === "var(--ink-2)" ? "#9aa7b8" : statusCol} 14%, transparent)`,
            padding: "2px 8px", borderRadius: 20, textTransform: "uppercase", letterSpacing: 0.4 }}>
            <span style={{ width: 6, height: 6, borderRadius: 6, background: statusCol,
              animation: isLive ? "pulse 1.4s ease-in-out infinite" : "none" }} />
            {status}
          </span>
          <span style={{ fontSize: 11, fontFamily: "monospace", color: "var(--ink-2)" }}>{run.runId.replace(/^run_/, "")}</span>
          <span style={{ fontSize: 11, fontWeight: 700, color: "var(--violet)" }}>{run.model}</span>
          <span style={{ fontSize: 10, padding: "1px 7px", borderRadius: 8, background: "var(--line)", color: "var(--ink-2)" }}>
            memory: {run.memory || "none"}</span>
        </div>
        <div style={{ display: "flex", gap: 8, marginTop: 9 }}>
          {[["success rate", `${srPct}%`, srPct >= 50 ? OK : "var(--ink)"],
            ["epoch", `${curEpoch ? curEpoch.epoch + 1 : epochs.filter((e) => e.done).length}/${run.epochsTotal || epochs.length}`],
            ["tasks/epoch", `${run.tasksPerEpoch || "—"}`]].map(([l, v, c]) => (
            <div key={l} style={{ flex: 1, background: "var(--bg-2,rgba(0,0,0,0.15))", borderRadius: 8,
              padding: "6px 9px", border: "1px solid var(--line)" }}>
              <div style={{ fontSize: 8.5, color: "var(--ink-3)", textTransform: "uppercase", letterSpacing: 0.4 }}>{l}</div>
              <div style={{ fontSize: 17, fontWeight: 800, color: c || "var(--ink)" }}>{v}</div>
            </div>
          ))}
        </div>
      </div>

      <LearningCurve curve={run.learningCurve || []} epochsTotal={run.epochsTotal} />

      {/* EPOCHS → TASKS → STEPS */}
      <div style={{ overflow: "auto", flex: 1 }}>
        {epochs.map((ep) => (
          <EpochRow key={ep.epoch} runId={run.runId} ep={ep} open={openEpoch === ep.epoch}
            onToggle={() => setOpenEpoch(openEpoch === ep.epoch ? null : ep.epoch)} />
        ))}
      </div>
    </div>
  );
}
