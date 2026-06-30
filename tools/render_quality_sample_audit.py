#!/usr/bin/env python3
"""Create final render-quality contact sheets and sample report.

This is a read-only QA helper for an existing asset DB. It samples catalog
records across categories, source packs, quality warnings, unusual
category/setting combinations, and geometry outliers, then writes thumbnails,
contact sheets, and a markdown/JSON report.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import math
import pathlib
import random
import re
import shutil
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageStat


DEFAULT_ASSET_DB_DIR = pathlib.Path("/data/siddhant/asset_db_ue58_qwen")


def utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def iter_ndjson(path: pathlib.Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def clean_name(text: str, limit: int = 96) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    return text[:limit] or "asset"


def nested(record: dict[str, Any], *keys: str, default=None):
    cur: Any = record
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def dimensions(record: dict[str, Any]) -> dict[str, float]:
    dims = nested(record, "geometry", "dimensions_m", default={}) or {}
    w = float(dims.get("width") or 0)
    d = float(dims.get("depth") or 0)
    h = float(dims.get("height") or 0)
    vals = [v for v in [w, d, h] if v > 0]
    mn = min(vals) if vals else 0.0
    mx = max(vals) if vals else 0.0
    footprint = w * d
    volume = w * d * h
    aspect = mx / mn if mn > 0 else math.inf
    flatness = h / mx if mx > 0 else 0.0
    return {
        "width": w,
        "depth": d,
        "height": h,
        "min_extent": mn,
        "max_extent": mx,
        "footprint": footprint,
        "volume": volume,
        "aspect_ratio": aspect,
        "height_ratio": flatness,
    }


def collect_records(asset_db_dir: pathlib.Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((asset_db_dir / "catalog").glob("*/*.json")):
        rec = load_json(path)
        rec["_catalog_path"] = str(path)
        rec["_asset_db_dir"] = str(asset_db_dir)
        records.append(rec)
    return records


def record_asset_id(record: dict[str, Any]) -> str:
    return str(nested(record, "identity", "asset_id", default=""))


def record_category(record: dict[str, Any]) -> str:
    return str(nested(record, "identity", "category", default=""))


def record_pack(record: dict[str, Any]) -> str:
    return str(nested(record, "identity", "source_pack", default=""))


def add_reason(sample: dict[str, set[str]], asset_id: str, reason: str) -> None:
    if asset_id:
        sample.setdefault(asset_id, set()).add(reason)


def sample_records(
    records: list[dict[str, Any]],
    *,
    asset_db_dir: pathlib.Path,
    seed: int,
    large_category_quota: int,
    small_category_quota: int,
    global_random: int,
    top_pack_count: int,
    top_pack_quota: int,
    dimension_top_n: int,
) -> dict[str, set[str]]:
    rng = random.Random(seed)
    by_id = {record_asset_id(r): r for r in records}
    sample: dict[str, set[str]] = {}

    by_category: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    by_pack: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    by_combo: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for rec in records:
        by_category[record_category(rec)].append(rec)
        by_pack[record_pack(rec)].append(rec)
        by_combo[(record_category(rec), str(nested(rec, "semantic", "setting", default="")))].append(rec)

    for category, rows in sorted(by_category.items()):
        quota = large_category_quota if len(rows) >= 100 else small_category_quota
        for rec in rng.sample(rows, min(quota, len(rows))):
            add_reason(sample, record_asset_id(rec), f"category_sample:{category}")

    for rec in rng.sample(records, min(global_random, len(records))):
        add_reason(sample, record_asset_id(rec), "global_random")

    top_packs = collections.Counter({pack: len(rows) for pack, rows in by_pack.items()}).most_common(top_pack_count)
    for pack, _count in top_packs:
        rows = by_pack[pack]
        for rec in rng.sample(rows, min(top_pack_quota, len(rows))):
            add_reason(sample, record_asset_id(rec), f"major_pack_sample:{pack}")

    warning_ids: set[str] = set()
    for path in sorted((asset_db_dir / "runs").glob("*/quality_warnings.ndjson")):
        for row in iter_ndjson(path):
            aid = str(row.get("asset_id") or "")
            if aid:
                warning_ids.add(aid)
    for aid in sorted(warning_ids):
        if aid in by_id:
            add_reason(sample, aid, "quality_warning")

    dim_rows = [(record_asset_id(rec), rec, dimensions(rec)) for rec in records]
    dim_rows = [(aid, rec, dims) for aid, rec, dims in dim_rows if aid]
    outlier_specs = [
        ("dimension:max_extent_top", lambda item: item[2]["max_extent"], True),
        ("dimension:height_top", lambda item: item[2]["height"], True),
        ("dimension:footprint_top", lambda item: item[2]["footprint"], True),
        ("dimension:volume_top", lambda item: item[2]["volume"], True),
        ("dimension:aspect_ratio_top", lambda item: item[2]["aspect_ratio"], True),
        ("dimension:small_volume_top", lambda item: item[2]["volume"], False),
        ("dimension:thin_extent_top", lambda item: item[2]["min_extent"], False),
    ]
    for reason, key_fn, reverse in outlier_specs:
        rows = [item for item in dim_rows if math.isfinite(key_fn(item)) and key_fn(item) > 0]
        rows.sort(key=key_fn, reverse=reverse)
        for aid, _rec, _dims in rows[:dimension_top_n]:
            add_reason(sample, aid, reason)

    for combo, rows in sorted(by_combo.items(), key=lambda kv: (len(kv[1]), kv[0])):
        if len(rows) <= 3:
            for rec in rows:
                add_reason(sample, record_asset_id(rec), f"rare_category_setting:{combo[0]}:{combo[1]}")

    return sample


def first_render_path(asset_db_dir: pathlib.Path, record: dict[str, Any]) -> pathlib.Path | None:
    views = nested(record, "indexing", "render_views", default=[]) or []
    for rel in views:
        path = asset_db_dir / str(rel)
        if path.exists():
            return path
    aid = record_asset_id(record)
    for path in sorted((asset_db_dir / "renders" / aid).glob("view_*.png")):
        if path.exists():
            return path
    return None


def image_stats(path: pathlib.Path) -> dict[str, Any]:
    with Image.open(path) as im:
        rgb = im.convert("RGB")
        stat = ImageStat.Stat(rgb)
        mean = sum(stat.mean) / len(stat.mean)
        stddev = sum(stat.stddev) / len(stat.stddev)
        return {
            "width": im.width,
            "height": im.height,
            "mean": round(mean, 3),
            "stddev": round(stddev, 3),
        }


def fit_text(text: str, max_chars: int) -> str:
    text = str(text)
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def default_font(size: int):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]:
        if pathlib.Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def make_thumbnail(src: pathlib.Path, dst: pathlib.Path, size: int) -> None:
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (size, size), "white")
        x = (size - im.width) // 2
        y = (size - im.height) // 2
        canvas.paste(im, (x, y))
        canvas.save(dst, quality=92)


def make_contact_sheets(rows: list[dict[str, Any]], out_dir: pathlib.Path, *, thumb_size: int, columns: int, rows_per_sheet: int) -> list[pathlib.Path]:
    font = default_font(16)
    small = default_font(13)
    label_h = 92
    pad = 14
    cell_w = thumb_size + pad * 2
    cell_h = thumb_size + label_h + pad
    per_sheet = columns * rows_per_sheet
    sheets: list[pathlib.Path] = []

    for sheet_idx in range(0, len(rows), per_sheet):
        chunk = rows[sheet_idx : sheet_idx + per_sheet]
        sheet_no = len(sheets) + 1
        canvas = Image.new("RGB", (columns * cell_w, rows_per_sheet * cell_h), "white")
        draw = ImageDraw.Draw(canvas)
        for i, row in enumerate(chunk):
            col = i % columns
            r = i // columns
            x = col * cell_w
            y = r * cell_h
            thumb = Image.open(row["thumbnail_path"]).convert("RGB")
            canvas.paste(thumb, (x + pad, y + pad))
            text_y = y + pad + thumb_size + 6
            draw.text((x + pad, text_y), fit_text(row["asset_id"], 31), fill="black", font=font)
            draw.text(
                (x + pad, text_y + 21),
                fit_text(f"{row['category']} / {row['setting']}", 36),
                fill=(40, 40, 40),
                font=small,
            )
            draw.text(
                (x + pad, text_y + 39),
                fit_text(row["source_pack"], 36),
                fill=(70, 70, 70),
                font=small,
            )
            draw.text(
                (x + pad, text_y + 57),
                fit_text(", ".join(row["reasons"][:2]), 41),
                fill=(90, 90, 90),
                font=small,
            )
        path = out_dir / f"contact_sheet_{sheet_no:02d}.jpg"
        canvas.save(path, quality=90)
        sheets.append(path)
    return sheets


def build_rows(asset_db_dir: pathlib.Path, records: list[dict[str, Any]], sample: dict[str, set[str]], out_dir: pathlib.Path, thumb_size: int) -> list[dict[str, Any]]:
    by_id = {record_asset_id(r): r for r in records}
    sample_views = out_dir / "sample_views"
    sample_views.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for idx, aid in enumerate(sorted(sample), 1):
        rec = by_id.get(aid)
        if not rec:
            continue
        render_path = first_render_path(asset_db_dir, rec)
        if render_path is None:
            continue
        thumb_path = sample_views / f"{idx:04d}_{clean_name(aid)}.jpg"
        make_thumbnail(render_path, thumb_path, thumb_size)
        dims = dimensions(rec)
        stats = image_stats(render_path)
        flags = []
        if stats["stddev"] < 12:
            flags.append("low_visual_variance")
        if stats["mean"] < 20:
            flags.append("very_dark")
        if stats["mean"] > 235:
            flags.append("very_bright")
        if dims["aspect_ratio"] > 50:
            flags.append("extreme_aspect_ratio")
        if dims["volume"] < 0.001:
            flags.append("tiny_volume")
        if int(nested(rec, "indexing", "view_count", default=0) or 0) < 8:
            flags.append("less_than_8_views")
        rows.append({
            "rank": idx,
            "asset_id": aid,
            "name": nested(rec, "identity", "name", default=""),
            "category": record_category(rec),
            "setting": nested(rec, "semantic", "setting", default=""),
            "source_pack": record_pack(rec),
            "asset_type": nested(rec, "technical", "asset_type", default=""),
            "dimensions_m": {k: round(dims[k], 4) for k in ["width", "depth", "height"]},
            "max_extent_m": round(dims["max_extent"], 4),
            "volume_m3": round(dims["volume"], 6),
            "aspect_ratio": round(dims["aspect_ratio"], 3) if math.isfinite(dims["aspect_ratio"]) else None,
            "view_count": nested(rec, "indexing", "view_count", default=0),
            "short_description": nested(rec, "semantic", "short_description", default=""),
            "tags": nested(rec, "semantic", "tags", default=[]) or [],
            "materials": nested(rec, "semantic", "materials", default=[]) or [],
            "reasons": sorted(sample[aid]),
            "flags": flags,
            "render_path": str(render_path),
            "thumbnail_path": str(thumb_path),
            "image_stats": stats,
        })
    return rows


def markdown_report(rows: list[dict[str, Any]], sheets: list[pathlib.Path], out_dir: pathlib.Path) -> str:
    reason_counts = collections.Counter(reason for row in rows for reason in row["reasons"])
    category_counts = collections.Counter(row["category"] for row in rows)
    pack_counts = collections.Counter(row["source_pack"] for row in rows)
    flag_counts = collections.Counter(flag for row in rows for flag in row["flags"])

    def rel(path: pathlib.Path | str) -> str:
        return pathlib.Path(path).relative_to(out_dir).as_posix()

    lines = [
        "# Final Render Quality Sample Audit",
        "",
        f"Updated: {dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()}",
        "",
        "## Summary",
        "",
        f"- Sampled assets: {len(rows)}",
        f"- Categories represented: {len(category_counts)}",
        f"- Source packs represented: {len(pack_counts)}",
        f"- Contact sheets: {len(sheets)}",
        f"- Assets with automated visual/dimension flags: {sum(1 for row in rows if row['flags'])}",
        "",
        "Selection intentionally includes category-stratified samples, global random",
        "samples, major source packs, quality-warning assets, rare category/setting",
        "pairs, and geometry outliers.",
        "",
        "## Contact Sheets",
        "",
    ]
    for path in sheets:
        lines.append(f"- `{rel(path)}`")
    lines.extend([
        "",
        "## Selection Reason Counts",
        "",
        "| Reason | Count |",
        "|---|---:|",
    ])
    for reason, count in reason_counts.most_common():
        lines.append(f"| `{reason}` | {count} |")
    lines.extend([
        "",
        "## Automated Flag Counts",
        "",
        "| Flag | Count |",
        "|---|---:|",
    ])
    if flag_counts:
        for flag, count in flag_counts.most_common():
            lines.append(f"| `{flag}` | {count} |")
    else:
        lines.append("| none | 0 |")

    lines.extend([
        "",
        "## Dimension Outlier Examples",
        "",
        "### Largest Extent",
        "",
        "| asset_id | category | max extent m | dimensions m |",
        "|---|---|---:|---|",
    ])
    for row in sorted(rows, key=lambda r: r["max_extent_m"], reverse=True)[:20]:
        lines.append(f"| `{row['asset_id']}` | `{row['category']}` | {row['max_extent_m']} | {row['dimensions_m']} |")
    lines.extend([
        "",
        "### Highest Aspect Ratio",
        "",
        "| asset_id | category | aspect ratio | dimensions m |",
        "|---|---|---:|---|",
    ])
    for row in sorted([r for r in rows if r["aspect_ratio"] is not None], key=lambda r: r["aspect_ratio"], reverse=True)[:20]:
        lines.append(f"| `{row['asset_id']}` | `{row['category']}` | {row['aspect_ratio']} | {row['dimensions_m']} |")
    lines.extend([
        "",
        "### Smallest Volume",
        "",
        "| asset_id | category | volume m3 | dimensions m |",
        "|---|---|---:|---|",
    ])
    for row in sorted([r for r in rows if r["volume_m3"] > 0], key=lambda r: r["volume_m3"])[:20]:
        lines.append(f"| `{row['asset_id']}` | `{row['category']}` | {row['volume_m3']} | {row['dimensions_m']} |")

    lines.extend([
        "",
        "## Sample Rows",
        "",
        "| # | asset_id | category | setting | pack | reasons | flags | description | tags | materials |",
        "|---:|---|---|---|---|---|---|---|---|---|",
    ])
    for row in rows:
        tags = ", ".join(map(str, row["tags"][:8]))
        materials = ", ".join(map(str, row["materials"][:6]))
        lines.append(
            "| "
            f"{row['rank']} | "
            f"`{row['asset_id']}` | "
            f"`{row['category']}` | "
            f"`{row['setting']}` | "
            f"`{row['source_pack']}` | "
            f"{', '.join(f'`{r}`' for r in row['reasons'])} | "
            f"{', '.join(f'`{f}`' for f in row['flags']) or ''} | "
            f"{fit_text(row['short_description'], 140)} | "
            f"{fit_text(tags, 100)} | "
            f"{fit_text(materials, 80)} |"
        )
    lines.extend([
        "",
        "## Interpretation",
        "",
        "This audit is a sampling artifact for human visual review. It does not mutate",
        "catalog JSONs, Postgres, Qdrant, or the backup directory. The structural",
        "pass/fail source of truth remains the final DB audit; these contact sheets",
        "are for checking visual plausibility, category coverage, and outlier renders.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-db-dir", default=str(DEFAULT_ASSET_DB_DIR))
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--seed", type=int, default=314159)
    parser.add_argument("--large-category-quota", type=int, default=10)
    parser.add_argument("--small-category-quota", type=int, default=5)
    parser.add_argument("--global-random", type=int, default=100)
    parser.add_argument("--top-pack-count", type=int, default=20)
    parser.add_argument("--top-pack-quota", type=int, default=3)
    parser.add_argument("--dimension-top-n", type=int, default=20)
    parser.add_argument("--thumb-size", type=int, default=256)
    parser.add_argument("--sheet-columns", type=int, default=4)
    parser.add_argument("--sheet-rows", type=int, default=5)
    args = parser.parse_args()

    asset_db_dir = pathlib.Path(args.asset_db_dir)
    out_dir = pathlib.Path(args.out_dir) if args.out_dir else asset_db_dir / "qa_audits" / f"render_quality_sample_{utc_stamp()}"
    out_dir.mkdir(parents=True, exist_ok=True)

    records = collect_records(asset_db_dir)
    sample = sample_records(
        records,
        asset_db_dir=asset_db_dir,
        seed=args.seed,
        large_category_quota=args.large_category_quota,
        small_category_quota=args.small_category_quota,
        global_random=args.global_random,
        top_pack_count=args.top_pack_count,
        top_pack_quota=args.top_pack_quota,
        dimension_top_n=args.dimension_top_n,
    )
    rows = build_rows(asset_db_dir, records, sample, out_dir, args.thumb_size)
    sheets = make_contact_sheets(
        rows,
        out_dir,
        thumb_size=args.thumb_size,
        columns=args.sheet_columns,
        rows_per_sheet=args.sheet_rows,
    )
    report = {
        "asset_db_dir": str(asset_db_dir),
        "out_dir": str(out_dir),
        "sampled_assets": len(rows),
        "contact_sheets": [str(path) for path in sheets],
        "rows": rows,
    }
    (out_dir / "render_quality_sample.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / "render_quality_sample.md").write_text(markdown_report(rows, sheets, out_dir), encoding="utf-8")
    shutil.copy2(__file__, out_dir / "render_quality_sample_audit_script.py")
    print(f"Wrote {out_dir / 'render_quality_sample.md'}")
    print(f"Wrote {out_dir / 'render_quality_sample.json'}")
    print(f"sampled_assets={len(rows)} contact_sheets={len(sheets)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
