# VISTA CC0 UE 5.3 Interchange import-job preparation

Status: offline job preparation is code-verified; no Unreal Editor was started
and no asset has been imported into UE Content.

## Boundary

`tools/prepare_vista_ue_interchange_import_job.py` bridges one already complete
CC0 acquisition bundle into a deterministic
`simworld-ue-interchange-import-job/v1`. It does not download a source, launch
Unreal, execute Python in the editor, write `/Game`, discover object paths, or
write a semantic catalog, Postgres, or Qdrant.

The tool reuses the strict manifest, receipt, file-mode and tree verification
from `tools/fetch_vista_bootstrap_assets.py`. It then securely re-reads only
the pinned `.gltf` files and fails closed unless all of the following hold:

- every JSON object has unique keys and all numbers used as bounds are finite;
- each asset has exactly one glTF 2.0 JSON file;
- every external buffer/image URI is literal relative POSIX syntax;
- URI schemes, hosts, data URIs, percent encoding, queries, fragments,
  backslashes, absolute paths, `.` and `..` components are rejected;
- every referenced dependency is pinned and every pinned non-glTF file is
  referenced, so missing and orphan files both fail;
- each external buffer `byteLength` exactly equals its pinned file size;
- every mesh primitive references a VEC3 POSITION accessor with finite
  `min`/`max` metadata.

The emitted source bounds are explicitly
`gltf_POSITION_accessor_local_extrema_aggregate`. They are source metadata in
metres, do not include node transforms, and are not proof of post-import UE
bounds.

## Required acquisition

The import-job tool never uses the network. It requires the output of the
separately gated acquisition procedure in
`docs/specs/production-readiness/vista-bootstrap-assets.md`. The directory
must contain the exact private 0700/0600 tree plus its canonical
`source-manifest.json` and `acquisition-receipt.json`.

This repository has not run that acquisition into a persistent asset
directory. The examples below use an operator-selected placeholder:

```bash
ACQUISITION_DIR="$HOME/.simworld/vendor-assets/vista-mmg-040-polyhaven-v2"
```

## Offline dry run

Dry run is the default. It reads and validates the acquisition, calculates the
deterministic job digest, prints a bounded JSON result, and writes nothing:

```bash
uv run --project tools --frozen python \
  tools/prepare_vista_ue_interchange_import_job.py \
  --acquisition-dir "$ACQUISITION_DIR"
```

The result must say `network_used: false`, `unreal_started: false`,
`content_imported: false`, and `status: dry_run`.

## Publish a preparation bundle

Publication is optional and non-overwriting. The parent must already exist,
be owned by the current user, have no group/other permission, and contain no
symlink component. `--apply` requires a non-secret change/approval identifier;
only its domain-separated SHA-256 is retained.

```bash
install -d -m 0700 "$HOME/.simworld/evidence"
install -d -m 0700 "$HOME/.simworld/evidence/ue-import-jobs"

uv run --project tools --frozen python \
  tools/prepare_vista_ue_interchange_import_job.py \
  --acquisition-dir "$ACQUISITION_DIR" \
  --output-dir "$HOME/.simworld/evidence/ue-import-jobs/vista-mmg-040-r1" \
  --apply \
  --approval-ref CHANGE-VISTA-UE-IMPORT-001
```

The tool prepares private files, writes `preparation-receipt.json` last, and
publishes:

- `import-job.json` — deterministic source/import/verification contract;
- `preparation-receipt.json` — job/source digests, hashed approval reference,
  and explicit `unreal_started: false`, `content_imported: false` statements.

An existing output directory is always an error. Choose a new revisioned
directory; do not delete or overwrite prior evidence.

## Fixed UE 5.3.2 execution contract (live gate)

The job pins destination folders below
`/Game/VISTA/External/PolyHaven/<safe-id>` and the following
`unreal.AssetImportTask` settings:

| Job field | UE 5.3 property | Fixed value |
| --- | --- | --- |
| `automated` | `automated` | `true` |
| `async` | `async_` | `false` |
| `replace_existing` | `replace_existing` | `false` |
| `replace_existing_settings` | `replace_existing_settings` | `false` |
| `save` | `save` | `true` |
| source | `filename` | securely resolved absolute path for the pinned glTF |
| destination | `destination_path` | exact job `destination_content_path` |

Do not set `destination_name`: the UE 5.3 documentation says Interchange
ignores that AssetImportTask field and expects naming through its pipeline.
The executor must not accept caller-authored Python or an alternate
destination. It must prove the selected Editor is exactly 5.3.2, revalidate
the source tree, verify every destination is absent, construct only the fixed
tasks above, execute synchronously, and collect results with `get_objects()`.

This mapping is based on Epic's experimental UE 5.3 Python documentation for
[AssetImportTask](https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/AssetImportTask.html?application_version=5.3)
and
[InterchangeManager](https://dev.epicgames.com/documentation/en-us/unreal-engine/python-api/class/InterchangeManager.html?application_version=5.3).
The latter documents `create_source_data()` and `import_asset()`, but this
repository has not probed those APIs in the exact packaged Editor. Therefore
there is deliberately no live executor in this slice. An administrator must
first authorize a disposable project copy and a bounded 5.3.2 API probe.

## Required post-import evidence

The synchronous call returning success is not enough. For every source asset,
the approved executor/reviewer must append evidence that:

1. at least one returned object is a real `StaticMesh`, all exact object paths
   are recorded after import, and no object is an `ObjectRedirector`;
2. material slots and generated textures account for every source image and
   are not missing/default-only;
3. observed mesh bounds are reconciled with mesh splitting and node transforms,
   then compared to the source metre bounds using 100 cm/unit, 2% relative and
   1 cm absolute tolerance;
4. simple collision is non-empty, or an explicit complex-as-simple choice is
   reviewed and recorded with primitive counts;
5. base color uses sRGB; packed ARM is non-sRGB with R=AO, G=roughness,
   B=metallic; and the OpenGL normal map's green-channel conversion is
   visually and numerically reviewed;
6. all imported assets can be loaded and spawned in a disposable `mmg_040` scene with
   screenshots for scale, contact/floating, collision and PBR appearance.

Only a later AssetRegistry capture may establish final object paths. Only
after the content owner accepts that evidence may a separate translator add
those paths to a semantic object manifest. This preparation bundle is never a
semantic-index receipt.

## Verification

```bash
uv run --project tools --frozen python -m unittest \
  tools.tests.test_prepare_vista_ue_interchange_import_job

uv run --project tools --frozen python -m py_compile \
  tools/prepare_vista_ue_interchange_import_job.py

git diff --check
```

The synthetic tests cover valid closure/bounds evidence, bad acquisition
receipt/tree, duplicate JSON keys, traversal/remote/data/encoded URIs,
missing/orphan dependencies, buffer-size mismatch, deterministic job bytes,
zero-write dry run, private non-overwriting publication, approval handling,
symlink rejection, and foreign-lock preservation. They do not use a network or
Unreal Engine.
