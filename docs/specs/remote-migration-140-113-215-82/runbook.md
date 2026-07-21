# Runbook: continue VISTA production on 140.113.215.82

Updated: 2026-07-21 Asia/Taipei

Target: `yhliu@140.113.215.82`

Source: `git@github.com:IvesLiu1026/SimWorld-Studio.git`, branch
`codex/vista-production-completion`

## 0. Stop rule and remote-agent prompt

The last read-only SSH retry on 2026-07-21 failed with `No route to host`. No session was established
and no remote command ran. Do not infer current host state from the 2026-07-15 ledger. Start this
runbook only after the route is restored.

Give the remote Codex coordinator this prompt:

```text
You are the integration coordinator on 140.113.215.82. Read AGENTS.md and all
files under docs/specs/remote-migration-140-113-215-82, then the referenced
production-readiness runbooks. Preserve /home/yhliu/SimWorld-Studio-src and
/home/yhliu/SimWorld as historical dirty migration evidence.

Source sync is GitHub-only from IvesLiu1026/SimWorld-Studio branch
codex/vista-production-completion. Resolve and record the exact remote SHA;
never copy a dirty checkout and never edit the detached activation generation.

Run only the read-only inventory first. Every sudo/admin, model download,
database/index write, UE/plugin/scene mutation, provider call, service restart,
DNS/TLS/firewall/Coturn/public listener, or external test keeps its separate
gate. Do not print secrets. Keep assets/animation/review/WebRTC not_ready until
their exact live receipts exist.
```

## 1. Read-only first contact

The first recovered SSH session runs only these commands. Save output through the operator's approved
terminal capture; do not write inside Git, install anything, or start a service.

```bash
set -eu
export PATH="$HOME/.local/bin:$PATH"
date --iso-8601=seconds
hostname --fqdn
id
uname -a
sed -n '1,20p' /etc/os-release
git --version
node --version
npm --version
uv --version
claude --version
codex --version
nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader
free -h
df -h "$HOME" /tmp
findmnt -T "$HOME"
ss -lntup
systemctl --no-pager --type=service --state=running | grep -Ei 'simworld|unreal|cirrus|turn|coturn|nginx|docker|postgres|qdrant' || true
docker version || true
docker compose version || true
docker info || true
command -v vulkaninfo || true
find "$HOME/.local/share/simworld-studio" -maxdepth 3 -type d -print 2>/dev/null | sort
```

Inventory the migrated runtime without launching it:

```bash
RUNTIME="$HOME/.local/share/simworld-studio/binary/SimWorld-Studio-Minimal-806e869a"
test -x "$RUNTIME/Engine/Binaries/Linux/UnrealEditor"
test -f "$RUNTIME/gym_citynav/gym_citynav.uproject"
find "$RUNTIME/Engine/Build" -maxdepth 3 -name 'Build.version' -o -name 'RunUAT.sh' 2>/dev/null
find "$RUNTIME/gym_citynav/Plugins" -maxdepth 2 -name '*.uplugin' -print 2>/dev/null | sort
sha256sum "$RUNTIME/gym_citynav/gym_citynav.uproject"
```

Stop if the route is unstable, disk is insufficient, GPU/driver is unhealthy, a target path has unknown
ownership, or any intended port/GPU/service already has another owner. Do not kill or replace it.

## 2. Publish and materialize an exact GitHub checkpoint

The coordinator must first push the reviewed integration branch. On the target, create a new checkout
generation. These commands never use the historical dirty tree.

```bash
set -eu
export PATH="$HOME/.local/bin:$PATH"
export REPO_URL='git@github.com:IvesLiu1026/SimWorld-Studio.git'
export INTEGRATION_BRANCH='codex/vista-production-completion'
export CHECKOUT_ROOT="$HOME/.local/share/simworld-studio/checkouts"
export STATE_ROOT="$HOME/.local/state/simworld-studio"

install -d -m 0700 "$CHECKOUT_ROOT" "$STATE_ROOT/checkpoints"
REMOTE_LINE="$(git ls-remote --exit-code "$REPO_URL" "refs/heads/$INTEGRATION_BRANCH")"
REMOTE_SHA="$(printf '%s\n' "$REMOTE_LINE" | awk 'NR==1 {print $1}')"
printf '%s\n' "$REMOTE_SHA" | grep -Eq '^[0-9a-f]{40}$'

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PARTIAL="$CHECKOUT_ROOT/.partial-$STAMP"
CHECKOUT="$CHECKOUT_ROOT/$REMOTE_SHA"
test ! -e "$PARTIAL"
test ! -e "$CHECKOUT"

git clone --no-tags --single-branch --branch "$INTEGRATION_BRANCH" "$REPO_URL" "$PARTIAL"
test "$(git -C "$PARTIAL" rev-parse HEAD)" = "$REMOTE_SHA"
test "$(git -C "$PARTIAL" remote get-url origin)" = "$REPO_URL"
test -z "$(git -C "$PARTIAL" status --porcelain)"
git -C "$PARTIAL" fsck --full
git -C "$PARTIAL" switch --detach "$REMOTE_SHA"
mv "$PARTIAL" "$CHECKOUT"

umask 077
CHECKPOINT_FILE="$STATE_ROOT/checkpoints/source-$STAMP.env"
printf 'REPO_URL=%s\nINTEGRATION_BRANCH=%s\nCHECKPOINT=%s\nCHECKOUT=%s\n' \
  "$REPO_URL" "$INTEGRATION_BRANCH" "$REMOTE_SHA" "$CHECKOUT" > "$CHECKPOINT_FILE"
chmod 0600 "$CHECKPOINT_FILE"
printf 'CHECKPOINT=%s\nCHECKOUT=%s\nCHECKPOINT_FILE=%s\n' \
  "$REMOTE_SHA" "$CHECKOUT" "$CHECKPOINT_FILE"
```

