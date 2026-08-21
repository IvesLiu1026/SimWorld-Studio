# Design: VISTA World Repository Extraction

Status: Draft
Updated: 2026-08-21
Depends on: requirements.md

## Summary

建立 standalone `VISTA-World` monorepo，先抽離已證明可獨立運作的 contracts、
world packs、UE plugin、Blender/UE pipelines、tests 與 packaged runtime。現有
SimWorld fork 保留，並透過 `VistaWorldTransport` compatibility adapter 使用其
NLP/MCP、review、asset search 與 browser streaming 能力。UI/control plane 最後抽離。

## Architecture and Flow

```text
VISTA dataset / verified events
        |
        v
contracts + compiler ---> world pack ---> Blender/UE pipeline ---> packaged game
        |                                                        |
        +---------------- VistaWorldTransport -------------------+
                              |
             +----------------+----------------+
             |                                 |
      standalone TCP                    SimWorld adapter
                                               |
                              NLP / MCP / review / browser stream
```

Target layout:

```text
VISTA-World/
  contracts/
  world_packs/
  plugins/unreal/VistaPlayableHome/
  pipelines/blender/
  pipelines/unreal/
  runtime/
  services/control-plane/
  adapters/simworld-studio/
  apps/control-center/
  tests/
  docs/
```

## Current-to-Target Mapping

| Current path | Initial target | Coupling |
|---|---|---|
| `world_packs/schemas/**` | `contracts/schemas/**` | none |
| `world_packs/vista_playable_home_r1/**` | `world_packs/vista_playable_home_r1/**` | none |
| `tools/worlds/playable_home.py` | `contracts/python/vista_world/**` | low |
| `unreal_plugins/VistaPlayableHome/**` | `plugins/unreal/VistaPlayableHome/**` | UE only |
| `tools/blender/vista_playable_home*` | `pipelines/blender/**` | Blender/assets |
| `tools/ue/vista_playable_home/**` | `pipelines/unreal/**` | UE/RunUAT |
| `tools/runtime/vista_playable_home/**` | `runtime/**` | OS/GPU/Sunshine |
| `simworld_studio_workspace/web/server/vista-world-*` | `adapters/simworld-studio/**` | medium |
| `vista-import-*`, `vista-scene-build-*`, `vista-animation-*` | later service extraction | high |
| SimWorld `App.jsx`, `index.js`, `mcp-server.js` edits | compatibility patch only | high |

During the first extraction, source paths may remain unchanged inside the new repo if path
renaming would break import contracts. Layout normalization happens only after baseline tests pass.

## Interfaces and Contracts

### VistaWorldTransport

- Request: typed `vista_world_action` payload with contract version, run identity and timeout.
- Response: typed result/error with retryability and observed world revision.
- Implementations:
  - standalone loopback TCP transport;
  - SimWorld `UeMcpBroker` adapter;
  - future browser/WebRTC control adapter.

### Versioned packages

- `vista-world-contracts`: JSON schemas plus pure compiler/validator.
- `VistaPlayableHome`: UE plugin source/artifact.
- `vista-world-pack-*`: world/event/profile manifests; no unlicensed binary assets.
- `vista-world-runtime`: launch, acceptance, packaging and Sunshine integration CLI.
- `@vista-world/simworld-adapter`: optional Node control-plane adapter.

## Data Model and Migration

- v1 schema IDs and receipt semantics remain immutable.
- New contracts use a `vista.world.*` v2 namespace only after compatibility tests exist.
- Existing accepted receipts stay bound to the `d80aa78f`/`077ec0aa` lineage and NAS evidence.
- Asset manifests carry source URL, license, digest, transformation receipt and commercial-use flag.
- No canonical dataset, UE content archive or generated package is copied into Git.

## Git and Repository Model

- New remote: standalone `IvesLiu1026/VISTA-World`, default `main`.
- Seed history: filtered selected-path history from the realism lineage; no squash of meaningful
  authorship, and no unreviewed WIP/quarantine branches.
- Development: short-lived `codex/YYYY-MM-DD-slug` or equivalent tool-prefixed branches.
- Integration: required PR, CI and one integrator; prohibit force push on `main`.
- Existing SimWorld fork becomes `simworld-compat` reference and remains pinned by commit SHA.
- Preserve former upstream pin `7c03df9d`; add the current GitHub parent as a separate remote
  rather than overwriting legacy provenance.

## Daily Delivery Loop

The approved implementation contract lives in `../vista-world-daily-maintainer/`.

1. 09:17 Asia/Taipei: select one trusted issue/finding with acceptance criterion and owned paths.
2. Create a short branch/worktree from current `main`.
3. Implement one reviewable logical unit and run focused validation outside the model process.
4. Stage named files, commit with transparent automation attribution, and open a draft PR.
5. Merge only after required CI and the approved risk-tier policy; verify the SHA is reachable from
   remote `main`.
6. If blocked or no safe candidate exists, record truthful run status; never generate an empty,
   timestamp-only or backdated commit.

Suitable daily units include a schema invariant plus test, one verified VISTA event mapping,
one interaction regression, one asset-license/provenance gate, one runtime hardening change,
or one experimentally verified runbook update.

## Failure Handling and Rollback

- If extraction tests fail, keep canonical operation on the current fork and package.
- If GitHub owner/auth mismatch is detected, do not create or publish the repository.
- If a selected-path commit depends on excluded SimWorld code, introduce an interface or leave it
  in the adapter phase; do not silently copy the dependency into core.
- Preserve the existing fork, branches, receipts and accepted package until standalone acceptance.

## Testing Strategy

- Pure contract/compiler unit tests without SimWorld workspace imports.
- UE plugin dependency audit and compile/package validation.
- Blender/UE pipeline fixture tests with pinned external tools and offline mode.
- Packaged game launch/acceptance without Studio server.
- Adapter contract tests against a fake transport, then a pinned SimWorld integration smoke.
- License/secret/large-file scans before first public push and every release.

## Rollout and Observability

1. Draft/approve extraction spec.
2. Create standalone remote and protections.
3. Produce selected-path seed and baseline CI.
4. Accept standalone packaged runtime.
5. Cut SimWorld control-plane integration to the adapter package.
6. Build the VISTA World Control Center only after core interfaces stabilize.

Every phase publishes source SHA, test summary, package/receipt digests, known license limits and
rollback pin. Runtime/GPU evidence remains append-only outside Git.

## Traceability

- R1 -> repository model, rollout phases 1-3
- R2 -> selected-path history, attribution and receipt migration
- R3 -> standalone package layout and tests
- R4 -> `VistaWorldTransport` and adapters
- R5 -> versioned packages and release metadata
- R6 -> daily delivery loop
- R7 -> branch/worktree and integrator policy
- R8 -> compatibility adapter and rollback

## Open Questions

- Repository visibility and GitHub owner must be confirmed before remote creation.
- Decide whether `git filter-repo` is available/approved or a scripted replay is required.
- Branch protection settings depend on the account plan and repository visibility.

## Approval

- Requested by: Codex integrator
- Approved by:
- Date:
