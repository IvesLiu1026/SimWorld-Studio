# VistaAnimationContentApi

Status: **portable `1.1.0` source with a byte-pinned `mmg_040` content contract and concrete policy
driver; not compiled or live/content ready.** The earlier `1.0.0` source passed an offline UE 5.7.3
BuildPlugin package on 2026-07-21, but that binary/hash predates this driver and cannot attest this
revision. `1.1.0` has not been rebuilt with the target UE 5.3.2, installed into the target project,
exact-dispatched by a project listener, loaded in a live UE process, or exercised with real character
content. Installing this directory must not change Studio readiness until a live nonce challenge,
verified content profile, root-owned binary manifest, and disposable-project run all pass.

## What this plugin closes

The runtime module exposes one C++ `UEngineSubsystem` with exactly four reserved JSON entry points:

- `vista_animation_capabilities` for the read-only live capability challenge;
- `vista_animation_content_api` for the seven fixed lifecycle operations;
- `vista_animation_engine_time` for a digest- and slot-bound sample of the UE process-local monotonic
  clock;
- `vista_animation_evidence_capture` for a digest- and slot-bound typed request to the trusted content
  driver's real evidence capture implementation.

The subsystem validates bounded ASCII JSON with exact object shapes, rejects duplicate keys, recomputes
the canonical SHA-256 challenge/request/context digest, binds the active owner/session/slot/scene and
content proof, and keeps bounded nonce and mutation-invocation journals. A mutation invocation is
reserved before the trusted driver is called. A timeout or driver/protocol failure therefore remains
outcome-unknown and cannot execute a second time.

The concrete `mmg_040` driver also reserves a backend handle before mutation using a random
driver-instance GUID namespace plus a monotonic sequence. Backend Start cannot choose the handle and a
false return is contractually atomic/no-side-effect. Invalid successful output is action-typed rolled
back; failed rollback quarantines the process/slot for trusted operator reconciliation and restart. The
failed call does not disclose its internal handle to the wire caller. The sequence has a deterministic
1,000,000-handle rotation budget, so historical handle tracking cannot grow without bound.

The engine-time request contains only `schema`, `run_id`, `timeline_id`, `event_id`, the four-field runtime
slot binding, and `request_digest`. The response echoes every correlation field and obtains
`engine_time_sec` directly from `FPlatformTime::Seconds()` relative to this plugin subsystem process
instance; it does not accept browser time or a driver-supplied clock.

The evidence request contains only a fixed evidence kind, the runtime slot binding, the exact typed
checkpoint context, and its canonical `context_digest`. `IVistaAnimationContentDriver::CaptureEvidence`
must capture the real project-owned artifact and return its immutable descriptor. The plugin never
creates a screenshot, digest, evidence ID, or passing assertion itself. It rejects absolute/traversing
artifact references, non-lowercase SHA-256 values, an assertion on pose/screenshot evidence, and a
missing Pass/Fail result for interaction/scene-validation evidence.
For a required-target action, the concrete driver authorizes capture only for the exact action-target
capability/anchor pair established by preflight; readiness of the same target for another action is not
sufficient.

The only action identifiers compiled into the module are:

| Action | Trusted bridge ID | Target binding |
| --- | --- | --- |
| `look_at` | `vista_look_at_v1` | required |
| `brace` | `vista_brace_ik_v1` | required |
| `drag` | `vista_drag_ik_v1` | required |
| `lift_foot` | `vista_lift_foot_ik_v1` | required |
| `pause` | `vista_pause_pose_v1` | optional |
| `fall` | `vista_fall_montage_v1` | forbidden |
| `recover` | `vista_recover_montage_v1` | forbidden |

Wire JSON cannot name an AnimBP, montage, Control Rig, class, function, `/Game` asset, filesystem path,
Python body, console command, or generic bridge operation. `FVistaMmg040ContentDriver` is the
project-owned fixed mapping for the first VISTA profile. It accepts only a byte-pinned receipt for 13
assets under `/Game/VISTA/MMG040/`, then delegates to seven separate typed
`IVistaMmg040ProjectBackend::Start*` methods. Its action/evidence input contains no caller path, class,
function, script, console command, or asset identifier.

The subsystem calls every driver's `ValidateTrustedProfile` before accepting trusted runtime
configuration. The trusted action entry carries the fixed adapter ID, bridge ID, completion signal and
timeout; all four must match the driver's byte-pinned contract. A receipt whose
profile/content/verification identity or action mapping does not match the server-owned proof leaves the
Content API unconfigured.

## Deliberate boundary

This artifact implements the security/protocol/state-machine and concrete content-policy sides. It does
**not** invent or bundle the VISTA character, skeleton, hand/foot IK rigs, contact anchors, drag physics,
fall/recover montages, or animation notifies. Those are project content and must be authored at the
pinned namespace and executed by a reviewed typed project backend. Until the assets, backend and live
receipt exist, construct no driver, configure no action and keep `start_allowed=false`.

The driver must provide the following behavior before all seven actions can be declared verified:

- `look_at`: constrained head/eye gaze toward the slot-scoped target;
- `brace`: two-hand contact IK with planted feet and an observed contact assertion;
- `drag`: hand IK plus bounded root motion and chair physics/caster preservation;
- `lift_foot`: lower-body IK to a verified foot-contact anchor without penetration;
- `pause`: deterministic pose hold for the normalized duration;
- `fall`: verified direction-specific montage/collision transition, with no implicit recovery;
- `recover`: explicit recovery montage and root/capsule realignment.

