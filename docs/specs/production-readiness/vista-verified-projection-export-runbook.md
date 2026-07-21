# VISTA raw-to-verified projection export runbook

Status: code-ready and offline. Dataset-owner approval and publication are
still open under **T2.12 [Data Gate]**. Building and testing the exporter does
not approve or publish a real VISTA projection.

`tools/export_vista_verified_projection.py` converts one explicitly named raw
VISTA selection into the `vista-verified-source-manifest/v1` contract consumed
by `tools/stage_vista_import_bundle.py`. It does not search for a sample,
choose an attempt by directory order, call a provider, contact object storage,
or infer approval from a review ledger.

## Security boundary

The command accepts exactly five relative source paths under one explicit
`--dataset-root`:

1. the no-oracle JSONL handoff;
2. the selected-attempt ledger;
3. the raw render YAML;
4. the attempt-specific media summary;
5. the selected MP4.

The root and every path component must be non-symlink filesystem objects. Each
source must be a bounded regular file. All five paths and the full sample
identity are command-line inputs; the exporter does no dataset directory scan.
The destination must be outside the source root, and its existing parent must
be owned by the current user with no group/world permissions.

The raw schemas are exact allowlists. Unknown fields, duplicate JSON/YAML keys,
unsafe YAML tags/anchors, ambiguous rows, duplicate attempts, a non-completed
selection, or mismatched path declarations fail closed. The selected MP4 is
opened and its `ftyp`, `moov/mvhd` duration, and `trak/tkhd` dimensions are
checked against the media summary and render duration.

## What is emitted and what is excluded

An approved atomic apply creates:

```text
<projection>/
  verified-manifest.json
  render_script.yaml
  dialogue.no-oracle.json
  provenance-report.json
  media/
    reference.mp4
```

Directories are `0700`; files are `0600`. `verified-manifest.json` declares
the exact checksums and byte counts of the sanitized render, evaluation-safe
dialogue, and MP4. The output can therefore be used directly as the
`--dataset-root` of the existing staging adapter.

The exporter deliberately does not emit:

- signed URLs, URL expiry fields, object keys, absolute source paths, or
  reference-image paths;
- reviewer identity, review notes/decisions, trigger operators, or review
  group data;
- oracle/answer/label/intervention fields;
- video prompts, generation triggers, generation backend fields, emotional
  generation controls, speech/audio policy, or raw viewpoint policy;
- raw `Intervention_Cues` or embedded render dialogue.

The render output keeps only the reconstruction fields used by the importer:
environment, lighting, first-person camera continuity, visual elements,
description, timestamped actions, and exit action. The no-oracle dialogue
output contains only allowlisted `context` turns from `other_person` or `user`.
Emitted text that resembles a URL, signed credential, private key, or absolute
server path is rejected even when it appears inside an otherwise allowlisted
field.

`provenance-report.json` contains relative source paths, SHA-256/byte evidence,
validated/gating/emitted/omitted field names, validation names, and projection
entry hashes. It does not contain raw source values or an absolute output path.
The approval reference is stored only as SHA-256, not in cleartext.

## Dry-run first

The default is read-only. `--output-dir` is still required so containment and
private-parent policy are validated, but the directory is not created.

```bash
umask 077
install -d -m 0700 /srv/simworld/vista-verified-projections

APPROVED_RUN=owner_published_run_revision
CASE="benchmarks/vista_mm_dialogue_exp/runs/$APPROVED_RUN/cases/multimodal_grounded_safety_040"

uv run --project tools --frozen python tools/export_vista_verified_projection.py \
  --dataset-root /data/VISTA_OWNER_APPROVED_SOURCE \
  --no-oracle-jsonl for_senior_no_oracle/vista_assist_step_inputs_no_oracle.jsonl \
  --attempt-ledger "$CASE/pipeline_v2/media/video_attempts.json" \
  --raw-render-script "$CASE/pipeline_v2/media/render_script.yaml" \
  --media-summary "$CASE/pipeline_v2/media/attempts/attempt_007/media_summary.json" \
  --media "$CASE/pipeline_v2/media/attempts/attempt_007/video.mp4" \
  --dataset-revision round1_reviewed_latest \
  --row-id mmg_040__multimodal_grounded::multimodal_grounded_safety_040::sora2::attempt_007 \
  --visual-id mmg_040 \
  --case-scope multimodal_grounded_safety_040 \
  --scenario-type multimodal_grounded \
  --provider sora2 \
  --attempt 7 \
  --output-dir /srv/simworld/vista-verified-projections/mmg-040-attempt-007 \
  > /tmp/mmg-040-projection-dry-run.json
```

