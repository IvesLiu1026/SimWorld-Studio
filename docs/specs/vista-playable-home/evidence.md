# Evidence: VISTA Playable Home

Status: Implementation evidence in progress
Updated: 2026-08-15

This ledger points to append-only generated artifacts without committing large
Blender or Unreal outputs to Git. A checked task means the stated acceptance
evidence has been retained; it does not imply that later runtime tasks passed.

## Contracts and Blender forge

- Run root:
  `/mnt/NAS2/yhliu/SimWorldStudio/vista-playable-home/runs/20260815T081201Z`
- Canonical build plan: `contracts/build-plan.json`
- Build-plan file SHA-256:
  `71a0796b3c1c6b9cf977bcc5a86089d94814c4f3f4aa434944b5ca717a515507`
- World content digest:
  `0987e540baa4d399538c49e58ffd9b3446c39e2b7d594bab75e819b184781fe0`
- HouseSpec digest:
  `b9674469c5ee7ca2315be1df7145dbef5527a38ee2a145f9f1e2ad5709b7ecb1`
- Blender: pinned Linux Blender 4.5.8 LTS, seed `20260815`.
- Independent outputs: `blender/build-a` and `blender/build-b`.
- Normalized manifest SHA-256, identical for both builds:
  `c4e1e63b64aa9957eefac64b8020be30bd9ae220a00b82a7dc94ac5b6f996b54`
- Normalized Blender content digest, identical for both builds:
  `333db58984fd22459f031728d8aaf5263805df2642225be5258ae5f9a5297492`
- Retained counts: 6 rooms, 34 semantic entities, 35 one-asset GLBs,
  12 room-bundle nodes, 154 primitives, and 38 materials.
- Both builds exited cleanly and retained an inspectable full-world GLB,
  source `.blend`, overview preview, and interior preview.
- Focused combined validation:
  `49 passed, 23 subtests passed` for contracts, compiler, Blender forge, and
  runtime-profile tests.

The normalized source graph and semantic content are deterministic. Blender's
generated GLB binary chunks and Eevee preview pixels are not claimed to be
bit-for-bit identical: their file sizes and normalized content agree, but
their media SHA-256 values differ between the two executions. Runtime
acceptance therefore uses the normalized manifest plus visual/Unreal probes,
not a false byte-equality claim for renderer output.

## Unreal and remote runtime

Pending T8-T15. Runtime receipts, fixed screenshots, gameplay actions and
Sunshine/Moonlight status will be added only after real UE 5.7.3 execution.
