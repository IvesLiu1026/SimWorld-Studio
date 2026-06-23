import React, { useEffect, useRef, useState } from "react";
import { fetchAgents, runArena, shareToGallery, updateAgent, voteOnBattle } from "../../api/appApi.js";

function IconSlot({ icons, name, size }) {
  const icon = icons?.[name];
  return icon ? icon(size) : null;
}

function BattleSide({ icons, isLoser, isWinner, label, onVote, revealed, side }) {
  return (
    <div className={`arena-battle-side${isWinner ? " winner" : ""}${isLoser ? " loser" : ""}`}>
      <div className="arena-battle-header">
        <span>
          {label}
          {isWinner && <span className="arena-winner-icon"><IconSlot icons={icons} name="check" size={12} /></span>}
        </span>
        {revealed && side && <span className="arena-agent-name">{side.agentName}</span>}
        {!revealed && <span className="arena-hidden-label">Identity hidden</span>}
      </div>

      <div className="arena-battle-image">
        {side && side.screenshots.length > 0 ? (
          <img src={side.screenshots[0]} alt={label} />
        ) : (
          <div>{side ? "No screenshot available" : "Waiting for generation..."}</div>
        )}
      </div>

      {onVote && (
        <div className="arena-vote-wrap">
          <button className="arena-vote-btn" onClick={onVote} type="button">
            Vote for {label}
          </button>
        </div>
      )}
    </div>
  );
}