Successful stdout uses `vista-verified-projection-result/v1`, status
`dry_run`, and includes only the projection digest and bounded provenance
report. Failure is machine-readable on stderr and never echoes a rejected raw
value.

## Owner-approved atomic apply

The private ledger's `review_decision` and `selected_for_export` values are not
owner authorization. After the dataset owner reviews the dry-run evidence,
rerun the exact command with an immutable, non-secret ticket/decision id and
`--apply`:

```bash
uv run --project tools --frozen python tools/export_vista_verified_projection.py \
  ...the same reviewed arguments... \
  --owner-approval-ref VISTA-DATA-APPROVAL-2026-001 \
  --apply
```

Do not put a URL, token, email address, free-form review note, or secret in the
approval reference. The exporter writes to a private temporary sibling, fsyncs
files/directories, and atomically renames it into place. A byte-identical rerun
with the same approval reference returns `idempotent`. Any changed/missing/
extra file, unsafe mode, symlink, hard link, different approval reference, or
content mismatch returns `VISTA_PROJECTION_OUTPUT_CONFLICT`; nothing is
overwritten.

## Hand the result to the existing staging adapter

After a real approved apply, use only explicit output-relative paths:

```bash
uv run --project tools --frozen python tools/stage_vista_import_bundle.py \
  --dataset-root /srv/simworld/vista-verified-projections/mmg-040-attempt-007 \
  --verified-source verified-manifest.json \
  --verified-format manifest \
  --dataset-revision round1_reviewed_latest \
  --sample-id mmg_040 \
  --provider sora2 \
  --attempt 7 \
  --render-script render_script.yaml \
  --dialogue-no-oracle dialogue.no-oracle.json \
  --media media/reference.mp4 \
  --output-dir /srv/simworld/vista-import-bundles/mmg-040-attempt-007
```

Run this staging command without `--apply` first. Projection approval does not
authorize Studio registration, server restart, semantic retrieval, provider
calls, or Unreal mutation.

## Current `mmg_040` source-layout gate

The focused read-only check on 2026-07-21 found that the current repository
route to the approved-run evidence crosses NAS symlink components. The
exporter correctly returns `VISTA_STAGING_SYMLINK_REJECTED` for that layout.
No projection was written and no MP4 was copied.

Before a real dry-run can pass, the dataset owner must expose the five exact
inputs as regular files under one private, non-symlink, owner-controlled root
and record that immutable source snapshot/revision. This is a data-publication
step, not something the exporter may silently work around by following NAS
links. T2.12 therefore remains open even though exporter code and synthetic
compatibility tests are ready.

## Offline validation

All fixtures are synthetic and contain no real VISTA dialogue, URL, object
key, review note, image, or video frame:

```bash
uv run --project tools --frozen python -m unittest \
  tools.tests.test_export_vista_verified_projection -v

uv run --project tools --frozen python -m unittest \
  tools.tests.test_stage_vista_import_bundle -v
```

The exporter suite covers dry-run behavior, explicit owner approval, private
atomic apply, idempotency/no-overwrite, schema drift, duplicate keys, leakage,
identity/path joins, selection ambiguity, symlinks, MP4 metadata, source
change detection, machine-readable errors, and compatibility with the staging
adapter.
