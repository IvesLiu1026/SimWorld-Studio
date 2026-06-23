#!/usr/bin/env python3
"""Check asset_db manifest/catalog metadata for duplicate asset records."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_ASSET_DB_DIR = Path(__file__).resolve().parents[2] / "asset_db"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_value(value: Any) -> str:
    return str(value or "").strip().lower()


def add_group(groups: dict[str, list[Any]], key: Any, item: Any) -> None:
    normalized = normalize_value(key)
    if normalized:
        groups[normalized].append(item)


def duplicate_groups(groups: dict[str, list[Any]]) -> dict[str, list[Any]]:
    return {key: values for key, values in groups.items() if len(values) > 1}


def short_list(items: list[str], limit: int) -> str:
    shown = items[:limit]
    suffix = "" if len(items) <= limit else f" ... (+{len(items) - limit})"
    return ", ".join(shown) + suffix


def collect_manifest(manifest_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    manifest = load_json(manifest_path)
    assets = manifest.get("assets", [])
    errors: list[str] = []
    if not isinstance(assets, list):
        errors.append(f"{manifest_path}: expected top-level 'assets' list")
        return [], errors
    return [asset for asset in assets if isinstance(asset, dict)], errors


def collect_catalog(catalog_dir: Path) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    records: list[tuple[Path, dict[str, Any]]] = []
    errors: list[str] = []
    for path in sorted(catalog_dir.glob("*/*.json")):
        try:
            data = load_json(path)
        except Exception as exc:  # noqa: BLE001 - report all malformed records.
            errors.append(f"{path}: {exc}")
            continue
        if not isinstance(data, dict):
            errors.append(f"{path}: expected JSON object")
            continue
        records.append((path, data))
    return records, errors


def print_group_report(title: str, groups: dict[str, list[str]], limit: int) -> None:
    print(f"- {title}: {len(groups)} duplicate keys")
    for key, values in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]:
        print(f"  - {key}: {short_list(values, limit)}")
    if len(groups) > limit:
        print(f"  - ... {len(groups) - limit} more duplicate keys")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-db-dir", type=Path, default=DEFAULT_ASSET_DB_DIR)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=10, help="Max duplicate groups/examples to print")
    args = parser.parse_args()

    asset_db_dir = args.asset_db_dir.resolve()
    manifest_path = (args.manifest or asset_db_dir / "manifest_full.json").resolve()
    catalog_dir = asset_db_dir / "catalog"

    manifest_assets, manifest_errors = collect_manifest(manifest_path)
    catalog_records, catalog_errors = collect_catalog(catalog_dir)

    manifest_ids: dict[str, list[str]] = defaultdict(list)
    manifest_ue_paths: dict[str, list[str]] = defaultdict(list)
    manifest_pack_names: dict[str, list[str]] = defaultdict(list)
    manifest_id_set: set[str] = set()

    for asset in manifest_assets:
        asset_id = str(asset.get("asset_id") or "")
        source_pack = str(asset.get("source_pack") or "")
        ue_name = str(asset.get("ue_name") or "")
        ue_path = str(asset.get("ue_path") or "")
        label = asset_id or ue_path or "<missing id/path>"
        add_group(manifest_ids, asset_id, label)
        add_group(manifest_ue_paths, ue_path, label)
        add_group(manifest_pack_names, f"{source_pack}/{ue_name}", label)
        if asset_id:
            manifest_id_set.add(asset_id)

    catalog_file_stems: dict[str, list[str]] = defaultdict(list)
    catalog_identity_ids: dict[str, list[str]] = defaultdict(list)
    catalog_ue_paths: dict[str, list[str]] = defaultdict(list)
    catalog_orphans: list[str] = []
    catalog_id_mismatches: list[str] = []

    for path, record in catalog_records:
        identity = record.get("identity") or {}
        technical = record.get("technical") or {}
        asset_id = str(identity.get("asset_id") or "")
        ue_path = str(technical.get("unreal_asset_path") or "")
        rel_path = str(path.relative_to(asset_db_dir))
        add_group(catalog_file_stems, path.stem, rel_path)
        add_group(catalog_identity_ids, asset_id, rel_path)
        add_group(catalog_ue_paths, ue_path, asset_id or rel_path)
        if asset_id and asset_id != path.stem:
            catalog_id_mismatches.append(f"{rel_path} identity.asset_id={asset_id}")
        if asset_id and asset_id not in manifest_id_set:
            catalog_orphans.append(f"{asset_id} ({rel_path})")

    hard_checks = {
        "manifest duplicate asset_id": duplicate_groups(manifest_ids),
        "manifest duplicate ue_path": duplicate_groups(manifest_ue_paths),
        "catalog duplicate filename/stem": duplicate_groups(catalog_file_stems),
        "catalog duplicate identity.asset_id": duplicate_groups(catalog_identity_ids),
        "catalog duplicate technical.unreal_asset_path": duplicate_groups(catalog_ue_paths),
    }
    soft_checks = {
        "manifest same source_pack/ue_name": duplicate_groups(manifest_pack_names),
    }

    hard_count = sum(len(groups) for groups in hard_checks.values())

    print("Asset duplicate check")
    print(f"- asset_db: {asset_db_dir}")
    print(f"- manifest assets: {len(manifest_assets)} ({manifest_path.name})")
    print(f"- catalog records: {len(catalog_records)}")
    print("")

    if manifest_errors or catalog_errors:
        print("Read errors:")
        for error in (manifest_errors + catalog_errors)[: args.limit]:
            print(f"- {error}")
        if len(manifest_errors) + len(catalog_errors) > args.limit:
            print(f"- ... {len(manifest_errors) + len(catalog_errors) - args.limit} more errors")
        print("")

    for title, groups in hard_checks.items():
        print_group_report(title, groups, args.limit)
    print("")

    print(f"- catalog id/file mismatches: {len(catalog_id_mismatches)}")
    for item in catalog_id_mismatches[: args.limit]:
        print(f"  - {item}")
    if len(catalog_id_mismatches) > args.limit:
        print(f"  - ... {len(catalog_id_mismatches) - args.limit} more")

    print(f"- catalog ids not in manifest: {len(catalog_orphans)}")
    for item in catalog_orphans[: args.limit]:
        print(f"  - {item}")
    if len(catalog_orphans) > args.limit:
        print(f"  - ... {len(catalog_orphans) - args.limit} more")
    print("")

    for title, groups in soft_checks.items():
        print_group_report(f"soft check: {title}", groups, args.limit)

    if hard_count == 0:
        print("\nResult: no hard duplicates found.")
    else:
        print(f"\nResult: found {hard_count} hard duplicate key groups.")

    return 1 if hard_count or manifest_errors or catalog_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
