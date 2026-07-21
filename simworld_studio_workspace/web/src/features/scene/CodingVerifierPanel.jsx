import React, { useCallback, useEffect, useRef, useState } from "react";
import { runSceneCheck } from "../../api/appApi.js";
import LineChart from "../../components/charts/LineChart.jsx";
import { useMetrics } from "../../state/pollContext.jsx";

function countTone(value, warningTone = "red") {
  if (typeof value !== "number") return "muted";
  return value === 0 ? "green" : warningTone;
}

function StatChip({ label, tone, value }) {
  return (
    <div className="scene-verifier-stat">
      <div className={tone}>{value}</div>
      <span>{label}</span>
    </div>
  );
}

export default function CodingVerifierPanel({ latestScreenshot }) {
  const [collisionData, setCollisionData] = useState(null);
  const [checking, setChecking] = useState(false);
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

  useEffect(() => {
    if (!latestScreenshot || latestScreenshot === prevScreenRef.current) return;
    prevScreenRef.current = latestScreenshot;
    runCollisionCheck();
  }, [latestScreenshot, runCollisionCheck]);

  const collisionCount = collisionData?.collision_count ?? "-";
  const floatingCount = collisionData?.floating_count ?? "-";
  const collisionTone = countTone(collisionCount);
  const floatingTone = countTone(floatingCount, "orange");

  return (
    <div className="scene-verifier">
      <div className="scene-verifier-toolbar">
        <div className="scene-verifier-heading">
          <strong>Geometry validation</strong>
          <span>Deterministic scene checks</span>
        </div>
        <button className="scene-verifier-action" onClick={runCollisionCheck} disabled={checking} type="button">
          {checking ? "Checking..." : "Run check"}
        </button>
      </div>

      <div className="scene-verifier-body">
        {collisionData ? (
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
            {checking ? "Checking..." : "Run a check to inspect collisions and grounding"}
          </div>
        )}

        <div className="scene-verifier-review-note">
          <strong>Qualitative review</strong>
          <span>Choose Text or Visual in the Chat Review control, then send a review request.</span>
        </div>
      </div>
    </div>
  );
}