`Wait` may block while `Stop` is called from another thread, so the trusted driver must be thread-safe.
Completion must come from the registered notify/signal, not elapsed wall-clock time alone.
The backend `Wait` result must also identify the observed signal and an immutable completion evidence
ID/SHA-256 that is present in its evidence list; the subsystem never substitutes the configured signal.

This first profile intentionally accepts only the server defaults verified for `mmg_040`: look-at 1s,
brace both hands for 2s, drag right hand 120cm for 2s, lift left foot 35cm for 2s, pause 3s, and forward
fall/recover. Other values and fall directions fail closed until a later profile revision carries
variant-specific live evidence.

## Trusted host integration

At process startup, a project/launcher module constructs `FVistaMmg040VerifiedProfileReceipt` from the
operator-owned deployment record, creates `FVistaMmg040ContentDriver` with its typed project backend,
then constructs `FVistaAnimationTrustedRuntimeConfig` and calls `ConfigureTrustedRuntime` once. The
configuration holds only opaque binding/proof IDs and the verified fixed action identity/signal/timeout.
Content mapping stays inside the concrete driver.

```cpp
UVistaAnimationContentApiSubsystem* Api =
    GEngine->GetEngineSubsystem<UVistaAnimationContentApiSubsystem>();

FVistaAnimationTrustedRuntimeConfig Config;
Config.SlotBinding = { OwnerId, SessionId, SlotId, SceneRevision };
Config.ContentProof = { ProfileId, ProfileRevision, ContentRevision, ContentDigest, ReceiptId };
Config.Actions = VerifiedActions;

FString SafeErrorCode;
TSharedPtr<FVistaMmg040ContentDriver, ESPMode::ThreadSafe> TrustedDriver =
    FVistaMmg040ContentDriver::Create(Receipt, ProjectBackend, SafeErrorCode);
if (!TrustedDriver ||
    !Api->ConfigureTrustedRuntime(Config, TrustedDriver.ToSharedRef(), SafeErrorCode))
{
    // Do not expose the listener and do not fall back to a generic command.
}
```

The private listener must exact-dispatch the four reserved command names before any generic MCP bridge.
For a `vista_animation_*` name, `RejectedUnknownCommand` is terminal: it must never fall through to
`Bridge->ExecuteCommand`, reflection, Python, console, or `vbp`.

```cpp
const EVistaAnimationFixedDispatchResult Result =
    Api->DispatchFixedJsonCommand(CommandType, RequestJson, ResponseJson);
if (CommandType.StartsWith(TEXT("vista_animation_")))
{
    SendBoundedJson(ResponseJson); // handled or rejected; never generic dispatch
    return;
}
```

The listener itself is intentionally not included: the Studio checkout only has an orphan generic MCP
patch and no complete listener module/API to compile against. The deployment owner must add a dedicated,
single-attempt transport adapter around these four methods.

## Build and install

All scripts default to a dry run. They do not download dependencies.

```bash
# Source install into an existing UE project (destination must not exist).
./Scripts/install-plugin.sh --project-root /absolute/path/to/Project
./Scripts/install-plugin.sh --project-root /absolute/path/to/Project --apply

# Package against the exact target engine. Build ID is compiled into the module.
export VISTA_ANIMATION_PLUGIN_BUILD_ID=vista-animation-linux-ue5.3-build001
./Scripts/build-plugin.sh \
  --engine-root /absolute/path/to/UnrealEngine \
  --output /absolute/empty/output \
  --platform Linux
# Repeat with --apply only after reviewing the printed command.

# After compiling, emit the pinned manifest to stdout. The deployment owner
# writes it to a root-owned location and sets mode/ownership separately.
node ./Scripts/create-artifact-manifest.mjs \
  --binary /absolute/path/to/libUnrealEditor-VistaAnimationContentApi.so \
  --build-id vista-animation-linux-ue5.3-build001 \
  --engine-version 5.3.2 \
  --target-platform linux-x86_64
```

Before producing a runtime profile, inspect the pinned source contract. Without a live receipt this
command intentionally exits `3` with `ready=false` and `start_allowed=false`:

```bash
node ./Scripts/prepare-content-profile.mjs \
  --contract /absolute/path/to/ContentProfiles/vista-mmg040-project-profile-source-v1.json \
  --mode preflight
```

The complete authoring, digest, receipt and activation workflow is documented in
`docs/specs/production-readiness/animation-mmg040-content-driver-runbook.md`.

The capability response hashes the actually loaded module file at runtime. Its build ID, engine version,
platform, and binary digest must exactly match the independently generated, root-owned server manifest.

## Verification

Offline checks available in this repository:

```bash
node --test Tests/offline-contract.test.mjs Tests/mmg040-content-profile.test.mjs
sh -n Scripts/install-plugin.sh
sh -n Scripts/build-plugin.sh
git diff --check
```

Still-required live gates:

1. Build `1.1.0` with the exact target UE 5.3.2 patch/platform and run UnrealHeaderTool/compiler successfully.
2. Author every pinned project asset, implement/review the typed project backend, and produce its immutable live content proof.
3. Exact-dispatch all four reserved commands through a no-retry dedicated transport; prove unknown
   `vista_animation_*` commands cannot reach the generic bridge.
4. Load in a disposable project and pass capability, preflight, 12-second completion, Stop race, notify
   timeout, disconnect/outcome-unknown, restart/reconciliation, fall collision, recover alignment, IK
   contact, screenshot, pose, interaction, and scene-validation evidence.
5. Pin the resulting module SHA-256 and only then enable Production timeline readiness.
