# Semantic isolation and `mmg_040` asset-gap checkpoint

Date: 2026-07-22

Branch: `codex/semantic-production-adapter`

Commits:

- `a53896d9` — archival semantic control-plane checkpoint;
- `3df767c2` — isolated executor hardening, pushed to `origin`;
- `e31ee835` — official-content `mmg_040` candidate-set expansion, pushed to
  `origin`.

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

The same initial search found no package filename containing `stool`,
`cabinet`, or `shelf`. A bounded synonym expansion subsequently identified
three official-content candidates that can be inspected before acquiring any
external asset:

| Candidate role | Content-relative package | SHA-256 |
| --- | --- | --- |
| stable step/seat alternative | `Camping_Pack/Props/Seat_Table_01/Meshes/SM_SeatTable_01a.uasset` | `e282a14a42d1824220ef8312932580b60de33d6cf1005b4e5de57d85fef31981` |
| static high-storage support | `Industrial_Carts/Meshes/SM_Industrial_Carts_Static_Carts_1.uasset` | `a6f3047276af42d00f0a9e8a4d910a8efc1e7be871e37285c5ba3c45bda13d83` |
| service-cart high-storage support | `Industrial_Carts/Meshes/SM_Industrial_Carts_Service_Carts_8.uasset` | `e3f7896f34be1596a20956cde1cd730117412cf4363e00fc357b00d926213f5c` |

The two industrial-cart material families have adjacent BaseColor, Normal,
Roughness and Metallic 4K texture packages in the same verified archive. The
candidate set is raw-byte pinned as
`35aaf9741650d028f8f11e303035ab10168ef78d5244411c64d9f37db7cf9f4e`
and the generated inspection profile keeps all three at
`candidate_unverified`, `start_allowed=false`. Filename and package-byte
evidence still does not prove object path, class, loadability, spawnability,
bounds, collision, resolved material slots, physical stability, support
surface, or visual role.

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
   load/spawn/material/bounds/collision receipt for the chair, box, three new
   official scene candidates, and selected Human Avatar candidates.
2. If one step candidate and one high-storage candidate pass visual-role,
   support-surface and stability review, use their exact observed object paths
   to produce the non-fixture `mmg_040` layout profile without external assets.
3. Only if the official candidates fail, an operator explicitly approves the
   pinned CC0 acquisition; the resulting receipt and tree are verified before
   any disposable UE import.
4. Only the reviewed object manifest becomes input to a generation-isolated
   Postgres/Qdrant semantic snapshot build and parity receipt.

Until those four items exist, the repository proves a hardened offline worker
boundary and a precise asset gap, not a textured `mmg_040` Production scene.
