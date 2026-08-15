# VISTA Playable Home — HSSD visual binding pass

This optional pass replaces the procedural **visual** GLBs for semantic props
with locally available, textured HSSD models. It does not alter the approved
HouseSpec, room topology, gameplay transforms, collision proxies, doors,
event markers, NPCs, or the existing `vista_playable_home` forge.

## License and release gate

HSSD is the *Habitat Synthetic Scenes Dataset*, published by the HSSD authors
at <https://3dlg-hcvc.github.io/hssd/>. The local dataset README declares
**Creative Commons Attribution-NonCommercial 4.0 International
(CC BY-NC 4.0)**: <https://creativecommons.org/licenses/by-nc/4.0/>.

Every binding records the HSSD repository revision, README hash, object id,
semantic class, source GLB hash, license identifier, and modification notice.
The generated models are suitable only for research and non-commercial demos
unless separate permission is obtained. A commercial/public product release
must replace these assets or clear a separate license.

The generated assets are modified copies: they are imported, joined to one
primary mesh, reoriented, rescaled to the approved target AABB, grounded at
`z=0`, and re-exported while retaining their PBR materials and texture slots.
The Habitat-ready HSSD files commonly require `KHR_texture_basisu`, which
Blender 4.5.8 cannot decode. For those files the builder imports a temporary
material-index surrogate, then reattaches the original KTX2 image bytes and
exact source material/texture records to the normalized GLB. The attribution
manifest records `texture_transport: KHR_texture_basisu_preserved` and
`blender_decoded_textures: false`; it never claims Blender decoded the images
and never silently drops or replaces compressed textures. Every KTX2 payload
is SHA-256 checked image-by-image after rehydration, along with closed buffer,
bufferView, material, texture, sampler, accessor, and extension indexes.

## Contract

Inputs:

- a `simworld.vista.playable-home-blender-manifest/v1` normalized manifest;
- the pinned local HSSD checkout;
- explicit `--license-accept CC-BY-NC-4.0` acknowledgement.

Outputs:

- `binding-plan.json`: deterministic semantic selection and source receipts;
- `assets/<logical_asset_id>.glb`: one primary mesh per logical UE asset;
- `binding-attribution-manifest.json`: output hashes, measured dimensions,
  PBR/mesh inspection, source attribution, and closed-world coverage.

Selection is independent of directory/CSV order. It first uses HSSD's
condensed semantic labels, then target dimension fit, PBR texture-slot and
triangle gates, and finally the HSSD object id as a stable tie-breaker. Missing
or unknown categories fail closed. The only explicit procedural preservations
are room shell/ceiling/collision bundles, gameplay doors, runtime NPCs, event
markers, and loose keys (HSSD has no verified loose-key semantic category).

The policy already recognizes sofa/couch, bed, table, desk, chair, cabinet,
fridge, stove, sink, toilet, bathtub, shelf, TV, lamp, plant, and the portable
prop classes used by the current home. A category is selected only when it is
present in the normalized manifest and therefore has an approved target AABB.

## Deterministic plan

Run from the repository root. The output file must not already exist.

```bash
uv run --offline --project tools python \
  -m tools.blender.vista_playable_home_hssd \
  --normalized-manifest /absolute/run/blender/normalized-manifest.json \
  --hssd-root /mnt/NAS2/yhliu/habitat_data/versioned_data/hssd-hab \
  --output /absolute/run/hssd-binding-plan.json \
  --license-accept CC-BY-NC-4.0
```

Repeat `--asset-id` for an explicitly closed smoke subset.

## Blender 4.5.8 build

The output root must be absolute, non-symlinked, and empty. This pass does no
rendering and needs no GPU.

```bash
CUDA_VISIBLE_DEVICES='' \
  /home/yhliu/.local/opt/blender-4.5.8-linux-x64/blender \
  --background --factory-startup \
  --python tools/blender/vista_playable_home_hssd/build.py -- \
  --normalized-manifest /absolute/run/blender/normalized-manifest.json \
  --hssd-root /mnt/NAS2/yhliu/habitat_data/versioned_data/hssd-hab \
  --output-root /absolute/empty/run/hssd-visuals \
  --license-accept CC-BY-NC-4.0
```

For a three-asset smoke, add:

```text
--asset-id asset.prop.sofa \
--asset-id asset.prop.bed \
--asset-id asset.prop.fridge
```

UE integration should overlay only the built `asset.prop.*` entries onto the
procedural forge artifact bindings. It must continue using the existing door
and room-bundle GLBs/collision policy. HSSD articulated URDF behavior is not
carried into this static one-mesh pass; Unreal gameplay actors remain the
authority for open/close and pickup state.

## Validation

```bash
uv run --offline --with pytest pytest -q \
  tools/tests/test_vista_playable_home_hssd.py
```
