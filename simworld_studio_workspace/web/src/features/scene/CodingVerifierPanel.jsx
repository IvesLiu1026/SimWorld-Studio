import React, { useCallback, useEffect, useRef, useState } from "react";
import { runSceneCheck, scoreLatestScreenshot } from "../../api/appApi.js";
import LineChart from "../../components/charts/LineChart.jsx";
import { useMetrics } from "../../state/pollContext.jsx";

const TABS = [
  { id: "collisions", label: "Rule-based Checker" },
  { id: "vlm", label: "VLM Score" },
];

function countTone(value, warningTone = "red") {
  if (typeof value !== "number") return "muted";
  return value === 0 ? "green" : warningTone;
}

function scoreTone(score) {
  if (score >= 7) return "green";
  if (score >= 4) return "orange";
  return "red";
}

function StatChip({ label, tone, value }) {
  return (
    <div className="scene-verifier-stat">
      <div className={tone}>{value}</div>
      <span>{label}</span>
    </div>
  );
}

export default function CodingVerifierPanel({ latestScreenshot, sessionId }) {
  const [tab, setTab] = useState("collisions");
  const [collisionData, setCollisionData] = useState(null);
  const [scores, setScores] = useState([]);
  const [checking, setChecking] = useState(false);
  const [vlmRunning, setVlmRunning] = useState(false);
  const [vlmError, setVlmError] = useState(null);
  const prevScreenRef = useRef(null);
  const metrics = useMetrics();
  const sceneCollisionHistory = (metrics.sceneCollisions || []).map((item) => item.count);

  const runCollisionCheck = useCallback(async () => {
    setChecking(true);
    try {
      setCollisionData(await runSceneCheck());
    } catch {
      setCollisionData(null);
    } finally {
      setChecking(false);
    }
  }, []);

  const runVlmScore = useCallback(async () => {
    setVlmRunning(true);
    setVlmError(null);
    try {
      const result = await scoreLatestScreenshot(sessionId);
      setScores((prev) => [...prev.slice(-9), {
        feedback: result.feedback,
        label: result.label,
        score: result.score,
        screenshot: result.imageDataUrl,
        ts: Date.now(),
      }]);
    } catch (error) {
      setVlmError(error.message);
    } finally {
      setVlmRunning(false);
    }
  }, [sessionId]);

  useEffect(() => {
    if (!latestScreenshot || latestScreenshot === prevScreenRef.current) return;
    prevScreenRef.current = latestScreenshot;
    runCollisionCheck();
    runVlmScore();
  }, [latestScreenshot, runCollisionCheck, runVlmScore]);

  const collisionCount = collisionData?.collision_count ?? "-";
  const floatingCount = collisionData?.floating_count ?? "-";
  const collisionTone = countTone(collisionCount);
  const floatingTone = countTone(floatingCount, "orange");

  return (
    <div className="scene-verifier">
      <div className="scene-verifier-toolbar">
        <div className="scene-verifier-tabs">
          {TABS.map((item) => (
            <button
              key={item.id}
              className={tab === item.id ? "active" : ""}
              onClick={() => setTab(item.id)}
              type="button"
            >
              {item.label}
            </button>
          ))}
        </div>

        {tab === "collisions" && (
          <button className="scene-verifier-action" onClick={runCollisionCheck} disabled={checking} type="button">
            {checking ? "Checking..." : "Check"}
          </button>
        )}
        {tab === "vlm" && (
          <button className="scene-verifier-action" onClick={runVlmScore} disabled={vlmRunning} type="button">
            {vlmRunning ? "Scoring..." : "Score"}
          </button>
        )}
      </div>

      <div className="scene-verifier-body">
        {tab === "collisions" && (
          collisionData ? (
            <div>
              <div className="scene-verifier-stats">
                <StatChip label="Collisions" tone={collisionTone} value={collisionData.collision_count ?? 0} />
                <StatChip label="Floating" tone={floatingTone} value={collisionData.floating_count ?? 0} />
                <StatChip label="Actors" tone="muted" value={collisionData.checked_actors_count ?? 0} />
              </div>

              {sceneCollisionHistory.length > 1 && (
                <div className="scene-verifier-chart">
                  <LineChart series={sceneCollisionHistory} label="Collision history" color="var(--red)" W={380} H={72} />
                </div>
              )}

              {(collisionData.collision_pairs || []).slice(0, 5).map((pair, index) => (
                <div key={index} className="scene-verifier-issue red">
                  <strong>{pair.actor1}</strong> vs <strong>{pair.actor2}</strong>
                  <span>{pair.collision_type} - {Math.round(pair.penetration_depth)}cm</span>
                </div>
              ))}

              {collisionData.collision_count === 0 && (
                <div className="scene-verifier-pass">No collisions detected</div>
              )}

              {(collisionData.floating_actors || []).length > 0 && (
                <div className="scene-verifier-section">
                  <div className="scene-verifier-section-title">Floating Actors</div>
                  {(collisionData.floating_actors || []).slice(0, 5).map((actor, index) => (
                    <div key={index} className={`scene-verifier-issue ${actor.no_surface ? "red" : "orange"}`}>
                      <strong>{actor.name}</strong>
                      <span>
                        {actor.no_surface
                          ? "no surface below"
                          : `+${Math.round(actor.gap_cm)}cm above surface (Z=${actor.surface_z})`}
                      </span>
                    </div>
                  ))}
                </div>
              )}

              {collisionData.floating_count === 0 && collisionData.collision_count === 0 && (
                <div className="scene-verifier-note">All actors grounded - no overlaps</div>
              )}
            </div>
          ) : (
            <div className="scene-verifier-empty">
              {checking ? "Checking..." : "Click Check to run collision detection"}
            </div>
          )
        )}

        {tab === "vlm" && (
          <div>
            {vlmError && <div className="scene-verifier-error">{vlmError}</div>}
            {scores.length === 0 && !vlmRunning && (
              <div className="scene-verifier-empty">Click Score to evaluate the current scene with VLM</div>
            )}
            {scores.slice().reverse().map((score, index) => (
              <div key={index} className="scene-verifier-score-card">
                <div className="scene-verifier-score-row">
                  <span className={`scene-verifier-score ${scoreTone(score.score)}`}>
                    {score.score}<small>/10</small>
                  </span>
                  {score.label && <span className="scene-verifier-label">{score.label}</span>}
                  <time>{new Date(score.ts).toLocaleTimeString()}</time>
                </div>
                {score.feedback && <p>{score.feedback}</p>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
