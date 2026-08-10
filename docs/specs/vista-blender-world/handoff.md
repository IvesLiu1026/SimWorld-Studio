# Handoff: VISTA Blender-to-Unreal Interactive World

Status: Loopback vertical slice accepted — source committed locally, GitHub push pending; not Production
Updated: 2026-08-11

## Outcome so far

The deterministic modeling, UE commandlet, and isolated live-runtime portions
are complete for one bounded `mmg_040` vertical slice. The final Blender r3
bundle contains a room shell, detailed tall office cabinet, and detailed
ergonomic caster chair. All three were imported into the fresh UE 5.3.2
attempt-07 project, and the map
`/Game/VISTA/Scenes/MMG040_Office_BlenderR1` was saved after removing the
visible provisional chair and other recorded geometry surrogates.

Attempt-11 is the only delivery runtime. It passed exact loopback/auth/UE health,
grounded PIE state, correlated browser keyboard movement, reset-to-PlayerStart
grounding, safe action-trace scanning, and bounded visual review. The signed
attempt-07 scene receipt remains an immutable machine-composition receipt with
`production_ready: false`; live acceptance is carried by the separate
attempt-11 receipts below, not by rewriting that historical receipt.

This is an accepted SSH-loopback development demo, not a public or Production
deployment. The source implementation is committed locally; the documentation
checkpoint and GitHub push are still pending and must not be claimed yet.

## Source and Git identity

- Worktree: `/home/yhliu/SimWorld-Studio-worktrees/vista-blender-world`
- Branch: `codex/vista-blender-world`
- Base commit:
  `aeea9f76b8cd874205d4a39021a5acfb6e116d8c`
- User fork: `git@github.com:IvesLiu1026/SimWorld-Studio.git`
- Intended PR base: `codex/semantic-production-adapter`
- Local implementation commits:
  - `fcfff03e` — reproducible VISTA Blender forge
  - `3e1557ab` — hardened loopback Blender MCP lane
  - `91b5af94` — verified Blender-to-UE import and composition
  - `82f46f33` — isolated VISTA world runtime and browser-input proof
- Current warning: this local branch still inherits
  `origin/codex/semantic-production-adapter` as its upstream. It has not yet
  been truthfully recorded as committed/pushed for this slice.

After final validation and commits, correct and push it without force:

```bash
cd /home/yhliu/SimWorld-Studio-worktrees/vista-blender-world
git branch --unset-upstream
git push --set-upstream origin HEAD:refs/heads/codex/vista-blender-world
```

Never stage `.playwright-cli/`, generated screenshots outside the evidence
root, `__pycache__/`, tokens, or runtime artifacts. Stage explicit source/spec/
test paths only; do not use `git add .`.

## Immutable Blender evidence

Canonical r3 evidence root:

```text
/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/
  20260811T-procedural-chair-upgrade-r3/blender
```

The exact same final Blender bytes used by attempt-07 are also under:

```text
/home/yhliu/SimWorld-Studio-runs/
  20260811T015500-vista-blender-world-final/blender
```

Recorded facts:

- Blender: `4.5.8 LTS`; fixed seed: `4040`
- 285 meshes, 33 named materials, 72,320 triangles
- Room shell: 76 meshes / 10,448 triangles
- Tall cabinet: 121 meshes / 33,500 triangles
- Ergonomic chair: 88 meshes / 28,372 triangles
- Manifest SHA-256:
  `1c1008ebcd3b9cb6f130a54f65e04de530e81f3a6954c042e43f237fbe35cc55`
- GLB SHA-256:
  `c3d67a34f0f0bd720133dc8ce08c7bb52b0c3008386aec04957014d76010e759`
- `.blend` SHA-256:
  `4e2000b057c6cb9590e6b8511ad6b3165948a6942a7599140b6dd983a2b36ebb`