Before continuing, compare `REMOTE_SHA` to the coordinator-announced reviewed SHA. If the branch moved
during clone, resolve again and create another generation; do not silently deploy an unreviewed HEAD.

For a remote-only code fix, never edit `CHECKOUT`:

```bash
WORK_BRANCH="codex/remote-82-<bounded-task>"
WORKTREE="$HOME/SimWorld-Studio-worktrees/remote-82-<bounded-task>"
git -C "$CHECKOUT" worktree add -b "$WORK_BRANCH" "$WORKTREE" "$REMOTE_SHA"
# Read rules, declare owned paths, edit, test, stage specific files, commit.
git -C "$WORKTREE" push --set-upstream origin "$WORK_BRANCH"
```

Wait for coordinator integration and create a new checkout generation from the updated integration
branch. Never scp/rsync the worktree as a release.

## 3. Ownership checkpoint before each stateful phase

Create one secret-free private record. Replace placeholders; do not record tokens, DSNs or credentials.

```bash
set -eu
PHASE='<ue-plugin|vista-stage|asset-index|scene|animation|review|webrtc>'
RUN_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
RUN_ROOT="$STATE_ROOT/checkpoints/$REMOTE_SHA-$PHASE-$RUN_STAMP"
install -d -m 0700 "$RUN_ROOT"
umask 077
printf 'schema=simworld-remote-ownership/v1\nphase=%s\ngit_sha=%s\ncheckout=%s\nowner=%s\ngpu=%s\nslot=%s\nports=%s\ngate_reference=%s\nrollback_generation=%s\n' \
  "$PHASE" "$REMOTE_SHA" "$CHECKOUT" '<human-or-agent>' '<none-or-index>' \
  '<none-or-slot>' '<none-or-list>' '<approval-record>' '<prior-generation>' \
  > "$RUN_ROOT/ownership.txt"
chmod 0600 "$RUN_ROOT/ownership.txt"
```

Only that owner starts/stops the recorded processes or writers. If ownership changes, close the old
checkpoint and create a new one.

## 4. Offline dependency and acceptance checks

These commands use the exact clean generation and do not contact UE/provider/DB. `npm ci` may access
the package registry if caches are incomplete; obtain normal dependency-download approval first.

```bash
set -eu
export PATH="$HOME/.local/bin:$PATH"
cd "$CHECKOUT/simworld_studio_workspace/web"
npm ci
cd server
npm ci
node --test tests/*.test.js
cd ..
npm run build

cd "$CHECKOUT"
uv sync --project tools --frozen
uv run --project tools --frozen python -m unittest discover -s tools/tests -p 'test_*.py' -v
node --test unreal_plugins/VistaAnimationContentApi/Tests/offline-contract.test.mjs
sh -n unreal_plugins/VistaAnimationContentApi/Scripts/install-plugin.sh
sh -n unreal_plugins/VistaAnimationContentApi/Scripts/build-plugin.sh
bash -n deploy/aws/scripts/slot-launcher.sh
git diff --check
git diff --exit-code -- \
  simworld_studio_workspace/web/package-lock.json \
  simworld_studio_workspace/web/server/package-lock.json \
  tools/uv.lock
test -z "$(git status --porcelain)"
```

Record exact totals and failures. Do not patch a detached generation. A passing suite proves code
contracts only; it does not satisfy any live gate.

## 5. Administrator prerequisite checklist

Ask the administrator for only the missing items demonstrated by Phase 1 inventory:

1. Network route/ACL permitting the approved SSH path.
2. `vulkan-tools` for `vulkaninfo`; render/video group access only if a real device permission error is
   captured.
3. An exact Linux UE 5.3.2 full build root containing executable
   `Engine/Build/BatchFiles/RunUAT.sh`, `Engine/Binaries/Linux/UnrealEditor`, headers and its expected
   compiler/toolchain. Install `clang`/`cmake`/`ninja` only if that build reports them missing.
4. Rootless Docker, an explicitly reviewed docker-group grant, or managed Postgres/Qdrant/embedding.
   Docker group is root-equivalent.
5. Private model/config/secret/backup directories with reviewed ownership and capacity.
6. Existing 80/443 ingress ownership plus DNS/TLS/Coturn/firewall decisions; never launch competing
   Nginx/Coturn first.

Verification after the admin change is still read-only:

```bash
vulkaninfo --summary
test -x "$UE_ENGINE_ROOT/Engine/Build/BatchFiles/RunUAT.sh"
test -x "$UE_ENGINE_ROOT/Engine/Binaries/Linux/UnrealEditor"
docker info
ss -lntup
```

## 6. Rebuild and load the UE plugin against 5.3.2

### 6.1 Exact-engine and offline checks

Set `UE_ENGINE_ROOT` to the administrator-provided full build, not automatically to the migrated runtime.

