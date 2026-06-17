#!/usr/bin/env python3
"""Remap legacy catalog JSON asset ids to current manifest ids.

The safe remap key is technical.unreal_asset_path -> manifest_full asset_id.
This is intended for preserving previously generated VLM metadata after the
manifest asset_id naming scheme changed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
from typing import Any


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d_%H%M%S")


def load_json(path: pathlib.Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_lines(path: pathlib.Path, values: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{value}\n" for value in values), encoding="utf-8")


def catalog_files(asset_db_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted((asset_db_dir / "catalog").glob("*/*.json"))


def archive_path(archive_root: pathlib.Path, source: pathlib.Path, asset_db_dir: pathlib.Path, bucket: str) -> pathlib.Path:
    rel = source.relative_to(asset_db_dir)
    return archive_root / bucket / rel


def move_to_archive(archive_root: pathlib.Path, source: pathlib.Path, asset_db_dir: pathlib.Path, bucket: str) -> pathlib.Path:
    dest = archive_path(archive_root, source, asset_db_dir, bucket)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(dest))
    return dest


def build_manifest_maps(manifest_path: pathlib.Path) -> tuple[set[str], dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    manifest = load_json(manifest_path)
    assets = manifest.get("assets") or []
    manifest_ids = {str(asset["asset_id"]) for asset in assets if asset.get("asset_id")}
    by_path_multi: dict[str, list[dict[str, Any]]] = {}
    for asset in assets:
        path = asset.get("ue_path")
        if path:
            by_path_multi.setdefault(str(path), []).append(asset)
    by_path_unique = {path: rows[0] for path, rows in by_path_multi.items() if len(rows) == 1}
    ambiguous = {path: rows for path, rows in by_path_multi.items() if len(rows) > 1}
    return manifest_ids, by_path_unique, ambiguous


def remap_render_views(asset_db_dir: pathlib.Path, rec: dict[str, Any], old_id: str, new_id: str, dry_run: bool) -> dict[str, Any]:
    old_dir = asset_db_dir / "renders" / old_id
    new_dir = asset_db_dir / "renders" / new_id
    moved = False
    if old_dir.exists() and not new_dir.exists():
        moved = True
        if not dry_run:
            shutil.move(str(old_dir), str(new_dir))
    idx = rec.setdefault("indexing", {})
    views = idx.get("render_views")
    if isinstance(views, list):
        idx["render_views"] = [
            str(view).replace(f"renders/{old_id}/", f"renders/{new_id}/", 1)
            if str(view).startswith(f"renders/{old_id}/")
            else view
            for view in views
        ]
    return {"old_render_dir": str(old_dir), "new_render_dir": str(new_dir), "moved": moved}


def plan_remap(asset_db_dir: pathlib.Path, manifest_path: pathlib.Path) -> dict[str, Any]:
    manifest_ids, by_path, ambiguous_paths = build_manifest_maps(manifest_path)
    files = catalog_files(asset_db_dir)
    by_current_file_id = {path.stem: path for path in files}

    plan = {
        "catalog_files": len(files),
        "manifest_ids": len(manifest_ids),
        "already_manifest": [],
        "remap": [],
        "conflict": [],
        "unmatched": [],
        "ambiguous_manifest_path": [],
    }

    for path in files:
        rec = load_json(path)
        ident = rec.get("identity") or {}
        tech = rec.get("technical") or {}
        old_id = str(ident.get("asset_id") or path.stem)
        if old_id in manifest_ids:
            plan["already_manifest"].append({"asset_id": old_id, "path": str(path)})
            continue
        ue_path = tech.get("unreal_asset_path")
        if ue_path in ambiguous_paths:
            plan["ambiguous_manifest_path"].append({
                "old_id": old_id,
                "path": str(path),
                "unreal_asset_path": ue_path,
                "candidate_asset_ids": [asset.get("asset_id") for asset in ambiguous_paths[str(ue_path)]],
            })
            continue
        asset = by_path.get(str(ue_path))
        if not asset:
            plan["unmatched"].append({"old_id": old_id, "path": str(path), "unreal_asset_path": ue_path})
            continue
        new_id = str(asset["asset_id"])
        target_exists = by_current_file_id.get(new_id)
        item = {
            "old_id": old_id,
            "new_id": new_id,
            "path": str(path),
            "target_path": str(path.parent / f"{new_id}.json"),
            "unreal_asset_path": ue_path,
            "manifest_ue_name": asset.get("ue_name"),
            "manifest_source_pack": asset.get("source_pack"),
            "manifest_asset_type": asset.get("asset_type"),
        }
        if target_exists:
            item["existing_target_path"] = str(target_exists)
            plan["conflict"].append(item)
        else:
            plan["remap"].append(item)
    return plan


def apply_remap(
    *,
    asset_db_dir: pathlib.Path,
    manifest_path: pathlib.Path,
    plan: dict[str, Any],
    archive_root: pathlib.Path,
    dry_run: bool,
) -> dict[str, Any]:
    manifest_ids, by_path, _ = build_manifest_maps(manifest_path)
    del manifest_ids
    applied: list[dict[str, Any]] = []
    archived_conflicts: list[dict[str, Any]] = []
    ts = utc_now()

    for item in plan["remap"]:
        source = pathlib.Path(item["path"])
        target = pathlib.Path(item["target_path"])
        rec = load_json(source)
        old_id = item["old_id"]
        new_id = item["new_id"]
        asset = by_path[str(item["unreal_asset_path"])]

        ident = rec.setdefault("identity", {})
        tech = rec.setdefault("technical", {})
        idx = rec.setdefault("indexing", {})
        ident["asset_id"] = new_id
        if asset.get("source_pack"):
            ident["source_pack"] = asset.get("source_pack")
        tech["unreal_asset_path"] = asset.get("ue_path") or tech.get("unreal_asset_path")
        tech["asset_type"] = asset.get("asset_type") or tech.get("asset_type")
        idx["asset_id_remap"] = {
            "from": old_id,
            "to": new_id,
            "source": "manifest_full.technical.unreal_asset_path",
            "remapped_at_utc": ts,
            "original_catalog_path": str(source),
            "manifest_ue_name": asset.get("ue_name"),
            "manifest_source_pack": asset.get("source_pack"),
        }
        render_info = remap_render_views(asset_db_dir, rec, old_id, new_id, dry_run)

        archived = archive_path(archive_root, source, asset_db_dir, "remapped_originals")
        if not dry_run:
            write_json_atomic(target, rec)
            move_to_archive(archive_root, source, asset_db_dir, "remapped_originals")
        applied.append({**item, "archive_path": str(archived), "render_info": render_info})

    for item in plan["conflict"]:
        source = pathlib.Path(item["path"])
        archived = archive_path(archive_root, source, asset_db_dir, "conflicts")
        if not dry_run:
            move_to_archive(archive_root, source, asset_db_dir, "conflicts")
        archived_conflicts.append({**item, "archive_path": str(archived)})

    return {
        "remapped": applied,
        "archived_conflicts": archived_conflicts,
        "old_asset_ids": [item["old_id"] for item in applied + archived_conflicts],
        "new_asset_ids": [item["new_id"] for item in applied],
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--asset-db-dir", default="/data/siddhant/asset_db")
    p.add_argument("--manifest", default="/data/siddhant/asset_db/manifest_full.json")
    p.add_argument("--archive-root", default="")
    p.add_argument("--report-dir", default="")
    p.add_argument("--apply", action="store_true", help="Apply changes. Without this, only writes a dry-run report.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    asset_db_dir = pathlib.Path(args.asset_db_dir)
    manifest_path = pathlib.Path(args.manifest)
    run_stamp = stamp()
    report_dir = pathlib.Path(args.report_dir) if args.report_dir else asset_db_dir / "catalog_id_remap_reports" / run_stamp
    archive_root = pathlib.Path(args.archive_root) if args.archive_root else asset_db_dir / "catalog_id_remap_archive" / run_stamp
    dry_run = not args.apply

    plan = plan_remap(asset_db_dir, manifest_path)
    result = apply_remap(
        asset_db_dir=asset_db_dir,
        manifest_path=manifest_path,
        plan=plan,
        archive_root=archive_root,
        dry_run=dry_run,
    )
    summary = {
        "ts": utc_now(),
        "mode": "dry_run" if dry_run else "apply",
        "asset_db_dir": str(asset_db_dir),
        "manifest": str(manifest_path),
        "report_dir": str(report_dir),
        "archive_root": str(archive_root),
        "catalog_files": plan["catalog_files"],
        "already_manifest": len(plan["already_manifest"]),
        "remap_count": len(plan["remap"]),
        "conflict_count": len(plan["conflict"]),
        "unmatched_count": len(plan["unmatched"]),
        "ambiguous_manifest_path_count": len(plan["ambiguous_manifest_path"]),
        "applied_remapped": len(result["remapped"]),
        "applied_archived_conflicts": len(result["archived_conflicts"]),
    }

    report_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(report_dir / "summary.json", summary)
    write_json_atomic(report_dir / "plan.json", plan)
    write_json_atomic(report_dir / "result.json", result)
    write_lines(report_dir / "remapped_new_asset_ids.txt", sorted(set(result["new_asset_ids"])))
    write_lines(report_dir / "remapped_old_asset_ids.txt", sorted(set(result["old_asset_ids"])))
    write_lines(report_dir / "unmatched_old_asset_ids.txt", sorted(item["old_id"] for item in plan["unmatched"]))
    write_lines(report_dir / "conflict_old_asset_ids.txt", sorted(item["old_id"] for item in plan["conflict"]))

    print(json.dumps(summary, indent=2))
    print(f"Report dir: {report_dir}")
    if dry_run:
        print("Dry run only. Re-run with --apply to mutate catalog files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
