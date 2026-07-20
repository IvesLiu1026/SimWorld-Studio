# Runbook: remote SimWorld continuation

Updated: 2026-07-15
Target: `yhliu@140.113.215.82`

## Start here for the remote Codex agent

Use GPT-5.6 Sol Ultra as coordinator if that profile is available, but verify the actual CLI/model configuration instead of assuming it from the prompt. Begin with:

```text
You are the migration integrator on 140.113.215.82.
Read /home/yhliu/SimWorld-Studio-src/AGENTS.md and every file under
/home/yhliu/SimWorld-Studio-src/docs/specs/remote-migration-140-113-215-82/.
Then read docs/specs/production-readiness/{requirements,design,tasks}.md.

The dirty filesystem state is intentional and is the source of truth. Do not
git pull, reset, checkout, clean, stage, commit, reformat broadly, or discard
untracked files. Do not use sudo, alter Docker groups, touch ports 80/443/14500,
copy or print secrets, call a paid model, start a public listener, mutate UE
content, or run asset indexing without the corresponding explicit gate.

First execute only the read-only preflight and offline validation sections of
this runbook. Record evidence in HANDOFF.md before asking to run the stateful UE
smoke. Use /home/yhliu/.local/bin before the system PATH.
```

## Known target facts

- Host: Ubuntu 24.04 x86_64, 2×RTX5090, NVIDIA driver580.105.08.
- RAM:125 GiB; free disk at audit: approximately670 GiB.
- User Node22.23.1/npm10.9.8: `/home/yhliu/.local/bin`.
- Codex/Claude are installed in`~/.local/bin`; Hermes was not found. Migration installed the verified user-localuv0.11.0 binary and rebuilt the Python environment.
- Docker/Compose binaries exist but`yhliu` cannot access the daemon.
-80/443/14500 are occupied. Do not kill or replace those listeners.
- At audit,3002/55559/6333/7777/8585/8586 were free; recheck before launch.

## Read-only preflight

```bash
export PATH="$HOME/.local/bin:$PATH"
hostname
id
node --version
npm --version
git --version
nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader
df -h "$HOME"
ss -ltn
```

Expected source/runtime paths after transfer:

```bash
test -d /home/yhliu/SimWorld-Studio-src/.git
test -d /home/yhliu/SimWorld/.git
test -f /home/yhliu/.local/share/simworld-studio/downloads/SimWorld-Studio-Minimal-806e869a.tar.gz
test -x /home/yhliu/.local/share/simworld-studio/binary/SimWorld-Studio-Minimal-806e869a/Engine/Binaries/Linux/UnrealEditor
test -f /home/yhliu/.local/share/simworld-studio/binary/SimWorld-Studio-Minimal-806e869a/gym_citynav/gym_citynav.uproject
```

## Integrity checks

```bash
stat -c '%s %n' /home/yhliu/.local/share/simworld-studio/downloads/SimWorld-Studio-Minimal-806e869a.tar.gz
sha256sum /home/yhliu/.local/share/simworld-studio/downloads/SimWorld-Studio-Minimal-806e869a.tar.gz
git -C /home/yhliu/SimWorld-Studio-src branch --show-current
git -C /home/yhliu/SimWorld-Studio-src rev-parse HEAD
git -C /home/yhliu/SimWorld-Studio-src status --short --branch
git -C /home/yhliu/SimWorld status --short --branch
```

Expected archive:

```text
bytes  15170703068
sha256 806e869ad1c65b298f05a39854b28e4188bb50817f539744451849e054990e2f
```

Expected Studio identity:

```text
branch codex/vista-loopback
HEAD   caf6d9309ad4fe256a6ba1e212d8bb1fb1fa7f7b
dirty  48 tracked modified + 69 untracked in the accepted migration snapshot
```

Do not demand a clean status. A clean target would mean progress was lost.

## Source materialization from the migration snapshot

The transferred snapshot root is recorded in`HANDOFF.md`. Materialize Studio without staging the dirty patch:

```bash
MIGRATION_ROOT=/home/yhliu/SimWorld-Migration/20260715T163851-simworld-to-140-113-215-82
STUDIO_PARTIAL=/home/yhliu/SimWorld-Studio-src.partial-20260715
test ! -e /home/yhliu/SimWorld-Studio-src
test ! -e "$STUDIO_PARTIAL"
git clone "$MIGRATION_ROOT/studio/studio-all.bundle" "$STUDIO_PARTIAL"
git -C "$STUDIO_PARTIAL" switch codex/vista-loopback
git -C "$STUDIO_PARTIAL" apply --check "$MIGRATION_ROOT/studio/studio-dirty-tracked.patch"
git -C "$STUDIO_PARTIAL" apply "$MIGRATION_ROOT/studio/studio-dirty-tracked.patch"
tar --extract --gzip --file="$MIGRATION_ROOT/studio/studio-untracked.tar.gz" --directory="$STUDIO_PARTIAL" --no-same-owner
git -C "$STUDIO_PARTIAL" status --short --branch
```

After comparing against the status/manifest in`HANDOFF.md`, promote with one same-filesystem rename:

```bash
mv "$STUDIO_PARTIAL" /home/yhliu/SimWorld-Studio-src
```

Materialize the Python/bridge repo the same way using`simworld/simworld-all.bundle`,`simworld-dirty-tracked.patch` and`simworld-untracked.tar.gz`. Expected base HEAD is`0921180909105158a7ff87445eb032706b10113e`. Do not repair the misspelled upstream URL until parity is recorded.

## Offline dependency rebuild

These commands write only dependency/build directories and make no provider/UE/DB call:

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /home/yhliu/SimWorld-Studio-src/simworld_studio_workspace/web
npm ci
cd server
npm ci
```

After installation, verify lockfiles did not change:

```bash
git -C /home/yhliu/SimWorld-Studio-src diff -- \
  simworld_studio_workspace/web/package-lock.json \
  simworld_studio_workspace/web/server/package-lock.json
```

The Python repo has legacy`setup.py` rather than`pyproject.toml`. Do not copy the old Python3.10 venv. Afteruv is installed at user scope or approved by the administrator:

```bash
cd /home/yhliu/SimWorld
uv venv --python 3.12
uv pip install -e '.[dev]'
uv run python -c 'import simworld; print(simworld.__file__)'
```

## Offline validation

```bash
export PATH="$HOME/.local/bin:$PATH"
cd /home/yhliu/SimWorld-Studio-src/simworld_studio_workspace/web
npm run test:server:unit
node --test tests/ui/review-runtime.unit.mjs tests/ui/vista-import.unit.mjs
npx vite build --mode development
npx playwright test --config tests/ui/review.playwright.config.js
npx playwright test --config tests/ui/vista-import.playwright.config.js
```

Run the exact accepted server contract selection from the repository root; do not add `ue-broker-integration.test.js` unless its local mock port is intentionally owned:

```bash
cd /home/yhliu/SimWorld-Studio-src
set -- $(git ls-files --others --exclude-standard \
  simworld_studio_workspace/web/server/tests | \
  grep '\.test\.js$' | sort) \
  simworld_studio_workspace/web/server/tests/runtime-security.test.js \
  simworld_studio_workspace/web/server/tests/security-source-parity.test.js \
  simworld_studio_workspace/web/server/tests/vista-runtime-broker.test.js