```bash
set -eu
export UE_ENGINE_ROOT='/absolute/admin-approved/UE_5.3.2'
export PLUGIN_ROOT="$CHECKOUT/unreal_plugins/VistaAnimationContentApi"
export PLUGIN_BUILD_ROOT="$HOME/.local/share/simworld-studio/plugin-packages"

test -x "$UE_ENGINE_ROOT/Engine/Build/BatchFiles/RunUAT.sh"
test -x "$UE_ENGINE_ROOT/Engine/Binaries/Linux/UnrealEditor"
test -f "$UE_ENGINE_ROOT/Engine/Build/Build.version"
node - "$UE_ENGINE_ROOT/Engine/Build/Build.version" <<'NODE'
const fs = require('node:fs');
const file = process.argv[2];
const v = JSON.parse(fs.readFileSync(file, 'utf8'));
const actual = `${v.MajorVersion}.${v.MinorVersion}.${v.PatchVersion}`;
if (actual !== '5.3.2') throw new Error(`expected UE 5.3.2, got ${actual}`);
process.stdout.write(`${actual}\n`);
NODE
node --test "$PLUGIN_ROOT/Tests/offline-contract.test.mjs"
```

### 6.2 BuildPlugin — state/admin gate

Dry-run first. The apply command compiles and may consume substantial CPU/disk; run it only after the
recorded gate.

```bash
CHECKPOINT12="$(printf '%s' "$REMOTE_SHA" | cut -c1-12)"
export VISTA_ANIMATION_PLUGIN_BUILD_ID="ue532-$CHECKPOINT12-build001"
PACKAGE="$PLUGIN_BUILD_ROOT/$VISTA_ANIMATION_PLUGIN_BUILD_ID"
test ! -e "$PACKAGE"

"$PLUGIN_ROOT/Scripts/build-plugin.sh" \
  --engine-root "$UE_ENGINE_ROOT" \
  --output "$PACKAGE" \
  --platform Linux
```

After reviewing the printed command and obtaining the State/Admin approval:

```bash
"$PLUGIN_ROOT/Scripts/build-plugin.sh" \
  --engine-root "$UE_ENGINE_ROOT" \
  --output "$PACKAGE" \
  --platform Linux \
  --apply 2>&1 | tee "$RUN_ROOT/build-plugin.log"

PLUGIN_BINARY="$(find "$PACKAGE" -type f -name 'libUnrealEditor-VistaAnimationContentApi.so' -print -quit)"
test -n "$PLUGIN_BINARY"
test -f "$PLUGIN_BINARY"
MANIFEST="$PACKAGE/vista-animation-plugin-artifact.json"
umask 077
node "$PLUGIN_ROOT/Scripts/create-artifact-manifest.mjs" \
  --binary "$PLUGIN_BINARY" \
  --build-id "$VISTA_ANIMATION_PLUGIN_BUILD_ID" \
  --engine-version 5.3.2 \
  --target-platform linux-x86_64 > "$MANIFEST"
chmod 0600 "$MANIFEST"
sha256sum "$PLUGIN_BINARY" "$MANIFEST"
```

This file is a build-output candidate, not yet the server trust anchor. The administrator must install an
independent protected copy, verify its raw-file SHA again, and point the service at that copy.

### 6.3 Disposable project and live load — separate UE State gate

Do not mutate the canonical archive or run `deploy/aws/scripts/slot-launcher.sh` unchanged: its default
project name/paths do not match this migrated runtime, and the target-specific launcher must first be
reviewed against the current slot registry, NOWRITE policy and project generation.

Create a writable disposable project generation only after capacity review. The package root is the
directory containing `VistaAnimationContentApi.uplugin`.

```bash
RUNTIME="$HOME/.local/share/simworld-studio/binary/SimWorld-Studio-Minimal-806e869a"
SOURCE_PROJECT="$RUNTIME/gym_citynav"
PROJECT_GENERATIONS="$HOME/.local/share/simworld-studio/ue-project-generations"
CONTENT_REVISION='<immutable-content-revision>'
PROJECT="$PROJECT_GENERATIONS/$REMOTE_SHA-$CONTENT_REVISION"
PARTIAL_PROJECT="$PROJECT.partial-$RUN_STAMP"
PACKAGED_UPLUGIN="$(find "$PACKAGE" -type f -name 'VistaAnimationContentApi.uplugin' -print -quit)"
PACKAGED_PLUGIN="$(dirname "$PACKAGED_UPLUGIN")"

test -f "$SOURCE_PROJECT/gym_citynav.uproject"
test -n "$PACKAGED_UPLUGIN"
test ! -e "$PARTIAL_PROJECT"
test ! -e "$PROJECT"
install -d -m 0700 "$PROJECT_GENERATIONS"
cp -a --reflink=auto "$SOURCE_PROJECT" "$PARTIAL_PROJECT"
test ! -e "$PARTIAL_PROJECT/Plugins/VistaAnimationContentApi"
install -d -m 0700 "$PARTIAL_PROJECT/Plugins/VistaAnimationContentApi"
cp -a "$PACKAGED_PLUGIN/." "$PARTIAL_PROJECT/Plugins/VistaAnimationContentApi/"
mv "$PARTIAL_PROJECT" "$PROJECT"
```

The project owner must review/commit the `.uproject` plugin enablement, private-listener exact dispatch,
and concrete `IVistaAnimationContentDriver` on a GitHub branch before live proof. Loading only the
abstract packaged plugin is not content readiness.

Use one registered slot owner to launch this exact project with `-RenderOffScreen -NOWRITE`, one GPU,
loopback-only MCP/Cirrus/UnrealCV and a unique state directory. Record PID start token and ports before
the process becomes ready. Acceptance checks:

