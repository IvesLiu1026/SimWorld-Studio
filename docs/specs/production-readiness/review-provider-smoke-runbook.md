# Text / Visual Review Provider Smoke Runbook

Status: code-ready; no provider call has been made by this change.

This runbook covers the cost-gated production proof required by `T1A.11`.
It does not authorize a model call, a UE mutation, or a production deploy.

The offline `T1A.10` gate is complete: a real HTTP coordinator harness covers
Text and Visual success, independent cost budgets, 401/429/500/timeout and
malformed-SSE failures, exact cancellation, and cross-lease isolation. In a
trusted-proxy deployment the coordinator derives its internal Review scope
from the server-validated active Studio lease; caller-provided session IDs do
not grant cancellation or state access. This fake-provider evidence does not
replace the two approved live calls below.

## What the runner proves

`server/review-provider-smoke-cli.js` invokes the existing Claude review
adapter with all of the following controls:

- explicit provider, model, immutable build revision, and pre/post scene
  SHA-256 digests;
- a fresh, bounded `claude --version` measurement of the exact configured
  binary; the operator-supplied CLI name/version are expectations only and a
  mismatch fails before any provider request;
- one to four bounded PNG/JPEG evidence files and a bounded review request;
- no tools, safe mode, an empty MCP configuration, no session persistence,
  strict JSON schema output, and no permission bypass;
- an independent wall-clock timeout, maximum dollar budget, and
  cache-inclusive input/output token ceilings;
- exact provider/model identity checking and `PASS`-only receipt publication;
- Visual Review pre/post scene digest equality;
- atomic `0600` receipt creation only after every check passes.

The receipt intentionally contains only provider/model/CLI/build identity,
timestamps, scene digests, usage totals, and compact verdict counts. It does
not contain the prompt, images, image paths, provider prose, stderr, access
tokens, or API credentials.

## Required evidence preparation

Use a disposable scene and the process-wide shared UE broker. Do not open a
second UE socket from an ad-hoc script. The trusted coordinator must capture a
canonical actor/light/ground snapshot immediately before the provider call and
again after it. Hash each snapshot with the repository implementation:

```bash
cd /path/to/SimWorld-Studio/simworld_studio_workspace/web
node -e 'const fs=require("node:fs"); const {digestReviewScene}=require("./server/review-smoke-receipt"); const value=JSON.parse(fs.readFileSync(process.argv[1],"utf8")); process.stdout.write(digestReviewScene(value)+"\n")' /managed/evidence/scene-before.json
```

Repeat for `scene-after.json`. The snapshot contract must include the same
actor transforms, lights, sky, ground, and relevant camera state on both
sides. Capturing a screenshot is read-only; moving a camera or rebuilding
lighting to obtain the screenshot invalidates the smoke.

The in-process API is preferred for live orchestration. The following is an
integration sketch; `captureCanonicalReviewSnapshot` must be the deployment's
shared-broker snapshot adapter, not a second socket client:

```js
const { createReviewProviderSmokeRunner } = require("./server/review-provider-smoke");

const runner = createReviewProviderSmokeRunner({
  providerAdapter,
  captureSceneDigestAfter: async ({ signal }) => {
    const snapshot = await captureCanonicalReviewSnapshot({
      ueBroker: sharedUeBroker,
      signal,
    });
    return digestReviewScene(snapshot);
  },
});
```

That callback is invoked only after the provider returns. The standalone CLI
accepts the two explicit digests for an already controlled evidence workflow;
a Visual receipt is production-valid only when those digests came from the
trusted shared-broker sequence above.

Before requesting cost approval, verify:

1. The disposable scene is stable and no build/timeline job is active.
2. The evidence screenshot is inside the managed evidence directory and is at
   most 8 MiB.
3. The review request file contains only the expected scene criteria.
4. The current commit is the exact build that will consume the receipt.
5. `claude --version` reports the expected CLI version supplied to the command.
   The smoke runner independently repeats this measurement and records only
   the measured identity.
6. The receipt destination is outside the repository and has a `0700` parent
   directory.

## Cost gate and exact live commands

Ask for this bounded approval before running either command:

> Approve exactly two tool-free Claude review calls using
> `claude-opus-4-8`: one Text smoke and one read-only Visual smoke, with no
> automatic retry and a hard ceiling of USD 0.05 per call / USD 0.10 total.

