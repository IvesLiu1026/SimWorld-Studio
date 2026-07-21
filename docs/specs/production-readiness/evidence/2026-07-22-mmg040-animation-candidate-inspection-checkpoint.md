# `mmg_040` animation candidate inspection checkpoint

Date: 2026-07-22

Stable branch: `codex/semantic-production-adapter`

## Outcome

A bounded UE 5.3.2 NullRHI Python commandlet loaded 12 archive animation-stack
source objects without saving Content or changing the existing `mmg_040` map. The
second append-only run exited zero with zero errors and 15 source warnings.

The raw commandlet observation proves:

- the saved pawn Blueprint generated class is a Character and its CDO uses
  `SKM_Quinn_Simple` plus `ABP_Quinn_C`;
- Quinn mesh, Quinn AnimBP, the legacy pick-up sequence, two pick-up montages
  and `MM_Fall_Loop` resolve to the same `SK_Mannequin` skeleton;
- both observed Control Rig assets and the IK Rig load through UE;
- the legacy pick-up clip/montage is 7.2 seconds and the LiftSet 150 cm
  pick-up montage is 1.6667 seconds;
- the candidate named `MM_Fall_Loop` is a 3-second clip with root motion
  disabled and forced root lock; and
- the project descriptor and saved scene hashes are unchanged before/after.

The operator receipt is:

```text
/home/yhliu/SimWorldStudio-live/0.2.0-806e869a/releases/
  ec5ed8dd4beb-mmg040-live-r1/evidence/
  ue-animation-candidate-inspection/commandlet-r2-r1/
  observation-receipt.json
```

Receipt SHA-256:
`8d603ba9286c838bd75d7dabb96693d51d47ab2df32eb3bc4bedb2fcf0b8a155`.

Raw observation SHA-256:
`0b7c41154cb67538ecfb114fcd1b6e5797cbe620abce479a99046fafe73fe865`.

UE log SHA-256:
`1e95a8f724b7e9031d7ce66da772395c8e878872e3db7b8dff92cefb09b59e5a`.

Inspection script SHA-256:
`955acaf42f3b81c9d1929f9ae2446c6a28453528b24430345f2a9577508b270e`.

## Fail-closed interpretation

This changes the archive animation-stack objects from filename-only guesses to
`derived_verified_loadable` source evidence. It does **not** make the
Production animation profile runtime-ready:

- none of the assets are project-owned `/Game/VISTA/MMG040/...` content;
- Control Rig and IK Rig preview Manny while the pawn mesh is Quinn, so exact
  runtime rig behavior is not proven by their shared skeleton alone;
- UE reported 14 out-of-date source PoseAsset warnings plus one
  inspection-script deprecation warning;
- no `vista_pick_up_attached` notify, hand-contact anchor or actual object
  attachment was observed;
- the fall candidate has root motion disabled and no landed fall/recovery
  behavior was verified; and
- no live playback, IK contact, collision, screenshot or 12-second timeline
  receipt exists.

The candidate r2 plugin branch therefore remains quarantined. The next live
content step is still to author and inspect the pinned project-owned content
with an exact UE 5.3.2 plugin build, then run disposable montage, attachment,
fall and rollback evidence.
