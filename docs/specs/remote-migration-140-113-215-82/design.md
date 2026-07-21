# Design: SimWorld migration to 140.113.215.82

Status: Approved for staged execution  
Updated: 2026-07-15  
Depends on: `requirements.md`

## Summary

Migration採五個可獨立驗證的unit，而不是複製整個`/home/yhliu`：Studio dirty source、Python/bridge dirty source、canonical packaged UE runtime archive、sanitized run evidence、以及新建的operator state/config roots。每個unit先進`.partial-20260715`，通過checksum/parity後才promote。Dependencies與secrets不複製，分別由lockfile重建及遠端重新建立。

首次啟動只做loopback、model-off、asset fail-closed smoke。DB、真實model、public WebRTC與system integration保留為後續gate。

## Observed environment

### Source host

| Unit | Current source | Observation |
| --- | --- | --- |
| Studio source | `/home/yhliu/SimWorld-Studio-src` | 約1.1 GiB含dependencies/evidence；branch `codex/vista-loopback`; HEAD `caf6d930...`; migration spec加入後為48 tracked modified + 69 untracked files；0 staged |
| Python/bridge | `/home/yhliu/SimWorld` | 約802 MiB含308 MiB venv；`main`; HEAD `0921180909105158a7ff87445eb032706b10113e`; 5 tracked modified + 104 untracked；實驗影片約443 MiB |
| UE runtime tree | `/mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/runtime/SimWorld-Studio-Minimal-806e869a` | 約21 GiB；包含Engine與`gym_citynav` project |
| UE canonical archive | `/mnt/NAS2/yhliu/SimWorldStudio/0.2.0-806e869a/downloads/SimWorld-Studio-Minimal.tar.gz` | 15,170,703,068 bytes；SHA-256 `806e869a...990e2f` |
| Accepted run | `/home/yhliu/SimWorld-Studio-runs/20260714T120039-simworld-opus` | model-off Studio +一個已核准Opus call；scene為NOWRITE live state，非持久化asset |

### Target host

| Area | Observation | Consequence |
| --- | --- | --- |
| Compute | Ubuntu 24.04, 2×RTX 5090 32 GiB, 125 GiB RAM, 670 GiB free | 足以執行binary與雙slot，但先單GPU smoke |
| Node | system v18.19.1；`~/.local/bin/node` v22.23.1 | 所有runbook先prepend user PATH |
| Agents | Claude Code 2.1.209、Codex CLI 0.144.4在`~/.local/bin`; Hermes absent | Codex可接手；不要假設Hermes或provider profile已登入 |
| Docker | Docker 29/Compose 5 installed；user無daemon權限 | Postgres/Qdrant/embed需admin或rootless/managed替代 |
| Vulkan | NVIDIA ICD/libs存在；`vulkaninfo`缺 | 可以傳binary；UE launch前保留Vulkan smoke/admin gate |
| Network | 80/443/14500 occupied；3002/55559/6333/7777/8585/8586 free at audit | 只用loopback free ports；public ingress另案整合 |
| SimWorld | source/runtime/data均不存在 | 可安全使用新partial/canonical paths |

## Target layout

```text
/home/yhliu/
  SimWorld-Studio-src/                         # dirty Studio checkout, .git included
  SimWorld/                                    # dirty Python/bridge checkout, .git included
  .config/simworld-studio/                     # non-secret operator config
  .local/share/simworld-studio/
    downloads/
      SimWorld-Studio-Minimal-806e869a.tar.gz
    binary/
      SimWorld-Studio-Minimal-806e869a/
    evidence/
      20260714T120039-simworld-opus/
    asset-db/                                  # empty until snapshot/data gate
  .local/state/simworld-studio/
    logs/
    slots/0/
    vista-imports/
    secrets/                                   # mode 0700 dir; files mode 0600
```

Temporary paths append `.partial-20260715`; promotion usessame-filesystem `mv` only after validation.

## Transfer inclusion and exclusion

### Studio source

Include `.git`, tracked files, current tracked modifications, and untracked source/spec/tests. Exclude only disposable or local-sensitive content:

