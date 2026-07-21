# `mmg_040` CC0 asset bootstrap

Status: source metadata and acquisition tooling are code-verified; no external
asset has been installed into UE Content.

## Purpose

The official minimal SimWorld archive already contains a real
`SM_chair_b`, several `BP_Box` variants, humanoid lift animations, a fall loop,
IK Rig and Control Rig packages. A bounded filename audit did not identify a
high cabinet/shelf or a stable step stool. The pinned source manifest
`tools/assets/vista_mmg_040_cc0_bootstrap.json` fills only those two visual gaps:

- Poly Haven `painted_wooden_stool`, used as a candidate stable step stool;
- Poly Haven `Shelf_01`, used as a candidate high storage shelf.

Both are human-authored CC0 models with glTF geometry plus 1K diffuse,
normal, and packed ARM PBR textures. `Shelf_01` is a shelf, not a closed
cabinet; downstream labels and review must preserve that distinction.

The manifest pins every direct download to its HTTPS origin, relative path,
byte count, upstream MD5, and independently observed SHA-256. Total payload is
2,480,281 bytes across ten files. It also retains the provider, source pages,
CC0 license URL, and a display attribution. No binary is committed to Git.

## Safe acquisition

`tools/fetch_vista_bootstrap_assets.py` is offline by default. A dry run parses
the strict manifest, validates all origins/paths/hashes and prints the expected
tree digest without opening the network or creating the output directory.

The output parent must already exist, be owned by the current user, and have no
group/other permissions:

```bash
install -d -m 700 "$HOME/.simworld"
install -d -m 700 "$HOME/.simworld/vendor-assets"

uv run --project tools --frozen python tools/fetch_vista_bootstrap_assets.py \
  --output-dir "$HOME/.simworld/vendor-assets/vista-mmg-040-polyhaven-v1"
```

After an operator approves the external fetch and reviews the pinned CC0
source, acquisition is explicit:

```bash
uv run --project tools --frozen python tools/fetch_vista_bootstrap_assets.py \
  --output-dir "$HOME/.simworld/vendor-assets/vista-mmg-040-polyhaven-v1" \
  --apply \
  --accept-license CC0-1.0
```

Apply uses a unique user agent, allows only the pinned Poly Haven download
origin (including redirects), streams into a private temporary directory,
checks size + MD5 + SHA-256, and atomically publishes 0700/0600 output. An exact
existing tree is idempotent and does not re-open the network; extra, modified,
symlinked, hard-linked, or permission-drifted output fails closed. The output
includes canonical `source-manifest.json` and a bounded
`acquisition-receipt.json`.

This repository work intentionally stopped at dry-run/source verification. It
did not run `--apply` into a persistent asset directory.

## UE import gate

Acquisition alone is not a UE asset or semantic-index receipt. In a disposable
copy of the exact target project, the content owner still must:

1. import each glTF and its dependencies through the target UE Interchange
   version into a reviewed `/Game/VISTA/External/PolyHaven/...` namespace;
2. verify unit scale, pivot, collision, UVs, texture color space, tangent-space
   normal orientation, and the ambient-occlusion/roughness/metallic channels;
3. record final StaticMesh and Material object paths, dimensions, material
   slots, source tree digest, UE Content revision and import settings;
4. spawn both assets in the disposable `mmg_040` layout and capture visual,
   floating, collision, and PBR evidence;
5. add only the verified resulting object paths to the catalog, then rebuild
   and attest the Postgres/Qdrant snapshot.

Until those steps pass, these files are `external_source_candidates`. They must
not satisfy `ASSET-001`, semantic asset readiness, or the real-PBR scene gate.

## Verification

```bash
uv run --project tools --frozen python -m unittest \
  tools.tests.test_fetch_vista_bootstrap_assets
uv run --project tools --frozen python -m py_compile \
  tools/fetch_vista_bootstrap_assets.py
git diff --check
```

Tests use only in-memory synthetic payloads. They do not contact Poly Haven or
modify UE Content.
