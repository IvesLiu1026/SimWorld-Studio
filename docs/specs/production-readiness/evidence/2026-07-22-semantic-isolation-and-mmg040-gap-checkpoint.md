# Semantic isolation and `mmg_040` asset-gap checkpoint

Date: 2026-07-22

Branch: `codex/semantic-production-adapter`

Commits:

- `a53896d9` — archival semantic control-plane checkpoint;
- `3df767c2` — isolated executor hardening, pushed to `origin`.

## Resource boundary

This checkpoint did not start, stop, query, or bind the live demo's GPU or
loopback ports. It did not call a model provider, contact Postgres/Qdrant, open
a public listener, modify UE Content, or touch VISTA Production port `8000`.
The separate live-demo task remains the sole owner of GPU 1 and ports
`3012/55570/8595/8596/8899`.

## Closed isolation defects

The executor now supports Linux pidfds even when the uv-managed CPython build
does not expose `os.pidfd_open` or `signal.pidfd_send_signal`: it uses the
bounded Linux syscall interface only on an explicit architecture allowlist.
The parent enables `PR_SET_CHILD_SUBREAPER` while the single callback is live,
then kills and reaps descendants that call `setsid()` to escape the original
process group. The child enumerates `/proc/self/fd` and closes inherited file
descriptors above the fixed result channel rather than relying on a numeric FD
cap. The typed executor error is no longer a frozen exception, so ordinary
traceback cleanup cannot mask the fixed public failure.

New regression cases prove that:

- a descendant in a new session is killed and reaped before return;
- an inherited descriptor at FD 4096 is unavailable in the callback;
- the uv Python build without native pidfd wrappers still executes the full
  isolation lifecycle.

The earlier approval-aggregate P1 is also covered: independent references and
the aggregate proof bind the exact trust-bundle revision, issuer-membership
digest, issue/expiry timestamps and maximum lifetime. Negative tests change
each trust/lifetime pin independently and require a reference mismatch.

## Verification

- focused approval, control-ledger, isolated-executor and worker suite:
  84 tests passed;
- all `test_semantic_index*.py` discovery:
  259 tests passed, 20 intentional platform/dependency skips;
- isolated executor alone: 12 tests passed;
- changed-file `py_compile` and `git diff --check`: passed.

These are offline code results, not a real semantic-index snapshot.

## Real `mmg_040` content observation

A bounded filename-only read of the verified official minimal Content tree
confirmed packages for:

- `CityDatabase/meshes/SM_chair_b.uasset`;
- `CityDatabase/blueprints/BP_Box.uasset`;
- `Human_Avatar/Blueprint/BP_Human_Base.uasset`;
- `Human_Avatar/DefaultCharacter/Blueprint/BP_DefaultHuman.uasset`;
- `Human_Avatar/DefaultCharacter/Blueprint/BP_Default_Character.uasset`.

The same focused search found no package filename containing `stool`,
`cabinet`, or `shelf`. This remains filename evidence only; it does not prove
object path, class, loadability, spawnability, bounds, collision, materials or
animation compatibility.

The checked-in Poly Haven bootstrap manifest passed its zero-network dry run:

- status: `dry_run`, `network_used=false`;
- license: `CC0-1.0`;
- 10 files, 2,480,281 bytes;
- manifest SHA-256:
  `edab8924c72aa9233f98977eb58e9a7e1d12c93e9522f83ddedead56c209b329`;
- expected tree SHA-256:
  `2e9eabcfc74c87b71abea8ab239cb16ed7ef2b350ebfda023a64bca0d1f773dc`.

No asset was downloaded. A separate operator decision must explicitly accept
the CC0 acquisition before `--apply`; UE Interchange import and semantic-index
publication remain later, separately evidenced state changes.

## Next authoritative evidence

1. The live runtime owner captures a revision-bound AssetRegistry audit and a
   read-only load/spawn/material/bounds/collision receipt for the chair, box and
   selected Human Avatar candidates.
2. An operator explicitly approves the pinned CC0 acquisition; the resulting
   receipt and tree are verified before any UE import.
3. A disposable UE copy imports and validates stool/shelf PBR assets, then
   produces exact object paths and a non-fixture `mmg_040` layout profile.
4. Only that reviewed object manifest becomes input to a generation-isolated
   Postgres/Qdrant semantic snapshot build and parity receipt.

Until those four items exist, the repository proves a hardened offline worker
boundary and a precise asset gap, not a textured `mmg_040` Production scene.