```text
**/node_modules/**
**/.venv/**
**/__pycache__/**
**/.pytest_cache/**
**/.ruff_cache/**
**/dist/**
**/build/**
simworld_studio_workspace/logs/**
simworld_studio_workspace/tmp/**
simworld_studio_workspace/web/test-results/**
simworld_studio_workspace/web/playwright-report/**
simworld_studio_workspace/web/.runtime/**
.claude/settings.local.json
.codex/config.toml
.env
.env.*
*.pem
*.key
```

Primary transfer is a three-layer snapshot:

1. `git bundle --all` preserves committed objects, local branch and the 12 commits not present on GitHub.
2. `git diff --binary HEAD` preserves the 48 tracked but unstaged modifications.
3. A reviewed tar from `git ls-files --others --exclude-standard` preserves 69 untracked files, including this migration package.

Do not use `git archive`: it would discard the dirty layer. Do not use `git apply --index`: source modifications were not staged. Do not use `git pull` on target before recording parity. Direct rsync is only a fallback and must apply the exclusion list above without `--delete`.

### Python/bridge repo

Include `.git`, modified package source, `docs/`, `scripts/`, `ue_plugin/`, and `experiments/` includingofficial reference videos. Exclude `.venv/`, Python caches, `dist/`, `*.egg-info/`, `wget-log*`, local `.env*`, keys and credentials.

### Unreal runtime

Transfer the canonical archive as one immutable file. Do not rsync a filtered Engine tree. Verify exact byte count and SHA-256 before extracting. Extract into a new partial directory and verify at minimum:

- `Engine/Binaries/Linux/UnrealEditor` executable exists;
- `gym_citynav/gym_citynav.uproject` exists;
- Pixel Streaming `SignallingWebServer/cirrus.js` exists;
- runtime root size is plausible (source observation approximately21 GiB).

The archive contains one top-level directory named`SimWorld-Studio-Minimal/`. Extract with`--no-same-owner` into a user-owned partial directory, verify it, then rename that top-level directory to`SimWorld-Studio-Minimal-806e869a` during promotion.

The archive contains stock Cirrus SHA-256`92298e881c9240ebe76adfdf0fda39310cc28f5fd5934070194940cca7be29a4`, which must not be exposed or launched as the final signalling service. After an explicit runtime state-change gate, apply`tools/patch_cirrus_loopback.py`; expected patched SHA-256 is`133a12cf843c69914263a41c3ea3d7f09914ad9241125358850ea0318e55300e` and the tool must produce its receipt.

### Evidence

Include `run-metadata.json`, `evidence/`, final screenshots, acceptance/audit artifacts, `prompt.txt`, `system-prompt.txt` and source receipt. Exclude raw provider stream/stderr, run-local `mcp-config.json`, any token-bearing config, copied `node_modules`, caches and live PIDs.

## Configuration mapping

| Logical value | Target value |
| --- | --- |
| `SIMWORLD_REPO` | `/home/yhliu/SimWorld-Studio-src` |
| `SIMWORLD_WEB_DIR` | `/home/yhliu/SimWorld-Studio-src/simworld_studio_workspace/web` |
| `UE_ENGINE_DIR` | `/home/yhliu/.local/share/simworld-studio/binary/SimWorld-Studio-Minimal-806e869a` |
| `UE_EDITOR` | `$UE_ENGINE_DIR/Engine/Binaries/Linux/UnrealEditor` |
| `UE_PROJECT_FILE` | `$UE_ENGINE_DIR/gym_citynav/gym_citynav.uproject` |
| `CIRRUS_JS` | `$UE_ENGINE_DIR/Engine/Plugins/Media/PixelStreaming/Resources/WebServers/SignallingWebServer/cirrus.js` |
| Studio | `127.0.0.1:3002` |
| MCP | `127.0.0.1:55559` |
| Cirrus HTTP/Streamer | `127.0.0.1:8585` / `127.0.0.1:8586` |
| State | `/home/yhliu/.local/state/simworld-studio` |
| Asset data | `/home/yhliu/.local/share/simworld-studio/asset-db` |