```bash
grep -F 'VistaAnimationContentApi' "$RUN_ROOT/ue.log"
ss -lntup | grep -E '127\.0\.0\.1:<(recorded-mcp|cirrus|ucv-port)>'
sha256sum "$PLUGIN_BINARY" "$MANIFEST"
READY_HTTP="$(curl --silent --show-error --config "$CURL_AUTH_CONFIG" --cookie "$COOKIE_JAR" \
  --output "$RUN_ROOT/readiness.json" --write-out '%{http_code}' \
  "$STUDIO_BASE/api/health/ready")"
case "$READY_HTTP" in 200|503) ;; *) exit 1 ;; esac
```

The live nonce response must match plugin manifest, content profile, process instance, owner/session/
slot/lease/scene and all fixed operation fingerprints. Unknown `vista_animation_*` must terminal-reject;
mutation timeout/disconnect must not retry. Stop only the recorded process through the same lifecycle
owner and prove all recorded listeners are gone. Overall readiness may correctly remain HTTP `503` while
assets, content, Review or WebRTC gates are still open; inspect the animation component rather than
requiring an overall `200`.

## 7. Stage one authoritative VISTA sample

This phase is offline. The Data owner supplies an immutable verified projection and exact selected files.
The command defaults to dry-run and must be byte-identical when later approved with `--apply`.

```bash
set -eu
DATASET_ROOT='/data/VISTA_VERIFIED'
DATASET_REVISION='<immutable-verified-dataset-revision>'
BUNDLE_ROOT="$HOME/.local/share/simworld-studio/vista-import-bundles"
BUNDLE="$BUNDLE_ROOT/$DATASET_REVISION/mmg_040-attempt-007"
install -d -m 0700 "$BUNDLE_ROOT" "$BUNDLE_ROOT/$DATASET_REVISION"

cd "$CHECKOUT"
uv run --project tools --frozen python tools/stage_vista_import_bundle.py \
  --dataset-root "$DATASET_ROOT" \
  --verified-source verified/round1.jsonl \
  --verified-format jsonl \
  --dataset-revision "$DATASET_REVISION" \
  --sample-id mmg_040 \
  --provider sora2 \
  --attempt 7 \
  --render-script pipeline_v2/media/mmg_040/attempt_007/render_script.yaml \
  --dialogue-no-oracle verified/dialogue/mmg_040.attempt_007.no-oracle.json \
  --media pipeline_v2/media/mmg_040/attempt_007/video.mp4 \
  --output-dir "$BUNDLE" > "$RUN_ROOT/vista-stage-dry-run.json"
```

Review the `vista-import-staging-result/v1`: exact row/attempt identity, checksums/bytes, 12-second
duration, strictly increasing source timestamps, media dimensions, no-oracle join, restricted-field
absence, bundle digest and registry snippet. The dry-run must not create `BUNDLE`.

After Data/State approval, repeat the exact arguments with `--apply` and then verify:

```bash
uv run --project tools --frozen python tools/stage_vista_import_bundle.py \
  --dataset-root "$DATASET_ROOT" \
  --verified-source verified/round1.jsonl \
  --verified-format jsonl \
  --dataset-revision "$DATASET_REVISION" \
  --sample-id mmg_040 \
  --provider sora2 \
  --attempt 7 \
  --render-script pipeline_v2/media/mmg_040/attempt_007/render_script.yaml \
  --dialogue-no-oracle verified/dialogue/mmg_040.attempt_007.no-oracle.json \
  --media pipeline_v2/media/mmg_040/attempt_007/video.mp4 \
  --output-dir "$BUNDLE" \
  --apply > "$RUN_ROOT/vista-stage-apply.json"

test "$(stat -c '%a' "$BUNDLE")" = 700
find "$BUNDLE" -type f -printf '%m %p\n' | \
  awk '$1 != "600" { print; bad=1 } END { exit bad }'
test -z "$(find "$BUNDLE" -type l -print -quit)"
uv run --project tools --frozen python -m unittest tools.tests.test_stage_vista_import_bundle -v
```

A second apply must return `idempotent`. Install `registry-snippet.json` through protected service config;
never put a server path in the public import request. See
[the detailed staging runbook](../production-readiness/vista-raw-staging-runbook.md).

## 8. Provision Postgres/Qdrant/embedding and audit the snapshot

Follow [asset-stack-operations.md](../production-readiness/asset-stack-operations.md) in full. The
commands below are the phase checkpoints, not blanket authorization.

### 8.1 Pin model artifacts and run offline preflight

Model download/cache population and image pull/build require prior Data/Admin approval. Once immutable
directories exist, capture then verify their exact manifests:

```bash
cd "$CHECKOUT"
export ASSET_DB_DIR='/srv/simworld/asset-db'
export ASSET_SNAPSHOT_REVISION='<immutable-asset-snapshot-revision>'
export UE_CONTENT_REVISION='<immutable-ue-content-revision>'
export VISTA_UE_CONTENT_REVISION="$UE_CONTENT_REVISION"
export EMBED_DENSE_MODEL='BAAI/bge-large-en-v1.5'
export EMBED_SPARSE_MODEL='Qdrant/bm25'
export EMBED_DENSE_MODEL_DIR_HOST='/opt/simworld-models/dense/<artifact-revision>'
export EMBED_SPARSE_MODEL_DIR_HOST='/opt/simworld-models/sparse/<artifact-revision>'
export EMBED_DENSE_SIZE=1024

uv run --project tools --frozen python tools/embedding_model_artifact.py capture \
  --model-dir "$EMBED_DENSE_MODEL_DIR_HOST" --kind dense \
  --model-id "$EMBED_DENSE_MODEL" --dense-size "$EMBED_DENSE_SIZE"
uv run --project tools --frozen python tools/embedding_model_artifact.py capture \
  --model-dir "$EMBED_SPARSE_MODEL_DIR_HOST" --kind sparse \
  --model-id "$EMBED_SPARSE_MODEL"

export EMBED_DENSE_REVISION='sha256:<dense-artifact-manifest-digest>'
export EMBED_SPARSE_REVISION='sha256:<sparse-artifact-manifest-digest>'
uv run --project tools --frozen python tools/embedding_model_artifact.py verify \
  --model-dir "$EMBED_DENSE_MODEL_DIR_HOST" --kind dense \
  --model-id "$EMBED_DENSE_MODEL" --dense-size "$EMBED_DENSE_SIZE" \
  --revision "$EMBED_DENSE_REVISION"
uv run --project tools --frozen python tools/embedding_model_artifact.py verify \
  --model-dir "$EMBED_SPARSE_MODEL_DIR_HOST" --kind sparse \
  --model-id "$EMBED_SPARSE_MODEL" --revision "$EMBED_SPARSE_REVISION"
```

