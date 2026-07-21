# VISTA verified-source staging runbook

Status: code-ready, offline only. No canonical VISTA data, model, database,
network, provider, or Unreal service is accessed by this workflow.

`tools/stage_vista_import_bundle.py` turns one explicitly selected verified
VISTA sample/attempt into the existing `vista-import-source/v1` curated bundle
contract. It is a staging boundary, not a dataset crawler: every input path is
named on the command line and must exactly match the selected evidence row.

## 1. Authoritative input projection

The adapter accepts one of two explicit versioned formats:

- `--verified-format manifest`: one JSON object with schema
  `vista-verified-source-manifest/v1`, a `dataset_revision`, and a bounded
  `records[]` array.
- `--verified-format jsonl`: one complete
  `vista-verified-sample/v1` object per non-empty line.

Each verified record has this exact shape:

```json
{
  "schema": "vista-verified-sample/v1",
  "dataset_revision": "sanitized_round_r1",
  "source_row_id": "mmg_040__multimodal_grounded::sanitized_case_040::sora2::attempt_007",
  "visual_id": "mmg_040",
  "case_scope": "sanitized_case_040",
  "scenario_type": "multimodal_grounded",
  "duration_sec": 12,
  "selected_attempt": {
    "provider": "sora2",
    "index": 7,
    "selected": true,
    "render_script": { "path": "...", "sha256": "...", "bytes": 923 },
    "dialogue_no_oracle": { "path": "...", "sha256": "...", "bytes": 840 },
    "media": {
      "path": "...",
      "media_id": "mmg_040:sora2:attempt_007:video",
      "media_type": "video/mp4",
      "sha256": "...",
      "bytes": 9393745,
      "duration_sec": 12,
      "width": 1280,
      "height": 720
    }
  }
}
```

See the sanitized fixtures in
`tools/tests/fixtures/vista_staging/verified-manifest.json` and
`verified-record.json`. An upstream export may project a larger private
verified ledger into this exact schema, but this adapter intentionally does
not guess column names, search backup directories, or accept unversioned raw
rows. This keeps the source of truth reviewable and prevents selecting an
unreviewed attempt by directory layout.

Restricted oracle labels, answers, review notes/decisions, seeds,
visible-evidence atoms, assist-step labels, and similarly named fields are
rejected in the verified projection, render script, and no-oracle dialogue.
Values are never copied into an error report. The dialogue file must already
use `vista-dialogue-no-oracle/v1`, carry an exact sample/case/provider/attempt
join, contain only `context` turns from the allowlisted speakers, and declare
`evaluation_input_allowed: true`.

## 2. Filesystem prerequisites

All four input arguments below are relative POSIX paths below one explicit
`--dataset-root`:

- verified manifest or JSONL;
- selected `render_script.yaml`;
- selected no-oracle dialogue JSON;
- selected MP4 binary.

The dataset root, every parent component, and every selected file must be
non-symlink filesystem objects. Files must be regular and bounded. The output
must be outside the authoritative dataset root. Its existing parent must be
owned by the current user and have no group/world permissions:

```bash
install -d -m 0700 /srv/simworld/vista-import-bundles
```

Do not point the command at a canonical dataset directory as its output.

## 3. Dry-run first

The default is read-only. It writes one machine-readable result to stdout and
does not create `--output-dir`:

```bash
uv run --project tools --frozen python tools/stage_vista_import_bundle.py \
  --dataset-root /data/VISTA_VERIFIED \
  --verified-source verified/round1.jsonl \
  --verified-format jsonl \
  --dataset-revision round1_reviewed_latest \
  --sample-id mmg_040 \
  --provider sora2 \
  --attempt 7 \
  --render-script pipeline_v2/media/mmg_040/attempt_007/render_script.yaml \
  --dialogue-no-oracle verified/dialogue/mmg_040.attempt_007.no-oracle.json \
  --media pipeline_v2/media/mmg_040/attempt_007/video.mp4 \
  --output-dir /srv/simworld/vista-import-bundles/mmg_040-attempt-007 \
  > /tmp/mmg_040-stage-dry-run.json
```