- Overview preview SHA-256:
  `b5124cff7ff1f45c47425d81323656958873cd7b29e1e9f4f0eb46233d91a164`
- Detail preview SHA-256:
  `fa20738ec255ad5d09dd9a49893ea502f87330ff16beccce24283880f2cde993`

Reproduce only into a new append-only run directory:

```bash
cd /home/yhliu/SimWorld-Studio-worktrees/vista-blender-world
bash tools/blender/run_vista_blender_build.sh \
  /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/NEW_RUN_ID
```

Do not overwrite r3. The generated materials use version-controlled
Principled BSDF constants and geometry detail; they do not include authored
bitmap albedo/normal/roughness PBR texture sets. This vertical slice is more
detailed than a basic-geometry fallback, but it is not a claim of a complete
photogrammetry/PBR content pipeline.

## UE attempt-07 evidence

Final candidate root:

```text
/home/yhliu/SimWorld-Studio-runs/20260811T015500-vista-blender-world-final/
  ue/attempt-07
```

Do not promote or reuse attempt-06; its preparation contract predated the
three-asset final bundle.

Attempt-07 machine evidence:

- Preparation plan SHA-256:
  `d48d9982347f0a8878092a4ffbc41e0d53330b666d3daeda59a8db0b2763ed50`
- Import receipt SHA-256:
  `07726899bcbfcb36e6470bde5d88d17c396757fdb9467f5dc47dbaaf3dd456c1`
- Scene receipt SHA-256:
  `7b7b55f930ada9f0210ef9e6b3e5ad13a0c1edcee571b0cc04e97869a87a9409`
- Imported inventory: 318 objects = 285 Static Mesh assets + 33 Material assets
- Required mesh coverage: cabinet 121/121, room 76/76, chair 88/88
- Generated scene actors: 285; total saved actor inventory: 296
- Removed surrogates: `VISTA_back_wall`, `VISTA_high_shelf`,
  `VISTA_office_chair_provisional`, `VISTA_runtime_ground`, `VISTA_side_wall`
- PlayerStart: `[150, -150, 100]` cm, capsule radius 35 cm, half-height
  90 cm, zero blocking actors
- Physics candidate: `VISTA_high_cardboard_box`, movable, gravity enabled,
  collision profile `PhysicsActor`. This configuration is recorded by
  attempt-07; the attempt-11 initial live verification observed the box
  settled near the floor.
- Map file SHA-256:
  `8dd416ca31f66cbdbbdc60c1ef2c21c8bdf7d83b1abe36e177f202bc01e8d9f4`

The receipt statuses are intentionally candidate-level:
`imported_inspected_candidate` and `saved_machine_candidate`. They prove the
imported content/map, while attempt-11 proves its live delivery. Neither is a
Production-ready declaration.

## Accepted runtime: attempt-11

- Server used for this run: `140.113.215.69`
- GPU: 1 only
- tmux: `vista-blender-world-final-r5-20260811`
- Studio UI: `127.0.0.1:3022`
- Unreal MCP: `127.0.0.1:55582`
- Cirrus HTTP/streamer: `127.0.0.1:8615` / `127.0.0.1:8616`
- SFU: `127.0.0.1:8919`
- Runtime workspace:
  `/home/yhliu/SimWorld-Studio-runs/20260811T015500-vista-blender-world-final/ue/attempt-11`
- Runtime project:
  `/home/yhliu/SimWorld-Studio-runs/20260811T015500-vista-blender-world-final/ue/attempt-11/ue/project/gym_citynav/gym_citynav.uproject`
- Runtime is launched with `--model-mode off`.
- `ready_at`: `2026-08-10T18:21:24.771742Z`
- UE readiness timeout: explicit 900 seconds for this cold-start-safe run.

For audit, this is the exact no-secret invocation currently owned by the tmux
session (it is shown here for reproduction review, not for concurrent rerun):

