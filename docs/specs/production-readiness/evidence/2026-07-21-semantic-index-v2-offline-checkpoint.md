# Semantic-index v2 offline checkpoint

Date: 2026-07-21

Branch: `codex/semantic-production-adapter`

Base checkpoint: `6b0d5046`

## Scope and safety boundary

This is code and offline contract evidence only. The work item owned no GPU,
runtime, database, UE lease, provider, or network listener. It did not inspect,
bind, restart, or terminate GPU 1 or loopback ports
`3012/55570/8595/8596/8899`, which belong to a separate demo task. It did not
touch VISTA Production port `8000`. No paid provider call, public listener,
PostgreSQL/Qdrant mutation, semantic-index job, UE mutation, or Production
adapter registration occurred.

## Closed contract surface

- 21 release-pinned formal schemas, including
  `simworld-semantic-index-launcher-verification-receipt/v1`.
- 20 schema digests in the explicitly unregistered adapter manifest.
- 24 semantic schemas accepted by the local closed-subset runtime.
- Seven raw phase requests and seven phase-evidence documents are bound to
  exact operation revisions, request digests, ledger identity, and ledger
  revision.
- Launcher receipt verification uses a caller-supplied exact Ed25519 trust map,
  expected pins, and explicit UTC time. A standalone verified signed receipt
  cannot project local handoff truth. Only an exact receipt-to-handoff digest
  composite that has transferred socket ownership can project the state and
  terminal `*_handoff_verified=true` fields.
- `ReceivedWorkerRequest` is sentinel-created, slotted, exact-type checked,
  non-subclassable, and non-copyable. Direct or uninitialized session objects
  fail before service execution or response send.
- The pathless durable ordinary-phase ledger publishes immutable prepare and
  result records with no-replace semantics. A visible entry whose earlier
  directory `fsync` failed is re-`fsync`ed before recovery/replay truth is
  returned.
- The ordinary-phase service validates an exact independently supplied ledger
  revision string, prepares before callback execution, commits the validated
  exact result before send, transmits those committed canonical bytes directly,
  and never blindly re-executes an incomplete entry.

Pinned raw schema hashes at this checkpoint:

| Schema | SHA-256 |
| --- | --- |
| execution plan v2 | `45c1165e1983a4ff9ef83012045f4de936d71c32bd85e867ad5058621a95f2c5` |
| unregistered adapter v2 | `9aaba0d7eaa7bcbc3a6a8dcd8edbce06004600afa7b2694607b4938f0d75c27b` |
| launcher verification receipt v1 | `e396e1ac1bc29f81a43ae19fab569e707e6296ecd32915fc80c47a938032ad2b` |

## Verification results

The following commands ran without starting application services:

```bash
cd tools
uv lock --check
uv run --group dev python -m unittest discover -s tests -p 'test_*.py'
```

Result: 405 tests passed; 20 tests were intentionally skipped because the
uv-managed Python lacks the Linux `memfd` sealing API set required by those
paths.

The pure-stdlib and fail-closed groups were also run directly under
`/usr/bin/python3 -I -S`: Production contracts 16/16, adapter 8/8, schema
runtime 13/13 with one optional differential skip, durable ledger 18/18,
worker service 19/19, worker protocol 37/37 with one root-capability skip,
launcher handoff 20/20 with one root-capability skip, launcher verification
20/20 with nine cryptography-dependent skips, and launcher/worker bridge 6/6.

An ephemeral Python 3.10 virtual environment with `cryptography 46.0.7` was
used only to check the combined interpreter/crypto/memfd code path. Launcher
verification passed 20/20, worker protocol passed 37/37 with one root-only
skip, launcher handoff passed 20/20 with one root-only skip, and the bridge
passed 6/6; the worker service also passed 19/19. The combined run was 102 tests
with two root-only skips. That disposable environment is not a pinned image,
signed launcher, or live deployment receipt and must not be presented as
Production evidence.

Changed Python files pass focused Ruff and byte compilation. `git diff --check`
passes. The required frontend build also passes:

```text
vite v5.4.21
1879 modules transformed
built in 3.14s
```

## Release blockers intentionally left fail-closed

This checkpoint must not be called Production-ready:

1. The static Production adapter registry remains empty. The current adapter
   schema permits only `production_capable=false`, while a real execution plan
   requires a future `production_capable=true` reviewed revision.
2. Durable recovery-control storage is absent. An ordinary phase request is not
   allowed to bypass its original deadline; post-deadline receipt recovery must
   use a separately authorized control request and new deadline.
3. The real operation executor is still a same-process callback. An attested
   isolated child process, absolute deadline, complete process-group kill, and
   bounded reap are required before untrusted target code can run.
4. The seven UE/provider/embedding/PostgreSQL/Qdrant/reconcile executors and
   their independently observed target receipts do not exist yet.
5. Approval receipts have closed payload/signature-digest contracts, but their
   issuer public-key verification is not yet included in the signed launcher
   verification closure.
6. Phase evidence declares worker payload/signature digests, but the wire
   protocol does not yet transport and cryptographically verify a signature
   against a release-pinned worker key.
7. Root-capability public handoff, multi-process crash/race evidence, live
   semantic snapshot/PBR smoke, activation, and PA-01 through PA-21 evidence
   remain pending under their separate data/cost/admin/state gates.
