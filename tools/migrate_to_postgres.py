#!/usr/bin/env python3
"""Import asset_db/catalog JSON records into Postgres.

Usage:
  ASSET_DB_DIR=/data/siddhant/asset_db \
  POSTGRES_URL=postgresql://USER:PASSWORD@127.0.0.1:55432/asset_db \
  python3 tools/migrate_to_postgres.py
"""
import argparse
import glob
import json
import os
import pathlib
import re
import sys
import uuid

import psycopg2
from psycopg2.extras import Json


def point_id(asset_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simworld-asset:{asset_id}"))


def arr(value):
    if value is None:
        raw_items = []
    elif isinstance(value, list):
        raw_items = value
    elif isinstance(value, tuple):
        raw_items = list(value)
    else:
        raw_items = [value]

    out = []
    seen = set()
    for item in raw_items:
        if item is None:
            continue
        if isinstance(item, (list, tuple)):
            parts = item
        elif isinstance(item, str):
            parts = re.split(r"[,;]", item)
        else:
            parts = [item]
        for part in parts:
            text = re.sub(r"\s+", " ", str(part).strip()).strip(" \t\r\n\"'")
            if not text:
                continue
            key = text.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
    return out


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--asset-db-dir", default=os.environ.get("ASSET_DB_DIR"))
    p.add_argument("--postgres-url", default=os.environ.get("POSTGRES_URL"))
    p.add_argument("--asset-ids", default="", help="Comma/newline-separated asset ids to import")
    p.add_argument("--asset-id-file", default="", help="Text/JSON file of asset ids to import")
    p.add_argument("--commit-every", type=int, default=100, help="Commit after this many successful rows")
    p.add_argument("--fail-fast", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Resolve catalog files but do not connect to Postgres")
    return p.parse_args()


def read_asset_ids(raw: str, file_path: str) -> list[str]:
    ids: list[str] = []
    if raw:
        ids.extend(x.strip() for x in raw.replace("\n", ",").split(",") if x.strip())
    if file_path:
        text = pathlib.Path(file_path).read_text(encoding="utf-8").strip()
        if text.startswith("{") or text.startswith("["):
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("asset_ids"), list):
                ids.extend(str(x) for x in data["asset_ids"])
            elif isinstance(data, dict) and isinstance(data.get("assets"), list):
                ids.extend(str(x["asset_id"]) for x in data["assets"] if x.get("asset_id"))
            elif isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        ids.append(item)
                    elif isinstance(item, dict) and item.get("asset_id"):
                        ids.append(str(item["asset_id"]))
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                ids.append(line.split()[0].strip(","))
    seen = set()
    out = []
    for asset_id in ids:
        if asset_id not in seen:
            out.append(asset_id)
            seen.add(asset_id)
    return out


def resolve_catalog_files(asset_db_dir: str, asset_ids: list[str]) -> list[str]:
    all_files = sorted(glob.glob(os.path.join(asset_db_dir, "catalog", "*", "*.json")))
    if not asset_ids:
        return all_files

    by_id = {os.path.splitext(os.path.basename(fp))[0]: fp for fp in all_files}
    files: list[str] = []
    missing: list[str] = []
    for asset_id in asset_ids:
        fp = by_id.get(asset_id)
        if fp:
            files.append(fp)
        else:
            missing.append(asset_id)
    if missing:
        print(f"Warning: {len(missing)} requested asset ids have no catalog JSON", file=sys.stderr)
        for asset_id in missing[:20]:
            print(f"  missing: {asset_id}", file=sys.stderr)
        if len(missing) > 20:
            print(f"  ... {len(missing) - 20} more", file=sys.stderr)
    return files


def upsert_record(cur, fp: str):
    rec = json.load(open(fp, encoding="utf-8"))
    ident = rec.get("identity", {})
    sem = rec.get("semantic", {})
    geo = rec.get("geometry", {})
    tech = rec.get("technical", {})
    idx = rec.get("indexing", {})
    dims = geo.get("dimensions_m") or {}
    footp = geo.get("footprint_m") or {}
    asset_id = ident.get("asset_id") or os.path.splitext(os.path.basename(fp))[0]

    cur.execute(
        """
        INSERT INTO assets (
          asset_id, qdrant_point_id, name, category, subcategory, source_pack,
          setting, style, condition, short_description, description, "function",
          scene_types, tags, materials, mood, typical_placement, affordances, color_palette,
          width_m, depth_m, height_m, footprint_w_m, footprint_d_m, bounding_radius_m, is_symmetric,
          unreal_asset_path, asset_type, mobility, has_collision,
          triangle_count, lod_count, material_slots,
          caption_model, render_views, view_count, schema_version, raw_metadata
        ) VALUES (
          %s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,
          %s,%s,%s,
          %s,%s,%s,%s,%s
        )
        ON CONFLICT (asset_id) DO UPDATE SET
          qdrant_point_id=EXCLUDED.qdrant_point_id,
          name=EXCLUDED.name,
          category=EXCLUDED.category,
          subcategory=EXCLUDED.subcategory,
          source_pack=EXCLUDED.source_pack,
          setting=EXCLUDED.setting,
          style=EXCLUDED.style,
          condition=EXCLUDED.condition,
          short_description=EXCLUDED.short_description,
          description=EXCLUDED.description,
          "function"=EXCLUDED."function",
          scene_types=EXCLUDED.scene_types,
          tags=EXCLUDED.tags,
          materials=EXCLUDED.materials,
          mood=EXCLUDED.mood,
          typical_placement=EXCLUDED.typical_placement,
          affordances=EXCLUDED.affordances,
          color_palette=EXCLUDED.color_palette,
          width_m=EXCLUDED.width_m,
          depth_m=EXCLUDED.depth_m,
          height_m=EXCLUDED.height_m,
          footprint_w_m=EXCLUDED.footprint_w_m,
          footprint_d_m=EXCLUDED.footprint_d_m,
          bounding_radius_m=EXCLUDED.bounding_radius_m,
          is_symmetric=EXCLUDED.is_symmetric,
          unreal_asset_path=EXCLUDED.unreal_asset_path,
          asset_type=EXCLUDED.asset_type,
          mobility=EXCLUDED.mobility,
          has_collision=EXCLUDED.has_collision,
          triangle_count=EXCLUDED.triangle_count,
          lod_count=EXCLUDED.lod_count,
          material_slots=EXCLUDED.material_slots,
          caption_model=EXCLUDED.caption_model,
          render_views=EXCLUDED.render_views,
          view_count=EXCLUDED.view_count,
          schema_version=EXCLUDED.schema_version,
          raw_metadata=EXCLUDED.raw_metadata,
          updated_at=now()
        """,
        (
            asset_id,
            point_id(asset_id),
            ident.get("name") or asset_id,
            ident.get("category"),
            ident.get("subcategory"),
            ident.get("source_pack"),
            sem.get("setting"),
            sem.get("style"),
            sem.get("condition"),
            sem.get("short_description"),
            sem.get("description"),
            sem.get("function"),
            arr(sem.get("scene_types")),
            arr(sem.get("tags")),
            arr(sem.get("materials")),
            arr(sem.get("mood")),
            arr(sem.get("typical_placement")),
            arr(sem.get("affordances")),
            arr(sem.get("color_palette")),
            dims.get("width"),
            dims.get("depth"),
            dims.get("height"),
            footp.get("width"),
            footp.get("depth"),
            geo.get("bounding_radius_m"),
            geo.get("is_symmetric"),
            tech.get("unreal_asset_path"),
            tech.get("asset_type"),
            tech.get("mobility"),
            tech.get("has_collision"),
            tech.get("triangle_count"),
            tech.get("lod_count"),
            arr(tech.get("material_slots")),
            idx.get("caption_model"),
            arr(idx.get("render_views")),
            idx.get("view_count", 0),
            idx.get("schema_version", "1.0"),
            Json(rec),
        ),
    )
    return asset_id


def main():
    args = parse_args()
    if not args.asset_db_dir:
        print("ERROR: ASSET_DB_DIR is required", file=sys.stderr)
        sys.exit(2)
    if not args.postgres_url and not args.dry_run:
        print("ERROR: POSTGRES_URL is required", file=sys.stderr)
        sys.exit(2)

    asset_ids = read_asset_ids(args.asset_ids, args.asset_id_file)
    catalog_files = resolve_catalog_files(args.asset_db_dir, asset_ids)
    print(f"Found {len(catalog_files)} asset JSONs")
    if asset_ids:
        print(f"Requested {len(asset_ids)} asset ids")
    if args.dry_run:
        for fp in catalog_files[:20]:
            rec = json.load(open(fp, encoding="utf-8"))
            ident = rec.get("identity", {})
            print(f"  {ident.get('asset_id') or pathlib.Path(fp).stem}: {ident.get('category')}")
        if len(catalog_files) > 20:
            print(f"  ... {len(catalog_files) - 20} more")
        return

    conn = psycopg2.connect(args.postgres_url)
    cur = conn.cursor()

    ok = 0
    failed = 0
    since_commit = 0
    commit_every = max(1, args.commit_every)
    for fp in catalog_files:
        cur.execute("SAVEPOINT asset_import")
        try:
            upsert_record(cur, fp)
            cur.execute("RELEASE SAVEPOINT asset_import")
            ok += 1
            since_commit += 1
            if since_commit >= commit_every:
                conn.commit()
                since_commit = 0
        except Exception as e:
            cur.execute("ROLLBACK TO SAVEPOINT asset_import")
            cur.execute("RELEASE SAVEPOINT asset_import")
            failed += 1
            print(f"ERR {fp}: {e}", file=sys.stderr)
            if args.fail_fast:
                conn.rollback()
                raise
            continue

    conn.commit()
    cur.execute("SELECT count(*), count(DISTINCT category) FROM assets")
    count, cats = cur.fetchone()
    print(f"Import complete: processed={ok}, failed={failed}, table_assets={count}, categories={cats}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