Set only approved pinned image digests and file-backed secrets; values below are identifiers/paths, not
secret contents:

```bash
export ASSET_STACK_PROFILE=local
export POSTGRES_IMAGE='postgres@sha256:<approved-digest>'
export QDRANT_IMAGE='qdrant/qdrant@sha256:<approved-digest>'
export ASSET_TOOLS_PYTHON_IMAGE='python@sha256:<approved-digest>'
export ASSET_TOOLS_UV_IMAGE='ghcr.io/astral-sh/uv@sha256:<approved-digest>'
export EMBED_SERVICE_IMAGE='<registry/image>@sha256:<approved-digest>'
export POSTGRES_DB=asset_db
export POSTGRES_USER=simworld
export POSTGRES_PASSWORD_FILE_HOST='/etc/simworld/secrets/postgres_password'
export POSTGRES_URL_FILE='/etc/simworld/secrets/postgres_url'
export QDRANT_API_KEY_FILE='/etc/simworld/secrets/qdrant_api_key'
export EMBED_SERVICE_TOKEN_FILE_HOST='/etc/simworld/secrets/embed_service_token'
export EMBED_SERVICE_TOKEN_FILE="$EMBED_SERVICE_TOKEN_FILE_HOST"
export EMBED_VERSION='<immutable-embedding-recipe-version>'
export QDRANT_URL='http://127.0.0.1:6333'
export QDRANT_COLLECTION='<immutable-snapshot-specific-collection>'
export EMBED_SERVICE_URL='http://127.0.0.1:7777'
export ASSET_BACKUP_ROOT='/srv/backups/simworld/asset-stack'
export ASSET_BACKUP_RETENTION_DAYS=30
export ASSET_BACKUP_MIN_FREE_BYTES=107374182400

uv run --project tools --frozen python tools/asset_stack_preflight.py \
  --output "$ASSET_DB_DIR/deployment-preflight.json"
docker compose --profile asset-stack config --quiet
```

Preflight `ready_for_admin_gates` is only offline coherence.

### 8.2 Start/migrate/index — Admin/Data/State gate

Only inside the approved maintenance window:

```bash
docker compose --profile asset-stack up -d
docker compose --profile asset-stack ps

POSTGRES_URL_FILE="$POSTGRES_URL_FILE" \
  uv run --project tools --frozen python tools/apply_schema.py

uv run --project tools --frozen python tools/migrate_to_postgres.py \
  --asset-db-dir "$ASSET_DB_DIR" \
  --snapshot-revision "$ASSET_SNAPSHOT_REVISION" \
  --dry-run
```

Review exact catalog count. Then obtain the separate import approval and remove only `--dry-run`:

```bash
POSTGRES_URL_FILE="$POSTGRES_URL_FILE" \
  uv run --project tools --frozen python tools/migrate_to_postgres.py \
  --asset-db-dir "$ASSET_DB_DIR" \
  --snapshot-revision "$ASSET_SNAPSHOT_REVISION" \
  --fail-fast

POSTGRES_URL_FILE="$POSTGRES_URL_FILE" \
QDRANT_API_KEY_FILE="$QDRANT_API_KEY_FILE" \
QDRANT_URL='http://127.0.0.1:6333' \
QDRANT_COLLECTION='<immutable-collection-name>' \
EMBED_DENSE_MODEL_PATH="$EMBED_DENSE_MODEL_DIR_HOST" \
EMBED_SPARSE_MODEL_PATH="$EMBED_SPARSE_MODEL_DIR_HOST" \
  uv run --project tools --frozen python tools/build_qdrant_index.py --dry-run
```

Review pending count and model/dimension/revision identity. Approve the full embedding/index cost, then
rerun the same command without `--dry-run`. Do not use `--force` unless a separately reviewed rebuild
requires it.

Prove the pinned Qdrant/embed services reject unauthenticated requests and accept the secret-backed
health/query path. Do not paste keys/tokens into curl argv; use a mode-`0600` curl config or service test.

### 8.3 Live audit and restore

After one matching Blueprint and one StaticMesh PBR UE probe succeeds:

```bash
export QDRANT_URL='http://127.0.0.1:6333'
export QDRANT_COLLECTION='<immutable-collection-name>'
export EMBED_SERVICE_URL='http://127.0.0.1:7777'

uv run --project tools --frozen python tools/verify_asset_snapshot.py capture \
  --output "$ASSET_DB_DIR/snapshot-manifest.json" \
  --receipt-output "$ASSET_DB_DIR/snapshot-live-audit.json" \
  --receipt-ttl-seconds 300
uv run --project tools --frozen python tools/verify_asset_snapshot.py verify \
  --manifest "$ASSET_DB_DIR/snapshot-manifest.json" \
  --receipt-output "$ASSET_DB_DIR/snapshot-live-audit.json" \
  --receipt-ttl-seconds 300 \
  --replace
```