`web/mcp.json` is source input only. Runtime shall generate a config with target host/ports; it shall not copy a local run token or hardcoded3022/55570 value.

## Activation flow

1. Validate target paths absent and ports free.
2. Materialize source units from bundle + binary patch + reviewed untracked tar intopartial paths; verify Git status parity.
3. Rsync archive into a`.partial` filename; verify size/SHA; rename.
4. Extract into `binary/.extracting-20260715`; verify fixed files; rename.
5. Prepend`~/.local/bin`; run `npm ci` forweb andserver; use `uv` for Python only afteruv is provisioned.
6. Run offline unit/contract/UI/build suite.
7. Create a fresh random Studio token remotely; keep model mode off.
8. Launch single GPU/slot with isolated state, loopback Cirrus/MCP/Studio, no public ingress.
9. Verify MCP/health/streaming/read-only screenshot and controlledstop.
10. Only then open separate tasks for asset services, real review, timeline/UE adapters and public WebRTC.

## Security and secret handling

- Never rsync `$HOME/.claude*`, `$HOME/.codex`, `$HOME/.ssh`, `.env*`, provider logs or database dumps without an explicit reviewed manifest.
- The existing user-local Claude/Codex installation may use remote host login state already present; migration does not inspect or depend on it.
- `STUDIO_ACCESS_TOKEN` is generated on target and never printed in docs/logs.
- Public browser access remains SSH tunnel or existing authenticated ingress after an admin-ownedproxy change; direct `0.0.0.0:3002` is forbidden.
- Asset retrieval stays fail-closed until a matching `simworld-asset-snapshot/v1` is audited.

## Failure handling and rollback

- Transfer interruption: rerun same rsync into the samepartial path with `--partial`; never remove source.
- Parity failure: keep partial path, write comparison report, do not promote.
- Extract failure: remove only `.extracting-20260715` after confirming it is the migration-owned path; retain archive.
- Runtime smoke failure: terminate only recorded migration PIDs/tmux session; keep all files for diagnostics.
- Port conflict: choose a new approved loopback slot; do not kill unknown listener.
- Full rollback: rename canonical migration paths to`.failed-<timestamp>`; do not touch original host or target 80/443/14500.

## Testing strategy

### Offline, no UE/provider/DB

- `npm ci` using committed lockfiles.
- Relevant `node --test` contracts, existing server unit suite, UI unit suite.
- `npx vite build --mode development`.
- Review and VISTA Import Playwright mock E2E.
- JSON schema checks and `mmg_040` 0/2/5/9/12 timeline golden tests.

### Local UE smoke, stateful gate

- NVIDIA/Vulkan preflight.
- Single UE process, single Cirrus, single Studio; all loopback.
- MCP ready, authenticated health, decoded Pixel Streaming frame.
- Read-only screenshot and fixed VISTA setup/state/stop contract.
- Clean stop with no remaining owned listener/PID.

### Deferred admin/network tests

- Docker or managed Postgres/Qdrant/embed and audited snapshot.
- Existing 80/443 ingress WSS proxy, Coturn, firewall and forced-relay test.
- Systemd/restart/backup/restore.

## Tradeoffs

- Bundle + patch + reviewed untracked tar is chosen over committing a mixed checkpoint because the current changes span several logical units and staging them would violate repository discipline. It preserves provenance without inventing a commit or copying host-specific dependency trees.
- Canonical archive transfer is chosen over filtered runtime-tree rsync because it is independently checksummed, smaller in transit and less likely to omit Engine/Content files.
- Dependencies are rebuilt because copying host-specific `node_modules`/venvs is slower, less auditable and can carry ABI/path incompatibility.

## Traceability

- MIG-001/002/006 -> partial-path rsync, `.git` retention, parity checks.
- MIG-003/004/007 -> target layout, archive extraction, dependency rebuild.
- MIG-005 -> exclusion policy and target-generated secrets.
- MIG-008/009 -> fail-closed readiness and staged testing.
- MIG-010/011 -> admin matrix, runbook and handoff.
- MIG-012 -> rollback and no-impact rules.
