import React from "react";

export default function CheckpointBar({ activeLeafId, checkpoint, checkpoints, icons, onRestore, restoring }) {
  if (!checkpoint) return null;

  const parent = checkpoint.parentId || null;
  const siblings = checkpoints.filter((item) => (item.parentId || null) === parent);
  const index = siblings.findIndex((item) => item.id === checkpoint.id);
  const hasBranches = siblings.length > 1;
  const isActive = activeLeafId === checkpoint.id;

  const leafOf = (checkpointId) => {
    let current = checkpointId;
    for (let guard = 0; guard < 1000; guard += 1) {
      const kids = checkpoints.filter((item) => item.parentId === current);
      if (!kids.length) return current;
      kids.sort((a, b) => new Date(b.createdAt) - new Date(a.createdAt));
      current = kids[0].id;
    }
    return current;
  };

  const pill = {
    display: "inline-flex",
    alignItems: "center",
    gap: 4,
    padding: "1px 8px",
    borderRadius: 999,
    border: "1px solid var(--line)",
    background: "var(--panel-2)",
    fontSize: 11,
    fontWeight: 600,
    color: "var(--ink-3)",
    cursor: "pointer",
    fontFamily: "inherit",
  };
  const pager = { ...pill, padding: "0 6px", minWidth: 18, justifyContent: "center" };

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, margin: "-4px 0 2px 38px", flexWrap: "wrap" }}>
      <span
        title={`Scene checkpoint - turn ${checkpoint.turnIndex} - ${checkpoint.actorCount} object(s)`}
        style={{
          ...pill,
          cursor: "default",
          color: isActive ? "var(--blue)" : "var(--ink-3)",
          borderColor: isActive ? "var(--blue)" : "var(--line)",
        }}
      >
        {icons?.map?.(11)} Checkpoint{isActive ? " - current" : ""}
      </span>
      {parent && (
        <button
          type="button"
          style={pill}
          disabled={restoring}
          title="Revert the live scene to how it was before this message"
          onClick={() => onRestore(parent)}
        >
          {restoring ? "Reverting..." : "Undo this change"}
        </button>
      )}
      {hasBranches && (
        <span
          style={{ display: "inline-flex", alignItems: "center", gap: 3, color: "var(--ink-3)", fontSize: 11 }}
          title="Parallel branches that diverge from this point"
        >
          <button
            type="button"
            style={pager}
            disabled={restoring}
            onClick={() => onRestore(leafOf(siblings[(index - 1 + siblings.length) % siblings.length].id))}
          >
            {"<"}
          </button>
          <span>branch {index + 1}/{siblings.length}</span>
          <button
            type="button"
            style={pager}
            disabled={restoring}
            onClick={() => onRestore(leafOf(siblings[(index + 1) % siblings.length].id))}
          >
            {">"}
          </button>
        </span>
      )}
    </div>
  );
}
