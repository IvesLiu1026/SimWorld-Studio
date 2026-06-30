#!/usr/bin/env python3
"""Audit the SimWorld asset retrieval catalog, Postgres rows, Qdrant points, and renders."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import os
import pathlib
import random
import statistics
import sys
import urllib.error
import urllib.request
from typing import Any

import psycopg2
import psycopg2.extras
from PIL import Image, ImageStat


DEFAULT_ASSET_DB_DIR = pathlib.Path("/data/siddhant/asset_db_ue58_qwen")
DEFAULT_RUN_ID = "ue58_parallel_qwen36_full_20260610_155051"
DEFAULT_POSTGRES_URL = "postgresql://simworld:simworld@127.0.0.1:55432/asset_db_ue58_qwen"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
DEFAULT_QDRANT_COLLECTION = "assets_ue58_qwen"

REQUIRED_IDENTITY = ["asset_id", "name", "category", "source_pack"]
REQUIRED_SEMANTIC = [
    "setting",
    "short_description",
    "description",
    "function",
    "tags",
    "materials",
    "scene_types",
]
REQUIRED_TECHNICAL = ["unreal_asset_path", "asset_type"]
VALID_SETTINGS = {
    "modern_urban",
    "industrial",
    "suburban_residential",
    "commercial_retail",
    "nature_rural",
    "coastal_harbor",
    "medieval",
    "fantasy_gothic",
    "ancient_temple",
    "middle_eastern",
    "east_asian",
    "winter",
    "sci_fi",
    "indoor",
    "generic",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_ids(path: pathlib.Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def iter_ndjson(path: pathlib.Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield {"_bad_json": line[:200]}


def nested(record: dict[str, Any], *keys: str, default=None):
    cur: Any = record
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) > 0
    return True


def get_asset_id(record: dict[str, Any]) -> str:
    return str(nested(record, "identity", "asset_id", default="") or "")


def get_category(record: dict[str, Any]) -> str:
    return str(nested(record, "identity", "category", default="") or "")


def collect_catalog(asset_db_dir: pathlib.Path) -> tuple[list[pathlib.Path], list[dict[str, Any]], dict[str, Any]]:
    files = sorted((asset_db_dir / "catalog").glob("*/*.json"))
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in files:
        try:
            rec = load_json(path)
            if not isinstance(rec, dict):
                errors.append({"path": str(path), "error": "top-level JSON is not object"})
                continue
            rec["_catalog_path"] = str(path)
            records.append(rec)
        except Exception as e:  # noqa: BLE001 - report every parse/file problem.
            errors.append({"path": str(path), "error": str(e)})
    return files, records, {"parse_errors": errors}


def validate_catalog(records: list[dict[str, Any]], category_index: dict[str, Any]) -> dict[str, Any]:
    problems: dict[str, list[str]] = collections.defaultdict(list)
    ids: list[str] = []
    category_counts: collections.Counter[str] = collections.Counter()
    seen: set[str] = set()
    duplicates: list[str] = []

    for rec in records:
        aid = get_asset_id(rec)
        ids.append(aid)
        if aid in seen:
            duplicates.append(aid)
            problems[aid].append("duplicate asset_id")
        seen.add(aid)
        cat = get_category(rec)
        if cat:
            category_counts[cat] += 1

        for field in REQUIRED_IDENTITY:
            if not nonempty(nested(rec, "identity", field)):
                problems[aid or str(rec.get("_catalog_path"))].append(f"missing identity.{field}")
        for field in REQUIRED_SEMANTIC:
            if not nonempty(nested(rec, "semantic", field)):
                problems[aid].append(f"missing semantic.{field}")
        setting = nested(rec, "semantic", "setting")
        if setting and setting not in VALID_SETTINGS:
            problems[aid].append(f"invalid semantic.setting={setting!r}")

        dims = nested(rec, "geometry", "dimensions_m", default={}) or {}
        for field in ["width", "depth", "height"]:
            try:
                if float(dims.get(field, 0)) <= 0:
                    problems[aid].append(f"nonpositive geometry.dimensions_m.{field}")
            except Exception:
                problems[aid].append(f"invalid geometry.dimensions_m.{field}")
        try:
            if float(nested(rec, "geometry", "bounding_radius_m", default=0) or 0) <= 0:
                problems[aid].append("nonpositive geometry.bounding_radius_m")
        except Exception:
            problems[aid].append("invalid geometry.bounding_radius_m")

        for field in REQUIRED_TECHNICAL:
            if not nonempty(nested(rec, "technical", field)):
                problems[aid].append(f"missing technical.{field}")
        if not nonempty(nested(rec, "indexing", "render_views")):
            problems[aid].append("missing indexing.render_views")
        try:
            if int(nested(rec, "indexing", "view_count", default=0) or 0) <= 0:
                problems[aid].append("nonpositive indexing.view_count")
        except Exception:
            problems[aid].append("invalid indexing.view_count")
        if not nonempty(nested(rec, "indexing", "caption_model")):
            problems[aid].append("missing indexing.caption_model")

    idx_counts = {}
    for c in category_index.get("categories", []):
        if isinstance(c, dict):
            idx_counts[str(c.get("id") or "")] = int(c.get("count") or 0)
    mismatches = []
    for cat in sorted(set(idx_counts) | set(category_counts)):
        if idx_counts.get(cat, 0) != category_counts.get(cat, 0):
            mismatches.append({"category": cat, "category_index": idx_counts.get(cat, 0), "catalog": category_counts.get(cat, 0)})

    return {
        "catalog_records": len(records),
        "unique_asset_ids": len(set(ids)),
        "duplicate_asset_ids": sorted(set(duplicates)),
        "bad_catalog_asset_ids": sorted(k for k, v in problems.items() if v),
        "problem_examples": {k: problems[k][:8] for k in list(problems)[:50]},
        "problem_count": sum(len(v) for v in problems.values()),
        "category_counts": dict(sorted(category_counts.items())),
        "category_index_total": category_index.get("total_assets"),
        "category_index_mismatches": mismatches,
    }


def audit_renders(asset_db_dir: pathlib.Path, records: list[dict[str, Any]], sample_images: int, seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    render_root = asset_db_dir
    missing: dict[str, list[str]] = collections.defaultdict(list)
    bad_header: dict[str, list[str]] = collections.defaultdict(list)
    resolution_counts: collections.Counter[str] = collections.Counter()
    total_refs = 0
    existing_paths: list[tuple[str, pathlib.Path]] = []

    for rec in records:
        aid = get_asset_id(rec)
        for rel in nested(rec, "indexing", "render_views", default=[]) or []:
            total_refs += 1
            path = render_root / rel
            if not path.exists():
                missing[aid].append(rel)
                continue
            try:
                with Image.open(path) as im:
                    resolution_counts[f"{im.size[0]}x{im.size[1]}"] += 1
                existing_paths.append((aid, path))
            except Exception as e:  # noqa: BLE001
                bad_header[aid].append(f"{rel}: {e}")

    sample = existing_paths
    if len(sample) > sample_images:
        sample = rng.sample(sample, sample_images)

    pixel_flags: dict[str, list[str]] = collections.defaultdict(list)
    pixel_stats = []
    for aid, path in sample:
        try:
            with Image.open(path) as im:
                rgb = im.convert("RGB").resize((64, 64))
                stat = ImageStat.Stat(rgb)
                mean = statistics.mean(stat.mean)
                std = statistics.mean(stat.stddev)
                hist = rgb.convert("L").histogram()
                total = sum(hist) or 1
                entropy = -sum((n / total) * math.log2(n / total) for n in hist if n)
                pixel_stats.append({"asset_id": aid, "path": str(path), "mean": mean, "stddev": std, "entropy": entropy})
                flags = []
                if std < 6:
                    flags.append(f"low_stddev={std:.2f}")
                if entropy < 2.2:
                    flags.append(f"low_entropy={entropy:.2f}")
                if mean < 8 or mean > 247:
                    flags.append(f"extreme_mean={mean:.1f}")
                if flags:
                    pixel_flags[aid].append(f"{path.name}: " + ", ".join(flags))
        except Exception as e:  # noqa: BLE001
            pixel_flags[aid].append(f"{path.name}: pixel audit error {e}")

    return {
        "render_refs": total_refs,
        "render_files_checked_header": len(existing_paths),
        "missing_render_asset_ids": sorted(missing),
        "bad_header_asset_ids": sorted(bad_header),
        "resolution_counts": dict(resolution_counts.most_common()),
        "pixel_sample_images": len(sample),
        "pixel_flag_asset_ids": sorted(pixel_flags),
        "pixel_flag_examples": {k: pixel_flags[k][:5] for k in list(pixel_flags)[:50]},
        "pixel_stats_summary": summarize_numeric(pixel_stats, ["mean", "stddev", "entropy"]),
    }


def summarize_numeric(rows: list[dict[str, Any]], keys: list[str]) -> dict[str, dict[str, float]]:
    out = {}
    for key in keys:
        vals = [float(r[key]) for r in rows if key in r]
        if not vals:
            out[key] = {}
            continue
        vals_sorted = sorted(vals)
        out[key] = {
            "min": round(vals_sorted[0], 4),
            "p05": round(vals_sorted[int(0.05 * (len(vals_sorted) - 1))], 4),
            "median": round(statistics.median(vals_sorted), 4),
            "p95": round(vals_sorted[int(0.95 * (len(vals_sorted) - 1))], 4),
            "max": round(vals_sorted[-1], 4),
            "mean": round(statistics.mean(vals_sorted), 4),
        }
    return out


def pg_audit(postgres_url: str) -> dict[str, Any]:
    conn = psycopg2.connect(postgres_url)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        queries = {
            "assets_count": "SELECT count(*) AS n FROM assets",
            "qdrant_nonnull": "SELECT count(*) AS n FROM assets WHERE qdrant_point_id IS NOT NULL",
            "search_tsv_nonnull": "SELECT count(*) AS n FROM assets WHERE search_tsv IS NOT NULL",
            "null_required": """
                SELECT count(*) AS n FROM assets
                WHERE asset_id IS NULL OR name IS NULL OR category IS NULL OR source_pack IS NULL
                   OR setting IS NULL OR short_description IS NULL OR description IS NULL
                   OR "function" IS NULL OR tags IS NULL OR materials IS NULL OR scene_types IS NULL
                   OR unreal_asset_path IS NULL OR asset_type IS NULL
            """,
            "empty_semantic_arrays": """
                SELECT count(*) AS n FROM assets
                WHERE cardinality(tags) = 0 OR cardinality(materials) = 0 OR cardinality(scene_types) = 0
            """,
            "nonpositive_dims": """
                SELECT count(*) AS n FROM assets
                WHERE width_m <= 0 OR depth_m <= 0 OR height_m <= 0 OR bounding_radius_m <= 0
            """,
            "invalid_settings": """
                SELECT count(*) AS n FROM assets
                WHERE setting <> ALL(%s::text[])
            """,
        }
        out: dict[str, Any] = {}
        for name, sql in queries.items():
            if name == "invalid_settings":
                cur.execute(sql, (sorted(VALID_SETTINGS),))
            else:
                cur.execute(sql)
            out[name] = int(cur.fetchone()["n"])

        cur.execute("SELECT category, count(*) AS n FROM assets GROUP BY category ORDER BY category")
        out["category_counts"] = {row["category"]: int(row["n"]) for row in cur.fetchall()}
        cur.execute("SELECT setting, count(*) AS n FROM assets GROUP BY setting ORDER BY n DESC, setting")
        out["setting_counts"] = {row["setting"]: int(row["n"]) for row in cur.fetchall()}
        cur.execute(
            """
            SELECT asset_id, qdrant_point_id::text AS qdrant_point_id, name, category, setting, asset_type, unreal_asset_path
            FROM assets ORDER BY md5(asset_id) LIMIT 256
            """
        )
        out["qdrant_sample_rows"] = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public' AND tablename = 'assets'
            ORDER BY indexname
            """
        )
        out["indexes"] = [row["indexname"] for row in cur.fetchall()]
        return out
    finally:
        conn.close()


