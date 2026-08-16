"""Independent structural inspection for r2 GLB and manifest outputs."""

from __future__ import annotations

import argparse
import json
import pathlib
import struct
from typing import Any, Sequence

from .config import ForgeInputError, canonical_json_bytes, sha256_file


GLB_MAGIC = 0x46546C67
GLB_JSON_CHUNK = 0x4E4F534A


def inspect_glb(path: pathlib.Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        header = handle.read(12)
        if len(header) != 12:
            raise ForgeInputError(f"truncated GLB header: {path}")
        magic, version, total_length = struct.unpack("<III", header)
        if magic != GLB_MAGIC or version != 2 or total_length != path.stat().st_size:
            raise ForgeInputError(f"invalid GLB 2.0 header: {path}")
        chunk_header = handle.read(8)
        if len(chunk_header) != 8:
            raise ForgeInputError(f"missing GLB JSON chunk: {path}")
        chunk_length, chunk_type = struct.unpack("<II", chunk_header)
        if chunk_type != GLB_JSON_CHUNK:
            raise ForgeInputError(f"first GLB chunk is not JSON: {path}")
        document = json.loads(handle.read(chunk_length).decode("utf-8").rstrip(" \t\r\n\x00"))
    nodes = document.get("nodes", [])
    extras = [node.get("extras", {}) for node in nodes if isinstance(node, dict)]
    component_extras = [item for item in extras if isinstance(item, dict) and item.get("vista_component_id")]
    return {
        "relative_or_absolute_path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "asset_version": document.get("asset", {}).get("version"),
        "scene_count": len(document.get("scenes", [])),
        "node_count": len(nodes),
        "mesh_count": len(document.get("meshes", [])),
        "material_count": len(document.get("materials", [])),
        "image_count": len(document.get("images", [])),
        "texture_count": len(document.get("textures", [])),
        "camera_count": len(document.get("cameras", [])),
        "component_extra_count": len(component_extras),
        "component_roles": sorted({str(item.get("vista_export_role")) for item in component_extras}),
        "extensions_used": sorted(document.get("extensionsUsed", [])),
        "extensions_required": sorted(document.get("extensionsRequired", [])),
    }


def inspect_output(output_root: pathlib.Path) -> dict[str, Any]:
    output_root = output_root.resolve(strict=True)
    manifest_path = output_root / "normalized-manifest.json"
    artifact_path = output_root / "artifact-receipt.json"
    if not manifest_path.is_file() or not artifact_path.is_file():
        raise ForgeInputError("output root is missing normalized-manifest.json or artifact-receipt.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt = json.loads(artifact_path.read_text(encoding="utf-8"))
    components = manifest.get("components", [])
    if not isinstance(components, list) or len(components) < 60:
        raise ForgeInputError("normalized manifest has insufficient architectural components")
    required_roles = {"architecture_shell", "architectural_detail", "cabinetry"}
    if not required_roles.issubset(set(manifest.get("role_counts", {}))):
        raise ForgeInputError("normalized manifest is missing required export roles")
    glbs: list[dict[str, Any]] = []
    for artifact in receipt.get("artifacts", []):
        if artifact.get("media_type") != "model/gltf-binary":
            continue
        relative_path = artifact.get("relative_path")
        if not isinstance(relative_path, str) or not relative_path:
            raise ForgeInputError("GLB artifact relative_path must be a non-empty string")
        relative = pathlib.PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts or "\\" in relative_path:
            raise ForgeInputError(f"unsafe GLB artifact relative_path: {relative_path!r}")
        path = (output_root / pathlib.Path(*relative.parts)).resolve(strict=True)
        if not path.is_relative_to(output_root) or path.is_symlink() or not path.is_file():
            raise ForgeInputError(f"GLB artifact escapes output root or is not a regular file: {relative_path!r}")
        inspection = inspect_glb(path)
        if inspection["camera_count"] != 0:
            raise ForgeInputError(f"production GLB unexpectedly contains cameras: {path}")
        if inspection["component_extra_count"] == 0:
            raise ForgeInputError(f"production GLB lacks presentation role metadata: {path}")
        # ``inspect_glb`` remains useful as a standalone diagnostic and may
        # identify the caller-provided path.  Persistent build receipts must
        # never bind a host-private attempt root, so normalize at this boundary.
        inspection.pop("relative_or_absolute_path", None)
        inspection["relative_path"] = relative.as_posix()
        glbs.append(inspection)
    return {
        "schema_version": "simworld.vista.playable-home-realism-inspection/v1",
        "forge_plan_digest": manifest.get("forge_plan_digest"),
        "build_quality": manifest.get("build_quality"),
        "component_count": len(components),
        "glbs": glbs,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=pathlib.Path, required=True)
    parser.add_argument("--write", type=pathlib.Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    root = args.output_root.resolve(strict=True)
    result = inspect_output(root)
    payload = canonical_json_bytes(result)
    if args.write:
        args.write.write_bytes(payload)
    else:
        print(payload.decode("utf-8"), end="")


if __name__ == "__main__":
    main()
