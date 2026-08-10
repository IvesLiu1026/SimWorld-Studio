# Active Work: VISTA Blender-to-Unreal Interactive World

- Agent: Codex `/root`
- Session: 2026-08-11 overnight vertical slice
- Worktree: `/home/yhliu/SimWorld-Studio-worktrees/vista-blender-world`
- Branch: `codex/vista-blender-world`
- Base: `origin/codex/semantic-production-adapter@aeea9f76b8cd874205d4a39021a5acfb6e116d8c`
- Goal: reproducible Blender asset -> disposable UE 5.3.2 scene -> interactive
  loopback Pixel Streaming evidence.
- Current phase: attempt-07 supplies the authoritative import/map evidence and
  attempt-11 is the accepted, live loopback delivery runtime. Final aggregate
  validation, targeted commits, upstream correction, and GitHub branch
  publication are complete.

## Ownership

- Owns: `docs/specs/vista-blender-world/**`, new `tools/blender/**`, and new
  narrowly scoped UE import/capture tooling created for this slice.
- Must not touch: existing `docs/specs/production-readiness/**` ownership,
  canonical VISTA datasets, archived runs, production port `8000`, existing
  GPU 1 demo processes or its `3012/55570/8595/8596/8899` ports.
- Runtime ownership: GPU 1 only for a second bounded process after VRAM/port
  preflight; assigned ports are Studio `3022`, UE MCP `55582`, Cirrus
  HTTP/streamer `8615/8616`, and SFU `8919`.
- Artifact root: append-only `/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/`.
- Validation: requirements/design/tasks plus task-specific checks in `tasks.md`.
- Handoff: `docs/specs/vista-blender-world/handoff.md` records the accepted
  loopback runtime, final commit IDs, and compare URL.

## Exact candidate and evidence roots

- Final Blender source build (r3):
  `/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/20260811T-procedural-chair-upgrade-r3/blender`.
- Final consolidated run:
  `/home/yhliu/SimWorld-Studio-runs/20260811T015500-vista-blender-world-final`.
- Authoritative machine import/composition: the `ue/attempt-07` child of that
  run.
- Only delivery runtime: `ue/attempt-11`.
- Live UE project:
  `ue/attempt-11/ue/project/gym_citynav/gym_citynav.uproject`.
- UE map: `/Game/VISTA/Scenes/MMG040_Office_BlenderR1`.
- Final Blender MCP preparation root:
  `/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/20260811T-final-blender-mcp-r1`.
- Attempts 06-10 are non-delivery history. Attempt-10's secret-bearing browser
  trace must remain quarantined and must not be copied or shared.

## Live process ownership

- Final Unreal runtime tmux: `vista-blender-world-final-r5-20260811`.
- Final runtime owns only GPU 1 and ports `3022/55582/8615/8616/8919`.
- Blender MCP uses loopback `127.0.0.1:8400` and the owned tmux session
  `vista-blender-mcp-final-20260811`. The accepted final authenticated probe
  passed:
  42 tools, `execute_python` disabled, 294 objects, four collections, and 89
  chair-named root/objects.
- Existing demo tmux `simworld-nlp-demo-20260721-142800` and ports
  `3012/55570/8595/8596/8899` remain out of scope and must stay alive.
- Production port `8000` and canonical datasets remain untouched.

## Acceptance boundary

Completed machine gates:

- deterministic Blender bundle and structural/nonblank-preview validation;
- attempt-07 GLB import with all 285 required meshes and 33 materials;
- saved map with 285 generated actors, no provisional chair, and zero
  PlayerStart capsule blockers;
- one movable, gravity-enabled cardboard-box prop configured in the map.
- attempt-11 ready/health/auth/listener verification;
- grounded PIE verification before input and again after reset;
- action-only browser trace correlated to `86.58065473498606 cm` grounded pawn
  movement, with no network log or active token;
- four retained final/source views and bounded visual review;
- accepted final Blender MCP probe on port `8400`.

Production follow-on work, still not claimable:

- Production semantic index, public Coturn/WebRTC, real VLM review provider,
  full character IK/fall/12-second timeline, or bitmap PBR completion.

## Claude Code boundary

Any Claude worker uses a dedicated tmux window/worktree or read-only review
prompt, owns only explicitly assigned files, receives no secrets, and is limited
to at most two iterations. Exact model/profile identity must be measured, not
assumed from the phrase `fable5 ultracode`.

`claude auth status` currently reports `loggedIn: false`. The runtime is
therefore intentionally launched with `--model-mode off`; this does not block
the deterministic Blender/UE path, but the live NLP agent lane is unavailable
until the user runs `claude login` and separately approves any paid review.

## Coordinator handoff

1. Preserve the one accepted attempt-11 runtime and final Blender MCP tmux; do
   not start a competing process on their assigned ports.
2. Review or merge the published branch through
   `https://github.com/IvesLiu1026/SimWorld-Studio/compare/codex/semantic-production-adapter...codex/vista-blender-world`.
3. Keep `.playwright-cli/`, tokens, attempt directories, and other runtime
   artifacts untracked.
