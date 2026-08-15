# Active Work: VISTA Playable Home

- Coordinator: Codex `/root`
- Session: 2026-08-15 playable-home implementation
- Integration worktree: `/home/yhliu/SimWorld-Studio-worktrees/vista-playable-home`
- Branch: `codex/vista-playable-home`
- Base: `origin/codex/vista-multiscene-world@a0c943d9`
- Status: sealed Linux Development package and all-room technical capture
  accepted; remote input permission and presentation-quality polish remain open

## Ownership

### Coordinator / integrator

- Owns: `docs/specs/vista-playable-home/**`,
  `tools/runtime/vista_playable_home/**`, final integration, validation,
  runtime lifecycle and evidence pointers.
- Must not touch: production configuration, canonical VISTA datasets,
  historical run evidence, unrelated UI/source changes.

### Contracts worker

- Child branch/worktree: assigned after specification commit.
- Owns: `world_packs/schemas/**`, `world_packs/vista_playable_home_r1/**`,
  `tools/worlds/**`, focused tests.
- Must not touch: Blender, Unreal plugin/composition, runtime or Studio UI.

### Blender worker

- Child branch/worktree: assigned after specification commit.
- Owns: `tools/blender/vista_playable_home/**`, focused tests and small source
  preview assets only when approved.
- Must not touch: schemas owned by contracts worker, Unreal, runtime or UI.

### Unreal worker

- Child branch/worktree: assigned after specification commit.
- Owns: `unreal_plugins/VistaPlayableHome/**`,
  `tools/ue/vista_playable_home/**`, focused tests.
- Must not touch: contracts, Blender, runtime, Studio UI or live projects.

## Runtime Exclusions

- No worker owns a live service, GPU or port during source implementation.
- Preserve GPU 1 and ports
  `3012/3022/55570/55582/8595/8596/8615/8616/8899/8919/8400`.
- Preserve tmux sessions `vista-blender-world-final-r5-20260811`,
  `vista-blender-mcp-final-20260811`, and
  `simworld-nlp-demo-20260721-142800`.
- Production port 8000 and all canonical VISTA data are forbidden.
- The coordinator alone may later claim an isolated GPU/display/port profile
  after a fresh preflight.

## Validation and Handoff

- Workers commit one logical change on their child branch after focused tests.
- Each worker returns commit hash, changed files, validation, residual blockers
  and any generated-artifact locations.
- The coordinator inspects and cherry-picks commits, resolves integration,
  runs the combined suite, performs runtime validation and pushes the final
  branch.
- Generated Blender/UE/runtime artifacts remain outside Git in a new
  append-only run root.

## Current Integrated State

- Accepted UE build: active run root `20260815T110115Z-navfix`, attempt 10.
- Accepted runtime proof: 29 generation-checked actions cover two distinct
  doors, keys pickup/place, NPC physical navigation with a cross-room carried
  item, and `mmg_001`/`mmg_044`/`mmg_045` start/reset.
- Accepted package: Linux Development archive under
  `ue/package-linux-development/attempt-04-no-afs-clean`; package receipt and
  NullRHI smoke are accepted, and the package-bound supervisor reached typed
  READY after two full archive re-hashes.
- Live surface: packaged ELF on GPU 0, display `:117`, loopback `55620`;
  Sunshine application `VISTA World`, user service
  `vista-sunshine.service`, Tailnet IP `100.114.80.121`.
- Remote video is ready. Moonlight control remains administrator-blocked only
  by root-only `/dev/uinput` and `/dev/uhid`; do not report it control-ready.
- Open product work: retained end-to-end NLP-to-live-UE proof, an uncut player
  traversal through all six rooms, review-camera reframing, visual-quality/
  lighting pass, and administrator input-device permission. Attempt 07 already
  retains a strict six-room nonblank technical receipt; it is not a realism
  acceptance claim.