def qdrant_request(qdrant_url: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = qdrant_url.rstrip("/") + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def qdrant_audit(qdrant_url: str, collection: str, sample_rows: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        status = qdrant_request(qdrant_url, f"/collections/{collection}")
        result = status.get("result", {})
        out["status"] = result.get("status")
        out["optimizer_status"] = result.get("optimizer_status")
        out["points_count"] = result.get("points_count")
        out["indexed_vectors_count"] = result.get("indexed_vectors_count")
        params = result.get("config", {}).get("params", {})
        out["vectors"] = sorted((params.get("vectors") or {}).keys())
        out["sparse_vectors"] = sorted((params.get("sparse_vectors") or {}).keys())
        out["payload_schema_fields"] = sorted((result.get("payload_schema") or {}).keys())
    except Exception as e:  # noqa: BLE001
        out["collection_error"] = str(e)
        return out

    ids = [row["qdrant_point_id"] for row in sample_rows if row.get("qdrant_point_id")]
    try:
        retrieved = qdrant_request(
            qdrant_url,
            f"/collections/{collection}/points",
            {"ids": ids, "with_payload": True, "with_vector": False},
        ).get("result", [])
        got = {str(p.get("id")): p for p in retrieved}
        mismatches = []
        for row in sample_rows:
            point = got.get(row["qdrant_point_id"])
            if not point:
                mismatches.append({"asset_id": row["asset_id"], "issue": "missing sampled point"})
                continue
            payload = point.get("payload") or {}
            for key in ["asset_id", "category", "setting", "asset_type", "unreal_asset_path"]:
                if str(payload.get(key)) != str(row.get(key)):
                    mismatches.append(
                        {
                            "asset_id": row["asset_id"],
                            "field": key,
                            "postgres": row.get(key),
                            "qdrant": payload.get(key),
                        }
                    )
        out["sample_checked"] = len(sample_rows)
        out["sample_retrieved"] = len(retrieved)
        out["sample_payload_mismatches"] = mismatches[:50]
        out["sample_payload_mismatch_count"] = len(mismatches)
    except Exception as e:  # noqa: BLE001
        out["sample_error"] = str(e)
    return out


def failure_audit(run_dir: pathlib.Path) -> dict[str, Any]:
    failed_ids = read_ids(run_dir / "failed_asset_ids.txt")
    skipped_ids = read_ids(run_dir / "skipped_asset_ids.txt")
    last_failed: dict[str, dict[str, Any]] = {}
    for p in [run_dir / "failed_assets.ndjson", run_dir / "asset_results.ndjson"]:
        for row in iter_ndjson(p):
            aid = row.get("asset_id") or row.get("id") or row.get("asset")
            if aid:
                last_failed[str(aid)] = row

    hist: collections.Counter[str] = collections.Counter()
    examples: dict[str, str] = {}
    recoverable: list[str] = []
    permanent: list[str] = []
    for aid in failed_ids:
        row = last_failed.get(aid, {})
        reason = str(row.get("reason") or row.get("error") or row.get("message") or "<missing reason>")
        hist[reason] += 1
        examples.setdefault(reason, aid)
        rlow = reason.lower()
        if "nonpositive dimensions" in rlow:
            permanent.append(aid)
        else:
            recoverable.append(aid)

    return {
        "failed_final_count": len(failed_ids),
        "skipped_final_count": len(skipped_ids),
        "failure_histogram": [{"reason": k, "count": v, "example": examples.get(k)} for k, v in hist.most_common()],
        "recoverable_failed_asset_ids": recoverable,
        "permanent_failed_asset_ids": permanent,
        "configured_skip_asset_ids": skipped_ids,
    }


def write_lines(path: pathlib.Path, values: list[str]):
    path.write_text("".join(f"{v}\n" for v in values), encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    c = report["catalog"]
    r = report["renders"]
    pg = report["postgres"]
    qd = report["qdrant"]
    f = report["failures"]
    summary = report["summary"]

    lines = [
        "# Final Asset Retrieval DB Audit",
        "",
        f"Generated: {report['generated_at_utc']}",
        f"Asset DB: `{report['asset_db_dir']}`",
        f"Run dir: `{report['run_dir']}`",
        "",
        "## Executive Summary",
        "",
        f"- Overall status: **{summary['overall_status']}**",
        f"- Catalog JSONs: `{c['catalog_records']}`; unique asset ids: `{c['unique_asset_ids']}`.",
        f"- Postgres rows: `{pg.get('assets_count')}`; Qdrant points: `{qd.get('points_count')}`.",
        f"- Render references checked: `{r['render_refs']}`; missing render assets: `{len(r['missing_render_asset_ids'])}`; bad image-header assets: `{len(r['bad_header_asset_ids'])}`.",
        f"- Final selected manifest breakdown: `{summary['selected_assets']}` selected = `{summary['indexed_assets']}` indexed + `{summary['configured_skips']}` configured skips + `{summary['failed_assets']}` failures.",
        f"- Quality warnings from indexer: `{summary['quality_warnings']}`.",
        "",
        "## Methods",
        "",
        "- Parsed every catalog JSON under `catalog/*/*.json`.",
        "- Validated required identity, semantic, geometry, technical, and indexing fields.",
        "- Compared catalog category counts against `category_index.json`.",
        "- Verified every referenced render path exists and can be opened by Pillow; recorded resolution counts.",
        f"- Computed pixel statistics on a deterministic sample of `{r['pixel_sample_images']}` render images for low-variance/blank-risk detection.",
        "- Queried Postgres for row counts, required-field nulls, semantic arrays, positive dimensions, `search_tsv`, indexes, settings, and category counts.",
        "- Queried Qdrant collection status, named vectors, sparse vector config, payload indexes, and sampled Postgres-to-Qdrant payload consistency.",
        "- Triaged final failures using the authoritative `failed_asset_ids.txt` and latest failure reason per id.",
        "",
        "## Catalog Validation",
        "",
        f"- Parse/schema problem assets: `{len(c['bad_catalog_asset_ids'])}`.",
        f"- Duplicate asset ids: `{len(c['duplicate_asset_ids'])}`.",
        f"- Category-index mismatches: `{len(c['category_index_mismatches'])}`.",
        f"- Category index total: `{c.get('category_index_total')}`.",
        "",
    ]
    if c["problem_examples"]:
        lines.extend(["### Catalog Problem Examples", ""])
        for aid, probs in list(c["problem_examples"].items())[:20]:
            lines.append(f"- `{aid}`: {', '.join(probs)}")
        lines.append("")

    lines.extend([
        "## Postgres Audit",
        "",
        f"- Rows: `{pg.get('assets_count')}`.",
        f"- Rows with Qdrant point id: `{pg.get('qdrant_nonnull')}`.",
        f"- Rows with populated `search_tsv`: `{pg.get('search_tsv_nonnull')}`.",
        f"- Rows with null required fields: `{pg.get('null_required')}`.",
        f"- Rows with empty semantic arrays: `{pg.get('empty_semantic_arrays')}`.",
        f"- Rows with nonpositive dimensions: `{pg.get('nonpositive_dims')}`.",
        f"- Rows with invalid settings: `{pg.get('invalid_settings')}`.",
        f"- Indexes present: `{len(pg.get('indexes', []))}`.",
        "",
        "## Qdrant Audit",
        "",
        f"- Collection status: `{qd.get('status')}`; optimizer: `{qd.get('optimizer_status')}`.",
        f"- Points: `{qd.get('points_count')}`; indexed vectors: `{qd.get('indexed_vectors_count')}`.",
        f"- Dense vectors: `{', '.join(qd.get('vectors', []))}`.",
        f"- Sparse vectors: `{', '.join(qd.get('sparse_vectors', []))}`.",
        f"- Payload fields indexed: `{len(qd.get('payload_schema_fields', []))}`.",
        f"- Sample payload mismatches: `{qd.get('sample_payload_mismatch_count')}` across `{qd.get('sample_checked')}` sampled rows.",
        "",
        "## Render Audit",
        "",
        f"- Render refs: `{r['render_refs']}`.",
        f"- Header/size-opened images: `{r['render_files_checked_header']}`.",
        f"- Missing-render assets: `{len(r['missing_render_asset_ids'])}`.",
        f"- Bad-header assets: `{len(r['bad_header_asset_ids'])}`.",
        f"- Pixel low-quality flag assets in sample: `{len(r['pixel_flag_asset_ids'])}`.",
        f"- Resolution counts: `{r['resolution_counts']}`.",
        "",
        "Pixel sample summary:",
        "",
        "```json",
        json.dumps(r["pixel_stats_summary"], indent=2, sort_keys=True),
        "```",
        "",
    ])
    if r["pixel_flag_examples"]:
        lines.extend(["### Render Flag Examples", ""])
        for aid, flags in list(r["pixel_flag_examples"].items())[:20]:
            lines.append(f"- `{aid}`: {'; '.join(flags)}")
        lines.append("")

    lines.extend([
        "## Failure Triage",
        "",
        f"- Final failed ids: `{f['failed_final_count']}`.",
        f"- Configured skip ids: `{f['skipped_final_count']}`.",
        f"- Permanent failed candidates: `{len(f['permanent_failed_asset_ids'])}`.",
        f"- Recoverable failed candidates: `{len(f['recoverable_failed_asset_ids'])}`.",
        "",
        "| Count | Reason | Example |",
        "|---:|---|---|",
    ])
    for row in f["failure_histogram"]:
        lines.append(f"| {row['count']} | `{row['reason']}` | `{row['example']}` |")

    lines.extend([
        "",
        "## Category Counts",
        "",
        "| Category | Catalog | Postgres |",
        "|---|---:|---:|",
    ])
    for cat, n in c["category_counts"].items():
        lines.append(f"| `{cat}` | {n} | {pg.get('category_counts', {}).get(cat, 0)} |")

    lines.extend([
        "",
        "## Output Artifacts",
        "",
        f"- JSON report: `{report['artifacts']['json']}`",
        f"- Bad catalog ids: `{report['artifacts']['bad_catalog_asset_ids']}`",
        f"- Recoverable failed ids: `{report['artifacts']['recoverable_failed_asset_ids']}`",
        f"- Permanent skip ids: `{report['artifacts']['permanent_skip_asset_ids']}`",
        f"- Bad render ids: `{report['artifacts']['bad_render_asset_ids']}`",
        "",
        "## Notes",
        "",
        "- Historical run log counters include retry/resume rows. This report uses final `run_summary.json.status_counts` for selected-manifest accounting.",
        "- This audit does not mutate the catalog, Postgres, Qdrant, or the backup directory.",
    ])
    return "\n".join(lines) + "\n"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--asset-db-dir", default=str(DEFAULT_ASSET_DB_DIR))
    p.add_argument("--run-dir", default="")
    p.add_argument("--postgres-url", default=os.environ.get("POSTGRES_URL", DEFAULT_POSTGRES_URL))
    p.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", DEFAULT_QDRANT_URL))
    p.add_argument("--qdrant-collection", default=os.environ.get("QDRANT_COLLECTION", DEFAULT_QDRANT_COLLECTION))
    p.add_argument("--out-dir", default="")
    p.add_argument("--seed", type=int, default=58)
    p.add_argument("--render-sample-images", type=int, default=2000)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    asset_db_dir = pathlib.Path(args.asset_db_dir)
    run_dir = pathlib.Path(args.run_dir) if args.run_dir else asset_db_dir / "runs" / DEFAULT_RUN_ID
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else asset_db_dir / "qa_audits" / f"final_asset_db_audit_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=False)

    category_index = load_json(asset_db_dir / "category_index.json")
    _, records, catalog_load = collect_catalog(asset_db_dir)
    catalog = validate_catalog(records, category_index)
    renders = audit_renders(asset_db_dir, records, args.render_sample_images, args.seed)
    postgres = pg_audit(args.postgres_url)
    qdrant = qdrant_audit(args.qdrant_url, args.qdrant_collection, postgres.get("qdrant_sample_rows", []))
    failures = failure_audit(run_dir)
    run_summary = load_json(run_dir / "run_summary.json")
    status_counts = run_summary.get("status_counts", {})
    quality_warnings = len(list(iter_ndjson(run_dir / "quality_warnings.ndjson")))

    bad_render_ids = sorted(set(renders["missing_render_asset_ids"]) | set(renders["bad_header_asset_ids"]) | set(renders["pixel_flag_asset_ids"]))
    overall_ok = (
        not catalog_load["parse_errors"]
        and catalog["catalog_records"] == status_counts.get("ok") == postgres.get("assets_count") == qdrant.get("points_count")
        and catalog["unique_asset_ids"] == catalog["catalog_records"]
        and postgres.get("qdrant_nonnull") == catalog["catalog_records"]
        and postgres.get("search_tsv_nonnull") == catalog["catalog_records"]
        and postgres.get("null_required") == 0
        and postgres.get("nonpositive_dims") == 0
        and qdrant.get("sample_payload_mismatch_count") == 0
        and not renders["missing_render_asset_ids"]
        and not renders["bad_header_asset_ids"]
    )

    report: dict[str, Any] = {
        "generated_at_utc": utc_now(),
        "asset_db_dir": str(asset_db_dir),
        "run_dir": str(run_dir),
        "catalog_load": catalog_load,
        "catalog": catalog,
        "renders": renders,
        "postgres": {k: v for k, v in postgres.items() if k != "qdrant_sample_rows"},
        "qdrant": qdrant,
        "failures": failures,
        "summary": {
            "overall_status": "PASS with review items" if overall_ok and not bad_render_ids else "REVIEW_NEEDED",
            "selected_assets": run_summary.get("state", {}).get("total_selected"),
            "indexed_assets": status_counts.get("ok"),
            "configured_skips": status_counts.get("skip_configured"),
            "failed_assets": status_counts.get("failed"),
            "quality_warnings": quality_warnings,
            "last_db_sync_at": run_summary.get("state", {}).get("last_db_sync_at"),
            "pending_db_sync": run_summary.get("pending_db_sync"),
            "usage_totals": run_summary.get("usage_totals"),
        },
        "artifacts": {},
    }

    artifacts = {
        "json": out_dir / "final_asset_db_audit.json",
        "markdown": out_dir / "final_asset_db_audit.md",
        "bad_catalog_asset_ids": out_dir / "bad_catalog_asset_ids.txt",
        "recoverable_failed_asset_ids": out_dir / "recoverable_failed_asset_ids.txt",
        "permanent_skip_asset_ids": out_dir / "permanent_skip_asset_ids.txt",
        "bad_render_asset_ids": out_dir / "bad_render_asset_ids.txt",
    }
    report["artifacts"] = {k: str(v) for k, v in artifacts.items()}

    write_lines(artifacts["bad_catalog_asset_ids"], catalog["bad_catalog_asset_ids"])
    write_lines(artifacts["recoverable_failed_asset_ids"], failures["recoverable_failed_asset_ids"])
    permanent_ids = sorted(set(failures["configured_skip_asset_ids"]) | set(failures["permanent_failed_asset_ids"]))
    write_lines(artifacts["permanent_skip_asset_ids"], permanent_ids)
    write_lines(artifacts["bad_render_asset_ids"], bad_render_ids)

    artifacts["json"].write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    artifacts["markdown"].write_text(render_markdown(report), encoding="utf-8")

    print(f"Wrote {artifacts['markdown']}")
    print(f"Wrote {artifacts['json']}")
    print(f"overall_status={report['summary']['overall_status']}")
    print(f"catalog={catalog['catalog_records']} postgres={postgres.get('assets_count')} qdrant={qdrant.get('points_count')}")
    print(f"bad_catalog={len(catalog['bad_catalog_asset_ids'])} bad_render={len(bad_render_ids)} recoverable_failures={len(failures['recoverable_failed_asset_ids'])} permanent_skip={len(permanent_ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
