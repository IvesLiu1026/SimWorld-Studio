#!/usr/bin/env python3
"""Build the strict UE 5.8 object-only asset manifest.

This queries the running UE 5.8 legacy TCP bridge, asks Unreal's AssetRegistry
for /Game assets, filters to actual object candidates, and writes a manifest in
the same shape as asset_db/manifest_full.json.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import socket
import tempfile
from collections import Counter
from typing import Any


DEFAULT_OUT = pathlib.Path("/data/siddhant/asset_db/ue58_object_manifest.json")
DEFAULT_REJECTS = pathlib.Path("/data/siddhant/asset_db/ue58_object_manifest_rejects.jsonl")
CONTENT_ROOT = "/data/koe/simworld-content-store/current/Content"

NOISE_PATH_TERMS = [
    "/__externalactors__/",
    "/__externalobjects__/",
    "/texture",
    "/textures",
    "/material",
    "/materials",
    "/function",
    "/functions",
    "/maps/",
    "/map/",
    "/demo",
    "/test",
    "/editor",
    "/overview",
    "/builtdata",
    "/_builtdata",
    "/render",
    "/renders",
    "/thumbnail",
    "/thumbnails",
]

SURFACE_TERMS = [
    "floor",
    "ground",
    "road",
    "sidewalk",
    "pavement",
    "asphalt",
    "terrain",
    "landscape",
    "grass",
    "wall",
    "ceiling",
    "roof",
    "facade",
    "decal",
    "trim",
    "molding",
    "tile",
    "tiles",
    "plane",
    "platform",
    "stairs",
    "stair",
    "railing",
    "curb",
    "line",
    "marking",
    "window",
    "doorframe",
    "column",
    "beam",
    "arch",
    "corner",
    "ledge",
    "baseboard",
    "skirting",
]

POSITIVE_OBJECT_TERMS = [
    "barrel",
    "barrier",
    "bench",
    "bin",
    "bollard",
    "bottle",
    "box",
    "bus",
    "can",
    "car",
    "cart",
    "chair",
    "cone",
    "couch",
    "crate",
    "fence",
    "fountain",
    "hydrant",
    "lamp",
    "light",
    "plant",
    "pot",
    "prop",
    "rack",
    "rock",
    "scooter",
    "shelf",
    "sign",
    "sofa",
    "statue",
    "table",
    "trash",
    "tree",
    "vehicle",
]

HELPER_TERMS = [
    "controller",
    "manager",
    "generator",
    "customizer",
    "template",
    "proxy",
    "preview",
    "helper",
    "parent",
    "master",
    "abstract",
    "bp_fluid",
    "foliagetype",
    "spawner",
]

CHARACTER_TERMS = [
    "character",
    "crowd",
    "npc",
    "pedestrian",
    "mannequin",
    "body",
    "head",
    "hair",
    "cloth",
    "clothes",
    "skeleton",
    "anim",
    "groom",
    "metahuman",
    "vrhasian",
    "vrhm_urban_npc",
    "citizennpc",
]

CHARACTER_ROOTS = [
    "CitySampleCrowd",
    "CitizenNPC",
    "Human_Avatar",
    "VRHAsian",
    "VRHM_Urban_NPC",
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_id(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "asset"


def make_asset_id(row: dict[str, Any], seen: set[str]) -> str:
    root = safe_id(row.get("root") or "game")
    name = safe_id(row.get("name") or pathlib.PurePosixPath(row["package"]).name)
    base = f"{root}_{name}"
    if base not in seen:
        seen.add(base)
        return base
    digest = hashlib.sha1(str(row["package"]).encode("utf-8")).hexdigest()[:8]
    asset_id = f"{base}_{digest}"
    seen.add(asset_id)
    return asset_id


def send_ue_script(host: str, port: int, script: str, timeout: int) -> dict[str, Any]:
    payload = {"type": "execute_python_script", "params": {"script": script}}
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        sock.settimeout(timeout)
        buf = b""
        while True:
            part = sock.recv(65536)
            if not part:
                break
            buf += part
            try:
                return json.loads(buf.decode("utf-8"))
            except json.JSONDecodeError:
                continue
    raise RuntimeError("UE bridge closed without a complete JSON response")


def logs(resp: dict[str, Any]) -> list[str]:
    return (resp.get("result") or {}).get("python_logs") or []


def build_ue_script(tmp_path: str) -> str:
    return f"""
import collections, json, re, unreal

tmp_path = {tmp_path!r}
noise_path_terms = {NOISE_PATH_TERMS!r}
surface_terms = {SURFACE_TERMS!r}
positive_object_terms = {POSITIVE_OBJECT_TERMS!r}
helper_terms = {HELPER_TERMS!r}
character_terms = {CHARACTER_TERMS!r}
character_roots = {CHARACTER_ROOTS!r}
include_classes = {{"StaticMesh", "Blueprint", "SkeletalMesh"}}

def _tokens(value):
    value = re.sub(r"([a-z0-9])([A-Z])", r"\\1_\\2", value)
    value = re.sub(r"([A-Za-z])([0-9])", r"\\1_\\2", value)
    value = re.sub(r"([0-9])([A-Za-z])", r"\\1_\\2", value)
    return set(re.findall(r"[a-z0-9]+", value.lower()))

reg = unreal.AssetRegistryHelpers.get_asset_registry()
try:
    reg.search_all_assets(True)
except Exception:
    pass

