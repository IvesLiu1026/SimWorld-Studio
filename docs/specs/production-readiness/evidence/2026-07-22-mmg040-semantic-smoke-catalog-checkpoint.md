# `mmg_040` isolated semantic smoke catalog checkpoint

Date: 2026-07-22

Branch: `codex/semantic-production-adapter`

Implementation commit: `f129cc32` (`feat: add isolated mmg040 semantic smoke catalog`)

## Outcome

The three externally acquired Poly Haven assets that were already observed in
UE 5.3.2 now have a deterministic, operator-curated semantic smoke catalog.
This closes a local retrieval-input checkpoint only. It does not claim a live
PostgreSQL/Qdrant index, embeddings, Production registration, rendered review,
or canonical official-asset lineage.

The preparer independently pins the source manifest, import job, Interchange
observation and saved-scene observation. Synthetic receipts are rejected even
when a caller supplies their matching digest. Publication revalidates a
domain-separated seal across the bundle, records, category index, evidence
bindings and review gates.

## Published evidence

Exactly one private append-only publication was made at:

```text
/home/yhliu/SimWorldStudio-live/0.2.0-806e869a/releases/
  ec5ed8dd4beb-mmg040-live-r1/evidence/
  semantic-smoke-catalog-nonproduction-r1
```

It contains three namespaced records plus a non-Production category index and
receipt. All directories are mode `0700`; all files are mode `0600`.

Pins:

- catalog SHA-256:
  `f5e4fe29b238d71bea54e44af121fa0e85bbbd6d1b111082cf9cc10583018673`;
- prepared seal SHA-256:
  `f9f3a5604184bf93eb26b5f4dda8b9ceb837c611718a4792e8b33d20bb306061`;
- publication receipt SHA-256:
  `47c4429830084dccaefe4e728c4778a068504172cf094b8c048a75f9b7fa93e4`.

The output intentionally uses `nonproduction_catalog/` and
`nonproduction_category_index.json`. It does not contain the legacy
`catalog/` or `category_index.json` names consumed by existing retrieval,
migration or category-index tools. The bundle and receipt additionally fix:

```text
production_ready = false
semantic_index_eligible = false
generic_consumers_permitted = false
database_migration_permitted = false
category_index_rebuild_permitted = false
```

All seven rendered/PBR/contact/collision/screenshot gates remain
`review_pending`; there are zero rendered views versus the Production minimum
of four.

## Validation

The canonical locked tool environment passed 15 focused tests, including the
real-evidence regression, synthetic receipt rejection, all prepared-surface
mutation checks, private atomic publication and legacy-consumer incompatibility.
An independent second review found no remaining P0/P1 and authorized exactly
one fresh private append-only apply outside every canonical `ASSET_DB_DIR` and
Production snapshot path.

No database, network, UE process, model provider or public listener was used.

## Remaining Production gates

- install or authorize isolated PostgreSQL and Qdrant infrastructure;
- implement a dedicated consumer that enforces this namespace and smoke
  profile instead of renaming the files into legacy paths;
- compute and pin embeddings in an isolated collection;
- complete rendered scale/contact/collision/PBR review;
- acquire real Text/Visual Review evidence;
- register only a separately approved Production adapter and immutable asset
  snapshot after all production evidence gates pass.