Successful output has schema `vista-import-staging-result/v1`, status
`dry_run`, the exact bundle digest, a complete
`vista-import-staging-validation/v1` report, and the registry snippet. Failure
uses the same result schema on stderr, exits `2`, and contains only a typed
safe error.

Validation covers:

- unique sample/revision/provider/selected-attempt identity;
- exact CLI path versus authoritative-row path;
- source SHA-256 and byte counts;
- `Duration_sec` across verified row, render script, and MP4 `mvhd` metadata;
- strictly increasing, unique `Scene.Actions` timestamps within duration;
- MP4 type, `moov`/`mvhd`, visual-track width/height, checksum, and bytes;
- exact no-oracle dialogue identity and evaluation privilege;
- restricted-field absence and three-file importer compatibility.

## 4. Atomic apply

After reviewing the dry-run report, repeat the exact command with `--apply`.
The tool creates a private temporary sibling, fsyncs it, and renames it into
place while holding a private staging lock:

```bash
uv run --project tools --frozen python tools/stage_vista_import_bundle.py \
  ...same reviewed arguments... \
  --apply
```

The final directory is mode `0700`; every file is mode `0600`. A second apply
with byte-identical inputs returns status `idempotent`. If the destination has
different bytes, modes, symlinks, missing files, or extra files, the command
returns `VISTA_STAGING_OUTPUT_CONFLICT` and changes nothing. It never
overwrites a different bundle.

Output layout:

```text
<bundle>/
  manifest.json
  render_script.yaml
  dialogue.no-oracle.json
  media.descriptor.json
  media/reference.mp4
  validation-report.json
  registry-snippet.json
```

`manifest.json` declares exactly the three files accepted by the existing
`vista-import-source/v1` importer. `media/reference.mp4` is a
reconstruction-only sidecar, deliberately not a fourth importer input. The
v1 descriptor therefore retains `bundled: false`—meaning “not declared in the
three-file importer source contract”—while its `logical_ref`, SHA-256, byte
count, duration, and dimensions bind the sidecar exactly. The validation
report and bundle tree digest include the sidecar.

The bundle digest algorithm is `vista-import-bundle-tree-sha256/v1`: it hashes
the algorithm label followed by each sorted core relative path, byte count,
and raw SHA-256 digest. Core entries are the manifest, its three declared
files, and the media sidecar. The report and registry snippet are not included
to avoid circular digests.

## 5. Register with Studio

`registry-snippet.json` is the exact mapping expected by
`VISTA_IMPORT_REGISTRY_JSON`. Review the absolute root, then install it through
the deployment secret/config mechanism; do not paste it into public request
parameters:

```bash
export VISTA_IMPORT_REGISTRY_JSON="$(
  tr -d '\n' < /srv/simworld/vista-import-bundles/mmg_040-attempt-007/registry-snippet.json
)"
```

The public preview/commit APIs continue to accept only revision, sample id,
attempt, and scenario type. They never receive a filesystem path.

## 6. Offline validation

```bash
uv run --project tools --frozen python -m unittest \
  tools.tests.test_stage_vista_import_bundle -v
```

When `simworld_studio_workspace/web/server/node_modules` is present, the suite
also gives the staged bundle to the existing Node importer. In a source-only
worktree without server dependencies, that one cross-runtime check is skipped;
all Python validation and failure cases still run.

## 7. Deliberate limits and next deployment gate

- Only `video/mp4` up to 1 GiB is supported. The bounded ISO-BMFF reader needs
  one `moov`, an `mvhd`, and at least one positive integral `tkhd` dimension.
- The adapter does no video decode and makes no `ffprobe` subprocess call. It
  validates container metadata, checksum, bytes, duration, and dimensions.
- It does not mutate the VISTA source tree or create the upstream verified
  projection. The dataset owner must publish that immutable projection and
  its exact revision first.
- It does not register/restart Studio, call semantic retrieval, create a scene
  artifact, invoke a provider, or mutate Unreal. Those are separate reviewed
  deployment/runtime gates.
