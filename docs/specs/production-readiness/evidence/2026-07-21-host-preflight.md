# 2026-07-21 host preflight evidence

This is a read-only host audit for the VISTA-world production-completion work.
It records observed facts; it is not a Production readiness receipt and does
not authorize a service start, database migration, provider call, public port,
or UE scene mutation.

## Host and accelerator

- Audited host: `server`, IPv4 `140.113.215.69`.
- User: `yhliu` (`uid=1000021`), without membership in the local `docker`
  group.
- Accelerators: two NVIDIA RTX A6000 GPUs, each reporting 49,140 MiB, driver
  `590.48.01`; both were effectively idle during the audit.
- Root filesystem: 915 GiB total, 76 GiB available, 92% used.

## Unreal Engine and project

- The active shell does not set `UE_ROOT`, and `UnrealEditor` is not on
  `PATH`.
- Historical project logs identify the exact engine root as
  `/mnt/NAS2/yhliu/UE_5.7.3_prebuilt`.
- Focused checks confirmed regular executable files for `UnrealEditor`,
  `RunUAT.sh`, and the Linux `Build.sh` under that root.
- `/home/yhliu/SimWorld/scripts/ue_preflight.sh` passed for this UE 5.7.3
  engine.
- Existing project candidate:
  `/home/yhliu/SimWorld/experiments/ue_runtime_project/SimWorldHost.uproject`.
  It declares EngineAssociation `5.7` and only the legacy
  `SimWorldTcpBridge` plugin.
- `/home/yhliu/SimWorld` has substantial pre-existing tracked and untracked
  research changes. This work did not install files into or otherwise modify
  that checkout.
- Historical logs show Control Rig, IKRig, FullBodyIK, and PythonScriptPlugin
  were mounted in prior UE runs. This proves engine plugin availability only;
  it does not prove VISTA pawn, animation content, or current runtime health.

## VISTA animation plugin compile evidence

The coordinator ran the repository's reviewed `BuildPlugin` wrapper against
the exact UE 5.7.3 NAS engine. No project Content, map, or live UE session was
modified.

- The first package attempt stopped before compilation when the NAS returned
  an I/O error while UnrealBuildTool read an engine plugin descriptor.
- The next two attempts reached Clang 20.1.8 and exposed two source-level UE
  5.7 compatibility issues: the engine's generic `MakeError` template won
  overload resolution for string literals, and generated vtable helper code
  required an incomplete-safe PImpl deleter. Both were fixed in source; the
  deprecated `TArray::RemoveAt(..., bool)` form was also removed.
- On the third retry, UnrealHeaderTool completed and the `UnrealEditor Linux
  Development` target compiled all plugin translation units, linked, and
  wrote target metadata successfully. The resulting stripped x86-64 ELF was
  581,200 bytes with SHA-256
  `7ac47606a1658d0793bc5de1788f63e9d35c2f3781a3e80aaaefdedc182e9c6f`.
  Its compiled build ID was `ue573-abc1035a-compat2`; `readelf` showed only
  the expected UE Core/CoreUObject/Engine and system dynamic dependencies.
- `BuildPlugin` then began its additional `UnrealGame Linux Development`
  package target, but the process remained in the kernel's
  `nfs_wait_bit_killable` state for more than five minutes. The mount is a
  hard NFSv4.1 mount from `140.113.215.71` with `timeo=600,retrans=2`.
  AutomationTool was interrupted cleanly rather than leaving an unbounded
  packaging job. Therefore this is valid Editor compile/link evidence, not a
  completed distributable package or live-load receipt.

After the four fixed transport commands were added, a fresh build exposed one
additional C++ type mismatch in the evidence/preflight action-name check. The
check was corrected without removing the preflight requirement and an offline
regression assertion was added. A second clean package build then completed
all of the following against the same UE 5.7.3 engine and Clang 20.1.8
toolchain:

- UnrealHeaderTool with warnings treated as errors;
- `UnrealEditor Linux Development` compile, link, and metadata;
- `UnrealGame Linux Development` compile;
- `UnrealGame Linux Shipping` compile; and
- the complete `BuildPlugin` packaging/filter step.

The final portable package is under the ignored, disposable path
`.runtime/plugin-build-ue573-final-9a5eb314-fourcmd2`. Its stripped x86-64
Editor module is 605,112 bytes, has ELF build ID
`9f49b93068fa5ce59bbbffdb0a5c506903569a06`, and SHA-256
`d9b43eb89bcf50bdd185933a6d4a199b52cf0cf32ff8a123784ba795f0d58443`.
The compiled UTF-16 string inventory contains the reviewed build ID
`ue573-9a5eb314-fourcmd2` and exactly the four reserved command literals.
`readelf` reports only the expected UE Core/CoreUObject/Engine and system
dependencies. The independently generated artifact-manifest payload is:

```json
{
  "schema": "vista-animation-ue-plugin-artifact/v1",
  "plugin_name": "VistaAnimationContentApi",
  "plugin_version": "1.0.0",
  "plugin_build_id": "ue573-9a5eb314-fourcmd2",
  "binary_sha256": "d9b43eb89bcf50bdd185933a6d4a199b52cf0cf32ff8a123784ba795f0d58443",
  "engine_version": "5.7.3",
  "target_platform": "linux-x86_64",
  "api_schema": "vista-animation-ue-content-api/v1"
}
```

This supersedes the interim compile evidence above for the plugin source and
packaging gates. The binary is still not committed or deployed, and this is
not a live-load/content receipt: an administrator must copy the reviewed
package, install the project-owned listener/content driver, pin the manifest
in a root-owned location, and pass a live nonce challenge in a disposable UE
project before Production readiness can become true.

## Current runtime and service observations

- No `UnrealEditor`, Studio Node server, Cirrus, Coturn, or Nginx process was
  observed during the audit.
- Ports `3002`, `55559`, `8585`, `8586`, `3478`, and `5349` were not listening.
  Port `14500` was already listening and was left untouched.
- Docker service is active, but `yhliu` cannot access
  `/var/run/docker.sock` (`permission denied`).
- Loopback ports `5432` (PostgreSQL), `6333` (Qdrant), and `7777` (embedding)
  were closed.
- `turnserver`, `coturn`, `nginx`, `blender`, and `gltf-transform` were not
  found on `PATH`.
- Claude Code is installed at `/home/yhliu/.local/bin/claude`, version
  `2.1.215`. No provider request was made.

## Asset data observations

- No `catalog/**/*.json` corpus was present in the production worktree or the
  preserved source checkout.
- The checked-in `assets_full.json` contains only ten records and is not an
  authoritative complete asset catalog.
- Consequently a complete Postgres/Qdrant snapshot cannot truthfully be
  generated from this host checkout alone. The matching UE Content revision
  and authoritative catalog remain a Data/Admin gate.

## Remote 5090 host

- The previously named target `yhliu@140.113.215.82` was probed with
  `BatchMode=yes` and an eight-second connection timeout.
- The result was `No route to host`; no remote command ran and no remote state
  changed. Network/VPN/firewall reachability must be restored before migration
  or live build work can be delegated to that host.

## Truthful gate state

- Local UE build tooling: available through the exact NAS path.
- VISTA animation plugin source: present in this branch; compilation and live
  load are separate evidence gates.
- Real animation content driver, pawn/rig, IK anchors, fall/recover montages:
  not yet verified.
- Semantic asset stack: not provisioned and missing its authoritative corpus.
- Public WebRTC: not provisioned and missing DNS/TLS/Coturn/firewall evidence.
- Text/Visual Review: implementation is present, but no bounded real-provider
  receipts have been created; Visual Review also requires a live read-only UE
  scene snapshot.
