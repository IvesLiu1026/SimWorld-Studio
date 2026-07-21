# Review CLI identity and internal-auth checkpoint

Date: 2026-07-22

Status: offline/code evidence passed; live Text and Visual provider calls remain
cost-gated and were not executed.

## What was revalidated

- `index.js` resolves the Studio access token once from the direct value or
  configured token file and injects that server-side value into both Review
  handlers.
- Text and Visual inner `/api/chat` requests send the token only as a Bearer
  header to numeric loopback. It is not copied into the request body, URL,
  browser storage, provider argv, or provider child environment.
- Trusted-proxy inner calls are rebound to the active server-validated Studio
  lease; caller-supplied session IDs do not authorize the nested builder.
- Fake-provider HTTP coverage proves Text and Visual success plus 401, 403,
  429, 500, timeout, malformed SSE, cancellation, and cross-lease isolation.

The earlier `Studio access token required` report is therefore not reproduced
by this branch's current auth chain. A live process that still returns that
message is either running an older checkout/configuration or was started
without the same token source consumed by the Studio process.

## Evidence-integrity fix

The production smoke CLI previously accepted `--cli-name` and `--cli-version`
and copied those caller-provided strings into a passing receipt. The provider
binary itself was never asked to prove that identity, so a receipt could claim
an arbitrary CLI version.

The smoke runner now:

1. executes only `<configured Claude binary> --version` in the same allowlisted
   provider environment;
2. bounds time, stdout, stderr, abort, and termination behavior;
3. requires the exact `N.N.N (Claude Code)` identity format;
4. treats the command-line name/version as approved expectations;
5. fails before the paid provider call when measured and expected identities
   differ; and
6. writes only the freshly measured identity into the receipt.

This check does not send a prompt, resolve a model, consume provider tokens, or
touch UE.

## Local read-only observation

- configured executable resolved by the shell: `/home/yhliu/.local/bin/claude`
- measured identity through the repository adapter:
  `{"name":"claude-code","version":"2.1.215"}`
- authentication preflight: logged in through `claude.ai`, Team subscription;
  no account identity, credential, or token value was recorded

## Validation

```text
node --test server/tests/review-provider.test.js server/tests/review-provider-smoke.test.js
36 tests passed, 0 failed, 0 skipped

node --test server/tests/internal-auth-propagation.test.js \
  server/tests/review-loop-http.test.js \
  server/tests/review-provider.test.js \
  server/tests/review-provider-smoke.test.js \
  server/tests/review-coordinator.e2e.test.js \
  server/tests/review-evidence-readiness.test.js \
  server/tests/review-evidence-route.test.js \
  server/tests/review-run-registry.test.js \
  server/tests/review-scene-binding.test.js \
  server/tests/review-smoke-receipt.test.js
138 tests passed, 0 failed, 0 skipped

node --test --test-reporter=tap server/tests/*.test.js
700 tests passed, 0 failed, 0 skipped

npx vite build --mode development
1,879 modules transformed; build passed
```

## Remaining gate

This checkpoint does not prove that `claude-opus-4-8` is enabled for the Team
account or that real Text and Visual receipts pass. `T1A.11` remains open until
the user approves exactly two bounded, tool-free provider calls and both
receipts validate against the same immutable build, provider, model, measured
CLI identity, and read-only scene evidence.
