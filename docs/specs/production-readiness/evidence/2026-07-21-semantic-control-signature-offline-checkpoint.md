# Semantic control and approval-signature offline checkpoint

Date: 2026-07-21

Branch: `codex/semantic-production-adapter`

Parent checkpoint: `852edb4c`

## Scope and resource boundary

This change is code, contract, and offline test evidence only. It owned no GPU,
runtime, database, provider, UE lease, or listener. A separate demo task owns
GPU 1 and loopback ports `3012/55570/8595/8596/8899`; this work did not inspect,
bind, restart, or terminate those resources. It did not touch VISTA Production
port `8000`. No paid call, database connection/mutation, semantic-index job, UE
mutation, public listener, or Production registration occurred.

## Implemented offline boundary

- `semantic_index_approval_verification.py` verifies exact canonical approval
  receipts with real Ed25519, caller-supplied UTC time, explicit trust keys,
  exact issuer/role/key/trust/environment/basis/scope/authorization pins, and
  bounded validity windows. Digest-only phase-evidence v1 still fails closed.
- `semantic_index_approval_aggregate.py` accepts only six exact opaque verified
  results and six independent expected-pin objects. It requires the complete
  purpose set, enforces one exact Production build binding, rebinds the opaque
  fields to the canonical signed raw receipt, and emits a deterministic proof
  digest without raw receipt or signature bytes.
- `semantic_index_control_ledger.py` publishes immutable prepare/result records
  beneath a caller-opened private directory FD. It binds job, plan, generation,
  nonce, run, lease, slot and worker pins independently from the control request,
  supports exact replay, and never blind-retries an incomplete mutation.
- `semantic_index_isolated_executor.py` runs one already-selected callback in a
  fresh session/process group, closes inherited descriptors, enforces the
  request's absolute monotonic deadline, bounds canonical output, kills the
  complete group, and boundedly reaps the leader on every exit path.
- `semantic_index_worker_service.py` now has an isolated candidate path for both
  authenticated phase and control sessions. It durably prepares before the
  child callback, validates and commits exact canonical result bytes, and only
  then sends those committed bytes. Legacy same-process fixture service remains
  phase-only and rejects control requests.

## Focused verification

The following tests passed in the uv-managed runtime and, where optional crypto
is unavailable, directly under isolated system Python:

- worker service: 27/27 in both runtimes;
- approval verifier plus aggregate: 24/24 under uv; 24/24 with 13 expected
  crypto-dependent skips under `/usr/bin/python3 -I -S`;
- control ledger: 20/20 in both runtimes;
- isolated executor: 10/10 in both runtimes, plus 20 consecutive uv runs of
  the focused file after the descendant-FD close race fix;
- changed-file Ruff and `git diff --check`: passed.

The complete uv-managed tools regression passed 467 tests with 20 intentional
platform-dependent skips. `uv lock --check` also passed.

The one-shot control-session test observes the durable control ledger as
`replay` from inside the response-send hook and then confirms the peer receives
the exact committed frame followed by EOF. Replay tests patch the isolation
entry point to fail if it is called again, proving committed requests do not
refork.

## What this does not prove

This checkpoint is not Production-ready and does not satisfy PA-01 through
PA-21:

1. The approval aggregate proof has no release-pinned formal schema and is not
   signed into launcher, execution-state, or terminal receipts. It cannot grant
   launch authority.
2. Phase-evidence v1 still contains only signature digests. No detached worker
   signature bytes, signer/trust identity, or public-key verification exists.
3. Control pins must ultimately be constructed from sealed launcher/plan state;
   constructing them from request echoes would collapse the boundary.
4. The isolated executor accepts an already-selected callback. A Production
   worker still needs a fixed reviewed operation-to-executor dispatch that does
   not expose callback injection.
5. The static Production adapter registry remains empty. The existing adapter
   schema intentionally requires `production_capable=false`; a separately
   reviewed registered revision is required.
6. There are no seven real phase executors, reproducible semantic worker image,
   recalculated transitive source closure, authoritative complete snapshot, or
   disposable PostgreSQL/Qdrant/UE/provider evidence.

The repository does contain a real query runtime for Postgres/Qdrant/embedding.
It also contains legacy index-build CLIs, but the v2 specification explicitly
forbids importing, wrapping, or delegating to them: their overwrite behavior is
not generation-safe. New exact generation executors must be implemented behind
the v2 worker protocol.