```bash
cd /home/yhliu/SimWorld-Studio-worktrees/vista-blender-world
uv run --project tools python \
  tools/runtime/vista_blender_world/launch.py \
  --workspace /home/yhliu/SimWorld-Studio-runs/20260811T015500-vista-blender-world-final/ue/attempt-11 \
  --project /home/yhliu/SimWorld-Studio-runs/20260811T015500-vista-blender-world-final/ue/attempt-11/ue/project/gym_citynav/gym_citynav.uproject \
  --map /Game/VISTA/Scenes/MMG040_Office_BlenderR1 \
  --ue-editor /mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/runtime/SimWorld-Studio-Minimal-806e869a/Engine/Binaries/Linux/UnrealEditor \
  --cirrus-dir /mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/runtime/SimWorld-Studio-Minimal-806e869a/Engine/Plugins/Media/PixelStreaming/Resources/WebServers/SignallingWebServer \
  --studio-workspace /home/yhliu/SimWorld-Studio-runs/20260811T015500-vista-blender-world-final/ue/attempt-11/studio-workspace \
  --node-bin /home/yhliu/.local/opt/node-v22.22.2/bin/node \
  --nvidia-icd /home/yhliu/SimWorld-Studio-runs/20260721T142800-nlp-live-demo/workspace/runtime/nvidia-headless-icd.json \
  --nvidia-compat /home/yhliu/SimWorld-Studio-runs/20260721T142800-nlp-live-demo/workspace/runtime/nvidia-compat \
  --model-mode off --gpu 1 \
  --studio-port 3022 --ue-mcp-port 55582 \
  --cirrus-http-port 8615 --cirrus-streamer-port 8616 \
  --cirrus-sfu-port 8919 --width 1280 --height 720 --fps 60 \
  --ue-ready-timeout-seconds 900
```

The independent older demo owns GPU 1 plus
`3012/55570/8595/8596/8899` in tmux
`simworld-nlp-demo-20260721-142800`. Do not kill it, repurpose its ports, or
include it in cleanup. Production port `8000` is also out of scope.

### Runtime acceptance receipts

- Health:
  `receipts/health-20260810T182134.151144+0000.json`, SHA-256
  `96f55fa4645c66b472b026014a0bad3ef0484ba84a26c13d1ba9177b2d6e8a9e`.
  It proves all five listeners are `127.0.0.1`, unauthenticated Studio/Cirrus
  requests return 401, authenticated requests return 200, and both
  `ueConnected` and `mcpConnected` are true.
- Initial grounded verify:
  `receipts/live-qa-verify-2026-08-10T182204.888019_0000.json`, SHA-256
  `d74d95f83e8ea3979b0490520a33206744128d62f71eb736e00a656d753c2aaf`.
  The possessed pawn is grounded at `[150, -150, 92.27500200271606]` cm with
  zero velocity; the physics-enabled box has settled near floor height.
- Browser input:
  `receipts/browser-input-2026-08-10T182235.329472_0000.json`, SHA-256
  `fa229923be2e124f9b86cf0a5258093d2294514c18affe0108631d57960ee871`.
  It correlates a Pixel Streaming browser key action to
  `86.58065473498606 cm` of pawn displacement while the before and after state
  receipts both report `on_ground: true`.
- Initial/final input states are bound by SHA-256
  `e4f331b21cbbd5e09ccf1e5e22ca87696096ac49fdda1b18e9c9bc39be38d0e6`
  and `5e2fe5c7c7fe7086e36c97eeaf1ccccc51ed3496edf179d375cff4c56385f7b0`.
- Accepted action-only trace:
  `evidence/browser-input-action-trace.zip`, 48,955,877 bytes, SHA-256
  `786663baabbb6027a28f0954cefb02b0fa440335a25b7fb988c154ff8e32f5ec`.
  Its receipt records `active_token_present: false` and
  `network_log_included: false`.