node --test "$@"
```

For this snapshot the command expands to24 files and230 tests. Broader legacy tests are diagnostic only; see `HANDOFF.md` for the two current portability failures.

Expected migrated baseline from source host:

- Relevant Node contracts:230 passed.
- Existing server unit:11 passed,18 integration/pipeline skipped.
- UI unit:11 passed.
- Mock browser E2E: Review1 + VISTA Import1 passed.
- Vite development build:1879 modules.
- Six Draft2020-12 schemas and two sanitized fixtures validated.

Record differences rather than forcing the output to match.

## Runtime environment contract

Set paths explicitly; do not reuse source-host absolute paths:

```bash
export PATH="$HOME/.local/bin:$PATH"
export SIMWORLD_REPO=/home/yhliu/SimWorld-Studio-src
export SIMWORLD_WEB_DIR="$SIMWORLD_REPO/simworld_studio_workspace/web"
export UE_ENGINE_DIR=/home/yhliu/.local/share/simworld-studio/binary/SimWorld-Studio-Minimal-806e869a
export UE_EDITOR="$UE_ENGINE_DIR/Engine/Binaries/Linux/UnrealEditor"
export UE_PROJECT_FILE="$UE_ENGINE_DIR/gym_citynav/gym_citynav.uproject"
export CIRRUS_JS="$UE_ENGINE_DIR/Engine/Plugins/Media/PixelStreaming/Resources/WebServers/SignallingWebServer/cirrus.js"
export XDG_DATA_HOME=/home/yhliu/.local/share
export XDG_STATE_HOME=/home/yhliu/.local/state
export PORT=3002
export UNREAL_HOST=127.0.0.1
export UNREAL_PORT=55559
export CIRRUS_HTTP_PORT=8585
export CIRRUS_WS_PORT=8586
export STUDIO_MODEL_MODE=off
export ASSET_REQUIRE_REAL_ASSETS=true
export ASSET_DEGRADED_MODE=disabled
```

Generate`STUDIO_ACCESS_TOKEN` remotely and store it outside Git. Never paste it into`HANDOFF.md`, shell history or issue text.

## Stateful UE smoke — requires an explicit continuation decision

Before launch:

1. Recheck ports and unknown process ownership.
2. Confirm no other agent owns runtime lifecycle.
3. Create one unique tmux session and one slot state directory.
4. Use GPU0 only.
5. Keep Studio, MCP and Cirrus loopback-only.
6. Keepmodel-off,`-NOWRITE`,`-RenderOffScreen`, isolated`Saved`/`Intermediate` and state/log directories.

Do not use `deploy/aws/scripts/slot-launcher.sh` unchanged. It assumes`SimWorld.uproject`, `/opt`/`/var/lib` paths, writes an older Cirrus config, and does not yet integrate the newprocess/port and opaque-endpoint registries. Adapt it in a reviewed change or create a bounded user-owned launcher. The extracted archive contains stock Cirrus; after explicit state-change approval apply`tools/patch_cirrus_loopback.py`, verify patched SHA`133a12cf843c69914263a41c3ea3d7f09914ad9241125358850ea0318e55300e`, and retain its receipt before any Cirrus launch.

Minimum acceptance sequence:

1. Cirrus HTTP and Streamer listeners are only`127.0.0.1:8585/8586`.
2. UE starts and MCP becomes ready only on`127.0.0.1:55559`.
3. Studio starts only on`127.0.0.1:3002` with access guard.
4. Unauthenticated Studio/Cirrus request is denied.
5. Authenticated health identifies UE/MCP correctly; readiness keepsassets not-ready.
6. Browser receives decoded Pixel Streaming frames through an SSH tunnel or approved existing ingress.
7. Read-only screenshot works.
8. Fixed VISTA setup/state/stop contract works; no arbitrary Python input.
9. Stop all migration-owned processes and confirm the three port families arefree.

Do not call Claude or create the scene during this smoke.

## Gates for the remote administrator

Request only what is needed:

1. Install`vulkan-tools` for`vulkaninfo` and diagnose NVIDIA ICD; add`render` group only if a real DRM permission failure is demonstrated.
2. Choose eitherrootless Docker, reviewed`docker` group access, or managed Postgres/Qdrant/embed services. Docker group is root-equivalent and must be an explicit decision.
3. Reuse the existing80/443 ingress owner for HTTPS/WSS; do not launch a competing Nginx. Coturn/firewall/relay ports need a separate network design.
4. Installclang/cmake/ninja only if the team decides to compile UE; the packaged runtime path does not require an engine source build.

## Cost and mutation gates

- Real Claude/Codex/other provider call: explicit cost approval, exact model, max budget and evidence retention policy.
- Scene mutation or timeline action: disposable/NOWRITE scene and exact adapter allowlist.
- Asset index/database import: snapshot revision, dry-run counts and admin/data approval.
- Public WebRTC: ingress/TLS/Coturn/firewall approval plus external normal/forced-relay tests.

## Rollback

Only stop PIDs recorded by the migration-owned tmux/slot. Never use broad`pkill` or kill a listener just because its port conflicts. For file rollback, rename migration paths to`.failed-<timestamp>` after verifying ownership. Leave the source host untouched until remote acceptance is signed off.