export default function ArenaPage({ icons = {} }) {
  const [prompt, setPrompt] = useState("");
  const [battle, setBattle] = useState(null);
  const [phase, setPhase] = useState("prompt");
  const [voted, setVoted] = useState(null);
  const [progress, setProgress] = useState(null);
  const [agents, setAgents] = useState([]);
  const [showAgents, setShowAgents] = useState(false);
  const [shared, setShared] = useState(false);
  const abortRef = useRef(null);

  useEffect(() => {
    fetchAgents()
      .then(setAgents)
      .catch(() => {});
  }, []);

  const enabledAgentCount = agents.filter((agent) => agent.enabled).length;

  const startBattle = async () => {
    if (!prompt.trim()) return;
    setPhase("generating");
    setProgress(null);
    setShared(false);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await runArena(
        prompt,
        [],
        (eventType, data) => {
          if (eventType === "battle_created") return;
          if (eventType === "progress") {
            setProgress(data);
          } else if (eventType === "complete") {
            setBattle(data);
            setPhase("voting");
          } else if (eventType === "error") {
            console.error("Battle error:", data);
            setPhase("prompt");
          }
        },
        controller.signal,
      );
    } catch (error) {
      if (error.name !== "AbortError") console.error("Battle failed:", error);
      setPhase((current) => (current === "generating" ? "prompt" : current));
    }
  };

  const handleVote = async (winner) => {
    if (!battle) return;
    setVoted(winner);
    try {
      const result = await voteOnBattle(battle.id, winner);
      setBattle(result);
      setPhase("result");
    } catch {}
  };

  const handleShareWinner = async () => {
    if (!battle) return;
    const winnerSide = voted === "a" ? battle.side_a : voted === "b" ? battle.side_b : battle.side_a;
    if (!winnerSide) return;

    try {
      await shareToGallery({
        agentName: winnerSide.agentName,
        prompt: battle.prompt,
        screenshots: winnerSide.screenshots,
        skills: battle.skills,
        tags: ["arena", "battle"],
      });
      setShared(true);
    } catch {}
  };

  const resetBattle = () => {
    abortRef.current?.abort();
    setPrompt("");
    setBattle(null);
    setPhase("prompt");
    setVoted(null);
    setProgress(null);
    setShared(false);
  };

  const toggleAgent = async (agentId, enabled) => {
    try {
      const result = await updateAgent(agentId, { enabled });
      setAgents((prev) => prev.map((agent) => (agent.id === agentId ? { ...agent, ...result } : agent)));
    } catch {}
  };

  return (
    <div className="arena-page">
      <div className="arena-header">
        <span className="arena-title-icon"><IconSlot icons={icons} name="swords" size={24} /></span>
        <div className="arena-heading">
          <h2>Arena Battle</h2>
          <p>Two agents generate scenes from the same prompt. You decide which is better.</p>
        </div>
        <div className="arena-header-actions">
          <button
            className={`arena-agent-toggle${showAgents ? " active" : ""}`}
            onClick={() => setShowAgents((visible) => !visible)}
            type="button"
          >
            Agents ({enabledAgentCount}/{agents.length})
          </button>
        </div>
      </div>

      {showAgents && (
        <div className="arena-agent-list">
          <div className="arena-section-label">Available Agents</div>
          <div className="arena-agent-grid">
            {agents.map((agent) => (
              <label key={agent.id} className={`arena-agent-card${agent.enabled ? " enabled" : ""}`}>
                <input
                  type="checkbox"
                  checked={agent.enabled}
                  onChange={(event) => toggleAgent(agent.id, event.target.checked)}
                />
                <span>
                  <strong>{agent.name}</strong>
                  <small>{agent.type}{agent.model ? ` (${agent.model})` : ""}</small>
                  {agent.description && <em>{agent.description}</em>}
                </span>
              </label>
            ))}
          </div>
        </div>
      )}

      <div className="arena-content">
        {phase === "prompt" && (
          <div className="arena-prompt-panel">
            <h3>Enter a Scene Prompt</h3>
            <p>Both agents will try to build this scene. Vote for the better result.</p>
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="A quiet residential neighborhood with tree-lined streets and a small park..."
            />
            <div className="arena-actions centered">
              <button className="arena-primary-btn" onClick={startBattle} disabled={!prompt.trim()} type="button">
                Start Battle
              </button>
            </div>
            <div className="arena-count">
              {enabledAgentCount} agent{enabledAgentCount !== 1 ? "s" : ""} enabled
            </div>
          </div>
        )}

        {phase === "generating" && (
          <div className="arena-generating">
            <div className="arena-generating-icon"><IconSlot icons={icons} name="swords" size={40} /></div>
            <h3>Generating scenes...</h3>
            <p>"{prompt}"</p>
            {progress && (
              <div className="arena-progress-text">
                {progress.phase === "starting" && <>Matched: {progress.agentA} vs {progress.agentB}</>}
                {progress.phase === "generating_a" && <>Agent A ({progress.agent}) is generating...</>}
                {progress.phase === "generating_b" && <>Agent A done. Agent B ({progress.agent}) is generating...</>}
              </div>
            )}
            <div className="arena-progress-bar">
              <span className={progress?.phase === "generating_b" ? "wide" : ""} />
            </div>
            <button className="arena-secondary-btn" onClick={resetBattle} type="button">Cancel</button>
          </div>
        )}

        {(phase === "voting" || phase === "result") && battle && (
          <div>
            <div className="arena-prompt-summary">
              <span>Prompt</span>
              <strong>"{battle.prompt}"</strong>
            </div>

            <div className="arena-battle-grid">
              <BattleSide
                icons={icons}
                label="Agent A"
                side={battle.side_a}
                isWinner={voted === "a" || battle.winner === "a"}
                isLoser={voted !== null && voted !== "a" && voted !== "tie" && voted !== "both_bad"}
                revealed={phase === "result"}
                onVote={phase === "voting" ? () => handleVote("a") : undefined}
              />
              <BattleSide
                icons={icons}
                label="Agent B"
                side={battle.side_b}
                isWinner={voted === "b" || battle.winner === "b"}
                isLoser={voted !== null && voted !== "b" && voted !== "tie" && voted !== "both_bad"}
                revealed={phase === "result"}
                onVote={phase === "voting" ? () => handleVote("b") : undefined}
              />
            </div>

            {phase === "voting" && (
              <div className="arena-actions centered">
                <button className="arena-secondary-btn" onClick={() => handleVote("tie")} type="button">
                  Tie - Both Good
                </button>
                <button className="arena-secondary-btn" onClick={() => handleVote("both_bad")} type="button">
                  Both Bad
                </button>
              </div>
            )}

            {phase === "result" && (
              <div className="arena-result">
                <div className="arena-result-vote">
                  {voted === "tie"
                    ? "You voted: Tie"
                    : voted === "both_bad"
                      ? "You voted: Both Bad"
                      : `You voted: Agent ${voted?.toUpperCase()} wins!`}
                </div>
                {battle.side_a && battle.side_b && (
                  <div className="arena-result-agents">
                    Agent A: {battle.side_a.agentName} | Agent B: {battle.side_b.agentName}
                  </div>
                )}
                <div className="arena-actions centered">
                  <button className="arena-primary-btn" onClick={resetBattle} type="button">
                    New Battle
                  </button>
                  <button className="arena-success-btn" onClick={handleShareWinner} disabled={shared} type="button">
                    {shared ? "Shared!" : "Share to Gallery"}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