- Final reset grounded verify:
  `receipts/live-qa-verify-2026-08-10T182321.151349_0000.json`, SHA-256
  `22e376df6a282b8a99cef73e80c52f7e907cc586b7bab7e6e58be0028879dac3`.
  It proves the delivery was returned to a possessed, grounded PlayerStart
  state after the input test.

An exact active-token scan covered 5,175 attempt-11 regular files and found zero token
matches outside the expected mode-0600 `access-token` file. The access token is
never part of a receipt, screenshot, or committed source artifact.

### Retained visual evidence

All paths below are relative to attempt-11 `evidence/screenshots/`:

| View | Dimensions | SHA-256 |
| --- | --- | --- |
| `final-studio-live.png` | 1600x1000 | `ecd1f6867d0793017ebe172ca67c77d3b3937cfe12acc2cf38804947116a9150` |
| `final-ue-live.png` | 666x728 | `c8e9d27e54c667b748782a814f8827bc1391eb5f7aa228e550870ebf59e4aa36` |
| `source-blender-chair-detail.png` | 720x540 | `fa20738ec255ad5d09dd9a49893ea502f87330ff16beccce24283880f2cde993` |
| `source-blender-overview.png` | 720x540 | `b5124cff7ff1f45c47425d81323656958873cd7b29e1e9f4f0eb46233d91a164` |

The bounded visual review used zero paid Claude/VLM iterations. No P0 black
frame, checker material, gross floating/penetration, or provisional chair was
accepted. Known non-P0 limitations—procedural constant materials instead of
bitmap PBR, a small single-room slice, and no full character animation—remain
explicitly deferred rather than hidden by an open-ended repair loop.

### Quarantined runtime history

- Attempt-07's first cold launch exceeded the original readiness allowance.
  The launcher now accepts only a bounded 60-900 second
  `--ue-ready-timeout-seconds`, defaults to 600, and attempt-11 used 900.
- Attempt-09 was stopped after token exposure; its token was revoked/rotated
  and it is not a delivery candidate.
- Attempt-10 passed functional checks but was stopped and quarantined after its
  Playwright `.network` trace was found to contain the HTTP-only auth cookie.
  That secret-bearing trace is not shareable evidence and must never be
  promoted, copied into docs, or committed.
- Attempt-11 uses a fresh rotated token and a network-log-free action trace. It
  is the only delivery runtime.

## Safe Mac access

From the Mac, create the SSH tunnel:

```bash
ssh -N -L 3022:127.0.0.1:3022 yhliu@140.113.215.69
```

In a second local terminal, retrieve the Studio token through authenticated
SSH. This prints it only to the user's terminal; do not paste it into chat,
issues, screenshots, or logs:

```bash
ssh yhliu@140.113.215.69 \
  'cat /home/yhliu/SimWorld-Studio-runs/20260811T015500-vista-blender-world-final/ue/attempt-11/access-token'
```

Open this on the Mac, substituting the privately retrieved value:

```text
http://127.0.0.1:3022/?token=<STUDIO_ACCESS_TOKEN>
```

Closing the browser does not stop the server. Reopen the same URL while the
owned tmux runtime remains alive.

Server-side health verification, which reads but does not print the token:

```bash
cd /home/yhliu/SimWorld-Studio-worktrees/vista-blender-world
PYTHONPATH=. uv run --project tools python \
  tools/runtime/vista_blender_world/verify.py \
  --workspace /home/yhliu/SimWorld-Studio-runs/20260811T015500-vista-blender-world-final/ue/attempt-11
```

The current launch command is retained by the tmux pane and
`runtime-state.json`. Do not rerun it against attempt-11: the launcher correctly
refuses an existing append-only state. If the runtime must be recreated, make
a new attempt directory and repeat preflight/import ownership checks instead of
deleting attempt-11 state.

## Safe stop commands

Stop only the recorded attempt-11 runtime identities:

```bash
cd /home/yhliu/SimWorld-Studio-worktrees/vista-blender-world
PYTHONPATH=. uv run --project tools python \
  tools/runtime/vista_blender_world/stop.py \
  --workspace /home/yhliu/SimWorld-Studio-runs/20260811T015500-vista-blender-world-final/ue/attempt-11
```

Do not use process-name-wide `pkill`, GPU-wide cleanup, or a broad tmux kill.

## Blender MCP handoff

The hardened implementation is pinned to `zorak1103/blender-mcp` v0.5.1,
commit `43d60c36aadc892739d42051f64f87fe55a57b48`. It binds only to
`127.0.0.1:8400`, authenticates every MCP request, runs in a bounded `bwrap`
workspace, and excludes `execute_python`.

Final MCP run root:

```text
/mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/
  20260811T-final-blender-mcp-r1
```

The exact final `source.blend` is at `blender/source.blend` in that root. The
owned tmux session `vista-blender-mcp-final-20260811` serves it on port `8400`.
The authenticated final probe passed with:

- 42 tools;
- `execute_python_enabled: false`;
- loopback-only URL `http://127.0.0.1:8400/mcp`;
- four collections and 294 objects;
- 89 `VISTA_Chair*` objects including one root plus 88 meshes;
- receipt
  `mcp/final-live-probe.receipt.json`, SHA-256
  `9bd496f9d22f0010db8f38f25194a07411885bc905ac97f7c95fa40a293a4c5e`.

The canonical launch shape is:

```bash
cd /home/yhliu/SimWorld-Studio-worktrees/vista-blender-world
bash tools/blender/run_vista_blender_mcp.sh \
  --run-root /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/20260811T-final-blender-mcp-r1 \
  --blend-file /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/20260811T-final-blender-mcp-r1/blender/source.blend \
  --port 8400 --display 118
```

Run it only in the named owned tmux session; never start a second listener on
the same port. Probe without printing the bearer token:

```bash
cd /home/yhliu/SimWorld-Studio-worktrees/vista-blender-world
PYTHONPATH=. uv run --project tools python \
  tools/blender/probe_vista_blender_mcp.py \
  --url http://127.0.0.1:8400/mcp \
  --token-file /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/20260811T-final-blender-mcp-r1/mcp-home/.config/blender-mcp/token \
  --output /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/20260811T-final-blender-mcp-r1/mcp/NEW_PROBE_RECEIPT.json
```

Use a new output filename for every probe because the evidence tree is
append-only. The accepted probe receipt remains
`mcp/final-live-probe.receipt.json` as recorded above.

The token file must remain mode `0600`. For a Mac MCP client, separately
tunnel `8400` through SSH:

```bash
ssh -N -L 8400:127.0.0.1:8400 yhliu@140.113.215.69
```

Retrieve its bearer token only in a separate private terminal:

```bash
ssh yhliu@140.113.215.69 \
  'cat /mnt/NAS2/yhliu/SimWorldStudio/vista-blender-world/runs/20260811T-final-blender-mcp-r1/mcp-home/.config/blender-mcp/token'
```

Configure the MCP client for `http://127.0.0.1:8400/mcp` with an
`Authorization: Bearer <BLENDER_MCP_TOKEN>` header. Never publish port `8400`,
paste the token into chat/logs, or commit it.

Stop only the owned MCP tmux session:

```bash
tmux kill-session -t vista-blender-mcp-final-20260811
```

## User actions versus administrator actions

No administrator action is required to finish the current GPU-1 loopback
vertical slice. The following user-owned actions are distinct from server
administration:

| Owner | Action | Blocks the current visual runtime? |
| --- | --- | --- |
| User | Run `claude login`; `claude auth status` currently reports `loggedIn: false` | No; only live NLP/optional Claude review |
| User | Explicitly approve paid Claude/VLM review calls and their budget | No; deterministic acceptance can proceed |
| User | Maintain GitHub SSH authorization and approve the final PR/merge | No; blocks synchronization/merge only |
| User/content owner | Approve asset licenses, embedding provider and content allowed in a real semantic index | No; blocks Production semantic retrieval |
| User/research owner | Supply the exact VISTA animation/timeline behavior and acceptance cases | No; blocks full animation/IK/fall/12-second scope |

