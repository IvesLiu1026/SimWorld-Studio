#!/usr/bin/env python3
"""Import asset_db/catalog JSON records into Postgres.

Usage:
  ASSET_DB_DIR=/srv/simworld/asset-db \
  POSTGRES_URL_FILE=/run/secrets/postgres_url \
  ASSET_SNAPSHOT_REVISION=asset-snapshot-IMMUTABLE \
  uv run --project tools --frozen python tools/migrate_to_postgres.py
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

try:
    from asset_stack_config import load_secret
except ModuleNotFoundError:  # Loaded by path in focused unit tests.
    from tools.asset_stack_config import load_secret


SAFE_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,159}$")
UNPINNED_REVISIONS = {"dev", "latest", "main", "master", "unknown", "unversioned"}


def point_id(asset_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simworld-asset:{asset_id}"))


def require_snapshot_revision(value: str | None) -> str:
    revision = str(value or "").strip()
    if not SAFE_REVISION.fullmatch(revision):
        raise ValueError(
            "ASSET_SNAPSHOT_REVISION must be a safe immutable revision identifier"
        )
    if revision.casefold() in UNPINNED_REVISIONS:
        raise ValueError(
            "ASSET_SNAPSHOT_REVISION must identify an immutable snapshot"
        )
    return revision


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
    # The DSN is environment/file-only so it cannot leak through argv.
    p.set_defaults(postgres_url="")
    p.add_argument(
        "--snapshot-revision",
        default=os.environ.get("ASSET_SNAPSHOT_REVISION", ""),
        help="Immutable snapshot revision stamped on every row (default: ASSET_SNAPSHOT_REVISION)",
    )
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


def upsert_record(cur, fp: str, snapshot_revision: str):
    snapshot_revision = require_snapshot_revision(snapshot_revision)
    rec = json.loads(pathlib.Path(fp).read_text(encoding="utf-8"))
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
          asset_id, qdrant_point_id, asset_snapshot_revision,
          name, category, subcategory, source_pack,
          setting, style, condition, short_description, description, "function",
          scene_types, tags, materials, mood, typical_placement, affordances, color_palette,
          width_m, depth_m, height_m, footprint_w_m, footprint_d_m, bounding_radius_m, is_symmetric,
          unreal_asset_path, asset_type, mobility, has_collision,
          triangle_count, lod_count, material_slots,
          caption_model, render_views, view_count, schema_version, raw_metadata
        ) VALUES (
          %s,%s,%s,
          %s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,%s,%s,%s,
          %s,%s,%s,%s,
          %s,%s,%s,
          %s,%s,%s,%s,%s
        )
        ON CONFLICT (asset_id) DO UPDATE SET
          qdrant_point_id=EXCLUDED.qdrant_point_id,
          asset_snapshot_revision=EXCLUDED.asset_snapshot_revision,
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
            snapshot_revision,
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
    try:
        snapshot_revision = require_snapshot_revision(args.snapshot_revision)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(2)
    if not args.asset_db_dir:
        print("ERROR: ASSET_DB_DIR is required", file=sys.stderr)
        sys.exit(2)
    if not args.dry_run:
        args.postgres_url, _source = load_secret(
            os.environ, "POSTGRES_URL", "POSTGRES_URL_FILE", required=True
        )

    asset_ids = read_asset_ids(args.asset_ids, args.asset_id_file)
    catalog_files = resolve_catalog_files(args.asset_db_dir, asset_ids)
    if not catalog_files:
        print("ERROR: no catalog JSON records were selected", file=sys.stderr)
        sys.exit(2)
    if asset_ids and len(catalog_files) != len(asset_ids):
        print(
            "ERROR: every requested asset id must resolve before migration",
            file=sys.stderr,
        )
        sys.exit(2)
    print(f"Found {len(catalog_files)} asset JSONs")
    print(f"Asset snapshot revision: {snapshot_revision}")
    if asset_ids:
        print(f"Requested {len(asset_ids)} asset ids")
    if args.dry_run:
        for fp in catalog_files[:20]:
            rec = json.loads(pathlib.Path(fp).read_text(encoding="utf-8"))
            ident = rec.get("identity", {})
            print(f"  {ident.get('asset_id') or pathlib.Path(fp).stem}: {ident.get('category')}")
        if len(catalog_files) > 20:
            print(f"  ... {len(catalog_files) - 20} more")
        return

    conn = psycopg2.connect(
        args.postgres_url,
        connect_timeout=10,
        application_name="simworld_asset_catalog_migration",
    )
    cur = conn.cursor()
    cur.execute(
        "SELECT schema_version FROM simworld_schema_metadata WHERE component = %s",
        ("asset_catalog",),
    )
    schema_row = cur.fetchone()
    if not schema_row or schema_row[0] != 2:
        conn.rollback()
        print(
            "ERROR: tools/schema.sql schema version 2 must be applied before migration",
            file=sys.stderr,
        )
        sys.exit(2)

    ok = 0
    failed = 0
    since_commit = 0
    commit_every = max(1, args.commit_every)
    for fp in catalog_files:
        cur.execute("SAVEPOINT asset_import")
        try:
            upsert_record(cur, fp, snapshot_revision)
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

    if failed:
        conn.commit()
        sys.exit(1)

    cur.execute(
        """
        SELECT
          count(*),
          count(DISTINCT category),
          count(*) FILTER (WHERE asset_snapshot_revision = %s)
        FROM assets
        """,
        (snapshot_revision,),
    )
    count, cats, matching_snapshot_rows = cur.fetchone()
    if not asset_ids and count != len(catalog_files):
        conn.commit()
        print(
            "ERROR: PostgreSQL row count does not exactly match the full catalog: "
            f"table={count}, catalog={len(catalog_files)}",
            file=sys.stderr,
        )
        sys.exit(1)
    if matching_snapshot_rows != count:
        conn.commit()
        print(
            "ERROR: PostgreSQL contains rows outside ASSET_SNAPSHOT_REVISION "
            f"{snapshot_revision}: matching={matching_snapshot_rows}, total={count}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Finalize the staged v1 -> v2 transition only after the whole live table
    # proves it belongs to exactly one immutable snapshot.
    cur.execute("ALTER TABLE assets VALIDATE CONSTRAINT assets_snapshot_revision_present")
    cur.execute("ALTER TABLE assets ALTER COLUMN asset_snapshot_revision SET NOT NULL")
    conn.commit()
    print(
        "Import complete: "
        f"processed={ok}, failed={failed}, table_assets={count}, categories={cats}, "
        f"snapshot_rows={matching_snapshot_rows}"
    )


if __name__ == "__main__":
    main()