Pin the printed snapshot ID, manifest SHA and live receipt SHA in protected deployment config. Complete
the exact backup bundle and disposable restore commands in `asset-stack-operations.md`, then repeat the
live audit against the restored services. Until that passes, keep assets `not_ready`.

## 9. Typed SceneSpec -> BuildPlan -> disposable UE proof

Prerequisites: active asset live receipt, production layout profile with no fixture/demo/BasicShapes
surface, approved disposable UE generation, one current Studio slot and protected auth/session files.

Use a mode-`0600` curl config (`CURL_AUTH_CONFIG`) containing the Studio Authorization header; do not put
the token in argv. Use a mode-`0600` cookie jar. The following request JSON contains no secret:

```bash
set -eu
export STUDIO_BASE='http://127.0.0.1:3002'
export CURL_AUTH_CONFIG='/run/user/<uid>/simworld-curl-auth.conf'
export COOKIE_JAR="$RUN_ROOT/studio.cookies"
chmod 0600 "$CURL_AUTH_CONFIG"
umask 077

curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie-jar "$COOKIE_JAR" --request POST "$STUDIO_BASE/api/session/acquire" \
  > "$RUN_ROOT/session-acquire.json"

printf '%s\n' '{"datasetRevision":"<immutable-verified-dataset-revision>","sampleId":"mmg_040","attempt":7,"scenarioType":"multimodal_grounded"}' \
  > "$RUN_ROOT/import-request.json"

curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" --cookie-jar "$COOKIE_JAR" \
  --header 'Content-Type: application/json' \
  --data-binary "@$RUN_ROOT/import-request.json" \
  "$STUDIO_BASE/api/vista/imports/preview" > "$RUN_ROOT/import-preview.json"

curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" --cookie-jar "$COOKIE_JAR" \
  --header 'Content-Type: application/json' \
  --data-binary "@$RUN_ROOT/import-request.json" \
  "$STUDIO_BASE/api/vista/imports" > "$RUN_ROOT/import-commit.json"

IMPORT_ID="$(node -e 'const x=require(process.argv[1]); if(!x.artifact_id) process.exit(2); process.stdout.write(x.artifact_id)' "$RUN_ROOT/import-commit.json")"
```

Inspect the preview/commit before planning. All required assets must resolve to the active snapshot.

```bash
curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" --header 'Content-Type: application/json' \
  --data-binary '{}' \
  "$STUDIO_BASE/api/vista/imports/$IMPORT_ID/build/plan" > "$RUN_ROOT/build-plan.json"
PLAN_ID="$(node -e 'const x=require(process.argv[1]); if(!x.plan?.plan_id) process.exit(2); process.stdout.write(x.plan.plan_id)' "$RUN_ROOT/build-plan.json")"

printf '{"plan_id":"%s"}\n' "$PLAN_ID" > "$RUN_ROOT/build-preflight-request.json"
curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" --header 'Content-Type: application/json' \
  --data-binary "@$RUN_ROOT/build-preflight-request.json" \
  "$STUDIO_BASE/api/vista/imports/$IMPORT_ID/build/preflight" > "$RUN_ROOT/build-preflight.json"
```

Require `ready:true`, exact Blueprint/StaticMesh paths, material/PBR/content evidence and zero fallback.
Then obtain the Scene State gate and execute only the exact plan:

```bash
printf '{"plan_id":"%s","confirm":true}\n' "$PLAN_ID" > "$RUN_ROOT/build-execute-request.json"
curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" --header 'Content-Type: application/json' \
  --data-binary "@$RUN_ROOT/build-execute-request.json" \
  "$STUDIO_BASE/api/vista/imports/$IMPORT_ID/build/execute" > "$RUN_ROOT/build-execute.json"

curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" "$STUDIO_BASE/api/vista/imports/$IMPORT_ID/build" \
  > "$RUN_ROOT/build-status.json"
```

Acceptance requires a single scene digest across actor snapshot, collision/floating reports and screenshot;
all actor classes/assets and every material slot are verified `/Game` paths with current content receipt.
Then test backend lifecycle, never browser toolbar coordinates:

```bash
curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" --header 'Content-Type: application/json' --data-binary '{}' \
  "$STUDIO_BASE/api/vista/setup_vista_play_mode" > "$RUN_ROOT/pie-start.json"
curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" "$STUDIO_BASE/api/vista/get_vista_state" > "$RUN_ROOT/pie-state-live.json"
curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" --header 'Content-Type: application/json' --data-binary '{}' \
  "$STUDIO_BASE/api/vista/stop_vista_play_mode" > "$RUN_ROOT/pie-stop.json"
curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" "$STUDIO_BASE/api/vista/get_vista_state" > "$RUN_ROOT/pie-state-stopped.json"
```

Verify confirmed possession/live state and confirmed stopped/ended PIE. Also run stale-plan, revoked-lease,
mesh/material/content-receipt drift and rollback probes; they must fail before unsafe continuation.

## 10. Character driver and 12-second timeline

This phase cannot begin with only the abstract plugin. The project content owner must first publish:

- reviewed `IVistaAnimationContentDriver` implementation;
- verified pawn/skeleton/AnimBP or Control Rig;
- hand/foot anchors, draggable chair/caster physics;
- look-at, brace, drag, lift-foot, pause, directional fall and explicit recover assets/notifies;
- immutable `vista-animation-content-profile/v1` and receipt;
- root-owned UE 5.3.2 plugin artifact manifest and exact hashes.

Configure the service with all required fields together:

```bash
export VISTA_ANIMATION_TIMELINE_ENABLED=1
export VISTA_ANIMATION_CONTENT_PROFILE_FILE='/etc/simworld/config/vista-animation-content-profile.json'
export VISTA_ANIMATION_CONTENT_PROFILE_SHA256='<exact-file-sha256>'
export VISTA_ANIMATION_UE_PLUGIN_ARTIFACT_FILE='/etc/simworld/config/vista-animation-plugin-artifact.json'
export VISTA_ANIMATION_UE_PLUGIN_ARTIFACT_SHA256='<exact-file-sha256>'
export VISTA_ANIMATION_RECORD_ROOT='/var/lib/simworld/vista-animation'
export VISTA_ANIMATION_UE_PROBE_TIMEOUT_MS=5000
```

With the exact scene still built and the same active lease, run preflight:

```bash
printf '{"plan_id":"%s"}\n' "$PLAN_ID" > "$RUN_ROOT/animation-preflight-request.json"
curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" --header 'Content-Type: application/json' \
  --data-binary "@$RUN_ROOT/animation-preflight-request.json" \
  "$STUDIO_BASE/api/vista/imports/$IMPORT_ID/animation/preflight" \
  > "$RUN_ROOT/animation-preflight.json"

PREFLIGHT_ID="$(node -e 'const x=require(process.argv[1]); if(!x.preflight_id)process.exit(2); process.stdout.write(x.preflight_id)' "$RUN_ROOT/animation-preflight.json")"
TIMELINE_ID="$(node -e 'const x=require(process.argv[1]); if(!x.timeline_id)process.exit(2); process.stdout.write(x.timeline_id)' "$RUN_ROOT/animation-preflight.json")"
PROGRAM_ID="$(node -e 'const x=require(process.argv[1]); if(!x.program_id)process.exit(2); process.stdout.write(x.program_id)' "$RUN_ROOT/animation-preflight.json")"
```

Inspect that every event/action is verified and the plugin/content/process/lease revisions are exact. After
the Animation State gate, explicitly confirm all four IDs:

```bash
printf '{"plan_id":"%s","preflight_id":"%s","timeline_id":"%s","program_id":"%s","confirm":true}\n' \
  "$PLAN_ID" "$PREFLIGHT_ID" "$TIMELINE_ID" "$PROGRAM_ID" \
  > "$RUN_ROOT/animation-start-request.json"
curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" --header 'Content-Type: application/json' \
  --data-binary "@$RUN_ROOT/animation-start-request.json" \
  "$STUDIO_BASE/api/vista/imports/$IMPORT_ID/animation/start" \
  > "$RUN_ROOT/animation-start.json"
ANIMATION_RUN_ID="$(node -e 'const x=require(process.argv[1]); if(!x.run_id)process.exit(2); process.stdout.write(x.run_id)' "$RUN_ROOT/animation-start.json")"

curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" \
  "$STUDIO_BASE/api/vista/imports/$IMPORT_ID/animation/runs/$ANIMATION_RUN_ID" \
  > "$RUN_ROOT/animation-status.json"
```

For the Stop test, use only the exact current lease/run:

```bash
curl --fail-with-body --silent --show-error --config "$CURL_AUTH_CONFIG" \
  --cookie "$COOKIE_JAR" --header 'Content-Type: application/json' --data-binary '{}' \
  "$STUDIO_BASE/api/vista/imports/$IMPORT_ID/animation/runs/$ANIMATION_RUN_ID/stop" \
  > "$RUN_ROOT/animation-stop.json"
```

Acceptance is not merely terminal `completed`: evidence must cover 0/2/5/9/12 seconds, pose, hand/foot
contact, chair interaction/casters, fall collision, explicit recovery/root alignment, screenshot, engine
time/drift, terminal scene validation, confirmed cleanup and ended PIE. Repeat in disposable generations
for notify timeout, Stop race, disconnect/outcome-unknown and restart/recovery-required. Never retry a
mutation after uncertain delivery.

## 11. Exactly two real Review provider smokes

Follow [review-provider-smoke-runbook.md](../production-readiness/review-provider-smoke-runbook.md).
Before either command, obtain approval for exactly two tool-free `claude-opus-4-8` calls, no retry,
USD 0.05 maximum each / USD 0.10 total, 120-second timeout and the listed token ceilings.

Use shared-broker canonical before/after scene digests. A Visual digest change is terminal failure.

