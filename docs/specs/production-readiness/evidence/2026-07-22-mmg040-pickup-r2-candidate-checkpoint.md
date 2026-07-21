# `mmg_040` PickUp r2 candidate checkpoint

Date: 2026-07-22

Stable branch: `codex/semantic-production-adapter`

Quarantined candidate branch: `codex/mmg040-pickup-r2-candidate`

Candidate commit: `bed9aafa` (`wip: quarantine mmg040 pickup r2 candidate`)

Stable fail-closed fix: `b38a2e1b` (`fix: reject unknown mmg040 animation revisions`)

## Outcome

The repository now preserves a typed UE-side PickUp candidate without making
the current semantic/Production branch red or implying that character
animation is live. The isolated candidate adds:

- `PickUp`, `PickUpMontage` and typed `StartPickUp` C++ surfaces;
- exact `vista_pick_up_ik_v1` and `vista_pick_up_attached` identities;
- upper-body IK and object-attachment actor capabilities;
- pickupable/hand-contact target capabilities and a hand-contact anchor;
- explicit rejection of the legacy `EndHandTrace` signal;
- immutable `mmg040_project_content_r2` with 14 assets and eight actions;
- revision-aware r1/r2 preparation and JSON Schema validation.

The original r1 source bytes remain SHA-256
`1b0aa6e48d251cb8dbeac4f34528ca8fa6084fb330fc2d150ef341f630528b1c`.
The candidate r2 source SHA-256 is
`9772da93c1054a3e3dbf71d0c066e41e6f19740901cda72b258445124be4e5a4`.

Focused candidate tests pass 29/29 and the unchanged legacy inspection-builder
tests pass 10/10. The candidate branch deliberately does not update the stable
Production source manifest or register r2 compatibility, so it must not be
merged or installed.

## Fail-closed correction on the stable branch

Independent review demonstrated that the prior compatibility helper returned
no mismatch when a known `vista_mmg040` revision lacked a policy. That allowed
arbitrary plugin/engine/platform tuples to clear the static compatibility
check. Commit `b38a2e1b` changes only the stable server policy and tests:

- an unknown `vista_mmg040` revision now fails before transport or runtime
  construction;
- r2 is rejected with plugin versions 1.0, 1.1, 1.2 and 99;
- exact r1 remains accepted;
- unrelated profile IDs retain their existing no-policy behavior.

The combined readiness, runtime and timeline tests pass 60/60.

## Live blockers

- The saved scene uses the archive `BP_ThirdPersonCharacter_C`, not the pinned
  project-owned `BP_MMG040Character_C`.
- The 14 project-owned `/Game/VISTA/MMG040/...` r2 assets are not authored.
- Candidate LiftSet/Manny assets have only registry/package observations; no
  skeleton, notify, root-motion, socket or live attachment receipt exists.
- The elevated cardboard box has no verified pickupable component or
  hand-contact/attachment anchor.
- The host's UE 5.3.2 minimal runtime contains editor binaries only. It lacks
  `RunUAT.sh`, UnrealBuildTool, Build.version and Engine Source, so exact plugin
  1.2 build/package evidence cannot be produced here yet.
- GPU 0 rendering remains separately blocked by `/dev/dri/renderD128` group
  permission.

Promotion order is fixed: obtain a full UE 5.3.2 development/source install;
build and pin plugin 1.2; author and inspect the project-owned r2 content;
create a versioned source audit; then—and only then—register exact r2
compatibility and run live attachment/fall/rollback evidence.
