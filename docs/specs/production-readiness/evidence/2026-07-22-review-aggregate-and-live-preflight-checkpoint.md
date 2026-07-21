# Review Aggregate Accounting and Authorized Live Preflight Checkpoint

Date: 2026-07-22

Branch: `codex/semantic-production-adapter`

Worktree: `/home/yhliu/SimWorld-Studio-worktrees/semantic-production-adapter`

## Scope and authorization

The user authorized continuing the bounded GPU 0 live run and the T1A.11
provider smoke. The provider scope is exactly one Text and one read-only Visual
`claude-opus-4-8` call, with no retry and the limits in
`review-provider-smoke-runbook.md`. It does not authorize a public listener,
database/index mutation, Production deployment, canonical VISTA/NAS mutation,
or use of GPU 1 and its separately owned ports.

This checkpoint completed the Review accounting implementation and performed a
read-only launch preflight. The preflight failed two mandatory gates, so it did
not start UE, Cirrus, the Studio server, or a model provider. No paid call was
sent.

## Pushed implementation

- `0164feb1` — adds REVIEW-011 and the aggregate summarizer design/task.
- `eda2b2f7` — adds the shared summarizer stage to the Review budget and both
  Text/Visual workflows.
- `c8fb7b98` — commits verified terminal accounting before abort, timeout, or
  process-failure settlement.
- `58e5a546` — also parses a complete final JSON record without a trailing
  newline before failure settlement.

The resulting contract is:

1. Summarizer, builder, and critic usage/cost share one per-run snapshot.
2. Summarizer admission happens before builder mutation or Visual capture.
3. Its CLI cap is floored to four decimal places and cannot exceed aggregate
   run-remaining money.
4. Only a proven pre-provider spawn failure can use the deterministic intent
   fallback. A possible provider attempt with unknown accounting stops before
   builder work.
5. Known paid failure or overspend is recorded before the Review fails.
6. A valid terminal usage/cost result is submitted at most once across close,
   abort, timeout, process-error, and late-close races. Missing/invalid
   accounting, callback failure, zero terminal records, and duplicate terminal
   records remain fail closed.

Independent P0/P1 review found no remaining high-priority issue after the
no-trailing-newline fix.

## Validation

From `simworld_studio_workspace/web`:

```text
node -c server/llm-oneshot.js
node --test server/tests/review-provider.test.js \
  server/tests/review-intent-stage.test.js \
  server/tests/review-budget.test.js \
  server/tests/review-loop-http.test.js \
  server/tests/review-coordinator.e2e.test.js
```

Result: 101 tests passed, 0 failed.

```text
node --test --test-reporter=tap server/tests/*.test.js
```

Result: 733 tests passed, 0 failed.

```text
npx vite build --mode development
```

Result: 1,879 modules transformed and the development build completed.
`git diff --check` also passed.

These are code/contract results. They do not constitute a real provider,
rendered scene, or Production deployment receipt.

## Read-only live preflight

Only GPU 0 and loopback ports `3010/55560/8585/8586/8889` were inspected.

```text
uid=1000021(yhliu) gid=1000001(users)
/dev/dri/renderD128 owner=root group=render mode=660
render_access=blocked
sandbox_auth_root=not_configured
GPU 0: NVIDIA RTX A6000, 20 MiB, 0% utilization
owned_ports=idle
Claude Code 2.1.215
ue_binary=present
ue_project=present
```

The current login has no `render` group membership. This matches the earlier
fresh GPU 0 failure at `VK_ERROR_INCOMPATIBLE_DRIVER`; starting the same Vulkan
path again would only repeat a known failure and would not produce a visible
scene or screenshot.

## Required external prerequisites

1. An administrator must grant `yhliu` access to the host `render` group for
   `/dev/dri/renderD128` (for a local account the conventional command is
   `sudo usermod -aG render yhliu`; directory-backed accounts may require the
   site's identity-management method). Then terminate every existing SSH login
   for this user and create a fresh login so the supplementary group is active.
2. Provision a service-owned, mode-`0700` `AGENT_SANDBOX_AUTH_ROOT` with a
   private `claude/` subtree containing only the dedicated Studio Claude
   service credential/configuration. Do not copy the human user's complete
   `~/.claude` directory.
3. Re-run render/Vulkan and auth-root preflight. Only after both pass should the
   owned UE/Pixel Streaming stack start, capture a managed PNG, bind exact
   before/after scene digests, and consume the two-call smoke budget.

After UE starts, the Mac client can reach the loopback services through an SSH
tunnel such as:

```text
ssh -L 3010:127.0.0.1:3010 -L 8585:127.0.0.1:8585 yhliu@<server>
```

T1A.11 therefore remains open. This checkpoint proves the code path and names
the two external gates; it does not claim that UE, WebRTC, Claude Opus 4.8, the
semantic index, animation playback, or Production readiness is live.