```bash
cd "$CHECKOUT/simworld_studio_workspace/web"
umask 077
BUILD_REVISION="$(git rev-parse HEAD)"
CLAUDE_VERSION="$(claude --version | awk 'NR==1 {print $1}')"
BEFORE_SHA='<shared-broker-scene-before-sha256>'
AFTER_SHA='<shared-broker-scene-after-sha256>'

node server/review-provider-smoke-cli.js \
  --review-type text --provider claude --model claude-opus-4-8 \
  --build-revision "$BUILD_REVISION" \
  --scene-digest-before "$BEFORE_SHA" --scene-digest-after "$AFTER_SHA" \
  --prompt-file /managed/evidence/mmg_040-review-request.txt \
  --image /managed/evidence/mmg_040-text.png \
  --cli-name claude-code --cli-version "$CLAUDE_VERSION" \
  --max-budget-usd 0.05 --timeout-ms 120000 \
  --max-input-tokens 50000 --max-output-tokens 2048 \
  --receipt-ttl-seconds 3600 \
  --receipt /run/simworld/review-smoke-text.json

node server/review-provider-smoke-cli.js \
  --review-type visual --provider claude --model claude-opus-4-8 \
  --build-revision "$BUILD_REVISION" \
  --scene-digest-before "$BEFORE_SHA" --scene-digest-after "$AFTER_SHA" \
  --prompt-file /managed/evidence/mmg_040-review-request.txt \
  --image /managed/evidence/mmg_040-visual.png \
  --cli-name claude-code --cli-version "$CLAUDE_VERSION" \
  --max-budget-usd 0.05 --timeout-ms 120000 \
  --max-input-tokens 50000 --max-output-tokens 2048 \
  --receipt-ttl-seconds 3600 \
  --receipt /run/simworld/review-smoke-visual.json
```

No retry. Pin both receipt paths and exact file SHA-256 values with provider/model/build in protected
service config. A fake/single/expired/mismatched receipt must leave review `not_ready`.

## 12. DNS/TLS/Coturn/firewall and forced relay

Follow [webrtc-coturn-runbook.md](../production-readiness/webrtc-coturn-runbook.md). This phase is entirely
Admin/Public/State gated. First record decisions for:

- `studio.<domain>` and `turn.<domain>` ownership;
- existing 80/443 ingress and certificate renewal;
- Coturn public/private IP, realm, REST/HMAC secret, quota/monitoring;
- public 3478 UDP/TCP, 5349 TLS and approved bounded UDP relay range;
- separate IP/LB if TURN/TLS must use 443;
- at least two external client networks and credential-rotation mode.

After administrators install secrets outside Git, materialize/validate Coturn and same-origin gateway
using the exact integration generation:

```bash
cd "$CHECKOUT"
node deploy/aws/scripts/materialize-coturn-config.js \
  --template deploy/aws/templates/coturn.conf \
  --output /etc/turnserver.conf
nginx -t
ss -lntup
```

The actual materializer consumes administrator-provided environment/secret-file paths. Never pass the
shared secret value on argv. Public listeners may be only managed ingress and Coturn; Node/Cirrus
HttpPort/StreamerPort/SFU/MCP/UnrealCV remain loopback and externally unreachable.

Run one live E2E proving authenticated WSS `101`, streamer registration, advancing frames, keyboard/mouse
data channel, reconnect, copied-path denial and cross-slot denial. Then, from at least two external
network classes, run normal ICE and forced UDP/TCP/TLS relay for at least 12 minutes per required row.
Store only the redacted schema—never SDP, IP, port, TURN username/password, cookie, token or opaque path.

After normal/forced-relay and an approved live credential-rotation drill, create the bounded receipt:

```bash
cd "$CHECKOUT/simworld_studio_workspace/web/server"
node webrtc-readiness-cli.js \
  --input /secure/evidence/webrtc-probes.redacted.json \
  --expect-build-revision "$REMOTE_SHA" \
  --expect-deployment-fingerprint "$WEBRTC_DEPLOYMENT_FINGERPRINT" \
  --expect-origin "$STUDIO_PUBLIC_ORIGIN" \
  --expect-certificate-fingerprint "$WEBRTC_CERTIFICATE_SHA256" \
  --output /secure/evidence/webrtc-readiness-receipt.json
```

Pin the receipt's raw-file SHA with exact build/deployment/origin/certificate. Browser telemetry or
`PUBLIC_WEBRTC_EXTERNAL_VERIFIED` cannot replace this external receipt.

## 13. Release, rollback, and Git checkpoint commands

Before any activation, record prior code/plugin/project/content/model/index/config generations and exact
owned PIDs/services. Activation points at a verified immutable generation; it never edits one in place.

Rollback rules:

- Source/test failure: leave current active generation unchanged; rename only the new owned generation
  to `.failed-<timestamp>` if needed.
- UE/plugin/scene/timeline: Stop through the exact lease/lifecycle owner, verify PID start token, end PIE,
  release ports, preserve evidence, then select the prior project/plugin generation.
- Asset stack: stop only approved writers; restore prior backup to disposable DB/Qdrant first and repeat
  live audit before switching. Never relabel a partial index.
- Review: record safe failure code; no automatic retry and no receipt.
- WebRTC: close only newly approved rules/listeners, restore prior pinned config/secret generation, rerun
  listener and forced-relay audits.

Never use broad `pkill`, delete an unknown path, kill a port owner, `git reset --hard`, or overwrite the
historical dirty snapshots.

Remote development handoff uses GitHub:

```bash
git -C "$WORKTREE" status --short --branch
git -C "$WORKTREE" diff --check
git -C "$WORKTREE" add <specific-owned-files>
git -C "$WORKTREE" commit -m '<type>: <one logical change>'
git -C "$WORKTREE" push --set-upstream origin "$WORK_BRANCH"
```

The coordinator reviews/cherry-picks or merges to `codex/vista-production-completion`, runs the full
suite, pushes a new integration checkpoint, and announces its exact SHA. The target then repeats Phase 2
and creates a new clean generation. Do not hardcode or predict the eventual branch HEAD.

Production-ready may be declared only when every unchecked live/admin/data/cost/public task in
[tasks.md](tasks.md) has current evidence and rollback/user/admin sign-off.
