# Active Work: VISTA Playable Home Realistic Interior

Updated: 2026-08-16

## Ownership

- Branch: codex/vista-playable-home-realism
- Worktree: /home/yhliu/SimWorld-Studio-worktrees/vista-playable-home-realism
- Base commit: 205ed1c59410621ec1037a528936e36a43b61a31
- Current owner: /root (implementation integrator)
- Integrator-owned paths:
  docs/specs/vista-playable-home-realism/**
- Planned worker ownership:
  - contracts worker: visual-profile schema, fixture, and focused contract tests
  - Blender worker: tools/blender/vista_playable_home_realism/** and focused
    Blender tests
  - Unreal worker: additive camera/renderer/presentation changes under
    tools/ue/vista_playable_home/** and its focused tests

## Runtime Ownership

- None during contract/source implementation.
- Do not restart, replace, or stop the accepted r1 packaged runtime.
- Do not touch GPU 1 or its existing Unreal processes.
- Do not change Sunshine, Tailscale, production port 8000, or the r1 package
  pointer during this phase.

## External-State Gates

- No external asset download, purchase, Epic/Fab account use, paid model/API,
  upload, public deployment, or long-running GPU build is authorized by the
  implementation approval.
- Generated implementation artifacts will use a new append-only run root after
  approval.

## Handoff

- Requirements/design/tasks were approved by the user on 2026-08-16.
- Workers receive separate worktrees and non-overlapping file ownership.