Administrator or infrastructure-owner work is needed only for the following
Production expansion:

| Administrator scope | Minimal permission/provisioning request | Why it is needed |
| --- | --- | --- |
| Full Unreal toolchain | Provide the exact complete UE `5.3.2` build tree, Engine Source, RunUAT and matching Linux SDK; keep it read-only to the project user where possible | Compile/review the native backend and unfinished animation plugin instead of relying on the packaged-editor compatibility path |
| Semantic asset services | Provide managed Postgres + Qdrant + embedding service, or permit a rootless Podman deployment with private persistent volumes and backups | `POSTGRES_URL` alone is not an index; the present catalog is not a Production semantic asset system |
| Service identity and secrets | Create a least-privilege service account, private secret location such as `/etc/simworld/secrets`, runtime directory, systemd units, backup/rotation policy and disk monitoring | Required for unattended Production operation; do not put secrets in Git or user-visible URLs |
| Public WebRTC | Allocate public DNS, TLS certificates, Coturn credentials, firewall/NAT rules and a bounded UDP relay range; coordinate with the existing owners of ports 80/443 | Current delivery is native UE Pixel Streaming/WebRTC transported through an SSH loopback tunnel, not public Coturn/WebRTC |
| Render device, only if required | Grant only the necessary `render`/`video` group access to the exact `/dev/dri` node | GPU 1 works for the current slice; broad GPU or root access is unnecessary |
| Target server migration | On `140.113.215.82`, provide SSH ACL, disk quota, GPU/container inventory and the exact UE path before copying artifacts | The current proven host is `.69`; `.82` needs a separate read-only preflight and must not be assumed equivalent |

The root filesystem was already near capacity during preflight, so large UE,
database, model, and generated-artifact storage should remain on NAS or a
managed volume. Any cleanup of shared disk content requires its owner's
approval.

## Known limitations and honest product status

- This is one high-detail procedural vertical slice, not the complete
  VISTA-world dataset importer or a general text-to-world Production system.
- The scene uses deterministic procedural material constants, not bitmap PBR
  texture maps or photogrammetry.
- Claude is logged out, and this runtime is in model mode `off`; the live NLP
  scene-building lane is not currently enabled.
- The full Postgres/Qdrant semantic index and embedding ingestion job are not
  complete.
- Public Coturn/DNS/TLS/firewall deployment is not complete. The current
  transport stays loopback-only and is reached over SSH.
- Full character animation, hand/foot IK, fall animation, and automatic
  12-second VISTA timeline execution are not complete.
- Real Text/Visual Review providers remain separate Production work; this
  checkpoint does not claim a VLM review.
- The packaged UE 5.3.2 tree lacks the full build/source toolchain. The current
  compatibility route is acceptable only for this disposable loopback demo.
- The loopback runtime and accepted final MCP probe are accepted. Final
  aggregate validation passed: 76 tests plus 17 subtests, Ruff, shell syntax,
  Python compile, schema/receipt checks, source secret scanning, and diff
  checks are green. Documentation commit and GitHub push remain pending.

## Final validation checklist for the coordinator

Before changing T10 and Git publication status to complete:

1. Commit this reviewed spec/handoff checkpoint. Explicitly exclude attempts
   06-11, all token files, `.playwright-cli/`, and the quarantined attempt-10
   network trace.
2. Correct the inherited upstream, push the branch without force, and record
   the documentation commit plus the PR/compare URL here.
3. Only then mark T10 complete and describe the branch as published. T8/T9 are
   already complete for the bounded loopback slice and do not imply Production
   readiness.
