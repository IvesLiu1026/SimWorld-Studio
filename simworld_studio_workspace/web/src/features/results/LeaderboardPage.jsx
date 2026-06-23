import React, { useEffect, useState } from "react";
import { fetchLeaderboard } from "../../api/appApi.js";

function IconSlot({ icons, name, size }) {
  const icon = icons?.[name];
  return icon ? icon(size) : null;
}

function ratingTone(rating) {
  if (rating >= 1200) return "green";
  if (rating >= 1000) return "muted";
  return "red";
}

function winRateTone(winRate) {
  if (winRate >= 0.6) return "green";
  if (winRate >= 0.4) return "muted";
  return "red";
}

function RankCell({ icons, index }) {
  if (index === 0) return <span className="leaderboard-rank top"><IconSlot icons={icons} name="gold" size={18} /></span>;
  if (index === 1) return <span className="leaderboard-rank top"><IconSlot icons={icons} name="silver" size={18} /></span>;
  if (index === 2) return <span className="leaderboard-rank top"><IconSlot icons={icons} name="bronze" size={18} /></span>;
  return <span className="leaderboard-rank">{index + 1}</span>;
}

export default function LeaderboardPage({ icons = {} }) {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchLeaderboard()
      .then(setEntries)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const totalBattles = entries.reduce((sum, entry) => sum + entry.numBattles, 0);

  return (
    <div className="leaderboard-page">
      <div className="leaderboard-header">
        <span className="leaderboard-header-icon"><IconSlot icons={icons} name="trophy" size={24} /></span>
        <div className="leaderboard-heading">
          <h2>Leaderboard</h2>
          <p>Agent rankings based on Elo ratings from arena battles</p>
        </div>
        {entries.length > 0 && <div className="leaderboard-total">{totalBattles} total battles</div>}
      </div>

      <div className="leaderboard-body">
        {loading ? (
          <div className="leaderboard-empty">Loading...</div>
        ) : entries.length === 0 ? (
          <div className="leaderboard-empty">
            <div><IconSlot icons={icons} name="trophy" size={40} /></div>
            <h3>No battles yet</h3>
            <p>Run arena battles to see agents compete and build the leaderboard.</p>
          </div>
        ) : (
          <div className="leaderboard-table-wrap">
            <div className="leaderboard-table-header">
              <span>#</span>
              <span>Agent</span>
              <span>Rating</span>
              <span>Battles</span>
              <span>Wins</span>
              <span>Losses</span>
              <span>Win Rate</span>
            </div>

            {entries.map((entry, index) => {
              const delta = entry.rating - 1200;
              return (
                <div key={entry.agentName} className={`leaderboard-row${index === 0 ? " champion" : ""}`}>
                  <RankCell icons={icons} index={index} />
                  <div className="leaderboard-agent">
                    <span>{entry.agentName}</span>
                    {index === 0 && <strong>Champion</strong>}
                  </div>
                  <div className="leaderboard-number">
                    <span className={ratingTone(entry.rating)}>{Math.round(entry.rating)}</span>
                    <small className={delta >= 0 ? "green" : "red"}>
                      {delta >= 0 ? "+" : ""}{delta}
                    </small>
                  </div>
                  <span className="leaderboard-number muted">{entry.numBattles}</span>
                  <span className="leaderboard-number green">{entry.wins}</span>
                  <span className="leaderboard-number red">{entry.losses}</span>
                  <span className={`leaderboard-number ${winRateTone(entry.winRate)}`}>
                    {(entry.winRate * 100).toFixed(1)}%
                  </span>
                </div>
              );
            })}

            <div className="leaderboard-info">
              <strong>How ratings work</strong>
              <p>
                Agents start at 1200 Elo. Each battle updates ratings using the Bradley-Terry model
                (K=32). Winning against a higher-rated agent gives more points. Ties give half credit.
                Run more battles for more accurate rankings.
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
