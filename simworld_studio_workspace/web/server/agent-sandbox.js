"use strict";

// agent-sandbox.js — OS-level guardrail for the coding-agent CLIs.
//
// The scene-generation agents (claude/codex/opencode/gemini) are launched with broad
// permissions so they can drive MCP autonomously. Per-CLI tool restrictions help, but the
// only CLI-agnostic guarantee that they cannot MODIFY Studio's source is an OS sandbox.
//
// We wrap each agent process in `bwrap` (bubblewrap) with the repo mounted READ-ONLY:
//   - everything else stays read-write (home, ~/.config caches, /tmp) so the CLIs work,
//   - network is shared (agents need the LLM API + MCP TCP to UE),
//   - the repo subtree is re-bound read-only → any write to source fails at the kernel.
//
// The agent can still READ the repo (needed: MCP server + configs live there). Blocking
// reads too would require not binding the repo at all and instead exposing just the MCP
// server's files — more fragile; "cannot modify" is the firm requirement. Reads are
// further discouraged by the per-CLI tool restrictions (Claude --disallowedTools, etc.).
//
// Fail closed if bwrap is unavailable or AGENT_SANDBOX=0. Coding-agent execution must
// never silently downgrade to an unsandboxed process.

const path = require("path");
const { execSync } = require("child_process");

// web/server -> repo root (the SimWorld-Studio[-Internal]/devrun dir). Resolves correctly
// per deployment (boss vs dev copy).
const REPO_ROOT = path.resolve(__dirname, "../../..");

const HAS_BWRAP = (() => {
  try { execSync("command -v bwrap", { stdio: "ignore" }); return true; }
  catch { return false; }
})();

const SANDBOX_DISABLED = process.env.AGENT_SANDBOX === "0";

function buildSandboxCommand({ hasBwrap, disabled, repoRoot, bin, args, cwd }) {
  if (disabled) {
    throw new Error("Coding-agent sandbox is required; AGENT_SANDBOX=0 is forbidden");
  }
  if (!hasBwrap) {
    throw new Error("Coding-agent sandbox is required but bwrap is unavailable");
  }
  const bw = [
    "--dev-bind", "/", "/",            // share the host read-write (incl. /dev, network)
    "--ro-bind", repoRoot, repoRoot,     // ...except the repo: read-only (no source writes)
    "--die-with-parent",               // sandbox dies if the web server kills us
    ...(cwd ? ["--chdir", cwd] : []),
    "--", bin, ...args,
  ];
  return { cmd: "bwrap", args: bw, sandboxed: true };
}

/**
 * Wrap a CLI invocation so the repo is read-only.
 *
 * This only proves a fail-closed, read-only source boundary. It does not make
 * the rest of HOME private or remove network access, so live agents remain
 * disabled until the stronger T2 confinement gate passes.
 */
function sandboxedSpawn(bin, args, cwd) {
  return buildSandboxCommand({
    hasBwrap: HAS_BWRAP,
    disabled: SANDBOX_DISABLED,
    repoRoot: REPO_ROOT,
    bin,
    args,
    cwd,
  });
}

module.exports = {
  buildSandboxCommand,
  sandboxedSpawn,
  HAS_BWRAP,
  REPO_ROOT,
  SANDBOX_DISABLED,
};