After approval, substitute only the managed evidence paths and the two
shared-broker digests. Keep credentials in the existing Claude credential
store or allowlisted provider environment; never add a credential flag.

```bash
cd /path/to/SimWorld-Studio/simworld_studio_workspace/web
umask 077
BUILD_REVISION="$(git rev-parse HEAD)"
BEFORE_SHA="<shared-broker-scene-before-sha256>"
AFTER_SHA="<shared-broker-scene-after-sha256>"

node server/review-provider-smoke-cli.js \
  --review-type text \
  --provider claude \
  --model claude-opus-4-8 \
  --build-revision "$BUILD_REVISION" \
  --scene-digest-before "$BEFORE_SHA" \
  --scene-digest-after "$AFTER_SHA" \
  --prompt-file /managed/evidence/mmg_040-review-request.txt \
  --image /managed/evidence/mmg_040-text.png \
  --cli-name claude-code \
  --cli-version 2.1.215 \
  --max-budget-usd 0.05 \
  --timeout-ms 120000 \
  --max-input-tokens 50000 \
  --max-output-tokens 2048 \
  --receipt-ttl-seconds 3600 \
  --receipt /run/simworld/review-smoke-text.json

node server/review-provider-smoke-cli.js \
  --review-type visual \
  --provider claude \
  --model claude-opus-4-8 \
  --build-revision "$BUILD_REVISION" \
  --scene-digest-before "$BEFORE_SHA" \
  --scene-digest-after "$AFTER_SHA" \
  --prompt-file /managed/evidence/mmg_040-review-request.txt \
  --image /managed/evidence/mmg_040-visual.png \
  --cli-name claude-code \
  --cli-version 2.1.215 \
  --max-budget-usd 0.05 \
  --timeout-ms 120000 \
  --max-input-tokens 50000 \
  --max-output-tokens 2048 \
  --receipt-ttl-seconds 3600 \
  --receipt /run/simworld/review-smoke-visual.json
```

There is no automatic retry. A timeout, provider error, non-JSON response,
schema error, non-`PASS` verdict, missing/over-budget usage, CLI/provider/model identity mismatch,
or Visual scene change exits non-zero and writes no new receipt.

## Readiness binding

Production readiness requires both independently generated receipts from the
same pinned provider/model/CLI/build. Point the service at both approved files:

```bash
export REVIEW_TEXT_SMOKE_RECEIPT_PATH=/run/simworld/review-smoke-text.json
export REVIEW_TEXT_SMOKE_RECEIPT_SHA256="$(sha256sum "$REVIEW_TEXT_SMOKE_RECEIPT_PATH" | awk '{print $1}')"
export REVIEW_VISUAL_SMOKE_RECEIPT_PATH=/run/simworld/review-smoke-visual.json
export REVIEW_VISUAL_SMOKE_RECEIPT_SHA256="$(sha256sum "$REVIEW_VISUAL_SMOKE_RECEIPT_PATH" | awk '{print $1}')"
export CRITIC_PROVIDER=claude
export CRITIC_MODEL=claude-opus-4-8
export CRITIC_MAX_BUDGET_USD=0.05
export SIMWORLD_BUILD_REVISION="$(git rev-parse HEAD)"
```

`/health/ready` opens each receipt with `O_NOFOLLOW`, verifies a stable bounded
read and its deployment-pinned raw-file SHA-256, and remains not-ready if either
file is absent, expired, malformed,
credential-like, bound to another provider/model/build/type/CLI, or reports a
non-`PASS` verdict or a mutated Visual scene. The single-receipt
`REVIEW_SMOKE_RECEIPT_PATH` remains available only for non-production policy
checks; it cannot satisfy Production Text + Visual readiness. Do not use
`REVIEW_READINESS_VERIFIED=1`; it is legacy metadata and is deliberately
rejected as proof.
Production review requests cannot override the pinned provider/model or raise
the deployment budget cap; a caller may only request a lower per-call budget.

## Retention and incident handling

- Receipts expire after one hour in the commands above and never after more
  than 24 hours.
- Keep screenshots and scene snapshots in the managed evidence store under
  its normal TTL and access policy; they are not copied into the receipt.
- If a provider invocation fails, record only the safe error code and the
  correlation/run ID in operational logs. Do not paste stderr or credentials.
- If pre/post Visual digests differ, stop. Inspect the shared broker trace and
  scene diff; do not override or hand-edit a passing receipt.