accepted = []
rejected = []
class_counts = collections.Counter()
reject_counts = collections.Counter()
root_counts = collections.Counter()
assets = reg.get_assets_by_path("/Game", recursive=True, include_only_on_disk_assets=True)

for asset in assets:
    pkg = str(asset.package_name)
    name = str(asset.asset_name)
    try:
        cls = str(asset.asset_class_path.asset_name)
    except Exception:
        try:
            cls = str(asset.asset_class)
        except Exception:
            cls = "unknown"
    if cls not in include_classes:
        continue
    parts = pkg.split("/")
    root = parts[2] if len(parts) > 2 else ""
    low = (pkg + "/" + name).lower()
    tokens = _tokens(pkg + "/" + name)
    class_counts[cls] += 1

    reason = None
    if any(term in low for term in noise_path_terms):
        reason = "noise_path"
    elif tokens.intersection(helper_terms):
        reason = "helper_system"
    elif root in character_roots or tokens.intersection(character_terms):
        reason = "character_or_bodypart"
    elif cls == "SkeletalMesh":
        reason = "skeletal_separate"
    elif tokens.intersection(surface_terms) and not tokens.intersection(positive_object_terms):
        reason = "surface_modular_shell"

    row = {{"package": pkg, "name": name, "class": cls, "root": root}}
    if reason:
        row["reject_reason"] = reason
        rejected.append(row)
        reject_counts[reason] += 1
        continue
    accepted.append(row)
    root_counts[root] += 1

result = {{
    "generated_at_utc": {utc_now()!r},
    "total_asset_registry_spawnable_classes": sum(class_counts.values()),
    "class_counts": dict(class_counts),
    "accepted_count": len(accepted),
    "accepted_root_counts": dict(root_counts),
    "reject_counts": dict(reject_counts),
    "accepted": accepted,
    "rejected": rejected,
}}
with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False)
print("UE58_OBJECT_MANIFEST_TMP:" + tmp_path)
print("UE58_OBJECT_MANIFEST_SUMMARY:" + json.dumps({{
    "accepted_count": len(accepted),
    "class_counts": dict(class_counts),
    "reject_counts": dict(reject_counts),
}}))
"""


def write_json(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_rejects(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def build_manifest(raw: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    assets: list[dict[str, Any]] = []
    packs: Counter[str] = Counter()
    class_counts: Counter[str] = Counter()
    for row in sorted(raw["accepted"], key=lambda r: (r.get("root") or "", r.get("package") or "")):
        asset_id = make_asset_id(row, seen)
        package = str(row["package"])
        name = str(row["name"])
        cls = str(row["class"])
        root = str(row.get("root") or "")
        assets.append(
            {
                "asset_id": asset_id,
                "ue_name": name,
                "ue_path": f"{package}.{name}",
                "asset_type": cls,
                "source_pack": root,
                "indexed": False,
                "filter_tags": ["ue58", "actual_object_candidate"],
            }
        )
        packs[root] += 1
        class_counts[cls] += 1

    return {
        "schema_version": "1.0",
        "manifest_kind": "ue58_actual_object_candidates",
        "generated_at_utc": utc_now(),
        "content_root": CONTENT_ROOT,
        "count": len(assets),
        "packs": dict(sorted(packs.items())),
        "asset_class_counts": dict(sorted(class_counts.items())),
        "filter": {
            "include_classes": ["StaticMesh", "Blueprint"],
            "excluded_classes": ["SkeletalMesh"],
            "noise_path_terms": NOISE_PATH_TERMS,
            "surface_terms": SURFACE_TERMS,
            "positive_object_terms": POSITIVE_OBJECT_TERMS,
            "helper_terms": HELPER_TERMS,
            "character_terms": CHARACTER_TERMS,
            "character_roots": CHARACTER_ROOTS,
        },
        "source_audit": {
            "total_asset_registry_spawnable_classes": raw.get("total_asset_registry_spawnable_classes"),
            "class_counts_before_filter": raw.get("class_counts"),
            "reject_counts": raw.get("reject_counts"),
            "accepted_root_counts": raw.get("accepted_root_counts"),
        },
        "assets": assets,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=55568)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--rejects-out", default=str(DEFAULT_REJECTS))
    parser.add_argument("--keep-ue-temp", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out = pathlib.Path(args.out)
    rejects_out = pathlib.Path(args.rejects_out)
    tmp_path = pathlib.Path(tempfile.gettempdir()) / f"ue58_object_manifest_raw_{os.getpid()}.json"
    resp = send_ue_script(args.host, args.port, build_ue_script(str(tmp_path)), args.timeout)
    if resp.get("status") != "success":
        raise SystemExit(f"UE bridge returned error: {json.dumps(resp)[:2000]}")
    if not tmp_path.exists():
        raise SystemExit(f"UE did not write expected temp file: {tmp_path}; logs={logs(resp)}")
    raw = json.loads(tmp_path.read_text(encoding="utf-8"))
    manifest = build_manifest(raw)
    write_json(out, manifest)
    write_rejects(rejects_out, raw.get("rejected") or [])
    if not args.keep_ue_temp:
        try:
            tmp_path.unlink()
        except OSError:
            pass
    print(f"wrote manifest: {out} ({manifest['count']} assets)")
    print(f"wrote rejects:  {rejects_out} ({len(raw.get('rejected') or [])} rows)")
    print(f"class counts:   {manifest['asset_class_counts']}")
    print(f"reject counts:  {manifest['source_audit']['reject_counts']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
